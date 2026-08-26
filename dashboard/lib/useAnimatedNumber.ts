"use client";

/**
 * Tweens a displayed number toward a target over a short duration, instead of
 * snapping - the same reasoning as the margin bar's CSS width transition, for
 * a number rather than a bar. Plain `requestAnimationFrame`, no motion
 * library: the only thing being animated is a single scalar on an interval
 * timer, which does not need more machinery than this.
 *
 * Respects `prefers-reduced-motion` directly, since this is a JS-driven
 * animation the CSS blanket rule in globals.css cannot reach.
 */

import { useEffect, useRef, useState } from "react";

const DURATION_MS = 500;

function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** Ease-out - decelerating into the new value, never overshooting it. A
 * monitoring page for money is not the place for a bouncy spring. */
function easeOutQuad(t: number): number {
  return 1 - (1 - t) * (1 - t);
}

export function useAnimatedNumber(target: number | null): number | null {
  const [displayed, setDisplayed] = useState(target);
  const frame = useRef<number | undefined>(undefined);
  const previous = useRef(target);

  useEffect(() => {
    if (target === null) {
      setDisplayed(null);
      previous.current = null;
      return;
    }
    if (previous.current === null || prefersReducedMotion()) {
      setDisplayed(target);
      previous.current = target;
      return;
    }
    const from = previous.current;
    const delta = target - from;
    if (delta === 0) return;

    const start = performance.now();
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / DURATION_MS);
      setDisplayed(from + delta * easeOutQuad(t));
      if (t < 1) {
        frame.current = requestAnimationFrame(tick);
      } else {
        previous.current = target;
      }
    };
    frame.current = requestAnimationFrame(tick);
    return () => {
      if (frame.current !== undefined) cancelAnimationFrame(frame.current);
    };
  }, [target]);

  return displayed;
}
