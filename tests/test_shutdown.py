"""Graceful shutdown: the loop stops between passes, never inside one.

The property that matters is not "Ctrl-C works" - the default handler already
does something. It is that a stop requested *during* a pass does not truncate
that pass, because the window it would truncate is the one between an order
reaching the broker and the journal recording what came back. A test that only
asserted "the loop stopped" would pass just as happily against the dangerous
behaviour.
"""

from __future__ import annotations

import signal
import threading
from pathlib import Path

import pytest

from algo.live.shutdown import ShutdownRequest, StopFile, graceful_shutdown


class TestShutdownRequest:
    def test_it_starts_unrequested(self) -> None:
        assert ShutdownRequest().requested is False

    def test_a_request_is_recorded_with_its_reason(self) -> None:
        request = ShutdownRequest()

        assert request.request("SIGINT received") == 1
        assert request.requested is True
        assert request.reason == "SIGINT received"

    def test_the_first_reason_is_kept(self) -> None:
        """A second signal escalates; it does not rewrite why the stop began."""
        request = ShutdownRequest()
        request.request("SIGINT received")
        request.request("SIGTERM received")

        assert request.count == 2
        assert request.reason == "SIGINT received"

    def test_it_is_safe_to_set_from_another_thread(self) -> None:
        """A handler may run on whichever thread the interpreter picks, while
        the loop reads the flag from its own."""
        request = ShutdownRequest()
        done = threading.Event()

        def ask() -> None:
            request.request("from a thread")
            done.set()

        thread = threading.Thread(target=ask)
        thread.start()
        done.wait(timeout=5)
        thread.join(timeout=5)

        assert request.requested is True


class TestHandlersAreRestored:
    def test_the_previous_handler_comes_back(self) -> None:
        """This can be used inside a longer-lived process, so it must not leave
        the signal table rearranged behind it."""
        original = signal.getsignal(signal.SIGINT)

        with graceful_shutdown():
            assert signal.getsignal(signal.SIGINT) is not original

        assert signal.getsignal(signal.SIGINT) is original

    def test_a_signal_sets_the_flag_rather_than_raising(self) -> None:
        """The whole point: the handler returns, so the in-flight pass finishes.
        A handler that raised would interrupt exactly where it must not."""
        with graceful_shutdown() as stopping:
            handler = signal.getsignal(signal.SIGINT)
            assert callable(handler)

            handler(int(signal.SIGINT), None)  # no exception

            assert stopping.requested is True
            assert "SIGINT" in stopping.reason

    def test_a_second_signal_forces(self) -> None:
        with graceful_shutdown() as stopping:
            handler = signal.getsignal(signal.SIGINT)
            assert callable(handler)
            handler(int(signal.SIGINT), None)

            with pytest.raises(KeyboardInterrupt):
                handler(int(signal.SIGINT), None)

            assert stopping.count == 2

    def test_the_callback_sees_each_request(self) -> None:
        seen: list[tuple[int, str]] = []

        with graceful_shutdown(on_request=lambda n, name: seen.append((n, name))):
            handler = signal.getsignal(signal.SIGINT)
            assert callable(handler)
            handler(int(signal.SIGINT), None)

        assert seen == [(1, "SIGINT")]


class TestStopFile:
    """The route that works when there is no terminal to Ctrl-C.

    A background loop, a service manager, or another console - and on Windows a
    POSIX signal does not cross that boundary at all, so without this the only
    option is killing the process, which is exactly what the graceful path
    exists to avoid.
    """

    def test_absent_means_no_request(self, tmp_path: Path) -> None:
        assert StopFile(tmp_path / "STOP").requested is False

    def test_creating_it_is_the_request(self, tmp_path: Path) -> None:
        sentinel = StopFile(tmp_path / "STOP")

        sentinel.request("going to bed")

        assert sentinel.requested is True
        assert sentinel.reason == "going to bed"

    def test_it_creates_missing_parent_directories(self, tmp_path: Path) -> None:
        """`state/` may not exist on a fresh checkout, and failing to ask a
        loop to stop because of a missing directory would be absurd."""
        sentinel = StopFile(tmp_path / "nested" / "deeper" / "STOP")

        sentinel.request()

        assert sentinel.requested is True

    def test_an_empty_file_still_reads_as_a_request(self, tmp_path: Path) -> None:
        """`touch state/STOP` is a perfectly reasonable way to ask. The file
        existing is the request; its contents are only an explanation."""
        path = tmp_path / "STOP"
        path.write_text("", encoding="utf-8")
        sentinel = StopFile(path)

        assert sentinel.requested is True
        assert "STOP" in sentinel.reason

    def test_clearing_reports_whether_there_was_one(self, tmp_path: Path) -> None:
        sentinel = StopFile(tmp_path / "STOP")

        assert sentinel.clear() is False  # nothing there
        sentinel.request()
        assert sentinel.clear() is True
        assert sentinel.requested is False

    def test_requesting_twice_is_harmless(self, tmp_path: Path) -> None:
        sentinel = StopFile(tmp_path / "STOP")

        sentinel.request("first")
        sentinel.request("second")

        assert sentinel.reason == "second"

    def test_an_unreadable_file_still_stops_the_loop(self, tmp_path: Path) -> None:
        """The file existing is the request. A `reason` that cannot be read must
        degrade to a default, never raise into the loop that is asking."""
        path = tmp_path / "STOP"
        path.mkdir()  # a directory where a file is expected - read_text raises
        sentinel = StopFile(path)

        assert sentinel.requested is True
        assert sentinel.reason  # a usable string, not an exception


class TestEitherRouteStopsTheLoop:
    """`should_stop` is one question with two askers, so the loop needs no
    knowledge of which one it was."""

    def test_the_signal_route_alone(self, tmp_path: Path) -> None:
        request = ShutdownRequest()
        sentinel = StopFile(tmp_path / "STOP")

        def should_stop() -> bool:
            return request.requested or sentinel.requested

        assert should_stop() is False
        request.request("SIGINT received")
        assert should_stop() is True

    def test_the_file_route_alone(self, tmp_path: Path) -> None:
        request = ShutdownRequest()
        sentinel = StopFile(tmp_path / "STOP")

        def should_stop() -> bool:
            return request.requested or sentinel.requested

        assert should_stop() is False
        sentinel.request("from another console")
        assert should_stop() is True
