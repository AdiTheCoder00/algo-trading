"use client";

/**
 * The trade log. Brief §10's "why did this fire six weeks later" question,
 * answered from what travelled with the trade rather than a log file that may
 * have rotated away — the same reasoning `page.tsx`'s signals panel already
 * follows for entries; this is its closing half.
 *
 * R-multiple, not just rupees, because the stop is what was actually risked and
 * a win/loss table without it cannot tell a big position that won small from a
 * small position that won big.
 */

import { parseLegs, type Trade } from "@/lib/api";
import { inr, shortTime } from "@/lib/api";

interface Props {
  trades: Trade[];
  /** trade_ids that arrived since the previous poll - flashed once so a new
   * entry is noticed without depending on the reader having watched it land. */
  newIds?: Set<string>;
}

export function TradeLog({ trades, newIds }: Props) {
  if (trades.length === 0) {
    return <p className="empty">No completed trades yet.</p>;
  }

  return (
    <div className="scroll">
      <table>
        <thead>
          <tr>
            <th>Opened</th>
            <th>Closed</th>
            <th>Legs</th>
            <th className="num">Net P&amp;L</th>
            <th className="num">R</th>
            <th>Exit</th>
            <th>Reason</th>
          </tr>
        </thead>
        <tbody>
          {trades.map((trade) => {
            const net = Number(trade.net_pnl);
            const r = trade.r_multiple ? Number(trade.r_multiple) : null;
            return (
              <tr key={trade.trade_id} className={newIds?.has(trade.trade_id) ? "row-new" : ""}>
                <td className="mono">{shortTime(trade.opened_at)}</td>
                <td className="mono">
                  {trade.closed_at ? shortTime(trade.closed_at) : "open"}
                </td>
                <td>
                  <div className="tags">
                    {parseLegs(trade.legs).map((leg, i) => (
                      <span className="tag" key={`${trade.trade_id}-${i}`}>
                        {leg.side} {leg.lots} {leg.instrument.split(":").slice(-2).join(" ")}
                      </span>
                    ))}
                  </div>
                </td>
                <td className={`num mono ${net > 0 ? "up" : net < 0 ? "down" : ""}`}>
                  {inr(trade.net_pnl)}
                </td>
                <td className={`num mono ${r !== null && r > 0 ? "up" : r !== null && r < 0 ? "down" : ""}`}>
                  {r !== null ? `${r.toFixed(2)}R` : "—"}
                </td>
                <td className="mono">{trade.exit_reason || "—"}</td>
                <td className="reason">{trade.reason}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
