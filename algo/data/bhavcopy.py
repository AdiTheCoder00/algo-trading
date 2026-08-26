"""MCX bhavcopy — daily, strike-wise, and it covers expired contracts.

This exists because of a hard limit on the broker feed. Angel One state plainly
that **"data of expired contracts is not stored"**: an expired contract drops out
of the instrument master, so there is no `symboltoken` to ask for history with.
That makes SmartAPI excellent for GOLDM *futures* bars and useless for any option
cycle that has already settled — which is every cycle worth backtesting.

The bhavcopy is contract-wise end-of-day: open, high, low, close, volume, value
and open interest for every contract that traded, **including expired ones**, back
to 2016 through third-party archives. It turns "wait two and a half years for the
recorder" into "about a hundred monthly cycles, today".

## What this data is not

Stated here rather than in a footnote, because every number derived from it
inherits these:

* **Daily.** There is no 09:30 bar. An entry at the 09:30 close has to be proxied
  by the day's open, which is an approximation of the strategy, not the strategy.
* **No bid or ask.** The spread stays an assumption. On a thin GOLDM option book
  the spread *is* the dominant cost, so this data cannot settle the question that
  matters most — only the recorder can.
* **Stops can only be judged against the daily high and low**, which is why
  `assume_spread` marks what it touches and why the engine keeps reporting the
  spread as modelled.

So a bhavcopy backtest is a **shape test**: has this strategy ever worked, across
many real cycles. It is not a fill-accurate one. Shape over a hundred cycles still
beats precision over none.

## The column mapping — now verified for one layout

`MCX_DEFAULT_COLUMNS` was written blind, because MCX serves the file through a
browser flow behind bot protection. A real "commodity wise" export has since been
read (D-105) and the guess was **almost** right: only `Volume` and
`Open Interest` were wrong, both carrying a unit suffix in the real file.
`MCX_COMMODITY_WISE_COLUMNS` records the checked layout. `MCX_DEFAULT_COLUMNS`
remains **unverified** and is kept only because it may still match the plain CSV
bhavcopy, which has never been seen — do not trust anything derived through it
until a real CSV confirms it.

`parse_rows` tries every known layout and, when none fit, raises with the columns
it actually found next to each candidate — so correcting it stays a one-line
remap rather than a debugging session.

## The file is not always a CSV, or an Excel workbook

The "commodity wise" download arrives with an `.xls` extension and is neither: it
is an **HTML `<table>`**. Opening it with a CSV reader yields one meaningless
column, and with an Excel reader an error. `parse_rows` sniffs the content and
reads either form, because the extension is not evidence of anything.
"""

from __future__ import annotations

import csv
from collections.abc import Callable, Iterable, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from algo.core.chain import ChainRow, OptionChainSnapshot
from algo.core.enums import Exchange, Right
from algo.core.errors import DataError
from algo.core.instrument import FutureId, OptionId
from algo.core.quote import Quote
from algo.core.timeutil import ist_to_utc

#: Instrument-name values that mark a row as a futures contract.
FUTURES_KINDS = frozenset({"FUTCOM", "FUTIDX", "FUTBAS"})
#: ...and an option on one.
OPTION_KINDS = frozenset({"OPTFUT", "OPTCOM"})


class BhavcopyColumns(BaseModel):
    """Which CSV column holds which field.

    Data, not code, so a format change is a config edit. Every value is the header
    text as it appears in the file.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    trade_date: str
    instrument_kind: str
    symbol: str
    expiry: str
    strike: str
    option_type: str
    open: str
    high: str
    low: str
    close: str
    volume: str
    open_interest: str

    def required(self) -> tuple[str, ...]:
        return tuple(getattr(self, name) for name in type(self).model_fields)


#: UNVERIFIED — see the module docstring. Confirm against a real file before
#: trusting anything derived from it.
MCX_DEFAULT_COLUMNS = BhavcopyColumns(
    trade_date="Date",
    instrument_kind="Instrument Name",
    symbol="Symbol",
    expiry="Expiry Date",
    strike="Strike Price",
    option_type="Option Type",
    open="Open",
    high="High",
    low="Low",
    close="Close",
    volume="Volume",
    open_interest="Open Interest",
)

#: VERIFIED against a real MCX "commodity wise" export, 2026-08-27 (D-105).
#: Differs from the blind guess above in exactly two places: both the volume and
#: open-interest headers carry a unit suffix.
MCX_COMMODITY_WISE_COLUMNS = MCX_DEFAULT_COLUMNS.model_copy(
    update={"volume": "Volume(Lots)", "open_interest": "Open Interest(Lots)"}
)

#: Tried in order by `parse_rows`. The verified layout goes first so a real file
#: matches on the first attempt and the unverified guess stays a fallback.
KNOWN_LAYOUTS: tuple[BhavcopyColumns, ...] = (
    MCX_COMMODITY_WISE_COLUMNS,
    MCX_DEFAULT_COLUMNS,
)

#: Extensions a bhavcopy actually arrives with. `.xls` is in here because the
#: commodity-wise export uses it for HTML - the content is sniffed either way.
DATA_FILE_PATTERNS = ("*.csv", "*.xls")

#: Date formats seen in Indian exchange files. Tried in order.
#: `%d %b %Y` is the commodity-wise trade date ("29 Jul 2026"); `%d%b%Y` is the
#: expiry in the same file ("29JUL2026").
DATE_FORMATS = ("%d-%b-%Y", "%d%b%Y", "%d %b %Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y")


class BhavcopyRow(BaseModel):
    """One contract's end-of-day summary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trade_date: date
    symbol: str
    expiry: date
    is_option: bool
    strike: Decimal | None
    right: Right | None
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    open_interest: int

    @property
    def traded(self) -> bool:
        """Whether anything actually changed hands.

        The closest thing end-of-day data has to a tradeability test. A strike
        with zero volume was *listed*, not *available* — and on a thin option
        ladder that distinction is the whole question.
        """
        return self.volume > 0


class _TableRows(HTMLParser):
    """Pull one `<table>` out of the HTML that MCX serves as an `.xls`.

    Deliberately stdlib: the project carries no HTML dependency, and this is a
    single flat table with no nesting to resolve. Cells are taken in document
    order and zipped against the header row, which is the same contract
    `csv.DictReader` offers — so `_to_row` cannot tell the two sources apart.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.header: list[str] = []
        self.rows: list[list[str]] = []
        self._cell: list[str] | None = None
        self._row: list[str] = []
        self._is_header = False

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag == "tr":
            self._row = []
            self._is_header = False
        elif tag in ("td", "th"):
            self._cell = []
            self._is_header = self._is_header or tag == "th"

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row:
            if self._is_header and not self.header:
                self.header = self._row
            else:
                self.rows.append(self._row)
            self._row = []


def _read_table(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Header and records, from either a CSV or an HTML table.

    The extension is not consulted. The commodity-wise export is named `.xls`
    while being HTML, so trusting the name is how a file gets read as one
    meaningless column without anything raising.
    """
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if text.lstrip()[:1] == "<":
        parser = _TableRows()
        parser.feed(text)
        if not parser.header:
            raise DataError(f"{path} is HTML but contains no table header row")
        return parser.header, [
            dict(zip(parser.header, cells, strict=False)) for cells in parser.rows
        ]

    reader = csv.DictReader(text.splitlines())
    header = [name.strip() for name in (reader.fieldnames or [])]
    return header, list(reader)


def parse_rows(
    path: Path,
    *,
    columns: BhavcopyColumns | None = None,
    symbols: frozenset[str] | None = None,
) -> list[BhavcopyRow]:
    """Read a bhavcopy, as CSV or as the HTML table MCX serves with an `.xls` name.

    With `columns=None` every layout in `KNOWN_LAYOUTS` is tried and the first
    that fits is used. Raises with the headers it actually found when none fit,
    rather than parsing whatever happens to be in the right position. A file
    silently read against the wrong columns is the worst possible outcome here —
    every downstream number would be confidently wrong.
    """
    if not path.exists():
        raise DataError(f"bhavcopy not found: {path}")

    found, records = _read_table(path)
    candidates = (columns,) if columns is not None else KNOWN_LAYOUTS
    layout = next(
        (c for c in candidates if not [n for n in c.required() if n not in found]), None
    )
    if layout is None:
        detail = "\n".join(
            f"  layout {i}: missing {', '.join(n for n in c.required() if n not in found)}"
            for i, c in enumerate(candidates, start=1)
        )
        raise DataError(
            f"{path} does not match any known bhavcopy layout.\n"
            f"{detail}\n"
            f"  file has:  {', '.join(found) or '(no header row)'}\n"
            "MCX_DEFAULT_COLUMNS is still an unverified assumption (only the "
            "commodity-wise layout has been checked against a real file) - "
            "pass a corrected BhavcopyColumns rather than editing the parser."
        )

    rows: list[BhavcopyRow] = []
    for line_no, raw in enumerate(records, start=2):
        row = _to_row(raw, layout, path, line_no)
        if row is None:
            continue
        if symbols is not None and row.symbol not in symbols:
            continue
        rows.append(row)
    return rows


def _to_row(
    raw: dict[str, str], columns: BhavcopyColumns, path: Path, line_no: int
) -> BhavcopyRow | None:
    kind = _clean(raw.get(columns.instrument_kind, "")).upper()
    if kind not in FUTURES_KINDS and kind not in OPTION_KINDS:
        # Spot, index and cash rows share the file. Skipping them is expected,
        # not an error.
        return None
    is_option = kind in OPTION_KINDS

    try:
        strike_text = _clean(raw.get(columns.strike, ""))
        right_text = _clean(raw.get(columns.option_type, "")).upper()
        return BhavcopyRow(
            trade_date=_parse_date(raw[columns.trade_date], columns.trade_date),
            symbol=_clean(raw[columns.symbol]).upper(),
            expiry=_parse_date(raw[columns.expiry], columns.expiry),
            is_option=is_option,
            strike=_decimal(strike_text) if is_option and strike_text else None,
            right=Right(right_text) if is_option and right_text in ("CE", "PE") else None,
            # A contract that never traded has an empty open/high/low and only a
            # settlement close. Falling back to the close is not inventing a
            # price - it is saying the only price this contract had all day was
            # its settlement, which is exactly what an empty cell means here. The
            # row still reports volume 0, so `traded` and `assume_spread` both
            # continue to treat it as untradeable.
            open=_decimal_or(raw[columns.open], raw[columns.close]),
            high=_decimal_or(raw[columns.high], raw[columns.close]),
            low=_decimal_or(raw[columns.low], raw[columns.close]),
            close=_decimal(raw[columns.close]),
            volume=_int(raw.get(columns.volume, "0")),
            open_interest=_int(raw.get(columns.open_interest, "0")),
        )
    except (KeyError, ValueError, InvalidOperation) as exc:
        # Brief §12: never swallow. Re-raise with the one fact the original lacked.
        raise DataError(f"{path}:{line_no} could not be parsed: {exc}") from exc


def futures_close(rows: Sequence[BhavcopyRow], *, symbol: str, on: date) -> Decimal | None:
    """The nearest-expiry futures close for `symbol` on `on`.

    Options are priced off the future, so this is the `F` every delta in the
    snapshot depends on. Nearest expiry because that is the contract the option
    cycle settles into; where it is not, `resolve_underlying` overrides it.
    """
    candidates = [
        row for row in rows if row.symbol == symbol and not row.is_option and row.trade_date == on
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda r: r.expiry).close


def nearest_futures_expiry(
    rows: Sequence[BhavcopyRow], *, symbol: str
) -> Callable[[date], date]:
    """The default option-expiry -> futures-expiry resolver: earliest futures
    contract expiring on or after the option.

    A documented heuristic, not a fact — open question **Q1c** exists to replace
    it. Public so a caller building bars or an expiry table from the same rows
    (`algo/backtest/bhavcopy_runner.py`) uses the identical pairing `build_snapshots`
    uses, rather than a second, possibly-diverging guess.
    """
    futures_expiries = sorted({r.expiry for r in rows if r.symbol == symbol and not r.is_option})

    def resolve(option_expiry: date) -> date:
        later = [e for e in futures_expiries if e >= option_expiry]
        return later[0] if later else option_expiry

    return resolve


def build_snapshots(
    rows: Sequence[BhavcopyRow],
    *,
    symbol: str,
    exchange: Exchange = Exchange.MCX,
    session_close_ist: tuple[int, int] = (23, 30),
    resolve_underlying: Callable[[date], date] | None = None,
) -> list[OptionChainSnapshot]:
    """Group rows into one chain snapshot per (trade date, option expiry).

    Snapshots are stamped at the **session close**, not at midnight, because the
    prices are closing prices — dating them to the start of the day would place
    the whole chain before the market that produced it.

    `session_close_ist` is a fixed hour and minute, not DST-aware — MCX actually
    closes at 23:30 IST during US daylight saving and 23:55 otherwise (D-014), so
    this stamps roughly a third of the year's snapshots up to 25 minutes early.
    Acceptable for the `algo bhavcopy` inspector this feeds; a caller building bars
    for the engine (`algo/backtest/bhavcopy_runner.py`) uses a real `MarketCalendar`
    instead and does not go through this function's default.

    `resolve_underlying` maps an option expiry to the futures expiry it settles
    into; see `nearest_futures_expiry` for the default.
    """
    resolver = resolve_underlying or nearest_futures_expiry(rows, symbol=symbol)
    hour, minute = session_close_ist

    grouped: dict[tuple[date, date], list[BhavcopyRow]] = {}
    for row in rows:
        if row.symbol != symbol or not row.is_option:
            continue
        if row.strike is None or row.right is None:
            continue
        grouped.setdefault((row.trade_date, row.expiry), []).append(row)

    snapshots: list[OptionChainSnapshot] = []
    for (trade_date, option_expiry), members in sorted(grouped.items()):
        underlying = futures_close(rows, symbol=symbol, on=trade_date)
        if underlying is None or underlying <= 0:
            # No futures close means no forward, and without a forward every
            # delta in this chain would be invented.
            continue

        future = FutureId(underlying=symbol, expiry=resolver(option_expiry), exchange=exchange)
        ts = ist_to_utc(trade_date, datetime.min.time().replace(hour=hour, minute=minute))

        chain_rows = [
            ChainRow(
                option=OptionId(
                    underlying_future=future,
                    option_expiry=option_expiry,
                    strike=member.strike,  # type: ignore[arg-type]
                    right=member.right,  # type: ignore[arg-type]
                    exchange=exchange,
                ),
                quote=Quote(
                    exchange_ts=ts,
                    received_ts=ts,
                    # No bid or ask: the file does not contain a book, and
                    # inventing one here would make every row look tradeable.
                    ltp=member.close,
                    volume=member.volume,
                    open_interest=member.open_interest,
                ),
            )
            for member in members
        ]
        chain_rows.sort(key=lambda r: (r.strike, r.right.value))

        snapshots.append(
            OptionChainSnapshot(
                ts=ts,
                underlying=symbol,
                option_expiry=option_expiry,
                futures_price=underlying,
                rows=tuple(chain_rows),
            )
        )
    return snapshots


def assume_spread(
    snapshot: OptionChainSnapshot,
    *,
    half_spread: Decimal,
    min_volume: int = 1,
) -> OptionChainSnapshot:
    """Give an end-of-day chain a synthetic book, so a backtest can fill against it.

    **This invents data, and it is a separate function on purpose.** A bhavcopy
    chain has no bid or ask, so nothing in it is tradeable by the engine's test —
    which is correct, and would otherwise mean no backtest could run at all. The
    fix is not to loosen the tradeability rule (that rule protects the live path)
    but to make assuming a spread an explicit, visible line of code.

    Rows that did not trade get **no** book. A strike with zero volume was listed,
    not available, and that distinction is the most valuable thing this dataset
    has to say about a thin ladder.
    """
    if half_spread < 0:
        raise DataError(f"half spread cannot be negative, got {half_spread}")

    rebuilt: list[ChainRow] = []
    for row in snapshot.rows:
        last = row.quote.ltp
        if last is None or last <= 0 or row.quote.volume < min_volume:
            rebuilt.append(row)
            continue
        rebuilt.append(
            row.model_copy(
                update={
                    "quote": row.quote.model_copy(
                        update={
                            "bid": max(last - half_spread, Decimal("0.05")),
                            "ask": last + half_spread,
                            "bid_qty": row.quote.volume,
                            "ask_qty": row.quote.volume,
                        }
                    )
                }
            )
        )
    return snapshot.model_copy(update={"rows": tuple(rebuilt)})


class BhavcopyChainFeed:
    """A `ChainFeed` over a directory of bhavcopy files."""

    __slots__ = ("_by_expiry", "_underlying")

    def __init__(self, snapshots: Iterable[OptionChainSnapshot], *, underlying: str) -> None:
        self._underlying = underlying
        self._by_expiry: dict[date, list[OptionChainSnapshot]] = {}
        for snapshot in snapshots:
            self._by_expiry.setdefault(snapshot.option_expiry, []).append(snapshot)
        for series in self._by_expiry.values():
            series.sort(key=lambda s: s.ts)

    @property
    def underlying(self) -> str:
        return self._underlying

    def snapshots(self, option_expiry: date):  # type: ignore[no-untyped-def]
        return iter(self._by_expiry.get(option_expiry, []))

    def expiries(self) -> tuple[date, ...]:
        return tuple(sorted(self._by_expiry))


def load_directory(
    directory: Path,
    *,
    symbol: str,
    columns: BhavcopyColumns | None = None,
    pattern: str | None = None,
) -> list[BhavcopyRow]:
    """Read every bhavcopy in `directory`, sorted by filename for determinism.

    `columns=None` lets each file match against `KNOWN_LAYOUTS` independently, so
    a directory holding both a CSV bhavcopy and a commodity-wise HTML export
    loads without the caller having to separate them first.

    `pattern=None` picks up both extensions. The commodity-wise export is HTML
    named `.xls`, so keying the default off `.csv` alone would silently find
    nothing in a directory that is entirely full of usable data - which is
    exactly what happened the first time real files arrived.
    """
    patterns = DATA_FILE_PATTERNS if pattern is None else (pattern,)
    files = sorted({f for p in patterns for f in directory.glob(p)})
    if not files:
        raise DataError(f"no files matching {pattern} in {directory}")
    rows: list[BhavcopyRow] = []
    for file in files:
        rows.extend(parse_rows(file, columns=columns, symbols=frozenset({symbol})))
    return rows


def coverage(rows: Sequence[BhavcopyRow], *, symbol: str) -> str:
    """What this dataset actually contains — the first thing to look at.

    Reports cycles, date span and how many strikes genuinely traded, because
    "a hundred cycles of history" is only worth having if the strikes the
    strategy wants were changing hands.
    """
    options = [r for r in rows if r.symbol == symbol and r.is_option]
    if not options:
        return f"no {symbol} option rows found"

    dates = sorted({r.trade_date for r in options})
    expiries = sorted({r.expiry for r in options})
    traded = [r for r in options if r.traded]
    lines = [
        f"{symbol}: {len(options):,} option rows over {len(dates)} sessions",
        f"  span     {dates[0]} .. {dates[-1]}",
        f"  cycles   {len(expiries)} expiries",
        f"  traded   {len(traded):,} rows had volume "
        f"({len(traded) / len(options) * 100:.1f}% of the ladder)",
    ]
    if traded:
        strikes_per_day = len(traded) / max(len(dates), 1)
        lines.append(f"  breadth  {strikes_per_day:.1f} strikes traded per session on average")
    return "\n".join(lines)


# ------------------------------------------------------------------- helpers


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _parse_date(value: str, column: str) -> date:
    text = _clean(value)
    for fmt in DATE_FORMATS:
        try:
            # DTZ007: a bhavcopy date is a calendar date, not an instant. The
            # naive datetime is discarded by .date() and never escapes here;
            # the session time is attached in build_snapshots (D-085).
            return datetime.strptime(text, fmt).date()  # noqa: DTZ007
        except ValueError:
            continue
    raise ValueError(f"{column}={text!r} is not a date in any known format {DATE_FORMATS}")


def _decimal_or(value: str, fallback: str) -> Decimal:
    """`value` when it holds a number, otherwise `fallback`. See `_to_row`."""
    return _decimal(value) if _clean(value) else _decimal(fallback)


def _decimal(value: str) -> Decimal:
    text = _clean(value).replace(",", "")
    if not text:
        raise ValueError("empty numeric field")
    # Constructed from the string, never via float — brief §2.5.
    return Decimal(text)


def _int(value: str) -> int:
    text = _clean(value).replace(",", "")
    if not text:
        return 0
    return int(Decimal(text))
