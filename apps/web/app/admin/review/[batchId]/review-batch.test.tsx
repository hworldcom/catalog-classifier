import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ReviewBatch from "@/app/admin/review/[batchId]/review-batch";
import {
  createLocalBatchGroup,
  loadLocalBatch,
  moveLocalBatchImage,
} from "@/lib/local-batches";

vi.mock("@/lib/local-batches", () => ({
  createLocalBatchGroup: vi.fn(),
  loadLocalBatch: vi.fn(),
  localBatchAssetUrl: (path: string) => `http://api.test${path}`,
  moveLocalBatchImage: vi.fn(),
}));

const loadLocalBatchMock = vi.mocked(loadLocalBatch);
const moveLocalBatchImageMock = vi.mocked(moveLocalBatchImage);
const createLocalBatchGroupMock = vi.mocked(createLocalBatchGroup);

const initialBatch = {
  batchId: "batch-1",
  status: "ready" as const,
  manifestVersion: 1,
  images: [
    {
      imageId: "image-1",
      originalFilename: "front.jpg",
      thumbnailUrl: "/thumbnail-1",
      imageUrl: "/image-1",
      sha256: "same",
      groupId: "group-1",
      isRetained: true,
    },
    {
      imageId: "image-2",
      originalFilename: "front-copy.jpg",
      thumbnailUrl: "/thumbnail-2",
      imageUrl: "/image-2",
      sha256: "same",
      groupId: "group-1",
      isRetained: false,
    },
    {
      imageId: "image-3",
      originalFilename: "back.jpg",
      thumbnailUrl: "/thumbnail-3",
      imageUrl: "/image-3",
      sha256: "unique",
      groupId: "group-2",
      isRetained: true,
    },
  ],
  groups: [
    {
      groupId: "group-1",
      retainedImageId: "image-1",
      imageIds: ["image-1", "image-2"],
    },
    {
      groupId: "group-2",
      retainedImageId: "image-3",
      imageIds: ["image-3"],
    },
  ],
};

describe("ReviewBatch", () => {
  beforeEach(() => {
    loadLocalBatchMock.mockReset();
    moveLocalBatchImageMock.mockReset();
    createLocalBatchGroupMock.mockReset();
  });

  it("renders neutral groups with retained images and edit controls", async () => {
    loadLocalBatchMock.mockResolvedValue(initialBatch);

    render(<ReviewBatch batchId="batch-1" />);

    expect(await screen.findByText("2 images")).toBeInTheDocument();
    expect(screen.getByText("1 image")).toBeInTheDocument();
    expect(screen.getByText("front.jpg")).toBeInTheDocument();
    expect(screen.getByText("front-copy.jpg")).toBeInTheDocument();
    expect(screen.getByText("back.jpg")).toBeInTheDocument();
    expect(screen.getAllByText("Retained")).toHaveLength(2);
    expect(screen.getByText("Member")).toBeInTheDocument();
    expect(screen.getByLabelText("Select front.jpg")).toBeInTheDocument();
    expect(
      screen.getByLabelText("Target group for front.jpg"),
    ).toBeInTheDocument();
  });

  it("moves an image and replaces local state with the response batch", async () => {
    const user = userEvent.setup();
    const updatedBatch = {
      ...initialBatch,
      groups: [
        {
          groupId: "group-1",
          retainedImageId: "image-1",
          imageIds: ["image-1", "image-2", "image-3"],
        },
      ],
      images: initialBatch.images.map((image) =>
        image.imageId === "image-3"
          ? { ...image, groupId: "group-1", isRetained: false }
          : image,
      ),
    };
    loadLocalBatchMock.mockResolvedValue(initialBatch);
    moveLocalBatchImageMock.mockResolvedValue({ batch: updatedBatch });
    render(<ReviewBatch batchId="batch-1" />);

    await user.selectOptions(
      await screen.findByLabelText("Target group for back.jpg"),
      "group-1",
    );
    const moveButtons = screen.getAllByRole("button", { name: "Move" });
    await user.click(moveButtons[2]);

    expect(moveLocalBatchImageMock).toHaveBeenCalledWith(
      "batch-1",
      "image-3",
      "group-1",
    );
    expect(await screen.findByText("3 images")).toBeInTheDocument();
  });

  it("creates a group from selected images in batch order", async () => {
    const user = userEvent.setup();
    loadLocalBatchMock.mockResolvedValue(initialBatch);
    createLocalBatchGroupMock.mockResolvedValue({
      groupId: "group-3",
      batch: initialBatch,
    });
    render(<ReviewBatch batchId="batch-1" />);

    await user.click(await screen.findByLabelText("Select back.jpg"));
    await user.click(screen.getByLabelText("Select front.jpg"));
    await user.click(screen.getByRole("button", { name: "Create group" }));

    expect(createLocalBatchGroupMock).toHaveBeenCalledWith(
      "batch-1",
      ["image-1", "image-3"],
    );
    expect(screen.getByText("0 selected")).toBeInTheDocument();
  });

  it("disables edit controls while a write is pending", async () => {
    const user = userEvent.setup();
    let resolveMove:
      | ((result: { batch: typeof initialBatch }) => void)
      | undefined;
    loadLocalBatchMock.mockResolvedValue(initialBatch);
    moveLocalBatchImageMock.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveMove = resolve;
        }),
    );
    render(<ReviewBatch batchId="batch-1" />);

    await user.selectOptions(
      await screen.findByLabelText("Target group for back.jpg"),
      "group-1",
    );
    await user.click(screen.getAllByRole("button", { name: "Move" })[2]);

    await waitFor(() => {
      expect(screen.getByLabelText("Select front.jpg")).toBeDisabled();
      expect(screen.getByRole("button", { name: "Saving..." })).toBeDisabled();
    });

    resolveMove?.({ batch: initialBatch });
    await waitFor(() => {
      expect(screen.getByLabelText("Select front.jpg")).toBeEnabled();
    });
  });

  it("shows a load error", async () => {
    loadLocalBatchMock.mockRejectedValue(new Error("Local batch was not found."));

    render(<ReviewBatch batchId="missing" />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Local batch was not found.",
    );
  });
});
