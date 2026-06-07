export const MAX_FILES_PER_REQUEST = 20;

export type LocalBatchFileResult = {
  imageId: string | null;
  originalFilename: string;
  status: "accepted" | "rejected";
  errorCode: string | null;
  errorMessage: string | null;
};

export type CreateLocalBatchResult = {
  batchId: string | null;
  status: "completed" | "partial" | "rejected";
  manifestVersion: number | null;
  files: LocalBatchFileResult[];
};

export type LocalBatchImage = {
  imageId: string;
  originalFilename: string;
  thumbnailUrl: string;
  imageUrl: string;
  sha256: string;
  groupId: string;
  isRetained: boolean;
};

export type LocalBatchGroup = {
  groupId: string;
  retainedImageId: string;
  imageIds: string[];
};

export type LocalBatch = {
  batchId: string;
  status: "ready";
  manifestVersion: number;
  images: LocalBatchImage[];
  groups: LocalBatchGroup[];
};

export type MoveImageResult = {
  batch: LocalBatch;
};

export type CreateGroupResult = {
  groupId: string;
  batch: LocalBatch;
};

type ApiErrorBody = {
  detail?: string | { message?: string };
};

export class LocalBatchError extends Error {}

function apiBaseUrl(): string {
  return process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
}

function apiUrl(path: string, baseUrl = apiBaseUrl()): string {
  return `${baseUrl.replace(/\/$/, "")}${path}`;
}

async function responseError(response: Response): Promise<LocalBatchError> {
  const body = (await response.json().catch(() => null)) as ApiErrorBody | null;
  let message = "The backend rejected the request.";

  if (typeof body?.detail === "string") {
    message = body.detail;
  } else if (body?.detail && typeof body.detail.message === "string") {
    message = body.detail.message;
  }

  return new LocalBatchError(message);
}

export async function createLocalBatch(
  files: File[],
  fetchImplementation: typeof fetch = fetch,
  baseUrl = apiBaseUrl(),
): Promise<CreateLocalBatchResult> {
  const formData = new FormData();
  files.forEach((file) => formData.append("files", file));

  const response = await fetchImplementation(apiUrl("/v1/local-batches", baseUrl), {
    method: "POST",
    body: formData,
  });

  if (!response.ok) {
    throw await responseError(response);
  }

  return (await response.json()) as CreateLocalBatchResult;
}

export async function loadLocalBatch(
  batchId: string,
  fetchImplementation: typeof fetch = fetch,
  baseUrl = apiBaseUrl(),
): Promise<LocalBatch> {
  const response = await fetchImplementation(
    apiUrl(`/v1/local-batches/${batchId}`, baseUrl),
  );

  if (!response.ok) {
    throw await responseError(response);
  }

  return (await response.json()) as LocalBatch;
}

export async function moveLocalBatchImage(
  batchId: string,
  imageId: string,
  groupId: string,
  fetchImplementation: typeof fetch = fetch,
  baseUrl = apiBaseUrl(),
): Promise<MoveImageResult> {
  const response = await fetchImplementation(
    apiUrl(`/v1/local-batches/${batchId}/images/${imageId}`, baseUrl),
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ groupId }),
    },
  );

  if (!response.ok) {
    throw await responseError(response);
  }

  return (await response.json()) as MoveImageResult;
}

export async function createLocalBatchGroup(
  batchId: string,
  imageIds: string[],
  fetchImplementation: typeof fetch = fetch,
  baseUrl = apiBaseUrl(),
): Promise<CreateGroupResult> {
  const response = await fetchImplementation(
    apiUrl(`/v1/local-batches/${batchId}/groups`, baseUrl),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ imageIds }),
    },
  );

  if (!response.ok) {
    throw await responseError(response);
  }

  return (await response.json()) as CreateGroupResult;
}

export function localBatchAssetUrl(
  path: string,
  baseUrl = apiBaseUrl(),
): string {
  return apiUrl(path, baseUrl);
}
