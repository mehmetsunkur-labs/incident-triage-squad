"""Fault injection for the grader's variants (D10).

Faults are applied inside the tool or agent, so the system really experiences them:

- empty tool: the tool still runs, but returns zero matches for every query
- failing agent: the agent raises InjectedFault when it starts
- delayed agent: the agent sleeps before starting, to run past its timeout
- tool-call cap: overrides Settings.max_tool_calls for every specialist

Your agent loop calls `before_agent(...)` at the start of each specialist, inside its trace
span, and builds each specialist's tool with `wrap_tool(...)`.
"""

import asyncio
import json
from dataclasses import asdict, dataclass, field
from typing import Callable

from .tools import TOOLS

AGENTS = ("log_analyst", "change_historian", "runbook_lookup")


class InjectedFault(RuntimeError):
    pass


@dataclass(frozen=True)
class Faults:
    empty_tools: frozenset[str] = field(default_factory=frozenset)
    fail_agents: frozenset[str] = field(default_factory=frozenset)
    delay_agents: dict[str, float] = field(default_factory=dict)
    max_tool_calls: int | None = None

    @property
    def any(self) -> bool:
        return bool(self.empty_tools or self.fail_agents or self.delay_agents or self.max_tool_calls)

    def to_json(self) -> str:
        d = asdict(self)
        d["empty_tools"], d["fail_agents"] = sorted(self.empty_tools), sorted(self.fail_agents)
        return json.dumps(d, indent=2) + "\n"


NO_FAULTS = Faults()


def parse(empty_tool: list[str], fail_agent: list[str], delay_agent: list[str],
          max_tool_calls: int | None) -> Faults:
    """Build Faults from CLI values; `delay_agent` items are `name=seconds`."""
    for t in empty_tool:
        if t not in TOOLS:
            raise ValueError(f"unknown tool {t!r}; expected one of {', '.join(TOOLS)}")
    for a in fail_agent:
        if a not in AGENTS:
            raise ValueError(f"unknown agent {a!r}; expected one of {', '.join(AGENTS)}")
    delays = {}
    for item in delay_agent:
        name, sep, secs = item.partition("=")
        if not sep or name not in AGENTS:
            raise ValueError(f"--delay-agent takes agent=seconds with agent in {', '.join(AGENTS)}, got {item!r}")
        delays[name] = float(secs)
    if max_tool_calls is not None and max_tool_calls < 1:
        raise ValueError("--max-tool-calls must be at least 1")
    return Faults(frozenset(empty_tool), frozenset(fail_agent), delays, max_tool_calls)


def wrap_tool(name: str, faults: Faults, fn: Callable[..., dict] | None = None) -> Callable[..., dict]:
    """The tool as the agent should see it: the real one, or one that runs the real search
    and then reports zero matches."""
    real = fn or TOOLS[name]
    if name not in faults.empty_tools:
        return real

    def empty(*args, **kwargs) -> dict:
        out = real(*args, **kwargs)
        if "error" in out:
            return out
        out.update(total_matches=0, returned=0, results=[])
        if "truncated" in out:
            out["truncated"] = False
        return out

    return empty


async def before_agent(agent: str, faults: Faults) -> None:
    """Call at the start of every specialist, inside its trace span."""
    if delay := faults.delay_agents.get(agent):
        await asyncio.sleep(delay)
    if agent in faults.fail_agents:
        raise InjectedFault(f"injected failure in {agent}")
