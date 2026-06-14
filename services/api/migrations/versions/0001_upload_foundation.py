"""Create the upload foundation schema.

Revision ID: 0001_upload_foundation
Revises:
Create Date: 2026-06-07
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001_upload_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_ORGANIZATION_ID = "00000000-0000-0000-0000-000000000001"

BATCH_STATUS_CHECK = (
    "status IN ('created', 'uploading', 'queued', 'processing', "
    "'review_required', 'approved', 'failed', 'cancelled')"
)
IMAGE_STATUS_CHECK = (
    "status IN ('pending', 'uploaded', 'processing', 'processed', 'failed')"
)


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_organizations"),
    )

    op.execute(
        sa.text(
            """
            INSERT INTO organizations (id, name)
            VALUES (CAST(:organization_id AS uuid), 'Default Organization')
            ON CONFLICT (id) DO UPDATE
            SET name = EXCLUDED.name
            """
        ).bindparams(organization_id=DEFAULT_ORGANIZATION_ID)
    )

    op.create_table(
        "upload_batches",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'created'"),
            nullable=False,
        ),
        sa.Column(
            "original_file_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "processed_file_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("pipeline_version", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "original_file_count >= 0",
            name="original_file_count_nonnegative",
        ),
        sa.CheckConstraint(
            "processed_file_count >= 0",
            name="processed_file_count_nonnegative",
        ),
        sa.CheckConstraint(
            "processed_file_count <= original_file_count",
            name="processed_within_original",
        ),
        sa.CheckConstraint(
            BATCH_STATUS_CHECK,
            name="status",
        ),
        sa.ForeignKeyConstraint(
            ("organization_id",),
            ("organizations.id",),
            name="fk_upload_batches_organization_id_organizations",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_upload_batches"),
        sa.UniqueConstraint(
            "id",
            "organization_id",
            name="uq_upload_batches_id_organization_id",
        ),
    )
    op.create_index(
        "ix_upload_batches_organization_id_created_at",
        "upload_batches",
        ("organization_id", "created_at"),
        unique=False,
    )

    op.create_table(
        "image_assets",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("original_object_key", sa.Text(), nullable=False),
        sa.Column("thumbnail_object_key", sa.Text(), nullable=True),
        sa.Column("original_filename", sa.Text(), nullable=False),
        sa.Column("upload_order", sa.Integer(), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "height IS NULL OR height >= 0",
            name="height_nonnegative",
        ),
        sa.CheckConstraint(
            "size_bytes >= 0",
            name="size_bytes_nonnegative",
        ),
        sa.CheckConstraint(
            IMAGE_STATUS_CHECK,
            name="status",
        ),
        sa.CheckConstraint(
            "upload_order >= 0",
            name="upload_order_nonnegative",
        ),
        sa.CheckConstraint(
            "width IS NULL OR width >= 0",
            name="width_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ("batch_id", "organization_id"),
            ("upload_batches.id", "upload_batches.organization_id"),
            name="fk_image_assets_batch_organization_upload_batches",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ("organization_id",),
            ("organizations.id",),
            name="fk_image_assets_organization_id_organizations",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_image_assets"),
        sa.UniqueConstraint(
            "organization_id",
            "batch_id",
            "upload_order",
            name="uq_image_assets_organization_batch_upload_order",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "original_object_key",
            name="uq_image_assets_organization_original_object_key",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "thumbnail_object_key",
            name="uq_image_assets_organization_thumbnail_object_key",
        ),
    )


def downgrade() -> None:
    op.drop_table("image_assets")
    op.drop_index(
        "ix_upload_batches_organization_id_created_at",
        table_name="upload_batches",
    )
    op.drop_table("upload_batches")
    op.execute(
        sa.text(
            "DELETE FROM organizations "
            "WHERE id = CAST(:organization_id AS uuid)"
        ).bindparams(organization_id=DEFAULT_ORGANIZATION_ID)
    )
    op.drop_table("organizations")
