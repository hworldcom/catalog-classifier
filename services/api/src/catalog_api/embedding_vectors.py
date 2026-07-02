from __future__ import annotations

from pgvector.sqlalchemy import Vector

EMBEDDING_DIMENSIONS = 768


def image_embedding_vector_type() -> Vector:
    return Vector(EMBEDDING_DIMENSIONS)
