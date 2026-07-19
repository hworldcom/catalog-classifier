"""Add membership-level image rejection.

Revision ID: 0008_image_rejection
Revises: 0007_grouping_review
Create Date: 2026-07-18
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0008_image_rejection"
down_revision: str | None = "0007_grouping_review"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "product_group_images",
        sa.Column(
            "is_rejected",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("product_group_images", "is_rejected")
