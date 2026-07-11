# Ticket 0019a QA: Review Editing API

Run these steps from a clean shell unless a step says otherwise.

## Objective

Manually verify that ticket 0019a:

- exposes backend review editing endpoints;
- allows creating, moving, removing, merging, and splitting review groups;
- allows cover image, approved category, and duplicate metadata edits;
- returns the updated review snapshot after each successful mutation;
- writes one `review_events` row for each real manual change;
- writes no `review_events` row for no-op requests;
- rejects invalid review edits with explicit errors.

Ticket 0019a is backend-only. It does not include review page user interface
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
  .venv/bin/pytest tests/test_review_edits.py tests/test_review_groups.py -q
```

Expected result:

```text
12 passed
```

Optional full backend regression check:

```bash
CATALOG_TEST_DATABASE_URL=postgresql+psycopg://catalog:catalog@localhost:5432/postgres \
  .venv/bin/pytest -q
```

Expected result:

```text
118 passed
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

## 6. Seed a Review-Ready Batch

Run from the repository root in the second terminal.

This creates:

- one `review_required` upload batch;
- four processed images;
- group one with `front.jpg` and `back.jpg`;
- group two with `solo-a.jpg`;
- group three with `solo-b.jpg`.

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
    4,
    4,
    '2026-06-01',
    now()
  )
  RETURNING id, organization_id
),
image_seed AS (
  SELECT * FROM (VALUES
    (0, 'front.jpg'),
    (1, 'back.jpg'),
    (2, 'solo-a.jpg'),
    (3, 'solo-b.jpg')
  ) AS rows(upload_order, original_filename)
),
images AS (
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
    batch.organization_id,
    batch.id,
    'qa/0019a/original-' || image_seed.upload_order::text || '.jpg',
    'qa/0019a/thumb-' || image_seed.upload_order::text || '.jpg',
    image_seed.original_filename,
    image_seed.upload_order,
    'image/jpeg',
    100 + image_seed.upload_order,
    'processed'
  FROM batch
  CROSS JOIN image_seed
  RETURNING id, organization_id, batch_id, original_filename, upload_order
),
category AS (
  SELECT id FROM categories WHERE slug = 't-shirts'
),
group_one AS (
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
    (SELECT id FROM images WHERE original_filename = 'front.jpg'),
    0.95
  FROM batch, category
  RETURNING id, organization_id, batch_id
),
group_two AS (
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
    (SELECT id FROM images WHERE original_filename = 'solo-a.jpg'),
    1.0
  FROM batch
  RETURNING id, organization_id, batch_id
),
group_three AS (
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
    (SELECT id FROM images WHERE original_filename = 'solo-b.jpg'),
    1.0
  FROM batch
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
  SELECT organization_id, batch_id, id, (SELECT id FROM images WHERE original_filename = 'front.jpg'), 0, 'engine', 0.95
  FROM group_one
  UNION ALL
  SELECT organization_id, batch_id, id, (SELECT id FROM images WHERE original_filename = 'back.jpg'), 1, 'engine', 0.92
  FROM group_one
  UNION ALL
  SELECT organization_id, batch_id, id, (SELECT id FROM images WHERE original_filename = 'solo-a.jpg'), 0, 'singleton', NULL
  FROM group_two
  UNION ALL
  SELECT organization_id, batch_id, id, (SELECT id FROM images WHERE original_filename = 'solo-b.jpg'), 0, 'singleton', NULL
  FROM group_three
  RETURNING image_id
),
ids AS (
  SELECT
    (SELECT id FROM batch) AS batch_id,
    (SELECT id FROM group_one) AS group_one_id,
    (SELECT id FROM group_two) AS group_two_id,
    (SELECT id FROM group_three) AS group_three_id,
    (SELECT id FROM images WHERE original_filename = 'front.jpg') AS front_image_id,
    (SELECT id FROM images WHERE original_filename = 'back.jpg') AS back_image_id,
    (SELECT id FROM images WHERE original_filename = 'solo-a.jpg') AS solo_a_image_id,
    (SELECT id FROM images WHERE original_filename = 'solo-b.jpg') AS solo_b_image_id
)
SELECT 'export BATCH_ID=' || batch_id FROM ids
UNION ALL SELECT 'export GROUP_ONE_ID=' || group_one_id FROM ids
UNION ALL SELECT 'export GROUP_TWO_ID=' || group_two_id FROM ids
UNION ALL SELECT 'export GROUP_THREE_ID=' || group_three_id FROM ids
UNION ALL SELECT 'export FRONT_IMAGE_ID=' || front_image_id FROM ids
UNION ALL SELECT 'export BACK_IMAGE_ID=' || back_image_id FROM ids
UNION ALL SELECT 'export SOLO_A_IMAGE_ID=' || solo_a_image_id FROM ids
UNION ALL SELECT 'export SOLO_B_IMAGE_ID=' || solo_b_image_id FROM ids;
"
)"

printf 'BATCH_ID=%s\nGROUP_ONE_ID=%s\nGROUP_TWO_ID=%s\nGROUP_THREE_ID=%s\nFRONT_IMAGE_ID=%s\nBACK_IMAGE_ID=%s\nSOLO_A_IMAGE_ID=%s\nSOLO_B_IMAGE_ID=%s\n' \
  "$BATCH_ID" \
  "$GROUP_ONE_ID" \
  "$GROUP_TWO_ID" \
  "$GROUP_THREE_ID" \
  "$FRONT_IMAGE_ID" \
  "$BACK_IMAGE_ID" \
  "$SOLO_A_IMAGE_ID" \
  "$SOLO_B_IMAGE_ID"
```

Expected result:

- every printed value is a Universally Unique Identifier (UUID);
- no variable is empty.

## 7. Confirm the Initial Review Snapshot

```bash
curl -fsS "$API_BASE/v1/upload-batches/$BATCH_ID/groups" \
  | tee /tmp/catalog-0019a-initial.json \
  | python3 -m json.tool
```

Expected result:

- `status` is `review_required`;
- `groups` has three groups;
- `front.jpg` and `back.jpg` are in the same group;
- `solo-a.jpg` and `solo-b.jpg` are separate singleton groups;
- each image has a `thumbnailUrl`.

## 8. Create a New Group From Selected Images

Move `back.jpg` and `solo-a.jpg` into a newly created manual group:

```bash
curl -fsS \
  -X POST "$API_BASE/v1/upload-batches/$BATCH_ID/groups" \
  -H 'Content-Type: application/json' \
  -d "{
    \"imageIds\": [\"$BACK_IMAGE_ID\", \"$SOLO_A_IMAGE_ID\"]
  }" \
  | tee /tmp/catalog-0019a-create-group.json \
  | python3 -m json.tool
```

Expected result:

- response status is `200`;
- one group contains exactly `back.jpg` and `solo-a.jpg`;
- those memberships have `membershipSource: manual_review`;
- empty source groups are removed.

Check the review event:

```bash
docker compose exec postgres psql \
  -U catalog \
  -d catalog_classifier \
  -c "
SELECT action_type, payload_json
FROM review_events
WHERE batch_id = '$BATCH_ID'::uuid
ORDER BY created_at, id;
"
```

Expected result:

- one row with `action_type = create_group`.

## 9. Verify No-Op Create Does Not Add an Event

Repeat the same create request:

```bash
export EVENT_COUNT_BEFORE=$(
  docker compose exec -T postgres psql \
    -U catalog \
    -d catalog_classifier \
    -tA \
    -c "SELECT count(*) FROM review_events WHERE batch_id = '$BATCH_ID'::uuid;"
)

curl -fsS \
  -X POST "$API_BASE/v1/upload-batches/$BATCH_ID/groups" \
  -H 'Content-Type: application/json' \
  -d "{
    \"imageIds\": [\"$BACK_IMAGE_ID\", \"$SOLO_A_IMAGE_ID\"]
  }" \
  >/tmp/catalog-0019a-create-group-noop.json

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

## 10. Capture the Newly Created Group Identifier

```bash
export CREATED_GROUP_ID=$(
  python3 - <<'PY'
import json
import os

back_id = os.environ["BACK_IMAGE_ID"]
solo_a_id = os.environ["SOLO_A_IMAGE_ID"]

with open("/tmp/catalog-0019a-create-group.json") as file:
    snapshot = json.load(file)

for group in snapshot["groups"]:
    image_ids = {image["imageId"] for image in group["images"]}
    if image_ids == {back_id, solo_a_id}:
        print(group["groupId"])
        break
else:
    raise SystemExit("created group was not found")
PY
)

printf 'CREATED_GROUP_ID=%s\n' "$CREATED_GROUP_ID"
```

Expected result:

- `CREATED_GROUP_ID` is not empty.

## 11. Move an Image Into Another Group

Move `solo-b.jpg` into the created group:

```bash
curl -fsS \
  -X POST "$API_BASE/v1/groups/$CREATED_GROUP_ID/images" \
  -H 'Content-Type: application/json' \
  -d "{\"imageId\":\"$SOLO_B_IMAGE_ID\"}" \
  | tee /tmp/catalog-0019a-move-image.json \
  | python3 -m json.tool
```

Expected result:

- response status is `200`;
- the created group now contains `back.jpg`, `solo-a.jpg`, and `solo-b.jpg`;
- `solo-b.jpg` has `membershipSource: manual_review`;
- the previous singleton group for `solo-b.jpg` is removed;
- a new `move_image` review event exists.

## 12. Remove an Image From a Group

Remove `solo-b.jpg` from the created group. The backend should put it into a new
singleton group.

```bash
curl -fsS \
  -X DELETE "$API_BASE/v1/groups/$CREATED_GROUP_ID/images/$SOLO_B_IMAGE_ID" \
  | tee /tmp/catalog-0019a-remove-image.json \
  | python3 -m json.tool
```

Expected result:

- response status is `200`;
- `solo-b.jpg` is now alone in its own group;
- that singleton membership has `membershipSource: manual_review`;
- a new `remove_image` review event exists.

Capture the singleton group id for later:

```bash
export SOLO_B_GROUP_ID=$(
  python3 - <<'PY'
import json
import os

solo_b_id = os.environ["SOLO_B_IMAGE_ID"]

with open("/tmp/catalog-0019a-remove-image.json") as file:
    snapshot = json.load(file)

for group in snapshot["groups"]:
    if [image["imageId"] for image in group["images"]] == [solo_b_id]:
        print(group["groupId"])
        break
else:
    raise SystemExit("solo-b singleton group was not found")
PY
)

printf 'SOLO_B_GROUP_ID=%s\n' "$SOLO_B_GROUP_ID"
```

## 13. Split a Group

Split `solo-a.jpg` out of the created group:

```bash
curl -fsS \
  -X POST "$API_BASE/v1/groups/$CREATED_GROUP_ID/split" \
  -H 'Content-Type: application/json' \
  -d "{\"imageIds\":[\"$SOLO_A_IMAGE_ID\"]}" \
  | tee /tmp/catalog-0019a-split-group.json \
  | python3 -m json.tool
```

Expected result:

- response status is `200`;
- `solo-a.jpg` is in a new group by itself;
- the original created group still contains `back.jpg`;
- a new `split_group` review event exists.

Capture the new `solo-a.jpg` group id:

```bash
export SOLO_A_GROUP_ID=$(
  python3 - <<'PY'
import json
import os

solo_a_id = os.environ["SOLO_A_IMAGE_ID"]

with open("/tmp/catalog-0019a-split-group.json") as file:
    snapshot = json.load(file)

for group in snapshot["groups"]:
    if [image["imageId"] for image in group["images"]] == [solo_a_id]:
        print(group["groupId"])
        break
else:
    raise SystemExit("solo-a singleton group was not found")
PY
)

printf 'SOLO_A_GROUP_ID=%s\n' "$SOLO_A_GROUP_ID"
```

## 14. Merge Groups

Merge the `solo-a.jpg` and `solo-b.jpg` singleton groups into the created group:

```bash
curl -fsS \
  -X POST "$API_BASE/v1/groups/merge" \
  -H 'Content-Type: application/json' \
  -d "{
    \"targetGroupId\": \"$CREATED_GROUP_ID\",
    \"sourceGroupIds\": [\"$SOLO_A_GROUP_ID\", \"$SOLO_B_GROUP_ID\"]
  }" \
  | tee /tmp/catalog-0019a-merge-groups.json \
  | python3 -m json.tool
```

Expected result:

- response status is `200`;
- the target group id is still `CREATED_GROUP_ID`;
- the target group contains `back.jpg`, `solo-a.jpg`, and `solo-b.jpg`;
- source groups no longer exist;
- a new `merge_groups` review event exists.

Repeating the merge should fail because source groups were deleted:

```bash
export REPEAT_MERGE_STATUS=$(
  curl -sS \
    -o /tmp/catalog-0019a-repeat-merge.json \
    -w '%{http_code}' \
    -X POST "$API_BASE/v1/groups/merge" \
    -H 'Content-Type: application/json' \
    -d "{
      \"targetGroupId\": \"$CREATED_GROUP_ID\",
      \"sourceGroupIds\": [\"$SOLO_A_GROUP_ID\"]
    }"
)

printf 'REPEAT_MERGE_STATUS=%s\n' "$REPEAT_MERGE_STATUS"
python3 -m json.tool /tmp/catalog-0019a-repeat-merge.json
```

Expected result:

- `REPEAT_MERGE_STATUS=404`;
- error code is `review_resource_not_found`.

## 15. Update Cover Image

```bash
curl -fsS \
  -X PATCH "$API_BASE/v1/groups/$CREATED_GROUP_ID" \
  -H 'Content-Type: application/json' \
  -d "{\"coverImageId\":\"$SOLO_A_IMAGE_ID\"}" \
  | tee /tmp/catalog-0019a-cover.json \
  | python3 -m json.tool
```

Expected result:

- response status is `200`;
- the group has `coverImageId` equal to `SOLO_A_IMAGE_ID`;
- a new `update_group` review event exists.

## 16. Update Approved Category

Capture a valid category id and set it as the approved category:

```bash
export TROUSERS_CATEGORY_ID=$(
  docker compose exec -T postgres psql \
    -U catalog \
    -d catalog_classifier \
    -tA \
    -v ON_ERROR_STOP=1 \
    -c "SELECT id FROM categories WHERE slug = 'trousers';"
)

curl -fsS \
  -X PATCH "$API_BASE/v1/groups/$CREATED_GROUP_ID" \
  -H 'Content-Type: application/json' \
  -d "{\"approvedCategoryId\":\"$TROUSERS_CATEGORY_ID\"}" \
  | tee /tmp/catalog-0019a-category.json \
  | python3 -m json.tool
```

Expected result:

- response status is `200`;
- the group has `approvedCategorySlug: trousers`;
- a new `update_group` review event exists.

Clear the approved category again:

```bash
curl -fsS \
  -X PATCH "$API_BASE/v1/groups/$CREATED_GROUP_ID" \
  -H 'Content-Type: application/json' \
  -d "{\"approvedCategoryId\":null}" \
  | tee /tmp/catalog-0019a-category-clear.json \
  | python3 -m json.tool
```

Expected result:

- response status is `200`;
- the group has `approvedCategorySlug: null`;
- a new `update_group` review event exists.

## 17. Mark and Restore Duplicate Metadata

Mark `solo-b.jpg` as a duplicate of `back.jpg`:

```bash
curl -fsS \
  -X PATCH "$API_BASE/v1/groups/$CREATED_GROUP_ID/images/$SOLO_B_IMAGE_ID" \
  -H 'Content-Type: application/json' \
  -d "{
    \"isDuplicate\": true,
    \"duplicateOfImageId\": \"$BACK_IMAGE_ID\"
  }" \
  | tee /tmp/catalog-0019a-mark-duplicate.json \
  | python3 -m json.tool
```

Expected result:

- response status is `200`;
- `solo-b.jpg` has `isDuplicate: true`;
- `solo-b.jpg` has `duplicateOfImageId` equal to `BACK_IMAGE_ID`;
- a new `mark_duplicate` review event exists.

Restore `solo-b.jpg`:

```bash
curl -fsS \
  -X PATCH "$API_BASE/v1/groups/$CREATED_GROUP_ID/images/$SOLO_B_IMAGE_ID" \
  -H 'Content-Type: application/json' \
  -d '{
    "isDuplicate": false,
    "duplicateOfImageId": null
  }' \
  | tee /tmp/catalog-0019a-restore-duplicate.json \
  | python3 -m json.tool
```

Expected result:

- response status is `200`;
- `solo-b.jpg` has `isDuplicate: false`;
- `solo-b.jpg` has `duplicateOfImageId: null`;
- a new `restore_duplicate` review event exists.

## 18. Verify Invalid Requests

Duplicate image ids are invalid:

```bash
export DUPLICATE_SELECTION_STATUS=$(
  curl -sS \
    -o /tmp/catalog-0019a-duplicate-selection.json \
    -w '%{http_code}' \
    -X POST "$API_BASE/v1/upload-batches/$BATCH_ID/groups" \
    -H 'Content-Type: application/json' \
    -d "{
      \"imageIds\": [\"$FRONT_IMAGE_ID\", \"$FRONT_IMAGE_ID\"]
    }"
)

printf 'DUPLICATE_SELECTION_STATUS=%s\n' "$DUPLICATE_SELECTION_STATUS"
python3 -m json.tool /tmp/catalog-0019a-duplicate-selection.json
```

Expected result:

- `DUPLICATE_SELECTION_STATUS=400`;
- error code is `invalid_review_edit`.

An empty group patch is invalid:

```bash
export EMPTY_PATCH_STATUS=$(
  curl -sS \
    -o /tmp/catalog-0019a-empty-patch.json \
    -w '%{http_code}' \
    -X PATCH "$API_BASE/v1/groups/$CREATED_GROUP_ID" \
    -H 'Content-Type: application/json' \
    -d '{}'
)

printf 'EMPTY_PATCH_STATUS=%s\n' "$EMPTY_PATCH_STATUS"
python3 -m json.tool /tmp/catalog-0019a-empty-patch.json
```

Expected result:

- `EMPTY_PATCH_STATUS=400`;
- error code is `invalid_review_edit`.

## 19. Verify Non-Review Batch Rejection

Create a `processing` batch with a proposed group, then attempt a review edit:

```bash
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
    'processing',
    1,
    1,
    '2026-06-01',
    now()
  )
  RETURNING id, organization_id
),
image AS (
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
    'qa/0019a/non-review-original.jpg',
    'qa/0019a/non-review-thumb.jpg',
    'non-review.jpg',
    0,
    'image/jpeg',
    100,
    'processed'
  FROM batch
  RETURNING id, organization_id, batch_id
),
product_group AS (
  INSERT INTO product_groups (
    organization_id,
    batch_id,
    status,
    cover_image_id,
    confidence
  )
  SELECT
    organization_id,
    batch_id,
    'proposed',
    id,
    1.0
  FROM image
  RETURNING id, organization_id, batch_id
),
membership AS (
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
    product_group.organization_id,
    product_group.batch_id,
    product_group.id,
    image.id,
    0,
    'singleton',
    NULL
  FROM product_group, image
)
SELECT 'export NON_REVIEW_GROUP_ID=' || id FROM product_group
UNION ALL SELECT 'export NON_REVIEW_IMAGE_ID=' || id FROM image;
"
)"

export NON_REVIEW_STATUS=$(
  curl -sS \
    -o /tmp/catalog-0019a-non-review.json \
    -w '%{http_code}' \
    -X POST "$API_BASE/v1/groups/$NON_REVIEW_GROUP_ID/images" \
    -H 'Content-Type: application/json' \
    -d "{\"imageId\":\"$NON_REVIEW_IMAGE_ID\"}"
)

printf 'NON_REVIEW_STATUS=%s\n' "$NON_REVIEW_STATUS"
python3 -m json.tool /tmp/catalog-0019a-non-review.json
```

Expected result:

- `NON_REVIEW_STATUS=409`;
- error code is `review_edit_not_allowed`;
- message says review edits require a review-ready batch.

## 20. Verify Final Review Events

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

Expected result includes:

```text
 create_group
 mark_duplicate
 merge_groups
 move_image
 remove_image
 restore_duplicate
 split_group
 update_group
```

`update_group` should have multiple rows because cover and category edits are
both group updates.

## 21. Optional Cleanup

This removes only the quality-assurance batches created by this guide:

```bash
docker compose exec postgres psql \
  -U catalog \
  -d catalog_classifier \
  -c "DELETE FROM upload_batches WHERE id = '$BATCH_ID'::uuid;"
```

To reset all local development data, use the broader local reset flow instead:

```bash
cd /Users/hoangdeveloper/catalog-classifier
docker compose down -v
docker compose up -d postgres
```
