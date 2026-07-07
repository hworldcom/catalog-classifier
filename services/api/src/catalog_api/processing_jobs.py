from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from catalog_api.category_suggestion_providers import (
    CategorySuggestionProvider,
    CategorySuggestionProviderError,
    CategorySuggestionResult,
    generate_category_suggestion,
)
from catalog_api.embedding_vectors import EMBEDDING_DIMENSIONS
from catalog_api.image_embedding_providers import (
    ImageEmbeddingResult,
    ImageEmbeddingProvider,
    ImageEmbeddingProviderError,
    generate_image_embedding,
)
from catalog_api.image_hashes import PerceptualHashError, compute_perceptual_hashes
from catalog_api.image_processing import (
    JPEG_CONTENT_TYPE,
    TerminalImageProcessingError,
    derived_image_keys,
    process_original_image,
)
from catalog_api.models import (
    Category,
    ImageAsset,
    ImageClassification,
    ImageEmbedding,
    ProcessingJob,
    UploadBatch,
)
from catalog_api.processing_storage import (
    WorkerObjectNotFoundError,
    WorkerObjectReadError,
    WorkerObjectWriteError,
    WorkerStorage,
)
from catalog_api.upload_batches import DEFAULT_ORGANIZATION_ID

PROCESS_IMAGE_JOB_TYPE = "process-image"
CLASSIFY_IMAGE_JOB_TYPE = "classify-image"
CLASSIFICATION_CONFIDENCE_THRESHOLD = 0.80
TERMINAL_IMAGE_STATUSES = {"processed", "failed"}


class ProcessingBatchNotFoundError(Exception):
    """Raised when a batch cannot be found for processing."""


class ProcessingBatchStateError(Exception):
    """Raised when a batch cannot be claimed for processing."""


class ProcessingJobNotFoundError(Exception):
    """Raised when a process-image task cannot find its job row."""


class ProcessingJobExecutionError(Exception):
    def __init__(self, *, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


@dataclass(frozen=True)
class ProcessImageTaskPayload:
    batch_id: UUID
    image_id: UUID
    pipeline_version: str


@dataclass(frozen=True)
class ClassifyImageTaskPayload:
    batch_id: UUID
    image_id: UUID
    pipeline_version: str


class ProcessingQueue(Protocol):
    def enqueue_process_image(self, payload: ProcessImageTaskPayload) -> None:
        """Enqueue a process-image task."""

    def enqueue_classify_image(self, payload: ClassifyImageTaskPayload) -> None:
        """Enqueue a classify-image task."""


class InMemoryProcessingQueue:
    def __init__(self) -> None:
        self.process_image_tasks: list[ProcessImageTaskPayload] = []
        self.classify_image_tasks: list[ClassifyImageTaskPayload] = []

    def enqueue_process_image(self, payload: ProcessImageTaskPayload) -> None:
        self.process_image_tasks.append(payload)

    def enqueue_classify_image(self, payload: ClassifyImageTaskPayload) -> None:
        self.classify_image_tasks.append(payload)


@lru_cache
def get_processing_queue() -> ProcessingQueue:
    return InMemoryProcessingQueue()


@dataclass(frozen=True)
class ClaimedProcessingJob:
    image_id: UUID
    idempotency_key: str
    status: str


@dataclass(frozen=True)
class BatchProcessingClaim:
    batch_id: UUID
    status: str
    pipeline_version: str
    jobs: list[ClaimedProcessingJob]
    enqueued_tasks: list[ProcessImageTaskPayload]


@dataclass(frozen=True)
class ProcessImageTaskResult:
    batch_id: UUID
    image_id: UUID
    pipeline_version: str
    job_status: str
    did_work: bool


@dataclass(frozen=True)
class ClassifyImageTaskResult:
    batch_id: UUID
    image_id: UUID
    pipeline_version: str
    job_status: str
    did_work: bool


def process_image_idempotency_key(
    *,
    image_id: UUID,
    pipeline_version: str,
) -> str:
    return f"{PROCESS_IMAGE_JOB_TYPE}:{image_id}:{pipeline_version}"


def classify_image_idempotency_key(
    *,
    image_id: UUID,
    pipeline_version: str,
) -> str:
    return f"{CLASSIFY_IMAGE_JOB_TYPE}:{image_id}:{pipeline_version}"


def claim_batch_for_processing(
    session: Session,
    *,
    batch_id: UUID,
    pipeline_version: str,
    queue: ProcessingQueue,
) -> BatchProcessingClaim:
    normalized_pipeline_version = pipeline_version.strip()
    if not normalized_pipeline_version:
        raise ProcessingBatchStateError("Pipeline version must be nonblank.")

    batch = session.scalar(
        select(UploadBatch)
        .where(
            UploadBatch.id == batch_id,
            UploadBatch.organization_id == DEFAULT_ORGANIZATION_ID,
        )
        .with_for_update()
    )
    if batch is None:
        raise ProcessingBatchNotFoundError
    if batch.status not in {"queued", "processing"}:
        raise ProcessingBatchStateError(
            "Only queued or processing batches can be claimed."
        )
    if (
        batch.status == "processing"
        and batch.pipeline_version != normalized_pipeline_version
    ):
        raise ProcessingBatchStateError(
            "Processing batches must be claimed with their active pipeline version."
        )

    images = session.scalars(
        select(ImageAsset)
        .where(
            ImageAsset.batch_id == batch.id,
            ImageAsset.organization_id == batch.organization_id,
        )
        .order_by(ImageAsset.upload_order)
        .with_for_update()
    ).all()
    existing_jobs = session.scalars(
        select(ProcessingJob)
        .where(
            ProcessingJob.batch_id == batch.id,
            ProcessingJob.organization_id == batch.organization_id,
            ProcessingJob.job_type == PROCESS_IMAGE_JOB_TYPE,
            ProcessingJob.pipeline_version == normalized_pipeline_version,
        )
        .with_for_update()
    ).all()
    existing_jobs_by_image_id = {
        job.image_id: job for job in existing_jobs if job.image_id is not None
    }

    enqueued_tasks: list[ProcessImageTaskPayload] = []
    for image in images:
        if image.id in existing_jobs_by_image_id or image.status == "failed":
            continue
        task = ProcessImageTaskPayload(
            batch_id=batch.id,
            image_id=image.id,
            pipeline_version=normalized_pipeline_version,
        )
        job = ProcessingJob(
            organization_id=batch.organization_id,
            batch_id=batch.id,
            image_id=image.id,
            job_type=PROCESS_IMAGE_JOB_TYPE,
            pipeline_version=normalized_pipeline_version,
            idempotency_key=process_image_idempotency_key(
                image_id=image.id,
                pipeline_version=normalized_pipeline_version,
            ),
        )
        session.add(job)
        existing_jobs.append(job)
        existing_jobs_by_image_id[image.id] = job
        enqueued_tasks.append(task)

    if batch.status == "queued":
        batch.status = "processing"
        batch.pipeline_version = normalized_pipeline_version

    session.commit()

    for task in enqueued_tasks:
        queue.enqueue_process_image(task)

    return BatchProcessingClaim(
        batch_id=batch.id,
        status=batch.status,
        pipeline_version=normalized_pipeline_version,
        jobs=[
            ClaimedProcessingJob(
                image_id=image.id,
                idempotency_key=existing_jobs_by_image_id[image.id].idempotency_key,
                status=existing_jobs_by_image_id[image.id].status,
            )
            for image in images
            if image.id in existing_jobs_by_image_id
        ],
        enqueued_tasks=enqueued_tasks,
    )


def ensure_classify_jobs_for_processed_images(
    session: Session,
    *,
    batch_id: UUID,
    pipeline_version: str,
) -> list[ClassifyImageTaskPayload]:
    processed_images = session.scalars(
        select(ImageAsset)
        .where(
            ImageAsset.batch_id == batch_id,
            ImageAsset.organization_id == DEFAULT_ORGANIZATION_ID,
            ImageAsset.status == "processed",
        )
        .order_by(ImageAsset.upload_order)
        .with_for_update()
    ).all()
    if not processed_images:
        return []

    existing_jobs = session.scalars(
        select(ProcessingJob)
        .where(
            ProcessingJob.batch_id == batch_id,
            ProcessingJob.organization_id == DEFAULT_ORGANIZATION_ID,
            ProcessingJob.job_type == CLASSIFY_IMAGE_JOB_TYPE,
            ProcessingJob.pipeline_version == pipeline_version,
            ProcessingJob.image_id.in_([image.id for image in processed_images]),
        )
        .with_for_update()
    ).all()
    existing_image_ids = {
        job.image_id for job in existing_jobs if job.image_id is not None
    }

    tasks: list[ClassifyImageTaskPayload] = []
    for image in processed_images:
        if image.id in existing_image_ids:
            continue
        task = ClassifyImageTaskPayload(
            batch_id=image.batch_id,
            image_id=image.id,
            pipeline_version=pipeline_version,
        )
        session.add(
            ProcessingJob(
                organization_id=image.organization_id,
                batch_id=image.batch_id,
                image_id=image.id,
                job_type=CLASSIFY_IMAGE_JOB_TYPE,
                pipeline_version=pipeline_version,
                idempotency_key=classify_image_idempotency_key(
                    image_id=image.id,
                    pipeline_version=pipeline_version,
                ),
            )
        )
        tasks.append(task)

    session.commit()
    return tasks


def process_image_task(
    session: Session,
    *,
    payload: ProcessImageTaskPayload,
    storage: WorkerStorage,
    embedding_provider: ImageEmbeddingProvider,
    queue: ProcessingQueue,
) -> ProcessImageTaskResult:
    job = session.scalar(
        select(ProcessingJob)
        .where(
            ProcessingJob.batch_id == payload.batch_id,
            ProcessingJob.image_id == payload.image_id,
            ProcessingJob.organization_id == DEFAULT_ORGANIZATION_ID,
            ProcessingJob.job_type == PROCESS_IMAGE_JOB_TYPE,
            ProcessingJob.pipeline_version == payload.pipeline_version,
            ProcessingJob.idempotency_key
            == process_image_idempotency_key(
                image_id=payload.image_id,
                pipeline_version=payload.pipeline_version,
            ),
        )
        .with_for_update()
    )
    if job is None:
        raise ProcessingJobNotFoundError

    image = session.scalar(
        select(ImageAsset)
        .where(
            ImageAsset.id == payload.image_id,
            ImageAsset.batch_id == payload.batch_id,
            ImageAsset.organization_id == DEFAULT_ORGANIZATION_ID,
        )
        .with_for_update()
    )
    if image is None:
        raise ProcessingJobNotFoundError

    batch = session.scalar(
        select(UploadBatch)
        .where(
            UploadBatch.id == payload.batch_id,
            UploadBatch.organization_id == DEFAULT_ORGANIZATION_ID,
        )
        .with_for_update()
    )
    if batch is None:
        raise ProcessingJobNotFoundError

    if job.status == "completed" and _image_has_completed_pipeline_outputs(
        session,
        image=image,
        pipeline_version=payload.pipeline_version,
    ):
        session.commit()
        return ProcessImageTaskResult(
            batch_id=payload.batch_id,
            image_id=payload.image_id,
            pipeline_version=payload.pipeline_version,
            job_status="completed",
            did_work=False,
        )

    if job.status == "failed" and image.status == "failed":
        session.commit()
        return ProcessImageTaskResult(
            batch_id=payload.batch_id,
            image_id=payload.image_id,
            pipeline_version=payload.pipeline_version,
            job_status="failed",
            did_work=False,
        )

    if image.status == "processed" and _image_has_completed_pipeline_outputs(
        session,
        image=image,
        pipeline_version=payload.pipeline_version,
    ):
        job.status = "completed"
        job.completed_at = datetime.now(UTC)
        job.error_message = None
        session.commit()
        return ProcessImageTaskResult(
            batch_id=payload.batch_id,
            image_id=payload.image_id,
            pipeline_version=payload.pipeline_version,
            job_status="completed",
            did_work=False,
        )

    if image.status not in {"uploaded", "processing", "processed"}:
        raise ProcessingJobExecutionError(
            error_code="invalid_image_state",
            message="Image is not ready for processing.",
        )

    was_terminal = image.status in TERMINAL_IMAGE_STATUSES
    job.status = "started"
    job.started_at = datetime.now(UTC)
    job.completed_at = None
    job.error_message = None
    job.attempt_count += 1
    image.status = "processing"
    image.error_code = None
    image.error_message = None

    try:
        original_bytes = storage.read_object_bytes(
            object_key=image.original_object_key
        )
    except WorkerObjectNotFoundError:
        _mark_terminal_image_failure(
            batch=batch,
            image=image,
            job=job,
            was_terminal=was_terminal,
            error_code="source_object_missing",
            error_message="The source object was not found in worker storage.",
        )
        session.commit()
        return ProcessImageTaskResult(
            batch_id=payload.batch_id,
            image_id=payload.image_id,
            pipeline_version=payload.pipeline_version,
            job_status="failed",
            did_work=True,
        )
    except WorkerObjectReadError as error:
        _mark_retryable_worker_failure(
            image=image,
            job=job,
            error_code="source_object_read_failed",
            error_message="The source object could not be read.",
        )
        session.commit()
        raise ProcessingJobExecutionError(
            error_code="source_object_read_failed",
            message="The source object could not be read.",
        ) from error

    try:
        derivatives = process_original_image(original_bytes)
    except TerminalImageProcessingError as error:
        _mark_terminal_image_failure(
            batch=batch,
            image=image,
            job=job,
            was_terminal=was_terminal,
            error_code=error.error_code,
            error_message=error.message,
        )
        session.commit()
        return ProcessImageTaskResult(
            batch_id=payload.batch_id,
            image_id=payload.image_id,
            pipeline_version=payload.pipeline_version,
            job_status="failed",
            did_work=True,
        )

    try:
        perceptual_hashes = compute_perceptual_hashes(derivatives.normalized_bytes)
    except PerceptualHashError as error:
        _mark_retryable_worker_failure(
            image=image,
            job=job,
            error_code="image_hash_generation_failed",
            error_message="The normalized image could not be perceptually hashed.",
        )
        session.commit()
        raise ProcessingJobExecutionError(
            error_code="image_hash_generation_failed",
            message="The normalized image could not be perceptually hashed.",
        ) from error

    try:
        embedding_result = generate_image_embedding(
            embedding_provider,
            image_bytes=derivatives.inference_bytes,
            mime_type=JPEG_CONTENT_TYPE,
        )
    except ImageEmbeddingProviderError as error:
        _mark_retryable_worker_failure(
            image=image,
            job=job,
            error_code="embedding_generation_failed",
            error_message="The image embedding could not be generated.",
        )
        session.commit()
        raise ProcessingJobExecutionError(
            error_code="embedding_generation_failed",
            message="The image embedding could not be generated.",
        ) from error

    keys = derived_image_keys(
        organization_id=image.organization_id,
        batch_id=image.batch_id,
        pipeline_version=payload.pipeline_version,
        image_id=image.id,
    )
    try:
        storage.write_object_bytes(
            object_key=keys.normalized_object_key,
            content_type=JPEG_CONTENT_TYPE,
            data=derivatives.normalized_bytes,
        )
        storage.write_object_bytes(
            object_key=keys.inference_object_key,
            content_type=JPEG_CONTENT_TYPE,
            data=derivatives.inference_bytes,
        )
        storage.write_object_bytes(
            object_key=keys.thumbnail_object_key,
            content_type=JPEG_CONTENT_TYPE,
            data=derivatives.thumbnail_bytes,
        )
    except WorkerObjectWriteError as error:
        _mark_retryable_worker_failure(
            image=image,
            job=job,
            error_code="derived_object_write_failed",
            error_message="A derived object could not be written.",
        )
        session.commit()
        raise ProcessingJobExecutionError(
            error_code="derived_object_write_failed",
            message="A derived object could not be written.",
        ) from error

    image.normalized_object_key = keys.normalized_object_key
    image.inference_object_key = keys.inference_object_key
    image.thumbnail_object_key = keys.thumbnail_object_key
    image.normalized_format = derivatives.normalized_format
    image.normalized_size_bytes = derivatives.normalized_size_bytes
    image.width = derivatives.width
    image.height = derivatives.height
    image.sha256 = derivatives.sha256
    image.phash = perceptual_hashes.phash
    image.dhash = perceptual_hashes.dhash
    image.status = "processed"
    image.error_code = None
    image.error_message = None
    _upsert_image_embedding(
        session,
        image=image,
        pipeline_version=payload.pipeline_version,
        embedding_result=embedding_result,
    )
    classify_task = _ensure_classify_image_job(
        session,
        image=image,
        pipeline_version=payload.pipeline_version,
    )
    job.status = "completed"
    job.completed_at = datetime.now(UTC)
    job.error_message = None
    _increment_processed_count_once(batch=batch, was_terminal=was_terminal)
    session.commit()
    if classify_task is not None:
        queue.enqueue_classify_image(classify_task)

    return ProcessImageTaskResult(
        batch_id=payload.batch_id,
        image_id=payload.image_id,
        pipeline_version=payload.pipeline_version,
        job_status="completed",
        did_work=True,
    )


def classify_image_task(
    session: Session,
    *,
    payload: ClassifyImageTaskPayload,
    storage: WorkerStorage,
    category_provider: CategorySuggestionProvider,
) -> ClassifyImageTaskResult:
    job = session.scalar(
        select(ProcessingJob)
        .where(
            ProcessingJob.batch_id == payload.batch_id,
            ProcessingJob.image_id == payload.image_id,
            ProcessingJob.organization_id == DEFAULT_ORGANIZATION_ID,
            ProcessingJob.job_type == CLASSIFY_IMAGE_JOB_TYPE,
            ProcessingJob.pipeline_version == payload.pipeline_version,
            ProcessingJob.idempotency_key
            == classify_image_idempotency_key(
                image_id=payload.image_id,
                pipeline_version=payload.pipeline_version,
            ),
        )
        .with_for_update()
    )
    if job is None:
        raise ProcessingJobNotFoundError

    image = session.scalar(
        select(ImageAsset)
        .where(
            ImageAsset.id == payload.image_id,
            ImageAsset.batch_id == payload.batch_id,
            ImageAsset.organization_id == DEFAULT_ORGANIZATION_ID,
        )
        .with_for_update()
    )
    if image is None:
        raise ProcessingJobNotFoundError

    if _image_has_classification(
        session,
        image=image,
        pipeline_version=payload.pipeline_version,
    ):
        job.status = "completed"
        job.completed_at = job.completed_at or datetime.now(UTC)
        job.error_message = None
        session.commit()
        return ClassifyImageTaskResult(
            batch_id=payload.batch_id,
            image_id=payload.image_id,
            pipeline_version=payload.pipeline_version,
            job_status="completed",
            did_work=False,
        )

    if image.status != "processed" or image.inference_object_key is None:
        raise ProcessingJobExecutionError(
            error_code="invalid_image_state",
            message="Image is not ready for category classification.",
        )

    job.status = "started"
    job.started_at = datetime.now(UTC)
    job.completed_at = None
    job.error_message = None
    job.attempt_count += 1

    try:
        inference_bytes = storage.read_object_bytes(
            object_key=image.inference_object_key
        )
    except (WorkerObjectNotFoundError, WorkerObjectReadError) as error:
        _mark_retryable_job_failure(
            job=job,
            error_code="classification_input_read_failed",
            error_message="The inference image could not be read.",
        )
        session.commit()
        raise ProcessingJobExecutionError(
            error_code="classification_input_read_failed",
            message="The inference image could not be read.",
        ) from error

    taxonomy = _load_active_global_taxonomy(session)
    try:
        suggestion = generate_category_suggestion(
            category_provider,
            image_bytes=inference_bytes,
            category_slugs=sorted(taxonomy),
            mime_type=JPEG_CONTENT_TYPE,
        )
    except CategorySuggestionProviderError as error:
        _mark_retryable_job_failure(
            job=job,
            error_code="category_suggestion_failed",
            error_message="The category suggestion could not be generated.",
        )
        session.commit()
        raise ProcessingJobExecutionError(
            error_code="category_suggestion_failed",
            message="The category suggestion could not be generated.",
        ) from error

    category_id = _accepted_category_id(
        suggestion=suggestion,
        taxonomy=taxonomy,
    )
    persisted_slug = (
        suggestion.category_slug
        if category_id is not None and suggestion.category_slug is not None
        else "unknown"
    )
    session.add(
        ImageClassification(
            organization_id=image.organization_id,
            image_id=image.id,
            category_id=category_id,
            confidence=suggestion.confidence,
            attributes_json={
                "categorySlug": persisted_slug,
                "confidence": suggestion.confidence,
            },
            provider=suggestion.provider,
            model=suggestion.model,
            raw_response_json=suggestion.raw_response,
            pipeline_version=payload.pipeline_version,
        )
    )
    job.status = "completed"
    job.completed_at = datetime.now(UTC)
    job.error_message = None
    session.commit()

    return ClassifyImageTaskResult(
        batch_id=payload.batch_id,
        image_id=payload.image_id,
        pipeline_version=payload.pipeline_version,
        job_status="completed",
        did_work=True,
    )


def _ensure_classify_image_job(
    session: Session,
    *,
    image: ImageAsset,
    pipeline_version: str,
) -> ClassifyImageTaskPayload | None:
    idempotency_key = classify_image_idempotency_key(
        image_id=image.id,
        pipeline_version=pipeline_version,
    )
    existing_job = session.scalar(
        select(ProcessingJob)
        .where(ProcessingJob.idempotency_key == idempotency_key)
        .with_for_update()
    )
    if existing_job is not None:
        return None

    task = ClassifyImageTaskPayload(
        batch_id=image.batch_id,
        image_id=image.id,
        pipeline_version=pipeline_version,
    )
    session.add(
        ProcessingJob(
            organization_id=image.organization_id,
            batch_id=image.batch_id,
            image_id=image.id,
            job_type=CLASSIFY_IMAGE_JOB_TYPE,
            pipeline_version=pipeline_version,
            idempotency_key=idempotency_key,
        )
    )
    return task


def _mark_terminal_image_failure(
    *,
    batch: UploadBatch,
    image: ImageAsset,
    job: ProcessingJob,
    was_terminal: bool,
    error_code: str,
    error_message: str,
) -> None:
    image.status = "failed"
    image.error_code = error_code
    image.error_message = error_message
    job.status = "failed"
    job.error_message = f"{error_code}: {error_message}"
    _increment_processed_count_once(batch=batch, was_terminal=was_terminal)


def _mark_retryable_worker_failure(
    *,
    image: ImageAsset,
    job: ProcessingJob,
    error_code: str,
    error_message: str,
) -> None:
    image.status = "uploaded"
    image.error_code = None
    image.error_message = None
    job.status = "failed"
    job.error_message = f"{error_code}: {error_message}"


def _mark_retryable_job_failure(
    *,
    job: ProcessingJob,
    error_code: str,
    error_message: str,
) -> None:
    job.status = "failed"
    job.error_message = f"{error_code}: {error_message}"


def _increment_processed_count_once(
    *,
    batch: UploadBatch,
    was_terminal: bool,
) -> None:
    if not was_terminal:
        batch.processed_file_count += 1


def _image_has_completed_pipeline_outputs(
    session: Session,
    *,
    image: ImageAsset,
    pipeline_version: str,
) -> bool:
    if image.phash is None or image.dhash is None:
        return False

    embedding = session.scalar(
        select(ImageEmbedding)
        .where(
            ImageEmbedding.organization_id == image.organization_id,
            ImageEmbedding.image_id == image.id,
            ImageEmbedding.pipeline_version == pipeline_version,
            ImageEmbedding.dimensions == EMBEDDING_DIMENSIONS,
        )
        .with_for_update()
    )
    return embedding is not None


def _image_has_classification(
    session: Session,
    *,
    image: ImageAsset,
    pipeline_version: str,
) -> bool:
    classification = session.scalar(
        select(ImageClassification)
        .where(
            ImageClassification.organization_id == image.organization_id,
            ImageClassification.image_id == image.id,
            ImageClassification.pipeline_version == pipeline_version,
        )
        .with_for_update()
    )
    return classification is not None


def _load_active_global_taxonomy(session: Session) -> dict[str, UUID]:
    categories = session.scalars(
        select(Category).where(
            Category.organization_id.is_(None),
            Category.active.is_(True),
        )
    ).all()
    return {category.slug: category.id for category in categories}


def _accepted_category_id(
    *,
    suggestion: CategorySuggestionResult,
    taxonomy: dict[str, UUID],
) -> UUID | None:
    if suggestion.confidence < CLASSIFICATION_CONFIDENCE_THRESHOLD:
        return None
    if suggestion.category_slug is None:
        return None
    return taxonomy.get(suggestion.category_slug)


def _upsert_image_embedding(
    session: Session,
    *,
    image: ImageAsset,
    pipeline_version: str,
    embedding_result: ImageEmbeddingResult,
) -> None:
    existing_embedding = session.scalar(
        select(ImageEmbedding)
        .where(
            ImageEmbedding.organization_id == image.organization_id,
            ImageEmbedding.image_id == image.id,
            ImageEmbedding.pipeline_version == pipeline_version,
        )
        .with_for_update()
    )
    if existing_embedding is None:
        session.add(
            ImageEmbedding(
                organization_id=image.organization_id,
                image_id=image.id,
                provider=embedding_result.provider,
                model=embedding_result.model,
                dimensions=embedding_result.dimensions,
                pipeline_version=pipeline_version,
                embedding=embedding_result.embedding,
            )
        )
        return

    existing_embedding.provider = embedding_result.provider
    existing_embedding.model = embedding_result.model
    existing_embedding.dimensions = embedding_result.dimensions
    existing_embedding.embedding = embedding_result.embedding
