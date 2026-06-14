from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from catalog_api.database import get_session
from catalog_api.main import app

TEST_DATABASE_URL_ENV = "CATALOG_TEST_DATABASE_URL"
API_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def empty_database_url() -> Iterator[str]:
    configured_url = os.getenv(TEST_DATABASE_URL_ENV)
    if not configured_url:
        pytest.skip(f"{TEST_DATABASE_URL_ENV} is not configured")

    admin_url = make_url(configured_url)
    database_name = f"catalog_classifier_test_{uuid4().hex}"
    database_url = admin_url.set(database=database_name).render_as_string(
        hide_password=False
    )
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")

    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))

    try:
        yield database_url
    finally:
        with admin_engine.connect() as connection:
            connection.execute(
                text(
                    """
                    SELECT pg_terminate_backend(pid)
                    FROM pg_stat_activity
                    WHERE datname = :database_name
                      AND pid <> pg_backend_pid()
                    """
                ),
                {"database_name": database_name},
            )
            connection.execute(text(f'DROP DATABASE "{database_name}"'))
        admin_engine.dispose()


@pytest.fixture
def migrated_engine(empty_database_url: str) -> Iterator[Engine]:
    config = Config(API_ROOT / "alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        empty_database_url.replace("%", "%%"),
    )
    command.upgrade(config, "head")

    engine = create_engine(empty_database_url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
async def database_client(
    migrated_engine: Engine,
) -> AsyncIterator[AsyncClient]:
    def override_session() -> Iterator[Session]:
        with Session(migrated_engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_session, None)
