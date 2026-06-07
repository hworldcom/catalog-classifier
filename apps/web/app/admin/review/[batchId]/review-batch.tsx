"use client";

import Image from "next/image";
import { useEffect, useMemo, useState } from "react";

import {
  LocalBatch,
  LocalBatchImage,
  createLocalBatchGroup,
  loadLocalBatch,
  localBatchAssetUrl,
  moveLocalBatchImage,
} from "@/lib/local-batches";

type ReviewBatchProps = {
  batchId: string;
};

export default function ReviewBatch({ batchId }: ReviewBatchProps) {
  const [batch, setBatch] = useState<LocalBatch | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [isEditing, setIsEditing] = useState(false);
  const [selectedImageIds, setSelectedImageIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [moveTargets, setMoveTargets] = useState<Record<string, string>>({});

  useEffect(() => {
    let isCurrent = true;

    async function loadBatch() {
      try {
        const loadedBatch = await loadLocalBatch(batchId);
        if (isCurrent) {
          setBatch(loadedBatch);
          setError(null);
        }
      } catch (loadError) {
        if (isCurrent) {
          setError(
            loadError instanceof Error
              ? loadError.message
              : "The local batch could not be loaded.",
          );
        }
      }
    }

    void loadBatch();
    return () => {
      isCurrent = false;
    };
  }, [batchId]);

  const imagesById = useMemo(
    () =>
      new Map<string, LocalBatchImage>(
        batch?.images.map((image) => [image.imageId, image]) ?? [],
      ),
    [batch],
  );

  function toggleImageSelection(imageId: string) {
    setSelectedImageIds((current) => {
      const updated = new Set(current);
      if (updated.has(imageId)) {
        updated.delete(imageId);
      } else {
        updated.add(imageId);
      }
      return updated;
    });
  }

  async function handleMove(image: LocalBatchImage) {
    const targetGroupId = moveTargets[image.imageId];
    if (!targetGroupId) {
      return;
    }

    setIsEditing(true);
    setActionError(null);
    try {
      const result = await moveLocalBatchImage(
        batchId,
        image.imageId,
        targetGroupId,
      );
      setBatch(result.batch);
      setMoveTargets({});
    } catch (moveError) {
      setActionError(
        moveError instanceof Error
          ? moveError.message
          : "The image could not be moved.",
      );
    } finally {
      setIsEditing(false);
    }
  }

  async function handleCreateGroup() {
    if (!batch || selectedImageIds.size === 0) {
      return;
    }

    const imageIds = batch.images
      .filter((image) => selectedImageIds.has(image.imageId))
      .map((image) => image.imageId);

    setIsEditing(true);
    setActionError(null);
    try {
      const result = await createLocalBatchGroup(batchId, imageIds);
      setBatch(result.batch);
      setSelectedImageIds(new Set());
      setMoveTargets({});
    } catch (createError) {
      setActionError(
        createError instanceof Error
          ? createError.message
          : "The group could not be created.",
      );
    } finally {
      setIsEditing(false);
    }
  }

  if (error) {
    return (
      <main className="review-shell">
        <section className="review-header">
          <p className="eyebrow">Manual group review</p>
          <h1>Batch unavailable</h1>
          <div className="message error" role="alert">
            {error}
          </div>
          <a className="text-link" href="/admin/ingest">
            Return to upload
          </a>
        </section>
      </main>
    );
  }

  if (!batch) {
    return (
      <main className="review-shell">
        <p className="loading-state" aria-live="polite">
          Loading local batch...
        </p>
      </main>
    );
  }

  return (
    <main className="review-shell">
      <header className="review-header">
        <div>
          <p className="eyebrow">Manual group review</p>
          <h1>Review batch</h1>
          <p className="intro">
            Move individual images or select several images to create a new group.
          </p>
        </div>
        <a className="text-link" href="/admin/ingest">
          Upload another batch
        </a>
      </header>

      <dl className="batch-summary">
        <div>
          <dt>Batch</dt>
          <dd>{batch.batchId}</dd>
        </div>
        <div>
          <dt>Images</dt>
          <dd>{batch.images.length}</dd>
        </div>
        <div>
          <dt>Groups</dt>
          <dd>{batch.groups.length}</dd>
        </div>
      </dl>

      <section className="selection-toolbar" aria-label="Group creation">
        <div>
          <strong>{selectedImageIds.size} selected</strong>
          <span>Select one or more images to create a new group.</span>
        </div>
        <button
          type="button"
          disabled={isEditing || selectedImageIds.size === 0}
          onClick={handleCreateGroup}
        >
          {isEditing ? "Saving..." : "Create group"}
        </button>
      </section>

      {actionError ? (
        <div className="message error review-action-error" role="alert">
          {actionError}
        </div>
      ) : null}

      <section className="group-grid" aria-label="Review groups">
        {batch.groups.map((group, groupIndex) => {
          const groupImages = group.imageIds
            .map((imageId) => imagesById.get(imageId))
            .filter((image): image is LocalBatchImage => image !== undefined);
          return (
            <article className="group-card" key={group.groupId}>
              <header className="group-header">
                <div>
                  <p className="group-label">Group {groupIndex + 1}</p>
                  <h2>
                    {groupImages.length} image
                    {groupImages.length === 1 ? "" : "s"}
                  </h2>
                </div>
                <span className="group-kind">Group</span>
              </header>

              <ul className="image-grid">
                {groupImages.map((image) => (
                  <li className="image-card" key={image.imageId}>
                    <label className="image-selection">
                      <input
                        type="checkbox"
                        checked={selectedImageIds.has(image.imageId)}
                        disabled={isEditing}
                        onChange={() => toggleImageSelection(image.imageId)}
                      />
                      <span>Select {image.originalFilename}</span>
                    </label>
                    <a
                      className="thumbnail-link"
                      href={localBatchAssetUrl(image.imageUrl)}
                      target="_blank"
                      rel="noreferrer"
                    >
                      <Image
                        src={localBatchAssetUrl(image.thumbnailUrl)}
                        alt={image.originalFilename}
                        width={320}
                        height={320}
                        unoptimized
                      />
                    </a>
                    <div className="image-meta">
                      <span className="filename">{image.originalFilename}</span>
                      {image.isRetained ? (
                        <span className="retained-label">Retained</span>
                      ) : (
                        <span className="duplicate-label">Member</span>
                      )}
                    </div>
                    <div className="move-controls">
                      <label>
                        <span>Move to</span>
                        <select
                          aria-label={`Target group for ${image.originalFilename}`}
                          value={moveTargets[image.imageId] ?? ""}
                          disabled={isEditing || batch.groups.length < 2}
                          onChange={(event) =>
                            setMoveTargets((current) => ({
                              ...current,
                              [image.imageId]: event.target.value,
                            }))
                          }
                        >
                          <option value="">Choose group</option>
                          {batch.groups.map((targetGroup, targetIndex) => (
                            <option
                              key={targetGroup.groupId}
                              value={targetGroup.groupId}
                              disabled={targetGroup.groupId === image.groupId}
                            >
                              Group {targetIndex + 1}
                            </option>
                          ))}
                        </select>
                      </label>
                      <button
                        type="button"
                        disabled={
                          isEditing || !moveTargets[image.imageId]
                        }
                        onClick={() => handleMove(image)}
                      >
                        Move
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            </article>
          );
        })}
      </section>
    </main>
  );
}
