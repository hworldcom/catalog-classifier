from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from catalog_api.database import Base

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
    thumbnail_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    upload_order: Mapped[int] = mapped_column(Integer, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
