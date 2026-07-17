import { describe, expect, it, vi } from "vitest";

import {
  ReviewBatchError,
  approveReviewBatch,
  approveReviewGroup,
  createReviewGroup,
  loadReviewCategories,
  loadReviewBatchGroups,
  mergeReviewGroups,
  moveReviewImage,
  reviewBatchAssetUrl,
  splitReviewGroup,
  updateReviewGroupCategory,
  updateReviewGroupCover,
  updateReviewImageDuplicate,
} from "@/lib/review-batches";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("review batch client", () => {
  it("loads durable review groups", async () => {
    const responseBody = {
      batchId: "batch-1",
      organizationId: "organization-1",
      status: "review_required",
      pipelineVersion: "2026-06-01",
      groups: [],
    };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(responseBody));

    await expect(
      loadReviewBatchGroups(
        "batch-1",
        fetchMock as typeof fetch,
        "http://api.example.test/",
      ),
    ).resolves.toEqual(responseBody);

    expect(fetchMock).toHaveBeenCalledWith(
      "http://api.example.test/v1/upload-batches/batch-1/groups",
    );
  });

  it("loads review categories", async () => {
    const responseBody = [
      {
        id: "category-1",
        slug: "clothing",
        parentId: null,
        nameEn: "Clothing",
      },
    ];
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(responseBody));

    await expect(
      loadReviewCategories(fetchMock as typeof fetch, "http://api.example.test/"),
    ).resolves.toEqual(responseBody);

    expect(fetchMock).toHaveBeenCalledWith(
      "http://api.example.test/v1/categories",
    );
  });

  it("surfaces backend review errors", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse(
        {
          detail: {
            code: "batch_not_review_ready",
            message: "Batch is not ready for review.",
          },
        },
        409,
      ),
    );

    await expect(
      loadReviewBatchGroups(
        "batch-1",
        fetchMock as typeof fetch,
        "http://api.example.test/",
      ),
    ).rejects.toEqual(new ReviewBatchError("Batch is not ready for review."));
  });

  it("sends review edit mutations", async () => {
    const responseBody = {
      batchId: "batch-1",
      organizationId: "organization-1",
      status: "review_required",
      pipelineVersion: "2026-06-01",
      groups: [],
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(responseBody))
      .mockResolvedValueOnce(jsonResponse(responseBody))
      .mockResolvedValueOnce(jsonResponse(responseBody))
      .mockResolvedValueOnce(jsonResponse(responseBody))
      .mockResolvedValueOnce(jsonResponse(responseBody))
      .mockResolvedValueOnce(jsonResponse(responseBody))
      .mockResolvedValueOnce(jsonResponse(responseBody))
      .mockResolvedValueOnce(jsonResponse(responseBody))
      .mockResolvedValueOnce(jsonResponse(responseBody))
      .mockResolvedValueOnce(jsonResponse(responseBody));

    await expect(
      createReviewGroup(
        "batch-1",
        ["image-1", "image-2"],
        fetchMock as typeof fetch,
        "http://api.example.test/",
      ),
    ).resolves.toEqual(responseBody);
    await expect(
      moveReviewImage(
        "group-2",
        "image-1",
        fetchMock as typeof fetch,
        "http://api.example.test/",
      ),
    ).resolves.toEqual(responseBody);
    await expect(
      mergeReviewGroups(
        "group-1",
        ["group-2", "group-3"],
        fetchMock as typeof fetch,
        "http://api.example.test/",
      ),
    ).resolves.toEqual(responseBody);
    await expect(
      splitReviewGroup(
        "group-1",
        ["image-2"],
        fetchMock as typeof fetch,
        "http://api.example.test/",
      ),
    ).resolves.toEqual(responseBody);
    await expect(
      updateReviewGroupCover(
        "group-1",
        "image-2",
        fetchMock as typeof fetch,
        "http://api.example.test/",
      ),
    ).resolves.toEqual(responseBody);
    await expect(
      updateReviewGroupCategory(
        "group-1",
        "category-1",
        fetchMock as typeof fetch,
        "http://api.example.test/",
      ),
    ).resolves.toEqual(responseBody);
    await expect(
      updateReviewImageDuplicate(
        "group-1",
        "image-2",
        "image-1",
        fetchMock as typeof fetch,
        "http://api.example.test/",
      ),
    ).resolves.toEqual(responseBody);
    await expect(
      updateReviewImageDuplicate(
        "group-1",
        "image-2",
        null,
        fetchMock as typeof fetch,
        "http://api.example.test/",
      ),
    ).resolves.toEqual(responseBody);
    await expect(
      approveReviewGroup(
        "group-1",
        fetchMock as typeof fetch,
        "http://api.example.test/",
      ),
    ).resolves.toEqual(responseBody);
    await expect(
      approveReviewBatch(
        "batch-1",
        fetchMock as typeof fetch,
        "http://api.example.test/",
      ),
    ).resolves.toEqual(responseBody);

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://api.example.test/v1/upload-batches/batch-1/groups",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ imageIds: ["image-1", "image-2"] }),
      },
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "http://api.example.test/v1/groups/group-2/images",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ imageId: "image-1" }),
      },
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "http://api.example.test/v1/groups/merge",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          targetGroupId: "group-1",
          sourceGroupIds: ["group-2", "group-3"],
        }),
      },
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      "http://api.example.test/v1/groups/group-1/split",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ imageIds: ["image-2"] }),
      },
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      5,
      "http://api.example.test/v1/groups/group-1",
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ coverImageId: "image-2" }),
      },
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      6,
      "http://api.example.test/v1/groups/group-1",
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approvedCategoryId: "category-1" }),
      },
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      7,
      "http://api.example.test/v1/groups/group-1/images/image-2",
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          isDuplicate: true,
          duplicateOfImageId: "image-1",
        }),
      },
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      8,
      "http://api.example.test/v1/groups/group-1/images/image-2",
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          isDuplicate: false,
          duplicateOfImageId: null,
        }),
      },
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      9,
      "http://api.example.test/v1/groups/group-1/approve",
      {
        method: "POST",
      },
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      10,
      "http://api.example.test/v1/upload-batches/batch-1/approve",
      {
        method: "POST",
      },
    );
  });

  it("resolves review asset paths against the API origin", () => {
    expect(
      reviewBatchAssetUrl(
        "/v1/upload-batches/batch-1/images/image-1/thumbnail",
        "http://api.example.test/",
      ),
    ).toBe(
      "http://api.example.test/v1/upload-batches/batch-1/images/image-1/thumbnail",
    );
    expect(
      reviewBatchAssetUrl(
        "https://cdn.example.test/image.jpg",
        "http://api.example.test/",
      ),
    ).toBe("https://cdn.example.test/image.jpg");
  });
});
