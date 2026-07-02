"""Add perceptual hashes and image embeddings.

Revision ID: 0005_hashes_embeddings
Revises: 0004_pgvector_foundation
Create Date: 2026-07-01
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from catalog_api.embedding_vectors import (
    EMBEDDING_DIMENSIONS,
    image_embedding_vector_type,
)

revision: str = "0005_hashes_embeddings"
down_revision: str | None = "0004_pgvector_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "image_assets",
        sa.Column("phash", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "image_assets",
        sa.Column("dhash", sa.String(length=16), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_image_assets_phash_lower_hex"),
        "image_assets",
        "phash IS NULL OR phash ~ '^[0-9a-f]{16}$'",
    )
    op.create_check_constraint(
        op.f("ck_image_assets_dhash_lower_hex"),
        "image_assets",
        "dhash IS NULL OR dhash ~ '^[0-9a-f]{16}$'",
    )

    op.create_table(
        "image_embeddings",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("image_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("pipeline_version", sa.String(length=100), nullable=False),
        sa.Column("embedding", image_embedding_vector_type(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"dimensions = {EMBEDDING_DIMENSIONS}",
            name=op.f("ck_image_embeddings_dimensions_supported"),
        ),
        sa.ForeignKeyConstraint(
            ("image_id", "organization_id"),
            ("image_assets.id", "image_assets.organization_id"),
            name=op.f("fk_image_embeddings_image_organization_image_assets"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ("organization_id",),
            ("organizations.id",),
            name=op.f("fk_image_embeddings_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_image_embeddings")),
        sa.UniqueConstraint(
            "organization_id",
            "image_id",
            "pipeline_version",
            name="uq_image_embeddings_organization_image_pipeline_version",
        ),
    )


def downgrade() -> None:
    op.drop_table("image_embeddings")
    op.drop_constraint(
        op.f("ck_image_assets_dhash_lower_hex"),
        "image_assets",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_image_assets_phash_lower_hex"),
        "image_assets",
        type_="check",
    )
    op.drop_column("image_assets", "dhash")
    op.drop_column("image_assets", "phash")
