"use client";

/**
 * The broker's own numbers for the logged-in account.
 *
 * This panel and the equity chart above it are answering two different
 * questions, and the whole reason it exists is that they can disagree. The
 * chart is the engine's book - what the strategy believes it holds, marked from
 * the bars it decided on. This is what the venue says is actually there. A run
 * where those two drift apart is the single most important thing on the page,
 * and it is invisible if only one of them is drawn.
 *
 * `null` means the run has no broker account behind it at all - a backtest, a
 * replay, or a paper run - which is a different statement from an account that
 * happens to hold nothing, and is rendered as one.
 *
 * Every figure is formatted with the *account's* currency, not the venue
 * currency in `health.detail`: the account is the thing being described here,
 * and it is the thing that knows what it settles in.
 */

import type { Account } from "@/lib/api";
import { money, shortTime } from "@/lib/api";

interface Props {
  account: Account | null;
}

/**
 * Margin level is the number that decides whether the broker starts closing
 * positions for you, so it gets the same severity treatment as the margin cap
 * bar rather than being printed flat. Vantage stops out at 50% and margin-calls
 * at 100%; the thresholds here sit above both, because a level worth colouring
 * is one worth seeing *before* it is the broker's decision and not yours.
 */
function levelSeverity(level: string | null): string {
  if (level === null) return "";
  const value = Number(level);
  if (value < 200) return "bad";
  if (value < 500) return "warn";
  return "good";
}

export function AccountPanel({ account }: Props) {
  if (account === null) {
    return (
      <p className="empty">
        No broker account behind this run — it is a backtest, a replay, or a paper
        run. Start the loop with <span className="mono">--broker live</span> to
        trade the logged-in MT5 account.
      </p>
    );
  }

  const currency = account.currency;
  const floating = Number(account.floating_pnl);
  const isDemo = account.trade_mode === "demo" || account.trade_mode === "contest";

  return (
    <div>
      <div className="chips" style={{ marginBottom: 14 }}>
        <span className="chip">{account.login}</span>
        <span className="chip">{account.server}</span>
        {/* Loud when it is real money, quiet when it is not. The colour is the
            point: this is the one fact on the page that changes what a mistake
            costs. */}
        <span className={`chip ${isDemo ? "good" : "bad"}`}>{account.trade_mode}</span>
        <span className="chip">1:{account.leverage}</span>
        <span className="chip">
          {account.open_tickets} ticket{account.open_tickets === 1 ? "" : "s"}
        </span>
      </div>

      <dl className="stats stats-tight">
        <div className="stat">
          <dt>Balance</dt>
          <dd>{money(account.balance, currency)}</dd>
        </div>
        <div className="stat">
          <dt>Equity</dt>
          <dd>{money(account.equity, currency)}</dd>
        </div>
        <div className="stat">
          <dt>Floating P&amp;L</dt>
          <dd className={floating > 0 ? "up" : floating < 0 ? "down" : ""}>
            {money(account.floating_pnl, currency)}
          </dd>
        </div>
        <div className="stat">
          <dt>Margin used</dt>
          <dd>{money(account.margin_used, currency)}</dd>
        </div>
        <div className="stat">
          <dt>Free margin</dt>
          <dd>{money(account.margin_free, currency)}</dd>
        </div>
        <div className="stat">
          <dt>Margin level</dt>
          {/* "—" rather than 0% with nothing open: 0% is what a margin call
              looks like, and a flat account is the opposite of one. */}
          <dd className={levelSeverity(account.margin_level)}>
            {account.margin_level === null
              ? "—"
              : `${Number(account.margin_level).toFixed(0)}%`}
          </dd>
        </div>
      </dl>

      <p className="chain-legend">
        Broker&apos;s own figures, read at{" "}
        <span className="mono">{shortTime(account.updated_at)}</span>. The equity
        chart above is the engine&apos;s book — the two are separate claims, and
        a gap between them is worth looking into rather than averaging away.
      </p>
    </div>
  );
}
