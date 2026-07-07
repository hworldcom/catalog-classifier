from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from catalog_api.database import Base
from catalog_api.embedding_vectors import (
    EMBEDDING_DIMENSIONS,
    image_embedding_vector_type,
)

BATCH_STATUSES = (
    "created",
    "uploading",
    "queued",
    "processing",
    "review_required",
    "approved",
    "failed",
    "cancelled",
)
IMAGE_STATUSES = (
    "pending",
    "uploaded",
    "processing",
    "processed",
    "failed",
)
PROCESSING_JOB_STATUSES = (
    "pending",
    "started",
    "completed",
    "failed",
)


def _status_check(column_name: str, values: tuple[str, ...]) -> str:
    allowed_values = ", ".join(f"'{value}'" for value in values)
    return f"{column_name} IN ({allowed_values})"


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class UploadBatch(Base):
    __tablename__ = "upload_batches"
    __table_args__ = (
        UniqueConstraint(
            "id",
            "organization_id",
            name="uq_upload_batches_id_organization_id",
        ),
        CheckConstraint(
            _status_check("status", BATCH_STATUSES),
            name="status",
        ),
        CheckConstraint(
            "original_file_count >= 0",
            name="original_file_count_nonnegative",
        ),
        CheckConstraint(
            "processed_file_count >= 0",
            name="processed_file_count_nonnegative",
        ),
        CheckConstraint(
            "processed_file_count <= original_file_count",
            name="processed_within_original",
        ),
        Index(
            "ix_upload_batches_organization_id_created_at",
            "organization_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_by: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=text("'created'"),
    )
    original_file_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    processed_file_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    pipeline_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    finalized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class ImageAsset(Base):
    __tablename__ = "image_assets"
    __table_args__ = (
        ForeignKeyConstraint(
            ("batch_id", "organization_id"),
            ("upload_batches.id", "upload_batches.organization_id"),
            name="fk_image_assets_batch_organization_upload_batches",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "organization_id",
            "batch_id",
            "upload_order",
            name="uq_image_assets_organization_batch_upload_order",
        ),
        UniqueConstraint(
            "organization_id",
            "original_object_key",
            name="uq_image_assets_organization_original_object_key",
        ),
        UniqueConstraint(
            "organization_id",
            "thumbnail_object_key",
            name="uq_image_assets_organization_thumbnail_object_key",
        ),
        UniqueConstraint(
            "id",
            "organization_id",
            name="uq_image_assets_id_organization_id",
        ),
        CheckConstraint(
            _status_check("status", IMAGE_STATUSES),
            name="status",
        ),
        CheckConstraint(
            "upload_order >= 0",
            name="upload_order_nonnegative",
        ),
        CheckConstraint(
            "size_bytes >= 0",
            name="size_bytes_nonnegative",
        ),
        CheckConstraint(
            "width IS NULL OR width >= 0",
            name="width_nonnegative",
        ),
        CheckConstraint(
            "height IS NULL OR height >= 0",
            name="height_nonnegative",
        ),
        CheckConstraint(
            "normalized_size_bytes IS NULL OR normalized_size_bytes >= 0",
            name="normalized_size_bytes_nonnegative",
        ),
        CheckConstraint(
            "phash IS NULL OR phash ~ '^[0-9a-f]{16}$'",
            name="phash_lower_hex",
        ),
        CheckConstraint(
            "dhash IS NULL OR dhash ~ '^[0-9a-f]{16}$'",
            name="dhash_lower_hex",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    batch_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        nullable=False,
    )
    original_object_key: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    inference_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    thumbnail_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    upload_order: Mapped[int] = mapped_column(Integer, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    normalized_format: Mapped[str | None] = mapped_column(String(32), nullable=True)
    normalized_size_bytes: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    phash: Mapped[str | None] = mapped_column(String(16), nullable=True)
    dhash: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=text("'pending'"),
    )
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ("batch_id", "organization_id"),
            ("upload_batches.id", "upload_batches.organization_id"),
            name="fk_processing_jobs_batch_organization_upload_batches",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ("image_id", "organization_id"),
            ("image_assets.id", "image_assets.organization_id"),
            name="fk_processing_jobs_image_organization_image_assets",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_processing_jobs_idempotency_key",
        ),
        CheckConstraint(
            _status_check("status", PROCESSING_JOB_STATUSES),
            name="status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="attempt_count_nonnegative",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    batch_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        nullable=False,
    )
    image_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        nullable=True,
    )
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=text("'pending'"),
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    pipeline_version: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)


class ImageEmbedding(Base):
    __tablename__ = "image_embeddings"
    __table_args__ = (
        ForeignKeyConstraint(
            ("image_id", "organization_id"),
            ("image_assets.id", "image_assets.organization_id"),
            name="fk_image_embeddings_image_organization_image_assets",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "organization_id",
            "image_id",
            "pipeline_version",
            name="uq_image_embeddings_organization_image_pipeline_version",
        ),
        CheckConstraint(
            f"dimensions = {EMBEDDING_DIMENSIONS}",
            name="dimensions_supported",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    image_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    pipeline_version: Mapped[str] = mapped_column(String(100), nullable=False)
    embedding: Mapped[list[float]] = mapped_column(
        image_embedding_vector_type(),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (
        ForeignKeyConstraint(
            ("parent_id",),
            ("categories.id",),
            name="fk_categories_parent_id_categories",
            ondelete="RESTRICT",
        ),
        Index(
            "uq_categories_global_slug",
            "slug",
            unique=True,
            postgresql_where=text("organization_id IS NULL"),
        ),
        Index(
            "uq_categories_organization_slug",
            "organization_id",
            "slug",
            unique=True,
            postgresql_where=text("organization_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    parent_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        nullable=True,
    )
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    name_pl: Mapped[str] = mapped_column(Text, nullable=False)
    name_en: Mapped[str] = mapped_column(Text, nullable=False)
    name_de: Mapped[str] = mapped_column(Text, nullable=False)
    name_vi: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )


class ImageClassification(Base):
    __tablename__ = "image_classifications"
    __table_args__ = (
        ForeignKeyConstraint(
            ("image_id", "organization_id"),
            ("image_assets.id", "image_assets.organization_id"),
            name="fk_image_classifications_image_organization_image_assets",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "organization_id",
            "image_id",
            "pipeline_version",
            name="uq_image_classifications_organization_image_pipeline_version",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="confidence_range",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    image_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        nullable=False,
    )
    category_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("categories.id", ondelete="RESTRICT"),
        nullable=True,
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    attributes_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    raw_response_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    pipeline_version: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
