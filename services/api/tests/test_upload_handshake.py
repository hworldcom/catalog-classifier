from __future__ import annotations

from io import BytesIO
from typing import AsyncIterator
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image

from catalog_api.main import MAX_FILE_SIZE_BYTES, app

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as test_client:
        yield test_client


def make_jpeg() -> bytes:
    output = BytesIO()
    Image.new("RGB", (2, 2), color="navy").save(output, format="JPEG")
    return output.getvalue()


async def test_accepts_multiple_jpeg_files(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/upload-handshake",
        files=[
            ("files", ("front.jpg", make_jpeg(), "image/jpeg")),
            ("files", ("back.jpg", make_jpeg(), "image/jpeg")),
        ],
    )

    assert response.status_code == 200
    body = response.json()
    UUID(body["uploadId"])
    assert body["status"] == "completed"
    assert [file["filename"] for file in body["files"]] == [
        "front.jpg",
        "back.jpg",
    ]
    assert all(file["status"] == "accepted" for file in body["files"])
    assert all(file["errorCode"] is None for file in body["files"])


async def test_returns_partial_result_for_mixed_validity(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/v1/upload-handshake",
        files=[
            ("files", ("valid.jpg", make_jpeg(), "application/octet-stream")),
            ("files", ("invalid.jpg", b"not an image", "image/jpeg")),
        ],
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial"
    assert body["files"][0]["status"] == "accepted"
    assert body["files"][1] == {
        "filename": "invalid.jpg",
        "status": "rejected",
        "sizeBytes": 12,
        "errorCode": "invalid_jpeg",
        "errorMessage": "The file content is not a valid JPEG image.",
    }


async def test_accepts_a_jpeg_at_the_size_limit(client: AsyncClient) -> None:
    jpeg = make_jpeg()
    jpeg_at_limit = jpeg + (b"\0" * (MAX_FILE_SIZE_BYTES - len(jpeg)))

    response = await client.post(
        "/v1/upload-handshake",
        files=[("files", ("limit.jpg", jpeg_at_limit, "image/jpeg"))],
    )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["files"][0]["sizeBytes"] == MAX_FILE_SIZE_BYTES


async def test_rejects_an_oversized_file_without_rejecting_the_request(
    client: AsyncClient,
) -> None:
    oversized = make_jpeg() + (b"\0" * MAX_FILE_SIZE_BYTES)

    response = await client.post(
        "/v1/upload-handshake",
        files=[("files", ("large.jpg", oversized, "image/jpeg"))],
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "rejected"
    assert body["files"][0]["status"] == "rejected"
    assert body["files"][0]["errorCode"] == "file_too_large"


async def test_rejects_more_than_twenty_files(client: AsyncClient) -> None:
    jpeg = make_jpeg()
    files = [
        ("files", (f"image-{index}.jpg", jpeg, "image/jpeg"))
        for index in range(21)
    ]

    response = await client.post("/v1/upload-handshake", files=files)

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "too_many_files"


async def test_rejects_a_request_without_files(client: AsyncClient) -> None:
    response = await client.post("/v1/upload-handshake")

    assert response.status_code == 400
    assert response.json() == {
        "detail": {
            "code": "files_required",
            "message": "Upload at least one JPEG file.",
        }
    }
