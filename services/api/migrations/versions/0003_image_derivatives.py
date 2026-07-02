"""Add image derivative metadata.

Revision ID: 0003_image_derivatives
Revises: 0002_processing_jobs
Create Date: 2026-06-28
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0003_image_derivatives"
down_revision: str | None = "0002_processing_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "image_assets",
        sa.Column("normalized_object_key", sa.Text(), nullable=True),
    )
    op.add_column(
        "image_assets",
        sa.Column("inference_object_key", sa.Text(), nullable=True),
    )
    op.add_column(
        "image_assets",
        sa.Column("normalized_format", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "image_assets",
        sa.Column("normalized_size_bytes", sa.BigInteger(), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_image_assets_normalized_size_bytes_nonnegative"),
        "image_assets",
        "normalized_size_bytes IS NULL OR normalized_size_bytes >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_image_assets_normalized_size_bytes_nonnegative"),
        "image_assets",
        type_="check",
    )
    op.drop_column("image_assets", "normalized_size_bytes")
    op.drop_column("image_assets", "normalized_format")
    op.drop_column("image_assets", "inference_object_key")
    op.drop_column("image_assets", "normalized_object_key")
