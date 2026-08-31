"use client";

/**
 * A two-axis parameter sweep, rendered as a heatmap.
 *
 * ## The panel argues with its own headline
 *
 * A heatmap invites reading off the greenest square and adopting it. D-131 is
 * the standing evidence that doing exactly that is how you fit noise:
 * optimising channel length per window returned a third of what leaving it
 * alone did, and the chosen value wandered across the whole grid. A sweep is a
 * *weaker* procedure than that walk-forward, not a stronger one — it has no
 * out-of-sample step at all.
 *
 * So the verdict is rendered above the grid, not below it, and an isolated peak
 * is called out as such. The best cell is outlined rather than filled with the
 * brightest colour, because the eye should land on the shape of the surface
 * first and the maximum second.
 *
 * ## Colour is diverging, anchored at zero
 *
 * Green above, clay below, intensity by magnitude against the largest absolute
 * value in the grid. Anchoring at zero rather than at the minimum matters: a
 * grid where every cell loses money must not render as a pleasant gradient
 * with a "best" green corner.
 */

import { useCallback, useEffect, useState } from "react";
import { api, loadCatalogue, usd, type Catalogue, type SweepResult } from "@/lib/api";

const VERDICT_TONE: Record<string, string> = {
  PLATEAU: "good",
  "ISOLATED PEAK": "warn",
  "NOTHING WORKS": "bad",
};

/** Diverging scale anchored at zero — never at the grid's own minimum. */
function cellStyle(value: number, scale: number): React.CSSProperties {
  if (scale === 0) return {};
  const intensity = Math.min(1, Math.abs(value) / scale);
  const colour = value >= 0 ? "var(--sage)" : "var(--clay)";
  return {
    // Alpha carries magnitude so a near-zero cell reads as neutral rather than
    // as a weak version of "good".
    background: `color-mix(in srgb, ${colour} ${(intensity * 70).toFixed(0)}%, transparent)`,
  };
}

export function Heatmap() {
  const [catalogue, setCatalogue] = useState<Catalogue | null>(null);
  const [strategy, setStrategy] = useState("breakout");
  const [rowAxis, setRowAxis] = useState("timeframe");
  const [columnAxis, setColumnAxis] = useState("stop_loss_pct");
  const [bars, setBars] = useState("5000");
  const [result, setResult] = useState<SweepResult | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    loadCatalogue()
      .then(setCatalogue)
      .catch((caught) =>
        setError(caught instanceof Error ? caught.message : String(caught)),
      );
  }, []);

  // MACD has no channel length; sweeping it would produce identical columns.
  const axes = (catalogue?.sweep_axes ?? []).filter(
    (a) => strategy === "breakout" || a.name !== "lookback",
  );

  const run = useCallback(async () => {
    setRunning(true);
    setError(null);
    try {
      setResult(
        await api.sweep({
          strategy,
          row_axis: rowAxis,
          column_axis: columnAxis,
          bars,
        }),
      );
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      setResult(null);
    } finally {
      setRunning(false);
    }
  }, [strategy, rowAxis, columnAxis, bars]);

  if (!catalogue) {
    return (
      <p className="empty">
        {error ? `Cannot load the catalogue: ${error}` : "Loading the catalogue…"}
      </p>
    );
  }

  const byKey = new Map(result?.cells.map((c) => [`${c.row}|${c.column}`, c]) ?? []);
  const scale = result
    ? Math.max(...result.cells.map((c) => Math.abs(Number(c.net_pnl))), 1)
    : 1;

  return (
    <div>
      <div className="bt-form">
        <div className="field">
          <label htmlFor="sw-strategy">Strategy</label>
          <select
            id="sw-strategy"
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
          <label htmlFor="sw-row">Rows</label>
          <select
            id="sw-row"
            value={rowAxis}
            onChange={(e) => setRowAxis(e.target.value)}
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
          <label htmlFor="sw-col">Columns</label>
          <select
            id="sw-col"
            value={columnAxis}
            onChange={(e) => setColumnAxis(e.target.value)}
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
          <label htmlFor="sw-bars">Bars of history</label>
          <input
            id="sw-bars"
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
          {running ? "Running…" : "Run sweep"}
        </button>
        <span className="chain-legend">
          One backtest per cell. No out-of-sample step — this is weaker evidence
          than the walk-forward panel, not stronger.
        </span>
      </div>

      {error && <p className="halt-note" style={{ color: "var(--clay)" }}>{error}</p>}

      {result && (
        <div className="bt-result">
          {/* The verdict sits above the grid deliberately: it is the thing that
              should be read before the eye finds the greenest square. */}
          <div className="chips" style={{ marginBottom: 12 }}>
            <span className={`chip ${VERDICT_TONE[result.robustness.verdict] ?? ""}`}>
              {result.robustness.verdict}
            </span>
            <span className="chip">
              {result.robustness.positive_cells}/{result.robustness.total_cells}{" "}
              cells profitable
            </span>
            <span className="chip">
              best {usd(result.robustness.best_net_pnl)} at {result.robustness.best_row}{" "}
              × {result.robustness.best_column}
            </span>
          </div>

          <div className="warnings" style={{ marginBottom: 16 }}>
            <h2>Before you read the best cell</h2>
            <ul>
              {result.robustness.notes.map((note) => (
                <li key={note}>{note}</li>
              ))}
            </ul>
          </div>

          <div className="scroll">
            <table className="heat">
              <thead>
                <tr>
                  <th className="mono">
                    {result.row_axis} \ {result.column_axis}
                  </th>
                  {result.column_values.map((c) => (
                    <th key={c} className="num mono">
                      {c}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {result.row_values.map((r) => (
                  <tr key={r}>
                    <td className="mono heat-axis">{r}</td>
                    {result.column_values.map((c) => {
                      const cell = byKey.get(`${r}|${c}`);
                      if (!cell) return <td key={c} className="num mono">—</td>;
                      const value = Number(cell.net_pnl);
                      const isBest =
                        r === result.robustness.best_row &&
                        c === result.robustness.best_column;
                      return (
                        <td
                          key={c}
                          className={`num mono heat-cell${isBest ? " heat-best" : ""}`}
                          style={cellStyle(value, scale)}
                          title={`${cell.trades} trades`}
                        >
                          {usd(cell.net_pnl)}
                          <span className="heat-trades">{cell.trades}t</span>
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <p className="chain-legend">
            {result.symbol} · median cell {usd(result.robustness.median_net_pnl)} ·
            server clock {result.server_offset}
          </p>
        </div>
      )}
    </div>
  );
}
