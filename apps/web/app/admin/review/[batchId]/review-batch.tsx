"use client";

import { useEffect, useRef, useState } from "react";

import {
  ReviewBatchError,
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
  rejectReviewImage,
  reviewBatchAssetUrl,
  restoreReviewImageRejection,
  runMultimodalComparison,
  splitReviewGroup,
  updateReviewGroupCategory,
  updateReviewGroupCover,
  updateReviewImageDuplicate,
} from "@/lib/review-batches";

type ReviewBatchProps = {
  batchId: string;
};

type PendingImageRejection = {
  groupId: string;
  imageId: string;
  originalFilename: string;
};

const CATEGORY_SUGGESTION_POLL_INTERVAL_MS = 2_000;

export default function ReviewBatch({ batchId }: ReviewBatchProps) {
  const [snapshot, setSnapshot] = useState<ReviewBatchGroups | null>(null);
  const [categories, setCategories] = useState<ReviewCategory[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionSuccess, setActionSuccess] = useState<string | null>(null);
  const [suggestionPollingError, setSuggestionPollingError] =
    useState<string | null>(null);
  const [isEditing, setIsEditing] = useState(false);
  const [isComparisonRunning, setIsComparisonRunning] = useState(false);
  const [isComparisonDialogOpen, setIsComparisonDialogOpen] = useState(false);
  const [pendingImageRejection, setPendingImageRejection] =
    useState<PendingImageRejection | null>(null);
  const [hasLocalReviewActivity, setHasLocalReviewActivity] = useState(false);
  const [transientStateVersion, setTransientStateVersion] = useState(0);
  const comparisonActionRef = useRef<HTMLButtonElement>(null);
  const rejectionActionRef = useRef<HTMLButtonElement | null>(null);
  const [selectedImageIds, setSelectedImageIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [moveTargets, setMoveTargets] = useState<Record<string, string>>({});
  const [mergeTargetGroupId, setMergeTargetGroupId] = useState("");
  const [mergeSourceGroupIds, setMergeSourceGroupIds] = useState<Set<string>>(
    () => new Set(),
  );
  const isBusy = isEditing || isComparisonRunning;
  const shouldPollCategorySuggestions =
    snapshot?.status === "review_required" &&
    snapshot.groups.some(
      (group) =>
        group.status === "proposed" &&
        group.categorySuggestionStatus === "pending",
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

  useEffect(() => {
    if (!shouldPollCategorySuggestions || isBusy) {
      return;
    }

    let isCurrent = true;
    let timeoutId: number | undefined;

    async function pollCategorySuggestions() {
      try {
        const updatedSnapshot = await loadReviewBatchGroups(batchId);
        if (isCurrent) {
          setSnapshot(updatedSnapshot);
          setSuggestionPollingError(null);
        }
      } catch (pollError) {
        if (isCurrent) {
          setSuggestionPollingError(
            errorMessage(
              pollError,
              "Category suggestions could not be refreshed.",
            ),
          );
        }
      } finally {
        if (isCurrent) {
          timeoutId = window.setTimeout(
            pollCategorySuggestions,
            CATEGORY_SUGGESTION_POLL_INTERVAL_MS,
          );
        }
      }
    }

    timeoutId = window.setTimeout(
      pollCategorySuggestions,
      CATEGORY_SUGGESTION_POLL_INTERVAL_MS,
    );
    return () => {
      isCurrent = false;
      if (timeoutId !== undefined) {
        window.clearTimeout(timeoutId);
      }
    };
  }, [batchId, isBusy, shouldPollCategorySuggestions]);

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
  const hasApprovedGroup = snapshot.groups.some(
    (group) => group.status === "approved",
  );
  const isComparisonUnavailable =
    hasApprovedGroup || hasLocalReviewActivity;
  const isComparisonDisabled =
    isBusy || isComparisonUnavailable;
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

  function resetTransientEditState() {
    setSelectedImageIds(new Set());
    setMoveTargets({});
    setMergeTargetGroupId("");
    setMergeSourceGroupIds(new Set());
    setTransientStateVersion((current) => current + 1);
  }

  function beginReviewMutation(): boolean {
    if (isBusy) {
      return false;
    }

    setIsEditing(true);
    setActionError(null);
    setActionSuccess(null);
    return true;
  }

  function applyReviewMutationSnapshot(updatedSnapshot: ReviewBatchGroups) {
    setSnapshot(updatedSnapshot);
    setSuggestionPollingError(null);
    setHasLocalReviewActivity(true);
    resetTransientEditState();
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
    if (selectedEditableImageIds.length === 0 || !beginReviewMutation()) {
      return;
    }

    try {
      const updatedSnapshot = await createReviewGroup(
        batchId,
        selectedEditableImageIds,
      );
      applyReviewMutationSnapshot(updatedSnapshot);
    } catch (createError) {
      setActionError(errorMessage(createError, "The group could not be created."));
    } finally {
      setIsEditing(false);
    }
  }

  async function handleMove(imageId: string) {
    const targetGroupId = moveTargets[imageId];
    if (!targetGroupId || !beginReviewMutation()) {
      return;
    }

    try {
      const updatedSnapshot = await moveReviewImage(targetGroupId, imageId);
      applyReviewMutationSnapshot(updatedSnapshot);
    } catch (moveError) {
      setActionError(errorMessage(moveError, "The image could not be moved."));
    } finally {
      setIsEditing(false);
    }
  }

  async function handleMergeGroups() {
    if (!canMergeGroups || !beginReviewMutation()) {
      return;
    }

    try {
      const updatedSnapshot = await mergeReviewGroups(
        mergeTargetGroupId,
        selectedMergeSourceGroupIds,
      );
      applyReviewMutationSnapshot(updatedSnapshot);
    } catch (mergeError) {
      setActionError(errorMessage(mergeError, "The groups could not be merged."));
    } finally {
      setIsEditing(false);
    }
  }

  async function handleSplitGroup(groupId: string, imageIds: string[]) {
    if (imageIds.length === 0 || !beginReviewMutation()) {
      return;
    }

    try {
      const updatedSnapshot = await splitReviewGroup(groupId, imageIds);
      applyReviewMutationSnapshot(updatedSnapshot);
    } catch (splitError) {
      setActionError(errorMessage(splitError, "The group could not be split."));
    } finally {
      setIsEditing(false);
    }
  }

  async function handleSetCover(groupId: string, imageId: string) {
    if (!beginReviewMutation()) {
      return;
    }

    try {
      const updatedSnapshot = await updateReviewGroupCover(groupId, imageId);
      applyReviewMutationSnapshot(updatedSnapshot);
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
    if (!beginReviewMutation()) {
      return;
    }

    try {
      const updatedSnapshot = await updateReviewGroupCategory(
        groupId,
        approvedCategoryId,
      );
      applyReviewMutationSnapshot(updatedSnapshot);
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
    if (!duplicateOfImageId || !beginReviewMutation()) {
      return;
    }

    try {
      const updatedSnapshot = await updateReviewImageDuplicate(
        groupId,
        imageId,
        duplicateOfImageId,
      );
      applyReviewMutationSnapshot(updatedSnapshot);
    } catch (duplicateError) {
      setActionError(
        errorMessage(duplicateError, "The duplicate state could not be updated."),
      );
    } finally {
      setIsEditing(false);
    }
  }

  async function handleRestoreDuplicate(groupId: string, imageId: string) {
    if (!beginReviewMutation()) {
      return;
    }

    try {
      const updatedSnapshot = await updateReviewImageDuplicate(
        groupId,
        imageId,
        null,
      );
      applyReviewMutationSnapshot(updatedSnapshot);
    } catch (duplicateError) {
      setActionError(
        errorMessage(duplicateError, "The duplicate state could not be updated."),
      );
    } finally {
      setIsEditing(false);
    }
  }

  function restoreRejectionActionFocus() {
    window.setTimeout(() => {
      rejectionActionRef.current?.focus();
    }, 0);
  }

  function handleOpenRejectionDialog(
    groupId: string,
    imageId: string,
    originalFilename: string,
    trigger: HTMLButtonElement,
  ) {
    if (isBusy) {
      return;
    }

    rejectionActionRef.current = trigger;
    setActionError(null);
    setActionSuccess(null);
    setPendingImageRejection({
      groupId,
      imageId,
      originalFilename,
    });
  }

  function handleCancelRejection() {
    setPendingImageRejection(null);
    restoreRejectionActionFocus();
  }

  async function handleRejectImage() {
    const pendingRejection = pendingImageRejection;
    if (!pendingRejection || !beginReviewMutation()) {
      return;
    }

    setPendingImageRejection(null);
    try {
      const updatedSnapshot = await rejectReviewImage(
        pendingRejection.groupId,
        pendingRejection.imageId,
      );
      applyReviewMutationSnapshot(updatedSnapshot);
    } catch (rejectionError) {
      setActionError(
        errorMessage(
          rejectionError,
          "The image could not be excluded from export.",
        ),
      );
    } finally {
      setIsEditing(false);
      restoreRejectionActionFocus();
    }
  }

  async function handleRestoreImageRejection(
    groupId: string,
    imageId: string,
  ) {
    if (!beginReviewMutation()) {
      return;
    }

    try {
      const updatedSnapshot = await restoreReviewImageRejection(
        groupId,
        imageId,
      );
      applyReviewMutationSnapshot(updatedSnapshot);
    } catch (rejectionError) {
      setActionError(
        errorMessage(
          rejectionError,
          "The image could not be restored for export.",
        ),
      );
    } finally {
      setIsEditing(false);
    }
  }

  async function handleApproveGroup(groupId: string) {
    if (!beginReviewMutation()) {
      return;
    }

    try {
      const updatedSnapshot = await approveReviewGroup(groupId);
      applyReviewMutationSnapshot(updatedSnapshot);
    } catch (approvalError) {
      setActionError(errorMessage(approvalError, "The group could not be approved."));
    } finally {
      setIsEditing(false);
    }
  }

  async function handleApproveBatch() {
    if (!canApproveBatch || !beginReviewMutation()) {
      return;
    }

    try {
      const updatedSnapshot = await approveReviewBatch(batchId);
      applyReviewMutationSnapshot(updatedSnapshot);
    } catch (approvalError) {
      setActionError(errorMessage(approvalError, "The batch could not be approved."));
    } finally {
      setIsEditing(false);
    }
  }

  function restoreComparisonActionFocus() {
    window.setTimeout(() => {
      comparisonActionRef.current?.focus();
    }, 0);
  }

  function handleOpenComparisonDialog() {
    if (isComparisonDisabled) {
      return;
    }

    setActionError(null);
    setActionSuccess(null);
    setIsComparisonDialogOpen(true);
  }

  function handleCancelComparison() {
    setIsComparisonDialogOpen(false);
    restoreComparisonActionFocus();
  }

  async function handleRunComparison() {
    if (isBusy) {
      return;
    }

    setIsComparisonDialogOpen(false);
    setIsComparisonRunning(true);
    setActionError(null);
    setActionSuccess(null);

    try {
      const updatedSnapshot = await runMultimodalComparison(batchId);
      setSnapshot(updatedSnapshot);
      resetTransientEditState();
      setActionSuccess(
        "Multimodal comparison completed. Review groups were refreshed.",
      );
    } catch (comparisonError) {
      const message = errorMessage(
        comparisonError,
        "Multimodal comparison could not be completed.",
      );
      setActionError(message);

      if (
        comparisonError instanceof ReviewBatchError &&
        comparisonError.status === 409 &&
        comparisonError.code === "multimodal_comparison_not_allowed"
      ) {
        setHasLocalReviewActivity(true);
        try {
          const refreshedSnapshot = await loadReviewBatchGroups(batchId);
          setSnapshot(refreshedSnapshot);
          resetTransientEditState();
        } catch {
          // Keep the original comparison error and current snapshot.
        }
      }
    } finally {
      setIsComparisonRunning(false);
      restoreComparisonActionFocus();
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
        <section
          className="comparison-toolbar"
          aria-label="Multimodal comparison"
        >
          <div>
            <strong>Optional multimodal comparison</strong>
            <span>
              {hasApprovedGroup || hasLocalReviewActivity
                ? "Multimodal comparison must run before manual review changes."
                : "Refine eligible uncertain pairs before manual review. Gemini usage may incur costs."}
            </span>
          </div>
          <button
            type="button"
            aria-disabled={isComparisonUnavailable}
            disabled={isBusy}
            onClick={handleOpenComparisonDialog}
            ref={comparisonActionRef}
          >
            {isComparisonRunning
              ? "Running multimodal comparison..."
              : "Run multimodal comparison"}
          </button>
        </section>
      ) : null}

      {isComparisonRunning ? (
        <div className="message uploading review-comparison-progress" role="status">
          Multimodal comparison is running. This may take several minutes.
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
                  disabled={isBusy || selectedEditableImageIds.length === 0}
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
                    disabled={isBusy || editableGroups.length === 0}
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
                          disabled={isBusy || group.groupId === mergeTargetGroupId}
                          onChange={() => toggleMergeSourceGroup(group.groupId)}
                        />
                        <span>{reviewGroupLabel(snapshot.groups, group.groupId)}</span>
                      </label>
                    ))}
                  </div>
                </fieldset>
                <button
                  type="button"
                  disabled={isBusy || !canMergeGroups}
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
              disabled={isBusy || !canApproveBatch}
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

      {suggestionPollingError ? (
        <div className="message error review-action-error" role="alert">
          {suggestionPollingError}
        </div>
      ) : null}

      {actionSuccess ? (
        <div className="message completed review-action-success" role="status">
          {actionSuccess}
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
              isBusy={isBusy}
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
              onRequestRejection={handleOpenRejectionDialog}
              onRestoreRejection={handleRestoreImageRejection}
              onSplit={handleSplitGroup}
              onRestoreDuplicate={handleRestoreDuplicate}
              onSetCover={handleSetCover}
              onUpdateCategory={handleUpdateCategory}
              onToggleImageSelection={toggleImageSelection}
              key={`${group.groupId}:${transientStateVersion}`}
            />
          ))}
        </section>
      )}

      {isComparisonDialogOpen ? (
        <ComparisonConfirmationDialog
          isBusy={isBusy}
          onCancel={handleCancelComparison}
          onConfirm={handleRunComparison}
        />
      ) : null}

      {pendingImageRejection ? (
        <RejectionConfirmationDialog
          imageFilename={pendingImageRejection.originalFilename}
          isBusy={isBusy}
          onCancel={handleCancelRejection}
          onConfirm={handleRejectImage}
        />
      ) : null}
    </main>
  );
}

function ComparisonConfirmationDialog({
  isBusy,
  onCancel,
  onConfirm,
}: {
  isBusy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const cancelButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    cancelButtonRef.current?.focus();

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape" && !isBusy) {
        event.preventDefault();
        onCancel();
        return;
      }
      if (event.key !== "Tab") {
        return;
      }

      const focusableButtons = Array.from(
        dialogRef.current?.querySelectorAll<HTMLButtonElement>(
          "button:not(:disabled)",
        ) ?? [],
      );
      if (focusableButtons.length === 0) {
        event.preventDefault();
        return;
      }

      const firstButton = focusableButtons[0];
      const lastButton = focusableButtons[focusableButtons.length - 1];
      if (event.shiftKey && document.activeElement === firstButton) {
        event.preventDefault();
        lastButton.focus();
      } else if (!event.shiftKey && document.activeElement === lastButton) {
        event.preventDefault();
        firstButton.focus();
      } else if (!dialogRef.current?.contains(document.activeElement)) {
        event.preventDefault();
        firstButton.focus();
      }
    }

    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [isBusy, onCancel]);

  return (
    <div className="comparison-dialog-backdrop">
      <div
        aria-describedby="comparison-dialog-description"
        aria-labelledby="comparison-dialog-title"
        aria-modal="true"
        className="comparison-dialog"
        ref={dialogRef}
        role="dialog"
      >
        <h2 id="comparison-dialog-title">Run multimodal comparison?</h2>
        <p id="comparison-dialog-description">
          This optional step sends eligible uncertain image pairs to Google Gemini
          and may incur usage costs. It may take several minutes and must run before
          manual review changes.
        </p>
        <div className="comparison-dialog-actions">
          <button
            type="button"
            className="secondary-action"
            disabled={isBusy}
            onClick={onCancel}
            ref={cancelButtonRef}
          >
            Cancel
          </button>
          <button type="button" disabled={isBusy} onClick={onConfirm}>
            Run comparison
          </button>
        </div>
      </div>
    </div>
  );
}

function RejectionConfirmationDialog({
  imageFilename,
  isBusy,
  onCancel,
  onConfirm,
}: {
  imageFilename: string;
  isBusy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const cancelButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    cancelButtonRef.current?.focus();

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape" && !isBusy) {
        event.preventDefault();
        onCancel();
        return;
      }
      if (event.key !== "Tab") {
        return;
      }

      const focusableButtons = Array.from(
        dialogRef.current?.querySelectorAll<HTMLButtonElement>(
          "button:not(:disabled)",
        ) ?? [],
      );
      if (focusableButtons.length === 0) {
        event.preventDefault();
        return;
      }

      const firstButton = focusableButtons[0];
      const lastButton = focusableButtons[focusableButtons.length - 1];
      if (event.shiftKey && document.activeElement === firstButton) {
        event.preventDefault();
        lastButton.focus();
      } else if (!event.shiftKey && document.activeElement === lastButton) {
        event.preventDefault();
        firstButton.focus();
      } else if (!dialogRef.current?.contains(document.activeElement)) {
        event.preventDefault();
        firstButton.focus();
      }
    }

    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [isBusy, onCancel]);

  return (
    <div className="comparison-dialog-backdrop">
      <div
        aria-describedby="rejection-dialog-description"
        aria-labelledby="rejection-dialog-title"
        aria-modal="true"
        className="comparison-dialog"
        ref={dialogRef}
        role="dialog"
      >
        <h2 id="rejection-dialog-title">Exclude this image from export?</h2>
        <p className="rejection-dialog-filename">{imageFilename}</p>
        <p id="rejection-dialog-description">
          The image will remain in this review group and can be restored. It will
          not be included in future product export.
        </p>
        <div className="comparison-dialog-actions">
          <button
            type="button"
            className="secondary-action"
            disabled={isBusy}
            onClick={onCancel}
            ref={cancelButtonRef}
          >
            Cancel
          </button>
          <button type="button" disabled={isBusy} onClick={onConfirm}>
            Exclude image
          </button>
        </div>
      </div>
    </div>
  );
}

function ReviewGroupCard({
  group,
  groups,
  groupIndex,
  categories,
  isBusy,
  isReviewEditable,
  moveTargets,
  selectedImageIds,
  onMove,
  onMoveTargetChange,
  onMarkDuplicate,
  onApproveGroup,
  onRequestRejection,
  onRestoreRejection,
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
  isBusy: boolean;
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
  onRequestRejection: (
    groupId: string,
    imageId: string,
    originalFilename: string,
    trigger: HTMLButtonElement,
  ) => void;
  onRestoreRejection: (groupId: string, imageId: string) => void;
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
  const hasApprovedCategory = group.approvedCategorySlug !== null;
  const hasActiveExportImage = group.images.some(
    (image) => !image.isRejected && !image.isDuplicate,
  );
  const canApproveGroup = hasApprovedCategory && hasActiveExportImage;
  const approvalAvailabilityMessage = groupApprovalAvailabilityMessage(
    hasApprovedCategory,
    hasActiveExportImage,
  );

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
        <ReviewField
          label="Suggestion status"
          value={group.categorySuggestionStatus ?? "final"}
        />
        <ReviewCategoryField
          approvedCategorySlug={group.approvedCategorySlug}
          approvedCategorySource={group.approvedCategorySource}
          categories={categories}
          groupLabel={groupLabel}
          isBusy={isBusy}
          isEditable={isGroupEditable}
          onUpdateCategory={(approvedCategoryId) =>
            onUpdateCategory(group.groupId, approvedCategoryId)
          }
          key={`${group.groupId}:${group.approvedCategorySlug ?? ""}:${group.approvedCategorySource ?? ""}`}
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
                isBusy ||
                selectedGroupImageIds.length === 0 ||
                selectedGroupImageIds.length >= group.images.length
              }
              onClick={() => onSplit(group.groupId, selectedGroupImageIds)}
            >
              Split into new group
            </button>
          </div>
          <div className="group-edit-action-block">
            <span>{approvalAvailabilityMessage}</span>
            <button
              type="button"
              disabled={isBusy || !canApproveGroup}
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
            groupImages={group.images}
            isBusy={isBusy}
            isGroupEditable={isGroupEditable}
            isCover={image.imageId === group.coverImageId}
            moveTarget={moveTargets[image.imageId] ?? ""}
            nonDuplicateImages={group.images.filter(
              (candidate) =>
                !candidate.isDuplicate && !candidate.isRejected,
            )}
            selected={selectedImageIds.has(image.imageId)}
            onMove={onMove}
            onMoveTargetChange={onMoveTargetChange}
            onMarkDuplicate={(imageId, duplicateOfImageId) =>
              onMarkDuplicate(group.groupId, imageId, duplicateOfImageId)
            }
            onRequestRejection={(imageId, originalFilename, trigger) =>
              onRequestRejection(
                group.groupId,
                imageId,
                originalFilename,
                trigger,
              )
            }
            onRestoreRejection={(imageId) =>
              onRestoreRejection(group.groupId, imageId)
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
  approvedCategorySource,
  categories,
  groupLabel,
  isBusy,
  isEditable,
  onUpdateCategory,
}: {
  approvedCategorySlug: string | null;
  approvedCategorySource: ReviewGroup["approvedCategorySource"];
  categories: ReviewCategory[];
  groupLabel: string;
  isBusy: boolean;
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
          disabled={!isEditable || isBusy}
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
        {isEditable && approvedCategorySource === "machine_suggestion" ? (
          <span className="category-source-note">
            Prefilled from machine suggestion
          </span>
        ) : null}
        {isEditable ? (
          <div className="category-actions">
            <button
              type="button"
              className="secondary-action"
              aria-label={`Save category for ${groupLabel}`}
              disabled={isBusy || !hasChanged}
              onClick={() => onUpdateCategory(selectedCategoryId || null)}
            >
              Save category
            </button>
            <button
              type="button"
              className="secondary-action"
              aria-label={`Clear category for ${groupLabel}`}
              disabled={isBusy || approvedCategorySlug === null}
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
  groupImages,
  image,
  isBusy,
  isCover,
  isGroupEditable,
  moveTarget,
  nonDuplicateImages,
  selected,
  onMove,
  onMoveTargetChange,
  onMarkDuplicate,
  onRequestRejection,
  onRestoreRejection,
  onRestoreDuplicate,
  onSetCover,
  onToggleImageSelection,
}: {
  editableTargetGroups: ReviewGroup[];
  groups: ReviewGroup[];
  groupImages: ReviewGroupImage[];
  image: ReviewGroupImage;
  isBusy: boolean;
  isCover: boolean;
  isGroupEditable: boolean;
  moveTarget: string;
  nonDuplicateImages: ReviewGroupImage[];
  selected: boolean;
  onMove: (imageId: string) => void;
  onMoveTargetChange: (imageId: string, targetGroupId: string) => void;
  onMarkDuplicate: (imageId: string, duplicateOfImageId: string) => void;
  onRequestRejection: (
    imageId: string,
    originalFilename: string,
    trigger: HTMLButtonElement,
  ) => void;
  onRestoreRejection: (imageId: string) => void;
  onRestoreDuplicate: (imageId: string) => void;
  onSetCover: (imageId: string) => void;
  onToggleImageSelection: (imageId: string) => void;
}) {
  const duplicateMasterOptions = nonDuplicateImages.filter(
    (candidate) => candidate.imageId !== image.imageId,
  );
  const hasActiveDuplicateDependents =
    !image.isDuplicate &&
    groupImages.some(
      (candidate) =>
        candidate.isDuplicate &&
        !candidate.isRejected &&
        candidate.duplicateOfImageId === image.imageId,
    );
  const duplicateMaster =
    image.duplicateOfImageId === null
      ? null
      : groupImages.find(
          (candidate) => candidate.imageId === image.duplicateOfImageId,
        ) ?? null;
  const isRestorationBlocked =
    image.isRejected &&
    image.isDuplicate &&
    duplicateMaster?.isRejected === true;
  const [duplicateMasterId, setDuplicateMasterId] = useState("");

  return (
    <li
      className={`image-card${image.isRejected ? " image-card-rejected" : ""}`}
    >
      {isGroupEditable ? (
        <label className="image-selection">
          <input
            type="checkbox"
            checked={selected}
            disabled={isBusy}
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
          {image.isRejected ? (
            <span className="rejected-label">Excluded from export</span>
          ) : null}
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
          {!image.isDuplicate && !image.isRejected && !isCover ? (
            <button
              type="button"
              className="secondary-action"
              disabled={isBusy}
              onClick={() => onSetCover(image.imageId)}
            >
              Set cover
            </button>
          ) : null}
          <div className="rejection-controls">
            {image.isRejected ? (
              <button
                type="button"
                className="secondary-action"
                aria-label={`Restore ${image.originalFilename} for export`}
                disabled={isBusy || isRestorationBlocked}
                onClick={() => onRestoreRejection(image.imageId)}
              >
                Restore for export
              </button>
            ) : (
              <button
                type="button"
                className="secondary-action"
                aria-label={`Reject ${image.originalFilename} from export`}
                disabled={isBusy || hasActiveDuplicateDependents}
                onClick={(event) =>
                  onRequestRejection(
                    image.imageId,
                    image.originalFilename,
                    event.currentTarget,
                  )
                }
              >
                Reject from export
              </button>
            )}
            {hasActiveDuplicateDependents ? (
              <span className="image-action-note">
                Restore or reassign active duplicates before rejecting this
                duplicate master.
              </span>
            ) : null}
            {isRestorationBlocked ? (
              <span className="image-action-note">
                Restore or replace the duplicate master before restoring this
                image for export.
              </span>
            ) : null}
          </div>
          {image.isDuplicate ? (
            <button
              type="button"
              className="secondary-action"
              disabled={isBusy}
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
                  disabled={isBusy || duplicateMasterOptions.length === 0}
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
                disabled={isBusy || !duplicateMasterId}
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
                disabled={isBusy || editableTargetGroups.length === 0}
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
              disabled={isBusy || !moveTarget}
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

function groupApprovalAvailabilityMessage(
  hasApprovedCategory: boolean,
  hasActiveExportImage: boolean,
): string {
  if (hasApprovedCategory && hasActiveExportImage) {
    return "This group is ready for approval.";
  }
  if (!hasApprovedCategory && !hasActiveExportImage) {
    return (
      "Select an approved category and restore at least one non-duplicate " +
      "image before approving this group."
    );
  }
  if (!hasApprovedCategory) {
    return "Select an approved category before approving this group.";
  }
  return "Restore at least one non-duplicate image before approving this group.";
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
