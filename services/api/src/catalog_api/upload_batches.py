from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from catalog_api.image_uploads import MAX_FILES_PER_REQUEST, MAX_FILE_SIZE_BYTES
from catalog_api.models import ImageAsset, UploadBatch
from catalog_api.upload_storage import (
    UploadObjectInspector,
    UploadObjectInspectionError,
    UploadObjectNotFoundError,
    UploadUrlSigner,
    UploadUrlSigningError,
)

DEFAULT_ORGANIZATION_ID = UUID("00000000-0000-0000-0000-000000000001")
JPEG_MIME_TYPE = "image/jpeg"


class UploadBatchCreationError(Exception):
    """Raised when a durable upload batch cannot be created."""


class InvalidUploadMetadataError(Exception):
    """Raised when file registration metadata violates the upload contract."""


class UploadBatchNotFoundError(Exception):
    """Raised when a durable upload batch does not exist."""


class UploadBatchStateError(Exception):
    """Raised when a batch cannot accept file registration."""


class UploadRegistrationError(Exception):
    """Raised when file registration cannot be persisted."""


class InvalidRetrySelectionError(Exception):
    """Raised when upload retry identifiers violate the retry contract."""


class UploadRetryError(Exception):
    """Raised when upload retries cannot be signed or persisted."""


@dataclass(frozen=True)
class UploadFileMetadata:
    original_filename: str
    mime_type: str
    size_bytes: int


@dataclass(frozen=True)
class RegisteredUpload:
    image_id: UUID
    upload_order: int
    original_filename: str
    original_object_key: str
    upload_url: str


@dataclass(frozen=True)
class UploadRegistration:
    batch_id: UUID
    status: str
    uploads: list[RegisteredUpload]


@dataclass(frozen=True)
class UploadBatchImageState:
    image_id: UUID
    upload_order: int
    original_filename: str
    status: str
    error_code: str | None
    error_message: str | None


@dataclass(frozen=True)
class UploadBatchState:
    batch_id: UUID
    status: str
    original_file_count: int
    processed_file_count: int
    created_at: datetime
    finalized_at: datetime | None
    completed_at: datetime | None
    images: list[UploadBatchImageState]


def create_upload_batch(session: Session) -> UploadBatch:
    batch = UploadBatch(organization_id=DEFAULT_ORGANIZATION_ID)
    session.add(batch)

    try:
        session.commit()
        session.refresh(batch)
    except SQLAlchemyError as error:
        session.rollback()
        raise UploadBatchCreationError from error

    return batch


def register_upload_files(
    session: Session,
    *,
    batch_id: UUID,
    files: list[UploadFileMetadata],
    signer: UploadUrlSigner,
) -> UploadRegistration:
    _validate_upload_metadata(files)

    try:
        batch = session.scalar(
            select(UploadBatch)
            .where(
                UploadBatch.id == batch_id,
                UploadBatch.organization_id == DEFAULT_ORGANIZATION_ID,
            )
            .with_for_update()
        )
        if batch is None:
            raise UploadBatchNotFoundError
        if batch.status != "created":
            raise UploadBatchStateError

        registered_uploads = []
        for upload_order, file in enumerate(files):
            image_id = uuid4()
            object_key = (
                f"organizations/{batch.organization_id}/batches/{batch.id}/"
                f"originals/{image_id}.jpg"
            )
            upload_url = signer.sign_upload_url(
                object_key=object_key,
                content_type=JPEG_MIME_TYPE,
            )
            session.add(
                ImageAsset(
                    id=image_id,
                    organization_id=batch.organization_id,
                    batch_id=batch.id,
                    original_object_key=object_key,
                    original_filename=file.original_filename,
                    upload_order=upload_order,
                    mime_type=file.mime_type,
                    size_bytes=file.size_bytes,
                )
            )
            registered_uploads.append(
                RegisteredUpload(
                    image_id=image_id,
                    upload_order=upload_order,
                    original_filename=file.original_filename,
                    original_object_key=object_key,
                    upload_url=upload_url,
                )
            )

        batch.status = "uploading"
        batch.original_file_count = len(files)
        batch.processed_file_count = 0
        session.commit()
    except (UploadBatchNotFoundError, UploadBatchStateError):
        session.rollback()
        raise
    except (SQLAlchemyError, UploadUrlSigningError) as error:
        session.rollback()
        raise UploadRegistrationError from error

    return UploadRegistration(
        batch_id=batch_id,
        status="uploading",
        uploads=registered_uploads,
    )


def get_upload_batch(
    session: Session,
    *,
    batch_id: UUID,
) -> UploadBatchState:
    batch = _load_upload_batch(session, batch_id)
    if batch is None:
        raise UploadBatchNotFoundError

    images = _load_upload_batch_images(session, batch_id)
    return _serialize_upload_batch(batch=batch, images=images)


def retry_failed_uploads(
    session: Session,
    *,
    batch_id: UUID,
    image_ids: list[UUID],
    signer: UploadUrlSigner,
) -> UploadRegistration:
    if not image_ids:
        raise InvalidRetrySelectionError("Select at least one image to retry.")
    if len(image_ids) != len(set(image_ids)):
        raise InvalidRetrySelectionError("Retry image identifiers must be unique.")

    try:
        batch = _load_upload_batch(session, batch_id, for_update=True)
        if batch is None:
            raise UploadBatchNotFoundError
        if batch.status != "uploading":
            raise UploadBatchStateError

        images = session.scalars(
            select(ImageAsset)
            .where(
                ImageAsset.id.in_(image_ids),
                ImageAsset.batch_id == batch_id,
                ImageAsset.organization_id == DEFAULT_ORGANIZATION_ID,
            )
            .order_by(ImageAsset.upload_order)
            .with_for_update()
        ).all()
        if len(images) != len(image_ids):
            raise InvalidRetrySelectionError(
                "Every retry image must belong to the upload batch."
            )
        if any(image.status not in {"pending", "failed"} for image in images):
            raise InvalidRetrySelectionError(
                "Only pending or failed images can be retried."
            )

        registered_uploads = []
        for image in images:
            retry_token = uuid4()
            object_key = (
                f"organizations/{batch.organization_id}/batches/{batch.id}/"
                f"originals/{image.id}/retries/{retry_token}.jpg"
            )
            image.original_object_key = object_key
            upload_url = signer.sign_upload_url(
                object_key=object_key,
                content_type=JPEG_MIME_TYPE,
            )
            registered_uploads.append(
                RegisteredUpload(
                    image_id=image.id,
                    upload_order=image.upload_order,
                    original_filename=image.original_filename,
                    original_object_key=object_key,
                    upload_url=upload_url,
                )
            )

        session.commit()
    except (
        InvalidRetrySelectionError,
        UploadBatchNotFoundError,
        UploadBatchStateError,
    ):
        session.rollback()
        raise
    except (SQLAlchemyError, UploadUrlSigningError) as error:
        session.rollback()
        raise UploadRetryError from error

    return UploadRegistration(
        batch_id=batch_id,
        status="uploading",
        uploads=registered_uploads,
    )


def finalize_upload_batch(
    session: Session,
    *,
    batch_id: UUID,
    inspector: UploadObjectInspector,
) -> UploadBatchState:
    batch = _load_upload_batch(session, batch_id, for_update=True)
    if batch is None:
        raise UploadBatchNotFoundError
    if batch.status not in {"uploading", "queued"}:
        raise UploadBatchStateError

    images = _load_upload_batch_images(session, batch_id)
    if batch.status == "queued":
        return _serialize_upload_batch(batch=batch, images=images)

    any_failed = False
    batch.finalized_at = None
    batch.completed_at = None

    for image in images:
        try:
            object_metadata = inspector.inspect_object(
                object_key=image.original_object_key
            )
        except UploadObjectNotFoundError:
            _mark_image_failed(
                image,
                error_code="object_missing",
                error_message="The uploaded object was not found in Cloud Storage.",
            )
            any_failed = True
            continue
        except UploadObjectInspectionError:
            session.rollback()
            raise

        if object_metadata.content_type != "image/jpeg":
            _mark_image_failed(
                image,
                error_code="content_type_mismatch",
                error_message="The uploaded object is not an image/jpeg object.",
            )
            any_failed = True
            continue
        if object_metadata.size_bytes != image.size_bytes:
            _mark_image_failed(
                image,
                error_code="size_mismatch",
                error_message="The uploaded object size does not match the registration.",
            )
            any_failed = True
            continue

        image.status = "uploaded"
        image.error_code = None
        image.error_message = None

    if any_failed:
        batch.status = "uploading"
    else:
        batch.status = "queued"
        batch.finalized_at = datetime.now(UTC)

    snapshot = _serialize_upload_batch(batch=batch, images=images)
    session.commit()
    return snapshot


def _validate_upload_metadata(files: list[UploadFileMetadata]) -> None:
    if not 1 <= len(files) <= MAX_FILES_PER_REQUEST:
        raise InvalidUploadMetadataError(
            f"Register between 1 and {MAX_FILES_PER_REQUEST} files."
        )

    for upload_order, file in enumerate(files):
        if not file.original_filename.strip():
            raise InvalidUploadMetadataError(
                f"File at upload order {upload_order} must have a nonblank filename."
            )
        if file.mime_type != JPEG_MIME_TYPE:
            raise InvalidUploadMetadataError(
                f"File at upload order {upload_order} must use image/jpeg."
            )
        if not 1 <= file.size_bytes <= MAX_FILE_SIZE_BYTES:
            raise InvalidUploadMetadataError(
                f"File at upload order {upload_order} must be between 1 byte "
                f"and {MAX_FILE_SIZE_BYTES} bytes."
            )


def _load_upload_batch(
    session: Session,
    batch_id: UUID,
    *,
    for_update: bool = False,
) -> UploadBatch | None:
    statement = select(UploadBatch).where(
        UploadBatch.id == batch_id,
        UploadBatch.organization_id == DEFAULT_ORGANIZATION_ID,
    )
    if for_update:
        statement = statement.with_for_update()
    return session.scalar(statement)


def _load_upload_batch_images(
    session: Session,
    batch_id: UUID,
) -> list[ImageAsset]:
    return session.scalars(
        select(ImageAsset)
        .where(
            ImageAsset.batch_id == batch_id,
            ImageAsset.organization_id == DEFAULT_ORGANIZATION_ID,
        )
        .order_by(ImageAsset.upload_order)
    ).all()


def _serialize_upload_batch(
    *,
    batch: UploadBatch,
    images: list[ImageAsset],
) -> UploadBatchState:
    return UploadBatchState(
        batch_id=batch.id,
        status=batch.status,
        original_file_count=batch.original_file_count,
        processed_file_count=batch.processed_file_count,
        created_at=batch.created_at,
        finalized_at=batch.finalized_at,
        completed_at=batch.completed_at,
        images=[
            UploadBatchImageState(
                image_id=image.id,
                upload_order=image.upload_order,
                original_filename=image.original_filename,
                status=image.status,
                error_code=image.error_code,
                error_message=image.error_message,
            )
            for image in images
        ],
    )


def _mark_image_failed(
    image: ImageAsset,
    *,
    error_code: str,
    error_message: str,
) -> None:
    image.status = "failed"
    image.error_code = error_code
    image.error_message = error_message
