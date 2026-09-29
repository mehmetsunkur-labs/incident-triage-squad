"""Specialist specs (D2). Each agent is one SpecialistSpec plus one prompt file.

Step 6b defines the log analyst; steps 7 and 8 add the change historian and the runbook
lookup here, and step 9 the synthesis spec.
"""

from .agent import SpecialistSpec
from .schemas import LogAnalystResult, SpecialistInput

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

SPECS = {s.agent: s for s in (LOG_ANALYST,)}
