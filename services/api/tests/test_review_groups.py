from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from catalog_api.image_processing import JPEG_CONTENT_TYPE
from catalog_api.models import (
    Category,
    ImageAsset,
    ProductGroup,
    ProductGroupImage,
    UploadBatch,
)
from catalog_api.upload_batches import DEFAULT_ORGANIZATION_ID

pytestmark = [pytest.mark.anyio, pytest.mark.postgresql]

PIPELINE_VERSION = "2026-06-01"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _create_review_batch_with_group(session: Session) -> tuple[UUID, list[UUID], UUID]:
    category = session.scalar(select(Category).where(Category.slug == "sportswear"))
    assert category is not None
    batch = UploadBatch(
        organization_id=DEFAULT_ORGANIZATION_ID,
        status="review_required",
        original_file_count=2,
        processed_file_count=2,
        finalized_at=datetime.now(UTC),
        pipeline_version=PIPELINE_VERSION,
    )
    session.add(batch)
    session.flush()

    image_ids: list[UUID] = []
    for upload_order, filename in ((0, "front.jpg"), (1, "back.jpg")):
        image_id = uuid4()
        session.add(
            ImageAsset(
                id=image_id,
                organization_id=DEFAULT_ORGANIZATION_ID,
                batch_id=batch.id,
                original_object_key=(
                    f"organizations/{DEFAULT_ORGANIZATION_ID}/batches/{batch.id}/"
                    f"originals/{image_id}.jpg"
                ),
                thumbnail_object_key=(
                    f"organizations/{DEFAULT_ORGANIZATION_ID}/batches/{batch.id}/"
                    f"derived/{PIPELINE_VERSION}/{image_id}/thumbnail.jpg"
                ),
                original_filename=filename,
                upload_order=upload_order,
                mime_type=JPEG_CONTENT_TYPE,
                size_bytes=100 + upload_order,
                status="processed",
            )
        )
        image_ids.append(image_id)

    group = ProductGroup(
        organization_id=DEFAULT_ORGANIZATION_ID,
        batch_id=batch.id,
        status="proposed",
        suggested_category_id=category.id,
        cover_image_id=image_ids[0],
        confidence=0.93,
    )
    session.add(group)
    session.flush()

    session.add_all(
        [
            ProductGroupImage(
                organization_id=DEFAULT_ORGANIZATION_ID,
                batch_id=batch.id,
                group_id=group.id,
                image_id=image_ids[0],
                position=0,
                membership_source="engine",
                membership_confidence=0.94,
            ),
            ProductGroupImage(
                organization_id=DEFAULT_ORGANIZATION_ID,
                batch_id=batch.id,
                group_id=group.id,
                image_id=image_ids[1],
                position=1,
                membership_source="engine",
                membership_confidence=0.91,
                is_duplicate=True,
                duplicate_of_image_id=image_ids[0],
            ),
        ]
    )
    session.commit()
    return batch.id, image_ids, group.id


def _create_review_batch_without_groups(session: Session) -> UUID:
    batch = UploadBatch(
        organization_id=DEFAULT_ORGANIZATION_ID,
        status="review_required",
        original_file_count=0,
        processed_file_count=0,
        finalized_at=datetime.now(UTC),
        pipeline_version=PIPELINE_VERSION,
    )
    session.add(batch)
    session.commit()
    return batch.id


async def test_review_groups_snapshot_is_read_only(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        batch_id, image_ids, group_id = _create_review_batch_with_group(session)
        before_count = session.scalar(
            select(func.count()).select_from(ProductGroupImage)
        )

    first_response = await database_client.get(f"/v1/upload-batches/{batch_id}/groups")
    second_response = await database_client.get(f"/v1/upload-batches/{batch_id}/groups")

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert second_response.json() == first_response.json()
    body = first_response.json()
    assert body["batchId"] == str(batch_id)
    assert body["organizationId"] == str(DEFAULT_ORGANIZATION_ID)
    assert body["status"] == "review_required"
    assert body["pipelineVersion"] == PIPELINE_VERSION
    assert body["groups"] == [
        {
            "groupId": str(group_id),
            "status": "proposed",
            "confidence": 0.93,
            "coverImageId": str(image_ids[0]),
            "suggestedCategorySlug": "sportswear",
            "approvedCategorySlug": None,
            "categorySuggestionStatus": "unavailable",
            "approvedCategorySource": None,
            "possibleExistingProductId": None,
            "warnings": [],
            "images": [
                {
                    "imageId": str(image_ids[0]),
                    "originalFilename": "front.jpg",
                    "uploadOrder": 0,
                    "thumbnailUrl": (
                        f"/v1/upload-batches/{batch_id}/images/"
                        f"{image_ids[0]}/thumbnail"
                    ),
                    "position": 0,
                    "isDuplicate": False,
                    "isRejected": False,
                    "duplicateOfImageId": None,
                    "membershipSource": "engine",
                    "membershipConfidence": 0.94,
                },
                {
                    "imageId": str(image_ids[1]),
                    "originalFilename": "back.jpg",
                    "uploadOrder": 1,
                    "thumbnailUrl": (
                        f"/v1/upload-batches/{batch_id}/images/"
                        f"{image_ids[1]}/thumbnail"
                    ),
                    "position": 1,
                    "isDuplicate": True,
                    "isRejected": False,
                    "duplicateOfImageId": str(image_ids[0]),
                    "membershipSource": "engine",
                    "membershipConfidence": 0.91,
                },
            ],
        }
    ]

    with Session(migrated_engine) as session:
        after_count = session.scalar(select(func.count()).select_from(ProductGroupImage))
    assert after_count == before_count


async def test_review_groups_returns_empty_groups_for_review_ready_batch(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        batch_id = _create_review_batch_without_groups(session)

    response = await database_client.get(f"/v1/upload-batches/{batch_id}/groups")

    assert response.status_code == 200
    body = response.json()
    assert body["batchId"] == str(batch_id)
    assert body["status"] == "review_required"
    assert body["groups"] == []


async def test_review_groups_rejects_non_review_batches(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        batch = UploadBatch(
            organization_id=DEFAULT_ORGANIZATION_ID,
            status="processing",
            original_file_count=0,
            processed_file_count=0,
            finalized_at=datetime.now(UTC),
            pipeline_version=PIPELINE_VERSION,
        )
        session.add(batch)
        session.commit()
        batch_id = batch.id

    response = await database_client.get(f"/v1/upload-batches/{batch_id}/groups")

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "batch_not_review_ready",
        "message": "Upload batch has not entered the review phase.",
    }


async def test_review_groups_returns_404_for_unknown_batch(
    database_client: AsyncClient,
) -> None:
    response = await database_client.get(f"/v1/upload-batches/{uuid4()}/groups")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "batch_not_found"
