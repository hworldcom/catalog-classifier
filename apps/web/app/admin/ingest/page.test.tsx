import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import UploadPage from "@/app/admin/ingest/page";
import { createLocalBatch } from "@/lib/local-batches";

const pushMock = vi.hoisted(() => vi.fn());

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock }),
}));

vi.mock("@/lib/local-batches", () => ({
  MAX_FILES_PER_REQUEST: 20,
  createLocalBatch: vi.fn(),
}));

const createLocalBatchMock = vi.mocked(createLocalBatch);

describe("UploadPage", () => {
  beforeEach(() => {
    createLocalBatchMock.mockReset();
    pushMock.mockReset();
  });

  it("navigates to review after creating a local batch", async () => {
    const user = userEvent.setup();
    createLocalBatchMock.mockResolvedValue({
      batchId: "batch-1",
      status: "completed",
      manifestVersion: 1,
      files: [
        {
          imageId: "image-1",
          originalFilename: "front.jpg",
          status: "accepted",
          errorCode: null,
          errorMessage: null,
        },
      ],
    });
    render(<UploadPage />);

    await user.upload(
      screen.getByLabelText("JPEG images"),
      new File(["image"], "front.jpg", { type: "image/jpeg" }),
    );
    await user.click(screen.getByRole("button", { name: "Upload 1 image" }));

    expect(createLocalBatchMock).toHaveBeenCalledWith([
      expect.objectContaining({ name: "front.jpg" }),
    ]);
    expect(pushMock).toHaveBeenCalledWith("/admin/review/batch-1");
  });

  it("shows per-file errors when every file is rejected", async () => {
    const user = userEvent.setup();
    createLocalBatchMock.mockResolvedValue({
      batchId: null,
      status: "rejected",
      manifestVersion: null,
      files: [
        {
          imageId: null,
          originalFilename: "invalid.jpg",
          status: "rejected",
          errorCode: "invalid_jpeg",
          errorMessage: "The file content is not a valid JPEG image.",
        },
      ],
    });
    render(<UploadPage />);

    await user.upload(
      screen.getByLabelText("JPEG images"),
      new File(["invalid"], "invalid.jpg", { type: "image/jpeg" }),
    );
    await user.click(screen.getByRole("button", { name: "Upload 1 image" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The backend rejected every selected file.",
    );
    expect(screen.getByText("invalid.jpg")).toBeInTheDocument();
    expect(pushMock).not.toHaveBeenCalled();
  });

  it("shows an error when the backend rejects the request", async () => {
    const user = userEvent.setup();
    createLocalBatchMock.mockRejectedValue(new Error("Backend unavailable."));
    render(<UploadPage />);

    await user.upload(
      screen.getByLabelText("JPEG images"),
      new File(["image"], "front.jpg", { type: "image/jpeg" }),
    );
    await user.click(screen.getByRole("button", { name: "Upload 1 image" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Backend unavailable.",
    );
  });
});
