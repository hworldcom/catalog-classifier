from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, aliased

from catalog_api.grouping import (
    DECISION_SAME_PRODUCT,
    DECISION_SOURCE_HEURISTIC,
    DECISION_SOURCE_MULTIMODAL_MODEL,
    DECISION_UNCERTAIN,
    grouping_settings_from_env,
    rebuild_product_groups_from_assessments,
)
from catalog_api.models import (
    Category,
    ImageAsset,
    ImageClassification,
    PairAssessment,
    ProcessingJob,
    ProductGroup,
    ReviewEvent,
    UploadBatch,
)
from catalog_api.multimodal_comparison_providers import (
    MultimodalComparisonProvider,
    MultimodalComparisonProviderError,
    MultimodalPairInput,
    generate_multimodal_comparison,
)
from catalog_api.processing_storage import (
    WorkerObjectNotFoundError,
    WorkerObjectReadError,
    WorkerStorage,
)
from catalog_api.review_groups import (
    ReviewBatchGroupsState,
    get_review_batch_groups,
)
from catalog_api.upload_batches import DEFAULT_ORGANIZATION_ID

MULTIMODAL_COMPARE_BATCH_JOB_TYPE = "multimodal-compare-batch"
MAX_COMPARISONS_ENV = "CATALOG_GROUPING_MAX_MULTIMODAL_COMPARISONS_PER_BATCH"
SAME_PRODUCT_THRESHOLD_ENV = (
    "CATALOG_GROUPING_MULTIMODAL_SAME_PRODUCT_THRESHOLD"
)
CLAIM_TIMEOUT_ENV = "CATALOG_MULTIMODAL_CLAIM_TIMEOUT_SECONDS"
PROVIDER_TIMEOUT_ENV = "CATALOG_MULTIMODAL_PROVIDER_TIMEOUT_SECONDS"
DEFAULT_MAX_COMPARISONS = 20
DEFAULT_SAME_PRODUCT_THRESHOLD = 0.90
DEFAULT_CLAIM_TIMEOUT_SECONDS = 900
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 30
RUN_SAFETY_MARGIN_SECONDS = 120

ERROR_CLAIM_EXPIRED = "multimodal_comparison_claim_expired"
ERROR_NOT_ALLOWED = "multimodal_comparison_not_allowed"
ERROR_PROVIDER_FAILED = "multimodal_comparison_provider_failed"
ERROR_STORAGE_FAILED = "multimodal_comparison_storage_failed"
ERROR_DATABASE_FAILED = "multimodal_comparison_database_failed"


class MultimodalComparisonConfigurationError(ValueError):
    """Raised when comparison settings cannot safely execute."""


class MultimodalComparisonBatchNotFoundError(Exception):
    """Raised when a comparison batch does not exist."""


class MultimodalComparisonNotAllowedError(Exception):
    """Raised when batch or review state prevents comparison."""


class MultimodalComparisonInProgressError(Exception):
    """Raised when another request owns the batch comparison claim."""


class MultimodalComparisonClaimLostError(Exception):
    """Raised when a newer attempt owns the durable comparison claim."""


class MultimodalComparisonExecutionError(Exception):
    def __init__(self, *, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


@dataclass(frozen=True)
class MultimodalComparisonSettings:
    max_comparisons: int
    candidate_similarity_threshold: float
    same_product_threshold: float
    claim_timeout_seconds: int
    provider_timeout_seconds: int


@dataclass(frozen=True)
class _PairCandidate:
    assessment_id: UUID
    image_a_id: UUID
    image_b_id: UUID
    image_a_filename: str
    image_b_filename: str
    image_a_thumbnail_key: str
    image_b_thumbnail_key: str
    embedding_similarity: float
    phash_distance: int | None
    category_match: bool | None
    suggested_category_a: str | None
    suggested_category_b: str | None


@dataclass(frozen=True)
class _ComparisonClaim:
    job_id: UUID
    attempt_token: int
    batch_id: UUID
    pipeline_version: str
    candidates: tuple[_PairCandidate, ...]


@dataclass(frozen=True)
class _PairOutcome:
    assessment_id: UUID
    decision: str
    confidence: float


def multimodal_comparison_settings_from_env() -> MultimodalComparisonSettings:
    grouping_settings = grouping_settings_from_env()
    settings = MultimodalComparisonSettings(
        max_comparisons=_read_int_setting(
            MAX_COMPARISONS_ENV,
            DEFAULT_MAX_COMPARISONS,
        ),
        candidate_similarity_threshold=(
            grouping_settings.same_product_similarity_threshold
        ),
        same_product_threshold=_read_float_setting(
            SAME_PRODUCT_THRESHOLD_ENV,
            DEFAULT_SAME_PRODUCT_THRESHOLD,
        ),
        claim_timeout_seconds=_read_int_setting(
            CLAIM_TIMEOUT_ENV,
            DEFAULT_CLAIM_TIMEOUT_SECONDS,
        ),
        provider_timeout_seconds=_read_int_setting(
            PROVIDER_TIMEOUT_ENV,
            DEFAULT_PROVIDER_TIMEOUT_SECONDS,
        ),
    )
    validate_multimodal_comparison_settings(settings)
    return settings


def validate_multimodal_comparison_configuration() -> None:
    multimodal_comparison_settings_from_env()


def validate_multimodal_comparison_settings(
    settings: MultimodalComparisonSettings,
) -> None:
    positive_settings = {
        MAX_COMPARISONS_ENV: settings.max_comparisons,
        CLAIM_TIMEOUT_ENV: settings.claim_timeout_seconds,
        PROVIDER_TIMEOUT_ENV: settings.provider_timeout_seconds,
    }
    for setting_name, value in positive_settings.items():
        if value <= 0:
            raise MultimodalComparisonConfigurationError(
                f"{setting_name} must be greater than zero."
            )
    for setting_name, value in (
        (
            "CATALOG_GROUPING_SAME_PRODUCT_SIMILARITY_THRESHOLD",
            settings.candidate_similarity_threshold,
        ),
        (SAME_PRODUCT_THRESHOLD_ENV, settings.same_product_threshold),
    ):
        if value < 0 or value > 1:
            raise MultimodalComparisonConfigurationError(
                f"{setting_name} must be between zero and one."
            )

    required_claim_seconds = (
        settings.max_comparisons * settings.provider_timeout_seconds
        + RUN_SAFETY_MARGIN_SECONDS
    )
    if settings.claim_timeout_seconds < required_claim_seconds:
        raise MultimodalComparisonConfigurationError(
            f"{CLAIM_TIMEOUT_ENV} must be at least {required_claim_seconds} "
            "seconds for the configured sequential comparison budget."
        )


def run_multimodal_comparison(
    session: Session,
    *,
    batch_id: UUID,
    storage: WorkerStorage,
    provider: MultimodalComparisonProvider,
    settings: MultimodalComparisonSettings | None = None,
) -> ReviewBatchGroupsState:
    resolved_settings = settings or multimodal_comparison_settings_from_env()
    validate_multimodal_comparison_settings(resolved_settings)
    claim = _claim_comparison_run(
        session,
        batch_id=batch_id,
        settings=resolved_settings,
    )
    if claim is None:
        return get_review_batch_groups(session, batch_id=batch_id)

    try:
        outcomes = _compare_claimed_pairs(
            claim=claim,
            storage=storage,
            provider=provider,
            settings=resolved_settings,
        )
    except WorkerObjectReadError as error:
        _fail_claim_if_owned(
            session,
            claim=claim,
            error_code=ERROR_STORAGE_FAILED,
        )
        raise MultimodalComparisonExecutionError(
            error_code=ERROR_STORAGE_FAILED,
            message="Unable to read comparison thumbnails.",
        ) from error
    except MultimodalComparisonProviderError as error:
        _fail_claim_if_owned(
            session,
            claim=claim,
            error_code=ERROR_PROVIDER_FAILED,
        )
        raise MultimodalComparisonExecutionError(
            error_code=ERROR_PROVIDER_FAILED,
            message="The multimodal comparison provider failed.",
        ) from error

    try:
        _persist_comparison_results(
            session,
            claim=claim,
            outcomes=outcomes,
            settings=resolved_settings,
        )
    except SQLAlchemyError as error:
        session.rollback()
        _fail_claim_if_owned(
            session,
            claim=claim,
            error_code=ERROR_DATABASE_FAILED,
        )
        raise MultimodalComparisonExecutionError(
            error_code=ERROR_DATABASE_FAILED,
            message="Unable to persist multimodal comparison results.",
        ) from error

    return get_review_batch_groups(session, batch_id=batch_id)


def comparison_idempotency_key(
    *,
    batch_id: UUID,
    pipeline_version: str,
    candidates: tuple[_PairCandidate, ...],
) -> str:
    canonical_pairs = sorted(
        f"{candidate.image_a_id}:{candidate.image_b_id}"
        for candidate in candidates
    )
    fingerprint = hashlib.sha256("|".join(canonical_pairs).encode()).hexdigest()
    return (
        f"{MULTIMODAL_COMPARE_BATCH_JOB_TYPE}:{batch_id}:"
        f"{pipeline_version}:{fingerprint}"
    )


def _claim_comparison_run(
    session: Session,
    *,
    batch_id: UUID,
    settings: MultimodalComparisonSettings,
) -> _ComparisonClaim | None:
    now = _utc_now()
    batch = session.scalar(
        select(UploadBatch)
        .where(
            UploadBatch.id == batch_id,
            UploadBatch.organization_id == DEFAULT_ORGANIZATION_ID,
        )
        .with_for_update()
    )
    if batch is None:
        session.rollback()
        raise MultimodalComparisonBatchNotFoundError
    _require_initial_batch_state(session, batch=batch)
    pipeline_version = batch.pipeline_version
    if pipeline_version is None or not pipeline_version.strip():
        session.rollback()
        raise MultimodalComparisonNotAllowedError(
            "Review batch has no active pipeline version."
        )

    candidates = tuple(
        _eligible_pair_candidates(
            session,
            batch=batch,
            pipeline_version=pipeline_version,
            settings=settings,
        )
    )
    if not candidates:
        session.rollback()
        return None

    active_jobs = session.scalars(
        select(ProcessingJob)
        .where(
            ProcessingJob.organization_id == batch.organization_id,
            ProcessingJob.batch_id == batch.id,
            ProcessingJob.image_id.is_(None),
            ProcessingJob.job_type == MULTIMODAL_COMPARE_BATCH_JOB_TYPE,
            ProcessingJob.pipeline_version == pipeline_version,
            ProcessingJob.status.in_(("pending", "started")),
        )
        .with_for_update()
    ).all()
    non_stale_jobs = [
        job
        for job in active_jobs
        if not _claim_is_expired(job, now=now, settings=settings)
    ]
    if non_stale_jobs:
        session.rollback()
        raise MultimodalComparisonInProgressError
    for job in active_jobs:
        job.status = "failed"
        job.completed_at = now
        job.error_message = ERROR_CLAIM_EXPIRED

    idempotency_key = comparison_idempotency_key(
        batch_id=batch.id,
        pipeline_version=pipeline_version,
        candidates=candidates,
    )
    job = session.scalar(
        select(ProcessingJob)
        .where(ProcessingJob.idempotency_key == idempotency_key)
        .with_for_update()
    )
    if job is not None and job.status == "completed":
        session.commit()
        return None
    if job is None:
        job = ProcessingJob(
            organization_id=batch.organization_id,
            batch_id=batch.id,
            image_id=None,
            job_type=MULTIMODAL_COMPARE_BATCH_JOB_TYPE,
            status="pending",
            attempt_count=0,
            pipeline_version=pipeline_version,
            idempotency_key=idempotency_key,
        )
        session.add(job)
        session.flush()

    job.status = "started"
    job.started_at = now
    job.completed_at = None
    job.error_message = None
    job.attempt_count += 1
    session.flush()
    claim = _ComparisonClaim(
        job_id=job.id,
        attempt_token=job.attempt_count,
        batch_id=batch.id,
        pipeline_version=pipeline_version,
        candidates=candidates,
    )
    session.commit()
    return claim


def _require_initial_batch_state(session: Session, *, batch: UploadBatch) -> None:
    if batch.status != "review_required":
        session.rollback()
        raise MultimodalComparisonNotAllowedError(
            "Only review-ready batches can run multimodal comparison."
        )
    review_event_id = session.scalar(
        select(ReviewEvent.id)
        .where(
            ReviewEvent.organization_id == batch.organization_id,
            ReviewEvent.batch_id == batch.id,
        )
        .limit(1)
    )
    if review_event_id is not None:
        session.rollback()
        raise MultimodalComparisonNotAllowedError(
            "Multimodal comparison cannot run after review activity."
        )


def _eligible_pair_candidates(
    session: Session,
    *,
    batch: UploadBatch,
    pipeline_version: str,
    settings: MultimodalComparisonSettings,
) -> list[_PairCandidate]:
    image_a = aliased(ImageAsset)
    image_b = aliased(ImageAsset)
    rows = session.execute(
        select(PairAssessment, image_a, image_b)
        .join(
            image_a,
            and_(
                image_a.id == PairAssessment.image_a_id,
                image_a.organization_id == PairAssessment.organization_id,
                image_a.batch_id == PairAssessment.batch_id,
            ),
        )
        .join(
            image_b,
            and_(
                image_b.id == PairAssessment.image_b_id,
                image_b.organization_id == PairAssessment.organization_id,
                image_b.batch_id == PairAssessment.batch_id,
            ),
        )
        .where(
            PairAssessment.organization_id == batch.organization_id,
            PairAssessment.batch_id == batch.id,
            PairAssessment.pipeline_version == pipeline_version,
            PairAssessment.decision == DECISION_UNCERTAIN,
            PairAssessment.decision_source == DECISION_SOURCE_HEURISTIC,
            PairAssessment.embedding_similarity
            >= settings.candidate_similarity_threshold,
            PairAssessment.category_match.is_not(False),
            image_a.thumbnail_object_key.is_not(None),
            image_b.thumbnail_object_key.is_not(None),
        )
        .order_by(
            PairAssessment.embedding_similarity.desc(),
            PairAssessment.image_a_id,
            PairAssessment.image_b_id,
        )
        .limit(settings.max_comparisons)
    ).all()
    category_slugs = _category_slugs_by_image_id(
        session,
        image_ids=list(
            {
                image.id
                for _, first_image, second_image in rows
                for image in (first_image, second_image)
            }
        ),
        pipeline_version=pipeline_version,
    )
    return [
        _PairCandidate(
            assessment_id=assessment.id,
            image_a_id=assessment.image_a_id,
            image_b_id=assessment.image_b_id,
            image_a_filename=first_image.original_filename,
            image_b_filename=second_image.original_filename,
            image_a_thumbnail_key=_required_thumbnail_key(first_image),
            image_b_thumbnail_key=_required_thumbnail_key(second_image),
            embedding_similarity=_required_similarity(assessment),
            phash_distance=assessment.phash_distance,
            category_match=assessment.category_match,
            suggested_category_a=category_slugs.get(first_image.id),
            suggested_category_b=category_slugs.get(second_image.id),
        )
        for assessment, first_image, second_image in rows
    ]


def _category_slugs_by_image_id(
    session: Session,
    *,
    image_ids: list[UUID],
    pipeline_version: str,
) -> dict[UUID, str]:
    if not image_ids:
        return {}
    rows = session.execute(
        select(ImageClassification.image_id, Category.slug)
        .join(Category, Category.id == ImageClassification.category_id)
        .where(
            ImageClassification.organization_id == DEFAULT_ORGANIZATION_ID,
            ImageClassification.image_id.in_(image_ids),
            ImageClassification.pipeline_version == pipeline_version,
        )
    ).all()
    return {image_id: slug for image_id, slug in rows}


def _compare_claimed_pairs(
    *,
    claim: _ComparisonClaim,
    storage: WorkerStorage,
    provider: MultimodalComparisonProvider,
    settings: MultimodalComparisonSettings,
) -> tuple[_PairOutcome, ...]:
    outcomes: list[_PairOutcome] = []
    for candidate in claim.candidates:
        try:
            image_a_bytes = storage.read_object_bytes(
                object_key=candidate.image_a_thumbnail_key,
            )
            image_b_bytes = storage.read_object_bytes(
                object_key=candidate.image_b_thumbnail_key,
            )
        except WorkerObjectNotFoundError:
            continue

        result = generate_multimodal_comparison(
            provider,
            pair=MultimodalPairInput(
                batch_id=claim.batch_id,
                image_a_id=candidate.image_a_id,
                image_b_id=candidate.image_b_id,
                image_a_filename=candidate.image_a_filename,
                image_b_filename=candidate.image_b_filename,
                embedding_similarity=candidate.embedding_similarity,
                phash_distance=candidate.phash_distance,
                category_match=candidate.category_match,
                suggested_category_a=candidate.suggested_category_a,
                suggested_category_b=candidate.suggested_category_b,
                pipeline_version=claim.pipeline_version,
            ),
            image_a_bytes=image_a_bytes,
            image_b_bytes=image_b_bytes,
            timeout_seconds=settings.provider_timeout_seconds,
        )
        decision = result.decision
        if (
            decision == DECISION_SAME_PRODUCT
            and result.confidence < settings.same_product_threshold
        ):
            decision = DECISION_UNCERTAIN
        outcomes.append(
            _PairOutcome(
                assessment_id=candidate.assessment_id,
                decision=decision,
                confidence=result.confidence,
            )
        )
    return tuple(outcomes)


def _persist_comparison_results(
    session: Session,
    *,
    claim: _ComparisonClaim,
    outcomes: tuple[_PairOutcome, ...],
    settings: MultimodalComparisonSettings,
) -> None:
    batch, job = _lock_claim_rows(session, claim=claim)
    _require_claim_ownership(session, job=job, claim=claim)
    if not _final_state_is_valid(
        session,
        batch=batch,
        claim=claim,
        settings=settings,
    ):
        job.status = "failed"
        job.completed_at = _utc_now()
        job.error_message = ERROR_NOT_ALLOWED
        session.commit()
        raise MultimodalComparisonNotAllowedError(
            "Batch or pair state changed while comparison was running."
        )

    outcomes_by_id = {outcome.assessment_id: outcome for outcome in outcomes}
    if outcomes_by_id:
        assessments = session.scalars(
            select(PairAssessment)
            .where(PairAssessment.id.in_(outcomes_by_id))
            .with_for_update()
        ).all()
        if len(assessments) != len(outcomes_by_id):
            job.status = "failed"
            job.completed_at = _utc_now()
            job.error_message = ERROR_NOT_ALLOWED
            session.commit()
            raise MultimodalComparisonNotAllowedError(
                "Selected pair assessments changed during comparison."
            )
        for assessment in assessments:
            outcome = outcomes_by_id[assessment.id]
            assessment.decision = outcome.decision
            assessment.confidence = outcome.confidence
            assessment.decision_source = DECISION_SOURCE_MULTIMODAL_MODEL
        rebuild_product_groups_from_assessments(
            session,
            batch=batch,
            pipeline_version=claim.pipeline_version,
        )

    job.status = "completed"
    job.completed_at = _utc_now()
    job.error_message = None
    session.commit()


def _final_state_is_valid(
    session: Session,
    *,
    batch: UploadBatch,
    claim: _ComparisonClaim,
    settings: MultimodalComparisonSettings,
) -> bool:
    if (
        batch.status != "review_required"
        or batch.pipeline_version != claim.pipeline_version
    ):
        return False
    if session.scalar(
        select(ReviewEvent.id)
        .where(
            ReviewEvent.organization_id == batch.organization_id,
            ReviewEvent.batch_id == batch.id,
        )
        .limit(1)
    ) is not None:
        return False
    group_statuses = session.scalars(
        select(ProductGroup.status).where(
            ProductGroup.organization_id == batch.organization_id,
            ProductGroup.batch_id == batch.id,
        )
    ).all()
    if any(status != "proposed" for status in group_statuses):
        return False

    assessments = session.scalars(
        select(PairAssessment)
        .where(
            PairAssessment.id.in_(
                [candidate.assessment_id for candidate in claim.candidates]
            )
        )
        .with_for_update()
    ).all()
    assessments_by_id = {assessment.id: assessment for assessment in assessments}
    if len(assessments_by_id) != len(claim.candidates):
        return False
    for candidate in claim.candidates:
        assessment = assessments_by_id[candidate.assessment_id]
        if (
            assessment.organization_id != batch.organization_id
            or assessment.batch_id != batch.id
            or assessment.pipeline_version != claim.pipeline_version
            or assessment.image_a_id != candidate.image_a_id
            or assessment.image_b_id != candidate.image_b_id
            or assessment.decision != DECISION_UNCERTAIN
            or assessment.decision_source != DECISION_SOURCE_HEURISTIC
            or assessment.category_match is False
            or assessment.embedding_similarity is None
            or assessment.embedding_similarity
            < settings.candidate_similarity_threshold
        ):
            return False

    selected_image_ids = {
        image_id
        for candidate in claim.candidates
        for image_id in (candidate.image_a_id, candidate.image_b_id)
    }
    images = session.scalars(
        select(ImageAsset).where(
            ImageAsset.organization_id == batch.organization_id,
            ImageAsset.batch_id == batch.id,
            ImageAsset.id.in_(selected_image_ids),
        )
    ).all()
    if len(images) != len(selected_image_ids):
        return False
    if any(image.thumbnail_object_key is None for image in images):
        return False
    return True


def _fail_claim_if_owned(
    session: Session,
    *,
    claim: _ComparisonClaim,
    error_code: str,
) -> None:
    session.rollback()
    _, job = _lock_claim_rows(session, claim=claim)
    _require_claim_ownership(session, job=job, claim=claim)
    job.status = "failed"
    job.completed_at = _utc_now()
    job.error_message = error_code
    session.commit()


def _lock_claim_rows(
    session: Session,
    *,
    claim: _ComparisonClaim,
) -> tuple[UploadBatch, ProcessingJob]:
    batch = session.scalar(
        select(UploadBatch)
        .where(
            UploadBatch.id == claim.batch_id,
            UploadBatch.organization_id == DEFAULT_ORGANIZATION_ID,
        )
        .with_for_update()
    )
    job = session.scalar(
        select(ProcessingJob)
        .where(
            ProcessingJob.id == claim.job_id,
            ProcessingJob.batch_id == claim.batch_id,
            ProcessingJob.organization_id == DEFAULT_ORGANIZATION_ID,
        )
        .with_for_update()
    )
    if batch is None or job is None:
        session.rollback()
        raise MultimodalComparisonClaimLostError
    return batch, job


def _require_claim_ownership(
    session: Session,
    *,
    job: ProcessingJob,
    claim: _ComparisonClaim,
) -> None:
    if (
        job.id != claim.job_id
        or job.status != "started"
        or job.attempt_count != claim.attempt_token
    ):
        session.rollback()
        raise MultimodalComparisonClaimLostError


def _claim_is_expired(
    job: ProcessingJob,
    *,
    now: datetime,
    settings: MultimodalComparisonSettings,
) -> bool:
    claim_time = job.started_at or job.created_at
    return now - claim_time >= timedelta(seconds=settings.claim_timeout_seconds)


def _required_thumbnail_key(image: ImageAsset) -> str:
    if image.thumbnail_object_key is None:
        raise AssertionError("Eligible images must have thumbnail object keys.")
    return image.thumbnail_object_key


def _required_similarity(assessment: PairAssessment) -> float:
    if assessment.embedding_similarity is None:
        raise AssertionError("Eligible pair assessments must have similarity.")
    return assessment.embedding_similarity


def _read_int_setting(name: str, default: int) -> int:
    configured_value = os.getenv(name)
    if configured_value is None:
        return default
    try:
        return int(configured_value)
    except ValueError as error:
        raise MultimodalComparisonConfigurationError(
            f"{name} must be an integer."
        ) from error


def _read_float_setting(name: str, default: float) -> float:
    configured_value = os.getenv(name)
    if configured_value is None:
        return default
    try:
        return float(configured_value)
    except ValueError as error:
        raise MultimodalComparisonConfigurationError(
            f"{name} must be numeric."
        ) from error


def _utc_now() -> datetime:
    return datetime.now(UTC)
