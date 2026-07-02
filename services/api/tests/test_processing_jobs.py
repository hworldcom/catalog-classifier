from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from PIL import Image
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from catalog_api.embedding_vectors import EMBEDDING_DIMENSIONS
from catalog_api.image_embedding_providers import (
    ImageEmbeddingProviderError,
    get_image_embedding_provider,
)
from catalog_api.image_processing import JPEG_CONTENT_TYPE, derived_image_keys
from catalog_api.main import app
from catalog_api.models import ImageAsset, ImageEmbedding, ProcessingJob, UploadBatch
from catalog_api.processing_jobs import (
    InMemoryProcessingQueue,
    ProcessingBatchStateError,
    claim_batch_for_processing,
    process_image_idempotency_key,
)
from catalog_api.processing_storage import (
    WorkerObjectNotFoundError,
    WorkerObjectReadError,
    WorkerObjectWriteError,
    get_worker_storage,
)
from catalog_api.upload_batches import DEFAULT_ORGANIZATION_ID

pytestmark = [pytest.mark.anyio, pytest.mark.postgresql]

PIPELINE_VERSION = "2026-06-01"


@dataclass(frozen=True)
class StoredObject:
    data: bytes
    content_type: str


class FakeWorkerStorage:
    def __init__(self) -> None:
        self.objects: dict[str, StoredObject] = {}
        self.read_error_keys: set[str] = set()
        self.write_error_keys: set[str] = set()
        self.reads: list[str] = []
        self.writes: list[tuple[str, str, bytes]] = []

    def read_object_bytes(self, *, object_key: str) -> bytes:
        self.reads.append(object_key)
        if object_key in self.read_error_keys:
            raise WorkerObjectReadError
        try:
            return self.objects[object_key].data
        except KeyError as error:
            raise WorkerObjectNotFoundError from error

    def write_object_bytes(
        self,
        *,
        object_key: str,
        content_type: str,
        data: bytes,
    ) -> None:
        if object_key in self.write_error_keys:
            raise WorkerObjectWriteError
        self.writes.append((object_key, content_type, data))
        self.objects[object_key] = StoredObject(
            data=data,
            content_type=content_type,
        )


class FakeImageEmbeddingProvider:
    provider = "fake-provider"
    model = "fake-image-embedding"
    dimensions = EMBEDDING_DIMENSIONS

    def __init__(self) -> None:
        self.embedding = [
            index / EMBEDDING_DIMENSIONS
            for index in range(EMBEDDING_DIMENSIONS)
        ]
        self.fail = False
        self.calls: list[tuple[bytes, str]] = []

    def embed_image(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
    ) -> list[float]:
        self.calls.append((image_bytes, mime_type))
        if self.fail:
            raise ImageEmbeddingProviderError("fake provider failure")
        return self.embedding


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def fake_image_embedding_provider() -> Iterator[FakeImageEmbeddingProvider]:
    provider = FakeImageEmbeddingProvider()
    app.dependency_overrides[get_image_embedding_provider] = lambda: provider
    try:
        yield provider
    finally:
        app.dependency_overrides.pop(get_image_embedding_provider, None)


@pytest.fixture
def fake_worker_storage() -> Iterator[FakeWorkerStorage]:
    storage = FakeWorkerStorage()
    app.dependency_overrides[get_worker_storage] = lambda: storage
    try:
        yield storage
    finally:
        app.dependency_overrides.pop(get_worker_storage, None)


def _create_batch_with_uploaded_images(
    session: Session,
    *,
    image_count: int,
    status: str = "queued",
) -> tuple[UUID, list[UUID], list[str]]:
    batch = UploadBatch(
        organization_id=DEFAULT_ORGANIZATION_ID,
        status=status,
        original_file_count=image_count,
        processed_file_count=0,
        finalized_at=datetime.now(UTC) if status == "queued" else None,
    )
    session.add(batch)
    session.flush()

    image_ids: list[UUID] = []
    object_keys: list[str] = []
    for upload_order in range(image_count):
        image_id = uuid4()
        object_key = (
            f"organizations/{DEFAULT_ORGANIZATION_ID}/batches/{batch.id}/"
            f"originals/{image_id}.jpg"
        )
        session.add(
            ImageAsset(
                id=image_id,
                organization_id=DEFAULT_ORGANIZATION_ID,
                batch_id=batch.id,
                original_object_key=object_key,
                original_filename=f"image-{upload_order}.jpg",
                upload_order=upload_order,
                mime_type="image/jpeg",
                size_bytes=100 + upload_order,
                status="uploaded",
            )
        )
        image_ids.append(image_id)
        object_keys.append(object_key)

    session.commit()
    return batch.id, image_ids, object_keys


def _jpeg_bytes(
    *,
    size: tuple[int, int] = (1200, 600),
    exif_orientation: int | None = None,
) -> bytes:
    image = Image.new("RGB", size, color=(230, 80, 40))
    output = BytesIO()
    if exif_orientation is None:
        image.save(output, format="JPEG")
    else:
        exif = Image.Exif()
        exif[274] = exif_orientation
        image.save(output, format="JPEG", exif=exif)
    return output.getvalue()


def _png_bytes() -> bytes:
    image = Image.new("RGB", (32, 16), color=(20, 90, 210))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _stored_image_size(storage: FakeWorkerStorage, object_key: str) -> tuple[int, int]:
    with Image.open(BytesIO(storage.objects[object_key].data)) as image:
        return image.size


def _claim_single_image(
    migrated_engine: Engine,
    *,
    batch_id: UUID,
) -> tuple[UUID, UUID]:
    queue = InMemoryProcessingQueue()
    with Session(migrated_engine) as session:
        claim = claim_batch_for_processing(
            session,
            batch_id=batch_id,
            pipeline_version=PIPELINE_VERSION,
            queue=queue,
        )
    payload = claim.enqueued_tasks[0]
    return payload.batch_id, payload.image_id


async def test_claim_batch_creates_processing_jobs_and_queue_payloads(
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        batch_id, image_ids, _ = _create_batch_with_uploaded_images(
            session,
            image_count=2,
        )

    queue = InMemoryProcessingQueue()
    with Session(migrated_engine) as session:
        claim = claim_batch_for_processing(
            session,
            batch_id=batch_id,
            pipeline_version=PIPELINE_VERSION,
            queue=queue,
        )

    assert claim.batch_id == batch_id
    assert claim.status == "processing"
    assert claim.pipeline_version == PIPELINE_VERSION
    assert [job.image_id for job in claim.jobs] == image_ids
    assert [task.image_id for task in claim.enqueued_tasks] == image_ids
    assert queue.process_image_tasks == claim.enqueued_tasks
    assert [
        (task.batch_id, task.image_id, task.pipeline_version)
        for task in queue.process_image_tasks
    ] == [
        (batch_id, image_ids[0], PIPELINE_VERSION),
        (batch_id, image_ids[1], PIPELINE_VERSION),
    ]

    with Session(migrated_engine) as session:
        batch = session.get(UploadBatch, batch_id)
        jobs = session.scalars(
            select(ProcessingJob)
            .where(ProcessingJob.batch_id == batch_id)
            .order_by(ProcessingJob.image_id)
        ).all()

    assert batch is not None
    assert batch.status == "processing"
    assert batch.pipeline_version == PIPELINE_VERSION
    assert len(jobs) == 2
    assert {job.image_id for job in jobs} == set(image_ids)
    assert all(job.organization_id == DEFAULT_ORGANIZATION_ID for job in jobs)
    assert all(job.job_type == "process-image" for job in jobs)
    assert all(job.status == "pending" for job in jobs)
    assert all(job.attempt_count == 0 for job in jobs)
    assert {
        job.idempotency_key for job in jobs
    } == {
        process_image_idempotency_key(
            image_id=image_id,
            pipeline_version=PIPELINE_VERSION,
        )
        for image_id in image_ids
    }


async def test_claim_batch_is_idempotent_after_batch_is_processing(
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        batch_id, image_ids, _ = _create_batch_with_uploaded_images(
            session,
            image_count=2,
        )

    first_queue = InMemoryProcessingQueue()
    with Session(migrated_engine) as session:
        claim_batch_for_processing(
            session,
            batch_id=batch_id,
            pipeline_version=PIPELINE_VERSION,
            queue=first_queue,
        )

    second_queue = InMemoryProcessingQueue()
    with Session(migrated_engine) as session:
        second_claim = claim_batch_for_processing(
            session,
            batch_id=batch_id,
            pipeline_version=PIPELINE_VERSION,
            queue=second_queue,
        )

    assert [task.image_id for task in first_queue.process_image_tasks] == image_ids
    assert second_queue.process_image_tasks == []
    assert [job.image_id for job in second_claim.jobs] == image_ids

    with Session(migrated_engine) as session:
        jobs = session.scalars(
            select(ProcessingJob).where(ProcessingJob.batch_id == batch_id)
        ).all()

    assert len(jobs) == 2


async def test_claim_rejects_batches_that_are_not_ready(
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        batch_id, _, _ = _create_batch_with_uploaded_images(
            session,
            image_count=1,
            status="uploading",
        )

    queue = InMemoryProcessingQueue()
    with pytest.raises(ProcessingBatchStateError):
        with Session(migrated_engine) as session:
            claim_batch_for_processing(
                session,
                batch_id=batch_id,
                pipeline_version=PIPELINE_VERSION,
                queue=queue,
            )

    assert queue.process_image_tasks == []
    with Session(migrated_engine) as session:
        jobs = session.scalars(
            select(ProcessingJob).where(ProcessingJob.batch_id == batch_id)
        ).all()
    assert jobs == []


async def test_process_image_worker_creates_derivatives_and_is_idempotent(
    database_client: AsyncClient,
    migrated_engine: Engine,
    fake_worker_storage: FakeWorkerStorage,
    fake_image_embedding_provider: FakeImageEmbeddingProvider,
) -> None:
    original_bytes = _jpeg_bytes(size=(1200, 600))
    with Session(migrated_engine) as session:
        batch_id, image_ids, object_keys = _create_batch_with_uploaded_images(
            session,
            image_count=1,
        )
    image_id = image_ids[0]
    fake_worker_storage.objects[object_keys[0]] = StoredObject(
        data=original_bytes,
        content_type=JPEG_CONTENT_TYPE,
    )
    _, payload_image_id = _claim_single_image(migrated_engine, batch_id=batch_id)
    expected_keys = derived_image_keys(
        organization_id=DEFAULT_ORGANIZATION_ID,
        batch_id=batch_id,
        pipeline_version=PIPELINE_VERSION,
        image_id=image_id,
    )

    response = await database_client.post(
        "/internal/tasks/process-image",
        json={
            "batchId": str(batch_id),
            "imageId": str(payload_image_id),
            "pipelineVersion": PIPELINE_VERSION,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "batchId": str(batch_id),
        "imageId": str(image_id),
        "pipelineVersion": PIPELINE_VERSION,
        "jobStatus": "completed",
        "didWork": True,
    }
    assert fake_worker_storage.reads == [object_keys[0]]
    assert [write[0] for write in fake_worker_storage.writes] == [
        expected_keys.normalized_object_key,
        expected_keys.inference_object_key,
        expected_keys.thumbnail_object_key,
    ]
    assert all(
        write[1] == JPEG_CONTENT_TYPE for write in fake_worker_storage.writes
    )
    assert len(fake_image_embedding_provider.calls) == 1
    assert fake_image_embedding_provider.calls[0][1] == JPEG_CONTENT_TYPE
    with Image.open(BytesIO(fake_image_embedding_provider.calls[0][0])) as image:
        assert image.size == (1024, 512)
    assert (
        _stored_image_size(fake_worker_storage, expected_keys.normalized_object_key)
        == (1200, 600)
    )
    assert (
        _stored_image_size(fake_worker_storage, expected_keys.inference_object_key)
        == (1024, 512)
    )
    assert (
        _stored_image_size(fake_worker_storage, expected_keys.thumbnail_object_key)
        == (480, 240)
    )

    with Session(migrated_engine) as session:
        batch = session.get(UploadBatch, batch_id)
        job = session.scalar(
            select(ProcessingJob).where(ProcessingJob.image_id == image_id)
        )
        image = session.get(ImageAsset, image_id)
        embedding = session.scalar(
            select(ImageEmbedding).where(ImageEmbedding.image_id == image_id)
        )

    assert batch is not None
    assert batch.processed_file_count == 1
    assert job is not None
    assert job.status == "completed"
    assert job.attempt_count == 1
    assert job.started_at is not None
    assert job.completed_at is not None
    assert job.error_message is None
    assert image is not None
    assert image.status == "processed"
    assert image.sha256 == sha256(original_bytes).hexdigest()
    assert image.phash is not None
    assert image.dhash is not None
    assert len(image.phash) == 16
    assert len(image.dhash) == 16
    assert image.phash == image.phash.lower()
    assert image.dhash == image.dhash.lower()
    assert image.width == 1200
    assert image.height == 600
    assert image.normalized_format == JPEG_CONTENT_TYPE
    assert image.normalized_size_bytes == len(
        fake_worker_storage.objects[expected_keys.normalized_object_key].data
    )
    assert image.normalized_object_key == expected_keys.normalized_object_key
    assert image.inference_object_key == expected_keys.inference_object_key
    assert image.thumbnail_object_key == expected_keys.thumbnail_object_key
    assert embedding is not None
    assert embedding.organization_id == DEFAULT_ORGANIZATION_ID
    assert embedding.image_id == image_id
    assert embedding.provider == fake_image_embedding_provider.provider
    assert embedding.model == fake_image_embedding_provider.model
    assert embedding.dimensions == EMBEDDING_DIMENSIONS
    assert embedding.pipeline_version == PIPELINE_VERSION
    assert list(embedding.embedding) == pytest.approx(
        fake_image_embedding_provider.embedding
    )

    write_count = len(fake_worker_storage.writes)
    embedding_call_count = len(fake_image_embedding_provider.calls)
    redelivery_response = await database_client.post(
        "/internal/tasks/process-image",
        json={
            "batchId": str(batch_id),
            "imageId": str(image_id),
            "pipelineVersion": PIPELINE_VERSION,
        },
    )

    assert redelivery_response.status_code == 200
    assert redelivery_response.json()["didWork"] is False
    assert len(fake_worker_storage.writes) == write_count
    assert len(fake_image_embedding_provider.calls) == embedding_call_count
    with Session(migrated_engine) as session:
        redelivered_batch = session.get(UploadBatch, batch_id)
        redelivered_job = session.scalar(
            select(ProcessingJob).where(ProcessingJob.image_id == image_id)
        )
        embedding_count = session.scalar(
            select(func.count())
            .select_from(ImageEmbedding)
            .where(ImageEmbedding.image_id == image_id)
        )
    assert redelivered_batch is not None
    assert redelivered_batch.processed_file_count == 1
    assert redelivered_job is not None
    assert redelivered_job.attempt_count == 1
    assert embedding_count == 1


async def test_process_image_applies_exif_orientation_to_derivatives_only(
    database_client: AsyncClient,
    migrated_engine: Engine,
    fake_worker_storage: FakeWorkerStorage,
) -> None:
    original_bytes = _jpeg_bytes(size=(80, 40), exif_orientation=6)
    with Session(migrated_engine) as session:
        batch_id, image_ids, object_keys = _create_batch_with_uploaded_images(
            session,
            image_count=1,
        )
    image_id = image_ids[0]
    fake_worker_storage.objects[object_keys[0]] = StoredObject(
        data=original_bytes,
        content_type=JPEG_CONTENT_TYPE,
    )
    _claim_single_image(migrated_engine, batch_id=batch_id)

    response = await database_client.post(
        "/internal/tasks/process-image",
        json={
            "batchId": str(batch_id),
            "imageId": str(image_id),
            "pipelineVersion": PIPELINE_VERSION,
        },
    )

    assert response.status_code == 200
    keys = derived_image_keys(
        organization_id=DEFAULT_ORGANIZATION_ID,
        batch_id=batch_id,
        pipeline_version=PIPELINE_VERSION,
        image_id=image_id,
    )
    assert (
        _stored_image_size(fake_worker_storage, keys.normalized_object_key)
        == (40, 80)
    )
    assert fake_worker_storage.objects[object_keys[0]].data == original_bytes

    with Session(migrated_engine) as session:
        image = session.get(ImageAsset, image_id)
    assert image is not None
    assert image.width == 40
    assert image.height == 80
    assert image.sha256 == sha256(original_bytes).hexdigest()


@pytest.mark.parametrize(
    ("source_bytes", "expected_error_code"),
    [
        (b"not an image", "image_decode_failed"),
        (_png_bytes(), "unsupported_image_mode"),
    ],
)
async def test_terminal_image_failures_do_not_retry_or_duplicate_counters(
    database_client: AsyncClient,
    migrated_engine: Engine,
    fake_worker_storage: FakeWorkerStorage,
    source_bytes: bytes,
    expected_error_code: str,
) -> None:
    with Session(migrated_engine) as session:
        batch_id, image_ids, object_keys = _create_batch_with_uploaded_images(
            session,
            image_count=1,
        )
    image_id = image_ids[0]
    fake_worker_storage.objects[object_keys[0]] = StoredObject(
        data=source_bytes,
        content_type=JPEG_CONTENT_TYPE,
    )
    _claim_single_image(migrated_engine, batch_id=batch_id)

    response = await database_client.post(
        "/internal/tasks/process-image",
        json={
            "batchId": str(batch_id),
            "imageId": str(image_id),
            "pipelineVersion": PIPELINE_VERSION,
        },
    )

    assert response.status_code == 200
    assert response.json()["jobStatus"] == "failed"
    assert response.json()["didWork"] is True
    assert fake_worker_storage.writes == []
    with Session(migrated_engine) as session:
        batch = session.get(UploadBatch, batch_id)
        job = session.scalar(
            select(ProcessingJob).where(ProcessingJob.image_id == image_id)
        )
        image = session.get(ImageAsset, image_id)
    assert batch is not None
    assert batch.processed_file_count == 1
    assert job is not None
    assert job.status == "failed"
    assert job.attempt_count == 1
    assert expected_error_code in (job.error_message or "")
    assert image is not None
    assert image.status == "failed"
    assert image.error_code == expected_error_code
    assert image.error_message is not None

    redelivery_response = await database_client.post(
        "/internal/tasks/process-image",
        json={
            "batchId": str(batch_id),
            "imageId": str(image_id),
            "pipelineVersion": PIPELINE_VERSION,
        },
    )

    assert redelivery_response.status_code == 200
    assert redelivery_response.json()["didWork"] is False
    with Session(migrated_engine) as session:
        redelivered_batch = session.get(UploadBatch, batch_id)
        redelivered_job = session.scalar(
            select(ProcessingJob).where(ProcessingJob.image_id == image_id)
        )
    assert redelivered_batch is not None
    assert redelivered_batch.processed_file_count == 1
    assert redelivered_job is not None
    assert redelivered_job.attempt_count == 1


async def test_missing_source_object_is_terminal(
    database_client: AsyncClient,
    migrated_engine: Engine,
    fake_worker_storage: FakeWorkerStorage,
) -> None:
    with Session(migrated_engine) as session:
        batch_id, image_ids, _ = _create_batch_with_uploaded_images(
            session,
            image_count=1,
        )
    image_id = image_ids[0]
    _claim_single_image(migrated_engine, batch_id=batch_id)

    response = await database_client.post(
        "/internal/tasks/process-image",
        json={
            "batchId": str(batch_id),
            "imageId": str(image_id),
            "pipelineVersion": PIPELINE_VERSION,
        },
    )

    assert response.status_code == 200
    assert response.json()["jobStatus"] == "failed"
    with Session(migrated_engine) as session:
        batch = session.get(UploadBatch, batch_id)
        image = session.get(ImageAsset, image_id)
    assert batch is not None
    assert batch.processed_file_count == 1
    assert image is not None
    assert image.status == "failed"
    assert image.error_code == "source_object_missing"


async def test_retryable_read_failure_returns_500_without_terminal_counter(
    database_client: AsyncClient,
    migrated_engine: Engine,
    fake_worker_storage: FakeWorkerStorage,
) -> None:
    original_bytes = _jpeg_bytes()
    with Session(migrated_engine) as session:
        batch_id, image_ids, object_keys = _create_batch_with_uploaded_images(
            session,
            image_count=1,
        )
    image_id = image_ids[0]
    object_key = object_keys[0]
    fake_worker_storage.objects[object_key] = StoredObject(
        data=original_bytes,
        content_type=JPEG_CONTENT_TYPE,
    )
    fake_worker_storage.read_error_keys.add(object_key)
    _claim_single_image(migrated_engine, batch_id=batch_id)

    response = await database_client.post(
        "/internal/tasks/process-image",
        json={
            "batchId": str(batch_id),
            "imageId": str(image_id),
            "pipelineVersion": PIPELINE_VERSION,
        },
    )

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "source_object_read_failed"
    with Session(migrated_engine) as session:
        batch = session.get(UploadBatch, batch_id)
        job = session.scalar(
            select(ProcessingJob).where(ProcessingJob.image_id == image_id)
        )
        image = session.get(ImageAsset, image_id)
    assert batch is not None
    assert batch.processed_file_count == 0
    assert job is not None
    assert job.status == "failed"
    assert job.attempt_count == 1
    assert image is not None
    assert image.status == "uploaded"
    assert image.error_code is None

    fake_worker_storage.read_error_keys.clear()
    retry_response = await database_client.post(
        "/internal/tasks/process-image",
        json={
            "batchId": str(batch_id),
            "imageId": str(image_id),
            "pipelineVersion": PIPELINE_VERSION,
        },
    )

    assert retry_response.status_code == 200
    with Session(migrated_engine) as session:
        retried_batch = session.get(UploadBatch, batch_id)
        retried_job = session.scalar(
            select(ProcessingJob).where(ProcessingJob.image_id == image_id)
        )
    assert retried_batch is not None
    assert retried_batch.processed_file_count == 1
    assert retried_job is not None
    assert retried_job.status == "completed"
    assert retried_job.attempt_count == 2


async def test_retryable_write_failure_returns_500_and_can_reuse_keys(
    database_client: AsyncClient,
    migrated_engine: Engine,
    fake_worker_storage: FakeWorkerStorage,
) -> None:
    original_bytes = _jpeg_bytes()
    with Session(migrated_engine) as session:
        batch_id, image_ids, object_keys = _create_batch_with_uploaded_images(
            session,
            image_count=1,
        )
    image_id = image_ids[0]
    fake_worker_storage.objects[object_keys[0]] = StoredObject(
        data=original_bytes,
        content_type=JPEG_CONTENT_TYPE,
    )
    keys = derived_image_keys(
        organization_id=DEFAULT_ORGANIZATION_ID,
        batch_id=batch_id,
        pipeline_version=PIPELINE_VERSION,
        image_id=image_id,
    )
    fake_worker_storage.write_error_keys.add(keys.inference_object_key)
    _claim_single_image(migrated_engine, batch_id=batch_id)

    response = await database_client.post(
        "/internal/tasks/process-image",
        json={
            "batchId": str(batch_id),
            "imageId": str(image_id),
            "pipelineVersion": PIPELINE_VERSION,
        },
    )

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "derived_object_write_failed"
    with Session(migrated_engine) as session:
        batch = session.get(UploadBatch, batch_id)
        job = session.scalar(
            select(ProcessingJob).where(ProcessingJob.image_id == image_id)
        )
        image = session.get(ImageAsset, image_id)
    assert batch is not None
    assert batch.processed_file_count == 0
    assert job is not None
    assert job.status == "failed"
    assert job.attempt_count == 1
    assert image is not None
    assert image.status == "uploaded"
    assert image.normalized_object_key is None

    fake_worker_storage.write_error_keys.clear()
    retry_response = await database_client.post(
        "/internal/tasks/process-image",
        json={
            "batchId": str(batch_id),
            "imageId": str(image_id),
            "pipelineVersion": PIPELINE_VERSION,
        },
    )

    assert retry_response.status_code == 200
    with Session(migrated_engine) as session:
        retried_batch = session.get(UploadBatch, batch_id)
        retried_image = session.get(ImageAsset, image_id)
        retried_job = session.scalar(
            select(ProcessingJob).where(ProcessingJob.image_id == image_id)
        )
    assert retried_batch is not None
    assert retried_batch.processed_file_count == 1
    assert retried_image is not None
    assert retried_image.normalized_object_key == keys.normalized_object_key
    assert retried_job is not None
    assert retried_job.status == "completed"
    assert retried_job.attempt_count == 2


@pytest.mark.parametrize(
    ("provider_embedding", "provider_fails"),
    [
        (None, True),
        ([0.1, 0.2], False),
    ],
)
async def test_embedding_failures_are_retryable_without_partial_rows(
    database_client: AsyncClient,
    migrated_engine: Engine,
    fake_worker_storage: FakeWorkerStorage,
    fake_image_embedding_provider: FakeImageEmbeddingProvider,
    provider_embedding: list[float] | None,
    provider_fails: bool,
) -> None:
    original_bytes = _jpeg_bytes()
    with Session(migrated_engine) as session:
        batch_id, image_ids, object_keys = _create_batch_with_uploaded_images(
            session,
            image_count=1,
        )
    image_id = image_ids[0]
    fake_worker_storage.objects[object_keys[0]] = StoredObject(
        data=original_bytes,
        content_type=JPEG_CONTENT_TYPE,
    )
    fake_image_embedding_provider.fail = provider_fails
    if provider_embedding is not None:
        fake_image_embedding_provider.embedding = provider_embedding
    _claim_single_image(migrated_engine, batch_id=batch_id)

    response = await database_client.post(
        "/internal/tasks/process-image",
        json={
            "batchId": str(batch_id),
            "imageId": str(image_id),
            "pipelineVersion": PIPELINE_VERSION,
        },
    )

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "embedding_generation_failed"
    assert fake_worker_storage.writes == []
    with Session(migrated_engine) as session:
        batch = session.get(UploadBatch, batch_id)
        job = session.scalar(
            select(ProcessingJob).where(ProcessingJob.image_id == image_id)
        )
        image = session.get(ImageAsset, image_id)
        embeddings = session.scalars(
            select(ImageEmbedding).where(ImageEmbedding.image_id == image_id)
        ).all()

    assert batch is not None
    assert batch.processed_file_count == 0
    assert job is not None
    assert job.status == "failed"
    assert job.attempt_count == 1
    assert "embedding_generation_failed" in (job.error_message or "")
    assert image is not None
    assert image.status == "uploaded"
    assert image.phash is None
    assert image.dhash is None
    assert image.normalized_object_key is None
    assert embeddings == []

    fake_image_embedding_provider.fail = False
    fake_image_embedding_provider.embedding = [
        index / EMBEDDING_DIMENSIONS
        for index in range(EMBEDDING_DIMENSIONS)
    ]
    retry_response = await database_client.post(
        "/internal/tasks/process-image",
        json={
            "batchId": str(batch_id),
            "imageId": str(image_id),
            "pipelineVersion": PIPELINE_VERSION,
        },
    )

    assert retry_response.status_code == 200
    with Session(migrated_engine) as session:
        retried_batch = session.get(UploadBatch, batch_id)
        retried_job = session.scalar(
            select(ProcessingJob).where(ProcessingJob.image_id == image_id)
        )
        retried_image = session.get(ImageAsset, image_id)
        embedding_count = session.scalar(
            select(func.count())
            .select_from(ImageEmbedding)
            .where(ImageEmbedding.image_id == image_id)
        )

    assert retried_batch is not None
    assert retried_batch.processed_file_count == 1
    assert retried_job is not None
    assert retried_job.status == "completed"
    assert retried_job.attempt_count == 2
    assert retried_image is not None
    assert retried_image.status == "processed"
    assert retried_image.phash is not None
    assert retried_image.dhash is not None
    assert embedding_count == 1
