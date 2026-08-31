"""Cooperative shutdown for the live loop.

`LiveLoop.run` is bounded by `max_passes` and `until`, which stops a loop that
runs to completion. It does nothing for the case that actually happens: an
operator pressing Ctrl-C, or a service manager sending SIGTERM, at whatever
instant it happens to arrive.

## The window this exists to close

Python's default SIGINT handler raises `KeyboardInterrupt` at the next
bytecode boundary. Inside `pass_once` that boundary can fall between
`router.place()` handing an order to the broker and the journal recording what
came back - which is precisely the state the journal's `SENT` marker describes
and precisely the state nobody wants to restart from. The order may or may not
exist at the broker, and the local record says "we were in the middle of
finding out".

So the handler here does **not** raise. It sets a flag and returns, the
in-flight pass finishes on its own terms, and the loop stops at the boundary
between passes - the one place where the journal is consistent by construction.
`pass_once` is already the atomic unit of work (settle, decide, route); this
just makes it uninterruptible.

## A second signal still forces the issue

An operator who presses Ctrl-C twice means it, and refusing them would be its
own kind of failure - a loop that cannot be stopped is not safer than one that
stops messily. The second signal restores the default handler and re-raises, so
the usual `KeyboardInterrupt` path takes over. The first press is a request;
the second is an instruction.

## It is a request the loop reads, not an action it takes

Nothing here touches a position, a broker or the journal. It flips a boolean
that `LiveLoop.run` checks. That is the same shape as the kill switch (D-012:
the API records a request, the engine acts on it at its next bar) and for the
same reason: a signal handler runs at an arbitrary moment, and the list of
things it is safe to do there is very short.
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from types import FrameType

#: Signals worth catching, by name - looked up rather than referenced so this
#: imports cleanly on Windows, where SIGTERM exists but SIGBREAK is the one
#: Ctrl-Break actually sends and neither is guaranteed present everywhere.
_WANTED = ("SIGINT", "SIGTERM", "SIGBREAK")


class ShutdownRequest:
    """A stop asked for, and whether it has been asked for twice."""

    __slots__ = ("_count", "_lock", "_reason")

    def __init__(self) -> None:
        self._reason = ""
        self._count = 0
        # A handler can fire on any thread the interpreter chooses to run it on;
        # the flag is read from the loop thread.
        self._lock = threading.Lock()

    @property
    def requested(self) -> bool:
        with self._lock:
            return self._count > 0

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    def request(self, reason: str) -> int:
        """Record a stop request. Returns how many have now been made."""
        with self._lock:
            self._count += 1
            if self._count == 1:
                self._reason = reason
            return self._count


@contextmanager
def graceful_shutdown(
    *, on_request: object = None, force_on_second: bool = True
) -> Iterator[ShutdownRequest]:
    """Install handlers that ask the loop to stop rather than interrupting it.

    Restores whatever handlers were there before on the way out, so this can be
    used inside a larger process without leaving its signal table rearranged.

    Outside the main thread `signal.signal` raises, and a `ShutdownRequest` that
    nothing will ever set is still a perfectly good object for the loop to poll -
    so this degrades to "no handlers installed" rather than refusing to run.
    """
    request = ShutdownRequest()
    installed: dict[int, object] = {}

    def handle(signum: int, frame: FrameType | None) -> None:
        del frame
        name = signal.Signals(signum).name
        count = request.request(f"{name} received")
        if callable(on_request):
            on_request(count, name)
        if count >= 2 and force_on_second:
            # They mean it. Put the default back and let the next one through
            # as an ordinary KeyboardInterrupt.
            previous = installed.get(signum)
            if previous is not None:
                signal.signal(signum, previous)  # type: ignore[arg-type]
            raise KeyboardInterrupt(f"{name} twice - stopping now")

    for name in _WANTED:
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            installed[int(signum)] = signal.getsignal(signum)
            signal.signal(signum, handle)
        except (ValueError, OSError):
            # Not the main thread, or the platform will not have it. Either way
            # the loop still gets a pollable request object.
            installed.pop(int(signum), None)

    try:
        yield request
    finally:
        for signum, previous in installed.items():
            with suppress(ValueError, OSError):
                signal.signal(signum, previous)  # type: ignore[arg-type]


class StopFile:
    """A stop request made by creating a file, for loops nobody can Ctrl-C.

    `graceful_shutdown` covers the operator at the keyboard. It does not cover
    the loop started in the background, by a service manager, or from another
    console - and on Windows a POSIX signal does not cross that boundary at all,
    which is not a detail but the ordinary case for anything left running.

    So there is a second way in, and it is the shape this codebase already uses
    for the kill switch (D-012): a *request the loop reads*, not an action taken
    against it. Creating the file asks; the loop notices at its next boundary,
    finishes the pass in flight, and exits through exactly the same path a
    Ctrl-C would have taken. Nothing signals, nothing interrupts, and the
    journal stays consistent by construction.

    ## The file is removed by the loop, not by the asker

    A sentinel left behind would stop the *next* run the moment it started, and
    the operator would be left restarting a loop that keeps exiting for a reason
    they already dealt with. So the loop clears it on the way out, and clears a
    stale one on the way in - loudly, because "I asked it to stop and it started
    anyway" is a thing worth being told rather than discovering later.
    """

    __slots__ = ("_path",)

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def requested(self) -> bool:
        return self._path.exists()

    @property
    def reason(self) -> str:
        """Whatever the asker wrote in it, or a default naming the file.

        Read defensively: the file existing *is* the request, and a read that
        fails must not turn a stop into an exception inside the loop.
        """
        try:
            written = self._path.read_text(encoding="utf-8").strip()
        except OSError:
            written = ""
        return written or f"stop file {self._path} present"

    def request(self, reason: str = "") -> None:
        """Ask the loop to stop. Safe to call when one is already pending."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(reason or "stop requested", encoding="utf-8")

    def clear(self) -> bool:
        """Remove it. Returns whether there was one to remove."""
        try:
            self._path.unlink()
        except FileNotFoundError:
            return False
        except OSError:
            # Unremovable is worth knowing about but is not worth crashing a
            # loop that has already stopped.
            return False
        return True
