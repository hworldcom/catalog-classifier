# Bazoria Website Integration Guide

This document summarizes what Bazoria website backend and frontend developers
need to integrate with `catalog-classifier`.

The classifier is an internal ingestion and review tool. Bazoria Web remains the
source of truth for public catalog data, public product pages, public images,
seller storefronts, leads, inquiries, subscriptions, and publication workflow.

## Current Status

The current classifier implementation supports:

* durable upload batches;
* direct browser upload to private Google Cloud Storage;
* upload verification and retry;
* image processing with hashes, thumbnails, embeddings, and category
  classification;
* same-product grouping proposals;
* a durable review page;
* review edits for move, merge, split, duplicate marking, cover image, and
  approved category;
* group approval and batch approval.

The classifier does not yet provide a production product-draft export endpoint.
That is a later integration slice. Until that exists, Bazoria Web should treat
approved classifier groups as reviewed internal ingestion results, not as public
products.

## Integration Principle

The integration boundary is:

```text
approved classifier group
-> Bazoria product-draft payload
-> Bazoria Web ProductDraft
-> Bazoria Web publication workflow
-> public product
```

Nothing from the classifier should become public automatically.

Classification completion should never create a public product. Even approved
classifier groups should become Bazoria product drafts first, where the website
side can handle seller data, names, descriptions, prices, stock, image promotion,
publication, and public catalog rules.

## Ownership Boundary

| Area | catalog-classifier owns | Bazoria Web owns |
| --- | --- | --- |
| Upload batch state | Yes | No |
| Private ingestion images | Yes | No |
| Signed upload URLs | Yes | No |
| Image validation and processing | Yes | No |
| Thumbnails for admin review | Yes | No |
| Category suggestions | Yes | No |
| Same-product grouping | Yes | No |
| Human grouping review | Yes, until export | Optional display after export |
| Review audit events | Yes | Optional import/reference |
| ProductDraft persistence | No | Yes |
| Public product catalog | No | Yes |
| Public image storage | No | Yes |
| Seller storefronts | No | Yes |
| Buyer inquiry flow | No | Yes |
| Product publication | No | Yes |

## Source of Truth

Use separate databases and treat them as separate bounded contexts:

```text
catalog-classifier PostgreSQL
  ingestion state
  processing state
  image assets
  grouping proposals
  review decisions

Bazoria Web PostgreSQL
  sellers
  product drafts
  public products
  public product images
  buyer-facing catalog data
```

Do not share classifier database tables directly with Bazoria Web. The website
backend should consume an Application Programming Interface (API) or an explicit
export payload, not read classifier tables.

## Sanity Is Out of Scope

The older Sanity content management system assumption is no longer part of the
target architecture.

The current target is:

```text
catalog-classifier
-> approved group export
-> Bazoria Web ProductDraft
-> Bazoria Web Product
```

Do not build new Sanity publication, Sanity asset upload, Sanity document
mutation, or Sanity revalidation integration for this project.

## Deployment Shape

Recommended production shape:

```text
Bazoria admin frontend
  calls Bazoria Web backend for website-owned workflows
  can call catalog-classifier API for internal ingestion/review screens if
  authorized

Bazoria Web backend
  owns seller, ProductDraft, Product, publication, public asset promotion
  calls catalog-classifier backend for approved ingestion results

catalog-classifier backend
  owns upload, processing, grouping, review, approval, export payloads

catalog-classifier worker
  owns image processing, classification, grouping, existing-product matching

private Google Cloud Storage bucket
  stores raw ingestion images and classifier thumbnails
```

The classifier frontend can remain a separate internal admin application, or the
Bazoria admin surface can embed/recreate the same flows. If Bazoria Web recreates
the flows, use the API contracts below instead of duplicating classifier logic.

## Local Development URLs

Current local defaults:

```text
catalog-classifier API: http://localhost:8000
catalog-classifier web: http://localhost:3000
```

The classifier web app reads:

```text
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
```

The private bucket Cross-Origin Resource Sharing (CORS) configuration must allow
the local frontend origins used by developers:

```text
http://localhost:3000
http://127.0.0.1:3000
```

The signed upload request must send:

```http
Content-Type: image/jpeg
```

## Current End-to-End Flow

### 1. Create Upload Batch

The browser creates a durable batch:

```http
POST /v1/upload-batches
```

Response:

```json
{
  "batchId": "uuid",
  "status": "created",
  "maxFiles": 20
}
```

This only creates the batch row. It does not register files and does not upload
image bytes.

### 2. Register Files

The browser registers selected files:

```http
POST /v1/upload-batches/{batchId}/uploads
Content-Type: application/json
```

Request:

```json
{
  "files": [
    {
      "originalFilename": "front.jpg",
      "mimeType": "image/jpeg",
      "sizeBytes": 193703
    }
  ]
}
```

Response:

```json
{
  "batchId": "uuid",
  "status": "uploading",
  "uploads": [
    {
      "imageId": "uuid",
      "uploadOrder": 0,
      "originalFilename": "front.jpg",
      "originalObjectKey": "organizations/{organizationId}/batches/{batchId}/originals/{imageId}.jpg",
      "uploadUrl": "https://storage.googleapis.com/..."
    }
  ]
}
```

Registration constraints:

* 1 to 20 files;
* JPEG only;
* file size from 1 byte through 10 mebibytes;
* registration is all-or-nothing;
* successful registration transitions the batch to `uploading`;
* signed upload URLs expire after 15 minutes.

The object path format is:

```text
organizations/{organizationId}/batches/{batchId}/originals/{imageId}.jpg
```

Client-provided filenames must never be used as storage paths.

### 3. Upload Directly To Google Cloud Storage

The browser uploads each file directly to its signed URL:

```http
PUT {uploadUrl}
Content-Type: image/jpeg
```

The application server must not proxy image file bodies.

Current browser behavior:

* maximum four concurrent uploads;
* duplicate filenames are allowed because rows are matched by `uploadOrder`;
* one failed upload does not stop the remaining uploads;
* failed or partially uploaded batches remain in `uploading`.

### 4. Finalize Upload Batch

After all intended uploads complete, the browser finalizes the batch:

```http
POST /v1/upload-batches/{batchId}/finalize
```

The backend verifies that every registered object exists and matches expected
metadata. If every image verifies, the batch becomes `queued`.

If any image fails verification:

* the batch stays `uploading`;
* per-image failures are returned;
* `finalizedAt` remains `null`;
* the user can retry failed uploads.

Finalizing an already queued batch is an idempotent no-op success.

The batch can be read with:

```http
GET /v1/upload-batches/{batchId}
```

Typical response shape:

```json
{
  "batchId": "uuid",
  "status": "queued",
  "originalFileCount": 2,
  "processedFileCount": 0,
  "createdAt": "2026-07-16T10:00:00Z",
  "finalizedAt": "2026-07-16T10:01:00Z",
  "completedAt": null,
  "images": [
    {
      "imageId": "uuid",
      "uploadOrder": 0,
      "originalFilename": "front.jpg",
      "status": "uploaded",
      "errorCode": null,
      "errorMessage": null
    }
  ]
}
```

### 5. Retry Failed Uploads

If finalization leaves retryable images in `pending` or `failed`, the browser can
request fresh signed URLs:

```http
POST /v1/upload-batches/{batchId}/retry-failed
Content-Type: application/json
```

Request:

```json
{
  "imageIds": ["uuid"]
}
```

The batch must still be `uploading`. The backend assigns new object keys and
returns fresh signed URLs for the selected images.

### 6. Start Processing

Once the batch is `queued`, processing can be started:

```http
POST /v1/upload-batches/{batchId}/start-processing
```

The endpoint should return promptly with the current processing snapshot. It
claims the batch and schedules backend-owned processing work.

Response shape:

```json
{
  "batchId": "uuid",
  "status": "processing",
  "originalFileCount": 2,
  "processedFileCount": 0,
  "pipelineVersion": "2026-06-01",
  "images": [
    {
      "imageId": "uuid",
      "uploadOrder": 0,
      "originalFilename": "front.jpg",
      "imageStatus": "uploaded",
      "processJobStatus": "pending",
      "processError": null,
      "classifyJobStatus": null,
      "classifyError": null,
      "categorySlug": null,
      "confidence": null,
      "hasHashes": false,
      "hasEmbedding": false
    }
  ]
}
```

### 7. Poll Processing State

The frontend polls:

```http
GET /v1/upload-batches/{batchId}/processing
```

When per-image processing and classification complete, image rows should show:

```json
{
  "imageStatus": "processed",
  "processJobStatus": "completed",
  "classifyJobStatus": "completed",
  "categorySlug": "trousers",
  "confidence": 0.95,
  "hasHashes": true,
  "hasEmbedding": true
}
```

When processing and grouping complete, the batch should move to
`review_required`.

### 8. Review Groups

The review page reads:

```http
GET /v1/upload-batches/{batchId}/groups
```

This endpoint works when the batch status is:

```text
review_required
approved
```

Other statuses return:

```text
409 batch_not_review_ready
```

Response:

```json
{
  "batchId": "uuid",
  "organizationId": "uuid",
  "status": "review_required",
  "pipelineVersion": "2026-06-01",
  "groups": [
    {
      "groupId": "uuid",
      "status": "proposed",
      "confidence": 1.0,
      "coverImageId": "uuid",
      "suggestedCategorySlug": "t-shirts",
      "approvedCategorySlug": null,
      "possibleExistingProductId": null,
      "warnings": [],
      "images": [
        {
          "imageId": "uuid",
          "originalFilename": "front.jpg",
          "uploadOrder": 0,
          "thumbnailUrl": "/v1/upload-batches/{batchId}/images/{imageId}/thumbnail",
          "position": 0,
          "isDuplicate": false,
          "duplicateOfImageId": null,
          "membershipSource": "engine",
          "membershipConfidence": 1.0
        }
      ]
    }
  ]
}
```

The `thumbnailUrl` is relative to the classifier API base URL. The frontend
should resolve it against the classifier API origin.

Thumbnail endpoint:

```http
GET /v1/upload-batches/{batchId}/images/{imageId}/thumbnail
```

The thumbnail endpoint is read-only and should not generate thumbnails. It only
serves thumbnails already created by the worker pipeline. Missing thumbnails
should display a placeholder and must not be treated as processing failures.

### 9. Edit Review Groups

The active review API surface is:

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

Every successful mutation returns the updated review snapshot from:

```http
GET /v1/upload-batches/{batchId}/groups
```

Review edit endpoints only work while the batch is `review_required`. Approved
batches are read-only.

#### Create a New Group

```http
POST /v1/upload-batches/{batchId}/groups
Content-Type: application/json
```

Request:

```json
{
  "imageIds": ["uuid"]
}
```

#### Move Image To Existing Group

```http
POST /v1/groups/{targetGroupId}/images
Content-Type: application/json
```

Request:

```json
{
  "imageId": "uuid"
}
```

#### Remove Image From Group

```http
DELETE /v1/groups/{groupId}/images/{imageId}
```

#### Merge Groups

```http
POST /v1/groups/merge
Content-Type: application/json
```

Request:

```json
{
  "targetGroupId": "uuid",
  "sourceGroupIds": ["uuid"]
}
```

#### Split Group

```http
POST /v1/groups/{groupId}/split
Content-Type: application/json
```

Request:

```json
{
  "imageIds": ["uuid"]
}
```

Empty selections are invalid. Selecting all images already in the group is a
no-op success. Selecting a single image is allowed and creates a singleton group.

#### Update Cover Image

```http
PATCH /v1/groups/{groupId}
Content-Type: application/json
```

Request:

```json
{
  "coverImageId": "uuid"
}
```

The cover image must be a non-duplicate member of the group.

#### Update Approved Category

```http
PATCH /v1/groups/{groupId}
Content-Type: application/json
```

Request:

```json
{
  "approvedCategoryId": "uuid"
}
```

Use `null` to clear the approved category:

```json
{
  "approvedCategoryId": null
}
```

Non-null `approvedCategoryId` must point to an active global leaf category.
Parent categories are visible in the user interface but cannot be selected.

#### Mark or Restore Duplicate

```http
PATCH /v1/groups/{groupId}/images/{imageId}
Content-Type: application/json
```

Mark duplicate:

```json
{
  "isDuplicate": true,
  "duplicateOfImageId": "uuid"
}
```

Restore duplicate:

```json
{
  "isDuplicate": false,
  "duplicateOfImageId": null
}
```

When `isDuplicate` is `true`, `duplicateOfImageId` must point to another
non-duplicate image in the same group.

### 10. Approve Review Results

Group approval:

```http
POST /v1/groups/{groupId}/approve
```

Rules:

* batch must be `review_required`;
* group must be `proposed`;
* group must have a non-null approved category;
* approved groups are read-only.

Batch approval:

```http
POST /v1/upload-batches/{batchId}/approve
```

Rules:

* batch must be `review_required`;
* every group must already be approved;
* batch approval only changes the batch state;
* batch approval does not mutate group membership or duplicate flags;
* approved batches are read-only.

Once batch approval succeeds, the batch status becomes:

```text
approved
```

The approved batch can still be read with:

```http
GET /v1/upload-batches/{batchId}/groups
```

## Current Frontend Routes

The classifier frontend currently exposes internal admin routes:

```text
/admin/ingest
/admin/processing/{batchId}
/admin/review/{batchId}
```

Current flow for a human operator:

```text
1. Open /admin/ingest.
2. Select JPEG files.
3. Upload files.
4. Batch finalizes automatically after successful upload.
5. Start processing.
6. Watch /admin/processing/{batchId}.
7. Open /admin/review/{batchId} once the batch is review_required.
8. Fix groups and categories.
9. Approve groups.
10. Approve the batch.
```

The current pages are internal tools. They should require admin access before a
production website integration.

## Frontend Integration Guidance For Bazoria Web

Bazoria frontend developers have two viable integration options.

### Option A: Link To Classifier Admin

Use the classifier as a separate internal admin application. Bazoria Web links
authorized operators to:

```text
{classifierAdminBaseUrl}/admin/ingest
{classifierAdminBaseUrl}/admin/processing/{batchId}
{classifierAdminBaseUrl}/admin/review/{batchId}
```

This is the lowest-risk integration because the classifier frontend already
knows the upload, processing, and review contracts.

### Option B: Rebuild The Flow Inside Bazoria Admin

Bazoria Web can build native admin pages using the API contracts in this
document.

If doing this:

* do not proxy file bodies through Bazoria Web;
* use classifier signed URLs for direct browser upload;
* keep the browser upload concurrency at four unless the classifier changes it;
* resolve classifier thumbnail URLs against the classifier API base URL;
* display placeholders for missing thumbnails;
* poll processing through `GET /v1/upload-batches/{batchId}/processing`;
* refresh the review snapshot after every review mutation;
* disable edit controls after group or batch approval;
* require approved categories before enabling group approval;
* require all groups to be approved before enabling batch approval.

## Backend Integration Guidance For Bazoria Web

The Bazoria Web backend should integrate at the approved-group boundary, not at
the raw upload boundary unless Bazoria Web is explicitly owning the admin ingest
experience.

Recommended backend responsibilities:

1. Map Bazoria seller identifiers to classifier organization identifiers.
2. Authorize which Bazoria admin users can access classifier batches.
3. Read approved classifier batches.
4. Copy or promote approved images into Bazoria-owned public storage.
5. Create Bazoria `ProductDraft` records.
6. Store classifier source references for traceability.
7. Keep the public catalog unpublished until Bazoria review/publish is complete.

Do not put Bazoria Web write credentials in the browser. Export into Bazoria Web
should be done server-to-server.

## Seller And Organization Mapping

The local prototype uses a seeded default organization:

```text
00000000-0000-0000-0000-000000000001
```

That is acceptable only for local development.

Production integration needs an explicit mapping:

```text
Bazoria Seller.id <-> catalog-classifier Organization.id
```

Every classifier query and export must be authorized at organization level.
Before production rollout, do not rely on the default organization for real
seller data.

## Category Taxonomy

The current taxonomy is clothing-focused and intentionally small. The classifier
currently supports category suggestions and approved categories, but the broader
Bazoria taxonomy should be expanded later.

Important current rules:

* category classification is only a suggestion;
* human review must choose or confirm the approved category;
* approved category must be an active global leaf category;
* parent categories can be shown as context but are not selectable.

Category read endpoint:

```http
GET /v1/categories
```

Response:

```json
[
  {
    "id": "uuid",
    "slug": "t-shirts",
    "parentId": "uuid",
    "nameEn": "T-shirts"
  }
]
```

## ProductDraft Mapping

The classifier can provide reviewed group data. Bazoria Web should turn that
into a `ProductDraft`, not a public product.

Recommended mapping:

| Bazoria ProductDraft field | Source |
| --- | --- |
| `sourceSystem` | constant, for example `catalog-classifier` |
| `sourceBatchId` | classifier `batchId` |
| `sourceGroupId` | classifier `groupId` |
| `sourceImageIds` | classifier group image identifiers |
| `sellerId` | Bazoria seller mapped from classifier organization |
| `categoryId` | Bazoria category mapped from classifier `approvedCategorySlug` or future category mapping |
| `coverImage` | classifier `coverImageId`, after promotion into Bazoria public storage |
| `images` | non-duplicate classifier group images, after promotion |
| `duplicateImages` | duplicate classifier images, optional trace/debug data |
| `groupingConfidence` | classifier group confidence |
| `suggestedCategorySlug` | classifier suggested category |
| `approvedCategorySlug` | classifier approved category |
| `pipelineVersion` | classifier pipeline version |
| `reviewedAt` | classifier review or approval timestamp, once exposed |
| `status` | Bazoria draft status, not classifier batch status |

Do not fill these fields from image classification alone:

* final product name;
* public slug;
* final marketing description;
* price;
* minimum order quantity;
* stock;
* sizes;
* material;
* seller promises;
* certifications;
* country of origin.

Those fields require seller input, human review, or a later explicit generation
and review stage.

## Future Approved-Group Export Contract

This endpoint is recommended but not implemented yet:

```http
GET /v1/upload-batches/{batchId}/approved-groups
```

Recommended response shape:

```json
{
  "batchId": "uuid",
  "organizationId": "uuid",
  "status": "approved",
  "pipelineVersion": "2026-06-01",
  "groups": [
    {
      "groupId": "uuid",
      "approvedCategorySlug": "t-shirts",
      "coverImageId": "uuid",
      "confidence": 0.94,
      "warnings": [],
      "images": [
        {
          "imageId": "uuid",
          "originalFilename": "front.jpg",
          "originalObjectKey": "organizations/.../originals/...",
          "thumbnailObjectKey": "organizations/.../thumbnails/...",
          "isDuplicate": false,
          "duplicateOfImageId": null,
          "position": 0
        }
      ]
    }
  ]
}
```

This export endpoint should only return approved groups from approved batches.
It should not expose unreviewed groups.

A later server-to-server export command may also be useful:

```http
POST /v1/upload-batches/{batchId}/export-product-drafts
```

That endpoint would call Bazoria Web backend and persist draft creation status,
but this should be implemented only after both teams agree on the Bazoria
ProductDraft schema and image promotion flow.

## Image And Storage Handling

Classifier storage is private ingestion storage. It is not public catalog
storage.

Current classifier storage contains:

* original upload objects;
* generated thumbnail objects;
* possibly later normalized or derived processing objects.

Website integration rules:

* do not use classifier bucket objects directly as public product images;
* do not make the classifier bucket public;
* do not expose long-lived classifier object URLs to buyers;
* use classifier thumbnail endpoints for admin review only;
* during ProductDraft creation, copy or promote approved images into
  Bazoria-owned storage;
* Bazoria Web should own public image URLs and image lifecycle after promotion.

## Authentication And Authorization

The prototype has not completed production authentication for every path.
Before connecting real seller data, production integration needs:

* admin authentication for the classifier web app;
* JSON Web Token (JWT) validation or equivalent on the classifier API;
* organization-level authorization on every batch, image, group, and export
  query;
* service-to-service authentication for worker and export calls;
* separate service accounts for API, worker, and export/integration;
* secrets stored in deployment secrets or Google Secret Manager;
* no Bazoria Web write token in any browser code.

Internal worker endpoints are not browser-facing:

```text
POST /internal/tasks/process-image
POST /internal/tasks/classify-image
POST /internal/tasks/group-batch
POST /internal/tasks/find-existing-products
```

In production, those endpoints must require authenticated service-to-service
requests.

## Error Handling Expectations

Frontend and backend integrations should handle these broad categories:

| HTTP status | Meaning | Typical handling |
| --- | --- | --- |
| `400` | invalid request or invalid selection | show validation message |
| `404` | batch, group, image, or thumbnail not found | show not found or placeholder for thumbnails |
| `409` | batch or group is in the wrong state | reload state and show action-level message |
| `500` | unexpected server or external provider failure | show retryable server error and log details |

Known state errors include:

```text
batch_not_review_ready
review_approval_not_allowed
```

For review pages, prefer action-level errors for group actions and page-level
errors for batch approval.

## Operational Requirements

The classifier API service needs access to:

* the classifier PostgreSQL database;
* the private ingestion Google Cloud Storage bucket;
* signing credentials for upload URLs;
* object metadata read permission for finalize verification;
* Gemini API key for embeddings and image classification in local prototype
  processing.

The worker pipeline needs permission to:

* read original private image objects;
* write thumbnail objects;
* update processing jobs and image records;
* call model providers.

The private bucket should keep:

* public access prevention enabled;
* uniform bucket-level access enabled;
* Cross-Origin Resource Sharing (CORS) configured only for approved admin
  frontend origins and upload headers.

## Current Local Configuration

Common local API environment variables:

```text
DATABASE_URL=postgresql+psycopg://catalog:catalog@localhost:5432/catalog_classifier
CATALOG_UPLOAD_BUCKET=lnlabs-bucket
CATALOG_SIGNING_SERVICE_ACCOUNT=catalog-api@catalog-classifier.iam.gserviceaccount.com
GEMINI_API_KEY=...
```

Common local frontend environment variable:

```text
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
```

For local startup details, see:

```text
docs/local-start.md
```

## What Website Developers Should Not Reimplement

Do not duplicate these classifier responsibilities inside Bazoria Web:

* perceptual hashing;
* image embedding generation;
* exact duplicate detection;
* same-product grouping decisions;
* classifier review event semantics;
* signed upload object key generation;
* thumbnail generation for classifier ingestion images;
* direct database reads from classifier tables.

If Bazoria Web needs additional fields, add them to the classifier API or export
payload instead of reading classifier internals.

## Recommended Integration Sequence

1. Keep classifier upload, processing, and review as an internal admin flow.
2. Add production authentication and organization authorization.
3. Add a dedicated approved-groups read endpoint.
4. Agree on the Bazoria `ProductDraft` schema and category mapping.
5. Implement server-side ProductDraft export from approved classifier batches.
6. Implement image promotion from classifier private storage to Bazoria public
   storage.
7. Add Bazoria Web admin screens for imported product drafts.
8. Add publication workflow from ProductDraft to public Product.

## Open Decisions

The following decisions should be made before production integration:

* exact seller-to-organization provisioning flow;
* classifier API authentication mechanism;
* whether Bazoria Web links to classifier admin or embeds the workflow;
* approved-group export endpoint shape;
* Bazoria ProductDraft schema;
* category mapping between classifier taxonomy and Bazoria taxonomy;
* public image promotion strategy;
* export retry and idempotency strategy;
* how Bazoria Web surfaces classifier warnings;
* whether review events should be imported into Bazoria Web;
* image rejection semantics, currently deferred;
* existing product matching semantics, still evolving.

## Minimal Contract For The Website Team

For the current milestone, the website team should assume this contract:

```text
Input:
  reviewed and approved classifier batch

Available today:
  GET /v1/upload-batches/{batchId}/groups
  when batch status is approved

Not production-ready yet:
  dedicated approved-groups export endpoint
  automatic ProductDraft creation
  public image promotion
  production authentication and seller authorization

Website-side responsibility:
  convert approved classifier groups into Bazoria ProductDraft records only
  after explicit server-side export
```

That boundary keeps the classifier focused on ingestion quality and keeps
Bazoria Web in control of public catalog data.
