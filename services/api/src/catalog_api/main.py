from __future__ import annotations

import os
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import FastAPI, File, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

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
