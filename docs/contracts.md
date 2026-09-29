# Contracts

The JSON shapes that cross each boundary in the system. The exercise fixes two sets already
and they are not repeated here: the tool contracts (`search_logs`, `search_changes`,
`search_runbook`, `post_update`) and the final output (`output-contract.md`), both in the
exercise repo's `docs/`. This file covers everything in between.

**Source of truth.** These docs describe the design. The enforced version is the pydantic
models, whose `model_json_schema()` is what structured outputs receive. When they
disagree, fix one of them; don't let both drift.

**Examples are fictional.** They use made-up services and ids so they illustrate shape
without hinting at any incident's answer.

---

## Conventions

These apply to every schema below.

- **Timestamps** are ISO 8601 UTC strings with a `Z` suffix, e.g. `2026-01-01T10:00:00Z`
  (JSON Schema `format: date-time`, pydantic `AwareDatetime`).
- **Evidence** is a list of strings, each one of:
  - `file:line` for a log line, e.g. `orders-api.log:12`, 1 based as `search_logs` returns it
  - `CHG-nnnn` for a change record
  - `RB-nnn` or `LEG-nnn` for a runbook entry

  Validate the format client-side with a pydantic validator, and check in code that every
  id appeared in a tool result from this run (D6).
- **No free-form objects.** Structured outputs require `additionalProperties: false` on
  every object, so key/value data is a list of `{key, value}` pairs, never a dict.
- **Every field is required.** Use `null` or `[]` for "nothing", never omit a field. It
  keeps strict schemas simple and makes "nothing found" explicit.
- **Constraints that structured outputs ignore** (string lengths, numeric ranges) are
  enforced by pydantic after parsing, not relied on in the schema.

Shared enums:

| Enum | Values |
|---|---|
| `Verdict` | `supported`, `ruled_out`, `inconclusive` |
| `Strength` | `strong`, `weak` |
| `Confidence` | `high`, `medium`, `low` |
| `RunStatus` | `ok`, `partial`, `failed`, `timeout` |

---

## 1. Specialist input

Orchestrator to log analyst and change historian. Both get the same object, rendered into
their prompt template (`$incident_id`, `$incident_report`, `$hypotheses_note`). They never
receive each other's output.

```json
{
  "incident_id": "INC-9001",
  "incident_report": "# INC-9001 - Orders page slow\n\n...verbatim markdown...",
  "hypotheses_note": "The report contains theories from the reporter. Treat them as unverified hypotheses: test each one against your corpus and report a verdict with evidence."
}
```

| Field | Type | Notes |
|---|---|---|
| `incident_id` | string | |
| `incident_report` | string | Verbatim file contents. Not summarised (D4). |
| `hypotheses_note` | string | Fixed text from the orchestrator, the same for every incident. |

---

## 2. `LogAnalystResult`

The log analyst's structured output: what the model returns on its final turn.

```json
{
  "findings": [
    {
      "ts": "2026-01-01T10:04:10Z",
      "service": "orders-api",
      "kind": "error",
      "observation": "Requests start failing with upstream timeouts.",
      "attributes": [
        {"key": "timeout_ms", "value": "3000"},
        {"key": "status", "value": "504"}
      ],
      "evidence": ["orders-api.log:12", "orders-api.log:13"]
    }
  ],
  "first_failure_at": "2026-01-01T10:04:10Z",
  "failure_mode": "Upstream timeouts on /orders, rising in frequency.",
  "hypotheses_checked": [
    {
      "theory": "The CDN is down.",
      "verdict": "ruled_out",
      "why": "Edge requests keep succeeding throughout the window.",
      "evidence": ["edge.log:40"]
    }
  ],
  "gaps": ["No logs from the inventory service before 10:00."]
}
```

| Field | Type | Notes |
|---|---|---|
| `findings[]` | array of `LogFinding` | Chronological. |
| `findings[].ts` | date-time | Taken from the log line, not estimated. |
| `findings[].service` | string | The service named in the line. |
| `findings[].kind` | enum | `error`, `warning`, `deploy`, `config`, `recovery`, `info` |
| `findings[].observation` | string | Plain words: one event per finding. |
| `findings[].attributes[]` | `{key, value}` | Values as strings. This is what the join matches config keys against (D5). |
| `findings[].evidence` | evidence list | At least one entry. |
| `first_failure_at` | date-time or null | The earliest user-facing failure. `null` if none was found. |
| `failure_mode` | string or null | A one-line description of how it fails. |
| `hypotheses_checked[]` | `HypothesisCheck` | One per theory in the report, plus any the analyst raised itself. |
| `gaps` | array of string | What the analyst looked for and could not establish. |

`HypothesisCheck` is shared with the historian: `theory` (string), `verdict` (`Verdict`),
`why` (string), `evidence` (evidence list, which may be empty only when `inconclusive`).

---

## 3. `ChangeHistorianResult`

The change historian's structured output.

```json
{
  "changes": [
    {
      "change_id": "CHG-9042",
      "title": "Lower upstream timeout on orders-api",
      "effective_at": "2026-01-01T10:02:00Z",
      "services": ["orders-api"],
      "kind": "config",
      "keys_changed": [
        {"key": "upstream.timeout_ms", "old": "10000", "new": "3000"}
      ],
      "reverted_by": null,
      "relevance": "Touches the upstream client on the affected service, shortly before the reported window.",
      "evidence": ["CHG-9042"]
    }
  ],
  "hypotheses_checked": [
    {
      "theory": "The CDN is down.",
      "verdict": "inconclusive",
      "why": "No CDN or partner change was recorded in the window.",
      "evidence": []
    }
  ],
  "gaps": []
}
```

| Field | Type | Notes |
|---|---|---|
| `changes[]` | array of `ChangeFinding` | Newest first. Only changes the historian judges plausibly relevant, not every search hit. |
| `changes[].change_id` | string | `CHG-nnnn` |
| `changes[].title` | string | From the record heading. |
| `changes[].effective_at` | date-time | When it reached production: `Shipped`, else `Applied`, else `Merged` (D3). |
| `changes[].services` | array of string | As listed in the record. Empty for changes with no service. |
| `changes[].kind` | enum | `release`, `config`, `flag`, `infrastructure`, `external`, `documentation`, `other` |
| `changes[].keys_changed[]` | `{key, old, new}` | `old` and `new` are strings or null. Empty for a release with no stated config change. |
| `changes[].reverted_by` | string or null | `CHG-nnnn` of a rollback, if one exists. |
| `changes[].relevance` | string | Why the historian kept it. |
| `changes[].evidence` | evidence list | At least the record's own id. |
| `hypotheses_checked[]` | `HypothesisCheck` | As in §2. |
| `gaps` | array of string | |

---

## 4. `SpecialistRun` (code-side wrapper)

The loop (D2) wraps whatever the model returned. The model never produces these fields,
so they are not part of any structured-output schema.

```json
{
  "agent": "log_analyst",
  "status": "ok",
  "error": null,
  "tool_calls": 5,
  "queries": ["timeout", "orders-api error", "deploy orders-api"],
  "started_at": "2026-01-01T12:00:00.120Z",
  "finished_at": "2026-01-01T12:00:21.870Z",
  "result": { "...": "LogAnalystResult | ChangeHistorianResult | RunbookResult, or null" }
}
```

| Field | Type | Notes |
|---|---|---|
| `agent` | enum | `log_analyst`, `change_historian`, `runbook_lookup` |
| `status` | `RunStatus` | `partial` = the tool-call cap was hit but a result came back. `failed` or `timeout` means `result` is `null`. |
| `error` | string or null | Exception text for `failed` and `timeout`. |
| `tool_calls` | integer | |
| `queries` | array of string | Every query sent to the tool, in order. It feeds the runbook `why` when nothing matched, and the debrief. |
| `started_at`, `finished_at` | date-time | Wall-clock times for `result.md`. The grader's concurrency check reads the trace (§9), not these. |
| `result` | object or null | |

Faults injected with the D10 flags show up here like real ones: `--fail-agent` gives
`failed`, `--delay-agent` past the timeout gives `timeout`, and `--empty-tool` gives `ok`
with empty findings.

---

## 5. Runbook input

Built by a code template from the joined findings (D4). It must not contain log lines,
change records or evidence ids: the runbook lookup sees the symptom only.

```json
{
  "symptom": "Requests to the orders endpoint fail with upstream timeouts after roughly 3 seconds, rising in frequency.",
  "service": "orders-api",
  "observed": [
    {"key": "timeout_ms", "value": "3000"},
    {"key": "status", "value": "504"}
  ]
}
```

| Field | Type | Notes |
|---|---|---|
| `symptom` | string | From `failure_mode` plus the key observations. Describes the symptom, not the cause. |
| `service` | string or null | |
| `observed[]` | `{key, value}` | Attribute values that help pick between entries. |

**When there is no symptom.** If the log analyst's run is `failed` or `timeout`, or it
returned no `failure_mode`, no runbook input is built and the lookup is skipped (D7). The
runbook lookup never gets the historian's output or the raw report as a substitute.

**Component runs.** `triage eval-runbook` (D10) builds this object from a bare symptom
string: `{"symptom": "<text>", "service": null, "observed": []}`. The lookup must work from
`symptom` alone.

Decide deliberately whether the suspected cause belongs in `symptom`. The brief says the
lookup gets a symptom description; including the cause makes matching easier but also
lets a wrong cause steer the runbook choice.

---

## 6. `RunbookResult`

The runbook lookup's structured output.

```json
{
  "matched": {
    "id": "RB-907",
    "title": "Upstream timeouts in a service",
    "source": "platform.md"
  },
  "why": "Matches the symptom and the observed timeout; LEG-903 keyword-matches too but is archived.",
  "remediation": [
    "Step one, verbatim from the entry.",
    "Step two, verbatim from the entry."
  ],
  "considered": [
    {"id": "RB-907", "source": "platform.md", "archived": false, "verdict": "match", "reason": "Symptom and signals match."},
    {"id": "LEG-903", "source": "_archive/old-runbook.md", "archived": true, "verdict": "near_miss", "reason": "Same keywords, superseded advice."},
    {"id": "RB-904", "source": "database.md", "archived": false, "verdict": "rejected", "reason": "Needs database-side errors, which are not observed."}
  ],
  "fallback": null
}
```

When nothing current fits, `matched` is `null`, `remediation` is `[]`, and `fallback` names
the entry for that case:

```json
{
  "matched": null,
  "why": "Searched for timeouts, latency and upstream errors. The only close entries do not match the observed signals.",
  "remediation": [],
  "considered": [
    {"id": "RB-904", "source": "database.md", "archived": false, "verdict": "near_miss", "reason": "Similar wording, different failure mode."}
  ],
  "fallback": {"id": "RB-900", "source": "platform.md", "steps": ["Steps from the no-match entry, verbatim."]}
}
```

| Field | Type | Notes |
|---|---|---|
| `matched` | `{id, title, source}` or null | Code rejects an archived `source` (`_archive/…`) or a `LEG-` id here (D6). |
| `why` | string | Why this entry and not the near misses. When `matched` is null: what was searched and what came close. |
| `remediation` | array of string | Verbatim steps from the matched entry. Code checks each one is a substring of that entry's text (D6). |
| `considered[]` | `CandidateEntry` | Every entry the agent read, including rejected ones. |
| `considered[].archived` | boolean | Worked out by the agent from `source`, not given by the tool. |
| `considered[].verdict` | enum | `match`, `near_miss`, `rejected` |
| `fallback` | `{id, source, steps}` or null | Set only when `matched` is null. |

If an archived entry is the closest match, the orchestrator copies that fact into
`open_questions` as a gap in the current runbooks (output contract rule).

---

## 7. Synthesis call

The orchestrator's one model call (D5). It gets no tools and writes prose over facts that
code has already joined. It decides nothing that code already decided.

### Input

```json
{
  "incident_id": "INC-9001",
  "incident_report": "...verbatim...",
  "analyst": { "...": "SpecialistRun with LogAnalystResult" },
  "historian": { "...": "SpecialistRun with ChangeHistorianResult" },
  "correlations": [
    {
      "log_evidence": ["orders-api.log:12"],
      "change_id": "CHG-9042",
      "service": "orders-api",
      "minutes_before_first_failure": 2.2,
      "matched_keys": ["timeout_ms ~ upstream.timeout_ms"],
      "strength": "strong"
    }
  ],
  "confidence": "high",
  "confidence_reason": "Strong match on service, time and config key; no competing change.",
  "runbook": { "...": "SpecialistRun with RunbookResult" }
}
```

| Field | Type | Notes |
|---|---|---|
| `correlations[]` | `Correlation` | Produced by the join code. Empty means code found nothing to pair. |
| `correlations[].minutes_before_first_failure` | number | Negative if the change came after the failure. |
| `correlations[].matched_keys` | array of string | The log attribute and change key that lined up, when there is one. |
| `correlations[].strength` | `Strength` | |
| `confidence`, `confidence_reason` | `Confidence`, string | Set by code rules (D5). The model does not change them. |

### Output (`SynthesisResult`)

```json
{
  "summary": "Two to four sentences a duty manager could read aloud.",
  "root_cause_statement": "One sentence.",
  "root_cause_evidence": ["orders-api.log:12", "CHG-9042"],
  "correlation": "What lined up with what, and on which keys.",
  "ruled_out": [
    {"theory": "The CDN is down.", "why": "Edge requests succeed throughout.", "evidence": ["edge.log:40"]}
  ],
  "actions": [
    {"action": "Revert CHG-9042.", "urgency": "now", "source": "orchestrator"}
  ],
  "open_questions": []
}
```

| Field | Type | Notes |
|---|---|---|
| `root_cause_evidence` | evidence list | Must contain at least one log id and one change id when `correlations` is non-empty. Code checks this. |
| `ruled_out[]` | `{theory, why, evidence}` | Every theory from the report must appear here or be supported by the root cause. |
| `actions[].urgency` | enum | `now`, `today`, `follow-up` |
| `actions[].source` | enum | `analyst`, `historian`, `orchestrator`, or a runbook id. Runbook steps are copied by code into `runbook.remediation`, not rewritten here. |

---

## 8. Assembling `TriageResult`

The final output follows `output-contract.md` exactly. Code assembles it; the model
writes none of it directly.

| Output field | Comes from |
|---|---|
| `incident_id` | input |
| `summary` | `SynthesisResult.summary` |
| `timeline` | code: analyst `findings` and historian `changes` merged by time, with `observation` / `title` as the event |
| `root_cause.statement`, `.evidence`, `.correlation` | `SynthesisResult` |
| `root_cause.confidence` | code (D5, capped by D7) |
| `ruled_out` | `SynthesisResult.ruled_out` |
| `runbook.matched`, `.title`, `.source`, `.why`, `.remediation` | `RunbookResult`, flattened (`matched` becomes the id, or null) |
| `actions` | the runbook fallback steps when nothing matched, then `SynthesisResult.actions` |
| `stakeholder_update` | the comms drafter, or `null` |
| `open_questions` | `SynthesisResult.open_questions`, plus D6 guardrail failures, missing branches (D7) and archived closest matches |

The finished object is validated against a `TriageResult` pydantic model written from
`output-contract.md` (self-check #8).

---

## 9. Trace events

`trace.jsonl` in each run folder (D8). The format is the one `agentic_exercise_grader`
reads, so `grade.py run ... --trace trace.jsonl` works unchanged. One JSON object per line:

```json
{"ts": 0.00, "agent": "log_analyst", "type": "start"}
{"ts": 0.01, "agent": "log_analyst", "type": "input", "payload": "<full prompt text>"}
{"ts": 0.80, "agent": "log_analyst", "type": "tool_call", "tool": "search_logs", "query": "timeout"}
{"ts": 4.20, "agent": "log_analyst", "type": "end", "status": "ok", "input_tokens": 5120, "output_tokens": 830}
```

| Field | Type | Notes |
|---|---|---|
| `ts` | number | Seconds since the run started (`time.monotonic()` offset). Not an ISO string: the grader does arithmetic on it. |
| `agent` | enum | `log_analyst`, `change_historian`, `runbook_lookup`, `comms_drafter`, `synthesis` |
| `type` | enum | `start`, `end`, `tool_call`, `input`, and any of our own (e.g. `model_call`, `error`) |
| `tool` | string | On `tool_call` only. Must be the agent's own tool, by its plain name (`search_logs`), not the MCP-prefixed name the Agent SDK uses (D11). |
| `payload` | string | On `input` only: the system prompt plus user message we send. Under the Agent SDK (D11) the harness may add more, which we cannot log. |

The grader's checks, and what they need from us:

- **Concurrency:** `log_analyst` and `change_historian` each have a `start` and an `end`,
  and their spans overlap. `end` is written in a `finally`, including on failure and
  timeout.
- **Tool sandboxing:** every `tool_call` names the agent's own tool. `synthesis` makes no
  tool calls.
- **Context sandboxing,** checked against `payload`:
  - `log_analyst` must not contain `CHG-nnnn`.
  - `change_historian` must not contain an ISO timestamp (`2026-01-01T10:00:00Z`) or
    `file.log:n`.
  - `runbook_lookup` must contain none of these.

  Prompt files must avoid these patterns in their examples too, because the whole prompt
  is logged.

Extra fields (`query`, `status`, token counts) are ours; the grader ignores them.
