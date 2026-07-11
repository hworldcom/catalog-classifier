from __future__ import annotations

from dataclasses import dataclass
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
    ReviewEvent,
    UploadBatch,
)
from catalog_api.upload_batches import DEFAULT_ORGANIZATION_ID

pytestmark = [pytest.mark.anyio, pytest.mark.postgresql]

PIPELINE_VERSION = "2026-06-01"


@dataclass(frozen=True)
class ReviewEditFixture:
    batch_id: UUID
    image_ids: list[UUID]
    group_ids: list[UUID]
    category_ids_by_slug: dict[str, UUID]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_create_review_group_moves_selected_images_and_logs_event(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        fixture = _create_review_edit_fixture(session)

    response = await database_client.post(
        f"/v1/upload-batches/{fixture.batch_id}/groups",
        json={
            "imageIds": [
                str(fixture.image_ids[1]),
                str(fixture.image_ids[2]),
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    created_group = _group_containing(body, fixture.image_ids[1], fixture.image_ids[2])
    assert [image["membershipSource"] for image in created_group["images"]] == [
        "manual_review",
        "manual_review",
    ]
    assert _group_containing(body, fixture.image_ids[0]) is not None

    with Session(migrated_engine) as session:
        assert _review_event_actions(session, fixture.batch_id) == ["create_group"]
        first_event_count = _review_event_count(session, fixture.batch_id)

    no_op_response = await database_client.post(
        f"/v1/upload-batches/{fixture.batch_id}/groups",
        json={
            "imageIds": [
                str(fixture.image_ids[1]),
                str(fixture.image_ids[2]),
            ],
        },
    )

    assert no_op_response.status_code == 200
    with Session(migrated_engine) as session:
        assert _review_event_count(session, fixture.batch_id) == first_event_count


async def test_move_review_image_removes_empty_source_group(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        fixture = _create_review_edit_fixture(session)

    response = await database_client.post(
        f"/v1/groups/{fixture.group_ids[0]}/images",
        json={"imageId": str(fixture.image_ids[2])},
    )

    assert response.status_code == 200
    body = response.json()
    target_group = _group_containing(
        body,
        fixture.image_ids[0],
        fixture.image_ids[1],
        fixture.image_ids[2],
    )
    assert target_group["groupId"] == str(fixture.group_ids[0])
    assert target_group["images"][2]["membershipSource"] == "manual_review"
    assert _image_group_id(body, fixture.image_ids[2]) == str(fixture.group_ids[0])

    with Session(migrated_engine) as session:
        group_count = session.scalar(
            select(func.count()).select_from(ProductGroup).where(
                ProductGroup.batch_id == fixture.batch_id
            )
        )
        assert group_count == 2
        assert _review_event_actions(session, fixture.batch_id) == ["move_image"]


async def test_remove_review_image_creates_singleton_group(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        fixture = _create_review_edit_fixture(session)

    response = await database_client.delete(
        f"/v1/groups/{fixture.group_ids[0]}/images/{fixture.image_ids[1]}",
    )

    assert response.status_code == 200
    body = response.json()
    assert _group_containing(body, fixture.image_ids[0])["groupId"] == str(
        fixture.group_ids[0]
    )
    singleton_group = _group_containing(body, fixture.image_ids[1])
    assert singleton_group["images"][0]["membershipSource"] == "manual_review"

    with Session(migrated_engine) as session:
        assert _review_event_actions(session, fixture.batch_id) == ["remove_image"]


async def test_merge_review_groups_deletes_source_groups_and_logs_event(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        fixture = _create_review_edit_fixture(session)

    response = await database_client.post(
        "/v1/groups/merge",
        json={
            "targetGroupId": str(fixture.group_ids[0]),
            "sourceGroupIds": [
                str(fixture.group_ids[1]),
                str(fixture.group_ids[2]),
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    merged_group = _group_containing(body, *fixture.image_ids)
    assert merged_group["groupId"] == str(fixture.group_ids[0])

    repeat_response = await database_client.post(
        "/v1/groups/merge",
        json={
            "targetGroupId": str(fixture.group_ids[0]),
            "sourceGroupIds": [str(fixture.group_ids[1])],
        },
    )

    assert repeat_response.status_code == 404
    assert repeat_response.json()["detail"]["code"] == "review_resource_not_found"
    with Session(migrated_engine) as session:
        group_count = session.scalar(
            select(func.count()).select_from(ProductGroup).where(
                ProductGroup.batch_id == fixture.batch_id
            )
        )
        assert group_count == 1
        assert _review_event_actions(session, fixture.batch_id) == ["merge_groups"]


async def test_split_review_group_creates_new_group_and_full_split_is_noop(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        fixture = _create_review_edit_fixture(session)

    response = await database_client.post(
        f"/v1/groups/{fixture.group_ids[0]}/split",
        json={"imageIds": [str(fixture.image_ids[1])]},
    )

    assert response.status_code == 200
    body = response.json()
    new_group = _group_containing(body, fixture.image_ids[1])
    assert new_group["groupId"] != str(fixture.group_ids[0])
    assert new_group["images"][0]["membershipSource"] == "manual_review"

    with Session(migrated_engine) as session:
        first_event_count = _review_event_count(session, fixture.batch_id)
        assert _review_event_actions(session, fixture.batch_id) == ["split_group"]

    no_op_response = await database_client.post(
        f"/v1/groups/{new_group['groupId']}/split",
        json={"imageIds": [str(fixture.image_ids[1])]},
    )

    assert no_op_response.status_code == 200
    with Session(migrated_engine) as session:
        assert _review_event_count(session, fixture.batch_id) == first_event_count


async def test_patch_review_group_updates_cover_and_approved_category(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        fixture = _create_review_edit_fixture(session)
        approved_category_id = fixture.category_ids_by_slug["trousers"]

    cover_response = await database_client.patch(
        f"/v1/groups/{fixture.group_ids[0]}",
        json={"coverImageId": str(fixture.image_ids[1])},
    )
    category_response = await database_client.patch(
        f"/v1/groups/{fixture.group_ids[0]}",
        json={"approvedCategoryId": str(approved_category_id)},
    )

    assert cover_response.status_code == 200
    assert category_response.status_code == 200
    group = _group_by_id(category_response.json(), fixture.group_ids[0])
    assert group["coverImageId"] == str(fixture.image_ids[1])
    assert group["approvedCategorySlug"] == "trousers"
    with Session(migrated_engine) as session:
        assert _review_event_actions(session, fixture.batch_id) == [
            "update_group",
            "update_group",
        ]


async def test_patch_review_group_image_marks_and_restores_duplicate(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        fixture = _create_review_edit_fixture(session)

    mark_response = await database_client.patch(
        f"/v1/groups/{fixture.group_ids[0]}/images/{fixture.image_ids[1]}",
        json={
            "isDuplicate": True,
            "duplicateOfImageId": str(fixture.image_ids[0]),
        },
    )
    restore_response = await database_client.patch(
        f"/v1/groups/{fixture.group_ids[0]}/images/{fixture.image_ids[1]}",
        json={"isDuplicate": False, "duplicateOfImageId": None},
    )

    assert mark_response.status_code == 200
    marked_image = _group_image(mark_response.json(), fixture.image_ids[1])
    assert marked_image["isDuplicate"] is True
    assert marked_image["duplicateOfImageId"] == str(fixture.image_ids[0])

    assert restore_response.status_code == 200
    restored_image = _group_image(restore_response.json(), fixture.image_ids[1])
    assert restored_image["isDuplicate"] is False
    assert restored_image["duplicateOfImageId"] is None
    with Session(migrated_engine) as session:
        assert _review_event_actions(session, fixture.batch_id) == [
            "mark_duplicate",
            "restore_duplicate",
        ]


async def test_review_edits_reject_invalid_requests_and_non_review_batches(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        fixture = _create_review_edit_fixture(session)
        processing_fixture = _create_review_edit_fixture(session, status="processing")

    duplicate_response = await database_client.post(
        f"/v1/upload-batches/{fixture.batch_id}/groups",
        json={
            "imageIds": [
                str(fixture.image_ids[0]),
                str(fixture.image_ids[0]),
            ],
        },
    )
    state_response = await database_client.post(
        f"/v1/groups/{processing_fixture.group_ids[0]}/images",
        json={"imageId": str(processing_fixture.image_ids[1])},
    )

    assert duplicate_response.status_code == 400
    assert duplicate_response.json()["detail"]["code"] == "invalid_review_edit"
    assert state_response.status_code == 409
    assert state_response.json()["detail"] == {
        "code": "review_edit_not_allowed",
        "message": "Review edits require a review-ready batch.",
    }


def _create_review_edit_fixture(
    session: Session,
    *,
    status: str = "review_required",
) -> ReviewEditFixture:
    category_ids_by_slug = _category_ids_by_slug(session)
    assert "t-shirts" in category_ids_by_slug
    assert "trousers" in category_ids_by_slug

    batch = UploadBatch(
        organization_id=DEFAULT_ORGANIZATION_ID,
        status=status,
        original_file_count=4,
        processed_file_count=4,
        finalized_at=datetime.now(UTC),
        pipeline_version=PIPELINE_VERSION,
    )
    session.add(batch)
    session.flush()

    image_ids: list[UUID] = []
    for upload_order in range(4):
        image_id = uuid4()
        image_ids.append(image_id)
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
                original_filename=f"image-{upload_order}.jpg",
                upload_order=upload_order,
                mime_type=JPEG_CONTENT_TYPE,
                size_bytes=100 + upload_order,
                status="processed",
            )
        )

    group_ids: list[UUID] = []
    group_specs = [
        (image_ids[0], category_ids_by_slug["t-shirts"], 0.95),
        (image_ids[2], None, 1.0),
        (image_ids[3], None, 1.0),
    ]
    for cover_image_id, suggested_category_id, confidence in group_specs:
        group = ProductGroup(
            organization_id=DEFAULT_ORGANIZATION_ID,
            batch_id=batch.id,
            status="proposed",
            suggested_category_id=suggested_category_id,
            cover_image_id=cover_image_id,
            confidence=confidence,
        )
        session.add(group)
        session.flush()
        group_ids.append(group.id)

    session.add_all(
        [
            ProductGroupImage(
                organization_id=DEFAULT_ORGANIZATION_ID,
                batch_id=batch.id,
                group_id=group_ids[0],
                image_id=image_ids[0],
                position=0,
                membership_source="engine",
                membership_confidence=0.95,
            ),
            ProductGroupImage(
                organization_id=DEFAULT_ORGANIZATION_ID,
                batch_id=batch.id,
                group_id=group_ids[0],
                image_id=image_ids[1],
                position=1,
                membership_source="engine",
                membership_confidence=0.92,
            ),
            ProductGroupImage(
                organization_id=DEFAULT_ORGANIZATION_ID,
                batch_id=batch.id,
                group_id=group_ids[1],
                image_id=image_ids[2],
                position=0,
                membership_source="singleton",
                membership_confidence=None,
            ),
            ProductGroupImage(
                organization_id=DEFAULT_ORGANIZATION_ID,
                batch_id=batch.id,
                group_id=group_ids[2],
                image_id=image_ids[3],
                position=0,
                membership_source="singleton",
                membership_confidence=None,
            ),
        ]
    )
    session.commit()
    return ReviewEditFixture(
        batch_id=batch.id,
        image_ids=image_ids,
        group_ids=group_ids,
        category_ids_by_slug=category_ids_by_slug,
    )


def _category_ids_by_slug(session: Session) -> dict[str, UUID]:
    categories = session.scalars(select(Category)).all()
    return {category.slug: category.id for category in categories}


def _group_containing(snapshot: dict, *image_ids: UUID) -> dict:
    selected_ids = {str(image_id) for image_id in image_ids}
    for group in snapshot["groups"]:
        group_image_ids = {image["imageId"] for image in group["images"]}
        if group_image_ids == selected_ids:
            return group
    raise AssertionError(f"Group containing only {sorted(selected_ids)} was not found.")


def _group_by_id(snapshot: dict, group_id: UUID) -> dict:
    for group in snapshot["groups"]:
        if group["groupId"] == str(group_id):
            return group
    raise AssertionError(f"Group {group_id} was not found.")


def _image_group_id(snapshot: dict, image_id: UUID) -> str:
    for group in snapshot["groups"]:
        if any(image["imageId"] == str(image_id) for image in group["images"]):
            return group["groupId"]
    raise AssertionError(f"Image {image_id} was not found.")


def _group_image(snapshot: dict, image_id: UUID) -> dict:
    for group in snapshot["groups"]:
        for image in group["images"]:
            if image["imageId"] == str(image_id):
                return image
    raise AssertionError(f"Image {image_id} was not found.")


def _review_event_count(session: Session, batch_id: UUID) -> int:
    count = session.scalar(
        select(func.count()).select_from(ReviewEvent).where(
            ReviewEvent.batch_id == batch_id
        )
    )
    assert count is not None
    return count


def _review_event_actions(session: Session, batch_id: UUID) -> list[str]:
    return session.scalars(
        select(ReviewEvent.action_type)
        .where(ReviewEvent.batch_id == batch_id)
        .order_by(ReviewEvent.created_at, ReviewEvent.id)
    ).all()
