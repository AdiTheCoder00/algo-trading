/**
 * Types and fetch helpers for the monitoring API.
 *
 * Every money field is a `string`, not a `number`, all the way through. The engine
 * serialises `Decimal` as a string precisely so it does not pass through a float,
 * and parsing it into a JavaScript number here would undo that at the last step —
 * JS numbers are float64, so 1000000.05 becomes 1000000.0499999999. Money is
 * displayed as text and only converted for drawing a chart, where a pixel of
 * error is invisible and a paisa of error in a reported figure is not.
 */

export interface Health {
  status: string;
  mode: string;
  kill_switch: string;
  broker: string;
  last_bar: string | null;
  detail: Record<string, string>;
  warnings: string[];
}

export interface EquityPoint {
  ts: string;
  equity: string;
  cash: string;
  realised: string;
  unrealised: string;
  charges: string;
  open_positions: number;
}

export interface Position {
  instrument: string;
  lots: number;
  qty: string;
  average_price: string;
  mark: string | null;
  unrealised: string | null;
  updated_at: string;
}

export interface Signal {
  signal_id: string;
  ts: string;
  strategy: string;
  action: string;
  reason: string;
  context: Record<string, string>;
}

export interface Note {
  ts: string;
  message: string;
}

export interface KillSwitchRequest {
  id: number;
  requested_at: string;
  requested_by: string;
  reason: string;
  flatten: boolean;
  acted_on_at: string | null;
}

/**
 * Brief §10's summary half - win rate, profit factor, expectancy, the
 * R-multiple distribution - computed server-side by the same `trade_stats()`
 * the CLI's tearsheet uses (algo/reporting/metrics.py), not reimplemented
 * here. Every ratio and money field is `None`/string rather than a bare 0:
 * a profit factor of 0.0 reads as "loses everything", None reads as
 * "nothing to divide by yet" - a different, truer statement with no trades.
 */
export interface TradeStats {
  trades: number;
  wins: number;
  losses: number;
  win_rate: string | null;
  profit_factor: string | null;
  gross_profit: string;
  gross_loss: string;
  largest_win: string | null;
  largest_loss: string | null;
  longest_losing_streak: number;
  trades_with_r: number;
  expectancy_r: string | null;
  average_win_r: string | null;
  average_loss_r: string | null;
  histogram: { label: string; count: number }[];
}

/**
 * One strike/right, as the engine actually saw it at the last recorded bar -
 * built by `algo/backtest/engine.py`'s `_record_chain_snapshot`, which mirrors
 * exactly what `BarContext.chain()` hands the strategy, so this is not a
 * second, possibly-diverging view of the chain.
 */
export interface ChainRow {
  strike: string;
  right: "CE" | "PE";
  bid: string | null;
  ask: string | null;
  ltp: string | null;
  volume: number;
  iv: number | null;
  delta: number | null;
  tradeable: boolean;
  /** `QuoteFlag` — why this row is not tradeable, when it is not. Absent on
   * chains recorded before the flag was carried through. */
  flag?: string;
  held: boolean;
}

export interface ChainSnapshot {
  ts: string;
  underlying: string;
  option_expiry: string;
  futures_price: string;
  dte: number;
  /** `null` when the run has no devolvement guard wired in at all - a run
   * that never enforces the rule should not claim to track its deadline. */
  forced_exit_in_sessions: number | null;
  rows: ChainRow[];
}

/**
 * One completed (or still-open) round trip, exactly as `Trade.to_log_row()`
 * writes it (algo/core/trade.py) — every field a string, `closed_at` and
 * `r_multiple` empty rather than absent when there is nothing to report yet.
 * This is the record brief §10 exists for: "why did this trade fire six weeks
 * later" answered by what travelled with the trade itself, not a log file that
 * may have rotated away.
 */
export interface Trade {
  trade_id: string;
  strategy_id: string;
  signal_id: string;
  opened_at: string;
  closed_at: string;
  legs: string;
  gross_pnl: string;
  charges_total: string;
  net_pnl: string;
  r_multiple: string;
  exit_reason: string;
  reason: string;
}

/**
 * One leg of `Trade.legs`. The instrument key is itself colon-separated
 * (`MCX:GOLDM:20260828:157000:CE`), so the split has to come from the *right* —
 * side and lots are always the last two fields, everything before them is the
 * key.
 */
export interface TradeLeg {
  instrument: string;
  side: string;
  lots: string;
}

export function parseLegs(legs: string): TradeLeg[] {
  if (!legs) return [];
  return legs.split("|").map((leg) => {
    const parts = leg.split(":");
    const lots = parts.pop() ?? "";
    const side = parts.pop() ?? "";
    return { instrument: parts.join(":"), side, lots };
  });
}

export class ApiError extends Error {}

async function get<T>(path: string): Promise<T> {
  const response = await fetch(`/api/state/${path}`, { cache: "no-store" });
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as { error?: string };
    throw new ApiError(body.error ?? `${path} returned ${response.status}`);
  }
  return (await response.json()) as T;
}

export const api = {
  health: () => get<Health>("health"),
  equity: (limit = 500) => get<EquityPoint[]>(`equity?limit=${limit}`),
  positions: () => get<Position[]>("positions"),
  signals: (limit = 20) => get<Signal[]>(`signals?limit=${limit}`),
  notes: (limit = 20) => get<Note[]>(`notes?limit=${limit}`),
  trades: (limit = 50) => get<Trade[]>(`trades?limit=${limit}`),
  tradeStats: () => get<TradeStats>("trade-stats"),
  chain: () => get<ChainSnapshot | null>("chain"),
  killSwitchHistory: () => get<KillSwitchRequest[]>("kill-switch"),

  async requestHalt(reason: string, flatten: boolean): Promise<{ note: string }> {
    const response = await fetch("/api/state/kill-switch", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ reason, flatten, requested_by: "dashboard" }),
    });
    const body = (await response.json().catch(() => ({}))) as {
      error?: string;
      note?: string;
    };
    if (!response.ok) throw new ApiError(body.error ?? `halt request failed (${response.status})`);
    return { note: body.note ?? "halt requested" };
  },
};

/** Format a Decimal string as Indian-grouped rupees, without ever parsing it. */
export function inr(value: string): string {
  const negative = value.startsWith("-");
  const [whole = "0", fraction] = value.replace("-", "").split(".");
  const last3 = whole.slice(-3);
  const rest = whole.slice(0, -3);
  const grouped = rest ? `${rest.replace(/\B(?=(\d{2})+(?!\d))/g, ",")},${last3}` : last3;
  const decimals = fraction ? `.${fraction.slice(0, 2)}` : "";
  return `${negative ? "-" : ""}₹${grouped}${decimals}`;
}

export function shortTime(iso: string): string {
  return new Date(iso).toLocaleString("en-IN", {
    timeZone: "Asia/Kolkata",
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}
