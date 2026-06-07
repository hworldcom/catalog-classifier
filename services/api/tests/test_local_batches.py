from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from typing import AsyncIterator
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image

from catalog_api.local_batches import LOCAL_STORAGE_ROOT_ENV
from catalog_api.main import app

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


@pytest.fixture
def storage_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(LOCAL_STORAGE_ROOT_ENV, str(tmp_path))
    return tmp_path


def make_jpeg(
    color: str,
    *,
    size: tuple[int, int] = (8, 4),
    orientation: int | None = None,
) -> bytes:
    output = BytesIO()
    image = Image.new("RGB", size, color=color)
    exif = Image.Exif()
    if orientation is not None:
        exif[274] = orientation
    image.save(output, format="JPEG", exif=exif)
    return output.getvalue()


async def test_persists_originals_and_groups_exact_duplicates(
    client: AsyncClient,
    storage_root: Path,
) -> None:
    duplicate = make_jpeg("navy")
    unique = make_jpeg("red")

    response = await client.post(
        "/v1/local-batches",
        files=[
            ("files", ("front.jpg", duplicate, "image/jpeg")),
            ("files", ("front-copy.jpg", duplicate, "image/jpeg")),
            ("files", ("back.jpg", unique, "image/jpeg")),
        ],
    )

    assert response.status_code == 200
    body = response.json()
    batch_id = UUID(body["batchId"])
    assert body["status"] == "completed"
    assert body["manifestVersion"] == 1
    assert all(file["status"] == "accepted" for file in body["files"])

    batch_directory = storage_root / "batches" / str(batch_id)
    manifest = json.loads((batch_directory / "manifest.json").read_text())
    assert manifest["manifestVersion"] == 1
    assert len(manifest["images"]) == 3
    assert sorted(len(group["imageIds"]) for group in manifest["groups"]) == [1, 2]

    first_image, duplicate_image, unique_image = manifest["images"]
    assert first_image["groupId"] == duplicate_image["groupId"]
    assert first_image["isRetained"] is True
    assert duplicate_image["isRetained"] is False
    assert unique_image["isRetained"] is True
    assert unique_image["groupId"] != first_image["groupId"]

    first_original = batch_directory / "images" / f"{first_image['imageId']}.jpg"
    assert first_original.read_bytes() == duplicate
    assert not list(batch_directory.glob(".manifest-*.tmp"))


async def test_loads_batch_and_serves_oriented_thumbnail(
    client: AsyncClient,
    storage_root: Path,
) -> None:
    rotated = make_jpeg("green", size=(8, 4), orientation=6)
    create_response = await client.post(
        "/v1/local-batches",
        files=[("files", ("rotated.jpg", rotated, "image/jpeg"))],
    )
    create_body = create_response.json()
    batch_id = create_body["batchId"]
    image_id = create_body["files"][0]["imageId"]

    load_response = await client.get(f"/v1/local-batches/{batch_id}")

    assert load_response.status_code == 200
    batch = load_response.json()
    assert batch["status"] == "ready"
    assert batch["images"][0]["thumbnailUrl"].endswith(
        f"/images/{image_id}/thumbnail"
    )
    assert batch["groups"][0]["retainedImageId"] == image_id

    original_response = await client.get(
        f"/v1/local-batches/{batch_id}/images/{image_id}"
    )
    thumbnail_response = await client.get(
        f"/v1/local-batches/{batch_id}/images/{image_id}/thumbnail"
    )

    assert original_response.content == rotated
    with Image.open(BytesIO(thumbnail_response.content)) as thumbnail:
        assert thumbnail.size == (4, 8)

    assert (storage_root / "batches" / batch_id / "manifest.json").is_file()


async def test_partial_batch_excludes_invalid_files_from_manifest(
    client: AsyncClient,
    storage_root: Path,
) -> None:
    response = await client.post(
        "/v1/local-batches",
        files=[
            ("files", ("valid.jpg", make_jpeg("navy"), "image/jpeg")),
            ("files", ("invalid.jpg", b"not an image", "image/jpeg")),
        ],
    )

    body = response.json()
    assert body["status"] == "partial"
    assert body["files"][0]["imageId"] is not None
    assert body["files"][1]["imageId"] is None
    assert body["files"][1]["errorCode"] == "invalid_jpeg"

    manifest_path = storage_root / "batches" / body["batchId"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert len(manifest["images"]) == 1


async def test_all_rejected_files_do_not_create_a_batch(
    client: AsyncClient,
    storage_root: Path,
) -> None:
    response = await client.post(
        "/v1/local-batches",
        files=[("files", ("invalid.jpg", b"not an image", "image/jpeg"))],
    )

    assert response.status_code == 200
    assert response.json() == {
        "batchId": None,
        "status": "rejected",
        "manifestVersion": None,
        "files": [
            {
                "imageId": None,
                "originalFilename": "invalid.jpg",
                "status": "rejected",
                "errorCode": "invalid_jpeg",
                "errorMessage": "The file content is not a valid JPEG image.",
            }
        ],
    }
    assert not (storage_root / "batches").exists()


async def test_uploading_the_same_files_creates_separate_batches(
    client: AsyncClient,
    storage_root: Path,
) -> None:
    jpeg = make_jpeg("navy")

    first = await client.post(
        "/v1/local-batches",
        files=[("files", ("front.jpg", jpeg, "image/jpeg"))],
    )
    second = await client.post(
        "/v1/local-batches",
        files=[("files", ("front.jpg", jpeg, "image/jpeg"))],
    )

    first_batch_id = first.json()["batchId"]
    second_batch_id = second.json()["batchId"]
    assert first_batch_id != second_batch_id
    assert (storage_root / "batches" / first_batch_id).is_dir()
    assert (storage_root / "batches" / second_batch_id).is_dir()


async def test_unknown_batches_and_images_return_not_found(
    client: AsyncClient,
    storage_root: Path,
) -> None:
    unknown_batch = uuid4()
    batch_response = await client.get(f"/v1/local-batches/{unknown_batch}")
    assert batch_response.status_code == 404
    assert batch_response.json()["detail"]["code"] == "batch_not_found"

    create_response = await client.post(
        "/v1/local-batches",
        files=[("files", ("front.jpg", make_jpeg("navy"), "image/jpeg"))],
    )
    batch_id = create_response.json()["batchId"]
    image_response = await client.get(
        f"/v1/local-batches/{batch_id}/images/{uuid4()}"
    )
    assert image_response.status_code == 404
    assert image_response.json()["detail"]["code"] == "image_not_found"


async def test_moves_an_image_and_deletes_an_empty_group(
    client: AsyncClient,
    storage_root: Path,
) -> None:
    create_response = await client.post(
        "/v1/local-batches",
        files=[
            ("files", ("first.jpg", make_jpeg("navy"), "image/jpeg")),
            ("files", ("second.jpg", make_jpeg("red"), "image/jpeg")),
            ("files", ("third.jpg", make_jpeg("green"), "image/jpeg")),
        ],
    )
    batch_id = create_response.json()["batchId"]
    batch = (await client.get(f"/v1/local-batches/{batch_id}")).json()
    first_image, second_image, _ = batch["images"]
    target_group_id = second_image["groupId"]

    response = await client.patch(
        f"/v1/local-batches/{batch_id}/images/{first_image['imageId']}",
        json={"groupId": target_group_id},
    )

    assert response.status_code == 200
    updated = response.json()["batch"]
    assert len(updated["groups"]) == 2
    target_group = next(
        group for group in updated["groups"] if group["groupId"] == target_group_id
    )
    assert target_group["imageIds"] == [
        first_image["imageId"],
        second_image["imageId"],
    ]
    assert target_group["retainedImageId"] == first_image["imageId"]
    updated_first = next(
        image
        for image in updated["images"]
        if image["imageId"] == first_image["imageId"]
    )
    updated_second = next(
        image
        for image in updated["images"]
        if image["imageId"] == second_image["imageId"]
    )
    assert updated_first["isRetained"] is True
    assert updated_second["isRetained"] is False

    manifest_path = storage_root / "batches" / batch_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert len(manifest["groups"]) == 2
    assert not list(manifest_path.parent.glob(".manifest-*.tmp"))


async def test_move_to_current_group_is_a_successful_no_op(
    client: AsyncClient,
    storage_root: Path,
) -> None:
    create_response = await client.post(
        "/v1/local-batches",
        files=[("files", ("first.jpg", make_jpeg("navy"), "image/jpeg"))],
    )
    batch_id = create_response.json()["batchId"]
    batch = (await client.get(f"/v1/local-batches/{batch_id}")).json()
    image = batch["images"][0]

    response = await client.patch(
        f"/v1/local-batches/{batch_id}/images/{image['imageId']}",
        json={"groupId": image["groupId"]},
    )

    assert response.status_code == 200
    assert response.json()["batch"] == batch


async def test_creates_a_new_group_and_appends_it_after_remaining_groups(
    client: AsyncClient,
    storage_root: Path,
) -> None:
    create_response = await client.post(
        "/v1/local-batches",
        files=[
            ("files", ("first.jpg", make_jpeg("navy"), "image/jpeg")),
            ("files", ("second.jpg", make_jpeg("red"), "image/jpeg")),
            ("files", ("third.jpg", make_jpeg("green"), "image/jpeg")),
        ],
    )
    batch_id = create_response.json()["batchId"]
    batch = (await client.get(f"/v1/local-batches/{batch_id}")).json()
    first_image, second_image, third_image = batch["images"]
    remaining_group_id = third_image["groupId"]

    response = await client.post(
        f"/v1/local-batches/{batch_id}/groups",
        json={
            "imageIds": [
                second_image["imageId"],
                first_image["imageId"],
            ]
        },
    )

    assert response.status_code == 200
    body = response.json()
    updated = body["batch"]
    assert [group["groupId"] for group in updated["groups"]] == [
        remaining_group_id,
        body["groupId"],
    ]
    new_group = updated["groups"][-1]
    assert new_group["imageIds"] == [
        first_image["imageId"],
        second_image["imageId"],
    ]
    assert new_group["retainedImageId"] == first_image["imageId"]


@pytest.mark.parametrize(
    ("image_ids", "expected_message"),
    [
        ([], "Select at least one image to create a group."),
        (["same", "same"], "Image identifiers must be unique."),
    ],
)
async def test_rejects_invalid_group_selections(
    client: AsyncClient,
    storage_root: Path,
    image_ids: list[str],
    expected_message: str,
) -> None:
    create_response = await client.post(
        "/v1/local-batches",
        files=[("files", ("first.jpg", make_jpeg("navy"), "image/jpeg"))],
    )
    batch_id = create_response.json()["batchId"]
    existing_image_id = create_response.json()["files"][0]["imageId"]
    request_ids = [
        existing_image_id if image_id == "same" else image_id
        for image_id in image_ids
    ]

    response = await client.post(
        f"/v1/local-batches/{batch_id}/groups",
        json={"imageIds": request_ids},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == {
        "code": "invalid_selection",
        "message": expected_message,
    }


async def test_rejects_existing_group_membership_ignoring_order(
    client: AsyncClient,
    storage_root: Path,
) -> None:
    duplicate = make_jpeg("navy")
    create_response = await client.post(
        "/v1/local-batches",
        files=[
            ("files", ("first.jpg", duplicate, "image/jpeg")),
            ("files", ("copy.jpg", duplicate, "image/jpeg")),
        ],
    )
    batch_id = create_response.json()["batchId"]
    image_ids = [
        file["imageId"] for file in reversed(create_response.json()["files"])
    ]

    response = await client.post(
        f"/v1/local-batches/{batch_id}/groups",
        json={"imageIds": image_ids},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["message"] == (
        "The selected images already form an existing group."
    )


async def test_edit_endpoints_return_not_found_for_unknown_references(
    client: AsyncClient,
    storage_root: Path,
) -> None:
    create_response = await client.post(
        "/v1/local-batches",
        files=[("files", ("first.jpg", make_jpeg("navy"), "image/jpeg"))],
    )
    batch_id = create_response.json()["batchId"]
    image_id = create_response.json()["files"][0]["imageId"]

    missing_group = await client.patch(
        f"/v1/local-batches/{batch_id}/images/{image_id}",
        json={"groupId": str(uuid4())},
    )
    assert missing_group.status_code == 404
    assert missing_group.json()["detail"]["code"] == "group_not_found"

    missing_image = await client.post(
        f"/v1/local-batches/{batch_id}/groups",
        json={"imageIds": [str(uuid4())]},
    )
    assert missing_image.status_code == 404
    assert missing_image.json()["detail"]["code"] == "image_not_found"
