# Incident Triage Squad

A small multi-agent system: a deterministic orchestrator fans out to a log analyst and a
change historian in parallel, joins their findings, and hands the symptom to a runbook lookup.

## Running it

The incident data is not included in this repository. Point `TRIAGE_DATA_DIR` at a folder
laid out as `incidents/`, `logs/`, `changes/` and `runbook/` (see `.env.example`).

```
uv sync
uv run triage run INC-2043                              # one incident -> out/runs/<id>-<time>/
uv run triage run INC-2043 --empty-tool search_changes  # a grader variant (see docs/decisions.md D10)
uv run triage eval-tools ../agentic_exercise_grader/golden/tools.json
uv run triage eval-runbook ../agentic_exercise_grader/golden/variants.json
uv run pytest
```

Model access goes through the Claude Agent SDK and your Claude Code login (D11); no API key
is needed. Design: [docs/decisions.md](docs/decisions.md), [docs/contracts.md](docs/contracts.md),
[docs/plan.md](docs/plan.md).
