"""Add processing job foundation.

Revision ID: 0002_processing_jobs
Revises: 0001_upload_foundation
Create Date: 2026-06-21
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0002_processing_jobs"
down_revision: str | None = "0001_upload_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROCESSING_JOB_STATUS_CHECK = (
    "status IN ('pending', 'started', 'completed', 'failed')"
)


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_image_assets_id_organization_id",
        "image_assets",
        ("id", "organization_id"),
    )
    op.create_table(
        "processing_jobs",
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
        sa.Column("image_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("job_type", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("pipeline_version", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="attempt_count_nonnegative",
        ),
        sa.CheckConstraint(
            PROCESSING_JOB_STATUS_CHECK,
            name="status",
        ),
        sa.ForeignKeyConstraint(
            ("batch_id", "organization_id"),
            ("upload_batches.id", "upload_batches.organization_id"),
            name="fk_processing_jobs_batch_organization_upload_batches",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ("image_id", "organization_id"),
            ("image_assets.id", "image_assets.organization_id"),
            name="fk_processing_jobs_image_organization_image_assets",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ("organization_id",),
            ("organizations.id",),
            name="fk_processing_jobs_organization_id_organizations",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_processing_jobs"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_processing_jobs_idempotency_key",
        ),
    )


def downgrade() -> None:
    op.drop_table("processing_jobs")
    op.drop_constraint(
        "uq_image_assets_id_organization_id",
        "image_assets",
        type_="unique",
    )
