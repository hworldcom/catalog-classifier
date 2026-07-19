from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient, Response as HttpxResponse
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from catalog_api.approved_image_exports import (
    APPROVED_IMAGE_EXPORT_ENABLED_ENV,
    ApprovedImageExportDisabledError,
    read_approved_normalized_image,
)
from catalog_api.image_processing import JPEG_CONTENT_TYPE
from catalog_api.main import app
from catalog_api.models import (
    ImageAsset,
    Organization,
    ProductGroup,
    ProductGroupImage,
    ReviewEvent,
    UploadBatch,
)
from catalog_api.processing_storage import (
    WorkerObjectNotFoundError,
    WorkerObjectReadError,
    get_worker_storage,
)
from catalog_api.upload_batches import DEFAULT_ORGANIZATION_ID

pytestmark = [pytest.mark.anyio, pytest.mark.postgresql]

PIPELINE_VERSION = "2026-06-01"
NORMALIZED_BYTES = b"\xff\xd8approved-normalized-image\xff\xd9"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeWorkerStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.read_error_keys: set[str] = set()
        self.reads: list[str] = []

    def read_object_bytes(self, *, object_key: str) -> bytes:
        self.reads.append(object_key)
        if object_key in self.read_error_keys:
            raise WorkerObjectReadError
        try:
            return self.objects[object_key]
        except KeyError as error:
            raise WorkerObjectNotFoundError from error

    def write_object_bytes(
        self,
        *,
        object_key: str,
        content_type: str,
        data: bytes,
    ) -> None:
        raise AssertionError("Approved image export must not write storage.")


@pytest.fixture
def fake_worker_storage() -> Iterator[FakeWorkerStorage]:
    storage = FakeWorkerStorage()
    app.dependency_overrides[get_worker_storage] = lambda: storage
    try:
        yield storage
    finally:
        app.dependency_overrides.pop(get_worker_storage, None)


@dataclass(frozen=True)
class ApprovedImageFixture:
    organization_id: UUID
    batch_id: UUID
    group_id: UUID
    image_id: UUID
    normalized_object_key: str
    original_object_key: str
    thumbnail_object_key: str


def _seed_approved_image(
    session: Session,
    *,
    organization_id: UUID = DEFAULT_ORGANIZATION_ID,
) -> ApprovedImageFixture:
    batch = UploadBatch(
        organization_id=organization_id,
        status="approved",
        original_file_count=1,
        processed_file_count=1,
        pipeline_version=PIPELINE_VERSION,
        finalized_at=datetime(2026, 7, 19, 9, 0, tzinfo=UTC),
        completed_at=datetime(2026, 7, 19, 9, 30, tzinfo=UTC),
    )
    session.add(batch)
    session.flush()

    image_id = uuid4()
    prefix = (
        f"organizations/{organization_id}/batches/{batch.id}/"
        f"derived/{PIPELINE_VERSION}/{image_id}"
    )
    original_object_key = (
        f"organizations/{organization_id}/batches/{batch.id}/"
        f"originals/{image_id}.jpg"
    )
    normalized_object_key = f"{prefix}/normalized.jpg"
    thumbnail_object_key = f"{prefix}/thumbnail.jpg"
    image = ImageAsset(
        id=image_id,
        organization_id=organization_id,
        batch_id=batch.id,
        original_object_key=original_object_key,
        normalized_object_key=normalized_object_key,
        thumbnail_object_key=thumbnail_object_key,
        original_filename="approved.jpg",
        upload_order=0,
        mime_type=JPEG_CONTENT_TYPE,
        size_bytes=100,
        normalized_format=JPEG_CONTENT_TYPE,
        normalized_size_bytes=len(NORMALIZED_BYTES),
        status="processed",
    )
    session.add(image)
    session.flush()

    group = ProductGroup(
        organization_id=organization_id,
        batch_id=batch.id,
        status="approved",
        cover_image_id=image.id,
        confidence=1.0,
        approved_at=datetime(2026, 7, 19, 9, 25, tzinfo=UTC),
    )
    session.add(group)
    session.flush()
    session.add(
        ProductGroupImage(
            organization_id=organization_id,
            batch_id=batch.id,
            group_id=group.id,
            image_id=image.id,
            position=0,
            membership_source="singleton",
            membership_confidence=None,
        )
    )
    session.commit()
    return ApprovedImageFixture(
        organization_id=organization_id,
        batch_id=batch.id,
        group_id=group.id,
        image_id=image.id,
        normalized_object_key=normalized_object_key,
        original_object_key=original_object_key,
        thumbnail_object_key=thumbnail_object_key,
    )


def _endpoint(fixture: ApprovedImageFixture) -> str:
    return (
        f"/internal/v1/export/batches/{fixture.batch_id}/"
        f"groups/{fixture.group_id}/images/{fixture.image_id}/normalized"
    )


def _enable_export(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(APPROVED_IMAGE_EXPORT_ENABLED_ENV, "true")


async def test_approved_image_export_returns_normalized_jpeg_and_is_read_only(
    database_client: AsyncClient,
    migrated_engine: Engine,
    fake_worker_storage: FakeWorkerStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_export(monkeypatch)
    with Session(migrated_engine) as session:
        fixture = _seed_approved_image(session)
        before_counts = _export_row_counts(session)
    fake_worker_storage.objects[fixture.normalized_object_key] = NORMALIZED_BYTES

    first_response = await database_client.get(_endpoint(fixture))
    second_response = await database_client.get(_endpoint(fixture))

    assert first_response.status_code == 200
    assert first_response.content == NORMALIZED_BYTES
    assert first_response.headers["content-type"] == JPEG_CONTENT_TYPE
    assert first_response.headers["content-length"] == str(len(NORMALIZED_BYTES))
    assert first_response.headers["cache-control"] == "no-store"
    assert second_response.status_code == 200
    assert second_response.content == NORMALIZED_BYTES
    assert fake_worker_storage.reads == [
        fixture.normalized_object_key,
        fixture.normalized_object_key,
    ]
    assert fixture.original_object_key not in fake_worker_storage.reads
    assert fixture.thumbnail_object_key not in fake_worker_storage.reads
    with Session(migrated_engine) as session:
        assert _export_row_counts(session) == before_counts


async def test_approved_image_export_is_disabled_before_database_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(APPROVED_IMAGE_EXPORT_ENABLED_ENV, raising=False)

    class QueryFailingSession:
        def scalar(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("Disabled export must not query the database.")

    storage = FakeWorkerStorage()
    with pytest.raises(ApprovedImageExportDisabledError):
        read_approved_normalized_image(
            cast(Session, QueryFailingSession()),
            batch_id=uuid4(),
            group_id=uuid4(),
            image_id=uuid4(),
            storage=storage,
        )
    assert storage.reads == []


async def test_disabled_approved_image_route_returns_stable_404(
    database_client: AsyncClient,
    fake_worker_storage: FakeWorkerStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(APPROVED_IMAGE_EXPORT_ENABLED_ENV, raising=False)

    response = await database_client.get(
        "/internal/v1/export/"
        f"batches/{uuid4()}/groups/{uuid4()}/images/{uuid4()}/normalized"
    )

    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["detail"] == {
        "code": "approved_image_export_disabled",
        "message": "Approved image export is not enabled.",
    }
    assert fake_worker_storage.reads == []


async def test_approved_image_export_returns_404_for_unknown_batch(
    database_client: AsyncClient,
    fake_worker_storage: FakeWorkerStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_export(monkeypatch)

    response = await database_client.get(
        "/internal/v1/export/"
        f"batches/{uuid4()}/groups/{uuid4()}/images/{uuid4()}/normalized"
    )

    _assert_not_found(response)
    assert fake_worker_storage.reads == []


async def test_approved_image_export_hides_other_organizations(
    database_client: AsyncClient,
    migrated_engine: Engine,
    fake_worker_storage: FakeWorkerStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_export(monkeypatch)
    with Session(migrated_engine) as session:
        foreign_organization = Organization(name="Foreign organization")
        session.add(foreign_organization)
        session.flush()
        fixture = _seed_approved_image(
            session,
            organization_id=foreign_organization.id,
        )

    response = await database_client.get(_endpoint(fixture))

    _assert_not_found(response)
    assert fake_worker_storage.reads == []


@pytest.mark.parametrize(
    "state",
    ["batch_not_approved", "group_not_approved"],
)
async def test_approved_image_export_requires_approved_batch_and_group(
    database_client: AsyncClient,
    migrated_engine: Engine,
    fake_worker_storage: FakeWorkerStorage,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    _enable_export(monkeypatch)
    with Session(migrated_engine) as session:
        fixture = _seed_approved_image(session)
        if state == "batch_not_approved":
            batch = session.get(UploadBatch, fixture.batch_id)
            assert batch is not None
            batch.status = "review_required"
        else:
            group = session.get(ProductGroup, fixture.group_id)
            assert group is not None
            group.status = "proposed"
        session.commit()

    response = await database_client.get(_endpoint(fixture))

    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["detail"] == {
        "code": "approved_image_not_approved",
        "message": (
            "Approved image export requires an approved batch and group."
        ),
    }
    assert fake_worker_storage.reads == []


@pytest.mark.parametrize(
    "ineligible_case",
    ["unknown_group", "unknown_image", "rejected", "duplicate"],
)
async def test_approved_image_export_hides_unknown_and_ineligible_resources(
    database_client: AsyncClient,
    migrated_engine: Engine,
    fake_worker_storage: FakeWorkerStorage,
    monkeypatch: pytest.MonkeyPatch,
    ineligible_case: str,
) -> None:
    _enable_export(monkeypatch)
    with Session(migrated_engine) as session:
        fixture = _seed_approved_image(session)
        if ineligible_case in {"rejected", "duplicate"}:
            membership = session.get(
                ProductGroupImage,
                (fixture.group_id, fixture.image_id),
            )
            assert membership is not None
            if ineligible_case == "rejected":
                membership.is_rejected = True
            else:
                retained_image_id = _add_retained_image(
                    session,
                    fixture=fixture,
                )
                membership.is_duplicate = True
                membership.duplicate_of_image_id = retained_image_id
            session.commit()

    if ineligible_case == "unknown_group":
        endpoint = (
            f"/internal/v1/export/batches/{fixture.batch_id}/groups/{uuid4()}/"
            f"images/{fixture.image_id}/normalized"
        )
    elif ineligible_case == "unknown_image":
        endpoint = (
            f"/internal/v1/export/batches/{fixture.batch_id}/"
            f"groups/{fixture.group_id}/images/{uuid4()}/normalized"
        )
    else:
        endpoint = _endpoint(fixture)

    response = await database_client.get(endpoint)

    _assert_not_found(response)
    assert fake_worker_storage.reads == []


@pytest.mark.parametrize(
    "invalid_case",
    [
        "not_processed",
        "missing_object_key",
        "blank_object_key",
        "wrong_format",
        "missing_size",
        "zero_size",
    ],
)
async def test_approved_image_export_rejects_invalid_normalized_metadata(
    database_client: AsyncClient,
    migrated_engine: Engine,
    fake_worker_storage: FakeWorkerStorage,
    monkeypatch: pytest.MonkeyPatch,
    invalid_case: str,
) -> None:
    _enable_export(monkeypatch)
    with Session(migrated_engine) as session:
        fixture = _seed_approved_image(session)
        image = session.get(ImageAsset, fixture.image_id)
        assert image is not None
        if invalid_case == "not_processed":
            image.status = "failed"
        elif invalid_case == "missing_object_key":
            image.normalized_object_key = None
        elif invalid_case == "blank_object_key":
            image.normalized_object_key = " "
        elif invalid_case == "wrong_format":
            image.normalized_format = "image/png"
        elif invalid_case == "missing_size":
            image.normalized_size_bytes = None
        elif invalid_case == "zero_size":
            image.normalized_size_bytes = 0
        else:
            raise AssertionError(f"Unsupported invalid case: {invalid_case}")
        session.commit()

    response = await database_client.get(_endpoint(fixture))

    _assert_unavailable(response)
    assert fake_worker_storage.reads == []


@pytest.mark.parametrize(
    "storage_case",
    ["missing", "read_error", "empty", "length_mismatch"],
)
async def test_approved_image_export_sanitizes_storage_failures(
    database_client: AsyncClient,
    migrated_engine: Engine,
    fake_worker_storage: FakeWorkerStorage,
    monkeypatch: pytest.MonkeyPatch,
    storage_case: str,
) -> None:
    _enable_export(monkeypatch)
    with Session(migrated_engine) as session:
        fixture = _seed_approved_image(session)

    if storage_case == "read_error":
        fake_worker_storage.read_error_keys.add(fixture.normalized_object_key)
    elif storage_case == "empty":
        fake_worker_storage.objects[fixture.normalized_object_key] = b""
    elif storage_case == "length_mismatch":
        fake_worker_storage.objects[fixture.normalized_object_key] = b"short"
    elif storage_case != "missing":
        raise AssertionError(f"Unsupported storage case: {storage_case}")

    response = await database_client.get(_endpoint(fixture))

    _assert_unavailable(response)
    assert fake_worker_storage.reads == [fixture.normalized_object_key]
    assert fixture.normalized_object_key not in response.text


def _add_retained_image(
    session: Session,
    *,
    fixture: ApprovedImageFixture,
) -> UUID:
    retained_image_id = uuid4()
    session.add(
        ImageAsset(
            id=retained_image_id,
            organization_id=fixture.organization_id,
            batch_id=fixture.batch_id,
            original_object_key=f"qa/0024a/original/{retained_image_id}.jpg",
            original_filename="retained.jpg",
            upload_order=1,
            mime_type=JPEG_CONTENT_TYPE,
            size_bytes=100,
            status="processed",
        )
    )
    session.add(
        ProductGroupImage(
            organization_id=fixture.organization_id,
            batch_id=fixture.batch_id,
            group_id=fixture.group_id,
            image_id=retained_image_id,
            position=1,
            membership_source="engine",
            membership_confidence=1.0,
        )
    )
    return retained_image_id


def _assert_not_found(response: HttpxResponse) -> None:
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["detail"] == {
        "code": "approved_image_not_found",
        "message": "Approved image was not found.",
    }


def _assert_unavailable(response: HttpxResponse) -> None:
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["detail"] == {
        "code": "approved_image_unavailable",
        "message": (
            "The approved normalized image is temporarily unavailable."
        ),
    }


def _export_row_counts(session: Session) -> tuple[int, int, int]:
    return (
        session.scalar(select(func.count()).select_from(ProductGroup)) or 0,
        session.scalar(select(func.count()).select_from(ProductGroupImage)) or 0,
        session.scalar(select(func.count()).select_from(ReviewEvent)) or 0,
    )
