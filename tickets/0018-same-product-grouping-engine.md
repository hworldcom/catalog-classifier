# Ticket 0018: Same-Product Grouping Engine

## Status

Implemented

## Objective

Automatically propose conservative same-product groups for processed images,
using category suggestions when available.

This ticket turns processed, categorized images into review-ready product-group
proposals. It uses the existing image embeddings, perceptual hashes, and
category suggestions when available, then persists the result for the review
UI.

## Scope

- Add `POST /internal/tasks/group-batch` as the batch-grouping worker
  entrypoint.
- After a `process-image` job reaches a terminal state, check whether all
  `process-image` jobs for the batch are terminal. Lock the batch row while
  doing that check and while creating the grouping job. If the batch is ready,
  create exactly one `group-batch` processing job with an idempotency key.
  Duplicate attempts must be rejected by the database or become no-op
  successes.
- `process-image` job statuses are `pending`, `started`, `completed`, and
  `failed`; terminal `process-image` statuses are `completed` and `failed`.
- Generate candidate pairs using existing image signals:
  - embedding similarity;
  - perceptual-hash distance;
  - category compatibility;
  - upload-order distance;
  - image dimensions and aspect ratio when useful.
- Use configuration-backed thresholds with conservative starting values:
  - `CATALOG_GROUPING_MAX_CANDIDATES_PER_IMAGE = 50`;
  - `CATALOG_GROUPING_PHASH_MAX_DISTANCE = 8`;
  - `CATALOG_GROUPING_UNCERTAIN_SIMILARITY_THRESHOLD = 0.80`;
  - `CATALOG_GROUPING_SAME_PRODUCT_SIMILARITY_THRESHOLD = 0.92`.
- Pair decisions use those thresholds as follows:
  - `same_product` when similarity clears the same-product threshold, the
    category gate passes, and perceptual-hash distance is acceptable when both
    hashes are available;
  - `uncertain` when similarity is between the uncertain and same-product
    thresholds, or when signals conflict, or when missing evidence prevents a
    safe merge;
  - `different_product` when similarity falls below the uncertain threshold or
    when known categories conflict.
- Classify candidate pairs as same-product, different-product, or uncertain.
- Persist pair assessments.
- Build groups with constrained agglomerative logic.
- Avoid chain merges that would collapse uncertain images into the same group.
- Merge only when the candidate image is sufficiently compatible with every
  current member of the target group.
- Group only images whose `image_assets.status` is `processed`.
- Exclude `failed` images from grouping in this slice.
- Images without embeddings are not used for similarity candidate generation;
  they become singleton groups unless exact-duplicate logic applies.
- Treat missing or failed category suggestions as unknown or neutral.
- Leave uncertain images as singleton groups.
- Create the initial `product_groups` and `product_group_images` rows for the
  batch.
- Move the batch into the review phase when proposal generation completes.
- If no eligible processed images exist, still move the batch into
  `review_required` with zero groups.
- If `product_groups` already exist for the batch, return success without
  creating, deleting, or overwriting groups, memberships, or pair
  assessments.
- Allow the local prototype to invoke the same grouping service function
  synchronously in tests.
- Add focused backend tests for high-confidence matches, uncertain pairs,
  chain-merge avoidance, and idempotent repeat execution.

## Assumptions

- Ticket `0017` is complete.
- Tickets `0014b`, `0015b`, and `0015c` are complete.
- The grouping engine works on a batch whose `process-image` jobs are
  terminal. It consumes the processed image snapshot, creates review-ready
  grouping proposals, and then moves the batch to `review_required`. It does
  not wait for classification to finish.
- The first grouping target is the same product design, not publication or
  catalog matching.
- Classification is advisory, not a gate, and missing category suggestions are
  treated as unknown.
- Category compatibility is a hard gate when both category suggestions are
  known; unknown suggestions are neutral.
- Missing embeddings or hashes do not fail grouping; they only reduce
  available evidence and usually leave the image as a singleton.
- Failed images are excluded from grouping for this slice.
- Multimodal comparison is deferred to ticket `0021`.

## Acceptance Criteria

- A processed batch can be converted into proposed product groups.
- The grouping engine records pair assessments.
- The grouping engine starts once `process-image` is terminal for the batch and
  does not wait for `classify-image`.
- The batch readiness check and `group-batch` insert are protected so repeated
  terminal completions cannot create duplicate grouping jobs.
- The grouping engine does not merge through an unsafe similarity chain.
- Uncertain images remain singleton groups.
- Singleton memberships use `membership_source = singleton` and
  `membership_confidence = null`.
- Failed images are excluded from grouping.
- Rerunning grouping for a batch with existing `product_groups` is a no-op
  success.
- The batch enters the review phase after proposals are created.
- Repeated execution does not create duplicate groups or duplicate pair rows.
- Focused backend tests pass locally.
- The grouping thresholds are read from configuration, not hard-coded.

## Dependencies

- Ticket `0017-grouping-schema-and-review-read-model`.
- Ticket `0015a-start-processing-endpoint`.
- Ticket `0014b-category-suggestions-worker`.
- Ticket `0013c-perceptual-hashes-and-embeddings`.

## Non-goals

- Manual review edits.
- Review UI.
- Group approval.
- Existing-catalog matching.
- Product publication.
- Worker authentication.
- Multimodal pair comparison.

## Validation Notes

- Verify a known same-product pair is grouped together.
- Verify an uncertain chain does not collapse into one group.
- Verify pair assessments are persisted once per pair and pipeline version.
- Verify repeated job execution is a no-op.
