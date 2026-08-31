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

import pytest

from algo.live.shutdown import ShutdownRequest, graceful_shutdown


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
