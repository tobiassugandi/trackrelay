"""Liveness probe semantics must not import application/database code."""

from types import SimpleNamespace

from pytest import mark

from trackrelay.healthcheck import check_liveness, main


@mark.parametrize("status,expected", [(200, True), (202, False), (500, False)])
def test_probe_requires_200_and_closes_connection(monkeypatch, status, expected):
    calls = []

    def connection(host, port, timeout):
        assert (host, port, timeout) == ("127.0.0.1", 8001, 2)
        return SimpleNamespace(
            request=lambda *args: calls.append(args),
            getresponse=lambda: SimpleNamespace(status=status),
            close=lambda: calls.append("closed"),
        )

    monkeypatch.setattr("trackrelay.healthcheck.HTTPConnection", connection)
    assert check_liveness(8001) is expected
    assert calls == [("GET", "/health/live"), "closed"]


def test_unreachable_probe_fails_closed(monkeypatch):
    closed = []

    def failed_request(*args):
        raise TimeoutError

    monkeypatch.setattr(
        "trackrelay.healthcheck.HTTPConnection",
        lambda *a, **kw: SimpleNamespace(
            request=failed_request, close=lambda: closed.append(True)
        ),
    )
    assert not check_liveness(8000)
    assert closed == [True]


@mark.parametrize("args", [[], ["9999"], ["8001", "extra"]])
def test_cli_rejects_other_targets(monkeypatch, args):
    monkeypatch.setattr("sys.argv", ["healthcheck", *args])
    assert main() == 1
