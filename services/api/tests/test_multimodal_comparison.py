from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from itertools import combinations
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from catalog_api.image_processing import JPEG_CONTENT_TYPE
from catalog_api.main import app
from catalog_api.models import (
    Category,
    ImageAsset,
    ImageClassification,
    PairAssessment,
    ProcessingJob,
    ProductGroup,
    ProductGroupImage,
    ReviewEvent,
    UploadBatch,
)
from catalog_api.multimodal_comparison import (
    ERROR_CLAIM_EXPIRED,
    MAX_COMPARISONS_ENV,
    MULTIMODAL_COMPARE_BATCH_JOB_TYPE,
    MultimodalComparisonConfigurationError,
    MultimodalComparisonSettings,
    validate_multimodal_comparison_settings,
)
from catalog_api.multimodal_comparison_providers import (
    MultimodalComparisonProviderError,
    MultimodalPairInput,
    get_multimodal_comparison_provider,
)
from catalog_api.processing_storage import (
    WorkerObjectNotFoundError,
    get_worker_storage,
)
from catalog_api.upload_batches import DEFAULT_ORGANIZATION_ID

pytestmark = [pytest.mark.anyio, pytest.mark.postgresql]

PIPELINE_VERSION = "2026-06-01"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeStorage:
    def __init__(self, *, missing_keys: set[str] | None = None) -> None:
        self.missing_keys = missing_keys or set()
        self.read_keys: list[str] = []

    def read_object_bytes(self, *, object_key: str) -> bytes:
        self.read_keys.append(object_key)
        if object_key in self.missing_keys:
            raise WorkerObjectNotFoundError
        return f"thumbnail:{object_key}".encode()

    def write_object_bytes(
        self,
        *,
        object_key: str,
        content_type: str,
        data: bytes,
    ) -> None:
        raise AssertionError("Multimodal comparison must not write storage objects.")


class FakeProvider:
    provider = "fake-provider"
    model = "fake-model"

    def __init__(
        self,
        responses: list[dict[str, object]] | None = None,
        *,
        error: Exception | None = None,
        on_call: Callable[[], None] | None = None,
    ) -> None:
        self.responses = responses or [
            {
                "decision": "same_product",
                "confidence": 0.95,
                "reason": "same product",
            }
        ]
        self.error = error
        self.on_call = on_call
        self.calls: list[MultimodalPairInput] = []

    def compare_pair(
        self,
        *,
        pair: MultimodalPairInput,
        image_a_bytes: bytes,
        image_b_bytes: bytes,
        mime_type: str,
        timeout_seconds: int,
    ) -> dict[str, object]:
        self.calls.append(pair)
        assert image_a_bytes
        assert image_b_bytes
        assert mime_type == JPEG_CONTENT_TYPE
        assert timeout_seconds == 30
        if self.on_call is not None:
            self.on_call()
        if self.error is not None:
            raise self.error
        response_index = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[response_index]


async def test_high_confidence_comparison_rebuilds_groups(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        batch_id, image_ids, _ = _create_review_batch(session)

    provider = FakeProvider()
    storage = FakeStorage()
    response = await _post_comparison(
        database_client,
        batch_id=batch_id,
        provider=provider,
        storage=storage,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "review_required"
    assert len(body["groups"]) == 1
    assert {image["imageId"] for image in body["groups"][0]["images"]} == {
        str(image_id) for image_id in image_ids
    }
    assert len(provider.calls) == 1
    assert provider.calls[0].image_a_id < provider.calls[0].image_b_id

    with Session(migrated_engine) as session:
        assessment = session.scalar(
            select(PairAssessment).where(PairAssessment.batch_id == batch_id)
        )
        job = _comparison_job(session, batch_id=batch_id)
    assert assessment is not None
    assert assessment.decision == "same_product"
    assert assessment.decision_source == "multimodal_model"
    assert assessment.confidence == pytest.approx(0.95)
    assert job is not None
    assert job.status == "completed"
    assert job.attempt_count == 1


async def test_low_confidence_same_product_is_persisted_as_uncertain(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        batch_id, _, _ = _create_review_batch(session)

    provider = FakeProvider(
        responses=[
            {
                "decision": "same_product",
                "confidence": 0.89,
                "reason": "probably the same product",
            }
        ]
    )
    response = await _post_comparison(
        database_client,
        batch_id=batch_id,
        provider=provider,
        storage=FakeStorage(),
    )

    assert response.status_code == 200
    assert len(response.json()["groups"]) == 2
    with Session(migrated_engine) as session:
        assessment = session.scalar(
            select(PairAssessment).where(PairAssessment.batch_id == batch_id)
        )
    assert assessment is not None
    assert assessment.decision == "uncertain"
    assert assessment.decision_source == "multimodal_model"
    assert assessment.confidence == pytest.approx(0.89)

    second_response = await _post_comparison(
        database_client,
        batch_id=batch_id,
        provider=provider,
        storage=FakeStorage(),
    )
    assert second_response.status_code == 200
    assert len(provider.calls) == 1


async def test_missing_thumbnail_skips_pair_without_failing(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        batch_id, _, thumbnail_keys = _create_review_batch(session)

    provider = FakeProvider()
    response = await _post_comparison(
        database_client,
        batch_id=batch_id,
        provider=provider,
        storage=FakeStorage(missing_keys={thumbnail_keys[0]}),
    )

    assert response.status_code == 200
    assert len(response.json()["groups"]) == 2
    assert provider.calls == []
    with Session(migrated_engine) as session:
        assessment = session.scalar(
            select(PairAssessment).where(PairAssessment.batch_id == batch_id)
        )
        job = _comparison_job(session, batch_id=batch_id)
    assert assessment is not None
    assert assessment.decision_source == "heuristic"
    assert job is not None
    assert job.status == "completed"


async def test_malformed_provider_output_fails_atomically(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        batch_id, _, _ = _create_review_batch(session)
        initial_group_ids = set(
            session.scalars(
                select(ProductGroup.id).where(ProductGroup.batch_id == batch_id)
            ).all()
        )

    provider = FakeProvider(
        responses=[
            {
                "decision": "same_product",
                "confidence": 2.0,
                "reason": "invalid confidence",
            }
        ]
    )
    response = await _post_comparison(
        database_client,
        batch_id=batch_id,
        provider=provider,
        storage=FakeStorage(),
    )

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == (
        "multimodal_comparison_provider_failed"
    )
    with Session(migrated_engine) as session:
        assessment = session.scalar(
            select(PairAssessment).where(PairAssessment.batch_id == batch_id)
        )
        group_ids = set(
            session.scalars(
                select(ProductGroup.id).where(ProductGroup.batch_id == batch_id)
            ).all()
        )
        job = _comparison_job(session, batch_id=batch_id)
    assert assessment is not None
    assert assessment.decision_source == "heuristic"
    assert group_ids == initial_group_ids
    assert job is not None
    assert job.status == "failed"


async def test_failed_provider_attempt_can_retry_same_claim(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        batch_id, _, _ = _create_review_batch(session)

    failed_response = await _post_comparison(
        database_client,
        batch_id=batch_id,
        provider=FakeProvider(
            error=MultimodalComparisonProviderError("provider failed")
        ),
        storage=FakeStorage(),
    )
    successful_response = await _post_comparison(
        database_client,
        batch_id=batch_id,
        provider=FakeProvider(),
        storage=FakeStorage(),
    )

    assert failed_response.status_code == 500
    assert successful_response.status_code == 200
    with Session(migrated_engine) as session:
        jobs = session.scalars(
            select(ProcessingJob).where(
                ProcessingJob.batch_id == batch_id,
                ProcessingJob.job_type == MULTIMODAL_COMPARE_BATCH_JOB_TYPE,
            )
        ).all()
        assessment = session.scalar(
            select(PairAssessment).where(PairAssessment.batch_id == batch_id)
        )
    assert len(jobs) == 1
    assert jobs[0].status == "completed"
    assert jobs[0].attempt_count == 2
    assert assessment is not None
    assert assessment.decision_source == "multimodal_model"


async def test_comparison_cap_limits_sequential_provider_calls(
    database_client: AsyncClient,
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(MAX_COMPARISONS_ENV, "1")
    with Session(migrated_engine) as session:
        batch_id, _, _ = _create_review_batch(session, image_count=3)

    provider = FakeProvider(
        responses=[
            {
                "decision": "uncertain",
                "confidence": 0.86,
                "reason": "insufficient evidence",
            }
        ]
    )
    response = await _post_comparison(
        database_client,
        batch_id=batch_id,
        provider=provider,
        storage=FakeStorage(),
    )

    assert response.status_code == 200
    assert len(provider.calls) == 1
    with Session(migrated_engine) as session:
        multimodal_count = session.scalar(
            select(func.count())
            .select_from(PairAssessment)
            .where(
                PairAssessment.batch_id == batch_id,
                PairAssessment.decision_source == "multimodal_model",
            )
        )
    assert multimodal_count == 1


async def test_active_claim_returns_conflict_without_provider_call(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        batch_id, _, _ = _create_review_batch(session)
        session.add(
            _active_comparison_job(
                batch_id=batch_id,
                started_at=datetime.now(UTC),
            )
        )
        session.commit()

    provider = FakeProvider()
    response = await _post_comparison(
        database_client,
        batch_id=batch_id,
        provider=provider,
        storage=FakeStorage(),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == (
        "multimodal_comparison_in_progress"
    )
    assert provider.calls == []


async def test_expired_claim_is_failed_and_new_claim_runs(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        batch_id, _, _ = _create_review_batch(session)
        expired_job = _active_comparison_job(
            batch_id=batch_id,
            started_at=datetime.now(UTC) - timedelta(seconds=901),
        )
        session.add(expired_job)
        session.commit()
        expired_job_id = expired_job.id

    response = await _post_comparison(
        database_client,
        batch_id=batch_id,
        provider=FakeProvider(),
        storage=FakeStorage(),
    )

    assert response.status_code == 200
    with Session(migrated_engine) as session:
        expired_job = session.get(ProcessingJob, expired_job_id)
        jobs = session.scalars(
            select(ProcessingJob).where(
                ProcessingJob.batch_id == batch_id,
                ProcessingJob.job_type == MULTIMODAL_COMPARE_BATCH_JOB_TYPE,
            )
        ).all()
    assert expired_job is not None
    assert expired_job.status == "failed"
    assert expired_job.error_message == ERROR_CLAIM_EXPIRED
    assert any(job.status == "completed" for job in jobs)


async def test_review_event_blocks_comparison(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        batch_id, _, _ = _create_review_batch(session)
        session.add(
            ReviewEvent(
                organization_id=DEFAULT_ORGANIZATION_ID,
                batch_id=batch_id,
                group_id=None,
                image_id=None,
                user_id=None,
                action_type="qa_edit",
                payload_json={},
            )
        )
        session.commit()

    provider = FakeProvider()
    response = await _post_comparison(
        database_client,
        batch_id=batch_id,
        provider=provider,
        storage=FakeStorage(),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == (
        "multimodal_comparison_not_allowed"
    )
    assert provider.calls == []


async def test_review_event_during_provider_call_discards_results(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        batch_id, _, _ = _create_review_batch(session)
        initial_group_ids = set(
            session.scalars(
                select(ProductGroup.id).where(ProductGroup.batch_id == batch_id)
            ).all()
        )

    provider = FakeProvider(
        on_call=lambda: _create_review_event(migrated_engine, batch_id=batch_id)
    )
    response = await _post_comparison(
        database_client,
        batch_id=batch_id,
        provider=provider,
        storage=FakeStorage(),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == (
        "multimodal_comparison_not_allowed"
    )
    with Session(migrated_engine) as session:
        assessment = session.scalar(
            select(PairAssessment).where(PairAssessment.batch_id == batch_id)
        )
        group_ids = set(
            session.scalars(
                select(ProductGroup.id).where(ProductGroup.batch_id == batch_id)
            ).all()
        )
        job = _comparison_job(session, batch_id=batch_id)
    assert assessment is not None
    assert assessment.decision_source == "heuristic"
    assert group_ids == initial_group_ids
    assert job is not None
    assert job.status == "failed"
    assert job.error_message == "multimodal_comparison_not_allowed"


async def test_late_success_cannot_write_after_attempt_token_changes(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        batch_id, _, _ = _create_review_batch(session)

    provider = FakeProvider(
        on_call=lambda: _take_over_claim(migrated_engine, batch_id=batch_id)
    )
    response = await _post_comparison(
        database_client,
        batch_id=batch_id,
        provider=provider,
        storage=FakeStorage(),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == (
        "multimodal_comparison_claim_lost"
    )
    with Session(migrated_engine) as session:
        assessment = session.scalar(
            select(PairAssessment).where(PairAssessment.batch_id == batch_id)
        )
        job = _comparison_job(session, batch_id=batch_id)
    assert assessment is not None
    assert assessment.decision_source == "heuristic"
    assert job is not None
    assert job.status == "started"
    assert job.attempt_count == 2


async def test_late_failure_cannot_fail_newer_attempt(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        batch_id, _, _ = _create_review_batch(session)

    provider = FakeProvider(
        error=MultimodalComparisonProviderError("provider failed"),
        on_call=lambda: _take_over_claim(migrated_engine, batch_id=batch_id),
    )
    response = await _post_comparison(
        database_client,
        batch_id=batch_id,
        provider=provider,
        storage=FakeStorage(),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == (
        "multimodal_comparison_claim_lost"
    )
    with Session(migrated_engine) as session:
        job = _comparison_job(session, batch_id=batch_id)
        assessment = session.scalar(
            select(PairAssessment).where(PairAssessment.batch_id == batch_id)
        )
    assert job is not None
    assert job.status == "started"
    assert job.attempt_count == 2
    assert assessment is not None
    assert assessment.decision_source == "heuristic"


def test_settings_reject_unsafe_sequential_budget() -> None:
    with pytest.raises(MultimodalComparisonConfigurationError):
        validate_multimodal_comparison_settings(
            _settings(claim_timeout_seconds=719)
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_comparisons": 0},
        {"provider_timeout_seconds": 0},
        {"claim_timeout_seconds": 0},
    ],
)
def test_settings_reject_non_positive_values(
    overrides: dict[str, int],
) -> None:
    with pytest.raises(MultimodalComparisonConfigurationError):
        validate_multimodal_comparison_settings(_settings(**overrides))


async def _post_comparison(
    client: AsyncClient,
    *,
    batch_id: UUID,
    provider: FakeProvider,
    storage: FakeStorage,
):
    app.dependency_overrides[get_multimodal_comparison_provider] = lambda: provider
    app.dependency_overrides[get_worker_storage] = lambda: storage
    try:
        return await client.post(
            f"/v1/upload-batches/{batch_id}/run-multimodal-comparison"
        )
    finally:
        app.dependency_overrides.pop(get_multimodal_comparison_provider, None)
        app.dependency_overrides.pop(get_worker_storage, None)


def _create_review_batch(
    session: Session,
    *,
    image_count: int = 2,
) -> tuple[UUID, list[UUID], list[str]]:
    category = session.scalar(select(Category).where(Category.slug == "t-shirts"))
    assert category is not None
    batch = UploadBatch(
        organization_id=DEFAULT_ORGANIZATION_ID,
        status="review_required",
        original_file_count=image_count,
        processed_file_count=image_count,
        finalized_at=datetime.now(UTC),
        pipeline_version=PIPELINE_VERSION,
    )
    session.add(batch)
    session.flush()

    image_ids = sorted(uuid4() for _ in range(image_count))
    thumbnail_keys: list[str] = []
    for upload_order, image_id in enumerate(image_ids):
        thumbnail_key = (
            f"organizations/{DEFAULT_ORGANIZATION_ID}/batches/{batch.id}/"
            f"derived/{PIPELINE_VERSION}/{image_id}/thumbnail.jpg"
        )
        thumbnail_keys.append(thumbnail_key)
        session.add(
            ImageAsset(
                id=image_id,
                organization_id=DEFAULT_ORGANIZATION_ID,
                batch_id=batch.id,
                original_object_key=f"qa/originals/{image_id}.jpg",
                thumbnail_object_key=thumbnail_key,
                original_filename=f"image-{upload_order}.jpg",
                upload_order=upload_order,
                mime_type=JPEG_CONTENT_TYPE,
                size_bytes=100 + upload_order,
                width=100,
                height=100,
                phash=f"{upload_order + 1:016x}",
                dhash=f"{upload_order + 1:016x}",
                status="processed",
            )
        )
        session.add(
            ImageClassification(
                organization_id=DEFAULT_ORGANIZATION_ID,
                image_id=image_id,
                category_id=category.id,
                confidence=0.95,
                attributes_json={"categorySlug": "t-shirts", "confidence": 0.95},
                provider="qa-provider",
                model="qa-model",
                raw_response_json={
                    "categorySlug": "t-shirts",
                    "confidence": 0.95,
                },
                pipeline_version=PIPELINE_VERSION,
            )
        )

    session.flush()
    for image_a_id, image_b_id in combinations(image_ids, 2):
        upload_order_a = image_ids.index(image_a_id)
        upload_order_b = image_ids.index(image_b_id)
        similarity = 0.91 - (0.01 * upload_order_b)
        session.add(
            PairAssessment(
                organization_id=DEFAULT_ORGANIZATION_ID,
                batch_id=batch.id,
                image_a_id=image_a_id,
                image_b_id=image_b_id,
                embedding_similarity=similarity,
                phash_distance=20,
                category_match=True,
                upload_order_distance=abs(upload_order_a - upload_order_b),
                decision="uncertain",
                confidence=similarity,
                decision_source="heuristic",
                pipeline_version=PIPELINE_VERSION,
            )
        )

    for image_id in image_ids:
        group = ProductGroup(
            organization_id=DEFAULT_ORGANIZATION_ID,
            batch_id=batch.id,
            status="proposed",
            suggested_category_id=category.id,
            cover_image_id=image_id,
            confidence=1.0,
        )
        session.add(group)
        session.flush()
        session.add(
            ProductGroupImage(
                organization_id=DEFAULT_ORGANIZATION_ID,
                batch_id=batch.id,
                group_id=group.id,
                image_id=image_id,
                position=0,
                membership_source="singleton",
                membership_confidence=None,
            )
        )
    session.commit()
    return batch.id, image_ids, thumbnail_keys


def _active_comparison_job(
    *,
    batch_id: UUID,
    started_at: datetime,
) -> ProcessingJob:
    return ProcessingJob(
        id=uuid4(),
        organization_id=DEFAULT_ORGANIZATION_ID,
        batch_id=batch_id,
        image_id=None,
        job_type=MULTIMODAL_COMPARE_BATCH_JOB_TYPE,
        status="started",
        attempt_count=1,
        pipeline_version=PIPELINE_VERSION,
        started_at=started_at,
        idempotency_key=f"{MULTIMODAL_COMPARE_BATCH_JOB_TYPE}:{uuid4()}",
    )


def _comparison_job(
    session: Session,
    *,
    batch_id: UUID,
) -> ProcessingJob | None:
    return session.scalar(
        select(ProcessingJob)
        .where(
            ProcessingJob.batch_id == batch_id,
            ProcessingJob.job_type == MULTIMODAL_COMPARE_BATCH_JOB_TYPE,
        )
        .order_by(ProcessingJob.created_at.desc())
    )


def _take_over_claim(engine: Engine, *, batch_id: UUID) -> None:
    with Session(engine) as session:
        job = session.scalar(
            select(ProcessingJob)
            .where(
                ProcessingJob.batch_id == batch_id,
                ProcessingJob.job_type == MULTIMODAL_COMPARE_BATCH_JOB_TYPE,
                ProcessingJob.status == "started",
            )
            .with_for_update()
        )
        assert job is not None
        job.attempt_count += 1
        session.commit()


def _create_review_event(engine: Engine, *, batch_id: UUID) -> None:
    with Session(engine) as session:
        session.add(
            ReviewEvent(
                organization_id=DEFAULT_ORGANIZATION_ID,
                batch_id=batch_id,
                group_id=None,
                image_id=None,
                user_id=None,
                action_type="qa_concurrent_edit",
                payload_json={},
            )
        )
        session.commit()


def _settings(
    **overrides: int,
) -> MultimodalComparisonSettings:
    values: dict[str, int | float] = {
        "max_comparisons": 20,
        "candidate_similarity_threshold": 0.85,
        "same_product_threshold": 0.90,
        "claim_timeout_seconds": 900,
        "provider_timeout_seconds": 30,
    }
    values.update(overrides)
    return MultimodalComparisonSettings(**values)
