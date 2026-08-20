"use client";

/**
 * The kill-switch button. Brief §2.2.
 *
 * Three deliberate frictions, none of them accidental:
 *
 * **A reason is required.** The field cannot be blank, and the reason is stored
 * with the request. Three weeks later "why did trading stop on the 14th" needs an
 * answer, and the only moment anyone knows it is the moment they click.
 *
 * **Flatten is off by default and asks again.** Halting stops new orders;
 * flattening market-closes a short strangle, possibly into the move that tripped
 * the limit, which can cost more than the breach did (D-012). Ticking it is a
 * second, separate decision.
 *
 * **It reports "requested", never "halted".** The API returns 202 — the engine
 * acts on its next bar. Showing "halted" the instant the request returns would be
 * a lie for as long as that takes, and it is exactly the moment an operator most
 * needs to be told the truth.
 */

import { useState } from "react";
import { api } from "@/lib/api";

interface Props {
  killSwitchState: string;
  onRequested: () => void;
}

export function KillSwitch({ killSwitchState, onRequested }: Props) {
  const [reason, setReason] = useState("");
  const [flatten, setFlatten] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const alreadyTripped = killSwitchState.toLowerCase().includes("tripped");

  async function submit() {
    if (!reason.trim()) return;
    if (flatten && !window.confirm(
      "Flatten will market-close every open position, including a short strangle " +
        "that may be mid-move. Halting alone stops new orders and leaves positions " +
        "untouched.\n\nFlatten anyway?",
    )) {
      return;
    }

    setBusy(true);
    setError(null);
    setNote(null);
    try {
      const result = await api.requestHalt(reason.trim(), flatten);
      setNote(result.note);
      setReason("");
      setFlatten(false);
      onRequested();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel halt">
      <div className="panel-bar">
        <span>kill switch</span>
        <span className="mono">{alreadyTripped ? "TRIPPED" : killSwitchState || "unknown"}</span>
      </div>
      <div className="panel-in">
        <div className="halt-body">
          <div className="field">
            <label htmlFor="halt-reason">Reason (required)</label>
            <input
              id="halt-reason"
              type="text"
              value={reason}
              placeholder="e.g. stepping away from the desk"
              onChange={(event) => setReason(event.target.value)}
              disabled={busy}
            />
          </div>
          <label className="check">
            <input
              type="checkbox"
              checked={flatten}
              onChange={(event) => setFlatten(event.target.checked)}
              disabled={busy}
            />
            also flatten positions
          </label>
          <button
            className="halt-button"
            onClick={submit}
            disabled={busy || !reason.trim()}
            type="button"
          >
            {busy ? "Requesting…" : "Halt trading"}
          </button>
        </div>

        <p className="halt-note">
          Halting stops new orders. It does not close what is open — flattening a
          short strangle into the move that tripped the limit can cost more than the
          breach. Clearing a halt is deliberately not possible from here; use{" "}
          <code>algo killswitch --reset</code> after looking at why it tripped.
        </p>

        {note && <p className="halt-note" style={{ color: "var(--sage)" }}>{note}</p>}
        {error && <p className="halt-note" style={{ color: "var(--clay)" }}>{error}</p>}
      </div>
    </section>
  );
}
