# Ticket 0015a: Start Processing Endpoint

## Status

Planned

## Objective

Add small admin-facing backend endpoints that start the already implemented
per-image processing pipeline for a finalized upload batch and return a
read-only processing snapshot.

This endpoint replaces the current manual terminal sequence used during QA:
claim batch, call `process-image`, then call `classify-image`.

## Scope

- Add `POST /v1/upload-batches/{batchId}/start-processing`.
- Add `GET /v1/upload-batches/{batchId}/processing`.
- Add or reuse a backend service function that:
  - locks and claims a `queued` or already `processing` batch;
  - creates missing `process-image` jobs using the existing idempotency rules;
  - enqueues pending or failed retryable `process-image` jobs for that batch;
  - starts a local in-process background runner for the queued work;
  - returns promptly with the current processing snapshot instead of waiting for
    the whole batch to finish.
- The local background runner should:
  - create and own its own database session instead of reusing the request
    session;
  - resolve its own storage and provider dependencies inside the runner;
  - run pending or failed retryable `process-image` jobs for that batch;
  - run pending or failed retryable `classify-image` jobs created by successful
    image processing;
  - persist job failures and continue processing other eligible jobs when safe.
- Add or reuse a backend read function that returns the same processing snapshot
  without starting any work.
- Keep worker internals hidden from the frontend.
- Keep `/internal/tasks/process-image` and `/internal/tasks/classify-image` as
  worker endpoints, not frontend endpoints.
- Use the existing local queue and worker functions for this prototype.
- Do not implement Cloud Tasks in this ticket; keep the execution model shaped so
  the local runner can be replaced by Cloud Tasks later.
- Keep the endpoint idempotent:
  - calling it again for a completed or partially completed batch should not
    duplicate jobs, derived objects, embeddings, or classifications;
  - repeated successful image and classification jobs should be no-ops.
- Return per-image state that the frontend can render immediately:
  - image identifier;
  - upload order;
  - original filename;
  - image status;
  - process job status and error;
  - classify job status and error;
  - category suggestion slug or `unknown`;
  - confidence;
  - whether hashes and embeddings are present.
- Add focused backend tests for:
  - happy path from queued batch to processed and classified images;
  - idempotent repeated calls;
  - retryable provider failure surfaces job errors without duplicating rows;
  - invalid batch state is rejected;
  - the `GET` endpoint returns a stable read-only snapshot without creating jobs.

## API Contract

### Start processing

```http
POST /v1/upload-batches/{batchId}/start-processing
```

### Read processing state

```http
GET /v1/upload-batches/{batchId}/processing
```

Both endpoints return the same processing snapshot shape:

```json
{
  "batchId": "uuid",
  "status": "processing",
  "originalFileCount": 3,
  "processedFileCount": 3,
  "pipelineVersion": "2026-06-01",
  "images": [
    {
      "imageId": "uuid",
      "uploadOrder": 0,
      "originalFilename": "shirt.jpg",
      "imageStatus": "processed",
      "processJobStatus": "completed",
      "processError": null,
      "classifyJobStatus": "completed",
      "classifyError": null,
      "categorySlug": "t-shirts",
      "confidence": 0.91,
      "hasHashes": true,
      "hasEmbedding": true
    }
  ]
}
```

Status behavior:

- `queued` batch: claim it, enqueue image work, start the local background
  runner, and return the current snapshot promptly.
- `processing` batch: ensure eligible pending or retryable work is queued for the
  same pipeline version, start the local background runner if needed, and return
  the current snapshot promptly.
- already completed image/classification work: return as no-op state.
- non-finalized batch states such as `created` or `uploading`: return `409`.
- unknown batch: return `404`.

Read behavior:

- `GET /processing` never creates jobs and never calls worker code.
- `GET /processing` returns the current persisted snapshot only for batches that
  have reached the finalized processing path, starting with `queued`.
- non-finalized batch states such as `created` or `uploading`: return `409`.
- unknown batch: return `404`.

## Assumptions

- Tickets `0011` through `0014b`, including `0013a`, `0013b`, `0013c`,
  `0014a`, and `0014b`, are complete.
- This is a prototype-local orchestration endpoint.
- No request-scoped database session or FastAPI dependency is passed into deferred
  background work.
- Cloud Tasks integration and worker authentication remain deferred to ticket
  `0016`.
- The default pipeline version remains `2026-06-01` unless the backend already
  has a central version constant by implementation time.
- `POST /start-processing` returns promptly; progress is observed through
  `GET /processing`.

## Acceptance Criteria

- Frontend no longer needs to call internal worker endpoints.
- A finalized queued batch can be moved through processing and classification by
  calling one backend endpoint.
- The frontend has a stable read-only `GET` endpoint for polling processing
  state.
- `POST /start-processing` does not synchronously wait for the whole batch to
  finish.
- Repeated calls are safe and do not create duplicate jobs or result rows.
- The response includes enough per-image state for a read-only processing page.
- Focused backend tests pass locally.

## Dependencies

- Ticket `0014b-category-suggestions-worker`.
- Ticket `0014a-category-taxonomy-and-classification-schema`.
- Ticket `0013c-perceptual-hashes-and-embeddings`.
- Ticket `0013b-frontend-finalize-after-upload`.
- Ticket `0011-cloud-tasks-worker-foundation`.

## Non-goals

- Production queue execution.
- Worker authentication.
- Frontend page implementation.
- Grouping or review.
- Retry user interface.
- Editing category suggestions.

## Validation Notes

- Verify a newly finalized frontend upload can be processed without terminal
  scripts.
- Verify `image_assets`, `processing_jobs`, `image_embeddings`, and
  `image_classifications` are populated as expected.
- Verify repeated endpoint calls return stable results.
- Verify provider failures are visible in the response and database job rows.
- Verify the local runner opens its own database session and does not depend on
  the request session after the response returns.
- Verify `GET /processing` does not create jobs or trigger workers.
- Verify `created` and `uploading` batches return `409` for processing endpoints.
