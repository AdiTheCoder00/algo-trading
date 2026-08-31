"use client";

/**
 * The equity curve, drawn as inline SVG.
 *
 * No charting library. A line and an underwater plot are about thirty lines of
 * path arithmetic, and pulling in a chart package for them would add a few hundred
 * transitive dependencies to a page that runs on the same machine as a trading
 * engine. The smaller the dependency surface next to the money, the better.
 *
 * The drawdown panel underneath is not decoration either — brief §10 asks for the
 * underwater plot specifically, because depth and duration are different
 * experiences and a single equity line shows neither clearly.
 */

import type { EquityPoint } from "@/lib/api";
import { money } from "@/lib/api";

interface Props {
  points: EquityPoint[];
  /** The venue's settlement currency, from `health.detail`. */
  currency?: string;
}

const WIDTH = 900;
const HEIGHT = 200;
const UNDERWATER = 70;
const PAD = 8;

export function EquityChart({ points, currency }: Props) {
  if (points.length < 2) {
    return <p className="empty">Not enough points yet to draw a curve.</p>;
  }

  // Converted to float only for pixel geometry. Every figure the reader actually
  // reads stays a Decimal string — a pixel of rounding is invisible, a paisa in a
  // reported number is not.
  const values = points.map((p) => Number(p.equity));
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;

  const x = (i: number) => PAD + (i / (points.length - 1)) * (WIDTH - PAD * 2);
  const y = (v: number) => PAD + (1 - (v - min) / span) * (HEIGHT - PAD * 2);

  const line = values.map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const area = `${line} L${x(values.length - 1).toFixed(1)},${HEIGHT - PAD} L${x(0).toFixed(1)},${HEIGHT - PAD} Z`;

  // Underwater: distance below the running peak, as a percentage.
  let peak = values[0] ?? 0;
  const drawdown = values.map((v) => {
    peak = Math.max(peak, v);
    return peak > 0 ? ((peak - v) / peak) * 100 : 0;
  });
  const worst = Math.max(...drawdown, 0.0001);
  const dy = (d: number) => PAD + (d / worst) * (UNDERWATER - PAD * 2);
  const underwater =
    drawdown.map((d, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${dy(d).toFixed(1)}`).join(" ") +
    ` L${x(drawdown.length - 1).toFixed(1)},${PAD} L${x(0).toFixed(1)},${PAD} Z`;

  const last = points[points.length - 1];
  const first = points[0];

  return (
    <div>
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        width="100%"
        height={HEIGHT}
        role="img"
        aria-label={`Equity curve, ${points.length} points, latest ${last?.equity ?? ""}`}
        preserveAspectRatio="none"
      >
        <defs>
          <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--ice)" stopOpacity="0.28" />
            <stop offset="100%" stopColor="var(--ice)" stopOpacity="0" />
          </linearGradient>
        </defs>
        <path d={area} fill="url(#equityFill)" />
        <path d={line} fill="none" stroke="var(--ice)" strokeWidth="1.6" />
        <circle
          cx={x(values.length - 1)}
          cy={y(values[values.length - 1] ?? 0)}
          r="3"
          fill="var(--ice)"
        />
      </svg>

      <div className="panel-bar" style={{ borderTop: "1px solid var(--line)", marginTop: 4 }}>
        <span>underwater — depth below the running peak</span>
        <span className="mono">worst {worst.toFixed(3)}%</span>
      </div>
      <svg
        viewBox={`0 0 ${WIDTH} ${UNDERWATER}`}
        width="100%"
        height={UNDERWATER}
        role="img"
        aria-label={`Drawdown, worst ${worst.toFixed(3)} percent`}
        preserveAspectRatio="none"
      >
        <path d={underwater} fill="var(--clay)" fillOpacity="0.22" stroke="var(--clay)" strokeWidth="1" />
      </svg>

      <div className="panel-bar" style={{ borderTop: "1px solid var(--line)" }}>
        <span className="mono">{first ? money(first.equity, currency) : ""} at open</span>
        <span className="mono">{last ? money(last.equity, currency) : ""} now</span>
      </div>
    </div>
  );
}
