import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import UploadPage from "@/app/admin/ingest/page";
import {
  createUploadBatch,
  registerUploadFiles,
  uploadDirectFiles,
  validateUploadFiles,
} from "@/lib/durable-uploads";

vi.mock("@/lib/durable-uploads", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("@/lib/durable-uploads")>();

  return {
    ...actual,
    createUploadBatch: vi.fn(),
    registerUploadFiles: vi.fn(),
    uploadDirectFiles: vi.fn(),
    validateUploadFiles: vi.fn(),
  };
});

const createUploadBatchMock = vi.mocked(createUploadBatch);
const registerUploadFilesMock = vi.mocked(registerUploadFiles);
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

describe("UploadPage", () => {
  beforeEach(() => {
    createUploadBatchMock.mockReset();
    registerUploadFilesMock.mockReset();
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

  it("re-enables controls after all upload attempts finish", async () => {
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
    render(<UploadPage />);

    await user.upload(
      screen.getByLabelText("JPEG images"),
      new File(["image"], "front.jpg", { type: "image/jpeg" }),
    );
    await user.click(screen.getByRole("button", { name: "Upload 1 image" }));

    expect(await screen.findByText("1 uploaded, 0 failed.")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByLabelText("JPEG images")).toBeEnabled();
      expect(
        screen.getByRole("button", { name: "Upload 1 image" }),
      ).toBeEnabled();
    });
  });
});
