"use client";

/**
 * How much of the configured margin cap is actually in use.
 *
 * `RiskEngine`'s own cap (brief's risk.caps.max_total_margin_pct) is what
 * decides whether the *next* signal can size at all - it silently refuses a
 * signal that would push margin past the cap. Without this, an operator can
 * only find that out by noticing entries have stopped happening and guessing
 * why; this makes the number the decision is actually made from visible
 * directly, the same reasoning the trade log already applies to entries and
 * exits.
 *
 * `health.detail` carries these three keys only when a cap is configured at
 * all (`algo/backtest/engine.py`'s `_record_margin_utilisation`) - a run with
 * no cap renders nothing here rather than a bar that is always at 0%.
 */

import { inr } from "@/lib/api";

interface Props {
  used: string | undefined;
  cap: string | undefined;
  capPct: string | undefined;
}

export function MarginUtilisation({ used, cap, capPct }: Props) {
  if (used === undefined || cap === undefined || capPct === undefined) {
    return <p className="empty">No margin cap configured for this run.</p>;
  }

  const usedN = Number(used);
  const capN = Number(cap);
  const pctOfCap = capN > 0 ? Math.min(100, (usedN / capN) * 100) : 0;
  const severity = pctOfCap >= 90 ? "danger" : pctOfCap >= 70 ? "warn" : "ok";

  return (
    <div>
      <div className="margin-bar-row">
        <div className="margin-bar-track">
          <div
            className={`margin-bar-fill margin-${severity}`}
            style={{ transform: `scaleX(${pctOfCap / 100})` }}
          />
        </div>
        <span className="mono margin-bar-pct">{pctOfCap.toFixed(1)}%</span>
      </div>
      <p className="chain-legend">
        <span className="mono">{inr(used)}</span> used of <span className="mono">{inr(cap)}</span> cap (
        {capPct}% of equity)
      </p>
    </div>
  );
}
