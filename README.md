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

Run one specialist alone while working on it: `uv run triage debug-agent log_analyst INC-2043`
(or `change_historian`; `runbook_lookup` takes `--symptom "..."`).
Tests that call the model are marked slow and skipped by default: `uv run pytest -m slow`.

## Running and grading with make

`make help` lists everything. The grader is expected at `../agentic_exercise_grader`; override
with `GRADER=...`.

```
make test                          # fast tests, no model
make tools                         # tool cases, graded, no model
make incidents                     # run and grade INC-2043, INC-2051, INC-2062; summary at the end
make incident-INC-2062             # one incident
make summary                       # newest result per incident
make variants                      # every grader variant, with its fault flags
make stability ID=INC-2043 N=5     # N runs graded together: pass rate per check
```

Each graded run keeps a `grade.txt` next to its `result.json`. Variant and stability runs go
to `out/variants/` and `out/stability/`, so they never count as an incident's newest run.
