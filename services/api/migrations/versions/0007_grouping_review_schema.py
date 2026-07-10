"""Add grouping schema and review read model tables.

Revision ID: 0007_grouping_review
Revises: 0006_category_schema
Create Date: 2026-07-10
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0007_grouping_review"
down_revision: str | None = "0006_category_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRODUCT_GROUP_STATUSES = ("proposed", "approved", "rejected")
PAIR_ASSESSMENT_DECISIONS = ("same_product", "different_product", "uncertain")


def _allowed_values(column_name: str, values: tuple[str, ...]) -> str:
    formatted_values = ", ".join(f"'{value}'" for value in values)
    return f"{column_name} IN ({formatted_values})"


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_image_assets_id_organization_batch",
        "image_assets",
        ["id", "organization_id", "batch_id"],
    )

    op.create_table(
        "pair_assessments",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("image_a_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("image_b_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("embedding_similarity", sa.Float(), nullable=True),
        sa.Column("phash_distance", sa.Integer(), nullable=True),
        sa.Column("category_match", sa.Boolean(), nullable=True),
        sa.Column("upload_order_distance", sa.Integer(), nullable=True),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("decision_source", sa.String(length=100), nullable=False),
        sa.Column("pipeline_version", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            _allowed_values("decision", PAIR_ASSESSMENT_DECISIONS),
            name=op.f("ck_pair_assessments_decision"),
        ),
        sa.CheckConstraint(
            "image_a_id < image_b_id",
            name=op.f("ck_pair_assessments_canonical_image_order"),
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name=op.f("ck_pair_assessments_confidence_range"),
        ),
        sa.CheckConstraint(
            "phash_distance IS NULL OR phash_distance >= 0",
            name=op.f("ck_pair_assessments_phash_distance_nonnegative"),
        ),
        sa.CheckConstraint(
            "upload_order_distance IS NULL OR upload_order_distance >= 0",
            name=op.f("ck_pair_assessments_upload_order_distance_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ("batch_id", "organization_id"),
            ("upload_batches.id", "upload_batches.organization_id"),
            name=op.f("fk_pair_assessments_batch_organization_upload_batches"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ("image_a_id", "organization_id", "batch_id"),
            ("image_assets.id", "image_assets.organization_id", "image_assets.batch_id"),
            name=op.f("fk_pair_assessments_image_a_organization_batch_image_assets"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ("image_b_id", "organization_id", "batch_id"),
            ("image_assets.id", "image_assets.organization_id", "image_assets.batch_id"),
            name=op.f("fk_pair_assessments_image_b_organization_batch_image_assets"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ("organization_id",),
            ("organizations.id",),
            name=op.f("fk_pair_assessments_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pair_assessments")),
        sa.UniqueConstraint(
            "organization_id",
            "batch_id",
            "image_a_id",
            "image_b_id",
            "pipeline_version",
            name="uq_pair_assessments_organization_batch_pair_pipeline",
        ),
    )

    op.create_table(
        "product_groups",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'proposed'"),
            nullable=False,
        ),
        sa.Column("suggested_category_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_category_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("cover_image_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("possible_existing_product_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            _allowed_values("status", PRODUCT_GROUP_STATUSES),
            name=op.f("ck_product_groups_status"),
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name=op.f("ck_product_groups_confidence_range"),
        ),
        sa.ForeignKeyConstraint(
            ("approved_category_id",),
            ("categories.id",),
            name=op.f("fk_product_groups_approved_category_id_categories"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ("batch_id", "organization_id"),
            ("upload_batches.id", "upload_batches.organization_id"),
            name=op.f("fk_product_groups_batch_organization_upload_batches"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ("cover_image_id", "organization_id", "batch_id"),
            ("image_assets.id", "image_assets.organization_id", "image_assets.batch_id"),
            name=op.f("fk_product_groups_cover_image_organization_batch_image_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ("organization_id",),
            ("organizations.id",),
            name=op.f("fk_product_groups_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ("suggested_category_id",),
            ("categories.id",),
            name=op.f("fk_product_groups_suggested_category_id_categories"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_product_groups")),
        sa.UniqueConstraint(
            "id",
            "organization_id",
            "batch_id",
            name="uq_product_groups_id_organization_batch",
        ),
    )

    op.create_table(
        "product_group_images",
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("image_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("membership_source", sa.String(length=64), nullable=False),
        sa.Column("membership_confidence", sa.Float(), nullable=True),
        sa.Column(
            "is_duplicate",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("duplicate_of_image_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "position >= 0",
            name=op.f("ck_product_group_images_position_nonnegative"),
        ),
        sa.CheckConstraint(
            "membership_confidence IS NULL OR "
            "(membership_confidence >= 0 AND membership_confidence <= 1)",
            name=op.f("ck_product_group_images_membership_confidence_range"),
        ),
        sa.CheckConstraint(
            "duplicate_of_image_id IS NULL OR duplicate_of_image_id <> image_id",
            name=op.f("ck_product_group_images_duplicate_not_self"),
        ),
        sa.ForeignKeyConstraint(
            ("duplicate_of_image_id", "organization_id", "batch_id"),
            ("image_assets.id", "image_assets.organization_id", "image_assets.batch_id"),
            name=op.f("fk_product_group_images_duplicate_image_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ("group_id", "organization_id", "batch_id"),
            ("product_groups.id", "product_groups.organization_id", "product_groups.batch_id"),
            name=op.f("fk_product_group_images_group_organization_batch_product_groups"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ("image_id", "organization_id", "batch_id"),
            ("image_assets.id", "image_assets.organization_id", "image_assets.batch_id"),
            name=op.f("fk_product_group_images_image_organization_batch_image_assets"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ("organization_id",),
            ("organizations.id",),
            name=op.f("fk_product_group_images_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "group_id",
            "image_id",
            name=op.f("pk_product_group_images"),
        ),
        sa.UniqueConstraint(
            "group_id",
            "position",
            name="uq_product_group_images_group_position",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "batch_id",
            "image_id",
            name="uq_product_group_images_organization_batch_image",
        ),
    )

    op.create_table(
        "review_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("image_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action_type", sa.String(length=100), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ("batch_id", "organization_id"),
            ("upload_batches.id", "upload_batches.organization_id"),
            name=op.f("fk_review_events_batch_organization_upload_batches"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ("organization_id",),
            ("organizations.id",),
            name=op.f("fk_review_events_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_events")),
    )


def downgrade() -> None:
    op.drop_table("review_events")
    op.drop_table("product_group_images")
    op.drop_table("product_groups")
    op.drop_table("pair_assessments")
    op.drop_constraint(
        "uq_image_assets_id_organization_batch",
        "image_assets",
        type_="unique",
    )
