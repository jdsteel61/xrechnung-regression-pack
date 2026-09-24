# Tenant isolation — staging pilot (provider-enforced vs application-enforced)

Scope: the single-tenant-customer XRechnung pilot on Railway staging
(`xreg-staging` / `xreg-api` at REDACTED-STAGING-HOST.up.railway.app), processing
customer-owned XRechnung test fixtures. No cloud or storage provider was added
or changed for isolation; this document records what the existing providers
enforce, what the application enforces itself, where the credential boundaries
sit, and what blast radius remains.

Input-surface note (verified by the independent review): the shipped policy
still restricts submitted fixtures to the **public-corpus hash allowlist**
(`worker.public_hashes`) and `Service.__init__` rejects enabling
`customer_uploads`. Enabling customer-owned fixture uploads is therefore the
functional pre-customer step, and it widens the reviewed input surface — it
needs its own review pass, separate from the isolation verdict below.

## Providers in play (unchanged)

| Component | Provider | Region | Notes |
|---|---|---|---|
| App service | Railway `xreg-api` | — | one container, app runs as PID 1 |
| Database | Railway Postgres service | US West | single database, schema `xreg` |
| Raw-input bucket | Railway Object Storage `xreg-inputs-*` | Amsterdam | S3-compatible (t3.storageapi.dev) |
| Report bucket | Railway Object Storage `xreg-reports-*` | Amsterdam | provisioned; **no application code path uses it** |
| Payments | Stripe TEST sandbox acct_REDACTED | — | no live mode anywhere |

Delivered reports are stored in Postgres (`reports` table) and served over the
authenticated order routes. The reports bucket is not read or written by the
serving application; its credential exists only for the one-shot
`pilot/staging_storage_probe.py` diagnostic.

## What the provider enforces

1. **Bucket-scoped object-storage credentials, cross-access denied.** The
   input-bucket credential and the report-bucket credential are distinct keys.
   Probed live on the container (2026-09-17, codes only, never values):
   - input credential → list `xreg-reports-*`: **AccessDenied**
   - report credential → list `xreg-inputs-*`: **AccessDenied**
   - input credential → list `xreg-inputs-*`: allowed (expected)
   Neither key can enumerate or touch the other bucket. This is enforced by the
   storage provider's per-bucket key model, not by application code.
2. **Postgres role separation.** The app connects as `xreg_api`, which holds
   DML on the application tables only; migrations run as the administrator
   over `railway ssh`, and `xreg_api` has no schema/DDL rights. The gate
   audit table is append-only at the GRANT level: `xreg_api` holds
   SELECT + INSERT on `gate_audit` and **no UPDATE/DELETE** (verified in the
   live DB), so running code cannot rewrite gate-transition history. Note the
   boundary's exact shape: the migration *grants* the append-only privileges
   but does not revoke pre-existing ones, and the app role can still insert
   fabricated audit rows or update `gates` directly — so the audit trail is
   tamper-evident against rewriting, not against a fully compromised
   application role. No customer-facing path to either action exists.
3. **Signed webhook entitlement.** Payment entitlement arrives only via a
   Stripe-signed webhook verified with `WEBHOOK_SECRET`; nothing about payment
   state is client-asserted.

## What the application enforces

1. **Order capabilities.** Every order read, submit, report fetch and support
   action requires the order's 64-hex access token, compared with
   `hmac.compare_digest` (unauthenticated reads get 409). Refund requests are
   made with the access token alone; the separate recovery token is used only
   to rotate access via `POST /recover`. No customer-reachable route enumerates
   orders; the separately-authenticated `/operator/state` route paginates an
   allowlist of operational fields (id, payment/refund/job/reservation state)
   with no fixtures, reports, errors or free text.
2. **Raw-object key scoping.** Customer inputs persist briefly under
   `raw/{order}/{job}/…` in the input bucket. `S3RawFixtures` constructs and
   verifies every key against that prefix and raises `ObjectScopeError` on any
   out-of-scope key (including during `list_raw` verification sweeps), so one
   order's store cannot name another order's objects. Per-job deletion runs in
   a `finally` on every submit. The recovery tick retries deletion for orders
   with a recorded `raw_job` pointer and closes the safety gate if deletion
   cannot be proven; gate verification additionally **detects** any leftover
   object via a full bucket listing (a non-empty listing fails verification)
   but does not itself delete unreferenced keys. Deletion is therefore not
   guaranteed during an outage — it is guaranteed to be *detected*, and the
   gate stays closed until it is proven.
3. **Credential-free worker environment.** Validation runs as
   `python -B -m pilot.job` under POSIX containment (setsid, PR_SET_PDEATHSIG,
   subreaper) with an allowlisted environment: only `TMPDIR`, `HOME`,
   `LANG`/`LC_ALL` and the resolved `XREG_JAVA` path. The job never receives
   the Stripe key, the database password, the webhook secret or any S3
   credential, and inherited file descriptors are closed. This is an
   environment allowlist, **not a security sandbox**: there is no UID
   separation and no filesystem/network restriction, so a *compromised*
   validator could in principle read the parent's environment through `/proc`.
   No XML-to-command execution path exists (DTDs/entities are rejected before
   parsing; filenames and commands are generated internally), so worker
   compromise is a residual risk, not a demonstrated credential escape.
4. **Operator capability separation.** `/operator/*` requires the separate
   `OPERATOR_TOKEN`; its tool set has no reopen/unpause primitive — the only
   reopen path is `verify`, which always runs the full deterministic
   reconciliation (`pilot/service.py` `verify_gate`) and audits
   `verification_failed` otherwise.
5. **Idempotent submissions.** Submissions are keyed by customer
   `Idempotency-Key` + payload hash; replays return the recorded result and
   never re-execute.

## Credential boundaries (who holds what)

| Credential | Held by | Can do | Cannot do |
|---|---|---|---|
| Input-bucket key (`AWS_*` ≡ `INPUT_S3_*`) | `xreg-api` container env | all of `xreg-inputs-*` | reach the reports bucket (provider-denied), DB, Stripe |
| Report-bucket key (`REPORT_S3_*`) | `xreg-api` container env (unused by server code) | all of `xreg-reports-*` | reach the input bucket (provider-denied) |
| `xreg_api` DB password | `xreg-api` container env | DML on app tables; SELECT/INSERT audit | DDL; UPDATE/DELETE audit; admin ops |
| Postgres admin | Railway ssh only (not in app env) | everything | — never present to the app |
| `STRIPE_TEST_KEY` | `xreg-api` container env | TEST-mode sessions/refunds in the sandbox | live charges (test-mode key by construction) |
| `WEBHOOK_SECRET`, `OPERATOR_TOKEN` | container env / operator store | verify webhooks / operator routes | order data, bucket access |
| Worker env (`pilot.job`, java) | per-job only | read its scratch dir | **no credentials at all** |

## Residual blast radius (accepted for the pilot)

1. **One container compromise reads both bucket keys from the environment.**
   Although the serving code constructs only an input-bucket client, the
   report-bucket credential is present in the service environment. A future
   non-pilot hardening: remove `REPORT_S3_*` from the app service (keep it in
   the probe context only) so a container compromise cannot mint report-bucket
   access at all. Until then this is an environment-level, not code-level,
   boundary.
2. **The input-bucket key is bucket-wide.** Object-level tenant separation
   inside `xreg-inputs-*` (per-order key prefixes, prompt deletion, verified
   empty bucket) is application-enforced; the provider cannot enforce
   per-order granularity. Exposure window is the duration of one submit plus
   the cleanup sweep (orders that die mid-job keep raw objects until the
   recovery tick and gate verification prove deletion).
3. **Single database schema.** All orders share schema `xreg`; row-level
   tenant separation is the access-token capability model plus the absence of
   any enumeration route. A DB-role compromise sees all pilot orders.
4. **Cross-region data path.** Postgres (US West) and buckets (Amsterdam)
   sit in different regions — a latency/failure-domain property, not a tenant
   boundary.
5. **Stripe TEST key.** Worst case is sandbox clutter in the test account;
   no live money exists in this deployment.

## Verdict for the pilot scope

For a narrowly scoped pilot with one customer's own XRechnung test fixtures:
the provider-enforced boundaries (per-bucket keys with proven cross-bucket
denial, append-only audit at the GRANT level, signed webhook entitlement,
credential-free validation subprocesses) plus application-enforced per-order
capabilities and verified raw-object deletion are adequate. Residual items
1–3 above are recorded as pre-wider-launch hardening, not pilot blockers; an
independent model review was run against this document (see
INDEPENDENT_REVIEW_ISOLATION.md).
