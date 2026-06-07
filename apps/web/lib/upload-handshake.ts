export const MAX_FILES_PER_REQUEST = 20;
export const MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024;

export type UploadFileResult = {
  filename: string;
  status: "accepted" | "rejected";
  sizeBytes: number;
  errorCode: string | null;
  errorMessage: string | null;
};

export type UploadHandshakeResult = {
  uploadId: string;
  status: "completed" | "partial" | "rejected";
  files: UploadFileResult[];
};

type ApiErrorBody = {
  detail?: string | { message?: string };
};

export class UploadHandshakeError extends Error {}

function readErrorMessage(body: ApiErrorBody | null): string {
  if (typeof body?.detail === "string") {
    return body.detail;
  }

  if (body?.detail && typeof body.detail.message === "string") {
    return body.detail.message;
  }

  return "The backend rejected the upload request.";
}

export async function uploadImages(
  files: File[],
  fetchImplementation: typeof fetch = fetch,
  apiBaseUrl = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000",
): Promise<UploadHandshakeResult> {
  const formData = new FormData();
  files.forEach((file) => formData.append("files", file));

  const response = await fetchImplementation(
    `${apiBaseUrl.replace(/\/$/, "")}/v1/upload-handshake`,
    {
      method: "POST",
      body: formData,
    },
  );

  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as ApiErrorBody | null;
    throw new UploadHandshakeError(readErrorMessage(body));
  }

  return (await response.json()) as UploadHandshakeResult;
}

