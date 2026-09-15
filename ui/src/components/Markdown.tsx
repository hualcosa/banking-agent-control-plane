/**
 * Just enough markdown for what this agent actually emits.
 *
 * Sixty lines instead of a dependency, and the reasoning is narrow rather than
 * ideological. The project has two runtime dependencies; the content comes from
 * its own backend rather than from a third party, so this is not a sanitisation
 * problem; and the subset in use is small and observable — fenced blocks,
 * inline code, bold, and lists.
 *
 * **When to throw this away:** the day a response contains a table, a link, or
 * nested markdown. At that point this stops being a small correct thing and
 * starts being a bad parser, and the answer is `marked` plus `dompurify` with
 * this file deleted entire.
 *
 * Nothing here builds HTML from a string. Every branch returns React elements,
 * so there is no `dangerouslySetInnerHTML` and no injection surface to reason
 * about — which is also why the missing sanitiser is not a gap.
 */

import type { ReactNode } from "react";

/** ```lang … ``` — captured with its body, across lines. */
const FENCE = /^```([\w-]*)\n([\s\S]*?)```$/;

/** `code`, **bold**. Split on the delimiters so the matches survive. */
const INLINE = /(`[^`]+`|\*\*[^*]+\*\*)/g;

function renderInline(text: string, keyPrefix: string): ReactNode[] {
  return text.split(INLINE).map((part, index) => {
    const key = `${keyPrefix}-${index}`;
    if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
      return <code key={key}>{part.slice(1, -1)}</code>;
    }
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
      return <strong key={key}>{part.slice(2, -2)}</strong>;
    }
    return part;
  });
}

/**
 * Split on blank lines, but never inside a fence.
 *
 * A fenced ASCII diagram is full of blank lines, and splitting on them would
 * shatter the drawing into a dozen paragraphs — which is exactly the failure
 * this component exists to prevent.
 */
function blocks(text: string): string[] {
  const out: string[] = [];
  let buffer: string[] = [];
  let fenced = false;

  for (const line of text.split("\n")) {
    if (line.trimStart().startsWith("```")) {
      buffer.push(line);
      if (fenced) {
        out.push(buffer.join("\n"));
        buffer = [];
      }
      fenced = !fenced;
      continue;
    }
    if (!fenced && !line.trim()) {
      if (buffer.length) out.push(buffer.join("\n"));
      buffer = [];
      continue;
    }
    buffer.push(line);
  }
  if (buffer.length) out.push(buffer.join("\n"));
  return out;
}

export function Markdown({ text }: { text: string }) {
  return (
    <div className="md">
      {blocks(text).map((block, index) => {
        const key = `b${index}`;
        const fence = FENCE.exec(block.trim());
        if (fence) {
          // Its own scroll container, and this is not optional: the guide
          // answers architecture questions with 80-column ASCII diagrams, and
          // without this the whole page scrolls sideways to show one of them.
          return (
            <pre className="md__code mono" key={key}>
              <code>{fence[2]?.replace(/\n$/, "")}</code>
            </pre>
          );
        }

        const lines = block.split("\n");
        if (lines.every((line) => /^\s*[-*]\s+/.test(line))) {
          return (
            <ul className="md__list" key={key}>
              {lines.map((line, item) => (
                <li key={`${key}-${item}`}>
                  {renderInline(line.replace(/^\s*[-*]\s+/, ""), `${key}-${item}`)}
                </li>
              ))}
            </ul>
          );
        }

        // `pre-wrap` in CSS, so a hand-drawn alignment inside a paragraph — the
        // agent cites `file:line` and lines up columns — survives.
        return (
          <p className="md__p" key={key}>
            {renderInline(block, key)}
          </p>
        );
      })}
    </div>
  );
}
