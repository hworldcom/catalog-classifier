from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from catalog_api.embedding_vectors import EMBEDDING_DIMENSIONS
from catalog_api.grouping import (
    GroupingSettings,
    group_batch_task,
    record_group_batch_terminal_failure,
)
from catalog_api.image_processing import JPEG_CONTENT_TYPE
from catalog_api.models import (
    Category,
    ImageAsset,
    ImageClassification,
    ImageEmbedding,
    PairAssessment,
    ProcessingJob,
    ProductGroup,
    ProductGroupImage,
    UploadBatch,
)
from catalog_api.processing_jobs import (
    CLASSIFY_IMAGE_JOB_TYPE,
    GROUP_BATCH_JOB_TYPE,
    PROCESS_IMAGE_JOB_TYPE,
    GroupBatchTaskPayload,
    classify_image_idempotency_key,
    group_batch_idempotency_key,
    process_image_idempotency_key,
)
from catalog_api.upload_batches import DEFAULT_ORGANIZATION_ID

pytestmark = [pytest.mark.anyio, pytest.mark.postgresql]

PIPELINE_VERSION = "2026-06-01"


@dataclass(frozen=True)
class GroupingImageSpec:
    upload_order: int
    embedding: tuple[float, float] | None = None
    category_slug: str | None = "t-shirts"
    phash: str | None = "0000000000000000"
    sha256: str | None = None
    status: str = "processed"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_group_batch_worker_creates_same_product_group(
    database_client: AsyncClient,
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(
        "CATALOG_GROUPING_SAME_PRODUCT_SIMILARITY_THRESHOLD",
        raising=False,
    )
    with Session(migrated_engine) as session:
        batch_id, image_ids = _create_grouping_batch(
            session,
            specs=[
                GroupingImageSpec(upload_order=0, embedding=(1.0, 0.0)),
                GroupingImageSpec(upload_order=1, embedding=(0.88, 0.475)),
            ],
        )

    response = await database_client.post(
        "/internal/tasks/group-batch",
        json={"batchId": str(batch_id), "pipelineVersion": PIPELINE_VERSION},
    )

    assert response.status_code == 200
    assert response.json() == {
        "batchId": str(batch_id),
        "pipelineVersion": PIPELINE_VERSION,
        "jobStatus": "completed",
        "didWork": True,
    }
    with Session(migrated_engine) as session:
        batch = session.get(UploadBatch, batch_id)
        category = session.scalar(select(Category).where(Category.slug == "t-shirts"))
        groups = session.scalars(
            select(ProductGroup).where(ProductGroup.batch_id == batch_id)
        ).all()
        memberships = session.scalars(
            select(ProductGroupImage)
            .where(ProductGroupImage.batch_id == batch_id)
            .order_by(ProductGroupImage.position)
        ).all()
        assessment = session.scalar(
            select(PairAssessment).where(PairAssessment.batch_id == batch_id)
        )

    assert batch is not None
    assert batch.status == "review_required"
    assert category is not None
    assert len(groups) == 1
    assert groups[0].suggested_category_id == category.id
    assert groups[0].approved_category_id == category.id
    assert groups[0].approved_category_source == "machine_suggestion"
    assert groups[0].status == "proposed"
    assert groups[0].cover_image_id == image_ids[0]
    assert [membership.image_id for membership in memberships] == image_ids
    assert all(membership.membership_source == "engine" for membership in memberships)
    assert all(membership.membership_confidence is not None for membership in memberships)
    assert assessment is not None
    assert assessment.decision == "same_product"
    assert assessment.decision_source == "heuristic"
    assert assessment.confidence is not None
    assert 0.85 <= assessment.confidence < 0.92


async def test_group_batch_strong_similarity_overrides_phash_conflict(
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        batch_id, image_ids = _create_grouping_batch(
            session,
            specs=[
                GroupingImageSpec(
                    upload_order=0,
                    embedding=(1.0, 0.0),
                    phash="0000000000000000",
                ),
                GroupingImageSpec(
                    upload_order=1,
                    embedding=(0.9254, 0.37899187326379447),
                    phash="00000000000fffff",
                ),
            ],
        )

    with Session(migrated_engine) as session:
        result = group_batch_task(
            session,
            payload=GroupBatchTaskPayload(
                batch_id=batch_id,
                pipeline_version=PIPELINE_VERSION,
            ),
            settings=_default_grouping_settings(),
        )

    assert result.did_work is True
    with Session(migrated_engine) as session:
        groups = session.scalars(
            select(ProductGroup).where(ProductGroup.batch_id == batch_id)
        ).all()
        memberships = session.scalars(
            select(ProductGroupImage)
            .where(ProductGroupImage.batch_id == batch_id)
            .order_by(ProductGroupImage.position)
        ).all()
        assessment = session.scalar(
            select(PairAssessment).where(PairAssessment.batch_id == batch_id)
        )

    assert len(groups) == 1
    assert [membership.image_id for membership in memberships] == image_ids
    assert assessment is not None
    assert assessment.decision == "same_product"
    assert assessment.confidence is not None
    assert assessment.confidence >= 0.92
    assert assessment.phash_distance is not None
    assert assessment.phash_distance > 8


async def test_group_batch_borderline_similarity_keeps_phash_conflict_uncertain(
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        batch_id, image_ids = _create_grouping_batch(
            session,
            specs=[
                GroupingImageSpec(
                    upload_order=0,
                    embedding=(1.0, 0.0),
                    phash="0000000000000000",
                ),
                GroupingImageSpec(
                    upload_order=1,
                    embedding=(0.8945, 0.4470679478558042),
                    phash="00000000000fffff",
                ),
            ],
        )

    with Session(migrated_engine) as session:
        result = group_batch_task(
            session,
            payload=GroupBatchTaskPayload(
                batch_id=batch_id,
                pipeline_version=PIPELINE_VERSION,
            ),
            settings=_default_grouping_settings(),
        )

    assert result.did_work is True
    with Session(migrated_engine) as session:
        group_sizes = session.scalars(
            select(func.count(ProductGroupImage.image_id))
            .where(ProductGroupImage.batch_id == batch_id)
            .group_by(ProductGroupImage.group_id)
            .order_by(func.count(ProductGroupImage.image_id))
        ).all()
        grouped_image_ids = set(
            session.scalars(
                select(ProductGroupImage.image_id).where(
                    ProductGroupImage.batch_id == batch_id
                )
            ).all()
        )
        assessment = session.scalar(
            select(PairAssessment).where(PairAssessment.batch_id == batch_id)
        )

    assert group_sizes == [1, 1]
    assert grouped_image_ids == set(image_ids)
    assert assessment is not None
    assert assessment.decision == "uncertain"
    assert assessment.confidence is not None
    assert 0.85 <= assessment.confidence < 0.92
    assert assessment.phash_distance is not None
    assert assessment.phash_distance > 8


async def test_group_batch_category_conflict_blocks_strong_similarity(
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        batch_id, image_ids = _create_grouping_batch(
            session,
            specs=[
                GroupingImageSpec(
                    upload_order=0,
                    embedding=(1.0, 0.0),
                    category_slug="t-shirts",
                    phash="0000000000000000",
                ),
                GroupingImageSpec(
                    upload_order=1,
                    embedding=(0.9254, 0.37899187326379447),
                    category_slug="trousers",
                    phash="0000000000000000",
                ),
            ],
        )

    with Session(migrated_engine) as session:
        result = group_batch_task(
            session,
            payload=GroupBatchTaskPayload(
                batch_id=batch_id,
                pipeline_version=PIPELINE_VERSION,
            ),
            settings=_default_grouping_settings(),
        )

    assert result.did_work is True
    with Session(migrated_engine) as session:
        group_sizes = session.scalars(
            select(func.count(ProductGroupImage.image_id))
            .where(ProductGroupImage.batch_id == batch_id)
            .group_by(ProductGroupImage.group_id)
            .order_by(func.count(ProductGroupImage.image_id))
        ).all()
        grouped_image_ids = set(
            session.scalars(
                select(ProductGroupImage.image_id).where(
                    ProductGroupImage.batch_id == batch_id
                )
            ).all()
        )
        assessment = session.scalar(
            select(PairAssessment).where(PairAssessment.batch_id == batch_id)
        )

    assert group_sizes == [1, 1]
    assert grouped_image_ids == set(image_ids)
    assert assessment is not None
    assert assessment.decision == "different_product"
    assert assessment.category_match is False
    assert assessment.confidence is not None
    assert assessment.confidence >= 0.92


async def test_group_batch_does_not_collapse_uncertain_chain(
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        batch_id, image_ids = _create_grouping_batch(
            session,
            specs=[
                GroupingImageSpec(upload_order=0, embedding=(1.0, 0.0)),
                GroupingImageSpec(upload_order=1, embedding=(0.99, 0.10)),
                GroupingImageSpec(upload_order=2, embedding=(0.90, 0.435)),
            ],
        )

    with Session(migrated_engine) as session:
        result = group_batch_task(
            session,
            payload=GroupBatchTaskPayload(
                batch_id=batch_id,
                pipeline_version=PIPELINE_VERSION,
            ),
            settings=_strict_grouping_settings(),
        )

    assert result.did_work is True
    with Session(migrated_engine) as session:
        group_sizes = session.scalars(
            select(func.count(ProductGroupImage.image_id))
            .where(ProductGroupImage.batch_id == batch_id)
            .group_by(ProductGroupImage.group_id)
            .order_by(func.count(ProductGroupImage.image_id))
        ).all()
        decisions = session.scalars(
            select(PairAssessment.decision)
            .where(PairAssessment.batch_id == batch_id)
            .order_by(PairAssessment.decision)
        ).all()
        grouped_image_ids = set(
            session.scalars(
                select(ProductGroupImage.image_id).where(
                    ProductGroupImage.batch_id == batch_id
                )
            ).all()
        )

    assert group_sizes == [1, 2]
    assert grouped_image_ids == set(image_ids)
    assert decisions.count("same_product") == 2
    assert decisions.count("uncertain") == 1


async def test_group_batch_is_idempotent_when_groups_exist(
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        batch_id, _ = _create_grouping_batch(
            session,
            specs=[
                GroupingImageSpec(upload_order=0, embedding=(1.0, 0.0)),
                GroupingImageSpec(upload_order=1, embedding=(0.99, 0.10)),
            ],
        )

    payload = GroupBatchTaskPayload(
        batch_id=batch_id,
        pipeline_version=PIPELINE_VERSION,
    )
    with Session(migrated_engine) as session:
        first_result = group_batch_task(
            session,
            payload=payload,
            settings=_strict_grouping_settings(),
        )
    with Session(migrated_engine) as session:
        second_result = group_batch_task(
            session,
            payload=payload,
            settings=_strict_grouping_settings(),
        )
        group_count = session.scalar(
            select(func.count()).select_from(ProductGroup).where(
                ProductGroup.batch_id == batch_id
            )
        )
        job = session.scalar(
            select(ProcessingJob).where(
                ProcessingJob.batch_id == batch_id,
                ProcessingJob.job_type == GROUP_BATCH_JOB_TYPE,
            )
        )

    assert first_result.did_work is True
    assert second_result.did_work is False
    assert group_count == 1
    assert job is not None
    assert job.status == "completed"
    assert job.attempt_count == 1


@pytest.mark.parametrize("terminal_status", ["review_required", "approved"])
async def test_group_batch_terminal_failure_does_not_regress_completed_grouping(
    migrated_engine: Engine,
    terminal_status: str,
) -> None:
    with Session(migrated_engine) as session:
        batch_id, _ = _create_grouping_batch(
            session,
            specs=[
                GroupingImageSpec(upload_order=0, embedding=(1.0, 0.0)),
                GroupingImageSpec(upload_order=1, embedding=(0.99, 0.10)),
            ],
        )

    payload = GroupBatchTaskPayload(
        batch_id=batch_id,
        pipeline_version=PIPELINE_VERSION,
    )
    with Session(migrated_engine) as session:
        group_batch_task(
            session,
            payload=payload,
            settings=_strict_grouping_settings(),
        )
    if terminal_status == "approved":
        with Session(migrated_engine) as session:
            batch = session.get(UploadBatch, batch_id)
            assert batch is not None
            batch.status = "approved"
            session.commit()

    with Session(migrated_engine) as session:
        did_record_failure = record_group_batch_terminal_failure(
            session,
            payload=payload,
        )
    with Session(migrated_engine) as session:
        batch = session.get(UploadBatch, batch_id)
        job = session.scalar(
            select(ProcessingJob).where(
                ProcessingJob.batch_id == batch_id,
                ProcessingJob.job_type == GROUP_BATCH_JOB_TYPE,
            )
        )

    assert did_record_failure is False
    assert batch is not None
    assert batch.status == terminal_status
    assert job is not None
    assert job.status == "completed"
    assert job.error_message is None


async def test_group_batch_terminal_failure_is_noop_for_missing_rows(
    migrated_engine: Engine,
) -> None:
    payload = GroupBatchTaskPayload(
        batch_id=uuid4(),
        pipeline_version=PIPELINE_VERSION,
    )
    with Session(migrated_engine) as session:
        batch_count_before = session.scalar(
            select(func.count()).select_from(UploadBatch)
        )
        job_count_before = session.scalar(
            select(func.count()).select_from(ProcessingJob)
        )
        did_record_failure = record_group_batch_terminal_failure(
            session,
            payload=payload,
        )
        batch_count_after = session.scalar(
            select(func.count()).select_from(UploadBatch)
        )
        job_count_after = session.scalar(
            select(func.count()).select_from(ProcessingJob)
        )

    assert did_record_failure is False
    assert batch_count_after == batch_count_before
    assert job_count_after == job_count_before


async def test_group_batch_moves_empty_eligible_batch_to_review(
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        batch_id, _ = _create_grouping_batch(
            session,
            specs=[
                GroupingImageSpec(
                    upload_order=0,
                    embedding=None,
                    category_slug=None,
                    status="failed",
                )
            ],
        )

    with Session(migrated_engine) as session:
        result = group_batch_task(
            session,
            payload=GroupBatchTaskPayload(
                batch_id=batch_id,
                pipeline_version=PIPELINE_VERSION,
            ),
            settings=_strict_grouping_settings(),
        )

    assert result.did_work is True
    with Session(migrated_engine) as session:
        batch = session.get(UploadBatch, batch_id)
        group_count = session.scalar(
            select(func.count()).select_from(ProductGroup).where(
                ProductGroup.batch_id == batch_id
            )
        )
        membership_count = session.scalar(
            select(func.count()).select_from(ProductGroupImage).where(
                ProductGroupImage.batch_id == batch_id
            )
        )

    assert batch is not None
    assert batch.status == "review_required"
    assert group_count == 0
    assert membership_count == 0


def _create_grouping_batch(
    session: Session,
    *,
    specs: list[GroupingImageSpec],
) -> tuple[UUID, list[UUID]]:
    batch = UploadBatch(
        organization_id=DEFAULT_ORGANIZATION_ID,
        status="processing",
        original_file_count=len(specs),
        processed_file_count=len(specs),
        finalized_at=datetime.now(UTC),
        pipeline_version=PIPELINE_VERSION,
    )
    session.add(batch)
    session.flush()

    category_ids_by_slug = _category_ids_by_slug(session)
    image_ids: list[UUID] = []
    for spec in specs:
        image_id = uuid4()
        image_ids.append(image_id)
        session.add(
            ImageAsset(
                id=image_id,
                organization_id=DEFAULT_ORGANIZATION_ID,
                batch_id=batch.id,
                original_object_key=(
                    f"organizations/{DEFAULT_ORGANIZATION_ID}/batches/{batch.id}/"
                    f"originals/{image_id}.jpg"
                ),
                thumbnail_object_key=(
                    f"organizations/{DEFAULT_ORGANIZATION_ID}/batches/{batch.id}/"
                    f"derived/{PIPELINE_VERSION}/{image_id}/thumbnail.jpg"
                ),
                original_filename=f"image-{spec.upload_order}.jpg",
                upload_order=spec.upload_order,
                mime_type=JPEG_CONTENT_TYPE,
                size_bytes=100 + spec.upload_order,
                width=100,
                height=100,
                sha256=spec.sha256,
                phash=spec.phash,
                dhash=spec.phash,
                status=spec.status,
            )
        )
        session.add(
            ProcessingJob(
                organization_id=DEFAULT_ORGANIZATION_ID,
                batch_id=batch.id,
                image_id=image_id,
                job_type=PROCESS_IMAGE_JOB_TYPE,
                status="completed" if spec.status == "processed" else "failed",
                pipeline_version=PIPELINE_VERSION,
                completed_at=datetime.now(UTC),
                idempotency_key=process_image_idempotency_key(
                    image_id=image_id,
                    pipeline_version=PIPELINE_VERSION,
                ),
            )
        )
        if spec.embedding is not None and spec.status == "processed":
            session.add(
                ImageEmbedding(
                    organization_id=DEFAULT_ORGANIZATION_ID,
                    image_id=image_id,
                    provider="qa-provider",
                    model="qa-model",
                    dimensions=EMBEDDING_DIMENSIONS,
                    pipeline_version=PIPELINE_VERSION,
                    embedding=_embedding(spec.embedding),
                )
            )
        if spec.category_slug is not None and spec.status == "processed":
            session.add(
                ProcessingJob(
                    organization_id=DEFAULT_ORGANIZATION_ID,
                    batch_id=batch.id,
                    image_id=image_id,
                    job_type=CLASSIFY_IMAGE_JOB_TYPE,
                    status="completed",
                    pipeline_version=PIPELINE_VERSION,
                    completed_at=datetime.now(UTC),
                    idempotency_key=classify_image_idempotency_key(
                        image_id=image_id,
                        pipeline_version=PIPELINE_VERSION,
                    ),
                )
            )
            session.add(
                ImageClassification(
                    organization_id=DEFAULT_ORGANIZATION_ID,
                    image_id=image_id,
                    category_id=category_ids_by_slug[spec.category_slug],
                    confidence=0.95,
                    attributes_json={
                        "categorySlug": spec.category_slug,
                        "confidence": 0.95,
                    },
                    provider="qa-provider",
                    model="qa-model",
                    raw_response_json={
                        "categorySlug": spec.category_slug,
                        "confidence": 0.95,
                    },
                    pipeline_version=PIPELINE_VERSION,
                )
            )

    session.add(
        ProcessingJob(
            organization_id=DEFAULT_ORGANIZATION_ID,
            batch_id=batch.id,
            image_id=None,
            job_type=GROUP_BATCH_JOB_TYPE,
            pipeline_version=PIPELINE_VERSION,
            idempotency_key=group_batch_idempotency_key(
                batch_id=batch.id,
                pipeline_version=PIPELINE_VERSION,
            ),
        )
    )
    session.commit()
    return batch.id, image_ids


def _category_ids_by_slug(session: Session) -> dict[str, UUID]:
    categories = session.scalars(select(Category)).all()
    return {category.slug: category.id for category in categories}


def _embedding(values: tuple[float, float]) -> list[float]:
    return [values[0], values[1], *([0.0] * (EMBEDDING_DIMENSIONS - 2))]


def _strict_grouping_settings() -> GroupingSettings:
    return GroupingSettings(
        max_candidates_per_image=50,
        phash_max_distance=8,
        uncertain_similarity_threshold=0.80,
        same_product_similarity_threshold=0.92,
        strong_same_product_similarity_threshold=0.92,
    )


def _default_grouping_settings() -> GroupingSettings:
    return GroupingSettings(
        max_candidates_per_image=50,
        phash_max_distance=8,
        uncertain_similarity_threshold=0.80,
        same_product_similarity_threshold=0.85,
        strong_same_product_similarity_threshold=0.92,
    )
