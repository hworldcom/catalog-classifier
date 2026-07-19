"""Add approved category source tracking.

Revision ID: 0009_group_category_prefill
Revises: 0008_image_rejection
Create Date: 2026-07-19
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0009_group_category_prefill"
down_revision: str | None = "0008_image_rejection"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPROVED_CATEGORY_SOURCES = (
    "machine_suggestion",
    "reviewer_selection",
    "reviewer_cleared",
)


def _allowed_values(column_name: str, values: tuple[str, ...]) -> str:
    formatted_values = ", ".join(f"'{value}'" for value in values)
    return f"{column_name} IN ({formatted_values})"


def upgrade() -> None:
    op.add_column(
        "product_groups",
        sa.Column(
            "approved_category_source",
            sa.String(length=32),
            nullable=True,
        ),
    )
    op.execute(
        sa.text(
            """
            UPDATE product_groups
            SET approved_category_source = 'reviewer_selection'
            WHERE approved_category_id IS NOT NULL
            """
        )
    )
    op.create_check_constraint(
        op.f("ck_product_groups_approved_category_source"),
        "product_groups",
        "approved_category_source IS NULL OR "
        + _allowed_values(
            "approved_category_source",
            APPROVED_CATEGORY_SOURCES,
        ),
    )
    op.create_check_constraint(
        op.f("ck_product_groups_approved_category_source_consistency"),
        "product_groups",
        "(approved_category_source IS NULL AND approved_category_id IS NULL) "
        "OR approved_category_source = 'machine_suggestion' "
        "OR (approved_category_source = 'reviewer_selection' "
        "AND approved_category_id IS NOT NULL) "
        "OR (approved_category_source = 'reviewer_cleared' "
        "AND approved_category_id IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_product_groups_approved_category_source_consistency"),
        "product_groups",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_product_groups_approved_category_source"),
        "product_groups",
        type_="check",
    )
    op.drop_column("product_groups", "approved_category_source")
