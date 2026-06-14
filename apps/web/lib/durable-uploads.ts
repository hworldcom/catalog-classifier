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
