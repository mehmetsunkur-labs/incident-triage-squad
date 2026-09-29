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
