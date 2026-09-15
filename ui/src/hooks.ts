/**
 * Three hooks, and each exists because a CSS-only answer would not have been
 * enough.
 *
 * `prefers-reduced-motion` is honoured in the stylesheet as well, but a
 * JavaScript timer is not a transition — CSS cannot switch one off, so a
 * component running one has to ask.
 *
 * The 1024px breakpoint is a structural change and not a styling one: below it
 * the sidebar becomes a control with `aria-expanded`, and a control that exists
 * in the accessibility tree only at some viewport widths has to be rendered
 * conditionally, not hidden with `display: none`.
 *
 * `usePersisted` is where the theme and the sidebar keep their state. One
 * helper rather than two bespoke hooks, because the interesting part — that
 * every read and every write is wrapped, since Safari's private mode throws
 * rather than returning null — is worth writing once.
 */

import { useCallback, useEffect, useState } from "react";

function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState<boolean>(() =>
    typeof window === "undefined" ? false : window.matchMedia(query).matches,
  );

  useEffect(() => {
    const list = window.matchMedia(query);
    const onChange = (event: MediaQueryListEvent) => setMatches(event.matches);
    // Re-read on subscribe: the query can have flipped between the initial
    // render and this effect, and a stale `false` here is a permanently
    // animating page for someone who asked for no animation.
    setMatches(list.matches);
    list.addEventListener("change", onChange);
    return () => list.removeEventListener("change", onChange);
  }, [query]);

  return matches;
}

export function useReducedMotion(): boolean {
  return useMediaQuery("(prefers-reduced-motion: reduce)");
}

export function useNarrowViewport(): boolean {
  return useMediaQuery("(max-width: 1023px)");
}

/**
 * A value that survives a reload, or the fallback when it cannot.
 *
 * Every access is guarded. A browser with site data blocked throws on the read
 * *and* on the write, and a preference that cannot be saved is a smaller
 * problem than an app that will not render.
 */
export function usePersisted(
  key: string,
  fallback: string,
): [string, (value: string) => void] {
  const [value, setValue] = useState<string>(() => {
    try {
      return window.localStorage.getItem(key) ?? fallback;
    } catch {
      return fallback;
    }
  });

  const write = useCallback(
    (next: string) => {
      setValue(next);
      try {
        window.localStorage.setItem(key, next);
      } catch {
        // Preference lost on reload; the app still works.
      }
    },
    [key],
  );

  return [value, write];
}
