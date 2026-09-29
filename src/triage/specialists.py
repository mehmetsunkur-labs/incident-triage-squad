"""Specialist specs (D2). Each agent is one SpecialistSpec plus one prompt file.

Steps 6b, 7 and 8 defined the log analyst, the change historian and the runbook lookup;
step 9 adds the synthesis spec.
"""

from .agent import SpecialistSpec
from .schemas import ChangeHistorianResult, LogAnalystResult, RunbookInput, RunbookResult, SpecialistInput

# contracts §1: the same fixed text for every incident.
HYPOTHESES_NOTE = (
    "The report contains theories from the reporter. Treat them as unverified hypotheses: "
    "test each one against your corpus and report a verdict with evidence."
)


def specialist_input(incident_id: str, incident_report: str) -> SpecialistInput:
    return SpecialistInput(incident_id=incident_id, incident_report=incident_report,
                           hypotheses_note=HYPOTHESES_NOTE)


def specialist_user_message(inp: SpecialistInput) -> str:
    return (f"Incident {inp.incident_id}. The report, as written by the person who raised it:\n\n"
            f"{inp.incident_report.strip()}\n\n{inp.hypotheses_note}")


LOG_ANALYST = SpecialistSpec(
    agent="log_analyst",
    prompt_file="log_analyst.md",
    output_model=LogAnalystResult,
    user_message=specialist_user_message,
    tool="search_logs",
    tool_description=(
        "Search every log file on the platform. The query is split on whitespace and a line "
        "matches only if every term appears in it as a case-insensitive substring: no regex, "
        "stemming or synonyms. Results are sorted by timestamp, oldest first, at most 20. "
        "truncated=true means more lines matched than were returned, so narrow the query. "
        "Each result gives the file and 1-based line number to cite."
    ),
)

CHANGE_HISTORIAN = SpecialistSpec(
    agent="change_historian",
    prompt_file="change_historian.md",
    output_model=ChangeHistorianResult,
    user_message=specialist_user_message,  # the same input as the analyst (D4, rule 4)
    tool="search_changes",
    tool_description=(
        "Search the production change log. Each result is a whole change record. The query "
        "is split on whitespace and a record matches only if every term appears somewhere "
        "in it as a case-insensitive substring: no regex, stemming or synonyms. Results are "
        "newest first, at most 5. truncated=true means more records matched, so narrow the "
        "query. Each result gives the record's id to cite, its title and its full text."
    ),
)

def runbook_user_message(inp: RunbookInput) -> str:
    """The symptom only (D4, contracts §5). RunbookInput already rejects log lines, change
    ids and ISO timestamps, the patterns the grader's leak check looks for."""
    lines = [f"Symptom: {inp.symptom.strip()}"]
    if inp.service:
        lines.append(f"Service: {inp.service}")
    if inp.observed:
        lines.append("Observed: " + "; ".join(f"{o.key}={o.value}" for o in inp.observed))
    return "\n".join(lines)


RUNBOOK_LOOKUP = SpecialistSpec(
    agent="runbook_lookup",
    prompt_file="runbook_lookup.md",
    output_model=RunbookResult,
    user_message=runbook_user_message,
    tool="search_runbook",
    tool_description=(
        "Search every operations runbook, including the _archive/ folder. Each result is one "
        "entry: its id, title, source file path and full text. The query is split on "
        "whitespace and an entry matches only if every term appears somewhere in it as a "
        "case-insensitive substring: no regex, stemming or synonyms. At most 3 results, "
        "ordered by entry id, not by relevance; total_matches larger than returned means "
        "there are more."
    ),
)

SPECS = {s.agent: s for s in (LOG_ANALYST, CHANGE_HISTORIAN, RUNBOOK_LOOKUP)}
