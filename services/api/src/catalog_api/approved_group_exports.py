from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from catalog_api.models import (
    Category,
    ProductGroup,
    ProductGroupImage,
    UploadBatch,
)
from catalog_api.upload_batches import DEFAULT_ORGANIZATION_ID

APPROVED_GROUPS_EXPORT_ENABLED_ENV = "CATALOG_APPROVED_GROUPS_EXPORT_ENABLED"


class ApprovedGroupsExportDisabledError(Exception):
    """Raised when the local approved-groups prototype is disabled."""


class ApprovedGroupsBatchNotFoundError(Exception):
    """Raised when an approved-groups request cannot find its batch."""


class ApprovedGroupsBatchStateError(Exception):
    """Raised when an approved-groups request targets a non-approved batch."""


class ApprovedGroupsInvalidError(Exception):
    """Raised when an approved export snapshot violates its contract."""


@dataclass(frozen=True)
class ApprovedGroupExportImageState:
    image_id: UUID
    position: int
    is_duplicate: bool
    duplicate_of_image_id: UUID | None


@dataclass(frozen=True)
class ApprovedGroupExportState:
    group_id: UUID
    approved_category_slug: str
    suggested_category_slug: str | None
    cover_image_id: UUID
    confidence: float | None
    images: list[ApprovedGroupExportImageState]


@dataclass(frozen=True)
class ApprovedGroupsExportState:
    batch_id: UUID
    organization_id: UUID
    status: str
    pipeline_version: str
    groups: list[ApprovedGroupExportState]


def approved_groups_export_enabled() -> bool:
    return (
        os.getenv(APPROVED_GROUPS_EXPORT_ENABLED_ENV, "").strip().lower()
        == "true"
    )


def get_approved_groups_export(
    session: Session,
    *,
    batch_id: UUID,
) -> ApprovedGroupsExportState:
    if not approved_groups_export_enabled():
        raise ApprovedGroupsExportDisabledError

    batch = session.scalar(
        select(UploadBatch).where(
            UploadBatch.id == batch_id,
            UploadBatch.organization_id == DEFAULT_ORGANIZATION_ID,
        )
    )
    if batch is None:
        raise ApprovedGroupsBatchNotFoundError
    if batch.status != "approved":
        raise ApprovedGroupsBatchStateError

    pipeline_version = batch.pipeline_version
    if pipeline_version is None or not pipeline_version.strip():
        raise ApprovedGroupsInvalidError("Pipeline version is missing.")

    groups = session.scalars(
        select(ProductGroup)
        .where(
            ProductGroup.organization_id == batch.organization_id,
            ProductGroup.batch_id == batch.id,
        )
        .order_by(ProductGroup.created_at, ProductGroup.id)
    ).all()
    memberships_by_group_id = _active_memberships_by_group_id(
        session,
        organization_id=batch.organization_id,
        batch_id=batch.id,
    )
    category_slugs = _category_slugs_by_id(
        session,
        category_ids={
            category_id
            for group in groups
            for category_id in (
                group.approved_category_id,
                group.suggested_category_id,
            )
            if category_id is not None
        },
    )

    export_groups = [
        _approved_group_state(
            group=group,
            memberships=memberships_by_group_id.get(group.id, []),
            category_slugs=category_slugs,
        )
        for group in groups
    ]
    return ApprovedGroupsExportState(
        batch_id=batch.id,
        organization_id=batch.organization_id,
        status=batch.status,
        pipeline_version=pipeline_version,
        groups=export_groups,
    )


def _active_memberships_by_group_id(
    session: Session,
    *,
    organization_id: UUID,
    batch_id: UUID,
) -> dict[UUID, list[ProductGroupImage]]:
    memberships = session.scalars(
        select(ProductGroupImage)
        .where(
            ProductGroupImage.organization_id == organization_id,
            ProductGroupImage.batch_id == batch_id,
            ProductGroupImage.is_rejected.is_(False),
        )
        .order_by(ProductGroupImage.group_id, ProductGroupImage.position)
    ).all()
    memberships_by_group_id: dict[UUID, list[ProductGroupImage]] = defaultdict(list)
    for membership in memberships:
        memberships_by_group_id[membership.group_id].append(membership)
    return dict(memberships_by_group_id)


def _category_slugs_by_id(
    session: Session,
    *,
    category_ids: set[UUID],
) -> dict[UUID, str]:
    if not category_ids:
        return {}
    categories = session.scalars(
        select(Category).where(Category.id.in_(category_ids))
    ).all()
    return {category.id: category.slug for category in categories}


def _approved_group_state(
    *,
    group: ProductGroup,
    memberships: list[ProductGroupImage],
    category_slugs: dict[UUID, str],
) -> ApprovedGroupExportState:
    if group.status != "approved":
        raise ApprovedGroupsInvalidError("The batch contains an unapproved group.")

    approved_category_slug = category_slugs.get(group.approved_category_id)
    if approved_category_slug is None or not approved_category_slug.strip():
        raise ApprovedGroupsInvalidError("The approved category slug is missing.")

    active_non_duplicate_ids = {
        membership.image_id
        for membership in memberships
        if not membership.is_duplicate
    }
    if not active_non_duplicate_ids:
        raise ApprovedGroupsInvalidError(
            "The group has no active non-duplicate image."
        )
    if group.cover_image_id not in active_non_duplicate_ids:
        raise ApprovedGroupsInvalidError("The group cover is not exportable.")

    export_images: list[ApprovedGroupExportImageState] = []
    for membership in memberships:
        duplicate_of_image_id = membership.duplicate_of_image_id
        if membership.is_duplicate:
            if duplicate_of_image_id not in active_non_duplicate_ids:
                raise ApprovedGroupsInvalidError(
                    "An active duplicate has no exportable retained image."
                )
        elif duplicate_of_image_id is not None:
            raise ApprovedGroupsInvalidError(
                "A non-duplicate membership has duplicate metadata."
            )

        export_images.append(
            ApprovedGroupExportImageState(
                image_id=membership.image_id,
                position=membership.position,
                is_duplicate=membership.is_duplicate,
                duplicate_of_image_id=duplicate_of_image_id,
            )
        )

    suggested_category_slug = (
        category_slugs.get(group.suggested_category_id)
        if group.suggested_category_id is not None
        else None
    )
    return ApprovedGroupExportState(
        group_id=group.id,
        approved_category_slug=approved_category_slug,
        suggested_category_slug=suggested_category_slug,
        cover_image_id=group.cover_image_id,
        confidence=group.confidence,
        images=export_images,
    )
