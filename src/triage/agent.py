"""The specialist loop (plan step 6b, D2 and D11): one function every agent runs on.

`run_specialist` drives a Claude Agent SDK session with at most one in-process tool, returns
a SpecialistRun and never raises. Behaviour it relies on was established in the step 5 and
6a spikes and is recorded in D11.
"""

import asyncio
import json
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, RateLimitEvent, ResultMessage,
    SystemMessage, TextBlock, ThinkingBlock, ToolAnnotations, ToolUseBlock, UserMessage,
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
TEXT_PREVIEW = 200  # chars of a text reply kept in the trace
MAX_SYSTEM_DATA = 2000  # a system message's data past this is traced as its keys only
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
# A reply's usage is the API's snapshot from the start of the response, so its output count
# is not the final one; only the input side is traced per reply (totals are on `end`).
REPLY_USAGE_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
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


class StderrLog:
    """The CLI's stderr, one timestamped line at a time, into stderr-<agent>.log beside the
    trace. The file is only created if the CLI writes something; the count goes on `end`."""

    def __init__(self, path: Path, span: Span):
        self.path, self.span, self.lines, self.f, self.closed = path, span, 0, None, False

    def __call__(self, line: str) -> None:
        if self.closed:
            return
        self.lines += 1
        self.span.extra["stderr_lines"] = self.lines
        try:
            if self.f is None:
                self.f = self.path.open("a")
            self.f.write(f"{self.span.trace.elapsed():.3f} {line.rstrip()}\n")
            self.f.flush()
        except (OSError, ValueError):
            pass

    def close(self) -> None:
        self.closed = True
        if self.f is not None:
            self.f.close()


def _options(spec: SpecialistSpec, settings: Settings, system_prompt: str, cwd: str,
             state: ToolState | None, cap: int, stderr: Callable[[str], None] | None = None,
             debug_file: Path | None = None) -> ClaudeAgentOptions:
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
        stderr=stderr, extra_args={"debug-file": str(debug_file)} if debug_file else {},
    )


def _seen(span: Span, kind: str) -> float:
    """Note a message from the CLI on the span, so `end` says where a stalled run stopped
    (messages, last_message, last_message_ts), and return seconds since the previous one."""
    now = span.trace.elapsed()
    since = now - span.extra.get("last_message_ts", span.started)
    span.extra.update(messages=span.extra.get("messages", 0) + 1, last_message=kind,
                      last_message_ts=round(now, 3))
    return round(since, 3)


def _usage(usage: dict | None, keys: tuple[str, ...] = USAGE_KEYS) -> dict | None:
    return {k: usage[k] for k in keys if k in usage} if usage else None


def _thinking(span: Span, data: dict) -> None:
    """Fold the CLI's thinking progress (about one message a second while the model thinks)
    into one `thinking` field on `end`: how many updates, the estimated tokens, and when
    thinking was first and last reported. A gap after `last_ts` is the model writing."""
    t = span.extra.setdefault("thinking", {"updates": 0, "est_tokens": 0,
                                           "first_ts": span.extra["last_message_ts"]})
    t["updates"] += 1
    t["est_tokens"] += data.get("estimated_tokens_delta") or 0
    t["last_ts"] = span.extra["last_message_ts"]


def _blocks(content) -> list[dict]:
    """What a model reply held, by kind and size: enough to tell thinking from text from a
    tool request without copying the reply. Tool requests keep the SDK's name; they are not
    `tool_call` events, which only the tool wrapper writes (D11)."""
    out = []
    for b in content:
        if isinstance(b, ThinkingBlock):
            out.append({"type": "thinking", "chars": len(b.thinking)})
        elif isinstance(b, TextBlock):
            out.append({"type": "text", "chars": len(b.text), "preview": b.text[:TEXT_PREVIEW]})
        elif isinstance(b, ToolUseBlock):
            out.append({"type": "tool_use", "name": b.name})
        else:
            out.append({"type": type(b).__name__})
    return out


def _system_data(data: dict) -> dict:
    """A system message's fields for the trace, e.g. an api_retry's attempt, delay and error
    status; the envelope keys are dropped, and a large payload is reduced to its keys."""
    data = {k: v for k, v in data.items() if k not in ("type", "subtype", "session_id", "uuid")}
    return data if len(json.dumps(data, default=str)) <= MAX_SYSTEM_DATA else {"keys": sorted(data)}


async def _receive(client, span: Span) -> ResultMessage | None:
    """Read one response and trace what the CLI reports along the way, so a stall can be
    told apart from slow work (step 10): every model reply (`sdk_message`), every system
    message other than init, such as the CLI's API retries (`sdk_system`), and usage-limit
    status (`rate_limit`). The init message gives the session's tools and id, and thinking
    progress is summarised on `end` rather than traced line by line.

    The CLI sends one AssistantMessage per content block, each repeating its API call's
    usage, so usage is traced on the first block of each call only, input side only."""
    result, last_id = None, None
    async for msg in client.receive_response():
        if isinstance(msg, SystemMessage):
            since = _seen(span, f"system:{msg.subtype}")
            if msg.subtype == "init":
                span.extra.setdefault("init_tools", msg.data.get("tools"))
                span.extra.setdefault("init_s", round(span.extra["last_message_ts"] - span.started, 3))
                span.extra.setdefault("session_id", msg.data.get("session_id"))
            elif msg.subtype == "thinking_tokens":
                _thinking(span, msg.data)
            else:
                span.trace.emit(span.agent, "sdk_system", subtype=msg.subtype, since_prev_s=since,
                                data=_system_data(msg.data))
        elif isinstance(msg, AssistantMessage):
            since = _seen(span, "assistant")
            usage = _usage(msg.usage, REPLY_USAGE_KEYS) if msg.message_id != last_id else None
            last_id = msg.message_id
            span.trace.emit(span.agent, "sdk_message", since_prev_s=since, message_id=msg.message_id,
                            blocks=_blocks(msg.content), usage=usage,
                            stop_reason=msg.stop_reason, error=msg.error)
        elif isinstance(msg, UserMessage):
            _seen(span, "user")  # tool results going back to the model; tool_call has them
        elif isinstance(msg, RateLimitEvent):
            since = _seen(span, "rate_limit")
            info = msg.rate_limit_info
            span.trace.emit(span.agent, "rate_limit", status=info.status, utilization=info.utilization,
                            rate_limit_type=info.rate_limit_type, resets_at=info.resets_at,
                            since_prev_s=since)
        elif isinstance(msg, ResultMessage):
            _seen(span, "result")
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
            run_dir = trace.path.parent
            stderr = StderrLog(run_dir / f"stderr-{spec.agent}.log", span)
            debug_file = run_dir / f"debug-{spec.agent}.log" if settings.sdk_debug else None
            with tempfile.TemporaryDirectory(prefix=f"triage-{spec.agent}-") as tmp, closing(stderr):
                options = _options(spec, settings, system_prompt, str(cwd or tmp), state, cap,
                                   stderr, debug_file)
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
                span.extra.update(turns=msg.num_turns, cost_usd=msg.total_cost_usd,
                                  api_s=round(msg.duration_api_ms / 1000, 1), usage=_usage(msg.usage))
                if msg.api_error_status:
                    span.extra["api_error_status"] = msg.api_error_status
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
