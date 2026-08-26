"use client";

/**
 * The option chain as of the last recorded bar - calls on the left, puts on
 * the right, strike in the middle, the same ladder shape as a real broker
 * terminal (and the one the strategy's own screenshot showed at the start of
 * this project). Two things this view exists to make visible that a flat
 * trade log cannot:
 *
 * **Which strikes are actually tradeable, not just listed.** A strike with no
 * bid/ask is real information - "listed but nobody is quoting it" is a
 * different fact from "not listed at all", and `DeltaStrangle`'s own
 * `_explain_miss` reasoning depends on that distinction. Untradeable rows are
 * dimmed rather than hidden, so the gap in the ladder is visible, not absent.
 *
 * **Which two strikes the strategy actually holds**, highlighted against the
 * whole ladder - the "why this one and not the one next to it" question a
 * flat position list cannot answer on its own.
 */

import type { ChainSnapshot } from "@/lib/api";
import { inr } from "@/lib/api";

interface Props {
  snapshot: ChainSnapshot | null;
}

/**
 * Strikes shown either side of the money. A real GOLDM ladder lists ~140
 * strikes, and the ones 30,000 points away are noise on every question this
 * panel exists to answer - the strategy sells inside roughly 0.15 delta, which
 * on a 500-point ladder is well within this window. The count of what was
 * clipped is still printed, so a narrow window never reads as a short ladder.
 */
const STRIKES_EACH_SIDE = 15;

function fmtDelta(delta: number | null): string {
  return delta !== null ? delta.toFixed(3) : "—";
}

function fmtIv(iv: number | null): string {
  return iv !== null ? `${(iv * 100).toFixed(1)}%` : "—";
}

function fmtPrice(value: string | null): string {
  return value !== null ? Number(value).toFixed(2) : "—";
}

/**
 * Why a row is untradeable, short enough to sit in a cell title. "Listed but
 * nobody is quoting it" and "quoted, but so wide the book is a fiction" are
 * different facts and only the second one nearly got sold (Q17), so the panel
 * distinguishes them rather than dimming both the same grey.
 */
const FLAG_REASON: Record<string, string> = {
  EMPTY_BOOK: "listed, but nobody is quoting it",
  TOO_WIDE: "quoted, but the spread is too wide to be a real book",
  NO_OPEN_INTEREST: "nobody holds this contract",
  CROSSED: "bid is above the ask — a broken book",
  NON_POSITIVE: "a zero or negative price",
  STALE: "the quote is too old to fill against",
  ZERO_VOLUME: "nothing has traded",
  CIRCUIT_LOCKED: "locked at a circuit limit",
};

function reasonFor(row: { tradeable: boolean; flag?: string } | undefined): string | undefined {
  if (!row || row.tradeable || !row.flag) return undefined;
  return FLAG_REASON[row.flag] ?? row.flag;
}

export function OptionChain({ snapshot }: Props) {
  if (!snapshot || snapshot.rows.length === 0) {
    return (
      <p className="empty">
        No chain recorded yet - this run has no chain provider wired in, or has
        not reached a bar with one.
      </p>
    );
  }

  const byStrike = new Map<string, { ce?: (typeof snapshot.rows)[number]; pe?: (typeof snapshot.rows)[number] }>();
  for (const row of snapshot.rows) {
    const entry = byStrike.get(row.strike) ?? {};
    if (row.right === "CE") entry.ce = row;
    else entry.pe = row;
    byStrike.set(row.strike, entry);
  }
  const allStrikes = [...byStrike.keys()].sort((a, b) => Number(a) - Number(b));
  const futures = Number(snapshot.futures_price);
  const atmStrike = allStrikes.reduce((closest, s) =>
    Math.abs(Number(s) - futures) < Math.abs(Number(closest) - futures) ? s : closest,
  );

  // Window around the money, but never drop a strike the strategy is actually
  // holding: a held leg that has drifted outside the window is exactly the
  // position worth looking at, and hiding it would be the one real failure
  // this panel could have.
  const atmIndex = allStrikes.indexOf(atmStrike);
  const low = Math.max(0, atmIndex - STRIKES_EACH_SIDE);
  const high = Math.min(allStrikes.length, atmIndex + STRIKES_EACH_SIDE + 1);
  const windowed = new Set(allStrikes.slice(low, high));
  for (const strike of allStrikes) {
    const { ce, pe } = byStrike.get(strike)!;
    if (ce?.held || pe?.held) windowed.add(strike);
  }
  const strikes = allStrikes.filter((s) => windowed.has(s));
  const hidden = allStrikes.length - strikes.length;

  const visibleRows = strikes.flatMap((s) => {
    const { ce, pe } = byStrike.get(s)!;
    return [ce, pe].filter((r) => r !== undefined);
  });
  const shown = visibleRows.length;
  const untradeable = visibleRows.filter((r) => !r.tradeable).length;

  return (
    <div>
      <div className="chain-meta">
        <span className="mono">{snapshot.underlying}</span>
        <span className="mono">expiry {snapshot.option_expiry}</span>
        <span className="mono">future {inr(snapshot.futures_price)}</span>
        <span className="mono">{snapshot.dte} DTE</span>
        {snapshot.forced_exit_in_sessions !== null && (
          <span className={`mono ${snapshot.forced_exit_in_sessions <= 1 ? "chain-deadline-soon" : ""}`}>
            {snapshot.forced_exit_in_sessions < 0
              ? "forced-exit deadline passed"
              : `forced exit in ${snapshot.forced_exit_in_sessions} ${
                  snapshot.forced_exit_in_sessions === 1 ? "session" : "sessions"
                }`}
          </span>
        )}
      </div>
      <div className="scroll">
        <table className="chain">
          <thead>
            <tr>
              <th colSpan={4} className="chain-side">
                calls
              </th>
              <th>strike</th>
              <th colSpan={4} className="chain-side">
                puts
              </th>
            </tr>
            <tr>
              <th className="num">delta</th>
              <th className="num">iv</th>
              <th className="num">ltp</th>
              <th className="num">vol</th>
              <th className="num">&nbsp;</th>
              <th className="num">ltp</th>
              <th className="num">vol</th>
              <th className="num">iv</th>
              <th className="num">delta</th>
            </tr>
          </thead>
          <tbody>
            {strikes.map((strike) => {
              const { ce, pe } = byStrike.get(strike)!;
              const cell = (
                row: (typeof snapshot.rows)[number] | undefined,
                text: string,
                { markHeld = false }: { markHeld?: boolean } = {},
              ) => {
                const reason = reasonFor(row);
                return (
                  <td
                    className={[
                      "num",
                      "mono",
                      markHeld && row?.held ? "chain-held" : "",
                      row && !row.tradeable ? "chain-quiet" : "",
                    ]
                      .filter(Boolean)
                      .join(" ")}
                    title={reason}
                  >
                    {row ? text : "—"}
                  </td>
                );
              };
              return (
                <tr key={strike} className={strike === atmStrike ? "chain-atm" : ""}>
                  {cell(ce, fmtDelta(ce?.delta ?? null), { markHeld: true })}
                  {cell(ce, fmtIv(ce?.iv ?? null))}
                  {cell(ce, fmtPrice(ce?.ltp ?? null), { markHeld: true })}
                  {cell(ce, String(ce?.volume ?? ""))}
                  <td className="num mono chain-strike">{strike}</td>
                  {cell(pe, fmtPrice(pe?.ltp ?? null))}
                  {cell(pe, String(pe?.volume ?? ""))}
                  {cell(pe, fmtIv(pe?.iv ?? null))}
                  {cell(pe, fmtDelta(pe?.delta ?? null), { markHeld: true })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="chain-legend">
        <span className="chain-held-dot" /> currently held &nbsp;&nbsp; dimmed = not tradeable
        {untradeable > 0 && ` (${untradeable} of ${shown}; hover one for the reason)`}
        {hidden > 0 && (
          <>
            &nbsp;&nbsp; showing {STRIKES_EACH_SIDE} strikes either side of the money,{" "}
            {hidden} further out not shown
          </>
        )}
      </p>
    </div>
  );
}
