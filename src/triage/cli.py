"""Command line entry point: `uv run triage --help`."""

import json
from pathlib import Path

import typer

from . import evals
from .config import ConfigError, load_settings

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


def _write(obj, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(obj, indent=2) + "\n")
    typer.echo(f"wrote {out}")


@app.command("eval-tools")
def eval_tools(
    tools_json: Path = typer.Argument(..., exists=True, help="The grader's golden/tools.json"),
    out: Path = typer.Option(Path("out/tools.json"), "-o", "--out"),
    data_dir: str | None = typer.Option(None, help="Overrides TRIAGE_DATA_DIR"),
):
    """Run every tool case and write {case id: raw output} for `grade.py tools`."""
    _write(evals.run_tool_cases(tools_json, _settings(data_dir).data_dir), out)
