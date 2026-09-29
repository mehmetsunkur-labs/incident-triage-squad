"""Schema tests (plan step 3)."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from triage.config import PROJECT_ROOT
from triage.schemas import (
    MODEL_OUTPUTS, ChangeFinding, HypothesisCheck, LogFinding, RunbookInput, RunbookOut,
    RunbookResult, SpecialistRun, LogAnalystResult, TriageResult,
)

NOW = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)


def walk(node, path="$"):
    if isinstance(node, dict):
        yield path, node
        for k, v in node.items():
            yield from walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk(v, f"{path}[{i}]")


@pytest.mark.parametrize("model", MODEL_OUTPUTS, ids=lambda m: m.__name__)
def test_structured_output_compatible(model):
    schema = model.model_json_schema()
    for path, node in walk(schema):
        if node.get("type") == "object":
            assert "properties" in node, f"{path}: free-form object"
            assert node.get("additionalProperties") is False, f"{path}: additionalProperties"
            assert set(node.get("required", [])) == set(node["properties"]), f"{path}: optional fields"
        for unsupported in ("minLength", "maxLength", "minimum", "maximum", "minItems", "pattern"):
            assert unsupported not in node, f"{path}: {unsupported} is not enforced by structured outputs"


def test_evidence_format_rejects_log_text():
    with pytest.raises(ValidationError, match="evidence must be"):
        LogFinding(ts=NOW, service="s", kind="error", observation="o", attributes=[],
                   evidence=["stub.log:1 — 2026-01-01T10:00:00Z stub ERROR stub failure"])


def test_finding_needs_evidence():
    with pytest.raises(ValidationError, match="must not be empty"):
        LogFinding(ts=NOW, service="s", kind="error", observation="o", attributes=[], evidence=[])


def test_naive_timestamp_rejected():
    with pytest.raises(ValidationError):
        LogFinding(ts=datetime(2026, 1, 1, 10, 0), service="s", kind="error", observation="o",
                   attributes=[], evidence=["a.log:1"])


def test_hypothesis_evidence_required_unless_inconclusive():
    HypothesisCheck(theory="t", verdict="inconclusive", why="w", evidence=[])
    with pytest.raises(ValidationError):
        HypothesisCheck(theory="t", verdict="ruled_out", why="w", evidence=[])


def test_change_cites_itself():
    kw = dict(title="t", effective_at=NOW, services=[], kind="config", keys_changed=[],
              reverted_by=None, relevance="r")
    ChangeFinding(change_id="CHG-9042", evidence=["CHG-9042"], **kw)
    with pytest.raises(ValidationError, match="its own id"):
        ChangeFinding(change_id="CHG-9042", evidence=["CHG-9001"], **kw)


@pytest.mark.parametrize("symptom", [
    "errors since 2026-01-01T10:00:00Z", "see orders-api.log:12", "after CHG-9042 shipped"])
def test_runbook_input_is_symptom_only(symptom):
    with pytest.raises(ValidationError, match="symptom only"):
        RunbookInput(symptom=symptom, service=None, observed=[])


def test_runbook_result_consistency():
    with pytest.raises(ValidationError):
        RunbookResult(matched=None, why="w", remediation=["step"], considered=[], fallback=None)


def test_archived_entry_never_matched_in_output():
    with pytest.raises(ValidationError):
        RunbookOut(matched="LEG-014", title="t", source="_archive/x.md", why="w", remediation=[])


def test_run_status_and_result_agree():
    kw = dict(agent="log_analyst", error=None, tool_calls=0, queries=[], evidence_seen=[],
              started_at=NOW, finished_at=NOW)
    with pytest.raises(ValidationError):
        SpecialistRun[LogAnalystResult](status="ok", result=None, **kw)
    SpecialistRun[LogAnalystResult](status="timeout", result=None, **kw)


def grader_dir() -> Path:
    return Path(os.environ.get("GRADER_DIR", PROJECT_ROOT.parent / "agentic_exercise_grader"))


@pytest.mark.skipif(not (grader_dir() / "golden").is_dir(), reason="grader repo not found (set GRADER_DIR)")
@pytest.mark.parametrize("incident", ["INC-2043", "INC-2051", "INC-2062"])
def test_golden_reference_validates(incident):
    """Only checks that our TriageResult accepts a known-good output; the content is never
    copied into this repo (D10 isolation)."""
    reference = json.loads((grader_dir() / "golden" / f"{incident}.json").read_text())["reference"]
    TriageResult.model_validate(reference)
