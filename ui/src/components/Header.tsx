/**
 * The masthead: what is mounted, under what settings, and the theme control.
 *
 * Every field here is a fact the service reported, and that is a rule rather
 * than a coincidence. An earlier version of this header displayed a protocol
 * version the service had no endpoint for, which meant a number on screen that
 * nothing could contradict. If the service does not say it, it does not appear.
 *
 * `guardrails` earns its place: it is the dial this whole scaffold is about, and
 * a screenshot of a conversation is worth much less if you cannot tell whether
 * the gates were on.
 */

import { threadRef } from "../format";

export function Header({
  agent,
  guardrails,
  threadId,
  theme,
  onTheme,
  onToggleSidebar,
  showSidebarToggle,
  sidebarOpen,
}: {
  agent: string | null;
  guardrails: string | null;
  threadId: string | null;
  theme: string;
  onTheme: () => void;
  onToggleSidebar: () => void;
  showSidebarToggle: boolean;
  sidebarOpen: boolean;
}) {
  return (
    <header className="topbar">
      {showSidebarToggle ? (
        <button
          id="sidebar-toggle"
          type="button"
          className="iconbtn"
          aria-expanded={sidebarOpen}
          aria-controls="sidebar"
          aria-label={sidebarOpen ? "Close conversations" : "Open conversations"}
          onClick={onToggleSidebar}
        >
          <PanelIcon />
        </button>
      ) : null}

      <h1 className="topbar__title">
        {agent === "banking" ? (
          <>
            <span>Banking Control Plane</span>
            <small>powered by TRAIL</small>
          </>
        ) : (
          <span>TRAIL</span>
        )}
      </h1>

      <p className="topbar__meta">
        {agent ? <span className="topbar__field mono">{agent}</span> : null}
        {guardrails ? (
          <span
            className={`topbar__field topbar__field--dial mono${
              guardrails === "none" ? " is-off" : ""
            }`}
          >
            guardrails {guardrails}
          </span>
        ) : null}
        {threadId ? (
          <span className="topbar__field mono">{threadRef(threadId)}</span>
        ) : null}
      </p>

      <button
        type="button"
        className="iconbtn"
        onClick={onTheme}
        // The label names the current state and the title names the next, so a
        // screen reader is told what is true and a pointer user what will
        // happen.
        aria-label={`Theme: ${THEME_LABEL[theme] ?? "system"}`}
        title={`Switch to ${THEME_LABEL[NEXT_THEME[theme] ?? ""] ?? "system"}`}
      >
        {theme === "dark" ? <MoonIcon /> : theme === "light" ? <SunIcon /> : <AutoIcon />}
      </button>
    </header>
  );
}

/**
 * Three states, not two.
 *
 * A binary toggle traps a reader who clicked once: there is no way back to
 * following the operating system without clearing site data.
 */
export const NEXT_THEME: Record<string, string> = {
  "": "light",
  light: "dark",
  dark: "",
};

const THEME_LABEL: Record<string, string> = {
  "": "system",
  light: "light",
  dark: "dark",
};

/* Three inline paths rather than an icon dependency, and rather than the
   Unicode ☀ ☾ ▤ — those render as colour emoji on some platforms and as
   tofu on others, which is a lottery for a control the reader has to
   recognise. `currentColor` keeps them in step with the theme they switch. */

function SunIcon() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"
      fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"
      fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round"
      strokeLinejoin="round">
      <path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5Z" />
    </svg>
  );
}

function AutoIcon() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"
      fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
      <circle cx="12" cy="12" r="8" />
      <path d="M12 4a8 8 0 0 1 0 16Z" fill="currentColor" stroke="none" />
    </svg>
  );
}

function PanelIcon() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"
      fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round">
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="M9 4v16" />
    </svg>
  );
}
