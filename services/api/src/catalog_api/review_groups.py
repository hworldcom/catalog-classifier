from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from catalog_api.models import (
    Category,
    ImageAsset,
    ProductGroup,
    ProductGroupImage,
    UploadBatch,
)
from catalog_api.upload_batches import DEFAULT_ORGANIZATION_ID

REVIEW_BATCH_STATUSES = {"review_required", "approved"}


class ReviewBatchNotFoundError(Exception):
    """Raised when a batch cannot be found for review."""


class ReviewBatchStateError(Exception):
    """Raised when a batch has not entered the review phase."""


@dataclass(frozen=True)
class ReviewGroupImageState:
    image_id: UUID
    original_filename: str
    upload_order: int
    thumbnail_url: str
    position: int
    is_duplicate: bool
    duplicate_of_image_id: UUID | None
    membership_source: str
    membership_confidence: float | None


@dataclass(frozen=True)
class ReviewGroupState:
    group_id: UUID
    status: str
    confidence: float | None
    cover_image_id: UUID | None
    suggested_category_slug: str | None
    approved_category_slug: str | None
    possible_existing_product_id: UUID | None
    warnings: list[str]
    images: list[ReviewGroupImageState]


@dataclass(frozen=True)
class ReviewBatchGroupsState:
    batch_id: UUID
    organization_id: UUID
    status: str
    pipeline_version: str | None
    groups: list[ReviewGroupState]


def get_review_batch_groups(
    session: Session,
    *,
    batch_id: UUID,
) -> ReviewBatchGroupsState:
    batch = session.scalar(
        select(UploadBatch).where(
            UploadBatch.id == batch_id,
            UploadBatch.organization_id == DEFAULT_ORGANIZATION_ID,
        )
    )
    if batch is None:
        raise ReviewBatchNotFoundError
    if batch.status not in REVIEW_BATCH_STATUSES:
        raise ReviewBatchStateError("Batch is not ready for review.")

    groups = session.scalars(
        select(ProductGroup)
        .where(
            ProductGroup.organization_id == batch.organization_id,
            ProductGroup.batch_id == batch.id,
        )
        .order_by(ProductGroup.created_at, ProductGroup.id)
    ).all()
    category_slugs = _category_slugs_by_id(
        session,
        category_ids=[
            category_id
            for group in groups
            for category_id in (
                group.suggested_category_id,
                group.approved_category_id,
            )
            if category_id is not None
        ],
    )
    images_by_group_id = _review_images_by_group_id(
        session,
        organization_id=batch.organization_id,
        batch_id=batch.id,
    )

    return ReviewBatchGroupsState(
        batch_id=batch.id,
        organization_id=batch.organization_id,
        status=batch.status,
        pipeline_version=batch.pipeline_version,
        groups=[
            ReviewGroupState(
                group_id=group.id,
                status=group.status,
                confidence=group.confidence,
                cover_image_id=group.cover_image_id,
                suggested_category_slug=(
                    category_slugs.get(group.suggested_category_id)
                    if group.suggested_category_id is not None
                    else None
                ),
                approved_category_slug=(
                    category_slugs.get(group.approved_category_id)
                    if group.approved_category_id is not None
                    else None
                ),
                possible_existing_product_id=group.possible_existing_product_id,
                warnings=[],
                images=images_by_group_id.get(group.id, []),
            )
            for group in groups
        ],
    )


def _category_slugs_by_id(
    session: Session,
    *,
    category_ids: list[UUID],
) -> dict[UUID, str]:
    if not category_ids:
        return {}
    categories = session.scalars(
        select(Category).where(Category.id.in_(set(category_ids)))
    ).all()
    return {category.id: category.slug for category in categories}


def _review_images_by_group_id(
    session: Session,
    *,
    organization_id: UUID,
    batch_id: UUID,
) -> dict[UUID, list[ReviewGroupImageState]]:
    rows = session.execute(
        select(ProductGroupImage, ImageAsset)
        .join(
            ImageAsset,
            and_(
                ImageAsset.id == ProductGroupImage.image_id,
                ImageAsset.organization_id == ProductGroupImage.organization_id,
                ImageAsset.batch_id == ProductGroupImage.batch_id,
            ),
        )
        .where(
            ProductGroupImage.organization_id == organization_id,
            ProductGroupImage.batch_id == batch_id,
        )
        .order_by(ProductGroupImage.group_id, ProductGroupImage.position)
    ).all()
    images_by_group_id: dict[UUID, list[ReviewGroupImageState]] = {}
    for membership, image in rows:
        images_by_group_id.setdefault(membership.group_id, []).append(
            ReviewGroupImageState(
                image_id=image.id,
                original_filename=image.original_filename,
                upload_order=image.upload_order,
                thumbnail_url=(
                    f"/v1/upload-batches/{batch_id}/images/{image.id}/thumbnail"
                ),
                position=membership.position,
                is_duplicate=membership.is_duplicate,
                duplicate_of_image_id=membership.duplicate_of_image_id,
                membership_source=membership.membership_source,
                membership_confidence=membership.membership_confidence,
            )
        )
    return images_by_group_id
