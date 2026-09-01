"""Drive an Excel workbook from Kotak Neo: quotes, chain, positions, order entry.

Create the workbook, open it in Excel, then leave this running beside it:

    python scripts/excel_bridge.py --create
    python scripts/excel_bridge.py --loop

With no flags it does one refresh and exits, which is what a scheduler should call.
`--loop` polls on an interval, which is what a person watching the sheet wants.

**What each part needs.** Quotes and the chain authenticate on the consumer key
alone, so `ALGO_KOTAK_CONSUMER_KEY` (or `ALGO_KOTAK_MARKET_DATA_KEY`) is enough to
watch prices. Positions, funds and order entry need the full two-step trade session
— mobile number, UCC, TOTP seed and MPIN — and are switched on with `--trade`.
Without it the workbook still fills with market data and says so in Status.

**Sending real orders takes three separate acts**, exactly as the engine demands
(algo/config/modes.py): `--mode live`, `TRADING_MODE=live` in the environment, and
`--i-understand-this-is-real-money` on the command line. Miss any one and armed
rows are validated, marked DRY RUN, and not sent. That is the default.

**Certificates.** `requests`' bundled `certifi` CA file does not trust Kotak's
certificate chain on at least some Windows machines, while the OS trust store does.
`truststore.inject_into_ssl()` makes `requests` — and therefore the Kotak SDK —
validate against the OS store instead of skipping verification.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import truststore

truststore.inject_into_ssl()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collections.abc import Callable  # noqa: E402
from datetime import datetime  # noqa: E402

from dotenv import load_dotenv  # noqa: E402
from structlog import get_logger  # noqa: E402

from algo.config.modes import resolve_mode  # noqa: E402
from algo.core.clock import SystemClock  # noqa: E402
from algo.core.enums import Exchange, Mode  # noqa: E402
from algo.core.errors import AlgoError  # noqa: E402
from algo.data.kotak_feed import KotakChainFeed, NeoQuotesTransport  # noqa: E402
from algo.data.live import SessionWindow  # noqa: E402
from algo.excel.io import XlwingsSheetIO, create_workbook, verify_layout  # noqa: E402
from algo.excel.service import ExcelBridge  # noqa: E402
from algo.exchange.calendar import mcx_calendar  # noqa: E402
from algo.exchange.master import (  # noqa: E402
    InstrumentMaster,
    KotakMasterSource,
    fetch_master,
)
from algo.execution.kotak import (  # noqa: E402
    KotakBroker,
    NeoTransport,
    credentials_from_env,
)

log = get_logger(__name__)
IST = ZoneInfo("Asia/Kolkata")


def _chain_feed_factory(
    *,
    quotes: NeoQuotesTransport,
    master: InstrumentMaster,
    clock: SystemClock,
    session: SessionWindow,
) -> Callable[[str], KotakChainFeed]:
    """A chain feed per underlying, since the operator names it in the sheet."""

    def make(underlying: str) -> KotakChainFeed:
        return KotakChainFeed(
            transport=quotes,
            master=master,
            underlying=underlying,
            clock=clock,
            session=session,
            exchange=Exchange.MCX,
        )

    return make


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--workbook",
        type=Path,
        default=Path("data/kotak_bridge.xlsx"),
        help="the workbook to drive (default data/kotak_bridge.xlsx)",
    )
    parser.add_argument(
        "--create", action="store_true", help="scaffold the workbook and exit"
    )
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="overwrite an existing workbook — discards whatever is in its Orders sheet",
    )
    parser.add_argument(
        "--master",
        type=Path,
        default=Path("state/kotak_master.json"),
        help="Kotak instrument-master snapshot (fetched automatically if missing)",
    )
    parser.add_argument(
        "--refresh-master", action="store_true", help="re-download the instrument master first"
    )
    parser.add_argument(
        "--trade",
        action="store_true",
        help="establish the full trade session, enabling positions, funds and order entry",
    )
    parser.add_argument(
        "--mode",
        choices=[Mode.PAPER.value, Mode.LIVE.value],
        default=Mode.PAPER.value,
        help="paper (default) validates armed order rows without sending them",
    )
    parser.add_argument(
        "--i-understand-this-is-real-money",
        dest="real_money",
        action="store_true",
        help="third of the three conditions required to actually send orders",
    )
    parser.add_argument("--loop", action="store_true", help="keep refreshing")
    parser.add_argument(
        "--interval-seconds", type=float, default=5.0, help="refresh interval when --loop is set"
    )
    parser.add_argument(
        "--no-chain", action="store_true", help="skip the option chain (it is the slowest section)"
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()

    if args.create or args.recreate:
        path = create_workbook(args.workbook, overwrite=args.recreate)
        log.info("workbook created", path=str(path), next="open it in Excel, then rerun --loop")
        return

    load_dotenv()
    credentials = credentials_from_env()
    consumer_key = credentials.market_data_key or credentials.consumer_key
    if not consumer_key:
        log.error(
            "no Kotak consumer key set",
            hint="set ALGO_KOTAK_CONSUMER_KEY (or ALGO_KOTAK_MARKET_DATA_KEY); "
            "copy .env.example to .env and fill it in",
        )
        raise SystemExit(1)

    # Resolved before anything connects, so a misconfigured live run fails at the
    # command line rather than after a session is open and a row is armed.
    mode = resolve_mode(Mode(args.mode), real_money_flag=args.real_money)

    if not args.workbook.exists():
        log.error(
            "workbook not found",
            path=str(args.workbook),
            hint="run with --create first",
        )
        raise SystemExit(1)

    clock = SystemClock()
    now_ist = datetime.now(tz=IST)
    if args.refresh_master or not args.master.exists():
        log.info("fetching Kotak instrument master", path=str(args.master))
        master = fetch_master(
            KotakMasterSource(consumer_key=consumer_key), args.master, now=now_ist
        )
    else:
        master = InstrumentMaster.from_snapshot(args.master)

    quotes = NeoQuotesTransport(consumer_key)

    broker: KotakBroker | None = None
    if args.trade or mode is Mode.LIVE:
        missing = credentials.missing()
        if missing:
            log.error(
                "a trade session needs the full credential set",
                missing=list(missing),
                hint="quotes and the chain work without them; drop --trade to run read-only",
            )
            raise SystemExit(1)
        broker = KotakBroker(
            transport=NeoTransport(consumer_key),
            master=master,
            credentials=credentials,
            clock=clock,
        )
        broker.connect()

    chain_feed_for: Callable[[str], KotakChainFeed] | None = None
    if not args.no_chain:
        # `poll()` is the only method the bridge calls and it never consults the
        # session; the calendar is required by the constructor. `allow_unverified`
        # is True because this is a read-only display, not a run whose exit
        # deadlines depend on holidays being modelled (see mcx_calendar).
        chain_feed_for = _chain_feed_factory(
            quotes=quotes,
            master=master,
            clock=clock,
            session=SessionWindow(mcx_calendar(holidays_file=None, allow_unverified=True)),
        )

    io = XlwingsSheetIO.attach(args.workbook)
    verify_layout(io)

    bridge = ExcelBridge(
        io=io,
        master=master,
        quotes=quotes,
        clock=clock,
        mode=mode,
        broker=broker,
        chain_feed_for=chain_feed_for,
    )

    log.info(
        "excel bridge ready",
        workbook=str(args.workbook),
        mode=str(mode),
        trade_session=broker is not None,
        orders="live" if mode is Mode.LIVE else "dry run",
    )

    if not args.loop:
        bridge.refresh()
        return

    try:
        while True:
            bridge.refresh()
            time.sleep(args.interval_seconds)
    except KeyboardInterrupt:
        log.info("stopped")
    finally:
        if broker is not None:
            broker.disconnect()


if __name__ == "__main__":
    try:
        main()
    except AlgoError as exc:
        # The engine's own errors carry messages written for an operator; a
        # traceback would bury them.
        log.error("excel bridge failed", error=str(exc))
        raise SystemExit(1) from exc
