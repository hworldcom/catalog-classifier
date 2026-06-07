"use client";

import { ChangeEvent, FormEvent, useState } from "react";
import { useRouter } from "next/navigation";

import {
  CreateLocalBatchResult,
  MAX_FILES_PER_REQUEST,
  createLocalBatch,
} from "@/lib/local-batches";

export default function UploadPage() {
  const router = useRouter();
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  const [result, setResult] = useState<CreateLocalBatchResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const uploadButtonLabel =
    selectedFiles.length === 0
      ? "Upload images"
      : `Upload ${selectedFiles.length} image${
          selectedFiles.length === 1 ? "" : "s"
        }`;

  function handleFileSelection(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? []);
    setSelectedFiles(files);
    setResult(null);

    if (files.length > MAX_FILES_PER_REQUEST) {
      setError(`Select at most ${MAX_FILES_PER_REQUEST} JPEG files.`);
    } else {
      setError(null);
    }
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();

    if (selectedFiles.length === 0) {
      setError("Select at least one JPEG file.");
      return;
    }

    if (selectedFiles.length > MAX_FILES_PER_REQUEST) {
      setError(`Select at most ${MAX_FILES_PER_REQUEST} JPEG files.`);
      return;
    }

    setIsUploading(true);
    setError(null);
    setResult(null);

    try {
      const batchResult = await createLocalBatch(selectedFiles);
      if (batchResult.batchId) {
        router.push(`/admin/review/${batchResult.batchId}`);
      } else {
        setResult(batchResult);
      }
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
          Accepted files are stored locally and grouped only when their original
          bytes are identical.
        </p>

        <form onSubmit={handleSubmit} className="upload-form">
          <label className="file-picker">
            <span>JPEG images</span>
            <input
              type="file"
              accept=".jpg,.jpeg,image/jpeg"
              multiple
              onChange={handleFileSelection}
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

        {result ? (
          <section
            className={`message ${result.status}`}
            aria-live="polite"
            aria-label="Upload result"
            role="alert"
          >
            <strong>The backend rejected every selected file.</strong>
            <ul className="file-results">
              {result.files.map((file, index) => (
                <li key={`${file.originalFilename}-${index}`}>
                  <span>{file.originalFilename}</span>
                  <span className={`file-status ${file.status}`}>
                    {file.status}
                  </span>
                  {file.errorMessage ? <small>{file.errorMessage}</small> : null}
                </li>
              ))}
            </ul>
          </section>
        ) : null}
      </section>
    </main>
  );
}
