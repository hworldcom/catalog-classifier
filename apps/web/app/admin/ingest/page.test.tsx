import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import UploadPage from "@/app/admin/ingest/page";
import {
  createUploadBatch,
  finalizeUploadBatch,
  loadUploadBatch,
  registerUploadFiles,
  requestRetryUploads,
  uploadDirectFiles,
  validateUploadFiles,
} from "@/lib/durable-uploads";

vi.mock("@/lib/durable-uploads", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("@/lib/durable-uploads")>();

  return {
    ...actual,
    createUploadBatch: vi.fn(),
    finalizeUploadBatch: vi.fn(),
    loadUploadBatch: vi.fn(),
    registerUploadFiles: vi.fn(),
    requestRetryUploads: vi.fn(),
    uploadDirectFiles: vi.fn(),
    validateUploadFiles: vi.fn(),
  };
});

const createUploadBatchMock = vi.mocked(createUploadBatch);
const finalizeUploadBatchMock = vi.mocked(finalizeUploadBatch);
const loadUploadBatchMock = vi.mocked(loadUploadBatch);
const registerUploadFilesMock = vi.mocked(registerUploadFiles);
const requestRetryUploadsMock = vi.mocked(requestRetryUploads);
const uploadDirectFilesMock = vi.mocked(uploadDirectFiles);
const validateUploadFilesMock = vi.mocked(validateUploadFiles);

function registration(uploadOrder: number, filename: string) {
  return {
    imageId: `image-${uploadOrder}`,
    uploadOrder,
    originalFilename: filename,
    originalObjectKey: `objects/image-${uploadOrder}.jpg`,
    uploadUrl: `https://uploads.example.test/image-${uploadOrder}`,
  };
}

function batchState(
  images: Array<{
    imageId: string;
    uploadOrder: number;
    originalFilename: string;
    status: string;
    errorCode: string | null;
    errorMessage: string | null;
  }>,
  status = "uploading",
) {
  return {
    batchId: "batch-1",
    status,
    originalFileCount: images.length,
    processedFileCount: 0,
    createdAt: "2026-06-14T12:00:00Z",
    finalizedAt: status === "queued" ? "2026-06-14T12:01:00Z" : null,
    completedAt: null,
    images,
  };
}

function batchImage(
  uploadOrder: number,
  filename: string,
  status: string,
) {
  return {
    imageId: `image-${uploadOrder}`,
    uploadOrder,
    originalFilename: filename,
    status,
    errorCode: status === "failed" ? "object_missing" : null,
    errorMessage: status === "failed" ? "Object missing." : null,
  };
}

describe("UploadPage", () => {
  beforeEach(() => {
    createUploadBatchMock.mockReset();
    finalizeUploadBatchMock.mockReset();
    loadUploadBatchMock.mockReset();
    registerUploadFilesMock.mockReset();
    requestRetryUploadsMock.mockReset();
    uploadDirectFilesMock.mockReset();
    validateUploadFilesMock.mockReset();
    validateUploadFilesMock.mockReturnValue(null);
  });

  it("creates, registers, and renders direct upload results", async () => {
    const user = userEvent.setup();
    createUploadBatchMock.mockResolvedValue({
      batchId: "batch-1",
      status: "created",
      maxFiles: 20,
    });
    registerUploadFilesMock.mockResolvedValue({
      batchId: "batch-1",
      status: "uploading",
      uploads: [
        registration(1, "product.jpg"),
        registration(0, "product.jpg"),
      ],
    });
    uploadDirectFilesMock.mockImplementation(async (uploads, onUpdate) => {
      const results = uploads.map((upload, index) => ({
        ...upload,
        status: index === 0 ? ("uploaded" as const) : ("failed" as const),
        errorMessage: index === 0 ? null : "Network unavailable.",
      }));
      results.forEach(onUpdate);
      return results;
    });
    render(<UploadPage />);
    const files = [
      new File(["first"], "product.jpg", { type: "image/jpeg" }),
      new File(["second"], "product.jpg", { type: "image/jpeg" }),
    ];

    await user.upload(screen.getByLabelText("JPEG images"), files);
    await user.click(screen.getByRole("button", { name: "Upload 2 images" }));

    expect(createUploadBatchMock).toHaveBeenCalledOnce();
    expect(registerUploadFilesMock).toHaveBeenCalledWith("batch-1", files);
    expect(uploadDirectFilesMock).toHaveBeenCalledOnce();
    expect(finalizeUploadBatchMock).not.toHaveBeenCalled();
    expect(await screen.findByText("1 uploaded, 1 failed.")).toBeInTheDocument();
    expect(screen.getByText("batch-1")).toBeInTheDocument();
    expect(screen.getAllByText("product.jpg")).toHaveLength(2);
    expect(screen.getByText("Network unavailable.")).toBeInTheDocument();
  });

  it("shows the batch identifier while registration is pending", async () => {
    const user = userEvent.setup();
    let rejectRegistration!: (error: Error) => void;
    createUploadBatchMock.mockResolvedValue({
      batchId: "batch-2",
      status: "created",
      maxFiles: 20,
    });
    registerUploadFilesMock.mockImplementation(
      () =>
        new Promise((_, reject) => {
          rejectRegistration = reject;
        }),
    );
    render(<UploadPage />);

    await user.upload(
      screen.getByLabelText("JPEG images"),
      new File(["image"], "front.jpg", { type: "image/jpeg" }),
    );
    await user.click(screen.getByRole("button", { name: "Upload 1 image" }));

    expect(await screen.findByText("batch-2")).toBeInTheDocument();
    expect(screen.getByLabelText("JPEG images")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Uploading..." })).toBeDisabled();
    expect(screen.queryByLabelText("Upload result")).not.toBeInTheDocument();

    rejectRegistration(new Error("Unable to register files for upload."));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Unable to register files for upload.",
    );
    expect(screen.getByText("batch-2")).toBeInTheDocument();
    expect(uploadDirectFilesMock).not.toHaveBeenCalled();
  });

  it("shows a page-level error when batch creation fails", async () => {
    const user = userEvent.setup();
    createUploadBatchMock.mockRejectedValue(
      new Error("Unable to create the upload batch."),
    );
    render(<UploadPage />);

    await user.upload(
      screen.getByLabelText("JPEG images"),
      new File(["image"], "front.jpg", { type: "image/jpeg" }),
    );
    await user.click(screen.getByRole("button", { name: "Upload 1 image" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Unable to create the upload batch.",
    );
    expect(registerUploadFilesMock).not.toHaveBeenCalled();
    expect(screen.queryByLabelText("Upload result")).not.toBeInTheDocument();
  });

  it("stops before creating a batch when client validation fails", async () => {
    const user = userEvent.setup({ applyAccept: false });
    validateUploadFilesMock.mockReturnValue("notes.txt must be a JPEG file.");
    render(<UploadPage />);

    await user.upload(
      screen.getByLabelText("JPEG images"),
      new File(["text"], "notes.txt", { type: "text/plain" }),
    );
    await user.click(screen.getByRole("button", { name: "Upload 1 image" }));

    expect(screen.getByRole("alert")).toHaveTextContent(
      "notes.txt must be a JPEG file.",
    );
    expect(createUploadBatchMock).not.toHaveBeenCalled();
  });

  it("finalizes and shows queued status after all upload attempts succeed", async () => {
    const user = userEvent.setup();
    createUploadBatchMock.mockResolvedValue({
      batchId: "batch-3",
      status: "created",
      maxFiles: 20,
    });
    registerUploadFilesMock.mockResolvedValue({
      batchId: "batch-3",
      status: "uploading",
      uploads: [registration(0, "front.jpg")],
    });
    uploadDirectFilesMock.mockImplementation(async (uploads, onUpdate) => {
      const uploaded = {
        ...uploads[0],
        status: "uploaded" as const,
        errorMessage: null,
      };
      onUpdate(uploaded);
      return [uploaded];
    });
    finalizeUploadBatchMock.mockResolvedValue(
      batchState([batchImage(0, "front.jpg", "uploaded")], "queued"),
    );
    render(<UploadPage />);

    await user.upload(
      screen.getByLabelText("JPEG images"),
      new File(["image"], "front.jpg", { type: "image/jpeg" }),
    );
    await user.click(screen.getByRole("button", { name: "Upload 1 image" }));

    expect(await screen.findByText("1 uploaded, 0 failed.")).toBeInTheDocument();
    expect(finalizeUploadBatchMock).toHaveBeenCalledWith("batch-3");
    expect(await screen.findByText("Backend status: queued")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByLabelText("JPEG images")).toBeEnabled();
      expect(
        screen.getByRole("button", { name: "Upload 1 image" }),
      ).toBeEnabled();
    });
  });

  it("refreshes, retries only selected failed rows, and preserves successes", async () => {
    const user = userEvent.setup();
    const files = [
      new File(["first"], "front.jpg", { type: "image/jpeg" }),
      new File(["second"], "back.jpg", { type: "image/jpeg" }),
    ];
    createUploadBatchMock.mockResolvedValue({
      batchId: "batch-1",
      status: "created",
      maxFiles: 20,
    });
    registerUploadFilesMock.mockResolvedValue({
      batchId: "batch-1",
      status: "uploading",
      uploads: [
        registration(0, "front.jpg"),
        registration(1, "back.jpg"),
      ],
    });
    uploadDirectFilesMock
      .mockImplementationOnce(async (uploads, onUpdate) => {
        const results = uploads.map((upload, index) => ({
          ...upload,
          status: index === 0 ? ("uploaded" as const) : ("failed" as const),
          errorMessage: index === 0 ? null : "Network unavailable.",
        }));
        results.forEach(onUpdate);
        return results;
      })
      .mockImplementationOnce(async (uploads, onUpdate) => {
        const uploaded = {
          ...uploads[0],
          status: "uploaded" as const,
          errorMessage: null,
        };
        onUpdate(uploaded);
        return [uploaded];
      });
    loadUploadBatchMock.mockResolvedValue(
      batchState([
        batchImage(0, "front.jpg", "pending"),
        batchImage(1, "back.jpg", "pending"),
      ]),
    );
    requestRetryUploadsMock.mockResolvedValue({
      batchId: "batch-1",
      status: "uploading",
      uploads: [
        {
          ...registration(1, "back.jpg"),
          originalObjectKey: "objects/retry-image-1.jpg",
          uploadUrl: "https://uploads.example.test/retry-image-1",
        },
      ],
    });
    finalizeUploadBatchMock.mockResolvedValue(
      batchState(
        [
          batchImage(0, "front.jpg", "uploaded"),
          batchImage(1, "back.jpg", "uploaded"),
        ],
        "queued",
      ),
    );
    render(<UploadPage />);

    await user.upload(screen.getByLabelText("JPEG images"), files);
    await user.click(screen.getByRole("button", { name: "Upload 2 images" }));

    expect(await screen.findByText("1 uploaded, 1 failed.")).toBeInTheDocument();
    const successfulCheckbox = screen.getByRole("checkbox", {
      name: "Select front.jpg item 1 for retry",
    });
    const failedCheckbox = screen.getByRole("checkbox", {
      name: "Select back.jpg item 2 for retry",
    });
    expect(successfulCheckbox).toBeDisabled();
    expect(failedCheckbox).toBeEnabled();
    expect(
      screen.getByRole("button", { name: "Retry selected" }),
    ).toBeDisabled();

    await user.click(failedCheckbox);
    await user.click(screen.getByRole("button", { name: "Retry selected" }));

    expect(loadUploadBatchMock).toHaveBeenCalledWith("batch-1");
    expect(requestRetryUploadsMock).toHaveBeenCalledWith("batch-1", [
      "image-1",
    ]);
    expect(uploadDirectFilesMock).toHaveBeenCalledTimes(2);
    expect(finalizeUploadBatchMock).toHaveBeenCalledWith("batch-1");
    const retriedUploads = uploadDirectFilesMock.mock.calls[1][0];
    expect(retriedUploads).toHaveLength(1);
    expect(retriedUploads[0].imageId).toBe("image-1");
    expect(retriedUploads[0].file).toBe(files[1]);
    expect(await screen.findByText("2 uploaded, 0 failed.")).toBeInTheDocument();
    expect(await screen.findByText("Backend status: queued")).toBeInTheDocument();
    expect(successfulCheckbox).toBeDisabled();
    expect(failedCheckbox).toBeDisabled();
    expect(screen.getByText("0 selected for retry")).toBeInTheDocument();
    expect(screen.getByText("batch-1")).toBeInTheDocument();
  });

  it("shows backend verification failures returned by finalize", async () => {
    const user = userEvent.setup();
    createUploadBatchMock.mockResolvedValue({
      batchId: "batch-1",
      status: "created",
      maxFiles: 20,
    });
    registerUploadFilesMock.mockResolvedValue({
      batchId: "batch-1",
      status: "uploading",
      uploads: [registration(0, "front.jpg")],
    });
    uploadDirectFilesMock.mockImplementationOnce(async (uploads, onUpdate) => {
      const uploaded = {
        ...uploads[0],
        status: "uploaded" as const,
        errorMessage: null,
      };
      onUpdate(uploaded);
      return [uploaded];
    });
    finalizeUploadBatchMock.mockResolvedValue(
      batchState([batchImage(0, "front.jpg", "failed")], "uploading"),
    );
    render(<UploadPage />);

    await user.upload(
      screen.getByLabelText("JPEG images"),
      new File(["image"], "front.jpg", { type: "image/jpeg" }),
    );
    await user.click(screen.getByRole("button", { name: "Upload 1 image" }));

    expect(finalizeUploadBatchMock).toHaveBeenCalledWith("batch-1");
    expect(await screen.findByText("0 uploaded, 1 failed.")).toBeInTheDocument();
    expect(screen.getByText("Object missing.")).toBeInTheDocument();
    expect(screen.getByText("Backend status: uploading")).toBeInTheDocument();
    expect(
      screen.getByRole("checkbox", {
        name: "Select front.jpg item 1 for retry",
      }),
    ).toBeEnabled();
  });

  it("keeps local upload results visible when finalize fails", async () => {
    const user = userEvent.setup();
    createUploadBatchMock.mockResolvedValue({
      batchId: "batch-1",
      status: "created",
      maxFiles: 20,
    });
    registerUploadFilesMock.mockResolvedValue({
      batchId: "batch-1",
      status: "uploading",
      uploads: [registration(0, "front.jpg")],
    });
    uploadDirectFilesMock.mockImplementationOnce(async (uploads, onUpdate) => {
      const uploaded = {
        ...uploads[0],
        status: "uploaded" as const,
        errorMessage: null,
      };
      onUpdate(uploaded);
      return [uploaded];
    });
    finalizeUploadBatchMock.mockRejectedValue(
      new Error("Unable to finalize the upload batch."),
    );
    render(<UploadPage />);

    await user.upload(
      screen.getByLabelText("JPEG images"),
      new File(["image"], "front.jpg", { type: "image/jpeg" }),
    );
    await user.click(screen.getByRole("button", { name: "Upload 1 image" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Unable to finalize the upload batch.",
    );
    expect(screen.getByText("1 uploaded, 0 failed.")).toBeInTheDocument();
    expect(screen.getByText("Backend status: uploading")).toBeInTheDocument();
  });

  it("stops and clears selection when refresh changes retry eligibility", async () => {
    const user = userEvent.setup();
    createUploadBatchMock.mockResolvedValue({
      batchId: "batch-1",
      status: "created",
      maxFiles: 20,
    });
    registerUploadFilesMock.mockResolvedValue({
      batchId: "batch-1",
      status: "uploading",
      uploads: [registration(0, "front.jpg")],
    });
    uploadDirectFilesMock.mockImplementationOnce(async (uploads, onUpdate) => {
      const failed = {
        ...uploads[0],
        status: "failed" as const,
        errorMessage: "Network unavailable.",
      };
      onUpdate(failed);
      return [failed];
    });
    loadUploadBatchMock.mockResolvedValue(
      batchState([batchImage(0, "front.jpg", "uploaded")]),
    );
    render(<UploadPage />);

    await user.upload(
      screen.getByLabelText("JPEG images"),
      new File(["image"], "front.jpg", { type: "image/jpeg" }),
    );
    await user.click(screen.getByRole("button", { name: "Upload 1 image" }));
    const retryCheckbox = await screen.findByRole("checkbox", {
      name: "Select front.jpg item 1 for retry",
    });
    await user.click(retryCheckbox);
    await user.click(screen.getByRole("button", { name: "Retry selected" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Upload state changed. Review the retryable rows and select them again.",
    );
    expect(requestRetryUploadsMock).not.toHaveBeenCalled();
    expect(uploadDirectFilesMock).toHaveBeenCalledOnce();
    expect(retryCheckbox).toBeDisabled();
    expect(retryCheckbox).not.toBeChecked();
  });

  it("stops before retry URL generation when batch refresh fails", async () => {
    const user = userEvent.setup();
    createUploadBatchMock.mockResolvedValue({
      batchId: "batch-1",
      status: "created",
      maxFiles: 20,
    });
    registerUploadFilesMock.mockResolvedValue({
      batchId: "batch-1",
      status: "uploading",
      uploads: [registration(0, "front.jpg")],
    });
    uploadDirectFilesMock.mockImplementationOnce(async (uploads, onUpdate) => {
      const failed = {
        ...uploads[0],
        status: "failed" as const,
        errorMessage: "Network unavailable.",
      };
      onUpdate(failed);
      return [failed];
    });
    loadUploadBatchMock.mockRejectedValue(
      new Error("Unable to load the upload batch."),
    );
    render(<UploadPage />);

    await user.upload(
      screen.getByLabelText("JPEG images"),
      new File(["image"], "front.jpg", { type: "image/jpeg" }),
    );
    await user.click(screen.getByRole("button", { name: "Upload 1 image" }));
    await user.click(
      await screen.findByRole("checkbox", {
        name: "Select front.jpg item 1 for retry",
      }),
    );
    await user.click(screen.getByRole("button", { name: "Retry selected" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Unable to load the upload batch.",
    );
    expect(requestRetryUploadsMock).not.toHaveBeenCalled();
    expect(uploadDirectFilesMock).toHaveBeenCalledOnce();
  });

  it("disables upload and retry controls while refreshing durable state", async () => {
    const user = userEvent.setup();
    let resolveBatch!: (batch: ReturnType<typeof batchState>) => void;
    createUploadBatchMock.mockResolvedValue({
      batchId: "batch-1",
      status: "created",
      maxFiles: 20,
    });
    registerUploadFilesMock.mockResolvedValue({
      batchId: "batch-1",
      status: "uploading",
      uploads: [registration(0, "front.jpg")],
    });
    uploadDirectFilesMock.mockImplementationOnce(async (uploads, onUpdate) => {
      const failed = {
        ...uploads[0],
        status: "failed" as const,
        errorMessage: "Network unavailable.",
      };
      onUpdate(failed);
      return [failed];
    });
    loadUploadBatchMock.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveBatch = resolve;
        }),
    );
    requestRetryUploadsMock.mockRejectedValue(new Error("Stop after refresh."));
    render(<UploadPage />);

    await user.upload(
      screen.getByLabelText("JPEG images"),
      new File(["image"], "front.jpg", { type: "image/jpeg" }),
    );
    await user.click(screen.getByRole("button", { name: "Upload 1 image" }));
    const retryCheckbox = await screen.findByRole("checkbox", {
      name: "Select front.jpg item 1 for retry",
    });
    await user.click(retryCheckbox);
    await user.click(screen.getByRole("button", { name: "Retry selected" }));

    expect(screen.getByRole("button", { name: "Retrying..." })).toBeDisabled();
    expect(retryCheckbox).toBeDisabled();
    expect(screen.getByLabelText("JPEG images")).toBeDisabled();

    resolveBatch(batchState([batchImage(0, "front.jpg", "pending")]));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Stop after refresh.",
    );
  });

  it("rejects an invalid retry response before direct uploads start", async () => {
    const user = userEvent.setup();
    createUploadBatchMock.mockResolvedValue({
      batchId: "batch-1",
      status: "created",
      maxFiles: 20,
    });
    registerUploadFilesMock.mockResolvedValue({
      batchId: "batch-1",
      status: "uploading",
      uploads: [registration(0, "front.jpg")],
    });
    uploadDirectFilesMock.mockImplementationOnce(async (uploads, onUpdate) => {
      const failed = {
        ...uploads[0],
        status: "failed" as const,
        errorMessage: "Network unavailable.",
      };
      onUpdate(failed);
      return [failed];
    });
    loadUploadBatchMock.mockResolvedValue(
      batchState([batchImage(0, "front.jpg", "pending")]),
    );
    requestRetryUploadsMock.mockResolvedValue({
      batchId: "batch-1",
      status: "uploading",
      uploads: [
        {
          ...registration(1, "wrong.jpg"),
          imageId: "unexpected-image",
        },
      ],
    });
    render(<UploadPage />);

    await user.upload(
      screen.getByLabelText("JPEG images"),
      new File(["image"], "front.jpg", { type: "image/jpeg" }),
    );
    await user.click(screen.getByRole("button", { name: "Upload 1 image" }));
    await user.click(
      await screen.findByRole("checkbox", {
        name: "Select front.jpg item 1 for retry",
      }),
    );
    await user.click(screen.getByRole("button", { name: "Retry selected" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The backend returned invalid upload retry data.",
    );
    expect(uploadDirectFilesMock).toHaveBeenCalledOnce();
  });

  it("sends selected retries in upload order rather than click order", async () => {
    const user = userEvent.setup();
    const files = [
      new File(["first"], "front.jpg", { type: "image/jpeg" }),
      new File(["second"], "back.jpg", { type: "image/jpeg" }),
    ];
    createUploadBatchMock.mockResolvedValue({
      batchId: "batch-1",
      status: "created",
      maxFiles: 20,
    });
    registerUploadFilesMock.mockResolvedValue({
      batchId: "batch-1",
      status: "uploading",
      uploads: [
        registration(0, "front.jpg"),
        registration(1, "back.jpg"),
      ],
    });
    uploadDirectFilesMock
      .mockImplementationOnce(async (uploads, onUpdate) => {
        const failed = uploads.map((upload) => ({
          ...upload,
          status: "failed" as const,
          errorMessage: "Network unavailable.",
        }));
        failed.forEach(onUpdate);
        return failed;
      })
      .mockImplementationOnce(async (uploads) => uploads);
    loadUploadBatchMock.mockResolvedValue(
      batchState([
        batchImage(0, "front.jpg", "pending"),
        batchImage(1, "back.jpg", "pending"),
      ]),
    );
    requestRetryUploadsMock.mockResolvedValue({
      batchId: "batch-1",
      status: "uploading",
      uploads: [
        registration(0, "front.jpg"),
        registration(1, "back.jpg"),
      ],
    });
    render(<UploadPage />);

    await user.upload(screen.getByLabelText("JPEG images"), files);
    await user.click(screen.getByRole("button", { name: "Upload 2 images" }));
    await user.click(
      await screen.findByRole("checkbox", {
        name: "Select back.jpg item 2 for retry",
      }),
    );
    await user.click(
      screen.getByRole("checkbox", {
        name: "Select front.jpg item 1 for retry",
      }),
    );
    await user.click(screen.getByRole("button", { name: "Retry selected" }));

    expect(requestRetryUploadsMock).toHaveBeenCalledWith("batch-1", [
      "image-0",
      "image-1",
    ]);
  });

  it("stops retrying when the refreshed batch is no longer uploading", async () => {
    const user = userEvent.setup();
    createUploadBatchMock.mockResolvedValue({
      batchId: "batch-1",
      status: "created",
      maxFiles: 20,
    });
    registerUploadFilesMock.mockResolvedValue({
      batchId: "batch-1",
      status: "uploading",
      uploads: [registration(0, "front.jpg")],
    });
    uploadDirectFilesMock.mockImplementationOnce(async (uploads, onUpdate) => {
      const failed = {
        ...uploads[0],
        status: "failed" as const,
        errorMessage: "Network unavailable.",
      };
      onUpdate(failed);
      return [failed];
    });
    loadUploadBatchMock.mockResolvedValue(
      batchState([batchImage(0, "front.jpg", "uploaded")], "queued"),
    );
    render(<UploadPage />);

    await user.upload(
      screen.getByLabelText("JPEG images"),
      new File(["image"], "front.jpg", { type: "image/jpeg" }),
    );
    await user.click(screen.getByRole("button", { name: "Upload 1 image" }));
    await user.click(
      await screen.findByRole("checkbox", {
        name: "Select front.jpg item 1 for retry",
      }),
    );
    await user.click(screen.getByRole("button", { name: "Retry selected" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This batch is no longer uploading and cannot accept retries.",
    );
    expect(requestRetryUploadsMock).not.toHaveBeenCalled();
  });

  it("discards the current retry session when new files are selected", async () => {
    const user = userEvent.setup();
    createUploadBatchMock.mockResolvedValue({
      batchId: "batch-1",
      status: "created",
      maxFiles: 20,
    });
    registerUploadFilesMock.mockResolvedValue({
      batchId: "batch-1",
      status: "uploading",
      uploads: [registration(0, "front.jpg")],
    });
    uploadDirectFilesMock.mockImplementationOnce(async (uploads, onUpdate) => {
      const failed = {
        ...uploads[0],
        status: "failed" as const,
        errorMessage: "Network unavailable.",
      };
      onUpdate(failed);
      return [failed];
    });
    render(<UploadPage />);
    const input = screen.getByLabelText("JPEG images");

    await user.upload(
      input,
      new File(["image"], "front.jpg", { type: "image/jpeg" }),
    );
    await user.click(screen.getByRole("button", { name: "Upload 1 image" }));
    expect(await screen.findByText("batch-1")).toBeInTheDocument();
    await user.click(
      screen.getByRole("checkbox", {
        name: "Select front.jpg item 1 for retry",
      }),
    );

    await user.upload(
      input,
      new File(["replacement"], "replacement.jpg", { type: "image/jpeg" }),
    );

    expect(screen.queryByText("batch-1")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Upload result")).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Upload 1 image" }),
    ).toBeEnabled();
  });
});
