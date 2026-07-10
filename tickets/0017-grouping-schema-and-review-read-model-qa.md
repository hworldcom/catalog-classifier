# Ticket 0017 Quality Assurance: Grouping Schema and Review Read Model

Run these steps from a clean shell unless a step says otherwise.

## Objective

Manually verify that ticket 0017:

- applies the grouping and review database schema;
- exposes `GET /v1/upload-batches/{batchId}/groups`;
- returns a stable read-only review snapshot for `review_required` batches;
- returns `groups: []` for review-ready batches with no groups;
- rejects non-review batches with `409 batch_not_review_ready`;
- keeps schema constraints covered by deterministic backend tests.

This ticket does not include the grouping engine. The manual endpoint test below
therefore seeds review-ready rows directly in PostgreSQL.

## Expected Environment

These instructions use:

- repository: `/Users/hoangdeveloper/catalog-classifier`;
- PostgreSQL database: `catalog_classifier`;
- PostgreSQL user and password: `catalog`;
- API base URL: `http://localhost:8000`;
- pipeline version: `2026-06-01`;
- default organization:
  `00000000-0000-0000-0000-000000000001`.

## 1. Start PostgreSQL

```bash
cd /Users/hoangdeveloper/catalog-classifier
docker compose up -d postgres
docker compose ps postgres
```

Expected result:

- the `postgres` service is `healthy`;
- local port `5432` is available.

## 2. Install Backend Dependencies

```bash
cd /Users/hoangdeveloper/catalog-classifier/services/api
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

Expected result:

- editable `catalog-classifier-api` installs successfully.

## 3. Run Deterministic Backend Tests

These tests use temporary PostgreSQL databases and do not call Google Cloud
Storage or Gemini.

```bash
cd /Users/hoangdeveloper/catalog-classifier/services/api

CATALOG_TEST_DATABASE_URL=postgresql+psycopg://catalog:catalog@localhost:5432/postgres \
  .venv/bin/pytest tests/test_migrations.py tests/test_review_groups.py -q
```

Expected result:

```text
11 passed
```

Optional full API regression check:

```bash
CATALOG_TEST_DATABASE_URL=postgresql+psycopg://catalog:catalog@localhost:5432/postgres \
  .venv/bin/pytest -q
```

Expected result:

```text
106 passed
```

## 4. Apply Migrations to the Local Database

```bash
cd /Users/hoangdeveloper/catalog-classifier/services/api

export CATALOG_DATABASE_URL=postgresql+psycopg://catalog:catalog@localhost:5432/catalog_classifier
.venv/bin/alembic upgrade head
```

Expected result:

- Alembic upgrades the local database to `head`;
- no migration error is printed.

Confirm the new tables exist:

```bash
cd /Users/hoangdeveloper/catalog-classifier

docker compose exec postgres psql \
  -U catalog \
  -d catalog_classifier \
  -c "
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN (
    'pair_assessments',
    'product_groups',
    'product_group_images',
    'review_events'
  )
ORDER BY table_name;
"
```

Expected result:

```text
      table_name
------------------------
 pair_assessments
 product_group_images
 product_groups
 review_events
```

## 5. Start the API

In one terminal:

```bash
cd /Users/hoangdeveloper/catalog-classifier/services/api

export CATALOG_DATABASE_URL=postgresql+psycopg://catalog:catalog@localhost:5432/catalog_classifier
export CATALOG_UPLOAD_BUCKET=lnlabs-bucket
export CATALOG_SIGNING_SERVICE_ACCOUNT="catalog-api@catalog-classifier.iam.gserviceaccount.com"
export GEMINI_API_KEY="<your Gemini API key>"

.venv/bin/uvicorn catalog_api.main:app --reload --port 8000
```

Expected result:

- the API starts on `http://localhost:8000`;
- no import or migration error is printed.

In a second terminal:

```bash
export API_BASE=http://localhost:8000
```

## 6. Seed a Review-Ready Batch With One Proposed Group

Because ticket 0018 has not implemented automatic grouping yet, seed one
review-ready batch manually:

```bash
cd /Users/hoangdeveloper/catalog-classifier

export BATCH_ID=$(
  docker compose exec -T postgres psql \
    -U catalog \
    -d catalog_classifier \
    -tA \
    -v ON_ERROR_STOP=1 \
    -c "
WITH batch AS (
  INSERT INTO upload_batches (
    organization_id,
    status,
    original_file_count,
    processed_file_count,
    pipeline_version,
    finalized_at
  )
  VALUES (
    '00000000-0000-0000-0000-000000000001',
    'review_required',
    2,
    2,
    '2026-06-01',
    now()
  )
  RETURNING id, organization_id
),
front_image AS (
  INSERT INTO image_assets (
    id,
    organization_id,
    batch_id,
    original_object_key,
    thumbnail_object_key,
    original_filename,
    upload_order,
    mime_type,
    size_bytes,
    status
  )
  SELECT
    gen_random_uuid(),
    organization_id,
    id,
    'qa/0017/front-' || id::text || '.jpg',
    'qa/0017/front-thumb-' || id::text || '.jpg',
    'front.jpg',
    0,
    'image/jpeg',
    100,
    'processed'
  FROM batch
  RETURNING id, organization_id, batch_id
),
back_image AS (
  INSERT INTO image_assets (
    id,
    organization_id,
    batch_id,
    original_object_key,
    thumbnail_object_key,
    original_filename,
    upload_order,
    mime_type,
    size_bytes,
    status
  )
  SELECT
    gen_random_uuid(),
    organization_id,
    id,
    'qa/0017/back-' || id::text || '.jpg',
    'qa/0017/back-thumb-' || id::text || '.jpg',
    'back.jpg',
    1,
    'image/jpeg',
    101,
    'processed'
  FROM batch
  RETURNING id, organization_id, batch_id
),
category AS (
  SELECT id FROM categories WHERE slug = 'sportswear'
),
product_group AS (
  INSERT INTO product_groups (
    organization_id,
    batch_id,
    status,
    suggested_category_id,
    cover_image_id,
    confidence
  )
  SELECT
    batch.organization_id,
    batch.id,
    'proposed',
    category.id,
    front_image.id,
    0.93
  FROM batch, category, front_image
  RETURNING id, organization_id, batch_id
),
members AS (
  INSERT INTO product_group_images (
    organization_id,
    batch_id,
    group_id,
    image_id,
    position,
    membership_source,
    membership_confidence,
    is_duplicate,
    duplicate_of_image_id
  )
  SELECT
    product_group.organization_id,
    product_group.batch_id,
    product_group.id,
    front_image.id,
    0,
    'engine',
    0.94,
    false,
    NULL
  FROM product_group, front_image
  UNION ALL
  SELECT
    product_group.organization_id,
    product_group.batch_id,
    product_group.id,
    back_image.id,
    1,
    'engine',
    0.91,
    true,
    front_image.id
  FROM product_group, back_image, front_image
  RETURNING 1
)
SELECT id FROM batch;
"
)

printf 'BATCH_ID=%s\n' "$BATCH_ID"
```

Expected result:

- `BATCH_ID` prints one UUID.

## 7. Read the Review Snapshot

```bash
curl -fsS "$API_BASE/v1/upload-batches/$BATCH_ID/groups" \
  | tee /tmp/catalog-0017-review-groups.json \
  | python3 -m json.tool
```

Expected result:

- `status` is `review_required`;
- `pipelineVersion` is `2026-06-01`;
- `groups` contains one group;
- the group `status` is `proposed`;
- `suggestedCategorySlug` is `sportswear`;
- `approvedCategorySlug` is `null`;
- `warnings` is an empty list;
- the group contains two images ordered by `position`;
- the first image is `front.jpg`;
- the second image is `back.jpg` and has `isDuplicate: true`;
- each image has a `thumbnailUrl` in this shape:
  `/v1/upload-batches/{batchId}/images/{imageId}/thumbnail`.

Run these assertions:

```bash
python3 - <<'PY'
import json
import os

with open("/tmp/catalog-0017-review-groups.json") as file:
    snapshot = json.load(file)

assert snapshot["batchId"] == os.environ["BATCH_ID"]
assert snapshot["status"] == "review_required"
assert snapshot["pipelineVersion"] == "2026-06-01"
assert len(snapshot["groups"]) == 1

group = snapshot["groups"][0]
assert group["status"] == "proposed"
assert group["confidence"] == 0.93
assert group["suggestedCategorySlug"] == "sportswear"
assert group["approvedCategorySlug"] is None
assert group["possibleExistingProductId"] is None
assert group["warnings"] == []

images = group["images"]
assert [image["originalFilename"] for image in images] == ["front.jpg", "back.jpg"]
assert [image["position"] for image in images] == [0, 1]
assert images[0]["isDuplicate"] is False
assert images[1]["isDuplicate"] is True
assert images[1]["duplicateOfImageId"] == images[0]["imageId"]
assert all(
    image["thumbnailUrl"]
    == f"/v1/upload-batches/{snapshot['batchId']}/images/{image['imageId']}/thumbnail"
    for image in images
)

print("review snapshot assertions passed")
PY
```

Expected result:

```text
review snapshot assertions passed
```

## 8. Verify the Read Endpoint Is Stable

```bash
curl -fsS "$API_BASE/v1/upload-batches/$BATCH_ID/groups" \
  > /tmp/catalog-0017-review-groups-second.json

diff \
  /tmp/catalog-0017-review-groups.json \
  /tmp/catalog-0017-review-groups-second.json
```

Expected result:

- `diff` prints no output.

## 9. Verify Empty Review-Ready Batches Return `groups: []`

```bash
export EMPTY_BATCH_ID=$(
  docker compose exec -T postgres psql \
    -U catalog \
    -d catalog_classifier \
    -tA \
    -v ON_ERROR_STOP=1 \
    -c "
WITH inserted_batch AS (
  INSERT INTO upload_batches (
    organization_id,
    status,
    original_file_count,
    processed_file_count,
    pipeline_version,
    finalized_at
  )
  VALUES (
    '00000000-0000-0000-0000-000000000001',
    'review_required',
    0,
    0,
    '2026-06-01',
    now()
  )
  RETURNING id
)
SELECT id FROM inserted_batch;
"
)

curl -fsS "$API_BASE/v1/upload-batches/$EMPTY_BATCH_ID/groups" \
  | tee /tmp/catalog-0017-empty-groups.json \
  | python3 -m json.tool
```

Expected result:

- `status` is `review_required`;
- `groups` is an empty list.

Confirm with:

```bash
python3 - <<'PY'
import json

with open("/tmp/catalog-0017-empty-groups.json") as file:
    snapshot = json.load(file)

assert snapshot["status"] == "review_required"
assert snapshot["groups"] == []
print("empty review snapshot assertions passed")
PY
```

Expected result:

```text
empty review snapshot assertions passed
```

## 10. Verify Non-Review Batches Return `409`

Create a normal upload batch through the API:

```bash
export CREATED_BATCH_ID=$(
  curl -fsS -X POST "$API_BASE/v1/upload-batches" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["batchId"])'
)

printf 'CREATED_BATCH_ID=%s\n' "$CREATED_BATCH_ID"
```

Call the groups endpoint:

```bash
export REVIEW_STATUS=$(
  curl -sS \
    -o /tmp/catalog-0017-not-ready.json \
    -w '%{http_code}' \
    "$API_BASE/v1/upload-batches/$CREATED_BATCH_ID/groups"
)

printf 'REVIEW_STATUS=%s\n' "$REVIEW_STATUS"
python3 -m json.tool /tmp/catalog-0017-not-ready.json
```

Expected result:

```text
REVIEW_STATUS=409
```

The response body should contain:

```json
{
  "detail": {
    "code": "batch_not_review_ready",
    "message": "Upload batch has not entered the review phase."
  }
}
```

## 11. Verify Unknown Batches Return `404`

```bash
export UNKNOWN_BATCH_ID=$(python3 -c 'import uuid; print(uuid.uuid4())')

export UNKNOWN_STATUS=$(
  curl -sS \
    -o /tmp/catalog-0017-unknown.json \
    -w '%{http_code}' \
    "$API_BASE/v1/upload-batches/$UNKNOWN_BATCH_ID/groups"
)

printf 'UNKNOWN_STATUS=%s\n' "$UNKNOWN_STATUS"
python3 -m json.tool /tmp/catalog-0017-unknown.json
```

Expected result:

```text
UNKNOWN_STATUS=404
```

The response body should contain:

```json
{
  "detail": {
    "code": "batch_not_found"
  }
}
```

## 12. Optional Constraint Smoke Test

The automated migration tests already cover constraints. If you want one manual
database smoke test, try inserting a non-canonical pair:

```bash
cd /Users/hoangdeveloper/catalog-classifier

docker compose exec postgres psql \
  -U catalog \
  -d catalog_classifier \
  -v ON_ERROR_STOP=1 \
  -c "
WITH images AS (
  SELECT id
  FROM image_assets
  WHERE batch_id = '$BATCH_ID'::uuid
  ORDER BY id
),
ordered AS (
  SELECT
    min(id) AS image_a_id,
    max(id) AS image_b_id
  FROM images
)
INSERT INTO pair_assessments (
  organization_id,
  batch_id,
  image_a_id,
  image_b_id,
  decision,
  decision_source,
  pipeline_version
)
SELECT
  '00000000-0000-0000-0000-000000000001',
  '$BATCH_ID'::uuid,
  image_b_id,
  image_a_id,
  'same_product',
  'manual-qa',
  '2026-06-01'
FROM ordered;
"
```

Expected result:

- PostgreSQL rejects the insert with a check constraint error for canonical
  image order.

This failure is expected and confirms the database rejects reversed image pairs.
