# Catalog Image Classification Tool

## Engineering Architecture and Implementation Instructions

## 1. Objective

Build an internal catalog-ingestion application that allows a shop employee to:

1. Drag or select one or more product images.
2. Upload them reliably with visible progress.
3. Automatically identify:

   * exact duplicate files;
   * visually near-identical images;
   * broad product category;
   * images belonging to the same product;
   * potential matches with products already in the catalog.
4. Review the proposed groups.
5. Merge, split, move, or remove images manually.
6. Approve the final product groups.

The first usable release ends with reviewed product groups stored in PostgreSQL. The first implementation milestone is upload foundation. Product codes, multilingual descriptions, and Bazoria product-draft export should be built as the next layer on the same architecture.

The system must favor precision over recall:

* A missed grouping is acceptable because the user can merge groups.
* A false merge is dangerous because it creates an incorrect product listing.
* Uncertain images must remain separate or be marked for review.

Do not implement an autonomous-agent framework. Use a deterministic processing pipeline with explicit model calls and persisted intermediate results.

### Current planning assumptions

* The frontend lives in this repository.
* The first iteration accepts JPEG files only.
* There is no minimum batch size; one or more JPEG images are valid.
* Authentication is deferred for the first iteration.
* The first step is to prove frontend/backend upload communication before database persistence.
* The next local proof of concept is manual corrections on temporary filesystem state, not PostgreSQL.
* That local proof of concept clusters exact duplicates only; it does not attempt near-duplicate or same-product matching.
* The local persistence endpoint retains the same limits as the upload-handshake endpoint: 1 to 20 files per request and 10 mebibytes per file.
* Every accepted image belongs to exactly one group; exact duplicates share a group and unmatched images become singleton groups.
* The first milestone is upload foundation, not automatic product review.
* The temporary upload-handshake endpoint accepts at most 20 files per request.
* Each file may be at most 10 mebibytes (10,485,760 bytes).
* These limits are configuration values and may change when direct signed uploads are implemented.

### Ticketing structure

Implementation work is tracked in the `tickets/` folder at the repository root.

Ticket rules:

* one ticket per small task or vertical slice;
* use a stable numeric prefix and short slug, for example `tickets/0001-upload-handshake.md`;
* include objective, scope, assumptions, acceptance criteria, dependencies, and validation notes;
* keep the ticket updated if the scope changes;
* use tickets to break the roadmap into reviewable steps.

---

## 2. Recommended technology stack

### Frontend

* Next.js App Router
* TypeScript
* React
* Tailwind CSS
* shadcn/ui for standard components
* `react-dropzone` for drag-and-drop selection
* TanStack Query for API calls, polling, retries, and cache state
* Authentication is deferred until after upload foundation
* Vercel deployment

The classification tool lives in this repository and should use routes such as:

```text
/admin/ingest
/admin/ingest/[batchId]
/admin/review/[batchId]
```

Public catalog routes must remain separate from internal ingestion routes.

### Backend API

* Python
* FastAPI
* Pydantic
* SQLAlchemy 2
* Alembic migrations
* PostgreSQL
* pgvector
* Google Cloud Run

FastAPI owns:

* authentication and authorization;
* batch creation;
* signed-upload URL generation;
* workflow state;
* review operations;
* job creation;
* Bazoria product-draft export commands.

FastAPI must not perform image classification inside normal user-facing HTTP requests.

### Processing worker

* Python
* Pillow
* OpenCV where needed
* `imagehash` for perceptual hashes
* NumPy
* Google Gemini API for embeddings and visual classification
* Cloud Run private worker service
* Cloud Tasks for asynchronous execution

### Storage

* Google Cloud Storage for temporary and original ingestion files
* public asset storage only after a product has been approved for publication
* PostgreSQL for metadata, workflow state, scores, vectors, and review actions

### Existing catalog integration

* Bazoria Web owns the public catalog and product-draft persistence.
* PostgreSQL becomes the operational ingestion database.
* Do not use the classifier as the public catalog system.
* Approved groups are explicitly exported from PostgreSQL into Bazoria Web.

---

## 3. System architecture

```text
┌──────────────────────────────┐
│ Next.js admin application    │
│ Vercel                       │
│                              │
│ Upload / status / review UI  │
└──────────────┬───────────────┘
               │ API metadata
               ▼
┌──────────────────────────────┐
│ FastAPI service              │
│ Google Cloud Run             │
│                              │
│ Auth, batches, review, jobs  │
└───────┬─────────┬────────────┘
        │         │
        │         ├──────────────► PostgreSQL + pgvector
        │
        ├──────────────► Cloud Tasks
        │                      │
        │                      ▼
        │              ┌──────────────────────┐
        │              │ Image worker         │
        │              │ Private Cloud Run    │
        │              │                      │
        │              │ Hash, embed, classify│
        │              │ group, compare       │
        │              └───────┬──────────────┘
        │                      │
        │                      ├────► Gemini API
        │                      ├────► PostgreSQL
        │                      └────► Cloud Storage
        │
Browser ───────────────► Private Cloud Storage bucket
        direct signed upload

Approved products only:
FastAPI / export service ───────────────► Bazoria Web
```

---

## 4. Repository structure

Use a monorepo:

```text
catalog-classifier/
├── apps/
│   └── web/                    # Next.js application
├── services/
│   ├── api/                    # FastAPI HTTP API; see services/api/README.md
│   └── worker/                 # Image-processing worker
├── packages/
│   └── api-client/             # Generated TypeScript OpenAPI client
├── infra/
│   └── terraform/              # GCP infrastructure
├── docker-compose.yml          # Local Postgres and services
└── README.md
```

For the current local startup flow, including the commands to start each running
component, see [`docs/local-start.md`](docs/local-start.md).

The Next.js frontend must use a generated OpenAPI client by default. Do not
manually duplicate request and response interfaces in Python and TypeScript.
Prototype slices may use handwritten helpers when a ticket explicitly says so;
ticket `0020a` is one such temporary exception.

---

## 5. Upload workflow

### Step 1: Create an upload batch

The frontend sends:

```http
POST /v1/upload-batches
```

The API responds with `200 OK` and body:

```json
{
  "batchId": "uuid",
  "status": "created",
  "maxFiles": 20
}
```

Each request creates a durable `upload_batches` row under the seeded default
organization. The database supplies the identifier, `created` status, zero counts, and
timestamps. This endpoint does not register files or store image bytes.

### Step 2: Register files and obtain signed URLs

The frontend sends filenames, MIME types, file sizes, and client-side ordering:

```http
POST /v1/upload-batches/{batchId}/uploads
```

The API:

1. Validates file count and file metadata.
2. Creates an `image_asset` row for every file.
3. Generates a unique object path.
4. Returns one temporary signed `PUT` upload URL per file.

The batch must still be in `created` state when registration starts. The request is
all-or-nothing: if any file metadata is invalid, the registration fails and no rows
are written. Successful registration transitions the batch to `uploading`, records
the file count in `original_file_count`, keeps `processed_file_count` at zero, and
leaves the image rows in `pending` while file transfers are still in progress.

The signed upload URLs expire after 15 minutes.

Registration is not idempotent. If a later request needs fresh signed URLs for the
same uploaded image rows, a separate re-sign endpoint will handle that without
creating new image rows. Retryable rows may move to a new object key during that
flow so stale upload objects do not get reused.

Object path format:

```text
organizations/{organizationId}/batches/{batchId}/originals/{imageId}.jpg
```

### Step 3: Upload directly from browser to Cloud Storage

The ingest page implements the first reduced slice of this step: create a durable
batch, register the selected JPEG metadata, upload each file directly to its signed
`PUT` URL, and show uploaded or failed status per file.

Requirements for this reduced slice:

* maximum four concurrent uploads;
* validate one to 20 JPEG files from 1 byte through 10 mebibytes before submission;
* preserve original selection order;
* match registered files by `uploadOrder` so duplicate filenames remain valid;
* send `Content-Type: image/jpeg` with every signed `PUT` request;
* show the durable batch identifier as soon as batch creation succeeds;
* continue queued uploads when one upload fails;
* leave failed or partially uploaded batches in `uploading` until a later recovery
  flow exists;
* do not finalize the batch or redirect to the local review page;
* do not proxy the file bodies through Next.js or FastAPI.

Later upload-user-experience improvements such as percentage progress and
remove-before-finalize behavior are intentionally deferred to later tickets.

### Step 4: Finalize the batch

After all intended uploads complete:

```http
POST /v1/upload-batches/{batchId}/finalize
```

The backend confirms that the objects exist, records per-image verification results,
and marks the batch `queued` only after every image verifies. This slice does not
create processing tasks yet; `queued` means verified and ready for future work.

The same batch resource is also readable through:

```http
GET /v1/upload-batches/{batchId}
```

That readback returns the batch status, counts, timestamps, and images ordered by
`uploadOrder`. If any image fails verification, the batch stays `uploading`, the
failed images keep their error details, `finalized_at` and `completed_at` remain
`null`, and repeated finalize on an already `queued` batch returns the current state
without changing anything or re-checking storage.

Repeated finalize requests must be idempotent.

### Step 5: Re-sign retryable uploads

When finalization leaves images in `pending` or `failed`, the backend can prepare a
selected subset for another direct browser upload:

```http
POST /v1/upload-batches/{batchId}/retry-failed
```

The batch must still be `uploading`. The backend locks the batch and selected image
rows, rejects non-retryable selections, assigns each selected row a new object key,
and returns fresh signed `PUT` URLs in `uploadOrder`. Image statuses and error details
remain unchanged until finalization runs again.

Each retry uses a new UUID-based object path so the API service account does not need
permission to overwrite existing objects. Signing and object-key updates are
all-or-nothing. Objects from earlier attempts are intentionally retained until the
later stale-object cleanup slice.

The ingest page exposes this flow inline. Retryable rows have checkboxes and a
`Retry selected` action. Before requesting new URLs, the browser reloads the durable
batch and reconciles it with the current in-memory upload results:

* durable `uploaded` rows are never retried;
* durable `failed` rows require their original browser `File`;
* durable `pending` rows preserve a local `uploaded` result and allow a local
  `pending` or `failed` result to retry;
* rows without an in-memory file remain visible but cannot retry.

If the batch or selected rows changed, the user must review and select again. The
browser validates that the retry response contains exactly the requested image
identifiers before sending any file bytes, uploads at most four retry files at once,
and preserves per-row local results after the attempts finish. Selecting a new file
set discards the current in-memory retry session.

---

## 6. Image-processing pipeline

Each image passes through the following stages.

### Stage A: Validation and normalization

The worker must:

1. Download the object from private storage.
2. Decode the image fully.
3. Reject corrupted or unsupported files.
4. Apply EXIF orientation.
5. Convert unsupported formats into normalized JPEG or PNG.
6. Create an inference-sized copy.
7. Create a thumbnail for the review UI.
8. Record width, height, format, and size.
9. Strip unnecessary EXIF metadata from derived images.

Keep the untouched original separately.

### Stage B: Exact duplicate detection

Calculate:

```text
SHA-256(original file bytes)
```

Images with identical SHA-256 values are exact duplicates.

Exact duplicates may be automatically marked as duplicates, but the original upload records must not be deleted. The review screen should show which image is retained.

### Stage C: Near-duplicate detection

Calculate perceptual hashes, initially:

* pHash;
* dHash.

Use perceptual hashes only to identify candidates such as:

* resized copies;
* recompressed copies;
* lightly cropped copies;
* screenshots of the same image.

A perceptual-hash match does not by itself prove that two images belong to the same product.

### Stage D: Image embedding

Generate one image embedding per processed image.
Store one embedding row per organization, image, and pipeline version.
This stage is forward-only: images already completed before this ticket lands
are not backfilled here.
The embedding input is the bounded inference image created in Stage B.
This stage depends on ticket `0013a-pgvector-foundation`, which enables vector
storage in PostgreSQL.
Local development uses the `pgvector/pgvector:pg16` PostgreSQL image; service-level
setup and reset notes live in `services/api/README.md`.

Initial configuration:

```text
Provider: Google Gemini
Model: gemini-embedding-2
Dimensions: 768
Distance: cosine distance
```

Store:

* provider;
* model;
* dimensions;
* vector;
* generation timestamp;
* pipeline version.

All embedding access must go through an internal interface:

```python
class ImageEmbeddingProvider(Protocol):
    def embed_image(self, image: bytes) -> list[float]:
        ...
```

Business logic must not call the Gemini SDK directly.
The prototype implementation uses `google-genai` behind that interface and reads
`GEMINI_API_KEY` from the worker environment. Automated tests must use a fake
embedding provider rather than calling Gemini.

### Stage E: Category classification

Classify each image into one primary category against a closed category taxonomy.

The model must return structured JSON:

```json
{
  "categorySlug": "t-shirts",
  "confidence": 0.91
}
```

Rules:

* Category slugs must come from the database taxonomy.
* Never let the model invent a category slug.
* Use `unknown` when confidence is below `0.80`, the slug is missing, or the
  category is not in the active global taxonomy.
* Store `unknown` as a null `category_id` in `image_classifications`.
* Store the raw model response for debugging.
* Category classification is a suggestion until approved.
* Malformed JSON or provider failures are retryable and do not persist a row.

Initial seeded categories include:

```text
clothing
  ├── t-shirts
  ├── hoodies
  ├── trousers
  ├── jackets
  └── sportswear
```

The taxonomy must be editable without changing model code.
The prototype implementation uses Google Gemini through `google-genai` behind
an internal category-suggestion provider interface and reads `GEMINI_API_KEY`
from the worker environment. The category model defaults to `gemini-2.5-flash`
and can be overridden with `CATALOG_CATEGORY_MODEL`. Automated tests use a fake
provider rather than calling Gemini.
The broader Bazoria taxonomy and seller-to-organization mapping are deferred to
later tickets; the MVP continues with the current classifier organization model
and seeded clothing categories.

### Stage F: Candidate-pair generation

Do not compare every image with every historical image.

For each image:

1. Find its nearest images within the current batch.
2. Find its nearest approved products from the same organization.
3. Prefer candidates within the predicted category.
4. Keep the top candidate set, for example the nearest 10–20 images.

Candidate generation may consider:

* embedding cosine similarity;
* perceptual-hash distance;
* upload sequence distance;
* category compatibility;
* image dimensions and aspect ratio;
* existing approved product matches.

Upload ordering is a weak signal. Adjacent images are often related, but ordering must never override strong visual evidence.

### Stage G: Same-product decision

Classify candidate pairs into:

```text
same_product
different_product
uncertain
```

The first pass should use deterministic signals only. The decision should
consider:

* embedding similarity;
* whether category predictions agree;
* visible design, pattern, logo, construction, and shape;
* whether differences appear to be camera angle or background;
* whether differences indicate a genuinely different design;
* whether colour differences represent variants of the same design.

When both category predictions are known, category compatibility is a hard
gate. Unknown or missing category suggestions are neutral.

Use these threshold bands for pair decisions:

* `same_product` normal path: similarity clears the same-product threshold, the
  category gate passes, and perceptual-hash distance is acceptable when both
  hashes are available.
* `same_product` strong path: similarity clears the strong same-product
  threshold and the category gate passes. This strong embedding signal may
  override a perceptual-hash conflict.
* `uncertain`: similarity is between the uncertain and same-product
  thresholds, or signals conflict, or missing evidence prevents a safe merge.
* `different_product`: similarity falls below the uncertain threshold, or
  known categories conflict.

Missing embeddings or hashes do not fail grouping; they only reduce available
evidence. When evidence is insufficient, keep the image singleton.

If the deterministic signals are still too conservative, add multimodal
comparison later in ticket `0021`. Do not send every possible pair to the
generative model in the first grouping slice.

Persist:

```text
image_a_id
image_b_id
embedding_similarity
phash_distance
category_match
sequence_distance
model_decision
model_confidence
pipeline_version
```

### Stage H: Group construction

Build groups from high-confidence same-product relationships.

Do not use unrestricted connected components alone. A chain such as:

```text
A similar to B
B similar to C
A not similar to C
```

must not automatically merge all three images.

Use constrained agglomerative grouping:

1. Start each non-duplicate image as its own group.
2. Select a representative or medoid for each group.
3. Merge only when the candidate image is sufficiently compatible with every
   current member of the target group.
4. Stop merging when confidence falls below the high-precision threshold.
5. Leave uncertain images as singleton groups.

Thresholds must be configuration values, not constants scattered through the code.

Use these starting values:

* `CATALOG_GROUPING_MAX_CANDIDATES_PER_IMAGE = 50`
* `CATALOG_GROUPING_PHASH_MAX_DISTANCE = 8`
* `CATALOG_GROUPING_UNCERTAIN_SIMILARITY_THRESHOLD = 0.80`
* `CATALOG_GROUPING_SAME_PRODUCT_SIMILARITY_THRESHOLD = 0.85`
* `CATALOG_GROUPING_STRONG_SAME_PRODUCT_SIMILARITY_THRESHOLD = 0.92`

Pairs below the uncertain threshold stay separate. Pairs between the uncertain
and same-product thresholds remain uncertain. Pairs that clear the same-product
threshold and the category gate may merge when perceptual-hash distance is
acceptable. Pairs that clear the strong same-product threshold and the category
gate may merge even when perceptual-hash distance is above the normal maximum.
Known category conflicts always block automatic merging.

If proposed groups already exist for a batch, rerunning grouping is a no-op
success that returns the existing groups unchanged.

---

## 7. Product and variant semantics

The data model must distinguish:

* `product_group`: the sellable product design;
* `product_variant`: colour, size, or similar option;
* `image_asset`: an individual photograph;
* `duplicate_image`: a redundant copy of another photograph.

For the first milestone, automatic grouping should target the same product design.

When there is uncertainty about whether two colours are variants of one product or separate products, keep them in separate groups and show a “possible variant” suggestion.

Do not permanently encode the assumption that every colour is a separate product.

---

## 8. Review interface

The review page is a required part of the MVP. Automatic grouping without human correction is insufficient.

Display proposed product groups as cards or columns.

Each group should show:

* cover image;
* category suggestion;
* grouping confidence;
* number of images;
* possible existing-product match;
* warnings;
* processing status.

Required operations:

* drag an image from one group to another;
* create a new group from selected images;
* merge groups;
* split a group;
* mark an image as duplicate;
* restore a duplicate;
* change category;
* select cover image;
* image rejection is deferred (ticket `0022`);
* approve a group.

Every manual action must be stored as a review event.

Do not recalculate and overwrite manual corrections when a pipeline is rerun.

### Recommended screen flow

```text
Upload
   ↓
Processing
   ↓
Review proposed groups
   ↓
Approve
   ↓
Ready for product details
```

The processing page should show counts:

```text
Uploaded: 100
Validated: 100
Embedded: 93
Classified: 84
Grouped: 0
Failed: 1
```

Use polling initially. WebSockets are not required for the MVP.

---

## 9. Database model

Every business table must include `organization_id`, even though the first deployment may serve only one shop.

### Core tables

#### `organizations`

```text
id
name
created_at
```

#### `upload_batches`

```text
id
organization_id
created_by
status
original_file_count
processed_file_count
pipeline_version
created_at
finalized_at
completed_at
```

Suggested statuses:

```text
created
uploading
queued
processing
review_required
approved
failed
cancelled
```

#### `image_assets`

```text
id
organization_id
batch_id
original_object_key
normalized_object_key
inference_object_key
thumbnail_object_key
original_filename
upload_order
mime_type
size_bytes
width
height
normalized_format
normalized_size_bytes
sha256
phash
dhash
status
error_code
error_message
created_at
```

`size_bytes` remains the original uploaded file size from registration. The
normalized image uses the new `normalized_format` and `normalized_size_bytes`
fields, while `width` and `height` describe the normalized image after EXIF
orientation and conversion.

`phash` and `dhash` are stored as lowercase 16-character hexadecimal strings
derived from the normalized image.

#### `image_embeddings`

```text
id
organization_id
image_id
provider
model
dimensions
pipeline_version
embedding vector(768)
created_at
```

The schema stores one embedding row per organization, image, and pipeline
version.
The embedding row uses an organization-scoped foreign key from
`(organization_id, image_id)` to the processed image row.

#### `categories`

```text
id
organization_id nullable
parent_id
slug
name_pl
name_en
name_de
name_vi
active
```

Seeded categories are global rows with `organization_id = null`. Global slugs are
unique through a partial unique index for rows where `organization_id` is null.
Organization-specific rows may be added later without changing model code.

#### `image_classifications`

```text
id
organization_id
image_id
category_id nullable
confidence
attributes_json
provider
model
raw_response_json
pipeline_version
created_at
```

The schema enforces one classification row per organization, image, and
pipeline version. The classification row uses an organization-scoped foreign key
from `(organization_id, image_id)` to the image row. A null `category_id`
represents an `unknown` suggestion.
The `attributes_json` column stores the structured classification payload for
this slice, which currently contains the category slug and confidence.
`raw_response_json` stores the unmodified provider response for debugging.

#### `pair_assessments`

```text
id
organization_id
batch_id
image_a_id
image_b_id
embedding_similarity
phash_distance
category_match
upload_order_distance
decision
confidence
decision_source
pipeline_version
created_at
```

Store one row per unordered image pair and pipeline version. Canonicalize the
image order before insert and enforce uniqueness on
`(organization_id, batch_id, image_a_id, image_b_id, pipeline_version)`.
Pair assessments are tenant- and batch-scoped.
The application canonicalizes pair order, and the database rejects reversed
rows with a check constraint.
Use `CHECK` constraints for status and decision values, and require
`image_a_id < image_b_id` for canonical pair storage.
`decision_source` values are `heuristic`, `exact_duplicate`, and later
`multimodal_model`. `singleton` is a group outcome, not a pair source.

#### `product_groups`

```text
id
organization_id
batch_id
status
suggested_category_id
approved_category_id
cover_image_id
confidence
possible_existing_product_id
created_at
approved_at
```

`product_groups.status` values are `proposed`, `approved`, and `rejected`.
When grouping proposals are created, the batch moves from `processing` into
`review_required`.

`product_groups.cover_image_id` must point to the lowest `upload_order`
non-duplicate image in the same organization and batch. `suggested_category_id`
and `approved_category_id` are nullable foreign keys to `categories`.
`product_groups.confidence` is the minimum accepted same-product pair
confidence used to build the group, or `1.0` for singleton groups.

#### `product_group_images`

```text
organization_id
batch_id
group_id
image_id
position
membership_source
membership_confidence
is_duplicate
duplicate_of_image_id
```

Enforce one row per `(organization_id, batch_id, image_id)` and a unique
`position` value per group. Use `organization_id` and `batch_id` to keep
membership tenant- and batch-scoped and prevent cross-batch attachment.
`membership_source` values are `engine`, `singleton`, `exact_duplicate`, and
later `manual_review`.
`membership_confidence` is nullable and should be `null` for singleton
memberships.
`position` follows the image `upload_order` within the group.
`duplicate_of_image_id` must be null or point to another image in the same
organization, batch, and group.

#### `review_events`

```text
id
organization_id
batch_id
group_id nullable
image_id nullable
user_id nullable
action_type
payload_json
created_at
```

Review events store the minimum audit data needed for review edits and approval
tracking. Suggested `action_type` values are `create_group`, `move_image`,
`remove_image`, `merge_groups`, `split_group`, `update_group`,
`mark_duplicate`, `restore_duplicate`, `approve_group`, and `approve_batch`.
Successful no-op requests do not write a review event.

Implementation should use stricter database constraints where practical:

* composite foreign keys using `(id, organization_id)` or
  `(id, organization_id, batch_id)` where practical;
* unique membership on `(organization_id, batch_id, image_id)`;
* flexible JSON for `review_events.payload_json`.

#### `processing_jobs`

```text
id
organization_id
batch_id
image_id nullable
job_type
status
attempt_count
pipeline_version
created_at
started_at
completed_at
error_message
idempotency_key
```

Processing jobs use the statuses `pending`, `started`, `completed`, and `failed`.

#### `catalog_products`

This becomes useful during the Bazoria product-draft export milestone:

```text
id
organization_id
product_code
category_id
status
bazoria_product_draft_id
created_at
published_at
```

---

## 10. API surface

### Batch and upload API

```text
POST   /v1/upload-batches
POST   /v1/upload-batches/{batchId}/uploads
POST   /v1/upload-batches/{batchId}/finalize
GET    /v1/upload-batches/{batchId}
GET    /v1/upload-batches/{batchId}/images
POST   /v1/upload-batches/{batchId}/retry-failed
```

### Review API

```text
GET    /v1/upload-batches/{batchId}/groups
GET    /v1/categories
POST   /v1/upload-batches/{batchId}/groups
POST   /v1/groups/{groupId}/images
DELETE /v1/groups/{groupId}/images/{imageId}
POST   /v1/groups/merge
POST   /v1/groups/{groupId}/split
PATCH  /v1/groups/{groupId}
PATCH  /v1/groups/{groupId}/images/{imageId}
POST   /v1/groups/{groupId}/approve
POST   /v1/upload-batches/{batchId}/approve
```

`GET /v1/upload-batches/{batchId}/groups` returns a review snapshot when the
batch status is `review_required` or `approved`. A review-ready batch with zero
groups returns `groups: []`. An approved batch returns the same snapshot shape.
Any other batch status returns `409` with `batch_not_review_ready`.
The `thumbnailUrl` field in that snapshot uses the durable thumbnail endpoint
`/v1/upload-batches/{batchId}/images/{imageId}/thumbnail`.
`GET /v1/categories` returns the active global categories that power the
review-page category selector.
Categories are returned in deterministic tree order, then `nameEn` within each
sibling set.
The review page resolves `approvedCategorySlug` against that list before
sending `approvedCategoryId` back to the backend.
The review selector shows the full category tree. Parent categories remain
visible but disabled; only leaf categories are selectable in this prototype.
`PATCH /v1/groups/{groupId}` is a partial patch: send either `coverImageId` or
`approvedCategoryId` in one request. `approvedCategoryId` can be set to `null`
to clear the approval. Non-null `approvedCategoryId` must point to an active
global leaf category. `coverImageId` must point to a non-duplicate member of
the group.
`PATCH /v1/groups/{groupId}/images/{imageId}` requires `isDuplicate: true` to
include `duplicateOfImageId` pointing to another non-duplicate image in the
same group. When `isDuplicate: false`, `duplicateOfImageId` must be `null`.
Review edit endpoints only work when the batch status is `review_required`.
Reject `processing`, `queued`, and `approved` batches.
Groups that are already approved remain read-only even while the batch is still
`review_required`.
For `POST /v1/groups/{groupId}/split`, empty selections are invalid. Selecting
images that already exactly match the current group membership is a no-op
success. Selecting a single image is allowed and creates a singleton group.
Batch approval only changes batch state; it does not mutate group memberships
or duplicate flags.
Every successful review mutation returns the updated review snapshot from
`GET /v1/upload-batches/{batchId}/groups`.

### Future Review API: Ticket 0022

Image rejection is deferred. The active Review API contract does not include
rejection endpoints or `isRejected` snapshot fields.

If product later decides to add rejection, a likely shape is:

* store rejection state on `product_group_images.is_rejected`;
* expose `isRejected` in the review snapshot;
* add reject and restore review actions and corresponding review events;
* define how rejection interacts with cover images, duplicate masters, and
  empty groups.

Ticket `0022` will define those semantics if and when product decides to add
them.

### Processing API

Private worker endpoints:

```text
POST /internal/tasks/process-image
POST /internal/tasks/classify-image
POST /internal/tasks/group-batch
POST /internal/tasks/find-existing-products
```

`POST /internal/tasks/group-batch` is the Milestone 3 grouping entrypoint. The
backend creates one `group-batch` processing job when all `process-image`
jobs for the batch are terminal, and local tests may call the same grouping
service function synchronously.

Milestone 2 prototype work uses a local fake queue and the local `process-image`
and `classify-image` workers. Ticket `0016` adds Cloud Tasks and authenticated
service-to-service worker requests for production hardening.

In production, these endpoints must require authenticated service-to-service
requests. They must not be publicly callable.

---

## 11. Asynchronous processing rules

Cloud Tasks may retry requests. Therefore every processing operation must be idempotent.

Example:

```text
process-image:{imageId}:{pipelineVersion}
```

Before processing, the worker checks whether the operation has already completed successfully.

The worker should:

1. mark the job as started;
2. perform one bounded operation;
3. persist the result transactionally;
4. mark the job as completed;
5. return success.

`process-image` job statuses are `pending`, `started`, `completed`, and
`failed`. Terminal `process-image` statuses are `completed` and `failed`.

Do not send image bytes through the task payload. Send only identifiers such as:

```json
{
  "imageId": "uuid",
  "batchId": "uuid",
  "pipelineVersion": "2026-06-01"
}
```

The worker retrieves the image from Cloud Storage.

After a `process-image` job reaches a terminal state, the backend checks
whether all `process-image` jobs for the batch are terminal. If yes, it creates
exactly one `group-batch` `ProcessingJob` with an idempotency key. The local
runner may execute the grouping service synchronously in tests and local
development.

When a `process-image` job completes successfully for an image, enqueue one
`classify-image` job for the same image. Protect this with a database
constraint or idempotency key so it is created only once.
Grouping uses only images whose `image_assets.status` is `processed`; failed
images are excluded, and classification does not gate grouping.

---

## 12. Bazoria product-draft export

Do not export every raw ingestion image into Bazoria Web immediately.

Export flow:

1. User approves the product group.
2. Backend validates the product record.
3. Approved groups are exported to Bazoria Web as product-draft payloads.
4. Bazoria Web persists the product draft and handles public publication later.

The backend must validate all required fields before exporting a product draft.

Recommended product-draft fields:

```text
productCode
category
images
coverImage
name.pl
name.en
name.de
name.vi
description.pl
description.en
description.de
description.vi
status
sourceBatchId
```

Exporting must be explicit and initiated by an authorized user. Classification completion must never automatically create a product draft.

---

## 13. Future product-code and description stage

### Product codes

Generate product codes deterministically, not with an AI model.

Example format:

```text
{organizationPrefix}-{categoryPrefix}-{sequence}
HH-TS-000123
```

Use a database sequence or transactionally locked counter to guarantee uniqueness.

### Multilingual descriptions

Generate one structured source representation first:

```json
{
  "productType": "sports t-shirt",
  "material": null,
  "fit": "regular",
  "colors": ["black", "red"],
  "features": ["short sleeves"],
  "uncertainFields": ["material"]
}
```

Then produce Polish, Vietnamese, German, and English catalog text from the structured record.

Do not allow the model to invent:

* material;
* sizes;
* stock;
* certifications;
* brand;
* country of origin;
* technical features not visible in the images.

Unknown attributes must remain null or require manual input.

---

## 14. Security and privacy

Required controls:

* private Cloud Storage buckets;
* short-lived signed upload URLs;
* MIME-type and file-size validation;
* server-side image decoding;
* non-public worker service;
* JWT validation in FastAPI;
* organization-level authorization on every query;
* separate service accounts for API, worker, and exporter/integration;
* secrets in Google Secret Manager or deployment environment secrets;
* no Bazoria Web write token in the browser;
* audit records for merge, split, deletion, approval, and export.

The client-provided filename must never be used directly as a storage path.

## 14.1 Google Cloud upload foundation

The direct browser upload path depends on a small amount of manual Google Cloud setup
in the development project:

* Create a private Cloud Storage bucket for ingestion objects. The current local
  development bucket is `gs://lnlabs-bucket`.
* Keep public access prevention and uniform bucket-level access enabled so uploaded
  objects stay private.
* Configure bucket CORS for origins `http://localhost:3000` and
  `http://127.0.0.1:3000`, method `PUT`, and request header `Content-Type` so the
  browser can send direct signed upload requests from the local Next.js frontend.
* Create a dedicated API service account. The current one is
  `catalog-api@catalog-classifier.iam.gserviceaccount.com`.
* Grant that service account `roles/storage.objectCreator` on the bucket so the
  backend can write upload objects through signed URLs.
* Grant that service account `roles/storage.objectViewer` on the bucket so the
  backend can verify uploaded objects during finalization.
* Enable the Service Account Credentials API.
* Grant the local signing principal `roles/iam.serviceAccountTokenCreator` on the API
  service account so it can impersonate that account and call
  `iam.serviceAccounts.signBlob`.

This setup exists for the upload foundation work in ticket `0006`. The bucket stays
private, the browser never receives broad bucket access, and the API remains the only
component that can mint upload URLs.

The API process must receive the upload configuration in its environment. For local
development, set the values in the same terminal before starting the API:

```bash
export CATALOG_UPLOAD_BUCKET=lnlabs-bucket
export CATALOG_SIGNING_SERVICE_ACCOUNT="catalog-api@catalog-classifier.iam.gserviceaccount.com"
.venv/bin/uvicorn catalog_api.main:app --reload --port 8000
```

Restart the API after adding or changing these values because the running process does
not inherit later shell changes. If the bucket configuration or signing credentials
are unavailable, file registration returns `500` with code
`upload_registration_failed`. The database transaction is rolled back, so the batch
remains `created` and no image rows are retained.

A successful registration creates image metadata and returns signed URLs; it does not
upload image bytes. An object appears in Cloud Storage only after the client sends the
file in a separate signed `PUT` request.

If the frontend origin changes, add the new origin to bucket CORS and to
`CATALOG_WEB_ORIGINS` in the API service.

---

## 15. Observability

Every log line should include where applicable:

```text
organization_id
batch_id
image_id
group_id
job_id
pipeline_version
```

Record metrics for:

* upload failure rate;
* image-processing failure rate;
* average processing duration;
* embedding API latency;
* model API error rate;
* number of proposed groups;
* number of manual merges;
* number of manual splits;
* duplicate rate;
* percentage of singleton groups.

Manual review actions are important evaluation data. A high split rate indicates false merges; a high merge rate indicates overly conservative grouping.

---

## 16. Evaluation dataset

Before tuning thresholds, create a labelled benchmark from real shop images.

The benchmark should contain:

* exact duplicates;
* recompressed images;
* different angles of the same product;
* same design in different colours;
* visually similar but different products;
* front and back views;
* products on models and on plain backgrounds;
* packaging photos;
* labels or size-tag photos;
* existing catalog products.

Label image pairs as:

```text
same_product
different_product
same_variant
different_variant
duplicate_image
uncertain
```

Evaluate:

* same-product precision;
* same-product recall;
* duplicate precision;
* category accuracy;
* percentage requiring manual correction.

Initial production target:

```text
High-confidence automatic group precision: at least 98%
```

When uncertain, lower recall rather than lowering precision.

---

## 17. Testing requirements

### Unit tests

Test:

* hashing;
* normalization;
* file validation;
* category response parsing;
* idempotency keys;
* grouping constraints;
* authorization;
* product-code generation.

### Integration tests

Test:

* signed URL creation;
* upload finalization;
* Cloud Task creation;
* worker retry behavior;
* PostgreSQL vector persistence;
* review actions;
* Bazoria product-draft export with a test dataset.

### End-to-end tests

Use Playwright to test:

1. create batch;
2. upload several images;
3. observe processing;
4. move an image between groups;
5. merge groups;
6. approve the batch.

Model calls must be mockable. CI must not depend on live Gemini calls.

---

## 18. MVP implementation order

### Milestone 1: Upload foundation

Deliver:

* admin route;
* drag-and-drop component;
* batch creation;
* JPEG-only validation;
* signed uploads;
* progress and retry;
* image thumbnails;
* PostgreSQL schema.

### Milestone 0: Frontend/backend upload handshake

Deliver:

* web application scaffold in `apps/web`;
* backend service scaffold in `services/api`;
* upload page with multi-file JPEG selection;
* `POST /v1/upload-handshake`, accepting multipart form data under the `files` field;
* one to 20 files per request, with a 10 mebibyte limit per file;
* validation of decoded file content rather than trust in the filename or declared media type;
* a backend response containing an upload identifier, an overall status, and per-file results;
* temporary file handling that discards all file contents after validation;
* backend integration tests for the multipart contract;
* frontend tests for request construction and completed or failed response states;
* manual browser validation of the real frontend-to-backend round trip.

The temporary response uses this shape:

```json
{
  "uploadId": "uuid",
  "status": "completed",
  "files": [
    {
      "filename": "product-front.jpg",
      "status": "accepted",
      "sizeBytes": 123456,
      "errorCode": null,
      "errorMessage": null
    }
  ]
}
```

Overall status values:

* `completed`: every file was accepted;
* `partial`: at least one file was accepted and at least one was rejected;
* `rejected`: every supplied file was rejected.

Per-file status values are `accepted` and `rejected`. Invalid JPEG content and files larger
than 10 mebibytes are per-file rejections. Missing files, more than 20 files, and malformed
multipart requests reject the request as a whole.

#### Run Milestone 0 locally

Start the backend:

```bash
cd services/api
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/uvicorn catalog_api.main:app --reload --port 8000
```

Start the web application in another terminal:

```bash
cd apps/web
npm install
npm run dev
```

Open `http://localhost:3000/admin/ingest`. The web application uses
`http://localhost:8000` by default. Set `NEXT_PUBLIC_API_BASE_URL` when the backend
runs elsewhere. Set the backend `CATALOG_WEB_ORIGINS` variable to a comma-separated
list when the web application uses origins other than `http://localhost:3000` or
`http://127.0.0.1:3000`.

Run the automated checks:

```bash
cd services/api
.venv/bin/pytest

cd ../../apps/web
npm test
npm run lint
npm run build
```

Verify the backend directly with a JPEG file:

```bash
curl \
  -F 'files=@/absolute/path/product-front.jpg;type=image/jpeg' \
  http://localhost:8000/v1/upload-handshake
```

Non-goals:

* PostgreSQL persistence;
* batch workflow state;
* signed upload URLs;
* image classification;
* grouping;
* authentication;
* browser automation with Playwright.

### Milestone 0.1: Local persistence and exact duplicate review

This local proof of concept stores accepted JPEG files on the backend filesystem and
groups only byte-identical files. It does not perform perceptual hashing, category
classification, or same-product matching.

Create a batch:

```text
POST /v1/local-batches
```

The request uses the same multipart `files` field and limits as the upload handshake:
one to 20 files, with a maximum of 10 mebibytes per file. If at least one file is
accepted, the response includes a batch identifier and manifest version. If every file
is rejected, `batchId`, `manifestVersion`, and every rejected `imageId` are `null`; no
batch directory is created.

Load a batch and its exact-duplicate groups:

```text
GET /v1/local-batches/{batchId}
GET /v1/local-batches/{batchId}/images/{imageId}
GET /v1/local-batches/{batchId}/images/{imageId}/thumbnail
```

Unknown batches and images return `404`. The review route is:

```text
/admin/review/{batchId}
```

The local storage root defaults to `services/api/.local-data` and can be overridden
with `CATALOG_LOCAL_STORAGE_ROOT`. Each batch uses this layout:

```text
batches/{batchId}/manifest.json
batches/{batchId}/images/{imageId}.jpg
batches/{batchId}/thumbnails/{imageId}.jpg
```

Manifest version 1 uses this shape:

```json
{
  "manifestVersion": 1,
  "batchId": "uuid",
  "status": "ready",
  "createdAt": "2026-06-07T12:00:00Z",
  "images": [
    {
      "imageId": "uuid",
      "originalFilename": "front.jpg",
      "uploadOrder": 0,
      "sha256": "hex",
      "groupId": "uuid",
      "isRetained": true
    }
  ],
  "groups": [
    {
      "groupId": "uuid",
      "retainedImageId": "uuid",
      "imageIds": ["uuid"]
    }
  ]
}
```

The backend stores original uploaded bytes unchanged and computes Secure Hash Algorithm
256-bit (SHA-256) over those bytes. It creates oriented thumbnails separately. Every
accepted image belongs to exactly one group: identical hashes share a group, unmatched
images receive singleton groups, and the earliest uploaded member is retained. Manifest
writes use a temporary file followed by an atomic replacement.

### Milestone 0.2: Manual local review edits

The local review page supports two explicit edits:

```text
PATCH /v1/local-batches/{batchId}/images/{imageId}
POST  /v1/local-batches/{batchId}/groups
```

The move request contains a target `groupId` and returns `{ "batch": { ... } }`.
Moving an image to its current group is a successful no-op.

The create-group request contains one or more unique `imageIds` and returns
`{ "groupId": "uuid", "batch": { ... } }`. Empty selections, repeated identifiers,
and selections whose membership already equals an existing group are rejected with
`400`. Membership comparison ignores identifier order. Unknown batches, images, and
target groups return `404`.

Each write loads one manifest, applies the edit, removes empty groups, sorts group
members by `uploadOrder`, and retains the lowest-order member. Existing groups keep
their relative order and newly created groups are appended. The updated manifest is
written atomically and returned so the web application can replace its local state.

The editable review interface uses checkboxes to select images for a new group and a
target-group selector plus a Move button for each image. All edit controls are disabled
while a request is running. After manual edits, group labels are neutral: `Group`,
`Member`, and `Retained` do not claim that manually combined images are exact duplicates.
This local proof of concept does not add review history or concurrent editing.

### Milestone 1 implementation sequence

Keep the first milestone small and vertical. A practical order is:

1. Define the database schema and migrations for organizations, upload batches, and image assets.
2. Create the batch creation endpoint.
3. Create the file registration endpoint and signed upload URL generation.
4. Build the drag-and-drop upload screen with local previews.
5. Support direct browser upload to cloud storage.
6. Implement finalize and batch status retrieval.
7. Re-sign missing or failed uploads.
8. Add inline frontend retry without re-uploading successful files.
9. Add remove-before-finalize behavior and stale-batch cleanup.

Each slice in the sequence should include focused tests in its ticket; testing is part
of the slice, not a separate milestone.

#### Upload database foundation

The first Milestone 1 slice introduces PostgreSQL metadata persistence without changing
the existing local filesystem proof of concept. It adds:

* `organizations`, `upload_batches`, and `image_assets` tables;
* organization-scoped foreign keys and uniqueness constraints;
* ordered image registration within a batch;
* explicit batch and image lifecycle constraints;
* a stable default organization for local development;
* SQLAlchemy models and Alembic migrations;
* a PostgreSQL 16 service for local migration validation.

Image bytes and thumbnails remain outside PostgreSQL. The detailed schema contract,
engineering decisions, migration commands, and validation workflow are maintained in
[`services/api/README.md`](services/api/README.md).

### Milestone 2: Per-image processing

Deliver:

* processing job foundation;
* worker service;
* validation and normalization;
* exact hashes;
* perceptual hashes;
* image embeddings;
* category suggestions.

#### Milestone 2 implementation sequence

Keep this milestone narrow and vertical:

1. Add processing job schema, local queue dispatch, and a worker entrypoint.
2. Validate, normalize, and hash each image.
3. Add pgvector storage foundation in ticket `0013a`.
4. Finalize successful direct browser uploads from the frontend in ticket `0013b`.
5. Generate perceptual hashes and embeddings in ticket `0013c`.
6. Add the category taxonomy and classification schema in ticket `0014a`.
7. Write category suggestions in ticket `0014b`.
8. Add the start-processing endpoint in ticket `0015a`.
9. Add the read-only processing page with polling in ticket `0015b`.
10. Add thumbnail access and rendering in ticket `0015c`.

Milestone 2 ends with per-image processing data persisted in the database and a
read-only page that shows it, including thumbnails. Grouping and review remain
in Milestone 3.

Prototype work uses a local fake queue and the local `process-image` and
`classify-image` workers. Cloud Tasks integration and worker authentication are
deferred to a later hardening ticket.

### Milestone 3: Grouping and review

Deliver:

* candidate-pair generation;
* same-product scoring;
* conservative grouping;
* review interface;
* merge, split, move, duplicate, category, and approve operations;
* review-event logging.

#### Milestone 3 implementation sequence

Keep this milestone narrow and vertical:

1. Add the grouping schema and review read model in ticket `0017`.
2. Add the same-product grouping engine in ticket `0018`.
3. Add the review editing API in ticket `0019a` and the approval workflow in
   ticket `0019b`.
4. Add the review workbench UI across tickets `0020a` through `0020e`.
   Ticket `0022` is deferred until image-rejection semantics are decided.

Milestone 3 ends with proposed same-product groups stored in PostgreSQL and a
review page that lets an operator correct and approve them.

### Milestone 4: Existing-catalog matching

Deliver:

* embeddings for existing products;
* nearest-product search;
* “possible existing product” suggestions;
* no automatic linking during the first release.

### Milestone 5: Bazoria product-draft export

Deliver:

* deterministic product codes;
* multilingual structured descriptions;
* Bazoria product-draft export;
* export status and retry handling.

---

## 19. Explicit non-goals for the first milestone

Do not build yet:

* autonomous AI agents;
* automatic public publication;
* inventory management;
* pricing;
* checkout;
* supplier payments;
* mobile applications;
* advanced model training;
* custom GPU infrastructure;
* Kubernetes;
* fully automatic variant detection.

The first release should solve one operational bottleneck well: turning a disorganized image batch into reliable, human-approved product groups.

---

## 20. Definition of done for the first usable release

The release is complete when a user can:

1. Upload 100 images without routing them through the application server.
2. Refresh or reopen the page without losing the batch.
3. See independent progress and errors for every image.
4. Receive proposed duplicate and product groupings.
5. Correct all groupings through merge, split, and drag operations.
6. Approve the final groups.
7. Rerun failed jobs safely without producing duplicate records.
8. View a complete audit history of manual corrections.
9. Process another organization’s data without any cross-organization visibility.
10. Use the resulting approved groups as input for the later Bazoria product-draft export stage.
