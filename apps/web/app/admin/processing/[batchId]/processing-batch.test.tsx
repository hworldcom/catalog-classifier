import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ProcessingBatch from "@/app/admin/processing/[batchId]/processing-batch";
import {
  loadProcessingBatch,
  startUploadBatchProcessing,
} from "@/lib/durable-uploads";

vi.mock("@/lib/durable-uploads", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("@/lib/durable-uploads")>();

  return {
    ...actual,
    loadProcessingBatch: vi.fn(),
    startUploadBatchProcessing: vi.fn(),
  };
});

const loadProcessingBatchMock = vi.mocked(loadProcessingBatch);
const startUploadBatchProcessingMock = vi.mocked(startUploadBatchProcessing);

function processingSnapshot(
  overrides: {
    status?: string;
    imageStatus?: string;
    processJobStatus?: string | null;
    processError?: string | null;
    classifyJobStatus?: string | null;
    classifyError?: string | null;
    categorySlug?: string | null;
    confidence?: number | null;
    hasHashes?: boolean;
    hasEmbedding?: boolean;
    processedFileCount?: number;
  } = {},
) {
  const processJobStatus = overrides.processJobStatus ?? null;
  const classifyJobStatus = overrides.classifyJobStatus ?? null;
  return {
    batchId: "batch-1",
    status: overrides.status ?? "queued",
    originalFileCount: 1,
    processedFileCount: overrides.processedFileCount ?? 0,
    pipelineVersion: "2026-06-01",
    images: [
      {
        imageId: "image-1",
        uploadOrder: 0,
        originalFilename: "front.jpg",
        imageStatus: overrides.imageStatus ?? "uploaded",
        processJobStatus,
        processError: overrides.processError ?? null,
        classifyJobStatus,
        classifyError: overrides.classifyError ?? null,
        categorySlug: overrides.categorySlug ?? null,
        confidence: overrides.confidence ?? null,
        hasHashes: overrides.hasHashes ?? false,
        hasEmbedding: overrides.hasEmbedding ?? false,
      },
    ],
  };
}

describe("ProcessingBatch", () => {
  beforeEach(() => {
    loadProcessingBatchMock.mockReset();
    startUploadBatchProcessingMock.mockReset();
  });

  it("loads a queued batch and shows the start action", async () => {
    loadProcessingBatchMock.mockResolvedValue(processingSnapshot());

    render(<ProcessingBatch batchId="batch-1" pollIntervalMs={1} />);

    expect(await screen.findByText("Process batch")).toBeInTheDocument();
    expect(screen.getByText("batch-1")).toBeInTheDocument();
    expect(screen.getByText("queued")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Start processing" }),
    ).toBeEnabled();
    expect(screen.getByText("front.jpg")).toBeInTheDocument();
    expect(screen.getAllByText("not created")).toHaveLength(2);
    expect(screen.getByText("Thumbnail pending")).toBeInTheDocument();
    expect(
      screen.queryByRole("img", { name: "Thumbnail for front.jpg" }),
    ).not.toBeInTheDocument();
  });

  it("renders thumbnails after image processing completes", async () => {
    loadProcessingBatchMock.mockResolvedValue(
      processingSnapshot({
        status: "processing",
        imageStatus: "processed",
        processJobStatus: "completed",
        classifyJobStatus: "completed",
        hasHashes: true,
        hasEmbedding: true,
        processedFileCount: 1,
      }),
    );

    render(<ProcessingBatch batchId="batch-1" pollIntervalMs={1} />);

    const thumbnail = await screen.findByRole("img", {
      name: "Thumbnail for front.jpg",
    });
    expect(thumbnail).toHaveAttribute(
      "src",
      "http://localhost:8000/v1/upload-batches/batch-1/images/image-1/thumbnail",
    );
    fireEvent.error(thumbnail);
    expect(screen.getByText("Thumbnail pending")).toBeInTheDocument();
  });

  it("starts processing once, polls state, and renders completed classification", async () => {
    const user = userEvent.setup();
    loadProcessingBatchMock
      .mockResolvedValueOnce(processingSnapshot())
      .mockResolvedValueOnce(
        processingSnapshot({
          status: "processing",
          imageStatus: "processed",
          processJobStatus: "completed",
          classifyJobStatus: "completed",
          categorySlug: "trousers",
          confidence: 0.95,
          hasHashes: true,
          hasEmbedding: true,
          processedFileCount: 1,
        }),
      );
    startUploadBatchProcessingMock.mockResolvedValue(
      processingSnapshot({
        status: "processing",
        processJobStatus: "pending",
      }),
    );
    render(<ProcessingBatch batchId="batch-1" pollIntervalMs={50} />);

    await user.click(await screen.findByRole("button", { name: "Start processing" }));

    expect(startUploadBatchProcessingMock).toHaveBeenCalledOnce();
    expect(startUploadBatchProcessingMock).toHaveBeenCalledWith("batch-1");
    expect(await screen.findByText("pending")).toBeInTheDocument();
    expect(screen.getByText("Thumbnail pending")).toBeInTheDocument();

    await waitFor(() => {
      expect(loadProcessingBatchMock).toHaveBeenCalledTimes(2);
    });
    expect(await screen.findByText("trousers")).toBeInTheDocument();
    expect(
      screen.getByRole("img", { name: "Thumbnail for front.jpg" }),
    ).toBeInTheDocument();
    expect(screen.getByText("0.95")).toBeInTheDocument();
    expect(screen.getAllByText("yes")).toHaveLength(2);
    expect(
      screen.getByText("Processing reached terminal image states"),
    ).toBeInTheDocument();
    expect(startUploadBatchProcessingMock).toHaveBeenCalledOnce();
  });

  it("polls an already processing batch without calling start again", async () => {
    loadProcessingBatchMock
      .mockResolvedValueOnce(
        processingSnapshot({
          status: "processing",
          processJobStatus: "started",
        }),
      )
      .mockResolvedValueOnce(
        processingSnapshot({
          status: "processing",
          imageStatus: "processed",
          processJobStatus: "completed",
          classifyJobStatus: "completed",
          categorySlug: "t-shirts",
          confidence: 0.88,
          hasHashes: true,
          hasEmbedding: true,
          processedFileCount: 1,
        }),
      );

    render(<ProcessingBatch batchId="batch-1" pollIntervalMs={1} />);

    expect(await screen.findByText("started")).toBeInTheDocument();
    await waitFor(() => {
      expect(loadProcessingBatchMock).toHaveBeenCalledTimes(2);
    });
    expect(await screen.findByText("t-shirts")).toBeInTheDocument();
    expect(startUploadBatchProcessingMock).not.toHaveBeenCalled();
  });

  it("renders process and classify errors", async () => {
    loadProcessingBatchMock.mockResolvedValue(
      processingSnapshot({
        status: "processing",
        processJobStatus: "completed",
        classifyJobStatus: "failed",
        classifyError: "category_suggestion_failed: Provider failed.",
        hasHashes: true,
        hasEmbedding: true,
      }),
    );

    render(<ProcessingBatch batchId="batch-1" pollIntervalMs={1} />);

    expect(await screen.findByText("failed")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Classify error: category_suggestion_failed: Provider failed.",
      ),
    ).toBeInTheDocument();
  });
});
