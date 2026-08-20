"""The tearsheet. Brief §10: equity curve, drawdown underwater plot, R-multiple
distribution.

One self-contained HTML file with inline SVG and no dependencies — not matplotlib,
not a chart library, nothing fetched at open. A tearsheet you can email, open on a
machine with no Python, and still read in five years is worth more than a prettier
one that needs an environment.

The layout puts the caveats **above** the numbers, deliberately. Placeholder
charge rates and a modelled spread change what every figure below them means, and
a footnote is where that information goes to be ignored.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from algo.core.timeutil import iso
from algo.core.trade import Trade
from algo.portfolio.book import EquityPoint
from algo.reporting.metrics import Metrics

_CSS = """
:root{--ground:#0f1218;--panel:#171b23;--panel2:#1d222b;--line:#2b323e;
--text:#e3e7ed;--muted:#8e97a6;--faint:#6b7484;--brass:#cfa255;--ice:#7fb6d9;
--sage:#79a98a;--clay:#d07862}
@media(prefers-color-scheme:light){:root{--ground:#edeff2;--panel:#fff;
--panel2:#f5f6f8;--line:#d5dae2;--text:#161a21;--muted:#5b6474;--faint:#8b94a3;
--brass:#8a6420;--ice:#26688f;--sage:#3b7350;--clay:#a33e2a}}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--text);
font-family:ui-sans-serif,system-ui,sans-serif;font-size:14px;line-height:1.55}
.wrap{max-width:1000px;margin:0 auto;padding:32px 24px 72px}
h1{font-size:21px;margin:0 0 4px;letter-spacing:-.01em}
.sub{color:var(--muted);margin:0 0 22px;font-size:13px}
.mono,td.num,th.num{font-family:ui-monospace,monospace;font-variant-numeric:tabular-nums}
.caveat{border:1px solid var(--brass);border-left-width:3px;border-radius:3px;
background:var(--panel);padding:12px 16px;margin-bottom:22px}
.caveat h2{margin:0 0 6px;font-size:11px;letter-spacing:.09em;text-transform:uppercase;
color:var(--brass);font-family:ui-monospace,monospace}
.caveat ul{margin:0;padding-left:18px;color:var(--muted);font-size:13px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:1px;
background:var(--line);border:1px solid var(--line);border-radius:3px;overflow:hidden;
margin-bottom:22px}
.stat{background:var(--panel);padding:11px 14px}
.stat dt{font-family:ui-monospace,monospace;font-size:10px;letter-spacing:.11em;
text-transform:uppercase;color:var(--faint);margin-bottom:3px}
.stat dd{margin:0;font-family:ui-monospace,monospace;font-size:17px;font-weight:500}
.up{color:var(--sage)}.down{color:var(--clay)}
section{border:1px solid var(--line);border-radius:3px;background:var(--panel);
margin-bottom:22px;overflow:hidden}
.bar{padding:8px 15px;background:var(--panel2);border-bottom:1px solid var(--line);
font-family:ui-monospace,monospace;font-size:11px;letter-spacing:.09em;
text-transform:uppercase;color:var(--faint);display:flex;justify-content:space-between;gap:12px}
.in{padding:14px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;font-family:ui-monospace,monospace;font-size:10px;letter-spacing:.1em;
text-transform:uppercase;color:var(--faint);font-weight:500;padding:0 10px 7px;
border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:6px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th.num,td.num{text-align:right}
tr:last-child td{border-bottom:none}
.note{color:var(--muted);font-size:12px;max-width:60ch}
.scroll{overflow-x:auto}
footer{color:var(--faint);font-size:12.5px;border-top:1px solid var(--line);
padding-top:16px;margin-top:32px}
"""


def render(
    *,
    title: str,
    metrics: Metrics,
    curve: Sequence[EquityPoint],
    trades: Sequence[Trade],
    warnings: Sequence[str] = (),
    dataset_hash: str = "",
    config_hash: str = "",
    generated_at: datetime | None = None,
) -> str:
    """Build the tearsheet as a single self-contained HTML string."""
    stamp = iso(generated_at) if generated_at else "not stamped"
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head><body><div class='wrap'>",
        f"<h1>{html.escape(title)}</h1>",
        f"<p class='sub mono'>generated {stamp} &nbsp;·&nbsp; dataset {dataset_hash or '—'} "
        f"&nbsp;·&nbsp; config {config_hash or '—'}</p>",
    ]

    if warnings:
        parts.append("<div class='caveat'><h2>Read these before reading the numbers</h2><ul>")
        parts.extend(f"<li>{html.escape(w)}</li>" for w in warnings)
        parts.append("</ul></div>")

    parts.append(_stats_block(metrics))
    parts.append(_equity_section(curve))
    parts.append(_underwater_section(curve))

    if metrics.trade is not None and metrics.trade.trades:
        parts.append(_r_distribution_section(metrics))
        parts.append(_trade_stats_section(metrics))
    parts.append(_trade_log_section(trades))

    parts.append(
        "<footer>Synthetic or recorded — check the dataset hash above. This document "
        "reports numbers; it makes no claim that the strategy is profitable. At "
        f"{metrics.trade_count} completed trades, no ratio here can distinguish skill "
        "from luck.</footer>"
        if metrics.trade_count < 30
        else "<footer>This document reports numbers; it makes no claim that the "
        "strategy is profitable.</footer>"
    )
    parts.append("</div></body></html>")
    return "".join(parts)


def write(path: Path, markup: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markup, encoding="utf-8")
    return path


# ------------------------------------------------------------------ sections


def _stats_block(metrics: Metrics) -> str:
    drawdown = metrics.max_drawdown
    cells = [
        ("net P&L", f"{metrics.net_pnl:,}", "up" if metrics.net_pnl > 0 else "down"),
        ("gross P&L", f"{metrics.gross_pnl:,}", ""),
        ("total cost", f"{metrics.total_cost:,}", ""),
        ("cost drag", _dash(metrics.cost_drag_pct, "%"), ""),
        ("return", f"{metrics.return_pct:.4f}%", ""),
        ("trades", str(metrics.trade_count), ""),
        ("exposure", f"{metrics.exposure_pct:.1f}%", ""),
        (
            "max drawdown",
            f"{drawdown.depth_pct:.3f}%" if drawdown else "—",
            "down" if drawdown else "",
        ),
        ("Sharpe", _dash(metrics.sharpe), ""),
        ("Sortino", _dash(metrics.sortino), ""),
        ("Calmar", _dash(metrics.calmar), ""),
    ]
    body = "".join(
        f"<div class='stat'><dt>{html.escape(label)}</dt>"
        f"<dd class='{cls}'>{html.escape(value)}</dd></div>"
        for label, value, cls in cells
    )
    return f"<dl class='stats'>{body}</dl>"


def _equity_section(curve: Sequence[EquityPoint]) -> str:
    if len(curve) < 2:
        return _section("equity curve", "", "<p class='note'>Not enough points to draw.</p>")
    values = [float(p.equity) for p in curve]
    path, area = _line_path(values, 860, 210)
    svg = (
        "<svg viewBox='0 0 860 210' width='100%' height='210' preserveAspectRatio='none' "
        "role='img' aria-label='Equity curve'>"
        "<defs><linearGradient id='eq' x1='0' y1='0' x2='0' y2='1'>"
        "<stop offset='0%' stop-color='var(--ice)' stop-opacity='.28'/>"
        "<stop offset='100%' stop-color='var(--ice)' stop-opacity='0'/></linearGradient></defs>"
        f"<path d='{area}' fill='url(#eq)'/>"
        f"<path d='{path}' fill='none' stroke='var(--ice)' stroke-width='1.6'/></svg>"
    )
    return _section("equity curve", f"{len(curve)} bars", svg)


def _underwater_section(curve: Sequence[EquityPoint]) -> str:
    """Brief §10 asks for this specifically — depth and duration are different
    experiences, and one equity line shows neither clearly."""
    if len(curve) < 2:
        return ""
    peak = float(curve[0].equity)
    depths: list[float] = []
    for point in curve:
        peak = max(peak, float(point.equity))
        depths.append(((peak - float(point.equity)) / peak * 100) if peak else 0.0)
    worst = max(depths) or 0.0001
    width, height = 860, 90
    step = width / max(len(depths) - 1, 1)
    coords = " ".join(
        f"{'M' if i == 0 else 'L'}{i * step:.1f},{8 + (d / worst) * (height - 16):.1f}"
        for i, d in enumerate(depths)
    )
    svg = (
        f"<svg viewBox='0 0 {width} {height}' width='100%' height='{height}' "
        "preserveAspectRatio='none' role='img' aria-label='Drawdown underwater plot'>"
        f"<path d='{coords} L{width},8 L0,8 Z' fill='var(--clay)' fill-opacity='.22' "
        "stroke='var(--clay)' stroke-width='1'/></svg>"
    )
    return _section("underwater — depth below the running peak", f"worst {worst:.3f}%", svg)


def _r_distribution_section(metrics: Metrics) -> str:
    stats = metrics.trade
    if stats is None or not stats.r_multiples:
        return ""
    buckets = stats.histogram()
    tallest = max((count for _, count in buckets), default=1) or 1
    width, height = 860, 150
    bar_width = width / max(len(buckets), 1)
    bars = "".join(
        f"<rect x='{i * bar_width + 3:.1f}' y='{height - 24 - (c / tallest) * (height - 40):.1f}' "
        f"width='{bar_width - 6:.1f}' height='{(c / tallest) * (height - 40):.1f}' "
        f"fill='{'var(--clay)' if label.startswith('-') else 'var(--sage)'}' rx='1'/>"
        f"<text x='{i * bar_width + bar_width / 2:.1f}' y='{height - 8}' "
        "text-anchor='middle' font-size='9' font-family='ui-monospace,monospace' "
        f"fill='var(--faint)'>{html.escape(label)}</text>"
        for i, (label, c) in enumerate(buckets)
    )
    svg = (
        f"<svg viewBox='0 0 {width} {height}' width='100%' height='{height}' "
        "role='img' aria-label='R-multiple distribution'>" + bars + "</svg>"
        "<p class='note'>A premium-selling strategy's shape — many small wins, rare "
        "large losses — is invisible in an average and obvious here.</p>"
    )
    return _section(
        "R-multiple distribution", f"{stats.trades_with_r} trades with a stop", svg
    )


def _trade_stats_section(metrics: Metrics) -> str:
    stats = metrics.trade
    if stats is None:
        return ""
    rows = [
        ("round trips", f"{stats.trades} ({stats.wins}W / {stats.losses}L)"),
        ("win rate", _dash(stats.win_rate, "%")),
        ("profit factor", _dash(stats.profit_factor)),
        ("expectancy", _dash(stats.expectancy_r, "R")),
        ("average win", _dash(stats.average_win_r, "R")),
        ("average loss", _dash(stats.average_loss_r, "R")),
        ("largest win", _dash(stats.largest_win)),
        ("largest loss", _dash(stats.largest_loss)),
        ("longest losing streak", str(stats.longest_losing_streak)),
    ]
    body = "".join(
        f"<tr><td>{html.escape(k)}</td><td class='num mono'>{html.escape(v)}</td></tr>"
        for k, v in rows
    )
    return _section("round-trip statistics", "", f"<table>{body}</table>")


def _trade_log_section(trades: Sequence[Trade]) -> str:
    if not trades:
        return _section("trade log", "0", "<p class='note'>No completed round trips.</p>")

    head = (
        "<tr><th>Opened</th><th>Closed</th><th class='num'>Net</th>"
        "<th class='num'>R</th><th>Exit</th><th>Why it fired</th></tr>"
    )
    rows: list[str] = []
    for trade in trades:
        closed = f"{trade.closed_at:%d %b %H:%M}" if trade.closed_at else "still open"
        r = f"{trade.r_multiple:+.2f}" if trade.r_multiple is not None else "—"
        rows.append(
            "<tr>"
            f"<td class='mono'>{trade.opened_at:%d %b %H:%M}</td>"
            f"<td class='mono'>{html.escape(closed)}</td>"
            f"<td class='num mono'>{trade.net_pnl:,}</td>"
            f"<td class='num mono'>{html.escape(r)}</td>"
            f"<td class='mono'>{html.escape(trade.exit_reason)}</td>"
            f"<td class='note'>{html.escape(trade.reason)}</td>"
            "</tr>"
        )
    return _section(
        "trade log",
        str(len(trades)),
        f"<div class='scroll'><table>{head}{''.join(rows)}</table></div>",
    )


def _section(title: str, right: str, body: str) -> str:
    return (
        f"<section><div class='bar'><span>{html.escape(title)}</span>"
        f"<span class='mono'>{html.escape(right)}</span></div>"
        f"<div class='in'>{body}</div></section>"
    )


def _line_path(values: list[float], width: int, height: int) -> tuple[str, str]:
    low, high = min(values), max(values)
    span = high - low or 1
    pad = 8
    step = (width - pad * 2) / max(len(values) - 1, 1)

    def y(v: float) -> float:
        return pad + (1 - (v - low) / span) * (height - pad * 2)

    path = " ".join(
        f"{'M' if i == 0 else 'L'}{pad + i * step:.1f},{y(v):.1f}" for i, v in enumerate(values)
    )
    area = f"{path} L{pad + (len(values) - 1) * step:.1f},{height - pad} L{pad},{height - pad} Z"
    return path, area


def _dash(value: Decimal | None, suffix: str = "") -> str:
    return "—" if value is None else f"{value:.4f}{suffix}"
