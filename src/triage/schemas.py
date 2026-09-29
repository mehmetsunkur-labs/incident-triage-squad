"""Pydantic models for every boundary in docs/contracts.md, plus the final TriageResult
from the exercise's output-contract.md.

These models are the source of truth. The ones a model fills in (LogAnalystResult,
ChangeHistorianResult, RunbookResult, SynthesisResult) are sent to structured outputs via
`model_json_schema()`, so they follow the conventions in contracts.md: `extra="forbid"`
everywhere (additionalProperties: false), every field required, `null` or `[]` for
nothing, and no free-form dicts. Constraints that structured outputs ignore (formats of
strings, non-empty lists) are enforced here by validators after parsing, and are kept out
of the JSON schema.
"""

import re
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, model_validator

# ---------------------------------------------------------------- reference formats

LOG_REF = re.compile(r"^[\w.-]+\.log:[1-9]\d*$")
CHG_REF = re.compile(r"^CHG-\d{4}$")
RB_REF = re.compile(r"^(RB|LEG)-\d{3}$")
CURRENT_RB_REF = re.compile(r"^RB-\d{3}$")
ACTION_SOURCE = re.compile(r"^(RB-\d{3}|analyst|historian|orchestrator)$")

# Patterns the grader's context-leak check looks for (contracts §9).
ISO_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
LOG_REF_ANYWHERE = re.compile(r"\.log:\d+")
CHG_ANYWHERE = re.compile(r"CHG-\d{4}")


def _pattern(regex: re.Pattern, what: str):
    def check(v: str) -> str:
        if not regex.match(v):
            raise ValueError(f"not a valid {what}: {v!r}")
        return v
    return AfterValidator(check)


def _evidence(v: str) -> str:
    if not (LOG_REF.match(v) or CHG_REF.match(v) or RB_REF.match(v)):
        raise ValueError(f"evidence must be file.log:line, CHG-nnnn, RB-nnn or LEG-nnn, got {v!r}")
    return v


Evidence = Annotated[str, AfterValidator(_evidence)]
ChangeId = Annotated[str, _pattern(CHG_REF, "change id (CHG-nnnn)")]
RunbookId = Annotated[str, _pattern(RB_REF, "runbook id (RB-nnn or LEG-nnn)")]
CurrentRunbookId = Annotated[str, _pattern(CURRENT_RB_REF, "current runbook id (RB-nnn)")]
ActionSource = Annotated[str, _pattern(ACTION_SOURCE, "action source")]

Verdict = Literal["supported", "ruled_out", "inconclusive"]
Strength = Literal["strong", "weak"]
Confidence = Literal["high", "medium", "low"]
RunStatus = Literal["ok", "partial", "failed", "timeout"]
AgentName = Literal["log_analyst", "change_historian", "runbook_lookup", "comms_drafter", "synthesis"]
Urgency = Literal["now", "today", "follow-up"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _non_empty(name: str, values: list) -> None:
    if not values:
        raise ValueError(f"{name} must not be empty")


# ---------------------------------------------------------------- shared parts

class KeyValue(Strict):
    key: str
    value: str


class KeyChange(Strict):
    key: str
    old: str | None
    new: str | None


class HypothesisCheck(Strict):
    theory: str
    verdict: Verdict
    why: str
    evidence: list[Evidence]

    @model_validator(mode="after")
    def _evidence_unless_inconclusive(self):
        if self.verdict != "inconclusive":
            _non_empty(f"evidence for a {self.verdict} verdict", self.evidence)
        return self


# ---------------------------------------------------------------- §1 specialist input

class SpecialistInput(Strict):
    incident_id: str
    incident_report: str
    hypotheses_note: str


# ---------------------------------------------------------------- §2 log analyst

class LogFinding(Strict):
    ts: AwareDatetime
    service: str
    kind: Literal["error", "warning", "deploy", "config", "recovery", "info"]
    observation: str
    attributes: list[KeyValue]
    evidence: list[Evidence]

    @model_validator(mode="after")
    def _has_evidence(self):
        _non_empty("finding evidence", self.evidence)
        return self


class LogAnalystResult(Strict):
    findings: list[LogFinding]
    first_failure_at: AwareDatetime | None
    failure_mode: str | None
    hypotheses_checked: list[HypothesisCheck]
    gaps: list[str]


# ---------------------------------------------------------------- §3 change historian

class ChangeFinding(Strict):
    change_id: ChangeId
    title: str
    effective_at: AwareDatetime
    services: list[str]
    kind: Literal["release", "config", "flag", "infrastructure", "external", "documentation", "other"]
    keys_changed: list[KeyChange]
    reverted_by: ChangeId | None
    relevance: str
    evidence: list[Evidence]

    @model_validator(mode="after")
    def _cites_itself(self):
        if self.change_id not in self.evidence:
            raise ValueError(f"evidence for {self.change_id} must include its own id")
        return self


class ChangeHistorianResult(Strict):
    changes: list[ChangeFinding]
    hypotheses_checked: list[HypothesisCheck]
    gaps: list[str]


# ---------------------------------------------------------------- §5, §6 runbook

class RunbookInput(Strict):
    """The only thing the runbook lookup sees: a symptom (D4). Rejects log lines, change
    records and evidence ids, the same patterns the grader's context-leak check uses."""

    symptom: str
    service: str | None
    observed: list[KeyValue]

    @model_validator(mode="after")
    def _symptom_only(self):
        text = " ".join([self.symptom, self.service or ""] + [f"{o.key} {o.value}" for o in self.observed])
        for regex, what in ((ISO_TIMESTAMP, "an ISO timestamp"), (LOG_REF_ANYWHERE, "a log reference"),
                            (CHG_ANYWHERE, "a change id")):
            if regex.search(text):
                raise ValueError(f"runbook input must be a symptom only, but contains {what}")
        return self


class EntryRef(Strict):
    id: RunbookId
    title: str
    source: str


class CandidateEntry(Strict):
    id: RunbookId
    source: str
    archived: bool
    verdict: Literal["match", "near_miss", "rejected"]
    reason: str


class Fallback(Strict):
    id: CurrentRunbookId
    source: str
    steps: list[str]


class RunbookResult(Strict):
    matched: EntryRef | None
    why: str
    remediation: list[str]
    considered: list[CandidateEntry]
    fallback: Fallback | None

    @model_validator(mode="after")
    def _consistent(self):
        if self.matched is None and self.remediation:
            raise ValueError("remediation must be empty when nothing matched")
        if self.matched is not None and self.fallback is not None:
            raise ValueError("fallback is only set when nothing matched")
        return self


# ---------------------------------------------------------------- §4 run wrapper (code side)

T = TypeVar("T", bound=BaseModel)


class SpecialistRun(Strict, Generic[T]):
    agent: AgentName
    status: RunStatus
    error: str | None
    tool_calls: int
    queries: list[str]
    evidence_seen: list[Evidence]  # every reference the tool returned (D6); code-side only
    started_at: AwareDatetime
    finished_at: AwareDatetime
    result: T | None

    @model_validator(mode="after")
    def _result_matches_status(self):
        if self.status in ("failed", "timeout") and self.result is not None:
            raise ValueError(f"a {self.status} run has no result")
        if self.status in ("ok", "partial") and self.result is None:
            raise ValueError(f"an {self.status} run must carry a result")
        return self


# ---------------------------------------------------------------- §7 synthesis

class Correlation(Strict):
    log_evidence: list[Evidence]
    change_id: ChangeId
    service: str
    minutes_before_first_failure: float
    matched_keys: list[str]
    strength: Strength


class SynthesisInput(Strict):
    incident_id: str
    incident_report: str
    analyst: SpecialistRun[LogAnalystResult]
    historian: SpecialistRun[ChangeHistorianResult]
    correlations: list[Correlation]
    confidence: Confidence
    confidence_reason: str
    runbook: SpecialistRun[RunbookResult] | None  # None when the lookup was skipped (D7)


class RuledOut(Strict):
    theory: str
    why: str
    evidence: list[Evidence]


class Action(Strict):
    action: str
    urgency: Urgency
    source: ActionSource


class SynthesisResult(Strict):
    summary: str
    root_cause_statement: str
    root_cause_evidence: list[Evidence]
    correlation: str
    ruled_out: list[RuledOut]
    actions: list[Action]
    open_questions: list[str]


# ---------------------------------------------------------------- final output (output-contract.md)

class TimelineEntry(Strict):
    time: AwareDatetime
    event: str
    evidence: list[Evidence]


class RootCause(Strict):
    statement: str
    confidence: Confidence
    evidence: list[Evidence]
    correlation: str


class RunbookOut(Strict):
    matched: CurrentRunbookId | None  # an archived LEG-nnn is never a valid match
    title: str | None
    source: str | None
    why: str
    remediation: list[str]

    @model_validator(mode="after")
    def _consistent(self):
        if self.matched is None and (self.title or self.source or self.remediation):
            raise ValueError("title, source and remediation must be empty when nothing matched")
        if self.matched is not None and not (self.title and self.source):
            raise ValueError("a matched entry needs its title and source")
        return self


class TriageResult(Strict):
    incident_id: str
    summary: str
    timeline: list[TimelineEntry]
    root_cause: RootCause
    ruled_out: list[RuledOut]
    runbook: RunbookOut
    actions: list[Action]
    stakeholder_update: str | None
    open_questions: list[str]


# Models whose JSON schema goes to structured outputs.
MODEL_OUTPUTS = (LogAnalystResult, ChangeHistorianResult, RunbookResult, SynthesisResult)
