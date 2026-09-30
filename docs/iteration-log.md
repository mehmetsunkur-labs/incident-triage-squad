# Iteration log

One entry per change made while getting the incidents to pass (plan step 10). Each records
what failed, whether it was a **prompt** or an **architecture** problem, and what changed.
It feeds the write-up (plan step 13). Grades come from `agentic_exercise_grader`; `make
incidents` writes a `grade.txt` next to each run.

Entries 1 to 4 were made during steps 6 to 9, before step 10 formally started, and are
listed here because they are the same kind of change.

---

## 1. Log analyst mislabelled log levels and claimed causes (step 6 review)

- **Seen:** on INC-2043 the analyst labelled an `INFO` line `warning`, and marked "the
  deploy and config change shrank the pool" as `supported` from logs alone.
- **Kind:** prompt.
- **Changed:** `kind` now follows the log level. Verdicts are `supported` or `ruled_out`
  only when the logs show it directly; causes that depend on what a change contained are
  `inconclusive`, with the reason in `gaps`.
- **Result:** 0 of 22 findings mislabelled; the causal claim became `inconclusive`, and the
  analyst's gaps named exactly what the historian had to answer.

## 2. Historian over-attributed a rollback (step 7 review)

- **Seen:** CHG-1043 (a flag) got `reverted_by: CHG-1044`, although the rollback record
  doesn't mention it and the flag was disabled separately.
- **Kind:** prompt.
- **Changed:** `reverted_by` only when a later record names the change or the key it
  restored.
- **Result:** CHG-1043 now has no `reverted_by`; CHG-1042 keeps CHG-1044.

## 3. Timed-out branches' searches could be cited (step 9)

- **Seen:** in a unit test, output cited log lines from an analyst run that had timed out.
  `evidence_seen` included searches made before the timeout, although the orchestrator
  never received the findings. This is what V-2043-analyst-timeout checks.
- **Kind:** architecture.
- **Changed:** the citation guardrail counts `evidence_seen` only from runs that returned a
  result (D6).

## 4. Verbatim remediation and line breaks (step 8)

- **Seen:** runbook entries break lines mid-sentence, and the lookup joins them, so an
  exact substring check would reject correct steps.
- **Kind:** architecture (a guardrail detail).
- **Changed:** the check compares with whitespace collapsed (D6).

## 5. The join assumed every cause looks like INC-2043 (step 10)

- **Seen:** first `make incidents`: INC-2043 passed every hard check. INC-2051 and INC-2062
  each failed three: `root_cause.cites[...]`, `root_cause.both_corpora` and
  `root_cause.confidence`. In both, the specialists had found the right change, but the join
  paired nothing, so confidence was `low` and the root cause had no change evidence.
  - INC-2051: the change touched the failing service but shipped **8 hours** before the
    failure, which only appeared when a scheduled job ran. The 15-minute window rejected it,
    although the failing line carries the exact value the change set.
  - INC-2062: the change was to **another service** that the logs show rescheduled onto the
    same node as the failing one, 12 hours earlier. The same-service rule rejected it.
- **Kind:** architecture. The D5 rules were designed around one incident's shape (same
  service, minutes apart) and did not generalise. No prompt change could have fixed it,
  because the join is code.
- **Changed (D5):**
  - a 48-hour lookback instead of 15 minutes
  - three kinds of link: `same_service`, `co_located` (a shared node or host, visible only
    by joining the corpora) and `infrastructure`
  - a match is strong when the changed value is visible in the logs, at any time within the
    lookback
  - confidence among weak candidates prefers the single most specific link
  - A first version also counted a value seen *after* a rollback. Replaying the saved runs
    caught it, and value matches are now limited to between the change shipping and the
    first failure.
- **Checked offline first:** replaying the join on the specialist results saved from the
  failing runs, with no model calls, gave INC-2043 `high`, INC-2051 `high` and INC-2062
  `medium`.

## 6. Synthesis timed out (step 10)

- **Seen:** on INC-2043, synthesis hit its 90 s stage timeout. The code fallback produced a
  valid result, and the run still passed every hard check.
- **Kind:** architecture (configuration).
- **Changed (D7):** synthesis has its own 180 s timeout; the run backstop is 420 s.

## 7. Co-location depended on how the analyst grouped events (step 10)

- **Seen:** in run 1 of the new join, INC-2062 failed `root_cause.cites[CHG-1048]`. The
  analyst had merged two services' reschedule events into one finding, naming the second
  service only in the observation. The join looked for placements in `service=`
  attributes, so it missed the co-location link and fell back to the infrastructure change.
- **Kind:** both. The join was fragile, and the prompt allowed merged events.
- **Changed:** the join finds a known service name (failing or changed) anywhere in a
  finding that has a `node` or `host`, including pod names and the observation. The analyst
  prompt says not to merge events for different services. A correlation's `service` is now
  the first failure's own service.

## 8. Synthesis put a background condition in the root cause (step 10)

- **Seen:** in run 2, INC-2043 failed `root_cause.not_decoy`. The statement said the pool
  filled up "under evening-peak load", which presents a traffic peak (the archived
  runbook's wrong theory) as part of the cause.
- **Kind:** prompt.
- **Changed:** the synthesis prompt says to name the change and its mechanism, and to keep
  background conditions such as traffic levels or time of day in the summary.

## 9. No RB-000 action when the lookup ran out of searches (step 10)

- **Seen:** in run 2, INC-2062 failed `actions.follow[RB-000]`. The lookup correctly found no
  match, but used all its searches and never looked up the no-match entry, so there was no
  fallback and no RB-000 action.
- **Kind:** both.
- **Changed:** the runbook prompt keeps one search in reserve for the no-match entry. When
  nothing matched and no fallback came back, code adds a single "Follow RB-000" action (the
  runbooks' index says to), without inventing its steps, plus an open question.

## 10. Timeouts: first too tight, then usage throttling (step 10)

- **Seen:**
  - Run 3: INC-2051's log analyst timed out at 90 s, although real analyst runs usually
    take 40 to 60 s. D7 degraded exactly as designed (low confidence, no log citations,
    runbook skipped), and so the golden checks failed.
  - Run 4, after raising the timeout to 150 s: INC-2043's analyst and INC-2062's
    historian both timed out. In both, the trace shows the **first model response took
    about 136 s**; the agent then did all its searches in about 10 s. This was the fourth
    full sweep in about an hour, so it is very likely usage throttling on the Claude
    subscription, not slow work.
- **Kind:** environment (the model access chosen in D11), not prompt or architecture.
- **Changed:**
  - specialist timeout 150 s, synthesis 180 s, run backstop 600 s (D7)
  - the SDK's `RateLimitEvent`s are now written to the trace as `rate_limit` events, so the
    next timeout can be told apart from throttling
  - stopped running sweeps for the day rather than raising timeouts further to hide the
    throttling

## 11. A stall the trace could not explain (step 10)

- **Seen:** Run 5: INC-2043's change historian timed out at 150 s with no tool calls. Its
  only event between `start` and `end` was a `rate_limit` at 21 s (1.4 s on the other two
  incidents), and both INC-2043 specialists got theirs at about 21 s. The other two
  incidents passed. The trace dropped every other SDK message, so it could not say whether
  the CLI was retrying the API, the first response was queued, or the model was thinking,
  and the result message that carries turns and usage never arrives on a timeout.
- **Kind:** observability.
- **Changed:** `sdk_message` and `sdk_system` events (the CLI's `api_retry` among them),
  where it stopped on `end`, the CLI's stderr in `stderr-<agent>.log`, and
  `TRIAGE_SDK_DEBUG=1` for the CLI's debug log with per-request time to first byte (D8,
  contracts §9). No change to timeouts or retries until a stall has been captured.

## 12. An action that warned against the forbidden step (step 10)

- **Seen:** Two `make stability ID=INC-2043 N=5` batches with the entry 11 tracing, same
  code: 9/10 runs passed every hard check. The failure was `actions.no_forbidden`:
  synthesis wrote "Review the autoscaler policy... Adding replicas does not fix pool
  exhaustion." The grader skips a phrase with a negation before it in the same clause, but
  here the negation came after the phrase. 9 of the 10 runs had an action about the 19:52
  scale-out.
- **Kind:** prompt. A warning inside an action list is also poor for a responder skimming
  it, so the rule isn't only for the grader.
- **Changed:** synthesis prompt: actions are things to do; an approach the evidence shows
  won't work goes in `ruled_out`. Not a code filter built from the grader's phrase list.
- **Also seen (no change):** no stall in 10 runs, no `api_retry`, first byte 1.3 to 1.6 s on
  average, 7.5 s at worst. The log analyst's run ends in a 35 to 43 s gap with no thinking
  in it: writing its answer, 5,200 to 6,200 output tokens against the historian's 1,700 to
  2,200. It is the critical path of the parallel stage.
- **Tracing fixed:** per-reply usage keeps the input side only (the output count is a
  start-of-response snapshot), and thinking progress is summarised on `end` instead of a
  line per second.

## 13. The warning rule moved theories, not the autoscaler action (step 10)

- **Seen:** `make stability ID=INC-2043 N=5` with entry 12's prompt: 4/5 passed every hard
  check. Scale-out was now in `ruled_out` in 5/5 runs (2/5 before) and the express checkout
  flag in 3/5 (1/5), but 4/5 runs still had a follow-up about the autoscaler, and one said
  "Consider whether autoscaling should be allowed to scale out a service whose failures
  come from pool exhaustion": a question about the policy, not a warning, so the rule
  didn't cover it. Across 15 INC-2043 runs, `actions.no_forbidden` failed twice.
  With the same prompt, INC-2062 and INC-2051 passed 5/5 each, INC-2051 with no warnings.
- **Kind:** prompt.
- **Changed:** synthesis prompt: an action about what took a harmful step (an automated
  policy, a script, a decision) says what to find out, without naming the step again.
  `make stability` now writes each batch to its own folder, because the grade of the
  earlier batch had been mixed with the next one's runs.
- **Also seen (no change yet):** agents often use all 8 searches: INC-2051's historian 4/5
  and analyst 3/5, INC-2062's runbook lookup 5/5 (a no-match incident, where it keeps
  looking). Results didn't suffer, but runs that hit the cap write longer answers and take
  longer. No stall in 30 traced runs, no `api_retry`, first byte 1.5 s on average and
  7.5 s at worst.

## 14. Synthesis followed a specialist too closely (step 10)

- **Seen:** With entry 13's prompt, INC-2043 passed 10/10 (no action named the step;
  scale-out in `ruled_out` 10/10), and INC-2051 and INC-2062 passed 9/10 each:
  - INC-2051: the log analyst gave the reporter's "the job finished normally" a verdict
    of `supported`, because the job did finish, though as `status=partial` (8 other runs
    said `ruled_out`). The historian had the re-run theory `inconclusive`. Synthesis,
    told that a supported theory belongs in the root cause, left both reporter theories
    out of `ruled_out`. In a run where the analyst didn't check the theory at all,
    synthesis ruled it out from the evidence itself.
  - INC-2062: an action ended "The rollback search was not run", meaning the historian's
    search for `rollback` had hit the cap; the grader reads it as rolling back search.
    6/10 runs turned the historian's unfinished searches into actions ("finish the
    change-log check that was cut short"), though the prompt already sent gaps to
    `open_questions`.
- **Kind:** prompt, in both cases a later stage copying an earlier one's call.
- **Changed:**
  - log analyst prompt: judge a theory as the reporter meant it; one true only in a
    narrow sense but wrong about what matters is `ruled_out`, saying which part holds
  - synthesis prompt: every theory in the report ends up in `ruled_out`, the root cause
    or `open_questions`, decided from the evidence, not only a specialist's verdict
  - synthesis prompt: actions are steps on the responders' systems; an unfinished search
    or anything else a specialist couldn't check goes in `open_questions`
- **Also seen:** the historian finished `partial` in 7/10 INC-2051 and 5/10 INC-2062 runs;
  its unfinished searches are what the INC-2062 action came from, so the cap (entry 13) is
  now a correctness question as well as a speed one. INC-2043's LEG-014 near miss was
  missed once, when the runbook lookup's three searches never returned the archived entry.
  No stall in 60 traced runs.

## 15. A failed agent had a span of no length (step 11)

- **Seen:** `make variants` on the step 10 code: 5 of 6 variants pass every hard check.
  V-2043-historian-fails passes all its own checks (output produced, confidence `low`, no
  `CHG-` citations, open questions) but fails the grader's `trace.concurrent`, overlap
  0.00 s: the historian's span started and ended at 0.008 s, so it could not overlap the
  analyst's. The injected fault raised at the start of the span, before the agent's CLI
  session even opened, while D10 says the agent raises *on its first call*.
- **Kind:** fault injection (D10), not the orchestrator: the fan-out was concurrent and
  degraded as designed.
- **Changed:** `--fail-agent` now raises once the agent's session is open and before its
  first model call (`faults.before_first_call`); `--delay-agent` still sleeps at the start.
  A real agent failure also happens after its CLI has started. With the real CLI the
  failed historian's span is now about 1.7 s, with no model call made. The grader's
  check prompted the look, and D10's wording decided the fix.

## Step 10 status

Five full sweeps on the step 10 branch, as `make incidents` summaries:

| Run | Changes in effect | INC-2043 | INC-2051 | INC-2062 |
|---|---|---|---|---|
| 1 | new join (5, 6) | PASS | **PASS** | FAIL: entry 7 |
| 2 | + 7 | FAIL: entry 8 | PASS | FAIL: entry 9 |
| 3 | + 8, 9 | PASS | FAIL: timeout (entry 10) | **PASS** |
| 4 | + 150 s timeouts | FAIL: throttled | PASS | FAIL: throttled |
| 5 | same | FAIL: historian stalled (entry 11) | PASS | PASS |

Every incident has passed every hard check at least once, and the join decided correctly
in every run that got both specialists' results. There hasn't yet been a single run in
which all three pass together; the failures after run 1 were prompt variance (each fixed
once seen) and timeouts.

Stability, as `make stability ID=<id> N=5` batches, runs passing every hard check:

| Batch | Changes in effect | INC-2043 | INC-2051 | INC-2062 |
|---|---|---|---|---|
| A | + entry 11 tracing | 4/5 (entry 12) | | |
| B | same | 5/5 | | |
| C | + entry 12 prompt | 4/5 (entry 13) | 5/5 | 5/5 |
| D, E | + entry 13 prompt, N=10 | 10/10 | 9/10 (entry 14) | 9/10 (entry 14) |
| F | + entry 14 prompts, N=10 | **10/10** | **10/10** | **10/10** |

Batch F was also graded run by run with each run's trace (`grade.py run <id> result.json
--trace trace.jsonl`, since `make stability` grades without traces): 30/30 pass every
hard check, the trace checks included. That meets step 10's done condition.

What batch F still shows, none of it a hard failure:
- Soft checks: INC-2043 names LEG-014 as a near miss in 8/10 and rules out the express
  checkout flag in 7/10 (2/10 in batch D); INC-2051 names RB-008 in 8/10. The near misses
  are missed when the runbook lookup's searches never return the entry.
- The search cap is still hit often: INC-2043's analyst 8/10, INC-2051's historian 9/10,
  INC-2062's runbook lookup 10/10. What a capped specialist couldn't check now goes to
  `open_questions`.
- Runs take about 123 s (median), 171 s at worst, when every agent wrote long answers.
  No timeouts, failures or `api_retry` in 30 runs.
