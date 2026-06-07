import { describe, expect, it, vi } from "vitest";

import {
  UploadHandshakeError,
  uploadImages,
} from "@/lib/upload-handshake";

describe("uploadImages", () => {
  it("sends every file under the multipart files field", async () => {
    const responseBody = {
      uploadId: "upload-1",
      status: "completed",
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

    const result = await uploadImages(
      files,
      fetchMock as typeof fetch,
      "http://api.example.test/",
    );

    expect(result).toEqual(responseBody);
    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, request] = fetchMock.mock.calls[0];
    expect(url).toBe("http://api.example.test/v1/upload-handshake");
    expect(request.method).toBe("POST");
    expect((request.body as FormData).getAll("files")).toEqual(files);
  });

  it("surfaces a structured backend rejection", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          detail: { code: "too_many_files", message: "Upload at most 20 files." },
        }),
        {
          status: 400,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );

    await expect(
      uploadImages([], fetchMock as typeof fetch, "http://api.example.test"),
    ).rejects.toEqual(new UploadHandshakeError("Upload at most 20 files."));
  });
});

