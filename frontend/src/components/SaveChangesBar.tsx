/**
 * The one save affordance for a whole matrix or table of edited cells --
 * shared by staffing and serving limits (Task 72 continuation) so both
 * screens save, report progress and fail the same way instead of drifting
 * into two conventions for the same action.
 *
 * Always rendered once a table has loaded, even with nothing dirty: a
 * control that appears only sometimes is a control a person has to relearn
 * to look for, and "0 unsaved changes" is itself useful confirmation that an
 * edit actually registered.
 */

import type { ReactNode } from "react";

export function SaveChangesBar({
  dirtyCount,
  isSaving,
  canSave,
  onSave,
  onDiscard,
  error,
}: {
  dirtyCount: number;
  isSaving: boolean;
  /** False while any dirty cell is invalid -- Save stays disabled rather
   *  than silently skipping the bad cell. */
  canSave: boolean;
  onSave: () => void;
  onDiscard: () => void;
  error?: ReactNode;
}) {
  return (
    <div className="matrix-toolbar" role="status">
      <span className="matrix-toolbar__status">
        {dirtyCount === 0
          ? "No unsaved changes"
          : `${dirtyCount} unsaved change${dirtyCount === 1 ? "" : "s"}`}
      </span>
      <button
        className="button button--primary"
        type="button"
        onClick={onSave}
        disabled={isSaving || dirtyCount === 0 || !canSave}
      >
        {isSaving ? "Saving…" : "Save changes"}
      </button>
      <button
        className="button"
        type="button"
        onClick={onDiscard}
        disabled={isSaving || dirtyCount === 0}
      >
        Discard changes
      </button>
      {!canSave && dirtyCount > 0 && (
        <span className="small muted">Fix the highlighted cell before saving.</span>
      )}
      {error}
    </div>
  );
}
