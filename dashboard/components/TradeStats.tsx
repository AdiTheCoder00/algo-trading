"use client";

/**
 * Brief §10's summary half of the trade log: win rate, profit factor,
 * expectancy, longest losing streak, and the R-multiple distribution as a
 * histogram rather than just a mean - a premium seller's actual shape (many
 * small wins, rare large losses) is invisible in an average and obvious in a
 * histogram, the same reasoning the tearsheet already uses (D-090's sibling
 * logic in algo/reporting/tearsheet.py).
 *
 * Every figure here is `null` rather than 0 when it cannot be computed -
 * rendered as "—", never as a zero that would read as "this strategy loses
 * everything" when the truer statement is "not enough trades yet".
 */

import type { TradeStats as TradeStatsData } from "@/lib/api";
import { money } from "@/lib/api";

interface Props {
  stats: TradeStatsData | null;
  /** The venue's settlement currency, from `health.detail`. */
  currency?: string;
}

function pct(value: string | null): string {
  return value !== null ? `${Number(value).toFixed(1)}%` : "—";
}

function ratio(value: string | null): string {
  return value !== null ? Number(value).toFixed(2) : "—";
}

function rMultiple(value: string | null): string {
  return value !== null ? `${Number(value) >= 0 ? "+" : ""}${Number(value).toFixed(2)}R` : "—";
}

export function TradeStats({ stats, currency }: Props) {
  if (!stats || stats.trades === 0) {
    return <p className="empty">Not enough completed trades yet.</p>;
  }

  const maxCount = Math.max(...stats.histogram.map((b) => b.count), 1);

  return (
    <div>
      <dl className="stats stats-tight">
        <div className="stat">
          <dt>Win rate</dt>
          <dd>
            {pct(stats.win_rate)}
            <span className="stat-sub"> ({stats.wins}W / {stats.losses}L)</span>
          </dd>
        </div>
        <div className="stat">
          <dt>Profit factor</dt>
          <dd
            className={
              stats.profit_factor === null ? "" : Number(stats.profit_factor) >= 1 ? "up" : "down"
            }
          >
            {ratio(stats.profit_factor)}
          </dd>
        </div>
        <div className="stat">
          <dt>Expectancy</dt>
          <dd
            className={
              stats.expectancy_r === null ? "" : Number(stats.expectancy_r) >= 0 ? "up" : "down"
            }
          >
            {rMultiple(stats.expectancy_r)}
          </dd>
        </div>
        <div className="stat">
          <dt>Longest losing streak</dt>
          <dd>{stats.longest_losing_streak}</dd>
        </div>
        <div className="stat">
          <dt>Gross profit</dt>
          <dd className="up">{money(stats.gross_profit, currency)}</dd>
        </div>
        <div className="stat">
          <dt>Gross loss</dt>
          <dd className="down">{money(stats.gross_loss, currency)}</dd>
        </div>
      </dl>

      {stats.histogram.length > 0 && (
        <div className="rhist">
          <div className="panel-bar" style={{ padding: "9px 0", border: "none" }}>
            <span>R-multiple distribution</span>
            <span className="mono">
              {stats.trades_with_r} of {stats.trades} trades carried a stop
            </span>
          </div>
          <div className="rhist-bars">
            {stats.histogram.map((bucket) => (
              <div className="rhist-col" key={bucket.label}>
                <div
                  className={`rhist-bar ${bucket.label.startsWith("-") ? "down" : "up"}`}
                  style={{ height: `${Math.max((bucket.count / maxCount) * 100, bucket.count > 0 ? 4 : 0)}%` }}
                  title={`${bucket.label}: ${bucket.count} trade(s)`}
                />
                <span className="rhist-label">{bucket.label}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
