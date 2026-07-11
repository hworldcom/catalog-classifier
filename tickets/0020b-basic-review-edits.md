# Ticket 0020b: Basic Review Edits

## Status

Planned

## Objective

Add the first editable interactions to the durable review page.

## Scope

- Let the operator move an image to another group.
- Let the operator create a new group from selected images.
- The first implementation may use simple controls such as selecting a target
  group and clicking `Move`; drag-and-drop can be added later.
- Replace local UI state with the server response after each successful edit.
- Disable the relevant controls while a request is in flight.
- Keep approved batches read-only.
- Add focused frontend tests for move and create-group flows.

## Assumptions

- Tickets `0020a` and `0019a` are complete.
- The review page already renders the review snapshot.

## Acceptance Criteria

- The operator can move an image between groups.
- The operator can create a group from selected images.
- The rendered state updates from the server response.
- Controls are disabled while requests are running.
- Focused frontend tests pass locally.

## Dependencies

- Ticket `0020a-durable-review-read-only-page`.
- Ticket `0019a-review-editing-api`.

## Non-goals

- Merge groups.
- Split groups.
- Mark or restore duplicates.
- Change cover images.
- Approval actions.
- Existing-catalog matching.
- Product publication.
- Drag-and-drop interaction polish.

## Validation Notes

- Verify move-image updates the rendered groups.
- Verify create-group updates the rendered groups.
- Verify controls are disabled during requests.
- Verify frontend tests pass locally.
