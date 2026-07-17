"use client";

import { useEffect, useState } from "react";

import {
  ReviewBatchGroups,
  ReviewCategory,
  ReviewGroup,
  ReviewGroupImage,
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

type ReviewBatchProps = {
  batchId: string;
};

export default function ReviewBatch({ batchId }: ReviewBatchProps) {
  const [snapshot, setSnapshot] = useState<ReviewBatchGroups | null>(null);
  const [categories, setCategories] = useState<ReviewCategory[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [isEditing, setIsEditing] = useState(false);
  const [selectedImageIds, setSelectedImageIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [moveTargets, setMoveTargets] = useState<Record<string, string>>({});
  const [mergeTargetGroupId, setMergeTargetGroupId] = useState("");
  const [mergeSourceGroupIds, setMergeSourceGroupIds] = useState<Set<string>>(
    () => new Set(),
  );

  useEffect(() => {
    let isCurrent = true;

    async function loadReviewData() {
      try {
        const [loadedSnapshot, loadedCategories] = await Promise.all([
          loadReviewBatchGroups(batchId),
          loadReviewCategories(),
        ]);
        if (isCurrent) {
          setSnapshot(loadedSnapshot);
          setCategories(loadedCategories);
          setError(null);
        }
      } catch (loadError) {
        if (isCurrent) {
          setError(
            loadError instanceof Error
              ? loadError.message
              : "The review batch could not be loaded.",
          );
        }
      }
    }

    void loadReviewData();
    return () => {
      isCurrent = false;
    };
  }, [batchId]);

  if (error) {
    return (
      <main className="review-shell">
        <section className="review-header">
          <div>
            <p className="eyebrow">Manual product review</p>
            <h1>Batch unavailable</h1>
          </div>
          <a className="text-link" href="/admin/ingest">
            Return to upload
          </a>
        </section>
        <div className="message error" role="alert">
          {error}
        </div>
      </main>
    );
  }

  if (!snapshot || !categories) {
    return (
      <main className="review-shell">
        <p className="loading-state" aria-live="polite">
          Loading review batch...
        </p>
      </main>
    );
  }

  const imageCount = snapshot.groups.reduce(
    (total, group) => total + group.images.length,
    0,
  );
  const isApprovedBatch = snapshot.status === "approved";
  const isReviewEditable = snapshot.status === "review_required";
  const selectedEditableImageIds = orderedEditableImages(snapshot)
    .filter((image) => selectedImageIds.has(image.imageId))
    .map((image) => image.imageId);
  const editableGroups = snapshot.groups.filter(
    (group) => group.status !== "approved",
  );
  const hasEditableGroups = editableGroups.length > 0;
  const canApproveBatch = snapshot.groups.every(
    (group) => group.status === "approved",
  );
  const selectedMergeSourceGroupIds = editableGroups
    .filter((group) => mergeSourceGroupIds.has(group.groupId))
    .map((group) => group.groupId);
  const canMergeGroups =
    Boolean(mergeTargetGroupId) && selectedMergeSourceGroupIds.length > 0;

  function resetEditSelections() {
    setSelectedImageIds(new Set());
    setMoveTargets({});
    setMergeTargetGroupId("");
    setMergeSourceGroupIds(new Set());
  }

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

  function handleMergeTargetChange(targetGroupId: string) {
    setMergeTargetGroupId(targetGroupId);
    setMergeSourceGroupIds((current) => {
      const updated = new Set(current);
      updated.delete(targetGroupId);
      return updated;
    });
  }

  function toggleMergeSourceGroup(groupId: string) {
    if (groupId === mergeTargetGroupId) {
      return;
    }

    setMergeSourceGroupIds((current) => {
      const updated = new Set(current);
      if (updated.has(groupId)) {
        updated.delete(groupId);
      } else {
        updated.add(groupId);
      }
      return updated;
    });
  }

  async function handleCreateGroup() {
    if (selectedEditableImageIds.length === 0) {
      return;
    }

    setIsEditing(true);
    setActionError(null);
    try {
      const updatedSnapshot = await createReviewGroup(
        batchId,
        selectedEditableImageIds,
      );
      setSnapshot(updatedSnapshot);
      resetEditSelections();
    } catch (createError) {
      setActionError(errorMessage(createError, "The group could not be created."));
    } finally {
      setIsEditing(false);
    }
  }

  async function handleMove(imageId: string) {
    const targetGroupId = moveTargets[imageId];
    if (!targetGroupId) {
      return;
    }

    setIsEditing(true);
    setActionError(null);
    try {
      const updatedSnapshot = await moveReviewImage(targetGroupId, imageId);
      setSnapshot(updatedSnapshot);
      resetEditSelections();
    } catch (moveError) {
      setActionError(errorMessage(moveError, "The image could not be moved."));
    } finally {
      setIsEditing(false);
    }
  }

  async function handleMergeGroups() {
    if (!canMergeGroups) {
      return;
    }

    setIsEditing(true);
    setActionError(null);
    try {
      const updatedSnapshot = await mergeReviewGroups(
        mergeTargetGroupId,
        selectedMergeSourceGroupIds,
      );
      setSnapshot(updatedSnapshot);
      resetEditSelections();
    } catch (mergeError) {
      setActionError(errorMessage(mergeError, "The groups could not be merged."));
    } finally {
      setIsEditing(false);
    }
  }

  async function handleSplitGroup(groupId: string, imageIds: string[]) {
    if (imageIds.length === 0) {
      return;
    }

    setIsEditing(true);
    setActionError(null);
    try {
      const updatedSnapshot = await splitReviewGroup(groupId, imageIds);
      setSnapshot(updatedSnapshot);
      resetEditSelections();
    } catch (splitError) {
      setActionError(errorMessage(splitError, "The group could not be split."));
    } finally {
      setIsEditing(false);
    }
  }

  async function handleSetCover(groupId: string, imageId: string) {
    setIsEditing(true);
    setActionError(null);
    try {
      const updatedSnapshot = await updateReviewGroupCover(groupId, imageId);
      setSnapshot(updatedSnapshot);
      resetEditSelections();
    } catch (coverError) {
      setActionError(errorMessage(coverError, "The cover image could not be updated."));
    } finally {
      setIsEditing(false);
    }
  }

  async function handleUpdateCategory(
    groupId: string,
    approvedCategoryId: string | null,
  ) {
    setIsEditing(true);
    setActionError(null);
    try {
      const updatedSnapshot = await updateReviewGroupCategory(
        groupId,
        approvedCategoryId,
      );
      setSnapshot(updatedSnapshot);
      resetEditSelections();
    } catch (categoryError) {
      setActionError(
        errorMessage(categoryError, "The approved category could not be updated."),
      );
    } finally {
      setIsEditing(false);
    }
  }

  async function handleMarkDuplicate(
    groupId: string,
    imageId: string,
    duplicateOfImageId: string,
  ) {
    if (!duplicateOfImageId) {
      return;
    }

    setIsEditing(true);
    setActionError(null);
    try {
      const updatedSnapshot = await updateReviewImageDuplicate(
        groupId,
        imageId,
        duplicateOfImageId,
      );
      setSnapshot(updatedSnapshot);
      resetEditSelections();
    } catch (duplicateError) {
      setActionError(
        errorMessage(duplicateError, "The duplicate state could not be updated."),
      );
    } finally {
      setIsEditing(false);
    }
  }

  async function handleRestoreDuplicate(groupId: string, imageId: string) {
    setIsEditing(true);
    setActionError(null);
    try {
      const updatedSnapshot = await updateReviewImageDuplicate(
        groupId,
        imageId,
        null,
      );
      setSnapshot(updatedSnapshot);
      resetEditSelections();
    } catch (duplicateError) {
      setActionError(
        errorMessage(duplicateError, "The duplicate state could not be updated."),
      );
    } finally {
      setIsEditing(false);
    }
  }

  async function handleApproveGroup(groupId: string) {
    setIsEditing(true);
    setActionError(null);
    try {
      const updatedSnapshot = await approveReviewGroup(groupId);
      setSnapshot(updatedSnapshot);
      resetEditSelections();
    } catch (approvalError) {
      setActionError(errorMessage(approvalError, "The group could not be approved."));
    } finally {
      setIsEditing(false);
    }
  }

  async function handleApproveBatch() {
    if (!canApproveBatch) {
      return;
    }

    setIsEditing(true);
    setActionError(null);
    try {
      const updatedSnapshot = await approveReviewBatch(batchId);
      setSnapshot(updatedSnapshot);
      resetEditSelections();
    } catch (approvalError) {
      setActionError(errorMessage(approvalError, "The batch could not be approved."));
    } finally {
      setIsEditing(false);
    }
  }

  return (
    <main className="review-shell">
      <header className="review-header">
        <div>
          <p className="eyebrow">Manual product review</p>
          <h1>Review batch</h1>
          <p className="intro">
            Inspect durable product groups before manual edits or approval are
            enabled.
          </p>
        </div>
        <a className="text-link" href="/admin/ingest">
          Upload another batch
        </a>
      </header>

      <dl className="batch-summary review-summary">
        <div>
          <dt>Batch</dt>
          <dd>{snapshot.batchId}</dd>
        </div>
        <div>
          <dt>Status</dt>
          <dd>{snapshot.status}</dd>
        </div>
        <div>
          <dt>Groups</dt>
          <dd>{snapshot.groups.length}</dd>
        </div>
        <div>
          <dt>Images</dt>
          <dd>{imageCount}</dd>
        </div>
        <div>
          <dt>Pipeline</dt>
          <dd>{snapshot.pipelineVersion ?? "unknown"}</dd>
        </div>
      </dl>

      {isApprovedBatch ? (
        <div className="message completed review-readonly-note">
          This batch is approved and read-only.
        </div>
      ) : null}

      {isReviewEditable ? (
        <div className="review-edit-toolbars">
          {hasEditableGroups ? (
            <>
              <section className="selection-toolbar" aria-label="Group creation">
                <div>
                  <strong>{selectedEditableImageIds.length} selected</strong>
                  <span>Select one or more images to create a new group.</span>
                </div>
                <button
                  type="button"
                  disabled={isEditing || selectedEditableImageIds.length === 0}
                  onClick={handleCreateGroup}
                >
                  {isEditing ? "Saving..." : "Create group"}
                </button>
              </section>

              <section className="merge-toolbar" aria-label="Group merge">
                <div>
                  <strong>Merge groups</strong>
                  <span>Choose one target group and one or more source groups.</span>
                </div>
                <label>
                  <span>Target group</span>
                  <select
                    aria-label="Merge target group"
                    value={mergeTargetGroupId}
                    disabled={isEditing || editableGroups.length === 0}
                    onChange={(event) => handleMergeTargetChange(event.target.value)}
                  >
                    <option value="">Choose target</option>
                    {editableGroups.map((group) => (
                      <option key={group.groupId} value={group.groupId}>
                        {reviewGroupLabel(snapshot.groups, group.groupId)}
                      </option>
                    ))}
                  </select>
                </label>
                <fieldset>
                  <legend>Source groups</legend>
                  <div className="merge-source-list">
                    {editableGroups.map((group) => (
                      <label key={group.groupId}>
                        <input
                          type="checkbox"
                          checked={mergeSourceGroupIds.has(group.groupId)}
                          disabled={isEditing || group.groupId === mergeTargetGroupId}
                          onChange={() => toggleMergeSourceGroup(group.groupId)}
                        />
                        <span>{reviewGroupLabel(snapshot.groups, group.groupId)}</span>
                      </label>
                    ))}
                  </div>
                </fieldset>
                <button
                  type="button"
                  disabled={isEditing || !canMergeGroups}
                  onClick={handleMergeGroups}
                >
                  Merge
                </button>
              </section>
            </>
          ) : null}

          <section className="approval-toolbar" aria-label="Batch approval">
            <div>
              <strong>Approve batch</strong>
              <span>
                {canApproveBatch
                  ? "All groups are approved. The batch can now be frozen."
                  : "Approve every group before approving the batch."}
              </span>
            </div>
            <button
              type="button"
              disabled={isEditing || !canApproveBatch}
              onClick={handleApproveBatch}
            >
              Approve batch
            </button>
          </section>
        </div>
      ) : null}

      {actionError ? (
        <div className="message error review-action-error" role="alert">
          {actionError}
        </div>
      ) : null}

      {snapshot.groups.length === 0 ? (
        <section className="empty-review-state">
          <h2>No review groups yet</h2>
          <p>
            This batch is review-ready, but no product groups are currently
            attached to it.
          </p>
        </section>
      ) : (
        <section className="group-grid" aria-label="Review groups">
          {snapshot.groups.map((group, groupIndex) => (
            <ReviewGroupCard
              group={group}
              groups={snapshot.groups}
              groupIndex={groupIndex}
              categories={categories}
              isEditing={isEditing}
              isReviewEditable={isReviewEditable}
              moveTargets={moveTargets}
              selectedImageIds={selectedImageIds}
              onMove={handleMove}
              onMoveTargetChange={(imageId, targetGroupId) =>
                setMoveTargets((current) => ({
                  ...current,
                  [imageId]: targetGroupId,
                }))
              }
              onMarkDuplicate={handleMarkDuplicate}
              onApproveGroup={handleApproveGroup}
              onSplit={handleSplitGroup}
              onRestoreDuplicate={handleRestoreDuplicate}
              onSetCover={handleSetCover}
              onUpdateCategory={handleUpdateCategory}
              onToggleImageSelection={toggleImageSelection}
              key={group.groupId}
            />
          ))}
        </section>
      )}
    </main>
  );
}

function ReviewGroupCard({
  group,
  groups,
  groupIndex,
  categories,
  isEditing,
  isReviewEditable,
  moveTargets,
  selectedImageIds,
  onMove,
  onMoveTargetChange,
  onMarkDuplicate,
  onApproveGroup,
  onSplit,
  onRestoreDuplicate,
  onSetCover,
  onUpdateCategory,
  onToggleImageSelection,
}: {
  group: ReviewGroup;
  groups: ReviewGroup[];
  groupIndex: number;
  categories: ReviewCategory[];
  isEditing: boolean;
  isReviewEditable: boolean;
  moveTargets: Record<string, string>;
  selectedImageIds: Set<string>;
  onMove: (imageId: string) => void;
  onMoveTargetChange: (imageId: string, targetGroupId: string) => void;
  onMarkDuplicate: (
    groupId: string,
    imageId: string,
    duplicateOfImageId: string,
  ) => void;
  onApproveGroup: (groupId: string) => void;
  onSplit: (groupId: string, imageIds: string[]) => void;
  onRestoreDuplicate: (groupId: string, imageId: string) => void;
  onSetCover: (groupId: string, imageId: string) => void;
  onUpdateCategory: (groupId: string, approvedCategoryId: string | null) => void;
  onToggleImageSelection: (imageId: string) => void;
}) {
  const groupLabel = `Group ${groupIndex + 1}`;
  const isGroupEditable = isReviewEditable && group.status !== "approved";
  const editableTargetGroups = groups.filter(
    (targetGroup) =>
      targetGroup.groupId !== group.groupId && targetGroup.status !== "approved",
  );
  const selectedGroupImageIds = group.images
    .filter((image) => selectedImageIds.has(image.imageId))
    .map((image) => image.imageId);
  const canApproveGroup = group.approvedCategorySlug !== null;

  return (
    <article className="group-card">
      <header className="group-header">
        <div>
          <p className="group-label">{groupLabel}</p>
          <h2>
            {group.images.length} image
            {group.images.length === 1 ? "" : "s"}
          </h2>
        </div>
        <span className="group-kind">{group.status}</span>
      </header>

      <dl className="group-details">
        <ReviewField
          label="Suggested category"
          value={group.suggestedCategorySlug ?? "unknown"}
        />
        <ReviewCategoryField
          approvedCategorySlug={group.approvedCategorySlug}
          categories={categories}
          groupLabel={groupLabel}
          isEditing={isEditing}
          isEditable={isGroupEditable}
          onUpdateCategory={(approvedCategoryId) =>
            onUpdateCategory(group.groupId, approvedCategoryId)
          }
          key={`${group.groupId}:${group.approvedCategorySlug ?? ""}`}
        />
        <ReviewField label="Confidence" value={formatConfidence(group.confidence)} />
        <ReviewField
          label="Existing product"
          value={group.possibleExistingProductId ?? "none"}
        />
      </dl>

      {group.warnings.length > 0 ? (
        <ul className="warning-list" aria-label={`${groupLabel} warnings`}>
          {group.warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      ) : null}

      {isGroupEditable ? (
        <div className="group-edit-actions">
          <div className="group-edit-action-block">
            <span>
              {selectedGroupImageIds.length} selected in {groupLabel}
            </span>
            <button
              type="button"
              disabled={
                isEditing ||
                selectedGroupImageIds.length === 0 ||
                selectedGroupImageIds.length >= group.images.length
              }
              onClick={() => onSplit(group.groupId, selectedGroupImageIds)}
            >
              Split into new group
            </button>
          </div>
          <div className="group-edit-action-block">
            <span>
              {canApproveGroup
                ? "This group is ready for approval."
                : "Select an approved category before approving this group."}
            </span>
            <button
              type="button"
              disabled={isEditing || !canApproveGroup}
              onClick={() => onApproveGroup(group.groupId)}
            >
              Approve group
            </button>
          </div>
        </div>
      ) : null}

      <ul className="image-grid">
        {group.images.map((image) => (
          <ReviewImageCard
            editableTargetGroups={editableTargetGroups}
            groups={groups}
            image={image}
            isEditing={isEditing}
            isGroupEditable={isGroupEditable}
            isCover={image.imageId === group.coverImageId}
            moveTarget={moveTargets[image.imageId] ?? ""}
            nonDuplicateImages={group.images.filter(
              (candidate) => !candidate.isDuplicate,
            )}
            selected={selectedImageIds.has(image.imageId)}
            onMove={onMove}
            onMoveTargetChange={onMoveTargetChange}
            onMarkDuplicate={(imageId, duplicateOfImageId) =>
              onMarkDuplicate(group.groupId, imageId, duplicateOfImageId)
            }
            onRestoreDuplicate={(imageId) =>
              onRestoreDuplicate(group.groupId, imageId)
            }
            onSetCover={(imageId) => onSetCover(group.groupId, imageId)}
            onToggleImageSelection={onToggleImageSelection}
            key={image.imageId}
          />
        ))}
      </ul>
    </article>
  );
}

function ReviewCategoryField({
  approvedCategorySlug,
  categories,
  groupLabel,
  isEditing,
  isEditable,
  onUpdateCategory,
}: {
  approvedCategorySlug: string | null;
  categories: ReviewCategory[];
  groupLabel: string;
  isEditing: boolean;
  isEditable: boolean;
  onUpdateCategory: (approvedCategoryId: string | null) => void;
}) {
  const categoryById = new Map(
    categories.map((category) => [category.id, category]),
  );
  const categoryBySlug = new Map(
    categories.map((category) => [category.slug, category]),
  );
  const parentCategoryIds = new Set(
    categories
      .map((category) => category.parentId)
      .filter((parentId): parentId is string => parentId !== null),
  );
  const currentCategoryId =
    approvedCategorySlug !== null
      ? categoryBySlug.get(approvedCategorySlug)?.id ?? ""
      : "";
  const [selectedCategoryId, setSelectedCategoryId] =
    useState(currentCategoryId);

  const isStaleApprovedCategory =
    approvedCategorySlug !== null && currentCategoryId === "";
  const hasChanged = selectedCategoryId !== currentCategoryId;

  return (
    <div>
      <dt>Approved category</dt>
      <dd className="category-control">
        <select
          aria-label={`Approved category for ${groupLabel}`}
          value={selectedCategoryId}
          disabled={!isEditable || isEditing}
          onChange={(event) => setSelectedCategoryId(event.target.value)}
        >
          <option value="">No approved category</option>
          {categories.map((category) => (
            <option
              key={category.id}
              value={category.id}
              disabled={parentCategoryIds.has(category.id)}
            >
              {categoryOptionLabel(category, categoryById)}
            </option>
          ))}
        </select>
        {isStaleApprovedCategory ? (
          <span className="category-stale-note">
            Current approved category is inactive or missing: {approvedCategorySlug}
          </span>
        ) : null}
        {isEditable ? (
          <div className="category-actions">
            <button
              type="button"
              className="secondary-action"
              aria-label={`Save category for ${groupLabel}`}
              disabled={isEditing || !hasChanged}
              onClick={() => onUpdateCategory(selectedCategoryId || null)}
            >
              Save category
            </button>
            <button
              type="button"
              className="secondary-action"
              aria-label={`Clear category for ${groupLabel}`}
              disabled={isEditing || approvedCategorySlug === null}
              onClick={() => onUpdateCategory(null)}
            >
              Clear category
            </button>
          </div>
        ) : null}
      </dd>
    </div>
  );
}

function ReviewImageCard({
  editableTargetGroups,
  groups,
  image,
  isEditing,
  isCover,
  isGroupEditable,
  moveTarget,
  nonDuplicateImages,
  selected,
  onMove,
  onMoveTargetChange,
  onMarkDuplicate,
  onRestoreDuplicate,
  onSetCover,
  onToggleImageSelection,
}: {
  editableTargetGroups: ReviewGroup[];
  groups: ReviewGroup[];
  image: ReviewGroupImage;
  isEditing: boolean;
  isCover: boolean;
  isGroupEditable: boolean;
  moveTarget: string;
  nonDuplicateImages: ReviewGroupImage[];
  selected: boolean;
  onMove: (imageId: string) => void;
  onMoveTargetChange: (imageId: string, targetGroupId: string) => void;
  onMarkDuplicate: (imageId: string, duplicateOfImageId: string) => void;
  onRestoreDuplicate: (imageId: string) => void;
  onSetCover: (imageId: string) => void;
  onToggleImageSelection: (imageId: string) => void;
}) {
  const duplicateMasterOptions = nonDuplicateImages.filter(
    (candidate) => candidate.imageId !== image.imageId,
  );
  const [duplicateMasterId, setDuplicateMasterId] = useState("");

  return (
    <li className="image-card">
      {isGroupEditable ? (
        <label className="image-selection">
          <input
            type="checkbox"
            checked={selected}
            disabled={isEditing}
            onChange={() => onToggleImageSelection(image.imageId)}
          />
          <span>Select {image.originalFilename}</span>
        </label>
      ) : (
        <div className="image-position">Image {image.uploadOrder + 1}</div>
      )}
      <a
        className="thumbnail-link"
        href={reviewBatchAssetUrl(image.thumbnailUrl)}
        target="_blank"
        rel="noreferrer"
      >
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={reviewBatchAssetUrl(image.thumbnailUrl)}
          alt={image.originalFilename}
        />
      </a>
      <div className="image-meta">
        <span className="filename">{image.originalFilename}</span>
        <div className="review-image-badges">
          {isCover ? <span className="cover-label">Cover</span> : null}
          {image.isDuplicate ? (
            <span className="duplicate-label">Duplicate</span>
          ) : (
            <span className="retained-label">Member</span>
          )}
        </div>
        <dl className="image-details">
          <ReviewField label="Source" value={image.membershipSource} />
          <ReviewField
            label="Confidence"
            value={formatConfidence(image.membershipConfidence)}
          />
          {image.duplicateOfImageId ? (
            <ReviewField label="Duplicate of" value={image.duplicateOfImageId} />
          ) : null}
        </dl>
      </div>
      {isGroupEditable ? (
        <div className="image-edit-controls">
          {!image.isDuplicate && !isCover ? (
            <button
              type="button"
              className="secondary-action"
              disabled={isEditing}
              onClick={() => onSetCover(image.imageId)}
            >
              Set cover
            </button>
          ) : null}
          {image.isDuplicate ? (
            <button
              type="button"
              className="secondary-action"
              disabled={isEditing}
              onClick={() => onRestoreDuplicate(image.imageId)}
            >
              Restore duplicate
            </button>
          ) : (
            <div className="duplicate-controls">
              <label>
                <span>Duplicate of</span>
                <select
                  aria-label={`Duplicate master for ${image.originalFilename}`}
                  value={duplicateMasterId}
                  disabled={isEditing || duplicateMasterOptions.length === 0}
                  onChange={(event) => setDuplicateMasterId(event.target.value)}
                >
                  <option value="">Choose image</option>
                  {duplicateMasterOptions.map((candidate) => (
                    <option key={candidate.imageId} value={candidate.imageId}>
                      {candidate.originalFilename}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                disabled={isEditing || !duplicateMasterId}
                onClick={() => onMarkDuplicate(image.imageId, duplicateMasterId)}
              >
                Mark duplicate
              </button>
            </div>
          )}
          <div className="move-controls">
            <label>
              <span>Move to</span>
              <select
                aria-label={`Target group for ${image.originalFilename}`}
                value={moveTarget}
                disabled={isEditing || editableTargetGroups.length === 0}
                onChange={(event) =>
                  onMoveTargetChange(image.imageId, event.target.value)
                }
              >
                <option value="">Choose group</option>
                {editableTargetGroups.map((targetGroup) => (
                  <option key={targetGroup.groupId} value={targetGroup.groupId}>
                    {reviewGroupLabel(groups, targetGroup.groupId)}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              disabled={isEditing || !moveTarget}
              onClick={() => onMove(image.imageId)}
            >
              Move
            </button>
          </div>
        </div>
      ) : null}
    </li>
  );
}

function categoryOptionLabel(
  category: ReviewCategory,
  categoryById: Map<string, ReviewCategory>,
): string {
  let depth = 0;
  let parentId = category.parentId;
  const visitedCategoryIds = new Set<string>();

  while (
    parentId !== null &&
    categoryById.has(parentId) &&
    !visitedCategoryIds.has(parentId)
  ) {
    visitedCategoryIds.add(parentId);
    depth += 1;
    parentId = categoryById.get(parentId)?.parentId ?? null;
  }

  return `${"  ".repeat(depth)}${category.nameEn}`;
}

function ReviewField({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function formatConfidence(confidence: number | null): string {
  return confidence === null ? "unknown" : confidence.toFixed(2);
}

function reviewGroupLabel(groups: ReviewGroup[], groupId: string): string {
  const groupIndex = groups.findIndex((group) => group.groupId === groupId);
  return `Group ${groupIndex + 1}`;
}

function orderedEditableImages(snapshot: ReviewBatchGroups): ReviewGroupImage[] {
  return snapshot.groups
    .filter((group) => group.status !== "approved")
    .flatMap((group) => group.images)
    .sort((first, second) => first.uploadOrder - second.uploadOrder);
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}
