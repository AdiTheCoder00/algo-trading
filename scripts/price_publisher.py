"""Publish XAUUSD and GOLDM as one small JSON document, for a phone widget to read.

A home-screen widget cannot talk to the brokers directly, and the reason is worth
stating plainly rather than discovering later:

* **Credentials cannot ship in an APK.** `.env` holds TOTP seeds and MPINs for
  both Angel One and Kotak. Those are order-placement credentials, not read-only
  quote keys, and an APK is trivially decompiled. Anything embedded in an app on
  a phone should be treated as published.
* **The Kotak consumer key is IP-whitelisted** (see `.env.example`), so it would
  not authenticate from a mobile network even if embedding it were acceptable.

So the machine that already has the credentials keeps them, and exposes exactly
one read-only artefact: last-traded prices. The phone reads that. No key ever
leaves this box, and the worst case if the endpoint leaks is that somebody learns
the gold price.

## The two legs are not symmetrical

`XAUUSD` comes from a public spot feed and needs nothing from this repo — the
widget could fetch it directly if that were the only ask. `GOLDM` is the reason a
relay exists at all: MCX has no free public quote API, so it comes through the
same `NeoQuotesTransport` path `mcx_live_to_excel.py` already uses.

That asymmetry has an operational consequence: **XAUUSD stays live even when this
machine is off; GOLDM does not.** `stale` marks it rather than serving a
last-known number as if it were current, because a stale price shown in a big
bold font is worse than no price.

## Serving

With no flags this writes `state/prices.json` and exits — drive it from Task
Scheduler alongside `run_mcx_poll.bat`. `--serve` additionally runs a small HTTP
server so a phone on the same network (or through a tunnel) can poll it.

`--token` requires callers to send `Authorization: Bearer <token>`. It is not
meaningful security on a LAN, but it stops an open tunnel from being indexed.
Bind stays on 0.0.0.0 only because a phone is by definition another host; use a
tunnel rather than a port-forward if this needs to work off the home network.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import truststore

truststore.inject_into_ssl()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402
from structlog import get_logger  # noqa: E402

from algo.core.enums import Exchange  # noqa: E402
from algo.data.kotak_feed import NeoQuotesTransport  # noqa: E402
from algo.exchange.master import (  # noqa: E402
    InstrumentMaster,
    KotakMasterSource,
    fetch_master,
)

log = get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_OUT = Path("state/prices.json")
DEFAULT_MASTER = Path("state/kotak_master.json")
DEFAULT_SESSION = Path("state/spot_session.json")
WIDGET_PAGE = Path(__file__).resolve().parent / "price_widget.html"

# How old a GOLDM quote may be before the widget should stop trusting it. MCX
# ticks continuously in session, so anything beyond a couple of minutes means the
# poll failed or the market is shut, and either way the number is not "live".
GOLDM_STALE_AFTER_S = 150.0


# --------------------------------------------------------------------- XAUUSD


def _http_json(url: str, *, timeout: float) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "algo-price-publisher/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _xau_gold_api(timeout: float) -> dict[str, Any]:
    """Primary spot source: free, keyless, updates every few seconds."""
    payload = _http_json("https://api.gold-api.com/price/XAU", timeout=timeout)
    return {
        "price": float(payload["price"]),
        "source": "gold-api.com",
        "quote_time": payload.get("updatedAt"),
    }


def _comex_fallback(timeout: float) -> dict[str, Any]:
    """COMEX front-month, used only when the spot feed is unreachable.

    It is *not* used for the day change any more. COMEX carries a basis of tens of
    dollars over spot and, more importantly, keeps its own session: quoting its
    percentage against a spot price produced a visibly wrong figure (-0.31% when
    spot was -0.14%) and a day range spot sat outside. Spot's own session is
    tracked in `_SpotSession` instead.
    """
    payload = _http_json(
        "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?interval=1m&range=1d",
        timeout=timeout,
    )
    meta = payload["chart"]["result"][0]["meta"]
    return {
        "price": float(meta["regularMarketPrice"]),
        "source": f"yahoo:GC=F ({meta.get('shortName')}) — futures, not spot",
    }


# Spot gold trades nearly around the clock and rolls at 17:00 New York, which is
# the close every quote vendor reports "previous close" against. Anchoring to
# midnight anywhere else would put the change out by a whole evening's move.
_SESSION_TZ = ZoneInfo("America/New_York")
_SESSION_ROLL_HOUR = 17


def _session_date(now: datetime) -> str:
    local = now.astimezone(_SESSION_TZ)
    if local.hour >= _SESSION_ROLL_HOUR:
        local = local + timedelta(days=1)
    return local.date().isoformat()


class _SpotSession:
    """Track spot's own open/high/low and previous close.

    No free spot feed publishes OHLC — gold-api returns a bare price, and Yahoo
    has no spot symbol at all (every XAUUSD variant 404s). So the session is
    accumulated here, from the poll loop that is already running.

    `seed_prev_close` exists because the tracker is otherwise blind until it has
    lived through one 17:00 rollover; seeding makes the change correct from the
    first poll and is overwritten by a real observed close at the next roll.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.state: dict[str, Any] = {}
        if path.exists():
            try:
                self.state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.state = {}

    def update(
        self,
        price: float,
        *,
        now: datetime,
        seed_prev_close: float | None = None,
        seed_high: float | None = None,
        seed_low: float | None = None,
    ) -> None:
        today = _session_date(now)
        if self.state.get("session") != today:
            # The high/low seeds matter as much as the close: a range accumulated
            # from the moment the publisher started is narrower than the session's
            # real range, and a range the current price sits inside looks correct
            # while being wrong. Seeding makes it right immediately.
            self.state = {
                "session": today,
                # The last price of the session that just ended is this session's
                # previous close. On a cold start there is none, so fall back to
                # the seed, then to the current price (change reads 0.00%, which
                # is honest -- better than inventing a move).
                "prev_close": self.state.get("last") or seed_prev_close or price,
                "open": price,
                "high": max(price, seed_high or price),
                "low": min(price, seed_low or price),
            }
        else:
            self.state["high"] = max(self.state.get("high", price), price)
            self.state["low"] = min(self.state.get("low", price), price)
        self.state["last"] = price
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            scratch = self.path.with_suffix(".tmp")
            scratch.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
            scratch.replace(self.path)
        except OSError as exc:
            log.warning("could not persist spot session", error=str(exc)[:120])

    def decorate(self, leg: dict[str, Any]) -> dict[str, Any]:
        prev = self.state.get("prev_close")
        price = leg.get("price")
        if prev and price:
            leg["previous_close"] = round(prev, 2)
            leg["change"] = round(price - prev, 2)
            leg["per_change"] = round((price / prev - 1.0) * 100.0, 2)
        leg["day_high"] = self.state.get("high")
        leg["day_low"] = self.state.get("low")
        leg["session"] = self.state.get("session")
        return leg


class _ComexAnchor:
    """Fill the gaps between spot updates using COMEX movement.

    The free spot feed publishes a new price every 30 seconds, on the :20 and
    :50. Polling it faster just returns the same number, so the widget looked
    frozen for half a minute at a time. COMEX front-month ticks several times a
    minute, but sits tens of dollars above spot and cannot be shown as XAUUSD.

    So spot stays the anchor and COMEX supplies the shape between anchors:

        basis      = comex - spot        (recomputed each time spot moves)
        estimate   = comex_now - basis

    The only error is basis drift within a 30-second window, which is cents. The
    published price is still spot-accurate; it simply stops being a staircase.

    COMEX is fetched on its own throttle rather than every loop -- Yahoo would
    rate-limit a 2-second cadence, and it gains nothing over ~4s.
    """

    def __init__(self, *, min_interval_s: float = 4.0) -> None:
        self.min_interval_s = min_interval_s
        self._price: float | None = None
        self._fetched_at: float = 0.0
        self._basis: float | None = None
        self._anchor_quote_time: str | None = None

    def poll(self, timeout: float, *, now: float) -> float | None:
        if self._price is not None and (now - self._fetched_at) < self.min_interval_s:
            return self._price
        try:
            self._price = _comex_fallback(timeout)["price"]
            self._fetched_at = now
        except (urllib.error.URLError, OSError, KeyError, ValueError, TypeError):
            pass  # keep the last value; a missed COMEX poll is not fatal
        return self._price

    def refresh_basis(self, spot: float, quote_time: str | None) -> None:
        """Re-anchor when the spot feed publishes a genuinely new quote."""
        if quote_time is not None and quote_time == self._anchor_quote_time:
            return
        self._anchor_quote_time = quote_time
        if self._price is not None:
            self._basis = self._price - spot

    def estimate(self, spot: float) -> tuple[float, bool]:
        """Return (price, interpolated). Falls back to raw spot when unusable."""
        if self._price is None or self._basis is None:
            return spot, False
        est = self._price - self._basis
        # A basis that has drifted absurdly means something is wrong (a contract
        # roll, a bad print); trust spot rather than publish a fabricated number.
        if abs(est - spot) > 15.0:
            return spot, False
        return round(est, 2), True


def fetch_xauusd(
    session: "_SpotSession | None" = None,
    *,
    timeout: float = 10.0,
    seed_prev_close: float | None = None,
    seed_high: float | None = None,
    seed_low: float | None = None,
    anchor: "_ComexAnchor | None" = None,
) -> dict[str, Any]:
    """Spot price, with change and range derived from spot's own session."""
    errors: list[str] = []
    try:
        leg = _xau_gold_api(timeout)
    except (urllib.error.URLError, OSError, KeyError, ValueError, TypeError) as exc:
        errors.append(f"gold_api: {type(exc).__name__}")
        try:
            leg = _comex_fallback(timeout)
        except (urllib.error.URLError, OSError, KeyError, ValueError, TypeError) as exc2:
            errors.append(f"comex: {type(exc2).__name__}")
            log.warning("all XAUUSD sources failed", errors=errors)
            return {"price": None, "source": None, "stale": True, "errors": errors}

    leg["stale"] = False

    # Smooth the 30-second spot staircase using COMEX movement.
    if anchor is not None and leg.get("price"):
        anchor.poll(timeout, now=time.monotonic())
        anchor.refresh_basis(leg["price"], leg.get("quote_time"))
        estimated, interpolated = anchor.estimate(leg["price"])
        if interpolated:
            leg["spot_price"] = leg["price"]
            leg["price"] = estimated
            leg["interpolated"] = True

    if session is not None and leg.get("price"):
        session.update(
            leg["price"],
            now=datetime.now(UTC),
            seed_prev_close=seed_prev_close,
            seed_high=seed_high,
            seed_low=seed_low,
        )
        leg = session.decorate(leg)
    if errors:
        leg["errors"] = errors
    return leg


# ---------------------------------------------------------------------- GOLDM


def _payload_for(payloads: list[dict[str, Any]], token: str) -> dict[str, Any] | None:
    """Match a quote payload to its token, tolerating the `SEGMENT|token` prefix
    the API sometimes writes into `exchange_token`."""
    for entry in payloads:
        exchange_token = str(entry.get("exchange_token") or "")
        if exchange_token == token or exchange_token.endswith(f"|{token}"):
            return entry
    return None


def _num(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def fetch_goldm(
    *,
    consumer_key: str,
    master_path: Path,
    symbol: str = "GOLDM",
    refresh_master: bool = False,
) -> dict[str, Any]:
    """Front-month GOLDM future, via the same quote path as the Excel poller."""
    now_ist = datetime.now(IST)
    if refresh_master or not master_path.exists():
        log.info("fetching Kotak instrument master", path=str(master_path))
        master = fetch_master(KotakMasterSource(consumer_key=consumer_key), master_path, now=now_ist)
    else:
        master = InstrumentMaster.from_snapshot(master_path)

    futures = master.future_rows(symbol, Exchange.MCX)
    if not futures:
        return {"price": None, "stale": True, "error": "no futures contract in master"}
    row = min(futures, key=lambda r: r.expiry)

    payload = NeoQuotesTransport(consumer_key).quotes(
        [{"exchange_segment": "mcx_fo", "instrument_token": row.symboltoken}]
    )
    # The transport signals failure by returning a dict and success by returning a
    # list — see the same check in scripts/mcx_live_to_excel.py.
    if isinstance(payload, dict):
        return {
            "price": None,
            "stale": True,
            "error": str(payload.get("Error") or payload.get("error") or payload)[:200],
        }
    if not isinstance(payload, list):
        return {"price": None, "stale": True, "error": f"unreadable payload: {type(payload).__name__}"}

    quote = _payload_for(
        [entry for entry in payload if isinstance(entry, dict)], row.symboltoken
    )
    if quote is None:
        return {"price": None, "stale": True, "error": f"no quote for token {row.symboltoken}"}

    ltp = _num(quote.get("ltp") or quote.get("last_traded_price"))
    change = _num(quote.get("change"))
    per_change = _num(quote.get("per_change"))
    # `lstup_time` is the exchange's own last-update epoch. It is the only honest
    # basis for staleness: our poll being recent says nothing about whether the
    # exchange has printed a new tick.
    exchange_time = _num(quote.get("lstup_time"))
    return {
        "price": float(ltp) if ltp is not None else None,
        "change": float(change) if change is not None else None,
        "per_change": float(per_change) if per_change is not None else None,
        "tradingsymbol": row.tradingsymbol,
        "expiry": row.expiry.isoformat(),
        "open_interest": int(_num(quote.get("open_int")) or 0),
        "volume": int(_num(quote.get("last_volume") or quote.get("volume")) or 0),
        "exchange_time": (
            datetime.fromtimestamp(int(exchange_time), UTC).isoformat().replace("+00:00", "Z")
            if exchange_time
            else None
        ),
        "stale": ltp is None,
    }


# -------------------------------------------------------------------- publish


def build_document(
    *,
    consumer_key: str | None,
    master_path: Path,
    symbol: str,
    refresh_master: bool,
    session: "_SpotSession | None" = None,
    seed_prev_close: float | None = None,
    seed_high: float | None = None,
    seed_low: float | None = None,
    anchor: "_ComexAnchor | None" = None,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    xau = fetch_xauusd(
        session,
        seed_prev_close=seed_prev_close,
        seed_high=seed_high,
        seed_low=seed_low,
        anchor=anchor,
    )

    if consumer_key:
        try:
            goldm = fetch_goldm(
                consumer_key=consumer_key,
                master_path=master_path,
                symbol=symbol,
                refresh_master=refresh_master,
            )
        except Exception as exc:  # noqa: BLE001 — a widget feed must never crash the loop
            log.warning("GOLDM poll failed", error=str(exc)[:200])
            goldm = {"price": None, "stale": True, "error": f"{type(exc).__name__}: {exc}"[:200]}
    else:
        goldm = {"price": None, "stale": True, "error": "no Kotak consumer key in environment"}

    return {
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "generated_at_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
        "xauusd": xau,
        "goldm": goldm,
    }


def write_document(document: dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename: the HTTP thread and any reader always see a whole file.
    scratch = out_path.with_suffix(out_path.suffix + ".tmp")
    scratch.write_text(json.dumps(document, indent=2), encoding="utf-8")
    scratch.replace(out_path)


def mark_staleness(document: dict[str, Any]) -> dict[str, Any]:
    """Re-check age at serve time, so a cached file is never served as fresh."""
    try:
        age = (
            datetime.now(UTC) - datetime.fromisoformat(document["generated_at"].replace("Z", "+00:00"))
        ).total_seconds()
    except (KeyError, ValueError, TypeError):
        return document
    document["age_seconds"] = round(age, 1)
    if age > GOLDM_STALE_AFTER_S:
        document.setdefault("goldm", {})["stale"] = True
    return document


# ----------------------------------------------------------------------- serve


def _handler_class(out_path: Path, token: str | None) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _reject(self, code: int, message: str) -> None:
            body = json.dumps({"error": message}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorised(self) -> bool:
            if not token:
                return True
            if self.headers.get("Authorization") == f"Bearer {token}":
                return True
            # A phone browser cannot set a header on a top-level navigation, so the
            # token is also accepted as ?k= — which does put it in the URL. That is
            # acceptable only because the payload is a public commodity price; do
            # not copy this pattern for anything that identifies you.
            query = self.path.partition("?")[2]
            return f"k={token}" in query

        def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's interface
            route = self.path.split("?")[0]
            if route not in ("/", "/index.html", "/prices", "/prices.json"):
                self._reject(404, "not found")
                return
            if not self._authorised():
                self._reject(401, "unauthorized")
                return

            if route in ("/", "/index.html"):
                try:
                    page = WIDGET_PAGE.read_bytes()
                except OSError as exc:
                    self._reject(503, f"page missing: {type(exc).__name__}")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
                return
            try:
                document = mark_staleness(json.loads(out_path.read_text(encoding="utf-8")))
            except (OSError, ValueError) as exc:
                self._reject(503, f"no document yet: {type(exc).__name__}")
                return
            body = json.dumps(document).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("http", request=fmt % args)

    return Handler


# ------------------------------------------------------------------------ cli


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="JSON output path")
    parser.add_argument("--master", type=Path, default=DEFAULT_MASTER, help="Kotak instrument master")
    parser.add_argument("--symbol", default="GOLDM", help="MCX underlying (default GOLDM)")
    parser.add_argument("--refresh-master", action="store_true", help="Re-download the master first")
    parser.add_argument("--loop", type=float, metavar="SECONDS", help="Poll forever on this interval")
    parser.add_argument("--serve", action="store_true", help="Also serve the document over HTTP")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address for --serve")
    parser.add_argument("--port", type=int, default=8787, help="Bind port for --serve")
    parser.add_argument("--token", help="Require 'Authorization: Bearer <token>'")
    parser.add_argument("--xau-only", action="store_true", help="Skip GOLDM (no broker key needed)")
    parser.add_argument(
        "--session", type=Path, default=DEFAULT_SESSION, help="Spot session state file"
    )
    parser.add_argument("--seed-day-high", type=float, help="Seed the session high")
    parser.add_argument("--seed-day-low", type=float, help="Seed the session low")
    parser.add_argument(
        "--seed-prev-close",
        type=float,
        help=(
            "Spot previous close to use until the tracker has seen a real 17:00 NY "
            "rollover. Only affects a cold start; a genuine observed close replaces it."
        ),
    )
    args = parser.parse_args()

    load_dotenv()
    import os

    consumer_key = None
    if not args.xau_only:
        consumer_key = os.environ.get("ALGO_KOTAK_MARKET_DATA_KEY") or os.environ.get(
            "ALGO_KOTAK_CONSUMER_KEY"
        )
        if not consumer_key:
            log.warning("no Kotak consumer key; GOLDM will publish as stale")

    session = _SpotSession(args.session)
    anchor = _ComexAnchor()

    def poll(refresh: bool = False) -> dict[str, Any]:
        document = build_document(
            consumer_key=consumer_key,
            master_path=args.master,
            symbol=args.symbol,
            refresh_master=refresh,
            session=session,
            seed_prev_close=args.seed_prev_close,
            seed_high=args.seed_day_high,
            seed_low=args.seed_day_low,
            anchor=anchor,
        )
        write_document(document, args.out)
        log.info(
            "published",
            xauusd=document["xauusd"].get("price"),
            goldm=document["goldm"].get("price"),
            out=str(args.out),
        )
        return document

    poll(args.refresh_master)

    server = None
    if args.serve:
        server = ThreadingHTTPServer((args.host, args.port), _handler_class(args.out, args.token))
        server.daemon_threads = True
        import threading

        threading.Thread(target=server.serve_forever, daemon=True).start()
        log.info("serving", url=f"http://{args.host}:{args.port}/prices.json", token_required=bool(args.token))

    if args.loop:
        try:
            while True:
                time.sleep(args.loop)
                poll()
        except KeyboardInterrupt:
            log.info("stopped")
    elif args.serve:
        log.info("serving without --loop; document will not refresh. Ctrl-C to stop.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            log.info("stopped")

    if server is not None:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
