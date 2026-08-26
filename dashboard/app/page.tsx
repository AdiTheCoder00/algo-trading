"use client";

/**
 * The monitoring page.
 *
 * Ordered by what an operator needs first when something looks wrong: is the
 * engine alive and is trading halted; what is open right now; what did it do and
 * why; and what did it decline to do and why. The equity curve is important but
 * it is not the thing you check at 11pm.
 *
 * Nothing here can change trading state except the kill switch, and that only
 * records a request. Parameters are not editable from the UI by design (Q21): they
 * change through config and a restart, so every live parameter traces to a
 * committed file rather than to something someone typed into a browser once.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { EquityChart } from "@/components/EquityChart";
import { KillSwitch } from "@/components/KillSwitch";
import { KillSwitchHistory } from "@/components/KillSwitchHistory";
import { MarginUtilisation } from "@/components/MarginUtilisation";
import { OptionChain } from "@/components/OptionChain";
import { TradeLog } from "@/components/TradeLog";
import { TradeStats } from "@/components/TradeStats";
import { useAnimatedNumber } from "@/lib/useAnimatedNumber";
import {
  api,
  inr,
  shortTime,
  type ChainSnapshot,
  type EquityPoint,
  type Health,
  type KillSwitchRequest,
  type Note,
  type Position,
  type Signal,
  type Trade,
  type TradeStats as TradeStatsData,
} from "@/lib/api";

const REFRESH_MS = 15_000;

export default function Page() {
  const [health, setHealth] = useState<Health | null>(null);
  const [equity, setEquity] = useState<EquityPoint[]>([]);
  const [positions, setPositions] = useState<Position[]>([]);
  const [signals, setSignals] = useState<Signal[]>([]);
  const [notes, setNotes] = useState<Note[]>([]);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [tradeStats, setTradeStats] = useState<TradeStatsData | null>(null);
  const [chain, setChain] = useState<ChainSnapshot | null>(null);
  const [haltHistory, setHaltHistory] = useState<KillSwitchRequest[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [now, setNow] = useState<Date | null>(null);
  const [newTradeIds, setNewTradeIds] = useState<Set<string>>(new Set());
  const [newSignalIds, setNewSignalIds] = useState<Set<string>>(new Set());

  // What was on the page after the *previous* successful load - compared
  // against each new fetch to tell "arrived just now" from "was already
  // here", so the arrival flash (globals.css: .row-new) only ever plays once
  // per entry, not on every 15s poll for rows nothing changed about.
  const seenTradeIds = useRef<Set<string> | null>(null);
  const seenSignalIds = useRef<Set<string> | null>(null);

  const load = useCallback(async () => {
    try {
      const [h, e, p, s, n, t, ts, c, k] = await Promise.all([
        api.health(),
        api.equity(),
        api.positions(),
        api.signals(),
        api.notes(),
        api.trades(),
        api.tradeStats(),
        api.chain(),
        api.killSwitchHistory(),
      ]);
      setHealth(h);
      setEquity(e);
      setPositions(p);
      setSignals(s);
      setNotes(n);
      setTrades(t);
      setTradeStats(ts);
      setChain(c);
      setHaltHistory(k);
      setError(null);
      setLastUpdated(new Date());

      // The first load establishes the baseline silently - nothing in the
      // existing history should flash as "new" just because the page opened.
      const tradeIds = new Set(t.map((trade) => trade.trade_id));
      setNewTradeIds(
        seenTradeIds.current ? new Set([...tradeIds].filter((id) => !seenTradeIds.current!.has(id))) : new Set(),
      );
      seenTradeIds.current = tradeIds;

      const signalIds = new Set(s.map((signal) => signal.signal_id));
      setNewSignalIds(
        seenSignalIds.current
          ? new Set([...signalIds].filter((id) => !seenSignalIds.current!.has(id)))
          : new Set(),
      );
      seenSignalIds.current = signalIds;
    } catch (caught) {
      // Rendered, not thrown. A monitoring page that goes blank when the thing it
      // monitors goes down is exactly backwards.
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), REFRESH_MS);
    return () => clearInterval(timer);
  }, [load]);

  // A second, independent tick just for "updated Xs ago" - staleness is the
  // one thing a 15-second auto-refresh cannot make visible on its own, since
  // the page looks identical whether the last fetch was 2s or 200s ago.
  useEffect(() => {
    setNow(new Date());
    const timer = setInterval(() => setNow(new Date()), 1_000);
    return () => clearInterval(timer);
  }, []);

  const stalenessSeconds =
    lastUpdated && now ? Math.max(0, Math.round((now.getTime() - lastUpdated.getTime()) / 1000)) : null;
  const staleness =
    stalenessSeconds === null
      ? null
      : stalenessSeconds < 2
        ? "just now"
        : stalenessSeconds < 60
          ? `${stalenessSeconds}s ago`
          : `${Math.round(stalenessSeconds / 60)}m ago`;
  // Refreshes every REFRESH_MS; three missed cycles in a row is a genuine
  // signal (a fetch failing silently, or the tab throttled in the background),
  // not just normal jitter between ticks.
  const isStale = stalenessSeconds !== null && stalenessSeconds > (REFRESH_MS / 1000) * 3;

  const latest = equity.at(-1);
  const opening = equity.at(0);
  const change =
    latest && opening ? Number(latest.equity) - Number(opening.equity) : 0;

  // Tweened toward the real value on each refresh, but only ever *displayed*
  // once it has settled back onto the exact Decimal string - during the brief
  // transition itself, a float intermediate is a visual effect, not a
  // reported figure, and the distinction the rest of this file draws between
  // the two still holds once the motion stops.
  const animatedEquity = useAnimatedNumber(latest ? Number(latest.equity) : null);
  const equityText =
    latest && animatedEquity !== null
      ? Math.abs(animatedEquity - Number(latest.equity)) < 0.005
        ? inr(latest.equity)
        : inr(animatedEquity.toFixed(2))
      : "—";

  return (
    <main className="wrap">
      <header className="mast">
        <h1>GOLDM strangle engine</h1>
        <div className="chips">
          <span className={`chip ${health?.status === "ok" ? "good" : "warn"}`}>
            engine {health?.status ?? "…"}
          </span>
          <span className="chip">mode {health?.mode ?? "…"}</span>
          <span
            className={`chip ${
              health?.kill_switch?.toLowerCase().includes("tripped") ? "bad" : "good"
            }`}
          >
            kill switch {health?.kill_switch ?? "…"}
          </span>
          <span className={`chip ${health?.broker === "connected" ? "good" : "bad"}`}>
            broker {health?.broker ?? "…"}
          </span>
          {staleness && (
            <span className={`chip stale ${isStale ? "warn" : ""}`}>updated {staleness}</span>
          )}
        </div>
      </header>

      {error && (
        <div className="error-banner">
          <strong>Cannot reach the engine.</strong> {error}
          <br />
          The figures below are the last ones successfully read, and may be stale.
        </div>
      )}

      {health && health.warnings.length > 0 && (
        <div className="warnings">
          <h2>Read these before reading the numbers</h2>
          <ul>
            {health.warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </div>
      )}

      <dl className="stats">
        <div className="stat">
          <dt>Equity</dt>
          <dd>{equityText}</dd>
        </div>
        <div className="stat">
          <dt>Change</dt>
          <dd className={change > 0 ? "up" : change < 0 ? "down" : ""}>
            {latest && opening ? inr(String(change.toFixed(2))) : "—"}
          </dd>
        </div>
        <div className="stat">
          <dt>Unrealised</dt>
          <dd>{latest ? inr(latest.unrealised) : "—"}</dd>
        </div>
        <div className="stat">
          <dt>Charges</dt>
          <dd>{latest ? inr(latest.charges) : "—"}</dd>
        </div>
        <div className="stat">
          <dt>Open legs</dt>
          <dd>{positions.length}</dd>
        </div>
        <div className="stat">
          <dt>Last bar</dt>
          <dd style={{ fontSize: 14 }}>
            {health?.last_bar ? shortTime(health.last_bar) : "—"}
          </dd>
        </div>
      </dl>

      <section className="panel">
        <div className="panel-bar">
          <span>margin utilisation</span>
        </div>
        <div className="panel-in">
          <MarginUtilisation
            used={health?.detail.margin_used}
            cap={health?.detail.margin_cap}
            capPct={health?.detail.margin_cap_pct}
          />
        </div>
      </section>

      <KillSwitch killSwitchState={health?.kill_switch ?? ""} onRequested={() => void load()} />

      <section className="panel">
        <div className="panel-bar">
          <span>halt history</span>
          <span className="mono">{haltHistory.length}</span>
        </div>
        <div className="panel-in">
          <KillSwitchHistory requests={haltHistory} />
        </div>
      </section>

      <section className="panel">
        <div className="panel-bar">
          <span>equity curve</span>
          <span className="mono">{equity.length} bars</span>
        </div>
        <div className="panel-in">
          <EquityChart points={equity} />
        </div>
      </section>

      <section className="panel">
        <div className="panel-bar">
          <span>open positions</span>
          <span className="mono">{positions.length}</span>
        </div>
        <div className="panel-in scroll">
          {positions.length === 0 ? (
            <p className="empty">Flat. Nothing open.</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Instrument</th>
                  <th className="num">Lots</th>
                  <th className="num">Entry</th>
                  <th className="num">Mark</th>
                  <th className="num">Unrealised</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((position) => (
                  <tr key={position.instrument}>
                    <td className="mono">{position.instrument}</td>
                    <td className="num mono">{position.lots}</td>
                    <td className="num mono">{position.average_price}</td>
                    <td className="num mono">{position.mark ?? "—"}</td>
                    <td className="num mono">
                      {position.unrealised ? inr(position.unrealised) : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>

      <section className="panel">
        <div className="panel-bar">
          <span>option chain</span>
          <span className="mono">
            {chain ? `${new Set(chain.rows.map((r) => r.strike)).size} strikes` : "—"}
          </span>
        </div>
        <div className="panel-in">
          <OptionChain snapshot={chain} />
        </div>
      </section>

      <section className="panel">
        <div className="panel-bar">
          <span>trade statistics</span>
          <span className="mono">{tradeStats?.trades ?? 0} completed</span>
        </div>
        <div className="panel-in">
          <TradeStats stats={tradeStats} />
        </div>
      </section>

      <section className="panel">
        <div className="panel-bar">
          <span>trade log</span>
          <span className="mono">{trades.length}</span>
        </div>
        <div className="panel-in">
          <TradeLog trades={trades} newIds={newTradeIds} />
        </div>
      </section>

      <div className="grid-2">
        <section className="panel">
          <div className="panel-bar">
            <span>signals — and why</span>
            <span className="mono">{signals.length}</span>
          </div>
          <div className="panel-in">
            {signals.length === 0 ? (
              <p className="empty">No signals recorded yet.</p>
            ) : (
              signals.map((signal) => (
                <div
                  key={signal.signal_id}
                  className={newSignalIds.has(signal.signal_id) ? "row-new" : ""}
                  style={{ marginBottom: 14, padding: "4px 6px", borderRadius: 3 }}
                >
                  <div className="mono" style={{ fontSize: 12, color: "var(--brass)" }}>
                    {shortTime(signal.ts)} · {signal.action}
                  </div>
                  <div className="reason">{signal.reason}</div>
                  <div className="tags">
                    {Object.entries(signal.context)
                      .slice(0, 8)
                      .map(([key, value]) => (
                        <span className="tag" key={key}>
                          {key} {value}
                        </span>
                      ))}
                  </div>
                </div>
              ))
            )}
          </div>
        </section>

        <section className="panel">
          <div className="panel-bar">
            <span>why it did not trade</span>
            <span className="mono">{notes.length}</span>
          </div>
          <div className="panel-in">
            {notes.length === 0 ? (
              <p className="empty">Nothing declined.</p>
            ) : (
              notes.map((note, index) => (
                <div key={`${note.ts}-${index}`} style={{ marginBottom: 10 }}>
                  <div className="mono" style={{ fontSize: 12, color: "var(--faint)" }}>
                    {shortTime(note.ts)}
                  </div>
                  <div className="reason">{note.message}</div>
                </div>
              ))
            )}
          </div>
        </section>
      </div>

      <footer>
        Read-only. The only write is a kill-switch request, and the engine acts on it
        at its next bar. Parameters change through config and a restart, never from
        here, so every live setting traces to a committed file. Refreshes every{" "}
        {REFRESH_MS / 1000}s.
      </footer>
    </main>
  );
}
