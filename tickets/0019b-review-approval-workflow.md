# Ticket 0019b: Review Approval Workflow

## Status

Planned

## Objective

Add the backend endpoints that approve reviewed groups and approve a batch.

This ticket locks in the human-reviewed result and prepares it for the later
product-draft export stage.

## Scope

- Add the approval endpoints already described in the README:
  - `POST /v1/groups/{groupId}/approve`
  - `POST /v1/upload-batches/{batchId}/approve`
- Persist approval actions as `review_events` rows.
- All successful approval endpoints return the updated review snapshot from
  `GET /v1/upload-batches/{batchId}/groups`.
- The read endpoint returns snapshots for batches in `review_required` and
  `approved`.
- Keep approval operations idempotent where practical.
- Keep approved groups immutable after approval.
- Allow group approval with `approvedCategoryId = null`; category approval is
  optional in this slice.
- Batch approval requires every group in the batch to be approved.
- Batch approval sets the batch to `approved` and records `completed_at`.
- Batch approval does not mutate group memberships, duplicate flags, or
  approved categories; it only finalizes the batch state.
- Batch approval leaves the approved group rows unchanged; it only promotes
  the batch state and returns the same reviewed snapshot under `approved`.
- Reject invalid or cross-batch approval operations with explicit errors.
- Add focused backend tests for group approval and batch approval.

## Endpoint Contracts

Unless otherwise noted, repeated requests that would not change state are
no-op successes and do not write review events.

- `POST /v1/groups/{groupId}/approve`
  - No request body.
  - Approves one group.
  - If the group is already approved, return a no-op success.
- `POST /v1/upload-batches/{batchId}/approve`
  - No request body.
  - Approves the batch once every group in the batch is approved.
  - If the batch has no groups, approval is allowed once the batch is in the
    review phase.
  - If any group is still proposed, return an explicit error.
  - If the batch is already approved, return a no-op success.

## Review Event Contract

- `action_type` values are `approve_group` and `approve_batch`.
- `payload_json` stores the ids and values changed by the approval action.
- Successful no-op requests do not write a review event.

## Assumptions

- Tickets `0017`, `0018`, and `0019a` are complete.
- Review edits are already persisted before approval.
- The review page will consume the API in ticket `0020a`.

## Acceptance Criteria

- The backend can approve a group.
- The backend can approve a batch.
- Approved groups become immutable.
- Batch approval only succeeds when the batch is fully reviewed.
- Batch approval does not rewrite group rows or memberships.
- Every approval writes a review event.
- Repeated approval requests do not create duplicate state.
- Focused backend tests pass locally.

## Dependencies

- Ticket `0018-same-product-grouping-engine`.
- Ticket `0017-grouping-schema-and-review-read-model`.
- Ticket `0019a-review-editing-api`.

## Non-goals

- Review page UI.
- Review edit operations.
- Existing-catalog matching.
- Product publication.
- Cloud Tasks integration.
- Worker authentication.

## Validation Notes

- Verify a group can be approved after review edits are applied.
- Verify a batch can be approved only after all groups are approved.
- Verify approval writes one review event.
- Verify repeated approval is a no-op.
