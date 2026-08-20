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
  trades: (limit = 50) => get<Record<string, unknown>[]>(`trades?limit=${limit}`),
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
