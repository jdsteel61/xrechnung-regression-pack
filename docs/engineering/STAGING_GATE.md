# Staging gate — Railway target, 16 September 2026

## Continuation — 18 September 2026 (final acceptance gate)

Current deployment **`REDACTED-DEPLOY-ID-1` (SUCCESS, 21:18 UTC 17 Sep)** — the provider-absence wedge-fix build (image `REDACTED-IMAGE-DIGEST`). `/health` HTTP 200, database reachable. Local full suite: **131 passed, 5 skipped**, including the nine `pilot/test_gate_recovery.py` tests. The live fault-injection scenario set (§F) ran to completion at 21:41–21:54 UTC 18 Sep; every scenario settled fail-closed and every gate it closed was reopened only by audited deterministic verification. All four gates healthy at the end of the run. **Final recommendation: GO** (see the end of this section).

- **A. Bounded safety-gate recovery — implemented, locally proven, live-proven.** `Service.verify_gate` (`pilot/service.py:415`) is the *only* code path that can set a closed gate healthy. It runs under the job lock **and** the cluster-wide PostgreSQL advisory lease (so no submit can execute anywhere), and reopens only after six deterministic checks pass: (1) no stranded running jobs (any found are failed through the same durable transition the timed recovery uses), (2) zero submissions without a durable result, (3) raw-object cleanup retried to zero backlog and *proven* by both empty DB pointers and an empty bucket listing, (4) the payment/refund ledger reconciles page-by-page against the provider, (5) the reservations sweep passes with no unresolved reservations, (6) health checks (database reachable, scratch empty, no interrupted maintenance, no running jobs). There is deliberately no parameter to skip or weaken a check. Every actual close/reopen transition is audited in the append-only `gate_audit` (actor, reason, evidence JSON; the `xreg_api` role has SELECT+INSERT only — no UPDATE/DELETE, see `pilot/migrations/002`).
- **B. Live no-bypass proof.** Forged operator requests (`reopen`, `unpause`, arbitrary `state`) against `POST /operator/action` all returned `409 operator_action_forbidden`; the operator tool surface (`health, maintenance, pause_acquisition, retry, pause, verify, refund`) contains no reopen/unpause, and extra arguments are rejected. Evidence: [operator-no-bypass.json](evidence/staging-gate/fault-injection-20260917/operator-no-bypass.json). The operator may *request* verification; only the deterministic checks reopen.
- **C. Live close → verified-reopen through the real operator route.** The operator maintenance `verify_acceptance` ran the full acceptance suite **on the container**: `{"passed": true, "exit_code": 0}`. Its verification pass closed `acquisition` (audit: `closed actor=system reason=acceptance_verification_started` — verification does not auto-reopen). A live operator `verify` then returned `passed: true` with all six checks clean (ledger 1 page, `unresolved=[]`; reservations sweep passed, 0 unresolved; raw cleanup 0 unresolved, 0 pointers, **0 objects in the bucket**; health all green) and produced audit row **`acquisition reopened actor=operator reason=deterministic_verification_passed`**. Three preceding attempts were audited `verification_failed/worker_busy` — the 1 s recovery tick legitimately holds the same lease while paging Stripe; patient retry is the designed path. Evidence: [acquisition-reopen.json](evidence/staging-gate/fault-injection-20260917/acquisition-reopen.json) (gates, 12 audit rows, orders, submissions, events, maintenance, bucket listing).
- **D. Provider-absence wedge defect — found live, fixed, regression-tested, redeployed.** Deleted sandbox checkout sessions (well-formed `cs_test_…` returning 404) previously raised out of `StripeSandbox.session`, wedging ledger/reservation reconciliation and therefore every gate verification forever. Fix: a well-formed `cs_test_` 404 now returns `None` = provider absence; absence is authoritative **only** for never-charged orders (`unpaid`/`expired` reconcile and release); a paid order with no provider object stays unresolved (fail-closed). Regression test: `test_deleted_checkout_session_is_authoritative_absence`. Live proof on the fixed build: the recovery tick healed wedge order `order_E` unattended; `order_T` (paid+delivered, session *and* PaymentIntent both 404) was deliberately unreachable and was removed by a **documented one-off DB-admin `psql` DELETE** (the single administrator intervention this gate; 15 orders remain) — see [order_T-debris-before.json](evidence/staging-gate/fault-injection-20260917/order_T-debris-before.json). Post-fix verification passed all checks.
- **E. Tenant isolation — documented (no provider change) + fresh independent review.** [TENANT_ISOLATION.md](TENANT_ISOLATION.md) records exactly what is provider-enforced (per-bucket credentials with bucket-scoped ACLs; the `xreg_api` DB role's schema-scoped DML; the webhook signing secret; the separate `OPERATOR_TOKEN` capability) versus application-enforced (order-scoped raw keys `raw/{order}/{job}/…`; hash-only credential storage; public-corpus hash allowlist with `customer_uploads` disabled; report/input bucket credential separation), the credential boundaries, and the residual blast radius (chiefly: the single input-bucket credential's app-layer scope is the whole bucket — row/key-level tenant separation is enforced by the application, not IAM). A fresh independent **Astra** review asked specifically whether the implemented isolation is adequate for the narrowly scoped pilot using customer-owned XRechnung *test* fixtures: verdict **ADEQUATE**, empty MUST-FIX list, stronger provider-enforced tenant isolation explicitly **not** a pre-customer blocker for this scope. See [INDEPENDENT_REVIEW_ISOLATION.md](INDEPENDENT_REVIEW_ISOLATION.md).
- **F. Live fault-injection scenarios — DONE, all four PASS.** The four reminted TEST orders (A–D in `.cache/fault-injection/orders.json`) were paid by the account holder in the browser at ~21:25–21:27 UTC 18 Sep. Order A proved the live HTTPS `submit`→validate→deliver path, including the included rerun, on the fixed build (23.5 s and 22.6 s, `runs=2`). That spent order A, so the faults were targeted at orders B, C and D. All four ran live on deployment `REDACTED-DEPLOY-ID-1`, one fault at a time, with one idempotency key per scenario:

| Scenario (order, payload) | Fault injected | Terminal state + durable result | Raw-object deletion | Idempotent replay (same key + payload) | Gate close → verified reopen (`gate_audit` ids) |
|---|---|---|---|---|---|
| **A** — validator SIGKILL mid-validation, live HTTPS (C, payload-small: 2 pairs, 4 batches) | On-container loop armed before the submit and matching only `argv[0]` ending in `/bin/java`. It saw the validator JVM (pid 6225) and SIGKILLed it 3.0 s later, during the first batch. | Worker containment recorded `process_exit_code: -9` for the batch. The job finished as incomplete (`exit_code 2`, 2 execution errors, 4 incomplete comparisons, scratch removed), was returned inline over HTTPS in 20.3 s and recorded `job=failed`, `runs=0`, `attempts=1`. The incomplete pack was kept as run `-1`; the included run was not consumed. | `raw_job` cleared in `finally`; bucket `raw/` count **0** | HTTP 200 in 0.8 s: same recorded result, `attempts` still 1, no new run | #13 `delivery closed system validator_incomplete_exit` → #14 `worker_busy` → #15 `delivery reopened operator deterministic_verification_passed` (all six checks clean) |
| **B** — service restart during 250-pair raw staging (B, payload-full: 500 objects) | `railway restart` of the same deployment (no rebuild) issued 5.5 s after `job=running`. At that moment the bucket held **26 of 500** staged objects under the persisted `raw_job` pointer. The in-flight request got HTTP 502 after 26.3 s. | Unattended: the restarted instance's 1 s recovery tick took the cluster lease and failed the orphaned job (`worker_interrupted_resubmit_or_refund`). It wrote the durable result for the NULL submission (`submission_interrupted`) at 21:44:59, about 3 s after the old process died. `/health` was back to 200 by 21:45:06. | The recovery pass deleted the 26 partially staged objects and cleared the pointer: bucket count **0**, `raw_job` null. This is the partial-staging retry path. | HTTP 200 in 1.2 s: recorded failed result, `attempts` still 1 | #16 `safety closed system worker_interrupted (source=incomplete_submission_recovery)` → #17 **failed closed** (`ledger pages=0`: `reconcile_ledger` returned `reconciliation_running` because the recovery tick held `reconciliation_lock`) → #18 `worker_busy` → #19 `safety reopened operator deterministic_verification_passed` |
| **C** — crash in the report phase (C 2nd attempt, payload-small) | On-container harness `crash_report_phase`. Real validation completed, then the harness process SIGKILLed itself (exit 137) before report persistence. (A first invocation was rejected before admission with exit 1 and no state change, `attempts` unchanged; stderr was not captured, but this fits the lease contention seen elsewhere in the run. The retry was admitted.) | Unattended: the serving instance's recovery tick failed the job about 18 s after `job_started` (`worker_interrupted_resubmit_or_refund`, `attempts=2`, `runs=0`, no report delivered) and wrote the durable submission result. | The kill skipped the `finally` block, so by construction the 4 staged objects and the `raw_job` pointer were left behind. The recovery pass removed them: bucket count **0**, pointer null. | HTTP 200 in 0.4 s over live HTTPS (the HTTP key maps to the harness `request_id`): recorded failed result | #20 `safety closed system worker_interrupted (incomplete_submission_recovery)` → #21 `safety reopened operator deterministic_verification_passed` (first try) |
| **D** — job deadline expiry (D, payload-full) | On-container harness `force_timeout`: a real submit with a 2 s `job_timeout_seconds`, enforced through the production `contained_run` timeout. | `TimeoutExpired` took the exception path. The result came back inline (harness exit 0) after 233.8 s: about 231 s of staging 500 objects plus the 2 s deadline. The job recorded `failed` with `execution_failed_resubmit_or_refund`, `temporary_directory_empty: true` and `runs=0`. | `finally` deleted all 500 objects: bucket count **0**, pointer null | HTTP 200 in 1.2 s: recorded failed result, `attempts` still 1 | #22 `delivery closed system execution_failed` → #23 `delivery reopened operator deterministic_verification_passed` (first try) |

End state (`scenario-d-reopened`): all four gates healthy, 0 running jobs, 0 unfinished submissions, bucket `raw/` count 0, ledger 1 page with `unresolved=[]`, and reservations sweep passed. Final orders:
- A: `delivered`, reservation `spent`.
- B, C and D: TEST orders left `paid` + `failed` with reservation `held`. The normal refund path is still available to them and they were deliberately not refunded.

No code, configuration or deployment was changed during the run, and there was no administrator DB intervention.

Operational findings, none of them blockers:
- (i) The restart kept the container filesystem: `/tmp` and the fault kit survived, so no re-transfer was needed. The earlier expectation that `/tmp` would be wiped applies to a redeploy, not a restart.
- (ii) Operator `verify` can fail closed on transient contention with the 1 s recovery tick: either the job lease (`worker_busy`) or the `reconciliation_lock` (ledger `pages=0`). Patient retry is the designed path, and it always converged, within at most 3 tries.
- (iii) On the incomplete-exit path (scenario A) the public order status shows `job=failed` with `error: null`. No customer-facing error code is set there; it is cosmetic and was left unfixed within this gate's scope.

Evidence in [fault-injection-20260917/](evidence/staging-gate/fault-injection-20260917/):
- `scenario-{a,b,c,d}-fault.json` and `scenario-{a,b,c,d}-reopened.json`: the fault and reopened snapshots, containing gates, the 40 most recent audit rows, orders, submissions, events, maintenance and the bucket listing.
- `scenario-a-kill.json`: the kill log.
- `scenario-b-restart.json`: the timeline and the pre-restart bucket count.

The kill loop and the bucket/pointer probe are saved as `.cache/fault-injection/killjava.py` and `rawcount.py`.
- **G. Incremental costs.** Railway workspace, billing period 2026-08-21…09-21, final `railway usage` read after the scenarios: current bill **$0.9920**, estimated **$1.14**. When this gate opened it was $0.7895 / $0.9668, and before the scenario run $0.9358 / $1.09. That makes the incremental cost ≈ **$0.20** for the whole gate window: the redeploy, all live testing, and the four fault scenarios, which cost about $0.06 including the one service restart. Stripe: TEST mode only, **£0** real money, and no new payments were made during the scenario run. Validation model calls and tokens: 0. Independent reviews: Astra via the existing codex subscription (token-metered, no cash). No new infrastructure, no plan change and no new deployment (the restart reused `REDACTED-DEPLOY-ID-1`). No usage limits were set.

| Remaining blocker | Status |
|---|---|
| Bounded safety-gate recovery (close/reopen, audit, no bypass) | **GO** — implemented, 9 local tests, live audited close→verified-reopen proven (§A–C) |
| Provider-absence wedge (deleted checkout sessions) | **GO** — fixed, regression-tested, redeployed `REDACTED-DEPLOY-ID-1`, live-healed (§D) |
| Tenant isolation documentation (no provider change) | **GO** — TENANT_ISOLATION.md (§E) |
| Independent review of isolation adequacy | **GO** — Astra verdict ADEQUATE, no pre-customer blocker (§E) |
| Live fault-injection scenarios (validator kill, restart during staging, report-phase crash, deadline timeout) | **GO**: all four passed live on `REDACTED-DEPLOY-ID-1`. Each proved a fail-closed terminal state with a durable result, raw-object deletion to 0 (including partially staged objects after a restart), idempotent replay and an audited verified reopen (§F) |

**Final recommendation: GO for the staging acceptance gate.** The scope is the narrowly scoped pilot using customer-owned XRechnung *test* fixtures and TEST-mode Stripe.

Every blocker in this table is closed with live evidence on the running build. The live checks show:
- Bounded, audited gate recovery with no bypass.
- The provider-absence wedge fixed.
- Tenant isolation documented and independently judged adequate.
- Live HTTPS delivery including the rerun.
- All four live fault scenarios fail closed and recover through deterministic verification, never through manual state edits.

This is not a live-payments launch: `live_payments` stays construction-locked. The DB/bucket region split (US West Postgres, Amsterdam buckets; see "Region decision") remains an owner decision to make before any real customer data. It is not a staging blocker.

## Continuation — 17 September 2026

The current `xreg-api` staging deployment is **`REDACTED-DEPLOY-ID-2` (SUCCESS)**. `/health` returned HTTP 200 with the database reachable. The local full suite passed **127 tests, five skipped**. The older status text below describes earlier deployment snapshots; this section records the newer observed state. The live payment/delivery/refund sequence below ran on predecessor `REDACTED-DEPLOY-ID-3`; subsequent deploys added tested interrupted-job admission handling, fenced PostgreSQL recovery/startup writes, and lease-guarded recovery for incomplete submissions and maintenance. The latest revision ensures an interrupted rerun cannot inherit an earlier delivery result.

- **Raw S3 fixtures:** `pilot/object_storage.py` stages raw bytes under generated order/job keys in the existing private `xreg-inputs` bucket. The API reloads those bytes and deletes them in `finally`; a persisted `raw_job` pointer lets timed maintenance retry failed deletion. Local tests cover success, failure, timeout, retry, restart, cross-order token denial and failed-delete retry. A live public-fixture delivery and included rerun left **zero `raw/` objects** in the input bucket. A direct synthetic bucket probe confirmed A/B prefix deletion and report-bucket credentials were denied access to the input bucket. These are application order-scope and bucket-scope results, **not per-tenant IAM isolation**. No live forced worker crash/timeout deletion or object-version/backups proof was run.
- **Durable finance:** a bounded provider-backed ledger pass repairs missed paid and refund states while retaining uncertain reservations. The submit/decode refund race found by the independent review was reproduced, fixed, and regression-tested. PostgreSQL job execution uses a cluster-wide advisory lease; overdue recovery obtains the same lease before failing a job. A live Stripe **TEST** purchase reached paid entitlement, one public fixture delivered twice with the same-key replay staying at run 1, all three report formats downloaded, and a TEST refund reached `succeeded`. Independent provider reads matched the database's paid session and succeeded refund IDs/status. The test order ended with `reservation=spent`, `next_action=none`; a staging database scan found zero running jobs, zero failed reservations and zero raw-job pointers. Crash, timeout, provider-outage and late-payment paths are locally tested but not proven through actual Railway process kills or Stripe failure injection.
- **Operator API:** the authenticated dispatcher provides bounded retry, verified-payment refund, gate pause, and aggregate health/spend/ruleset reads. `railway_app.py` accepts an optional `OPERATOR_TOKEN`, but none was provisioned for a manager and the live operator route remains disabled (`409 operator_access_denied`). No Grok/Claude model was connected, and no model spend was incurred.
- **Review limits:** the current single input-bucket credential cannot prove provider-enforced tenant A/B isolation. A safety gate closed by a worker interruption still needs a verified reopening path before unattended reruns can be claimed. Exact live crash/restart and timeout deletion acceptance remains outstanding. Astra independently reviewed the running staging system, reproduced and reported several recovery defects that were patched, and confirmed the last replay fix on the final deployed revision. The final read-only review found no new P1/P2 defect; it did not establish the unrun provider and crash-boundary proofs.

## Railway transition status (current)

**PARTIAL — the API is deployed and Postgres-backed with a real Stripe TEST payment path, and Linux validation now runs with byte-identical KoSIT parity on the actual Railway container; full live staging acceptance is still NO-GO pending a durable redeploy.** On 16 September 2026 the `xreg-api` service was deployed and is serving at **https://REDACTED-STAGING-HOST.up.railway.app**, backed by Railway PostgreSQL through the restricted `xreg_api` role, with a working unauthenticated `/health` readiness probe and a **genuine hosted Stripe TEST Checkout → signed webhook → entitlement** path (verified with test card 4242). **The previously critical "validator is Windows-only" blocker is resolved in code and proven on Linux (see "Priority 1" below): the KoSIT engine + both ruleset bundles are byte-identical across OS, a Linux Temurin 21.0.8+9 JRE is separately pinned, and a real validation on the Railway Linux container reproduces the Windows benchmark byte-for-byte (identical JUnit, identical findings, deterministic) with the sole difference being the by-design per-OS Java-launcher digest.** The remaining gap to a live NO-GO→GO is operational, not architectural: the durable `railway up` redeploy is currently blocked by a transient network-path TLS fault (`BadRecordMac`) on this machine, so the *running* image is still the pre-fix build (which fails closed) and the end-to-end HTTPS `submit`→report has not yet been exercised against the deployed service; Priorities 2–4 (S3 object storage, durable refund/replay/ledger, operator API) are not yet started. Buckets are provisioned but not wired into the application (report blobs still live in Postgres; there is no S3 adapter). The earlier GCP proposal below is historical and superseded. Railway CLI must be v5.x (v4 cannot read the stored token); the metered Railway Agent was not invoked.

Minimal code changes made this run to make the existing app Linux-deployable, keeping all 104 local tests green: `pilot/process.py` moved the Windows-only `ctypes.wintypes` import inside the already Windows-guarded runner; `pilot/server.py` gained an env-gated `deploy` mode (bind `0.0.0.0`, unauthenticated `/health`, and a same-origin write guard for a public domain instead of the loopback guard); `pilot/payments.py` `StripeSandbox.checkout` now sends `managed_payments[enabled]=false` (the sandbox defaults to Managed Payments, which rejected the explicit `payment_method_types`); and a new `pilot/railway_app.py` entrypoint builds a Postgres-backed `Service` and selects the real `StripeSandbox` provider when a `sk_test_` key and public HTTPS origin are present. Build config: root `main.py`, `requirements.txt`, `.python-version`, `railway.json`, and a `.railwayignore` that trims the 132 MB `evidence/` tree to the two files the app reads. Evidence: [railway-deploy-probe.json](evidence/staging-gate/railway-deploy-probe.json).

| Component | Actual deployed state on 16 September 2026 |
|---|---|
| Workspace | `the owner's Railway workspace` (`REDACTED-WORKSPACE-ID`); existing active subscription, not upgraded |
| Project/environment | `xreg-staging` (`REDACTED-PROJECT-ID`) / `staging` (`REDACTED-ENV-ID`); default `production` environment left empty |
| PostgreSQL | Railway PostgreSQL 18.6, service `REDACTED-PG-SERVICE-ID`, running with private endpoint, no public TCP proxy; 134.7 MB observed volume use. Railway placed the database and volume in **US West**, despite the intended EU region. |
| Buckets | Private `xreg-inputs` (`REDACTED-BUCKET-INPUTS-ID`) and `xreg-reports` (`REDACTED-BUCKET-REPORTS-ID`) in **Amsterdam**. Both report 0 objects and 0 B after the probe. |
| API | `xreg-api` (`REDACTED-API-SERVICE-ID`) **deployed and RUNNING** (Railpack, Python 3.13) at `https://REDACTED-STAGING-HOST.up.railway.app`, bound `0.0.0.0:$PORT`, with a `/health` readiness probe returning `{"status":"ok","database":"reachable"}` (HTTP 200, ~0.33 s). Serves the storefront, checkout, order status and `/webhook`. |
| Worker | `xreg-worker` (`REDACTED-WORKER-SERVICE-ID`) still empty — **not deployed**. It cannot run the pinned validator on Linux (Windows `java.exe` + Windows process containment), so no worker/validation runs in staging. |
| Database migration | [001_railway_postgres.sql](pilot/migrations/001_railway_postgres.sql) applied; version 1 and the `xreg` tables verified. The **deployed** API connects through `PostgresState` on boot and reads/writes live: `POST /checkout`→201 created an order; authed `GET /orders/{id}`→200; unauth→409. Report blobs are stored in `xreg.reports` (bytea). The local (non-Railway) path still uses SQLite. |
| Credentials | The **deployed** API authenticates to Postgres as `xreg_api` (confirmed `current_user='xreg_api'`, `has_table_privilege(...,'xreg.schema_migrations','SELECT')=false`). The role has DML on application tables only — no schema CREATE, no migration-ledger SELECT, no superuser. Its password and PostgreSQL/bucket variable references are on `xreg-api`; `xreg-worker` has no DB/bucket credentials. This is schema-level least privilege, not tenant isolation. |
| Stripe TEST | Sandbox `acct_REDACTED` re-authorized this run (`sk_test_`, `livemode=false`, key expires 2026-12-15; `charges_enabled=false`, `details_submitted=false` — expected for a sandbox). A hosted-webhook endpoint `we_REDACTED` (7 events) was created pointing at `/webhook`; its signing secret and the test key are Railway variables on `xreg-api` (never printed). The deployed API creates **real** hosted Checkout Sessions (`https://checkout.stripe.com/…`, `simulated=false`). A full test payment (card 4242) drove a signed `checkout.session.completed` webhook → signature verified → order `order_H` reached `payment=paid, job=awaiting_inputs`. Live mode was never touched. |

One Railway configuration inspection printed the template-generated PostgreSQL administrator password in CLI output. The password was rotated immediately in PostgreSQL and Railway, and password-authenticated connection was verified after redeploy. No secret value is recorded in this file. A dedicated personal SSH key was registered temporarily for database administration, then deregistered and deleted locally after the probes; Railway reported no registered SSH keys afterwards.

### Priority 1 — Linux validation execution (17 September 2026)

Objective: replace the Windows-only validator assumptions with a production-credible Linux execution path on Railway, preserve the validated KoSIT/ruleset artefact hashes and provenance, separately pin the Linux Java runtime, reproduce direct KoSIT parity and deterministic JSON/JUnit, run the complete suite, and document any difference from the Windows benchmark. **Result: GO at the code and container-execution level; the only unproven step is the live HTTPS end-to-end, which is gated on the durable redeploy below.** Every result here was measured on the actual `xreg-api` Railway Linux container (env `staging`) via `railway ssh`, with the patched files transferred in and `XREG_JAVA` set to the pinned launcher.

- **Artefacts byte-identical across OS (provenance preserved, nothing rewritten).** The KoSIT engine (`validator-v1.6.3`, 19 files), the OLD bundle (`…release-2025-07-10`, 97 files) and the NEW bundle (`…v2026-01-31`, 97 files) present on the container have exactly the same set of `(relative-posix-path, sha256)` pairs as the stored Windows-validated provenance — zero added, removed or content-changed files. The only cross-OS difference was the *serialization order* of `xreg.tree_identity()` (it sorts native `Path` objects, so Windows and POSIX enumerate the identical files in different order). This is handled by an **order-independent comparison** (`worker._same_tree`, compares the canonical set of pairs); **no stored evidence digest was rewritten**, so `manifests/provenance.json`, the maintenance provenance and every sha-of-file pin are untouched. The exact-content check is unchanged — a genuinely added/removed/changed file still fails `validated_dependency_drift`.
- **Linux Java runtime separately pinned.** The container installs Eclipse Temurin JRE **21.0.8+9** via `mise` (`mise.toml`: `java = "temurin-21.0.8+9.0.LTS"`), matching the Windows `jdk-21.0.8+9-jre`. The real launcher is `/mise/installs/java/temurin-21.0.8+9.0.LTS/bin/java` (the `java`/`/mise/shims/java` on `PATH` is a shim that re-execs to it), sha256 `90d0a6d25da103678013f193db078b90ed8aeb27b66ebe78e0149616c9c1b811`, recorded in [`manifests/linux-runtime-provenance.json`](manifests/linux-runtime-provenance.json). `XREG_JAVA` is set on both `xreg-api` and `xreg-worker` so `worker.java_binary()` resolves the launcher, not the shim. `verify_pinned_core()` reads the launcher bytes and **fails closed** (`validated_java_runtime_drift`) on any mismatch; it passed on the container. No validator or ruleset version changed — engine stays 1.6.3, bundles stay the two pinned releases.
- **Direct KoSIT parity + determinism (byte-for-byte).** Running the deployed worker path (`worker.execute`) on the container against a synthetic public fixture (KoSIT test instance `ubl-inv-br-de-1-test.xml`, sha `c54a3b7c…`, validated through both bundles) versus the same input on the Windows benchmark:

  | Output | Windows | Linux (Railway) | Match |
  |---|---|---|---|
  | `junit.xml` sha256 | `98265e56…` | `98265e56…` | **identical** |
  | `results.json` findings (minus `provenance`) sha256 | `aab32a18…` | `aab32a18…` | **identical** |
  | summary | attempted 4 / passes 4 / 0 errors / 0 regressions | attempted 4 / passes 4 / 0 errors / 0 regressions | **identical** |
  | `provenance.java_executable_sha256` | `8853c818…` | `90d0a6d2…` | **differs — by design (per-OS launcher pin)** |
  | wall time | 49.2 s | 15.4 s | Linux faster |

  Determinism: two consecutive Linux runs produced byte-identical `junit.xml` and findings. **The only difference from the Windows benchmark is the pinned Java-launcher digest inside `results.json.provenance`** — expected and documented; every validation finding is identical.
- **Complete suite runs on Linux.** `python -m unittest` of the full suite on the container: **109 tests, 6 skipped (the Windows-only `ProcessContainmentTests`), 103 passing**, including 5 new Linux containment tests. Windows locally: **104 passed, 5 skipped** (the new POSIX tests skip off-Linux). No regression on either OS.
- **Production containment bug found and fixed (Linux).** The new `PosixProcessContainmentTests` initially failed on the container: a SIGKILLed orphaned descendant became a **zombie that nothing reaped**, because Railway runs the app as **PID 1** and a Python PID 1 does not reap orphans. `process._pgid_alive()` counted the zombie as alive, so `_contained_run_posix` raised `process_tree_termination_unconfirmed` on an already-dead tree and zombies would accumulate across every job timeout/crash. Fixes: `_pgid_alive()` now treats `Z`/`X` (zombie/dead) states as not-alive (termination *is* confirmed — a zombie holds no memory, fds or scratch and cannot run), and `_contained_run_posix` sets `PR_SET_CHILD_SUBREAPER` and drain-reaps the job's process group (`waitpid(-pgid, WNOHANG)`) so orphans do not pile up at PID 1. The five tests now prove, on Linux: process-ownership evidence, descendant-kill-on-timeout, whole-tree-kill-on-supervisor-death (via `PR_SET_PDEATHSIG`), the scratch-size watchdog, and whole-job-deadline scratch cleanup.
- **Files changed this run:** `pilot/process.py` (OS-dispatched containment + the zombie/reaper fix), `pilot/worker.py` (`java_binary()` Linux resolution, `_same_tree` order-independent artefact check, `_worker_env`, Linux provenance pin), `manifests/linux-runtime-provenance.json` (real digest recorded), `mise.toml`, `.railwayignore` (ship the ~22 MB engine+2 bundles, exclude the 145 MB Windows JRE), `pilot/test_remediation2.py` (+5 POSIX containment tests). All local: 104 pass / 5 skip.
- **Remaining Priority-1 gap (operational, not architectural): the durable redeploy.** `railway up` for `xreg-api` currently fails during upload with TLS `received fatal alert: BadRecordMac` (the same ~25 MB context deployed successfully earlier the same day, and `railway status`/`ssh`/`variables` all work — so this is a transient large-upload network-path fault on this machine, not a code or account problem). Until a redeploy lands, the **running** image is the pre-fix build (no `_same_tree`, placeholder Java pin, no `XREG_JAVA` effect), which correctly **fails closed**, so the live HTTPS `submit`→validate→report has not been exercised against the deployed service. The worker path was proven on the identical container via `railway ssh`; the outstanding step is purely shipping the already-validated files. **Single operator/next action:** re-run `railway up --service xreg-api --environment staging --ci` from a network where the upload succeeds (or let the codex handoff run it), then execute the live end-to-end submit on the paid order `order_H` (currently `awaiting_inputs`).

### Region decision (pre-customer-data, not a synthetic-staging blocker)

The Railway Postgres/volume are in **US West** and the buckets in **Amsterdam**. This split is recorded here as a **deployment decision to resolve before any real customer/EU data is stored**, not a blocker to synthetic staging validation: the current sandbox is public/synthetic-only and does not place personal data in either store. Co-locating is destructive (recreate the Postgres service in the EU region: re-run the migration, re-provision the `xreg_api` role, reset the `PG*` variables) and needs owner approval; it was deliberately **not** performed this run.

### Target, feasibility and cost

The API half of the pilot is deployed and Linux-compatible, and as of 17 September 2026 **real KoSIT validation and process containment are proven on the Railway Linux container** (see "Priority 1"): the former critical architectural blocker — Windows-only validator — is resolved, with the artefact provenance preserved and a Linux JRE separately pinned. The remaining work is now (a) landing the durable redeploy that ships those already-validated files (currently blocked by a transient `BadRecordMac` upload fault, not a code or account task) so the live HTTPS end-to-end can run, and Priorities 2–4: (b) there is still **no S3 application adapter**, so report/input objects live in Postgres and the two buckets are unused by the app; (c) durable refund/replay/ledger/reconciliation and crash/restart idempotency are not yet exercised end-to-end; and (d) the bounded operator API is not built. `StripeSandbox`'s lost-Checkout recovery and expiry-race paths still have only local unit coverage, not a real Stripe exercise. The DB/bucket region split (recorded above as a pre-customer-data decision) must be resolved before any non-public fixtures are considered — not needed for the current public-only sandbox, which does not touch buckets.

The workspace already had an active subscription containing a $5 plan item; this run did not upgrade it and created no new paid resources. `railway usage` (billing period 2026-08-21…2026-09-21) reported workspace **current bill $0.7895** and **estimated bill $0.9668**, essentially unchanged from the $0.787 / $0.965 observed at provisioning — the added always-on API service's incremental cost so far is negligible, though it will accrue more over the remaining days. No soft or hard workspace usage limit was set, to avoid pausing unrelated projects (another project shares this workspace). Idle PostgreSQL, the volume, and the running API service continue to accrue; empty buckets are 0 B ($0.015/GB-month). The Free plan's 0.5 GB per-service memory ceiling was not a factor because the deployed API is a small Python HTTP service (no JVM); the 512 MiB Java heap concern only applies to the worker, which is not deployed. Railway Agent token use is separately metered and was not invoked. [Plans](https://docs.railway.com/pricing/plans), [Pricing](https://docs.railway.com/pricing), [Bucket billing](https://docs.railway.com/storage-buckets/billing).

### Required provider tests in this attempt

| Gate | Result | Reason |
|---|---|
| Database role privileges | PASS (schema-scoped) | The **deployed** API runs as `xreg_api` with DML on application tables only and migration-ledger SELECT denied (`has_table_privilege=false`). Still schema-level least privilege, not per-tenant context. |
| PostgreSQL application adapter | PASS | The **deployed** API reads/writes live Postgres via `PostgresState`: checkout created an order, authed status read it back, gates/events/webhooks all persisted. Verified against the real service, not just a tunnel. |
| Stripe TEST Checkout, signed webhook, entitlement | PASS | Real hosted Checkout (`checkout.stripe.com`, `simulated=false`); test payment (card 4242) delivered a signed `checkout.session.completed` to `/webhook`; signature verified with the endpoint secret; order `order_H`→`paid`/`awaiting_inputs`; webhook idempotency ledger recorded the event. |
| Bucket credential isolation | LIMITED PASS | Report-bucket key received `AccessDenied` reading a synthetic input-bucket object (prior probe); bucket-level separation only. The buckets are **not wired into the application** (no S3 adapter), so this is infrastructure, not app-enforced. |
| Ordinary object deletion | LIMITED PASS | Synthetic object uploaded/read, deleted and independently confirmed absent by HEAD; [probe](evidence/staging-gate/railway-bucket-probe.json). Failure/timeout deletion, versions, backups and resurrection remain NOT RUN. |
| PostgreSQL restart persistence | LIMITED PASS | Service restart returned success; migration and `xreg_api` access still worked; the deployed API also survives redeploys with durable Postgres state. Deliberate API/worker crash-recovery under load remains NOT RUN. |
| Stripe replay / refund lifecycle / ledger reconciliation | NOT RUN | Entitlement proven; duplicate/out-of-order events, refund request→settle, and local↔Stripe ledger reconciliation not exercised (no HTTP refund route in this build; refunds run via reconcile/operator, not driven this run). |
| Tenant A/B isolation and job-scoped worker credentials | NOT RUN | No tenant identities or one-job broker; the worker service is not deployed and has no credentials. |
| Linux validation execution + direct KoSIT parity | PASS (on container) | Proven on the Railway Linux container: artefacts byte-identical across OS, Linux Temurin 21.0.8+9 pinned, `worker.execute` reproduces the Windows benchmark byte-for-byte (identical JUnit/findings, deterministic), full suite 103/103 on Linux. See "Priority 1" above. |
| Linux process containment (timeout/crash/scratch) | PASS (on container) | 5 new `PosixProcessContainmentTests` pass on Linux: descendant-kill-on-timeout, tree-kill-on-supervisor-death (`PR_SET_PDEATHSIG`), scratch watchdog, job-deadline cleanup. A real container zombie-reaping bug (app is PID 1) was found and fixed. |
| Termination during upload, queue, validation, report, refund and rerun | PARTIAL | Validation-side containment/termination now proven on the Linux container (above). The order/refund/queue termination rows still depend on Priorities 3–4 (durable refund/replay/ledger, operator API), not yet started. |
| End-to-end purchase, upload, report, rerun and refund | NOT RUN (deploy-gated) | Purchase→entitlement passes; `submit`→validation→report is proven on the container via the worker path, but the **live HTTPS** end-to-end has not run because the running image is the pre-fix build — the durable `railway up` redeploy is blocked by a transient `BadRecordMac` upload fault (see "Priority 1"). No architectural blocker remains. |

Railway buckets are private and environment instances have separate credentials, but those documented properties and the narrow probe above are not evidence that this application's tenant/job isolation or deletion requirements pass. [Storage Buckets](https://docs.railway.com/storage-buckets), [Environment isolation](https://docs.railway.com/guides/isolate-staging-production).

Current local regression after the Stripe adapter change: `python -B -m unittest test_xreg test_location_matching pilot.test_pilot pilot.test_remediation pilot.test_remediation2 -q` returned **104 tests, 0 failures**. This does not replace any provider gate above. The adapter uses Stripe's documented [session list](https://docs.stripe.com/api/checkout/sessions/list) and [session expiry](https://docs.stripe.com/api/checkout/sessions/expire) endpoints.

## Previous local/GCP assessment (historical evidence)

**NO-GO. Requested deployment and provider acceptance are incomplete. No staging infrastructure was deployed.** Local regression verification passed; it does not close any of the six real-infrastructure acceptance gates. No real Stripe requests, customer data, live payments, advertising or manager integration occurred.

## Infrastructure actually used

| Component | Observed current state |
|---|---|
| Application/API | Windows 11 local loopback Python service, benchmark-owned temporary state |
| Business state | Local SQLite; no external relational database provisioned |
| Queue/job state | Local submission/order records and process ownership; no external leased job queue |
| Invoice objects | Local temporary files; no staging storage bucket |
| Validator | Existing pinned Windows Java/KoSIT runtime and July 2025 / January 2026 rulesets |
| Payments | `OfflineStripe` in this benchmark; CLI source uses `DurableOfflineStripe`; neither is Stripe |
| Monitoring | Local test/benchmark evidence; no deployed monitoring or billing ingestion |
| Deployment tooling | Docker executable present; Linux engine unavailable. `gcloud`, `terraform`, and `stripe` were not found on PATH |

Exact Docker probe: `docker info --format '{{.OSType}}'`. It failed because `//./pipe/dockerDesktopLinuxEngine` does not exist. No cloud account inventory was obtained; absent CLI tooling does not establish that the owner has no cloud accounts.

`pilot/deployment.json` still explicitly says `DESIGN_ONLY_NOT_DEPLOYED`. No Terraform files, Dockerfiles or deployment YAML were found by the repository file search. Merely deploying the current loopback service with SQLite on ephemeral storage would not satisfy this task.

## Minimal target and current pricing

The earlier Google Cloud europe-west1 proposal is a candidate, not infrastructure used. Its Firestore choice must be replaced to satisfy the requested **relational** state. A minimal implementation can use one managed PostgreSQL database for business records, transactional reservations, webhook identities, deletion tombstones and leased jobs; Cloud Run for API and isolated validator executions; separate private input/report buckets; deterministic scheduled maintenance; and provider logging. No model or general agent platform is required.

Published prices checked online on 16 September 2026. These are reference USD prices, not a configured europe-west1 quote, actual bill, or spending cap. Free allowances, tax, currency conversion and account-specific discounts are not assumed.

| Candidate service | Current published reference and limitation |
|---|---|
| Cloud Run jobs | Displayed default rate: $0.000018/vCPU-second plus $0.000002/GiB-second; one-minute minimum. At 2 vCPU/2 GiB, $0.000040/second, or $0.048 for 1,200 billed seconds. Startup and other services are additional. [Pricing](https://cloud.google.com/run/pricing) |
| Cloud Run request-based API | Displayed active rates: $0.000024/vCPU-second, $0.0000025/GiB-second, $0.40/million requests. Regional selection still requires verification. [Pricing](https://cloud.google.com/run/pricing) |
| Cloud SQL PostgreSQL | Official indexed pricing lists shared-core `db-f1-micro`, 0.6 GiB RAM, at $0.0105/hour: $7.665 for 730 hours, compute only. Shared-core instances have no Cloud SQL SLA. Final regional sizing/storage/backup/network quote remains unresolved; the full pricing page fetch failed. This is not a recommended capacity guarantee. [Pricing](https://cloud.google.com/sql/pricing) |
| Cloud Storage | Regional Standard flat-namespace operations: $0.005/1,000 Class A and $0.0004/1,000 Class B; object DELETE is in the free-operation category. Region-specific byte storage and transfer price remain unconfirmed. [Pricing](https://cloud.google.com/storage/pricing) |
| Cloud Scheduler | $0.10/job/31 days outside the billing-account free allowance. [Pricing](https://cloud.google.com/scheduler/pricing) |
| Cloud Logging | $0.50/GiB ingested into ordinary log storage, including up to 30 days, outside the 50 GiB/project/month allowance; longer retention is $0.01/GiB-month. Monitoring and network logs have separate tariffs. [Pricing](https://cloud.google.com/products/observability/pricing) |
| Stripe test mode | No test transaction executed. Genuine integration must use Stripe's test environment and test payment methods. Live processing tariffs are irrelevant to this acceptance run. [Testing documentation](https://docs.stripe.com/testing) |
| Registry/builds, secrets, network, DB storage/backups | Not provisioned, measured or fully quoted; not included in the arithmetic above |

A complete monthly estimate cannot be responsibly stated before deployment configuration and workload limits are selected. No provider hard spending cap has been established.

## Exact tests and results

Executed from the project root:

```powershell
python -B -m pilot.benchmark --out evidence/staging-gate/local-baseline --count 2
```

Exit code **0**, benchmark `passed=true`. The command runs the complete existing five-module suite (`test_xreg`, `test_location_matching`, `pilot.test_pilot`, `pilot.test_remediation`, `pilot.test_remediation2`) before its HTTP/Java flow. No tests were removed or weakened.

| Local check | Observed result |
|---|---|
| Full existing suite | **102 passed**, 0 failures, 0 errors, 9.962 seconds |
| Real Java | **16 completed validations**: two public test IDs × paired identical outputs × two rulesets × initial/rerun |
| Initial delivery | HTTP 200, `delivered`, 28.883 seconds |
| Rerun | HTTP 200, `delivered`, 29.230 seconds |
| Complete benchmark | 72.752 seconds including suite/setup/flow |
| Report repeatability | JSON, JUnit and HTML each byte-identical |
| Submission replay | Same key returns original result; third execution denied |
| Access/payment redirect | Unauthenticated access denied; success redirect alone does not fulfil |
| Refund | Simulated pending → succeeded; provider result is deliberately set by the harness |
| Local scratch/caller preservation | Harness checks passed; not storage-provider deletion evidence |
| Source stability | Core and pilot source hash checks passed throughout execution |

Evidence: [summary](evidence/staging-gate/local-baseline/summary.json), [full tests](evidence/staging-gate/local-baseline/tests.txt), [events](evidence/staging-gate/local-baseline/events.json), [initial reports](evidence/staging-gate/local-baseline/run-1/results.json), [rerun reports](evidence/staging-gate/local-baseline/run-2/results.json).

Existing tests include synthetic cross-order authorization, local process death/recovery, Java descendant containment, refund/replay fault injection and cleanup. Their successful execution is **not** provider-enforced tenant isolation, an external database recovery test, or genuine Stripe acceptance. No attempt was made to remove the historical rejected synthetic directories or work around sandbox safety controls.

## Required staging acceptance matrix

All rows below remain **NOT RUN / BLOCKED**, except **Checkout and entitlement**, which now PASSES (real hosted test Checkout → signed webhook → `paid`/`awaiting_inputs`; see the provider-test table above). The "failed payment → no fulfilment" half of that row is not yet exercised. No separate cloud tenants were created.

| Required proof | Missing execution/evidence |
|---|---|
| Tenant A cannot access B data/results | Two real staging identities; API, DB and object-storage negative probes under their actual permissions |
| Raw files scoped by tenant/job | Provider IAM and application-owned object namespace checks; workers denied other tenants/jobs |
| Delete after success, failure and timeout | Provider deletion requests and independently verified object absence for each terminal outcome |
| No resurrection on retry/restart | Durable tombstones/fencing; stale upload/retry denial; version and soft-delete listings; restore attempts and backup behavior |
| No raw invoice bodies in reports/logs | Synthetic sentinel scans across actual reports, provider logs and storage; no request-body logging |
| Termination during upload | Kill API/container during partial transfer; expire upload authority and verify partial/final object cleanup |
| Termination while queued | Kill dispatcher/API with committed queue entry; restart and demonstrate one claimed execution |
| Termination during validation | Kill actual worker/container; recover lease and clean objects after confirmed process termination |
| Termination during report generation | Kill before/after report publication and DB commit; recover exact replay without duplicate fulfilment |
| Termination during refund | Kill around provider side effect/local commit; reconcile the same refund intent |
| Termination during rerun | Preserve entitlement and attempt identity across each commit boundary |
| Checkout and entitlement | PASS for paid → entitlement (real hosted Checkout + signed webhook, order `order_H`). Still missing: failed-payment → no-fulfilment case |
| Event ordering | Genuine duplicate and out-of-order events; current-provider-state reconciliation |
| Refund lifecycle | Genuine requested/pending/succeeded/failed cases, duplicate requests and restart recovery; fixture-generated events alone cannot prove provider outcomes |
| Financial ledger | Reconcile local reservations/entitlements/refunds against actual Stripe test objects; no stranded reservations |
| Entire staging flow | Synthetic purchase → upload → report → rerun → refund on the deployed system |

Local scratch deletion is expressly excluded as proof for these storage-provider rows. Deletion completion must be a durable, inspectable outcome; uncertainty must remain machine-actionable rather than silently counted as success.

## Provider-enforced controls

**None verified in staging.** No deployed IAM policies, service identities, network rules, DB grants/RLS, storage retention settings, container image digest, encryption configuration or provider audit receipts exist from this run.

Required implementation includes separate API/worker/maintenance/deploy identities, tenant-scoped database and object access, worker denial of Stripe/model/deploy permissions, protected metadata access, bounded temporary resources, and default-denied unnecessary egress. Raw-content buckets need explicitly verified versioning, soft-delete, retention and backup settings. A TTL or intended firewall setting is not enforcement evidence.

## Operating interface gaps

Current optional loopback endpoints are `GET /operator/state` and `POST /operator/action`, separately authenticated when the server factory is supplied an operator capability. The shipped CLI does not supply one. Current actions are fixed maintenance codes and pause acquisition.

| Requested operation | Current limitation |
|---|---|
| List unhealthy / inspect job | Paginated local state exposes enumerated job/payment/refund/reservation fields; no deployed unhealthy-job or job inspection interface |
| Retry eligible job | No bounded operator retry endpoint backed by a durable queue |
| Cancel/refund under policy | Customer refund and maintenance reconciliation exist; no complete operator cancellation/refund operation |
| Inspect spending/usage | Snapshot reports simulated reserves and zero configured cash budget; no provider usage reconciliation |
| Inspect service health | No deployed dependency/queue/deletion-backlog health endpoint |
| Inspect upstream ruleset state | Frozen manifests exist; no bounded operating endpoint |
| Create bounded maintenance task | Local fixed-code dispatcher exists; no external task leases/scheduled deployment |
| Pause/resume new intake | Pause exists locally; policy-checked resume is missing |

These are unfinished engineering requirements. A future manager must receive only authenticated bounded APIs and metadata, with no shell or invoice access. No Grok, Claude or other model integration is needed to close this task.

## Observed costs and interventions

- New paid cloud resources created and **plan upgrades: 0**. This run deployed code to the existing `xreg-api` service and generated a free `*.up.railway.app` domain — no new billable service, database or bucket was created. Workspace `railway usage`: current bill **$0.7895**, estimated **$0.9668** (≈ unchanged from provisioning). No workspace spending limit set (shared workspace).
- Real Stripe TEST API calls were made (account retrieve, webhook-endpoint create, and Checkout sessions incl. one completed test payment on card 4242). **Live mode was never touched.** Application validation/operating model calls and tokens: **0**; Railway Agent not invoked.
- One account-holder action was required and completed mid-run: re-authorizing the Stripe CLI to the sandbox (its device authorization had expired). Secret values were never printed; the test key and webhook secret were written to Railway variables via stdin.
- The Railway CLI on PATH was v4.30.3, which cannot read the stored v5 token (reported "Unauthorized"); it was upgraded to v5.57.2 to proceed. The database administrator password was **not** re-exposed this run.
- Local wall times measured; CPU seconds, peak memory/disk, electricity and host amortization were not measured. This session's own token/subscription cost is **UNKNOWN**.
- Manual state edit made: the `acquisition` gate was reset to healthy after the pre-fix Stripe adapter error tripped it; the fix (`managed_payments[enabled]=false`) prevents recurrence. No invoice interpretation or approval-review rejection occurred.

## Remaining blockers and recommendation

**Meaningful staging progress, but still NO-GO for production-like acceptance and for launch.** Now genuinely working on Railway: a deployed, Postgres-backed API with a health check and least-privilege DB role, and a real Stripe TEST Checkout→signed-webhook→entitlement path. What remains open is engineering, not account configuration:

1. **Linux validation — RESOLVED in code, proven on the container (17 Sep 2026).** A Linux Temurin 21.0.8+9 JRE is separately pinned, `pilot/process.py` has a POSIX containment path (session/process-group + `PR_SET_PDEATHSIG` + subreaper reaping) with 5 passing Linux containment tests, and `worker.execute` on the Railway container reproduces the Windows benchmark byte-for-byte (identical JUnit/findings, deterministic) with artefact provenance preserved. See "Priority 1". Outstanding: land the durable redeploy (blocked by a transient `BadRecordMac` upload fault) so the running image is the fixed build and the live HTTPS `submit`→report can run — no architectural work remains.
2. **Worker service** still not deployed. It is now *capable* of running the validator on Linux (its `XREG_JAVA` is set), but deploying it waits on the same durable-redeploy step and on the queue/isolation work.
3. **Bucket storage not wired (Priority 2)** — add a tenant-scoped S3 adapter so raw inputs use the provisioned buckets with deletion after success/failure/timeout/retry and cross-tenant denial, instead of Postgres blobs. Not started.
4. **Durable refund/replay/ledger/reconciliation + crash/restart idempotency (Priority 3)** — not yet exercised end-to-end. Not started.
5. **Bounded operator API (Priority 4)** and **tenant/job isolation** remain unbuilt.
6. **DB/bucket region split** — recorded above as a **pre-customer-data decision** (US West Postgres / Amsterdam buckets), not a synthetic-staging blocker. Co-locating is destructive (recreate Postgres in the EU region: re-run the migration, re-provision `xreg_api`, reset `PG*` variables) and needs owner approval; not performed this run.

Local (non-Railway) regression after this run's changes: `python -m pytest -q` → **104 passed, 5 skipped** (the 5 new POSIX containment tests skip off-Linux). On the Railway Linux container: **109 tests, 6 skipped (Windows-only), 103 passing**. Final completion still requires landing the redeploy and closing #2–#5 with provider evidence for every acceptance row above; local/container PASS results cannot substitute for the live end-to-end.
