"""Data quality gates.

Brief §6: the engine must never fill against a phantom price. That means bad data
has to be *detected* rather than absorbed — a missing bar silently skipped looks
identical to a quiet market, and the two have opposite implications for a stop.

Every finding carries the timestamp it occurred at, so a report points at a
minute rather than at a file.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from algo.core.bar import Bar, Timeframe
from algo.core.timeutil import iso, ist_date
from algo.exchange.calendar import MarketCalendar


class Severity(StrEnum):
    WARN = "WARN"
    ERROR = "ERROR"


class Finding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    severity: Severity
    at: datetime | None
    detail: str

    def __str__(self) -> str:
        where = iso(self.at) if self.at else "-"
        return f"[{self.severity}] {self.code} {where} {self.detail}"


class QualityReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    bars_checked: int
    findings: tuple[Finding, ...]

    @property
    def errors(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.ERROR)

    @property
    def is_clean(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        if self.is_clean and not self.findings:
            return f"{self.bars_checked} bars, no findings"
        counts: dict[str, int] = {}
        for finding in self.findings:
            counts[finding.code] = counts.get(finding.code, 0) + 1
        detail = ", ".join(f"{code}={n}" for code, n in sorted(counts.items()))
        return f"{self.bars_checked} bars, {len(self.findings)} findings: {detail}"


def validate_bars(
    bars: Sequence[Bar],
    *,
    calendar: MarketCalendar,
    timeframe: Timeframe,
    expect_full_sessions: bool = True,
) -> QualityReport:
    """Check a bar series for the failure modes that produce wrong backtests."""
    findings: list[Finding] = []

    seen: set[datetime] = set()
    previous: Bar | None = None
    for bar in bars:
        if bar.ts in seen:
            findings.append(
                Finding(code="DUPLICATE_TS", severity=Severity.ERROR, at=bar.ts, detail="repeated")
            )
        seen.add(bar.ts)

        if previous is not None and bar.ts <= previous.ts:
            findings.append(
                Finding(
                    code="NON_MONOTONIC",
                    severity=Severity.ERROR,
                    at=bar.ts,
                    detail=f"follows {iso(previous.ts)}",
                )
            )

        if bar.volume == 0:
            findings.append(
                Finding(
                    code="ZERO_VOLUME",
                    severity=Severity.WARN,
                    at=bar.ts,
                    detail="untradeable — no fill may be simulated against this bar",
                )
            )
        if bar.high == bar.low and bar.volume > 0:
            findings.append(
                Finding(
                    code="FLAT_BAR",
                    severity=Severity.WARN,
                    at=bar.ts,
                    detail="zero range with volume; possible stale print",
                )
            )
        previous = bar

    if expect_full_sessions:
        findings.extend(_session_coverage(bars, calendar=calendar, timeframe=timeframe))

    return QualityReport(bars_checked=len(bars), findings=tuple(findings))


def _session_coverage(
    bars: Sequence[Bar], *, calendar: MarketCalendar, timeframe: Timeframe
) -> list[Finding]:
    """Compare bars present against bars the session should have produced.

    This is the check that catches a recorder outage. Without it, a day with three
    hours missing looks like a day that was simply quiet.
    """
    by_day: dict[date, int] = {}
    for bar in bars:
        by_day[ist_date(bar.ts)] = by_day.get(ist_date(bar.ts), 0) + 1

    findings: list[Finding] = []
    for session_day, count in sorted(by_day.items()):
        expected = len(calendar.bar_boundaries(session_day, timeframe))
        if count < expected:
            findings.append(
                Finding(
                    code="INCOMPLETE_SESSION",
                    severity=Severity.WARN,
                    at=None,
                    detail=(
                        f"{session_day}: {count} bars, expected {expected} "
                        f"({'US DST' if calendar.is_us_dst_session(session_day) else 'standard'} "
                        "session)"
                    ),
                )
            )
        elif count > expected:
            findings.append(
                Finding(
                    code="EXCESS_BARS",
                    severity=Severity.ERROR,
                    at=None,
                    detail=f"{session_day}: {count} bars, session allows only {expected}",
                )
            )
    return findings
