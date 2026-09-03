"""Best-effort operator progress, never experiment or qualification evidence.

Library calls are silent unless enclosed in ``progress_output``. A scoped
reporter reaches nested controllers without changing their action signatures.
Only deliberate labels and existing numeric observations belong here: never
pass subprocess arguments, captured output, HTTP payloads, or AWS credentials.
"""

import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from re import sub
from threading import Event, RLock, Thread
from time import monotonic

StatusReporter = Callable[[str], None]


def terminal_status(message: str) -> None:
    """Flush one timestamped local-time line, also when stderr is redirected."""
    stamp = datetime.now().astimezone().strftime("%H:%M:%S%z")
    print(f"[{stamp}] {message}", file=sys.stderr, flush=True)


def elapsed_text(seconds: float) -> str:
    minutes, seconds = divmod(max(0, int(seconds)), 60)
    return f"{minutes}m{seconds:02d}s" if minutes else f"{seconds}s"


class _Progress:
    def __init__(self, reporter: StatusReporter, clock: Callable[[], float]):
        self.reporter: StatusReporter | None = reporter
        self.clock = clock
        self.lock = RLock()
        self.activities: list[tuple[str, float]] = []
        self.last_message_at = clock()

    def emit(self, message: str) -> None:
        with self.lock:
            if self.reporter is None:
                return
            try:
                # Strip terminal controls/newlines from otherwise safe labels.
                self.reporter("".join(c if c.isprintable() else " " for c in message))
            except Exception:  # noqa: BLE001 - a broken reporter must not affect cleanup
                self.reporter = None
            self.last_message_at = self.clock()

    def repeat_activity(self, interval: float) -> None:
        with self.lock:
            if self.activities and self.clock() - self.last_message_at >= interval:
                label, started = self.activities[-1]
                self.emit(f"{label} — elapsed {elapsed_text(self.clock() - started)}")


_current: ContextVar[_Progress | None] = ContextVar("operator_progress", default=None)


@contextmanager
def progress_output(
    reporter: StatusReporter | None = terminal_status,
    *,
    repeat_interval_seconds: float = 60,
    clock: Callable[[], float] = monotonic,
) -> Iterator[None]:
    """Scope an injectable sink and a cancellable elapsed-time activity ticker.

    The ticker reports the most specific active operation during blocking calls,
    not a generic heartbeat. No AWS calls, subprocess reads or artifact writes
    occur here. Pass interval=0 for deterministic tests without a background thread.
    """
    progress = _Progress(reporter, clock) if reporter is not None else None
    token = _current.set(progress)
    stopped = Event()
    thread = None

    def repeat() -> None:
        delay = repeat_interval_seconds
        while not stopped.wait(delay):
            progress.repeat_activity(repeat_interval_seconds)
            with progress.lock:
                delay = (
                    max(
                        0.01,
                        repeat_interval_seconds
                        - (progress.clock() - progress.last_message_at),
                    )
                    if progress.activities and progress.reporter is not None
                    else repeat_interval_seconds
                )

    try:
        if progress is not None and repeat_interval_seconds > 0:
            thread = Thread(target=repeat, name="operator-progress", daemon=True)
            thread.start()
        yield
    finally:
        stopped.set()
        _current.reset(token)
        if thread is not None:
            thread.join(timeout=1)


def operator_status(message: str) -> None:
    progress = _current.get()
    if progress is not None:
        progress.emit(message)


def operator_failure(label: str, error: BaseException) -> None:
    """Print the safe leaf action/reason, never arbitrary client exception text."""
    seen: set[int] = set()
    while id(error) not in seen and len(seen) < 20:
        seen.add(id(error))
        nested = getattr(error, "workflow_error", None)
        if not isinstance(nested, BaseException):
            break
        error = nested
    reason = type(error).__name__
    if type(error).__module__.startswith("trackrelay.aws_") and not getattr(
        error, "cleanup_errors", None
    ):
        reason += (
            ": " + sub(r"arn:[^\s)]+|https?://[^\s)]+", "[resource]", str(error))[:1000]
        )
    operator_status(f"FAILED: {label} — {reason}")


@contextmanager
def status_activity(label: str) -> Iterator[None]:
    """Announce start/end and expose elapsed time while an operation blocks."""
    progress = _current.get()
    if progress is None:
        yield
        return
    started = progress.clock()
    with progress.lock:
        progress.activities.append((label, started))
        progress.emit(f"Starting: {label}")
    try:
        yield
    except BaseException:
        progress.emit(f"Stopped: {label} ({elapsed_text(progress.clock() - started)})")
        raise
    else:
        progress.emit(
            f"Completed: {label} ({elapsed_text(progress.clock() - started)})"
        )
    finally:
        with progress.lock:
            progress.activities.pop()


class PeriodicStatus:
    """Rate-limit numeric loop updates using the loop's existing elapsed clock."""

    def __init__(self, interval_seconds: float = 60):
        self.interval_seconds = interval_seconds
        self.next_at = 0.0

    def update(self, elapsed_seconds: float, message: str) -> None:
        if elapsed_seconds >= self.next_at:
            operator_status(message)
            self.next_at = elapsed_seconds + self.interval_seconds
