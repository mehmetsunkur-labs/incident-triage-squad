"""Guardrails (plan step 9, D6): checks in code after synthesis. Each violation is recorded
and surfaced in open_questions rather than silently fixed.
"""

from dataclasses import dataclass, field
from pathlib import Path

from .corpus import split_sections
from .schemas import LOG_REF, CHG_REF, Correlation, RuledOut, SynthesisResult, TimelineEntry


def normalise(text: str) -> str:
    return " ".join(text.split())


def runbook_texts(data_dir: Path) -> dict[str, str]:
    """Entry id -> whitespace-normalised entry text, for the verbatim check."""
    root = data_dir / "runbook"
    return {s.id: normalise(s.body) for p in sorted(root.rglob("*.md")) for s in split_sections(p.read_text())}


@dataclass
class Guard:
    seen: set[str]
    violations: list[dict] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)

    def flag(self, check: str, detail: str, question: str | None = None) -> None:
        self.violations.append({"check": check, "detail": detail})
        if question:
            self.open_questions.append(question)

    def keep_seen(self, where: str, evidence: list[str]) -> list[str]:
        """Drop citations no tool returned during this run."""
        kept = [e for e in evidence if e in self.seen]
        dropped = [e for e in evidence if e not in self.seen]
        if dropped:
            self.flag("citation_not_seen", f"{where}: removed {dropped}")
        return kept

    def timeline(self, entries: list[TimelineEntry]) -> list[TimelineEntry]:
        out = []
        for t in entries:
            ev = self.keep_seen(f"timeline {t.time.isoformat()}", t.evidence)
            if ev:
                out.append(t.model_copy(update={"evidence": ev}))
            else:
                self.flag("claim_unsupported", f"timeline entry dropped: {t.event}",
                          f"Unsupported timeline claim, no citation from this run: {t.event}")
        return out

    def ruled_out(self, items: list[RuledOut]) -> list[RuledOut]:
        out = []
        for r in items:
            ev = self.keep_seen(f"ruled_out {r.theory!r}", r.evidence)
            if ev:
                out.append(r.model_copy(update={"evidence": ev}))
            else:
                self.flag("claim_unsupported", f"ruled_out dropped: {r.theory}",
                          f"Could not rule out, no citation from this run: {r.theory}")
        return out

    def root_cause_evidence(self, synthesis: SynthesisResult, correlations: list[Correlation]) -> list[str]:
        """Seen citations only; when code found a correlation, the root cause must cite both
        corpora, so missing sides are added from the correlation code computed."""
        ev = self.keep_seen("root_cause", synthesis.root_cause_evidence)
        if correlations:
            top = correlations[0]
            if not any(LOG_REF.match(e) for e in ev):
                add = [e for e in top.log_evidence if e in self.seen]
                self.flag("root_cause_one_corpus", f"no log citation; added {add} from the correlation")
                ev += add
            if not any(CHG_REF.match(e) for e in ev) and top.change_id in self.seen:
                self.flag("root_cause_one_corpus", f"no change citation; added {top.change_id} from the correlation")
                ev.append(top.change_id)
        return list(dict.fromkeys(ev))

    def remediation(self, entry_id: str, steps: list[str], texts: dict[str, str]) -> list[str]:
        """Keep only steps that appear verbatim (whitespace-normalised) in the entry."""
        text = texts.get(entry_id, "")
        kept = [s for s in steps if normalise(s) in text]
        for s in steps:
            if normalise(s) not in text:
                self.flag("remediation_not_verbatim", f"{entry_id}: {s[:120]}",
                          f"A remediation step was not found verbatim in {entry_id} and was removed: {s[:120]}")
        return kept
