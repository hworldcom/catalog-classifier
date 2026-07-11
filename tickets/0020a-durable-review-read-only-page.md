# Ticket 0020a: Durable Review Read-Only Page

## Status

Planned

## Objective

Build the durable review page that lets an operator inspect proposed groups
before any edits or approvals are enabled.

## Scope

- Add a durable review page for a batch at `/admin/review/{batchId}`.
- Replace the previous local proof-of-concept review route with the durable
  review page.
- Load the review snapshot from `GET /v1/upload-batches/{batchId}/groups`.
- Render batch status, proposed groups, image thumbnails, duplicate state,
  suggested category, approved category, and confidence.
- Use the `thumbnailUrl` values from the review snapshot as-is.
- Use the existing handwritten frontend API helpers for this slice; generated
  client setup is deferred.
- If the batch is `approved`, render the page read-only.
- Show an empty state when the batch has no groups.
- Show an error state when the snapshot cannot be loaded or the batch is not in
  `review_required` or `approved`.
- Do not add edit actions or approval actions in this slice.
- Add focused frontend tests for loading, empty groups, approved/read-only, and
  error states.

## Assumptions

- Tickets `0015c`, `0017`, `0018`, `0019a`, and `0019b` are complete.
- The review snapshot already contains enough data to render the page,
  including `thumbnailUrl`.
- Existing-catalog match fields may be empty until Milestone 4.

## Acceptance Criteria

- The operator can open a review page for a review-ready batch.
- The operator can inspect the proposed groups before approval.
- The operator can open an approved batch in read-only mode.
- The page does not expose edit or approval controls.
- Focused frontend tests pass locally.

## Dependencies

- Ticket `0015c-thumbnail-access-and-rendering`.
- Ticket `0017-grouping-schema-and-review-read-model`.
- Ticket `0018-same-product-grouping-engine`.
- Ticket `0019a-review-editing-api`.
- Ticket `0019b-review-approval-workflow`.

## Non-goals

- Same-product grouping algorithm.
- Review edit actions.
- Approval actions.
- Existing-catalog matching.
- Product publication.
- Cloud Tasks integration.
- Worker authentication.

## Validation Notes

- Verify the page renders a review-ready batch with groups.
- Verify the page renders an approved batch as read-only.
- Verify the empty state renders cleanly.
- Verify thumbnail and error fallback behavior.
- Verify frontend tests pass locally.
