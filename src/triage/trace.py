"""trace.jsonl writer in the grader's event format (D8, contracts §9).

    {"ts": 0.00, "agent": "log_analyst", "type": "start"}
    {"ts": 0.01, "agent": "log_analyst", "type": "input", "payload": "<prompt text>"}
    {"ts": 0.80, "agent": "log_analyst", "type": "tool_call", "tool": "search_logs"}
    {"ts": 4.20, "agent": "log_analyst", "type": "end", "status": "ok"}

`ts` is seconds since the run started, from a monotonic clock. Extra fields are ours; the
grader ignores them. Tool calls must be logged by the tool wrapper with the plain tool
name, never copied from the Agent SDK message stream (D11).
"""

import asyncio
import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class Span:
    """Handle for an open agent span. Set `status` (and `error`) before it closes."""

    def __init__(self, trace: "Trace", agent: str):
        self.trace, self.agent = trace, agent
        self.status, self.error, self.extra = "ok", None, {}

    def input(self, payload: str) -> None:
        self.trace.emit(self.agent, "input", payload=payload)

    def tool_call(self, tool: str, **fields) -> None:
        self.trace.emit(self.agent, "tool_call", tool=tool, **fields)


class Trace:
    def __init__(self, path: Path):
        self.path = path
        self.t0 = time.monotonic()
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")

    def elapsed(self) -> float:
        return time.monotonic() - self.t0

    def emit(self, agent: str, type: str, **fields) -> None:
        event = {"ts": round(self.elapsed(), 3), "agent": agent, "type": type, **fields}
        line = json.dumps(event, default=str) + "\n"
        with self._lock, self.path.open("a") as f:
            f.write(line)

    @contextmanager
    def span(self, agent: str):
        """Writes `start` now and `end` on exit, always: on success, on an exception
        (status `failed`) and on cancellation such as an asyncio timeout (status
        `timeout`). Exceptions are re-raised."""
        span = Span(self, agent)
        self.emit(agent, "start")
        try:
            yield span
        except (asyncio.CancelledError, TimeoutError) as e:
            span.status, span.error = "timeout", span.error or type(e).__name__
            raise
        except BaseException as e:
            span.status, span.error = "failed", span.error or f"{type(e).__name__}: {e}"
            raise
        finally:
            fields = {"status": span.status, **span.extra}
            if span.error:
                fields["error"] = span.error
            self.emit(agent, "end", **fields)


def new_run_dir(out_dir: Path, incident_id: str) -> Path:
    """out/runs/<incident>-<UTC timestamp>/, created, never reused."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = out_dir / "runs" / f"{incident_id}-{stamp}"
    path, n = base, 1
    while path.exists():
        n += 1
        path = base.with_name(f"{base.name}-{n}")
    path.mkdir(parents=True)
    return path


def timing_summary(path: Path) -> str:
    """Per-agent durations and the fan-out overlap, read back from trace.jsonl."""
    spans: dict[str, list[float | None]] = {}
    for line in path.read_text().splitlines():
        e = json.loads(line)
        s = spans.setdefault(e["agent"], [None, None, None])
        if e["type"] == "start":
            s[0] = e["ts"]
        elif e["type"] == "end":
            s[1], s[2] = e["ts"], e.get("status")
    lines = []
    for agent, (start, end, status) in spans.items():
        took = f"{end - start:6.1f}s" if start is not None and end is not None else "   open"
        lines.append(f"  {agent:17} {took}  {status or ''}")
    la, ch = spans.get("log_analyst"), spans.get("change_historian")
    if la and ch and None not in (la[0], la[1], ch[0], ch[1]):
        overlap = min(la[1], ch[1]) - max(la[0], ch[0])
        wall = max(la[1], ch[1]) - min(la[0], ch[0])
        lines.append(f"  fan-out wall {wall:.1f}s, specialists overlapped {max(overlap, 0):.1f}s")
    return "\n".join(lines)
