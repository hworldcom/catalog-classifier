from __future__ import annotations

import os
from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, MetaData, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DATABASE_URL_ENV = "CATALOG_DATABASE_URL"
DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://catalog:catalog@localhost:5432/catalog_classifier"
)

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def get_database_url() -> str:
    return os.getenv(DATABASE_URL_ENV, DEFAULT_DATABASE_URL)


@lru_cache
def get_engine() -> Engine:
    return create_engine(get_database_url(), pool_pre_ping=True)


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine())


def get_session() -> Iterator[Session]:
    with get_session_factory()() as session:
        yield session
