from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from catalog_api.models import ProductGroup, ReviewEvent, UploadBatch
from catalog_api.review_groups import ReviewBatchGroupsState, get_review_batch_groups
from catalog_api.upload_batches import DEFAULT_ORGANIZATION_ID


class ReviewApprovalBatchNotFoundError(Exception):
    """Raised when a review approval cannot find its batch."""


class ReviewApprovalResourceNotFoundError(Exception):
    """Raised when a review approval cannot find its group."""


class ReviewApprovalStateError(Exception):
    """Raised when a review approval is not allowed for the current state."""


def approve_review_group(
    session: Session,
    *,
    group_id: UUID,
) -> ReviewBatchGroupsState:
    group = session.scalar(
        select(ProductGroup)
        .where(
            ProductGroup.id == group_id,
            ProductGroup.organization_id == DEFAULT_ORGANIZATION_ID,
        )
        .with_for_update()
    )
    if group is None:
        raise ReviewApprovalResourceNotFoundError("Group was not found.")

    batch = _approval_batch(session, batch_id=group.batch_id)
    if group.status == "approved":
        return get_review_batch_groups(session, batch_id=batch.id)
    if batch.status != "review_required":
        raise ReviewApprovalStateError(
            "Group approval requires a review-ready batch."
        )
    if group.status != "proposed":
        raise ReviewApprovalStateError("Only proposed groups can be approved.")

    group.status = "approved"
    group.approved_at = datetime.now(UTC)
    _write_review_event(
        session,
        batch=batch,
        action_type="approve_group",
        group_id=group.id,
        payload={
            "groupId": str(group.id),
            "approvedAt": group.approved_at.isoformat(),
        },
    )
    session.commit()
    return get_review_batch_groups(session, batch_id=batch.id)


def approve_review_batch(
    session: Session,
    *,
    batch_id: UUID,
) -> ReviewBatchGroupsState:
    batch = _approval_batch(session, batch_id=batch_id)
    if batch.status == "approved":
        return get_review_batch_groups(session, batch_id=batch.id)
    if batch.status != "review_required":
        raise ReviewApprovalStateError(
            "Batch approval requires a review-ready batch."
        )

    groups = session.scalars(
        select(ProductGroup)
        .where(
            ProductGroup.organization_id == batch.organization_id,
            ProductGroup.batch_id == batch.id,
        )
        .with_for_update()
    ).all()
    proposed_group_ids = [group.id for group in groups if group.status != "approved"]
    if proposed_group_ids:
        raise ReviewApprovalStateError(
            "All groups must be approved before batch approval."
        )

    batch.status = "approved"
    batch.completed_at = datetime.now(UTC)
    _write_review_event(
        session,
        batch=batch,
        action_type="approve_batch",
        group_id=None,
        payload={
            "batchId": str(batch.id),
            "groupIds": [str(group.id) for group in groups],
            "completedAt": batch.completed_at.isoformat(),
        },
    )
    session.commit()
    return get_review_batch_groups(session, batch_id=batch.id)


def _approval_batch(session: Session, *, batch_id: UUID) -> UploadBatch:
    batch = session.scalar(
        select(UploadBatch)
        .where(
            UploadBatch.id == batch_id,
            UploadBatch.organization_id == DEFAULT_ORGANIZATION_ID,
        )
        .with_for_update()
    )
    if batch is None:
        raise ReviewApprovalBatchNotFoundError
    if batch.status not in {"review_required", "approved"}:
        raise ReviewApprovalStateError("Batch is not ready for review approval.")
    return batch


def _write_review_event(
    session: Session,
    *,
    batch: UploadBatch,
    action_type: str,
    group_id: UUID | None,
    payload: dict[str, Any],
) -> None:
    session.add(
        ReviewEvent(
            organization_id=batch.organization_id,
            batch_id=batch.id,
            group_id=group_id,
            image_id=None,
            user_id=None,
            action_type=action_type,
            payload_json=payload,
            created_at=datetime.now(UTC),
        )
    )
