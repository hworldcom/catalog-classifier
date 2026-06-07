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

The first usable release ends with reviewed product groups stored in PostgreSQL. The first implementation milestone is upload foundation. Product codes, multilingual descriptions, and Sanity publication should be built as the next layer on the same architecture.

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
* Sanity publication commands.

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
* Sanity asset storage only after a product has been approved for publication
* PostgreSQL for metadata, workflow state, scores, vectors, and review actions

### Existing catalog integration

* Sanity remains the public catalog CMS.
* PostgreSQL becomes the operational ingestion database.
* Do not use Sanity as the processing queue or image-grouping database.
* Approved products are explicitly published from PostgreSQL into Sanity.

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
FastAPI / publication service ─────────► Sanity
```

---

## 4. Repository structure

Use a monorepo:

```text
catalog-classifier/
├── apps/
│   └── web/                    # Next.js application
├── services/
│   ├── api/                    # FastAPI HTTP API
│   └── worker/                 # Image-processing worker
├── packages/
│   └── api-client/             # Generated TypeScript OpenAPI client
├── infra/
│   └── terraform/              # GCP infrastructure
├── docker-compose.yml          # Local Postgres and services
└── README.md
```

The Next.js frontend must use a generated OpenAPI client. Do not manually duplicate request and response interfaces in Python and TypeScript.

---

## 5. Upload workflow

### Step 1: Create an upload batch

The frontend sends:

```http
POST /v1/upload-batches
```

Example response:

```json
{
  "batchId": "uuid",
  "status": "created",
  "maxFiles": 200
}
```

### Step 2: Register files and obtain signed URLs

The frontend sends filenames, MIME types, file sizes, and client-side ordering:

```http
POST /v1/upload-batches/{batchId}/uploads
```

The API:

1. Validates file count and file metadata.
2. Creates an `image_asset` row for every file.
3. Generates a unique object path.
4. Returns one temporary signed upload URL per file.

Object path format:

```text
organizations/{organizationId}/batches/{batchId}/originals/{imageId}.jpg
```

### Step 3: Upload directly from browser to Cloud Storage

The browser uploads each file directly to Cloud Storage.

Requirements:

* maximum four concurrent uploads;
* individual progress indicators;
* retry failed files independently;
* do not restart successful uploads;
* preserve original selection order;
* allow the user to remove a file before processing starts.

Do not proxy the file bodies through Next.js or FastAPI.

### Step 4: Finalize the batch

After all intended uploads complete:

```http
POST /v1/upload-batches/{batchId}/finalize
```

The backend confirms that the objects exist and enqueues processing tasks.

Repeated finalize requests must be idempotent.

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

Generate one image embedding per normalized image.

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

### Stage E: Category classification

Classify each image against a closed category taxonomy.

The model must return structured JSON:

```json
{
  "categoryId": "tshirts",
  "confidence": 0.91,
  "attributes": {
    "productType": "t-shirt",
    "audience": "men",
    "dominantColor": "black"
  }
}
```

Rules:

* Category IDs must come from the database taxonomy.
* Never let the model invent a category slug.
* Use `unknown` when confidence is insufficient.
* Store the raw model response for debugging.
* Category classification is a suggestion until approved.

Initial categories might include:

```text
clothing
  ├── t-shirts
  ├── hoodies
  ├── trousers
  ├── jackets
  └── sportswear
```

The taxonomy must be editable without changing model code.

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

The decision should consider:

* embedding similarity;
* whether category predictions agree;
* visible design, pattern, logo, construction, and shape;
* whether differences appear to be camera angle or background;
* whether differences indicate a genuinely different design;
* whether colour differences represent variants of the same design.

Use a multimodal comparison model only for ambiguous candidate pairs. Do not send every possible pair to the generative model.

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
3. Merge only when the candidate image is sufficiently compatible with:

   * the group representative; and
   * the group overall.
4. Stop merging when confidence falls below the high-precision threshold.
5. Leave uncertain images as singleton groups.

Thresholds must be configuration values, not constants scattered through the code.

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
* reject an image;
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
thumbnail_object_key
original_filename
upload_order
mime_type
size_bytes
width
height
sha256
phash
dhash
status
error_code
error_message
created_at
```

#### `image_embeddings`

```text
id
image_id
provider
model
dimensions
embedding vector(768)
created_at
```

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

#### `image_classifications`

```text
id
image_id
category_id
confidence
attributes_json
provider
model
raw_response_json
pipeline_version
created_at
```

#### `pair_assessments`

```text
id
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

#### `product_group_images`

```text
group_id
image_id
position
membership_source
membership_confidence
is_duplicate
duplicate_of_image_id
```

#### `review_events`

```text
id
organization_id
batch_id
user_id
action_type
payload_json
created_at
```

#### `processing_jobs`

```text
id
batch_id
image_id nullable
job_type
status
attempt_count
started_at
completed_at
error_message
idempotency_key
```

#### `catalog_products`

This becomes useful during the Sanity publication milestone:

```text
id
organization_id
product_code
category_id
status
sanity_document_id
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
POST   /v1/groups/{groupId}/images
DELETE /v1/groups/{groupId}/images/{imageId}
POST   /v1/groups/merge
POST   /v1/groups/{groupId}/split
PATCH  /v1/groups/{groupId}
POST   /v1/groups/{groupId}/approve
POST   /v1/upload-batches/{batchId}/approve
```

### Processing API

Private worker endpoints:

```text
POST /internal/tasks/process-image
POST /internal/tasks/classify-image
POST /internal/tasks/group-batch
POST /internal/tasks/find-existing-products
```

These endpoints must require authenticated service-to-service requests. They must not be publicly callable.

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

Do not send image bytes through the task payload. Send only identifiers such as:

```json
{
  "imageId": "uuid",
  "batchId": "uuid",
  "pipelineVersion": "2026-06-01"
}
```

The worker retrieves the image from Cloud Storage.

When all image-level jobs are complete, enqueue one batch-grouping job. Protect this with a database constraint or idempotency key so that it is created only once.

---

## 12. Sanity integration

Do not upload every raw ingestion image into Sanity immediately.

Publishing flow:

1. User approves the product group.
2. Backend validates the product record.
3. Approved images are uploaded to Sanity.
4. Sanity asset references are stored in PostgreSQL.
5. Product document is created or updated.
6. Sanity document ID is persisted.
7. The public catalog is revalidated.

The backend must validate all required fields before making Sanity mutations.

Recommended product document fields:

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

Publishing must be explicit and initiated by an authorized user. Classification completion must never automatically publish a product.

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
* separate service accounts for API, worker, and publisher;
* secrets in Google Secret Manager or deployment environment secrets;
* no Sanity write token in the browser;
* audit records for merge, split, deletion, approval, and publication.

The client-provided filename must never be used directly as a storage path.

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
* Sanity publishing with a test dataset.

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

Unknown batches and images return `404`. The read-only review route is:

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
6. Show per-file progress, retry, and remove-before-finalize behavior.
7. Implement finalize and batch status retrieval.
8. Persist uploaded file metadata and preserve selection order.
9. Add tests for upload validation, idempotent finalize behavior, and batch reload.

### Milestone 2: Per-image processing

Deliver:

* Cloud Tasks integration;
* worker service;
* validation and normalization;
* exact hashes;
* perceptual hashes;
* image embeddings;
* category suggestions.

### Milestone 3: Grouping and review

Deliver:

* candidate-pair generation;
* same-product scoring;
* conservative grouping;
* review interface;
* merge, split, move, duplicate, and approve operations;
* review-event logging.

### Milestone 4: Existing-catalog matching

Deliver:

* embeddings for existing products;
* nearest-product search;
* “possible existing product” suggestions;
* no automatic linking during the first release.

### Milestone 5: Catalog publication

Deliver:

* deterministic product codes;
* multilingual structured descriptions;
* Sanity asset upload;
* Sanity product mutation;
* publication status and retry handling.

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
10. Use the resulting approved groups as input for the later Sanity-publication stage.
