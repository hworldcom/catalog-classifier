"use client";

import { useEffect, useState } from "react";

import {
  ProcessingBatchImage,
  ProcessingBatchResult,
  isProcessingBatchTerminal,
  loadProcessingBatch,
  processingThumbnailUrl,
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
  const [pollCycle, setPollCycle] = useState(0);
  const [failedThumbnailKeys, setFailedThumbnailKeys] = useState<Set<string>>(
    () => new Set(),
  );

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

  const batchStatus = snapshot?.status;

  useEffect(() => {
    if (batchStatus !== "processing") {
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
      } finally {
        if (isCurrent) {
          setPollCycle((currentCycle) => currentCycle + 1);
        }
      }
    }, pollIntervalMs);

    return () => {
      isCurrent = false;
      window.clearTimeout(timeoutId);
    };
  }, [batchId, batchStatus, pollCycle, pollIntervalMs]);

  async function handleStartProcessing() {
    setIsStarting(true);
    setError(null);
    try {
      const startedSnapshot = await startUploadBatchProcessing(batchId);
      setSnapshot(startedSnapshot);
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
  const statusMessage = processingStatusMessage(snapshot.status, isTerminal);
  const reviewNavigation = reviewNavigationFor(
    snapshot.status,
    snapshot.batchId,
  );

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
          <strong>{statusMessage.heading}</strong>
          <span>{statusMessage.description}</span>
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
        {reviewNavigation ? (
          <a
            className="processing-review-link"
            href={reviewNavigation.href}
          >
            {reviewNavigation.label}
          </a>
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
              <ThumbnailPreview
                batchId={snapshot.batchId}
                image={image}
                failedThumbnailKeys={failedThumbnailKeys}
                onThumbnailError={(thumbnailKey) =>
                  setFailedThumbnailKeys((currentKeys) => {
                    const nextKeys = new Set(currentKeys);
                    nextKeys.add(thumbnailKey);
                    return nextKeys;
                  })
                }
              />
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

function ThumbnailPreview({
  batchId,
  image,
  failedThumbnailKeys,
  onThumbnailError,
}: {
  batchId: string;
  image: ProcessingBatchImage;
  failedThumbnailKeys: Set<string>;
  onThumbnailError: (thumbnailKey: string) => void;
}) {
  const thumbnailKey = thumbnailStateKey(image);
  const isReadyForThumbnail =
    image.imageStatus === "processed" && image.processJobStatus === "completed";
  const isUnavailable = failedThumbnailKeys.has(thumbnailKey);

  return (
    <div className="processing-thumbnail">
      {!isReadyForThumbnail || isUnavailable ? (
        <div className="thumbnail-placeholder">Thumbnail pending</div>
      ) : (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          key={thumbnailKey}
          src={processingThumbnailUrl(batchId, image.imageId)}
          alt={`Thumbnail for ${image.originalFilename}`}
          onError={() => onThumbnailError(thumbnailKey)}
        />
      )}
    </div>
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

function thumbnailStateKey(image: ProcessingBatchImage): string {
  return [
    image.imageId,
    image.imageStatus,
    image.processJobStatus ?? "none",
    image.classifyJobStatus ?? "none",
    image.hasHashes ? "hashes" : "no-hashes",
    image.hasEmbedding ? "embedding" : "no-embedding",
  ].join(":");
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

function processingStatusMessage(
  status: string,
  imageJobsAreTerminal: boolean,
): { heading: string; description: string } {
  if (status === "queued") {
    return {
      heading: "Batch is queued",
      description:
        "Start processing when you are ready to run image processing and classification.",
    };
  }
  if (status === "processing" && imageJobsAreTerminal) {
    return {
      heading: "Preparing review groups",
      description:
        "Image processing is complete. Preparing review groups...",
    };
  }
  if (status === "processing") {
    return {
      heading: "Processing is running",
      description:
        "The page polls the backend processing snapshot until review groups are ready.",
    };
  }
  if (status === "review_required") {
    return {
      heading: "Review groups are ready",
      description:
        "Open the durable review page when you are ready to inspect the proposed groups.",
    };
  }
  if (status === "approved") {
    return {
      heading: "Review is approved",
      description:
        "The approved review remains available as a read-only record.",
    };
  }
  if (status === "failed") {
    return {
      heading: "Processing stopped",
      description:
        "Processing stopped before review groups could be prepared.",
    };
  }
  if (status === "cancelled") {
    return {
      heading: "Processing was cancelled",
      description: "Processing was cancelled.",
    };
  }
  return {
    heading: "Processing state is unavailable",
    description: `The batch reported an unsupported status: ${status}.`,
  };
}

function reviewNavigationFor(
  status: string,
  batchId: string,
): { href: string; label: string } | null {
  if (status === "review_required") {
    return {
      href: `/admin/review/${batchId}`,
      label: "Review groups",
    };
  }
  if (status === "approved") {
    return {
      href: `/admin/review/${batchId}`,
      label: "View approved review",
    };
  }
  return null;
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}
