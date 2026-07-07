from __future__ import annotations

import os
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

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
from catalog_api.local_batches import (
    MANIFEST_VERSION,
    BatchManifest,
    InvalidLocalBatchEditError,
    LocalBatchNotFoundError,
    LocalBatchStore,
    LocalGroupNotFoundError,
    LocalImageNotFoundError,
)
from catalog_api.processing_jobs import (
    ClassifyImageTaskPayload,
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
    get_processing_batch_state,
    get_processing_runner,
    start_processing_batch,
)
from catalog_api.processing_storage import WorkerStorage, get_worker_storage
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


app = FastAPI(title="Catalog Classifier API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_web_origins(),
    allow_credentials=False,
    allow_methods=["GET", "PATCH", "POST"],
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
