"use client";

import { ChangeEvent, FormEvent, useState } from "react";

import {
  DurableUploadError,
  UploadSessionRow,
  createUploadBatch,
  finalizeUploadBatch,
  isRetryableUpload,
  loadUploadBatch,
  prepareDirectUploads,
  prepareRetryUploads,
  reconcileUploadSessionRows,
  registerUploadFiles,
  requestRetryUploads,
  toUploadSessionRows,
  uploadDirectFiles,
  validateUploadFiles,
} from "@/lib/durable-uploads";

export default function UploadPage() {
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  const [batchId, setBatchId] = useState<string | null>(null);
  const [batchStatus, setBatchStatus] = useState<string | null>(null);
  const [uploads, setUploads] = useState<UploadSessionRow[] | null>(null);
  const [selectedRetryImageIds, setSelectedRetryImageIds] = useState<Set<string>>(
    new Set(),
  );
  const [error, setError] = useState<string | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const [isRetrying, setIsRetrying] = useState(false);
  const isBusy = isUploading || isRetrying;
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
    setBatchStatus(null);
    setUploads(null);
    setSelectedRetryImageIds(new Set());
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
    setBatchStatus(null);
    setUploads(null);
    setSelectedRetryImageIds(new Set());

    try {
      const batch = await createUploadBatch();
      setBatchId(batch.batchId);
      setBatchStatus(batch.status);

      const registration = await registerUploadFiles(
        batch.batchId,
        selectedFiles,
      );
      setBatchStatus(registration.status);
      const pendingUploads = prepareDirectUploads(
        selectedFiles,
        registration.uploads,
      );
      setUploads(toUploadSessionRows(pendingUploads));

      const completedUploads = await uploadDirectFiles(
        pendingUploads,
        (updatedUpload) => {
          setUploads((currentUploads) =>
            currentUploads?.map((upload) =>
              upload.imageId === updatedUpload.imageId
                ? sessionRowFromUpload(updatedUpload)
                : upload,
            ) ?? null,
          );
        },
      );
      const completedRows = toUploadSessionRows(completedUploads);
      setUploads(completedRows);
      await finalizeIfEveryRowUploaded(batch.batchId, completedRows);
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

  function toggleRetrySelection(imageId: string) {
    setSelectedRetryImageIds((currentSelection) => {
      const nextSelection = new Set(currentSelection);
      if (nextSelection.has(imageId)) {
        nextSelection.delete(imageId);
      } else {
        nextSelection.add(imageId);
      }
      return nextSelection;
    });
  }

  async function handleRetrySelected() {
    if (!batchId || !uploads || selectedRetryImageIds.size === 0) {
      return;
    }

    setIsRetrying(true);
    setError(null);

    try {
      const batch = await loadUploadBatch(batchId);
      setBatchStatus(batch.status);
      const reconciledUploads = reconcileUploadSessionRows(uploads, batch);
      setUploads(reconciledUploads);

      if (batch.status !== "uploading") {
        setSelectedRetryImageIds(new Set());
        throw new DurableUploadError(
          "This batch is no longer uploading and cannot accept retries.",
        );
      }

      const selectedRows = reconciledUploads
        .filter((upload) => selectedRetryImageIds.has(upload.imageId))
        .sort((left, right) => left.uploadOrder - right.uploadOrder);
      if (
        selectedRows.length !== selectedRetryImageIds.size ||
        selectedRows.some((upload) => !isRetryableUpload(upload))
      ) {
        setSelectedRetryImageIds(new Set());
        throw new DurableUploadError(
          "Upload state changed. Review the retryable rows and select them again.",
        );
      }

      const selectedImageIds = selectedRows.map((upload) => upload.imageId);
      const retryRegistration = await requestRetryUploads(
        batchId,
        selectedImageIds,
      );
      setBatchStatus(retryRegistration.status);
      const retryUploads = prepareRetryUploads(
        reconciledUploads,
        selectedImageIds,
        retryRegistration.uploads,
      );

      setUploads((currentUploads) =>
        updateSessionRows(
          currentUploads,
          retryUploads.map(sessionRowFromUpload),
        ),
      );

      const completedRetries = await uploadDirectFiles(
        retryUploads,
        (updatedUpload) => {
          setUploads((currentUploads) =>
            updateSessionRows(currentUploads, [
              sessionRowFromUpload(updatedUpload),
            ]),
          );
        },
      );
      const completedRetryRows = completedRetries.map(sessionRowFromUpload);
      const nextRows = updateSessionRows(reconciledUploads, completedRetryRows);
      setUploads(nextRows);
      setSelectedRetryImageIds(new Set());
      if (nextRows) {
        await finalizeIfEveryRowUploaded(batchId, nextRows);
      }
    } catch (retryError) {
      setError(
        retryError instanceof Error
          ? retryError.message
          : "The retry request failed.",
      );
    } finally {
      setIsRetrying(false);
    }
  }

  async function finalizeIfEveryRowUploaded(
    uploadBatchId: string,
    rows: UploadSessionRow[],
  ) {
    if (!rows.every((row) => row.status === "uploaded")) {
      return;
    }

    const finalizedBatch = await finalizeUploadBatch(uploadBatchId);
    setBatchStatus(finalizedBatch.status);
    setUploads(reconcileUploadSessionRows(rows, finalizedBatch));
    setSelectedRetryImageIds(new Set());
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
              disabled={isBusy}
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
            disabled={isBusy || selectedFiles.length === 0}
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
            {batchStatus ? (
              <span className="batch-status">Backend status: {batchStatus}</span>
            ) : null}
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
                  <label className="retry-checkbox">
                    <input
                      type="checkbox"
                      aria-label={`Select ${upload.originalFilename} item ${
                        upload.uploadOrder + 1
                      } for retry`}
                      checked={selectedRetryImageIds.has(upload.imageId)}
                      disabled={isBusy || !isRetryableUpload(upload)}
                      onChange={() => toggleRetrySelection(upload.imageId)}
                    />
                    <span>Retry</span>
                  </label>
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
            <div className="retry-actions">
              <span>
                {selectedRetryImageIds.size} selected for retry
              </span>
              <button
                type="button"
                onClick={handleRetrySelected}
                disabled={isBusy || selectedRetryImageIds.size === 0}
              >
                {isRetrying ? "Retrying..." : "Retry selected"}
              </button>
            </div>
          </section>
        ) : null}
      </section>
    </main>
  );
}

function sessionRowFromUpload(
  upload: {
    imageId: string;
    uploadOrder: number;
    originalFilename: string;
    file: File;
    status: UploadSessionRow["status"];
    errorMessage: string | null;
  },
): UploadSessionRow {
  return {
    imageId: upload.imageId,
    uploadOrder: upload.uploadOrder,
    originalFilename: upload.originalFilename,
    file: upload.file,
    status: upload.status,
    errorMessage: upload.errorMessage,
  };
}

function updateSessionRows(
  currentRows: UploadSessionRow[] | null,
  updatedRows: UploadSessionRow[],
): UploadSessionRow[] | null {
  if (!currentRows) {
    return null;
  }
  const updatesById = new Map(
    updatedRows.map((upload) => [upload.imageId, upload]),
  );
  return currentRows.map((upload) => updatesById.get(upload.imageId) ?? upload);
}
