"""The join (plan step 9, D5): correlate log findings with changes in code, and compute
confidence by rule. No model call.

A change correlates with the logs when it touches a service the failing log lines name and
reached production 0 to 15 minutes before the first failure. The match is **strong** when a
key the change set shows up in the logs at the change's new value (for example a change
setting `db.pool.max` to 4, and a finding with `max=4` on that service); it is **weak** when
the change only lines up on time and service, or its key is merely named in a log line.
"""

import re
from datetime import timedelta

from .schemas import (
    ChangeFinding, ChangeHistorianResult, Confidence, Correlation, LogAnalystResult, LogFinding,
    SpecialistRun,
)

WINDOW = timedelta(minutes=15)
FAILING_KINDS = ("error", "warning")
SERVICE_KEYS = ("service", "upstream")


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if t}


def first_failure(analyst: LogAnalystResult) -> LogFinding | None:
    """The finding at the analyst's first_failure_at, else the earliest error finding."""
    if analyst.first_failure_at:
        at = [f for f in analyst.findings if f.ts == analyst.first_failure_at]
        if at:
            return at[0]
    errors = sorted((f for f in analyst.findings if f.kind == "error"), key=lambda f: f.ts)
    return errors[0] if errors else None


def affected_services(analyst: LogAnalystResult) -> set[str]:
    """Services named on failing lines: the line's own service and any service= or upstream=."""
    out = set()
    for f in analyst.findings:
        if f.kind in FAILING_KINDS:
            out.add(f.service.lower())
            out.update(a.value.lower() for a in f.attributes if a.key.lower() in SERVICE_KEYS)
    return out


def _value_matches(change: ChangeFinding, findings: list[LogFinding]) -> list[tuple[str, LogFinding]]:
    """(description, finding) for each changed key whose new value appears in a finding's
    attributes under a key sharing the change key's last segment and one other segment
    (the other may be in the observation)."""
    hits = []
    for kc in change.keys_changed:
        if kc.new is None:
            continue
        segments = [s for s in re.split(r"[._-]", kc.key.lower()) if s]
        last, others = segments[-1], set(segments[:-1])
        for f in findings:
            context = _tokens(f.observation)
            for a in f.attributes:
                attr = _tokens(a.key)
                if last in attr and a.value.strip().lower() == kc.new.strip().lower() \
                        and (not others or others & (attr | context)):
                    hits.append((f"{a.key}={a.value} ~ {kc.key}", f))
    return hits


def _named(change: ChangeFinding, findings: list[LogFinding]) -> list[tuple[str, LogFinding]]:
    hits = []
    for kc in change.keys_changed:
        for f in findings:
            text = " ".join([f.observation] + [a.value for a in f.attributes]).lower()
            if kc.key.lower() in text:
                hits.append((f"{kc.key} named in the logs", f))
    return hits


def correlate(analyst: LogAnalystResult | None, historian: ChangeHistorianResult | None) -> list[Correlation]:
    if analyst is None or historian is None:
        return []
    failure = first_failure(analyst)
    if failure is None:
        return []
    services = affected_services(analyst)
    out = []
    for c in historian.changes:
        before = failure.ts - c.effective_at
        touched = services & {s.lower() for s in c.services}
        if not touched or not timedelta(0) <= before <= WINDOW:
            continue
        near = [f for f in analyst.findings if c.effective_at - timedelta(minutes=5) <= f.ts <= failure.ts]
        values, named = _value_matches(c, near), _named(c, near)
        chosen = values or named
        log_evidence = sorted({e for _, f in chosen for e in f.evidence} | set(failure.evidence))
        out.append(Correlation(
            log_evidence=log_evidence, change_id=c.change_id, service=sorted(touched)[0],
            minutes_before_first_failure=round(before.total_seconds() / 60, 1),
            matched_keys=sorted({d for d, _ in chosen}),
            strength="strong" if values else "weak",
        ))
    return sorted(out, key=lambda c: (c.strength != "strong", c.minutes_before_first_failure))


def confidence(analyst: SpecialistRun, historian: SpecialistRun,
               correlations: list[Correlation]) -> tuple[Confidence, str]:
    """D5's rules, capped by D7 when a branch has no result."""
    missing = [r.agent for r in (analyst, historian) if r.result is None]
    if missing:
        return "low", f"No result from {' and '.join(missing)} ({', '.join(r.status for r in (analyst, historian) if r.result is None)}), so only one side of the evidence is available."
    if not correlations:
        return "low", "No change lines up with the log findings on both service and time."
    strong = sorted({c.change_id for c in correlations if c.strength == "strong"})
    weak = sorted({c.change_id for c in correlations if c.strength == "weak"} - set(strong))
    others = f" {', '.join(weak)} also line{'s' if len(weak) == 1 else ''} up on time and service only." if weak else ""
    if len(strong) > 1:
        return "low", f"More than one change matches strongly: {', '.join(strong)}."
    if len(strong) == 1:
        top = next(c for c in correlations if c.change_id == strong[0])
        ruled_out = any(h.verdict == "ruled_out" for r in (analyst, historian) for h in r.result.hypotheses_checked)
        keys = "; ".join(top.matched_keys)
        if ruled_out:
            return "high", (f"{top.change_id} matches on service, time ({top.minutes_before_first_failure:g} min "
                            f"before the first failure) and changed value ({keys}), and competing theories "
                            f"were ruled out.{others}")
        return "medium", f"{top.change_id} matches strongly ({keys}), but no competing theory was ruled out.{others}"
    if len(weak) == 1:
        return "medium", f"{weak[0]} lines up on service and time, but no changed value is visible in the logs."
    return "low", f"Several changes line up on service and time only: {', '.join(weak)}."
