from __future__ import annotations

import asyncio
from collections.abc import Iterator
from threading import Event
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

from catalog_api.database import get_session
from catalog_api.main import app
from catalog_api.models import ImageAsset, UploadBatch
from catalog_api.upload_storage import (
    UploadUrlSigningError,
    get_upload_url_signer,
)

pytestmark = [pytest.mark.anyio, pytest.mark.postgresql]


class FakeUploadUrlSigner:
    def __init__(self, *, fail_at: int | None = None) -> None:
        self.fail_at = fail_at
        self.requests: list[tuple[str, str]] = []

    def sign_upload_url(self, *, object_key: str, content_type: str) -> str:
        if self.fail_at == len(self.requests):
            raise UploadUrlSigningError
        self.requests.append((object_key, content_type))
        return f"https://uploads.example.test/{object_key}"


class BlockingUploadUrlSigner(FakeUploadUrlSigner):
    def __init__(self) -> None:
        super().__init__()
        self.entered = Event()
        self.release = Event()
        self.attempt_count = 0

    def sign_upload_url(self, *, object_key: str, content_type: str) -> str:
        self.attempt_count += 1
        if self.attempt_count == 1:
            self.entered.set()
            if not self.release.wait(timeout=5):
                raise UploadUrlSigningError
        return super().sign_upload_url(
            object_key=object_key,
            content_type=content_type,
        )


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
    file_count: int,
) -> list[dict[str, object]]:
    response = await client.post(
        f"/v1/upload-batches/{batch_id}/uploads",
        json={
            "files": [
                {
                    "originalFilename": f"image-{upload_order}.jpg",
                    "mimeType": "image/jpeg",
                    "sizeBytes": 100 + upload_order,
                }
                for upload_order in range(file_count)
            ]
        },
    )
    assert response.status_code == 200
    return response.json()["uploads"]


def _assert_retry_key(
    *,
    object_key: str,
    batch_id: UUID,
    image_id: UUID,
) -> None:
    prefix = (
        "organizations/00000000-0000-0000-0000-000000000001/"
        f"batches/{batch_id}/originals/{image_id}/retries/"
    )
    assert object_key.startswith(prefix)
    assert object_key.endswith(".jpg")
    UUID(object_key.removeprefix(prefix).removesuffix(".jpg"))


async def test_retries_selected_images_in_upload_order_and_preserves_state(
    database_client: AsyncClient,
    migrated_engine: Engine,
    fake_signer: FakeUploadUrlSigner,
) -> None:
    batch_id = await _create_batch(database_client)
    uploads = await _register_batch(
        database_client,
        batch_id,
        file_count=3,
    )
    image_ids = [UUID(upload["imageId"]) for upload in uploads]
    old_keys = {
        UUID(upload["imageId"]): upload["originalObjectKey"]
        for upload in uploads
    }

    with Session(migrated_engine) as session:
        batch = session.get(UploadBatch, batch_id)
        images = session.scalars(
            select(ImageAsset)
            .where(ImageAsset.batch_id == batch_id)
            .order_by(ImageAsset.upload_order)
        ).all()
        assert batch is not None
        original_batch_state = (
            batch.status,
            batch.original_file_count,
            batch.processed_file_count,
            batch.created_at,
            batch.finalized_at,
            batch.completed_at,
        )
        images[0].status = "failed"
        images[0].error_code = "object_missing"
        images[0].error_message = "The object was missing."
        images[2].status = "uploaded"
        session.commit()

    fake_signer.requests.clear()
    response = await database_client.post(
        f"/v1/upload-batches/{batch_id}/retry-failed",
        json={"imageIds": [str(image_ids[1]), str(image_ids[0])]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["batchId"] == str(batch_id)
    assert body["status"] == "uploading"
    assert [upload["uploadOrder"] for upload in body["uploads"]] == [0, 1]
    assert [UUID(upload["imageId"]) for upload in body["uploads"]] == image_ids[:2]

    for upload in body["uploads"]:
        image_id = UUID(upload["imageId"])
        object_key = upload["originalObjectKey"]
        _assert_retry_key(
            object_key=object_key,
            batch_id=batch_id,
            image_id=image_id,
        )
        assert object_key != old_keys[image_id]
        assert upload["uploadUrl"] == (
            f"https://uploads.example.test/{object_key}"
        )

    assert fake_signer.requests == [
        (upload["originalObjectKey"], "image/jpeg")
        for upload in body["uploads"]
    ]

    with Session(migrated_engine) as session:
        batch = session.get(UploadBatch, batch_id)
        images = session.scalars(
            select(ImageAsset)
            .where(ImageAsset.batch_id == batch_id)
            .order_by(ImageAsset.upload_order)
        ).all()

    assert batch is not None
    assert (
        batch.status,
        batch.original_file_count,
        batch.processed_file_count,
        batch.created_at,
        batch.finalized_at,
        batch.completed_at,
    ) == original_batch_state
    assert len(images) == 3
    assert [image.status for image in images] == [
        "failed",
        "pending",
        "uploaded",
    ]
    assert images[0].error_code == "object_missing"
    assert images[0].error_message == "The object was missing."
    assert images[1].error_code is None
    assert images[2].original_object_key == old_keys[image_ids[2]]
    assert [image.original_object_key for image in images[:2]] == [
        upload["originalObjectKey"] for upload in body["uploads"]
    ]


@pytest.mark.parametrize(
    "image_ids",
    [
        [],
        ["duplicate", "duplicate"],
    ],
)
async def test_rejects_empty_or_duplicate_retry_selections(
    database_client: AsyncClient,
    migrated_engine: Engine,
    fake_signer: FakeUploadUrlSigner,
    image_ids: list[str],
) -> None:
    batch_id = await _create_batch(database_client)
    uploads = await _register_batch(database_client, batch_id, file_count=1)
    image_id = uploads[0]["imageId"]
    request_ids = [] if not image_ids else [image_id, image_id]
    fake_signer.requests.clear()

    response = await database_client.post(
        f"/v1/upload-batches/{batch_id}/retry-failed",
        json={"imageIds": request_ids},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_retry_selection"
    assert fake_signer.requests == []
    with Session(migrated_engine) as session:
        image = session.get(ImageAsset, UUID(image_id))
    assert image is not None
    assert image.original_object_key == uploads[0]["originalObjectKey"]


async def test_rejects_images_outside_the_batch_or_not_retryable(
    database_client: AsyncClient,
    migrated_engine: Engine,
    fake_signer: FakeUploadUrlSigner,
) -> None:
    batch_id = await _create_batch(database_client)
    uploads = await _register_batch(database_client, batch_id, file_count=2)
    other_batch_id = await _create_batch(database_client)
    other_uploads = await _register_batch(
        database_client,
        other_batch_id,
        file_count=1,
    )
    uploaded_image_id = UUID(uploads[1]["imageId"])
    with Session(migrated_engine) as session:
        uploaded_image = session.get(ImageAsset, uploaded_image_id)
        assert uploaded_image is not None
        uploaded_image.status = "uploaded"
        session.commit()

    for selected_image_id in (
        other_uploads[0]["imageId"],
        str(uploaded_image_id),
    ):
        fake_signer.requests.clear()
        response = await database_client.post(
            f"/v1/upload-batches/{batch_id}/retry-failed",
            json={"imageIds": [selected_image_id]},
        )
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "invalid_retry_selection"
        assert fake_signer.requests == []

    with Session(migrated_engine) as session:
        images = session.scalars(
            select(ImageAsset)
            .where(ImageAsset.batch_id.in_([batch_id, other_batch_id]))
            .order_by(ImageAsset.batch_id, ImageAsset.upload_order)
        ).all()
    assert {image.original_object_key for image in images} == {
        upload["originalObjectKey"] for upload in uploads + other_uploads
    }


async def test_rejects_retry_when_batch_is_not_uploading(
    database_client: AsyncClient,
    migrated_engine: Engine,
    fake_signer: FakeUploadUrlSigner,
) -> None:
    batch_id = await _create_batch(database_client)
    uploads = await _register_batch(database_client, batch_id, file_count=1)
    with Session(migrated_engine) as session:
        batch = session.get(UploadBatch, batch_id)
        assert batch is not None
        batch.status = "queued"
        session.commit()
    fake_signer.requests.clear()

    response = await database_client.post(
        f"/v1/upload-batches/{batch_id}/retry-failed",
        json={"imageIds": [uploads[0]["imageId"]]},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "invalid_batch_state"
    assert fake_signer.requests == []


async def test_signing_failure_rolls_back_all_object_key_changes(
    database_client: AsyncClient,
    migrated_engine: Engine,
    fake_signer: FakeUploadUrlSigner,
) -> None:
    batch_id = await _create_batch(database_client)
    uploads = await _register_batch(database_client, batch_id, file_count=2)
    failing_signer = FakeUploadUrlSigner(fail_at=1)
    app.dependency_overrides[get_upload_url_signer] = lambda: failing_signer

    try:
        response = await database_client.post(
            f"/v1/upload-batches/{batch_id}/retry-failed",
            json={"imageIds": [upload["imageId"] for upload in uploads]},
        )
    finally:
        app.dependency_overrides[get_upload_url_signer] = lambda: fake_signer

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "upload_retry_failed"
    assert len(failing_signer.requests) == 1
    with Session(migrated_engine) as session:
        images = session.scalars(
            select(ImageAsset)
            .where(ImageAsset.batch_id == batch_id)
            .order_by(ImageAsset.upload_order)
        ).all()
    assert [image.original_object_key for image in images] == [
        upload["originalObjectKey"] for upload in uploads
    ]
    assert all(image.status == "pending" for image in images)


async def test_repeated_retries_rotate_the_object_key_without_new_rows(
    database_client: AsyncClient,
    migrated_engine: Engine,
    fake_signer: FakeUploadUrlSigner,
) -> None:
    batch_id = await _create_batch(database_client)
    uploads = await _register_batch(database_client, batch_id, file_count=1)
    image_id = uploads[0]["imageId"]
    fake_signer.requests.clear()

    first_response = await database_client.post(
        f"/v1/upload-batches/{batch_id}/retry-failed",
        json={"imageIds": [image_id]},
    )
    second_response = await database_client.post(
        f"/v1/upload-batches/{batch_id}/retry-failed",
        json={"imageIds": [image_id]},
    )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    first_key = first_response.json()["uploads"][0]["originalObjectKey"]
    second_key = second_response.json()["uploads"][0]["originalObjectKey"]
    assert first_key != second_key
    assert len(fake_signer.requests) == 2
    with Session(migrated_engine) as session:
        images = session.scalars(
            select(ImageAsset).where(ImageAsset.batch_id == batch_id)
        ).all()
    assert len(images) == 1
    assert images[0].original_object_key == second_key
    assert images[0].status == "pending"


async def test_concurrent_retries_are_serialized_by_the_batch_lock(
    database_client: AsyncClient,
    migrated_engine: Engine,
    fake_signer: FakeUploadUrlSigner,
) -> None:
    batch_id = await _create_batch(database_client)
    uploads = await _register_batch(database_client, batch_id, file_count=1)
    signer = BlockingUploadUrlSigner()
    app.dependency_overrides[get_upload_url_signer] = lambda: signer

    try:
        first_request = asyncio.create_task(
            database_client.post(
                f"/v1/upload-batches/{batch_id}/retry-failed",
                json={"imageIds": [uploads[0]["imageId"]]},
            )
        )
        assert await asyncio.to_thread(signer.entered.wait, 5)
        second_request = asyncio.create_task(
            database_client.post(
                f"/v1/upload-batches/{batch_id}/retry-failed",
                json={"imageIds": [uploads[0]["imageId"]]},
            )
        )
        await asyncio.sleep(0.1)
        assert signer.attempt_count == 1
        signer.release.set()
        first_response, second_response = await asyncio.gather(
            first_request,
            second_request,
        )
    finally:
        signer.release.set()
        app.dependency_overrides[get_upload_url_signer] = lambda: fake_signer

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    first_key = first_response.json()["uploads"][0]["originalObjectKey"]
    second_key = second_response.json()["uploads"][0]["originalObjectKey"]
    assert first_key != second_key
    assert signer.attempt_count == 2
    with Session(migrated_engine) as session:
        image = session.get(ImageAsset, UUID(uploads[0]["imageId"]))
    assert image is not None
    assert image.original_object_key == second_key


async def test_retry_identifier_and_request_errors_are_distinct(
    database_client: AsyncClient,
    fake_signer: FakeUploadUrlSigner,
) -> None:
    malformed_batch_response = await database_client.post(
        "/v1/upload-batches/not-a-uuid/retry-failed",
        json={"imageIds": [str(uuid4())]},
    )
    unknown_batch_response = await database_client.post(
        f"/v1/upload-batches/{uuid4()}/retry-failed",
        json={"imageIds": [str(uuid4())]},
    )
    malformed_request_response = await database_client.post(
        f"/v1/upload-batches/{uuid4()}/retry-failed",
        json={"imageIds": ["not-a-uuid"]},
    )

    assert malformed_batch_response.status_code == 422
    assert unknown_batch_response.status_code == 404
    assert unknown_batch_response.json()["detail"]["code"] == "batch_not_found"
    assert malformed_request_response.status_code == 422
    assert fake_signer.requests == []


async def test_database_failure_returns_internal_server_error(
    empty_database_url: str,
) -> None:
    engine = create_engine(empty_database_url)
    signer = FakeUploadUrlSigner()

    def override_session() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_upload_url_signer] = lambda: signer
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                f"/v1/upload-batches/{uuid4()}/retry-failed",
                json={"imageIds": [str(uuid4())]},
            )
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_upload_url_signer, None)
        engine.dispose()

    assert response.status_code == 500
    assert response.json() == {
        "detail": {
            "code": "upload_retry_failed",
            "message": "Unable to prepare the selected upload retries.",
        }
    }
