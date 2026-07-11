# Ticket 0018 QA: Same-Product Grouping Engine

## Prerequisites

- PostgreSQL is running through Docker Compose.
- The API virtual environment is installed.

```sh
cd /Users/hoangdeveloper/catalog-classifier
docker compose up -d postgres
```

## Automated QA

Run the focused grouping and processing tests:

```sh
cd /Users/hoangdeveloper/catalog-classifier/services/api

CATALOG_TEST_DATABASE_URL=postgresql+psycopg://catalog:catalog@localhost:5432/postgres \
  .venv/bin/pytest \
  tests/test_grouping.py \
  tests/test_processing_jobs.py \
  tests/test_processing_orchestration.py
```

Expected result:

- all selected tests pass;
- grouping creates proposed groups for high-confidence matches;
- uncertain chains do not collapse into one group;
- rerunning a grouping job is a no-op when groups already exist;
- failed or retryable processing rows do not create unsafe review groups.

Run the full backend suite:

```sh
cd /Users/hoangdeveloper/catalog-classifier/services/api

CATALOG_TEST_DATABASE_URL=postgresql+psycopg://catalog:catalog@localhost:5432/postgres \
  .venv/bin/pytest
```

Expected result:

- the full backend suite passes.

## Manual Smoke QA

1. Upload images through the frontend.
2. Start processing for the durable batch.
3. Poll processing state until the batch reaches `review_required`.
4. Load the review groups endpoint:

```sh
export API_BASE=http://localhost:8000
export BATCH_ID="<batch id>"

curl -fsS "$API_BASE/v1/upload-batches/$BATCH_ID/groups" \
  | python3 -m json.tool
```

Expected result:

- the response status is `review_required`;
- `groups` contains proposed groups for processed images;
- uncertain or weak matches remain separate groups;
- failed images are absent from groups.
