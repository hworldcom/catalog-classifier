from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from catalog_api.models import (
    Category,
    ImageClassification,
    ProcessingJob,
    ProductGroup,
    ProductGroupImage,
    UploadBatch,
)

CLASSIFY_IMAGE_JOB_TYPE = "classify-image"
CATEGORY_SUGGESTION_PENDING = "pending"
CATEGORY_SUGGESTION_READY = "ready"
CATEGORY_SUGGESTION_UNAVAILABLE = "unavailable"
APPROVED_CATEGORY_SOURCE_MACHINE = "machine_suggestion"
APPROVED_CATEGORY_SOURCE_REVIEWER = "reviewer_selection"
APPROVED_CATEGORY_SOURCE_CLEARED = "reviewer_cleared"
REVIEWER_CATEGORY_SOURCES = {
    APPROVED_CATEGORY_SOURCE_REVIEWER,
    APPROVED_CATEGORY_SOURCE_CLEARED,
}


@dataclass(frozen=True)
class GroupCategorySuggestion:
    status: str | None
    category_id: UUID | None


def reconcile_group_category(
    session: Session,
    *,
    group_id: UUID,
) -> GroupCategorySuggestion | None:
    group = session.scalar(
        select(ProductGroup)
        .where(ProductGroup.id == group_id)
        .with_for_update()
    )
    if group is None or group.status == "approved":
        return None

    batch = session.scalar(
        select(UploadBatch).where(
            UploadBatch.id == group.batch_id,
            UploadBatch.organization_id == group.organization_id,
        )
    )
    if batch is None:
        return None

    suggestion = evaluate_group_category_suggestion(
        session,
        batch=batch,
        group=group,
    )
    group.suggested_category_id = (
        suggestion.category_id
        if suggestion.status == CATEGORY_SUGGESTION_READY
        else None
    )

    if group.approved_category_source in REVIEWER_CATEGORY_SOURCES:
        return suggestion

    if suggestion.status == CATEGORY_SUGGESTION_READY:
        group.approved_category_id = suggestion.category_id
        group.approved_category_source = APPROVED_CATEGORY_SOURCE_MACHINE
    elif group.approved_category_source == APPROVED_CATEGORY_SOURCE_MACHINE:
        group.approved_category_id = None

    return suggestion


def reconcile_batch_group_categories(
    session: Session,
    *,
    batch_id: UUID,
    organization_id: UUID,
) -> None:
    group_ids = session.scalars(
        select(ProductGroup.id)
        .where(
            ProductGroup.batch_id == batch_id,
            ProductGroup.organization_id == organization_id,
            ProductGroup.status == "proposed",
        )
        .order_by(ProductGroup.id)
    ).all()
    for group_id in group_ids:
        reconcile_group_category(session, group_id=group_id)


def reconcile_groups_for_image(
    session: Session,
    *,
    batch_id: UUID,
    organization_id: UUID,
    image_id: UUID,
) -> None:
    group_ids = session.scalars(
        select(ProductGroupImage.group_id).where(
            ProductGroupImage.batch_id == batch_id,
            ProductGroupImage.organization_id == organization_id,
            ProductGroupImage.image_id == image_id,
        )
    ).all()
    for group_id in sorted(group_ids):
        reconcile_group_category(session, group_id=group_id)


def evaluate_group_category_suggestion(
    session: Session,
    *,
    batch: UploadBatch,
    group: ProductGroup,
) -> GroupCategorySuggestion:
    if group.status == "approved":
        return GroupCategorySuggestion(status=None, category_id=None)
    if batch.pipeline_version is None:
        return GroupCategorySuggestion(
            status=CATEGORY_SUGGESTION_UNAVAILABLE,
            category_id=None,
        )

    eligible_image_ids = session.scalars(
        select(ProductGroupImage.image_id).where(
            ProductGroupImage.organization_id == group.organization_id,
            ProductGroupImage.batch_id == group.batch_id,
            ProductGroupImage.group_id == group.id,
            ProductGroupImage.is_duplicate.is_(False),
            ProductGroupImage.is_rejected.is_(False),
        )
    ).all()
    if not eligible_image_ids:
        return GroupCategorySuggestion(
            status=CATEGORY_SUGGESTION_UNAVAILABLE,
            category_id=None,
        )

    classify_jobs = session.execute(
        select(ProcessingJob.image_id, ProcessingJob.status).where(
            ProcessingJob.organization_id == group.organization_id,
            ProcessingJob.batch_id == group.batch_id,
            ProcessingJob.image_id.in_(eligible_image_ids),
            ProcessingJob.job_type == CLASSIFY_IMAGE_JOB_TYPE,
            ProcessingJob.pipeline_version == batch.pipeline_version,
        )
    ).all()
    if any(status in {"pending", "started"} for _, status in classify_jobs):
        return GroupCategorySuggestion(
            status=CATEGORY_SUGGESTION_PENDING,
            category_id=None,
        )

    completed_image_ids = [
        image_id
        for image_id, status in classify_jobs
        if image_id is not None and status == "completed"
    ]
    if not completed_image_ids:
        return GroupCategorySuggestion(
            status=CATEGORY_SUGGESTION_UNAVAILABLE,
            category_id=None,
        )

    category_ids = set(
        session.scalars(
            select(ImageClassification.category_id).where(
                ImageClassification.organization_id == group.organization_id,
                ImageClassification.image_id.in_(completed_image_ids),
                ImageClassification.pipeline_version == batch.pipeline_version,
                ImageClassification.category_id.is_not(None),
            )
        ).all()
    )
    category_ids.discard(None)
    if len(category_ids) != 1:
        return GroupCategorySuggestion(
            status=CATEGORY_SUGGESTION_UNAVAILABLE,
            category_id=None,
        )

    category_id = next(iter(category_ids))
    if active_global_leaf_category(session, category_id=category_id) is None:
        return GroupCategorySuggestion(
            status=CATEGORY_SUGGESTION_UNAVAILABLE,
            category_id=None,
        )

    return GroupCategorySuggestion(
        status=CATEGORY_SUGGESTION_READY,
        category_id=category_id,
    )


def active_global_leaf_category(
    session: Session,
    *,
    category_id: UUID,
) -> Category | None:
    category = session.scalar(
        select(Category).where(
            Category.id == category_id,
            Category.organization_id.is_(None),
            Category.active.is_(True),
        )
    )
    if category is None:
        return None

    child_category_id = session.scalar(
        select(Category.id)
        .where(
            Category.parent_id == category.id,
            Category.organization_id.is_(None),
            Category.active.is_(True),
        )
        .limit(1)
    )
    return category if child_category_id is None else None
