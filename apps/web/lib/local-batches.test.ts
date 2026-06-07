import { describe, expect, it, vi } from "vitest";

import {
  LocalBatchError,
  createLocalBatch,
  createLocalBatchGroup,
  loadLocalBatch,
  localBatchAssetUrl,
  moveLocalBatchImage,
} from "@/lib/local-batches";

describe("local batch client", () => {
  it("creates a batch with files under the multipart files field", async () => {
    const responseBody = {
      batchId: "batch-1",
      status: "completed",
      manifestVersion: 1,
      files: [],
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(responseBody), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const files = [
      new File(["first"], "first.jpg", { type: "image/jpeg" }),
      new File(["second"], "second.jpg", { type: "image/jpeg" }),
    ];

    const result = await createLocalBatch(
      files,
      fetchMock as typeof fetch,
      "http://api.example.test/",
    );

    expect(result).toEqual(responseBody);
    const [url, request] = fetchMock.mock.calls[0];
    expect(url).toBe("http://api.example.test/v1/local-batches");
    expect(request.method).toBe("POST");
    expect((request.body as FormData).getAll("files")).toEqual(files);
  });

  it("loads a saved batch by identifier", async () => {
    const responseBody = {
      batchId: "batch-1",
      status: "ready",
      manifestVersion: 1,
      images: [],
      groups: [],
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(responseBody), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    const result = await loadLocalBatch(
      "batch-1",
      fetchMock as typeof fetch,
      "http://api.example.test",
    );

    expect(result).toEqual(responseBody);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://api.example.test/v1/local-batches/batch-1",
    );
  });

  it("surfaces structured backend errors", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          detail: { code: "batch_not_found", message: "Local batch was not found." },
        }),
        {
          status: 404,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );

    await expect(
      loadLocalBatch(
        "missing",
        fetchMock as typeof fetch,
        "http://api.example.test",
      ),
    ).rejects.toEqual(new LocalBatchError("Local batch was not found."));
  });

  it("resolves relative image paths against the backend", () => {
    expect(
      localBatchAssetUrl(
        "/v1/local-batches/batch-1/images/image-1",
        "http://api.example.test/",
      ),
    ).toBe("http://api.example.test/v1/local-batches/batch-1/images/image-1");
  });

  it("moves an image with a JSON patch request", async () => {
    const responseBody = {
      batch: {
        batchId: "batch-1",
        status: "ready",
        manifestVersion: 1,
        images: [],
        groups: [],
      },
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(responseBody), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    const result = await moveLocalBatchImage(
      "batch-1",
      "image-1",
      "group-2",
      fetchMock as typeof fetch,
      "http://api.example.test",
    );

    expect(result).toEqual(responseBody);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://api.example.test/v1/local-batches/batch-1/images/image-1",
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ groupId: "group-2" }),
      },
    );
  });

  it("creates a group with selected image identifiers", async () => {
    const responseBody = {
      groupId: "group-3",
      batch: {
        batchId: "batch-1",
        status: "ready",
        manifestVersion: 1,
        images: [],
        groups: [],
      },
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(responseBody), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    const result = await createLocalBatchGroup(
      "batch-1",
      ["image-1", "image-2"],
      fetchMock as typeof fetch,
      "http://api.example.test",
    );

    expect(result).toEqual(responseBody);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://api.example.test/v1/local-batches/batch-1/groups",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ imageIds: ["image-1", "image-2"] }),
      },
    );
  });
});
