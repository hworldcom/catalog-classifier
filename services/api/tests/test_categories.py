from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from catalog_api.models import Category
from catalog_api.upload_batches import DEFAULT_ORGANIZATION_ID

pytestmark = [pytest.mark.anyio, pytest.mark.postgresql]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_list_review_categories_returns_active_global_tree(
    database_client: AsyncClient,
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session:
        session.add_all(
            [
                Category(
                    organization_id=None,
                    parent_id=None,
                    slug="inactive-global",
                    name_pl="Inactive",
                    name_en="Inactive",
                    name_de="Inactive",
                    name_vi="Inactive",
                    active=False,
                ),
                Category(
                    organization_id=DEFAULT_ORGANIZATION_ID,
                    parent_id=None,
                    slug="organization-only",
                    name_pl="Organization only",
                    name_en="Organization only",
                    name_de="Organization only",
                    name_vi="Organization only",
                    active=True,
                ),
            ]
        )
        session.commit()

    response = await database_client.get("/v1/categories")

    assert response.status_code == 200
    categories = response.json()
    assert [category["slug"] for category in categories] == [
        "clothing",
        "hoodies",
        "jackets",
        "sportswear",
        "t-shirts",
        "trousers",
    ]
    clothing = categories[0]
    assert clothing["parentId"] is None
    assert clothing["nameEn"] == "Clothing"
    assert all(
        category["parentId"] == clothing["id"] for category in categories[1:]
    )
    assert "inactive-global" not in {category["slug"] for category in categories}
    assert "organization-only" not in {category["slug"] for category in categories}
