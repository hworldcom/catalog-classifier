from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from PIL import Image
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from catalog_api.embedding_vectors import EMBEDDING_DIMENSIONS
from catalog_api.image_embedding_providers import ImageEmbeddingProviderError
from catalog_api.image_processing import JPEG_CONTENT_TYPE
from catalog_api.main import app
from catalog_api.models import (
    ImageAsset,
    ImageClassification,
    ImageEmbedding,
    ProcessingJob,
    UploadBatch,
)
from catalog_api.processing_orchestration import (
    ProcessingRunner,
    get_processing_runner,
    run_processing_batch,
)
from catalog_api.processing_jobs import (
    PROCESS_IMAGE_JOB_TYPE,
    process_image_idempotency_key,
)
from catalog_api.processing_storage import (
    WorkerObjectNotFoundError,
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
        self.reads: list[str] = []
        self.writes: list[tuple[str, str, bytes]] = []

    def read_object_bytes(self, *, object_key: str) -> bytes:
        self.reads.append(object_key)
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
        self.writes.append((object_key, content_type, data))
        self.objects[object_key] = StoredObject(data=data, content_type=content_type)


class FakeImageEmbeddingProvider:
    provider = "fake-provider"
    model = "fake-image-embedding"
    dimensions = EMBEDDING_DIMENSIONS

    def __init__(self) -> None:
        self.fail = False
        self.embedding = [
            index / EMBEDDING_DIMENSIONS
            for index in range(EMBEDDING_DIMENSIONS)
        ]

    def embed_image(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
    ) -> list[float]:
        if self.fail:
            raise ImageEmbeddingProviderError("fake embedding failure")
        return self.embedding


class FakeCategorySuggestionProvider:
    provider = "fake-provider"
    model = "fake-category-suggestion"

    def suggest_category(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
        category_slugs: list[str],
    ) -> dict[str, object]:
        return {"categorySlug": "t-shirts", "confidence": 0.91}


class RecordingProcessingRunner:
    def __init__(self) -> None:
        self.starts: list[tuple[UUID, str]] = []

    def start(self, *, batch_id: UUID, pipeline_version: str) -> None:
        self.starts.append((batch_id, pipeline_version))


class InlineProcessingRunner:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        storage: FakeWorkerStorage,
        embedding_provider: FakeImageEmbeddingProvider,
        category_provider: FakeCategorySuggestionProvider,
    ) -> None:
        self.session_factory = session_factory
        self.storage = storage
        self.embedding_provider = embedding_provider
        self.category_provider = category_provider

    def start(self, *, batch_id: UUID, pipeline_version: str) -> None:
        run_processing_batch(
            session_factory=self.session_factory,
            storage=self.storage,
            embedding_provider=self.embedding_provider,
            category_provider=self.category_provider,
            batch_id=batch_id,
            pipeline_version=pipeline_version,
        )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def fake_worker_storage() -> FakeWorkerStorage:
    return FakeWorkerStorage()


@pytest.fixture
def fake_image_embedding_provider() -> FakeImageEmbeddingProvider:
    return FakeImageEmbeddingProvider()


@pytest.fixture
def fake_category_suggestion_provider() -> FakeCategorySuggestionProvider:
    return FakeCategorySuggestionProvider()


@contextmanager
def _override_processing_runner(runner: ProcessingRunner) -> Iterator[None]:
    app.dependency_overrides[get_processing_runner] = lambda: runner
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_processing_runner, None)


def _jpeg_bytes() -> bytes:
    image = Image.new("RGB", (640, 320), color=(20, 120, 200))
    output = BytesIO()
    image.save(output, format="JPEG")
    return output.getvalue()


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
                mime_type=JPEG_CONTENT_TYPE,
                size_bytes=100 + upload_order,
                status="uploaded",
            )
        )
        image_ids.append(image_id)
        object_keys.append(object_key)

    session.commit()
    return batch.id, image_ids, object_keys


def _create_processed_image_missing_classify_job(
    session: Session,
) -> tuple[UUID, UUID, str]:
    batch = UploadBatch(
        organization_id=DEFAULT_ORGANIZATION_ID,
        status="processing",
        original_file_count=1,
        processed_file_count=1,
        finalized_at=datetime.now(UTC),
        pipeline_version=PIPELINE_VERSION,
    )
    session.add(batch)
    session.flush()

    image_id = uuid4()
    inference_key = (
        f"organizations/{DEFAULT_ORGANIZATION_ID}/batches/{batch.id}/"
        f"derived/{PIPELINE_VERSION}/{image_id}/inference.jpg"
    )
    session.add(
        ImageAsset(
            id=image_id,
            organization_id=DEFAULT_ORGANIZATION_ID,
            batch_id=batch.id,
            original_object_key=(
                f"organizations/{DEFAULT_ORGANIZATION_ID}/batches/{batch.id}/"
                f"originals/{image_id}.jpg"
            ),
            normalized_object_key=(
                f"organizations/{DEFAULT_ORGANIZATION_ID}/batches/{batch.id}/"
                f"derived/{PIPELINE_VERSION}/{image_id}/normalized.jpg"
            ),
            inference_object_key=inference_key,
            thumbnail_object_key=(
                f"organizations/{DEFAULT_ORGANIZATION_ID}/batches/{batch.id}/"
                f"derived/{PIPELINE_VERSION}/{image_id}/thumbnail.jpg"
            ),
            original_filename="already-processed.jpg",
            upload_order=0,
            mime_type=JPEG_CONTENT_TYPE,
            size_bytes=100,
            width=640,
            height=320,
            normalized_format=JPEG_CONTENT_TYPE,
            normalized_size_bytes=100,
            sha256="0" * 64,
            phash="0" * 16,
            dhash="1" * 16,
            status="processed",
        )
    )
    session.add(
        ImageEmbedding(
            organization_id=DEFAULT_ORGANIZATION_ID,
            image_id=image_id,
            provider="fake-provider",
            model="fake-image-embedding",
            dimensions=EMBEDDING_DIMENSIONS,
            pipeline_version=PIPELINE_VERSION,
            embedding=[0.0 for _ in range(EMBEDDING_DIMENSIONS)],
        )
    )
    session.add(
        ProcessingJob(
            organization_id=DEFAULT_ORGANIZATION_ID,
            batch_id=batch.id,
            image_id=image_id,
            job_type=PROCESS_IMAGE_JOB_TYPE,
            status="completed",
            pipeline_version=PIPELINE_VERSION,
            idempotency_key=process_image_idempotency_key(
                image_id=image_id,
                pipeline_version=PIPELINE_VERSION,
            ),
            completed_at=datetime.now(UTC),
        )
    )
    session.commit()
    return batch.id, image_id, inference_key


async def test_processing_snapshot_is_read_only_for_queued_batch(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        batch_id, image_ids, _ = _create_batch_with_uploaded_images(
            session,
            image_count=2,
        )

    response = await database_client.get(f"/v1/upload-batches/{batch_id}/processing")

    assert response.status_code == 200
    body = response.json()
    assert body["batchId"] == str(batch_id)
    assert body["status"] == "queued"
    assert body["pipelineVersion"] == PIPELINE_VERSION
    assert [image["imageId"] for image in body["images"]] == [
        str(image_id) for image_id in image_ids
    ]
    assert all(image["processJobStatus"] is None for image in body["images"])
    assert all(image["classifyJobStatus"] is None for image in body["images"])

    with Session(migrated_engine) as session:
        job_count = session.scalar(select(func.count()).select_from(ProcessingJob))
    assert job_count == 0


async def test_start_processing_returns_promptly_after_claiming_jobs(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        batch_id, image_ids, _ = _create_batch_with_uploaded_images(
            session,
            image_count=2,
        )
    runner = RecordingProcessingRunner()

    with _override_processing_runner(runner):
        response = await database_client.post(
            f"/v1/upload-batches/{batch_id}/start-processing"
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "processing"
    assert runner.starts == [(batch_id, PIPELINE_VERSION)]
    assert [image["processJobStatus"] for image in body["images"]] == [
        "pending",
        "pending",
    ]
    assert [image["classifyJobStatus"] for image in body["images"]] == [None, None]

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
    assert {job.image_id for job in jobs} == set(image_ids)
    assert all(job.job_type == "process-image" for job in jobs)
    assert all(job.status == "pending" for job in jobs)


async def test_start_processing_runs_local_pipeline_and_is_idempotent(
    database_client: AsyncClient,
    migrated_engine: Engine,
    fake_worker_storage: FakeWorkerStorage,
    fake_image_embedding_provider: FakeImageEmbeddingProvider,
    fake_category_suggestion_provider: FakeCategorySuggestionProvider,
) -> None:
    original_bytes = _jpeg_bytes()
    with Session(migrated_engine) as session:
        batch_id, image_ids, object_keys = _create_batch_with_uploaded_images(
            session,
            image_count=2,
        )
    for object_key in object_keys:
        fake_worker_storage.objects[object_key] = StoredObject(
            data=original_bytes,
            content_type=JPEG_CONTENT_TYPE,
        )
    runner = InlineProcessingRunner(
        session_factory=sessionmaker(bind=migrated_engine),
        storage=fake_worker_storage,
        embedding_provider=fake_image_embedding_provider,
        category_provider=fake_category_suggestion_provider,
    )

    with _override_processing_runner(runner):
        response = await database_client.post(
            f"/v1/upload-batches/{batch_id}/start-processing"
        )
        redelivery_response = await database_client.post(
            f"/v1/upload-batches/{batch_id}/start-processing"
        )

    assert response.status_code == 200
    assert redelivery_response.status_code == 200
    body = redelivery_response.json()
    assert body["status"] == "processing"
    assert body["processedFileCount"] == 2
    assert [
        (
            image["imageStatus"],
            image["processJobStatus"],
            image["classifyJobStatus"],
            image["categorySlug"],
            image["confidence"],
            image["hasHashes"],
            image["hasEmbedding"],
        )
        for image in body["images"]
    ] == [
        ("processed", "completed", "completed", "t-shirts", 0.91, True, True),
        ("processed", "completed", "completed", "t-shirts", 0.91, True, True),
    ]

    with Session(migrated_engine) as session:
        job_count = session.scalar(
            select(func.count())
            .select_from(ProcessingJob)
            .where(ProcessingJob.batch_id == batch_id)
        )
        embedding_count = session.scalar(
            select(func.count())
            .select_from(ImageEmbedding)
            .where(ImageEmbedding.image_id.in_(image_ids))
        )
        classification_count = session.scalar(
            select(func.count())
            .select_from(ImageClassification)
            .where(ImageClassification.image_id.in_(image_ids))
        )

    assert job_count == 4
    assert embedding_count == 2
    assert classification_count == 2


async def test_start_processing_creates_missing_classify_jobs_for_processed_images(
    database_client: AsyncClient,
    migrated_engine: Engine,
    fake_worker_storage: FakeWorkerStorage,
    fake_image_embedding_provider: FakeImageEmbeddingProvider,
    fake_category_suggestion_provider: FakeCategorySuggestionProvider,
) -> None:
    with Session(migrated_engine) as session:
        batch_id, image_id, inference_key = (
            _create_processed_image_missing_classify_job(session)
        )
    fake_worker_storage.objects[inference_key] = StoredObject(
        data=_jpeg_bytes(),
        content_type=JPEG_CONTENT_TYPE,
    )
    runner = InlineProcessingRunner(
        session_factory=sessionmaker(bind=migrated_engine),
        storage=fake_worker_storage,
        embedding_provider=fake_image_embedding_provider,
        category_provider=fake_category_suggestion_provider,
    )

    with _override_processing_runner(runner):
        response = await database_client.post(
            f"/v1/upload-batches/{batch_id}/start-processing"
        )
        redelivery_response = await database_client.post(
            f"/v1/upload-batches/{batch_id}/start-processing"
        )

    assert response.status_code == 200
    assert redelivery_response.status_code == 200
    body = redelivery_response.json()
    [image] = body["images"]
    assert image["imageId"] == str(image_id)
    assert image["imageStatus"] == "processed"
    assert image["processJobStatus"] == "completed"
    assert image["classifyJobStatus"] == "completed"
    assert image["categorySlug"] == "t-shirts"
    assert image["confidence"] == 0.91

    with Session(migrated_engine) as session:
        classify_job_count = session.scalar(
            select(func.count())
            .select_from(ProcessingJob)
            .where(
                ProcessingJob.batch_id == batch_id,
                ProcessingJob.job_type == "classify-image",
            )
        )
        classification_count = session.scalar(
            select(func.count())
            .select_from(ImageClassification)
            .where(ImageClassification.image_id == image_id)
        )

    assert classify_job_count == 1
    assert classification_count == 1


async def test_start_processing_surfaces_retryable_provider_failure(
    database_client: AsyncClient,
    migrated_engine: Engine,
    fake_worker_storage: FakeWorkerStorage,
    fake_image_embedding_provider: FakeImageEmbeddingProvider,
    fake_category_suggestion_provider: FakeCategorySuggestionProvider,
) -> None:
    with Session(migrated_engine) as session:
        batch_id, image_ids, object_keys = _create_batch_with_uploaded_images(
            session,
            image_count=1,
        )
    fake_worker_storage.objects[object_keys[0]] = StoredObject(
        data=_jpeg_bytes(),
        content_type=JPEG_CONTENT_TYPE,
    )
    fake_image_embedding_provider.fail = True
    runner = InlineProcessingRunner(
        session_factory=sessionmaker(bind=migrated_engine),
        storage=fake_worker_storage,
        embedding_provider=fake_image_embedding_provider,
        category_provider=fake_category_suggestion_provider,
    )

    with _override_processing_runner(runner):
        response = await database_client.post(
            f"/v1/upload-batches/{batch_id}/start-processing"
        )

    assert response.status_code == 200
    body = response.json()
    [image] = body["images"]
    assert image["imageId"] == str(image_ids[0])
    assert image["imageStatus"] == "uploaded"
    assert image["processJobStatus"] == "failed"
    assert "embedding_generation_failed" in image["processError"]
    assert image["classifyJobStatus"] is None
    assert image["hasEmbedding"] is False

    with Session(migrated_engine) as session:
        classification_count = session.scalar(
            select(func.count())
            .select_from(ImageClassification)
            .where(ImageClassification.image_id == image_ids[0])
        )
    assert classification_count == 0


@pytest.mark.parametrize("batch_status", ["created", "uploading"])
async def test_processing_endpoints_reject_non_finalized_batches(
    database_client: AsyncClient,
    migrated_engine: Engine,
    batch_status: str,
) -> None:
    with Session(migrated_engine) as session:
        batch_id, _, _ = _create_batch_with_uploaded_images(
            session,
            image_count=1,
            status=batch_status,
        )

    get_response = await database_client.get(
        f"/v1/upload-batches/{batch_id}/processing"
    )
    post_response = await database_client.post(
        f"/v1/upload-batches/{batch_id}/start-processing"
    )

    assert get_response.status_code == 409
    assert post_response.status_code == 409
