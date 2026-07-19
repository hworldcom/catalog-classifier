from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from catalog_api.approved_group_exports import (
    APPROVED_GROUPS_EXPORT_ENABLED_ENV,
)
from catalog_api.image_processing import JPEG_CONTENT_TYPE
from catalog_api.models import (
    Category,
    ImageAsset,
    ProductGroup,
    ProductGroupImage,
    ReviewEvent,
    UploadBatch,
)
from catalog_api.upload_batches import DEFAULT_ORGANIZATION_ID

pytestmark = [pytest.mark.anyio, pytest.mark.postgresql]

PIPELINE_VERSION = "2026-06-01"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@dataclass(frozen=True)
class ApprovedExportFixture:
    batch_id: UUID
    early_group_id: UUID
    later_group_id: UUID
    early_image_id: UUID
    retained_image_id: UUID
    rejected_image_id: UUID
    duplicate_image_id: UUID
    approved_category_id: UUID


def _seed_approved_export(session: Session) -> ApprovedExportFixture:
    categories = {
        category.slug: category
        for category in session.scalars(
            select(Category).where(
                Category.slug.in_(("sportswear", "t-shirts", "trousers"))
            )
        ).all()
    }
    assert set(categories) == {"sportswear", "t-shirts", "trousers"}

    batch = UploadBatch(
        organization_id=DEFAULT_ORGANIZATION_ID,
        status="approved",
        original_file_count=4,
        processed_file_count=4,
        finalized_at=datetime(2026, 7, 19, 9, 0, tzinfo=UTC),
        completed_at=datetime(2026, 7, 19, 9, 30, tzinfo=UTC),
        pipeline_version=PIPELINE_VERSION,
    )
    session.add(batch)
    session.flush()

    early_image_id = uuid4()
    retained_image_id = uuid4()
    rejected_image_id = uuid4()
    duplicate_image_id = uuid4()
    for upload_order, image_id, filename in (
        (0, retained_image_id, "front.jpg"),
        (1, rejected_image_id, "rejected.jpg"),
        (2, duplicate_image_id, "duplicate.jpg"),
        (3, early_image_id, "trousers.jpg"),
    ):
        session.add(
            ImageAsset(
                id=image_id,
                organization_id=DEFAULT_ORGANIZATION_ID,
                batch_id=batch.id,
                original_object_key=(
                    f"organizations/{DEFAULT_ORGANIZATION_ID}/batches/{batch.id}/"
                    f"originals/{image_id}.jpg"
                ),
                original_filename=filename,
                upload_order=upload_order,
                mime_type=JPEG_CONTENT_TYPE,
                size_bytes=100 + upload_order,
                status="processed",
            )
        )

    later_group = ProductGroup(
        organization_id=DEFAULT_ORGANIZATION_ID,
        batch_id=batch.id,
        status="approved",
        suggested_category_id=categories["sportswear"].id,
        approved_category_id=categories["t-shirts"].id,
        approved_category_source="reviewer_selection",
        cover_image_id=retained_image_id,
        confidence=0.92,
        created_at=datetime(2026, 7, 19, 9, 20, tzinfo=UTC),
        approved_at=datetime(2026, 7, 19, 9, 25, tzinfo=UTC),
    )
    early_group = ProductGroup(
        organization_id=DEFAULT_ORGANIZATION_ID,
        batch_id=batch.id,
        status="approved",
        suggested_category_id=None,
        approved_category_id=categories["trousers"].id,
        approved_category_source="machine_suggestion",
        cover_image_id=early_image_id,
        confidence=None,
        created_at=datetime(2026, 7, 19, 9, 10, tzinfo=UTC),
        approved_at=datetime(2026, 7, 19, 9, 24, tzinfo=UTC),
    )
    session.add_all([later_group, early_group])
    session.flush()

    session.add_all(
        [
            ProductGroupImage(
                organization_id=DEFAULT_ORGANIZATION_ID,
                batch_id=batch.id,
                group_id=later_group.id,
                image_id=retained_image_id,
                position=0,
                membership_source="engine",
                membership_confidence=0.95,
            ),
            ProductGroupImage(
                organization_id=DEFAULT_ORGANIZATION_ID,
                batch_id=batch.id,
                group_id=later_group.id,
                image_id=rejected_image_id,
                position=1,
                membership_source="engine",
                membership_confidence=0.85,
                is_rejected=True,
            ),
            ProductGroupImage(
                organization_id=DEFAULT_ORGANIZATION_ID,
                batch_id=batch.id,
                group_id=later_group.id,
                image_id=duplicate_image_id,
                position=2,
                membership_source="exact_duplicate",
                membership_confidence=1.0,
                is_duplicate=True,
                duplicate_of_image_id=retained_image_id,
            ),
            ProductGroupImage(
                organization_id=DEFAULT_ORGANIZATION_ID,
                batch_id=batch.id,
                group_id=early_group.id,
                image_id=early_image_id,
                position=0,
                membership_source="singleton",
                membership_confidence=None,
            ),
        ]
    )
    session.commit()
    return ApprovedExportFixture(
        batch_id=batch.id,
        early_group_id=early_group.id,
        later_group_id=later_group.id,
        early_image_id=early_image_id,
        retained_image_id=retained_image_id,
        rejected_image_id=rejected_image_id,
        duplicate_image_id=duplicate_image_id,
        approved_category_id=categories["t-shirts"].id,
    )


def _enable_export(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(APPROVED_GROUPS_EXPORT_ENABLED_ENV, "true")


async def test_approved_groups_export_is_deterministic_and_read_only(
    database_client: AsyncClient,
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_export(monkeypatch)
    with Session(migrated_engine) as session:
        fixture = _seed_approved_export(session)
        before_counts = _export_row_counts(session)

    endpoint = f"/v1/upload-batches/{fixture.batch_id}/approved-groups"
    first_response = await database_client.get(endpoint)
    second_response = await database_client.get(endpoint)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert second_response.json() == first_response.json()
    assert first_response.json() == {
        "batchId": str(fixture.batch_id),
        "organizationId": str(DEFAULT_ORGANIZATION_ID),
        "status": "approved",
        "pipelineVersion": PIPELINE_VERSION,
        "groups": [
            {
                "groupId": str(fixture.early_group_id),
                "approvedCategorySlug": "trousers",
                "suggestedCategorySlug": None,
                "coverImageId": str(fixture.early_image_id),
                "confidence": None,
                "images": [
                    {
                        "imageId": str(fixture.early_image_id),
                        "position": 0,
                        "isDuplicate": False,
                        "duplicateOfImageId": None,
                    }
                ],
            },
            {
                "groupId": str(fixture.later_group_id),
                "approvedCategorySlug": "t-shirts",
                "suggestedCategorySlug": "sportswear",
                "coverImageId": str(fixture.retained_image_id),
                "confidence": 0.92,
                "images": [
                    {
                        "imageId": str(fixture.retained_image_id),
                        "position": 0,
                        "isDuplicate": False,
                        "duplicateOfImageId": None,
                    },
                    {
                        "imageId": str(fixture.duplicate_image_id),
                        "position": 2,
                        "isDuplicate": True,
                        "duplicateOfImageId": str(fixture.retained_image_id),
                    },
                ],
            },
        ],
    }

    with Session(migrated_engine) as session:
        assert _export_row_counts(session) == before_counts


async def test_approved_groups_export_is_disabled_before_batch_lookup(
    database_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(APPROVED_GROUPS_EXPORT_ENABLED_ENV, raising=False)

    response = await database_client.get(
        f"/v1/upload-batches/{uuid4()}/approved-groups"
    )

    assert response.status_code == 404
    assert response.json()["detail"] == {
        "code": "approved_groups_export_disabled",
        "message": "Approved group export is not enabled.",
    }


async def test_approved_groups_export_returns_404_for_unknown_batch(
    database_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_export(monkeypatch)

    response = await database_client.get(
        f"/v1/upload-batches/{uuid4()}/approved-groups"
    )

    assert response.status_code == 404
    assert response.json()["detail"] == {
        "code": "batch_not_found",
        "message": "Upload batch was not found.",
    }


async def test_approved_groups_export_rejects_non_approved_batch(
    database_client: AsyncClient,
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_export(monkeypatch)
    with Session(migrated_engine) as session:
        batch = UploadBatch(
            organization_id=DEFAULT_ORGANIZATION_ID,
            status="review_required",
            original_file_count=0,
            processed_file_count=0,
            finalized_at=datetime.now(UTC),
            pipeline_version=PIPELINE_VERSION,
        )
        session.add(batch)
        session.commit()
        batch_id = batch.id

    response = await database_client.get(
        f"/v1/upload-batches/{batch_id}/approved-groups"
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "batch_not_approved",
        "message": "Approved groups are only available for approved batches.",
    }


async def test_approved_groups_export_returns_empty_approved_batch(
    database_client: AsyncClient,
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_export(monkeypatch)
    with Session(migrated_engine) as session:
        batch = UploadBatch(
            organization_id=DEFAULT_ORGANIZATION_ID,
            status="approved",
            original_file_count=0,
            processed_file_count=0,
            finalized_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            pipeline_version=PIPELINE_VERSION,
        )
        session.add(batch)
        session.commit()
        batch_id = batch.id

    response = await database_client.get(
        f"/v1/upload-batches/{batch_id}/approved-groups"
    )

    assert response.status_code == 200
    assert response.json() == {
        "batchId": str(batch_id),
        "organizationId": str(DEFAULT_ORGANIZATION_ID),
        "status": "approved",
        "pipelineVersion": PIPELINE_VERSION,
        "groups": [],
    }


@pytest.mark.parametrize(
    "invalid_case",
    [
        "missing_pipeline_version",
        "blank_pipeline_version",
        "unapproved_group",
        "missing_approved_category",
        "blank_approved_category_slug",
        "no_active_non_duplicate",
        "missing_cover",
        "duplicate_cover",
        "non_duplicate_with_duplicate_reference",
        "duplicate_without_reference",
        "duplicate_with_other_group_retained_image",
    ],
)
async def test_approved_groups_export_rejects_invalid_approved_snapshot(
    database_client: AsyncClient,
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    invalid_case: str,
) -> None:
    _enable_export(monkeypatch)
    with Session(migrated_engine) as session:
        fixture = _seed_approved_export(session)
        _break_export_invariant(
            session,
            fixture=fixture,
            invalid_case=invalid_case,
        )
        session.commit()
        before_counts = _export_row_counts(session)

    response = await database_client.get(
        f"/v1/upload-batches/{fixture.batch_id}/approved-groups"
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "approved_groups_invalid",
        "message": "The approved group export is internally inconsistent.",
    }
    with Session(migrated_engine) as session:
        assert _export_row_counts(session) == before_counts


def _break_export_invariant(
    session: Session,
    *,
    fixture: ApprovedExportFixture,
    invalid_case: str,
) -> None:
    batch = session.get(UploadBatch, fixture.batch_id)
    later_group = session.get(ProductGroup, fixture.later_group_id)
    retained = session.get(
        ProductGroupImage,
        (fixture.later_group_id, fixture.retained_image_id),
    )
    rejected = session.get(
        ProductGroupImage,
        (fixture.later_group_id, fixture.rejected_image_id),
    )
    duplicate = session.get(
        ProductGroupImage,
        (fixture.later_group_id, fixture.duplicate_image_id),
    )
    assert batch is not None
    assert later_group is not None
    assert retained is not None
    assert rejected is not None
    assert duplicate is not None

    if invalid_case == "missing_pipeline_version":
        batch.pipeline_version = None
    elif invalid_case == "blank_pipeline_version":
        batch.pipeline_version = " "
    elif invalid_case == "unapproved_group":
        later_group.status = "proposed"
    elif invalid_case == "missing_approved_category":
        later_group.approved_category_id = None
        later_group.approved_category_source = None
    elif invalid_case == "blank_approved_category_slug":
        category = session.get(Category, fixture.approved_category_id)
        assert category is not None
        category.slug = " "
    elif invalid_case == "no_active_non_duplicate":
        retained.is_rejected = True
    elif invalid_case == "missing_cover":
        later_group.cover_image_id = None
    elif invalid_case == "duplicate_cover":
        later_group.cover_image_id = fixture.duplicate_image_id
    elif invalid_case == "non_duplicate_with_duplicate_reference":
        retained.duplicate_of_image_id = fixture.rejected_image_id
    elif invalid_case == "duplicate_without_reference":
        duplicate.duplicate_of_image_id = None
    elif invalid_case == "duplicate_with_other_group_retained_image":
        duplicate.duplicate_of_image_id = fixture.early_image_id
    else:
        raise AssertionError(f"Unsupported invalid case: {invalid_case}")


def _export_row_counts(session: Session) -> tuple[int, int, int]:
    return (
        session.scalar(select(func.count()).select_from(ProductGroup)) or 0,
        session.scalar(select(func.count()).select_from(ProductGroupImage)) or 0,
        session.scalar(select(func.count()).select_from(ReviewEvent)) or 0,
    )
