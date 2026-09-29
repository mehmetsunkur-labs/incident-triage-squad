"""result.md: a human-readable rendering of a validated TriageResult (D8). Formatting only."""

from .schemas import TriageResult


def _refs(evidence: list[str]) -> str:
    return ", ".join(f"`{e}`" for e in evidence) if evidence else "none"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render(result: TriageResult) -> str:
    rc, rb = result.root_cause, result.runbook
    out = [f"# Triage: {result.incident_id}", "", result.summary, ""]

    out += ["## Root cause", "", f"{rc.statement}", "",
            f"- **Confidence:** {rc.confidence}",
            f"- **Correlation:** {rc.correlation}",
            f"- **Evidence:** {_refs(rc.evidence)}", ""]

    out += ["## Timeline", ""]
    if result.timeline:
        out += ["| Time (UTC) | Event | Evidence |", "|---|---|---|"]
        for t in result.timeline:
            out.append(f"| {t.time.strftime('%Y-%m-%d %H:%M:%S')} | {_cell(t.event)} | {_refs(t.evidence)} |")
    else:
        out.append("No timeline established.")
    out.append("")

    out += ["## Ruled out", ""]
    out += [f"- **{r.theory}:** {r.why} ({_refs(r.evidence)})" for r in result.ruled_out] or ["Nothing ruled out."]
    out.append("")

    out += ["## Runbook", ""]
    if rb.matched:
        out.append(f"**{rb.matched}: {rb.title}** (`{rb.source}`)")
    else:
        out.append("**No current runbook entry matched.**")
    out += ["", rb.why, ""]
    out += [f"{i}. {step}" for i, step in enumerate(rb.remediation, 1)]
    if rb.remediation:
        out.append("")

    out += ["## Actions", ""]
    if result.actions:
        out += ["| Urgency | Action | Source |", "|---|---|---|"]
        out += [f"| {a.urgency} | {_cell(a.action)} | {a.source} |" for a in result.actions]
    else:
        out.append("No actions.")
    out.append("")

    if result.stakeholder_update:
        out += ["## Stakeholder update", "", result.stakeholder_update, ""]

    out += ["## Open questions", ""]
    out += [f"- {q}" for q in result.open_questions] or ["None."]
    return "\n".join(out) + "\n"
