"""The NIFTY 50 constituents, in Angel One's cash-segment spelling.

## Why this is a list in a file and not a download

The index is rebalanced twice a year and the changes are announced weeks ahead,
so a hard-coded list goes stale slowly and visibly - a symbol that left the
index simply stops being watched, and one that joined is missing until someone
edits this file. The alternative, scraping the constituent list at startup, adds
a network dependency and a parser to a tool whose entire job is to not miss a
five-minute bar, in exchange for keeping a list current that moves twice a year.

`-EQ` is the suffix Angel One's instrument master uses for the cash series, and
it is what `row_by_symbol` matches on. Without it the lookup finds nothing, or
worse finds a derivative row.

**Last checked: see the git history of this file.** If a message never arrives
for a stock you expect, check its spelling here first: an unknown symbol is
reported once per run in the log and then skipped, deliberately, so that one
renamed constituent cannot stop the other 49 from being watched.
"""

from __future__ import annotations

NIFTY_50: tuple[str, ...] = (
    "ADANIENT-EQ",
    "ADANIPORTS-EQ",
    "APOLLOHOSP-EQ",
    "ASIANPAINT-EQ",
    "AXISBANK-EQ",
    "BAJAJ-AUTO-EQ",
    "BAJFINANCE-EQ",
    "BAJAJFINSV-EQ",
    "BEL-EQ",
    "BHARTIARTL-EQ",
    "CIPLA-EQ",
    "COALINDIA-EQ",
    "DRREDDY-EQ",
    "EICHERMOT-EQ",
    "ETERNAL-EQ",
    "GRASIM-EQ",
    "HCLTECH-EQ",
    "HDFCBANK-EQ",
    "HDFCLIFE-EQ",
    "HEROMOTOCO-EQ",
    "HINDALCO-EQ",
    "HINDUNILVR-EQ",
    "ICICIBANK-EQ",
    "INDUSINDBK-EQ",
    "INFY-EQ",
    "ITC-EQ",
    "JIOFIN-EQ",
    "JSWSTEEL-EQ",
    "KOTAKBANK-EQ",
    "LT-EQ",
    "M&M-EQ",
    "MARUTI-EQ",
    "NESTLEIND-EQ",
    "NTPC-EQ",
    "ONGC-EQ",
    "POWERGRID-EQ",
    "RELIANCE-EQ",
    "SBILIFE-EQ",
    "SBIN-EQ",
    "SHRIRAMFIN-EQ",
    "SUNPHARMA-EQ",
    "TATACONSUM-EQ",
    "TATAMOTORS-EQ",
    "TATASTEEL-EQ",
    "TCS-EQ",
    "TECHM-EQ",
    "TITAN-EQ",
    "TRENT-EQ",
    "ULTRACEMCO-EQ",
    "WIPRO-EQ",
)
