export const MAX_FILES_PER_REQUEST = 20;
export const MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024;
export const MAX_CONCURRENT_UPLOADS = 4;

export type CreateUploadBatchResult = {
  batchId: string;
  status: "created";
  maxFiles: number;
};

export type RegisteredUpload = {
  imageId: string;
  uploadOrder: number;
  originalFilename: string;
  originalObjectKey: string;
  uploadUrl: string;
};

export type RegisterUploadsResult = {
  batchId: string;
  status: "uploading";
  uploads: RegisteredUpload[];
};

export type UploadBatchImage = {
  imageId: string;
  uploadOrder: number;
  originalFilename: string;
  status: string;
  errorCode: string | null;
  errorMessage: string | null;
};

export type UploadBatchResult = {
  batchId: string;
  status: string;
  originalFileCount: number;
  processedFileCount: number;
  createdAt: string;
  finalizedAt: string | null;
  completedAt: string | null;
  images: UploadBatchImage[];
};

export type ProcessingBatchImage = {
  imageId: string;
  uploadOrder: number;
  originalFilename: string;
  imageStatus: string;
  processJobStatus: string | null;
  processError: string | null;
  classifyJobStatus: string | null;
  classifyError: string | null;
  categorySlug: string | null;
  confidence: number | null;
  hasHashes: boolean;
  hasEmbedding: boolean;
};

export type ProcessingBatchResult = {
  batchId: string;
  status: string;
  originalFileCount: number;
  processedFileCount: number;
  pipelineVersion: string;
  images: ProcessingBatchImage[];
};

export type DirectUploadStatus =
  | "pending"
  | "uploading"
  | "uploaded"
  | "failed";

export type DirectUpload = RegisteredUpload & {
  file: File;
  status: DirectUploadStatus;
  errorMessage: string | null;
};

export type UploadSessionRow = {
  imageId: string;
  uploadOrder: number;
  originalFilename: string;
  file: File | null;
  status: DirectUploadStatus;
  errorMessage: string | null;
};

type ApiErrorBody = {
  detail?: string | { message?: string };
};

export class DurableUploadError extends Error {}

function apiBaseUrl(): string {
  return process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
}

function apiUrl(path: string, baseUrl = apiBaseUrl()): string {
  return `${baseUrl.replace(/\/$/, "")}${path}`;
}

async function responseError(response: Response): Promise<DurableUploadError> {
  const body = (await response.json().catch(() => null)) as ApiErrorBody | null;
  let message = "The backend rejected the request.";

  if (typeof body?.detail === "string") {
    message = body.detail;
  } else if (body?.detail && typeof body.detail.message === "string") {
    message = body.detail.message;
  }

  return new DurableUploadError(message);
}

export function validateUploadFiles(files: File[]): string | null {
  if (files.length === 0) {
    return "Select at least one JPEG file.";
  }

  if (files.length > MAX_FILES_PER_REQUEST) {
    return `Select at most ${MAX_FILES_PER_REQUEST} JPEG files.`;
  }

  for (const file of files) {
    if (file.type !== "image/jpeg") {
      return `${file.name} must be a JPEG file.`;
    }

    if (file.size < 1) {
      return `${file.name} must not be empty.`;
    }

    if (file.size > MAX_FILE_SIZE_BYTES) {
      return `${file.name} must not exceed 10 mebibytes.`;
    }
  }

  return null;
}

export async function createUploadBatch(
  fetchImplementation: typeof fetch = fetch,
  baseUrl = apiBaseUrl(),
): Promise<CreateUploadBatchResult> {
  const response = await fetchImplementation(apiUrl("/v1/upload-batches", baseUrl), {
    method: "POST",
  });

  if (!response.ok) {
    throw await responseError(response);
  }

  return (await response.json()) as CreateUploadBatchResult;
}

export async function registerUploadFiles(
  batchId: string,
  files: File[],
  fetchImplementation: typeof fetch = fetch,
  baseUrl = apiBaseUrl(),
): Promise<RegisterUploadsResult> {
  const response = await fetchImplementation(
    apiUrl(`/v1/upload-batches/${batchId}/uploads`, baseUrl),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        files: files.map((file) => ({
          originalFilename: file.name,
          mimeType: file.type,
          sizeBytes: file.size,
        })),
      }),
    },
  );

  if (!response.ok) {
    throw await responseError(response);
  }

  return (await response.json()) as RegisterUploadsResult;
}

export async function loadUploadBatch(
  batchId: string,
  fetchImplementation: typeof fetch = fetch,
  baseUrl = apiBaseUrl(),
): Promise<UploadBatchResult> {
  const response = await fetchImplementation(
    apiUrl(`/v1/upload-batches/${batchId}`, baseUrl),
  );

  if (!response.ok) {
    throw await responseError(response);
  }

  return (await response.json()) as UploadBatchResult;
}

export async function finalizeUploadBatch(
  batchId: string,
  fetchImplementation: typeof fetch = fetch,
  baseUrl = apiBaseUrl(),
): Promise<UploadBatchResult> {
  const response = await fetchImplementation(
    apiUrl(`/v1/upload-batches/${batchId}/finalize`, baseUrl),
    { method: "POST" },
  );

  if (!response.ok) {
    throw await responseError(response);
  }

  return (await response.json()) as UploadBatchResult;
}

export async function loadProcessingBatch(
  batchId: string,
  fetchImplementation: typeof fetch = fetch,
  baseUrl = apiBaseUrl(),
): Promise<ProcessingBatchResult> {
  const response = await fetchImplementation(
    apiUrl(`/v1/upload-batches/${batchId}/processing`, baseUrl),
  );

  if (!response.ok) {
    throw await responseError(response);
  }

  return (await response.json()) as ProcessingBatchResult;
}

export async function startUploadBatchProcessing(
  batchId: string,
  fetchImplementation: typeof fetch = fetch,
  baseUrl = apiBaseUrl(),
): Promise<ProcessingBatchResult> {
  const response = await fetchImplementation(
    apiUrl(`/v1/upload-batches/${batchId}/start-processing`, baseUrl),
    { method: "POST" },
  );

  if (!response.ok) {
    throw await responseError(response);
  }

  return (await response.json()) as ProcessingBatchResult;
}

export function processingThumbnailUrl(
  batchId: string,
  imageId: string,
  baseUrl = apiBaseUrl(),
): string {
  return apiUrl(
    `/v1/upload-batches/${batchId}/images/${imageId}/thumbnail`,
    baseUrl,
  );
}

export async function requestRetryUploads(
  batchId: string,
  imageIds: string[],
  fetchImplementation: typeof fetch = fetch,
  baseUrl = apiBaseUrl(),
): Promise<RegisterUploadsResult> {
  const response = await fetchImplementation(
    apiUrl(`/v1/upload-batches/${batchId}/retry-failed`, baseUrl),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ imageIds }),
    },
  );

  if (!response.ok) {
    throw await responseError(response);
  }

  return (await response.json()) as RegisterUploadsResult;
}

export function prepareDirectUploads(
  files: File[],
  registeredUploads: RegisteredUpload[],
): DirectUpload[] {
  if (registeredUploads.length !== files.length) {
    throw new DurableUploadError(
      "The backend returned invalid upload registration data.",
    );
  }

  const uploadsByOrder = new Map(
    registeredUploads.map((upload) => [upload.uploadOrder, upload]),
  );

  if (
    uploadsByOrder.size !== files.length ||
    files.some((_, uploadOrder) => !uploadsByOrder.has(uploadOrder))
  ) {
    throw new DurableUploadError(
      "The backend returned invalid upload registration data.",
    );
  }

  return files.map((file, uploadOrder) => ({
    ...uploadsByOrder.get(uploadOrder)!,
    file,
    status: "pending",
    errorMessage: null,
  }));
}

export function toUploadSessionRows(
  uploads: DirectUpload[],
): UploadSessionRow[] {
  return uploads.map((upload) => ({
    imageId: upload.imageId,
    uploadOrder: upload.uploadOrder,
    originalFilename: upload.originalFilename,
    file: upload.file,
    status: upload.status,
    errorMessage: upload.errorMessage,
  }));
}

export function isRetryableUpload(row: UploadSessionRow): boolean {
  return (
    row.file !== null && (row.status === "pending" || row.status === "failed")
  );
}

export function reconcileUploadSessionRows(
  currentRows: UploadSessionRow[],
  batch: UploadBatchResult,
): UploadSessionRow[] {
  const currentRowsById = new Map(
    currentRows.map((row) => [row.imageId, row]),
  );
  const backendImageIds = new Set<string>();

  const reconciledRows = [...batch.images]
    .sort((left, right) => left.uploadOrder - right.uploadOrder)
    .map((image) => {
      if (backendImageIds.has(image.imageId)) {
        throw new DurableUploadError(
          "The backend returned invalid upload batch data.",
        );
      }
      backendImageIds.add(image.imageId);

      const currentRow = currentRowsById.get(image.imageId);
      if (!currentRow) {
        return {
          imageId: image.imageId,
          uploadOrder: image.uploadOrder,
          originalFilename: image.originalFilename,
          file: null,
          status: persistedStatusForDisplay(image.status),
          errorMessage: image.errorMessage,
        };
      }

      if (image.status === "uploaded") {
        return {
          ...currentRow,
          uploadOrder: image.uploadOrder,
          originalFilename: image.originalFilename,
          status: "uploaded" as const,
          errorMessage: null,
        };
      }

      if (image.status === "failed") {
        return {
          ...currentRow,
          uploadOrder: image.uploadOrder,
          originalFilename: image.originalFilename,
          status: "failed" as const,
          errorMessage: image.errorMessage,
        };
      }

      if (image.status !== "pending") {
        return {
          ...currentRow,
          uploadOrder: image.uploadOrder,
          originalFilename: image.originalFilename,
          status: "uploaded" as const,
          errorMessage: null,
        };
      }

      return {
        ...currentRow,
        uploadOrder: image.uploadOrder,
        originalFilename: image.originalFilename,
      };
    });

  if (currentRows.some((row) => !backendImageIds.has(row.imageId))) {
    throw new DurableUploadError(
      "The backend returned invalid upload batch data.",
    );
  }

  return reconciledRows;
}

export function isProcessingImageTerminal(
  image: ProcessingBatchImage,
): boolean {
  if (
    image.processJobStatus === "failed" &&
    image.classifyJobStatus === null
  ) {
    return true;
  }

  return (
    (image.processJobStatus === "completed" ||
      image.processJobStatus === "failed") &&
    (image.classifyJobStatus === "completed" ||
      image.classifyJobStatus === "failed")
  );
}

export function isProcessingBatchTerminal(
  batch: ProcessingBatchResult,
): boolean {
  return (
    batch.images.length > 0 &&
    batch.images.every((image) => isProcessingImageTerminal(image))
  );
}

export function prepareRetryUploads(
  rows: UploadSessionRow[],
  selectedImageIds: string[],
  registeredUploads: RegisteredUpload[],
): DirectUpload[] {
  const selectedIdSet = new Set(selectedImageIds);
  const registeredById = new Map<string, RegisteredUpload>();

  if (
    selectedIdSet.size !== selectedImageIds.length ||
    registeredUploads.length !== selectedImageIds.length
  ) {
    throw new DurableUploadError(
      "The backend returned invalid upload retry data.",
    );
  }

  for (const upload of registeredUploads) {
    if (
      !selectedIdSet.has(upload.imageId) ||
      registeredById.has(upload.imageId)
    ) {
      throw new DurableUploadError(
        "The backend returned invalid upload retry data.",
      );
    }
    registeredById.set(upload.imageId, upload);
  }

  const rowsById = new Map(rows.map((row) => [row.imageId, row]));
  return selectedImageIds.map((imageId) => {
    const row = rowsById.get(imageId);
    const registration = registeredById.get(imageId);
    if (!row?.file || !registration || !isRetryableUpload(row)) {
      throw new DurableUploadError(
        "The backend returned invalid upload retry data.",
      );
    }

    return {
      ...registration,
      file: row.file,
      status: "pending",
      errorMessage: null,
    };
  });
}

export async function uploadDirectFiles(
  uploads: DirectUpload[],
  onUpdate: (upload: DirectUpload) => void,
  fetchImplementation: typeof fetch = fetch,
): Promise<DirectUpload[]> {
  const results = [...uploads];
  let nextUploadIndex = 0;

  async function uploadNext(): Promise<void> {
    while (nextUploadIndex < uploads.length) {
      const uploadIndex = nextUploadIndex;
      nextUploadIndex += 1;

      const uploading = {
        ...results[uploadIndex],
        status: "uploading" as const,
        errorMessage: null,
      };
      results[uploadIndex] = uploading;
      onUpdate(uploading);

      let completed: DirectUpload;
      try {
        const response = await fetchImplementation(uploading.uploadUrl, {
          method: "PUT",
          headers: { "Content-Type": "image/jpeg" },
          body: uploading.file,
        });

        completed = response.ok
          ? { ...uploading, status: "uploaded", errorMessage: null }
          : {
              ...uploading,
              status: "failed",
              errorMessage: `Cloud Storage rejected the upload with status ${response.status}.`,
            };
      } catch (error) {
        completed = {
          ...uploading,
          status: "failed",
          errorMessage:
            error instanceof Error
              ? error.message
              : "The direct upload request failed.",
        };
      }

      results[uploadIndex] = completed;
      onUpdate(completed);
    }
  }

  const workerCount = Math.min(MAX_CONCURRENT_UPLOADS, uploads.length);
  await Promise.all(
    Array.from({ length: workerCount }, () => uploadNext()),
  );

  return results;
}

function persistedStatusForDisplay(status: string): DirectUploadStatus {
  if (status === "uploaded" || status === "failed" || status === "pending") {
    return status;
  }
  return "uploaded";
}
