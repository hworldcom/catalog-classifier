import { describe, expect, it, vi } from "vitest";

import {
  DurableUploadError,
  MAX_CONCURRENT_UPLOADS,
  MAX_FILE_SIZE_BYTES,
  createUploadBatch,
  prepareDirectUploads,
  registerUploadFiles,
  uploadDirectFiles,
  validateUploadFiles,
} from "@/lib/durable-uploads";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function registeredUpload(uploadOrder: number, filename: string) {
  return {
    imageId: `image-${uploadOrder}`,
    uploadOrder,
    originalFilename: filename,
    originalObjectKey: `objects/image-${uploadOrder}.jpg`,
    uploadUrl: `https://uploads.example.test/image-${uploadOrder}`,
  };
}

describe("durable upload client", () => {
  it("validates file count, JPEG type, and size boundaries", () => {
    expect(validateUploadFiles([])).toBe("Select at least one JPEG file.");
    expect(
      validateUploadFiles(
        Array.from({ length: 21 }, (_, index) =>
          new File(["image"], `image-${index}.jpg`, { type: "image/jpeg" }),
        ),
      ),
    ).toBe("Select at most 20 JPEG files.");
    expect(
      validateUploadFiles([
        new File(["text"], "notes.txt", { type: "text/plain" }),
      ]),
    ).toBe("notes.txt must be a JPEG file.");
    expect(
      validateUploadFiles([
        new File([], "empty.jpg", { type: "image/jpeg" }),
      ]),
    ).toBe("empty.jpg must not be empty.");
    expect(
      validateUploadFiles([
        new File(
          [new Uint8Array(MAX_FILE_SIZE_BYTES + 1)],
          "oversized.jpg",
          { type: "image/jpeg" },
        ),
      ]),
    ).toBe("oversized.jpg must not exceed 10 mebibytes.");
    expect(
      validateUploadFiles([
        new File(
          [new Uint8Array(MAX_FILE_SIZE_BYTES)],
          "maximum-size.jpg",
          { type: "image/jpeg" },
        ),
      ]),
    ).toBeNull();
    expect(
      validateUploadFiles([
        new File(["image"], "valid.jpg", { type: "image/jpeg" }),
      ]),
    ).toBeNull();
  });

  it("creates a durable upload batch", async () => {
    const responseBody = {
      batchId: "batch-1",
      status: "created",
      maxFiles: 20,
    };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(responseBody));

    const result = await createUploadBatch(
      fetchMock as typeof fetch,
      "http://api.example.test/",
    );

    expect(result).toEqual(responseBody);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://api.example.test/v1/upload-batches",
      { method: "POST" },
    );
  });

  it("registers ordered metadata and surfaces backend errors", async () => {
    const files = [
      new File(["first"], "product.jpg", { type: "image/jpeg" }),
      new File(["second"], "product.jpg", { type: "image/jpeg" }),
    ];
    const responseBody = {
      batchId: "batch-1",
      status: "uploading",
      uploads: [
        registeredUpload(0, "product.jpg"),
        registeredUpload(1, "product.jpg"),
      ],
    };
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse(responseBody));

    const result = await registerUploadFiles(
      "batch-1",
      files,
      fetchMock as typeof fetch,
      "http://api.example.test",
    );

    expect(result).toEqual(responseBody);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://api.example.test/v1/upload-batches/batch-1/uploads",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          files: [
            {
              originalFilename: "product.jpg",
              mimeType: "image/jpeg",
              sizeBytes: 5,
            },
            {
              originalFilename: "product.jpg",
              mimeType: "image/jpeg",
              sizeBytes: 6,
            },
          ],
        }),
      },
    );

    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        {
          detail: {
            code: "upload_registration_failed",
            message: "Unable to register files for upload.",
          },
        },
        500,
      ),
    );

    await expect(
      registerUploadFiles(
        "batch-1",
        files,
        fetchMock as typeof fetch,
        "http://api.example.test",
      ),
    ).rejects.toEqual(
      new DurableUploadError("Unable to register files for upload."),
    );
  });

  it("matches duplicate filenames to registrations by upload order", () => {
    const firstFile = new File(["first"], "product.jpg", {
      type: "image/jpeg",
    });
    const secondFile = new File(["second"], "product.jpg", {
      type: "image/jpeg",
    });

    const uploads = prepareDirectUploads(
      [firstFile, secondFile],
      [
        registeredUpload(1, "product.jpg"),
        registeredUpload(0, "product.jpg"),
      ],
    );

    expect(uploads.map((upload) => upload.file)).toEqual([
      firstFile,
      secondFile,
    ]);
    expect(uploads.map((upload) => upload.uploadOrder)).toEqual([0, 1]);
    expect(uploads.map((upload) => upload.status)).toEqual([
      "pending",
      "pending",
    ]);
  });

  it("rejects incomplete or duplicate registration ordering", () => {
    const files = [
      new File(["first"], "first.jpg", { type: "image/jpeg" }),
      new File(["second"], "second.jpg", { type: "image/jpeg" }),
    ];

    expect(() =>
      prepareDirectUploads(files, [registeredUpload(0, "first.jpg")]),
    ).toThrow("The backend returned invalid upload registration data.");
    expect(() =>
      prepareDirectUploads(files, [
        registeredUpload(0, "first.jpg"),
        registeredUpload(0, "second.jpg"),
      ]),
    ).toThrow("The backend returned invalid upload registration data.");
  });

  it("limits uploads to four workers and continues after failures", async () => {
    const uploads = prepareDirectUploads(
      Array.from({ length: 5 }, (_, index) =>
        new File([`image-${index}`], `image-${index}.jpg`, {
          type: "image/jpeg",
        }),
      ),
      Array.from({ length: 5 }, (_, index) =>
        registeredUpload(index, `image-${index}.jpg`),
      ),
    );
    const gates = Array.from({ length: 5 }, () => {
      let resolve!: (response: Response) => void;
      let reject!: (error: Error) => void;
      const promise = new Promise<Response>((resolvePromise, rejectPromise) => {
        resolve = resolvePromise;
        reject = rejectPromise;
      });
      return { promise, resolve, reject };
    });
    let activeUploads = 0;
    let maximumActiveUploads = 0;
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      const index = Number(url.split("-").at(-1));
      activeUploads += 1;
      maximumActiveUploads = Math.max(maximumActiveUploads, activeUploads);
      return gates[index].promise.finally(() => {
        activeUploads -= 1;
      });
    });
    const updates: string[] = [];

    const resultPromise = uploadDirectFiles(
      uploads,
      (upload) => updates.push(`${upload.uploadOrder}:${upload.status}`),
      fetchMock as typeof fetch,
    );

    await vi.waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(MAX_CONCURRENT_UPLOADS);
    });
    expect(maximumActiveUploads).toBe(MAX_CONCURRENT_UPLOADS);

    gates[0].resolve(new Response(null, { status: 200 }));
    gates[1].resolve(new Response(null, { status: 503 }));
    gates[2].reject(new Error("Network unavailable."));
    gates[3].resolve(new Response(null, { status: 204 }));

    await vi.waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(5);
    });
    gates[4].resolve(new Response(null, { status: 200 }));

    const results = await resultPromise;

    expect(results.map((upload) => upload.status)).toEqual([
      "uploaded",
      "failed",
      "failed",
      "uploaded",
      "uploaded",
    ]);
    expect(results[1].errorMessage).toBe(
      "Cloud Storage rejected the upload with status 503.",
    );
    expect(results[2].errorMessage).toBe("Network unavailable.");
    expect(updates).toContain("4:uploading");
    expect(updates).toContain("4:uploaded");
  });

  it("uses PUT with the signed content type and file body", async () => {
    const file = new File(["image"], "front.jpg", { type: "image/jpeg" });
    const uploads = prepareDirectUploads(
      [file],
      [registeredUpload(0, "front.jpg")],
    );
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(null, { status: 200 }));

    await uploadDirectFiles(
      uploads,
      () => undefined,
      fetchMock as typeof fetch,
    );

    expect(fetchMock).toHaveBeenCalledWith(
      "https://uploads.example.test/image-0",
      {
        method: "PUT",
        headers: { "Content-Type": "image/jpeg" },
        body: file,
      },
    );
  });
});
