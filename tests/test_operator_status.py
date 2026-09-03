"""Operator convenience cannot affect correctness, privacy, or cleanup."""

from re import fullmatch
from threading import Event
from threading import enumerate as enumerate_threads

from pytest import mark, raises

from trackrelay.aws_elasticity_cloudwatch import AwsElasticityCloudWatchError
from trackrelay.aws_fixed_control import AwsFixedControlCleanupError
from trackrelay.operator_status import (
    PeriodicStatus,
    _Progress,
    elapsed_text,
    operator_failure,
    operator_status,
    progress_output,
    status_activity,
    terminal_status,
)


def test_library_is_silent_and_nested_reporters_are_scoped(capsys):
    outer, inner = [], []
    operator_status("not enabled")
    with status_activity("silent activity"):
        pass
    with progress_output(outer.append, repeat_interval_seconds=0):
        operator_status("outer")
        with progress_output(None):
            operator_status("quiet")
        with progress_output(inner.append, repeat_interval_seconds=0):
            operator_status("inner")
        operator_status("restored")
    operator_status("disabled again")
    assert outer == ["outer", "restored"]
    assert inner == ["inner"]
    assert not capsys.readouterr().err


def test_terminal_sink_uses_timestamped_stderr_and_flushes(monkeypatch):
    class Stream:
        def __init__(self):
            self.text = ""
            self.flushed = False

        def write(self, text):
            self.text += text

        def flush(self):
            self.flushed = True

    stream = Stream()
    monkeypatch.setattr("sys.stderr", stream)
    terminal_status("Phase 1/6: foundation")
    assert fullmatch(
        r"\[\d{2}:\d{2}:\d{2}[+-]\d{4}\] Phase 1/6: foundation\n", stream.text
    )
    assert stream.flushed


@mark.parametrize(
    "error", [BrokenPipeError(), OSError("closed"), ValueError("sink failed")]
)
def test_reporter_failure_is_disabled_without_interrupting_cleanup(error):
    calls = []

    def broken(message):
        calls.append(message)
        raise error

    with progress_output(broken, repeat_interval_seconds=0):
        with status_activity("destroy"):
            operator_status("still destroying")
        operator_status("verified")
    assert calls == ["Starting: destroy"]


def test_activity_elapsed_and_interrupt_never_claim_completion():
    messages = []
    time = [0]
    with progress_output(
        messages.append, clock=lambda: time[0], repeat_interval_seconds=0
    ):
        with status_activity("migration"):
            time[0] = 74
        with raises(KeyboardInterrupt), status_activity("load"):
            time[0] = 84
            raise KeyboardInterrupt
    assert messages == [
        "Starting: migration",
        "Completed: migration (1m14s)",
        "Starting: load",
        "Stopped: load (10s)",
    ]


def test_ticker_only_repeats_specific_active_operation_after_silence():
    messages = []
    time = [0]
    progress = _Progress(messages.append, lambda: time[0])
    progress.activities = [("Phase deployment", 0), ("ECS convergence", 15)]
    time[0] = 59
    progress.repeat_activity(60)
    assert not messages
    time[0] = 60
    progress.repeat_activity(60)
    assert messages == ["ECS convergence — elapsed 45s"]
    time[0] = 100
    progress.emit("API 2/2, worker 1/1, simulator 1/1")
    time[0] = 120
    progress.repeat_activity(60)
    assert len(messages) == 2
    progress.activities.clear()
    time[0] = 180
    progress.repeat_activity(60)
    assert len(messages) == 2


def test_real_activity_ticker_stops_when_scope_exits():
    tick = Event()
    messages = []

    def sink(message):
        messages.append(message)
        if "elapsed" in message:
            tick.set()

    with (
        progress_output(sink, repeat_interval_seconds=0.01),
        status_activity("Terraform apply"),
    ):
        assert tick.wait(2)
    assert messages[-1].startswith("Completed: Terraform apply")
    assert not any(thread.name == "operator-progress" for thread in enumerate_threads())


def test_real_ticker_is_cancelled_after_interrupt():
    with (
        raises(KeyboardInterrupt),
        progress_output(lambda message: None),
        status_activity("load"),
    ):
        raise KeyboardInterrupt
    assert not any(thread.name == "operator-progress" for thread in enumerate_threads())


def test_numeric_updates_are_minutely_not_per_observation():
    messages = []
    with progress_output(messages.append, repeat_interval_seconds=0):
        progress = PeriodicStatus()
        for elapsed in range(0, 131, 10):
            progress.update(elapsed, f"load {elapsed}/660s")
    assert messages == ["load 0/660s", "load 60/660s", "load 120/660s"]
    assert elapsed_text(660) == "11m00s"


def test_failure_uses_safe_leaf_and_never_raw_client_exception_text():
    messages = []
    leaf = AwsElasticityCloudWatchError("simulator_cpu has unpublished native buckets")
    nested = AwsFixedControlCleanupError(
        "wrapper credentials=private", workflow_error=leaf, cleanup_errors=[]
    )
    with progress_output(messages.append, repeat_interval_seconds=0):
        operator_failure("Phase fixed", nested)
        operator_failure("HTTP", RuntimeError("https://user:password@private/payload"))
        operator_status("label\n\x1b\rcontrol")
    assert "unpublished native buckets" in messages[0]
    assert "private" not in " ".join(messages)
    assert "password" not in " ".join(messages)
    assert messages[1] == "FAILED: HTTP — RuntimeError"
    assert all("\n" not in message and "\x1b" not in message for message in messages)
