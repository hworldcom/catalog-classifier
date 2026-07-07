# Ticket 0015a QA: Start Processing Endpoint

Run these steps from a clean shell unless a step says otherwise.

## Objective

Manually verify that ticket 0015a:

- exposes `POST /v1/upload-batches/{batchId}/start-processing`;
- exposes `GET /v1/upload-batches/{batchId}/processing`;
- starts processing promptly without requiring frontend calls to internal worker
  endpoints;
- returns a stable per-image processing snapshot;
- keeps `GET /processing` read-only;
- keeps repeated `POST /start-processing` calls idempotent.

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

These tests use a temporary PostgreSQL database and fake worker dependencies.
They do not call Google Cloud Storage or Gemini.

```bash
cd /Users/hoangdeveloper/catalog-classifier/services/api

CATALOG_TEST_DATABASE_URL=postgresql+psycopg://catalog:catalog@localhost:5432/postgres \
  .venv/bin/pytest tests/test_processing_orchestration.py -q
```

Expected result:

```text
7 passed
```

Optional full API regression check:

```bash
CATALOG_TEST_DATABASE_URL=postgresql+psycopg://catalog:catalog@localhost:5432/postgres \
  .venv/bin/pytest -q
```

Expected result:

- all API tests pass.

## 4. Start the API for Real Integration QA

Use this only if you want to process real uploaded images from Cloud Storage.

```bash
cd /Users/hoangdeveloper/catalog-classifier/services/api

export CATALOG_DATABASE_URL=postgresql+psycopg://catalog:catalog@localhost:5432/catalog_classifier
export CATALOG_UPLOAD_BUCKET=lnlabs-bucket
export CATALOG_SIGNING_SERVICE_ACCOUNT="catalog-api@catalog-classifier.iam.gserviceaccount.com"
export GEMINI_API_KEY="<your Gemini API key>"

.venv/bin/alembic upgrade head
.venv/bin/uvicorn catalog_api.main:app --reload --port 8000
```

Expected result:

- the API starts on `http://localhost:8000`;
- migrations are at `head`;
- the API process has access to Cloud Storage and Gemini.

## 5. Create or Reuse a Finalized Batch

Create a frontend upload and wait until the ingest page shows:

```text
Backend status: queued
```

Copy the batch identifier from the page and export it in a second shell:

```bash
export API_BASE=http://localhost:8000
export BATCH_ID="<queued batch id from frontend>"
```

Confirm the batch is queued:

```bash
curl -sS "$API_BASE/v1/upload-batches/$BATCH_ID/processing" | python3 -m json.tool
```

Expected result:

- `status` is `queued`;
- `pipelineVersion` is `2026-06-01`;
- every image has `processJobStatus: null`;
- every image has `classifyJobStatus: null`.

Confirm `GET /processing` did not create jobs:

```bash
cd /Users/hoangdeveloper/catalog-classifier

docker compose exec postgres psql \
  -U catalog \
  -d catalog_classifier \
  -c "
SELECT job_type, status, count(*)
FROM processing_jobs
WHERE batch_id = '$BATCH_ID'::uuid
GROUP BY job_type, status
ORDER BY job_type, status;
"
```

Expected result:

- zero rows before processing starts.

## 6. Start Processing

```bash
curl -sS -X POST "$API_BASE/v1/upload-batches/$BATCH_ID/start-processing" \
  | tee /tmp/catalog-0015a-start-processing.json \
  | python3 -m json.tool
```

Expected result:

- `status` is `processing`;
- `pipelineVersion` is `2026-06-01`;
- every image has a non-null `processJobStatus`;
- on the immediate response, `processJobStatus` is usually `pending` because the
  endpoint returns before the background runner finishes;
- `classifyJobStatus` may be `null` on the immediate response because
  classification is created after successful image processing;
- `hasHashes` and `hasEmbedding` may still be `false` on the immediate response.

Example immediate response for one image:

```json
{
  "batchId": "057d795a-1a8a-43cf-9068-4dde8fe88c32",
  "status": "processing",
  "originalFileCount": 1,
  "processedFileCount": 0,
  "pipelineVersion": "2026-06-01",
  "images": [
    {
      "imageId": "feeb0ce8-9adf-4a0d-bdca-670926ec5cc7",
      "uploadOrder": 0,
      "originalFilename": "20260605_135609.jpg",
      "imageStatus": "uploaded",
      "processJobStatus": "pending",
      "processError": null,
      "classifyJobStatus": null,
      "classifyError": null,
      "categorySlug": null,
      "confidence": null,
      "hasHashes": false,
      "hasEmbedding": false
    }
  ]
}
```

This response is successful. Continue to the polling step to observe the image
move from `pending` to `completed` or `failed`.

## 7. Poll Processing State

```bash
for attempt in $(seq 1 30); do
  curl -fsS "$API_BASE/v1/upload-batches/$BATCH_ID/processing" \
    > /tmp/catalog-0015a-processing.json

  python3 - <<'PY'
import json

with open("/tmp/catalog-0015a-processing.json") as file:
    snapshot = json.load(file)

rows = [
    (
        image["originalFilename"],
        image["imageStatus"],
        image["processJobStatus"],
        image["classifyJobStatus"],
        image["categorySlug"],
        image["confidence"],
        image["hasHashes"],
        image["hasEmbedding"],
    )
    for image in snapshot["images"]
]

print(snapshot["status"], rows)

def terminal(image):
    process_status = image["processJobStatus"]
    classify_status = image["classifyJobStatus"]
    if process_status == "failed" and classify_status is None:
        return True
    return process_status in {"completed", "failed"} and classify_status in {
        "completed",
        "failed",
    }

raise SystemExit(0 if all(terminal(image) for image in snapshot["images"]) else 1)
PY

  if [ "$?" -eq 0 ]; then
    echo "processing reached terminal image states"
    break
  fi

  sleep 2
done
```

Expected happy-path result:

- each successful image has `imageStatus: processed`;
- each successful image has `processJobStatus: completed`;
- each successful image has `classifyJobStatus: completed`;
- each successful image has `hasHashes: true`;
- each successful image has `hasEmbedding: true`;
- each classified image has `categorySlug` and `confidence`.

If a provider or storage dependency fails, the failed job should show
`processError` or `classifyError`, and other eligible images should continue.

## 8. Verify Database Rows

```bash
cd /Users/hoangdeveloper/catalog-classifier

docker compose exec postgres psql \
  -U catalog \
  -d catalog_classifier \
  -c "
SELECT
  i.original_filename,
  i.status AS image_status,
  i.phash IS NOT NULL AS has_phash,
  i.dhash IS NOT NULL AS has_dhash,
  EXISTS (
    SELECT 1
    FROM image_embeddings e
    WHERE e.organization_id = i.organization_id
      AND e.image_id = i.id
      AND e.pipeline_version = '2026-06-01'
  ) AS has_embedding,
  EXISTS (
    SELECT 1
    FROM image_classifications c
    WHERE c.organization_id = i.organization_id
      AND c.image_id = i.id
      AND c.pipeline_version = '2026-06-01'
  ) AS has_classification
FROM image_assets i
WHERE i.batch_id = '$BATCH_ID'::uuid
ORDER BY i.upload_order;
"
```

Expected happy-path result:

- every uploaded image is `processed`;
- every processed image has both hashes;
- every processed image has one embedding row;
- every processed image has one classification row.

Check job counts:

```bash
docker compose exec postgres psql \
  -U catalog \
  -d catalog_classifier \
  -c "
SELECT job_type, status, count(*)
FROM processing_jobs
WHERE batch_id = '$BATCH_ID'::uuid
GROUP BY job_type, status
ORDER BY job_type, status;
"
```

Expected happy-path result:

- one `process-image` completed job per image;
- one `classify-image` completed job per processed image.

## 9. Verify Idempotency

Call start processing again:

```bash
curl -sS -X POST "$API_BASE/v1/upload-batches/$BATCH_ID/start-processing" \
  | python3 -m json.tool
```

Then re-run the job count query from step 8.

Expected result:

- no duplicate `processing_jobs` rows are created;
- no duplicate `image_embeddings` rows are created;
- no duplicate `image_classifications` rows are created;
- the snapshot remains stable.

## 10. Verify Invalid Batch State

This creates a temporary `created` batch and verifies both processing endpoints
return `409`.

```bash
cd /Users/hoangdeveloper/catalog-classifier

export API_BASE=${API_BASE:-http://localhost:8000}
export CREATED_BATCH_ID=$(
  docker compose exec -T postgres psql \
    -U catalog \
    -d catalog_classifier \
    -qAt \
    -v ON_ERROR_STOP=1 \
    -c "
INSERT INTO upload_batches (organization_id, status)
VALUES ('00000000-0000-0000-0000-000000000001', 'created')
RETURNING id;
  " | tr -d '[:space:]'
)

printf 'API_BASE=%s\nCREATED_BATCH_ID=%s\n' "$API_BASE" "$CREATED_BATCH_ID"

case "$CREATED_BATCH_ID" in
  ????????-????-????-????-????????????) ;;
  *)
    echo "CREATED_BATCH_ID is not a UUID; stop and inspect the psql output."
    exit 1
    ;;
esac

curl -sS -o /tmp/catalog-0015a-get-created.json -w '%{http_code}\n' \
  "$API_BASE/v1/upload-batches/$CREATED_BATCH_ID/processing"

curl -sS -o /tmp/catalog-0015a-post-created.json -w '%{http_code}\n' \
  -X POST "$API_BASE/v1/upload-batches/$CREATED_BATCH_ID/start-processing"
```

Expected output:

```text
409
409
```
