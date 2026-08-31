"""Command line entry points.

Three commands: `verify` proves the data pipeline end to end on synthetic bars,
`config` shows exactly what settings a run would use, and `backtest` runs the
Milestone 3 falsification.

`backtest` deliberately does not accept a real dataset yet. There is no recorded
data to point it at, and a command that quietly ran on generated bars while
looking like a strategy result would be worse than one that says what it is.
"""

from __future__ import annotations

import typer
from dotenv import load_dotenv

from algo.cli.cmd_backtest import backtest
from algo.cli.cmd_backtest_data import backtest_bhavcopy, backtest_smartapi
from algo.cli.cmd_bhavcopy import inspect_bhavcopy, inspect_chain
from algo.cli.cmd_config import show_config
from algo.cli.cmd_credentials import credentials
from algo.cli.cmd_killswitch import kill_switch
from algo.cli.cmd_live import live
from algo.cli.cmd_mt5 import live_mt5, mt5_replay
from algo.cli.cmd_serve import serve
from algo.cli.cmd_stop import stop
from algo.cli.cmd_telegram import telegram_check
from algo.cli.cmd_verify import verify
from algo.cli.cmd_walkforward import walk_forward_feasibility

app = typer.Typer(add_completion=False, help="MCX GOLDM short-strangle engine")

#: Credentials live in .env (gitignored). Loading it here means the whole CLI
#: sees the same environment without anyone hardcoding a secret.
load_dotenv()

app.command("verify")(verify)
app.command("config")(show_config)
app.command("backtest")(backtest)
app.command("live")(live)
app.command("walkforward")(walk_forward_feasibility)
app.command("serve")(serve)
app.command("killswitch")(kill_switch)
app.command("credentials")(credentials)
app.command("telegram-check")(telegram_check)
app.command("stop")(stop)
app.command("bhavcopy")(inspect_bhavcopy)
app.command("chain")(inspect_chain)
app.command("backtest-bhavcopy")(backtest_bhavcopy)
app.command("backtest-smartapi")(backtest_smartapi)
app.command("live-mt5")(live_mt5)
app.command("mt5-replay")(mt5_replay)

if __name__ == "__main__":
    app()
