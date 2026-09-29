# Implementation plan

Scope: the core system (self-checks 1 to 9) plus the brief's iteration prompts. Stretch goals
are out of scope. The design is fixed by [decisions.md](decisions.md) (D1 to D11) and
[contracts.md](contracts.md); this plan orders the work and says how to know each step is
done.

**You write the code.** The brief's first rule applies: this plan says what to build and
how to check it, not how to write it.

Runbook input carries the symptom only, never the suspected cause (contracts §5).

---

## Layout

```
pyproject.toml            uv project, [project.scripts] triage = "triage.cli:app"
.env                      TRIAGE_DATA_DIR (git-ignored; no model credentials, D11)
prompts/
  log_analyst.md
  change_historian.md
  runbook_lookup.md
  synthesis.md
src/triage/
  cli.py                  typer app: run, eval-tools, eval-runbook
  config.py               data dir, model, effort, caps, timeouts
  corpus.py               shared term matching and splitting (D9)
  tools.py                search_logs, search_changes, search_runbook
  schemas.py              pydantic models for every contract
  trace.py                trace.jsonl writer (contracts §9)
  agent.py                run_specialist via the Claude Agent SDK (D2, D11)
  specialists.py          the three specialist specs
  join.py                 correlation and confidence rules (D5)
  guardrails.py           evidence, archive and remediation checks (D6)
  synthesis.py            the one synthesis call
  orchestrator.py         fan-out, join, runbook, synthesis, assembly
  faults.py               fault injection (D10)
  render.py               result.md
tests/
```

Grader commands below assume the grader sits next to this repo:
`G=../agentic_exercise_grader/grade.py`.

---

## Step 0. Project setup

- `uv init`, `src/` layout, and dependencies `claude-agent-sdk`, `pydantic`, `typer`,
  `python-dotenv`; `pytest` as a dev dependency.
- Remove the OAuth tokens from `.env`. The Agent SDK uses the Claude Code login, not an
  environment variable (D11).
- `config.py` reads `TRIAGE_DATA_DIR` and fails clearly if `logs/` is missing.
- `triage --help` runs.

**Done when:** `uv run triage --help` works and `uv run pytest` runs (with zero tests).

## Step 1. Benchmark

Work INC-2043 out by hand before building anything, as the brief asks: root cause, start
time, what the reporter's theory gets wrong, and which runbook entry applies. Write it in
`docs/benchmark.md`.

If you have already read the grader's goldens, the benchmark is no longer independent.
Write down what you would have concluded anyway, and note that it was influenced.

**Done when:** `docs/benchmark.md` exists.

## Step 2. Tools (no model)

- `corpus.py`: split a query into terms, match every term as a case-insensitive
  substring, return an error for an empty query.
- `search_logs`: line unit, sorted by the leading timestamp, `truncated` when capped.
- `search_changes`: record unit (`## ` headings), full record text, newest first by
  `effective_at` (D3: `Shipped`, else `Applied`, else `Merged`; leading datetime only).
- `search_runbook`: entry unit across every `.md` file including `_archive/`, `source`
  relative to `runbook/`, ordered by terms matched then id, no `status` field.
- `triage eval-tools <tools.json> -o out/tools.json` (D10).
- Tests: the table at the end of `tool-contracts.md`.

**Done when:** `python3 $G tools out/tools.json` passes every case.

## Step 3. Schemas

- One pydantic model per contract in `contracts.md` §1 to §8, plus `TriageResult` written
  from `output-contract.md`.
- Evidence validator for the three reference formats.
- Test: every model sent to structured outputs has `additionalProperties: false` on every
  object and no free-form dicts, checked on `model_json_schema()`.
- Test: the grader's golden `reference` objects validate against `TriageResult`. Read
  them only for this: the test takes the file path as an argument and never copies the
  content into this repo (D10 isolation).

**Done when:** schema tests pass.

## Step 4. Trace and run folder

- `trace.py` writes contracts §9 events with `ts` from `time.monotonic()` relative to run
  start.
- `start` and `end` events come from a context manager whose exit writes `end` in a
  `finally`.
- The run folder is `out/runs/<incident>-<timestamp>/`.

**Done when:** a unit test writes a start, input, tool_call and end event and
reads back the expected lines.

## Step 5. Model access and SDK spike

- Confirm Claude Code is installed and logged in (`claude --version`, then a trivial
  `claude -p "hi"`).
- Read the current Agent SDK docs and check the option names D11 relies on:
  `allowed_tools`, `disallowed_tools`, `setting_sources`, `system_prompt`, `max_turns`,
  in-process MCP tools, and any JSON-schema output option. Note in D11 anything that
  differs.
- A throwaway script: one query with one in-process MCP tool (a stub), only that tool
  allowed, our own system prompt, no settings loaded, run from an empty temporary
  directory. It prints every tool call the model makes.

**Done when:** the spike gets a reply, the stub tool is called under its expected name,
and no built-in tool appears when the prompt invites one ("read ./README.md").

## Step 6. Specialist loop and log analyst

- `agent.py`: `run_specialist(spec, context)` per D2 as amended by D11:
  - A Claude Agent SDK query with the specialist's one tool as an in-process MCP tool,
    every other tool disallowed, no settings loaded, an empty temporary working directory
    and the prompt file as `system_prompt`.
  - The tool-call cap is a counter in the tool wrapper; past the cap it returns an error
    telling the agent to finish. `max_turns` is a backstop, and `asyncio.wait_for` enforces
    the timeout.
  - Structured output through the SDK if the spike found support. Otherwise parse the final
    message as JSON and validate with pydantic, with one retry.
  - The tool wrapper writes `tool_call` trace events with the plain tool name.
  - Returns a `SpecialistRun` (contracts §4) and never raises; `status` reflects what
    happened.
- Isolation test (D11): prompts inviting a file read, a directory listing and a shell
  command produce no call other than the agent's own tool.
- `prompts/log_analyst.md`: role, the incident, the hypotheses note, the evidence rule,
  when to stop. No change ids, ISO timestamps or `file.log:n` examples (D8).
- A temporary debug command runs the analyst alone on INC-2043.

**Done when:**
- On INC-2043 the analyst returns a valid `LogAnalystResult` whose evidence all exists in
  the data.
- `first_failure_at` matches your benchmark.
- The trace shows its tool calls.
- The isolation test passes.

## Step 7. Change historian

A new prompt and spec only. If `agent.py` has to change, note why: the brief says it is the
signal the loop was less reusable than you thought.

**Done when:** on INC-2043 the historian returns a valid `ChangeHistorianResult` and
its input payload contains no ISO timestamp or log reference.

## Step 8. Runbook lookup

- Prompt: find the matching current entry for a symptom, record every entry read in
  `considered`, treat `_archive/` sources as superseded, return `matched: null` with the
  no-match entry as `fallback` when nothing current fits.
- `triage eval-runbook <variants.json> -o out/runbook.json` (D10).

**Done when:** `python3 $G runbook out/runbook.json` passes every case, including the
no-match case and the vague-symptom case that word-matches the archive.

## Step 9. Orchestrator

In order:

1. **Fan-out.** Both specialists via `asyncio.gather(..., return_exceptions=True)` under
   the whole-run timeout. Identical input, and neither sees the other's output.
2. **Join** (`join.py`). Pair log findings with changes on service and time window, mark
   config-key matches as `strong`, compute `confidence` and `confidence_reason` by D5's
   rules, and cap at `low` when a branch is missing (D7).
3. **Runbook.** Build the §5 input from the analyst's `failure_mode` and attributes, with
   the symptom only. Skip the lookup when there is no symptom (D7).
4. **Synthesis.** One call with the §7 input and no tools.
5. **Guardrails** (`guardrails.py`). Remove citations missing from this run's tool
   results, reject a `LEG-` match, and check remediation is verbatim. Record each violation
   in the trace and in `open_questions` (D6).
6. **Assemble** `TriageResult` per §8, validate it, write `result.json` and `result.md`,
   and print the timing summary.

`triage run INC-2043` is the one command (self-check 1).

**Done when:** `python3 $G run INC-2043 <result.json> --trace <trace.jsonl>` passes its
contract, citation and trace checks. The golden checks may still fail at this point.

## Step 10. Get the three incidents passing

- INC-2043 first, iterating prompts until the golden checks pass and the result matches
  your benchmark.
- Then INC-2051 and INC-2062. INC-2062 is the no-match trap: the fix belongs in the
  runbook prompt, not in a special case in code.
- Keep a short log in `docs/iteration-log.md` of each change: what failed, whether it was
  a prompt or an architecture problem, and what you changed. It feeds the write-up.

**Done when:** `grade.py run` passes every hard check for all three incidents,
each with its trace.

## Step 11. Fault injection and variants

- `faults.py` plus the D10 flags on `triage run`: `--empty-tool`, `--fail-agent`,
  `--delay-agent`, `--max-tool-calls`. Inject inside the tool or agent, not by skipping the
  call.
- Run each variant: `python3 $G variant <id> <result.json> --trace <trace.jsonl>`.

**Done when:** every non-exploratory variant in `variants.json` passes.

## Step 12. Iteration prompts

Each is an experiment. Record the result in `docs/iteration-log.md` before changing
anything.

| Iteration prompt | How to run it | What to record |
|---|---|---|
| One parallel agent fails | `--fail-agent change_historian` (V-2043-historian-fails) | Crash, hang or degrade? Was that the intended outcome? |
| One parallel agent is slow | `--delay-agent log_analyst=200` (V-2043-analyst-timeout) | What happens to the half-finished fan-out; whether confidence reflects it |
| Confidence without the change log | `--empty-tool search_changes` (V-2043-no-changes, V-2051-no-changes) | Does confidence drop? It should. |
| No log results | `--empty-tool search_logs` (V-2043-no-logs) | Does the system admit it has no timeline, or invent one? |
| Three tool calls per specialist | `--max-tool-calls 3` (V-2043-tool-cap-3, exploratory) | Which checks fail compared with the uncapped runs, and what that says about the search strategy |
| "timeout" becomes "latency" | The `RL-pool-latency` case in step 8's runner | Whether the lookup still finds the entry, and why |
| A follow-up question to one specialist | New design work: see below | What the second question adds, and what it costs |
| Five identical runs | `triage run INC-2043` five times, then `grade.py run INC-2043 out/runs/INC-2043-*/result.json` | Pass rate per check, and which checks are unstable |

**Follow-up question.** This is the only iteration prompt that changes the design. Before
building it, add D12 to `decisions.md` covering:

- which specialist gets the follow-up, and what triggers it
- what extra context it may see, which must still pass the grader's context-leak checks
- the tool-call budget for the second round
- how its answer is merged into the join

There is no temperature setting to lean on: `claude-opus-5-5` rejects sampling parameters,
and the Agent SDK doesn't expose them. Stability comes from prompts and schemas alone.
Stability sweeps run on the Claude subscription (D11), so spread them out if you hit
usage limits.

**Done when:** every row has an entry in the iteration log.

## Step 13. Write-up

Answer the brief's debrief questions in `docs/write-up.md`, drawing on the iteration log:

- where the orchestrator wanted to become a model call
- what the join cost
- which failures were prompt problems and which were architecture problems
- what the citation rule cost and what it bought
- what a fourth specialist would require
- what writing without a framework cost

**Done when:** the write-up exists and the full grader sweep (tools, runbook, three
incidents, all variants, five-run stability) has been run once more on the final code.
