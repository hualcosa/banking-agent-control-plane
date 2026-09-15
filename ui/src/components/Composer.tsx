/**
 * Where the reader types.
 *
 * Enter sends and Shift+Enter opens a line, which is the convention every
 * messaging surface has trained into people's hands. While a turn is in flight
 * the field is disabled and the button *says* so rather than only looking so: a
 * greyed control with an unchanged label is indistinguishable from a click that
 * did not register, and this demo is watched by people who will click twice.
 *
 * The field grows with its content up to a ceiling, then scrolls. A fixed
 * two-row box hides the end of anything longer, which is where a typo is.
 */

import { useEffect, useRef, useState } from "react";

/** Rows, in pixels, before the field stops growing and starts scrolling. */
const MAX_HEIGHT = 200;

export function Composer({
  busy,
  agent,
  suggestedText,
  onSuggestionApplied,
  onSend,
}: {
  busy: boolean;
  agent: string | null;
  suggestedText: string | null;
  onSuggestionApplied: () => void;
  onSend: (message: string) => void;
}) {
  const [text, setText] = useState("");
  const field = useRef<HTMLTextAreaElement | null>(null);

  // Return focus the moment the turn lands, so the next question can be typed
  // without reaching for the mouse.
  useEffect(() => {
    if (!busy) field.current?.focus();
  }, [busy]);

  useEffect(() => {
    if (suggestedText === null) return;
    setText(suggestedText);
    onSuggestionApplied();
    window.requestAnimationFrame(() => {
      field.current?.focus();
      field.current?.setSelectionRange(suggestedText.length, suggestedText.length);
    });
  }, [suggestedText, onSuggestionApplied]);

  // Reset before measuring: scrollHeight only shrinks if the element is
  // allowed to, so without this the field grows and never comes back.
  useEffect(() => {
    const node = field.current;
    if (!node) return;
    node.style.height = "auto";
    node.style.height = `${Math.min(node.scrollHeight, MAX_HEIGHT)}px`;
  }, [text]);

  function submit() {
    const trimmed = text.trim();
    if (!trimmed || busy) return;
    onSend(trimmed);
    setText("");
  }

  return (
    <form
      className="composer"
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <div className="composer__pill">
        <label className="sr-only" htmlFor="composer-input">
          Message
        </label>
        <textarea
          id="composer-input"
          ref={field}
          className="composer__input"
          rows={1}
          value={text}
          disabled={busy}
          placeholder={
            agent === "banking"
              ? "Ask the banking agent…"
              : "Ask about TRAIL…"
          }
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              submit();
            }
          }}
        />
        <button
          type="submit"
          className="composer__send"
          disabled={busy || !text.trim()}
          aria-label={busy ? "Processing" : "Send"}
        >
          {busy ? (
            <span className="composer__spinner" aria-hidden="true" />
          ) : (
            <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"
              fill="none" stroke="currentColor" strokeWidth="2"
              strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 19V5M5 12l7-7 7 7" />
            </svg>
          )}
        </button>
      </div>
    </form>
  );
}
