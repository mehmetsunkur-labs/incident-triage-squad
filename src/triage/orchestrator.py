"""The orchestrator (plan steps 8 and 9): deterministic, no tools of its own.

Fans out to the log analyst and change historian concurrently, joins their findings in
code, looks up the runbook with the symptom only, has synthesis write the prose, applies the
guardrails and assembles the TriageResult. Every agent runs through `agent.run_specialist`,
which applies faults, the trace span and the stage timeout. See docs/workflow.md.
"""

import asyncio
import dataclasses
from collections import Counter
from datetime import datetime, timezone

from . import join
from .agent import run_specialist
from .config import Settings
from .faults import Faults
from .guardrails import Guard, runbook_texts
from .schemas import (
    CHG_ANYWHERE, ISO_TIMESTAMP, LOG_REF_ANYWHERE, Action, Correlation, KeyValue, RootCause,
    RunbookInput, RunbookOut, RunbookResult, RuledOut, SpecialistRun, SynthesisInput,
    SynthesisResult, TimelineEntry, TriageResult,
)
from .trace import Trace

ORCHESTRATOR = "orchestrator"  # trace events from code (guardrails); no span, no tools


async def lookup_runbook(runbook_input: RunbookInput, settings: Settings, faults: Faults,
                         trace: Trace) -> SpecialistRun[RunbookResult]:
    """The runbook lookup specialist on its own (plan step 8); step 9 calls it too."""
    from .specialists import RUNBOOK_LOOKUP
    return await run_specialist(RUNBOOK_LOOKUP, runbook_input, settings, faults, trace)


async def run_incident(incident_id: str, incident_report: str, settings: Settings,
                       faults: Faults, trace: Trace) -> TriageResult:
    """One whole run, under the run backstop (D7). Degrades rather than crashes: every
    stage's failure has a branch, and the backstop firing is a bug."""
    return await asyncio.wait_for(_run(incident_id, incident_report, settings, faults, trace),
                                  settings.run_timeout_s)


def _failed_run(spec, error: BaseException) -> SpecialistRun:
    """run_specialist never raises; this only guards against a bug in it (D1)."""
    now = datetime.now(timezone.utc)
    return SpecialistRun[spec.output_model](
        agent=spec.agent, status="failed", error=f"{type(error).__name__}: {error}", tool_calls=0,
        queries=[], evidence_seen=[], started_at=now, finished_at=now, result=None)


async def _run(incident_id, report, settings, faults, trace) -> TriageResult:
    from .specialists import CHANGE_HISTORIAN, LOG_ANALYST, SYNTHESIS, specialist_input

    # 1-2. Fan-out: the same input to both, concurrently; neither sees the other (D4).
    inp = specialist_input(incident_id, report)
    runs = await asyncio.gather(
        run_specialist(LOG_ANALYST, inp, settings, faults, trace),
        run_specialist(CHANGE_HISTORIAN, inp, settings, faults, trace),
        return_exceptions=True)
    analyst, historian = (r if isinstance(r, SpecialistRun) else _failed_run(spec, r)
                          for r, spec in zip(runs, (LOG_ANALYST, CHANGE_HISTORIAN)))

    # 3. Join, in code (D5), capped by D7.
    correlations = join.correlate(analyst.result, historian.result)
    confidence, reason = join.confidence(analyst, historian, correlations)

    # 4. Runbook lookup with the symptom only, or skipped (D7).
    runbook_input, skip_reason = build_runbook_input(analyst)
    runbook = await lookup_runbook(runbook_input, settings, faults, trace) if runbook_input else None

    # 5. Synthesis: prose over what code joined (zero-tool spec, D11).
    synthesis_input = SynthesisInput(
        incident_id=incident_id, incident_report=report, analyst=analyst, historian=historian,
        correlations=correlations, confidence=confidence, confidence_reason=reason, runbook=runbook)
    synthesis_spec = dataclasses.replace(SYNTHESIS, effort=settings.synthesis_effort,
                                         timeout_s=settings.synthesis_timeout_s)
    synthesis = await run_specialist(synthesis_spec, synthesis_input, settings, faults, trace)

    # 6-7. Guardrails (D6) and assembly (§8).
    result = assemble(incident_id, analyst, historian, correlations, confidence, reason,
                      runbook, skip_reason, synthesis, settings, trace)
    return TriageResult.model_validate(result.model_dump())


def build_runbook_input(analyst: SpecialistRun) -> tuple[RunbookInput | None, str | None]:
    """contracts §5: the symptom only, from the analyst's failure mode and the values on its
    failing lines. Returns (input, None), or (None, why the lookup is skipped)."""
    a = analyst.result
    if a is None:
        return None, f"the log analyst produced no result ({analyst.status})"
    if not a.failure_mode:
        return None, "the log analyst established no failure mode"
    failing = [f for f in a.findings if f.kind in join.FAILING_KINDS]
    service = Counter(f.service for f in failing).most_common(1)[0][0] if failing else None
    observed, seen = [], set()
    for f in failing:
        for kv in f.attributes:
            text = f"{kv.key} {kv.value}"
            if (kv.key, kv.value) in seen or any(p.search(text) for p in (ISO_TIMESTAMP, LOG_REF_ANYWHERE, CHG_ANYWHERE)):
                continue
            seen.add((kv.key, kv.value))
            observed.append(KeyValue(key=kv.key, value=kv.value))
    try:
        return RunbookInput(symptom=a.failure_mode, service=service, observed=observed[:12]), None
    except ValueError as e:
        return None, f"the failure mode could not be used as a symptom-only input ({e.errors()[0]['msg']})"


def build_timeline(analyst: SpecialistRun, historian: SpecialistRun) -> list[TimelineEntry]:
    entries = []
    if analyst.result:
        entries += [TimelineEntry(time=f.ts, event=f.observation, evidence=f.evidence) for f in analyst.result.findings]
    if historian.result:
        entries += [TimelineEntry(time=c.effective_at, event=f"{c.change_id} reached production: {c.title}",
                                  evidence=[c.change_id]) for c in historian.result.changes]
    return sorted(entries, key=lambda t: t.time)


def fallback_synthesis(analyst: SpecialistRun, historian: SpecialistRun,
                       correlations: list[Correlation], error: str | None) -> SynthesisResult:
    """When synthesis itself fails, code still produces an honest, cited result (D7)."""
    top = correlations[0] if correlations else None
    ruled_out = [RuledOut(theory=h.theory, why=h.why, evidence=h.evidence)
                 for r in (analyst, historian) if r.result for h in r.result.hypotheses_checked
                 if h.verdict == "ruled_out"]
    return SynthesisResult(
        summary="Synthesis failed, so this result was assembled by code from the specialists' findings.",
        root_cause_statement=(f"{top.change_id} lines up with the failure on {top.service}, "
                              f"{top.minutes_before_first_failure:g} minutes before it." if top
                              else "Root cause not established."),
        root_cause_evidence=(top.log_evidence + [top.change_id]) if top else [],
        correlation="; ".join(f"{c.change_id}: {c.strength}, {c.minutes_before_first_failure:g} min before, "
                              f"{', '.join(c.matched_keys) or 'time and service only'}" for c in correlations)
                    or "No change lines up with the log findings.",
        ruled_out=ruled_out, actions=[],
        open_questions=[f"Synthesis failed: {error}"])


def assemble(incident_id, analyst, historian, correlations, confidence, reason, runbook,
             skip_reason, synthesis, settings, trace) -> TriageResult:
    s = synthesis.result or fallback_synthesis(analyst, historian, correlations, synthesis.error)
    # Only runs that returned a result count: a failed or timed-out run may have searched
    # before stopping, but the orchestrator never received its findings (D6, D7).
    seen = {e for r in (analyst, historian, runbook) if r is not None and r.result is not None
            for e in r.evidence_seen}
    guard = Guard(seen)
    questions: list[str] = []

    # D7: missing branches; D2: specialists that ran out of searches.
    for r in (analyst, historian):
        if r.result is None:
            questions.append(f"The {r.agent.replace('_', ' ')} produced no result ({r.status}: {r.error}); "
                             "confidence is capped at low.")
        elif r.status == "partial":
            questions.append(f"The {r.agent.replace('_', ' ')} ran out of searches; its findings may be incomplete.")

    # Runbook (§6, §8; D6 no archived match; D7 skipped lookup).
    actions: list[Action] = []
    texts = runbook_texts(settings.data_dir)
    rb = runbook.result if runbook else None
    if rb and rb.matched and rb.matched.id.startswith("LEG-"):
        guard.flag("archived_match", f"{rb.matched.id} returned as matched",
                   f"The closest runbook entry, {rb.matched.id}, is archived; the current runbooks have a gap.")
        rb = rb.model_copy(update={"matched": None, "remediation": []})
    if rb and rb.matched:
        runbook_out = RunbookOut(matched=rb.matched.id, title=rb.matched.title, source=rb.matched.source,
                                 why=rb.why, remediation=guard.remediation(rb.matched.id, rb.remediation, texts))
    else:
        if rb is not None:
            why = rb.why
            if rb.fallback:
                steps = guard.remediation(rb.fallback.id, rb.fallback.steps, texts)
                actions += [Action(action=step, urgency="now", source=rb.fallback.id) for step in steps]
            closest_archived = [c.id for c in rb.considered if c.archived and c.verdict != "rejected"]
            if closest_archived:
                questions.append(f"The closest runbook entries are archived ({', '.join(closest_archived)}); "
                                 "the current runbooks may have a gap for this symptom.")
        elif runbook is not None:
            why = f"The runbook lookup produced no result ({runbook.status}: {runbook.error})."
            actions.append(Action(action="Follow RB-000 until a matching runbook entry is found.",
                                  urgency="now", source="orchestrator"))
        else:
            why = f"The runbook lookup did not run: {skip_reason}. No symptom was established to search with."
            actions.append(Action(action="Follow RB-000 until a symptom is established.",
                                  urgency="now", source="orchestrator"))
        runbook_out = RunbookOut(matched=None, title=None, source=None, why=why, remediation=[])

    root_cause = RootCause(statement=s.root_cause_statement, confidence=confidence,
                           evidence=guard.root_cause_evidence(s, correlations), correlation=s.correlation)
    if not root_cause.evidence and confidence != "low":
        guard.flag("root_cause_uncited", "no citation from this run remained", "The root cause has no citation from this run.")
        root_cause = root_cause.model_copy(update={"confidence": "low"})

    result = TriageResult(
        incident_id=incident_id, summary=s.summary,
        timeline=guard.timeline(build_timeline(analyst, historian)),
        root_cause=root_cause, ruled_out=guard.ruled_out(s.ruled_out), runbook=runbook_out,
        actions=actions + s.actions, stakeholder_update=None,
        open_questions=list(dict.fromkeys(s.open_questions + questions + guard.open_questions)))

    for v in guard.violations:
        trace.emit(ORCHESTRATOR, "guardrail", **v)
    trace.emit(ORCHESTRATOR, "join", confidence=confidence, confidence_reason=reason,
               correlations=[c.model_dump() for c in correlations])
    return result
