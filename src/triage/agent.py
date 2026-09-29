"""The specialist loop (plan step 6b, D2 and D11): one function every agent runs on.

`run_specialist` drives a Claude Agent SDK session with at most one in-process tool, returns
a SpecialistRun and never raises. Behaviour it relies on was established in the step 5 and
6a spikes and is recorded in D11.
"""

import asyncio
import json
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from claude_agent_sdk import (
    ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, SystemMessage, ToolAnnotations,
    create_sdk_mcp_server, tool,
)
from pydantic import BaseModel, ValidationError

from . import faults as faults_mod
from .config import Settings
from .faults import Faults
from .schemas import AgentName, RunStatus, SpecialistRun
from .trace import Span, Trace

MCP_SERVER = "triage"
MAX_RESULT_CHARS = 500_000  # far above any real result; past this Claude Code hides it (D11)
CAP_MESSAGE = (
    "Tool-call limit reached ({cap}). Do not search again. Submit your answer now from what "
    "you have, and list what you could not check in gaps."
)
RETRY_MESSAGE = (
    "Your answer failed validation:\n\n{errors}\n\nResubmit it corrected, without new "
    "searches unless needed."
)


@dataclass(frozen=True)
class SpecialistSpec:
    """What makes one agent different from another (D2 step 6 design)."""

    agent: AgentName
    prompt_file: str  # in settings.prompts_dir; used verbatim as the system prompt
    output_model: type[BaseModel]
    user_message: Callable[[Any], str]  # this agent's context -> the user message
    tool: str | None = None  # a key of tools.TOOLS, or None (synthesis)
    tool_description: str = ""
    effort: str | None = None  # None = settings.specialist_effort
    max_tool_calls: int | None = None  # None = settings.max_tool_calls
    timeout_s: float | None = None  # None = settings.specialist_timeout_s


class ToolState:
    """The one tool as the agent sees it, plus what the run needs to know about its use.

    Handles the cap (D11), fault injection (D10), `queries` and `evidence_seen` (§4), and
    `tool_call` trace events with the plain tool name (D8). The check-and-increment has no
    await in between, so parallel calls in one turn can't overshoot the cap.
    """

    def __init__(self, name: str, data_dir: Path, cap: int, faults: Faults, span: Span):
        self.name, self.data_dir, self.cap, self.span = name, data_dir, cap, span
        self.fn = faults_mod.wrap_tool(name, faults)
        self.calls, self.queries, self.evidence_seen, self.cap_hit = 0, [], set(), False

    async def call(self, args: dict) -> dict:
        query = str(args.get("query", ""))
        if self.calls >= self.cap:
            self.cap_hit = True
            self.span.trace.emit(self.span.agent, "tool_refused", tool=self.name, query=query, reason="cap")
            return {"content": [{"type": "text", "text": CAP_MESSAGE.format(cap=self.cap)}], "is_error": True}
        self.calls += 1
        self.queries.append(query)
        result = self.fn(self.data_dir, query)
        self.evidence_seen.update(references(result))
        self.span.tool_call(self.name, query=query, total_matches=result.get("total_matches"))
        return {"content": [{"type": "text", "text": json.dumps(result)}]}


def references(result: dict) -> list[str]:
    """Citable references in a tool result: file:line for log lines, ids otherwise."""
    return [f"{r['file']}:{r['line']}" if "file" in r else r["id"] for r in result.get("results", [])]


def _options(spec: SpecialistSpec, settings: Settings, system_prompt: str, cwd: str,
             state: ToolState | None, cap: int) -> ClaudeAgentOptions:
    servers, allowed = {}, []
    if state is not None:
        sdk_tool = tool(spec.tool, spec.tool_description,
                        {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
                        annotations=ToolAnnotations(maxResultSizeChars=MAX_RESULT_CHARS))(state.call)
        servers = {MCP_SERVER: create_sdk_mcp_server(MCP_SERVER, tools=[sdk_tool])}
        allowed = [f"mcp__{MCP_SERVER}__{spec.tool}"]
    return ClaudeAgentOptions(
        tools=[], mcp_servers=servers, strict_mcp_config=True, allowed_tools=allowed,
        setting_sources=[], system_prompt=system_prompt, cwd=cwd, model=settings.model,
        effort=spec.effort or settings.specialist_effort,
        # num_turns isn't one per tool call (6a spike), so this is a generous backstop.
        max_turns=2 * cap + 4,
        output_format={"type": "json_schema", "schema": spec.output_model.model_json_schema()},
    )


async def _receive(client, span: Span) -> ResultMessage | None:
    """Read one response; record the tools the session reports at start-up."""
    result = None
    async for msg in client.receive_response():
        if isinstance(msg, SystemMessage) and msg.subtype == "init":
            span.extra.setdefault("init_tools", msg.data.get("tools"))
        elif isinstance(msg, ResultMessage):
            result = msg
    return result


def _parse(model: type[BaseModel], msg: ResultMessage | None) -> tuple[BaseModel | None, str | None, bool]:
    """(parsed, error, retryable). Retry only on validation failures (D11), not on errors
    such as running out of turns."""
    if msg is None:
        return None, "no result message", False
    if msg.is_error:
        return None, f"{msg.subtype}: {msg.terminal_reason}: {msg.errors}", False
    if msg.structured_output is None:
        return None, "no structured output", True
    try:
        return model.model_validate(msg.structured_output), None, True
    except ValidationError as e:
        return None, str(e), True


async def run_specialist(spec: SpecialistSpec, context: Any, settings: Settings, faults: Faults,
                         trace: Trace, *, client_factory=ClaudeSDKClient,
                         cwd: Path | None = None) -> SpecialistRun:
    """Run one agent to a validated result. Never raises; the status says what happened.

    - ok: a valid result
    - partial: a valid result, and the tool-call cap was hit
    - failed: an exception (including an injected fault), or no valid result after one retry
    - timeout: the stage timeout expired

    `client_factory` and `cwd` exist for tests.
    """
    cap = spec.max_tool_calls or settings.max_tool_calls
    timeout = spec.timeout_s or settings.specialist_timeout_s
    started = datetime.now(timezone.utc)
    box: dict = {"state": None}

    async def attempt() -> tuple[RunStatus, BaseModel | None, str | None]:
        with trace.span(spec.agent) as span:
            await faults_mod.before_agent(spec.agent, faults)
            system_prompt = (settings.prompts_dir / spec.prompt_file).read_text()
            user = spec.user_message(context)
            span.input(f"{system_prompt}\n\n---\n\n{user}")
            state = ToolState(spec.tool, settings.data_dir, cap, faults, span) if spec.tool else None
            box["state"] = state
            with tempfile.TemporaryDirectory(prefix=f"triage-{spec.agent}-") as tmp:
                options = _options(spec, settings, system_prompt, str(cwd or tmp), state, cap)
                async with client_factory(options) as client:
                    await client.query(user)
                    msg = await _receive(client, span)
                    parsed, error, retryable = _parse(spec.output_model, msg)
                    if parsed is None and retryable:
                        span.extra["retried"] = True
                        await client.query(RETRY_MESSAGE.format(errors=error[:4000]))
                        msg = await _receive(client, span)
                        parsed, error, _ = _parse(spec.output_model, msg)
            if msg is not None:
                span.extra.update(turns=msg.num_turns, cost_usd=msg.total_cost_usd)
            status: RunStatus = "failed" if parsed is None else ("partial" if state and state.cap_hit else "ok")
            span.status, span.error = status, error
            return status, parsed, error

    try:
        status, parsed, error = await asyncio.wait_for(attempt(), timeout)
    except TimeoutError:
        status, parsed, error = "timeout", None, f"no result within {timeout:g}s"
    except Exception as e:  # the span has already recorded it as failed
        status, parsed, error = "failed", None, f"{type(e).__name__}: {e}"

    state: ToolState | None = box["state"]
    return SpecialistRun[spec.output_model](
        agent=spec.agent, status=status, error=error, result=parsed,
        tool_calls=state.calls if state else 0,
        queries=state.queries if state else [],
        evidence_seen=sorted(state.evidence_seen) if state else [],
        started_at=started, finished_at=datetime.now(timezone.utc),
    )
