/**
 * The wire contract, mirrored by hand from the Python.
 *
 * There is no code generation here and that is deliberate: the surface is four
 * SSE frame names and five payloads, and a generator plus its build step would
 * be more machinery than the thing it generates.
 *
 * The important property of this file is what it does **not** contain. There is
 * no closed list of stage names and no dictionary of Portuguese labels. Both
 * existed in the version of this file written for the previous agent, and both
 * are the reason that UI could not render any other agent — `runtime/events.py`
 * names that dictionary as the bug it fixed by putting `label` on the wire.
 *
 * What is closed is `kind` and `status`: five and four values, each of which the
 * client renders differently. What is open is every word.
 */

// --------------------------------------------------------------------------
// Threads
// --------------------------------------------------------------------------

/** `POST /threads` — a new conversation, and the dial it was opened under. */
export interface StartThreadResponse {
  thread_id: string;
  agent: string;
  greeting: string;
  /** `both` | `input` | `output` | `none`, echoed so the client can show it. */
  guardrails: string;
}

/** One row of the sidebar. */
export interface ThreadSummary {
  thread_id: string;
  title: string;
  turns: number;
  created_at: string;
  updated_at: string;
}

/**
 * `GET /threads`.
 *
 * `durable` is load-bearing. With `TRAIL_CHECKPOINTER=memory` this list is
 * empty after every restart, and a sidebar that cannot tell that apart from
 * "you have had no conversations" shows a bug where there is a setting.
 */
export interface ThreadListResponse {
  threads: ThreadSummary[];
  durable: boolean;
}

/** `GET /threads/{id}` — a conversation, reopened. Tool traffic is excluded. */
export interface ThreadResponse {
  thread_id: string;
  messages: { role: "user" | "agent"; text: string }[];
}

// --------------------------------------------------------------------------
// Stage frames
// --------------------------------------------------------------------------

/**
 * What kind of step ran. Closed, because the client renders each differently —
 * a guard is a shield, a tool is a wrench, a model call carries tokens.
 */
export type StageKind = "guard_in" | "model" | "tool" | "guard_out" | "io";

/**
 * How it went, and `blocked` is the one that matters.
 *
 * Without it a guardrail that fired and a guardrail that passed look identical,
 * and which of those happened is the single thing this interface exists to
 * show. `skip` is nearly as important: a gate switched off still reports
 * itself, struck through, because an absence that renders as nothing is
 * indistinguishable from a success.
 */
export type StageStatus = "start" | "done" | "skip" | "blocked";

/**
 * One step of the pipeline.
 *
 * `name` is an identifier (`guard_in`, `model`, `tool:search_docs`) and is
 * **not** from a fixed set — a tool contributes its own name, so the set is as
 * open as the agent's tool list. `label` is the human word, and it arrives on
 * the wire rather than being looked up here.
 */
export interface StageEvent {
  name: string;
  kind: StageKind;
  label: string;
  status: StageStatus;
  /**
   * Nanoseconds, not milliseconds, and the unit is the point. A gate runs in
   * microseconds and a model call in seconds; milliseconds cannot hold both,
   * and truncating to one rendered every guardrail as `0 ms`.
   */
  ns: number | null;
  detail: StageDetail | null;
}

/**
 * The open half of a stage frame.
 *
 * Deliberately not a union of exact shapes: each kind of middleware puts what
 * it has here, and closing it would mean a schema change in two languages
 * before a new kind of step could say anything about itself.
 */
export interface StageDetail {
  /** On a `done` model call. `cost_usd` is null for a model with no known rate. */
  input_tokens?: number;
  output_tokens?: number;
  cost_usd?: number | null;
  /** On a `blocked` guard. */
  violations?: Violation[];
  /** On a `blocked` model or tool call. */
  error?: string;
  message?: string;
}

/**
 * One failed check.
 *
 * `rule` names the policy rather than restating the check, so a violation on
 * screen explains itself instead of sending the reader to find the citation.
 */
export interface Violation {
  check: string;
  rule: string;
  detail: string;
  evidence: string;
}

// --------------------------------------------------------------------------
// The other three frames
// --------------------------------------------------------------------------

/**
 * The answer, and the turn's own wall time in nanoseconds.
 *
 * Null for the greeting, which cost no model call — a measurement never taken
 * is not a measurement of zero.
 *
 * This is not the sum of the rail's cells: the graph spends time between them.
 * Showing both is how that gap becomes visible instead of something a reader
 * has to compute and then doubt.
 */
export interface TurnEvent {
  thread_id: string;
  text: string;
  ns: number | null;
}

/** Always last, even after an error — a failed turn is the one you most want. */
export interface TraceEvent {
  trace_id: string | null;
  trace_url: string | null;
}

/** The status the buffered endpoint would have returned. */
export interface ErrorEvent {
  status: number;
  detail: string;
}

/** Everything that crosses the SSE boundary, discriminated by `event`. */
export type TurnStreamEvent =
  | { event: "stage"; data: StageEvent }
  | { event: "turn"; data: TurnEvent }
  | { event: "trace"; data: TraceEvent }
  | { event: "error"; data: ErrorEvent };
