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
class ImageRejectionFixture:
    batch_id: UUID
    group_id: UUID
    image_ids: list[UUID]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_reject_and_restore_are_auditable_idempotent_and_preserve_cover(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        fixture = _create_image_rejection_fixture(session)

    initial_response = await database_client.get(
        f"/v1/upload-batches/{fixture.batch_id}/groups"
    )
    reject_response = await database_client.post(
        f"/v1/groups/{fixture.group_id}/images/{fixture.image_ids[0]}/reject"
    )

    assert initial_response.status_code == 200
    assert all(
        image["isRejected"] is False
        for image in initial_response.json()["groups"][0]["images"]
    )
    assert reject_response.status_code == 200
    rejected_group = _group_by_id(reject_response.json(), fixture.group_id)
    assert rejected_group["coverImageId"] == str(fixture.image_ids[1])
    assert _group_image(reject_response.json(), fixture.image_ids[0])[
        "isRejected"
    ] is True

    with Session(migrated_engine) as session:
        events = _review_events(session, fixture.batch_id)
        assert len(events) == 1
        assert events[0].action_type == "reject_image"
        assert events[0].group_id == fixture.group_id
        assert events[0].image_id == fixture.image_ids[0]
        assert events[0].payload_json == {
            "groupId": str(fixture.group_id),
            "imageId": str(fixture.image_ids[0]),
            "before": {"isRejected": False},
            "after": {"isRejected": True},
        }

    repeated_reject_response = await database_client.post(
        f"/v1/groups/{fixture.group_id}/images/{fixture.image_ids[0]}/reject"
    )
    assert repeated_reject_response.status_code == 200
    with Session(migrated_engine) as session:
        assert _review_event_count(session, fixture.batch_id) == 1

    restore_response = await database_client.post(
        f"/v1/groups/{fixture.group_id}/images/"
        f"{fixture.image_ids[0]}/restore-rejection"
    )
    assert restore_response.status_code == 200
    restored_group = _group_by_id(restore_response.json(), fixture.group_id)
    assert restored_group["coverImageId"] == str(fixture.image_ids[1])
    assert _group_image(restore_response.json(), fixture.image_ids[0])[
        "isRejected"
    ] is False

    repeated_restore_response = await database_client.post(
        f"/v1/groups/{fixture.group_id}/images/"
        f"{fixture.image_ids[0]}/restore-rejection"
    )
    assert repeated_restore_response.status_code == 200
    with Session(migrated_engine) as session:
        events = _review_events(session, fixture.batch_id)
        assert [event.action_type for event in events] == [
            "reject_image",
            "restore_rejection",
        ]
        assert events[1].payload_json == {
            "groupId": str(fixture.group_id),
            "imageId": str(fixture.image_ids[0]),
            "before": {"isRejected": True},
            "after": {"isRejected": False},
        }


async def test_rejection_enforces_duplicate_master_invariants(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        fixture = _create_image_rejection_fixture(session)

    mark_duplicate_response = await database_client.patch(
        f"/v1/groups/{fixture.group_id}/images/{fixture.image_ids[1]}",
        json={
            "isDuplicate": True,
            "duplicateOfImageId": str(fixture.image_ids[0]),
        },
    )
    blocked_master_response = await database_client.post(
        f"/v1/groups/{fixture.group_id}/images/{fixture.image_ids[0]}/reject"
    )

    assert mark_duplicate_response.status_code == 200
    assert blocked_master_response.status_code == 409
    assert blocked_master_response.json()["detail"]["code"] == (
        "image_rejection_duplicate_master_in_use"
    )
    with Session(migrated_engine) as session:
        master = session.get(
            ProductGroupImage,
            (fixture.group_id, fixture.image_ids[0]),
        )
        assert master is not None
        assert master.is_rejected is False

    reject_duplicate_response = await database_client.post(
        f"/v1/groups/{fixture.group_id}/images/{fixture.image_ids[1]}/reject"
    )
    reject_master_response = await database_client.post(
        f"/v1/groups/{fixture.group_id}/images/{fixture.image_ids[0]}/reject"
    )
    blocked_restore_response = await database_client.post(
        f"/v1/groups/{fixture.group_id}/images/"
        f"{fixture.image_ids[1]}/restore-rejection"
    )

    assert reject_duplicate_response.status_code == 200
    assert reject_master_response.status_code == 200
    assert blocked_restore_response.status_code == 409
    assert blocked_restore_response.json()["detail"]["code"] == (
        "image_rejection_duplicate_master_rejected"
    )

    restore_master_response = await database_client.post(
        f"/v1/groups/{fixture.group_id}/images/"
        f"{fixture.image_ids[0]}/restore-rejection"
    )
    restore_duplicate_response = await database_client.post(
        f"/v1/groups/{fixture.group_id}/images/"
        f"{fixture.image_ids[1]}/restore-rejection"
    )
    reject_candidate_response = await database_client.post(
        f"/v1/groups/{fixture.group_id}/images/{fixture.image_ids[2]}/reject"
    )
    rejected_master_selection_response = await database_client.patch(
        f"/v1/groups/{fixture.group_id}/images/{fixture.image_ids[1]}",
        json={
            "isDuplicate": True,
            "duplicateOfImageId": str(fixture.image_ids[2]),
        },
    )
    rejected_cover_response = await database_client.patch(
        f"/v1/groups/{fixture.group_id}",
        json={"coverImageId": str(fixture.image_ids[2])},
    )

    assert restore_master_response.status_code == 200
    assert restore_duplicate_response.status_code == 200
    assert reject_candidate_response.status_code == 200
    assert rejected_master_selection_response.status_code == 409
    assert rejected_master_selection_response.json()["detail"]["code"] == (
        "image_rejection_duplicate_master_rejected"
    )
    assert rejected_cover_response.status_code == 400
    assert rejected_cover_response.json()["detail"] == {
        "code": "invalid_review_edit",
        "message": "coverImageId must not be rejected.",
    }


async def test_review_structure_edits_preserve_rejection(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        fixture = _create_image_rejection_fixture(session)

    reject_response = await database_client.post(
        f"/v1/groups/{fixture.group_id}/images/{fixture.image_ids[0]}/reject"
    )
    split_response = await database_client.post(
        f"/v1/groups/{fixture.group_id}/split",
        json={"imageIds": [str(fixture.image_ids[0])]},
    )
    split_group_id = _image_group_id(split_response.json(), fixture.image_ids[0])
    move_response = await database_client.post(
        f"/v1/groups/{fixture.group_id}/images",
        json={"imageId": str(fixture.image_ids[0])},
    )
    create_response = await database_client.post(
        f"/v1/upload-batches/{fixture.batch_id}/groups",
        json={
            "imageIds": [
                str(fixture.image_ids[0]),
                str(fixture.image_ids[2]),
            ]
        },
    )
    created_group_id = _image_group_id(
        create_response.json(),
        fixture.image_ids[0],
    )
    merge_response = await database_client.post(
        "/v1/groups/merge",
        json={
            "targetGroupId": str(fixture.group_id),
            "sourceGroupIds": [created_group_id],
        },
    )
    remove_response = await database_client.delete(
        f"/v1/groups/{fixture.group_id}/images/{fixture.image_ids[0]}"
    )

    assert reject_response.status_code == 200
    assert split_response.status_code == 200
    assert split_group_id != str(fixture.group_id)
    assert move_response.status_code == 200
    assert create_response.status_code == 200
    assert created_group_id != str(fixture.group_id)
    assert merge_response.status_code == 200
    assert remove_response.status_code == 200
    for response in (
        split_response,
        move_response,
        create_response,
        merge_response,
        remove_response,
    ):
        assert _group_image(response.json(), fixture.image_ids[0])[
            "isRejected"
        ] is True


async def test_rejection_routes_preserve_review_resource_and_state_errors(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        fixture = _create_image_rejection_fixture(session)
        processing_fixture = _create_image_rejection_fixture(
            session,
            status="processing",
        )

    unknown_group_response = await database_client.post(
        f"/v1/groups/{uuid4()}/images/{fixture.image_ids[0]}/reject"
    )
    missing_membership_response = await database_client.post(
        f"/v1/groups/{fixture.group_id}/images/{uuid4()}/reject"
    )
    invalid_state_response = await database_client.post(
        f"/v1/groups/{processing_fixture.group_id}/images/"
        f"{processing_fixture.image_ids[0]}/reject"
    )

    assert unknown_group_response.status_code == 404
    assert unknown_group_response.json()["detail"] == {
        "code": "review_resource_not_found",
        "message": "Group was not found.",
    }
    assert missing_membership_response.status_code == 404
    assert missing_membership_response.json()["detail"] == {
        "code": "review_resource_not_found",
        "message": "Group image membership was not found.",
    }
    assert invalid_state_response.status_code == 409
    assert invalid_state_response.json()["detail"] == {
        "code": "review_edit_not_allowed",
        "message": "Review edits require a review-ready batch.",
    }


def _create_image_rejection_fixture(
    session: Session,
    *,
    status: str = "review_required",
) -> ImageRejectionFixture:
    category = session.scalar(select(Category).where(Category.slug == "t-shirts"))
    assert category is not None
    batch = UploadBatch(
        organization_id=DEFAULT_ORGANIZATION_ID,
        status=status,
        original_file_count=3,
        processed_file_count=3,
        finalized_at=datetime.now(UTC),
        pipeline_version=PIPELINE_VERSION,
    )
    session.add(batch)
    session.flush()

    image_ids: list[UUID] = []
    for upload_order in range(3):
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

    group = ProductGroup(
        organization_id=DEFAULT_ORGANIZATION_ID,
        batch_id=batch.id,
        status="proposed",
        suggested_category_id=category.id,
        approved_category_id=category.id,
        approved_category_source="reviewer_selection",
        cover_image_id=image_ids[0],
        confidence=0.95,
    )
    session.add(group)
    session.flush()
    session.add_all(
        [
            ProductGroupImage(
                organization_id=DEFAULT_ORGANIZATION_ID,
                batch_id=batch.id,
                group_id=group.id,
                image_id=image_id,
                position=position,
                membership_source="engine",
                membership_confidence=0.95,
            )
            for position, image_id in enumerate(image_ids)
        ]
    )
    session.commit()
    return ImageRejectionFixture(
        batch_id=batch.id,
        group_id=group.id,
        image_ids=image_ids,
    )


def _group_by_id(snapshot: dict, group_id: UUID) -> dict:
    for group in snapshot["groups"]:
        if group["groupId"] == str(group_id):
            return group
    raise AssertionError(f"Group {group_id} was not found.")


def _group_image(snapshot: dict, image_id: UUID) -> dict:
    for group in snapshot["groups"]:
        for image in group["images"]:
            if image["imageId"] == str(image_id):
                return image
    raise AssertionError(f"Image {image_id} was not found.")


def _image_group_id(snapshot: dict, image_id: UUID) -> str:
    for group in snapshot["groups"]:
        if any(image["imageId"] == str(image_id) for image in group["images"]):
            return group["groupId"]
    raise AssertionError(f"Image {image_id} was not found.")


def _review_event_count(session: Session, batch_id: UUID) -> int:
    count = session.scalar(
        select(func.count()).select_from(ReviewEvent).where(
            ReviewEvent.batch_id == batch_id
        )
    )
    assert count is not None
    return count


def _review_events(session: Session, batch_id: UUID) -> list[ReviewEvent]:
    return session.scalars(
        select(ReviewEvent)
        .where(ReviewEvent.batch_id == batch_id)
        .order_by(ReviewEvent.created_at, ReviewEvent.id)
    ).all()
