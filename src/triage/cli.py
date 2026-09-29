"""Command line entry point: `uv run triage --help`."""

import asyncio
import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path

import typer

from . import evals, faults as faults_mod, orchestrator
from .config import ConfigError, load_settings, read_incident
from .render import render
from .schemas import RunbookInput
from .trace import Trace, new_run_dir, timing_summary

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def main():
    """Incident triage squad."""


def _settings(data_dir: str | None):
    try:
        return load_settings(data_dir)
    except ConfigError as e:
        typer.echo(f"config error: {e}", err=True)
        raise typer.Exit(2)


def _faults(empty_tool, fail_agent, delay_agent, max_tool_calls):
    try:
        return faults_mod.parse(empty_tool, fail_agent, delay_agent, max_tool_calls)
    except ValueError as e:
        typer.echo(f"fault error: {e}", err=True)
        raise typer.Exit(2)


EMPTY_TOOL = typer.Option([], "--empty-tool", help="search_logs | search_changes | search_runbook; repeatable")
FAIL_AGENT = typer.Option([], "--fail-agent", help="log_analyst | change_historian | runbook_lookup; repeatable")
DELAY_AGENT = typer.Option([], "--delay-agent", help="agent=seconds, e.g. log_analyst=200; repeatable")
MAX_TOOL_CALLS = typer.Option(None, "--max-tool-calls", help="Override the per-specialist tool-call cap")
DATA_DIR = typer.Option(None, "--data-dir", help="Overrides TRIAGE_DATA_DIR")


def _write(obj, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(obj, indent=2) + "\n")
    typer.echo(f"wrote {out}")


@app.command("eval-tools")
def eval_tools(
    tools_json: Path = typer.Argument(..., exists=True, help="The grader's golden/tools.json"),
    out: Path = typer.Option(Path("out/tools.json"), "-o", "--out"),
    data_dir: str | None = DATA_DIR,
):
    """Run every tool case and write {case id: raw output} for `grade.py tools`."""
    _write(evals.run_tool_cases(tools_json, _settings(data_dir).data_dir), out)


@app.command()
def run(
    incident_id: str = typer.Argument(..., help="e.g. INC-2043"),
    empty_tool: list[str] = EMPTY_TOOL,
    fail_agent: list[str] = FAIL_AGENT,
    delay_agent: list[str] = DELAY_AGENT,
    max_tool_calls: int | None = MAX_TOOL_CALLS,
    data_dir: str | None = DATA_DIR,
):
    """Triage one incident. Writes out/runs/<incident>-<time>/ with result.json and trace.jsonl."""
    settings = _settings(data_dir)
    faults = _faults(empty_tool, fail_agent, delay_agent, max_tool_calls)
    if faults.max_tool_calls:
        settings = dataclasses.replace(settings, max_tool_calls=faults.max_tool_calls)
    try:
        report = read_incident(settings.data_dir, incident_id)
    except ConfigError as e:
        typer.echo(f"config error: {e}", err=True)
        raise typer.Exit(2)

    run_dir = new_run_dir(settings.out_dir, incident_id)
    if faults.any:
        (run_dir / "faults.json").write_text(faults.to_json())
    trace = Trace(run_dir / "trace.jsonl")
    try:
        result = asyncio.run(orchestrator.run_incident(incident_id, report, settings, faults, trace))
    finally:
        typer.echo(f"run folder: {run_dir}")
        typer.echo(timing_summary(trace.path))
    (run_dir / "result.json").write_text(result.model_dump_json(indent=2) + "\n")
    (run_dir / "result.md").write_text(render(result))
    typer.echo(f"wrote {run_dir / 'result.json'} and result.md")


@app.command("debug-agent")
def debug_agent(
    agent: str = typer.Argument(..., help="A specialist with a spec, e.g. log_analyst"),
    incident_id: str = typer.Argument(..., help="e.g. INC-2043"),
    empty_tool: list[str] = EMPTY_TOOL,
    fail_agent: list[str] = FAIL_AGENT,
    delay_agent: list[str] = DELAY_AGENT,
    max_tool_calls: int | None = MAX_TOOL_CALLS,
    timeout: float | None = typer.Option(None, "--timeout", help="Override the stage timeout, seconds"),
    data_dir: str | None = DATA_DIR,
):
    """Run one specialist alone on one incident (plan step 6b). Writes <agent>.json and trace.jsonl."""
    from . import specialists
    from .agent import run_specialist

    spec = specialists.SPECS.get(agent)
    if spec is None:
        typer.echo(f"no spec for {agent!r}; known: {', '.join(specialists.SPECS)}", err=True)
        raise typer.Exit(2)
    settings = _settings(data_dir)
    faults = _faults(empty_tool, fail_agent, delay_agent, max_tool_calls)
    overrides = {k: v for k, v in (("max_tool_calls", faults.max_tool_calls), ("specialist_timeout_s", timeout)) if v}
    settings = dataclasses.replace(settings, **overrides)
    try:
        report = read_incident(settings.data_dir, incident_id)
    except ConfigError as e:
        typer.echo(f"config error: {e}", err=True)
        raise typer.Exit(2)

    run_dir = new_run_dir(settings.out_dir, f"{incident_id}-{agent}")
    trace = Trace(run_dir / "trace.jsonl")
    run = asyncio.run(run_specialist(spec, specialists.specialist_input(incident_id, report),
                                     settings, faults, trace))
    (run_dir / f"{agent}.json").write_text(run.model_dump_json(indent=2) + "\n")
    typer.echo(f"run folder: {run_dir}")
    typer.echo(timing_summary(trace.path))
    typer.echo(f"  status={run.status} tool_calls={run.tool_calls} evidence_seen={len(run.evidence_seen)}"
               + (f" error={run.error[:200]}" if run.error else ""))
    typer.echo(f"  queries: {run.queries}")


@app.command("eval-runbook")
def eval_runbook(
    variants_json: Path = typer.Argument(..., exists=True, help="The grader's golden/variants.json"),
    out: Path = typer.Option(Path("out/runbook.json"), "-o", "--out"),
    data_dir: str | None = DATA_DIR,
):
    """Run the runbook lookup on each case's symptom alone; write {case id: matched id or null}."""
    settings = _settings(data_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    trace_dir = settings.out_dir / "eval-runbook" / stamp
    results = {}
    for case_id, symptom in evals.runbook_symptoms(variants_json).items():
        trace = Trace(trace_dir / f"{case_id}.trace.jsonl")
        runbook_input = RunbookInput(symptom=symptom, service=None, observed=[])
        run = asyncio.run(orchestrator.lookup_runbook(runbook_input, settings, faults_mod.NO_FAULTS, trace))
        if run.result is None:
            results[case_id] = f"ERROR: {run.status} {run.error or ''}".strip()
        else:
            results[case_id] = run.result.matched.id if run.result.matched else None
        typer.echo(f"  {case_id:22} {results[case_id]}")
    _write(results, out)
