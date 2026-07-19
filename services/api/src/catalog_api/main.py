from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, File, HTTPException, Response, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from catalog_api.approved_group_exports import (
    ApprovedGroupsBatchNotFoundError,
    ApprovedGroupsBatchStateError,
    ApprovedGroupsExportDisabledError,
    ApprovedGroupsExportState,
    ApprovedGroupsInvalidError,
    get_approved_groups_export,
)
from catalog_api.category_suggestion_providers import (
    CategorySuggestionProvider,
    get_category_suggestion_provider,
)
from catalog_api.database import get_session
from catalog_api.image_embedding_providers import (
    ImageEmbeddingProvider,
    get_image_embedding_provider,
)
from catalog_api.image_uploads import (
    MAX_FILES_PER_REQUEST,
    MAX_FILE_SIZE_BYTES,
    ValidatedJpeg,
    validate_jpeg_upload,
)
from catalog_api.grouping import (
    GroupingBatchNotFoundError,
    GroupingBatchStateError,
    GroupingJobNotFoundError,
    group_batch_task,
)
from catalog_api.local_batches import (
    MANIFEST_VERSION,
    BatchManifest,
    InvalidLocalBatchEditError,
    LocalBatchNotFoundError,
    LocalBatchStore,
    LocalGroupNotFoundError,
    LocalImageNotFoundError,
)
from catalog_api.models import Category
from catalog_api.multimodal_comparison import (
    MultimodalComparisonBatchNotFoundError,
    MultimodalComparisonClaimLostError,
    MultimodalComparisonConfigurationError,
    MultimodalComparisonExecutionError,
    MultimodalComparisonInProgressError,
    MultimodalComparisonNotAllowedError,
    run_multimodal_comparison,
    validate_multimodal_comparison_configuration,
)
from catalog_api.multimodal_comparison_providers import (
    MultimodalComparisonProvider,
    get_multimodal_comparison_provider,
)
from catalog_api.processing_jobs import (
    ClassifyImageTaskPayload,
    GroupBatchTaskPayload,
    ProcessImageTaskPayload,
    ProcessingBatchNotFoundError,
    ProcessingBatchStateError,
    ProcessingQueue,
    ProcessingJobExecutionError,
    ProcessingJobNotFoundError,
    classify_image_task,
    get_processing_queue,
    process_image_task,
)
from catalog_api.processing_orchestration import (
    ProcessingBatchState,
    ProcessingRunner,
    ProcessingThumbnailNotFoundError,
    ProcessingThumbnailReadError,
    get_processing_batch_state,
    get_processing_runner,
    read_processing_thumbnail,
    start_processing_batch,
)
from catalog_api.processing_storage import WorkerStorage, get_worker_storage
from catalog_api.review_approvals import (
    ReviewApprovalBatchNotFoundError,
    ReviewApprovalResourceNotFoundError,
    ReviewApprovalStateError,
    approve_review_batch,
    approve_review_group,
)
from catalog_api.review_groups import (
    ReviewBatchGroupsState,
    ReviewBatchNotFoundError,
    ReviewBatchStateError,
    get_review_batch_groups,
)
from catalog_api.review_edits import (
    ReviewEditBatchNotFoundError,
    ReviewEditConflictError,
    ReviewEditResourceNotFoundError,
    ReviewEditStateError,
    ReviewEditValidationError,
    UpdateGroupPatch,
    create_review_group,
    merge_review_groups,
    move_image_to_group,
    reject_group_image,
    remove_image_from_group,
    restore_group_image_rejection,
    split_review_group,
    update_group_image_duplicate,
    update_review_group,
)
from catalog_api.upload_batches import (
    InvalidUploadMetadataError,
    InvalidRetrySelectionError,
    UploadBatchCreationError,
    UploadBatchNotFoundError,
    UploadBatchState,
    UploadBatchStateError,
    UploadFileMetadata,
    UploadRegistrationError,
    UploadRetryError,
    create_upload_batch,
    finalize_upload_batch,
    get_upload_batch,
    register_upload_files,
    retry_failed_uploads,
)
from catalog_api.upload_storage import (
    UploadObjectInspector,
    UploadObjectInspectionError,
    UploadUrlSigner,
    get_upload_object_inspector,
    get_upload_url_signer,
)


class ApiModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


NO_STORE_HEADERS = {"Cache-Control": "no-store"}


class UploadFileResult(ApiModel):
    filename: str
    status: Literal["accepted", "rejected"]
    size_bytes: int = Field(serialization_alias="sizeBytes")
    error_code: str | None = Field(default=None, serialization_alias="errorCode")
    error_message: str | None = Field(default=None, serialization_alias="errorMessage")


class UploadHandshakeResponse(ApiModel):
    upload_id: UUID = Field(serialization_alias="uploadId")
    status: Literal["completed", "partial", "rejected"]
    files: list[UploadFileResult]


class CreateUploadBatchResponse(ApiModel):
    batch_id: UUID = Field(serialization_alias="batchId")
    status: Literal["created"]
    max_files: int = Field(serialization_alias="maxFiles")


class RegisterUploadFileRequest(ApiModel):
    original_filename: StrictStr = Field(alias="originalFilename")
    mime_type: StrictStr = Field(alias="mimeType")
    size_bytes: StrictInt = Field(alias="sizeBytes")


class RegisterUploadsRequest(ApiModel):
    files: list[RegisterUploadFileRequest]


class RetryUploadsRequest(ApiModel):
    image_ids: list[UUID] = Field(alias="imageIds")


class ProcessImageTaskRequest(ApiModel):
    batch_id: UUID = Field(alias="batchId")
    image_id: UUID = Field(alias="imageId")
    pipeline_version: StrictStr = Field(alias="pipelineVersion")


class ProcessImageTaskResponse(ApiModel):
    batch_id: UUID = Field(serialization_alias="batchId")
    image_id: UUID = Field(serialization_alias="imageId")
    pipeline_version: str = Field(serialization_alias="pipelineVersion")
    job_status: str = Field(serialization_alias="jobStatus")
    did_work: bool = Field(serialization_alias="didWork")


class ClassifyImageTaskRequest(ApiModel):
    batch_id: UUID = Field(alias="batchId")
    image_id: UUID = Field(alias="imageId")
    pipeline_version: StrictStr = Field(alias="pipelineVersion")


class ClassifyImageTaskResponse(ApiModel):
    batch_id: UUID = Field(serialization_alias="batchId")
    image_id: UUID = Field(serialization_alias="imageId")
    pipeline_version: str = Field(serialization_alias="pipelineVersion")
    job_status: str = Field(serialization_alias="jobStatus")
    did_work: bool = Field(serialization_alias="didWork")


class GroupBatchTaskRequest(ApiModel):
    batch_id: UUID = Field(alias="batchId")
    pipeline_version: StrictStr = Field(alias="pipelineVersion")


class GroupBatchTaskResponse(ApiModel):
    batch_id: UUID = Field(serialization_alias="batchId")
    pipeline_version: str = Field(serialization_alias="pipelineVersion")
    job_status: str = Field(serialization_alias="jobStatus")
    did_work: bool = Field(serialization_alias="didWork")


class RegisteredUploadResponse(ApiModel):
    image_id: UUID = Field(serialization_alias="imageId")
    upload_order: int = Field(serialization_alias="uploadOrder")
    original_filename: str = Field(serialization_alias="originalFilename")
    original_object_key: str = Field(serialization_alias="originalObjectKey")
    upload_url: str = Field(serialization_alias="uploadUrl")


class RegisterUploadsResponse(ApiModel):
    batch_id: UUID = Field(serialization_alias="batchId")
    status: Literal["uploading"]
    uploads: list[RegisteredUploadResponse]


class UploadBatchImageResponse(ApiModel):
    image_id: UUID = Field(serialization_alias="imageId")
    upload_order: int = Field(serialization_alias="uploadOrder")
    original_filename: str = Field(serialization_alias="originalFilename")
    status: str
    error_code: str | None = Field(default=None, serialization_alias="errorCode")
    error_message: str | None = Field(default=None, serialization_alias="errorMessage")


class UploadBatchResponse(ApiModel):
    batch_id: UUID = Field(serialization_alias="batchId")
    status: str
    original_file_count: int = Field(serialization_alias="originalFileCount")
    processed_file_count: int = Field(serialization_alias="processedFileCount")
    created_at: datetime = Field(serialization_alias="createdAt")
    finalized_at: datetime | None = Field(serialization_alias="finalizedAt")
    completed_at: datetime | None = Field(serialization_alias="completedAt")
    images: list[UploadBatchImageResponse]


class ProcessingBatchImageResponse(ApiModel):
    image_id: UUID = Field(serialization_alias="imageId")
    upload_order: int = Field(serialization_alias="uploadOrder")
    original_filename: str = Field(serialization_alias="originalFilename")
    image_status: str = Field(serialization_alias="imageStatus")
    process_job_status: str | None = Field(
        default=None,
        serialization_alias="processJobStatus",
    )
    process_error: str | None = Field(default=None, serialization_alias="processError")
    classify_job_status: str | None = Field(
        default=None,
        serialization_alias="classifyJobStatus",
    )
    classify_error: str | None = Field(
        default=None,
        serialization_alias="classifyError",
    )
    category_slug: str | None = Field(default=None, serialization_alias="categorySlug")
    confidence: float | None = None
    has_hashes: bool = Field(serialization_alias="hasHashes")
    has_embedding: bool = Field(serialization_alias="hasEmbedding")


class ProcessingBatchResponse(ApiModel):
    batch_id: UUID = Field(serialization_alias="batchId")
    status: str
    original_file_count: int = Field(serialization_alias="originalFileCount")
    processed_file_count: int = Field(serialization_alias="processedFileCount")
    pipeline_version: str = Field(serialization_alias="pipelineVersion")
    images: list[ProcessingBatchImageResponse]


class ReviewGroupImageResponse(ApiModel):
    image_id: UUID = Field(serialization_alias="imageId")
    original_filename: str = Field(serialization_alias="originalFilename")
    upload_order: int = Field(serialization_alias="uploadOrder")
    thumbnail_url: str = Field(serialization_alias="thumbnailUrl")
    position: int
    is_duplicate: bool = Field(serialization_alias="isDuplicate")
    is_rejected: bool = Field(serialization_alias="isRejected")
    duplicate_of_image_id: UUID | None = Field(
        default=None,
        serialization_alias="duplicateOfImageId",
    )
    membership_source: str = Field(serialization_alias="membershipSource")
    membership_confidence: float | None = Field(
        default=None,
        serialization_alias="membershipConfidence",
    )


class ReviewGroupResponse(ApiModel):
    group_id: UUID = Field(serialization_alias="groupId")
    status: str
    confidence: float | None = None
    cover_image_id: UUID | None = Field(
        default=None,
        serialization_alias="coverImageId",
    )
    suggested_category_slug: str | None = Field(
        default=None,
        serialization_alias="suggestedCategorySlug",
    )
    approved_category_slug: str | None = Field(
        default=None,
        serialization_alias="approvedCategorySlug",
    )
    category_suggestion_status: str | None = Field(
        default=None,
        serialization_alias="categorySuggestionStatus",
    )
    approved_category_source: str | None = Field(
        default=None,
        serialization_alias="approvedCategorySource",
    )
    possible_existing_product_id: UUID | None = Field(
        default=None,
        serialization_alias="possibleExistingProductId",
    )
    warnings: list[str]
    images: list[ReviewGroupImageResponse]


class ReviewBatchGroupsResponse(ApiModel):
    batch_id: UUID = Field(serialization_alias="batchId")
    organization_id: UUID = Field(serialization_alias="organizationId")
    status: str
    pipeline_version: str | None = Field(
        default=None,
        serialization_alias="pipelineVersion",
    )
    groups: list[ReviewGroupResponse]


class ApprovedGroupExportImageResponse(ApiModel):
    image_id: UUID = Field(serialization_alias="imageId")
    position: int
    is_duplicate: bool = Field(serialization_alias="isDuplicate")
    duplicate_of_image_id: UUID | None = Field(
        serialization_alias="duplicateOfImageId",
    )


class ApprovedGroupExportResponse(ApiModel):
    group_id: UUID = Field(serialization_alias="groupId")
    approved_category_slug: str = Field(
        serialization_alias="approvedCategorySlug",
    )
    suggested_category_slug: str | None = Field(
        serialization_alias="suggestedCategorySlug",
    )
    cover_image_id: UUID = Field(serialization_alias="coverImageId")
    confidence: float | None
    images: list[ApprovedGroupExportImageResponse]


class ApprovedGroupsExportResponse(ApiModel):
    batch_id: UUID = Field(serialization_alias="batchId")
    organization_id: UUID = Field(serialization_alias="organizationId")
    status: Literal["approved"]
    pipeline_version: str = Field(serialization_alias="pipelineVersion")
    groups: list[ApprovedGroupExportResponse]


class ReviewCategoryResponse(ApiModel):
    id: UUID
    slug: str
    parent_id: UUID | None = Field(default=None, serialization_alias="parentId")
    name_en: str = Field(serialization_alias="nameEn")


class CreateReviewGroupRequest(ApiModel):
    image_ids: list[UUID] = Field(alias="imageIds")


class MoveReviewGroupImageRequest(ApiModel):
    image_id: UUID = Field(alias="imageId")


class MergeReviewGroupsRequest(ApiModel):
    target_group_id: UUID = Field(alias="targetGroupId")
    source_group_ids: list[UUID] = Field(alias="sourceGroupIds")


class SplitReviewGroupRequest(ApiModel):
    image_ids: list[UUID] = Field(alias="imageIds")


class UpdateReviewGroupRequest(ApiModel):
    cover_image_id: UUID | None = Field(default=None, alias="coverImageId")
    approved_category_id: UUID | None = Field(default=None, alias="approvedCategoryId")


class UpdateReviewGroupImageRequest(ApiModel):
    is_duplicate: bool = Field(alias="isDuplicate")
    duplicate_of_image_id: UUID | None = Field(default=None, alias="duplicateOfImageId")


class LocalBatchFileResult(ApiModel):
    image_id: UUID | None = Field(serialization_alias="imageId")
    original_filename: str = Field(serialization_alias="originalFilename")
    status: Literal["accepted", "rejected"]
    error_code: str | None = Field(default=None, serialization_alias="errorCode")
    error_message: str | None = Field(default=None, serialization_alias="errorMessage")


class CreateLocalBatchResponse(ApiModel):
    batch_id: UUID | None = Field(serialization_alias="batchId")
    status: Literal["completed", "partial", "rejected"]
    manifest_version: int | None = Field(serialization_alias="manifestVersion")
    files: list[LocalBatchFileResult]


class LocalBatchImageResponse(ApiModel):
    image_id: UUID = Field(serialization_alias="imageId")
    original_filename: str = Field(serialization_alias="originalFilename")
    thumbnail_url: str = Field(serialization_alias="thumbnailUrl")
    image_url: str = Field(serialization_alias="imageUrl")
    sha256: str
    group_id: UUID = Field(serialization_alias="groupId")
    is_retained: bool = Field(serialization_alias="isRetained")


class LocalBatchGroupResponse(ApiModel):
    group_id: UUID = Field(serialization_alias="groupId")
    retained_image_id: UUID = Field(serialization_alias="retainedImageId")
    image_ids: list[UUID] = Field(serialization_alias="imageIds")


class LocalBatchResponse(ApiModel):
    batch_id: UUID = Field(serialization_alias="batchId")
    status: Literal["ready"]
    manifest_version: int = Field(serialization_alias="manifestVersion")
    images: list[LocalBatchImageResponse]
    groups: list[LocalBatchGroupResponse]


class MoveImageRequest(ApiModel):
    group_id: UUID = Field(alias="groupId")


class MoveImageResponse(ApiModel):
    batch: LocalBatchResponse


class CreateGroupRequest(ApiModel):
    image_ids: list[UUID] = Field(alias="imageIds")


class CreateGroupResponse(ApiModel):
    group_id: UUID = Field(serialization_alias="groupId")
    batch: LocalBatchResponse


def _allowed_web_origins() -> list[str]:
    configured_origins = os.getenv(
        "CATALOG_WEB_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000",
    )
    return [origin.strip() for origin in configured_origins.split(",") if origin.strip()]


@asynccontextmanager
async def _app_lifespan(_: FastAPI) -> AsyncIterator[None]:
    validate_multimodal_comparison_configuration()
    yield


app = FastAPI(title="Catalog Classifier API", lifespan=_app_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_web_origins(),
    allow_credentials=False,
    allow_methods=["DELETE", "GET", "PATCH", "POST"],
    allow_headers=["*"],
)


def _request_error(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"code": code, "message": message},
    )


async def _validated_uploads(
    files: list[UploadFile] | None,
) -> list[ValidatedJpeg]:
    if not files:
        raise _request_error("files_required", "Upload at least one JPEG file.")

    if len(files) > MAX_FILES_PER_REQUEST:
        for upload in files:
            await upload.close()
        raise _request_error(
            "too_many_files",
            f"Upload at most {MAX_FILES_PER_REQUEST} files per request.",
        )

    validated_uploads = []
    for upload in files:
        validated_uploads.append(await validate_jpeg_upload(upload))
    return validated_uploads


def _overall_status(
    accepted_count: int,
    total_count: int,
) -> Literal["completed", "partial", "rejected"]:
    if accepted_count == total_count:
        return "completed"
    if accepted_count == 0:
        return "rejected"
    return "partial"


def _batch_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "batch_not_found", "message": "Local batch was not found."},
    )


def _image_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "image_not_found", "message": "Local image was not found."},
    )


def _group_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "group_not_found", "message": "Local group was not found."},
    )


def _invalid_selection(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"code": "invalid_selection", "message": message},
    )


def _upload_batch_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "code": "batch_not_found",
            "message": "Upload batch was not found.",
        },
    )


def _upload_batch_state_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "invalid_batch_state",
            "message": "Upload batch must be in uploading or queued state.",
        },
    )


def _upload_batch_database_error(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={
            "code": "database_error",
            "message": message,
        },
    )


def _upload_batch_finalization_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={
            "code": "upload_finalization_failed",
            "message": "Unable to finalize the upload batch.",
        },
    )


def _processing_batch_state_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "invalid_batch_state",
            "message": "Upload batch is not ready for processing.",
        },
    )


def _review_batch_not_ready() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "batch_not_review_ready",
            "message": "Upload batch has not entered the review phase.",
        },
    )


def _approved_groups_export_disabled() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "code": "approved_groups_export_disabled",
            "message": "Approved group export is not enabled.",
        },
    )


def _approved_groups_batch_not_approved() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "batch_not_approved",
            "message": "Approved groups are only available for approved batches.",
        },
    )


def _approved_groups_invalid() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "approved_groups_invalid",
            "message": "The approved group export is internally inconsistent.",
        },
    )


def _review_edit_not_found(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "code": "review_resource_not_found",
            "message": message,
        },
    )


def _invalid_review_edit(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={
            "code": "invalid_review_edit",
            "message": message,
        },
    )


def _review_edit_state_error(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "review_edit_not_allowed",
            "message": message,
        },
    )


def _review_edit_conflict(*, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": code,
            "message": message,
        },
    )


def _review_approval_state_error(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "review_approval_not_allowed",
            "message": message,
        },
    )


def _multimodal_comparison_conflict(
    *,
    code: str,
    message: str,
) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": code, "message": message},
    )


def _multimodal_comparison_error(
    *,
    code: str,
    message: str,
) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"code": code, "message": message},
    )


def _processing_thumbnail_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "code": "thumbnail_not_found",
            "message": "The thumbnail was not found.",
        },
        headers=NO_STORE_HEADERS,
    )


def _processing_thumbnail_read_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={
            "code": "thumbnail_read_failed",
            "message": "Unable to read the thumbnail.",
        },
        headers=NO_STORE_HEADERS,
    )


def _invalid_retry_selection(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={
            "code": "invalid_retry_selection",
            "message": message,
        },
    )


def _upload_retry_state_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "invalid_batch_state",
            "message": "Upload retries require an uploading batch.",
        },
    )


def _upload_retry_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={
            "code": "upload_retry_failed",
            "message": "Unable to prepare the selected upload retries.",
        },
    )


def _processing_job_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "code": "processing_job_not_found",
            "message": "Processing job was not found.",
        },
    )


def _processing_job_error(
    *,
    code: str = "processing_job_failed",
    message: str = "Unable to process the image job.",
) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={
            "code": code,
            "message": message,
        },
    )


def _upload_batch_response(snapshot: UploadBatchState) -> UploadBatchResponse:
    return UploadBatchResponse(
        batch_id=snapshot.batch_id,
        status=snapshot.status,
        original_file_count=snapshot.original_file_count,
        processed_file_count=snapshot.processed_file_count,
        created_at=snapshot.created_at,
        finalized_at=snapshot.finalized_at,
        completed_at=snapshot.completed_at,
        images=[
            UploadBatchImageResponse(
                image_id=image.image_id,
                upload_order=image.upload_order,
                original_filename=image.original_filename,
                status=image.status,
                error_code=image.error_code,
                error_message=image.error_message,
            )
            for image in snapshot.images
        ],
    )


def _processing_batch_response(
    snapshot: ProcessingBatchState,
) -> ProcessingBatchResponse:
    return ProcessingBatchResponse(
        batch_id=snapshot.batch_id,
        status=snapshot.status,
        original_file_count=snapshot.original_file_count,
        processed_file_count=snapshot.processed_file_count,
        pipeline_version=snapshot.pipeline_version,
        images=[
            ProcessingBatchImageResponse(
                image_id=image.image_id,
                upload_order=image.upload_order,
                original_filename=image.original_filename,
                image_status=image.image_status,
                process_job_status=image.process_job_status,
                process_error=image.process_error,
                classify_job_status=image.classify_job_status,
                classify_error=image.classify_error,
                category_slug=image.category_slug,
                confidence=image.confidence,
                has_hashes=image.has_hashes,
                has_embedding=image.has_embedding,
            )
            for image in snapshot.images
        ],
    )


def _review_batch_groups_response(
    snapshot: ReviewBatchGroupsState,
) -> ReviewBatchGroupsResponse:
    return ReviewBatchGroupsResponse(
        batch_id=snapshot.batch_id,
        organization_id=snapshot.organization_id,
        status=snapshot.status,
        pipeline_version=snapshot.pipeline_version,
        groups=[
            ReviewGroupResponse(
                group_id=group.group_id,
                status=group.status,
                confidence=group.confidence,
                cover_image_id=group.cover_image_id,
                suggested_category_slug=group.suggested_category_slug,
                approved_category_slug=group.approved_category_slug,
                category_suggestion_status=group.category_suggestion_status,
                approved_category_source=group.approved_category_source,
                possible_existing_product_id=group.possible_existing_product_id,
                warnings=group.warnings,
                images=[
                    ReviewGroupImageResponse(
                        image_id=image.image_id,
                        original_filename=image.original_filename,
                        upload_order=image.upload_order,
                        thumbnail_url=image.thumbnail_url,
                        position=image.position,
                        is_duplicate=image.is_duplicate,
                        is_rejected=image.is_rejected,
                        duplicate_of_image_id=image.duplicate_of_image_id,
                        membership_source=image.membership_source,
                        membership_confidence=image.membership_confidence,
                    )
                    for image in group.images
                ],
            )
            for group in snapshot.groups
        ],
    )


def _approved_groups_export_response(
    snapshot: ApprovedGroupsExportState,
) -> ApprovedGroupsExportResponse:
    return ApprovedGroupsExportResponse(
        batch_id=snapshot.batch_id,
        organization_id=snapshot.organization_id,
        status="approved",
        pipeline_version=snapshot.pipeline_version,
        groups=[
            ApprovedGroupExportResponse(
                group_id=group.group_id,
                approved_category_slug=group.approved_category_slug,
                suggested_category_slug=group.suggested_category_slug,
                cover_image_id=group.cover_image_id,
                confidence=group.confidence,
                images=[
                    ApprovedGroupExportImageResponse(
                        image_id=image.image_id,
                        position=image.position,
                        is_duplicate=image.is_duplicate,
                        duplicate_of_image_id=image.duplicate_of_image_id,
                    )
                    for image in group.images
                ],
            )
            for group in snapshot.groups
        ],
    )


def _active_global_categories(session: Session) -> list[Category]:
    categories = session.scalars(
        select(Category).where(
            Category.organization_id.is_(None),
            Category.active.is_(True),
        )
    ).all()
    categories_by_id = {category.id: category for category in categories}
    children_by_parent_id: dict[UUID | None, list[Category]] = {}

    for category in categories:
        parent_id = category.parent_id
        if parent_id not in categories_by_id:
            parent_id = None
        children_by_parent_id.setdefault(parent_id, []).append(category)

    for siblings in children_by_parent_id.values():
        siblings.sort(key=lambda category: (category.name_en.casefold(), category.slug))

    ordered_categories: list[Category] = []

    def append_tree(parent_id: UUID | None) -> None:
        for category in children_by_parent_id.get(parent_id, []):
            ordered_categories.append(category)
            append_tree(category.id)

    append_tree(None)
    return ordered_categories


@app.post(
    "/v1/upload-batches",
    response_model=CreateUploadBatchResponse,
    status_code=status.HTTP_200_OK,
)
def create_durable_upload_batch(
    session: Annotated[Session, Depends(get_session)],
) -> CreateUploadBatchResponse:
    try:
        batch = create_upload_batch(session)
    except UploadBatchCreationError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "database_error",
                "message": "Unable to create the upload batch.",
            },
        ) from error

    return CreateUploadBatchResponse(
        batch_id=batch.id,
        status=batch.status,
        max_files=MAX_FILES_PER_REQUEST,
    )


@app.post(
    "/v1/upload-batches/{batch_id}/uploads",
    response_model=RegisterUploadsResponse,
    status_code=status.HTTP_200_OK,
)
def register_durable_upload_files(
    batch_id: UUID,
    request: RegisterUploadsRequest,
    session: Annotated[Session, Depends(get_session)],
    signer: Annotated[UploadUrlSigner, Depends(get_upload_url_signer)],
) -> RegisterUploadsResponse:
    try:
        registration = register_upload_files(
            session,
            batch_id=batch_id,
            files=[
                UploadFileMetadata(
                    original_filename=file.original_filename,
                    mime_type=file.mime_type,
                    size_bytes=file.size_bytes,
                )
                for file in request.files
            ],
            signer=signer,
        )
    except InvalidUploadMetadataError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_upload_metadata", "message": str(error)},
        ) from error
    except UploadBatchNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "batch_not_found",
                "message": "Upload batch was not found.",
            },
        ) from error
    except UploadBatchStateError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "invalid_batch_state",
                "message": "Upload batch must be in created state.",
            },
        ) from error
    except UploadRegistrationError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "upload_registration_failed",
                "message": "Unable to register files for upload.",
            },
        ) from error

    return RegisterUploadsResponse(
        batch_id=registration.batch_id,
        status=registration.status,
        uploads=[
            RegisteredUploadResponse(
                image_id=upload.image_id,
                upload_order=upload.upload_order,
                original_filename=upload.original_filename,
                original_object_key=upload.original_object_key,
                upload_url=upload.upload_url,
            )
            for upload in registration.uploads
        ],
    )


@app.post(
    "/v1/upload-batches/{batch_id}/retry-failed",
    response_model=RegisterUploadsResponse,
    status_code=status.HTTP_200_OK,
)
def retry_durable_upload_files(
    batch_id: UUID,
    request: RetryUploadsRequest,
    session: Annotated[Session, Depends(get_session)],
    signer: Annotated[UploadUrlSigner, Depends(get_upload_url_signer)],
) -> RegisterUploadsResponse:
    try:
        registration = retry_failed_uploads(
            session,
            batch_id=batch_id,
            image_ids=request.image_ids,
            signer=signer,
        )
    except InvalidRetrySelectionError as error:
        raise _invalid_retry_selection(str(error)) from error
    except UploadBatchNotFoundError as error:
        raise _upload_batch_not_found() from error
    except UploadBatchStateError as error:
        raise _upload_retry_state_error() from error
    except UploadRetryError as error:
        raise _upload_retry_error() from error

    return RegisterUploadsResponse(
        batch_id=registration.batch_id,
        status=registration.status,
        uploads=[
            RegisteredUploadResponse(
                image_id=upload.image_id,
                upload_order=upload.upload_order,
                original_filename=upload.original_filename,
                original_object_key=upload.original_object_key,
                upload_url=upload.upload_url,
            )
            for upload in registration.uploads
        ],
    )


@app.post(
    "/v1/upload-batches/{batch_id}/finalize",
    response_model=UploadBatchResponse,
    status_code=status.HTTP_200_OK,
)
def finalize_durable_upload_batch(
    batch_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    inspector: Annotated[UploadObjectInspector, Depends(get_upload_object_inspector)],
) -> UploadBatchResponse:
    try:
        snapshot = finalize_upload_batch(
            session,
            batch_id=batch_id,
            inspector=inspector,
        )
    except UploadBatchNotFoundError as error:
        raise _upload_batch_not_found() from error
    except UploadBatchStateError as error:
        raise _upload_batch_state_error() from error
    except UploadObjectInspectionError as error:
        session.rollback()
        raise _upload_batch_finalization_error() from error
    except SQLAlchemyError as error:
        session.rollback()
        raise _upload_batch_database_error(
            "Unable to finalize the upload batch."
        ) from error

    return _upload_batch_response(snapshot)


@app.post(
    "/v1/upload-batches/{batch_id}/start-processing",
    response_model=ProcessingBatchResponse,
    status_code=status.HTTP_200_OK,
)
def start_durable_upload_batch_processing(
    batch_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    runner: Annotated[ProcessingRunner, Depends(get_processing_runner)],
) -> ProcessingBatchResponse:
    try:
        snapshot = start_processing_batch(
            session,
            batch_id=batch_id,
            runner=runner,
        )
    except ProcessingBatchNotFoundError as error:
        raise _upload_batch_not_found() from error
    except ProcessingBatchStateError as error:
        raise _processing_batch_state_error() from error
    except SQLAlchemyError as error:
        session.rollback()
        raise _upload_batch_database_error(
            "Unable to start upload batch processing."
        ) from error

    return _processing_batch_response(snapshot)


@app.get(
    "/v1/upload-batches/{batch_id}/processing",
    response_model=ProcessingBatchResponse,
)
def get_durable_upload_batch_processing(
    batch_id: UUID,
    session: Annotated[Session, Depends(get_session)],
) -> ProcessingBatchResponse:
    try:
        snapshot = get_processing_batch_state(session, batch_id=batch_id)
    except ProcessingBatchNotFoundError as error:
        raise _upload_batch_not_found() from error
    except ProcessingBatchStateError as error:
        raise _processing_batch_state_error() from error
    except SQLAlchemyError as error:
        session.rollback()
        raise _upload_batch_database_error(
            "Unable to load upload batch processing state."
        ) from error

    return _processing_batch_response(snapshot)


@app.get("/v1/upload-batches/{batch_id}/images/{image_id}/thumbnail")
def get_durable_upload_batch_thumbnail(
    batch_id: UUID,
    image_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    storage: Annotated[WorkerStorage, Depends(get_worker_storage)],
) -> Response:
    try:
        thumbnail_bytes = read_processing_thumbnail(
            session,
            batch_id=batch_id,
            image_id=image_id,
            storage=storage,
        )
    except ProcessingThumbnailNotFoundError as error:
        raise _processing_thumbnail_not_found() from error
    except ProcessingThumbnailReadError as error:
        raise _processing_thumbnail_read_error() from error
    except SQLAlchemyError as error:
        session.rollback()
        raise _processing_thumbnail_read_error() from error

    return Response(
        content=thumbnail_bytes,
        media_type="image/jpeg",
        headers=NO_STORE_HEADERS,
    )


@app.get(
    "/v1/categories",
    response_model=list[ReviewCategoryResponse],
)
def list_review_categories(
    session: Annotated[Session, Depends(get_session)],
) -> list[ReviewCategoryResponse]:
    try:
        categories = _active_global_categories(session)
    except SQLAlchemyError as error:
        session.rollback()
        raise _upload_batch_database_error("Unable to load categories.") from error

    return [
        ReviewCategoryResponse(
            id=category.id,
            slug=category.slug,
            parent_id=category.parent_id,
            name_en=category.name_en,
        )
        for category in categories
    ]


@app.get(
    "/v1/upload-batches/{batch_id}/groups",
    response_model=ReviewBatchGroupsResponse,
)
def get_durable_upload_batch_groups(
    batch_id: UUID,
    session: Annotated[Session, Depends(get_session)],
) -> ReviewBatchGroupsResponse:
    try:
        snapshot = get_review_batch_groups(session, batch_id=batch_id)
    except ReviewBatchNotFoundError as error:
        raise _upload_batch_not_found() from error
    except ReviewBatchStateError as error:
        raise _review_batch_not_ready() from error
    except SQLAlchemyError as error:
        session.rollback()
        raise _upload_batch_database_error(
            "Unable to load upload batch review groups."
        ) from error

    return _review_batch_groups_response(snapshot)


@app.get(
    "/v1/upload-batches/{batch_id}/approved-groups",
    response_model=ApprovedGroupsExportResponse,
)
def get_durable_approved_groups_export(
    batch_id: UUID,
    session: Annotated[Session, Depends(get_session)],
) -> ApprovedGroupsExportResponse:
    try:
        snapshot = get_approved_groups_export(session, batch_id=batch_id)
    except ApprovedGroupsExportDisabledError as error:
        raise _approved_groups_export_disabled() from error
    except ApprovedGroupsBatchNotFoundError as error:
        raise _upload_batch_not_found() from error
    except ApprovedGroupsBatchStateError as error:
        raise _approved_groups_batch_not_approved() from error
    except ApprovedGroupsInvalidError as error:
        raise _approved_groups_invalid() from error
    except SQLAlchemyError as error:
        session.rollback()
        raise _upload_batch_database_error(
            "Unable to load approved groups."
        ) from error

    return _approved_groups_export_response(snapshot)


@app.post(
    "/v1/upload-batches/{batch_id}/run-multimodal-comparison",
    response_model=ReviewBatchGroupsResponse,
    status_code=status.HTTP_200_OK,
)
def run_durable_upload_batch_multimodal_comparison(
    batch_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    storage: Annotated[WorkerStorage, Depends(get_worker_storage)],
    provider: Annotated[
        MultimodalComparisonProvider,
        Depends(get_multimodal_comparison_provider),
    ],
) -> ReviewBatchGroupsResponse:
    try:
        snapshot = run_multimodal_comparison(
            session,
            batch_id=batch_id,
            storage=storage,
            provider=provider,
        )
    except MultimodalComparisonBatchNotFoundError as error:
        raise _upload_batch_not_found() from error
    except MultimodalComparisonInProgressError as error:
        raise _multimodal_comparison_conflict(
            code="multimodal_comparison_in_progress",
            message="A multimodal comparison is already running for this batch.",
        ) from error
    except MultimodalComparisonClaimLostError as error:
        raise _multimodal_comparison_conflict(
            code="multimodal_comparison_claim_lost",
            message="A newer multimodal comparison attempt owns this batch.",
        ) from error
    except MultimodalComparisonNotAllowedError as error:
        raise _multimodal_comparison_conflict(
            code="multimodal_comparison_not_allowed",
            message=str(error),
        ) from error
    except MultimodalComparisonConfigurationError as error:
        raise _multimodal_comparison_error(
            code="multimodal_comparison_configuration_invalid",
            message=str(error),
        ) from error
    except MultimodalComparisonExecutionError as error:
        raise _multimodal_comparison_error(
            code=error.error_code,
            message=error.message,
        ) from error
    except SQLAlchemyError as error:
        session.rollback()
        raise _multimodal_comparison_error(
            code="database_error",
            message="Unable to run multimodal comparison.",
        ) from error

    return _review_batch_groups_response(snapshot)


@app.post(
    "/v1/upload-batches/{batch_id}/groups",
    response_model=ReviewBatchGroupsResponse,
    status_code=status.HTTP_200_OK,
)
def create_durable_upload_batch_review_group(
    batch_id: UUID,
    request: CreateReviewGroupRequest,
    session: Annotated[Session, Depends(get_session)],
) -> ReviewBatchGroupsResponse:
    try:
        snapshot = create_review_group(
            session,
            batch_id=batch_id,
            image_ids=request.image_ids,
        )
    except ReviewEditBatchNotFoundError as error:
        raise _upload_batch_not_found() from error
    except ReviewEditResourceNotFoundError as error:
        raise _review_edit_not_found(str(error)) from error
    except ReviewEditValidationError as error:
        raise _invalid_review_edit(str(error)) from error
    except ReviewEditStateError as error:
        raise _review_edit_state_error(str(error)) from error
    except SQLAlchemyError as error:
        session.rollback()
        raise _upload_batch_database_error(
            "Unable to update upload batch review groups."
        ) from error

    return _review_batch_groups_response(snapshot)


@app.post(
    "/v1/groups/merge",
    response_model=ReviewBatchGroupsResponse,
    status_code=status.HTTP_200_OK,
)
def merge_durable_review_groups(
    request: MergeReviewGroupsRequest,
    session: Annotated[Session, Depends(get_session)],
) -> ReviewBatchGroupsResponse:
    try:
        snapshot = merge_review_groups(
            session,
            target_group_id=request.target_group_id,
            source_group_ids=request.source_group_ids,
        )
    except ReviewEditBatchNotFoundError as error:
        raise _upload_batch_not_found() from error
    except ReviewEditResourceNotFoundError as error:
        raise _review_edit_not_found(str(error)) from error
    except ReviewEditValidationError as error:
        raise _invalid_review_edit(str(error)) from error
    except ReviewEditStateError as error:
        raise _review_edit_state_error(str(error)) from error
    except SQLAlchemyError as error:
        session.rollback()
        raise _upload_batch_database_error("Unable to merge review groups.") from error

    return _review_batch_groups_response(snapshot)


@app.post(
    "/v1/groups/{group_id}/images",
    response_model=ReviewBatchGroupsResponse,
    status_code=status.HTTP_200_OK,
)
def move_durable_review_group_image(
    group_id: UUID,
    request: MoveReviewGroupImageRequest,
    session: Annotated[Session, Depends(get_session)],
) -> ReviewBatchGroupsResponse:
    try:
        snapshot = move_image_to_group(
            session,
            target_group_id=group_id,
            image_id=request.image_id,
        )
    except ReviewEditBatchNotFoundError as error:
        raise _upload_batch_not_found() from error
    except ReviewEditResourceNotFoundError as error:
        raise _review_edit_not_found(str(error)) from error
    except ReviewEditValidationError as error:
        raise _invalid_review_edit(str(error)) from error
    except ReviewEditStateError as error:
        raise _review_edit_state_error(str(error)) from error
    except SQLAlchemyError as error:
        session.rollback()
        raise _upload_batch_database_error("Unable to move review image.") from error

    return _review_batch_groups_response(snapshot)


@app.delete(
    "/v1/groups/{group_id}/images/{image_id}",
    response_model=ReviewBatchGroupsResponse,
    status_code=status.HTTP_200_OK,
)
def remove_durable_review_group_image(
    group_id: UUID,
    image_id: UUID,
    session: Annotated[Session, Depends(get_session)],
) -> ReviewBatchGroupsResponse:
    try:
        snapshot = remove_image_from_group(
            session,
            group_id=group_id,
            image_id=image_id,
        )
    except ReviewEditBatchNotFoundError as error:
        raise _upload_batch_not_found() from error
    except ReviewEditResourceNotFoundError as error:
        raise _review_edit_not_found(str(error)) from error
    except ReviewEditValidationError as error:
        raise _invalid_review_edit(str(error)) from error
    except ReviewEditStateError as error:
        raise _review_edit_state_error(str(error)) from error
    except SQLAlchemyError as error:
        session.rollback()
        raise _upload_batch_database_error("Unable to remove review image.") from error

    return _review_batch_groups_response(snapshot)


@app.post(
    "/v1/groups/{group_id}/split",
    response_model=ReviewBatchGroupsResponse,
    status_code=status.HTTP_200_OK,
)
def split_durable_review_group(
    group_id: UUID,
    request: SplitReviewGroupRequest,
    session: Annotated[Session, Depends(get_session)],
) -> ReviewBatchGroupsResponse:
    try:
        snapshot = split_review_group(
            session,
            group_id=group_id,
            image_ids=request.image_ids,
        )
    except ReviewEditBatchNotFoundError as error:
        raise _upload_batch_not_found() from error
    except ReviewEditResourceNotFoundError as error:
        raise _review_edit_not_found(str(error)) from error
    except ReviewEditValidationError as error:
        raise _invalid_review_edit(str(error)) from error
    except ReviewEditStateError as error:
        raise _review_edit_state_error(str(error)) from error
    except SQLAlchemyError as error:
        session.rollback()
        raise _upload_batch_database_error("Unable to split review group.") from error

    return _review_batch_groups_response(snapshot)


@app.patch(
    "/v1/groups/{group_id}",
    response_model=ReviewBatchGroupsResponse,
    status_code=status.HTTP_200_OK,
)
def update_durable_review_group(
    group_id: UUID,
    request: UpdateReviewGroupRequest,
    session: Annotated[Session, Depends(get_session)],
) -> ReviewBatchGroupsResponse:
    provided_fields = request.model_fields_set
    patch = UpdateGroupPatch(
        cover_image_id=request.cover_image_id,
        approved_category_id=request.approved_category_id,
        has_cover_image_id="cover_image_id" in provided_fields,
        has_approved_category_id="approved_category_id" in provided_fields,
    )
    try:
        snapshot = update_review_group(
            session,
            group_id=group_id,
            patch=patch,
        )
    except ReviewEditBatchNotFoundError as error:
        raise _upload_batch_not_found() from error
    except ReviewEditResourceNotFoundError as error:
        raise _review_edit_not_found(str(error)) from error
    except ReviewEditValidationError as error:
        raise _invalid_review_edit(str(error)) from error
    except ReviewEditStateError as error:
        raise _review_edit_state_error(str(error)) from error
    except SQLAlchemyError as error:
        session.rollback()
        raise _upload_batch_database_error("Unable to update review group.") from error

    return _review_batch_groups_response(snapshot)


@app.patch(
    "/v1/groups/{group_id}/images/{image_id}",
    response_model=ReviewBatchGroupsResponse,
    status_code=status.HTTP_200_OK,
)
def update_durable_review_group_image(
    group_id: UUID,
    image_id: UUID,
    request: UpdateReviewGroupImageRequest,
    session: Annotated[Session, Depends(get_session)],
) -> ReviewBatchGroupsResponse:
    try:
        snapshot = update_group_image_duplicate(
            session,
            group_id=group_id,
            image_id=image_id,
            is_duplicate=request.is_duplicate,
            duplicate_of_image_id=request.duplicate_of_image_id,
        )
    except ReviewEditBatchNotFoundError as error:
        raise _upload_batch_not_found() from error
    except ReviewEditResourceNotFoundError as error:
        raise _review_edit_not_found(str(error)) from error
    except ReviewEditValidationError as error:
        raise _invalid_review_edit(str(error)) from error
    except ReviewEditConflictError as error:
        raise _review_edit_conflict(
            code=error.code,
            message=str(error),
        ) from error
    except ReviewEditStateError as error:
        raise _review_edit_state_error(str(error)) from error
    except SQLAlchemyError as error:
        session.rollback()
        raise _upload_batch_database_error(
            "Unable to update review group image."
        ) from error

    return _review_batch_groups_response(snapshot)


@app.post(
    "/v1/groups/{group_id}/images/{image_id}/reject",
    response_model=ReviewBatchGroupsResponse,
    status_code=status.HTTP_200_OK,
)
def reject_durable_review_group_image(
    group_id: UUID,
    image_id: UUID,
    session: Annotated[Session, Depends(get_session)],
) -> ReviewBatchGroupsResponse:
    try:
        snapshot = reject_group_image(
            session,
            group_id=group_id,
            image_id=image_id,
        )
    except ReviewEditBatchNotFoundError as error:
        raise _upload_batch_not_found() from error
    except ReviewEditResourceNotFoundError as error:
        raise _review_edit_not_found(str(error)) from error
    except ReviewEditValidationError as error:
        raise _invalid_review_edit(str(error)) from error
    except ReviewEditConflictError as error:
        raise _review_edit_conflict(
            code=error.code,
            message=str(error),
        ) from error
    except ReviewEditStateError as error:
        raise _review_edit_state_error(str(error)) from error
    except SQLAlchemyError as error:
        session.rollback()
        raise _upload_batch_database_error(
            "Unable to reject review group image."
        ) from error

    return _review_batch_groups_response(snapshot)


@app.post(
    "/v1/groups/{group_id}/images/{image_id}/restore-rejection",
    response_model=ReviewBatchGroupsResponse,
    status_code=status.HTTP_200_OK,
)
def restore_durable_review_group_image_rejection(
    group_id: UUID,
    image_id: UUID,
    session: Annotated[Session, Depends(get_session)],
) -> ReviewBatchGroupsResponse:
    try:
        snapshot = restore_group_image_rejection(
            session,
            group_id=group_id,
            image_id=image_id,
        )
    except ReviewEditBatchNotFoundError as error:
        raise _upload_batch_not_found() from error
    except ReviewEditResourceNotFoundError as error:
        raise _review_edit_not_found(str(error)) from error
    except ReviewEditValidationError as error:
        raise _invalid_review_edit(str(error)) from error
    except ReviewEditConflictError as error:
        raise _review_edit_conflict(
            code=error.code,
            message=str(error),
        ) from error
    except ReviewEditStateError as error:
        raise _review_edit_state_error(str(error)) from error
    except SQLAlchemyError as error:
        session.rollback()
        raise _upload_batch_database_error(
            "Unable to restore review group image rejection."
        ) from error

    return _review_batch_groups_response(snapshot)


@app.post(
    "/v1/groups/{group_id}/approve",
    response_model=ReviewBatchGroupsResponse,
    status_code=status.HTTP_200_OK,
)
def approve_durable_review_group(
    group_id: UUID,
    session: Annotated[Session, Depends(get_session)],
) -> ReviewBatchGroupsResponse:
    try:
        snapshot = approve_review_group(session, group_id=group_id)
    except ReviewApprovalBatchNotFoundError as error:
        raise _upload_batch_not_found() from error
    except ReviewApprovalResourceNotFoundError as error:
        raise _review_edit_not_found(str(error)) from error
    except ReviewApprovalStateError as error:
        raise _review_approval_state_error(str(error)) from error
    except SQLAlchemyError as error:
        session.rollback()
        raise _upload_batch_database_error("Unable to approve review group.") from error

    return _review_batch_groups_response(snapshot)


@app.post(
    "/v1/upload-batches/{batch_id}/approve",
    response_model=ReviewBatchGroupsResponse,
    status_code=status.HTTP_200_OK,
)
def approve_durable_review_batch(
    batch_id: UUID,
    session: Annotated[Session, Depends(get_session)],
) -> ReviewBatchGroupsResponse:
    try:
        snapshot = approve_review_batch(session, batch_id=batch_id)
    except ReviewApprovalBatchNotFoundError as error:
        raise _upload_batch_not_found() from error
    except ReviewApprovalResourceNotFoundError as error:
        raise _review_edit_not_found(str(error)) from error
    except ReviewApprovalStateError as error:
        raise _review_approval_state_error(str(error)) from error
    except SQLAlchemyError as error:
        session.rollback()
        raise _upload_batch_database_error("Unable to approve review batch.") from error

    return _review_batch_groups_response(snapshot)


@app.post(
    "/internal/tasks/process-image",
    response_model=ProcessImageTaskResponse,
    status_code=status.HTTP_200_OK,
)
def process_image_worker_task(
    request: ProcessImageTaskRequest,
    session: Annotated[Session, Depends(get_session)],
    storage_client: Annotated[WorkerStorage, Depends(get_worker_storage)],
    embedding_provider: Annotated[
        ImageEmbeddingProvider,
        Depends(get_image_embedding_provider),
    ],
    processing_queue: Annotated[
        ProcessingQueue,
        Depends(get_processing_queue),
    ],
) -> ProcessImageTaskResponse:
    try:
        result = process_image_task(
            session,
            payload=ProcessImageTaskPayload(
                batch_id=request.batch_id,
                image_id=request.image_id,
                pipeline_version=request.pipeline_version,
            ),
            storage=storage_client,
            embedding_provider=embedding_provider,
            queue=processing_queue,
        )
    except ProcessingJobNotFoundError as error:
        raise _processing_job_not_found() from error
    except ProcessingJobExecutionError as error:
        raise _processing_job_error(
            code=error.error_code,
            message=error.message,
        ) from error
    except SQLAlchemyError as error:
        session.rollback()
        raise _processing_job_error(
            code="database_error",
            message="Unable to persist the image processing result.",
        ) from error

    return ProcessImageTaskResponse(
        batch_id=result.batch_id,
        image_id=result.image_id,
        pipeline_version=result.pipeline_version,
        job_status=result.job_status,
        did_work=result.did_work,
    )


@app.post(
    "/internal/tasks/classify-image",
    response_model=ClassifyImageTaskResponse,
    status_code=status.HTTP_200_OK,
)
def classify_image_worker_task(
    request: ClassifyImageTaskRequest,
    session: Annotated[Session, Depends(get_session)],
    storage_client: Annotated[WorkerStorage, Depends(get_worker_storage)],
    category_provider: Annotated[
        CategorySuggestionProvider,
        Depends(get_category_suggestion_provider),
    ],
) -> ClassifyImageTaskResponse:
    try:
        result = classify_image_task(
            session,
            payload=ClassifyImageTaskPayload(
                batch_id=request.batch_id,
                image_id=request.image_id,
                pipeline_version=request.pipeline_version,
            ),
            storage=storage_client,
            category_provider=category_provider,
        )
    except ProcessingJobNotFoundError as error:
        raise _processing_job_not_found() from error
    except ProcessingJobExecutionError as error:
        raise _processing_job_error(
            code=error.error_code,
            message=error.message,
        ) from error
    except SQLAlchemyError as error:
        session.rollback()
        raise _processing_job_error(
            code="database_error",
            message="Unable to persist the image classification result.",
        ) from error

    return ClassifyImageTaskResponse(
        batch_id=result.batch_id,
        image_id=result.image_id,
        pipeline_version=result.pipeline_version,
        job_status=result.job_status,
        did_work=result.did_work,
    )


@app.post(
    "/internal/tasks/group-batch",
    response_model=GroupBatchTaskResponse,
    status_code=status.HTTP_200_OK,
)
def group_batch_worker_task(
    request: GroupBatchTaskRequest,
    session: Annotated[Session, Depends(get_session)],
) -> GroupBatchTaskResponse:
    try:
        result = group_batch_task(
            session,
            payload=GroupBatchTaskPayload(
                batch_id=request.batch_id,
                pipeline_version=request.pipeline_version,
            ),
        )
    except (GroupingBatchNotFoundError, GroupingJobNotFoundError) as error:
        raise _processing_job_not_found() from error
    except GroupingBatchStateError as error:
        raise _processing_job_error(
            code="invalid_batch_state",
            message="Upload batch is not ready for grouping.",
        ) from error
    except SQLAlchemyError as error:
        session.rollback()
        raise _processing_job_error(
            code="database_error",
            message="Unable to persist the batch grouping result.",
        ) from error

    return GroupBatchTaskResponse(
        batch_id=result.batch_id,
        pipeline_version=result.pipeline_version,
        job_status=result.job_status,
        did_work=result.did_work,
    )


@app.get(
    "/v1/upload-batches/{batch_id}",
    response_model=UploadBatchResponse,
)
def get_durable_upload_batch(
    batch_id: UUID,
    session: Annotated[Session, Depends(get_session)],
) -> UploadBatchResponse:
    try:
        snapshot = get_upload_batch(session, batch_id=batch_id)
    except UploadBatchNotFoundError as error:
        raise _upload_batch_not_found() from error
    except SQLAlchemyError as error:
        session.rollback()
        raise _upload_batch_database_error(
            "Unable to load the upload batch."
        ) from error

    return _upload_batch_response(snapshot)


@app.post(
    "/v1/upload-handshake",
    response_model=UploadHandshakeResponse,
    status_code=status.HTTP_200_OK,
)
async def upload_handshake(
    files: Annotated[list[UploadFile] | None, File()] = None,
) -> UploadHandshakeResponse:
    validated_uploads = await _validated_uploads(files)
    file_results = [
        UploadFileResult(
            filename=upload.original_filename,
            status="accepted" if upload.is_accepted else "rejected",
            size_bytes=upload.size_bytes,
            error_code=upload.error_code,
            error_message=upload.error_message,
        )
        for upload in validated_uploads
    ]
    accepted_count = sum(upload.is_accepted for upload in validated_uploads)

    return UploadHandshakeResponse(
        upload_id=uuid4(),
        status=_overall_status(accepted_count, len(validated_uploads)),
        files=file_results,
    )


@app.post(
    "/v1/local-batches",
    response_model=CreateLocalBatchResponse,
    status_code=status.HTTP_200_OK,
)
async def create_local_batch(
    files: Annotated[list[UploadFile] | None, File()] = None,
) -> CreateLocalBatchResponse:
    validated_uploads = await _validated_uploads(files)
    accepted_uploads = [
        upload for upload in validated_uploads if upload.is_accepted
    ]
    overall_status = _overall_status(
        len(accepted_uploads),
        len(validated_uploads),
    )

    if not accepted_uploads:
        return CreateLocalBatchResponse(
            batch_id=None,
            status=overall_status,
            manifest_version=None,
            files=[
                LocalBatchFileResult(
                    image_id=None,
                    original_filename=upload.original_filename,
                    status="rejected",
                    error_code=upload.error_code,
                    error_message=upload.error_message,
                )
                for upload in validated_uploads
            ],
        )

    manifest = LocalBatchStore.from_environment().create_batch(accepted_uploads)
    accepted_images = iter(manifest.images)
    file_results = []

    for upload in validated_uploads:
        if upload.is_accepted:
            image = next(accepted_images)
            file_results.append(
                LocalBatchFileResult(
                    image_id=image.image_id,
                    original_filename=upload.original_filename,
                    status="accepted",
                )
            )
        else:
            file_results.append(
                LocalBatchFileResult(
                    image_id=None,
                    original_filename=upload.original_filename,
                    status="rejected",
                    error_code=upload.error_code,
                    error_message=upload.error_message,
                )
            )

    return CreateLocalBatchResponse(
        batch_id=manifest.batch_id,
        status=overall_status,
        manifest_version=MANIFEST_VERSION,
        files=file_results,
    )


def _local_batch_response(manifest: BatchManifest) -> LocalBatchResponse:
    batch_id = manifest.batch_id
    return LocalBatchResponse(
        batch_id=batch_id,
        status="ready",
        manifest_version=manifest.manifest_version,
        images=[
            LocalBatchImageResponse(
                image_id=image.image_id,
                original_filename=image.original_filename,
                thumbnail_url=(
                    f"/v1/local-batches/{batch_id}/images/"
                    f"{image.image_id}/thumbnail"
                ),
                image_url=(
                    f"/v1/local-batches/{batch_id}/images/{image.image_id}"
                ),
                sha256=image.sha256,
                group_id=image.group_id,
                is_retained=image.is_retained,
            )
            for image in manifest.images
        ],
        groups=[
            LocalBatchGroupResponse(
                group_id=group.group_id,
                retained_image_id=group.retained_image_id,
                image_ids=group.image_ids,
            )
            for group in manifest.groups
        ],
    )


@app.get(
    "/v1/local-batches/{batch_id}",
    response_model=LocalBatchResponse,
)
async def get_local_batch(batch_id: UUID) -> LocalBatchResponse:
    try:
        manifest = LocalBatchStore.from_environment().load_batch(batch_id)
    except LocalBatchNotFoundError as error:
        raise _batch_not_found() from error
    return _local_batch_response(manifest)


@app.get("/v1/local-batches/{batch_id}/images/{image_id}")
async def get_local_batch_image(
    batch_id: UUID,
    image_id: UUID,
) -> FileResponse:
    return _local_image_response(batch_id, image_id, thumbnail=False)


@app.get("/v1/local-batches/{batch_id}/images/{image_id}/thumbnail")
async def get_local_batch_thumbnail(
    batch_id: UUID,
    image_id: UUID,
) -> FileResponse:
    return _local_image_response(batch_id, image_id, thumbnail=True)


@app.patch(
    "/v1/local-batches/{batch_id}/images/{image_id}",
    response_model=MoveImageResponse,
)
async def move_local_batch_image(
    batch_id: UUID,
    image_id: UUID,
    request: MoveImageRequest,
) -> MoveImageResponse:
    try:
        manifest = LocalBatchStore.from_environment().move_image(
            batch_id,
            image_id,
            request.group_id,
        )
    except LocalBatchNotFoundError as error:
        raise _batch_not_found() from error
    except LocalImageNotFoundError as error:
        raise _image_not_found() from error
    except LocalGroupNotFoundError as error:
        raise _group_not_found() from error

    return MoveImageResponse(batch=_local_batch_response(manifest))


@app.post(
    "/v1/local-batches/{batch_id}/groups",
    response_model=CreateGroupResponse,
)
async def create_local_batch_group(
    batch_id: UUID,
    request: CreateGroupRequest,
) -> CreateGroupResponse:
    try:
        group_id, manifest = LocalBatchStore.from_environment().create_group(
            batch_id,
            request.image_ids,
        )
    except LocalBatchNotFoundError as error:
        raise _batch_not_found() from error
    except LocalImageNotFoundError as error:
        raise _image_not_found() from error
    except InvalidLocalBatchEditError as error:
        raise _invalid_selection(str(error)) from error

    return CreateGroupResponse(
        group_id=group_id,
        batch=_local_batch_response(manifest),
    )


def _local_image_response(
    batch_id: UUID,
    image_id: UUID,
    *,
    thumbnail: bool,
) -> FileResponse:
    try:
        image_path = LocalBatchStore.from_environment().image_path(
            batch_id,
            image_id,
            thumbnail=thumbnail,
        )
    except LocalBatchNotFoundError as error:
        raise _batch_not_found() from error
    except LocalImageNotFoundError as error:
        raise _image_not_found() from error

    return FileResponse(image_path, media_type="image/jpeg")
