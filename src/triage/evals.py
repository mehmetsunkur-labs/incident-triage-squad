"""Component runners for agentic_exercise_grader (D10).

They take the grader's golden file path as an argument and read only the call or symptom
of each case, never its expectations, so answer keys don't reach this system.
"""

import json
from pathlib import Path

from .tools import TOOLS


def run_tool_cases(tools_json: Path, data_dir: Path) -> dict:
    """{case id: raw tool output} for `grade.py tools`. No model involved."""
    cases = json.loads(tools_json.read_text())["cases"]
    return {c["id"]: TOOLS[c["tool"]](data_dir, **c["args"]) for c in cases}


def runbook_symptoms(variants_json: Path) -> dict[str, str]:
    """{case id: symptom text} for the runbook component cases."""
    cases = json.loads(variants_json.read_text())["runbook_lookup_cases"]["cases"]
    return {c["id"]: c["symptom"] for c in cases}
