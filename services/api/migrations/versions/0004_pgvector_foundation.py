"""Enable pgvector extension.

Revision ID: 0004_pgvector_foundation
Revises: 0003_image_derivatives
Create Date: 2026-06-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_pgvector_foundation"
down_revision: str | None = "0003_image_derivatives"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS vector")
