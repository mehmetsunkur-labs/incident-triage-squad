# Workflow: one run of `triage run INC-2043`

What happens, in order, when the system triages one incident: which stages run
concurrently, what data crosses each boundary, who may see it, and what happens when a
stage fails. This page describes the flow and links to the design; the shapes live in
[contracts.md](contracts.md) and the reasons in [decisions.md](decisions.md).

Legend: **[built]** exists in `src/triage/`; **[yours]** is written in plan steps 6 to 9.

```
 triage run INC-2043 [--empty-tool ...] [--fail-agent ...] [--delay-agent ...] [--max-tool-calls N]
   │
   ▼
 0. CLI [built] ── settings, faults, incident report, run folder, trace.jsonl, faults.json
   │
   ▼
 1. orchestrator.run_incident [yours] ── builds SpecialistInput (§1), the same for both
   │
   ├───────────────── 2. fan-out, concurrently ─────────────────┐
   ▼                                                            ▼
 run_specialist(log_analyst)                      run_specialist(change_historian)
   tool: search_logs                                tool: search_changes
   → SpecialistRun[LogAnalystResult] (§2, §4)       → SpecialistRun[ChangeHistorianResult] (§3, §4)
   │                                                            │
   └────────────────────────────┬───────────────────────────────┘
                                ▼
 3. join, code only ── correlations and confidence (D5), capped by D7
                                │
                                ▼
 4. runbook ── symptom only, RunbookInput (§5) ──► run_specialist(runbook_lookup)
                                │                    tool: search_runbook
                                │                    → SpecialistRun[RunbookResult] (§6)
                                │   (skipped when there is no symptom, D7)
                                ▼
 5. synthesis ── SynthesisInput (§7) ──► run_specialist(synthesis), no tool → SynthesisResult (§7)
                                │
                                ▼
 6. guardrails, code only (D6) ── violations go to the trace and open_questions
                                │
                                ▼
 7. assemble TriageResult (§8), validate ── CLI writes result.json and result.md, prints timing
```

---

## Stages

| # | Stage | Who | In | Out | Design |
|---|---|---|---|---|---|
| 0 | Set up the run | CLI [built] | CLI args, `.env` | `Settings`, `Faults`, report text, `out/runs/<id>-<time>/`, `Trace` | D8, D10 |
| 1 | Build specialist input | orchestrator [yours] | report text | `SpecialistInput` | D4, §1 |
| 2 | Fan-out | `run_specialist` ×2 [yours] | `SpecialistInput` | two `SpecialistRun`s | D1, D2, D11, §2 to §4 |
| 3 | Join | code [yours] | both runs | `Correlation`s, `confidence`, `confidence_reason` | D5, D7, §7 |
| 4 | Runbook lookup | `run_specialist` [yours] | `RunbookInput` built from the analyst's result | `SpecialistRun[RunbookResult]`, or skipped | D4, D7, §5, §6 |
| 5 | Synthesis | `run_specialist`, zero-tool spec [yours] | `SynthesisInput` | `SpecialistRun[SynthesisResult]` | D5, D11, §7 |
| 6 | Guardrails | code [yours] | everything above, including every run's `evidence_seen` | cleaned results, violations | D6, §4 |
| 7 | Assemble | code [yours], then CLI [built] writes | everything above | `TriageResult`, `result.json`, `result.md` | D8, §8 |

## Inside `run_specialist` (stages 2 and 4)

The same function for the three specialists and synthesis; only the spec differs (D2,
D11). Synthesis is a zero-tool spec: no MCP server, `allowed_tools=[]`, no `tool_call`
events.

```
trace.span(agent) opens ──► start event
  faults.before_agent(agent, faults)        delay or InjectedFault (D10)
  render prompt from template               span.input(full prompt)
  query(...) with one in-process MCP tool   tools=[], setting_sources=[], empty cwd (D11)
    │  model calls the tool ──► wrapper: cap check, faults.wrap_tool, span.tool_call(plain name),
    │                           add returned refs to evidence_seen
    │  model submits via StructuredOutput
    ▼
  ResultMessage.structured_output ──► pydantic validate ──► one retry on failure
trace.span closes ──► end event with status
→ SpecialistRun(status ok | partial | failed | timeout)
```

The stage timeout (`asyncio.wait_for`, 90 s, D7) wraps the span from outside, so a timeout
is recorded as `timeout` in the trace. The whole run also has a 300 s backstop, which should
never fire. `run_specialist` never raises: the fan-out and the
D7 branches depend on getting a status back.

## Who sees what

The sandboxing rules from the brief, as they apply to each stage. The grader checks the
`input` payloads for the leak patterns in §9.

| Agent | Receives | Never receives | Tool |
|---|---|---|---|
| log_analyst | incident report, hypotheses note | change records, the historian's output | `search_logs` |
| change_historian | incident report, hypotheses note | log lines, the analyst's output | `search_changes` |
| runbook_lookup | symptom, service, observed key/values | log lines, change records, evidence ids, the raw report | `search_runbook` |
| synthesis | everything code has joined (§7) | nothing new: it gets no tools | none |

## Failure branches

| What happens | Effect on the run | Design |
|---|---|---|
| A specialist raises, or `--fail-agent` | `SpecialistRun.status = failed`, `result = null`; the run continues | D7, §4 |
| A specialist exceeds its timeout, or `--delay-agent` past it | `status = timeout`, `result = null`; the run continues | D7, §4 |
| A specialist hits the tool-call cap | `status = partial`, with a result | D2, D11 |
| A tool returns nothing, or `--empty-tool` | `status = ok` with empty findings and `gaps` filled | D10 |
| Either branch has no result | confidence capped at `low`; an open question names the missing branch | D7 |
| The analyst has no result or no `failure_mode` | runbook lookup skipped: `matched: null`, one orchestrator action to follow RB-000 | D7, §5 |
| Nothing current matches the symptom | `matched: null`, `fallback` = RB-000; actions follow it | §6 |
| A guardrail fails (unknown citation, `LEG-` match, non-verbatim step) | the claim is removed or moved to `open_questions`; the violation is logged | D6 |
| Output fails validation after the retry | `status = failed` for that agent | D2 |

## Trace events along the way

Each specialist, and the synthesis call, writes `start`, `input`, any `tool_call`s and
`end` (§9). The analyst's and historian's spans overlap in time; that overlap is what the
grader's concurrency check measures. Nothing is written by the join, guardrails or
assembly except guardrail violations.

---

## Gaps found while writing this, and how they were settled

1. **Timeouts didn't add up** (90 s per specialist, 120 s per run, sequential stages). Now
   every model stage has its own 90 s timeout and the run has a 300 s backstop (D7).
2. **The citation guardrail had no data to check against.** `SpecialistRun` now carries
   `evidence_seen`, filled by the tool wrapper (D6, §4).
3. **Synthesis has no tool.** It goes through `run_specialist` as a zero-tool spec (D11).
4. **Nothing wrote `result.md`.** `render.py` does now, from the CLI (D8).
