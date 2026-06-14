"use client";

import { ChangeEvent, FormEvent, useState } from "react";

import {
  DirectUpload,
  createUploadBatch,
  prepareDirectUploads,
  registerUploadFiles,
  uploadDirectFiles,
  validateUploadFiles,
} from "@/lib/durable-uploads";

export default function UploadPage() {
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  const [batchId, setBatchId] = useState<string | null>(null);
  const [uploads, setUploads] = useState<DirectUpload[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const uploadedCount =
    uploads?.filter((upload) => upload.status === "uploaded").length ?? 0;
  const failedCount =
    uploads?.filter((upload) => upload.status === "failed").length ?? 0;
  const uploadAttemptsFinished =
    uploads !== null &&
    uploads.every(
      (upload) => upload.status === "uploaded" || upload.status === "failed",
    );
  const resultClass = !uploadAttemptsFinished
    ? "uploading"
    : failedCount === 0
      ? "completed"
      : uploadedCount === 0
        ? "rejected"
        : "partial";
  const uploadButtonLabel =
    selectedFiles.length === 0
      ? "Upload images"
      : `Upload ${selectedFiles.length} image${
          selectedFiles.length === 1 ? "" : "s"
        }`;

  function handleFileSelection(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? []);
    setSelectedFiles(files);
    setBatchId(null);
    setUploads(null);
    setError(files.length === 0 ? null : validateUploadFiles(files));
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();

    const validationError = validateUploadFiles(selectedFiles);
    if (validationError) {
      setError(validationError);
      return;
    }

    setIsUploading(true);
    setError(null);
    setBatchId(null);
    setUploads(null);

    try {
      const batch = await createUploadBatch();
      setBatchId(batch.batchId);

      const registration = await registerUploadFiles(
        batch.batchId,
        selectedFiles,
      );
      const pendingUploads = prepareDirectUploads(
        selectedFiles,
        registration.uploads,
      );
      setUploads(pendingUploads);

      const completedUploads = await uploadDirectFiles(
        pendingUploads,
        (updatedUpload) => {
          setUploads((currentUploads) =>
            currentUploads?.map((upload) =>
              upload.uploadOrder === updatedUpload.uploadOrder
                ? updatedUpload
                : upload,
            ) ?? null,
          );
        },
      );
      setUploads(completedUploads);
    } catch (uploadError) {
      setError(
        uploadError instanceof Error
          ? uploadError.message
          : "The upload request failed.",
      );
    } finally {
      setIsUploading(false);
    }
  }

  return (
    <main className="page-shell">
      <section className="upload-card" aria-labelledby="upload-heading">
        <p className="eyebrow">Catalog ingestion</p>
        <h1 id="upload-heading">Upload JPEG images</h1>
        <p className="intro">
          Select up to 20 files. Each file may be no larger than 10 mebibytes.
          Files are registered in a durable batch and uploaded directly to
          private cloud storage.
        </p>

        <form onSubmit={handleSubmit} className="upload-form">
          <label className="file-picker">
            <span>JPEG images</span>
            <input
              type="file"
              accept=".jpg,.jpeg,image/jpeg"
              multiple
              onChange={handleFileSelection}
              disabled={isUploading}
            />
          </label>

          <div className="selection-summary">
            {selectedFiles.length === 0
              ? "No files selected"
              : `${selectedFiles.length} file${
                  selectedFiles.length === 1 ? "" : "s"
                } selected`}
          </div>

          <button
            type="submit"
            disabled={isUploading || selectedFiles.length === 0}
          >
            {isUploading ? "Uploading..." : uploadButtonLabel}
          </button>
        </form>

        {error ? (
          <div className="message error" role="alert">
            {error}
          </div>
        ) : null}

        {batchId ? (
          <section className="message batch-created" aria-live="polite">
            <strong>Durable upload batch</strong>
            <span className="upload-id">{batchId}</span>
          </section>
        ) : null}

        {uploads ? (
          <section
            className={`message ${resultClass}`}
            aria-live="polite"
            aria-label="Upload result"
          >
            <strong>
              {uploadAttemptsFinished
                ? `${uploadedCount} uploaded, ${failedCount} failed.`
                : "Uploading files..."}
            </strong>
            <ul className="file-results">
              {uploads.map((upload) => (
                <li key={upload.imageId}>
                  <span>{upload.originalFilename}</span>
                  <span className={`file-status ${upload.status}`}>
                    {upload.status}
                  </span>
                  {upload.errorMessage ? (
                    <small>{upload.errorMessage}</small>
                  ) : null}
                </li>
              ))}
            </ul>
          </section>
        ) : null}
      </section>
    </main>
  );
}
