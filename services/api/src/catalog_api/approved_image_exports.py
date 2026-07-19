from __future__ import annotations

import os
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from catalog_api.image_processing import JPEG_CONTENT_TYPE
from catalog_api.models import (
    ImageAsset,
    ProductGroup,
    ProductGroupImage,
    UploadBatch,
)
from catalog_api.processing_storage import (
    WorkerObjectNotFoundError,
    WorkerObjectReadError,
    WorkerStorage,
)
from catalog_api.upload_batches import DEFAULT_ORGANIZATION_ID

APPROVED_IMAGE_EXPORT_ENABLED_ENV = "CATALOG_APPROVED_IMAGE_EXPORT_ENABLED"


class ApprovedImageExportDisabledError(Exception):
    """Raised when approved normalized image export is disabled."""


class ApprovedImageNotFoundError(Exception):
    """Raised when the requested image is unknown or ineligible."""


class ApprovedImageNotApprovedError(Exception):
    """Raised when the requested batch or group is not approved."""


class ApprovedImageUnavailableError(Exception):
    """Raised when normalized image bytes cannot be returned safely."""


@dataclass(frozen=True)
class ApprovedImageExport:
    content: bytes
    content_length: int


def approved_image_export_enabled() -> bool:
    return (
        os.getenv(APPROVED_IMAGE_EXPORT_ENABLED_ENV, "").strip().lower()
        == "true"
    )


def read_approved_normalized_image(
    session: Session,
    *,
    batch_id: UUID,
    group_id: UUID,
    image_id: UUID,
    storage: WorkerStorage,
) -> ApprovedImageExport:
    if not approved_image_export_enabled():
        raise ApprovedImageExportDisabledError

    batch = session.scalar(
        select(UploadBatch).where(
            UploadBatch.id == batch_id,
            UploadBatch.organization_id == DEFAULT_ORGANIZATION_ID,
        )
    )
    if batch is None:
        raise ApprovedImageNotFoundError
    if batch.status != "approved":
        raise ApprovedImageNotApprovedError

    group = session.scalar(
        select(ProductGroup).where(
            ProductGroup.id == group_id,
            ProductGroup.organization_id == batch.organization_id,
            ProductGroup.batch_id == batch.id,
        )
    )
    if group is None:
        raise ApprovedImageNotFoundError
    if group.status != "approved":
        raise ApprovedImageNotApprovedError

    membership = session.scalar(
        select(ProductGroupImage).where(
            ProductGroupImage.organization_id == batch.organization_id,
            ProductGroupImage.batch_id == batch.id,
            ProductGroupImage.group_id == group.id,
            ProductGroupImage.image_id == image_id,
        )
    )
    if (
        membership is None
        or membership.is_rejected
        or membership.is_duplicate
    ):
        raise ApprovedImageNotFoundError

    image = session.scalar(
        select(ImageAsset).where(
            ImageAsset.id == image_id,
            ImageAsset.organization_id == batch.organization_id,
            ImageAsset.batch_id == batch.id,
        )
    )
    if image is None:
        raise ApprovedImageNotFoundError

    object_key = image.normalized_object_key
    expected_size = image.normalized_size_bytes
    if (
        image.status != "processed"
        or object_key is None
        or not object_key.strip()
        or image.normalized_format != JPEG_CONTENT_TYPE
        or expected_size is None
        or expected_size <= 0
    ):
        raise ApprovedImageUnavailableError

    try:
        content = storage.read_object_bytes(object_key=object_key)
    except (WorkerObjectNotFoundError, WorkerObjectReadError) as error:
        raise ApprovedImageUnavailableError from error

    if not content or len(content) != expected_size:
        raise ApprovedImageUnavailableError

    return ApprovedImageExport(
        content=content,
        content_length=len(content),
    )
