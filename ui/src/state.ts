/**
 * The shapes the transcript is made of, and the reducers that fold a stream of
 * stage frames into them.
 *
 * No I/O here and no React. Everything is a pure function of the frames that
 * arrived, which is what lets the interesting logic — cost accumulation, the
 * rail, whether a turn was blocked — be reasoned about without a browser.
 *
 * The rail is the part that changed shape rather than merely names. It used to
 * be a fixed record of six known stages. The contract now emits `tool:<name>`
 * once per tool call and `model` once per tool round, so the count is unbounded
 * and the names are the agent's to choose. It is a list in arrival order, and
 * arrival order is pipeline order — the backend emits a switched-off gate's
 * skip from the hook where that gate would have run, precisely so no client
 * has to sort.
 */

import type { StageEvent, TurnEvent, Violation } from "./types";

// --------------------------------------------------------------------------
// Transcript entries
// --------------------------------------------------------------------------

/**
 * What one turn cost, and how to reach its trace.
 *
 * Every field is nullable on purpose. The greeting has no latency because no
 * model ran; a model with no published rate has an unknown cost. Rendering
 * `0 ms` or `US$ 0.00` in those cases would be a measurement claim about
 * something never measured — a confident zero is the most expensive kind of
 * wrong.
 */
export interface TurnMetrics {
  /** The turn's wall time in nanoseconds — see `TurnEvent`. */
  ns: number | null;
  tokensIn: number | null;
  tokensOut: number | null;
  costUsd: number | null;
  traceUrl: string | null;
}

export const EMPTY_METRICS: TurnMetrics = {
  ns: null,
  tokensIn: null,
  tokensOut: null,
  costUsd: null,
  traceUrl: null,
};

export type Entry =
  | {
      kind: "agent";
      id: string;
      text: string;
      /** Arrival-ordered, `start` frames already dropped. */
      rail: StageEvent[];
      /** Gathered from every `blocked` guard frame in the turn. */
      violations: Violation[];
      /** Whether any gate refused this turn. Drives the whole visual treatment. */
      blocked: boolean;
      metrics: TurnMetrics;
      /** Freshly arrived, so the reveal animation runs once and only once. */
      fresh: boolean;
    }
  | { kind: "user"; id: string; text: string }
  | {
      kind: "error";
      id: string;
      status: number;
      detail: string;
      traceUrl: string | null;
    };

// --------------------------------------------------------------------------
// Folding a turn
// --------------------------------------------------------------------------

/** What a turn's frames add up to, accumulated as they arrive. */
export interface TurnAccumulator {
  rail: StageEvent[];
  violations: Violation[];
  blocked: boolean;
  tokensIn: number;
  tokensOut: number;
  /** Null until a priced model call reports one. See `TurnMetrics`. */
  costUsd: number | null;
  sawUsage: boolean;
}

export const EMPTY_ACCUMULATOR: TurnAccumulator = {
  rail: [],
  violations: [],
  blocked: false,
  tokensIn: 0,
  tokensOut: 0,
  costUsd: null,
  sawUsage: false,
};

/**
 * Fold one stage frame into the accumulator.
 *
 * Keyed on `kind` rather than on `name`, which is what keeps this function
 * agnostic: `model` is a model call whatever the agent calls it, and any
 * `tool:*` is a tool without this file knowing the tool list.
 *
 * The null handling around `cost_usd` is not defensive padding. The backend
 * reports `null` for a model it has no rate for, and `(costUsd ?? 0) + null`
 * would silently produce a number — turning "unknown" into "free" at exactly
 * the moment someone is reading the number to decide something.
 */
export function accumulateStage(
  acc: TurnAccumulator,
  event: StageEvent,
): TurnAccumulator {
  if (event.status === "start") return acc;

  const next: TurnAccumulator = { ...acc, rail: [...acc.rail, event] };
  const detail = event.detail;

  if (event.kind === "model" && event.status === "done" && detail) {
    next.tokensIn += detail.input_tokens ?? 0;
    next.tokensOut += detail.output_tokens ?? 0;
    if (typeof detail.cost_usd === "number") {
      next.costUsd = (next.costUsd ?? 0) + detail.cost_usd;
    }
    next.sawUsage = true;
  }

  if (event.status === "blocked") {
    next.blocked = true;
    if (detail?.violations?.length) {
      next.violations = [...next.violations, ...detail.violations];
    }
  }

  return next;
}

/** The accumulator plus the turn's own latency and trace, ready to render. */
export function metricsFrom(
  acc: TurnAccumulator,
  turn: TurnEvent | null,
  traceUrl: string | null,
): TurnMetrics {
  return {
    ns: turn?.ns ?? null,
    tokensIn: acc.sawUsage ? acc.tokensIn : null,
    tokensOut: acc.sawUsage ? acc.tokensOut : null,
    costUsd: acc.costUsd,
    traceUrl,
  };
}

// --------------------------------------------------------------------------
// Rendering helpers
// --------------------------------------------------------------------------

/**
 * The order a rail is read in, when something has to sort it.
 *
 * Nothing does today — frames arrive in pipeline order. This exists for the
 * transcript replayed from `GET /threads/{id}`, which has messages and no
 * frames at all, so that a reopened conversation and a live one agree on what
 * "no rail" looks like rather than one of them inventing one.
 */
export const KIND_ORDER: Record<string, number> = {
  guard_in: 0,
  model: 1,
  tool: 2,
  guard_out: 3,
  io: 4,
};

let counter = 0;

/** A stable React key. Monotonic, so a re-render never reuses one. */
export function nextId(): string {
  counter += 1;
  return `e${counter}`;
}
