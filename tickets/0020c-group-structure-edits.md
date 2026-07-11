# Ticket 0020c: Group Structure Edits

## Status

Planned

## Objective

Add the structural review actions that reshape groups before approval.

## Scope

- Let the operator merge groups using a simple selection flow:
  - choose one target group;
  - select one or more source groups;
  - click `Merge`.
- Let the operator split a group using a simple selection flow:
  - open one group;
  - select one or more images within that group;
  - click `Split into new group`.
- Let the operator mark an image as a duplicate by choosing a non-duplicate
  master image from the same group.
- Let the operator restore a duplicate.
- Show a `Set cover` control on each non-duplicate image.
- Replace local UI state with the server response after each successful edit.
- Disable the relevant controls while a request is in flight.
- Keep all controls disabled for approved batches.
- Add focused frontend tests for merge, split, duplicate, restore, and cover
  image flows.

## Assumptions

- Tickets `0020a` and `0019a` are complete.
- The review page already renders the review snapshot.

## Acceptance Criteria

- The operator can merge groups.
- The operator can split a group.
- The operator can mark and restore duplicates.
- The operator can change the cover image.
- The operator can only perform these actions while the batch is
  `review_required`.
- The rendered state updates from the server response.
- Controls are disabled while requests are running.
- Focused frontend tests pass locally.

## Dependencies

- Ticket `0020a-durable-review-read-only-page`.
- Ticket `0019a-review-editing-api`.

## Non-goals

- Move image to another group.
- Create group from selected images.
- Approved batch editing.
- Approval actions.
- Existing-catalog matching.
- Product publication.

## Validation Notes

- Verify merge, split, duplicate, restore, and cover image actions update the
  rendered groups.
- Verify approved batches render without editable controls.
- Verify controls are disabled during requests.
- Verify frontend tests pass locally.
