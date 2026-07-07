# Ticket 0015c: Thumbnail Access and Rendering

## Status

Planned

## Objective

Expose the already-generated thumbnail for each durable upload image and render
it on the processing page.

This ticket does not create thumbnails. The worker already generates and stores
them through `thumbnail_object_key`; this slice adds read access and frontend
rendering only.

## Scope

- Add `GET /v1/upload-batches/{batchId}/images/{imageId}/thumbnail`.
- Verify the batch and image belong to the default organization.
- Verify the image belongs to the requested batch.
- Return `404` when:
  - the batch does not exist;
  - the image does not exist;
  - the image does not belong to the requested batch;
  - `thumbnail_object_key` is `null`;
  - the storage object is missing.
- Read thumbnail bytes through the worker storage abstraction.
- Return `image/jpeg` on success.
- Set `Cache-Control: no-store` on all thumbnail responses, including `404`
  responses.
- Do not regenerate, resize, or rewrite thumbnails in this ticket.
- Do not expose signed URLs for thumbnails.
- Do not add thumbnail state to the processing snapshot.
- Derive the thumbnail URL in the frontend from `batchId` and `imageId`.
- Render thumbnails on the processing page when available.
- Show a placeholder when the thumbnail route returns `404` or the image fails
  to load.
- Do not treat a missing thumbnail as a processing failure.
- Add focused backend and frontend tests.

## API Contract

```http
GET /v1/upload-batches/{batchId}/images/{imageId}/thumbnail
```

Response behavior:

- `200 OK` with `Content-Type: image/jpeg` when the thumbnail exists;
- `404 Not Found` for missing batch, missing image, wrong batch, missing
  `thumbnail_object_key`, or missing storage object;
- `Cache-Control: no-store` on all responses.

## Assumptions

- Tickets `0012` and `0015b` are complete.
- Thumbnail generation already exists in the worker pipeline.
- The frontend processing page already exists and only needs thumbnail rendering.
- This ticket is read access only.

## Acceptance Criteria

- The processing page shows a thumbnail when the backend has created one.
- The processing page shows a placeholder while the thumbnail is unavailable.
- Missing thumbnails do not change processing state.
- The backend returns JPEG bytes for existing thumbnails.
- The backend returns `404` for missing or mismatched thumbnail requests.
- Thumbnail responses are not cached in a way that hides later availability.
- Focused backend and frontend tests pass locally.

## Dependencies

- Ticket `0015b-processing-dashboard-and-polling`.
- Ticket `0012-image-validation-normalization-exact-hash`.

## Non-goals

- Thumbnail generation.
- Image resizing.
- Thumbnail regeneration.
- Signed URL exposure.
- Cache-busting query parameters.
- Snapshot schema changes.
- Processing state changes.
- Grouping or review.

## Validation Notes

- Verify a present thumbnail returns `200` and `image/jpeg`.
- Verify missing batch, missing image, wrong batch, and missing thumbnail key
  all return `404`.
- Verify the response includes `Cache-Control: no-store`.
- Verify the processing page renders a thumbnail when available and a
  placeholder when not.
- Verify the processing page does not treat a missing thumbnail as a failure.
