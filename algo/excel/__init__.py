"""Excel as a front end for Kotak Neo: live quotes, the option chain, positions,
and order entry from a sheet.

The package is layered so that only `io.XlwingsSheetIO` ever touches Excel. Layout,
cell building and order parsing are all pure, which is what lets the tests run on a
machine with neither Excel nor a broker.

Entry point: `scripts/excel_bridge.py`.
"""

from __future__ import annotations
