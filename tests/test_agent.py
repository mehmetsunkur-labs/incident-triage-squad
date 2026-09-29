"""run_specialist without a model (plan step 6b): a fake client drives every status path.

The fake stands in for ClaudeSDKClient: it records the options it was given, calls the
agent's tool the way the model would, and replies with scripted ResultMessages.
"""

import asyncio
import json
from pathlib import Path

import pytest
from claude_agent_sdk import ResultMessage, SystemMessage

from triage import faults
from triage.agent import ToolState, run_specialist
from triage.config import load_settings
from triage.faults import NO_FAULTS
from triage.schemas import LogAnalystResult
from triage.specialists import LOG_ANALYST, specialist_input
from triage.trace import Trace

GOOD = {
    "findings": [{"ts": "2026-08-12T19:33:41Z", "service": "checkout-service", "kind": "error",
                  "observation": "timeout", "attributes": [], "evidence": ["checkout-service.log:9"]}],
    "first_failure_at": "2026-08-12T19:33:41Z", "failure_mode": "timeouts",
    "hypotheses_checked": [], "gaps": [],
}
BAD = {**GOOD, "findings": [{**GOOD["findings"][0], "evidence": ["checkout-service.log:9 - some text"]}]}


def result(structured=None, *, is_error=False, subtype="success", terminal_reason="completed"):
    return ResultMessage(subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=is_error,
                         num_turns=3, session_id="s", total_cost_usd=0.01, structured_output=structured,
                         terminal_reason=terminal_reason,
                         errors=["Reached maximum number of turns (1)"] if is_error else None)


class FakeClient:
    """Each script entry is (queries to run through the tool, ResultMessage)."""

    instances: list["FakeClient"] = []

    def __init__(self, script, sleep=0.0):
        self.script, self.sleep, self.options, self.sent = list(script), sleep, None, []

    def __call__(self, options):
        self.options = options
        FakeClient.instances.append(self)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True

    async def query(self, text):
        self.sent.append(text)

    async def receive_response(self):
        await asyncio.sleep(self.sleep)
        queries, msg = self.script.pop(0)
        yield SystemMessage(subtype="init", data={"tools": ["StructuredOutput", "mcp__triage__search_logs"]})
        handler = None
        if self.options.mcp_servers:
            handler = _tool_handler(self.options)
        for q in queries:
            await handler({"query": q})
        yield msg


def _tool_handler(options):
    """Find our in-process tool's handler inside the MCP server config."""
    server = options.mcp_servers["triage"]["instance"]
    return FakeClient.handler_for[id(server)]


@pytest.fixture
def settings():
    return load_settings()


@pytest.fixture
def trace(tmp_path):
    return Trace(tmp_path / "trace.jsonl")


@pytest.fixture(autouse=True)
def capture_handlers(monkeypatch):
    """Record the handler passed to create_sdk_mcp_server so the fake can call it."""
    import triage.agent as agent_mod
    FakeClient.handler_for = {}
    real = agent_mod.create_sdk_mcp_server

    def spy(name, tools):
        config = real(name, tools=tools)
        FakeClient.handler_for[id(config["instance"])] = tools[0].handler
        return config

    monkeypatch.setattr(agent_mod, "create_sdk_mcp_server", spy)


def run(settings, trace, client, *, spec=LOG_ANALYST, f=NO_FAULTS, **settings_overrides):
    import dataclasses
    s = dataclasses.replace(settings, **settings_overrides) if settings_overrides else settings
    ctx = specialist_input("INC-2043", "Checkout is failing.")
    return asyncio.run(run_specialist(spec, ctx, s, f, trace, client_factory=client))


def events(trace):
    return [json.loads(line) for line in trace.path.read_text().splitlines()]


def test_ok_records_queries_evidence_and_trace(settings, trace):
    client = FakeClient([(["2026-08-12T19:3 checkout"], result(GOOD))])
    r = run(settings, trace, client)
    assert r.status == "ok" and isinstance(r.result, LogAnalystResult)
    assert r.tool_calls == 1 and r.queries == ["2026-08-12T19:3 checkout"]
    assert "checkout-service.log:9" in r.evidence_seen
    ev = events(trace)
    assert [e["type"] for e in ev] == ["start", "input", "tool_call", "end"]
    assert ev[2]["tool"] == "search_logs" and ev[-1]["status"] == "ok"
    assert ev[-1]["init_tools"] == ["StructuredOutput", "mcp__triage__search_logs"]


def test_options_follow_d11(settings, trace):
    client = FakeClient([([], result(GOOD))])
    run(settings, trace, client)
    o = client.options
    assert o.tools == [] and o.setting_sources == [] and o.strict_mcp_config is True
    assert o.allowed_tools == ["mcp__triage__search_logs"]
    assert o.system_prompt == (settings.prompts_dir / "log_analyst.md").read_text()
    assert o.output_format["schema"] == LogAnalystResult.model_json_schema()
    assert o.model == settings.model and o.effort == settings.specialist_effort
    assert Path(o.cwd).name.startswith("triage-log_analyst-") and not Path(o.cwd).exists()


def test_input_payload_is_system_plus_user_and_has_no_change_ids(settings, trace):
    run(settings, trace, FakeClient([([], result(GOOD))]))
    payload = next(e for e in events(trace) if e["type"] == "input")["payload"]
    assert payload.startswith((settings.prompts_dir / "log_analyst.md").read_text())
    assert "Checkout is failing." in payload and "unverified hypotheses" in payload
    import re
    assert not re.search(r"CHG-\d{4}", payload)


def test_invalid_then_valid_retries_in_the_same_session(settings, trace):
    client = FakeClient([([], result(BAD)), ([], result(GOOD))])
    r = run(settings, trace, client)
    assert r.status == "ok" and len(client.sent) == 2
    assert "failed validation" in client.sent[1] and "evidence must be" in client.sent[1]
    assert events(trace)[-1]["retried"] is True


def test_invalid_twice_is_failed(settings, trace):
    r = run(settings, trace, FakeClient([([], result(BAD)), ([], result(BAD))]))
    assert r.status == "failed" and r.result is None and "evidence must be" in r.error
    assert events(trace)[-1]["status"] == "failed"


def test_max_turns_is_failed_without_retry(settings, trace):
    client = FakeClient([([], result(None, is_error=True, subtype="error_max_turns", terminal_reason="max_turns"))])
    r = run(settings, trace, client)
    assert r.status == "failed" and "error_max_turns" in r.error and len(client.sent) == 1


def test_cap_hit_with_valid_result_is_partial(settings, trace):
    client = FakeClient([(["pool", "checkout", "postgres"], result(GOOD))])
    r = run(settings, trace, client, max_tool_calls=2)
    assert r.status == "partial" and r.tool_calls == 2 and r.queries == ["pool", "checkout"]
    types = [e["type"] for e in events(trace)]
    assert types.count("tool_call") == 2 and types.count("tool_refused") == 1


def test_timeout_is_recorded_in_trace_and_run(settings, trace):
    r = run(settings, trace, FakeClient([([], result(GOOD))], sleep=5), specialist_timeout_s=0.1)
    assert r.status == "timeout" and r.result is None
    assert events(trace)[-1]["status"] == "timeout"


def test_injected_fault_is_failed(settings, trace):
    f = faults.parse([], ["log_analyst"], [], None)
    r = run(settings, trace, FakeClient([([], result(GOOD))]), f=f)
    assert r.status == "failed" and "InjectedFault" in r.error
    assert events(trace)[-1]["status"] == "failed"


def test_empty_tool_gives_empty_evidence(settings, trace):
    f = faults.parse(["search_logs"], [], [], None)
    r = run(settings, trace, FakeClient([(["pool"], result(GOOD))]), f=f)
    assert r.status == "ok" and r.tool_calls == 1 and r.evidence_seen == []


def test_parallel_calls_cannot_overshoot_the_cap(settings, trace):
    with trace.span("log_analyst") as span:
        state = ToolState("search_logs", settings.data_dir, 3, NO_FAULTS, span)

        async def many():
            return await asyncio.gather(*(state.call({"query": "pool"}) for _ in range(6)))

        replies = asyncio.run(many())
    assert state.calls == 3 and state.cap_hit
    assert sum(1 for r in replies if r.get("is_error")) == 3


def test_references_cover_all_three_tools(settings):
    from triage.agent import references
    from triage.tools import search_changes, search_logs, search_runbook
    d = settings.data_dir
    assert all(":" in r for r in references(search_logs(d, "pool")))
    assert references(search_changes(d, "db.pool.max")) == ["CHG-1044", "CHG-1042"]
    assert "RB-004" in references(search_runbook(d, "connection pool"))
