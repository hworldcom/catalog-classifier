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
            "image_assets",
            "image_embeddings",
            "organizations",
            "processing_jobs",
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
