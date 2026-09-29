# Design decisions

Decisions to settle before implementation. Tool behaviour, the final output shape and the
sandboxing/concurrency rules are fixed by the exercise brief and `docs/tool-contracts.md` /
`docs/output-contract.md` in the exercise repo; everything below is open.

Status: `open` | `proposed` | `decided` | `revisited`. Confirm each `proposed` entry (or
change it) before building that part.

Stack: Python, uv project, run locally as a CLI.

---

## D1. Runtime and framework

- **Question:** Language, raw SDK vs framework, model per agent.
- **Status:** proposed
- **Decision:**
  - uv project, `src/` layout; `typer` CLI, `pydantic` schemas, `python-dotenv` for
    `TRIAGE_DATA_DIR`. Entry point in `[project.scripts]`: `uv run triage INC-2043`.
  - Official `anthropic` SDK, `AsyncAnthropic`, no agent framework. Own manual tool loop
    (not the SDK Tool Runner).
  - Concurrency with `asyncio.gather(..., return_exceptions=True)`.
  - `claude-opus-5-5` for every agent. Set `effort` explicitly (default on this model is
    `medium`): `medium` for the specialists, `high` for the synthesis call.
- **Why:** The fan-out and join are the point of the exercise; a framework would hide them.
  `return_exceptions=True` keeps one failed branch from taking down the other (see D7).
- **Consider later:** `TaskGroup` instead, if you want a failure to cancel the sibling.

## D2. Reusable specialist loop

- **Question:** What parameterises one generic agent loop?
- **Status:** proposed
- **Decision:**
  - One `run_specialist(spec, context) -> Result`. `spec` is a dataclass: prompt path, one
    tool (definition + callable), output model, `max_tool_calls`, timeout.
  - Defaults: 8 tool calls, 90 s. At the cap, return partial findings with
    `complete=False` rather than raising.
  - Final answer via structured outputs (`output_config.format`), validated with pydantic;
    one retry on validation failure.
  - `tool_choice: auto` + `strict: true` on the tool; the prompt says when to search.
    (Forced `any`/`tool` choice returns 400 on this model.)
  - No `submit_findings` tool: that would break "exactly one tool".
- **Why:** The historian should be the analyst with different config; if it isn't, the
  loop is wrong. Partial results feed the confidence rules in D5.

## D3. Intermediate schemas

- **Question:** What does each specialist return, so the join has keys to work with?
- **Status:** proposed
- **Decision:**
  - `LogFinding`: `ts` (tz-aware datetime), `service`, `kind`
    (error / warning / deploy / config / recovery), `observation`, `attributes: dict`
    (e.g. `pool_max=4`), `evidence: list[str]` as `file:line`.
  - `ChangeFinding`: `change_id`, `effective_at`, `services`, `kind`,
    `keys_changed: list[{key, old, new}]`, `reverted_by: str | None`, `evidence`.
  - Both results also carry `hypotheses_checked: list[{theory, verdict, evidence}]` and
    `complete: bool`.
  - `effective_at` = `Shipped`, else `Applied`, else `Merged`. Parsed in the tool layer so
    `search_changes` orders by the same date.
- **Why:** Time, service and config key are the join keys; they have to be typed fields,
  not prose, for the join in D5 to run in code.

## D4. Context handed to each agent

- **Question:** Does the reporter's theory reach the specialists, and who writes the runbook
  symptom description?
- **Status:** proposed
- **Decision:**
  - Both specialists get the full incident report, plus an orchestrator note that the
    reporter's theories are unverified hypotheses to test.
  - The runbook lookup gets a symptom description built by a code template from the joined
    findings (symptom, affected service, observed metrics). Never raw lines or change ids.
- **Why:** Hiding the theory loses information; making it a hypothesis to test produces
  `ruled_out` evidence directly. A template keeps the runbook input deterministic and
  inspectable.

## D5. Orchestrator: code vs model call

- **Question:** Which parts of join-and-decide are code, and which are a model call?
- **Status:** proposed
- **Decision:** Hybrid.
  - **Correlation in code:** a change pairs with a log finding when it touches the same
    service and took effect 0 to 15 minutes before the first error. A config key from the
    change appearing in the log attributes is a strong match.
  - **Confidence by rule in code:**
    - `high`: a strong match, and at least one specialist found evidence ruling out other
      causes.
    - `medium`: a match, but a weaker one.
    - `low`: only one side found anything, or more than one change matches.
  - **One synthesis model call** (no tools) writes `summary`, `correlation` and
    `ruled_out` from the matched pairs and the specialists' findings.
- **Why:** Confidence rules can be tested and must drop when the change log is removed.
  The synthesis call only writes prose over facts code already joined, so "the orchestrator
  has no tools" still holds.

## D6. Guardrails: code vs prompt

- **Question:** Which traps are enforced in code after the fact?
- **Status:** proposed
- **Decision:**
  - Enforce in code: a `LEG-*` entry is never `runbook.matched`; every evidence id appears
    in a tool result from this run; `remediation` steps appear verbatim in the matched
    entry.
  - When a check fails, log it to the trace and move the claim to `open_questions`. Do not
    silently fix it.
  - `matched: null` plus RB-000 actions is decided by the prompt and checked in code.
- **Why:** Code checks are reliable; logging the violation keeps prompt weaknesses visible
  instead of masking them.

## D7. Failure and timeout behaviour

- **Question:** What happens when a parallel branch throws, times out or returns nothing?
- **Status:** proposed
- **Decision:**
  - Degrade, never crash: keep whatever finished, cap confidence at `low`, and add an open
    question naming the missing branch.
  - 90 s per specialist (D2), 120 s for the whole run. API errors use the SDK's built-in
    retries (2).
- **Why:** A partial answer with honest confidence is more useful to a duty manager than a
  stack trace, and it's what the iteration prompts test.

## D8. Observability

- **Question:** What does each run record?
- **Status:** proposed
- **Decision:**
  - `out/runs/<incident>-<timestamp>/` containing `trace.jsonl` (every model and tool call:
    agent, start/end, tokens), `result.json`, `result.md`.
  - Print a timing summary to stdout showing that the specialists overlap.
- **Why:** Proves self-check #2 (concurrency) and gives run-to-run output to compare for
  stability.

## D9. Smaller choices

- **Question:** Prompt layout/templating, CLI entry point, shared search core, tests.
- **Status:** proposed
- **Decision:**
  - Prompts in `prompts/<agent>.md` with `string.Template` placeholders (`$incident`); no
    Jinja.
  - One `corpus.py` with shared term matching; each tool supplies its retrieval unit
    (line / record / entry) and sort order.
  - `pytest` for the tool table in `tool-contracts.md` first; an INC-2043 golden test
    marked `slow`.
- **Why:** Prompt diffs stay readable; the three tools differ only in unit and order; the
  tools are tested before any model is involved.
