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
- **Status:** revisited, amended by D11 (model access goes through the Claude Agent SDK;
  the rest stands)
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
- **Status:** revisited, amended by D11 (the SDK drives the loop; the `run_specialist`
  interface, caps and partial results stand) and by the step 6 design below
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
- **Step 6 design (supersedes the spec and status wording above):**
  - **Spec:** a frozen dataclass with `agent` (an `AgentName`), `prompt_file` (in
    `prompts/`), `tool` (a tool name, or `None` for synthesis), `output_model` (the pydantic
    class), `user_message` (a function from this agent's context to text), and optional
    `effort`, `max_tool_calls` and `timeout_s` overrides, where `None` means the setting.
  - **Prompts:** the system prompt is the prompt file verbatim, with no placeholders. The
    per-incident context (the report and hypotheses note, the runbook input, the synthesis
    input) goes in the user message, built by the spec's `user_message`. Both parts are
    logged together as the trace `input` payload. This amends D9.
  - **Status:**
    - `ok`: a valid result
    - `partial`: a valid result, and the tool-call cap was hit
    - `failed`: no valid result after the retry, including `max_turns` reached without
      one; the reason (`terminal_reason` or the validation error) goes in `error`
    - `timeout`: the stage timeout expired

    The confidence rules treat `partial` like `ok`, and the orchestrator adds an open
    question naming the specialist that ran out of searches.
  - **Why:** four different inputs share one pattern; prompt files stay the same for every
    incident, so diffs stay readable (brief rule 7), with no `$` escaping. Tying `partial` to
    the cap alone keeps it meaning "stopped early but answered".

## D3. Intermediate schemas

- **Question:** What does each specialist return, so the join has keys to work with?
- **Status:** decided
- **Decision:** Full shapes in [contracts.md](contracts.md); summary:
  - `LogFinding`: `ts` (tz-aware datetime), `service`, `kind`
    (error / warning / deploy / config / recovery / info), `observation`,
    `attributes: list[{key, value}]`, `evidence: list[str]` as `file:line`. A list of
    pairs rather than a dict, because structured outputs require
    `additionalProperties: false` on every object.
  - `ChangeFinding`: `change_id`, `effective_at`, `services`, `kind`,
    `keys_changed: list[{key, old, new}]`, `reverted_by: str | None`, `evidence`.
  - Both results also carry `hypotheses_checked: list[{theory, verdict, evidence}]` and
    `complete: bool`.
  - `effective_at` = `Shipped`, else `Applied`, else `Merged`. Parsed in the tool layer so
    `search_changes` orders by the same date. Parse only the leading datetime: some values
    carry trailing text (`2026-08-12 19:31 UTC in checkout-service v4.12.0`).
- **Why:** Time, service and config key are the join keys; they have to be typed fields,
  not prose, for the join in D5 to run in code.

## D4. Context handed to each agent

- **Question:** Does the reporter's theory reach the specialists, and who writes the runbook
  symptom description?
- **Status:** decided
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
- **Status:** decided
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
- **Step 10 revision (`join.py`), superseding the step 9 rules and the 0 to 15 minute
  window above.** The step 9 join failed INC-2051 and INC-2062 because both of their
  causes break its assumptions. INC-2051's change shipped 8 hours before a scheduled job hit
  it. INC-2062's change was to a different service that shared a node with the failing one.
  These were architecture problems, not prompt problems (see `docs/iteration-log.md`).
  - **Candidates:** changes that reached production up to **48 hours** before the first
    failure, and are linked to a failing service. Failing services are the ones on `error`
    and `warning` findings, plus any `service=` or `upstream=` on them. The three kinds of
    link, most specific first:
    - `same_service`: the change touches a failing service.
    - `co_located`: the logs place the change's service on the same node or host as a
      failing service. The join can only see this by combining both corpora.
    - `infrastructure`: a platform-wide change (kind `infrastructure`, or services such as
      "all workloads").
  - **Strong:** a changed key's new value appears in the logs, on a `same_service` or
    `co_located` link, however long before the failure. It must appear between the change
    shipping and the first failure, under an attribute key sharing a word with the changed
    key, or under a generic `value=` key when the same line names the field. Being within
    15 minutes is reported, but it's no longer required.
  - **Confidence:**
    - no result from either branch → `low`
    - no candidate → `low`
    - more than one strong change → `low`
    - one strong change and a theory ruled out → `high`
    - one strong change and nothing ruled out → `medium`
    - only weak candidates → `medium` if exactly one has the most specific link, `low` if
      several tie
- **Why:** Confidence rules can be tested and must drop when the change log is removed.
  The synthesis call only writes prose over facts code already joined, so "the orchestrator
  has no tools" still holds.

## D6. Guardrails: code vs prompt

- **Question:** Which traps are enforced in code after the fact?
- **Status:** decided
- **Decision:**
  - Enforce in code: a `LEG-*` entry is never `runbook.matched`; every evidence id appears
    in a tool result from this run; `remediation` steps appear verbatim in the matched
    entry.
  - "Appears in a tool result from this run" is checked against the union of
    `SpecialistRun.evidence_seen` (contracts §4) over the runs that returned a result: each
    reference the tool actually returned, collected by the tool wrapper. A failed or
    timed-out run doesn't count, even if it searched before stopping, because the
    orchestrator never received its findings. With `--empty-tool`, that set is empty for the
    affected corpus, so any citation from it is removed without special cases.
  - "Verbatim" means a substring of the entry's text once runs of whitespace are collapsed
    to single spaces, in both. Entries break lines mid-sentence, and the lookup is told to
    join them (step 8).
  - When a check fails, log it to the trace and move the claim to `open_questions`. Do not
    silently fix it.
  - `matched: null` plus RB-000 actions is decided by the prompt and checked in code.
- **Why:** Code checks are reliable; logging the violation keeps prompt weaknesses visible
  instead of masking them.

## D7. Failure and timeout behaviour

- **Question:** What happens when a parallel branch throws, times out or returns nothing?
- **Status:** decided
- **Decision:**
  - Degrade, never crash: keep whatever finished, cap confidence at `low`, and add an open
    question naming the missing branch.
  - Every stage that calls the model has its own timeout: 90 s for each fan-out specialist
    and the runbook lookup, and 180 s for synthesis. Synthesis reads everything at `high`
    effort, and it hit 90 s on INC-2043 in step 10. Each ends as a `timeout` status that
    the branches below handle, so the run still produces an output.
  - The whole run has a 420 s backstop, above 90 + 90 + 180 s plus overhead (originally
    300 s). It fires only if
    something hangs outside a stage timeout, which is a bug, so it fails the run rather
    than degrading it.
  - API errors use the SDK's built-in retries (2).
  - If the log analyst produced no result, there is no established symptom, so the runbook
    lookup is skipped: `runbook.matched` is `null`, `why` says the lookup did not run and
    why, and a single `orchestrator` action says to follow RB-000 until a symptom is
    established. No steps are invented.
- **Why:** A partial answer with honest confidence is more useful to a duty manager than a
  stack trace, and it's what the iteration prompts test. The stages run in sequence, so a
  single 120 s run budget (the original value) couldn't fit a slow but successful run;
  per-stage timeouts can. The runbook lookup may only see a
  symptom (D4); the historian's output is not one, and the raw report carries the
  reporter's theory.
- **Consider later:** Feeding the lookup the reporter's described symptom instead, if
  skipping it proves too unhelpful.

## D8. Observability

- **Question:** What does each run record?
- **Status:** decided
- **Decision:**
  - `out/runs/<incident>-<timestamp>/` containing `trace.jsonl`, `result.json`,
    `result.md`. `result.md` is rendered by `render.py` from the validated
    `TriageResult`; it is formatting only, and the CLI writes it next to `result.json`.
  - `trace.jsonl` uses the grader's event format exactly (see
    [contracts.md §9](contracts.md#9-trace-events)): `ts` in seconds since run start,
    `type` of `start` / `end` / `tool_call` / `input`, plus our own extra fields (tokens,
    errors), which the grader ignores.
  - `end` is written in a `finally`, so failed and timed-out agents still have a span.
  - The `input` payload is the full text the agent received (system prompt plus user
    message). Prompt files therefore must not contain `CHG-nnnn`, ISO timestamps or
    `file.log:n` examples, or the grader's context-leak check flags them.
  - Print a timing summary to stdout showing that the specialists overlap.
- **Why:** Proves self-check #2 (concurrency) and gives run-to-run output to compare for
  stability. Matching the grader's format means `grade.py run --trace` works unchanged.

## D9. Smaller choices

- **Question:** Prompt layout/templating, CLI entry point, shared search core, tests.
- **Status:** revisited, amended by D2's step 6 design (no placeholders)
- **Decision:**
  - Prompts in `prompts/<agent>.md`, used verbatim as the system prompt. Per-incident
    context goes in the user message (D2). Originally `string.Template` placeholders; no
    Jinja either way.
  - One `corpus.py` with shared term matching; each tool supplies its retrieval unit
    (line / record / entry) and sort order.
  - `pytest` for the tool table in `tool-contracts.md` first; an INC-2043 golden test
    marked `slow`.
- **Why:** Prompt diffs stay readable; the three tools differ only in unit and order; the
  tools are tested before any model is involved.

## D10. Evaluation harness

- **Question:** How does the system plug into `agentic_exercise_grader`?
- **Status:** decided
- **Decision:**
  - **Fault injection** via CLI flags on the normal run, applied inside the tool or agent
    so the system really experiences the fault:
    - `--empty-tool search_logs|search_changes`: the tool returns zero matches for every
      query.
    - `--fail-agent log_analyst|change_historian`: the agent raises on its first call.
    - `--delay-agent log_analyst=200`: the agent sleeps that many seconds before starting,
      to go past the timeout.
    - `--max-tool-calls 3`: overrides the per-specialist cap (D2).
  - **Component runners**, no orchestrator involved:
    - `triage eval-tools <tools.json> -o results.json`: runs each case's tool call and
      writes `{case id: raw tool output}` for `grade.py tools`. No model.
    - `triage eval-runbook <variants.json> -o results.json`: runs the runbook lookup on
      each case's `symptom` alone (`service: null`, `observed: []`) and writes
      `{case id: matched id or null}` for `grade.py runbook`.
  - **Definition of done** for the plan:
    - `grade.py tools` passes before any model work starts.
    - `grade.py run <incident> <result.json> --trace <trace.jsonl>` passes for all three
      incidents.
    - Every non-exploratory variant in `variants.json` passes.
    - Five or more runs per incident give stable pass rates.
  - **Isolation:** nothing in this repo reads the grader repo. Component runners take the
    golden file path as an argument; the answer keys never reach prompts or code.
- **Why:** The variants require the harness to change behaviour, so injection has to be
  designed in rather than bolted on. Keeping goldens out of the system stops prompts being
  tuned to the answer key instead of the data.

## D11. Model access through the Claude Agent SDK

- **Question:** How do agents reach the model without Console API access?
- **Status:** decided
- **Decision:**
  - **Backend.** Use the `claude-agent-sdk` package with the local Claude Code login. No
    API key or token in `.env`. It is used only inside `run_specialist` and the synthesis
    call, behind the same interface, so a later switch to the raw `anthropic` SDK doesn't
    touch the orchestrator, join, guardrails or assembly.
  - **Amends D1:** no `AsyncAnthropic` client. The model is set through the SDK options.
    Keep `effort` in `config.py` and apply it only if the SDK exposes it.
  - **Amends D2:** the SDK drives the turns. The tool-call cap is a counter inside the
    tool function: past the cap it returns `is_error: True` with a message like "Tool-call
    limit reached (8). Do not search again. Submit your answer now from what you have, and
    list what you could not check in `gaps`." It never raises. The run is marked
    `partial` if a valid result then comes back. `max_turns` is a backstop set to twice
    the cap plus 4 (first written as the cap plus 3, raised after the 6a spike showed
    `num_turns` isn't one per tool call). The tool takes only a `query`: `max_results` stays
    at the contract default, so truncation stays visible to the agent. Timeouts use
    `asyncio.wait_for` around each call.
  - **Isolation**, per specialist:
    - `tools=[]`, which removes every built-in Claude Code tool so the model never sees
      them. `allowed_tools` lists only the agent's own MCP tool, to pre-approve it.
    - `setting_sources=[]` passed explicitly. Leaving it `None` loads user, project and
      local settings, including hooks and `CLAUDE.md`.
    - `strict_mcp_config=True`, so only our MCP server is loaded
    - run from an empty temporary working directory
    - use our own `system_prompt`, replacing the Claude Code default
  - **Tools.** Each tool is an in-process MCP tool wrapping the same `tools.py` function,
    with `ToolAnnotations(maxResultSizeChars=...)` set well above the largest result.
    Past that limit Claude Code saves the result to a file and shows only a preview, which
    an agent without `Read` cannot open.
  - **Trace.** `tool_call` events are written by our tool wrapper with the plain name
    (`search_logs`), never taken from the message stream. The stream also carries the
    MCP-prefixed name and the harness's `StructuredOutput` call, either of which would
    fail the grader's tool check.
  - **Zero-tool specs.** The tool is optional in a spec. With none, no MCP server is
    created and `allowed_tools=[]`; everything else (span, timeout, structured output,
    validation, retry, status) is the same. The synthesis call is a zero-tool spec, so it
    goes through `run_specialist` rather than a second function.
  - **Structured output.** `output_format={"type": "json_schema", "schema": ...}` with the
    pydantic model's schema; the parsed object arrives in `ResultMessage.structured_output`.
    Validate it with pydantic anyway and retry once (as D2): the schema does not constrain
    string contents, and in the spike the model wrote `file:line — <log text>` as evidence.
    The prompt states the evidence format exactly, and the D6 validator rejects anything
    else.
  - **Retry through `ClaudeSDKClient`.** Keep the session and send one follow-up: "Your
    answer failed validation: `<errors>`. Resubmit it corrected, without new searches
    unless needed." A fresh query would repeat every search and could change the findings.
    The retry runs inside the same stage timeout and tool-call cap. Verified in the
    step 6a spike: the follow-up returns `structured_output` in the schema, and it
    validated, with no new searches.
  - **The `StructuredOutput` tool is accepted.** The harness adds it to deliver the
    structured answer. It is effectively the `submit_findings` tool D2 rejected, but it is
    an output channel that cannot reach any corpus, so it does not break "exactly one
    data tool".
  - **Isolation test.** A test asks each specialist to read a file under `data/` directly,
    list the directory and run a shell command, and asserts that no call other than its own
    tool is made and no file content comes back.
  - **Verified in the step 5 spike** (`claude-agent-sdk` 0.2.161, Claude Code 2.1.284):
    - the Claude Code login works with no key
    - `claude-opus-5-5` and `effort="medium"` are accepted
    - the tool list at startup is only `StructuredOutput` plus our tool
    - an adversarial prompt produced no file, directory or shell call
    - a canary word in `CLAUDE.md` in the working directory never appeared in any output
    - structured output came back parsed
    - two queries ran concurrently: 9.2 s and 9.9 s alone, 9.9 s together
  - **Verified in the step 6a spike** (`spikes/step6_sdk.py`, written by Claude, same
    versions; INC-2043 with the real `search_logs` and a stub prompt):
    - `ClaudeSDKClient` works with the D11 options. Each `query` + `receive_response`
      yields, in order: `SystemMessage(init)` (repeated on every query in the session),
      `RateLimitEvent`s, `AssistantMessage`s holding `TextBlock`, `ThinkingBlock` or
      `ToolUseBlock`, `UserMessage`s carrying tool results, `SystemMessage(thinking_tokens)`,
      and a final `ResultMessage`.
    - The full `LogAnalystResult` schema, with `$defs`, nullable fields and enums, is
      accepted by `output_format`, and `structured_output` validates with pydantic. No
      inlining is needed.
    - The follow-up retry works (see Structured output above).
    - A successful result has `subtype="success"`, `is_error=False`,
      `terminal_reason="completed"`, and `stop_reason="tool_use"`, because the answer is a
      `StructuredOutput` call. Don't treat `stop_reason` as a failure signal.
    - Running out of turns gives `subtype="error_max_turns"`, `is_error=True`,
      `terminal_reason="max_turns"`, `errors=["Reached maximum number of turns (1)"]` and
      `structured_output=None`. Map this to `failed`.
    - `num_turns` doesn't count one per tool call: with `max_turns=1` it reported 2, and a
      normal run took 6 or 7. Keep `max_turns` a generous backstop.
    - The model makes parallel tool calls: several `ToolUseBlock`s before their results.
      The cap counter has to hold when the handler is called several times in one turn.
    - The prompt's "use at most 4 searches" was ignored (5 searches). The cap must be
      enforced in code, as designed.
    - `total_cost_usd` on a `ResultMessage` is cumulative for the session: the follow-up's
      value includes the first attempt.
    - A run cost about $0.15 as reported (`total_cost_usd`), charged against the
      subscription rather than billed.
  - **Verified by the step 6b tests** (`tests/test_agent_live.py`, real model):
    - an adversarial user message asking for a file read, a directory listing and a shell
      command produces only `search_logs` calls, and the session's tools are only
      `StructuredOutput` and our tool
    - a canary in `CLAUDE.md` in the working directory and one in that directory's
      auto-memory never appear in the output, with `setting_sources=[]`
    - with a cap of 2, the third call is refused, the run is `partial`, and a result still
      comes back
    - a 3 s stage timeout ends the run as `timeout` in both the run and the trace, and no
      Claude Code process is left running afterwards
    - a tool result over 100,000 characters reaches the model inline with
      `maxResultSizeChars=500000`
    - on INC-2043 every cited evidence id is in `evidence_seen`
- **Why:** No Console API access is available. The Agent SDK uses the Claude Code login as
  intended, unlike putting a subscription token in `.env`. Confining it to
  `run_specialist` keeps the parallel fan-out and join in our own code, which is the part
  of the exercise that matters.
- **Costs accepted:**
  - We don't own the loop.
  - The trace's `input` payload records only what we send, not what the harness adds.
  - Each call starts a Claude Code process.
  - Usage counts against the Claude subscription, shared with normal Claude Code use, so
    stability sweeps may hit limits.
  - Anthropic's docs ask products for other people to use API keys. This is a local
    learning exercise; re-check the terms if that changes.
- **Consider later:** switching to the raw `anthropic` SDK (D1 and D2 as originally
  written) once Console access exists. The debrief can then compare the two.
