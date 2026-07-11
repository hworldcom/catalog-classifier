# Ticket 0019a: Review Editing API

## Status

Planned

## Objective

Add the backend endpoints for manual review edits and review event logging.

This ticket gives the review page the operations it needs to correct proposed
groups before approval and export.

## Scope

- Add the review edit endpoints already described in the README:
  - `POST /v1/upload-batches/{batchId}/groups`
  - `POST /v1/groups/{groupId}/images`
  - `DELETE /v1/groups/{groupId}/images/{imageId}`
  - `POST /v1/groups/merge`
  - `POST /v1/groups/{groupId}/split`
  - `PATCH /v1/groups/{groupId}`
  - `PATCH /v1/groups/{groupId}/images/{imageId}`
- All successful mutation endpoints return the updated review snapshot from
  `GET /v1/upload-batches/{batchId}/groups`.
- Persist every manual change as a `review_events` row.
- Do not write review events for no-op idempotent requests.
- Keep review operations idempotent where practical.
- Maintain stable group ordering and group membership ordering after edits.
- Remove empty groups when the last image leaves them.
- When an image is removed from a group, re-home it as a singleton group so the
  one-image-per-group invariant remains explicit.
- Reject invalid or cross-batch operations with explicit errors.
- Keep automatic grouping results from being silently overwritten on rerun.
- Add focused backend tests for each edit operation.
- Review edit operations only work when the batch status is
  `review_required`. Reject `processing`, `queued`, and `approved` batches.

## Endpoint Contracts

Unless otherwise noted, repeated requests that would not change state are
no-op successes and do not write review events.

- `POST /v1/upload-batches/{batchId}/groups`
  - Request body: `{ "imageIds": ["uuid", ...] }`
  - Creates a new review group from the selected images.
  - Empty `imageIds` is invalid.
  - Duplicate `imageIds` are invalid.
  - All images must belong to the batch.
  - Selecting one image is allowed and creates a singleton group.
  - If the same selection already exists, return a no-op success.
- `POST /v1/groups/{groupId}/images`
  - Request body: `{ "imageId": "uuid" }`
  - Moves one image into the target group.
- `DELETE /v1/groups/{groupId}/images/{imageId}`
  - No request body.
  - Removes the image from the group and makes it a singleton group.
- `POST /v1/groups/merge`
  - Request body: `{ "targetGroupId": "uuid", "sourceGroupIds": ["uuid", ...] }`
  - Preserves the target group id and absorbs the source groups into it.
  - `sourceGroupIds` must be non-empty, unique, in the same batch as the
    target group, and must not include `targetGroupId`.
  - After merge, source groups are deleted.
  - Repeating the same merge after the source groups were already deleted is
    an explicit error, not a no-op.
- `POST /v1/groups/{groupId}/split`
  - Request body: `{ "imageIds": ["uuid", ...] }`
  - Selected images become a new group and the original group keeps the
    remainder.
  - Empty selections are invalid.
  - Selecting images that already exactly match the current group membership is
    a no-op success.
- `PATCH /v1/groups/{groupId}`
  - Request body: partial patch. Send either `{ "coverImageId": "uuid" }` or
    `{ "approvedCategoryId": null }` or `{ "approvedCategoryId": "uuid" }` in
    one request.
  - `coverImageId` must belong to the group and must not be marked duplicate.
  - Empty bodies are invalid.
- `PATCH /v1/groups/{groupId}/images/{imageId}`
  - Request body: `{ "isDuplicate": true|false, "duplicateOfImageId": "uuid | null" }`
  - Controls duplicate metadata for one membership row. When restoring a
    duplicate, set `isDuplicate` to `false` and `duplicateOfImageId` to `null`.
  - If `isDuplicate` is `true`, `duplicateOfImageId` is required and must point
    to another image in the same group that is not itself marked duplicate.
  - If `isDuplicate` is `false`, `duplicateOfImageId` must be `null`.

## Review Event Contract

- `action_type` values are `create_group`, `move_image`, `remove_image`,
  `merge_groups`, `split_group`, `update_group`, `mark_duplicate`, and
  `restore_duplicate`.
- `payload_json` stores the ids and values changed by the action.
- Successful no-op requests do not write a review event.

## Assumptions

- Tickets `0017` and `0018` are complete.
- The review read endpoint already exists and returns the proposed groups.
- The review page will consume the API in ticket `0020`.

## Acceptance Criteria

- The backend can move an image between groups.
- The backend can create, merge, and split groups.
- The backend can mark duplicates and restore them.
- The backend can change category and cover image selection.
- Every manual edit writes a review event.
- Repeated safe operations do not create duplicate state.
- Focused backend tests pass locally.

## Dependencies

- Ticket `0018-same-product-grouping-engine`.
- Ticket `0017-grouping-schema-and-review-read-model`.

## Non-goals

- Review page UI.
- Approval workflow.
- Existing-catalog matching.
- Product publication.
- Cloud Tasks integration.
- Worker authentication.

## Validation Notes

- Verify move, merge, split, duplicate, restore, and manual change operations
  work against a review-ready batch.
- Verify each operation emits one review event.
- Verify empty groups are removed.
- Verify invalid cross-batch edits fail cleanly.
