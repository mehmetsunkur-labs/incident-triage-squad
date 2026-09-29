# Benchmark: INC-2043

Written by hand before building the agents (plan step 1). Do the three passes in order and
don't look ahead: pass 1 uses only the incident report and `search_logs`, pass 2 only the
report and `search_changes`. Don't edit this afterwards to match what the system says.

- Date:
- Goldens seen beforehand: yes / no
- Pass 2 was done after pass 1, so some queries and wording were influenced by the logs.
  Those are marked "from pass 1" in the query table and in the text.

## Pass 1: log analyst view (logs only)

- Timeline:
  - `19:25:47` Checkout request succeeds with `pool_max=40` (`checkout-service.log:2`)
  - `19:29:51` Deploy of checkout-service 4.12.0 starts, commit `9f2c1ab`, by `ci-bot`
    (`platform-events.log:9`)
  - `19:31:12` Deploy succeeds (`platform-events.log:10`) and, in the same second, config is
    applied to checkout-service with keys `db.pool.max`, `feature.express_checkout`,
    `log.level`, but no values (`platform-events.log:11`). This is 1m14s before the first
    warning and 3m02s before the first customer failure.
  - `19:31:14` Database pool now has `max=4`, down from 40 (`checkout-service.log:5`)
  - `19:31:49` Last known-good: checkout request succeeds, `pool_in_use=4 pool_max=4`
    (`checkout-service.log:6`)
  - `19:32:26` First sign of trouble: slow connection acquisition, queue building
    (`checkout-service.log:7`)
  - `19:33:41` Likely earliest impact: first pool timeout tied to a real order, `ord_88213`,
    not logged as a failed request (`checkout-service.log:9`; a second at 19:33:58,
    `checkout-service.log:10`)
  - `19:34:12` First failed checkout request in any log: `status=500
    error=PoolAcquireTimeout` (`checkout-service.log:11`)
  - `19:34:14` First confirmed customer-facing failure: the gateway returns a 500 on
    `POST /v2/checkout` (`api-gateway.log:4`). Probably the same request as
    `checkout-service.log:11`, matched by timing only: the checkout line has no trace id.
  - Escalation, observed:
    - checkout-service pool queue grows 7 → 23 → 41 → 57 → 96 → 113, saturated for 270 s at
      19:38:15 (`checkout-service.log:7`, `:8`, `:9`, `:10`, `:12`, `:13`)
    - api-gateway: 500 at 19:34:14, error rate 18.4% at 19:35:02, breaker opens after 20
      consecutive failures at 19:37:44, 502 `upstream_circuit_open` from 19:37:45, 61.7% at
      19:42:11, still 502 at 19:55:30 (`api-gateway.log:4`, `:5`, `:6`, `:7`, `:8`, `:10`)
    - `19:44:37` checkout health check `db:fail`, cache and payments ok
      (`checkout-service.log:15`)
    - `19:52:30` autoscaler scales checkout-service 6 → 10 replicas on CPU
      (`platform-events.log:12`); replica 7 starts with `max=4` and is saturated within 37 s
      with 88 waiters (`checkout-service.log:16`, `:17`, `:18`); still 500s at 19:58:04
      (`checkout-service.log:19`)
  - Escalation, inferred:
    - Waiting requests time out after the 5000 ms acquire timeout and fail with a 500 (the
      timeout from `checkout-service.log:5`, failures of about 5 s on `:11`, `:14`, `:19`).
    - `db:fail` reflects the pool, not the database (see "healthy throughout").
    - The breaker opening is where impact went from some customers to most: from then the
      gateway rejects checkout requests itself, including ones that might have succeeded.
    - Scaling out didn't help because every new replica got the same 4-connection pool.
  - Healthy throughout:
    - Postgres: 5 or 6 active connections of 200 during 19:31 to 19:40, 11 at 19:55
      (`postgres.log:3`, `:4`, `:6`, `:8`); no long-running transactions (`postgres.log:7`);
      slowest query 118 ms (`postgres.log:5`)
    - payments-worker: charge queue drops 12 → 3 with `fewer_charges_enqueued`
      (`payments-worker.log:5`, `:6`), inferred to be fewer checkouts reaching it; Stripe
      healthy at 19:48:02 (`payments-worker.log:7`); gateway sees payments-worker at 0.0%
      errors (`api-gateway.log:9`)
  - Recovery:
    - `20:05:18` Rollback 4.12.0 → 4.11.3 starts, by `a.nwosu` rather than `ci-bot`
      (`platform-events.log:13`); succeeds at 20:06:44 (`platform-events.log:14`)
    - `20:06:46` 4.11.3 starts with the pool back at `min=4 max=40`
      (`checkout-service.log:20`, `:21`)
    - `20:07:31` First successful checkout, `pool_in_use=17 pool_max=40`
      (`checkout-service.log:22`)
    - `20:07:33` Circuit breaker closes (`api-gateway.log:11`): customer impact ends
    - `20:09:12` Gateway sees checkout at 0.4% errors, near the 0.1% before the incident
      (`api-gateway.log:12`, `api-gateway.log:3`)
    - `20:12:09` Health check fully healthy, `db:ok` (`checkout-service.log:23`): fully
      normal
- Failure mode: checkout requests wait for one of only 4 database connections, give up after
  the 5000 ms acquire timeout and return `500 PoolAcquireTimeout`, so failures take about
  5 s (`checkout-service.log:11`, `:14`, `:19`). From 19:37:45 the gateway's breaker is open
  and rejects most checkout requests immediately with `502 upstream_circuit_open`
  (`api-gateway.log:7`, `:10`), though some still reach checkout and time out there
  (`checkout-service.log:14`, `:19`). Against the report:
  - "An error page": fits: a 500 or a 502 on `POST /v2/checkout`. The page itself isn't
    logged.
  - "Worse over time": fits: the queue grows 7 → 113, the gateway error rate goes
    18.4% → 61.7%, and the breaker opens at 19:37:44.
  - "Worked on the second or third try": partly fits. The pool still serves some requests,
    so a retry can get a connection. No log line shows a retry succeeding, and how requests
    got through while the breaker was open isn't logged.
- Could not tell from the logs alone:
  1. What values the config set. `platform-events.log:11` names keys only. The pool's
     values (40 before, 4 after) are observed; that the config apply set them is inferred.
  2. Whether `max=4` was intended, or a typo, test value or wrong environment.
  3. Who applied the config and where it came from. The deploy has `actor=ci-bot
     commit=9f2c1ab`; the config line has no actor, so whether it was part of that commit
     or a separate change is unknown.
  4. What changed in `feature.express_checkout` and `log.level`, and whether either
     mattered.
  5. Whether the code or the config caused it. The rollback reverted the version and the
     pool setting together, so the logs can't separate them. The pool is the best
     explanation, but it is inference.
  6. How the rollout went: no canary or staged rollout is visible, and it isn't shown
     whether all 6 replicas ran 4.12.0. Only replica 7 is named; the count of 6 comes from
     `platform-events.log:12`.

## Pass 2: change historian view (change log only)

Facts from each record found (copied, not judged). Timing reference from the report only:
support contacts from about 19:35 UTC, incident raised 19:38 UTC on 2026-08-12.

| Record | Reached prod | Service | What changed | Risk / review | Later reverted |
|---|---|---|---|---|---|
| `CHG-1042` | 2026-08-12 19:31, in checkout-service v4.12.0 (merged 11:03) | checkout-service | `db.pool.max` 40 → 4. Meant for staging, but edited in the shared default block, so it applied to production. | Low; one reviewer; no load test | Yes: CHG-1044 restored 40 |
| `CHG-1043` | 2026-08-12 19:31, in checkout-service v4.12.0 | checkout-service | `feature.express_checkout` on for 25% of sessions | Medium | Flag off 20:15; back on next morning, no recurrence |
| `CHG-1044` | 2026-08-12 20:06 | checkout-service | Rollback 4.12.0 → 4.11.3, restoring `db.pool.max` 40 | Not assessed (incident action) | n/a (the rollback itself) |
| `CHG-1038` | 2026-08-11 14:22 | payments-worker | Provider retry backoff: fixed 500 ms → exponential with jitter. Notes: "No change to the checkout path." | Medium | Not recorded |
| `CHG-1050` | 2026-08-18 10:14 | api-gateway | Dependency updates, upstream timeout logging fix | Low | Not recorded |
| `CHG-1047` | 2026-08-17 22:40 to 23:05 | all workloads, eu-west-1 nodes | Rolling kernel patch; pods rescheduled | Not stated | Not recorded |
| `CHG-1026` | 2026-07-30 14:00 | none | Runbook review; 3 missing entries flagged | Not stated | n/a |

- Candidate changes (plausibly relevant, and why):
  - `CHG-1042`: strong. Checkout-service, shipped 19:31, minutes before the reported
    symptoms. Cuts the database pool from 40 to 4, a value meant for staging, with no load
    test. (That 4 connections starved the service is what pass 1's logs showed; the change
    log only shows the cut.)
  - `CHG-1043`: weak. Checkout-service, shipped in the same release at 19:31. Re-enabled
    the next morning with no recurrence.
- Checked and not relevant (and why):
  - `CHG-1044`: not a cause. It is the rollback at 20:06, and evidence about the cause: the
    fix came with undoing CHG-1042.
  - `CHG-1038`: a different service (payments-worker), shipped the day before, and its
    notes say "No change to the checkout path".
  - `CHG-1050`, `CHG-1047`: shipped after the incident (17 and 18 Aug).
  - `CHG-1026`: documentation only.
- The reporter's theory (payment provider), per the change log: no payment provider change
  is recorded on the evening of 12 Aug. That fails to support the theory rather than ruling
  it out, because a provider outage wouldn't appear as a change. (The log evidence against
  it goes in pass 3.)
- Could not tell from the change log alone:
  1. Whether any change actually broke something: the records show only that they shipped.
  2. Which of CHG-1042 and CHG-1043 mattered. They shipped in the same release at the same
     minute; separating them needs the logs.
  3. Whether the timing lines up with the symptoms. The report gives only "around 19:35";
     the logs are needed to check onset against the 19:31 ship time.

## Pass 3: join

- Root cause: CHG-1042, meant for staging, changed the shared default config, so
  checkout-service v4.12.0 shipped to production at 19:31 with `db.pool.max` cut from 40 to
  4; under evening peak load, checkout requests timed out waiting for a database
  connection, the API gateway's circuit breaker opened, and checkouts failed until the
  rollback at 20:06 restored 40. (`CHG-1042`, `CHG-1044`, `platform-events.log:11`,
  `checkout-service.log:5`, `:11`, `:21`)
- Correlation: `CHG-1042` lines up with the logs on all four keys:
  - time: shipped 19:31, deploy and config applied 19:31:12 (`platform-events.log:10`,
    `:11`)
  - service: checkout-service in both
  - config key: `db.pool.max` is named in the applied config (`platform-events.log:11`)
  - value: the new 4 matches the pool at startup (`checkout-service.log:5`), and the 40
    that `CHG-1044` restored matches the pool after rollback (`checkout-service.log:21`)
- Start time: first sign 19:32:26 (`checkout-service.log:7`); likely first impact 19:33:41
  (`checkout-service.log:9`); first confirmed customer failure 19:34:14
  (`api-gateway.log:4`).
- Recovery: customer impact ends 20:07:33 when the breaker closes (`api-gateway.log:11`);
  fully normal 20:12:09 (`checkout-service.log:23`).
- Anything else that lines up as well:
  - No other change lines up on both time and service.
  - `CHG-1043` (express checkout) shipped in the same release, so it matches on time and
    service. It is less likely but not ruled out:
    - nothing in the logs points at it beyond the config line naming the key
    - it was re-enabled the next morning with no recurrence
    - recovery at 20:07 came before the flag was turned off at 20:15, though the rollback
      reverted the whole release, so that alone doesn't separate the two
- Confidence: high. Evidence from two independent sources lines up on time, service, config
  key and value, before and after the rollback, and no competing change matches.

## Reporter's theory

- Theory: "the payment provider having problems again, like earlier this evening"
- Verdict: ruled out for this incident
- Evidence:
  - The failure happens inside checkout, before payment: `PoolAcquireTimeout`
    (`checkout-service.log:11`)
  - Fewer charges reach the payments worker, consistent with failures before payment
    (`payments-worker.log:6`)
  - Stripe healthy at 19:48:02 (`payments-worker.log:7`), and the gateway sees
    payments-worker at 0.0% errors (`api-gateway.log:9`)
  - The earlier provider trouble, 504s and timeouts from 18:02, was a separate event that
    had recovered by 18:07:33 (`payments-worker.log:1` to `:4`)
  - No payment provider change is recorded that evening (pass 2)

## Runbook

- Symptom searched (symptom only, no cause): checkout requests time out waiting for a
  database connection; the pool is saturated with rising waiters; the database is healthy
  and lightly loaded. Queries: `connection pool timeout`, `connection pool`, `timeout`.
- Match: `RB-004` from `database.md`, a current file, because:
  - its Symptom matches: one service timing out on the pool while the database is healthy
  - its Detection matches the cited lines: `timeout acquiring connection`,
    `connection acquisition slow` and `pool saturated` with a rising queue
    (`checkout-service.log:7`, `:9`, `:13`), while postgres shows low active connections
    and no long-running transactions (`postgres.log:3`, `:7`)
- Remediation steps that apply:
  - Step 1, check the change log for a pool config change: `CHG-1042`.
  - Step 2, roll back per `RB-010`: done as `CHG-1044`. RB-010 fits what happened: onset
    correlates with `deploy succeeded`, the previous config was restored, the error rate
    returned to baseline, 4.12.0 is blocked, and the rollback is recorded in the change
    log.
  - Step 4, don't scale out: this was broken by the autoscaler (see LEG-014 below).
  - Step 3, look for a leak: doesn't apply, because the pool size did change.
- Escalation: not needed. The rollback finished at 20:06:44 (`platform-events.log:14`) and
  customer impact ended at 20:07:33 (`api-gateway.log:11`), well within 15 minutes.
- Near misses, and why they're wrong:
  - `LEG-014` (`_archive/checkout-runbook-2024.md`): archived and superseded by RB-004, and
    its advice to scale out is wrong here. The incident did exactly that: the autoscaler
    went 6 → 10 replicas at 19:52:30 (`platform-events.log:12`), replica 7's 4-connection
    pool was full within 37 s (`checkout-service.log:18`), and 500s continued at 19:58:04
    (`checkout-service.log:19`). This is what RB-004 step 4 warns against.
  - `RB-002` (payment provider timeouts): the provider was healthy, and the failures
    happened inside checkout.
  - `RB-005` (slow queries): requests failed rather than succeeding slowly, and queries were
    fast (118 ms, `postgres.log:5`).
- Note for the runbook prompt: each file's `Status: current` line sits in its header,
  which `search_runbook` doesn't return. The agent can only tell current from archived by
  the `source` path.

## Queries that worked / didnt

Pass 1 (`search_logs`). The tool only does AND substring matching, so a date prefix such as
`2026-08-12T` stands in for a time filter, and `2026-08-12T19:3` for 19:30 to 19:39.

| Query | Matches | Notes |
|---|---|---|
| `2026-08-12T19:3 checkout` | 18 | |
| `2026-08-12T checkout` | 46 | The whole day. Truncated at the default `max_results=20`. |
| `2026-08-12T payments` | 11 | |
| `2026-08-12T stripe` | 5 | |
| `2026-08-12T provider` | 5 | Same lines as `stripe`. |
| `2026-08-12T postgres` | 9 | |
| `2026-08-12T api-gateway` | 10 | |
| `2026-08-12T platform` | 7 | |

Pass 2 (`search_changes`). Change records write dates as `2026-08-12 19:31 UTC`, with a
space, not `T`. Queries marked **from pass 1** used service names learned from the logs,
which the historian can't have (D4), so its prompt has to reach those records another way,
such as by listing what shipped around the incident.

| Query | Matches | Notes |
|---|---|---|
| `checkout 2026-08-12` | 3 | From the report. |
| `payment provider` | 1 | From the report's theory. `payment` also matches `payments-worker`. |
| `stripe` | 0 | Named in the logs, not the report. No record mentions it. |
| `checkout-service` | 3 | From the report. |
| `api-gateway` | 1 | **From pass 1.** |
| `payments-worker` | 1 | Borderline: the report blames the payment provider, but the service name came from the logs. |
| `postgres` | 0 | **From pass 1.** |
| `database` | 1 | **From pass 1**, via postgres. The word also appears in a record already found by `checkout 2026-08-12`. |
| `platform` | 2 | **From pass 1.** |
| `release 2026-08-12` | 3 | Historian-safe. The same records as `checkout 2026-08-12`; `release` matched words in the notes, not `Type: release`. |

Runbook (`search_runbook`). There is no `truncated` field; compare `total_matches` with
`returned`.

| Query | Matches | Notes |
|---|---|---|
| `connection pool timeout` | 1 | RB-004 only. |
| `connection pool` | 2 | Adds LEG-014, archived; only its `source` path shows that. |
| `timeout` | 3 | RB-002, RB-004, RB-005. |
| (RB-010) | n/a | Read from the file directly, not through the tool. An agent would need a search such as `rollback`. |
