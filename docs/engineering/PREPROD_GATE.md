# Pre-production gate — 19 September 2026

Final hardening pass against FABLE_ADVERSARIAL_REVIEW.md (F1–F8 + managed-agent gaps). Stripe TEST only; synthetic/public fixtures only; no live traffic, no product features.

**Deployed revision:** Railway `xreg-api` deployment **`REDACTED-DEPLOY-ID-4`** (SUCCESS 2026-09-18 23:14Z UTC). The project is not a git repo, so the deployment ID is the revision identifier. Intermediate deploys this pass: `REDACTED-DEPLOY-ID-5`, `REDACTED-DEPLOY-ID-6`.
**Tests:** full suite, 11 modules: **168 passed, 5 skipped** (baseline 139/5). New module: `pilot/test_preprod.py` (29 tests). `verify_acceptance` now also runs `test_fable_review` and `test_preprod`.

## Verdict per requirement

| # | Requirement | Result | Evidence |
|---|---|---|---|
| 1 | Fail-closed env config replaces source locks | **PASS** (live-mode path proven offline only) | `pilot/launch_config.py`: `XREG_LIVE_PAYMENTS` / `XREG_CUSTOMER_UPLOADS` default false; only the exact strings `true`/`false` are accepted. Enabling either requires `XREG_PRODUCTION_ACK=reviewed-production-path-v1`. Live also requires `STRIPE_LIVE_KEY` (`sk_live_`), `STRIPE_LIVE_WEBHOOK_SECRET` (`whsec_`) and a declared `XREG_TAX_TREATMENT`, and refuses the simulator. `build_service()` validates before any connection. Service refuses a provider whose mode differs from config; livemode is threaded through webhook, session, refund and adapter checks; the upload allowlist follows config. `policy.json` stays locked. Tests: `LaunchConfigTests` (6). |
| 2 | Deploy Fable F2/F3; verify on Postgres | **PASS** | On-container `preprod_pg_check.py` against staging Postgres: `latest_delivered_selected=delivered-run-1`, `fallback_when_nothing_delivered=failed-attempt-2`, `synthetic_rows_remaining=0`, `missing_key_refuses_start=stripe_test_key_and_public_origin_required`, deployed provider `simulated=false`. Live: `GET /simulated-checkout/…` → 404; `POST /simulate-payment/…` → 409 (route unreachable). |
| 3 | Real 250-fixture staging pack | **PASS, after one fix** | See §Measured. The first run exposed a real defect: Railway's edge drops requests at **300 s**, so the buyer saw a 502 on a successful delivery. Fixed: submit answers **202 + poll URL** after 240 s, the job completes, and the same Idempotency-Key returns the durable result (`test_long_submission_returns_202_then_completes`). The rerun proved the fix live. |
| 4 | Checkout/reservation flood controls | **PASS** | Unpaid reservation TTL 24 h → **1,860 s**, with Stripe `expires_at` on the session so it self-expires at the provider. Unpaid checkouts are capped at **10** concurrently, so they can never occupy paid capacity. Per-client limit is **3 per 3,600 s**, keyed on a hash of the edge `X-Real-IP` (migration `003_checkout_rate.sql`). Rejections never close a gate. **Live:** 4 checkouts carrying forged `X-Real-IP`/`X-Forwarded-For` → 201, 201, 201, **409**, so the edge overwrites the header and spoofing fails. Tests prove a legitimate buyer checks out and pays after the flood expires. |
| 5 | Stripe reconciliation incl. out-of-app actions | **PASS for refunds; disputes proven offline only** | Webhook subscribes to `charge.refunded`, `charge.refund.updated`, `charge.dispute.*` and `refund.*`, with or without metadata. Every event re-reads provider truth (`payment_state`: PaymentIntent + refunds + disputes). The ledger pass also converges with no event at all. The F7 wedge (dashboard refund followed by app refund) self-resolves. **Live (TEST):** refund `re_REDACTED` created through the Stripe API with **no metadata** on order C → signed `charge.refunded` → order `refund=succeeded`, `refunded_pence=29900` → sweep → `job=refunded`, `reservation=spent`, `reconciliation_failed=0`, gates healthy. No DB edit. Disputes (block fulfilment, close acquisition, verify can reopen) and partial refunds are tested only with offline doubles; a TEST dispute needs a browser payment with Stripe's dispute card. |
| 6 | Delivery reopen requires validator health | **PASS** | `verify_gate('delivery')` runs `worker.validator_health`: pinned-core check plus a real supervised run of a pinned public canary (`pilot/canary/`, sha `6d4ac67e…`, in the corpus manifest). A failing or crashing probe blocks the reopen. **Live:** `verify delivery` → `passed=true`, `validator_health={passed, exit_code 0, execution_errors 0, 6.4 s}`. |
| 7 | Read-only bounded observability | **PASS** | `GET /operator/{audit,events,errors,reconciliation,recovery,state}`: paged at 100 with validated cursors. Codes and identifiers only; tests assert no tokens, hashes or product names appear. All six return 200 live. |
| 8 | Split operator credentials | **PASS** | `OPERATOR_READ_TOKEN` / `OPERATOR_ACTION_TOKEN`; the legacy `OPERATOR_TOKEN` now refuses startup (and is deleted on Railway). Tokens must be ≥32 chars of `[A-Za-z0-9_-]` and must differ. The read token can call only `health`/`spend`/`ruleset` and GET views. **Live:** read token + `pause` → 409 `operator_read_only_credential`. Tests cover all 7 mutating verbs, with state unchanged afterwards. |
| 9 | Bounded provider-object recovery | **PASS** (offline; no live wedge occurred to exercise it) | Operator verb `resolve_order{order_id}` retries raw cleanup and stranded jobs. It re-issues a provider-failed refund with a new idempotency key, **max 2**, then reports `limit_reached_owner_required`. It voids a paid order **only** when the session and the PaymentIntent both 404 (the `order_T` class), with an audit event. Transient errors stay unresolved. The order is then re-reconciled. |
| — | Pilot refund policy | **PASS** | Customer refunds allowed within 7 days of `paid_at`, otherwise `refund_window_closed`. Automatic full refund on undeliverable/lapsed obligation is unchanged. The operator `refund` requires `reason ∈ {customer_request_within_7_days, pack_not_deliverable}`, each with a deterministic eligibility check. There is no amount parameter: refunds are always the single £299 and at most one per order (Stripe also rejects >amount). Extra arguments are rejected. |

## Measured: 250-fixture pack (order D, TEST-paid, 250 paired fixtures, 8.7 MB request, 1,000 validations)

| Run | Wall time | HTTP | Outcome |
|---|---|---|---|
| Delivery (pre-fix build `REDACTED-DEPLOY-ID-6`) | **477.5 s** job_started→evidence_ready | 502 at 300.1 s (edge limit) | `delivered`, runs=1. Job survived the dropped connection; nothing stranded |
| Rerun (fixed build `REDACTED-DEPLOY-ID-4`) | **≈408 s** (23:15:16 → ≤23:22:04, 10 s poll granularity) | **202** at 241 s, then **200** cached result on key retry | `delivered`, runs=2; report: processed 1,000, **execution_errors 0**, 6 rule regressions, 0 incomplete |

After both runs: `raw_cleanup_pending=[]`, `running_jobs=[]`, `submissions_without_result=0`, bucket raw keys 0 (ledger/verify evidence). No timeout, no stranded state.

**Resource cost (Railway metrics, 1 h window covering the run):** peak **7.6 vCPU**, peak **0.75 GB** RAM, about **24.6 vCPU-min** total (upper bound, includes idle). At Railway list prices (~$20 per vCPU-month, ~$10 per GB-month) that is **≈ $0.013 per 250-fixture pack**, plus negligible egress and one Stripe TEST fee equivalent. The list prices are estimates and the billing invoice was not checked.

## Remaining blockers (before a paying customer)

1. **External security, licence and privacy review** of the *unlocked* path (`XREG_CUSTOMER_UPLOADS=true`, live mode). The live-mode code is tested with doubles only. At the time of this gate, before the live cutover, it had never talked to a live Stripe account, by design.
2. **Tax treatment and seller identity:** an owner decision. Live mode refuses to start until `XREG_TAX_TREATMENT` is declared, and the only accepted value assumes no VAT is charged.
3. **Region decision** (DB US-West, buckets EU): unchanged, owner decision.
4. **Token delivery after payment** (Fable founder-rescue #4): not addressed here, because it's a UX/product change outside this scope.
5. **Live dispute path**: proven offline only. Optional: one TEST browser payment with the dispute card `4000 0000 0000 0259`.
6. Single-replica assumption (JSON-blob orders, in-process locks): unchanged. Railway must stay at 1 replica.

## Operator interventions this pass (all owner-authorised infra/TEST actions, no DB repair)

- Applied migration `003_checkout_rate.sql` as the Postgres admin.
- Generated split operator tokens (local copies in `.railway-tools/operator-{read,action}-token.local.txt`) and deleted `OPERATOR_TOKEN`. Initially they were set through a PowerShell pipe that **injected a BOM**, which made every operator call return 400. Tokens were reset via bash, and startup validation now rejects such tokens (regression tested).
- Extended the Stripe **TEST** webhook `we_REDACTED` enabled events (livemode=false).
- Created one out-of-app TEST refund on order C (the reconciliation test).
- Created 3 unpaid TEST checkouts during the spoof probe; they self-expire after 31 min.
- Three `railway up` deploys.
- **Zero** `psql` data edits and **zero** manual state repairs.

## GO / NO-GO

**GO for external security / licence / privacy review.** All nine requirements are implemented and regression-tested. Everything that can be exercised in TEST mode was verified live on Railway/Postgres, including a full 250-fixture delivery and rerun. The one launch-relevant defect this pass found (the 300 s edge timeout) is fixed and proven live. Reviewers should target the unlocked configuration (`launch_config.py`, `decode_submission` with uploads on, live-mode adapter) rather than the default locked build.

**NO-GO for live customer traffic** until blockers 1–3 are closed.
