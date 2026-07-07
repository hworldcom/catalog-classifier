"""Add category taxonomy and classification schema.

Revision ID: 0006_category_schema
Revises: 0005_hashes_embeddings
Create Date: 2026-07-03
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0006_category_schema"
down_revision: str | None = "0005_hashes_embeddings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CLOTHING_CATEGORY_ID = "10000000-0000-0000-0000-000000000001"
SEEDED_CATEGORIES = (
    (CLOTHING_CATEGORY_ID, None, "clothing", "Clothing"),
    (
        "10000000-0000-0000-0000-000000000002",
        CLOTHING_CATEGORY_ID,
        "t-shirts",
        "T-shirts",
    ),
    (
        "10000000-0000-0000-0000-000000000003",
        CLOTHING_CATEGORY_ID,
        "hoodies",
        "Hoodies",
    ),
    (
        "10000000-0000-0000-0000-000000000004",
        CLOTHING_CATEGORY_ID,
        "trousers",
        "Trousers",
    ),
    (
        "10000000-0000-0000-0000-000000000005",
        CLOTHING_CATEGORY_ID,
        "jackets",
        "Jackets",
    ),
    (
        "10000000-0000-0000-0000-000000000006",
        CLOTHING_CATEGORY_ID,
        "sportswear",
        "Sportswear",
    ),
)


def upgrade() -> None:
    op.create_table(
        "categories",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("name_pl", sa.Text(), nullable=False),
        sa.Column("name_en", sa.Text(), nullable=False),
        sa.Column("name_de", sa.Text(), nullable=False),
        sa.Column("name_vi", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.ForeignKeyConstraint(
            ("organization_id",),
            ("organizations.id",),
            name=op.f("fk_categories_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ("parent_id",),
            ("categories.id",),
            name=op.f("fk_categories_parent_id_categories"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_categories")),
    )
    op.create_index(
        "uq_categories_global_slug",
        "categories",
        ["slug"],
        unique=True,
        postgresql_where=sa.text("organization_id IS NULL"),
    )
    op.create_index(
        "uq_categories_organization_slug",
        "categories",
        ["organization_id", "slug"],
        unique=True,
        postgresql_where=sa.text("organization_id IS NOT NULL"),
    )

    _seed_initial_categories()

    op.create_table(
        "image_classifications",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("image_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("attributes_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("raw_response_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("pipeline_version", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name=op.f("ck_image_classifications_confidence_range"),
        ),
        sa.ForeignKeyConstraint(
            ("category_id",),
            ("categories.id",),
            name=op.f("fk_image_classifications_category_id_categories"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ("image_id", "organization_id"),
            ("image_assets.id", "image_assets.organization_id"),
            name=op.f("fk_image_classifications_image_organization_image_assets"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ("organization_id",),
            ("organizations.id",),
            name=op.f("fk_image_classifications_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_image_classifications")),
        sa.UniqueConstraint(
            "organization_id",
            "image_id",
            "pipeline_version",
            name="uq_image_classifications_organization_image_pipeline_version",
        ),
    )


def downgrade() -> None:
    op.drop_table("image_classifications")
    op.drop_index(
        "uq_categories_organization_slug",
        table_name="categories",
        postgresql_where=sa.text("organization_id IS NOT NULL"),
    )
    op.drop_index(
        "uq_categories_global_slug",
        table_name="categories",
        postgresql_where=sa.text("organization_id IS NULL"),
    )
    op.drop_table("categories")


def _seed_initial_categories() -> None:
    categories_table = sa.table(
        "categories",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("organization_id", postgresql.UUID(as_uuid=True)),
        sa.column("parent_id", postgresql.UUID(as_uuid=True)),
        sa.column("slug", sa.String(length=100)),
        sa.column("name_pl", sa.Text()),
        sa.column("name_en", sa.Text()),
        sa.column("name_de", sa.Text()),
        sa.column("name_vi", sa.Text()),
        sa.column("active", sa.Boolean()),
    )
    op.bulk_insert(
        categories_table,
        [
            {
                "id": category_id,
                "organization_id": None,
                "parent_id": parent_id,
                "slug": slug,
                "name_pl": label,
                "name_en": label,
                "name_de": label,
                "name_vi": label,
                "active": True,
            }
            for category_id, parent_id, slug, label in SEEDED_CATEGORIES
        ],
    )
