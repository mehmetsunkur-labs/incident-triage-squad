# Incident Triage Squad

A small multi-agent system: a deterministic orchestrator fans out to a log analyst and a
change historian in parallel, joins their findings, and hands the symptom to a runbook lookup.

## Running it

The incident data is not included in this repository. Point `TRIAGE_DATA_DIR` at a folder
laid out as `incidents/`, `logs/`, `changes/` and `runbook/` (see `.env.example`).
