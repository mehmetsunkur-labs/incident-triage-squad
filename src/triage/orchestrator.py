"""Orchestrator entry points. YOURS TO WRITE (plan steps 6 to 9).

The CLI calls these two functions. Their signatures are the plumbing contract; replace the
bodies. Build the specialists on `agent.run_specialist` (step 6), use `faults.wrap_tool`
for each specialist's tool and call `faults.before_agent` at the start of each specialist,
inside its `trace.span(...)`.
"""

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
    """The runbook lookup specialist on its own (plan step 8)."""
    raise NotImplementedError("orchestrator.lookup_runbook: plan step 8")
