from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import combinations
from math import sqrt
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from catalog_api.embedding_vectors import EMBEDDING_DIMENSIONS
from catalog_api.models import (
    ImageAsset,
    ImageClassification,
    ImageEmbedding,
    PairAssessment,
    ProductGroup,
    ProductGroupImage,
    ProcessingJob,
    UploadBatch,
)
from catalog_api.processing_jobs import (
    GROUP_BATCH_JOB_TYPE,
    GroupBatchTaskPayload,
    group_batch_idempotency_key,
)
from catalog_api.upload_batches import DEFAULT_ORGANIZATION_ID

DECISION_SAME_PRODUCT = "same_product"
DECISION_DIFFERENT_PRODUCT = "different_product"
DECISION_UNCERTAIN = "uncertain"
DECISION_SOURCE_HEURISTIC = "heuristic"
DECISION_SOURCE_EXACT_DUPLICATE = "exact_duplicate"
MEMBERSHIP_SOURCE_ENGINE = "engine"
MEMBERSHIP_SOURCE_EXACT_DUPLICATE = "exact_duplicate"
MEMBERSHIP_SOURCE_SINGLETON = "singleton"


class GroupingBatchNotFoundError(Exception):
    """Raised when a grouping task cannot find its batch."""


class GroupingJobNotFoundError(Exception):
    """Raised when a grouping task cannot find its job row."""


class GroupingBatchStateError(Exception):
    """Raised when a batch is not ready for grouping."""


@dataclass(frozen=True)
class GroupingSettings:
    max_candidates_per_image: int
    phash_max_distance: int
    uncertain_similarity_threshold: float
    same_product_similarity_threshold: float


@dataclass(frozen=True)
class GroupBatchTaskResult:
    batch_id: UUID
    pipeline_version: str
    job_status: str
    did_work: bool


@dataclass(frozen=True)
class _ImageSignals:
    image: ImageAsset
    embedding: list[float] | None
    category_id: UUID | None


@dataclass(frozen=True)
class _PairDecision:
    image_a_id: UUID
    image_b_id: UUID
    decision: str
    confidence: float | None
    decision_source: str
    embedding_similarity: float | None
    phash_distance: int | None
    category_match: bool | None
    upload_order_distance: int


@dataclass
class _WorkingGroup:
    image_ids: set[UUID]


def grouping_settings_from_env() -> GroupingSettings:
    return GroupingSettings(
        max_candidates_per_image=_read_int_setting(
            "CATALOG_GROUPING_MAX_CANDIDATES_PER_IMAGE",
            50,
        ),
        phash_max_distance=_read_int_setting(
            "CATALOG_GROUPING_PHASH_MAX_DISTANCE",
            8,
        ),
        uncertain_similarity_threshold=_read_float_setting(
            "CATALOG_GROUPING_UNCERTAIN_SIMILARITY_THRESHOLD",
            0.80,
        ),
        same_product_similarity_threshold=_read_float_setting(
            "CATALOG_GROUPING_SAME_PRODUCT_SIMILARITY_THRESHOLD",
            0.92,
        ),
    )


def group_batch_task(
    session: Session,
    *,
    payload: GroupBatchTaskPayload,
    settings: GroupingSettings | None = None,
) -> GroupBatchTaskResult:
    resolved_settings = settings or grouping_settings_from_env()
    job = session.scalar(
        select(ProcessingJob)
        .where(
            ProcessingJob.batch_id == payload.batch_id,
            ProcessingJob.image_id.is_(None),
            ProcessingJob.organization_id == DEFAULT_ORGANIZATION_ID,
            ProcessingJob.job_type == GROUP_BATCH_JOB_TYPE,
            ProcessingJob.pipeline_version == payload.pipeline_version,
            ProcessingJob.idempotency_key
            == group_batch_idempotency_key(
                batch_id=payload.batch_id,
                pipeline_version=payload.pipeline_version,
            ),
        )
        .with_for_update()
    )
    if job is None:
        raise GroupingJobNotFoundError

    batch = session.scalar(
        select(UploadBatch)
        .where(
            UploadBatch.id == payload.batch_id,
            UploadBatch.organization_id == DEFAULT_ORGANIZATION_ID,
        )
        .with_for_update()
    )
    if batch is None:
        raise GroupingBatchNotFoundError

    existing_group = session.scalar(
        select(ProductGroup.id).where(
            ProductGroup.organization_id == batch.organization_id,
            ProductGroup.batch_id == batch.id,
        )
    )
    if existing_group is not None or batch.status == "review_required":
        job.status = "completed"
        job.completed_at = job.completed_at or _utc_now()
        job.error_message = None
        session.commit()
        return GroupBatchTaskResult(
            batch_id=payload.batch_id,
            pipeline_version=payload.pipeline_version,
            job_status="completed",
            did_work=False,
        )

    if batch.status != "processing":
        raise GroupingBatchStateError("Batch is not ready for grouping.")
    if batch.pipeline_version != payload.pipeline_version:
        raise GroupingBatchStateError(
            "Grouping tasks must use the active batch pipeline version."
        )

    job.status = "started"
    job.started_at = _utc_now()
    job.completed_at = None
    job.error_message = None
    job.attempt_count += 1

    signals = _load_image_signals(
        session,
        batch_id=payload.batch_id,
        pipeline_version=payload.pipeline_version,
    )
    pair_decisions = _pair_decisions(
        signals=signals,
        settings=resolved_settings,
    )
    _persist_pair_assessments(
        session,
        batch=batch,
        pair_decisions=pair_decisions,
        pipeline_version=payload.pipeline_version,
    )
    _persist_product_groups(
        session,
        batch=batch,
        signals=signals,
        pair_decisions=pair_decisions,
    )
    batch.status = "review_required"
    job.status = "completed"
    job.completed_at = _utc_now()
    job.error_message = None
    session.commit()

    return GroupBatchTaskResult(
        batch_id=payload.batch_id,
        pipeline_version=payload.pipeline_version,
        job_status="completed",
        did_work=True,
    )


def _load_image_signals(
    session: Session,
    *,
    batch_id: UUID,
    pipeline_version: str,
) -> list[_ImageSignals]:
    images = session.scalars(
        select(ImageAsset)
        .where(
            ImageAsset.organization_id == DEFAULT_ORGANIZATION_ID,
            ImageAsset.batch_id == batch_id,
            ImageAsset.status == "processed",
        )
        .order_by(ImageAsset.upload_order)
    ).all()
    image_ids = [image.id for image in images]
    embeddings = _embeddings_by_image_id(
        session,
        image_ids=image_ids,
        pipeline_version=pipeline_version,
    )
    categories = _category_ids_by_image_id(
        session,
        image_ids=image_ids,
        pipeline_version=pipeline_version,
    )
    return [
        _ImageSignals(
            image=image,
            embedding=embeddings.get(image.id),
            category_id=categories.get(image.id),
        )
        for image in images
    ]


def _embeddings_by_image_id(
    session: Session,
    *,
    image_ids: list[UUID],
    pipeline_version: str,
) -> dict[UUID, list[float]]:
    if not image_ids:
        return {}
    embeddings = session.scalars(
        select(ImageEmbedding).where(
            ImageEmbedding.organization_id == DEFAULT_ORGANIZATION_ID,
            ImageEmbedding.image_id.in_(image_ids),
            ImageEmbedding.pipeline_version == pipeline_version,
            ImageEmbedding.dimensions == EMBEDDING_DIMENSIONS,
        )
    ).all()
    return {embedding.image_id: list(embedding.embedding) for embedding in embeddings}


def _category_ids_by_image_id(
    session: Session,
    *,
    image_ids: list[UUID],
    pipeline_version: str,
) -> dict[UUID, UUID]:
    if not image_ids:
        return {}
    classifications = session.scalars(
        select(ImageClassification).where(
            ImageClassification.organization_id == DEFAULT_ORGANIZATION_ID,
            ImageClassification.image_id.in_(image_ids),
            ImageClassification.pipeline_version == pipeline_version,
            ImageClassification.category_id.is_not(None),
        )
    ).all()
    return {
        classification.image_id: classification.category_id
        for classification in classifications
        if classification.category_id is not None
    }


def _pair_decisions(
    *,
    signals: list[_ImageSignals],
    settings: GroupingSettings,
) -> dict[tuple[UUID, UUID], _PairDecision]:
    exact_duplicate_pairs = _exact_duplicate_pairs(signals)
    candidate_pairs = set(exact_duplicate_pairs)
    candidate_pairs.update(
        _embedding_candidate_pairs(signals=signals, settings=settings)
    )
    decisions: dict[tuple[UUID, UUID], _PairDecision] = {}
    signals_by_id = {signal.image.id: signal for signal in signals}

    for pair_key in sorted(candidate_pairs):
        image_a = signals_by_id[pair_key[0]]
        image_b = signals_by_id[pair_key[1]]
        if pair_key in exact_duplicate_pairs:
            decisions[pair_key] = _exact_duplicate_decision(image_a, image_b)
        else:
            decisions[pair_key] = _heuristic_decision(
                image_a,
                image_b,
                settings=settings,
            )
    return decisions


def _exact_duplicate_pairs(signals: list[_ImageSignals]) -> set[tuple[UUID, UUID]]:
    signals_by_sha: dict[str, list[_ImageSignals]] = {}
    for signal in signals:
        if signal.image.sha256 is None:
            continue
        signals_by_sha.setdefault(signal.image.sha256, []).append(signal)

    pairs: set[tuple[UUID, UUID]] = set()
    for duplicate_signals in signals_by_sha.values():
        if len(duplicate_signals) < 2:
            continue
        for first, second in combinations(duplicate_signals, 2):
            pairs.add(_pair_key(first.image.id, second.image.id))
    return pairs


def _embedding_candidate_pairs(
    *,
    signals: list[_ImageSignals],
    settings: GroupingSettings,
) -> set[tuple[UUID, UUID]]:
    signals_with_embeddings = [
        signal for signal in signals if signal.embedding is not None
    ]
    pairs: set[tuple[UUID, UUID]] = set()
    for signal in signals_with_embeddings:
        scored_candidates = [
            (
                _cosine_similarity(signal.embedding, candidate.embedding),
                candidate,
            )
            for candidate in signals_with_embeddings
            if candidate.image.id != signal.image.id and candidate.embedding is not None
        ]
        scored_candidates.sort(
            key=lambda scored_candidate: (
                scored_candidate[0],
                -abs(
                    signal.image.upload_order
                    - scored_candidate[1].image.upload_order
                ),
            ),
            reverse=True,
        )
        for _, candidate in scored_candidates[: settings.max_candidates_per_image]:
            pairs.add(_pair_key(signal.image.id, candidate.image.id))
    return pairs


def _exact_duplicate_decision(
    image_a: _ImageSignals,
    image_b: _ImageSignals,
) -> _PairDecision:
    return _PairDecision(
        image_a_id=min(image_a.image.id, image_b.image.id),
        image_b_id=max(image_a.image.id, image_b.image.id),
        decision=DECISION_SAME_PRODUCT,
        confidence=1.0,
        decision_source=DECISION_SOURCE_EXACT_DUPLICATE,
        embedding_similarity=_optional_similarity(image_a.embedding, image_b.embedding),
        phash_distance=_optional_hamming_distance(
            image_a.image.phash,
            image_b.image.phash,
        ),
        category_match=_category_match(image_a.category_id, image_b.category_id),
        upload_order_distance=abs(
            image_a.image.upload_order - image_b.image.upload_order,
        ),
    )


def _heuristic_decision(
    image_a: _ImageSignals,
    image_b: _ImageSignals,
    *,
    settings: GroupingSettings,
) -> _PairDecision:
    similarity = _optional_similarity(image_a.embedding, image_b.embedding)
    phash_distance = _optional_hamming_distance(image_a.image.phash, image_b.image.phash)
    category_match = _category_match(image_a.category_id, image_b.category_id)
    category_conflicts = category_match is False
    phash_conflicts = (
        phash_distance is not None
        and phash_distance > settings.phash_max_distance
    )

    if category_conflicts:
        decision = DECISION_DIFFERENT_PRODUCT
        confidence = similarity
    elif similarity is None:
        decision = DECISION_UNCERTAIN
        confidence = None
    elif similarity < settings.uncertain_similarity_threshold:
        decision = DECISION_DIFFERENT_PRODUCT
        confidence = similarity
    elif similarity >= settings.same_product_similarity_threshold and not phash_conflicts:
        decision = DECISION_SAME_PRODUCT
        confidence = similarity
    else:
        decision = DECISION_UNCERTAIN
        confidence = similarity

    return _PairDecision(
        image_a_id=min(image_a.image.id, image_b.image.id),
        image_b_id=max(image_a.image.id, image_b.image.id),
        decision=decision,
        confidence=confidence,
        decision_source=DECISION_SOURCE_HEURISTIC,
        embedding_similarity=similarity,
        phash_distance=phash_distance,
        category_match=category_match,
        upload_order_distance=abs(
            image_a.image.upload_order - image_b.image.upload_order,
        ),
    )


def _persist_pair_assessments(
    session: Session,
    *,
    batch: UploadBatch,
    pair_decisions: dict[tuple[UUID, UUID], _PairDecision],
    pipeline_version: str,
) -> None:
    existing_keys = {
        (assessment.image_a_id, assessment.image_b_id)
        for assessment in session.scalars(
            select(PairAssessment).where(
                PairAssessment.organization_id == batch.organization_id,
                PairAssessment.batch_id == batch.id,
                PairAssessment.pipeline_version == pipeline_version,
            )
        ).all()
    }
    for pair_key, decision in pair_decisions.items():
        if pair_key in existing_keys:
            continue
        session.add(
            PairAssessment(
                organization_id=batch.organization_id,
                batch_id=batch.id,
                image_a_id=decision.image_a_id,
                image_b_id=decision.image_b_id,
                embedding_similarity=decision.embedding_similarity,
                phash_distance=decision.phash_distance,
                category_match=decision.category_match,
                upload_order_distance=decision.upload_order_distance,
                decision=decision.decision,
                confidence=decision.confidence,
                decision_source=decision.decision_source,
                pipeline_version=pipeline_version,
            )
        )


def _persist_product_groups(
    session: Session,
    *,
    batch: UploadBatch,
    signals: list[_ImageSignals],
    pair_decisions: dict[tuple[UUID, UUID], _PairDecision],
) -> None:
    if not signals:
        return

    groups = _build_groups(signals=signals, pair_decisions=pair_decisions)
    duplicate_retained_by_image_id = _duplicate_retained_by_image_id(signals)
    categories_by_group = _suggested_categories_by_group(signals=signals, groups=groups)
    signals_by_id = {signal.image.id: signal for signal in signals}

    for group in sorted(
        groups,
        key=lambda working_group: min(
            signals_by_id[image_id].image.upload_order
            for image_id in working_group.image_ids
        ),
    ):
        ordered_image_ids = sorted(
            group.image_ids,
            key=lambda image_id: signals_by_id[image_id].image.upload_order,
        )
        group_confidence = _group_confidence(
            group=group,
            pair_decisions=pair_decisions,
        )
        group_row = ProductGroup(
            organization_id=batch.organization_id,
            batch_id=batch.id,
            status="proposed",
            suggested_category_id=categories_by_group.get(id(group)),
            cover_image_id=_cover_image_id(
                ordered_image_ids=ordered_image_ids,
                duplicate_retained_by_image_id=duplicate_retained_by_image_id,
            ),
            confidence=group_confidence,
        )
        session.add(group_row)
        session.flush()

        for position, image_id in enumerate(ordered_image_ids):
            duplicate_of_image_id = duplicate_retained_by_image_id.get(image_id)
            is_duplicate = duplicate_of_image_id is not None
            membership_source = _membership_source(
                group=group,
                is_duplicate=is_duplicate,
            )
            session.add(
                ProductGroupImage(
                    organization_id=batch.organization_id,
                    batch_id=batch.id,
                    group_id=group_row.id,
                    image_id=image_id,
                    position=position,
                    membership_source=membership_source,
                    membership_confidence=(
                        None
                        if membership_source == MEMBERSHIP_SOURCE_SINGLETON
                        else group_confidence
                    ),
                    is_duplicate=is_duplicate,
                    duplicate_of_image_id=duplicate_of_image_id,
                )
            )


def _build_groups(
    *,
    signals: list[_ImageSignals],
    pair_decisions: dict[tuple[UUID, UUID], _PairDecision],
) -> list[_WorkingGroup]:
    groups_by_image_id = {
        signal.image.id: _WorkingGroup(image_ids={signal.image.id})
        for signal in signals
    }
    same_pairs = sorted(
        [
            decision
            for decision in pair_decisions.values()
            if decision.decision == DECISION_SAME_PRODUCT
        ],
        key=lambda decision: decision.confidence or 0.0,
        reverse=True,
    )
    for decision in same_pairs:
        first_group = groups_by_image_id[decision.image_a_id]
        second_group = groups_by_image_id[decision.image_b_id]
        if first_group is second_group:
            continue
        if not _can_merge(first_group, second_group, pair_decisions=pair_decisions):
            continue
        merged_ids = first_group.image_ids | second_group.image_ids
        merged_group = _WorkingGroup(image_ids=merged_ids)
        for image_id in merged_ids:
            groups_by_image_id[image_id] = merged_group

    unique_groups: list[_WorkingGroup] = []
    seen_group_ids: set[int] = set()
    for signal in signals:
        group = groups_by_image_id[signal.image.id]
        if id(group) in seen_group_ids:
            continue
        unique_groups.append(group)
        seen_group_ids.add(id(group))
    return unique_groups


def _can_merge(
    first_group: _WorkingGroup,
    second_group: _WorkingGroup,
    *,
    pair_decisions: dict[tuple[UUID, UUID], _PairDecision],
) -> bool:
    for first_id in first_group.image_ids:
        for second_id in second_group.image_ids:
            decision = pair_decisions.get(_pair_key(first_id, second_id))
            if decision is None or decision.decision != DECISION_SAME_PRODUCT:
                return False
    return True


def _duplicate_retained_by_image_id(
    signals: list[_ImageSignals],
) -> dict[UUID, UUID]:
    signals_by_sha: dict[str, list[_ImageSignals]] = {}
    for signal in signals:
        if signal.image.sha256 is None:
            continue
        signals_by_sha.setdefault(signal.image.sha256, []).append(signal)

    duplicate_retained_by_image_id: dict[UUID, UUID] = {}
    for duplicate_signals in signals_by_sha.values():
        if len(duplicate_signals) < 2:
            continue
        ordered_signals = sorted(
            duplicate_signals,
            key=lambda signal: signal.image.upload_order,
        )
        retained_id = ordered_signals[0].image.id
        for duplicate_signal in ordered_signals[1:]:
            duplicate_retained_by_image_id[duplicate_signal.image.id] = retained_id
    return duplicate_retained_by_image_id


def _suggested_categories_by_group(
    *,
    signals: list[_ImageSignals],
    groups: list[_WorkingGroup],
) -> dict[int, UUID]:
    signals_by_id = {signal.image.id: signal for signal in signals}
    categories_by_group: dict[int, UUID] = {}
    for group in groups:
        category_counts = Counter(
            signals_by_id[image_id].category_id
            for image_id in group.image_ids
            if signals_by_id[image_id].category_id is not None
        )
        if not category_counts:
            continue
        categories_by_group[id(group)] = category_counts.most_common(1)[0][0]
    return categories_by_group


def _group_confidence(
    *,
    group: _WorkingGroup,
    pair_decisions: dict[tuple[UUID, UUID], _PairDecision],
) -> float:
    if len(group.image_ids) == 1:
        return 1.0
    confidences = [
        decision.confidence
        for first_id, second_id in combinations(group.image_ids, 2)
        if (
            decision := pair_decisions.get(_pair_key(first_id, second_id))
        ) is not None
        and decision.decision == DECISION_SAME_PRODUCT
        and decision.confidence is not None
    ]
    return min(confidences) if confidences else 1.0


def _cover_image_id(
    *,
    ordered_image_ids: list[UUID],
    duplicate_retained_by_image_id: dict[UUID, UUID],
) -> UUID:
    for image_id in ordered_image_ids:
        if image_id not in duplicate_retained_by_image_id:
            return image_id
    return ordered_image_ids[0]


def _membership_source(
    *,
    group: _WorkingGroup,
    is_duplicate: bool,
) -> str:
    if len(group.image_ids) == 1:
        return MEMBERSHIP_SOURCE_SINGLETON
    if is_duplicate:
        return MEMBERSHIP_SOURCE_EXACT_DUPLICATE
    return MEMBERSHIP_SOURCE_ENGINE


def _pair_key(first_id: UUID, second_id: UUID) -> tuple[UUID, UUID]:
    return (first_id, second_id) if first_id < second_id else (second_id, first_id)


def _cosine_similarity(first: list[float], second: list[float]) -> float:
    first_norm = sqrt(sum(value * value for value in first))
    second_norm = sqrt(sum(value * value for value in second))
    if first_norm == 0 or second_norm == 0:
        return 0.0
    dot_product = sum(
        first_value * second_value
        for first_value, second_value in zip(first, second)
    )
    return dot_product / (first_norm * second_norm)


def _optional_similarity(
    first: list[float] | None,
    second: list[float] | None,
) -> float | None:
    if first is None or second is None:
        return None
    return _cosine_similarity(first, second)


def _optional_hamming_distance(first: str | None, second: str | None) -> int | None:
    if first is None or second is None:
        return None
    return bin(int(first, 16) ^ int(second, 16)).count("1")


def _category_match(
    first_category_id: UUID | None,
    second_category_id: UUID | None,
) -> bool | None:
    if first_category_id is None or second_category_id is None:
        return None
    return first_category_id == second_category_id


def _read_int_setting(name: str, default: int) -> int:
    configured_value = os.getenv(name)
    if configured_value is None:
        return default
    return int(configured_value)


def _read_float_setting(name: str, default: float) -> float:
    configured_value = os.getenv(name)
    if configured_value is None:
        return default
    return float(configured_value)


def _utc_now() -> datetime:
    return datetime.now(UTC)
