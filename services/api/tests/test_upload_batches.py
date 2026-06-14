from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

from catalog_api.database import get_session
from catalog_api.main import app
from catalog_api.models import UploadBatch
from catalog_api.upload_batches import DEFAULT_ORGANIZATION_ID

pytestmark = [pytest.mark.anyio, pytest.mark.postgresql]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_creates_separate_batches_with_database_defaults(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    first_response = await database_client.post("/v1/upload-batches")
    second_response = await database_client.post("/v1/upload-batches")

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    first_body = first_response.json()
    second_body = second_response.json()
    first_batch_id = UUID(first_body["batchId"])
    second_batch_id = UUID(second_body["batchId"])
    assert first_batch_id != second_batch_id
    assert first_body == {
        "batchId": str(first_batch_id),
        "status": "created",
        "maxFiles": 20,
    }

    with Session(migrated_engine) as session:
        batches = session.scalars(
            select(UploadBatch).order_by(UploadBatch.created_at)
        ).all()

    assert {batch.id for batch in batches} == {
        first_batch_id,
        second_batch_id,
    }
    assert all(
        batch.organization_id == DEFAULT_ORGANIZATION_ID
        for batch in batches
    )
    assert all(batch.status == "created" for batch in batches)
    assert all(batch.original_file_count == 0 for batch in batches)
    assert all(batch.processed_file_count == 0 for batch in batches)
    assert all(batch.created_by is None for batch in batches)
    assert all(batch.pipeline_version is None for batch in batches)
    assert all(batch.finalized_at is None for batch in batches)
    assert all(batch.completed_at is None for batch in batches)
    assert all(
        isinstance(batch.created_at, datetime)
        and batch.created_at.utcoffset() is not None
        for batch in batches
    )


async def test_returns_internal_server_error_when_schema_is_unavailable(
    empty_database_url: str,
) -> None:
    engine = create_engine(empty_database_url)

    def override_session() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post("/v1/upload-batches")
    finally:
        app.dependency_overrides.pop(get_session, None)
        engine.dispose()

    assert response.status_code == 500
    assert response.json() == {
        "detail": {
            "code": "database_error",
            "message": "Unable to create the upload batch.",
        }
    }
