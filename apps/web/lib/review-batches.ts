export type ReviewGroupImage = {
  imageId: string;
  originalFilename: string;
  uploadOrder: number;
  thumbnailUrl: string;
  position: number;
  isDuplicate: boolean;
  isRejected: boolean;
  duplicateOfImageId: string | null;
  membershipSource: string;
  membershipConfidence: number | null;
};

export type ReviewGroup = {
  groupId: string;
  status: string;
  confidence: number | null;
  coverImageId: string | null;
  suggestedCategorySlug: string | null;
  approvedCategorySlug: string | null;
  categorySuggestionStatus: "pending" | "ready" | "unavailable" | null;
  approvedCategorySource:
    | "machine_suggestion"
    | "reviewer_selection"
    | "reviewer_cleared"
    | null;
  possibleExistingProductId: string | null;
  warnings: string[];
  images: ReviewGroupImage[];
};

export type ReviewBatchGroups = {
  batchId: string;
  organizationId: string;
  status: string;
  pipelineVersion: string | null;
  groups: ReviewGroup[];
};

export type ReviewCategory = {
  id: string;
  slug: string;
  parentId: string | null;
  nameEn: string;
};

type ApiErrorBody = {
  detail?: string | { code?: string; message?: string };
};

export class ReviewBatchError extends Error {
  constructor(
    message: string,
    readonly status: number | null = null,
    readonly code: string | null = null,
  ) {
    super(message);
    this.name = "ReviewBatchError";
  }
}

function apiBaseUrl(): string {
  return process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
}

function apiUrl(path: string, baseUrl = apiBaseUrl()): string {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  return `${baseUrl.replace(/\/$/, "")}${normalizedPath}`;
}

async function responseError(response: Response): Promise<ReviewBatchError> {
  const body = (await response.json().catch(() => null)) as ApiErrorBody | null;
  let message = "The backend rejected the request.";
  let code: string | null = null;

  if (typeof body?.detail === "string") {
    message = body.detail;
  } else if (body?.detail) {
    if (typeof body.detail.message === "string") {
      message = body.detail.message;
    }
    if (typeof body.detail.code === "string") {
      code = body.detail.code;
    }
  }

  return new ReviewBatchError(message, response.status, code);
}

export async function loadReviewBatchGroups(
  batchId: string,
  fetchImplementation: typeof fetch = fetch,
  baseUrl = apiBaseUrl(),
): Promise<ReviewBatchGroups> {
  const response = await fetchImplementation(
    apiUrl(`/v1/upload-batches/${batchId}/groups`, baseUrl),
  );

  if (!response.ok) {
    throw await responseError(response);
  }

  return (await response.json()) as ReviewBatchGroups;
}

export async function loadReviewCategories(
  fetchImplementation: typeof fetch = fetch,
  baseUrl = apiBaseUrl(),
): Promise<ReviewCategory[]> {
  const response = await fetchImplementation(apiUrl("/v1/categories", baseUrl));

  if (!response.ok) {
    throw await responseError(response);
  }

  return (await response.json()) as ReviewCategory[];
}

export async function runMultimodalComparison(
  batchId: string,
  fetchImplementation: typeof fetch = fetch,
  baseUrl = apiBaseUrl(),
): Promise<ReviewBatchGroups> {
  const response = await fetchImplementation(
    apiUrl(
      `/v1/upload-batches/${batchId}/run-multimodal-comparison`,
      baseUrl,
    ),
    {
      method: "POST",
    },
  );

  if (!response.ok) {
    throw await responseError(response);
  }

  return (await response.json()) as ReviewBatchGroups;
}

export async function createReviewGroup(
  batchId: string,
  imageIds: string[],
  fetchImplementation: typeof fetch = fetch,
  baseUrl = apiBaseUrl(),
): Promise<ReviewBatchGroups> {
  const response = await fetchImplementation(
    apiUrl(`/v1/upload-batches/${batchId}/groups`, baseUrl),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ imageIds }),
    },
  );

  if (!response.ok) {
    throw await responseError(response);
  }

  return (await response.json()) as ReviewBatchGroups;
}

export async function moveReviewImage(
  targetGroupId: string,
  imageId: string,
  fetchImplementation: typeof fetch = fetch,
  baseUrl = apiBaseUrl(),
): Promise<ReviewBatchGroups> {
  const response = await fetchImplementation(
    apiUrl(`/v1/groups/${targetGroupId}/images`, baseUrl),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ imageId }),
    },
  );

  if (!response.ok) {
    throw await responseError(response);
  }

  return (await response.json()) as ReviewBatchGroups;
}

export async function mergeReviewGroups(
  targetGroupId: string,
  sourceGroupIds: string[],
  fetchImplementation: typeof fetch = fetch,
  baseUrl = apiBaseUrl(),
): Promise<ReviewBatchGroups> {
  const response = await fetchImplementation(apiUrl("/v1/groups/merge", baseUrl), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ targetGroupId, sourceGroupIds }),
  });

  if (!response.ok) {
    throw await responseError(response);
  }

  return (await response.json()) as ReviewBatchGroups;
}

export async function splitReviewGroup(
  groupId: string,
  imageIds: string[],
  fetchImplementation: typeof fetch = fetch,
  baseUrl = apiBaseUrl(),
): Promise<ReviewBatchGroups> {
  const response = await fetchImplementation(
    apiUrl(`/v1/groups/${groupId}/split`, baseUrl),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ imageIds }),
    },
  );

  if (!response.ok) {
    throw await responseError(response);
  }

  return (await response.json()) as ReviewBatchGroups;
}

export async function updateReviewGroupCover(
  groupId: string,
  coverImageId: string,
  fetchImplementation: typeof fetch = fetch,
  baseUrl = apiBaseUrl(),
): Promise<ReviewBatchGroups> {
  const response = await fetchImplementation(apiUrl(`/v1/groups/${groupId}`, baseUrl), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ coverImageId }),
  });

  if (!response.ok) {
    throw await responseError(response);
  }

  return (await response.json()) as ReviewBatchGroups;
}

export async function updateReviewGroupCategory(
  groupId: string,
  approvedCategoryId: string | null,
  fetchImplementation: typeof fetch = fetch,
  baseUrl = apiBaseUrl(),
): Promise<ReviewBatchGroups> {
  const response = await fetchImplementation(apiUrl(`/v1/groups/${groupId}`, baseUrl), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approvedCategoryId }),
  });

  if (!response.ok) {
    throw await responseError(response);
  }

  return (await response.json()) as ReviewBatchGroups;
}

export async function updateReviewImageDuplicate(
  groupId: string,
  imageId: string,
  duplicateOfImageId: string | null,
  fetchImplementation: typeof fetch = fetch,
  baseUrl = apiBaseUrl(),
): Promise<ReviewBatchGroups> {
  const response = await fetchImplementation(
    apiUrl(`/v1/groups/${groupId}/images/${imageId}`, baseUrl),
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        isDuplicate: duplicateOfImageId !== null,
        duplicateOfImageId,
      }),
    },
  );

  if (!response.ok) {
    throw await responseError(response);
  }

  return (await response.json()) as ReviewBatchGroups;
}

export async function rejectReviewImage(
  groupId: string,
  imageId: string,
  fetchImplementation: typeof fetch = fetch,
  baseUrl = apiBaseUrl(),
): Promise<ReviewBatchGroups> {
  const response = await fetchImplementation(
    apiUrl(`/v1/groups/${groupId}/images/${imageId}/reject`, baseUrl),
    {
      method: "POST",
    },
  );

  if (!response.ok) {
    throw await responseError(response);
  }

  return (await response.json()) as ReviewBatchGroups;
}

export async function restoreReviewImageRejection(
  groupId: string,
  imageId: string,
  fetchImplementation: typeof fetch = fetch,
  baseUrl = apiBaseUrl(),
): Promise<ReviewBatchGroups> {
  const response = await fetchImplementation(
    apiUrl(
      `/v1/groups/${groupId}/images/${imageId}/restore-rejection`,
      baseUrl,
    ),
    {
      method: "POST",
    },
  );

  if (!response.ok) {
    throw await responseError(response);
  }

  return (await response.json()) as ReviewBatchGroups;
}

export async function approveReviewGroup(
  groupId: string,
  fetchImplementation: typeof fetch = fetch,
  baseUrl = apiBaseUrl(),
): Promise<ReviewBatchGroups> {
  const response = await fetchImplementation(
    apiUrl(`/v1/groups/${groupId}/approve`, baseUrl),
    {
      method: "POST",
    },
  );

  if (!response.ok) {
    throw await responseError(response);
  }

  return (await response.json()) as ReviewBatchGroups;
}

export async function approveReviewBatch(
  batchId: string,
  fetchImplementation: typeof fetch = fetch,
  baseUrl = apiBaseUrl(),
): Promise<ReviewBatchGroups> {
  const response = await fetchImplementation(
    apiUrl(`/v1/upload-batches/${batchId}/approve`, baseUrl),
    {
      method: "POST",
    },
  );

  if (!response.ok) {
    throw await responseError(response);
  }

  return (await response.json()) as ReviewBatchGroups;
}

export function reviewBatchAssetUrl(
  path: string,
  baseUrl = apiBaseUrl(),
): string {
  if (/^https?:\/\//.test(path)) {
    return path;
  }

  return apiUrl(path, baseUrl);
}
