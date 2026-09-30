# Implementation plan

Scope: the core system (self-checks 1 to 9) plus the brief's iteration prompts. Stretch goals
are out of scope. The design is fixed by [decisions.md](decisions.md) (D1 to D11) and
[contracts.md](contracts.md); this plan orders the work and says how to know each step is
done.

**You write the code.** The brief's first rule applies: this plan says what to build and
how to check it, not how to write it.

Runbook input carries the symptom only, never the suspected cause (contracts §5).

**Who builds what.** Claude built the plumbing: steps 0, 2, 3 and 4, the fault-injection
flags from step 11, the `run`, `eval-tools` and `eval-runbook` commands, `result.md`
rendering, and the two SDK spikes (steps 5 and 6a). At your request Claude also wrote steps
6b (the specialist loop and the log analyst prompt), 7 (the change historian), 8 (the
runbook lookup) and 9 (the orchestrator) for your review. What's left is yours:
step 10's prompt iteration across the three incidents, running the variants and iteration
prompts (steps 11 and 12), and the write-up (step 13), which should say which parts Claude
wrote.

| Step | Status |
|---|---|
| 0 Setup | done |
| 1 Benchmark | yours |
| 2 Tools | done: `grade.py tools` passes 31/31 |
| 3 Schemas | done |
| 4 Trace | done |
| `result.md` rendering | done (`render.py`, D8) |
| 5 SDK spike | done (D11) |
| 6a SDK spike for the loop | done (D11), by Claude |
| 6b Loop and log analyst | written by Claude, awaiting your review |
| 7 Change historian | written by Claude, awaiting your review |
| 8 Runbook lookup | written by Claude, awaiting your review: `grade.py runbook` 7/7 |
| 9 Orchestrator | written by Claude, awaiting your review: INC-2043 passes every hard check |
| 10 Three incidents | join, stall tracing and synthesis action rules revised by Claude for review (D5, D7, D8); see `docs/iteration-log.md` |
| 11 Faults and variants | flags done; running the variants is yours |
| 12, 13 | yours |

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
  render.py               result.md (built)
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

## Step 6a. Agent SDK spike (Claude)

`spikes/step6_sdk.py`, a throwaway script outside `src/`, answers what D11 left open before
`run_specialist` is written: `ClaudeSDKClient` with the real tool, the full
`LogAnalystResult` schema through `output_format`, a follow-up retry after a forced
validation failure, and the shape of an error result.

**Done:** findings recorded in D11 under "Verified in the step 6a spike". Read those
rather than the script; the script is deliberately not a template.

## Step 6b. Specialist loop and log analyst

Written by Claude at your request, for you to review; this departs from the original split
(the loop and prompts were yours). The write-up should say so.

Built: `src/triage/agent.py` (`SpecialistSpec`, `ToolState`, `run_specialist`),
`src/triage/specialists.py` (the log analyst spec, `specialist_input`),
`prompts/log_analyst.md`, `triage debug-agent <agent> <incident>`, `tests/test_agent.py`
(12 fast tests with a fake client) and `tests/test_agent_live.py` (5 slow tests with the real
model, `uv run pytest -m slow`).

- `agent.py`: `run_specialist(spec, context)` per D2 as amended by D11:
  - A spec per D2's step 6 design: `agent`, `prompt_file`, `tool` (or `None`),
    `output_model`, `user_message`, optional overrides. The prompt file is the system
    prompt as written; the context goes in the user message.
  - A Claude Agent SDK session (`ClaudeSDKClient`) with the options D11 fixes:
    - the specialist's one tool as an in-process MCP tool, with `maxResultSizeChars` raised
    - `tools=[]`, `allowed_tools` set to that tool only, `strict_mcp_config=True`
    - `setting_sources=[]`, set explicitly
    - an empty temporary working directory, and the prompt file as `system_prompt`
  - The tool-call cap is a counter in the tool wrapper; past the cap it returns
    `is_error: True` with "limit reached, submit now, list gaps", never raising.
    `max_turns` is the cap plus 3, and `asyncio.wait_for` enforces the timeout from
    outside the span.
  - Structured output through `output_format` with the pydantic model's schema, read from
    `ResultMessage.structured_output`, then validated with pydantic. On failure, one
    follow-up in the same session with the errors, inside the same timeout and cap.
    First check that `output_format` holds for a follow-up; if not, retry with a fresh
    query (D11).
  - Status: `ok`; `partial` = valid result and cap hit; `failed` = no valid result after
    the retry, including `max_turns`; `timeout` (D2).
  - The tool wrapper writes `tool_call` trace events with the plain tool name, and adds
    every reference the tool returned to the run's `evidence_seen` (D6, contracts §4).
    Nothing is logged from the message stream, which also carries `StructuredOutput`.
  - The tool is optional in a spec, so synthesis can reuse the same function (D11).
  - Returns a `SpecialistRun` (contracts §4) and never raises; `status` reflects what
    happened.
- Tests for D11, including the items the spike did not verify:
  - **Isolation:** prompts inviting a file read, a directory listing and a shell command
    produce no call other than the agent's own tool.
  - **Settings:** a canary in `CLAUDE.md` in the working directory, and one in
    auto-memory, never appears in output.
  - **Cap:** with `max_tool_calls=2`, the third call gets the finish error, the run is
    `partial`, and a result still comes back.
  - **Timeout:** a tool that sleeps past the timeout ends the run as `timeout`, and no
    Claude Code process is left running.
  - **Large results:** a tool result larger than the biggest real `search_changes` output
    reaches the model inline, not as a file preview.
  - **Evidence format:** every evidence string in the result passes the D6 validator.
- `prompts/log_analyst.md`: role and blind spot, how the tool matches, a search method,
  the evidence rule, when to stop. No placeholders: the incident and hypotheses note arrive
  in the user message. No change ids, ISO timestamps or `file.log:n` examples (D8).
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

Written by Claude at your request, for you to review. Built: the `CHANGE_HISTORIAN` spec in
`src/triage/specialists.py`, `prompts/change_historian.md`, 3 fast tests and 1 slow test.
**`agent.py` needed no change**, which is worth recording for the debrief question about
what a fourth specialist would cost.

## Step 8. Runbook lookup

- Prompt: find the matching current entry for a symptom, record every entry read in
  `considered`, treat `_archive/` sources as superseded, return `matched: null` with the
  no-match entry as `fallback` when nothing current fits.
- `triage eval-runbook <variants.json> -o out/runbook.json` (D10).

**Done when:** `python3 $G runbook out/runbook.json` passes every case, including the
no-match case and the vague-symptom case that word-matches the archive.

Written by Claude at your request, for you to review. Built:
- the `RUNBOOK_LOOKUP` spec and `runbook_user_message` in `specialists.py`
- `prompts/runbook_lookup.md`
- `orchestrator.lookup_runbook`
- `debug-agent runbook_lookup --symptom ...`
- `eval-runbook` running cases concurrently (`--concurrency`, default 3) and saving each run
- 3 fast tests and 2 slow ones

`agent.py` again needed no change. The first `grade.py runbook` run passed 7/7 with no
tuning.

## Step 9. Orchestrator

In order:

1. **Fan-out.** Both specialists via `asyncio.gather(..., return_exceptions=True)`, each
   under its own stage timeout (150 s), with the run backstop (600 s) around the whole run
   (D7). Identical input, and neither sees the other's output.
2. **Join** (`join.py`). Pair log findings with changes on service and time window, mark
   config-key matches as `strong`, compute `confidence` and `confidence_reason` by D5's
   rules, and cap at `low` when a branch is missing (D7).
3. **Runbook.** Build the §5 input from the analyst's `failure_mode` and attributes, with
   the symptom only. Skip the lookup when there is no symptom (D7).
4. **Synthesis.** One call with the §7 input and no tools.
5. **Guardrails** (`guardrails.py`). Remove citations missing from this run's tool
   results (the union of every run's `evidence_seen`), reject a `LEG-` match, and check
   remediation is verbatim. Compare with whitespace normalised: runbook entries break
   lines mid-sentence, and the lookup joins them (step 8). Record each violation
   in the trace and in `open_questions` (D6).
6. **Assemble** `TriageResult` per §8, validate it, write `result.json` and `result.md`,
   and print the timing summary.

`triage run INC-2043` is the one command (self-check 1).

**Done when:** `python3 $G run INC-2043 <result.json> --trace <trace.jsonl>` passes its
contract, citation and trace checks. The golden checks may still fail at this point.

Written by Claude at your request, for you to review. Built:
- `src/triage/join.py`: correlation and confidence rules (D5)
- `src/triage/guardrails.py` (D6)
- `prompts/synthesis.md` and the `SYNTHESIS` zero-tool spec
- `orchestrator.run_incident`: fan-out, join, runbook input, synthesis, guardrails and
  assembly (§8), under the run backstop (D7)
- `tests/test_orchestrator.py`: 22 fast tests, including every D7 branch through a fake
  `run_specialist`

The first real `triage run INC-2043` passed **every hard grader check**, golden ones
included, with one soft warning (CHG-1043 not ruled out, which matches the benchmark).
An accidental run of the V-2043-no-changes variant also passed.

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
