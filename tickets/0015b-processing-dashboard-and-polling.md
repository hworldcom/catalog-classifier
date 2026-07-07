# Ticket 0015b: Processing Dashboard and Polling

## Status

Planned

## Objective

Add a read-only frontend processing page that lets a user start processing for a
finalized batch and observe per-image processing and classification results.

## Scope

- Add a processing page for a batch, separate from the ingest page.
- Link to the processing page after the ingest page successfully finalizes a
  durable upload batch.
- Show a `Start processing` action when the batch is `queued`.
- Call `POST /v1/upload-batches/{batchId}/start-processing` when the user starts
  processing.
- Treat the start-processing response as the initial snapshot; do not assume the
  batch has finished when the request returns.
- After the initial start call, poll `GET /v1/upload-batches/{batchId}/processing`
  until every visible image has terminal process and classify states.
- Do not call `POST /v1/upload-batches/{batchId}/start-processing` again during
  polling.
- Render batch-level state:
  - batch identifier;
  - batch status;
  - original file count;
  - processed file count;
  - pipeline version.
- Render ordered per-image state:
  - original filename;
  - image status;
  - process job status and error;
  - classify job status and error;
  - category suggestion slug or `unknown`;
  - confidence;
  - hash presence;
  - embedding presence.
- Keep the page read-only except for the explicit `Start processing` action.
- Add focused frontend tests for:
  - loading a queued batch;
  - clicking `Start processing`;
  - polling and rendering updated states;
  - rendering job errors.

## API Contract Used

This page consumes the endpoints from ticket `0015a`:

```http
POST /v1/upload-batches/{batchId}/start-processing
GET  /v1/upload-batches/{batchId}/processing
```

The page should not call:

```http
POST /internal/tasks/process-image
POST /internal/tasks/classify-image
```

Those are worker endpoints and must stay hidden behind the backend
orchestration endpoint.

## Assumptions

- Ticket `0015a-start-processing-endpoint` is complete.
- Tickets `0011` through `0014b`, including `0013a`, `0013b`, `0013c`,
  `0014a`, and `0014b`, are complete.
- The frontend still uses the existing local application shell.
- This page is for prototype QA and operator visibility, not review approval.

## Acceptance Criteria

- After upload finalization, the user can navigate to a processing page for the
  batch.
- A queued batch shows a clear `Start processing` action.
- Clicking `Start processing` starts the backend pipeline through the public
  admin endpoint, not internal worker endpoints.
- The page handles the prompt-returning start response and relies on polling to
  observe progress.
- The page shows updated process and classification state without manual refresh.
- The page polls `GET /v1/upload-batches/{batchId}/processing` after the initial
  start call; it does not poll by repeatedly calling `POST /start-processing`.
- The page stops polling when every visible image has terminal process and
  classify states.
- The page renders category suggestion and confidence when classification
  completes.
- The page renders process/classify errors clearly.
- The page does not expose grouping, review, category editing, or retry controls.
- Focused frontend tests pass locally.

## Dependencies

- Ticket `0015a-start-processing-endpoint`.
- Ticket `0014b-category-suggestions-worker`.
- Ticket `0013b-frontend-finalize-after-upload`.

## Non-goals

- Grouping.
- Review actions.
- Category edits.
- Retry flows.
- Cloud Tasks integration.
- Worker authentication.
- Product publication.

## Validation Notes

- Verify the full browser path:
  1. upload files;
  2. finalize batch;
  3. navigate to processing page;
  4. start processing;
  5. observe process and classification results.
- Verify the frontend never calls `/internal/tasks/*`.
- Verify polling stops or slows when all visible work reaches a terminal state.
