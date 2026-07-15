from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from catalog_api.models import (
    Category,
    ImageAsset,
    ProductGroup,
    ProductGroupImage,
    ReviewEvent,
    UploadBatch,
)
from catalog_api.review_groups import ReviewBatchGroupsState, get_review_batch_groups
from catalog_api.upload_batches import DEFAULT_ORGANIZATION_ID

MEMBERSHIP_SOURCE_MANUAL_REVIEW = "manual_review"


class ReviewEditBatchNotFoundError(Exception):
    """Raised when a review edit cannot find its batch."""


class ReviewEditResourceNotFoundError(Exception):
    """Raised when a review edit cannot find a group, image, or membership."""


class ReviewEditStateError(Exception):
    """Raised when a review edit is not allowed for the current state."""


class ReviewEditValidationError(Exception):
    """Raised when a review edit request is invalid."""


@dataclass(frozen=True)
class UpdateGroupPatch:
    cover_image_id: UUID | None = None
    approved_category_id: UUID | None = None
    has_cover_image_id: bool = False
    has_approved_category_id: bool = False


def create_review_group(
    session: Session,
    *,
    batch_id: UUID,
    image_ids: list[UUID],
) -> ReviewBatchGroupsState:
    selected_image_ids = _validated_unique_ids(image_ids, field_name="imageIds")
    batch = _review_batch(session, batch_id=batch_id)
    images_by_id = _images_by_id(session, batch=batch, image_ids=selected_image_ids)
    if set(images_by_id) != set(selected_image_ids):
        raise ReviewEditValidationError("All images must belong to the batch.")

    existing_memberships = _memberships_by_image_id(session, batch=batch)
    if any(image_id not in existing_memberships for image_id in selected_image_ids):
        raise ReviewEditResourceNotFoundError("Image membership was not found.")

    selected_set = set(selected_image_ids)
    for group_id in {
        membership.group_id for membership in existing_memberships.values()
    }:
        group_image_ids = {
            membership.image_id
            for membership in existing_memberships.values()
            if membership.group_id == group_id
        }
        if group_image_ids == selected_set:
            return get_review_batch_groups(session, batch_id=batch.id)

    new_group = ProductGroup(
        organization_id=batch.organization_id,
        batch_id=batch.id,
        status="proposed",
        confidence=None,
    )
    session.add(new_group)
    session.flush()

    moved_from_group_ids: list[str] = []
    for image_id in selected_image_ids:
        membership = existing_memberships[image_id]
        moved_from_group_ids.append(str(membership.group_id))
        session.delete(membership)
    session.flush()

    for position, image_id in enumerate(selected_image_ids):
        session.add(
            _manual_membership(
                batch=batch,
                group_id=new_group.id,
                image_id=image_id,
                position=position,
            )
        )

    _remove_empty_groups(session, batch=batch)
    _normalize_batch_groups(session, batch=batch)
    _write_review_event(
        session,
        batch=batch,
        action_type="create_group",
        group_id=new_group.id,
        image_id=None,
        payload={
            "groupId": str(new_group.id),
            "imageIds": _string_ids(selected_image_ids),
            "movedFromGroupIds": moved_from_group_ids,
        },
    )
    session.commit()
    return get_review_batch_groups(session, batch_id=batch.id)


def move_image_to_group(
    session: Session,
    *,
    target_group_id: UUID,
    image_id: UUID,
) -> ReviewBatchGroupsState:
    target_group, batch = _review_group_and_batch(
        session,
        group_id=target_group_id,
    )
    membership = _membership_for_image(session, batch=batch, image_id=image_id)
    if membership.group_id == target_group.id:
        return get_review_batch_groups(session, batch_id=batch.id)

    source_group_id = membership.group_id
    session.delete(membership)
    session.flush()
    session.add(
        _manual_membership(
            batch=batch,
            group_id=target_group.id,
            image_id=image_id,
            position=_next_group_position(session, batch=batch, group_id=target_group.id),
        )
    )

    _remove_empty_groups(session, batch=batch)
    _normalize_batch_groups(session, batch=batch)
    _write_review_event(
        session,
        batch=batch,
        action_type="move_image",
        group_id=target_group.id,
        image_id=image_id,
        payload={
            "imageId": str(image_id),
            "sourceGroupId": str(source_group_id),
            "targetGroupId": str(target_group.id),
        },
    )
    session.commit()
    return get_review_batch_groups(session, batch_id=batch.id)


def remove_image_from_group(
    session: Session,
    *,
    group_id: UUID,
    image_id: UUID,
) -> ReviewBatchGroupsState:
    group, batch = _review_group_and_batch(session, group_id=group_id)
    membership = _membership_in_group(
        session,
        batch=batch,
        group_id=group.id,
        image_id=image_id,
    )
    group_memberships = _memberships_for_group(session, batch=batch, group_id=group.id)
    if len(group_memberships) == 1:
        return get_review_batch_groups(session, batch_id=batch.id)

    singleton_group = ProductGroup(
        organization_id=batch.organization_id,
        batch_id=batch.id,
        status="proposed",
        confidence=None,
    )
    session.add(singleton_group)
    session.flush()

    session.delete(membership)
    session.flush()
    session.add(
        _manual_membership(
            batch=batch,
            group_id=singleton_group.id,
            image_id=image_id,
            position=0,
        )
    )

    _normalize_batch_groups(session, batch=batch)
    _write_review_event(
        session,
        batch=batch,
        action_type="remove_image",
        group_id=group.id,
        image_id=image_id,
        payload={
            "imageId": str(image_id),
            "sourceGroupId": str(group.id),
            "singletonGroupId": str(singleton_group.id),
        },
    )
    session.commit()
    return get_review_batch_groups(session, batch_id=batch.id)


def merge_review_groups(
    session: Session,
    *,
    target_group_id: UUID,
    source_group_ids: list[UUID],
) -> ReviewBatchGroupsState:
    unique_source_group_ids = _validated_unique_ids(
        source_group_ids,
        field_name="sourceGroupIds",
    )
    if target_group_id in unique_source_group_ids:
        raise ReviewEditValidationError("sourceGroupIds must not include targetGroupId.")

    target_group, batch = _review_group_and_batch(
        session,
        group_id=target_group_id,
    )
    source_groups = _groups_by_id(
        session,
        batch=batch,
        group_ids=unique_source_group_ids,
    )
    if set(source_groups) != set(unique_source_group_ids):
        raise ReviewEditResourceNotFoundError("Source group was not found.")
    _ensure_groups_editable([target_group, *source_groups.values()])

    moved_image_ids: list[UUID] = []
    for source_group_id in unique_source_group_ids:
        source_memberships = _memberships_for_group(
            session,
            batch=batch,
            group_id=source_group_id,
        )
        for membership in source_memberships:
            moved_image_ids.append(membership.image_id)
            session.delete(membership)
    session.flush()

    for source_group_id in unique_source_group_ids:
        session.delete(source_groups[source_group_id])
    session.flush()

    next_position = _next_group_position(
        session,
        batch=batch,
        group_id=target_group.id,
    )
    for offset, image_id in enumerate(moved_image_ids):
        session.add(
            _manual_membership(
                batch=batch,
                group_id=target_group.id,
                image_id=image_id,
                position=next_position + offset,
            )
        )

    _normalize_batch_groups(session, batch=batch)
    _write_review_event(
        session,
        batch=batch,
        action_type="merge_groups",
        group_id=target_group.id,
        image_id=None,
        payload={
            "targetGroupId": str(target_group.id),
            "sourceGroupIds": _string_ids(unique_source_group_ids),
            "movedImageIds": _string_ids(moved_image_ids),
        },
    )
    session.commit()
    return get_review_batch_groups(session, batch_id=batch.id)


def split_review_group(
    session: Session,
    *,
    group_id: UUID,
    image_ids: list[UUID],
) -> ReviewBatchGroupsState:
    selected_image_ids = _validated_unique_ids(image_ids, field_name="imageIds")
    group, batch = _review_group_and_batch(session, group_id=group_id)
    group_memberships = _memberships_for_group(session, batch=batch, group_id=group.id)
    group_image_ids = {membership.image_id for membership in group_memberships}
    selected_set = set(selected_image_ids)

    if not selected_set.issubset(group_image_ids):
        raise ReviewEditValidationError("All selected images must belong to the group.")
    if selected_set == group_image_ids:
        return get_review_batch_groups(session, batch_id=batch.id)

    new_group = ProductGroup(
        organization_id=batch.organization_id,
        batch_id=batch.id,
        status="proposed",
        confidence=None,
    )
    session.add(new_group)
    session.flush()

    for membership in group_memberships:
        if membership.image_id not in selected_set:
            continue
        session.delete(membership)
    session.flush()

    for position, image_id in enumerate(selected_image_ids):
        session.add(
            _manual_membership(
                batch=batch,
                group_id=new_group.id,
                image_id=image_id,
                position=position,
            )
        )

    _normalize_batch_groups(session, batch=batch)
    _write_review_event(
        session,
        batch=batch,
        action_type="split_group",
        group_id=group.id,
        image_id=None,
        payload={
            "sourceGroupId": str(group.id),
            "newGroupId": str(new_group.id),
            "imageIds": _string_ids(selected_image_ids),
        },
    )
    session.commit()
    return get_review_batch_groups(session, batch_id=batch.id)


def update_review_group(
    session: Session,
    *,
    group_id: UUID,
    patch: UpdateGroupPatch,
) -> ReviewBatchGroupsState:
    if patch.has_cover_image_id == patch.has_approved_category_id:
        raise ReviewEditValidationError("Send exactly one group field to update.")

    group, batch = _review_group_and_batch(session, group_id=group_id)
    changed_payload: dict[str, Any] = {"groupId": str(group.id)}

    if patch.has_cover_image_id:
        if patch.cover_image_id is None:
            raise ReviewEditValidationError("coverImageId must not be null.")
        membership = _membership_in_group(
            session,
            batch=batch,
            group_id=group.id,
            image_id=patch.cover_image_id,
        )
        if membership.is_duplicate:
            raise ReviewEditValidationError("coverImageId must not be a duplicate.")
        if group.cover_image_id == patch.cover_image_id:
            return get_review_batch_groups(session, batch_id=batch.id)
        changed_payload["before"] = {"coverImageId": _optional_str(group.cover_image_id)}
        group.cover_image_id = patch.cover_image_id
        changed_payload["after"] = {"coverImageId": str(patch.cover_image_id)}

    if patch.has_approved_category_id:
        if patch.approved_category_id is not None:
            _category(session, category_id=patch.approved_category_id)
        if group.approved_category_id == patch.approved_category_id:
            return get_review_batch_groups(session, batch_id=batch.id)
        changed_payload["before"] = {
            "approvedCategoryId": _optional_str(group.approved_category_id)
        }
        group.approved_category_id = patch.approved_category_id
        changed_payload["after"] = {
            "approvedCategoryId": _optional_str(patch.approved_category_id)
        }

    _write_review_event(
        session,
        batch=batch,
        action_type="update_group",
        group_id=group.id,
        image_id=None,
        payload=changed_payload,
    )
    session.commit()
    return get_review_batch_groups(session, batch_id=batch.id)


def update_group_image_duplicate(
    session: Session,
    *,
    group_id: UUID,
    image_id: UUID,
    is_duplicate: bool,
    duplicate_of_image_id: UUID | None,
) -> ReviewBatchGroupsState:
    group, batch = _review_group_and_batch(session, group_id=group_id)
    membership = _membership_in_group(
        session,
        batch=batch,
        group_id=group.id,
        image_id=image_id,
    )

    if is_duplicate:
        if duplicate_of_image_id is None:
            raise ReviewEditValidationError(
                "duplicateOfImageId is required when isDuplicate is true."
            )
        if duplicate_of_image_id == image_id:
            raise ReviewEditValidationError("An image cannot duplicate itself.")
        retained_membership = _membership_in_group(
            session,
            batch=batch,
            group_id=group.id,
            image_id=duplicate_of_image_id,
        )
        if retained_membership.is_duplicate:
            raise ReviewEditValidationError(
                "duplicateOfImageId must point to a non-duplicate image."
            )
    elif duplicate_of_image_id is not None:
        raise ReviewEditValidationError(
            "duplicateOfImageId must be null when isDuplicate is false."
        )

    if (
        membership.is_duplicate == is_duplicate
        and membership.duplicate_of_image_id == duplicate_of_image_id
    ):
        return get_review_batch_groups(session, batch_id=batch.id)

    before = {
        "isDuplicate": membership.is_duplicate,
        "duplicateOfImageId": _optional_str(membership.duplicate_of_image_id),
    }
    membership.is_duplicate = is_duplicate
    membership.duplicate_of_image_id = duplicate_of_image_id
    membership.membership_source = MEMBERSHIP_SOURCE_MANUAL_REVIEW
    membership.membership_confidence = None
    _normalize_batch_groups(session, batch=batch)

    action_type = "mark_duplicate" if is_duplicate else "restore_duplicate"
    _write_review_event(
        session,
        batch=batch,
        action_type=action_type,
        group_id=group.id,
        image_id=image_id,
        payload={
            "groupId": str(group.id),
            "imageId": str(image_id),
            "before": before,
            "after": {
                "isDuplicate": is_duplicate,
                "duplicateOfImageId": _optional_str(duplicate_of_image_id),
            },
        },
    )
    session.commit()
    return get_review_batch_groups(session, batch_id=batch.id)


def _review_batch(session: Session, *, batch_id: UUID) -> UploadBatch:
    batch = session.scalar(
        select(UploadBatch)
        .where(
            UploadBatch.id == batch_id,
            UploadBatch.organization_id == DEFAULT_ORGANIZATION_ID,
        )
        .with_for_update()
    )
    if batch is None:
        raise ReviewEditBatchNotFoundError
    if batch.status != "review_required":
        raise ReviewEditStateError("Review edits require a review-ready batch.")
    return batch


def _review_group_and_batch(
    session: Session,
    *,
    group_id: UUID,
) -> tuple[ProductGroup, UploadBatch]:
    group = session.scalar(
        select(ProductGroup)
        .where(
            ProductGroup.id == group_id,
            ProductGroup.organization_id == DEFAULT_ORGANIZATION_ID,
        )
        .with_for_update()
    )
    if group is None:
        raise ReviewEditResourceNotFoundError("Group was not found.")
    batch = _review_batch(session, batch_id=group.batch_id)
    _ensure_groups_editable([group])
    return group, batch


def _ensure_groups_editable(groups: list[ProductGroup]) -> None:
    if any(group.status != "proposed" for group in groups):
        raise ReviewEditStateError("Approved groups cannot be edited.")


def _category(session: Session, *, category_id: UUID) -> Category:
    category = session.scalar(
        select(Category).where(
            Category.id == category_id,
            Category.organization_id.is_(None),
            Category.active.is_(True),
        )
    )
    if category is None:
        raise ReviewEditValidationError(
            "approvedCategoryId must be an active global category."
        )
    child_category_id = session.scalar(
        select(Category.id)
        .where(
            Category.parent_id == category_id,
            Category.organization_id.is_(None),
            Category.active.is_(True),
        )
        .limit(1)
    )
    if child_category_id is not None:
        raise ReviewEditValidationError("approvedCategoryId must be a leaf category.")
    return category


def _images_by_id(
    session: Session,
    *,
    batch: UploadBatch,
    image_ids: list[UUID],
) -> dict[UUID, ImageAsset]:
    images = session.scalars(
        select(ImageAsset)
        .where(
            ImageAsset.organization_id == batch.organization_id,
            ImageAsset.batch_id == batch.id,
            ImageAsset.id.in_(image_ids),
        )
        .with_for_update()
    ).all()
    return {image.id: image for image in images}


def _groups_by_id(
    session: Session,
    *,
    batch: UploadBatch,
    group_ids: list[UUID],
) -> dict[UUID, ProductGroup]:
    groups = session.scalars(
        select(ProductGroup)
        .where(
            ProductGroup.organization_id == batch.organization_id,
            ProductGroup.batch_id == batch.id,
            ProductGroup.id.in_(group_ids),
        )
        .with_for_update()
    ).all()
    return {group.id: group for group in groups}


def _memberships_by_image_id(
    session: Session,
    *,
    batch: UploadBatch,
) -> dict[UUID, ProductGroupImage]:
    memberships = session.scalars(
        select(ProductGroupImage)
        .where(
            ProductGroupImage.organization_id == batch.organization_id,
            ProductGroupImage.batch_id == batch.id,
        )
        .with_for_update()
    ).all()
    return {membership.image_id: membership for membership in memberships}


def _membership_for_image(
    session: Session,
    *,
    batch: UploadBatch,
    image_id: UUID,
) -> ProductGroupImage:
    membership = session.scalar(
        select(ProductGroupImage)
        .where(
            ProductGroupImage.organization_id == batch.organization_id,
            ProductGroupImage.batch_id == batch.id,
            ProductGroupImage.image_id == image_id,
        )
        .with_for_update()
    )
    if membership is None:
        raise ReviewEditResourceNotFoundError("Image membership was not found.")
    return membership


def _membership_in_group(
    session: Session,
    *,
    batch: UploadBatch,
    group_id: UUID,
    image_id: UUID,
) -> ProductGroupImage:
    membership = session.scalar(
        select(ProductGroupImage)
        .where(
            ProductGroupImage.organization_id == batch.organization_id,
            ProductGroupImage.batch_id == batch.id,
            ProductGroupImage.group_id == group_id,
            ProductGroupImage.image_id == image_id,
        )
        .with_for_update()
    )
    if membership is None:
        raise ReviewEditResourceNotFoundError("Group image membership was not found.")
    return membership


def _memberships_for_group(
    session: Session,
    *,
    batch: UploadBatch,
    group_id: UUID,
) -> list[ProductGroupImage]:
    return session.scalars(
        select(ProductGroupImage)
        .where(
            ProductGroupImage.organization_id == batch.organization_id,
            ProductGroupImage.batch_id == batch.id,
            ProductGroupImage.group_id == group_id,
        )
        .with_for_update()
    ).all()


def _manual_membership(
    *,
    batch: UploadBatch,
    group_id: UUID,
    image_id: UUID,
    position: int,
) -> ProductGroupImage:
    return ProductGroupImage(
        organization_id=batch.organization_id,
        batch_id=batch.id,
        group_id=group_id,
        image_id=image_id,
        position=position,
        membership_source=MEMBERSHIP_SOURCE_MANUAL_REVIEW,
        membership_confidence=None,
        is_duplicate=False,
        duplicate_of_image_id=None,
    )


def _next_group_position(
    session: Session,
    *,
    batch: UploadBatch,
    group_id: UUID,
) -> int:
    positions = session.scalars(
        select(ProductGroupImage.position).where(
            ProductGroupImage.organization_id == batch.organization_id,
            ProductGroupImage.batch_id == batch.id,
            ProductGroupImage.group_id == group_id,
        )
    ).all()
    return max(positions, default=-1) + 1


def _remove_empty_groups(session: Session, *, batch: UploadBatch) -> None:
    groups = session.scalars(
        select(ProductGroup)
        .where(
            ProductGroup.organization_id == batch.organization_id,
            ProductGroup.batch_id == batch.id,
        )
        .with_for_update()
    ).all()
    memberships_by_group_id: dict[UUID, int] = {}
    for membership in session.scalars(
        select(ProductGroupImage).where(
            ProductGroupImage.organization_id == batch.organization_id,
            ProductGroupImage.batch_id == batch.id,
        )
    ).all():
        memberships_by_group_id[membership.group_id] = (
            memberships_by_group_id.get(membership.group_id, 0) + 1
        )
    for group in groups:
        if memberships_by_group_id.get(group.id, 0) == 0:
            session.delete(group)
    session.flush()


def _normalize_batch_groups(session: Session, *, batch: UploadBatch) -> None:
    rows = session.execute(
        select(ProductGroup, ProductGroupImage, ImageAsset)
        .join(
            ProductGroupImage,
            (ProductGroupImage.group_id == ProductGroup.id)
            & (ProductGroupImage.organization_id == ProductGroup.organization_id)
            & (ProductGroupImage.batch_id == ProductGroup.batch_id),
        )
        .join(
            ImageAsset,
            (ImageAsset.id == ProductGroupImage.image_id)
            & (ImageAsset.organization_id == ProductGroupImage.organization_id)
            & (ImageAsset.batch_id == ProductGroupImage.batch_id),
        )
        .where(
            ProductGroup.organization_id == batch.organization_id,
            ProductGroup.batch_id == batch.id,
        )
        .order_by(ProductGroup.created_at, ProductGroup.id, ImageAsset.upload_order)
        .with_for_update()
    ).all()

    rows_by_group_id: dict[UUID, list[tuple[ProductGroupImage, ImageAsset]]] = {}
    groups_by_id: dict[UUID, ProductGroup] = {}
    for group, membership, image in rows:
        groups_by_id[group.id] = group
        rows_by_group_id.setdefault(group.id, []).append((membership, image))

    for group_id, membership_rows in rows_by_group_id.items():
        group = groups_by_id[group_id]
        ordered_rows = sorted(
            membership_rows,
            key=lambda row: row[1].upload_order,
        )
        for position, (membership, _) in enumerate(ordered_rows):
            membership.position = position

        valid_cover_ids = {
            membership.image_id
            for membership, _ in ordered_rows
            if not membership.is_duplicate
        }
        if group.cover_image_id not in valid_cover_ids:
            group.cover_image_id = _first_non_duplicate_image_id(ordered_rows)


def _first_non_duplicate_image_id(
    rows: list[tuple[ProductGroupImage, ImageAsset]],
) -> UUID | None:
    for membership, _ in rows:
        if not membership.is_duplicate:
            return membership.image_id
    return rows[0][0].image_id if rows else None


def _write_review_event(
    session: Session,
    *,
    batch: UploadBatch,
    action_type: str,
    group_id: UUID | None,
    image_id: UUID | None,
    payload: dict[str, Any],
) -> None:
    session.add(
        ReviewEvent(
            organization_id=batch.organization_id,
            batch_id=batch.id,
            group_id=group_id,
            image_id=image_id,
            user_id=None,
            action_type=action_type,
            payload_json=payload,
            created_at=datetime.now(UTC),
        )
    )


def _validated_unique_ids(ids: list[UUID], *, field_name: str) -> list[UUID]:
    if not ids:
        raise ReviewEditValidationError(f"{field_name} must not be empty.")
    if len(set(ids)) != len(ids):
        raise ReviewEditValidationError(f"{field_name} must not contain duplicates.")
    return ids


def _string_ids(ids: list[UUID]) -> list[str]:
    return [str(value) for value in ids]


def _optional_str(value: UUID | None) -> str | None:
    return str(value) if value is not None else None
