"""result.md rendering (D8)."""

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from triage import orchestrator
from triage.cli import app
from triage.config import PROJECT_ROOT
from triage.render import render
from triage.schemas import TriageResult

MINIMAL = {
    "incident_id": "INC-9001", "summary": "Orders fail.",
    "timeline": [{"time": "2026-01-01T10:04:10Z", "event": "Errors | start", "evidence": ["orders-api.log:12"]}],
    "root_cause": {"statement": "A timeout was lowered.", "confidence": "low",
                   "evidence": ["orders-api.log:12", "CHG-9042"], "correlation": "Same service, 2 min apart."},
    "ruled_out": [],
    "runbook": {"matched": None, "title": None, "source": None, "why": "Nothing current fits.", "remediation": []},
    "actions": [{"action": "Follow RB-000.", "urgency": "now", "source": "RB-000"}],
    "stakeholder_update": None, "open_questions": ["Which replica?"],
}


def test_render_minimal_no_match():
    md = render(TriageResult.model_validate(MINIMAL))
    assert md.startswith("# Triage: INC-9001\n")
    assert "**No current runbook entry matched.**" in md
    assert "| 2026-01-01 10:04:10 | Errors \\| start | `orders-api.log:12` |" in md
    assert "Nothing ruled out." in md and "- Which replica?" in md
    assert "Stakeholder update" not in md


GRADER = Path(os.environ.get("GRADER_DIR", PROJECT_ROOT.parent / "agentic_exercise_grader")) / "golden"


@pytest.mark.skipif(not GRADER.is_dir(), reason="grader repo not found (set GRADER_DIR)")
@pytest.mark.parametrize("incident", ["INC-2043", "INC-2051", "INC-2062"])
def test_render_golden_reference(incident):
    """Renders a known-good result; only checks structure, never copies content."""
    result = TriageResult.model_validate(json.loads((GRADER / f"{incident}.json").read_text())["reference"])
    md = render(result)
    for heading in ("## Root cause", "## Timeline", "## Ruled out", "## Runbook", "## Actions", "## Open questions"):
        assert heading in md
    assert md.count("\n| ") >= len(result.timeline) + len(result.actions)
    if result.runbook.matched:
        assert f"**{result.runbook.matched}:" in md
        assert all(step in md for step in result.runbook.remediation)


def test_cli_writes_result_md(tmp_path, monkeypatch):
    async def fake_run(*args, **kwargs):
        return TriageResult.model_validate(MINIMAL)

    monkeypatch.setenv("TRIAGE_OUT_DIR", str(tmp_path))
    monkeypatch.setattr(orchestrator, "run_incident", fake_run)
    r = CliRunner().invoke(app, ["run", "INC-2043"])
    assert r.exit_code == 0, r.output
    [run_dir] = (tmp_path / "runs").iterdir()
    assert json.loads((run_dir / "result.json").read_text())["incident_id"] == "INC-9001"
    assert (run_dir / "result.md").read_text().startswith("# Triage: INC-9001")
