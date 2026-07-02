# Local Start Guide

This guide starts the current development app from a fully stopped state.

It starts three local components:

- PostgreSQL with pgvector, through Docker Compose.
- The FastAPI backend, through Uvicorn.
- The Next.js admin frontend, through the Next.js development server.

Google Cloud Storage is not started locally. It remains an external dependency for
direct browser uploads and worker storage reads/writes.

## Prerequisites

- Docker Desktop is running.
- Python 3.12 is available.
- Node.js 20.9 or newer is available.
- Google Cloud Application Default Credentials are configured if you want to test
  direct Cloud Storage uploads.

For upload flows, configure local Google credentials once:

```bash
gcloud auth application-default login \
  --impersonate-service-account=catalog-api@catalog-classifier.iam.gserviceaccount.com
gcloud config set project catalog-classifier
```

## 1. Start PostgreSQL

Run from the repository root:

```bash
cd /Users/hoangdeveloper/catalog-classifier
docker compose up -d --force-recreate postgres
docker compose ps postgres
```

Expected result:

- image: `pgvector/pgvector:pg16`;
- status: `healthy`;
- local port: `5432`.

This component stores durable application metadata, migration state, upload batch
state, image rows, processing job rows, perceptual hashes, and embedding vectors. It does not
store image file bytes.

If the existing local volume reports a collation version mismatch after switching
to the pgvector image, refresh the local database collation metadata:

```bash
docker compose exec postgres psql -U catalog -d postgres -v ON_ERROR_STOP=1 \
  -c "ALTER DATABASE postgres REFRESH COLLATION VERSION;" \
  -c "ALTER DATABASE template1 REFRESH COLLATION VERSION;" \
  -c "ALTER DATABASE catalog_classifier REFRESH COLLATION VERSION;"
```

Do not delete the database volume unless you intentionally want to erase local
development data.

## 2. Install Backend Dependencies

Run from the backend service directory:

```bash
cd /Users/hoangdeveloper/catalog-classifier/services/api
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

If `.venv` already exists, keep it and rerun only the install command:

```bash
.venv/bin/python -m pip install -e '.[dev]'
```

This installs the FastAPI service, test dependencies, database migration tooling,
Cloud Storage client libraries, image processing libraries, and pgvector support.

## 3. Run Database Migrations

Run from `services/api`:

```bash
.venv/bin/alembic upgrade head
.venv/bin/alembic current
```

Expected current migration:

```text
0005_hashes_embeddings (head)
```

Verify pgvector is enabled in the application database:

```bash
cd /Users/hoangdeveloper/catalog-classifier
docker compose exec postgres psql -U catalog -d catalog_classifier \
  -c "SELECT extname FROM pg_extension WHERE extname = 'vector';"
```

Expected result:

```text
 extname
---------
 vector
```

## 4. Start The Backend

Open a dedicated terminal for the backend:

```bash
cd /Users/hoangdeveloper/catalog-classifier/services/api
export CATALOG_UPLOAD_BUCKET=lnlabs-bucket
export CATALOG_SIGNING_SERVICE_ACCOUNT="catalog-api@catalog-classifier.iam.gserviceaccount.com"
export GEMINI_API_KEY="<your Gemini API key>"
.venv/bin/uvicorn catalog_api.main:app --reload --port 8000
```

This component serves the local Hypertext Transfer Protocol (HTTP) application
programming interface (API) on `http://localhost:8000`.

It owns:

- upload batch creation;
- file registration and signed upload URL generation;
- upload finalization and batch readback;
- retry signed URL generation;
- local processing job dispatch;
- the internal `process-image` worker endpoint.

The `GEMINI_API_KEY` value is only needed when you run real image embedding
generation through the worker endpoint. Backend tests replace the embedding provider
with a fake implementation.

There is no separate worker process in the current prototype. The internal worker
endpoint runs inside this backend process.

Smoke check in another terminal:

```bash
curl -fsS http://localhost:8000/docs >/dev/null
curl -fsS -X POST http://localhost:8000/v1/upload-batches
```

The second command should return a JSON body with `batchId`, `status`, and
`maxFiles`.

## 5. Install Frontend Dependencies

Run once, or whenever frontend dependencies change:

```bash
cd /Users/hoangdeveloper/catalog-classifier/apps/web
npm install
```

This installs the Next.js admin frontend and its test/build tooling.

## 6. Start The Frontend

Open a dedicated terminal for the frontend:

```bash
cd /Users/hoangdeveloper/catalog-classifier/apps/web
npm run dev
```

This component serves the local admin web app on `http://localhost:3000`.

It owns:

- the ingest page at `http://localhost:3000/admin/ingest`;
- browser-side file selection;
- direct upload requests to Cloud Storage signed URLs;
- upload progress and retry user interface state;
- local review proof-of-concept pages.

The frontend uses `http://localhost:8000` as the backend default. If the backend
runs elsewhere, start the frontend with:

```bash
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000 npm run dev
```

Open:

```text
http://localhost:3000/admin/ingest
```

After a successful direct upload, the frontend automatically finalizes the batch
with the backend. A fully verified batch should show `Backend status: queued`.

## 7. Stop The App

Stop the backend and frontend with `Ctrl-C` in their terminals.

Stop PostgreSQL without deleting local data:

```bash
cd /Users/hoangdeveloper/catalog-classifier
docker compose down
```

Only use this when you intentionally want to erase local development data:

```bash
docker compose down -v
```

## Component Summary

| Component | Command | Local address | Purpose |
| --- | --- | --- | --- |
| PostgreSQL with pgvector | `docker compose up -d --force-recreate postgres` | `localhost:5432` | Durable workflow metadata and vector-capable database |
| FastAPI backend | `.venv/bin/uvicorn catalog_api.main:app --reload --port 8000` | `http://localhost:8000` | Backend API, upload workflow, local worker endpoint |
| Next.js frontend | `npm run dev` | `http://localhost:3000` | Admin ingest and review user interface |
| Google Cloud Storage | external service | `gs://lnlabs-bucket` | Original and derived image object storage |
