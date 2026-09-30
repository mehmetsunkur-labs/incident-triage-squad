"""Fault injection and CLI wiring tests (plan step 11 plumbing)."""

import asyncio

import pytest
from typer.testing import CliRunner

from triage import faults
from triage.cli import app
from triage.tools import search_changes


def test_parse_valid():
    f = faults.parse(["search_changes"], ["change_historian"], ["log_analyst=2.5"], 3)
    assert f.empty_tools == {"search_changes"} and f.fail_agents == {"change_historian"}
    assert f.delay_agents == {"log_analyst": 2.5} and f.max_tool_calls == 3 and f.any
    assert not faults.NO_FAULTS.any


@pytest.mark.parametrize("args", [
    (["grep"], [], [], None), ([], ["critic"], [], None), ([], [], ["log_analyst"], None),
    ([], [], ["nobody=3"], None), ([], [], [], 0)])
def test_parse_rejects(args):
    with pytest.raises(ValueError):
        faults.parse(*args)


def test_empty_tool_runs_real_search_but_returns_nothing(data_dir):
    calls = []

    def spy(*a, **kw):
        calls.append(a)
        return search_changes(*a, **kw)

    f = faults.parse(["search_changes"], [], [], None)
    out = faults.wrap_tool("search_changes", f, spy)(data_dir, "checkout")
    assert calls and out["total_matches"] == 0 and out["results"] == [] and out["truncated"] is False
    assert out["query"] == "checkout"


def test_empty_tool_keeps_the_empty_query_error(data_dir):
    f = faults.parse(["search_logs"], [], [], None)
    assert "error" in faults.wrap_tool("search_logs", f)(data_dir, " ")


def test_unaffected_tool_is_the_real_one():
    assert faults.wrap_tool("search_logs", faults.NO_FAULTS) is faults.TOOLS["search_logs"]


def test_before_agent_delays_and_first_call_fails():
    f = faults.parse([], ["change_historian"], ["log_analyst=0.05"], None)
    asyncio.run(faults.before_agent("change_historian", f))  # failing is on the first call
    with pytest.raises(faults.InjectedFault):
        faults.before_first_call("change_historian", f)
    faults.before_first_call("log_analyst", f)

    async def timed():
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await faults.before_agent("log_analyst", f)
        return loop.time() - t0

    assert asyncio.run(timed()) >= 0.05
    asyncio.run(faults.before_agent("runbook_lookup", f))


runner = CliRunner()


def test_cli_rejects_bad_fault():
    r = runner.invoke(app, ["run", "INC-2043", "--fail-agent", "critic"])
    assert r.exit_code == 2 and "unknown agent" in r.output


def test_cli_rejects_unknown_incident():
    r = runner.invoke(app, ["run", "INC-0000"])
    assert r.exit_code == 2 and "known:" in r.output


def test_cli_run_writes_run_folder_and_fault_record(tmp_path, monkeypatch):
    """`run` creates the run folder, trace and fault record before the orchestrator runs,
    and keeps them if it fails. The orchestrator is faked: no model."""
    from triage import orchestrator

    async def boom(*args, **kwargs):
        raise RuntimeError("orchestrator failed")

    monkeypatch.setenv("TRIAGE_OUT_DIR", str(tmp_path))
    monkeypatch.setattr(orchestrator, "run_incident", boom)
    r = runner.invoke(app, ["run", "INC-2043", "--empty-tool", "search_changes"])
    assert isinstance(r.exception, RuntimeError)
    [run_dir] = (tmp_path / "runs").iterdir()
    assert (run_dir / "faults.json").exists() and (run_dir / "trace.jsonl").exists()
