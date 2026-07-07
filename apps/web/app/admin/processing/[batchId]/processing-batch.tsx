"use client";

import { useEffect, useState } from "react";

import {
  ProcessingBatchResult,
  isProcessingBatchTerminal,
  loadProcessingBatch,
  startUploadBatchProcessing,
} from "@/lib/durable-uploads";

type ProcessingBatchProps = {
  batchId: string;
  pollIntervalMs?: number;
};

const DEFAULT_POLL_INTERVAL_MS = 2_000;

export default function ProcessingBatch({
  batchId,
  pollIntervalMs = DEFAULT_POLL_INTERVAL_MS,
}: ProcessingBatchProps) {
  const [snapshot, setSnapshot] = useState<ProcessingBatchResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isStarting, setIsStarting] = useState(false);
  const [hasStartedProcessing, setHasStartedProcessing] = useState(false);

  useEffect(() => {
    let isCurrent = true;

    async function loadSnapshot() {
      setIsLoading(true);
      try {
        const loadedSnapshot = await loadProcessingBatch(batchId);
        if (!isCurrent) {
          return;
        }
        setSnapshot(loadedSnapshot);
        setHasStartedProcessing(loadedSnapshot.status !== "queued");
        setError(null);
      } catch (loadError) {
        if (isCurrent) {
          setError(errorMessage(loadError, "Processing state could not be loaded."));
        }
      } finally {
        if (isCurrent) {
          setIsLoading(false);
        }
      }
    }

    void loadSnapshot();
    return () => {
      isCurrent = false;
    };
  }, [batchId]);

  useEffect(() => {
    if (
      !snapshot ||
      !hasStartedProcessing ||
      isProcessingBatchTerminal(snapshot)
    ) {
      return;
    }

    let isCurrent = true;
    const timeoutId = window.setTimeout(async () => {
      try {
        const nextSnapshot = await loadProcessingBatch(batchId);
        if (isCurrent) {
          setSnapshot(nextSnapshot);
          setError(null);
        }
      } catch (pollError) {
        if (isCurrent) {
          setError(errorMessage(pollError, "Processing state could not be refreshed."));
        }
      }
    }, pollIntervalMs);

    return () => {
      isCurrent = false;
      window.clearTimeout(timeoutId);
    };
  }, [batchId, hasStartedProcessing, pollIntervalMs, snapshot]);

  async function handleStartProcessing() {
    setIsStarting(true);
    setError(null);
    try {
      const startedSnapshot = await startUploadBatchProcessing(batchId);
      setSnapshot(startedSnapshot);
      setHasStartedProcessing(true);
    } catch (startError) {
      setError(errorMessage(startError, "Processing could not be started."));
    } finally {
      setIsStarting(false);
    }
  }

  if (isLoading && !snapshot) {
    return (
      <main className="review-shell">
        <p className="loading-state" aria-live="polite">
          Loading processing state...
        </p>
      </main>
    );
  }

  if (!snapshot) {
    return (
      <main className="review-shell">
        <section className="review-header">
          <div>
            <p className="eyebrow">Classification processing</p>
            <h1>Batch unavailable</h1>
          </div>
          <a className="text-link" href="/admin/ingest">
            Return to upload
          </a>
        </section>
        {error ? (
          <div className="message error" role="alert">
            {error}
          </div>
        ) : null}
      </main>
    );
  }

  const isTerminal = isProcessingBatchTerminal(snapshot);

  return (
    <main className="review-shell">
      <header className="review-header">
        <div>
          <p className="eyebrow">Classification processing</p>
          <h1>Process batch</h1>
          <p className="intro">
            Start the backend classification pipeline and watch image processing
            and category suggestion state.
          </p>
        </div>
        <a className="text-link" href="/admin/ingest">
          Upload another batch
        </a>
      </header>

      <dl className="batch-summary processing-summary">
        <div>
          <dt>Batch</dt>
          <dd>{snapshot.batchId}</dd>
        </div>
        <div>
          <dt>Status</dt>
          <dd>{snapshot.status}</dd>
        </div>
        <div>
          <dt>Files</dt>
          <dd>
            {snapshot.processedFileCount} / {snapshot.originalFileCount}
          </dd>
        </div>
        <div>
          <dt>Pipeline</dt>
          <dd>{snapshot.pipelineVersion}</dd>
        </div>
      </dl>

      <section className="selection-toolbar" aria-label="Processing controls">
        <div>
          <strong>
            {isTerminal
              ? "Processing reached terminal image states"
              : snapshot.status === "queued"
                ? "Batch is queued"
                : "Processing is running"}
          </strong>
          <span>
            {snapshot.status === "queued"
              ? "Start processing when you are ready to run image processing and classification."
              : "The page polls the backend processing snapshot until every visible image is terminal."}
          </span>
        </div>
        {snapshot.status === "queued" ? (
          <button
            type="button"
            onClick={handleStartProcessing}
            disabled={isStarting}
          >
            {isStarting ? "Starting..." : "Start processing"}
          </button>
        ) : null}
      </section>

      {error ? (
        <div className="message error review-action-error" role="alert">
          {error}
        </div>
      ) : null}

      <section className="processing-results" aria-label="Processing images">
        {snapshot.images.map((image) => (
          <article className="processing-card" key={image.imageId}>
            <header className="processing-card-header">
              <div>
                <p className="group-label">Image {image.uploadOrder + 1}</p>
                <h2>{image.originalFilename}</h2>
              </div>
              <span className={`file-status ${statusClass(image.imageStatus)}`}>
                {image.imageStatus}
              </span>
            </header>

            <dl className="processing-grid">
              <ProcessingField
                label="Process job"
                value={image.processJobStatus ?? "not created"}
              />
              <ProcessingField
                label="Classify job"
                value={image.classifyJobStatus ?? "not created"}
              />
              <ProcessingField
                label="Category"
                value={image.categorySlug ?? "unknown"}
              />
              <ProcessingField
                label="Confidence"
                value={
                  image.confidence === null
                    ? "unknown"
                    : image.confidence.toFixed(2)
                }
              />
              <ProcessingField
                label="Hashes"
                value={image.hasHashes ? "yes" : "no"}
              />
              <ProcessingField
                label="Embedding"
                value={image.hasEmbedding ? "yes" : "no"}
              />
            </dl>

            {image.processError ? (
              <p className="processing-error">Process error: {image.processError}</p>
            ) : null}
            {image.classifyError ? (
              <p className="processing-error">
                Classify error: {image.classifyError}
              </p>
            ) : null}
          </article>
        ))}
      </section>
    </main>
  );
}

function ProcessingField({
  label,
  value,
}: {
  label: string;
  value: string;
}) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function statusClass(status: string): string {
  if (status === "processed" || status === "uploaded") {
    return "uploaded";
  }
  if (status === "failed") {
    return "failed";
  }
  return "pending";
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}
