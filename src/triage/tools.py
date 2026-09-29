"""The three search tools, exactly as specified in the exercise's docs/tool-contracts.md.

Each function takes the data folder explicitly so a tool can never read outside it.
Return values are plain JSON-serialisable dicts in the contract's shape.
"""

import re
from datetime import datetime, timezone
from pathlib import Path

from .corpus import EMPTY_QUERY_ERROR, matches, split_sections, split_terms

# D3: the date a change reached production. Order matters: first field found wins.
EFFECTIVE_FIELDS = ("Shipped", "Applied", "Merged")
DATE_VALUE = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})")


def effective_at(record_body: str) -> datetime | None:
    """The first of Shipped / Applied / Merged, taking only the leading datetime of the
    value (some values carry trailing text such as `... UTC in checkout-service v4.12.0`)."""
    for field in EFFECTIVE_FIELDS:
        m = re.search(rf"\*\*{field}:\*\*\s*(.+)", record_body)
        if m and (d := DATE_VALUE.search(m.group(1))):
            return datetime.fromisoformat(f"{d[1]}T{d[2]}:00").replace(tzinfo=timezone.utc)
    return None


def _result(query: str, matched: list, max_results: int, with_truncated: bool = True) -> dict:
    out = {"query": query, "total_matches": len(matched), "returned": min(len(matched), max_results)}
    if with_truncated:
        out["truncated"] = len(matched) > max_results
    out["results"] = matched[:max_results]
    return out


def search_logs(data_dir: Path, query: str, max_results: int = 20) -> dict:
    """Every `.log` file in logs/; a line matches if every term is a substring of it.
    Sorted by the timestamp at the start of the line, ascending."""
    terms = split_terms(query)
    if not terms:
        return dict(EMPTY_QUERY_ERROR)
    hits = []
    for path in sorted((data_dir / "logs").glob("*.log")):
        for n, line in enumerate(path.read_text().splitlines(), start=1):
            if line.strip() and matches(line, terms):
                hits.append((line.split(maxsplit=1)[0], path.name, n, line))
    hits.sort(key=lambda h: (h[0], h[1], h[2]))
    return _result(query, [{"file": f, "line": n, "text": t} for _, f, n, t in hits], max_results)


def search_changes(data_dir: Path, query: str, max_results: int = 5) -> dict:
    """Change records from changes/change-log.md; newest first by effective_at (D3).
    Returns the full record body."""
    terms = split_terms(query)
    if not terms:
        return dict(EMPTY_QUERY_ERROR)
    records = []
    for path in sorted((data_dir / "changes").glob("*.md")):
        records += split_sections(path.read_text())
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    hits = [r for r in records if matches(r.searchable, terms)]
    hits.sort(key=lambda r: (effective_at(r.body) or epoch, r.id), reverse=True)
    return _result(query, [{"id": r.id, "title": r.title, "text": r.body} for r in hits], max_results)


def search_runbook(data_dir: Path, query: str, max_results: int = 3) -> dict:
    """Entries from every .md file under runbook/, recursively, including _archive/.

    Ordering is "most terms matched, then id ascending". Because a match requires every
    term, all hits tie on terms matched, so in practice this is id ascending. No status
    field: working out what an archived source means is the agent's job."""
    terms = split_terms(query)
    if not terms:
        return dict(EMPTY_QUERY_ERROR)
    root = data_dir / "runbook"
    hits = []
    for path in sorted(root.rglob("*.md")):
        source = path.relative_to(root).as_posix()
        for s in split_sections(path.read_text()):
            if matches(s.searchable, terms):
                hits.append({"id": s.id, "title": s.title, "source": source, "text": s.body})
    hits.sort(key=lambda h: (-len(terms), h["id"]))
    return _result(query, hits, max_results, with_truncated=False)


TOOLS = {"search_logs": search_logs, "search_changes": search_changes, "search_runbook": search_runbook}
