"""Public paired data integrity and export refusal paths; no AWS access."""

from json import dumps, loads
from pathlib import Path

from pytest import mark, raises

from scripts.render_paired_results import export_data, render
from tests.test_aws_elasticity_report import fake_plotter, saved_session
from trackrelay.aws_elasticity_report import generate_elasticity_report

PUBLIC_DATA = Path(__file__).resolve().parents[1] / "docs/assets/paired/data.json"


def test_public_data_supports_backlog_claim_but_not_capacity_multiplier(tmp_path):
    data = loads(PUBLIC_DATA.read_text())
    fixed, elastic = (data["treatments"][k] for k in ("fixed", "elastic"))
    maxima = [
        max(p["outstanding"] for p in t["observations"]) for t in (fixed, elastic)
    ]
    assert maxima == [3583, 529]
    assert round(100 * (1 - maxima[1] / maxima[0]), 1) == 85.2
    assert data["comparison_multiplier"] is None
    for treatment in (fixed, elastic):
        assert treatment["highest_supported_rate"] is None
        assert not treatment["step_support"][0]["supported"]
        assert sum(w["sample_count"] for w in treatment["latency_windows"]) == 5730
        assert treatment["reconciliation"]["processed"] == 5730
        for p in treatment["observations"]:
            assert p["outstanding"] == p["accepted"] - p["completed"]
    for marker in (
        "arn:aws:",
        "amazonaws.com",
        "account_id",
        "raw_payload",
        "access_key",
    ):
        assert marker not in PUBLIC_DATA.read_text()
    render(data, tmp_path)
    assert (tmp_path / "comparison.png").read_bytes().startswith(b"\x89PNG")
    assert (
        "No capacity multiplier established"
        in (tmp_path / "comparison.svg").read_text()
    )


@mark.parametrize("corrupt", [False, True])
def test_export_revalidates_report_and_does_not_change_sources(
    tmp_path, monkeypatch, corrupt
):
    root = saved_session(tmp_path / "session")
    journal_path = root / "session.json"
    journal = loads(journal_path.read_text())
    journal["elasticity_session"] = {
        "phase": "cloud_complete",
        "failed_phase": None,
        "workflow_error": None,
        "cleanup_errors": [],
        "diagnostic_errors": [],
    }
    # A raw journal endpoint must never leak into the selected public data.
    journal["private_endpoint"] = "https://private.example.invalid"
    journal_path.write_text(dumps(journal))
    generate_elasticity_report(root, plotter=fake_plotter)
    if corrupt:
        p = root / "elasticity/report/comparison-report.json"
        data = loads(p.read_text())
        data["conclusion"] = "Altered favorable headline"
        p.write_text(dumps(data))
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}

    def prohibited(*args, **kwargs):
        raise AssertionError("Export attempted an external command")

    monkeypatch.setattr("subprocess.run", prohibited)
    if corrupt:
        with raises(ValueError, match="matching qualified report"):
            export_data(root)
    else:
        data = export_data(root)
        assert "private.example.invalid" not in dumps(data)
        assert data["treatments"]["fixed"]["reconciliation"]["processed"] == 5730
    assert all(p.read_bytes() == content for p, content in before.items())
