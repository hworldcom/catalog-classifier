# Ticket 0020d: Approval UI

## Status

Planned

## Objective

Add the approval controls that freeze the reviewed result for later export.

## Scope

- Let the operator approve a group.
- Let the operator approve the whole batch.
- Keep the batch approval action visible but disabled until every group is
  approved.
- Show helper text next to the disabled batch approval action:
  `Approve every group before approving the batch.`
- Show backend `409 review_approval_not_allowed` messages in the page-level
  action error area for stale state or race-condition failures.
- Disable all edit controls after approval.
- Keep approved batches read-only.
- Add focused frontend tests for group approval and batch approval flows.

## UI Behavior

- Show an `Approve group` action for each proposed group.
- Hide or disable the group approval action once the group is approved.
- Show an `Approve batch` action at the page level.
- Keep `Approve batch` disabled until every group is approved.
- Once the batch status is `approved`, render the whole review page as
  read-only:
  - no edit controls;
  - no approval controls;
  - approved status visible at the batch level;
  - approved status visible on groups.

## Assumptions

- Tickets `0020a`, `0020b`, `0020c`, and `0019b` are complete.
- The review page already renders the review snapshot and edit controls.

## Acceptance Criteria

- The operator can approve a group.
- The operator can approve a batch.
- Batch approval is visible but disabled until every group is approved.
- A stale backend approval error appears in the page-level action error area.
- All edit controls are disabled after approval.
- Focused frontend tests pass locally.

## Dependencies

- Ticket `0020a-durable-review-read-only-page`.
- Ticket `0020b-basic-review-edits`.
- Ticket `0020c-group-structure-edits`.
- Ticket `0019b-review-approval-workflow`.

## Non-goals

- Review edit actions.
- Same-product grouping algorithm.
- Existing-catalog matching.
- Product publication.
- Cloud Tasks integration.
- Worker authentication.

## Validation Notes

- Verify group approval updates the rendered state.
- Verify batch approval updates the rendered state.
- Verify batch approval is visible but disabled when not all groups are
  approved.
- Verify stale backend approval errors render in the page-level action error
  area.
- Verify edit controls are disabled after approval.
- Verify frontend tests pass locally.
