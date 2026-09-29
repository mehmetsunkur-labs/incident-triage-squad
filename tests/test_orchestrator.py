"""The orchestrator without a model (plan step 9): the join, confidence rules, runbook input,
guardrails, and every D7 branch through run_incident with a fake run_specialist.

Specialist results here are fictional (orders-api, CHG-9042) except where a check needs the
real runbook text.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from triage import join, orchestrator
from triage.config import load_settings
from triage.faults import NO_FAULTS
from triage.guardrails import Guard, normalise, runbook_texts
from triage.schemas import (
    ChangeHistorianResult, LogAnalystResult, RunbookResult, SpecialistRun, SynthesisResult,
    TriageResult,
)
from triage.trace import Trace

T0 = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)


def at(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def finding(minutes, kind, obs, evidence, service="orders-api", **attrs):
    return {"ts": at(minutes), "service": service, "kind": kind, "observation": obs,
            "attributes": [{"key": k, "value": str(v)} for k, v in attrs.items()], "evidence": evidence}


ANALYST = {
    "findings": [
        finding(-1, "config", "Upstream client started with a lower timeout", ["orders-api.log:5"], timeout_ms=3000),
        finding(2, "error", "Requests fail on upstream timeout", ["orders-api.log:9"], status=504),
        finding(3, "error", "Gateway sees errors from orders-api", ["gateway.log:4"], service="gateway", upstream="orders-api"),
    ],
    "first_failure_at": at(2), "failure_mode": "Order requests fail with upstream timeouts after 3 seconds.",
    "hypotheses_checked": [{"theory": "The CDN is down", "verdict": "ruled_out", "why": "Edge fine", "evidence": ["edge.log:1"]}],
    "gaps": [],
}


def change(cid, minutes, key=None, old=None, new=None, services=("orders-api",)):
    return {"change_id": cid, "title": f"title {cid}", "effective_at": at(minutes), "services": list(services),
            "kind": "config", "keys_changed": [{"key": key, "old": old, "new": new}] if key else [],
            "reverted_by": None, "relevance": "r", "evidence": [cid]}


HISTORIAN = {"changes": [change("CHG-9042", -2, "upstream.timeout_ms", "10000", "3000"),
                         change("CHG-9043", -2, "feature.new_cart", "off", "on")],
             "hypotheses_checked": [], "gaps": []}


def la(data=ANALYST):
    return LogAnalystResult.model_validate(data)


def ch(data=HISTORIAN):
    return ChangeHistorianResult.model_validate(data)


def run_of(model, agent, result=None, status="ok", evidence=(), error=None):
    now = datetime.now(timezone.utc)
    return SpecialistRun[model](agent=agent, status=status, error=error, tool_calls=1, queries=["q"],
                                evidence_seen=sorted(evidence), started_at=now, finished_at=now,
                                result=result)


# ---- join

def test_value_match_is_strong_and_time_only_is_weak():
    cs = join.correlate(la(), ch())
    assert [(c.change_id, c.strength, c.link) for c in cs] == [
        ("CHG-9042", "strong", "same_service"), ("CHG-9043", "weak", "same_service")]
    top = cs[0]
    assert top.service == "orders-api" and top.minutes_before_first_failure == 4.0
    assert top.matched_keys == ["timeout_ms=3000 ~ upstream.timeout_ms", "same service: orders-api", "within 15 minutes"]
    assert set(top.log_evidence) == {"orders-api.log:5", "orders-api.log:9"}


def test_outside_lookback_after_failure_or_unlinked_does_not_correlate():
    h = {**HISTORIAN, "changes": [change("CHG-9001", 3), change("CHG-9002", -49 * 60),
                                  change("CHG-9003", -2, services=("billing",))]}
    assert join.correlate(la(), ch(h)) == []


def test_value_match_counts_hours_before_the_failure():
    """A change whose value shows up in the logs is strong even long before the failure,
    e.g. a feed change that only bites when a scheduled job next runs."""
    h = {**HISTORIAN, "changes": [change("CHG-9050", -8 * 60, "upstream.timeout_ms", "10000", "3000")]}
    [c] = join.correlate(la(), ch(h))
    assert c.strength == "strong" and "8.0 hours before" in c.matched_keys


def test_value_match_uses_the_findings_words_not_just_its_keys():
    a = {**ANALYST, "findings": [finding(2, "error", "Message failed validation", ["worker.log:4"],
                                         service="worker", field="settled_at", value="0000-00-00")],
         "first_failure_at": at(2)}
    h = {**HISTORIAN, "changes": [change("CHG-9060", -600, "settled_at (unsettled rows)", "null", '"0000-00-00"',
                                         services=("worker",))]}
    [c] = join.correlate(la(a), ch(h))
    assert c.strength == "strong" and c.matched_keys[0] == "value=0000-00-00 ~ settled_at (unsettled rows)"


def placed(minutes, evidence, service, node):
    """A platform line placing a service's pod on a node."""
    f = finding(minutes, "info", f"{service} pod moved to {node}", evidence, service="platform", node=node)
    f["attributes"].append({"key": "service", "value": service})
    return f


CO_LOCATED = {**ANALYST, "findings": ANALYST["findings"] + [
    placed(-30, ["platform.log:1"], "orders-api", "n-7"), placed(-29, ["platform.log:2"], "batch-encoder", "n-7")]}


def test_co_located_change_links_through_a_shared_node():
    h = {**HISTORIAN, "changes": [change("CHG-9070", -12 * 60, services=("batch-encoder",)),
                                  {**change("CHG-9071", -13 * 60, services=("all workloads on the affected nodes",)),
                                   "kind": "infrastructure"}]}
    cs = join.correlate(la(CO_LOCATED), ch(h))
    assert [(c.change_id, c.link, c.strength) for c in cs] == [
        ("CHG-9070", "co_located", "weak"), ("CHG-9071", "infrastructure", "weak")]
    assert "shares n-7 with orders-api: batch-encoder" in cs[0].matched_keys
    assert {"platform.log:1", "platform.log:2"} <= set(cs[0].log_evidence)
    ar = run_of(LogAnalystResult, "log_analyst", la(CO_LOCATED))
    hr = run_of(ChangeHistorianResult, "change_historian", ch(h))
    level, reason = join.confidence(ar, hr, cs)
    assert level == "medium" and "CHG-9070 is the most specifically linked" in reason


def test_upstream_attribute_counts_as_affected_service():
    assert join.affected_services(la()) == {"orders-api", "gateway"}


def test_no_first_failure_no_correlation():
    a = {**ANALYST, "first_failure_at": None, "findings": [ANALYST["findings"][0]]}
    assert join.correlate(la(a), ch()) == []


@pytest.mark.parametrize("case,expected", [
    ("missing", "low"), ("none", "low"), ("strong+ruled_out", "high"), ("strong", "medium"),
    ("two_strong", "low"), ("one_weak", "medium"), ("two_weak", "low")])
def test_confidence_rules(case, expected):
    a, h = la(), ch()
    ar = run_of(LogAnalystResult, "log_analyst", a)
    hr = run_of(ChangeHistorianResult, "change_historian", h)
    cs = join.correlate(a, h)
    if case == "missing":
        hr = run_of(ChangeHistorianResult, "change_historian", None, status="failed", error="x")
    elif case == "none":
        cs = []
    elif case == "strong":
        ar = run_of(LogAnalystResult, "log_analyst", la({**ANALYST, "hypotheses_checked": []}))
    elif case == "two_strong":
        cs = cs[:1] + [cs[0].model_copy(update={"change_id": "CHG-9044"})]
    elif case == "one_weak":
        cs = cs[1:]
    elif case == "two_weak":  # equally specific weak links
        cs = cs[1:] + [cs[1].model_copy(update={"change_id": "CHG-9045"})]
    level, reason = join.confidence(ar, hr, cs)
    assert level == expected and reason


# ---- runbook input

def test_runbook_input_is_symptom_only():
    inp, skip = orchestrator.build_runbook_input(run_of(LogAnalystResult, "log_analyst", la()))
    assert skip is None and inp.symptom == ANALYST["failure_mode"] and inp.service == "orders-api"
    assert [(o.key, o.value) for o in inp.observed] == [("status", "504"), ("upstream", "orders-api")]


def test_runbook_skipped_without_symptom_or_with_a_leaky_one():
    none = run_of(LogAnalystResult, "log_analyst", None, status="timeout", error="t")
    assert orchestrator.build_runbook_input(none)[0] is None
    leaky = la({**ANALYST, "failure_mode": "Fails since 2026-01-01T10:02:00Z"})
    inp, skip = orchestrator.build_runbook_input(run_of(LogAnalystResult, "log_analyst", leaky))
    assert inp is None and "symptom-only" in skip


# ---- guardrails

def test_guard_drops_unseen_citations_and_unsupported_claims():
    from triage.schemas import RuledOut
    g = Guard(seen={"orders-api.log:9"})
    kept = g.ruled_out([RuledOut(theory="A", why="w", evidence=["orders-api.log:9", "orders-api.log:99"]),
                        RuledOut(theory="B", why="w", evidence=["CHG-9999"])])
    assert [(r.theory, r.evidence) for r in kept] == [("A", ["orders-api.log:9"])]
    assert {v["check"] for v in g.violations} == {"citation_not_seen", "claim_unsupported"}
    assert any("Could not rule out" in q for q in g.open_questions)


def test_guard_adds_the_missing_corpus_from_the_correlation():
    cs = join.correlate(la(), ch())
    s = SynthesisResult(summary="s", root_cause_statement="r", root_cause_evidence=["CHG-9042"],
                        correlation="c", ruled_out=[], actions=[], open_questions=[])
    g = Guard(seen={"CHG-9042", "orders-api.log:5", "orders-api.log:9"})
    ev = g.root_cause_evidence(s, cs)
    assert "CHG-9042" in ev and "orders-api.log:9" in ev
    assert g.violations[0]["check"] == "root_cause_one_corpus"


def test_verbatim_check_ignores_line_breaks():
    texts = runbook_texts(load_settings().data_dir)
    step = ("Do not scale the service horizontally to work around pool exhaustion. Extra replicas each "
            "get their own undersized pool and add load without adding capacity.")
    g = Guard(seen=set())
    assert g.remediation("RB-004", [step, "Restart everything."], texts) == [step]
    assert g.violations[0]["check"] == "remediation_not_verbatim"
    assert normalise("a\n   b") == "a b"


# ---- run_incident with a fake run_specialist

RB4_STEP = ("Do not scale the service horizontally to work around pool exhaustion. Extra replicas each get "
            "their own undersized pool and add load without adding capacity. This supersedes the advice "
            "in the archived checkout runbook.")
RUNBOOK = {"matched": {"id": "RB-004", "title": "Database connection pool exhaustion in a service",
                       "source": "database.md"},
           "why": "w", "remediation": [RB4_STEP],
           "considered": [{"id": "RB-004", "source": "database.md", "archived": False, "verdict": "match", "reason": "r"}],
           "fallback": None}
SYNTH = {"summary": "Orders fail.", "root_cause_statement": "CHG-9042 lowered the timeout.",
         "root_cause_evidence": ["orders-api.log:5", "CHG-9042"], "correlation": "Lines up on key and time.",
         "ruled_out": [{"theory": "The CDN is down", "why": "Edge fine", "evidence": ["edge.log:1"]}],
         "actions": [{"action": "Revert CHG-9042.", "urgency": "now", "source": "orchestrator"}],
         "open_questions": []}
SEEN = {"log_analyst": {"orders-api.log:5", "orders-api.log:9", "gateway.log:4", "edge.log:1"},
        "change_historian": {"CHG-9042", "CHG-9043"}, "runbook_lookup": {"RB-004"}}
MODELS = {"log_analyst": LogAnalystResult, "change_historian": ChangeHistorianResult,
          "runbook_lookup": RunbookResult, "synthesis": SynthesisResult}
BODIES = {"log_analyst": ANALYST, "change_historian": HISTORIAN, "runbook_lookup": RUNBOOK, "synthesis": SYNTH}


def fake_specialists(monkeypatch, overrides=None, calls=None):
    overrides = overrides or {}

    async def fake(spec, context, settings, faults, trace, **kw):
        if calls is not None:
            calls.append((spec.agent, context))
        with trace.span(spec.agent):
            await asyncio.sleep(0.05)
        status, body = overrides.get(spec.agent, ("ok", BODIES[spec.agent]))
        model = MODELS[spec.agent]
        return run_of(model, spec.agent, model.model_validate(body) if body else None, status=status,
                      evidence=SEEN.get(spec.agent, ()), error=None if body else f"{status} (fake)")

    monkeypatch.setattr(orchestrator, "run_specialist", fake)


def triage(tmp_path):
    trace = Trace(tmp_path / "trace.jsonl")
    r = asyncio.run(orchestrator.run_incident("INC-9001", "Orders are failing. Maybe the CDN?",
                                              load_settings(), NO_FAULTS, trace))
    return TriageResult.model_validate(r.model_dump()), [json.loads(l) for l in trace.path.read_text().splitlines()]


def citations(r: TriageResult) -> set[str]:
    return ({e for t in r.timeline for e in t.evidence} | set(r.root_cause.evidence)
            | {e for x in r.ruled_out for e in x.evidence})


def test_happy_path(monkeypatch, tmp_path):
    calls = []
    fake_specialists(monkeypatch, calls=calls)
    r, events = triage(tmp_path)
    assert r.root_cause.confidence == "high" and r.runbook.matched == "RB-004"
    assert r.runbook.remediation == [RB4_STEP]
    assert r.actions[0].source == "orchestrator" and r.open_questions == []
    assert [a for a, _ in calls] == ["log_analyst", "change_historian", "runbook_lookup", "synthesis"]
    runbook_input = calls[2][1]
    assert runbook_input.symptom == ANALYST["failure_mode"]
    assert calls[0][1] == calls[1][1]  # identical input to both specialists
    spans = {e["agent"]: e["ts"] for e in events if e["type"] == "start"}
    ends = {e["agent"]: e["ts"] for e in events if e["type"] == "end"}
    assert spans["change_historian"] < ends["log_analyst"]  # the fan-out overlaps
    assert any(e["type"] == "join" for e in events)


def test_historian_failed_degrades_to_low_without_change_citations(monkeypatch, tmp_path):
    fake_specialists(monkeypatch, {"change_historian": ("failed", None)})
    r, _ = triage(tmp_path)
    assert r.root_cause.confidence == "low"
    assert not any(e.startswith("CHG-") for e in citations(r))  # CHG-9042 was never seen
    assert any("change historian produced no result" in q for q in r.open_questions)


def test_analyst_timeout_skips_the_runbook(monkeypatch, tmp_path):
    fake_specialists(monkeypatch, {"log_analyst": ("timeout", None)})
    r, events = triage(tmp_path)
    assert r.root_cause.confidence == "low" and r.runbook.matched is None
    assert "did not run" in r.runbook.why
    assert r.actions[0].action.startswith("Follow RB-000") and r.actions[0].source == "orchestrator"
    assert not any(".log:" in e for e in citations(r))
    assert "runbook_lookup" not in {e["agent"] for e in events}


def test_no_match_follows_the_fallback(monkeypatch, tmp_path):
    texts = runbook_texts(load_settings().data_dir)
    rb0_step = "Record the symptom, the evidence gathered so far, and the fact that no entry matches."
    assert rb0_step in texts["RB-000"]
    no_match = {**RUNBOOK, "matched": None, "remediation": [],
                "considered": [{"id": "LEG-011", "source": "_archive/x.md", "archived": True,
                                "verdict": "near_miss", "reason": "r"}],
                "fallback": {"id": "RB-000", "source": "platform-operations.md", "steps": [rb0_step]}}
    fake_specialists(monkeypatch, {"runbook_lookup": ("ok", no_match)})
    r, _ = triage(tmp_path)
    assert r.runbook.matched is None and r.actions[0].source == "RB-000" and r.actions[0].action == rb0_step
    assert any("archived" in q for q in r.open_questions)


def test_archived_match_is_rejected(monkeypatch, tmp_path):
    leg = {**RUNBOOK, "matched": {"id": "LEG-014", "title": "t", "source": "_archive/x.md"}}
    fake_specialists(monkeypatch, {"runbook_lookup": ("ok", leg)})
    r, events = triage(tmp_path)
    assert r.runbook.matched is None
    assert any(e.get("check") == "archived_match" for e in events)


def test_synthesis_failure_still_produces_a_cited_result(monkeypatch, tmp_path):
    fake_specialists(monkeypatch, {"synthesis": ("failed", None)})
    r, _ = triage(tmp_path)
    assert "assembled by code" in r.summary and r.root_cause.evidence
    assert any("Synthesis failed" in q for q in r.open_questions)
    assert [x.theory for x in r.ruled_out] == ["The CDN is down"]
