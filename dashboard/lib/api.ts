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

/**
 * The research console's catalogue, served from the engine's own definitions
 * (algo/backtest/research.py) rather than hardcoded here — so the form cannot
 * offer a parameter the strategy does not have, or default one differently
 * from its constructor.
 */
export interface ParamSpec {
  name: string;
  label: string;
  kind: "decimal" | "int";
  default: string;
  minimum: string;
  maximum: string;
  help: string;
  /** Empty means every strategy; otherwise only these. */
  applies_to: string[];
}

export interface Catalogue {
  strategies: { id: string; label: string; blurb: string }[];
  timeframes: { minutes: number; label: string }[];
  parameters: ParamSpec[];
  max_bars: number;
  /** Which parameters walk-forward may optimise over, and the values it
   * steps through. Small on purpose - a wide grid fits the in-sample
   * window better without producing more real evidence. */
  walk_forward_axes: { name: string; label: string; values: string[] }[];
  /** Axes a sweep may vary. Includes `timeframe`, which changes the bars
   * themselves rather than a strategy knob. */
  sweep_axes: { name: string; label: string; values: string[] }[];
}

/**
 * One backtest result. Every money figure is a string for the same reason
 * everything else here is — see the note at the top of this file.
 */
export interface BacktestResult {
  strategy: string;
  symbol: string;
  timeframe_minutes: number;
  timeframe_label: string;
  params: Record<string, string | number>;
  server_offset: string;
  offset_was_cached: boolean;
  bars_seen: number;
  window_start: string;
  window_end: string;
  span_days: number;
  trades: number;
  wins: number;
  win_rate: string | null;
  gross_pnl: string;
  net_pnl: string;
  spread_paid: string;
  swap_paid: string;
  commission_paid: string;
  buy_and_hold: string;
  max_drawdown_pct: string | null;
  equity_curve: { ts: string; equity: string }[];
  recent_trades: {
    side: string;
    entry_ts: string;
    exit_ts: string | null;
    entry_price: string;
    exit_price: string | null;
    net_pnl: string;
    exit_reason: string;
  }[];
  /** Never empty by design — what this number is not evidence of. */
  caveats: string[];
}

/**
 * Walk-forward: fit on a window, validate on the window that follows.
 *
 * The two P&L figures are deliberately never combined — `walkforward.py` has
 * no `overall` property and no way to produce one, because averaging a number
 * the parameters were fitted to with one they were not is worse than either
 * alone. The UI keeps them in separate columns for the same reason.
 */
export interface WalkForwardResult {
  strategy: string;
  symbol: string;
  timeframe_label: string;
  axes: string[];
  in_sample_days: number;
  out_of_sample_days: number;
  window_start: string;
  window_end: string;
  server_offset: string;
  confidence: "INSUFFICIENT" | "THIN" | "ADEQUATE";
  feasibility: string;
  supports_a_conclusion: boolean;
  min_oos_trades: number;
  windows: number;
  oos_trades: number;
  in_sample_net_pnl: string;
  out_of_sample_net_pnl: string;
  /** Out-of-sample P&L from fixed, never-optimised parameters. */
  baseline_net_pnl: string | null;
  /** `false` here means the optimisation is fitting noise — the finding, not
   * a failure of the run. `null` when no baseline was supplied. */
  optimisation_beat_doing_nothing: boolean | null;
  stability: {
    name: string;
    verdict: "STABLE" | "DRIFTING" | "UNSTABLE";
    values: string[];
    distinct: number;
    flip_rate: string;
  }[];
  unstable: string[];
  results: {
    index: number;
    in_sample_start: string;
    in_sample_end: string;
    out_of_sample_start: string;
    out_of_sample_end: string;
    chosen: Record<string, string>;
    in_sample_net_pnl: string;
    in_sample_trades: number;
    out_of_sample_net_pnl: string;
    out_of_sample_trades: number;
    baseline_net_pnl: string | null;
  }[];
}

/**
 * A two-axis grid of backtests, plus an opinion on whether its best cell is
 * signal or a lucky square.
 *
 * `robustness` is the point. A heatmap invites reading off the greenest cell,
 * and D-131 is the standing evidence that doing so fits noise — a sweep has no
 * out-of-sample step at all, so it is a weaker procedure than the walk-forward
 * panel, not a stronger one.
 */
export interface SweepResult {
  strategy: string;
  symbol: string;
  window_start: string;
  window_end: string;
  server_offset: string;
  row_axis: string;
  row_values: string[];
  column_axis: string;
  column_values: string[];
  cells: { row: string; column: string; net_pnl: string; trades: number }[];
  robustness: {
    verdict: "PLATEAU" | "ISOLATED PEAK" | "NOTHING WORKS";
    best_row: string;
    best_column: string;
    best_net_pnl: string;
    neighbour_mean: string | null;
    positive_cells: number;
    total_cells: number;
    median_net_pnl: string;
    notes: string[];
  };
}

export class ApiError extends Error {}

/**
 * The catalogue, fetched once per page load and shared.
 *
 * Both research panels need it and it never changes between them, so the
 * promise is memoised at module scope rather than each component issuing its
 * own identical request. Rejections are not cached — a failed load should be
 * retryable on the next mount, not sticky for the life of the tab.
 */
let cataloguePromise: Promise<Catalogue> | null = null;

export function loadCatalogue(): Promise<Catalogue> {
  cataloguePromise ??= api.catalogue().catch((error: unknown) => {
    cataloguePromise = null;
    throw error;
  });
  return cataloguePromise;
}

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
  catalogue: () => get<Catalogue>("research/catalogue"),

  /** A two-axis sweep. A GET, same reasoning as `backtest` above. */
  sweep: (query: Record<string, string>) =>
    get<SweepResult>(`research/sweep?${new URLSearchParams(query)}`),

  /** Walk-forward. A GET, same reasoning as `backtest` above. */
  walkForward: (query: Record<string, string>) =>
    get<WalkForwardResult>(`research/walk-forward?${new URLSearchParams(query)}`),

  /**
   * Run one backtest. A GET, deliberately: the operation is nullipotent, and
   * the API's own `test_exactly_one_write_endpoint_exists` guards the promise
   * that the kill switch is the only write. A study has no business weakening
   * that guard, so it does not become a POST just to carry parameters.
   */
  async backtest(
    strategy: string,
    timeframeMinutes: number,
    params: Record<string, string>,
  ): Promise<BacktestResult> {
    const query = new URLSearchParams({
      strategy,
      timeframe_minutes: String(timeframeMinutes),
      symbol: "XAUUSD",
      ...params,
    });
    const response = await fetch(`/api/state/research/backtest?${query}`, {
      cache: "no-store",
    });
    const body = (await response.json().catch(() => ({}))) as {
      detail?: string;
      error?: string;
    };
    if (!response.ok) {
      throw new ApiError(body.detail ?? body.error ?? `backtest failed (${response.status})`);
    }
    return body as unknown as BacktestResult;
  },

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

/**
 * Format a Decimal string in whichever currency the engine says it is running.
 *
 * The venue decides, not the dashboard: MCX GOLDM settles in rupees and the
 * XAUUSD CFD settles in dollars, and rendering one behind the other's symbol
 * puts the right number next to the wrong meaning. `health.detail.currency`
 * carries it; absent, INR is assumed because that is what every run before the
 * CFD venue existed used.
 */
export function money(value: string, currency: string | undefined): string {
  return currency === "USD" ? usd(value) : inr(value);
}

/**
 * Format a Decimal string as US dollars. The CFD venue quotes and settles in
 * USD (D-121); rendering an XAUUSD result in rupees would put the right number
 * behind the wrong symbol, which is worse than no symbol at all.
 */
export function usd(value: string): string {
  const negative = value.trim().startsWith("-");
  const [whole = "0", fraction] = value.trim().replace("-", "").split(".");
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const decimals = fraction ? `.${fraction.slice(0, 2)}` : "";
  return `${negative ? "-" : ""}$${grouped}${decimals}`;
}

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
