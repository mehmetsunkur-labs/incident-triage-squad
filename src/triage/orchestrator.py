"""Orchestrator entry points (plan steps 8 and 9). `lookup_runbook` is done; `run_incident`
is plan step 9.

The CLI calls these two functions; their signatures are the plumbing contract. Every agent
runs through `agent.run_specialist` with a spec from `specialists`, which already applies
faults, the trace span and the stage timeout (D2, D7, D10, D11).
"""

from .agent import run_specialist
from .config import Settings
from .faults import Faults
from .schemas import RunbookInput, RunbookResult, SpecialistRun, TriageResult
from .trace import Trace


async def run_incident(incident_id: str, incident_report: str, settings: Settings,
                       faults: Faults, trace: Trace) -> TriageResult:
    """Fan out, join, runbook lookup, synthesis, guardrails, assembly (plan step 9)."""
    raise NotImplementedError("orchestrator.run_incident: plan step 9")


async def lookup_runbook(runbook_input: RunbookInput, settings: Settings, faults: Faults,
                         trace: Trace) -> SpecialistRun[RunbookResult]:
    """The runbook lookup specialist on its own (plan step 8). Step 9 calls this too, with
    the symptom built from the joined findings."""
    from .specialists import RUNBOOK_LOOKUP
    return await run_specialist(RUNBOOK_LOOKUP, runbook_input, settings, faults, trace)
