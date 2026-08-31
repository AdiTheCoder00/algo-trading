"use client";

/**
 * Walk-forward: fit on a window, validate on the window that follows, and
 * report only what the optimiser never saw.
 *
 * ## Why this panel exists
 *
 * Every single-window backtest in this project carries the same caveat: one
 * historical window, and gold trended through it (D-124, D-127, D-130). A
 * backtest cannot tell edge from a bull market. This can — and when the answer
 * is "it cannot", it says so rather than printing a ratio.
 *
 * ## Three things rendered deliberately
 *
 * **The two P&L figures never merge.** `walkforward.py` has no combined metric
 * and no property that would produce one; averaging a number the parameters
 * were fitted to with one they were not is worse than either alone. They stay
 * in separate columns here, with the out-of-sample one given the weight.
 *
 * **The baseline is shown next to them.** Every window is also validated with
 * fixed, never-optimised parameters. If optimising did not beat leaving them
 * alone, the optimisation is fitting noise — and that line is the single most
 * informative thing on the panel, so it is rendered as a verdict rather than
 * buried as a number.
 *
 * **Unstable parameters show their whole sequence.** A verdict on its own asks
 * for trust; `40 -> 20 -> 20 -> 40 -> 10` shows the wobble and lets the reader
 * judge it.
 */

import { useCallback, useEffect, useState } from "react";
import { api, loadCatalogue, usd, type Catalogue, type WalkForwardResult } from "@/lib/api";

const CONFIDENCE_TONE: Record<string, string> = {
  ADEQUATE: "good",
  THIN: "warn",
  INSUFFICIENT: "bad",
};

export function WalkForward() {
  // The same memoised catalogue the backtest console reads, so the two
  // panels cannot disagree about which strategies or axes exist.
  const [catalogue, setCatalogue] = useState<Catalogue | null>(null);
  const [strategy, setStrategy] = useState("breakout");
  const [timeframe, setTimeframe] = useState(60);
  const [axis, setAxis] = useState("lookback");
  const [inSample, setInSample] = useState("90");
  const [outSample, setOutSample] = useState("30");
  const [bars, setBars] = useState("8000");
  const [result, setResult] = useState<WalkForwardResult | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    loadCatalogue()
      .then(setCatalogue)
      .catch((caught) =>
        setError(caught instanceof Error ? caught.message : String(caught)),
      );
  }, []);

  // `lookback` is a Donchian channel length; MACD has no such knob, so
  // optimising over it would search a grid of identical runs. The engine
  // refuses that too - this just avoids offering it in the first place.
  const axes = (catalogue?.walk_forward_axes ?? []).filter(
    (a) => strategy === "breakout" || a.name !== "lookback",
  );
  const effectiveAxis = axes.some((a) => a.name === axis) ? axis : (axes[0]?.name ?? "");

  const run = useCallback(async () => {
    setRunning(true);
    setError(null);
    try {
      setResult(
        await api.walkForward({
          strategy,
          timeframe_minutes: String(timeframe),
          axis: effectiveAxis,
          in_sample_days: inSample,
          out_of_sample_days: outSample,
          bars,
        }),
      );
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      setResult(null);
    } finally {
      setRunning(false);
    }
  }, [strategy, timeframe, effectiveAxis, inSample, outSample, bars]);

  if (!catalogue) {
    return (
      <p className="empty">
        {error ? `Cannot load the catalogue: ${error}` : "Loading the catalogue…"}
      </p>
    );
  }

  const oos = result ? Number(result.out_of_sample_net_pnl) : 0;
  const baseline = result?.baseline_net_pnl ?? null;

  return (
    <div>
      <div className="bt-form">
        <div className="field">
          <label htmlFor="wf-strategy">Strategy</label>
          <select
            id="wf-strategy"
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
          <label htmlFor="wf-timeframe">Timeframe</label>
          <select
            id="wf-timeframe"
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
        <div className="field">
          <label htmlFor="wf-axis" title="The parameter re-chosen on every window">
            Optimise over
          </label>
          <select
            id="wf-axis"
            value={effectiveAxis}
            onChange={(e) => setAxis(e.target.value)}
            disabled={running}
          >
            {axes.map((a) => (
              <option key={a.name} value={a.name}>
                {a.label}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="wf-is" title="Days the optimiser may fit to">
            Fit window (days)
          </label>
          <input
            id="wf-is"
            type="number"
            min={7}
            value={inSample}
            onChange={(e) => setInSample(e.target.value)}
            disabled={running}
          />
        </div>
        <div className="field">
          <label htmlFor="wf-oos" title="Days it is then tested on, never fitted to">
            Test window (days)
          </label>
          <input
            id="wf-oos"
            type="number"
            min={7}
            value={outSample}
            onChange={(e) => setOutSample(e.target.value)}
            disabled={running}
          />
        </div>
        <div className="field">
          <label htmlFor="wf-bars">Bars of history</label>
          <input
            id="wf-bars"
            type="number"
            min={100}
            max={catalogue.max_bars}
            value={bars}
            onChange={(e) => setBars(e.target.value)}
            disabled={running}
          />
        </div>
      </div>

      <div className="bt-actions">
        <button type="button" onClick={run} disabled={running} className="bt-run">
          {running ? "Running…" : "Run walk-forward"}
        </button>
        <span className="chain-legend">
          Many backtests, one per window per candidate. This takes a while.
        </span>
      </div>

      {error && <p className="halt-note" style={{ color: "var(--clay)" }}>{error}</p>}

      {result && (
        <div className="bt-result">
          <div className="chips" style={{ marginBottom: 14 }}>
            <span className={`chip ${CONFIDENCE_TONE[result.confidence] ?? ""}`}>
              {result.confidence}
            </span>
            <span className="chip">
              {result.windows} windows · {result.oos_trades} OOS trades
            </span>
            <span className="chip">
              {result.timeframe_label} · optimising {result.axes.join(", ")}
            </span>
          </div>

          <p className="chain-legend" style={{ marginTop: 0 }}>
            {result.feasibility}
          </p>

          {/* The headline comparison. In-sample is deliberately rendered as the
              quieter number: it is the one the parameters were fitted to. */}
          <dl className="stats stats-tight">
            <div className="stat">
              <dt>Out of sample</dt>
              <dd className={oos > 0 ? "up" : oos < 0 ? "down" : ""}>
                {usd(result.out_of_sample_net_pnl)}
              </dd>
            </div>
            <div className="stat">
              <dt>In sample (fitted)</dt>
              <dd style={{ color: "var(--faint)" }}>{usd(result.in_sample_net_pnl)}</dd>
            </div>
            <div className="stat">
              <dt>Fixed params, OOS</dt>
              <dd>{baseline !== null ? usd(baseline) : "—"}</dd>
            </div>
          </dl>

          {result.optimisation_beat_doing_nothing === false && (
            <div className="warnings" style={{ marginTop: 14 }}>
              <h2>Optimising did not beat doing nothing</h2>
              <ul>
                <li>
                  Choosing {result.axes.join(" and ")} per window returned{" "}
                  {usd(result.out_of_sample_net_pnl)} out of sample, against{" "}
                  {baseline !== null ? usd(baseline) : "—"} from leaving the
                  parameters fixed. On this data the optimisation is fitting
                  noise, not finding signal.
                </li>
              </ul>
            </div>
          )}

          {result.unstable.length > 0 && (
            <div className="warnings" style={{ marginTop: 14 }}>
              <h2>Unstable parameters</h2>
              <ul>
                <li>
                  {result.unstable.join(", ")} changed optimal value in half the
                  windows or more. That is tracking noise, not signal.
                </li>
              </ul>
            </div>
          )}

          <div className="panel-bar" style={{ border: "none", padding: "12px 0" }}>
            <span>parameter stability across windows</span>
          </div>
          <div className="scroll">
            <table>
              <thead>
                <tr>
                  <th>Parameter</th>
                  <th>Verdict</th>
                  <th>Value chosen, window by window</th>
                </tr>
              </thead>
              <tbody>
                {result.stability.map((s) => (
                  <tr key={s.name}>
                    <td className="mono">{s.name}</td>
                    <td className={`mono ${s.verdict === "UNSTABLE" ? "down" : ""}`}>
                      {s.verdict}
                    </td>
                    <td className="mono">{s.values.join(" → ")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="panel-bar" style={{ border: "none", padding: "12px 0" }}>
            <span>every window</span>
            <span className="mono">
              fit {result.in_sample_days}d → test {result.out_of_sample_days}d
            </span>
          </div>
          <div className="scroll" style={{ maxHeight: 320 }}>
            <table>
              <thead>
                <tr>
                  <th>#</th>
                  <th>Tested on</th>
                  <th>Chose</th>
                  <th className="num">In sample</th>
                  <th className="num">Out of sample</th>
                  <th className="num">Fixed params</th>
                </tr>
              </thead>
              <tbody>
                {result.results.map((r) => {
                  const o = Number(r.out_of_sample_net_pnl);
                  return (
                    <tr key={r.index}>
                      <td className="mono">{r.index}</td>
                      <td className="mono">
                        {r.out_of_sample_start} → {r.out_of_sample_end}
                      </td>
                      <td className="mono">
                        {result.axes.map((a) => `${a}=${r.chosen[a]}`).join(" ")}
                      </td>
                      <td className="num mono" style={{ color: "var(--faint)" }}>
                        {usd(r.in_sample_net_pnl)}
                        <span className="stat-sub"> ({r.in_sample_trades}t)</span>
                      </td>
                      <td className={`num mono ${o > 0 ? "up" : o < 0 ? "down" : ""}`}>
                        {usd(r.out_of_sample_net_pnl)}
                        <span className="stat-sub"> ({r.out_of_sample_trades}t)</span>
                      </td>
                      <td className="num mono">
                        {r.baseline_net_pnl !== null ? usd(r.baseline_net_pnl) : "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
