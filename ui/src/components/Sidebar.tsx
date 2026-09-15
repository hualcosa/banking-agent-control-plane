/**
 * The conversation list.
 *
 * Two things here are decisions rather than layout.
 *
 * **The empty state distinguishes "nothing yet" from "nothing kept".** With
 * `TRAIL_CHECKPOINTER=memory` this list is empty after every restart, and a
 * sidebar that rendered the same blank panel in both cases would show a bug
 * where there is a setting. The service tells us which it is; the panel says so.
 *
 * **Deleting removes a conversation from the list, not from storage.** The
 * checkpoint stays. That is what "delete" means to someone tidying a sidebar,
 * and promising more than that in a button would be promising something this
 * interface does not do.
 */

import type { ThreadSummary } from "../types";

export function Sidebar({
  threads,
  durable,
  activeId,
  onSelect,
  onNew,
  onDelete,
  onClose,
}: {
  threads: ThreadSummary[];
  durable: boolean;
  activeId: string | null;
  onSelect: (threadId: string) => void;
  onNew: () => void;
  onDelete: (threadId: string) => void;
  onClose?: () => void;
}) {
  return (
    <nav className="sidebar" aria-label="Conversations">
      <div className="sidebar__actions">
        <button type="button" className="sidebar__new" onClick={onNew}>
          <span aria-hidden="true">+</span> New conversation
        </button>
        {onClose ? (
          <button
            type="button"
            className="sidebar__close iconbtn"
            data-sidebar-close
            aria-label="Close conversations"
            onClick={onClose}
          >
            <span aria-hidden="true">×</span>
          </button>
        ) : null}
      </div>

      <ul className="sidebar__list">
        {threads.map((thread) => (
          <li className="sidebar__item" key={thread.thread_id}>
            <button
              type="button"
              className={`sidebar__link${
                thread.thread_id === activeId ? " is-active" : ""
              }`}
              aria-current={thread.thread_id === activeId ? "true" : undefined}
              onClick={() => onSelect(thread.thread_id)}
            >
              {/* A thread opened and never answered has no title yet. Saying so
                  beats an empty row that looks like a rendering failure. */}
              {thread.title || "conversation without messages"}
            </button>
            <button
              type="button"
              className="sidebar__forget"
              aria-label={`Remove ${thread.title || "conversation"} from the list`}
              onClick={() => onDelete(thread.thread_id)}
            >
              <span aria-hidden="true">×</span>
            </button>
          </li>
        ))}
      </ul>

      {threads.length === 0 ? (
        <p className="sidebar__empty">
          {durable
            ? "No conversations yet."
            : "No conversations. With TRAIL_CHECKPOINTER=memory, history does not survive a restart."}
        </p>
      ) : null}

      {!durable && threads.length > 0 ? (
        <p className="sidebar__note">
          In-memory state — this history disappears on the next restart.
        </p>
      ) : null}
    </nav>
  );
}
