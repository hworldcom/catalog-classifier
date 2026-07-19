import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ReviewBatch from "@/app/admin/review/[batchId]/review-batch";
import {
  ReviewBatchError,
  ReviewBatchGroups,
  ReviewCategory,
  approveReviewBatch,
  approveReviewGroup,
  createReviewGroup,
  loadReviewCategories,
  loadReviewBatchGroups,
  mergeReviewGroups,
  moveReviewImage,
  rejectReviewImage,
  reviewBatchAssetUrl,
  restoreReviewImageRejection,
  runMultimodalComparison,
  splitReviewGroup,
  updateReviewGroupCategory,
  updateReviewGroupCover,
  updateReviewImageDuplicate,
} from "@/lib/review-batches";

vi.mock("@/lib/review-batches", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("@/lib/review-batches")>();
  return {
    ...actual,
    approveReviewBatch: vi.fn(),
    approveReviewGroup: vi.fn(),
    createReviewGroup: vi.fn(),
    loadReviewCategories: vi.fn(),
    loadReviewBatchGroups: vi.fn(),
    mergeReviewGroups: vi.fn(),
    moveReviewImage: vi.fn(),
    rejectReviewImage: vi.fn(),
    reviewBatchAssetUrl: vi.fn((path: string) => `http://api.test${path}`),
    restoreReviewImageRejection: vi.fn(),
    runMultimodalComparison: vi.fn(),
    splitReviewGroup: vi.fn(),
    updateReviewGroupCategory: vi.fn(),
    updateReviewGroupCover: vi.fn(),
    updateReviewImageDuplicate: vi.fn(),
  };
});

const approveReviewBatchMock = vi.mocked(approveReviewBatch);
const approveReviewGroupMock = vi.mocked(approveReviewGroup);
const createReviewGroupMock = vi.mocked(createReviewGroup);
const loadReviewCategoriesMock = vi.mocked(loadReviewCategories);
const loadReviewBatchGroupsMock = vi.mocked(loadReviewBatchGroups);
const mergeReviewGroupsMock = vi.mocked(mergeReviewGroups);
const moveReviewImageMock = vi.mocked(moveReviewImage);
const rejectReviewImageMock = vi.mocked(rejectReviewImage);
const reviewBatchAssetUrlMock = vi.mocked(reviewBatchAssetUrl);
const restoreReviewImageRejectionMock = vi.mocked(
  restoreReviewImageRejection,
);
const runMultimodalComparisonMock = vi.mocked(runMultimodalComparison);
const splitReviewGroupMock = vi.mocked(splitReviewGroup);
const updateReviewGroupCategoryMock = vi.mocked(updateReviewGroupCategory);
const updateReviewGroupCoverMock = vi.mocked(updateReviewGroupCover);
const updateReviewImageDuplicateMock = vi.mocked(updateReviewImageDuplicate);

const reviewCategories: ReviewCategory[] = [
  {
    id: "category-clothing",
    slug: "clothing",
    parentId: null,
    nameEn: "Clothing",
  },
  {
    id: "category-t-shirts",
    slug: "t-shirts",
    parentId: "category-clothing",
    nameEn: "T-shirts",
  },
  {
    id: "category-trousers",
    slug: "trousers",
    parentId: "category-clothing",
    nameEn: "Trousers",
  },
];

const reviewSnapshot: ReviewBatchGroups = {
  batchId: "batch-1",
  organizationId: "organization-1",
  status: "review_required",
  pipelineVersion: "2026-06-01",
  groups: [
    {
      groupId: "group-1",
      status: "proposed",
      confidence: 0.94,
      coverImageId: "image-1",
      suggestedCategorySlug: "t-shirts",
      approvedCategorySlug: null,
      categorySuggestionStatus: "ready",
      approvedCategorySource: "reviewer_cleared",
      possibleExistingProductId: null,
      warnings: [],
      images: [
        {
          imageId: "image-1",
          originalFilename: "front.jpg",
          uploadOrder: 0,
          thumbnailUrl: "/v1/upload-batches/batch-1/images/image-1/thumbnail",
          position: 0,
          isDuplicate: false,
          isRejected: false,
          duplicateOfImageId: null,
          membershipSource: "engine",
          membershipConfidence: 0.94,
        },
        {
          imageId: "image-2",
          originalFilename: "front-copy.jpg",
          uploadOrder: 1,
          thumbnailUrl: "/v1/upload-batches/batch-1/images/image-2/thumbnail",
          position: 1,
          isDuplicate: true,
          isRejected: false,
          duplicateOfImageId: "image-1",
          membershipSource: "exact_duplicate",
          membershipConfidence: 1,
        },
      ],
    },
    {
      groupId: "group-2",
      status: "proposed",
      confidence: null,
      coverImageId: "image-3",
      suggestedCategorySlug: null,
      approvedCategorySlug: "trousers",
      categorySuggestionStatus: "unavailable",
      approvedCategorySource: "reviewer_selection",
      possibleExistingProductId: "product-1",
      warnings: ["Possible variant of an existing item."],
      images: [
        {
          imageId: "image-3",
          originalFilename: "back.jpg",
          uploadOrder: 2,
          thumbnailUrl: "/v1/upload-batches/batch-1/images/image-3/thumbnail",
          position: 0,
          isDuplicate: false,
          isRejected: false,
          duplicateOfImageId: null,
          membershipSource: "singleton",
          membershipConfidence: null,
        },
      ],
    },
  ],
};

const rejectedBackSnapshot: ReviewBatchGroups = {
  ...reviewSnapshot,
  groups: [
    reviewSnapshot.groups[0],
    {
      ...reviewSnapshot.groups[1],
      coverImageId: null,
      images: reviewSnapshot.groups[1].images.map((image) => ({
        ...image,
        isRejected: true,
      })),
    },
  ],
};

describe("ReviewBatch", () => {
  beforeEach(() => {
    approveReviewBatchMock.mockReset();
    approveReviewGroupMock.mockReset();
    createReviewGroupMock.mockReset();
    loadReviewCategoriesMock.mockReset();
    loadReviewBatchGroupsMock.mockReset();
    mergeReviewGroupsMock.mockReset();
    moveReviewImageMock.mockReset();
    rejectReviewImageMock.mockReset();
    reviewBatchAssetUrlMock.mockClear();
    restoreReviewImageRejectionMock.mockReset();
    runMultimodalComparisonMock.mockReset();
    splitReviewGroupMock.mockReset();
    updateReviewGroupCategoryMock.mockReset();
    updateReviewGroupCoverMock.mockReset();
    updateReviewImageDuplicateMock.mockReset();
    loadReviewCategoriesMock.mockResolvedValue(reviewCategories);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("renders a durable review snapshot with basic edit controls", async () => {
    loadReviewBatchGroupsMock.mockResolvedValue(reviewSnapshot);

    render(<ReviewBatch batchId="batch-1" />);

    expect(screen.getByText("Loading review batch...")).toBeInTheDocument();
    expect(await screen.findByText("2 images")).toBeInTheDocument();
    expect(screen.getByText("1 image")).toBeInTheDocument();
    expect(screen.getByText("review_required")).toBeInTheDocument();
    expect(screen.getByText("2026-06-01")).toBeInTheDocument();
    expect(screen.getByText("front.jpg")).toBeInTheDocument();
    expect(screen.getByText("front-copy.jpg")).toBeInTheDocument();
    expect(screen.getByText("back.jpg")).toBeInTheDocument();
    expect(screen.getAllByText("Cover")).toHaveLength(2);
    expect(screen.getByText("Duplicate")).toBeInTheDocument();
    expect(screen.getAllByText("Member")).toHaveLength(2);
    expect(screen.getByText("t-shirts")).toBeInTheDocument();
    expect(screen.getByLabelText("Approved category for Group 1")).toHaveValue("");
    expect(screen.getByLabelText("Approved category for Group 2")).toHaveValue(
      "category-trousers",
    );
    expect(screen.getAllByRole("option", { name: "Clothing" })[0]).toBeDisabled();
    expect(screen.getAllByRole("option", { name: "T-shirts" })[0]).toBeEnabled();
    expect(screen.getAllByText("0.94")).toHaveLength(2);
    expect(screen.getByText("product-1")).toBeInTheDocument();
    expect(
      screen.getByText("Possible variant of an existing item."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create group" })).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Run multimodal comparison" }),
    ).toBeEnabled();
    expect(screen.getAllByRole("button", { name: "Approve group" })[0]).toBeDisabled();
    expect(screen.getAllByRole("button", { name: "Approve group" })[1]).toBeEnabled();
    expect(screen.getByRole("button", { name: "Approve batch" })).toBeDisabled();
    expect(
      screen.getByText("Select an approved category before approving this group."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Approve every group before approving the batch."),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Select front.jpg")).toBeInTheDocument();
    expect(screen.getByLabelText("Select front-copy.jpg")).toBeInTheDocument();
    expect(screen.getByLabelText("Select back.jpg")).toBeInTheDocument();
    expect(
      screen.getByLabelText("Target group for front.jpg"),
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText("Duplicate master for front.jpg"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Set cover" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restore duplicate" })).toBeInTheDocument();
    expect(loadReviewBatchGroupsMock).toHaveBeenCalledWith("batch-1");
    expect(loadReviewCategoriesMock).toHaveBeenCalled();
    expect(reviewBatchAssetUrlMock).toHaveBeenCalledWith(
      "/v1/upload-batches/batch-1/images/image-1/thumbnail",
    );
  });

  it("confirms multimodal comparison in an accessible modal dialog", async () => {
    const user = userEvent.setup();
    loadReviewBatchGroupsMock.mockResolvedValue(reviewSnapshot);

    render(<ReviewBatch batchId="batch-1" />);

    const comparisonAction = await screen.findByRole("button", {
      name: "Run multimodal comparison",
    });
    await user.click(comparisonAction);

    const dialog = screen.getByRole("dialog", {
      name: "Run multimodal comparison?",
    });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();

    await user.tab();
    expect(screen.getByRole("button", { name: "Run comparison" })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();

    await user.keyboard("{Escape}");

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() => expect(comparisonAction).toHaveFocus());

    await user.click(comparisonAction);
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() => expect(comparisonAction).toHaveFocus());
    expect(runMultimodalComparisonMock).not.toHaveBeenCalled();
  });

  it("confirms image rejection and replaces the snapshot without removing the image", async () => {
    const user = userEvent.setup();
    loadReviewBatchGroupsMock.mockResolvedValue(reviewSnapshot);
    rejectReviewImageMock.mockResolvedValue(rejectedBackSnapshot);

    render(<ReviewBatch batchId="batch-1" />);

    await user.click(await screen.findByLabelText("Select back.jpg"));
    const rejectionAction = screen.getByRole("button", {
      name: "Reject back.jpg from export",
    });
    await user.click(rejectionAction);

    const dialog = screen.getByRole("dialog", {
      name: "Exclude this image from export?",
    });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(
      within(dialog).getByText(
        "The image will remain in this review group and can be restored. It will not be included in future product export.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(rejectionAction).toHaveFocus());
    expect(rejectReviewImageMock).not.toHaveBeenCalled();

    await user.click(rejectionAction);
    await user.click(screen.getByRole("button", { name: "Exclude image" }));

    expect(rejectReviewImageMock).toHaveBeenCalledTimes(1);
    expect(rejectReviewImageMock).toHaveBeenCalledWith("group-2", "image-3");
    expect(await screen.findByText("Excluded from export")).toBeInTheDocument();
    expect(screen.getByText("back.jpg")).toBeInTheDocument();
    expect(screen.getByLabelText("Select back.jpg")).not.toBeChecked();
    expect(
      screen.getByRole("button", { name: "Restore back.jpg for export" }),
    ).toBeEnabled();
  });

  it("restores a rejected image directly from the returned snapshot", async () => {
    const user = userEvent.setup();
    loadReviewBatchGroupsMock.mockResolvedValue(rejectedBackSnapshot);
    restoreReviewImageRejectionMock.mockResolvedValue(reviewSnapshot);

    render(<ReviewBatch batchId="batch-1" />);

    await user.click(
      await screen.findByRole("button", {
        name: "Restore back.jpg for export",
      }),
    );

    expect(restoreReviewImageRejectionMock).toHaveBeenCalledTimes(1);
    expect(restoreReviewImageRejectionMock).toHaveBeenCalledWith(
      "group-2",
      "image-3",
    );
    await waitFor(() => {
      expect(screen.queryByText("Excluded from export")).not.toBeInTheDocument();
    });
    expect(
      screen.getByRole("button", { name: "Reject back.jpg from export" }),
    ).toBeEnabled();
  });

  it("renders duplicate dependency blocks for rejected images", async () => {
    const dependencySnapshot: ReviewBatchGroups = {
      ...reviewSnapshot,
      groups: [
        {
          ...reviewSnapshot.groups[0],
          coverImageId: null,
          approvedCategorySlug: "t-shirts",
          images: reviewSnapshot.groups[0].images.map((image) => ({
            ...image,
            isRejected: true,
          })),
        },
        reviewSnapshot.groups[1],
      ],
    };
    loadReviewBatchGroupsMock.mockResolvedValue(dependencySnapshot);

    render(<ReviewBatch batchId="batch-1" />);

    const restoreMaster = await screen.findByRole("button", {
      name: "Restore front.jpg for export",
    });
    const restoreDuplicate = screen.getByRole("button", {
      name: "Restore front-copy.jpg for export",
    });
    expect(restoreMaster).toBeEnabled();
    expect(restoreDuplicate).toBeDisabled();
    expect(
      screen.getByText(
        "Restore or replace the duplicate master before restoring this image for export.",
      ),
    ).toBeInTheDocument();
    expect(screen.getAllByText("Excluded from export")).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: "Approve group" })[0]).toBeDisabled();
    expect(
      screen.getByText(
        "Restore at least one non-duplicate image before approving this group.",
      ),
    ).toBeInTheDocument();

    const rejectedMasterCard = screen.getByText("front.jpg").closest("li");
    expect(rejectedMasterCard).not.toBeNull();
    expect(rejectedMasterCard).toHaveClass("image-card-rejected");
    expect(
      within(rejectedMasterCard as HTMLElement).queryByText("Set cover"),
    ).not.toBeInTheDocument();

  });

  it("filters rejected images from duplicate master choices", async () => {
    const filteredChoiceSnapshot: ReviewBatchGroups = {
      ...reviewSnapshot,
      groups: [
        {
          ...reviewSnapshot.groups[0],
          coverImageId: "image-2",
          images: reviewSnapshot.groups[0].images.map((image) =>
            image.imageId === "image-1"
              ? { ...image, isRejected: true }
              : {
                  ...image,
                  isDuplicate: false,
                  duplicateOfImageId: null,
                },
          ),
        },
        reviewSnapshot.groups[1],
      ],
    };
    loadReviewBatchGroupsMock.mockResolvedValue(filteredChoiceSnapshot);
    render(<ReviewBatch batchId="batch-1" />);

    const duplicateMasterSelect = await screen.findByLabelText(
      "Duplicate master for front-copy.jpg",
    );
    expect(
      within(duplicateMasterSelect).queryByRole("option", {
        name: "front.jpg",
      }),
    ).not.toBeInTheDocument();
  });

  it("disables rejection and every other mutation while rejection is pending", async () => {
    const user = userEvent.setup();
    let resolveRejection:
      | ((snapshot: ReviewBatchGroups) => void)
      | undefined;
    loadReviewBatchGroupsMock.mockResolvedValue(reviewSnapshot);
    rejectReviewImageMock.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveRejection = resolve;
        }),
    );

    render(<ReviewBatch batchId="batch-1" />);

    await user.click(
      await screen.findByRole("button", {
        name: "Reject back.jpg from export",
      }),
    );
    await user.click(screen.getByRole("button", { name: "Exclude image" }));

    await waitFor(() => {
      expect(screen.getByLabelText("Select back.jpg")).toBeDisabled();
      expect(
        screen.getByRole("button", { name: "Restore duplicate" }),
      ).toBeDisabled();
      expect(
        screen.getByRole("button", { name: "Run multimodal comparison" }),
      ).toBeDisabled();
    });

    await act(async () => {
      resolveRejection?.(rejectedBackSnapshot);
    });
    expect(
      await screen.findByRole("button", {
        name: "Restore back.jpg for export",
      }),
    ).toBeEnabled();
  });

  it("shows backend rejection errors without replacing the current snapshot", async () => {
    const user = userEvent.setup();
    loadReviewBatchGroupsMock.mockResolvedValue(reviewSnapshot);
    rejectReviewImageMock.mockRejectedValue(
      new ReviewBatchError(
        "Restore or reassign active duplicates before rejecting their master.",
        409,
        "image_rejection_duplicate_master_in_use",
      ),
    );

    render(<ReviewBatch batchId="batch-1" />);

    await user.click(
      await screen.findByRole("button", {
        name: "Reject back.jpg from export",
      }),
    );
    await user.click(screen.getByRole("button", { name: "Exclude image" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Restore or reassign active duplicates before rejecting their master.",
    );
    expect(screen.getByText("back.jpg")).toBeInTheDocument();
    expect(screen.queryByText("Excluded from export")).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Reject back.jpg from export" }),
    ).toBeEnabled();
  });

  it("runs comparison, locks edits, and replaces the review snapshot", async () => {
    const user = userEvent.setup();
    let resolveComparison:
      | ((snapshot: ReviewBatchGroups) => void)
      | undefined;
    const comparedSnapshot: ReviewBatchGroups = {
      ...reviewSnapshot,
      groups: [
        {
          ...reviewSnapshot.groups[0],
          groupId: "compared-group",
          images: [
            ...reviewSnapshot.groups[0].images,
            {
              ...reviewSnapshot.groups[1].images[0],
              position: 2,
              membershipSource: "multimodal_model",
            },
          ],
        },
      ],
    };
    loadReviewBatchGroupsMock.mockResolvedValue(reviewSnapshot);
    runMultimodalComparisonMock.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveComparison = resolve;
        }),
    );

    render(<ReviewBatch batchId="batch-1" />);

    const comparisonAction = await screen.findByRole("button", {
      name: "Run multimodal comparison",
    });
    await user.click(comparisonAction);
    await user.click(screen.getByRole("button", { name: "Run comparison" }));

    expect(runMultimodalComparisonMock).toHaveBeenCalledWith("batch-1");
    expect(
      await screen.findByText(
        "Multimodal comparison is running. This may take several minutes.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", {
        name: "Running multimodal comparison...",
      }),
    ).toBeDisabled();
    expect(screen.getByLabelText("Select front.jpg")).toBeDisabled();
    expect(screen.getAllByRole("button", { name: "Approve group" })[1]).toBeDisabled();

    await act(async () => {
      resolveComparison?.(comparedSnapshot);
    });

    expect(
      await screen.findByText(
        "Multimodal comparison completed. Review groups were refreshed.",
      ),
    ).toBeInTheDocument();
    expect(await screen.findByText("3 images")).toBeInTheDocument();
    expect(screen.queryByText("1 image")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run multimodal comparison" })).toBeEnabled();
    await waitFor(() => expect(comparisonAction).toHaveFocus());
  });

  it("clears pending review controls after a successful comparison no-op", async () => {
    const user = userEvent.setup();
    const editableSnapshot: ReviewBatchGroups = {
      ...reviewSnapshot,
      groups: [
        {
          ...reviewSnapshot.groups[0],
          images: reviewSnapshot.groups[0].images.map((image) => ({
            ...image,
            isDuplicate: false,
            duplicateOfImageId: null,
          })),
        },
        reviewSnapshot.groups[1],
      ],
    };
    loadReviewBatchGroupsMock.mockResolvedValue(editableSnapshot);
    runMultimodalComparisonMock.mockResolvedValue(editableSnapshot);

    render(<ReviewBatch batchId="batch-1" />);

    await user.click(await screen.findByLabelText("Select front.jpg"));
    await user.selectOptions(
      screen.getByLabelText("Target group for back.jpg"),
      "group-1",
    );
    await user.selectOptions(
      screen.getByLabelText("Duplicate master for front.jpg"),
      "image-2",
    );
    await user.selectOptions(
      screen.getByLabelText("Approved category for Group 1"),
      "category-t-shirts",
    );

    await user.click(
      screen.getByRole("button", { name: "Run multimodal comparison" }),
    );
    await user.click(screen.getByRole("button", { name: "Run comparison" }));

    expect(
      await screen.findByText(
        "Multimodal comparison completed. Review groups were refreshed.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Select front.jpg")).not.toBeChecked();
    expect(screen.getByLabelText("Target group for back.jpg")).toHaveValue("");
    expect(screen.getByLabelText("Duplicate master for front.jpg")).toHaveValue("");
    expect(screen.getByLabelText("Approved category for Group 1")).toHaveValue("");
  });

  it("refreshes stale state once and retains comparison-not-allowed errors", async () => {
    const user = userEvent.setup();
    const refreshedSnapshot: ReviewBatchGroups = {
      ...reviewSnapshot,
      groups: [
        {
          ...reviewSnapshot.groups[0],
          status: "approved",
        },
        reviewSnapshot.groups[1],
      ],
    };
    loadReviewBatchGroupsMock
      .mockResolvedValueOnce(reviewSnapshot)
      .mockResolvedValueOnce(refreshedSnapshot);
    runMultimodalComparisonMock.mockRejectedValue(
      new ReviewBatchError(
        "Multimodal comparison cannot run after review activity.",
        409,
        "multimodal_comparison_not_allowed",
      ),
    );

    render(<ReviewBatch batchId="batch-1" />);

    await user.click(
      await screen.findByRole("button", {
        name: "Run multimodal comparison",
      }),
    );
    await user.click(screen.getByRole("button", { name: "Run comparison" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Multimodal comparison cannot run after review activity.",
    );
    await waitFor(() => {
      expect(loadReviewBatchGroupsMock).toHaveBeenCalledTimes(2);
    });
    const comparisonAction = screen.getByRole("button", {
      name: "Run multimodal comparison",
    });
    expect(comparisonAction).toHaveAttribute("aria-disabled", "true");
    await waitFor(() => expect(comparisonAction).toHaveFocus());
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Multimodal comparison cannot run after review activity.",
    );
  });

  it("keeps the current snapshot when stale-state refresh fails", async () => {
    const user = userEvent.setup();
    loadReviewBatchGroupsMock
      .mockResolvedValueOnce(reviewSnapshot)
      .mockRejectedValueOnce(new Error("Refresh failed."));
    runMultimodalComparisonMock.mockRejectedValue(
      new ReviewBatchError(
        "Multimodal comparison cannot run after review activity.",
        409,
        "multimodal_comparison_not_allowed",
      ),
    );

    render(<ReviewBatch batchId="batch-1" />);

    await user.click(
      await screen.findByRole("button", {
        name: "Run multimodal comparison",
      }),
    );
    await user.click(screen.getByRole("button", { name: "Run comparison" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Multimodal comparison cannot run after review activity.",
    );
    await waitFor(() => {
      expect(loadReviewBatchGroupsMock).toHaveBeenCalledTimes(2);
    });
    expect(screen.getByText("front.jpg")).toBeInTheDocument();
    expect(screen.getByText("back.jpg")).toBeInTheDocument();
    expect(screen.getByRole("alert")).not.toHaveTextContent("Refresh failed.");
  });

  it("moves an image and replaces state with the server response", async () => {
    const user = userEvent.setup();
    const movedSnapshot: ReviewBatchGroups = {
      ...reviewSnapshot,
      groups: [
        {
          ...reviewSnapshot.groups[0],
          images: [
            ...reviewSnapshot.groups[0].images,
            {
              ...reviewSnapshot.groups[1].images[0],
              position: 2,
              membershipSource: "manual_review",
            },
          ],
        },
      ],
    };
    loadReviewBatchGroupsMock.mockResolvedValue(reviewSnapshot);
    moveReviewImageMock.mockResolvedValue(movedSnapshot);

    render(<ReviewBatch batchId="batch-1" />);

    await user.selectOptions(
      await screen.findByLabelText("Target group for back.jpg"),
      "group-1",
    );
    const moveButtons = screen.getAllByRole("button", { name: "Move" });
    await user.click(moveButtons[2]);

    expect(moveReviewImageMock).toHaveBeenCalledWith("group-1", "image-3");
    expect(await screen.findByText("3 images")).toBeInTheDocument();
    expect(screen.queryByText("1 image")).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Run multimodal comparison" }),
    ).toHaveAttribute("aria-disabled", "true");
  });

  it("creates a group from selected editable images in upload order", async () => {
    const user = userEvent.setup();
    loadReviewBatchGroupsMock.mockResolvedValue(reviewSnapshot);
    createReviewGroupMock.mockResolvedValue({
      ...reviewSnapshot,
      groups: [
        reviewSnapshot.groups[0],
        {
          ...reviewSnapshot.groups[1],
          groupId: "group-3",
          images: [
            reviewSnapshot.groups[0].images[0],
            reviewSnapshot.groups[1].images[0],
          ],
        },
      ],
    });

    render(<ReviewBatch batchId="batch-1" />);

    await user.click(await screen.findByLabelText("Select back.jpg"));
    await user.click(screen.getByLabelText("Select front.jpg"));
    await user.click(screen.getByRole("button", { name: "Create group" }));

    expect(createReviewGroupMock).toHaveBeenCalledWith("batch-1", [
      "image-1",
      "image-3",
    ]);
    expect(await screen.findByText("0 selected")).toBeInTheDocument();
  });

  it("merges selected source groups into the selected target group", async () => {
    const user = userEvent.setup();
    const mergedSnapshot: ReviewBatchGroups = {
      ...reviewSnapshot,
      groups: [
        {
          ...reviewSnapshot.groups[0],
          images: [
            ...reviewSnapshot.groups[0].images,
            {
              ...reviewSnapshot.groups[1].images[0],
              position: 2,
              membershipSource: "manual_review",
            },
          ],
        },
      ],
    };
    loadReviewBatchGroupsMock.mockResolvedValue(reviewSnapshot);
    mergeReviewGroupsMock.mockResolvedValue(mergedSnapshot);

    render(<ReviewBatch batchId="batch-1" />);

    await user.selectOptions(
      await screen.findByLabelText("Merge target group"),
      "group-1",
    );
    await user.click(screen.getByRole("checkbox", { name: "Group 2" }));
    await user.click(screen.getByRole("button", { name: "Merge" }));

    expect(mergeReviewGroupsMock).toHaveBeenCalledWith("group-1", ["group-2"]);
    expect(await screen.findByText("3 images")).toBeInTheDocument();
    expect(screen.queryByText("1 image")).not.toBeInTheDocument();
  });

  it("splits selected images into a new group", async () => {
    const user = userEvent.setup();
    const splitSnapshot: ReviewBatchGroups = {
      ...reviewSnapshot,
      groups: [
        {
          ...reviewSnapshot.groups[0],
          images: [reviewSnapshot.groups[0].images[1]],
        },
        reviewSnapshot.groups[1],
        {
          ...reviewSnapshot.groups[0],
          groupId: "group-3",
          images: [
            {
              ...reviewSnapshot.groups[0].images[0],
              position: 0,
              membershipSource: "manual_review",
            },
          ],
        },
      ],
    };
    loadReviewBatchGroupsMock.mockResolvedValue(reviewSnapshot);
    splitReviewGroupMock.mockResolvedValue(splitSnapshot);

    render(<ReviewBatch batchId="batch-1" />);

    await user.click(await screen.findByLabelText("Select front.jpg"));
    await user.click(screen.getAllByRole("button", { name: "Split into new group" })[0]);

    expect(splitReviewGroupMock).toHaveBeenCalledWith("group-1", ["image-1"]);
    expect(await screen.findByText("0 selected")).toBeInTheDocument();
    expect(screen.getAllByText("1 image")).toHaveLength(3);
  });

  it("disables split when all images in a group are selected", async () => {
    const user = userEvent.setup();
    loadReviewBatchGroupsMock.mockResolvedValue(reviewSnapshot);

    render(<ReviewBatch batchId="batch-1" />);

    await user.click(await screen.findByLabelText("Select front.jpg"));
    await user.click(screen.getByLabelText("Select front-copy.jpg"));

    expect(
      screen.getAllByRole("button", { name: "Split into new group" })[0],
    ).toBeDisabled();
  });

  it("sets a non-duplicate image as cover", async () => {
    const user = userEvent.setup();
    const coverSnapshot: ReviewBatchGroups = {
      ...reviewSnapshot,
      groups: [
        {
          ...reviewSnapshot.groups[0],
          coverImageId: "image-2",
          images: reviewSnapshot.groups[0].images.map((image) =>
            image.imageId === "image-2"
              ? { ...image, isDuplicate: false, duplicateOfImageId: null }
              : image,
          ),
        },
        reviewSnapshot.groups[1],
      ],
    };
    loadReviewBatchGroupsMock.mockResolvedValue({
      ...reviewSnapshot,
      groups: [
        {
          ...reviewSnapshot.groups[0],
          images: reviewSnapshot.groups[0].images.map((image) =>
            image.imageId === "image-2"
              ? { ...image, isDuplicate: false, duplicateOfImageId: null }
              : image,
          ),
        },
        reviewSnapshot.groups[1],
      ],
    });
    updateReviewGroupCoverMock.mockResolvedValue(coverSnapshot);

    render(<ReviewBatch batchId="batch-1" />);

    await user.click(await screen.findByRole("button", { name: "Set cover" }));

    expect(updateReviewGroupCoverMock).toHaveBeenCalledWith("group-1", "image-2");
    expect(await screen.findAllByText("Cover")).toHaveLength(2);
  });

  it("updates and clears an approved category", async () => {
    const user = userEvent.setup();
    const categorySnapshot: ReviewBatchGroups = {
      ...reviewSnapshot,
      groups: [
        {
          ...reviewSnapshot.groups[0],
          approvedCategorySlug: "t-shirts",
        },
        reviewSnapshot.groups[1],
      ],
    };
    const clearedSnapshot: ReviewBatchGroups = {
      ...categorySnapshot,
      groups: [
        categorySnapshot.groups[0],
        {
          ...categorySnapshot.groups[1],
          approvedCategorySlug: null,
        },
      ],
    };
    loadReviewBatchGroupsMock.mockResolvedValue(reviewSnapshot);
    updateReviewGroupCategoryMock
      .mockResolvedValueOnce(categorySnapshot)
      .mockResolvedValueOnce(clearedSnapshot);

    render(<ReviewBatch batchId="batch-1" />);

    await user.selectOptions(
      await screen.findByLabelText("Approved category for Group 1"),
      "category-t-shirts",
    );
    await user.click(screen.getByLabelText("Save category for Group 1"));

    expect(updateReviewGroupCategoryMock).toHaveBeenCalledWith(
      "group-1",
      "category-t-shirts",
    );

    await user.click(await screen.findByLabelText("Clear category for Group 2"));

    expect(updateReviewGroupCategoryMock).toHaveBeenLastCalledWith("group-2", null);
  });

  it("approves a machine-prefilled category without saving it first", async () => {
    const user = userEvent.setup();
    const machinePrefilledSnapshot: ReviewBatchGroups = {
      ...reviewSnapshot,
      groups: [
        {
          ...reviewSnapshot.groups[0],
          approvedCategorySlug: "t-shirts",
          approvedCategorySource: "machine_suggestion",
          categorySuggestionStatus: "ready",
        },
        reviewSnapshot.groups[1],
      ],
    };
    const approvedSnapshot: ReviewBatchGroups = {
      ...machinePrefilledSnapshot,
      groups: [
        {
          ...machinePrefilledSnapshot.groups[0],
          status: "approved",
          categorySuggestionStatus: null,
        },
        machinePrefilledSnapshot.groups[1],
      ],
    };
    loadReviewBatchGroupsMock.mockResolvedValue(machinePrefilledSnapshot);
    approveReviewGroupMock.mockResolvedValue(approvedSnapshot);

    render(<ReviewBatch batchId="batch-1" />);

    expect(
      await screen.findByText("Prefilled from machine suggestion"),
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText("Approved category for Group 1"),
    ).toHaveValue("category-t-shirts");

    await user.click(
      screen.getAllByRole("button", { name: "Approve group" })[0],
    );

    expect(approveReviewGroupMock).toHaveBeenCalledWith("group-1");
    expect(updateReviewGroupCategoryMock).not.toHaveBeenCalled();
    expect(
      screen.queryByText("Prefilled from machine suggestion"),
    ).not.toBeInTheDocument();
  });

  it("polls pending category suggestions and recovers from a transient error", async () => {
    vi.useFakeTimers();
    const pendingSnapshot: ReviewBatchGroups = {
      ...reviewSnapshot,
      groups: [
        {
          ...reviewSnapshot.groups[0],
          suggestedCategorySlug: null,
          approvedCategorySlug: null,
          categorySuggestionStatus: "pending",
          approvedCategorySource: null,
        },
        reviewSnapshot.groups[1],
      ],
    };
    const readySnapshot: ReviewBatchGroups = {
      ...pendingSnapshot,
      groups: [
        {
          ...pendingSnapshot.groups[0],
          suggestedCategorySlug: "t-shirts",
          approvedCategorySlug: "t-shirts",
          categorySuggestionStatus: "ready",
          approvedCategorySource: "machine_suggestion",
        },
        pendingSnapshot.groups[1],
      ],
    };
    loadReviewBatchGroupsMock
      .mockResolvedValueOnce(pendingSnapshot)
      .mockRejectedValueOnce(new Error("Temporary polling failure."))
      .mockResolvedValueOnce(readySnapshot);

    render(<ReviewBatch batchId="batch-1" />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(screen.getByText("pending")).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Temporary polling failure.",
    );
    expect(screen.getByText("pending")).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });

    expect(loadReviewBatchGroupsMock).toHaveBeenCalledTimes(3);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(
      screen.getByText("Prefilled from machine suggestion"),
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText("Approved category for Group 1"),
    ).toHaveValue("category-t-shirts");
  });

  it("shows stale approved categories and allows clearing them", async () => {
    const user = userEvent.setup();
    const staleSnapshot: ReviewBatchGroups = {
      ...reviewSnapshot,
      groups: [
        reviewSnapshot.groups[0],
        {
          ...reviewSnapshot.groups[1],
          approvedCategorySlug: "archived-trousers",
        },
      ],
    };
    loadReviewBatchGroupsMock.mockResolvedValue(staleSnapshot);
    updateReviewGroupCategoryMock.mockResolvedValue({
      ...staleSnapshot,
      groups: [
        staleSnapshot.groups[0],
        {
          ...staleSnapshot.groups[1],
          approvedCategorySlug: null,
        },
      ],
    });

    render(<ReviewBatch batchId="batch-1" />);

    expect(
      await screen.findByText(
        "Current approved category is inactive or missing: archived-trousers",
      ),
    ).toBeInTheDocument();

    await user.click(screen.getByLabelText("Clear category for Group 2"));

    expect(updateReviewGroupCategoryMock).toHaveBeenCalledWith("group-2", null);
  });

  it("approves a group with an approved category", async () => {
    const user = userEvent.setup();
    const approvableSnapshot: ReviewBatchGroups = {
      ...reviewSnapshot,
      groups: [
        {
          ...reviewSnapshot.groups[0],
          approvedCategorySlug: "t-shirts",
        },
        reviewSnapshot.groups[1],
      ],
    };
    const approvedGroupSnapshot: ReviewBatchGroups = {
      ...approvableSnapshot,
      groups: [
        {
          ...approvableSnapshot.groups[0],
          status: "approved",
        },
        approvableSnapshot.groups[1],
      ],
    };
    loadReviewBatchGroupsMock.mockResolvedValue(approvableSnapshot);
    approveReviewGroupMock.mockResolvedValue(approvedGroupSnapshot);

    render(<ReviewBatch batchId="batch-1" />);

    const approveGroupButtons = await screen.findAllByRole("button", {
      name: "Approve group",
    });
    await user.click(approveGroupButtons[0]);

    expect(approveReviewGroupMock).toHaveBeenCalledWith("group-1");
    await waitFor(() => {
      expect(screen.queryByLabelText("Select front.jpg")).not.toBeInTheDocument();
    });
    expect(screen.getByLabelText("Select back.jpg")).toBeInTheDocument();
  });

  it("approves a batch after every group is approved", async () => {
    const user = userEvent.setup();
    const readySnapshot: ReviewBatchGroups = {
      ...reviewSnapshot,
      groups: reviewSnapshot.groups.map((group) => ({
        ...group,
        status: "approved",
        approvedCategorySlug: group.approvedCategorySlug ?? "t-shirts",
      })),
    };
    const approvedBatchSnapshot: ReviewBatchGroups = {
      ...readySnapshot,
      status: "approved",
    };
    loadReviewBatchGroupsMock.mockResolvedValue(readySnapshot);
    approveReviewBatchMock.mockResolvedValue(approvedBatchSnapshot);

    render(<ReviewBatch batchId="batch-1" />);

    await user.click(await screen.findByRole("button", { name: "Approve batch" }));

    expect(approveReviewBatchMock).toHaveBeenCalledWith("batch-1");
    expect(
      await screen.findByText("This batch is approved and read-only."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("marks a non-duplicate image as duplicate of another group image", async () => {
    const user = userEvent.setup();
    const markedSnapshot: ReviewBatchGroups = {
      ...reviewSnapshot,
      groups: [
        {
          ...reviewSnapshot.groups[0],
          coverImageId: "image-2",
          images: reviewSnapshot.groups[0].images.map((image) =>
            image.imageId === "image-1"
              ? {
                  ...image,
                  isDuplicate: true,
                  duplicateOfImageId: "image-2",
                  membershipSource: "manual_review",
                  membershipConfidence: null,
                }
              : { ...image, isDuplicate: false, duplicateOfImageId: null },
          ),
        },
        reviewSnapshot.groups[1],
      ],
    };
    loadReviewBatchGroupsMock.mockResolvedValue({
      ...reviewSnapshot,
      groups: [
        {
          ...reviewSnapshot.groups[0],
          images: reviewSnapshot.groups[0].images.map((image) => ({
            ...image,
            isDuplicate: false,
            duplicateOfImageId: null,
          })),
        },
        reviewSnapshot.groups[1],
      ],
    });
    updateReviewImageDuplicateMock.mockResolvedValue(markedSnapshot);

    render(<ReviewBatch batchId="batch-1" />);

    await user.selectOptions(
      await screen.findByLabelText("Duplicate master for front.jpg"),
      "image-2",
    );
    await user.click(screen.getAllByRole("button", { name: "Mark duplicate" })[0]);

    expect(updateReviewImageDuplicateMock).toHaveBeenCalledWith(
      "group-1",
      "image-1",
      "image-2",
    );
    expect(await screen.findAllByText("Duplicate")).toHaveLength(1);
    expect(screen.getByText("image-2")).toBeInTheDocument();
  });

  it("restores a duplicate image", async () => {
    const user = userEvent.setup();
    const restoredSnapshot: ReviewBatchGroups = {
      ...reviewSnapshot,
      groups: [
        {
          ...reviewSnapshot.groups[0],
          images: reviewSnapshot.groups[0].images.map((image) =>
            image.imageId === "image-2"
              ? {
                  ...image,
                  isDuplicate: false,
                  duplicateOfImageId: null,
                  membershipSource: "manual_review",
                  membershipConfidence: null,
                }
              : image,
          ),
        },
        reviewSnapshot.groups[1],
      ],
    };
    loadReviewBatchGroupsMock.mockResolvedValue(reviewSnapshot);
    updateReviewImageDuplicateMock.mockResolvedValue(restoredSnapshot);

    render(<ReviewBatch batchId="batch-1" />);

    await user.click(await screen.findByRole("button", { name: "Restore duplicate" }));

    expect(updateReviewImageDuplicateMock).toHaveBeenCalledWith(
      "group-1",
      "image-2",
      null,
    );
    expect(await screen.findAllByText("Member")).toHaveLength(3);
  });

  it("does not show cover controls for duplicate images", async () => {
    loadReviewBatchGroupsMock.mockResolvedValue(reviewSnapshot);

    render(<ReviewBatch batchId="batch-1" />);

    expect(await screen.findByText("front-copy.jpg")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Set cover" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restore duplicate" })).toBeInTheDocument();
  });

  it("keeps approved groups read-only while other groups remain editable", async () => {
    loadReviewBatchGroupsMock.mockResolvedValue({
      ...reviewSnapshot,
      groups: [
        {
          ...reviewSnapshot.groups[0],
          status: "approved",
        },
        reviewSnapshot.groups[1],
      ],
    });

    render(<ReviewBatch batchId="batch-1" />);

    expect(await screen.findByText("front.jpg")).toBeInTheDocument();
    expect(screen.queryByLabelText("Select front.jpg")).not.toBeInTheDocument();
    expect(
      screen.queryByLabelText("Target group for front.jpg"),
    ).not.toBeInTheDocument();
    expect(screen.getByLabelText("Approved category for Group 1")).toBeDisabled();
    expect(
      screen.queryByLabelText("Save category for Group 1"),
    ).not.toBeInTheDocument();
    expect(screen.getByLabelText("Select back.jpg")).toBeInTheDocument();
    expect(screen.getByLabelText("Target group for back.jpg")).toBeDisabled();
    expect(screen.getByLabelText("Approved category for Group 2")).toBeEnabled();
  });

  it("disables edit controls while a write is pending", async () => {
    const user = userEvent.setup();
    let resolveMove: ((snapshot: ReviewBatchGroups) => void) | undefined;
    loadReviewBatchGroupsMock.mockResolvedValue(reviewSnapshot);
    moveReviewImageMock.mockImplementation(
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
      expect(screen.getAllByRole("button", { name: "Move" })[2]).toBeDisabled();
      expect(
        screen.getByRole("button", { name: "Run multimodal comparison" }),
      ).toBeDisabled();
    });

    resolveMove?.(reviewSnapshot);
    await waitFor(() => {
      expect(screen.getByLabelText("Select front.jpg")).toBeEnabled();
    });
  });

  it("shows action errors from failed edits", async () => {
    const user = userEvent.setup();
    loadReviewBatchGroupsMock.mockResolvedValue(reviewSnapshot);
    moveReviewImageMock.mockRejectedValue(
      new Error("Approved groups cannot be edited."),
    );

    render(<ReviewBatch batchId="batch-1" />);

    await user.selectOptions(
      await screen.findByLabelText("Target group for back.jpg"),
      "group-1",
    );
    await user.click(screen.getAllByRole("button", { name: "Move" })[2]);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Approved groups cannot be edited.",
    );
  });

  it("shows action errors from failed category updates", async () => {
    const user = userEvent.setup();
    loadReviewBatchGroupsMock.mockResolvedValue(reviewSnapshot);
    updateReviewGroupCategoryMock.mockRejectedValue(
      new Error("approvedCategoryId must be a leaf category."),
    );

    render(<ReviewBatch batchId="batch-1" />);

    await user.selectOptions(
      await screen.findByLabelText("Approved category for Group 1"),
      "category-t-shirts",
    );
    await user.click(screen.getByLabelText("Save category for Group 1"));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "approvedCategoryId must be a leaf category.",
    );
  });

  it("shows action errors from failed approval", async () => {
    const user = userEvent.setup();
    loadReviewBatchGroupsMock.mockResolvedValue({
      ...reviewSnapshot,
      groups: [
        {
          ...reviewSnapshot.groups[0],
          approvedCategorySlug: "t-shirts",
        },
        reviewSnapshot.groups[1],
      ],
    });
    approveReviewGroupMock.mockRejectedValue(
      new Error("Group approval requires an approved category."),
    );

    render(<ReviewBatch batchId="batch-1" />);

    const approveGroupButtons = await screen.findAllByRole("button", {
      name: "Approve group",
    });
    await user.click(approveGroupButtons[0]);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Group approval requires an approved category.",
    );
  });

  it("renders an empty review-ready batch", async () => {
    loadReviewBatchGroupsMock.mockResolvedValue({
      ...reviewSnapshot,
      groups: [],
    });

    render(<ReviewBatch batchId="empty-batch" />);

    expect(await screen.findByText("No review groups yet")).toBeInTheDocument();
    expect(screen.getAllByText("0")).toHaveLength(2);
    expect(
      screen.getByText(
        "This batch is review-ready, but no product groups are currently attached to it.",
      ),
    ).toBeInTheDocument();
  });

  it("renders an approved batch as read-only", async () => {
    loadReviewBatchGroupsMock.mockResolvedValue({
      ...reviewSnapshot,
      status: "approved",
      groups: reviewSnapshot.groups.map((group) => ({
        ...group,
        status: "approved",
      })),
    });

    render(<ReviewBatch batchId="approved-batch" />);

    expect(
      await screen.findByText("This batch is approved and read-only."),
    ).toBeInTheDocument();
    expect(screen.getAllByText("approved")).toHaveLength(3);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Select front.jpg")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Approved category for Group 1")).toBeDisabled();
  });

  it("shows a load error", async () => {
    loadReviewBatchGroupsMock.mockRejectedValue(
      new Error("Batch is not ready for review."),
    );

    render(<ReviewBatch batchId="queued-batch" />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Batch is not ready for review.",
    );
    expect(screen.getByRole("link", { name: "Return to upload" })).toHaveAttribute(
      "href",
      "/admin/ingest",
    );
  });
});
