/**
 * The pipeline rail: what the turn actually did, under the answer it produced.
 *
 * This is the component the product exists for. Everything else on screen is a
 * chat interface; this is the part that says which gates ran, which were
 * switched off, how long the model took and what it cost — the claim being that
 * an agent's behaviour should be arguable, not merely plausible.
 *
 * Three rules, each of which has been got wrong before:
 *
 * **A skipped stage renders struck through, never hidden.** A guardrail that
 * was switched off and a guardrail that ran and passed must not look alike, and
 * a hidden cell reads as neither — it reads as nothing at all.
 *
 * **A blocked stage gets its own glyph, not only its own colour.** Colour is
 * lost in a screenshot, in a printout, and to a reader who cannot distinguish
 * red from grey.
 *
 * **The order is arrival order.** No sorting. The service emits a switched-off
 * gate's skip from the hook where that gate would have run, precisely so that
 * no client has to reconstruct the sequence — and any sort able to place a skip
 * is also able to scramble the real interleaving of model and tool calls, which
 * is the ordering a reader is reading for.
 */

import { formatDuration, formatTokens, formatUsd } from "../format";
import type { StageEvent } from "../types";
import type { TurnMetrics } from "../state";

const MARK: Record<string, string> = {
  done: "▪",
  skip: "▫",
  blocked: "✗",
  start: "▪",
};

function Cell({ stage }: { stage: StageEvent }) {
  return (
    <span
      className={`rail__cell rail__cell--${stage.status}`}
      data-kind={stage.kind}
    >
      <span aria-hidden="true" className="rail__mark">
        {MARK[stage.status] ?? "·"}
      </span>
      <span className="rail__label">{stage.label}</span>
      <span className="rail__ms mono">
        {stage.status === "skip"
          ? "skipped"
          : stage.status === "blocked"
            ? "blocked"
            : formatDuration(stage.ns)}
      </span>
    </span>
  );
}

export function Rail({
  stages,
  metrics,
}: {
  stages: StageEvent[];
  metrics: TurnMetrics | null;
}) {
  // A metrics object of all nulls is not a measurement — it is the greeting,
  // or a conversation reopened from storage where the frames were never kept.
  // Rendering the rail's rule with nothing under it would draw a line that
  // claims a pipeline ran.
  const hasMetrics =
    metrics !== null &&
    (metrics.tokensIn !== null || metrics.ns !== null || metrics.traceUrl !== null);
  if (!stages.length && !hasMetrics) return null;

  const totals: string[] = [];
  if (metrics) {
    if (metrics.tokensIn !== null) totals.push(`${formatTokens(metrics.tokensIn)} in`);
    if (metrics.tokensOut !== null)
      totals.push(`${formatTokens(metrics.tokensOut)} out`);
    // Shown even when null, rendered as a dash: an unpriced model has an
    // unknown cost, and omitting the field entirely would let the reader
    // assume the turn was free.
    if (metrics.tokensIn !== null) totals.push(formatUsd(metrics.costUsd));
    // The turn's own wall time, last, and deliberately not the sum of the
    // cells above it. The graph spends time between steps; showing both is how
    // that gap becomes visible rather than something a reader computes and
    // then doubts.
    if (metrics.ns !== null) totals.push(`total ${formatDuration(metrics.ns)}`);
  }

  return (
    <div className="rail" aria-label="Turn stages">
      <div className="rail__cells">
        {stages.map((stage, index) => (
          <Cell key={`${stage.name}-${index}`} stage={stage} />
        ))}
      </div>
      {(totals.length > 0 || metrics?.traceUrl) && (
        <p className="rail__totals mono">
          {totals.join(" · ")}
          {totals.length > 0 && metrics?.traceUrl ? (
            <span aria-hidden="true"> · </span>
          ) : null}
          {metrics?.traceUrl ? (
            <a
              className="rail__trace"
              href={metrics.traceUrl}
              target="_blank"
              rel="noopener"
            >
              view trace ↗
            </a>
          ) : null}
        </p>
      )}
    </div>
  );
}
