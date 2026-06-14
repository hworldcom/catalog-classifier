from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from catalog_api.main import app
from catalog_api.models import ImageAsset, UploadBatch
from catalog_api.upload_storage import (
    UploadObjectInspectionError,
    UploadObjectMetadata,
    UploadObjectNotFoundError,
    get_upload_object_inspector,
    get_upload_url_signer,
)

pytestmark = [pytest.mark.anyio, pytest.mark.postgresql]


class FakeUploadUrlSigner:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []

    def sign_upload_url(self, *, object_key: str, content_type: str) -> str:
        self.requests.append((object_key, content_type))
        return f"https://uploads.example.test/{object_key}"


class FakeUploadObjectInspector:
    def __init__(
        self,
        *,
        metadata_by_key: dict[str, UploadObjectMetadata] | None = None,
        missing_keys: set[str] | None = None,
        failing_keys: set[str] | None = None,
    ) -> None:
        self.metadata_by_key = metadata_by_key or {}
        self.missing_keys = missing_keys or set()
        self.failing_keys = failing_keys or set()
        self.requests: list[str] = []

    def inspect_object(self, *, object_key: str) -> UploadObjectMetadata:
        self.requests.append(object_key)
        if object_key in self.failing_keys:
            raise UploadObjectInspectionError
        if object_key in self.missing_keys:
            raise UploadObjectNotFoundError
        return self.metadata_by_key[object_key]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def fake_signer() -> Iterator[FakeUploadUrlSigner]:
    signer = FakeUploadUrlSigner()
    app.dependency_overrides[get_upload_url_signer] = lambda: signer
    try:
        yield signer
    finally:
        app.dependency_overrides.pop(get_upload_url_signer, None)


async def _create_batch(client: AsyncClient) -> UUID:
    response = await client.post("/v1/upload-batches")
    assert response.status_code == 200
    return UUID(response.json()["batchId"])


async def _register_batch(
    client: AsyncClient,
    batch_id: UUID,
    *,
    payload: list[dict[str, object]],
) -> dict[str, object]:
    response = await client.post(
        f"/v1/upload-batches/{batch_id}/uploads",
        json={"files": payload},
    )
    assert response.status_code == 200
    return response.json()


def _override_inspector(
    inspector: FakeUploadObjectInspector,
) -> None:
    app.dependency_overrides[get_upload_object_inspector] = lambda: inspector


def _clear_inspector_override() -> None:
    app.dependency_overrides.pop(get_upload_object_inspector, None)


async def test_get_upload_batch_returns_ordered_images(
    database_client: AsyncClient,
    fake_signer: FakeUploadUrlSigner,
) -> None:
    batch_id = await _create_batch(database_client)
    await _register_batch(
        database_client,
        batch_id,
        payload=[
            {
                "originalFilename": "b.jpg",
                "mimeType": "image/jpeg",
                "sizeBytes": 200,
            },
            {
                "originalFilename": "a.jpg",
                "mimeType": "image/jpeg",
                "sizeBytes": 100,
            },
        ],
    )

    response = await database_client.get(f"/v1/upload-batches/{batch_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["batchId"] == str(batch_id)
    assert body["status"] == "uploading"
    assert body["originalFileCount"] == 2
    assert body["processedFileCount"] == 0
    assert body["finalizedAt"] is None
    assert body["completedAt"] is None
    assert [image["uploadOrder"] for image in body["images"]] == [0, 1]
    assert [image["originalFilename"] for image in body["images"]] == [
        "b.jpg",
        "a.jpg",
    ]
    assert all(image["status"] == "pending" for image in body["images"])
    assert all(image["errorCode"] is None for image in body["images"])
    assert all(image["errorMessage"] is None for image in body["images"])

    assert len(fake_signer.requests) == 2


async def test_finalize_upload_batch_queues_batch_when_all_objects_verify(
    database_client: AsyncClient,
    migrated_engine: Engine,
    fake_signer: FakeUploadUrlSigner,
) -> None:
    batch_id = await _create_batch(database_client)
    registration = await _register_batch(
        database_client,
        batch_id,
        payload=[
            {
                "originalFilename": "front.jpg",
                "mimeType": "image/jpeg",
                "sizeBytes": 100,
            },
            {
                "originalFilename": "back.jpg",
                "mimeType": "image/jpeg",
                "sizeBytes": 200,
            },
        ],
    )
    uploads = registration["uploads"]
    inspector = FakeUploadObjectInspector(
        metadata_by_key={
            upload["originalObjectKey"]: UploadObjectMetadata(
                content_type="image/jpeg",
                size_bytes=100 if upload["uploadOrder"] == 0 else 200,
            )
            for upload in uploads
        }
    )
    _override_inspector(inspector)

    try:
        response = await database_client.post(
            f"/v1/upload-batches/{batch_id}/finalize",
        )
    finally:
        _clear_inspector_override()

    assert response.status_code == 200
    body = response.json()
    assert body["batchId"] == str(batch_id)
    assert body["status"] == "queued"
    assert body["finalizedAt"] is not None
    assert body["completedAt"] is None
    assert [image["status"] for image in body["images"]] == [
        "uploaded",
        "uploaded",
    ]
    assert all(image["errorCode"] is None for image in body["images"])
    assert all(image["errorMessage"] is None for image in body["images"])
    assert inspector.requests == [upload["originalObjectKey"] for upload in uploads]

    with Session(migrated_engine) as session:
        batch = session.get(UploadBatch, batch_id)
        images = session.scalars(
            select(ImageAsset)
            .where(ImageAsset.batch_id == batch_id)
            .order_by(ImageAsset.upload_order)
        ).all()

    assert batch is not None
    assert batch.status == "queued"
    assert batch.finalized_at is not None
    assert batch.completed_at is None
    assert [image.status for image in images] == ["uploaded", "uploaded"]
    assert all(image.error_code is None for image in images)
    assert all(image.error_message is None for image in images)


async def test_finalize_upload_batch_persists_partial_failures(
    database_client: AsyncClient,
    migrated_engine: Engine,
    fake_signer: FakeUploadUrlSigner,
) -> None:
    batch_id = await _create_batch(database_client)
    registration = await _register_batch(
        database_client,
        batch_id,
        payload=[
            {
                "originalFilename": "front.jpg",
                "mimeType": "image/jpeg",
                "sizeBytes": 100,
            },
            {
                "originalFilename": "back.jpg",
                "mimeType": "image/jpeg",
                "sizeBytes": 200,
            },
        ],
    )
    uploads = registration["uploads"]
    inspector = FakeUploadObjectInspector(
        metadata_by_key={
            uploads[0]["originalObjectKey"]: UploadObjectMetadata(
                content_type="image/jpeg",
                size_bytes=100,
            )
        },
        missing_keys={uploads[1]["originalObjectKey"]},
    )
    _override_inspector(inspector)

    try:
        response = await database_client.post(
            f"/v1/upload-batches/{batch_id}/finalize",
        )
    finally:
        _clear_inspector_override()

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "uploading"
    assert body["finalizedAt"] is None
    assert body["completedAt"] is None
    assert [image["status"] for image in body["images"]] == [
        "uploaded",
        "failed",
    ]
    assert body["images"][0]["errorCode"] is None
    assert body["images"][0]["errorMessage"] is None
    assert body["images"][1]["errorCode"] == "object_missing"
    assert body["images"][1]["errorMessage"] is not None

    with Session(migrated_engine) as session:
        batch = session.get(UploadBatch, batch_id)
        images = session.scalars(
            select(ImageAsset)
            .where(ImageAsset.batch_id == batch_id)
            .order_by(ImageAsset.upload_order)
        ).all()

    assert batch is not None
    assert batch.status == "uploading"
    assert batch.finalized_at is None
    assert batch.completed_at is None
    assert [image.status for image in images] == ["uploaded", "failed"]
    assert images[0].error_code is None
    assert images[1].error_code == "object_missing"


@pytest.mark.parametrize(
    ("metadata", "expected_error_code"),
    [
        (
            UploadObjectMetadata(
                content_type="application/octet-stream",
                size_bytes=100,
            ),
            "content_type_mismatch",
        ),
        (
            UploadObjectMetadata(
                content_type="image/jpeg",
                size_bytes=99,
            ),
            "size_mismatch",
        ),
    ],
)
async def test_finalize_upload_batch_persists_metadata_mismatches(
    database_client: AsyncClient,
    fake_signer: FakeUploadUrlSigner,
    metadata: UploadObjectMetadata,
    expected_error_code: str,
) -> None:
    batch_id = await _create_batch(database_client)
    registration = await _register_batch(
        database_client,
        batch_id,
        payload=[
            {
                "originalFilename": "front.jpg",
                "mimeType": "image/jpeg",
                "sizeBytes": 100,
            }
        ],
    )
    upload = registration["uploads"][0]
    inspector = FakeUploadObjectInspector(
        metadata_by_key={upload["originalObjectKey"]: metadata}
    )
    _override_inspector(inspector)

    try:
        response = await database_client.post(
            f"/v1/upload-batches/{batch_id}/finalize",
        )
    finally:
        _clear_inspector_override()

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "uploading"
    assert body["finalizedAt"] is None
    assert body["images"][0]["status"] == "failed"
    assert body["images"][0]["errorCode"] == expected_error_code
    assert body["images"][0]["errorMessage"] is not None


async def test_finalize_upload_batch_clears_previous_error_after_successful_retry(
    database_client: AsyncClient,
    fake_signer: FakeUploadUrlSigner,
) -> None:
    batch_id = await _create_batch(database_client)
    registration = await _register_batch(
        database_client,
        batch_id,
        payload=[
            {
                "originalFilename": "front.jpg",
                "mimeType": "image/jpeg",
                "sizeBytes": 100,
            }
        ],
    )
    upload = registration["uploads"][0]
    missing_inspector = FakeUploadObjectInspector(
        missing_keys={upload["originalObjectKey"]}
    )
    _override_inspector(missing_inspector)

    try:
        failed_response = await database_client.post(
            f"/v1/upload-batches/{batch_id}/finalize",
        )
    finally:
        _clear_inspector_override()

    assert failed_response.status_code == 200
    assert failed_response.json()["images"][0]["status"] == "failed"

    successful_inspector = FakeUploadObjectInspector(
        metadata_by_key={
            upload["originalObjectKey"]: UploadObjectMetadata(
                content_type="image/jpeg",
                size_bytes=100,
            )
        }
    )
    _override_inspector(successful_inspector)

    try:
        successful_response = await database_client.post(
            f"/v1/upload-batches/{batch_id}/finalize",
        )
    finally:
        _clear_inspector_override()

    assert successful_response.status_code == 200
    body = successful_response.json()
    assert body["status"] == "queued"
    assert body["finalizedAt"] is not None
    assert body["images"][0]["status"] == "uploaded"
    assert body["images"][0]["errorCode"] is None
    assert body["images"][0]["errorMessage"] is None


async def test_finalize_upload_batch_returns_500_on_storage_failure(
    database_client: AsyncClient,
    migrated_engine: Engine,
    fake_signer: FakeUploadUrlSigner,
) -> None:
    batch_id = await _create_batch(database_client)
    registration = await _register_batch(
        database_client,
        batch_id,
        payload=[
            {
                "originalFilename": "front.jpg",
                "mimeType": "image/jpeg",
                "sizeBytes": 100,
            },
            {
                "originalFilename": "back.jpg",
                "mimeType": "image/jpeg",
                "sizeBytes": 200,
            },
        ],
    )
    uploads = registration["uploads"]
    inspector = FakeUploadObjectInspector(
        metadata_by_key={
            uploads[0]["originalObjectKey"]: UploadObjectMetadata(
                content_type="image/jpeg",
                size_bytes=100,
            )
        },
        failing_keys={uploads[1]["originalObjectKey"]},
    )
    _override_inspector(inspector)

    try:
        response = await database_client.post(
            f"/v1/upload-batches/{batch_id}/finalize",
        )
    finally:
        _clear_inspector_override()

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "upload_finalization_failed"
    assert inspector.requests == [
        uploads[0]["originalObjectKey"],
        uploads[1]["originalObjectKey"],
    ]

    with Session(migrated_engine) as session:
        batch = session.get(UploadBatch, batch_id)
        images = session.scalars(
            select(ImageAsset)
            .where(ImageAsset.batch_id == batch_id)
            .order_by(ImageAsset.upload_order)
        ).all()

    assert batch is not None
    assert batch.status == "uploading"
    assert batch.finalized_at is None
    assert batch.completed_at is None
    assert [image.status for image in images] == ["pending", "pending"]
    assert all(image.error_code is None for image in images)
    assert all(image.error_message is None for image in images)


async def test_finalize_upload_batch_is_idempotent_when_already_queued(
    database_client: AsyncClient,
    fake_signer: FakeUploadUrlSigner,
) -> None:
    batch_id = await _create_batch(database_client)
    registration = await _register_batch(
        database_client,
        batch_id,
        payload=[
            {
                "originalFilename": "front.jpg",
                "mimeType": "image/jpeg",
                "sizeBytes": 100,
            }
        ],
    )
    upload = registration["uploads"][0]
    inspector = FakeUploadObjectInspector(
        metadata_by_key={
            upload["originalObjectKey"]: UploadObjectMetadata(
                content_type="image/jpeg",
                size_bytes=100,
            )
        }
    )
    _override_inspector(inspector)

    try:
        first_response = await database_client.post(
            f"/v1/upload-batches/{batch_id}/finalize",
        )
        assert first_response.status_code == 200
        first_body = first_response.json()
        inspector.requests.clear()

        second_response = await database_client.post(
            f"/v1/upload-batches/{batch_id}/finalize",
        )
    finally:
        _clear_inspector_override()

    assert second_response.status_code == 200
    second_body = second_response.json()
    assert second_body["status"] == "queued"
    assert second_body["finalizedAt"] == first_body["finalizedAt"]
    assert inspector.requests == []


async def test_finalize_upload_batch_rejects_created_batches(
    database_client: AsyncClient,
) -> None:
    batch_id = await _create_batch(database_client)

    response = await database_client.post(
        f"/v1/upload-batches/{batch_id}/finalize",
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "invalid_batch_state"


async def test_finalize_upload_batch_returns_404_for_unknown_batch(
    database_client: AsyncClient,
) -> None:
    response = await database_client.post(
        f"/v1/upload-batches/{uuid4()}/finalize",
    )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "batch_not_found"
