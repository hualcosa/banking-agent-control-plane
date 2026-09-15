/**
 * One row of the transcript, and the asymmetry is the argument.
 *
 * The user gets a bubble; the agent does not. That is what ChatGPT and Claude
 * do, and copying it here is not imitation — an unbubbled agent reads as *the
 * system's output surface* rather than as *another person's turn*, which is the
 * honest description of what it is. A bubble on both sides asserts two
 * speakers.
 *
 * A blocked turn is the third treatment, and it gets the most. The refusal is
 * the moment this whole interface justifies itself: a gate fired, the reader
 * should be able to see *which* gate and *why* without opening anything. So the
 * violations render in full — check, rule, and the offending evidence — rather
 * than collapsing into "blocked".
 */

import { useEffect, useState } from "react";

import { useReducedMotion } from "../hooks";
import type { Entry } from "../state";
import { Markdown } from "./Markdown";
import { Rail } from "./Rail";

/** Milliseconds per character of the reveal. */
const CHAR_MS = 8;

/**
 * The typewriter, and it is honest about being cosmetic.
 *
 * The service sends the answer whole — there is no token stream to follow, and
 * `runtime/turns.py` already requests the `messages` channel so that adding one
 * later is a client change rather than a contract change. Until then this is an
 * animation and nothing more.
 *
 * The animated copy is `aria-hidden` and the full text sits in an `sr-only`
 * span, so a screen reader is read the answer once, complete, instead of one
 * character at a time. Clicking finishes it early, because a reader who wants
 * the text now should not have to wait for a decoration.
 */
function Revealed({ text, animate }: { text: string; animate: boolean }) {
  const [shown, setShown] = useState(animate ? 0 : text.length);

  useEffect(() => {
    if (!animate) {
      setShown(text.length);
      return;
    }
    setShown(0);
    const timer = window.setInterval(() => {
      setShown((n) => {
        if (n >= text.length) {
          window.clearInterval(timer);
          return n;
        }
        // A whole word per tick past the first few, so a long answer does not
        // take a minute to appear.
        return Math.min(text.length, n + 3);
      });
    }, CHAR_MS);
    return () => window.clearInterval(timer);
  }, [text, animate]);

  const done = shown >= text.length;
  if (done) return <Markdown text={text} />;

  return (
    <div onClick={() => setShown(text.length)}>
      <span aria-hidden="true">
        <Markdown text={text.slice(0, shown)} />
      </span>
      <span className="sr-only">{text}</span>
    </div>
  );
}

export function Message({ entry }: { entry: Entry }) {
  const reduced = useReducedMotion();

  if (entry.kind === "user") {
    return (
      <article className="msg msg--user">
        <div className="msg__bubble">{entry.text}</div>
      </article>
    );
  }

  if (entry.kind === "error") {
    return (
      <article className="msg msg--error" role="alert">
        <p className="msg__who">Turn failed</p>
        <p className="msg__text">{failureGuidance(entry.status)}</p>
        <p className="mono msg__detail">
          {entry.status} · {entry.detail}
        </p>
        {entry.traceUrl ? (
          <p className="rail__totals mono">
            <a
              className="rail__trace"
              href={entry.traceUrl}
              target="_blank"
              rel="noopener"
            >
              view trace ↗
            </a>
          </p>
        ) : null}
      </article>
    );
  }

  return (
    <article className={`msg msg--agent${entry.blocked ? " msg--blocked" : ""}`}>
      <p className="msg__who">
        <span aria-hidden="true" className="msg__mark" />
        {entry.blocked ? "Refused" : "Agent"}
      </p>
      <div className="msg__text">
        <Revealed text={entry.text} animate={entry.fresh && !reduced} />
      </div>
      {entry.violations.length > 0 ? (
        <ul className="violations">
          {entry.violations.map((violation, index) => (
            <li className="violations__item mono" key={index}>
              <span className="violations__check">{violation.check}</span>
              <span className="violations__rule">{violation.rule}</span>
              <span className="violations__detail">{violation.detail}</span>
              <span className="violations__evidence">“{violation.evidence}”</span>
            </li>
          ))}
        </ul>
      ) : null}
      <Rail stages={entry.rail} metrics={entry.metrics} />
    </article>
  );
}

/**
 * What the reader should do about this failure.
 *
 * A status code is a fact about the response; this is a fact about what to try
 * next, which is the only one of the two anybody wants at the moment it
 * appears.
 */
function failureGuidance(status: number): string {
  if (status === 502) {
    return "The model provider did not respond. The conversation is still open — send the message again.";
  }
  if (status === 422) {
    return "The service rejected the message. Rephrase it and try again.";
  }
  if (status >= 500) {
    return "The service failed while processing the turn. The conversation is still open.";
  }
  return "The service rejected the request.";
}
