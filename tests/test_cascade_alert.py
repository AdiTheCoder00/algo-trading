"""The alert tool's own wiring: dedupe, routing, and what the message says.

Whether the *rule* fired is `tests/test_signal_replay.py`'s question, and it is
answered against the strategy itself. This file is the layer above: given that
the replay found something, does the tool say it once, to the right chat, about
the right symbol - and does one dead symbol stop the rest?

Those are the failures that make a monitor useless without ever raising: a
duplicate every 75 seconds, a silent skip, a message naming the wrong side. None
of them are visible from the strategy's own tests.

The tool lives under `tools/`, so the import needs its directory on the path.
That is the same thing the script itself does at import time (for its `nifty50`
sibling), which is why doing it here is a path fix rather than a second way of
loading the module.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

_TOOL_DIR = Path(__file__).resolve().parent.parent / "tools" / "pivot_ema_telegram_alert"
if str(_TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOL_DIR))

import cascade_alert as ca  # noqa: E402
from nifty50 import NIFTY_50  # noqa: E402

from algo.backtest.signal_replay import Fired, forex_session_of  # noqa: E402
from algo.core.bar import Bar  # noqa: E402
from algo.core.enums import Exchange, Side, SignalAction  # noqa: E402
from algo.core.ids import signal_id  # noqa: E402
from algo.core.instrument import CfdId  # noqa: E402
from algo.core.signal import PriceIntent, Signal, SignalLeg  # noqa: E402
from algo.core.timeutil import iso  # noqa: E402

XAUUSD = CfdId(symbol="XAUUSD")
AT = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)


def _bar(close: str, ts: datetime = AT) -> Bar:
    value = Decimal(close)
    return Bar(
        ts=ts, timeframe=ca.TIMEFRAME, open=value, high=value, low=value,
        close=value, volume=100,
    )


def _signal(action: SignalAction, side: Side, reason: str) -> Signal:
    return Signal(
        signal_id=signal_id(
            strategy_id="test",
            params_hash="h",
            bar_close_iso=iso(AT),
            action=action.value,
            leg_keys=(f"{XAUUSD.key}:{side}",),
            config_hash="",
        ),
        strategy_id="test",
        ts=AT,
        action=action,
        legs=(SignalLeg(instrument=XAUUSD, direction=side, entry=PriceIntent.market()),),
        reason=reason,
        context={"close": "3500", "ema_10": "3972.4", "ema_200": "4039.5", "ema_20": "4020.6"},
    )


def _market(fetch: object, symbol: str = "XAUUSD") -> ca.Market:
    return ca.Market(
        symbol=symbol,
        exchange=Exchange.OTC,
        fetch=fetch,  # type: ignore[arg-type]
        session_of=forex_session_of,
    )


def _config(tmp_path: Path) -> ca.Config:
    return ca.Config(
        telegram_token="",
        telegram_chat_id="",
        mt5_symbols=("XAUUSD",),
        nse_symbols=(),
        poll_seconds=75,
        state_path=tmp_path / "state.json",
        dry_run=True,
    )


class _Series:
    """A fake feed that returns one prebuilt series and counts the calls."""

    def __init__(self, bars: list[Bar]) -> None:
        self.bars = bars
        self.calls = 0

    def fetch(self, symbol: str, count: int) -> list[Bar]:
        self.calls += 1
        return self.bars[-count:]


def _cascade_series() -> list[Bar]:
    """Enough real bars, on the DEFAULT periods, to complete a short cascade.

    Four calendar days so the pivots are drawn from a session watched end to
    end, a ranging session to give that session a range, a slow rally above R3
    and then one bar that collapses through every EMA at once - the "ya saath
    mein hi" case, which is the shortest path to a genuine entry.
    """
    start = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
    days = [
        [4000.0] * 200,
        [4000.0] * 200,
        [4000.0 + (10.0 if i % 2 else -10.0) for i in range(200)],
        [4000.0 + 0.4 * i for i in range(199)] + [3500.0],
    ]
    return [
        _bar(str(close), start + timedelta(days=day, minutes=5 * index))
        for day, closes in enumerate(days)
        for index, close in enumerate(closes)
    ]


# --------------------------------------------------------------------- the loop
def test_a_completed_cascade_produces_exactly_one_alert(tmp_path: Path) -> None:
    feed = _Series(_cascade_series())
    monitor = ca.Monitor(
        config=_config(tmp_path),
        markets=[_market(feed.fetch)],
        notifier=None,
        state=ca.StateStore(tmp_path / "state.json"),
    )
    sent = monitor.poll_once()
    assert len(sent) == 1
    market, fired = sent[0]
    assert market.symbol == "XAUUSD"
    assert fired.is_entry and fired.side is Side.SELL


def test_the_same_bar_is_never_alerted_twice(tmp_path: Path) -> None:
    """The failure that makes a monitor unusable: one signal, every 75 seconds.

    The bars do not change between polls - which is exactly the real case, since
    a poll happens several times per five-minute bar.
    """
    feed = _Series(_cascade_series())
    monitor = ca.Monitor(
        config=_config(tmp_path),
        markets=[_market(feed.fetch)],
        notifier=None,
        state=ca.StateStore(tmp_path / "state.json"),
    )
    assert len(monitor.poll_once()) == 1
    assert monitor.poll_once() == []
    assert monitor.poll_once() == []
    # Every poll still fetches - there is no way to learn whether a new bar has
    # closed without asking. What the dedupe saves is the replay and the
    # message, which is the part that would reach a phone.
    assert feed.calls == 3


def test_the_dedupe_survives_a_restart(tmp_path: Path) -> None:
    """A fresh Monitor reading the same state file must stay quiet."""
    state_path = tmp_path / "state.json"
    feed = _Series(_cascade_series())
    first = ca.Monitor(
        config=_config(tmp_path),
        markets=[_market(feed.fetch)],
        notifier=None,
        state=ca.StateStore(state_path),
    )
    assert len(first.poll_once()) == 1

    restarted = ca.Monitor(
        config=_config(tmp_path),
        markets=[_market(feed.fetch)],
        notifier=None,
        state=ca.StateStore(state_path),
    )
    assert restarted.poll_once() == []


def test_one_dead_symbol_does_not_stop_the_others(tmp_path: Path) -> None:
    """An MT5 terminal that cannot select a symbol is the common case: a
    misspelled volatility index, or a market that is closed. It must cost that
    symbol and nothing else."""

    def broken(symbol: str, count: int) -> list[Bar]:
        raise RuntimeError("MT5 cannot select VOL100")

    feed = _Series(_cascade_series())
    monitor = ca.Monitor(
        config=_config(tmp_path),
        markets=[_market(broken, symbol="VOL100"), _market(feed.fetch)],
        notifier=None,
        state=ca.StateStore(tmp_path / "state.json"),
    )
    sent = monitor.poll_once()
    assert [m.symbol for m, _ in sent] == ["XAUUSD"]


def test_too_few_bars_is_a_skip_not_a_signal(tmp_path: Path) -> None:
    """Below the warmup the 200 EMA is mostly its own seed. Alerting there
    would fire on an indicator that does not exist yet."""
    feed = _Series(_cascade_series()[-100:])
    monitor = ca.Monitor(
        config=_config(tmp_path),
        markets=[_market(feed.fetch)],
        notifier=None,
        state=ca.StateStore(tmp_path / "state.json"),
    )
    assert monitor.poll_once() == []


# ------------------------------------------------------------------ the message
def test_the_message_names_the_side_and_the_bar() -> None:
    fired = Fired(
        bar=_bar("3500"),
        signal=_signal(SignalAction.OPEN, Side.SELL, "pivot/EMA cascade: below the 200 EMA"),
    )
    text = ca.format_message(_market(None), fired)
    assert "XAUUSD" in text
    assert "ENTRY" in text and "SELL" in text
    assert "3500" in text
    assert "not advice" in text, "every message has to say what it is not"


def test_a_closing_buy_is_described_as_closing_a_short() -> None:
    """`Fired.side` is the side that FLATTENS on an exit, so a closing BUY ends
    a short. Printing that word raw would tell the reader to buy."""
    fired = Fired(
        bar=_bar("4085"),
        signal=_signal(SignalAction.CLOSE, Side.BUY, "closed back above the 10 and 20 EMA"),
    )
    text = ca.format_message(_market(None), fired)
    assert "EXIT" in text
    assert "CLOSE SHORT" in text
    assert "ENTRY" not in text


def test_the_emas_are_listed_by_period_not_by_string() -> None:
    """Sorted as strings, the 200 EMA lands between the 10 and the 20."""
    fired = Fired(bar=_bar("3500"), signal=_signal(SignalAction.OPEN, Side.SELL, "x"))
    text = ca.format_message(_market(None), fired)
    assert "10: 3972.4 · 20: 4020.6 · 200: 4039.5" in text


# ------------------------------------------------------------------- watch list
def test_the_nifty_list_is_fifty_distinct_cash_symbols() -> None:
    assert len(NIFTY_50) == 50
    assert len(set(NIFTY_50)) == 50
    assert all(symbol.endswith("-EQ") for symbol in NIFTY_50)


def test_mt5_and_nse_symbols_route_to_different_exchanges() -> None:
    config = ca.Config(
        telegram_token="t",
        telegram_chat_id="c",
        mt5_symbols=("XAUUSD", "BTCUSD"),
        nse_symbols=("RELIANCE-EQ",),
        poll_seconds=75,
        state_path=Path("unused.json"),
        dry_run=True,
    )
    routed = {m.symbol: m.exchange for m in ca.build_markets(config)}
    assert routed == {
        "XAUUSD": Exchange.OTC,
        "BTCUSD": Exchange.OTC,
        "RELIANCE-EQ": Exchange.NSE,
    }


def test_an_unreadable_state_file_is_ignored_rather_than_fatal(tmp_path: Path) -> None:
    """A truncated write must not stop the monitor starting - the worst it can
    cost is one duplicate message."""
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    store = ca.StateStore(path)
    assert not store.already_sent("XAUUSD", AT)
    store.record("XAUUSD", AT)
    assert ca.StateStore(path).already_sent("XAUUSD", AT)


@pytest.mark.parametrize("timeframe_minutes", [5])
def test_the_timeframe_is_the_one_the_rule_names(timeframe_minutes: int) -> None:
    assert ca.TIMEFRAME.minutes == timeframe_minutes
    #: 285 for the 200 EMA, the bar a crossing compares to, and one M5 session.
    assert ca.WARMUP_BARS == 286 + 288
