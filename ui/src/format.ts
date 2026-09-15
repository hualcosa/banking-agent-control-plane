/**
 * Formatters for the machine values on screen.
 *
 * What is *not* here is the point. The previous version of this file held a
 * dictionary mapping stage identifiers to Portuguese words, and
 * `runtime/events.py` names that dictionary as the reason `label` now travels
 * on the wire: a lookup table in the frontend is how a UI ends up knowing one
 * agent's vocabulary and no other's. Every human word this interface renders
 * now arrives from the service.
 *
 * These four survive because they format *numbers*, and a number's rendering is
 * the client's business.
 */

/** Cost in US dollars, or a dash when the model has no published rate.
 *
 * The dash is the whole function. `US$ 0.0000` is a claim that a call was free;
 * `—` is a claim that nobody knows, and only one of those is true for an
 * unpriced model.
 */
export function formatUsd(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return `US$ ${value.toFixed(4)}`;
}

/** Token counts, thinned above a thousand so a rail row stays one line. */
export function formatTokens(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (value < 1000) return String(value);
  return `${(value / 1000).toFixed(1)}k`;
}

/**
 * A nanosecond count, at the scale a person reads it.
 *
 * The steps on one rail span four orders of magnitude — a regex gate runs in
 * microseconds, a model call in seconds — so no single unit shows both. This
 * used to be milliseconds, and it rendered every guardrail in the system as
 * `0 ms`: true, useless, and easily read as "did not run", which is the exact
 * ambiguity the rail exists to remove.
 *
 * Three significant figures at each scale: the most anyone acts on, and the
 * least that still separates a 1.6 µs check from a 9.2 µs one.
 */
export function formatDuration(value: number | null | undefined): string {
  if (value === null || value === undefined) return "";
  if (value < 1_000) return `${value} ns`;
  if (value < 1_000_000) return `${(value / 1_000).toFixed(1)} µs`;
  if (value < 1_000_000_000) return `${(value / 1_000_000).toFixed(1)} ms`;
  return `${(value / 1_000_000_000).toFixed(2)} s`;
}

/**
 * A short reference for a thread, for a header that has to fit.
 *
 * The first segment of the UUID, which is what the CLI prints too — the two
 * clients should name the same conversation the same way, or comparing a
 * terminal session against a browser tab means translating.
 */
export function threadRef(threadId: string | null): string {
  if (!threadId) return "—";
  return threadId.slice(0, 8);
}
