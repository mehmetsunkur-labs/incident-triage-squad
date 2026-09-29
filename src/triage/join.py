"""The join (plan steps 9 and 10, D5): correlate log findings with changes in code, and
compute confidence by rule. No model call.

A change is a candidate when it reached production up to 48 hours before the first failure
and is linked to a failing service in one of three ways, from most to least specific:

- same_service: the change touches a service named on a failing line (the line's service,
  or a service= or upstream= on it)
- co_located: the change's service appears in the logs on the same node or host as a
  failing service, which the join can only see by combining both corpora
- infrastructure: the change has platform-wide scope (an infrastructure change, or
  services such as "all workloads on the affected nodes")

A candidate is **strong** when a key it changed shows up in the logs at its new value, on a
same-service or co-located link, however long before the failure it shipped. Otherwise it
is **weak**. Timing within 15 minutes is reported but no longer required: effects of a
change can wait for a scheduled job or a reschedule.
"""

import re
from datetime import timedelta

from .schemas import (
    ChangeFinding, ChangeHistorianResult, Confidence, Correlation, LogAnalystResult, LogFinding,
    SpecialistRun,
)

LOOKBACK = timedelta(hours=48)
CLOSE = timedelta(minutes=15)
FAILING_KINDS = ("error", "warning")
SERVICE_KEYS = ("service", "upstream")
PLACEMENT_KEYS = ("node", "host", "instance")
INFRA_WORDS = {"all", "node", "nodes", "workloads", "cluster", "fleet", "platform"}
STOPWORDS = {"at", "the", "of", "in", "on", "to", "a", "an", "and", "for", "rows"}
LINK_RANK = {"same_service": 3, "co_located": 2, "infrastructure": 1}


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if t and t not in STOPWORDS}


def _norm(value: str) -> str:
    return value.strip().strip("\"'").strip().lower()


def first_failure(analyst: LogAnalystResult) -> LogFinding | None:
    """The finding at the analyst's first_failure_at, else the earliest error finding."""
    if analyst.first_failure_at:
        at = [f for f in analyst.findings if f.ts == analyst.first_failure_at]
        if at:
            return at[0]
    errors = sorted((f for f in analyst.findings if f.kind == "error"), key=lambda f: f.ts)
    return errors[0] if errors else None


def _services_of(f: LogFinding) -> set[str]:
    return {f.service.lower()} | {a.value.lower() for a in f.attributes if a.key.lower() in SERVICE_KEYS}


def affected_services(analyst: LogAnalystResult) -> set[str]:
    """Services named on failing lines: the line's own service and any service= or upstream=."""
    return {s for f in analyst.findings if f.kind in FAILING_KINDS for s in _services_of(f)}


def _placements(analyst: LogAnalystResult) -> dict[str, list[tuple[str, LogFinding]]]:
    """node/host value -> [(service, finding)] for every finding that places a service there."""
    out: dict[str, list[tuple[str, LogFinding]]] = {}
    for f in analyst.findings:
        places = [a.value.lower() for a in f.attributes if a.key.lower() in PLACEMENT_KEYS]
        for place in places:
            for service in _services_of(f) - {"platform"}:
                out.setdefault(place, []).append((service, f))
    return out


def _link(change: ChangeFinding, affected: set[str], placements) -> tuple[str, str, list[LogFinding]] | None:
    """(link type, description, findings that show it), most specific first."""
    services = {s.lower() for s in change.services}
    direct = sorted(services & affected)
    if direct:
        return "same_service", f"same service: {direct[0]}", []
    for place, entries in sorted(placements.items()):
        hosted = {s for s, _ in entries}
        ours, theirs = services & hosted, affected & hosted
        if ours and theirs:
            shown = [f for s, f in entries if s in ours | theirs]
            return ("co_located", f"shares {place} with {sorted(theirs)[0]}: {sorted(ours)[0]}", shown)
    words = {w for s in change.services for w in _tokens(s)}
    if change.kind == "infrastructure" or (words & INFRA_WORDS and not direct):
        return "infrastructure", "infrastructure scope", []
    return None


GENERIC_VALUE_KEYS = {"value", "new", "to", "got"}


def _value_matches(change: ChangeFinding, findings: list[LogFinding]) -> list[tuple[str, LogFinding]]:
    """A changed key's new value appears in a finding's attributes, under an attribute key
    that shares a word with the changed key (`max=4` for `db.pool.max`), or under a generic
    key such as `value=` when the same line names the field (`field=settled_at value=...`).
    So a bare `4` elsewhere doesn't match."""
    hits = []
    for kc in change.keys_changed:
        if kc.new is None or not _norm(kc.new):
            continue
        key_words, new = _tokens(kc.key), _norm(kc.new)
        for f in findings:
            line_words = {t for a in f.attributes for t in _tokens(a.value)} | _tokens(f.observation)
            for a in f.attributes:
                if _norm(a.value) != new:
                    continue
                if key_words & _tokens(a.key) or (a.key.lower() in GENERIC_VALUE_KEYS and key_words & line_words):
                    hits.append((f"{a.key}={a.value} ~ {kc.key}", f))
    return hits


def correlate(analyst: LogAnalystResult | None, historian: ChangeHistorianResult | None) -> list[Correlation]:
    if analyst is None or historian is None:
        return []
    failure = first_failure(analyst)
    if failure is None:
        return []
    affected, placements = affected_services(analyst), _placements(analyst)
    out = []
    for c in historian.changes:
        before = failure.ts - c.effective_at
        if not timedelta(0) <= before <= LOOKBACK:
            continue
        link = _link(c, affected, placements)
        if link is None:
            continue
        kind, description, shown = link
        # Values count only between the change shipping and the first failure: a value seen
        # after a rollback isn't evidence for the change.
        on_service = [f for f in analyst.findings
                      if c.effective_at <= f.ts <= failure.ts
                      and _services_of(f) & ({s.lower() for s in c.services} | affected)]
        values = _value_matches(c, on_service) if kind in ("same_service", "co_located") else []
        timing = "within 15 minutes" if before <= CLOSE else f"{before.total_seconds() / 3600:.1f} hours before"
        evidence_findings = [f for _, f in values] + shown + [failure]
        out.append(Correlation(
            log_evidence=sorted({e for f in evidence_findings for e in f.evidence}),
            change_id=c.change_id, service=sorted(affected)[0] if kind != "same_service" else description.split(": ")[1],
            minutes_before_first_failure=round(before.total_seconds() / 60, 1),
            matched_keys=sorted({d for d, _ in values}) + [description, timing],
            strength="strong" if values else "weak", link=kind,
        ))
    return sorted(out, key=lambda c: (c.strength != "strong", -LINK_RANK[c.link], c.minutes_before_first_failure))


def confidence(analyst: SpecialistRun, historian: SpecialistRun,
               correlations: list[Correlation]) -> tuple[Confidence, str]:
    """D5's rules, capped by D7 when a branch has no result."""
    missing = [r for r in (analyst, historian) if r.result is None]
    if missing:
        return "low", (f"No result from {' and '.join(r.agent for r in missing)} "
                       f"({', '.join(r.status for r in missing)}), so only one side of the evidence is available.")
    if not correlations:
        return "low", "No change is linked to the failing services within 48 hours before the first failure."
    strong = sorted({c.change_id for c in correlations if c.strength == "strong"})
    weak = [c for c in correlations if c.strength == "weak" and c.change_id not in strong]
    others = f" Also linked, more weakly: {', '.join(sorted({c.change_id for c in weak}))}." if weak else ""
    if len(strong) > 1:
        return "low", f"More than one change matches strongly: {', '.join(strong)}."
    if len(strong) == 1:
        top = next(c for c in correlations if c.change_id == strong[0])
        why = f"{top.change_id} matches on {'; '.join(top.matched_keys)}"
        ruled_out = any(h.verdict == "ruled_out" for r in (analyst, historian) for h in r.result.hypotheses_checked)
        if ruled_out:
            return "high", f"{why}, and competing theories were ruled out.{others}"
        return "medium", f"{why}, but no competing theory was ruled out.{others}"
    best = max(LINK_RANK[c.link] for c in weak)
    top = [c for c in weak if LINK_RANK[c.link] == best]
    if len({c.change_id for c in top}) == 1:
        rest = sorted({c.change_id for c in weak} - {top[0].change_id})
        also = f" Less specifically linked: {', '.join(rest)}." if rest else ""
        return "medium", (f"{top[0].change_id} is the most specifically linked change ({'; '.join(top[0].matched_keys)}), "
                          f"but no changed value is visible in the logs.{also}")
    return "low", f"Several changes are linked equally weakly: {', '.join(sorted({c.change_id for c in top}))}."
