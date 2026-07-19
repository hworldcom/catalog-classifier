from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from threading import Lock, Thread
from typing import Callable, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from catalog_api.category_suggestion_providers import (
    CategorySuggestionProvider,
    get_category_suggestion_provider,
)
from catalog_api.database import get_session_factory
from catalog_api.image_embedding_providers import (
    ImageEmbeddingProvider,
    get_image_embedding_provider,
)
from catalog_api.grouping import (
    group_batch_task,
    record_group_batch_terminal_failure,
)
from catalog_api.models import (
    Category,
    ImageAsset,
    ImageClassification,
    ImageEmbedding,
    ProcessingJob,
    UploadBatch,
)
from catalog_api.processing_jobs import (
    CLASSIFY_IMAGE_JOB_TYPE,
    GROUP_BATCH_JOB_TYPE,
    PROCESS_IMAGE_JOB_TYPE,
    ClassifyImageTaskPayload,
    GroupBatchTaskPayload,
    InMemoryProcessingQueue,
    ProcessImageTaskPayload,
    ProcessingBatchNotFoundError,
    ProcessingBatchStateError,
    ProcessingJobExecutionError,
    ProcessingJobNotFoundError,
    classify_image_task,
    claim_batch_for_processing,
    ensure_classify_jobs_for_processed_images,
    ensure_group_batch_job_for_terminal_process_jobs,
    group_batch_idempotency_key,
    process_image_task,
)
from catalog_api.processing_storage import WorkerStorage, get_worker_storage
from catalog_api.processing_storage import (
    WorkerObjectNotFoundError,
    WorkerObjectReadError,
)
from catalog_api.upload_batches import DEFAULT_ORGANIZATION_ID

logger = logging.getLogger(__name__)

DEFAULT_PIPELINE_VERSION = "2026-06-01"
PROCESSING_BATCH_STATUSES = {
    "queued",
    "processing",
    "review_required",
    "approved",
    "failed",
    "cancelled",
}


@dataclass(frozen=True)
class ProcessingImageState:
    image_id: UUID
    upload_order: int
    original_filename: str
    image_status: str
    process_job_status: str | None
    process_error: str | None
    classify_job_status: str | None
    classify_error: str | None
    category_slug: str | None
    confidence: float | None
    has_hashes: bool
    has_embedding: bool


@dataclass(frozen=True)
class ProcessingBatchState:
    batch_id: UUID
    status: str
    original_file_count: int
    processed_file_count: int
    pipeline_version: str
    images: list[ProcessingImageState]


class ProcessingRunner(Protocol):
    def start(self, *, batch_id: UUID, pipeline_version: str) -> None:
        """Start local processing for a batch and return promptly."""


class ProcessingThumbnailNotFoundError(Exception):
    """Raised when a durable thumbnail is not available."""


class ProcessingThumbnailReadError(Exception):
    """Raised when a durable thumbnail cannot be read due to infrastructure."""


class LocalProcessingRunner:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        storage_factory: Callable[[], WorkerStorage],
        embedding_provider_factory: Callable[[], ImageEmbeddingProvider],
        category_provider_factory: Callable[[], CategorySuggestionProvider],
    ) -> None:
        self._session_factory = session_factory
        self._storage_factory = storage_factory
        self._embedding_provider_factory = embedding_provider_factory
        self._category_provider_factory = category_provider_factory
        self._running_keys: set[tuple[UUID, str]] = set()
        self._running_lock = Lock()

    def start(self, *, batch_id: UUID, pipeline_version: str) -> None:
        key = (batch_id, pipeline_version)
        with self._running_lock:
            if key in self._running_keys:
                return
            self._running_keys.add(key)

        thread = Thread(
            target=self._run_and_release,
            kwargs={"batch_id": batch_id, "pipeline_version": pipeline_version},
            daemon=True,
        )
        thread.start()

    def _run_and_release(self, *, batch_id: UUID, pipeline_version: str) -> None:
        try:
            run_processing_batch(
                session_factory=self._session_factory,
                storage=self._storage_factory(),
                embedding_provider=self._embedding_provider_factory(),
                category_provider=self._category_provider_factory(),
                batch_id=batch_id,
                pipeline_version=pipeline_version,
            )
        except Exception:
            logger.exception("Local processing runner failed.")
        finally:
            with self._running_lock:
                self._running_keys.discard((batch_id, pipeline_version))


def start_processing_batch(
    session: Session,
    *,
    batch_id: UUID,
    runner: ProcessingRunner,
    pipeline_version: str = DEFAULT_PIPELINE_VERSION,
) -> ProcessingBatchState:
    queue = InMemoryProcessingQueue()
    claim = claim_batch_for_processing(
        session,
        batch_id=batch_id,
        pipeline_version=pipeline_version,
        queue=queue,
    )
    ensure_classify_jobs_for_processed_images(
        session,
        batch_id=claim.batch_id,
        pipeline_version=claim.pipeline_version,
    )
    runner.start(
        batch_id=claim.batch_id,
        pipeline_version=claim.pipeline_version,
    )
    return get_processing_batch_state(
        session,
        batch_id=batch_id,
        fallback_pipeline_version=claim.pipeline_version,
    )


def get_processing_batch_state(
    session: Session,
    *,
    batch_id: UUID,
    fallback_pipeline_version: str = DEFAULT_PIPELINE_VERSION,
) -> ProcessingBatchState:
    batch = session.scalar(
        select(UploadBatch).where(
            UploadBatch.id == batch_id,
            UploadBatch.organization_id == DEFAULT_ORGANIZATION_ID,
        )
    )
    if batch is None:
        raise ProcessingBatchNotFoundError
    if batch.status not in PROCESSING_BATCH_STATUSES:
        raise ProcessingBatchStateError(
            "Processing state is only available for finalized batches."
        )

    pipeline_version = batch.pipeline_version or fallback_pipeline_version
    images = session.scalars(
        select(ImageAsset)
        .where(
            ImageAsset.batch_id == batch.id,
            ImageAsset.organization_id == batch.organization_id,
        )
        .order_by(ImageAsset.upload_order)
    ).all()
    image_ids = [image.id for image in images]

    process_jobs = _jobs_by_image_id(
        session,
        image_ids=image_ids,
        pipeline_version=pipeline_version,
        job_type=PROCESS_IMAGE_JOB_TYPE,
    )
    classify_jobs = _jobs_by_image_id(
        session,
        image_ids=image_ids,
        pipeline_version=pipeline_version,
        job_type=CLASSIFY_IMAGE_JOB_TYPE,
    )
    embedding_image_ids = _embedding_image_ids(
        session,
        image_ids=image_ids,
        pipeline_version=pipeline_version,
    )
    classifications = _classifications_by_image_id(
        session,
        image_ids=image_ids,
        pipeline_version=pipeline_version,
    )
    category_slugs = _category_slugs_by_id(
        session,
        category_ids=[
            classification.category_id
            for classification in classifications.values()
            if classification.category_id is not None
        ],
    )

    return ProcessingBatchState(
        batch_id=batch.id,
        status=batch.status,
        original_file_count=batch.original_file_count,
        processed_file_count=batch.processed_file_count,
        pipeline_version=pipeline_version,
        images=[
            _processing_image_state(
                image=image,
                process_job=process_jobs.get(image.id),
                classify_job=classify_jobs.get(image.id),
                classification=classifications.get(image.id),
                category_slugs=category_slugs,
                embedding_image_ids=embedding_image_ids,
            )
            for image in images
        ],
    )


def read_processing_thumbnail(
    session: Session,
    *,
    batch_id: UUID,
    image_id: UUID,
    storage: WorkerStorage,
) -> bytes:
    batch = session.scalar(
        select(UploadBatch.id).where(
            UploadBatch.id == batch_id,
            UploadBatch.organization_id == DEFAULT_ORGANIZATION_ID,
        )
    )
    if batch is None:
        raise ProcessingThumbnailNotFoundError

    thumbnail_object_key = session.scalar(
        select(ImageAsset.thumbnail_object_key).where(
            ImageAsset.id == image_id,
            ImageAsset.batch_id == batch_id,
            ImageAsset.organization_id == DEFAULT_ORGANIZATION_ID,
        )
    )
    if thumbnail_object_key is None:
        raise ProcessingThumbnailNotFoundError

    try:
        return storage.read_object_bytes(object_key=thumbnail_object_key)
    except WorkerObjectNotFoundError as error:
        raise ProcessingThumbnailNotFoundError from error
    except WorkerObjectReadError as error:
        raise ProcessingThumbnailReadError from error


def run_processing_batch(
    *,
    session_factory: sessionmaker[Session],
    storage: WorkerStorage,
    embedding_provider: ImageEmbeddingProvider,
    category_provider: CategorySuggestionProvider,
    batch_id: UUID,
    pipeline_version: str,
) -> None:
    process_payloads = _load_process_payloads(
        session_factory=session_factory,
        batch_id=batch_id,
        pipeline_version=pipeline_version,
    )
    processing_queue = InMemoryProcessingQueue()
    for payload in process_payloads:
        with session_factory() as session:
            try:
                process_image_task(
                    session,
                    payload=payload,
                    storage=storage,
                    embedding_provider=embedding_provider,
                    queue=processing_queue,
                )
            except (ProcessingJobExecutionError, ProcessingJobNotFoundError):
                continue

    with session_factory() as session:
        ensured_group_payload = ensure_group_batch_job_for_terminal_process_jobs(
            session,
            batch_id=batch_id,
            pipeline_version=pipeline_version,
        )

    queued_group_payloads = list(processing_queue.group_batch_tasks)
    if ensured_group_payload is not None:
        queued_group_payloads.append(ensured_group_payload)
    group_payloads = _merged_group_payloads(
        queued_payloads=queued_group_payloads,
        session_factory=session_factory,
        batch_id=batch_id,
        pipeline_version=pipeline_version,
    )
    for payload in group_payloads:
        with session_factory() as session:
            try:
                group_batch_task(session, payload=payload)
            except Exception:
                session.rollback()
                logger.exception("Local grouping task failed.")
                try:
                    with session_factory() as failure_session:
                        record_group_batch_terminal_failure(
                            failure_session,
                            payload=payload,
                        )
                except Exception:
                    logger.exception(
                        "Unable to record terminal local grouping failure."
                    )
                    raise
                return

    with session_factory() as session:
        ensure_classify_jobs_for_processed_images(
            session,
            batch_id=batch_id,
            pipeline_version=pipeline_version,
        )

    classify_payloads = _load_classify_payloads(
        session_factory=session_factory,
        batch_id=batch_id,
        pipeline_version=pipeline_version,
    )
    for payload in classify_payloads:
        with session_factory() as session:
            try:
                classify_image_task(
                    session,
                    payload=payload,
                    storage=storage,
                    category_provider=category_provider,
                )
            except (ProcessingJobExecutionError, ProcessingJobNotFoundError):
                continue


def _load_process_payloads(
    *,
    session_factory: sessionmaker[Session],
    batch_id: UUID,
    pipeline_version: str,
) -> list[ProcessImageTaskPayload]:
    with session_factory() as session:
        jobs = session.scalars(
            select(ProcessingJob)
            .join(
                ImageAsset,
                (ImageAsset.id == ProcessingJob.image_id)
                & (ImageAsset.organization_id == ProcessingJob.organization_id),
            )
            .where(
                ProcessingJob.batch_id == batch_id,
                ProcessingJob.organization_id == DEFAULT_ORGANIZATION_ID,
                ProcessingJob.job_type == PROCESS_IMAGE_JOB_TYPE,
                ProcessingJob.pipeline_version == pipeline_version,
                ProcessingJob.status.in_(("pending", "failed", "started")),
                ImageAsset.status != "failed",
            )
            .order_by(ImageAsset.upload_order)
        ).all()
        return [
            ProcessImageTaskPayload(
                batch_id=job.batch_id,
                image_id=job.image_id,
                pipeline_version=job.pipeline_version,
            )
            for job in jobs
            if job.image_id is not None
        ]


def _load_classify_payloads(
    *,
    session_factory: sessionmaker[Session],
    batch_id: UUID,
    pipeline_version: str,
) -> list[ClassifyImageTaskPayload]:
    with session_factory() as session:
        jobs = session.scalars(
            select(ProcessingJob)
            .join(
                ImageAsset,
                (ImageAsset.id == ProcessingJob.image_id)
                & (ImageAsset.organization_id == ProcessingJob.organization_id),
            )
            .where(
                ProcessingJob.batch_id == batch_id,
                ProcessingJob.organization_id == DEFAULT_ORGANIZATION_ID,
                ProcessingJob.job_type == CLASSIFY_IMAGE_JOB_TYPE,
                ProcessingJob.pipeline_version == pipeline_version,
                ProcessingJob.status.in_(("pending", "failed", "started")),
                ImageAsset.status == "processed",
            )
            .order_by(ImageAsset.upload_order)
        ).all()
        return [
            ClassifyImageTaskPayload(
                batch_id=job.batch_id,
                image_id=job.image_id,
                pipeline_version=job.pipeline_version,
            )
            for job in jobs
            if job.image_id is not None
        ]


def _merged_group_payloads(
    *,
    queued_payloads: list[GroupBatchTaskPayload],
    session_factory: sessionmaker[Session],
    batch_id: UUID,
    pipeline_version: str,
) -> list[GroupBatchTaskPayload]:
    payloads_by_key = {
        (payload.batch_id, payload.pipeline_version): payload
        for payload in queued_payloads
    }
    for payload in _load_group_payloads(
        session_factory=session_factory,
        batch_id=batch_id,
        pipeline_version=pipeline_version,
    ):
        payloads_by_key.setdefault((payload.batch_id, payload.pipeline_version), payload)
    return list(payloads_by_key.values())


def _load_group_payloads(
    *,
    session_factory: sessionmaker[Session],
    batch_id: UUID,
    pipeline_version: str,
) -> list[GroupBatchTaskPayload]:
    with session_factory() as session:
        jobs = session.scalars(
            select(ProcessingJob).where(
                ProcessingJob.batch_id == batch_id,
                ProcessingJob.organization_id == DEFAULT_ORGANIZATION_ID,
                ProcessingJob.job_type == GROUP_BATCH_JOB_TYPE,
                ProcessingJob.pipeline_version == pipeline_version,
                ProcessingJob.idempotency_key
                == group_batch_idempotency_key(
                    batch_id=batch_id,
                    pipeline_version=pipeline_version,
                ),
                ProcessingJob.status.in_(("pending", "failed", "started")),
            )
        ).all()
        return [
            GroupBatchTaskPayload(
                batch_id=job.batch_id,
                pipeline_version=job.pipeline_version,
            )
            for job in jobs
        ]


def _jobs_by_image_id(
    session: Session,
    *,
    image_ids: list[UUID],
    pipeline_version: str,
    job_type: str,
) -> dict[UUID, ProcessingJob]:
    if not image_ids:
        return {}
    jobs = session.scalars(
        select(ProcessingJob).where(
            ProcessingJob.organization_id == DEFAULT_ORGANIZATION_ID,
            ProcessingJob.image_id.in_(image_ids),
            ProcessingJob.pipeline_version == pipeline_version,
            ProcessingJob.job_type == job_type,
        )
    ).all()
    return {job.image_id: job for job in jobs if job.image_id is not None}


def _embedding_image_ids(
    session: Session,
    *,
    image_ids: list[UUID],
    pipeline_version: str,
) -> set[UUID]:
    if not image_ids:
        return set()
    return set(
        session.scalars(
            select(ImageEmbedding.image_id).where(
                ImageEmbedding.organization_id == DEFAULT_ORGANIZATION_ID,
                ImageEmbedding.image_id.in_(image_ids),
                ImageEmbedding.pipeline_version == pipeline_version,
            )
        ).all()
    )


def _classifications_by_image_id(
    session: Session,
    *,
    image_ids: list[UUID],
    pipeline_version: str,
) -> dict[UUID, ImageClassification]:
    if not image_ids:
        return {}
    classifications = session.scalars(
        select(ImageClassification).where(
            ImageClassification.organization_id == DEFAULT_ORGANIZATION_ID,
            ImageClassification.image_id.in_(image_ids),
            ImageClassification.pipeline_version == pipeline_version,
        )
    ).all()
    return {classification.image_id: classification for classification in classifications}


def _category_slugs_by_id(
    session: Session,
    *,
    category_ids: list[UUID],
) -> dict[UUID, str]:
    if not category_ids:
        return {}
    categories = session.scalars(
        select(Category).where(
            Category.id.in_(category_ids),
            Category.organization_id.is_(None),
        )
    ).all()
    return {category.id: category.slug for category in categories}


def _processing_image_state(
    *,
    image: ImageAsset,
    process_job: ProcessingJob | None,
    classify_job: ProcessingJob | None,
    classification: ImageClassification | None,
    category_slugs: dict[UUID, str],
    embedding_image_ids: set[UUID],
) -> ProcessingImageState:
    return ProcessingImageState(
        image_id=image.id,
        upload_order=image.upload_order,
        original_filename=image.original_filename,
        image_status=image.status,
        process_job_status=process_job.status if process_job is not None else None,
        process_error=process_job.error_message if process_job is not None else None,
        classify_job_status=classify_job.status if classify_job is not None else None,
        classify_error=classify_job.error_message if classify_job is not None else None,
        category_slug=_category_slug(
            classification=classification,
            category_slugs=category_slugs,
        ),
        confidence=classification.confidence if classification is not None else None,
        has_hashes=image.phash is not None and image.dhash is not None,
        has_embedding=image.id in embedding_image_ids,
    )


def _category_slug(
    *,
    classification: ImageClassification | None,
    category_slugs: dict[UUID, str],
) -> str | None:
    if classification is None:
        return None
    if classification.category_id is None:
        return "unknown"
    return category_slugs.get(classification.category_id)


@lru_cache
def get_processing_runner() -> ProcessingRunner:
    return LocalProcessingRunner(
        session_factory=get_session_factory(),
        storage_factory=get_worker_storage,
        embedding_provider_factory=get_image_embedding_provider,
        category_provider_factory=get_category_suggestion_provider,
    )
