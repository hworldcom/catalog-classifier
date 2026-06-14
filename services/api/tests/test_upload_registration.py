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

    def sign_upload_url(self, *, object_key: str, content_type: str) -> str:
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


async def test_registers_ordered_files_and_updates_the_batch(
    database_client: AsyncClient,
    migrated_engine: Engine,
    fake_signer: FakeUploadUrlSigner,
) -> None:
    batch_id = await _create_batch(database_client)
    response = await database_client.post(
        f"/v1/upload-batches/{batch_id}/uploads",
        json={
            "files": [
                {
                    "originalFilename": "product.jpg",
                    "mimeType": "image/jpeg",
                    "sizeBytes": 100,
                },
                {
                    "originalFilename": "product.jpg",
                    "mimeType": "image/jpeg",
                    "sizeBytes": 200,
                },
            ]
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["batchId"] == str(batch_id)
    assert body["status"] == "uploading"
    assert [upload["uploadOrder"] for upload in body["uploads"]] == [0, 1]
    assert [upload["originalFilename"] for upload in body["uploads"]] == [
        "product.jpg",
        "product.jpg",
    ]

    for upload in body["uploads"]:
        image_id = UUID(upload["imageId"])
        expected_key = (
            "organizations/00000000-0000-0000-0000-000000000001/"
            f"batches/{batch_id}/originals/{image_id}.jpg"
        )
        assert upload["originalObjectKey"] == expected_key
        assert upload["uploadUrl"] == (
            f"https://uploads.example.test/{expected_key}"
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
    assert batch.status == "uploading"
    assert batch.original_file_count == 2
    assert batch.processed_file_count == 0
    assert [image.id for image in images] == [
        UUID(upload["imageId"]) for upload in body["uploads"]
    ]
    assert [image.upload_order for image in images] == [0, 1]
    assert [image.original_filename for image in images] == [
        "product.jpg",
        "product.jpg",
    ]
    assert [image.size_bytes for image in images] == [100, 200]
    assert all(image.mime_type == "image/jpeg" for image in images)
    assert all(image.status == "pending" for image in images)


async def test_rejects_repeated_registration_without_new_rows(
    database_client: AsyncClient,
    migrated_engine: Engine,
    fake_signer: FakeUploadUrlSigner,
) -> None:
    batch_id = await _create_batch(database_client)
    payload = {
        "files": [
            {
                "originalFilename": "front.jpg",
                "mimeType": "image/jpeg",
                "sizeBytes": 100,
            }
        ]
    }

    first_response = await database_client.post(
        f"/v1/upload-batches/{batch_id}/uploads",
        json=payload,
    )
    second_response = await database_client.post(
        f"/v1/upload-batches/{batch_id}/uploads",
        json=payload,
    )

    assert first_response.status_code == 200
    assert second_response.status_code == 409
    assert second_response.json()["detail"]["code"] == "invalid_batch_state"
    assert len(fake_signer.requests) == 1

    with Session(migrated_engine) as session:
        images = session.scalars(
            select(ImageAsset).where(ImageAsset.batch_id == batch_id)
        ).all()
    assert len(images) == 1


async def test_concurrent_registration_creates_only_one_set_of_rows(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    signer = BlockingUploadUrlSigner()
    app.dependency_overrides[get_upload_url_signer] = lambda: signer
    batch_id = await _create_batch(database_client)
    payload = {
        "files": [
            {
                "originalFilename": "front.jpg",
                "mimeType": "image/jpeg",
                "sizeBytes": 100,
            }
        ]
    }

    try:
        first_request = asyncio.create_task(
            database_client.post(
                f"/v1/upload-batches/{batch_id}/uploads",
                json=payload,
            )
        )
        assert await asyncio.to_thread(signer.entered.wait, 5)
        second_request = asyncio.create_task(
            database_client.post(
                f"/v1/upload-batches/{batch_id}/uploads",
                json=payload,
            )
        )
        await asyncio.sleep(0.1)
        signer.release.set()
        responses = await asyncio.gather(first_request, second_request)
    finally:
        signer.release.set()
        app.dependency_overrides.pop(get_upload_url_signer, None)

    assert sorted(response.status_code for response in responses) == [200, 409]
    with Session(migrated_engine) as session:
        images = session.scalars(
            select(ImageAsset).where(ImageAsset.batch_id == batch_id)
        ).all()
    assert len(images) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"files": []},
        {
            "files": [
                {
                    "originalFilename": " ",
                    "mimeType": "image/jpeg",
                    "sizeBytes": 100,
                }
            ]
        },
        {
            "files": [
                {
                    "originalFilename": "front.jpg",
                    "mimeType": "image/png",
                    "sizeBytes": 100,
                }
            ]
        },
        {
            "files": [
                {
                    "originalFilename": "front.jpg",
                    "mimeType": "image/jpeg",
                    "sizeBytes": 0,
                }
            ]
        },
        {
            "files": [
                {
                    "originalFilename": "front.jpg",
                    "mimeType": "image/jpeg",
                    "sizeBytes": 10_485_761,
                }
            ]
        },
        {
            "files": [
                {
                    "originalFilename": f"image-{index}.jpg",
                    "mimeType": "image/jpeg",
                    "sizeBytes": 100,
                }
                for index in range(21)
            ]
        },
    ],
)
async def test_invalid_metadata_returns_bad_request_without_writes(
    database_client: AsyncClient,
    migrated_engine: Engine,
    fake_signer: FakeUploadUrlSigner,
    payload: dict[str, object],
) -> None:
    batch_id = await _create_batch(database_client)

    response = await database_client.post(
        f"/v1/upload-batches/{batch_id}/uploads",
        json=payload,
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_upload_metadata"
    assert fake_signer.requests == []
    with Session(migrated_engine) as session:
        batch = session.get(UploadBatch, batch_id)
        images = session.scalars(
            select(ImageAsset).where(ImageAsset.batch_id == batch_id)
        ).all()
    assert batch is not None
    assert batch.status == "created"
    assert batch.original_file_count == 0
    assert images == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"files": "front.jpg"},
        {
            "files": [
                {
                    "mimeType": "image/jpeg",
                    "sizeBytes": 100,
                }
            ]
        },
        {
            "files": [
                {
                    "originalFilename": "front.jpg",
                    "mimeType": "image/jpeg",
                    "sizeBytes": "100",
                }
            ]
        },
    ],
)
async def test_malformed_requests_return_unprocessable_entity(
    database_client: AsyncClient,
    fake_signer: FakeUploadUrlSigner,
    payload: dict[str, object],
) -> None:
    batch_id = await _create_batch(database_client)

    response = await database_client.post(
        f"/v1/upload-batches/{batch_id}/uploads",
        json=payload,
    )

    assert response.status_code == 422
    assert fake_signer.requests == []


async def test_batch_identifier_errors_are_distinct(
    database_client: AsyncClient,
    fake_signer: FakeUploadUrlSigner,
) -> None:
    payload = {
        "files": [
            {
                "originalFilename": "front.jpg",
                "mimeType": "image/jpeg",
                "sizeBytes": 100,
            }
        ]
    }

    malformed_response = await database_client.post(
        "/v1/upload-batches/not-a-uuid/uploads",
        json=payload,
    )
    unknown_response = await database_client.post(
        f"/v1/upload-batches/{uuid4()}/uploads",
        json=payload,
    )

    assert malformed_response.status_code == 422
    assert unknown_response.status_code == 404
    assert unknown_response.json()["detail"]["code"] == "batch_not_found"
    assert fake_signer.requests == []


async def test_signing_failure_rolls_back_images_and_batch_state(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    signer = FakeUploadUrlSigner(fail_at=1)
    app.dependency_overrides[get_upload_url_signer] = lambda: signer
    batch_id = await _create_batch(database_client)

    try:
        response = await database_client.post(
            f"/v1/upload-batches/{batch_id}/uploads",
            json={
                "files": [
                    {
                        "originalFilename": "front.jpg",
                        "mimeType": "image/jpeg",
                        "sizeBytes": 100,
                    },
                    {
                        "originalFilename": "back.jpg",
                        "mimeType": "image/jpeg",
                        "sizeBytes": 100,
                    },
                ]
            },
        )
    finally:
        app.dependency_overrides.pop(get_upload_url_signer, None)

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "upload_registration_failed"
    with Session(migrated_engine) as session:
        batch = session.get(UploadBatch, batch_id)
        images = session.scalars(
            select(ImageAsset).where(ImageAsset.batch_id == batch_id)
        ).all()
    assert batch is not None
    assert batch.status == "created"
    assert batch.original_file_count == 0
    assert images == []


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
                f"/v1/upload-batches/{uuid4()}/uploads",
                json={
                    "files": [
                        {
                            "originalFilename": "front.jpg",
                            "mimeType": "image/jpeg",
                            "sizeBytes": 100,
                        }
                    ]
                },
            )
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_upload_url_signer, None)
        engine.dispose()

    assert response.status_code == 500
    assert response.json() == {
        "detail": {
            "code": "upload_registration_failed",
            "message": "Unable to register files for upload.",
        }
    }
