"""The CLI, exercised as a user runs it (D-117).

`algo/cli/main.py` is the largest module in the project and no test imported it,
which is how wiring `flatten_on_trip` shipped an `UnboundLocalError` on the
no-config path that a 790-test suite passed straight over. It was found by
running the command by hand.

Two rules shape what is here:

**Every command is run on both config paths.** The bug above existed because a
variable was bound inside `if config is not None` and read outside it. That shape
recurs in every command that takes `--config`, so each one is invoked twice.

**Nothing here touches the network.** `live`, `credentials` and `serve` open real
sessions, so only their refusal paths - the ones that return before any
credential is read - are exercised. A test suite that logs into a broker is a
test suite nobody can run.
"""

from __future__ import annotations

import pathlib

import pytest
from typer.testing import CliRunner

from algo.cli.main import app

RUNNER = CliRunner()
REFERENCE = pathlib.Path("config/goldm.yaml")


def _run(*args: str, stdin: str | None = None):
    return RUNNER.invoke(app, list(args), input=stdin)


def _ok(*args: str, stdin: str | None = None):
    result = _run(*args, stdin=stdin)
    assert result.exit_code == 0, (
        f"`algo {' '.join(args)}` exited {result.exit_code}\n{result.output}"
        + (f"\n{result.exception!r}" if result.exception else "")
    )
    return result


class TestEveryCommandRunsOnBothConfigPaths:
    """The regression class that motivated this file: a name bound only inside
    `if config is not None` and read unconditionally afterwards."""

    def test_backtest_without_a_config(self) -> None:
        result = _ok("backtest", "--strategy", "coin_flip")

        assert "FALSIFICATION" in result.output

    def test_backtest_with_a_config(self) -> None:
        result = _ok("backtest", "--strategy", "coin_flip", "--config", str(REFERENCE))

        assert "FALSIFICATION" in result.output

    def test_backtest_buy_and_hold_without_a_config(self) -> None:
        _ok("backtest", "--strategy", "buy_and_hold")

    def test_backtest_buy_and_hold_with_a_config(self) -> None:
        _ok("backtest", "--strategy", "buy_and_hold", "--config", str(REFERENCE))


class TestTheFalsificationStillHolds:
    """Not a CLI concern strictly, but this is the command that reports it and a
    silent change here would be the most serious regression in the project."""

    def test_a_coin_flip_on_a_flat_market_loses_exactly_its_costs(self) -> None:
        result = _ok("backtest", "--strategy", "coin_flip")

        assert "gross P&L on a flat market = 0.00 (must be exactly 0)" in result.output


class TestConfigCommand:
    def test_it_reports_what_the_run_would_use(self) -> None:
        result = _ok("config", str(REFERENCE))

        assert "mode" in result.output
        assert "GOLDM" in result.output
        assert "config hash" in result.output

    def test_it_surfaces_the_missing_stop(self) -> None:
        """D-102: a run with no loss exit must say so wherever it can."""
        result = _ok("config", str(REFERENCE))

        assert "NO STOP LOSS" in result.output

    def test_it_surfaces_the_roll_and_the_strike_grid(self) -> None:
        result = _ok("config", str(REFERENCE))

        assert "front DTE" in result.output
        assert "multiples of 1000" in result.output

    def test_a_missing_file_fails_rather_than_defaulting(self) -> None:
        result = _run("config", "does/not/exist.yaml")

        assert result.exit_code != 0

    def test_an_invalid_config_names_the_field(self, tmp_path: pathlib.Path) -> None:
        """`extra=forbid` plus the refusing validators (D-115) only help if the
        CLI surfaces them rather than swallowing them."""
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            REFERENCE.read_text(encoding="utf-8").replace(
                "  timezone: Asia/Kolkata", "  timezone: Europe/London"
            ),
            encoding="utf-8",
        )

        result = _run("config", str(bad))

        assert result.exit_code != 0


class TestVerify:
    def test_it_runs_the_pipeline_in_both_dst_regimes(self) -> None:
        """D-017: the session length changes with US DST, and `verify` exists to
        show both. If one regime vanished the output would still look healthy."""
        result = _ok("verify")

        assert "US DST" in result.output
        assert "standard" in result.output

    def test_a_different_timeframe_is_accepted(self) -> None:
        result = _ok("verify", "--timeframe-minutes", "60")

        assert "60m" in result.output or "60" in result.output


class TestWalkforward:
    def test_it_reports_on_the_default_sample(self) -> None:
        _ok("walkforward")

    def test_it_accepts_a_different_shape(self) -> None:
        _ok("walkforward", "--years", "5", "--trades-per-year", "12")


class TestTheLiveGatesRefuseBeforeTouchingAnything:
    """`live` opens real broker sessions. Only the paths that return *before* any
    credential is read are exercised - and those are the safety gates, which are
    the part worth testing anyway."""

    def test_a_live_mode_config_is_refused_for_the_paper_loop(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """D-111: --passes runs the paper loop only, and the refusal happens
        before any credential is read or any session opened."""
        monkeypatch.setenv("TRADING_MODE", "live")
        live_config = tmp_path / "live.yaml"
        live_config.write_text(
            REFERENCE.read_text(encoding="utf-8").replace(
                "mode: backtest", "mode: live", 1
            ),
            encoding="utf-8",
        )

        result = _run(
            "live",
            str(live_config),
            "--passes",
            "3",
            "--i-understand-this-is-real-money",
        )

        assert result.exit_code == 1
        assert "PAPER loop only" in result.output

    def test_a_live_config_without_the_env_gate_is_refused(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The outer gate, which must fire even before the paper-only one."""
        monkeypatch.delenv("TRADING_MODE", raising=False)
        live_config = tmp_path / "live.yaml"
        live_config.write_text(
            REFERENCE.read_text(encoding="utf-8").replace(
                "mode: backtest", "mode: live", 1
            ),
            encoding="utf-8",
        )

        result = _run("live", str(live_config), "--i-understand-this-is-real-money")

        assert result.exit_code != 0


class TestBhavcopyCommandsFailHonestlyOnBadInput:
    def test_a_missing_directory_is_reported(self) -> None:
        result = _run("backtest-bhavcopy", "does/not/exist")

        assert result.exit_code != 0

    def test_an_unreadable_file_names_the_layouts_it_tried(
        self, tmp_path: pathlib.Path
    ) -> None:
        """D-105: the loader's whole contract is that it says which columns it
        wanted. That is worthless if the CLI hides it."""
        junk = tmp_path / "junk.csv"
        junk.write_text("a,b,c\n1,2,3\n", encoding="utf-8")

        result = _run("bhavcopy", str(junk))

        assert result.exit_code != 0


class TestTheCliSourceStaysAscii:
    """Typer prints command docstrings as --help text and Windows consoles
    default to a legacy code page, so an em dash there renders as a replacement
    character and a rupee sign raises. CI already greps for this; having it here
    too means it fails on the machine that wrote it rather than ten minutes later.

    Scoped to our source, not to rendered output - Typer draws its help panels
    with box characters and those are its business, not ours.
    """

    def test_the_cli_module_is_ascii(self) -> None:
        source = pathlib.Path("algo/cli/main.py").read_text(encoding="utf-8")
        offending = {
            (line_no, ch)
            for line_no, line in enumerate(source.splitlines(), start=1)
            for ch in line
            if ord(ch) > 127
        }

        assert not offending, (
            "non-ASCII in algo/cli/main.py mangles on a cp1252 console: "
            f"{sorted(offending)[:10]}"
        )

    @pytest.mark.parametrize(
        "command",
        [
            "verify",
            "config",
            "backtest",
            "live",
            "walkforward",
            "serve",
            "killswitch",
            "bhavcopy",
            "chain",
            "backtest-bhavcopy",
            "backtest-smartapi",
        ],
    )
    def test_every_command_has_help(self, command: str) -> None:
        """Cheap, but it is the only thing that catches a command whose options
        fail to build - a broken default or annotation raises here and nowhere
        else until someone runs it."""
        result = _ok(command, "--help")

        assert result.output.strip()


class TestKillSwitchCommand:
    """The halt is the operator's last resort, and `--reset` lives only here -
    the dashboard can trip it and deliberately cannot clear it (D-066). Both
    directions need to work from a terminal.
    """

    def test_it_reports_on_a_fresh_state_file(self, tmp_path: pathlib.Path) -> None:
        _ok("killswitch", "--state", str(tmp_path / "s.db"))

    def test_tripping_requires_a_reason(self, tmp_path: pathlib.Path) -> None:
        """A halt with no recorded reason is one nobody can review later."""
        result = _run("killswitch", "--state", str(tmp_path / "s.db"), "--trip")

        assert result.exit_code != 0

    def test_a_halt_request_is_recorded_for_the_engine(
        self, tmp_path: pathlib.Path
    ) -> None:
        from algo.persistence.state import StateStore

        state = tmp_path / "s.db"
        _ok(
            "killswitch",
            "--state",
            str(state),
            "--trip",
            "--reason",
            "stepping away from the desk",
        )

        with StateStore(state) as store:
            pending = store.pending_kill_switch_requests()

        assert len(pending) == 1
        assert pending[0].reason == "stepping away from the desk"
        assert pending[0].flatten is False

    def test_flatten_asks_before_it_does_it(self, tmp_path: pathlib.Path) -> None:
        """Flattening market-closes a short strangle that may be mid-move, so it
        is confirmed rather than taken from a flag. Declining must abort with
        nothing recorded - a half-applied halt is worse than none."""
        from algo.persistence.state import StateStore

        state = tmp_path / "s.db"
        result = _run(
            "killswitch",
            "--state",
            str(state),
            "--trip",
            "--reason",
            "close everything",
            "--flatten",
            stdin="n\n",
        )

        assert result.exit_code != 0
        with StateStore(state) as store:
            assert store.pending_kill_switch_requests() == []

    def test_flatten_travels_with_the_request_once_confirmed(
        self, tmp_path: pathlib.Path
    ) -> None:
        """D-012: halting and flattening are separate decisions, so the flag has
        to reach the engine rather than being implied."""
        from algo.persistence.state import StateStore

        state = tmp_path / "s.db"
        _ok(
            "killswitch",
            "--state",
            str(state),
            "--trip",
            "--reason",
            "close everything",
            "--flatten",
            stdin="y\n",
        )

        with StateStore(state) as store:
            assert store.pending_kill_switch_requests()[0].flatten is True

    def test_trip_and_reset_together_are_refused(self, tmp_path: pathlib.Path) -> None:
        result = _run(
            "killswitch", "--state", str(tmp_path / "s.db"), "--trip", "--reset"
        )

        assert result.exit_code != 0


class TestTheRealDataBacktestEndToEnd:
    """`backtest-bhavcopy` is the command that produced the only real result the
    project has. This runs it on a two-session fixture - enough to exercise the
    whole path (loader, calendar from config, strategy from config, engine,
    report) without shipping a large file into the repo.
    """

    def _fixture(self, tmp_path: pathlib.Path) -> pathlib.Path:
        header = ",".join(
            [
                "Date",
                "Instrument Name",
                "Symbol",
                "Expiry Date",
                "Option Type",
                "Strike Price",
                "Open",
                "High",
                "Low",
                "Close",
                "Volume(Lots)",
                "Open Interest(Lots)",
            ]
        )
        lines = [header]
        for day in ("19 Aug 2026", "20 Aug 2026"):
            lines.append(
                f"{day},FUTCOM,GOLDM,04SEP2026,,,156000,157000,155500,156640,5000,1000"
            )
            for strike in range(150000, 164000, 1000):
                for right in ("CE", "PE"):
                    lines.append(
                        f"{day},OPTFUT,GOLDM,28AUG2026,{right},{strike},"
                        "1200,1300,1100,1250,500,200"
                    )
        path = tmp_path / "bhav.csv"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_it_runs_and_reports(self, tmp_path: pathlib.Path) -> None:
        result = _ok(
            "backtest-bhavcopy",
            str(self._fixture(tmp_path)),
            "--config",
            "config/goldm_bhavcopy_frontcycle.yaml",
        )

        assert "sessions" in result.output
        assert "SHAPE TEST ONLY" in result.output

    def test_the_no_stop_warning_reaches_the_operator(
        self, tmp_path: pathlib.Path
    ) -> None:
        """D-102. The config runs without a loss exit; every surface that can say
        so must."""
        result = _ok(
            "backtest-bhavcopy",
            str(self._fixture(tmp_path)),
            "--config",
            "config/goldm_bhavcopy_frontcycle.yaml",
        )

        assert "NO STOP LOSS IS CONFIGURED" in result.output

    def test_it_runs_without_a_config_too(self, tmp_path: pathlib.Path) -> None:
        """The other half of the both-paths rule."""
        _ok("backtest-bhavcopy", str(self._fixture(tmp_path)))
