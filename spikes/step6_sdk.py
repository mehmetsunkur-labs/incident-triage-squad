"""REFERENCE ONLY, THROWAWAY. Plan step 6a: Agent SDK behaviour spike, written by Claude.

Answers the questions D11 left open, so `run_specialist` (step 6b, written by hand) can be
designed on facts. It is deliberately a flat script with none of run_specialist's structure:
no spec, no SpecialistRun, no trace spans, no faults, no timeouts. Don't copy it; read the
findings recorded in docs/decisions.md D11 instead.

    uv run python spikes/step6_sdk.py

Experiments:
  A. ClaudeSDKClient + real search_logs + the full LogAnalystResult schema on INC-2043
  B. a forced validation failure, then one follow-up message in the same session
  C. the shape of an error result (max_turns=1)
"""

import asyncio
import json
import tempfile

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, SystemMessage,
    ToolAnnotations, ToolUseBlock, create_sdk_mcp_server, tool,
)
from pydantic import ValidationError

from triage.config import load_settings, read_incident
from triage.schemas import LogAnalystResult
from triage.tools import search_logs

settings = load_settings()
REPORT = read_incident(settings.data_dir, "INC-2043")
SCHEMA = LogAnalystResult.model_json_schema()
calls: list[str] = []


@tool("search_logs",
      "Search log lines. Every whitespace-separated term must appear in the line "
      "(case-insensitive substring). Returns at most 20 lines, oldest first.",
      {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
      annotations=ToolAnnotations(maxResultSizeChars=200_000))
async def search_logs_tool(args):
    calls.append(args["query"])
    result = search_logs(settings.data_dir, args["query"])
    return {"content": [{"type": "text", "text": json.dumps(result)}]}


STUB_PROMPT = (
    "You are a log analyst. Your only tool is search_logs. Use at most 4 searches, then "
    "give your answer. Cite evidence as file.log:N exactly as the tool reports file and line."
)
# For experiment B: an instruction that makes pydantic's evidence validator fail.
BAD_EVIDENCE_PROMPT = STUB_PROMPT.replace(
    "Cite evidence as file.log:N exactly as the tool reports file and line.",
    "Write every evidence item as file.log:N followed by ' - ' and the full quoted log line.",
)
USER = (f"Incident report:\n\n{REPORT}\n\nThe report contains theories from the reporter. "
        "Treat them as unverified hypotheses and test each one against the logs.")


def options(system_prompt: str, cwd: str, max_turns: int = 12) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        tools=[], mcp_servers={"triage": create_sdk_mcp_server("triage", tools=[search_logs_tool])},
        strict_mcp_config=True, allowed_tools=["mcp__triage__search_logs"], setting_sources=[],
        system_prompt=system_prompt, cwd=cwd, model=settings.model,
        effort=settings.specialist_effort, max_turns=max_turns,
        output_format={"type": "json_schema", "schema": SCHEMA},
    )


async def read_until_result(client, label: str) -> ResultMessage | None:
    """Print a one-line summary of every message; return the ResultMessage."""
    result = None
    async for m in client.receive_response():
        if isinstance(m, SystemMessage):
            extra = f" tools={m.data.get('tools')}" if m.subtype == "init" else ""
            print(f"  [{label}] SystemMessage subtype={m.subtype}{extra}")
        elif isinstance(m, AssistantMessage):
            kinds = [f"ToolUse({b.name})" if isinstance(b, ToolUseBlock) else type(b).__name__
                     for b in m.content]
            print(f"  [{label}] AssistantMessage {kinds}")
        elif isinstance(m, ResultMessage):
            result = m
            print(f"  [{label}] ResultMessage subtype={m.subtype} is_error={m.is_error} "
                  f"turns={m.num_turns} stop_reason={m.stop_reason} terminal_reason={m.terminal_reason} "
                  f"errors={m.errors} structured_output={'set' if m.structured_output is not None else None} "
                  f"cost={m.total_cost_usd}")
        else:
            print(f"  [{label}] {type(m).__name__}")
    return result


def validate(result: ResultMessage | None):
    if result is None or result.structured_output is None:
        return None, "no structured_output"
    try:
        return LogAnalystResult.model_validate(result.structured_output), None
    except ValidationError as e:
        return None, e


async def experiment_a(cwd):
    print("\n== A. real tool + full LogAnalystResult schema")
    calls.clear()
    async with ClaudeSDKClient(options(STUB_PROMPT, cwd)) as client:
        await client.query(USER)
        result = await read_until_result(client, "A")
    parsed, err = validate(result)
    print(f"  queries={calls}")
    print(f"  validates={parsed is not None} error={str(err)[:300] if err else None}")
    if parsed:
        print(f"  findings={len(parsed.findings)} first_failure_at={parsed.first_failure_at} "
              f"hypotheses={[h.verdict for h in parsed.hypotheses_checked]}")
    return {"validates": parsed is not None, "queries": list(calls), "turns": result and result.num_turns}


async def experiment_b(cwd):
    print("\n== B. forced validation failure, then one follow-up in the same session")
    calls.clear()
    async with ClaudeSDKClient(options(BAD_EVIDENCE_PROMPT, cwd)) as client:
        await client.query(USER)
        first = await read_until_result(client, "B1")
        parsed, err = validate(first)
        print(f"  first attempt validates={parsed is not None}")
        if parsed is not None:
            return {"forced_failure": False}
        errors = str(err)[:1500]
        await client.query(
            f"Your answer failed validation:\n{errors}\n\nResubmit it corrected, without new "
            "searches unless needed. Evidence items must be exactly file.log:N and nothing else.")
        second = await read_until_result(client, "B2")
    parsed2, err2 = validate(second)
    print(f"  follow-up validates={parsed2 is not None} error={str(err2)[:300] if err2 else None}")
    print(f"  queries across both turns={calls}")
    return {"forced_failure": True, "follow_up_structured": second is not None and second.structured_output is not None,
            "follow_up_validates": parsed2 is not None, "queries": list(calls)}


async def experiment_c(cwd):
    print("\n== C. error result shape with max_turns=1")
    async with ClaudeSDKClient(options(STUB_PROMPT, cwd, max_turns=1)) as client:
        await client.query(USER)
        result = await read_until_result(client, "C")
    return {"subtype": result and result.subtype, "is_error": result and result.is_error,
            "terminal_reason": result and result.terminal_reason,
            "structured_output": None if result is None else result.structured_output is not None}


async def main():
    with tempfile.TemporaryDirectory(prefix="spike6-") as cwd:
        summary = {"schema_has_defs": "$defs" in SCHEMA,
                   "A": await experiment_a(cwd), "B": await experiment_b(cwd), "C": await experiment_c(cwd)}
    print("\n== summary\n" + json.dumps(summary, indent=2, default=str))


asyncio.run(main())
