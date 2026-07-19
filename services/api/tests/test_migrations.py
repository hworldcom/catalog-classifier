from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    Table,
    create_engine,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from catalog_api.database import Base
from catalog_api.embedding_vectors import (
    EMBEDDING_DIMENSIONS,
    image_embedding_vector_type,
)
from catalog_api import models  # noqa: F401

pytestmark = pytest.mark.postgresql

DEFAULT_ORGANIZATION_ID = UUID("00000000-0000-0000-0000-000000000001")
API_ROOT = Path(__file__).resolve().parents[1]


def _alembic_config(database_url: str) -> Config:
    config = Config(API_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def _upgrade(database_url: str) -> Engine:
    command.upgrade(_alembic_config(database_url), "head")
    return create_engine(database_url)


def test_upgrade_matches_models_and_supports_ordered_images(
    empty_database_url: str,
) -> None:
    engine = _upgrade(empty_database_url)

    try:
        inspector = inspect(engine)
        assert set(inspector.get_table_names()) == {
            "alembic_version",
            "categories",
            "image_assets",
            "image_classifications",
            "image_embeddings",
            "organizations",
            "pair_assessments",
            "processing_jobs",
            "product_group_images",
            "product_groups",
            "review_events",
            "upload_batches",
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_check_constraints("upload_batches")
        } == {
            "ck_upload_batches_original_file_count_nonnegative",
            "ck_upload_batches_processed_file_count_nonnegative",
            "ck_upload_batches_processed_within_original",
            "ck_upload_batches_status",
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_check_constraints("image_assets")
        } == {
            "ck_image_assets_dhash_lower_hex",
            "ck_image_assets_height_nonnegative",
            "ck_image_assets_normalized_size_bytes_nonnegative",
            "ck_image_assets_phash_lower_hex",
            "ck_image_assets_size_bytes_nonnegative",
            "ck_image_assets_status",
            "ck_image_assets_upload_order_nonnegative",
            "ck_image_assets_width_nonnegative",
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("image_assets")
        } == {
            "uq_image_assets_id_organization_id",
            "uq_image_assets_id_organization_batch",
            "uq_image_assets_organization_batch_upload_order",
            "uq_image_assets_organization_original_object_key",
            "uq_image_assets_organization_thumbnail_object_key",
        }
        image_foreign_keys = {
            foreign_key["name"]: foreign_key
            for foreign_key in inspector.get_foreign_keys("image_assets")
        }
        assert image_foreign_keys[
            "fk_image_assets_batch_organization_upload_batches"
        ]["options"]["ondelete"] == "CASCADE"
        assert image_foreign_keys[
            "fk_image_assets_organization_id_organizations"
        ]["options"]["ondelete"] == "RESTRICT"
        assert {
            constraint["name"]
            for constraint in inspector.get_check_constraints("processing_jobs")
        } == {
            "ck_processing_jobs_attempt_count_nonnegative",
            "ck_processing_jobs_status",
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("processing_jobs")
        } == {
            "uq_processing_jobs_idempotency_key",
        }
        processing_foreign_keys = {
            foreign_key["name"]: foreign_key
            for foreign_key in inspector.get_foreign_keys("processing_jobs")
        }
        assert processing_foreign_keys[
            "fk_processing_jobs_batch_organization_upload_batches"
        ]["options"]["ondelete"] == "CASCADE"
        assert processing_foreign_keys[
            "fk_processing_jobs_image_organization_image_assets"
        ]["options"]["ondelete"] == "CASCADE"
        assert processing_foreign_keys[
            "fk_processing_jobs_organization_id_organizations"
        ]["options"]["ondelete"] == "RESTRICT"
        assert {
            constraint["name"]
            for constraint in inspector.get_check_constraints("image_embeddings")
        } == {
            "ck_image_embeddings_dimensions_supported",
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("image_embeddings")
        } == {
            "uq_image_embeddings_organization_image_pipeline_version",
        }
        embedding_foreign_keys = {
            foreign_key["name"]: foreign_key
            for foreign_key in inspector.get_foreign_keys("image_embeddings")
        }
        assert embedding_foreign_keys[
            "fk_image_embeddings_image_organization_image_assets"
        ]["options"]["ondelete"] == "CASCADE"
        assert embedding_foreign_keys[
            "fk_image_embeddings_organization_id_organizations"
        ]["options"]["ondelete"] == "RESTRICT"
        category_indexes = {
            index["name"]: index for index in inspector.get_indexes("categories")
        }
        assert set(category_indexes) == {
            "uq_categories_global_slug",
            "uq_categories_organization_slug",
        }
        assert category_indexes["uq_categories_global_slug"]["unique"] is True
        assert category_indexes["uq_categories_organization_slug"]["unique"] is True
        category_foreign_keys = {
            foreign_key["name"]: foreign_key
            for foreign_key in inspector.get_foreign_keys("categories")
        }
        assert category_foreign_keys[
            "fk_categories_organization_id_organizations"
        ]["options"]["ondelete"] == "RESTRICT"
        assert category_foreign_keys[
            "fk_categories_parent_id_categories"
        ]["options"]["ondelete"] == "RESTRICT"
        assert {
            constraint["name"]
            for constraint in inspector.get_check_constraints("image_classifications")
        } == {
            "ck_image_classifications_confidence_range",
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("image_classifications")
        } == {
            "uq_image_classifications_organization_image_pipeline_version",
        }
        classification_foreign_keys = {
            foreign_key["name"]: foreign_key
            for foreign_key in inspector.get_foreign_keys("image_classifications")
        }
        assert classification_foreign_keys[
            "fk_image_classifications_category_id_categories"
        ]["options"]["ondelete"] == "RESTRICT"
        assert classification_foreign_keys[
            "fk_image_classifications_image_organization_image_assets"
        ]["options"]["ondelete"] == "CASCADE"
        assert classification_foreign_keys[
            "fk_image_classifications_organization_id_organizations"
        ]["options"]["ondelete"] == "RESTRICT"
        assert {
            constraint["name"]
            for constraint in inspector.get_check_constraints("pair_assessments")
        } == {
            "ck_pair_assessments_canonical_image_order",
            "ck_pair_assessments_confidence_range",
            "ck_pair_assessments_decision",
            "ck_pair_assessments_phash_distance_nonnegative",
            "ck_pair_assessments_upload_order_distance_nonnegative",
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("pair_assessments")
        } == {
            "uq_pair_assessments_organization_batch_pair_pipeline",
        }
        pair_foreign_keys = {
            foreign_key["name"]: foreign_key
            for foreign_key in inspector.get_foreign_keys("pair_assessments")
        }
        assert pair_foreign_keys[
            "fk_pair_assessments_batch_organization_upload_batches"
        ]["options"]["ondelete"] == "CASCADE"
        assert pair_foreign_keys[
            "fk_pair_assessments_image_a_organization_batch_image_assets"
        ]["options"]["ondelete"] == "CASCADE"
        assert pair_foreign_keys[
            "fk_pair_assessments_image_b_organization_batch_image_assets"
        ]["options"]["ondelete"] == "CASCADE"
        assert pair_foreign_keys[
            "fk_pair_assessments_organization_id_organizations"
        ]["options"]["ondelete"] == "RESTRICT"
        assert {
            constraint["name"]
            for constraint in inspector.get_check_constraints("product_groups")
        } == {
            "ck_product_groups_approved_category_source",
            "ck_product_groups_approved_category_source_consistency",
            "ck_product_groups_confidence_range",
            "ck_product_groups_status",
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("product_groups")
        } == {
            "uq_product_groups_id_organization_batch",
        }
        group_foreign_keys = {
            foreign_key["name"]: foreign_key
            for foreign_key in inspector.get_foreign_keys("product_groups")
        }
        assert group_foreign_keys[
            "fk_product_groups_approved_category_id_categories"
        ]["options"]["ondelete"] == "RESTRICT"
        assert group_foreign_keys[
            "fk_product_groups_batch_organization_upload_batches"
        ]["options"]["ondelete"] == "CASCADE"
        assert group_foreign_keys[
            "fk_product_groups_cover_image_organization_batch_image_assets"
        ]["options"]["ondelete"] == "RESTRICT"
        assert group_foreign_keys[
            "fk_product_groups_organization_id_organizations"
        ]["options"]["ondelete"] == "RESTRICT"
        assert group_foreign_keys[
            "fk_product_groups_suggested_category_id_categories"
        ]["options"]["ondelete"] == "RESTRICT"
        assert {
            constraint["name"]
            for constraint in inspector.get_check_constraints("product_group_images")
        } == {
            "ck_product_group_images_duplicate_not_self",
            "ck_product_group_images_membership_confidence_range",
            "ck_product_group_images_position_nonnegative",
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("product_group_images")
        } == {
            "uq_product_group_images_group_position",
            "uq_product_group_images_organization_batch_image",
        }
        membership_foreign_keys = {
            foreign_key["name"]: foreign_key
            for foreign_key in inspector.get_foreign_keys("product_group_images")
        }
        assert membership_foreign_keys[
            "fk_product_group_images_duplicate_image_assets"
        ]["options"]["ondelete"] == "RESTRICT"
        assert membership_foreign_keys[
            "fk_product_group_images_group_organization_batch_product_groups"
        ]["options"]["ondelete"] == "CASCADE"
        assert membership_foreign_keys[
            "fk_product_group_images_image_organization_batch_image_assets"
        ]["options"]["ondelete"] == "CASCADE"
        assert membership_foreign_keys[
            "fk_product_group_images_organization_id_organizations"
        ]["options"]["ondelete"] == "RESTRICT"
        review_foreign_keys = {
            foreign_key["name"]: foreign_key
            for foreign_key in inspector.get_foreign_keys("review_events")
        }
        assert review_foreign_keys[
            "fk_review_events_batch_organization_upload_batches"
        ]["options"]["ondelete"] == "CASCADE"
        assert review_foreign_keys[
            "fk_review_events_organization_id_organizations"
        ]["options"]["ondelete"] == "RESTRICT"

        with engine.begin() as connection:
            organization = connection.execute(
                text(
                    """
                    SELECT id, name
                    FROM organizations
                    WHERE id = :organization_id
                    """
                ),
                {"organization_id": DEFAULT_ORGANIZATION_ID},
            ).one()
            assert organization.name == "Default Organization"

            batch_id = connection.execute(
                text(
                    """
                    INSERT INTO upload_batches (organization_id)
                    VALUES (:organization_id)
                    RETURNING id
                    """
                ),
                {"organization_id": DEFAULT_ORGANIZATION_ID},
            ).scalar_one()

            for upload_order, filename in ((1, "back.jpg"), (0, "front.jpg")):
                connection.execute(
                    text(
                        """
                        INSERT INTO image_assets (
                            organization_id,
                            batch_id,
                            original_object_key,
                            original_filename,
                            upload_order,
                            mime_type,
                            size_bytes
                        )
                        VALUES (
                            :organization_id,
                            :batch_id,
                            :object_key,
                            :filename,
                            :upload_order,
                            'image/jpeg',
                            100
                        )
                        """
                    ),
                    {
                        "organization_id": DEFAULT_ORGANIZATION_ID,
                        "batch_id": batch_id,
                        "object_key": f"originals/{filename}",
                        "filename": filename,
                        "upload_order": upload_order,
                    },
                )

            filenames = connection.execute(
                text(
                    """
                    SELECT original_filename
                    FROM image_assets
                    WHERE organization_id = :organization_id
                      AND batch_id = :batch_id
                    ORDER BY upload_order
                    """
                ),
                {
                    "organization_id": DEFAULT_ORGANIZATION_ID,
                    "batch_id": batch_id,
                },
            ).scalars()
            assert list(filenames) == ["front.jpg", "back.jpg"]

            seeded_categories = connection.execute(
                text(
                    """
                    SELECT child.slug, parent.slug AS parent_slug, child.active
                    FROM categories child
                    LEFT JOIN categories parent ON parent.id = child.parent_id
                    WHERE child.organization_id IS NULL
                    ORDER BY child.slug
                    """
                )
            ).all()
            assert set(seeded_categories) == {
                ("clothing", None, True),
                ("hoodies", "clothing", True),
                ("jackets", "clothing", True),
                ("sportswear", "clothing", True),
                ("t-shirts", "clothing", True),
                ("trousers", "clothing", True),
            }

            image_id = connection.execute(
                text(
                    """
                    SELECT id
                    FROM image_assets
                    WHERE organization_id = :organization_id
                      AND batch_id = :batch_id
                    ORDER BY upload_order
                    LIMIT 1
                    """
                ),
                {
                    "organization_id": DEFAULT_ORGANIZATION_ID,
                    "batch_id": batch_id,
                },
            ).scalar_one()
            sample_embedding = [
                index / EMBEDDING_DIMENSIONS
                for index in range(EMBEDDING_DIMENSIONS)
            ]
            connection.execute(
                text(
                    """
                    UPDATE image_assets
                    SET phash = '0123456789abcdef',
                        dhash = 'fedcba9876543210'
                    WHERE id = :image_id
                    """
                ),
                {"image_id": image_id},
            )
            image_embeddings = Table(
                "image_embeddings",
                MetaData(),
                autoload_with=connection,
            )
            connection.execute(
                image_embeddings.insert().values(
                    organization_id=DEFAULT_ORGANIZATION_ID,
                    image_id=image_id,
                    provider="fake-provider",
                    model="fake-model",
                    dimensions=EMBEDDING_DIMENSIONS,
                    pipeline_version="2026-06-01",
                    embedding=sample_embedding,
                )
            )
            persisted_embedding = connection.execute(
                select(image_embeddings.c.embedding).where(
                    image_embeddings.c.image_id == image_id
                )
            ).scalar_one()
            assert list(persisted_embedding) == pytest.approx(sample_embedding)

            image_classifications = Table(
                "image_classifications",
                MetaData(),
                autoload_with=connection,
            )
            category_id = connection.execute(
                text("SELECT id FROM categories WHERE slug = 't-shirts'")
            ).scalar_one()
            connection.execute(
                image_classifications.insert().values(
                    organization_id=DEFAULT_ORGANIZATION_ID,
                    image_id=image_id,
                    category_id=category_id,
                    confidence=0.91,
                    attributes_json={
                        "categorySlug": "t-shirts",
                        "confidence": 0.91,
                    },
                    provider="fake-provider",
                    model="fake-model",
                    raw_response_json={
                        "categorySlug": "t-shirts",
                        "confidence": 0.91,
                    },
                    pipeline_version="2026-06-01",
                )
            )
            classification = connection.execute(
                select(
                    image_classifications.c.category_id,
                    image_classifications.c.confidence,
                    image_classifications.c.attributes_json,
                ).where(image_classifications.c.image_id == image_id)
            ).one()
            assert classification.category_id == category_id
            assert classification.confidence == pytest.approx(0.91)
            assert classification.attributes_json == {
                "categorySlug": "t-shirts",
                "confidence": 0.91,
            }

        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={
                    "compare_type": True,
                    "compare_server_default": True,
                },
            )
            assert compare_metadata(context, Base.metadata) == []
    finally:
        engine.dispose()


def test_image_rejection_migration_backfills_existing_memberships(
    empty_database_url: str,
) -> None:
    config = _alembic_config(empty_database_url)
    command.upgrade(config, "0007_grouping_review")
    engine = create_engine(empty_database_url)

    try:
        with engine.begin() as connection:
            batch_id = connection.execute(
                text(
                    """
                    INSERT INTO upload_batches (organization_id)
                    VALUES (:organization_id)
                    RETURNING id
                    """
                ),
                {"organization_id": DEFAULT_ORGANIZATION_ID},
            ).scalar_one()
            image_id = connection.execute(
                text(
                    """
                    INSERT INTO image_assets (
                        organization_id,
                        batch_id,
                        original_object_key,
                        original_filename,
                        upload_order,
                        mime_type,
                        size_bytes
                    )
                    VALUES (
                        :organization_id,
                        :batch_id,
                        'qa/0022/existing.jpg',
                        'existing.jpg',
                        0,
                        'image/jpeg',
                        100
                    )
                    RETURNING id
                    """
                ),
                {
                    "organization_id": DEFAULT_ORGANIZATION_ID,
                    "batch_id": batch_id,
                },
            ).scalar_one()
            group_id = connection.execute(
                text(
                    """
                    INSERT INTO product_groups (
                        organization_id,
                        batch_id,
                        cover_image_id
                    )
                    VALUES (
                        :organization_id,
                        :batch_id,
                        :image_id
                    )
                    RETURNING id
                    """
                ),
                {
                    "organization_id": DEFAULT_ORGANIZATION_ID,
                    "batch_id": batch_id,
                    "image_id": image_id,
                },
            ).scalar_one()
            connection.execute(
                text(
                    """
                    INSERT INTO product_group_images (
                        organization_id,
                        batch_id,
                        group_id,
                        image_id,
                        position,
                        membership_source
                    )
                    VALUES (
                        :organization_id,
                        :batch_id,
                        :group_id,
                        :image_id,
                        0,
                        'singleton'
                    )
                    """
                ),
                {
                    "organization_id": DEFAULT_ORGANIZATION_ID,
                    "batch_id": batch_id,
                    "group_id": group_id,
                    "image_id": image_id,
                },
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    upgraded_engine = create_engine(empty_database_url)
    try:
        columns = {
            column["name"]: column
            for column in inspect(upgraded_engine).get_columns(
                "product_group_images"
            )
        }
        assert columns["is_rejected"]["nullable"] is False
        assert columns["is_rejected"]["default"] in {
            "false",
            "false::boolean",
        }
        with upgraded_engine.connect() as connection:
            assert connection.execute(
                text(
                    """
                    SELECT is_rejected
                    FROM product_group_images
                    WHERE group_id = :group_id
                      AND image_id = :image_id
                    """
                ),
                {
                    "group_id": group_id,
                    "image_id": image_id,
                },
            ).scalar_one() is False
    finally:
        upgraded_engine.dispose()


def test_constraints_reject_cross_organization_image_relationships(
    empty_database_url: str,
) -> None:
    engine = _upgrade(empty_database_url)
    other_organization_id = uuid4()

    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO organizations (id, name) "
                    "VALUES (:organization_id, 'Other Organization')"
                ),
                {"organization_id": other_organization_id},
            )
            batch_id = connection.execute(
                text(
                    """
                    INSERT INTO upload_batches (organization_id)
                    VALUES (:organization_id)
                    RETURNING id
                    """
                ),
                {"organization_id": DEFAULT_ORGANIZATION_ID},
            ).scalar_one()

        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO image_assets (
                            organization_id,
                            batch_id,
                            original_object_key,
                            original_filename,
                            upload_order,
                            mime_type,
                            size_bytes
                        )
                        VALUES (
                            :organization_id,
                            :batch_id,
                            'other/front.jpg',
                            'front.jpg',
                            0,
                            'image/jpeg',
                            100
                        )
                        """
                    ),
                    {
                        "organization_id": other_organization_id,
                        "batch_id": batch_id,
                    },
                )

        with engine.begin() as connection:
            image_id = connection.execute(
                text(
                    """
                    INSERT INTO image_assets (
                        organization_id,
                        batch_id,
                        original_object_key,
                        original_filename,
                        upload_order,
                        mime_type,
                        size_bytes
                    )
                    VALUES (
                        :organization_id,
                        :batch_id,
                        'default/front.jpg',
                        'front.jpg',
                        0,
                        'image/jpeg',
                        100
                    )
                    RETURNING id
                    """
                ),
                {
                    "organization_id": DEFAULT_ORGANIZATION_ID,
                    "batch_id": batch_id,
                },
            ).scalar_one()

        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM organizations WHERE id = :organization_id"),
                    {"organization_id": DEFAULT_ORGANIZATION_ID},
                )

        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM upload_batches WHERE id = :batch_id"),
                {"batch_id": batch_id},
            )
            assert connection.execute(
                text("SELECT id FROM image_assets WHERE id = :image_id"),
                {"image_id": image_id},
            ).first() is None
    finally:
        engine.dispose()


def test_constraints_reject_duplicate_processing_idempotency_keys(
    empty_database_url: str,
) -> None:
    engine = _upgrade(empty_database_url)

    try:
        with engine.begin() as connection:
            batch_id = connection.execute(
                text(
                    """
                    INSERT INTO upload_batches (organization_id, status)
                    VALUES (:organization_id, 'processing')
                    RETURNING id
                    """
                ),
                {"organization_id": DEFAULT_ORGANIZATION_ID},
            ).scalar_one()
            image_id = connection.execute(
                text(
                    """
                    INSERT INTO image_assets (
                        organization_id,
                        batch_id,
                        original_object_key,
                        original_filename,
                        upload_order,
                        mime_type,
                        size_bytes,
                        status
                    )
                    VALUES (
                        :organization_id,
                        :batch_id,
                        'default/front.jpg',
                        'front.jpg',
                        0,
                        'image/jpeg',
                        100,
                        'uploaded'
                    )
                    RETURNING id
                    """
                ),
                {
                    "organization_id": DEFAULT_ORGANIZATION_ID,
                    "batch_id": batch_id,
                },
            ).scalar_one()
            connection.execute(
                text(
                    """
                    INSERT INTO processing_jobs (
                        organization_id,
                        batch_id,
                        image_id,
                        job_type,
                        pipeline_version,
                        idempotency_key
                    )
                    VALUES (
                        :organization_id,
                        :batch_id,
                        :image_id,
                        'process-image',
                        '2026-06-01',
                        'process-image:test:2026-06-01'
                    )
                    """
                ),
                {
                    "organization_id": DEFAULT_ORGANIZATION_ID,
                    "batch_id": batch_id,
                    "image_id": image_id,
                },
            )

        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO processing_jobs (
                            organization_id,
                            batch_id,
                            image_id,
                            job_type,
                            pipeline_version,
                            idempotency_key
                        )
                        VALUES (
                            :organization_id,
                            :batch_id,
                            :image_id,
                            'process-image',
                            '2026-06-01',
                            'process-image:test:2026-06-01'
                        )
                        """
                    ),
                    {
                        "organization_id": DEFAULT_ORGANIZATION_ID,
                        "batch_id": batch_id,
                        "image_id": image_id,
                    },
                )
    finally:
        engine.dispose()


def test_category_schema_constraints_and_unknown_classifications(
    empty_database_url: str,
) -> None:
    engine = _upgrade(empty_database_url)

    try:
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO categories (
                            organization_id,
                            slug,
                            name_pl,
                            name_en,
                            name_de,
                            name_vi
                        )
                        VALUES (
                            NULL,
                            'clothing',
                            'Duplicate',
                            'Duplicate',
                            'Duplicate',
                            'Duplicate'
                        )
                        """
                    )
                )

        with engine.begin() as connection:
            batch_id = connection.execute(
                text(
                    """
                    INSERT INTO upload_batches (organization_id)
                    VALUES (:organization_id)
                    RETURNING id
                    """
                ),
                {"organization_id": DEFAULT_ORGANIZATION_ID},
            ).scalar_one()
            image_id = connection.execute(
                text(
                    """
                    INSERT INTO image_assets (
                        organization_id,
                        batch_id,
                        original_object_key,
                        original_filename,
                        upload_order,
                        mime_type,
                        size_bytes,
                        status
                    )
                    VALUES (
                        :organization_id,
                        :batch_id,
                        'classifications/front.jpg',
                        'front.jpg',
                        0,
                        'image/jpeg',
                        100,
                        'processed'
                    )
                    RETURNING id
                    """
                ),
                {
                    "organization_id": DEFAULT_ORGANIZATION_ID,
                    "batch_id": batch_id,
                },
            ).scalar_one()
            image_classifications = Table(
                "image_classifications",
                MetaData(),
                autoload_with=connection,
            )
            connection.execute(
                image_classifications.insert().values(
                    organization_id=DEFAULT_ORGANIZATION_ID,
                    image_id=image_id,
                    category_id=None,
                    confidence=0.41,
                    attributes_json={
                        "categorySlug": "unknown",
                        "confidence": 0.41,
                    },
                    provider="fake-provider",
                    model="fake-model",
                    raw_response_json={
                        "categorySlug": "not-in-taxonomy",
                        "confidence": 0.41,
                    },
                    pipeline_version="2026-06-01",
                )
            )
            persisted_classification = connection.execute(
                select(
                    image_classifications.c.category_id,
                    image_classifications.c.confidence,
                    image_classifications.c.attributes_json,
                ).where(image_classifications.c.image_id == image_id)
            ).one()
            assert persisted_classification.category_id is None
            assert persisted_classification.confidence == pytest.approx(0.41)
            assert persisted_classification.attributes_json == {
                "categorySlug": "unknown",
                "confidence": 0.41,
            }

        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                image_classifications = Table(
                    "image_classifications",
                    MetaData(),
                    autoload_with=connection,
                )
                connection.execute(
                    image_classifications.insert().values(
                        organization_id=DEFAULT_ORGANIZATION_ID,
                        image_id=image_id,
                        category_id=None,
                        confidence=0.52,
                        attributes_json={
                            "categorySlug": "unknown",
                            "confidence": 0.52,
                        },
                        provider="fake-provider",
                        model="fake-model",
                        raw_response_json={
                            "categorySlug": "unknown",
                            "confidence": 0.52,
                        },
                        pipeline_version="2026-06-01",
                    )
                )

        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                image_classifications = Table(
                    "image_classifications",
                    MetaData(),
                    autoload_with=connection,
                )
                connection.execute(
                    image_classifications.insert().values(
                        organization_id=DEFAULT_ORGANIZATION_ID,
                        image_id=image_id,
                        category_id=None,
                        confidence=1.1,
                        attributes_json={
                            "categorySlug": "unknown",
                            "confidence": 1.1,
                        },
                        provider="fake-provider",
                        model="fake-model",
                        raw_response_json={
                            "categorySlug": "unknown",
                            "confidence": 1.1,
                        },
                        pipeline_version="2026-06-02",
                    )
                )
    finally:
        engine.dispose()


def test_grouping_schema_constraints_and_review_tables(
    empty_database_url: str,
) -> None:
    engine = _upgrade(empty_database_url)

    try:
        with engine.begin() as connection:
            batch_id = connection.execute(
                text(
                    """
                    INSERT INTO upload_batches (
                        organization_id,
                        status,
                        pipeline_version
                    )
                    VALUES (:organization_id, 'review_required', '2026-06-01')
                    RETURNING id
                    """
                ),
                {"organization_id": DEFAULT_ORGANIZATION_ID},
            ).scalar_one()
            image_ids = []
            for upload_order, filename in ((0, "front.jpg"), (1, "back.jpg")):
                image_ids.append(
                    connection.execute(
                        text(
                            """
                            INSERT INTO image_assets (
                                organization_id,
                                batch_id,
                                original_object_key,
                                thumbnail_object_key,
                                original_filename,
                                upload_order,
                                mime_type,
                                size_bytes,
                                status
                            )
                            VALUES (
                                :organization_id,
                                :batch_id,
                                :object_key,
                                :thumbnail_key,
                                :filename,
                                :upload_order,
                                'image/jpeg',
                                100,
                                'processed'
                            )
                            RETURNING id
                            """
                        ),
                        {
                            "organization_id": DEFAULT_ORGANIZATION_ID,
                            "batch_id": batch_id,
                            "object_key": f"grouping/{filename}",
                            "thumbnail_key": f"grouping/thumbs/{filename}",
                            "filename": filename,
                            "upload_order": upload_order,
                        },
                    ).scalar_one()
                )

            image_a_id, image_b_id = sorted(image_ids)
            category_id = connection.execute(
                text("SELECT id FROM categories WHERE slug = 'sportswear'")
            ).scalar_one()
            connection.execute(
                text(
                    """
                    INSERT INTO pair_assessments (
                        organization_id,
                        batch_id,
                        image_a_id,
                        image_b_id,
                        embedding_similarity,
                        phash_distance,
                        category_match,
                        upload_order_distance,
                        decision,
                        confidence,
                        decision_source,
                        pipeline_version
                    )
                    VALUES (
                        :organization_id,
                        :batch_id,
                        :image_a_id,
                        :image_b_id,
                        0.97,
                        3,
                        true,
                        1,
                        'same_product',
                        0.93,
                        'qa',
                        '2026-06-01'
                    )
                    """
                ),
                {
                    "organization_id": DEFAULT_ORGANIZATION_ID,
                    "batch_id": batch_id,
                    "image_a_id": image_a_id,
                    "image_b_id": image_b_id,
                },
            )
            group_id = connection.execute(
                text(
                    """
                    INSERT INTO product_groups (
                        organization_id,
                        batch_id,
                        status,
                        suggested_category_id,
                        cover_image_id,
                        confidence
                    )
                    VALUES (
                        :organization_id,
                        :batch_id,
                        'proposed',
                        :category_id,
                        :cover_image_id,
                        0.93
                    )
                    RETURNING id
                    """
                ),
                {
                    "organization_id": DEFAULT_ORGANIZATION_ID,
                    "batch_id": batch_id,
                    "category_id": category_id,
                    "cover_image_id": image_ids[0],
                },
            ).scalar_one()
            connection.execute(
                text(
                    """
                    INSERT INTO product_group_images (
                        organization_id,
                        batch_id,
                        group_id,
                        image_id,
                        position,
                        membership_source,
                        membership_confidence
                    )
                    VALUES (
                        :organization_id,
                        :batch_id,
                        :group_id,
                        :image_id,
                        0,
                        'engine',
                        0.94
                    )
                    """
                ),
                {
                    "organization_id": DEFAULT_ORGANIZATION_ID,
                    "batch_id": batch_id,
                    "group_id": group_id,
                    "image_id": image_ids[0],
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO product_group_images (
                        organization_id,
                        batch_id,
                        group_id,
                        image_id,
                        position,
                        membership_source,
                        membership_confidence,
                        is_duplicate,
                        duplicate_of_image_id
                    )
                    VALUES (
                        :organization_id,
                        :batch_id,
                        :group_id,
                        :image_id,
                        1,
                        'engine',
                        0.94,
                        true,
                        :duplicate_of_image_id
                    )
                    """
                ),
                {
                    "organization_id": DEFAULT_ORGANIZATION_ID,
                    "batch_id": batch_id,
                    "group_id": group_id,
                    "image_id": image_ids[1],
                    "duplicate_of_image_id": image_ids[0],
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO review_events (
                        organization_id,
                        batch_id,
                        group_id,
                        action_type,
                        payload_json
                    )
                    VALUES (
                        :organization_id,
                        :batch_id,
                        :group_id,
                        'qa_seed',
                        '{}'::jsonb
                    )
                    """
                ),
                {
                    "organization_id": DEFAULT_ORGANIZATION_ID,
                    "batch_id": batch_id,
                    "group_id": group_id,
                },
            )

            assert connection.execute(
                text("SELECT count(*) FROM product_group_images")
            ).scalar_one() == 2

        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO pair_assessments (
                            organization_id,
                            batch_id,
                            image_a_id,
                            image_b_id,
                            decision,
                            decision_source,
                            pipeline_version
                        )
                        VALUES (
                            :organization_id,
                            :batch_id,
                            :image_b_id,
                            :image_a_id,
                            'same_product',
                            'qa',
                            '2026-06-02'
                        )
                        """
                    ),
                    {
                        "organization_id": DEFAULT_ORGANIZATION_ID,
                        "batch_id": batch_id,
                        "image_a_id": image_a_id,
                        "image_b_id": image_b_id,
                    },
                )

        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO pair_assessments (
                            organization_id,
                            batch_id,
                            image_a_id,
                            image_b_id,
                            decision,
                            decision_source,
                            pipeline_version
                        )
                        VALUES (
                            :organization_id,
                            :batch_id,
                            :image_a_id,
                            :image_b_id,
                            'maybe',
                            'qa',
                            '2026-06-02'
                        )
                        """
                    ),
                    {
                        "organization_id": DEFAULT_ORGANIZATION_ID,
                        "batch_id": batch_id,
                        "image_a_id": image_a_id,
                        "image_b_id": image_b_id,
                    },
                )

        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO product_groups (
                            organization_id,
                            batch_id,
                            status
                        )
                        VALUES (:organization_id, :batch_id, 'pending')
                        """
                    ),
                    {
                        "organization_id": DEFAULT_ORGANIZATION_ID,
                        "batch_id": batch_id,
                    },
                )

        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                second_group_id = connection.execute(
                    text(
                        """
                        INSERT INTO product_groups (
                            organization_id,
                            batch_id,
                            status,
                            cover_image_id
                        )
                        VALUES (
                            :organization_id,
                            :batch_id,
                            'proposed',
                            :cover_image_id
                        )
                        RETURNING id
                        """
                    ),
                    {
                        "organization_id": DEFAULT_ORGANIZATION_ID,
                        "batch_id": batch_id,
                        "cover_image_id": image_ids[1],
                    },
                ).scalar_one()
                connection.execute(
                    text(
                        """
                        INSERT INTO product_group_images (
                            organization_id,
                            batch_id,
                            group_id,
                            image_id,
                            position,
                            membership_source
                        )
                        VALUES (
                            :organization_id,
                            :batch_id,
                            :group_id,
                            :image_id,
                            0,
                            'engine'
                        )
                        """
                    ),
                    {
                        "organization_id": DEFAULT_ORGANIZATION_ID,
                        "batch_id": batch_id,
                        "group_id": second_group_id,
                        "image_id": image_ids[0],
                    },
                )
    finally:
        engine.dispose()


def test_approved_category_source_migration_backfills_and_constrains_state(
    empty_database_url: str,
) -> None:
    config = _alembic_config(empty_database_url)
    command.upgrade(config, "0008_image_rejection")
    engine = create_engine(empty_database_url)

    with engine.begin() as connection:
        batch_id = connection.execute(
            text(
                """
                INSERT INTO upload_batches (
                    organization_id,
                    status,
                    pipeline_version
                )
                VALUES (
                    :organization_id,
                    'review_required',
                    '2026-06-01'
                )
                RETURNING id
                """
            ),
            {"organization_id": DEFAULT_ORGANIZATION_ID},
        ).scalar_one()
        category_id = connection.execute(
            text("SELECT id FROM categories WHERE slug = 't-shirts'")
        ).scalar_one()
        group_id = connection.execute(
            text(
                """
                INSERT INTO product_groups (
                    organization_id,
                    batch_id,
                    approved_category_id
                )
                VALUES (
                    :organization_id,
                    :batch_id,
                    :category_id
                )
                RETURNING id
                """
            ),
            {
                "organization_id": DEFAULT_ORGANIZATION_ID,
                "batch_id": batch_id,
                "category_id": category_id,
            },
        ).scalar_one()
        empty_group_id = connection.execute(
            text(
                """
                INSERT INTO product_groups (organization_id, batch_id)
                VALUES (:organization_id, :batch_id)
                RETURNING id
                """
            ),
            {
                "organization_id": DEFAULT_ORGANIZATION_ID,
                "batch_id": batch_id,
            },
        ).scalar_one()

    engine.dispose()
    command.upgrade(config, "head")
    engine = create_engine(empty_database_url)

    try:
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    """
                    SELECT approved_category_source
                    FROM product_groups
                    WHERE id = :group_id
                    """
                ),
                {"group_id": group_id},
            ).scalar_one() == "reviewer_selection"
            assert connection.execute(
                text(
                    """
                    SELECT approved_category_source
                    FROM product_groups
                    WHERE id = :group_id
                    """
                ),
                {"group_id": empty_group_id},
            ).scalar_one_or_none() is None

        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE product_groups
                    SET approved_category_source = 'machine_suggestion'
                    WHERE id = :group_id
                    """
                ),
                {"group_id": empty_group_id},
            )

        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        UPDATE product_groups
                        SET approved_category_source = 'reviewer_cleared'
                        WHERE id = :group_id
                        """
                    ),
                    {"group_id": group_id},
                )

        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        UPDATE product_groups
                        SET approved_category_source = 'reviewer_selection'
                        WHERE id = :group_id
                        """
                    ),
                    {"group_id": empty_group_id},
                )
    finally:
        engine.dispose()


def test_pgvector_extension_supports_embedding_vector_round_trip(
    empty_database_url: str,
) -> None:
    engine = _upgrade(empty_database_url)

    try:
        vector_table = Table(
            "vector_round_trip",
            MetaData(),
            Column("id", Integer, primary_key=True),
            Column(
                "embedding",
                image_embedding_vector_type(),
                nullable=False,
            ),
            prefixes=["TEMPORARY"],
        )
        sample_embedding = [
            index / EMBEDDING_DIMENSIONS
            for index in range(EMBEDDING_DIMENSIONS)
        ]

        with engine.begin() as connection:
            assert connection.execute(
                text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
            ).scalar_one() == "vector"

            vector_table.create(connection)
            connection.execute(
                vector_table.insert().values(id=1, embedding=sample_embedding)
            )
            persisted_embedding = connection.execute(
                select(vector_table.c.embedding).where(vector_table.c.id == 1)
            ).scalar_one()

        assert list(persisted_embedding) == pytest.approx(sample_embedding)
    finally:
        engine.dispose()


def test_downgrade_removes_all_application_tables(
    empty_database_url: str,
) -> None:
    engine = _upgrade(empty_database_url)
    engine.dispose()

    command.downgrade(_alembic_config(empty_database_url), "base")

    downgraded_engine = create_engine(empty_database_url)
    try:
        assert inspect(downgraded_engine).get_table_names() == [
            "alembic_version"
        ]
        with downgraded_engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).all() == []
            assert connection.execute(
                text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
            ).first() is None
    finally:
        downgraded_engine.dispose()
