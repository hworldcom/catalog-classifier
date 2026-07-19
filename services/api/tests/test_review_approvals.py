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
class ReviewApprovalFixture:
    batch_id: UUID
    image_ids: list[UUID]
    group_ids: list[UUID]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_approve_review_group_logs_event_and_is_idempotent(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        fixture = _create_review_approval_fixture(session, group_count=1)

    response = await database_client.post(
        f"/v1/groups/{fixture.group_ids[0]}/approve",
    )

    assert response.status_code == 200
    group = _group_by_id(response.json(), fixture.group_ids[0])
    assert group["status"] == "approved"
    assert group["approvedCategorySlug"] == "t-shirts"
    with Session(migrated_engine) as session:
        stored_group = session.get(ProductGroup, fixture.group_ids[0])
        assert stored_group is not None
        assert stored_group.status == "approved"
        assert stored_group.approved_at is not None
        assert _review_event_actions(session, fixture.batch_id) == ["approve_group"]
        approval_event = session.scalar(
            select(ReviewEvent).where(
                ReviewEvent.batch_id == fixture.batch_id,
                ReviewEvent.action_type == "approve_group",
            )
        )
        assert approval_event is not None
        assert approval_event.payload_json == {
            "groupId": str(stored_group.id),
            "approvedCategoryId": str(stored_group.approved_category_id),
            "approvedCategorySlug": "t-shirts",
            "categorySource": "reviewer_selection",
            "approvedAt": stored_group.approved_at.isoformat(),
        }
        first_event_count = _review_event_count(session, fixture.batch_id)

    no_op_response = await database_client.post(
        f"/v1/groups/{fixture.group_ids[0]}/approve",
    )
    rejection_response = await database_client.post(
        f"/v1/groups/{fixture.group_ids[0]}/images/"
        f"{fixture.image_ids[0]}/reject"
    )

    assert no_op_response.status_code == 200
    assert rejection_response.status_code == 409
    assert rejection_response.json()["detail"]["code"] == "review_edit_not_allowed"
    with Session(migrated_engine) as session:
        assert _review_event_count(session, fixture.batch_id) == first_event_count


async def test_approve_review_batch_requires_approved_groups_then_locks_batch(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        fixture = _create_review_approval_fixture(session, group_count=2)
        initial_membership_count = _membership_count(session, fixture.batch_id)

    first_group_response = await database_client.post(
        f"/v1/groups/{fixture.group_ids[0]}/approve",
    )
    incomplete_response = await database_client.post(
        f"/v1/upload-batches/{fixture.batch_id}/approve",
    )

    assert first_group_response.status_code == 200
    assert incomplete_response.status_code == 409
    assert incomplete_response.json()["detail"] == {
        "code": "review_approval_not_allowed",
        "message": "All groups must be approved before batch approval.",
    }

    second_group_response = await database_client.post(
        f"/v1/groups/{fixture.group_ids[1]}/approve",
    )
    batch_response = await database_client.post(
        f"/v1/upload-batches/{fixture.batch_id}/approve",
    )

    assert second_group_response.status_code == 200
    assert batch_response.status_code == 200
    body = batch_response.json()
    assert body["status"] == "approved"
    assert {group["status"] for group in body["groups"]} == {"approved"}

    with Session(migrated_engine) as session:
        batch = session.get(UploadBatch, fixture.batch_id)
        assert batch is not None
        assert batch.status == "approved"
        assert batch.completed_at is not None
        assert _membership_count(session, fixture.batch_id) == initial_membership_count
        assert _review_event_actions(session, fixture.batch_id) == [
            "approve_group",
            "approve_group",
            "approve_batch",
        ]
        first_event_count = _review_event_count(session, fixture.batch_id)

    read_response = await database_client.get(
        f"/v1/upload-batches/{fixture.batch_id}/groups",
    )
    repeated_batch_response = await database_client.post(
        f"/v1/upload-batches/{fixture.batch_id}/approve",
    )
    repeated_group_response = await database_client.post(
        f"/v1/groups/{fixture.group_ids[0]}/approve",
    )
    edit_response = await database_client.patch(
        f"/v1/groups/{fixture.group_ids[0]}",
        json={"coverImageId": str(fixture.image_ids[0])},
    )
    rejection_response = await database_client.post(
        f"/v1/groups/{fixture.group_ids[0]}/images/"
        f"{fixture.image_ids[0]}/reject"
    )

    assert read_response.status_code == 200
    assert read_response.json()["status"] == "approved"
    assert repeated_batch_response.status_code == 200
    assert repeated_group_response.status_code == 200
    assert edit_response.status_code == 409
    assert edit_response.json()["detail"]["code"] == "review_edit_not_allowed"
    assert rejection_response.status_code == 409
    assert rejection_response.json()["detail"]["code"] == "review_edit_not_allowed"
    with Session(migrated_engine) as session:
        assert _review_event_count(session, fixture.batch_id) == first_event_count


async def test_approve_review_group_requires_approved_category(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        fixture = _create_review_approval_fixture(
            session,
            group_count=1,
            has_approved_category=False,
        )

    response = await database_client.post(
        f"/v1/groups/{fixture.group_ids[0]}/approve",
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "review_approval_not_allowed",
        "message": "Group approval requires an approved category.",
    }
    with Session(migrated_engine) as session:
        stored_group = session.get(ProductGroup, fixture.group_ids[0])
        assert stored_group is not None
        assert stored_group.status == "proposed"
        assert stored_group.approved_at is None
        assert _review_event_count(session, fixture.batch_id) == 0


async def test_approve_review_group_requires_active_non_duplicate_image(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        fixture = _create_review_approval_fixture(session, group_count=1)

    reject_response = await database_client.post(
        f"/v1/groups/{fixture.group_ids[0]}/images/"
        f"{fixture.image_ids[0]}/reject"
    )
    approval_response = await database_client.post(
        f"/v1/groups/{fixture.group_ids[0]}/approve",
    )

    assert reject_response.status_code == 200
    rejected_group = _group_by_id(reject_response.json(), fixture.group_ids[0])
    assert rejected_group["coverImageId"] is None
    assert approval_response.status_code == 409
    assert approval_response.json()["detail"] == {
        "code": "review_approval_not_allowed",
        "message": (
            "Group approval requires at least one active non-duplicate image."
        ),
    }
    with Session(migrated_engine) as session:
        stored_group = session.get(ProductGroup, fixture.group_ids[0])
        assert stored_group is not None
        assert stored_group.status == "proposed"
        assert stored_group.approved_at is None
        assert _review_event_actions(session, fixture.batch_id) == [
            "reject_image"
        ]


async def test_approve_review_batch_allows_empty_review_batch(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        batch_id = _create_empty_review_batch(session)

    response = await database_client.post(f"/v1/upload-batches/{batch_id}/approve")

    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert response.json()["groups"] == []
    with Session(migrated_engine) as session:
        batch = session.get(UploadBatch, batch_id)
        assert batch is not None
        assert batch.status == "approved"
        assert batch.completed_at is not None
        assert _review_event_actions(session, batch_id) == ["approve_batch"]


async def test_review_approval_rejects_non_review_batches(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        fixture = _create_review_approval_fixture(
            session,
            group_count=1,
            status="processing",
        )

    group_response = await database_client.post(
        f"/v1/groups/{fixture.group_ids[0]}/approve",
    )
    batch_response = await database_client.post(
        f"/v1/upload-batches/{fixture.batch_id}/approve",
    )

    assert group_response.status_code == 409
    assert group_response.json()["detail"] == {
        "code": "review_approval_not_allowed",
        "message": "Batch is not ready for review approval.",
    }
    assert batch_response.status_code == 409
    assert batch_response.json()["detail"] == {
        "code": "review_approval_not_allowed",
        "message": "Batch is not ready for review approval.",
    }


def _create_review_approval_fixture(
    session: Session,
    *,
    group_count: int,
    status: str = "review_required",
    has_approved_category: bool = True,
) -> ReviewApprovalFixture:
    category = session.scalar(select(Category).where(Category.slug == "t-shirts"))
    assert category is not None

    batch = UploadBatch(
        organization_id=DEFAULT_ORGANIZATION_ID,
        status=status,
        original_file_count=group_count,
        processed_file_count=group_count,
        finalized_at=datetime.now(UTC),
        pipeline_version=PIPELINE_VERSION,
    )
    session.add(batch)
    session.flush()

    image_ids: list[UUID] = []
    group_ids: list[UUID] = []
    for upload_order in range(group_count):
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
            approved_category_id=category.id if has_approved_category else None,
            approved_category_source=(
                "reviewer_selection" if has_approved_category else None
            ),
            cover_image_id=image_id,
            confidence=1.0,
        )
        session.add(group)
        session.flush()
        group_ids.append(group.id)
        session.add(
            ProductGroupImage(
                organization_id=DEFAULT_ORGANIZATION_ID,
                batch_id=batch.id,
                group_id=group.id,
                image_id=image_id,
                position=0,
                membership_source="singleton",
                membership_confidence=None,
            )
        )

    session.commit()
    return ReviewApprovalFixture(
        batch_id=batch.id,
        image_ids=image_ids,
        group_ids=group_ids,
    )


def _create_empty_review_batch(session: Session) -> UUID:
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


def _group_by_id(snapshot: dict, group_id: UUID) -> dict:
    for group in snapshot["groups"]:
        if group["groupId"] == str(group_id):
            return group
    raise AssertionError(f"Group {group_id} was not found.")


def _membership_count(session: Session, batch_id: UUID) -> int:
    count = session.scalar(
        select(func.count()).select_from(ProductGroupImage).where(
            ProductGroupImage.batch_id == batch_id
        )
    )
    assert count is not None
    return count


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
