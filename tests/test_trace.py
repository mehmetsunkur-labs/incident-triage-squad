"""Trace writer tests (plan step 4), including compatibility with the grader's reader."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path

import pytest

from triage.config import PROJECT_ROOT
from triage.trace import Trace, new_run_dir


def events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_span_writes_start_input_tool_call_end(tmp_path):
    t = Trace(tmp_path / "trace.jsonl")
    with t.span("log_analyst") as s:
        s.input("prompt text")
        s.tool_call("search_logs", query="pool")
    ev = events(t.path)
    assert [e["type"] for e in ev] == ["start", "input", "tool_call", "end"]
    assert ev[1]["payload"] == "prompt text"
    assert ev[2]["tool"] == "search_logs" and ev[2]["query"] == "pool"
    assert ev[3]["status"] == "ok"
    assert all(isinstance(e["ts"], float) for e in ev)
    assert [e["ts"] for e in ev] == sorted(e["ts"] for e in ev)


def test_end_written_on_failure(tmp_path):
    t = Trace(tmp_path / "trace.jsonl")
    with pytest.raises(RuntimeError):
        with t.span("change_historian"):
            raise RuntimeError("boom")
    end = events(t.path)[-1]
    assert end["type"] == "end" and end["status"] == "failed" and "boom" in end["error"]


def test_end_written_on_timeout(tmp_path):
    t = Trace(tmp_path / "trace.jsonl")

    async def slow():
        with t.span("log_analyst"):
            await asyncio.sleep(5)

    async def main():
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(slow(), timeout=0.05)

    asyncio.run(main())
    end = events(t.path)[-1]
    assert end["type"] == "end" and end["status"] == "timeout"


def test_run_dirs_never_reused(tmp_path):
    a, b = new_run_dir(tmp_path, "INC-9001"), new_run_dir(tmp_path, "INC-9001")
    assert a != b and a.is_dir() and b.is_dir() and a.parent == tmp_path / "runs"


GRADER = Path(os.environ.get("GRADER_DIR", PROJECT_ROOT.parent / "agentic_exercise_grader")) / "grade.py"


@pytest.mark.skipif(not GRADER.exists(), reason="grader repo not found (set GRADER_DIR)")
def test_grader_reads_our_trace(tmp_path):
    """Two overlapping specialist spans, sandboxed inputs: the grader's trace checks pass."""
    spec = importlib.util.spec_from_file_location("grade", GRADER)
    grade = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(grade)

    t = Trace(tmp_path / "trace.jsonl")

    async def agent(name, tool):
        with t.span(name) as s:
            s.input(f"system prompt for {name}, incident report text")
            await asyncio.sleep(0.05)
            s.tool_call(tool)
            await asyncio.sleep(0.05)

    async def main():
        await asyncio.gather(agent("log_analyst", "search_logs"), agent("change_historian", "search_changes"))

    asyncio.run(main())
    report = grade.Report()
    grade.check_trace(t.path, report)
    failed = [(cid, detail) for cid, hard, ok, detail in report.results if not ok]
    assert not failed
