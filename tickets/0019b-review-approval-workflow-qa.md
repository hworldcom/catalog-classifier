# Ticket 0019b QA: Review Approval Workflow

Run these steps from a clean shell unless a step says otherwise.

## Objective

Manually verify that ticket 0019b:

- exposes group and batch approval endpoints;
- approves individual groups;
- blocks batch approval while any group is still proposed;
- approves the batch once every group is approved;
- keeps approved review snapshots readable;
- rejects review edits after approval;
- writes approval `review_events` rows;
- treats repeated approval requests as no-op successes without new events.

Ticket 0019b is backend-only. It does not include review page user interface
work.

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

If `.venv` already exists, rerun only:

```bash
.venv/bin/python -m pip install -e '.[dev]'
```

## 3. Run Automated Backend Tests

These tests use temporary PostgreSQL databases and do not call Google Cloud
Storage or Gemini.

```bash
cd /Users/hoangdeveloper/catalog-classifier/services/api

CATALOG_TEST_DATABASE_URL=postgresql+psycopg://catalog:catalog@localhost:5432/postgres \
  .venv/bin/pytest tests/test_review_approvals.py tests/test_review_edits.py tests/test_review_groups.py -q
```

Expected result:

```text
16 passed
```

Optional full backend regression check:

```bash
CATALOG_TEST_DATABASE_URL=postgresql+psycopg://catalog:catalog@localhost:5432/postgres \
  .venv/bin/pytest -q
```

Expected result:

```text
122 passed
```

The exact total can increase as later tickets add tests. Treat any failure as a
regression to investigate.

## 4. Apply Migrations to the Local Database

```bash
cd /Users/hoangdeveloper/catalog-classifier/services/api

export CATALOG_DATABASE_URL=postgresql+psycopg://catalog:catalog@localhost:5432/catalog_classifier
.venv/bin/alembic upgrade head
.venv/bin/alembic current
```

Expected result:

- Alembic upgrades the local database to `head`;
- no migration error is printed.

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

Smoke check:

```bash
curl -fsS "$API_BASE/docs" >/dev/null
```

## 6. Seed a Review-Ready Batch With Two Proposed Groups

Run from the repository root in the second terminal.

This creates:

- one `review_required` upload batch;
- two processed images;
- two proposed singleton groups.

```bash
cd /Users/hoangdeveloper/catalog-classifier

eval "$(
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
    'qa/0019b/front-' || id::text || '.jpg',
    'qa/0019b/front-thumb-' || id::text || '.jpg',
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
    'qa/0019b/back-' || id::text || '.jpg',
    'qa/0019b/back-thumb-' || id::text || '.jpg',
    'back.jpg',
    1,
    'image/jpeg',
    101,
    'processed'
  FROM batch
  RETURNING id, organization_id, batch_id
),
front_group AS (
  INSERT INTO product_groups (
    organization_id,
    batch_id,
    status,
    cover_image_id,
    confidence
  )
  SELECT
    batch.organization_id,
    batch.id,
    'proposed',
    front_image.id,
    1.0
  FROM batch, front_image
  RETURNING id, organization_id, batch_id
),
back_group AS (
  INSERT INTO product_groups (
    organization_id,
    batch_id,
    status,
    cover_image_id,
    confidence
  )
  SELECT
    batch.organization_id,
    batch.id,
    'proposed',
    back_image.id,
    1.0
  FROM batch, back_image
  RETURNING id, organization_id, batch_id
),
memberships AS (
  INSERT INTO product_group_images (
    organization_id,
    batch_id,
    group_id,
    image_id,
    position,
    membership_source,
    membership_confidence
  )
  SELECT
    front_group.organization_id,
    front_group.batch_id,
    front_group.id,
    front_image.id,
    0,
    'singleton',
    NULL::double precision
  FROM front_group, front_image
  UNION ALL
  SELECT
    back_group.organization_id,
    back_group.batch_id,
    back_group.id,
    back_image.id,
    0,
    'singleton',
    NULL::double precision
  FROM back_group, back_image
  RETURNING group_id
),
ids AS (
  SELECT
    (SELECT id FROM batch) AS batch_id,
    (SELECT id FROM front_group) AS front_group_id,
    (SELECT id FROM back_group) AS back_group_id,
    (SELECT id FROM front_image) AS front_image_id,
    (SELECT id FROM back_image) AS back_image_id,
    (SELECT count(*) FROM memberships) AS membership_count
)
SELECT 'export BATCH_ID=' || batch_id FROM ids
UNION ALL SELECT 'export FRONT_GROUP_ID=' || front_group_id FROM ids
UNION ALL SELECT 'export BACK_GROUP_ID=' || back_group_id FROM ids
UNION ALL SELECT 'export FRONT_IMAGE_ID=' || front_image_id FROM ids
UNION ALL SELECT 'export BACK_IMAGE_ID=' || back_image_id FROM ids
UNION ALL SELECT 'export SEEDED_MEMBERSHIP_COUNT=' || membership_count FROM ids;
"
)"

printf 'BATCH_ID=%s\nFRONT_GROUP_ID=%s\nBACK_GROUP_ID=%s\nFRONT_IMAGE_ID=%s\nBACK_IMAGE_ID=%s\nSEEDED_MEMBERSHIP_COUNT=%s\n' \
  "$BATCH_ID" \
  "$FRONT_GROUP_ID" \
  "$BACK_GROUP_ID" \
  "$FRONT_IMAGE_ID" \
  "$BACK_IMAGE_ID" \
  "$SEEDED_MEMBERSHIP_COUNT"
```

Expected result:

- every printed value is a Universally Unique Identifier (UUID);
- no identifier variable is empty;
- `SEEDED_MEMBERSHIP_COUNT` is `2`.

## 7. Confirm the Initial Review Snapshot

```bash
curl -fsS "$API_BASE/v1/upload-batches/$BATCH_ID/groups" \
  | tee /tmp/catalog-0019b-initial.json \
  | python3 -m json.tool
```

Expected result:

- `status` is `review_required`;
- there are two groups;
- both groups have `status: proposed`.

## 8. Approve One Group

```bash
curl -fsS \
  -X POST "$API_BASE/v1/groups/$FRONT_GROUP_ID/approve" \
  | tee /tmp/catalog-0019b-approve-front-group.json \
  | python3 -m json.tool
```

Expected result:

- response status is `200`;
- the front group has `status: approved`;
- the back group still has `status: proposed`;
- the batch still has `status: review_required`.

Check the event:

```bash
docker compose exec postgres psql \
  -U catalog \
  -d catalog_classifier \
  -c "
SELECT action_type, group_id, payload_json
FROM review_events
WHERE batch_id = '$BATCH_ID'::uuid
ORDER BY created_at, id;
"
```

Expected result:

- one row with `action_type = approve_group`;
- `group_id` equals `FRONT_GROUP_ID`.

## 9. Verify Batch Approval Is Blocked Until All Groups Are Approved

```bash
export INCOMPLETE_APPROVAL_STATUS=$(
  curl -sS \
    -o /tmp/catalog-0019b-incomplete-batch-approval.json \
    -w '%{http_code}' \
    -X POST "$API_BASE/v1/upload-batches/$BATCH_ID/approve"
)

printf 'INCOMPLETE_APPROVAL_STATUS=%s\n' "$INCOMPLETE_APPROVAL_STATUS"
python3 -m json.tool /tmp/catalog-0019b-incomplete-batch-approval.json
```

Expected result:

- `INCOMPLETE_APPROVAL_STATUS=409`;
- error code is `review_approval_not_allowed`;
- message says all groups must be approved before batch approval.

## 10. Approve the Remaining Group

```bash
curl -fsS \
  -X POST "$API_BASE/v1/groups/$BACK_GROUP_ID/approve" \
  | tee /tmp/catalog-0019b-approve-back-group.json \
  | python3 -m json.tool
```

Expected result:

- response status is `200`;
- both groups have `status: approved`;
- batch status is still `review_required`.

## 11. Approve the Batch

```bash
curl -fsS \
  -X POST "$API_BASE/v1/upload-batches/$BATCH_ID/approve" \
  | tee /tmp/catalog-0019b-approve-batch.json \
  | python3 -m json.tool
```

Expected result:

- response status is `200`;
- `status` is `approved`;
- both groups still have `status: approved`;
- group membership rows are unchanged.

Verify persisted batch state:

```bash
docker compose exec postgres psql \
  -U catalog \
  -d catalog_classifier \
  -c "
SELECT status, completed_at IS NOT NULL AS has_completed_at
FROM upload_batches
WHERE id = '$BATCH_ID'::uuid;
"
```

Expected result:

- `status` is `approved`;
- `has_completed_at` is `t`.

## 12. Verify Approved Batch Snapshot Is Still Readable

```bash
curl -fsS "$API_BASE/v1/upload-batches/$BATCH_ID/groups" \
  | tee /tmp/catalog-0019b-approved-readback.json \
  | python3 -m json.tool
```

Expected result:

- response status is `200`;
- `status` is `approved`;
- group and image shape is the same as before batch approval.

## 13. Verify Approved Groups Are Immutable

Try to edit the approved front group:

```bash
export APPROVED_EDIT_STATUS=$(
  curl -sS \
    -o /tmp/catalog-0019b-approved-edit.json \
    -w '%{http_code}' \
    -X PATCH "$API_BASE/v1/groups/$FRONT_GROUP_ID" \
    -H 'Content-Type: application/json' \
    -d "{\"coverImageId\":\"$FRONT_IMAGE_ID\"}"
)

printf 'APPROVED_EDIT_STATUS=%s\n' "$APPROVED_EDIT_STATUS"
python3 -m json.tool /tmp/catalog-0019b-approved-edit.json
```

Expected result:

- `APPROVED_EDIT_STATUS=409`;
- error code is `review_edit_not_allowed`.

## 14. Verify Repeated Approval Is a No-Op

Capture the event count, repeat group and batch approval, then compare counts:

```bash
export EVENT_COUNT_BEFORE=$(
  docker compose exec -T postgres psql \
    -U catalog \
    -d catalog_classifier \
    -tA \
    -c "SELECT count(*) FROM review_events WHERE batch_id = '$BATCH_ID'::uuid;"
)

curl -fsS -X POST "$API_BASE/v1/groups/$FRONT_GROUP_ID/approve" \
  >/tmp/catalog-0019b-repeat-group-approval.json

curl -fsS -X POST "$API_BASE/v1/upload-batches/$BATCH_ID/approve" \
  >/tmp/catalog-0019b-repeat-batch-approval.json

export EVENT_COUNT_AFTER=$(
  docker compose exec -T postgres psql \
    -U catalog \
    -d catalog_classifier \
    -tA \
    -c "SELECT count(*) FROM review_events WHERE batch_id = '$BATCH_ID'::uuid;"
)

printf 'EVENT_COUNT_BEFORE=%s\nEVENT_COUNT_AFTER=%s\n' \
  "$EVENT_COUNT_BEFORE" \
  "$EVENT_COUNT_AFTER"
```

Expected result:

- both counts are equal.

## 15. Verify Final Review Events

```bash
docker compose exec postgres psql \
  -U catalog \
  -d catalog_classifier \
  -c "
SELECT action_type, count(*) AS event_count
FROM review_events
WHERE batch_id = '$BATCH_ID'::uuid
GROUP BY action_type
ORDER BY action_type;
"
```

Expected result:

```text
 approve_batch | 1
 approve_group | 2
```

## 16. Verify Empty Review Batch Approval

Create an empty review-ready batch:

```bash
export EMPTY_BATCH_ID=$(
  docker compose exec -T postgres psql \
    -U catalog \
    -d catalog_classifier \
    -tA \
    -q \
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
  0,
  0,
  '2026-06-01',
  now()
)
RETURNING id
)
SELECT id FROM batch;
"
)

printf 'EMPTY_BATCH_ID=<%s>\n' "$EMPTY_BATCH_ID"

curl -fsS \
  -X POST "$API_BASE/v1/upload-batches/$EMPTY_BATCH_ID/approve" \
  | tee /tmp/catalog-0019b-empty-batch-approval.json \
  | python3 -m json.tool
```

Expected result:

- response status is `200`;
- `status` is `approved`;
- `groups` is `[]`.

## 17. Optional Cleanup

This removes only the quality-assurance batches created by this guide:

```bash
docker compose exec postgres psql \
  -U catalog \
  -d catalog_classifier \
  -c "
DELETE FROM upload_batches
WHERE id IN (
  '$BATCH_ID'::uuid,
  '$EMPTY_BATCH_ID'::uuid
);
"
```

To reset all local development data, use the broader local reset flow instead:

```bash
cd /Users/hoangdeveloper/catalog-classifier
docker compose down -v
docker compose up -d postgres
```
