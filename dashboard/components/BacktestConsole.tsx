"use client";

/**
 * The research console: pick a strategy, a timeframe and its parameters, run a
 * backtest over real MT5 history, read the result.
 *
 * ## What this can and cannot do, on purpose
 *
 * A study reads history and returns numbers. It holds no portfolio, no router
 * and no broker connection, and writes to no state file the live engine reads
 * (`algo/backtest/research.py` makes the full argument). So this does not
 * contradict the read-only rule the monitoring API opens with, nor Q21's
 * "parameters change through config and a restart, never through the UI" —
 * choosing a timeframe for a *study* is exploration; changing what the running
 * loop trades is a deployment, and still goes through a committed file.
 *
 * There is deliberately no "apply this to the live loop" button.
 *
 * ## The form is built from the engine's own catalogue
 *
 * Strategies, timeframes, parameter names, defaults and ranges all come from
 * `/research/catalogue` rather than being written here. A form that hardcoded
 * them could offer a knob the strategy does not have, or default one
 * differently from its constructor — and would drift the first time either
 * changed.
 *
 * ## Every result carries its caveats
 *
 * The tearsheet already refuses to print a ratio without its sample size, and
 * the trade-stats panel renders `null` rather than a flattering zero. A console
 * that returned a big green number with nothing qualifying it would undo both
 * at the last step, so `caveats` is rendered as prominently as the P&L and is
 * never empty.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  loadCatalogue,
  usd,
  shortTime,
  type BacktestResult,
  type Catalogue,
  type ParamSpec,
} from "@/lib/api";

function Sparkline({ points }: { points: { ts: string; equity: string }[] }) {
  if (points.length < 2) return null;
  const values = points.map((p) => Number(p.equity));
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const width = 900;
  const height = 120;
  const x = (i: number) => (i / (values.length - 1)) * width;
  const y = (v: number) => 6 + (1 - (v - min) / span) * (height - 12);
  const line = values
    .map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`)
    .join(" ");
  const last = values[values.length - 1] ?? 0;
  const first = values[0] ?? 0;
  const colour = last >= first ? "var(--sage)" : "var(--clay)";
  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      width="100%"
      height={height}
      preserveAspectRatio="none"
      role="img"
      aria-label={`Equity curve over ${points.length} points`}
    >
      <path d={`${line} L${width},${height} L0,${height} Z`} fill={colour} fillOpacity="0.12" />
      <path d={line} fill="none" stroke={colour} strokeWidth="1.6" />
    </svg>
  );
}

export function BacktestConsole() {
  const [catalogue, setCatalogue] = useState<Catalogue | null>(null);
  const [strategy, setStrategy] = useState("breakout");
  const [timeframe, setTimeframe] = useState(30);
  const [values, setValues] = useState<Record<string, string>>({});
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    loadCatalogue()
      .then((loaded) => {
        setCatalogue(loaded);
        setValues(
          Object.fromEntries(loaded.parameters.map((p) => [p.name, p.default])),
        );
      })
      .catch((caught) =>
        setError(caught instanceof Error ? caught.message : String(caught)),
      );
  }, []);

  /** Only the knobs this strategy actually has — `applies_to` empty means all. */
  const shown = useMemo<ParamSpec[]>(
    () =>
      (catalogue?.parameters ?? []).filter(
        (p) => p.applies_to.length === 0 || p.applies_to.includes(strategy),
      ),
    [catalogue, strategy],
  );

  const run = useCallback(async () => {
    setRunning(true);
    setError(null);
    try {
      // Only the parameters on screen are sent. Sending a hidden one (a channel
      // length to MACD, say) would have the engine range-check a knob the user
      // was never shown and cannot correct.
      const sent = Object.fromEntries(
        shown.map((p) => [p.name, values[p.name] ?? p.default]),
      );
      setResult(await api.backtest(strategy, timeframe, sent));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      setResult(null);
    } finally {
      setRunning(false);
    }
  }, [shown, values, strategy, timeframe]);

  if (!catalogue) {
    return (
      <p className="empty">
        {error ? `Cannot load the catalogue: ${error}` : "Loading the catalogue…"}
      </p>
    );
  }

  const net = result ? Number(result.net_pnl) : 0;
  const bh = result ? Number(result.buy_and_hold) : 0;

  return (
    <div>
      <div className="bt-form">
        <div className="field">
          <label htmlFor="bt-strategy">Strategy</label>
          <select
            id="bt-strategy"
            value={strategy}
            onChange={(e) => setStrategy(e.target.value)}
            disabled={running}
          >
            {catalogue.strategies.map((s) => (
              <option key={s.id} value={s.id}>
                {s.label}
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label htmlFor="bt-timeframe">Timeframe</label>
          <select
            id="bt-timeframe"
            value={timeframe}
            onChange={(e) => setTimeframe(Number(e.target.value))}
            disabled={running}
          >
            {catalogue.timeframes.map((t) => (
              <option key={t.minutes} value={t.minutes}>
                {t.label}
              </option>
            ))}
          </select>
        </div>

        {shown.map((p) => (
          <div className="field" key={p.name}>
            <label htmlFor={`bt-${p.name}`} title={p.help}>
              {p.label}
            </label>
            <input
              id={`bt-${p.name}`}
              type="number"
              inputMode="decimal"
              step={p.kind === "int" ? 1 : "any"}
              min={p.minimum}
              max={p.maximum}
              value={values[p.name] ?? p.default}
              title={p.help}
              onChange={(e) =>
                setValues((prev) => ({ ...prev, [p.name]: e.target.value }))
              }
              disabled={running}
            />
          </div>
        ))}
      </div>

      <div className="bt-actions">
        <button type="button" onClick={run} disabled={running} className="bt-run">
          {running ? "Running…" : "Run backtest"}
        </button>
        <span className="chain-legend">
          Reads history and returns numbers. Touches no live position, order or
          parameter.
        </span>
      </div>

      {error && <p className="halt-note" style={{ color: "var(--clay)" }}>{error}</p>}

      {result && (
        <div className="bt-result">
          <dl className="stats stats-tight">
            <div className="stat">
              <dt>Net P&amp;L</dt>
              <dd className={net > 0 ? "up" : net < 0 ? "down" : ""}>
                {usd(result.net_pnl)}
              </dd>
            </div>
            <div className="stat">
              <dt>Buy &amp; hold</dt>
              <dd className={bh > 0 ? "up" : bh < 0 ? "down" : ""}>
                {usd(result.buy_and_hold)}
              </dd>
            </div>
            <div className="stat">
              <dt>Trades</dt>
              <dd>
                {result.trades}
                <span className="stat-sub"> ({result.wins}W)</span>
              </dd>
            </div>
            <div className="stat">
              <dt>Win rate</dt>
              <dd>
                {result.win_rate !== null
                  ? `${Number(result.win_rate).toFixed(1)}%`
                  : "—"}
              </dd>
            </div>
            <div className="stat">
              <dt>Max drawdown</dt>
              <dd className={result.max_drawdown_pct !== null ? "down" : ""}>
                {result.max_drawdown_pct !== null
                  ? `${Number(result.max_drawdown_pct).toFixed(2)}%`
                  : "—"}
              </dd>
            </div>
            <div className="stat">
              <dt>Gross P&amp;L</dt>
              <dd>{usd(result.gross_pnl)}</dd>
            </div>
            <div className="stat">
              <dt>Spread paid</dt>
              <dd className="down">{usd(result.spread_paid)}</dd>
            </div>
            <div className="stat">
              <dt>Swap paid</dt>
              <dd className={Number(result.swap_paid) > 0 ? "down" : ""}>
                {usd(result.swap_paid)}
              </dd>
            </div>
          </dl>

          <div className="panel-bar" style={{ border: "none", padding: "10px 0" }}>
            <span>equity curve</span>
            <span className="mono">
              {result.bars_seen.toLocaleString()} bars · {result.span_days} days
            </span>
          </div>
          <Sparkline points={result.equity_curve} />

          <p className="chain-legend">
            {result.timeframe_label} · {shortTime(result.window_start)} to{" "}
            {shortTime(result.window_end)} · server clock {result.server_offset}
          </p>

          <div className="warnings" style={{ marginTop: 14 }}>
            <h2>What this is not evidence of</h2>
            <ul>
              {result.caveats.map((caveat) => (
                <li key={caveat}>{caveat}</li>
              ))}
            </ul>
          </div>

          {result.recent_trades.length > 0 && (
            <>
              <div className="panel-bar" style={{ border: "none", padding: "10px 0" }}>
                <span>last {result.recent_trades.length} trades</span>
              </div>
              <div className="scroll" style={{ maxHeight: 280 }}>
                <table>
                  <thead>
                    <tr>
                      <th>Opened</th>
                      <th>Closed</th>
                      <th>Side</th>
                      <th className="num">Entry</th>
                      <th className="num">Exit</th>
                      <th className="num">Net</th>
                      <th>Why it closed</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.recent_trades.map((trade, i) => {
                      const pnl = Number(trade.net_pnl);
                      return (
                        <tr key={`${trade.entry_ts}-${i}`}>
                          <td className="mono">{shortTime(trade.entry_ts)}</td>
                          <td className="mono">
                            {trade.exit_ts ? shortTime(trade.exit_ts) : "open"}
                          </td>
                          <td className="mono">{trade.side}</td>
                          <td className="num mono">{trade.entry_price}</td>
                          <td className="num mono">{trade.exit_price ?? "—"}</td>
                          <td
                            className={`num mono ${pnl > 0 ? "up" : pnl < 0 ? "down" : ""}`}
                          >
                            {usd(trade.net_pnl)}
                          </td>
                          <td className="reason">{trade.exit_reason || "—"}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
