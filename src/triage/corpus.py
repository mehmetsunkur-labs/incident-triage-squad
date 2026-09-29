"""Matching and splitting shared by the three search tools (D9).

Deliberately dumb, as tool-contracts.md requires: whitespace terms, case-insensitive plain
substrings, every term must match. No stemming, synonyms, regex or ranking.
"""

import re
from dataclasses import dataclass

HEADING = re.compile(r"^## ", re.M)
ENTRY_HEADING = re.compile(r"^(?P<id>[A-Z]+-\d+)\s+-\s+(?P<title>.+)$")
EMPTY_QUERY_ERROR = {"error": "query must contain at least one term"}


def split_terms(query: str) -> list[str]:
    return [t.lower() for t in query.split()]


def matches(text: str, terms: list[str]) -> bool:
    low = text.lower()
    return all(t in low for t in terms)


@dataclass(frozen=True)
class Section:
    """One `## ` section of a markdown file: a change record or a runbook entry."""

    id: str
    title: str
    body: str

    @property
    def searchable(self) -> str:
        return f"{self.id} - {self.title}\n{self.body}"


def split_sections(markdown: str) -> list[Section]:
    """Split on `## ` headings. Text before the first heading is dropped, as are headings
    without an `ID - Title` shape. Trailing `---` separators are trimmed from bodies."""
    sections = []
    for block in HEADING.split(markdown)[1:]:
        heading, _, body = block.partition("\n")
        m = ENTRY_HEADING.match(heading.strip())
        if not m:
            continue
        body = re.sub(r"\n\s*---\s*$", "", body.strip()).strip()
        sections.append(Section(m["id"], m["title"].strip(), body))
    return sections
