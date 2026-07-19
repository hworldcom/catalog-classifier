from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from catalog_api.group_category_reconciliation import (
    APPROVED_CATEGORY_SOURCE_CLEARED,
    APPROVED_CATEGORY_SOURCE_MACHINE,
    CATEGORY_SUGGESTION_PENDING,
    CATEGORY_SUGGESTION_READY,
    CATEGORY_SUGGESTION_UNAVAILABLE,
    evaluate_group_category_suggestion,
    reconcile_group_category,
)
from catalog_api.models import (
    Category,
    ImageAsset,
    ImageClassification,
    ProcessingJob,
    ProductGroup,
    ProductGroupImage,
    UploadBatch,
)
from catalog_api.processing_jobs import classify_image_idempotency_key
from catalog_api.upload_batches import DEFAULT_ORGANIZATION_ID

pytestmark = pytest.mark.postgresql

PIPELINE_VERSION = "2026-06-01"


@dataclass(frozen=True)
class GroupSeed:
    batch_id: UUID
    group_id: UUID
    image_ids: list[UUID]
    job_ids: list[UUID]


def test_pending_jobs_delay_machine_prefill_until_classifications_are_unanimous(
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        seed = _seed_group(session, image_count=2)
        category_id = _category_id(session, slug="t-shirts")

        suggestion = reconcile_group_category(session, group_id=seed.group_id)

        assert suggestion is not None
        assert suggestion.status == CATEGORY_SUGGESTION_PENDING
        group = session.get(ProductGroup, seed.group_id)
        assert group is not None
        assert group.status == "proposed"
        assert group.suggested_category_id is None
        assert group.approved_category_id is None
        assert group.approved_category_source is None

        for job_id, image_id in zip(seed.job_ids, seed.image_ids, strict=True):
            job = session.get(ProcessingJob, job_id)
            assert job is not None
            job.status = "completed"
            session.add(
                _classification(
                    image_id=image_id,
                    category_id=category_id,
                )
            )

        suggestion = reconcile_group_category(session, group_id=seed.group_id)

        assert suggestion is not None
        assert suggestion.status == CATEGORY_SUGGESTION_READY
        assert suggestion.category_id == category_id
        assert group.status == "proposed"
        assert group.suggested_category_id == category_id
        assert group.approved_category_id == category_id
        assert (
            group.approved_category_source
            == APPROVED_CATEGORY_SOURCE_MACHINE
        )


def test_reviewer_clear_is_not_overwritten_by_later_reconciliation(
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        seed = _seed_group(session, image_count=1, job_status="completed")
        t_shirts_id = _category_id(session, slug="t-shirts")
        trousers_id = _category_id(session, slug="trousers")
        classification = _classification(
            image_id=seed.image_ids[0],
            category_id=t_shirts_id,
        )
        session.add(classification)
        reconcile_group_category(session, group_id=seed.group_id)

        group = session.get(ProductGroup, seed.group_id)
        assert group is not None
        group.approved_category_id = None
        group.approved_category_source = APPROVED_CATEGORY_SOURCE_CLEARED
        classification.category_id = trousers_id

        suggestion = reconcile_group_category(session, group_id=seed.group_id)

        assert suggestion is not None
        assert suggestion.status == CATEGORY_SUGGESTION_READY
        assert suggestion.category_id == trousers_id
        assert group.suggested_category_id == trousers_id
        assert group.approved_category_id is None
        assert group.approved_category_source == APPROVED_CATEGORY_SOURCE_CLEARED


def test_duplicate_and_rejected_evidence_is_ignored(
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        seed = _seed_group(session, image_count=3, job_status="completed")
        t_shirts_id = _category_id(session, slug="t-shirts")
        trousers_id = _category_id(session, slug="trousers")
        session.add_all(
            [
                _classification(
                    image_id=seed.image_ids[0],
                    category_id=t_shirts_id,
                ),
                _classification(
                    image_id=seed.image_ids[1],
                    category_id=t_shirts_id,
                ),
                _classification(
                    image_id=seed.image_ids[2],
                    category_id=trousers_id,
                ),
            ]
        )
        excluded_membership = session.scalar(
            select(ProductGroupImage).where(
                ProductGroupImage.group_id == seed.group_id,
                ProductGroupImage.image_id == seed.image_ids[2],
            )
        )
        assert excluded_membership is not None
        excluded_membership.is_rejected = True

        suggestion = reconcile_group_category(session, group_id=seed.group_id)

        assert suggestion is not None
        assert suggestion.status == CATEGORY_SUGGESTION_READY
        assert suggestion.category_id == t_shirts_id

        excluded_membership.is_rejected = False
        excluded_membership.is_duplicate = True
        excluded_membership.duplicate_of_image_id = seed.image_ids[0]
        suggestion = reconcile_group_category(session, group_id=seed.group_id)
        assert suggestion is not None
        assert suggestion.status == CATEGORY_SUGGESTION_READY
        assert suggestion.category_id == t_shirts_id

        excluded_membership.is_duplicate = False
        excluded_membership.duplicate_of_image_id = None
        suggestion = reconcile_group_category(session, group_id=seed.group_id)

        group = session.get(ProductGroup, seed.group_id)
        assert suggestion is not None
        assert suggestion.status == CATEGORY_SUGGESTION_UNAVAILABLE
        assert group is not None
        assert group.suggested_category_id is None
        assert group.approved_category_id is None
        assert (
            group.approved_category_source
            == APPROVED_CATEGORY_SOURCE_MACHINE
        )


def test_missing_job_and_other_pipeline_classification_are_unavailable(
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        seed = _seed_group(session, image_count=1, create_jobs=False)
        session.add(
            _classification(
                image_id=seed.image_ids[0],
                category_id=_category_id(session, slug="t-shirts"),
                pipeline_version="older-pipeline",
            )
        )
        batch = session.get(UploadBatch, seed.batch_id)
        group = session.get(ProductGroup, seed.group_id)
        assert batch is not None
        assert group is not None

        suggestion = evaluate_group_category_suggestion(
            session,
            batch=batch,
            group=group,
        )

        assert suggestion.status == CATEGORY_SUGGESTION_UNAVAILABLE
        assert suggestion.category_id is None


def _seed_group(
    session: Session,
    *,
    image_count: int,
    job_status: str = "pending",
    create_jobs: bool = True,
) -> GroupSeed:
    batch = UploadBatch(
        id=uuid4(),
        organization_id=DEFAULT_ORGANIZATION_ID,
        status="review_required",
        original_file_count=image_count,
        processed_file_count=image_count,
        pipeline_version=PIPELINE_VERSION,
    )
    session.add(batch)
    session.flush()
    image_ids: list[UUID] = []
    for upload_order in range(image_count):
        image = ImageAsset(
            id=uuid4(),
            organization_id=DEFAULT_ORGANIZATION_ID,
            batch_id=batch.id,
            original_object_key=f"qa/0022e/{batch.id}/{upload_order}.jpg",
            original_filename=f"{upload_order}.jpg",
            upload_order=upload_order,
            mime_type="image/jpeg",
            size_bytes=100,
            status="processed",
        )
        session.add(image)
        image_ids.append(image.id)
    session.flush()

    group = ProductGroup(
        id=uuid4(),
        organization_id=DEFAULT_ORGANIZATION_ID,
        batch_id=batch.id,
        status="proposed",
        cover_image_id=image_ids[0],
    )
    session.add(group)
    session.flush()
    session.add_all(
        [
            ProductGroupImage(
                organization_id=DEFAULT_ORGANIZATION_ID,
                batch_id=batch.id,
                group_id=group.id,
                image_id=image_id,
                position=position,
                membership_source="engine",
                is_duplicate=False,
                is_rejected=False,
            )
            for position, image_id in enumerate(image_ids)
        ]
    )

    job_ids: list[UUID] = []
    if create_jobs:
        for image_id in image_ids:
            job = ProcessingJob(
                id=uuid4(),
                organization_id=DEFAULT_ORGANIZATION_ID,
                batch_id=batch.id,
                image_id=image_id,
                job_type="classify-image",
                status=job_status,
                pipeline_version=PIPELINE_VERSION,
                idempotency_key=classify_image_idempotency_key(
                    image_id=image_id,
                    pipeline_version=PIPELINE_VERSION,
                ),
            )
            session.add(job)
            job_ids.append(job.id)
    session.flush()
    return GroupSeed(
        batch_id=batch.id,
        group_id=group.id,
        image_ids=image_ids,
        job_ids=job_ids,
    )


def _category_id(session: Session, *, slug: str) -> UUID:
    return session.scalar(
        select(Category.id).where(Category.slug == slug)
    )  # type: ignore[return-value]


def _classification(
    *,
    image_id: UUID,
    category_id: UUID,
    pipeline_version: str = PIPELINE_VERSION,
) -> ImageClassification:
    return ImageClassification(
        organization_id=DEFAULT_ORGANIZATION_ID,
        image_id=image_id,
        category_id=category_id,
        confidence=0.95,
        attributes_json={
            "categorySlug": "qa-category",
            "confidence": 0.95,
        },
        provider="qa-provider",
        model="qa-model",
        raw_response_json={"categorySlug": "qa-category", "confidence": 0.95},
        pipeline_version=pipeline_version,
    )
