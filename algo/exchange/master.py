"""Instrument master — the broker's list of tradeable contracts.

Decision D-023: expiry dates are **data to be read**, not a formula. The master
file the broker publishes is the source of truth; it is downloaded, filtered to
the exchanges and underlyings we care about, and frozen to a snapshot file so
that no run depends on a live download. The snapshot is data, lives under
`state/` (gitignored), and is never committed.

The mapping this module owns is the one decision D-009 keeps out of `core/`:
the broker's `symboltoken` and `tradingsymbol` exist **here and in the adapter**,
and nowhere else. `core/` names contracts as `OptionId`/`FutureId`; this module
translates.

Two master formats are understood. Angel One publishes a JSON document (one
`parse_master`); Kotak Neo publishes a dated `mcx_fo.csv` with `p`-prefixed
columns (`parse_master_csv`). Both feed the same `MasterRow` and the same
frozen `InstrumentMaster`, so nothing above this module cares which broker the
rows came from.

The master file's schema changes occasionally (the `strike` and `expiry` fields
are strings that can be empty; the Kotak strike arrives in scientific notation).
Everything is parsed defensively: a row that cannot be understood is skipped,
never guessed at, and lookups that find nothing raise rather than invent a token
— an order to the wrong symboltoken is a real order in the wrong instrument.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from algo.core.enums import Exchange, Right
from algo.core.errors import DataError
from algo.core.instrument import InstrumentId, OptionId
from algo.core.timeutil import ensure_utc

_FROZEN = ConfigDict(frozen=True, extra="forbid")

#: Exchange-segment codes as Angel One writes them in the master file.
SEGMENT = {Exchange.MCX: "MCX", Exchange.NSE: "NSE", Exchange.NFO: "NFO"}


class MasterRow(BaseModel):
    """One contract as the broker lists it.

    The multiplier/precision/generation fields exist for Kotak Neo, whose
    position payloads carry amounts in scaled quote units: the average price has
    to be computed with them (see `algo/execution/kotak.py`). Angel One rows
    leave them None.
    """

    model_config = _FROZEN

    symboltoken: str
    tradingsymbol: str
    exch_seg: str
    name: str
    instrumenttype: str
    expiry: date | None = None
    strike: Decimal | None = None
    lot_size: Decimal | None = None
    tick_size: Decimal | None = None
    multiplier: Decimal | None = None
    precision: int | None = None
    gen_num: Decimal | None = None
    gen_den: Decimal | None = None
    price_num: Decimal | None = None
    price_den: Decimal | None = None


@runtime_checkable
class MasterSource(Protocol):
    """Anything that can hand us the parsed master rows. The network lives here."""

    def fetch_master(self) -> list[MasterRow]: ...


def parse_master(raw: list[object]) -> list[MasterRow]:
    """Parse the Angel One JSON rows, skipping anything we cannot read exactly.

    A row with an unparseable expiry or strike is *not* dropped from existence —
    it is dropped from our index, which means a lookup for it will fail loudly
    instead of silently sending an order at the wrong token.
    """
    rows: list[MasterRow] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        token = str(entry.get("token", ""))
        symbol = str(entry.get("symbol", ""))
        if not token or not symbol:
            continue
        try:
            rows.append(
                MasterRow(
                    symboltoken=token,
                    tradingsymbol=symbol,
                    exch_seg=str(entry.get("exch_seg", "")).upper(),
                    name=str(entry.get("name", "")).upper(),
                    instrumenttype=str(entry.get("instrumenttype", "")).upper(),
                    expiry=_parse_expiry(entry.get("expiry")),
                    strike=_parse_angel_one_price(entry.get("strike")),
                    lot_size=_parse_decimal(entry.get("lotsize")),
                    tick_size=_parse_angel_one_price(entry.get("tick_size")),
                )
            )
        except (ValueError, InvalidOperation):
            continue
    return rows


def _parse_expiry(value: object) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    # SmartAPI writes expiry as `DDMMMYYYY` (e.g. "28AUG2026"). There is no
    # time component to make timezone-aware; the date is what matters.
    return datetime.strptime(value.strip().upper(), "%d%b%Y").date()  # noqa: DTZ007


#: Angel One's scrip master publishes strike and tick size scaled by 100 for MCX
#: (its API does not carry a precision field the way Kotak's does). Verified
#: 2026-08-25 against the live master: GOLDM28AUG26157000CE, whose strike the
#: tradingsymbol itself states as 157000, is served as `"strike": "15700000.000000"`
#: - and its tick size, independently confirmed against the project's own sourced
#: GOLDM contract spec (algo/exchange/data/spec_goldm.yaml, tick_size 0.50), is
#: served as `"tick_size": "50.000000"`. Two independent checks, both x100.
_ANGEL_ONE_PRICE_SCALE = Decimal("100")


def _parse_angel_one_price(value: object) -> Decimal | None:
    """A strike or tick size from the Angel One JSON master, descaled to real
    rupees. See `_ANGEL_ONE_PRICE_SCALE` for the evidence this constant rests on.

    Applies to `parse_master` only. `parse_master_csv` (Kotak) has its own
    already-correct, precision-field-driven descaling for strike
    (`_parse_kotak_strike`) and has not been checked here for tick size - it is
    a separate broker's file format and this constant does not necessarily
    apply to it.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    return Decimal(value.strip()) / _ANGEL_ONE_PRICE_SCALE


def _parse_decimal(value: object) -> Decimal | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return Decimal(value.strip())


def _parse_precision(value: object) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def right_of(symbol: str) -> Right | None:
    """The CE/PE suffix of a tradingsymbol, or None. Public: reused by
    algo/backtest/smartapi_runner.py to build an OptionId from a MasterRow
    without a second, possibly-diverging implementation."""
    suffix = symbol[-2:].upper()
    if suffix == "CE":
        return Right.CE
    if suffix == "PE":
        return Right.PE
    return None


#: The published Angel One master file. One JSON document, every exchange.
MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"


class HttpMasterSource:
    """Downloads the published Angel One master file. The network lives here."""

    def __init__(self, *, url: str = MASTER_URL, timeout_s: float = 60.0) -> None:
        self._url = url
        self._timeout_s = timeout_s

    def fetch_master(self) -> list[MasterRow]:
        import requests

        try:
            response = requests.get(self._url, timeout=self._timeout_s)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise DataError(f"instrument master download failed: {exc}") from exc
        payload = response.json()
        if not isinstance(payload, list):
            raise DataError(
                f"instrument master at {self._url} is not the expected JSON list"
            )
        return parse_master(list(payload))


#: Kotak Neo exchange-segment codes -> the engine's exchange codes.
_KOTAK_SEGMENT = {
    "mcx_fo": "MCX",
    "nse_cm": "NSE",
    "nse_fo": "NFO",
    "bse_cm": "BSE",
    "bse_fo": "BFO",
    "cde_fo": "CDE",
}


def parse_master_csv(text: str) -> list[MasterRow]:
    """Parse the Kotak Neo `mcx_fo.csv` scrip master into rows.

    The file's header (observed 2026-07-14) uses `p`-prefixed names with stray
    whitespace and a trailing `;` on `dStrikePrice`; both are tolerated. The
    strike arrives in scientific notation (`1.485e+07`) and is stored scaled by
    the row's precision (`dStrikePrice / 10^precision`), matching the plain
    strike the trading symbol carries. Expiries are epoch seconds.
    """
    reader: Iterable[dict[str, str] | None] = csv.DictReader(text.splitlines())

    def key(name: str) -> str:
        return name.strip().rstrip(";").strip()

    rows: list[MasterRow] = []
    for entry in reader:
        if not isinstance(entry, dict):
            continue
        fields = {key(k): v for k, v in entry.items()}
        token = str(fields.get("pSymbol", "") or "").strip()
        symbol = str(fields.get("pTrdSymbol", "") or "").strip()
        if not token or not symbol:
            continue
        try:
            rows.append(
                MasterRow(
                    symboltoken=token,
                    tradingsymbol=symbol,
                    exch_seg=_KOTAK_SEGMENT.get(
                        str(fields.get("pExchSeg", "") or "").lower(),
                        str(fields.get("pExchSeg", "") or "").upper(),
                    ),
                    name=str(fields.get("pSymbolName", "") or "").upper(),
                    instrumenttype=str(fields.get("pInstType", "") or "").upper(),
                    expiry=_parse_epoch_expiry(
                        fields.get("pExpiryDate") or fields.get("lExpiryDate")
                    ),
                    strike=_parse_kotak_strike(
                        fields.get("dStrikePrice"), fields.get("lPrecision")
                    ),
                    lot_size=_parse_decimal(fields.get("lLotSize")),
                    tick_size=_parse_decimal(fields.get("dTickSize")),
                    multiplier=_parse_decimal(fields.get("lMultiplier")),
                    precision=_parse_precision(fields.get("lPrecision")),
                    gen_num=_parse_decimal(fields.get("dGenNum")),
                    gen_den=_parse_decimal(fields.get("dGenDen")),
                    price_num=_parse_decimal(fields.get("dPriceNum")),
                    price_den=_parse_decimal(fields.get("dPriceDen")),
                )
            )
        except (ValueError, InvalidOperation):
            continue
    return rows


def _parse_epoch_expiry(value: object) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=UTC).date()
    except (ValueError, OverflowError, OSError):
        return None


def _parse_kotak_strike(value: object, precision: object) -> Decimal | None:
    scaled = _parse_decimal(value)
    if scaled is None:
        return None
    places = _parse_precision(precision) or 0
    if places < 0:
        return None
    return scaled / Decimal(10) ** places


class KotakMasterSource:
    """Downloads the Kotak Neo scrip master CSV. The network lives here.

    The SDK resolves the dated download URL for a segment (`scrip_master`); the
    CSV is fetched from that URL. Only `consumer_key` is required — scrip-master
    access authenticates on the key alone, no 2FA session.
    """

    def __init__(
        self,
        *,
        consumer_key: str,
        exchange_segment: str = "mcx_fo",
        timeout_s: float = 60.0,
    ) -> None:
        self._consumer_key = consumer_key
        self._exchange_segment = exchange_segment
        self._timeout_s = timeout_s

    def fetch_master(self) -> list[MasterRow]:
        # The SDK ships py.typed without stubs and mypy's analysis of it does
        # not see its lazy exports, so the import below is ignored explicitly.
        from neo_api_client import NeoAPI  # type: ignore[attr-defined]

        client = NeoAPI(consumer_key=self._consumer_key, environment="prod")
        url: Any = client.scrip_master(exchange_segment=self._exchange_segment)
        if not isinstance(url, str) or not url.strip():
            raise DataError(
                f"Kotak scrip-master lookup failed for {self._exchange_segment}: {url!r}"
            )

        import requests

        try:
            response = requests.get(url, timeout=self._timeout_s)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise DataError(f"instrument master download failed: {exc}") from exc
        text = response.content.decode("utf-8-sig", errors="replace")
        return parse_master_csv(text)


class InstrumentMaster:
    """Indexed, frozen view of the broker's contract list."""

    __slots__ = ("_by_token", "_fetched_at", "_rows")

    def __init__(self, rows: list[MasterRow], *, fetched_at: datetime) -> None:
        self._rows = tuple(rows)
        self._fetched_at = ensure_utc(fetched_at)
        self._by_token: dict[str, MasterRow] = {r.symboltoken: r for r in rows}

    # ---------------------------------------------------------------- lookup
    def row_for(self, instrument: InstrumentId) -> MasterRow:
        exchange = instrument.exchange
        candidates = [
            r
            for r in self._rows
            if r.exch_seg == SEGMENT[exchange] and r.name == instrument.underlying
        ]
        if isinstance(instrument, OptionId):
            matches = [
                r for r in candidates if r.expiry == instrument.option_expiry
                and r.strike == instrument.strike
                and right_of(r.tradingsymbol) is instrument.right
            ]
        else:
            matches = [r for r in candidates if r.expiry == instrument.expiry]
        if not matches:
            raise DataError(
                f"no broker contract for {instrument.key} in the master snapshot "
                f"(fetched {self._fetched_at:%Y-%m-%d %H:%M}Z, {len(candidates)} "
                f"{instrument.underlying} rows on {SEGMENT[exchange]}). "
                "Refusing to guess a symboltoken."
            )
        if len(matches) > 1:
            raise DataError(
                f"master lists {len(matches)} contracts for {instrument.key}; "
                "the snapshot is ambiguous and must not be traded against"
            )
        return matches[0]

    def row_by_token(self, symboltoken: str) -> MasterRow | None:
        return self._by_token.get(symboltoken)

    def row_by_symbol(self, tradingsymbol: str) -> MasterRow | None:
        """Find a row by its trading symbol. Kotak's trade report sometimes
        omits the token (`tok`), so fills must be resolvable by symbol alone."""
        for row in self._rows:
            if row.tradingsymbol == tradingsymbol:
                return row
        return None

    # ----------------------------------------------------------------- reads
    @property
    def fetched_at(self) -> datetime:
        return self._fetched_at

    def option_expiries(self, underlying: str, exchange: Exchange) -> tuple[date, ...]:
        """The expiry dates the broker actually lists for `underlying`'s options.

        This is what `algo/exchange/expiries.py` treats as authoritative — the
        rule there only cross-checks it.
        """
        return tuple(
            sorted(
                {
                    r.expiry
                    for r in self._rows
                    if r.exch_seg == SEGMENT[exchange]
                    and r.name == underlying
                    and r.instrumenttype.startswith("OPT")
                    and r.expiry is not None
                }
            )
        )

    def option_rows(
        self, underlying: str, exchange: Exchange, option_expiry: date
    ) -> tuple[MasterRow, ...]:
        """Every option row for one expiry — what the chain feed subscribes to."""
        return tuple(
            sorted(
                (
                    r
                    for r in self._rows
                    if r.exch_seg == SEGMENT[exchange]
                    and r.name == underlying
                    and r.expiry == option_expiry
                    and r.instrumenttype.startswith("OPT")
                    and r.strike is not None
                    and right_of(r.tradingsymbol) is not None
                ),
                key=lambda r: (r.strike, r.tradingsymbol),
            )
        )

    def future_rows(self, underlying: str, exchange: Exchange) -> tuple[MasterRow, ...]:
        return tuple(
            sorted(
                (
                    r
                    for r in self._rows
                    if r.exch_seg == SEGMENT[exchange]
                    and r.name == underlying
                    and r.instrumenttype.startswith("FUT")
                    and r.expiry is not None
                ),
                key=lambda r: (r.expiry,),
            )
        )

    # ------------------------------------------------------------ snapshots
    def save_snapshot(self, path: Path | str) -> None:
        """Freeze the fetched rows to disk, with the fetch time attached.

        A snapshot is data: it carries `fetched_at` so a stale table is visible
        rather than silently assumed current.
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fetched_at": self._fetched_at.isoformat(),
            "rows": [r.model_dump(mode="json") for r in self._rows],
        }
        target.write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )

    @classmethod
    def from_snapshot(cls, path: Path | str) -> InstrumentMaster:
        source = Path(path)
        if not source.exists():
            raise DataError(
                f"no instrument master snapshot at {source}. Fetch one first "
                "(the `algo live` command downloads it) — an order needs a token."
            )
        raw = json.loads(source.read_text(encoding="utf-8"))
        return cls(
            [MasterRow.model_validate(e) for e in raw["rows"]],
            fetched_at=datetime.fromisoformat(str(raw["fetched_at"])),
        )


def fetch_master(source: MasterSource, path: Path | str, *, now: datetime) -> InstrumentMaster:
    """Download the master file and freeze it. The one network call in this module."""
    master = InstrumentMaster(source.fetch_master(), fetched_at=now)
    master.save_snapshot(path)
    return master
