# Fable adversarial review — 18 September 2026

One bounded review/hardening pass. Scope: operator API, payment/refund/reconciliation, storage/deletion, recovery/state machine, deploy entrypoint, and the six priority documents. Offline doubles only — no provider calls, no deployment, no spend, no customer data. Probes: `evidence/fable-review/probes.py` (after-fix output: `probes-after.json`).

## Verdict

**CONDITIONAL GO**

The deterministic core is sound and fails closed; I could not produce payment-without-entitlement, entitlement-without-payment, duplicate fulfilment, cross-order access or an operator gate bypass through any code path in normal configuration. The conditions are not defects in what was staged — they are that **the staged system is not the system a customer would use**: the customer-upload lock and the TEST-only payment lock are both hard-coded, so the first paid order requires code changes that no gate has exercised (F1). Two real defects were found and fixed (F2, F3).

## Scorecard

| # | Dimension | Score | Conf. | Reason |
|---|---|---|---|---|
| 1 | Product correctness/determinism | 4 | High | Byte-identical cross-OS parity, pinned provenance verified per job, unknown exit codes fail. Max-size pack never delivered live (F4). |
| 2 | State integrity/idempotency | 4 | High | Atomic order+report+replay commit; keyed checkout/submit/refund; webhook dedupe re-reads provider state. JSON-blob read-modify-write is only safe single-instance (residual). |
| 3 | Recovery/failure containment | 4 | High | Four live fault scenarios credible; every uncertainty path retains obligation and closes a gate. |
| 4 | Security/data isolation | 3 | Medium | Capability model and key scoping hold; no worker sandbox/UID separation; unauthenticated checkout has no rate limit (F5). |
| 5 | Privacy/deletion controls | 3 | Medium | Raw deletion proven to zero incl. partial staging. Reports (invoice-derived) sit in US-West Postgres for 8 days; backups/versions unproven; region open. |
| 6 | Payment/refund/reconciliation | 3 | Medium | Strong in-band. Out-of-band Stripe events (disputes, dashboard refunds) are invisible to the ledger (F7); failed refund has no automated exit. |
| 7 | Financial/action authority boundaries | 4 | High | No reopen/limit/policy tool; verify cannot be weakened; refund bounded to amount paid. No refund rate/policy cap (F6). |
| 8 | Observability/auditability | 3 | Medium | Append-only gate audit and event ledger exist, but neither is readable through the operator API; HTTP logging fully disabled. |
| 9 | Managed-agent/operator-API readiness | 3 | Medium | Control verbs sufficient; read surface too thin to diagnose exceptions without DB access (see below). |
| 10 | Overall narrow-pilot technical readiness | 3 | Medium | Core ready for external review; pilot needs the F1 unlock pass first. |

## P0/P1/P2 findings

No P0.

**F1 — P1 — The reviewed build cannot serve a paying customer; the unlock is unexercised code.**
Evidence: `worker.decode_submission` rejects any byte not in the public-corpus hash allowlist (`customer_uploads_disabled_public_fixture_required`); `Service.__init__` refuses to start if `customer_uploads` or `live_payments` is true; `StripeSandbox` refuses non-`sk_test_` keys; `validate_checkout`/`verify_event`/`record_refund` require `livemode is False` and zero tax. There is no configuration path to a real order — only source edits to intake and every payment check.
Consequence: all staging evidence (incl. fault scenarios) covers a build that differs from the pilot build in exactly the two highest-risk surfaces: hostile-input parsing and live money. First real customer becomes the first test.
Fix status: not fixed — unlocking is an owner authorisation, and out of this review's authority. Documented.
Residual: needs one bounded acceptance pass on the unlocked build (non-allowlisted fixtures through the full live path in TEST mode; then live-mode checks + tax). This should be what the external security reviewer tests.

**F2 — P1 — Deploy entrypoint silently fell back to free simulated payments. FIXED.**
Evidence: `railway_app.build_provider` returned `DurableOfflineStripe` whenever `STRIPE_TEST_KEY` was absent/malformed. In that mode the public server exposes unauthenticated `POST /simulate-payment/cs_test_<order_id>`; the buyer knows their order id. Probe: provider `simulated: true` with no key.
Consequence: one missing/renamed env var on a public deployment = entitlement without payment for anyone, with no gate closing.
Fix: simulator now requires explicit `XREG_ALLOW_SIMULATED_PAYMENTS=1`; otherwise startup raises. Staging has the key set, so it is unaffected. Not redeployed.
Residual: none material. (The random `WEBHOOK_SECRET` fallback is fail-safe: webhooks are rejected.)

**F3 — P2 — A failed rerun attempt masked the delivered report. FIXED.**
Evidence: report download without `run` used "latest row". Failed-attempt packs (exit 2) are stored as run `-attempts`, and the HTTP route has no `run` parameter. Probe: deliver run 1 → rerun attempt exits 2 → `GET …/results.json` returned the failed pack; the delivered £299 evidence was unreachable over HTTP until a later success.
Consequence: paid, delivered fulfilment disappears from the customer's view; looks like data loss; support/refund trigger.
Fix: default selection now prefers the highest delivered run, falling back to the latest failed-attempt pack only when nothing was delivered (portable SQL, no adapter dependence). Not redeployed.
Residual: after the rerun, run 1 is still not addressable over HTTP (only latest delivered). Acceptable; customers are told to download.

**F4 — P2 — A maximum-size pack has never completed on the live stack.**
Evidence: STAGING_GATE §F — live deliveries were 2-pair payloads (~23 s). Both 250-pair live submits were fault-injected; scenario D shows 231 s just to stage 500 objects (sequential put+get, EU bucket ↔ app). 1,000 validations then follow inside one synchronous HTTP request. `job_deadline` is set before staging, but the executor's 1,200 s timeout starts after it.
Consequence: the advertised ceiling may exceed platform request limits or the deadline; a customer at 250 fixtures could burn attempts into an auto-refund.
Fix status: not fixed (needs one live run, not code). Residual: run one full 250-pair TEST delivery + rerun before selling the 250 limit.

**F5 — P2 — Anonymous checkout flood blocks all sales for 24 h, repeatably.**
Evidence: probe — 100 unauthenticated `POST /checkout` calls with distinct keys → `spending_gate`; each holds a reservation for 24 h and creates a Stripe session. No rate limit; cannot be cleared by the operator (no tool releases unpaid reservations early).
Consequence: trivial sales DoS; a manager can see it but not resolve it → owner.
Fix status: documented, not fixed (rate limiting/edge control is new hardening, not a minimal correction). Residual: acceptable for a 3-customer pilot with direct links; put an edge rate limit in front before any advertising.

**F6 — P2 — Refund authority has no policy, for customer or operator.**
Evidence: probes — customer self-refund succeeds after both runs delivered and reports stay downloadable; operator `refund` refunded 5/5 paid orders with no cap, window or reason.
Consequence: product is effectively free to any buyer who asks; a confused manager can refund the whole book. Loss is bounded to revenue collected plus non-returned Stripe fees — never beyond.
Fix status: not fixed. LAUNCH_PACKET records the unconditional refund as deliberate draft policy, so this is an owner decision, not a bug. Residual: accept for the pilot; decide the refund window before scaling.

**F7 — P2 — Ledger is blind to out-of-band Stripe money movement.**
Evidence (code inspection): webhook handles only `checkout.session.*` and `refund.*`; `record_refund` requires `metadata.order_id`. Disputes/chargebacks are never observed; a dashboard refund has no metadata → the event 409s on every Stripe retry, and `reconcile_ledger` still sees the session as `paid`. A later automatic refund of that order would be rejected by Stripe and sit `requested` forever → reservation `reconciliation_failed` → **no gate can ever verify** until someone edits the DB (same class as the `order_T` psql delete in STAGING_GATE §D).
Consequence: refund state diverges from Stripe; permanent wedge needing DB admin.
Fix status: not fixed — needs a small design decision (observe `charge.refunded`/`charge.dispute.*`, and an operator "mark externally resolved" verb). Residual: acceptable for three customers **if the owner never refunds from the Stripe dashboard**; disputes remain owner work.

**F8 — P2 — Gate reopen evidence is generic, not cause-specific.**
Evidence: probe — delivery gate closed by `execution_failed` with a permanently broken executor; `verify delivery` passed and reopened it. None of the six checks exercises the validator.
Consequence: a manager can reopen sales while delivery is broken; next buyer pays, fails, gate recloses, buyer auto-refunds. Money-safe, but a paid-then-refunded customer loop.
Fix status: documented. Cheapest later fix: require a fresh passing `verify_acceptance` record before reopening `delivery`. Residual: acceptable for pilot.

## Hardening performed

| Change | File | Test evidence |
|---|---|---|
| Simulator only on explicit opt-in; otherwise deploy startup fails | `pilot/railway_app.py` `build_provider` | `test_deploy_never_silently_falls_back_to_simulated_payments` — failed before, passes after |
| Default report selection prefers delivered runs over failed-attempt packs | `pilot/service.py` `report` | `test_failed_attempt_pack_never_masks_delivered_report` — failed before, passes after; `test_failed_attempt_pack_served_when_nothing_delivered` |

New tests: `pilot/test_fable_review.py`. Full suite (all ten modules): **139 passed, 5 skipped** (baseline before changes: 136 passed, 5 skipped). Nothing was deployed; staging still runs `REDACTED-DEPLOY-ID-1`. The new report query has run on SQLite only — it is standard SQL, but confirm once on Postgres at the next deploy. `verify_acceptance` does not yet include the new test module.

## Founder-rescue risks

1. **Permanent reconciliation wedge** (F7 and any paid order whose provider object becomes unreachable): every gate stays closed until a DB admin intervenes. Already happened once in staging.
2. **Stripe TEST key expires 2026-12-15**; any key rotation, webhook-secret change or redeploy is owner-only Railway work. There is no CI/deploy path a manager can use.
3. **Disputes/chargebacks, failed/`requires_action` refunds, lost-both-tokens** — all end at the Stripe dashboard or DB.
4. **No customer notification channel.** Tokens are shown once, pre-payment, in a browser; `/success` grants nothing. A buyer who closes the tab has paid with no access and no support route other than contacting the owner.
5. **Checkout flood** (F5) — not clearable through the operator API.
6. **Verify contention** with the 1 s recovery tick needs patient retry; fine for an agent, noisy in the audit log.

## Managed-agent readiness

**Sufficient existing tools:** `health`, `spend`, `ruleset`, `/operator/state` (paginated order states), `pause`/`pause_acquisition`, `verify` (only reopen path, unweakenable, audited), `retry` ledger/reservations/refunds, `refund`, four fixed `maintenance` codes returning synchronous evidence (covers "verify completion of delegated work"). Forged/extra-argument actions are rejected; customer tokens do not work on operator routes.

**Genuinely missing:**
1. Read access to `gate_audit` and recent `events` (codes only). Without it the manager sees *that* a gate closed but not why, and would need DB access — the thing it must not have.
2. `error`, `attempts`, `runs` in the `/operator/state` order allowlist (enumerated codes, no content) — needed to tell "customer input error" from "system failure".
3. A verb to resolve an externally settled refund / unreachable provider object (F7), audited, so wedges do not need psql.
4. Separate read-only vs. action operator tokens. Today one token reads state and issues refunds.

Not needed now: improvement-task creation, real spend ingestion, ruleset update tooling.

**A manager must never receive:** Railway token/SSH, Postgres admin or `xreg_api` credentials, `STRIPE_TEST_KEY`/any Stripe key or dashboard login, `WEBHOOK_SECRET`, bucket keys, customer access/recovery tokens, report/fixture content, or write access to `policy.json`/source/deploy.

## Residual risks

**1. Pre-customer blockers**
- F1 unlock pass (customer uploads + live payments/tax) on the actual pilot build.
- F4 one full-size live delivery + rerun.
- Deploy F2/F3 fixes; confirm report query on Postgres.
- Region decision (reports are invoice-derived and currently land in US West).
- External security/licence/privacy review; seller identity/terms (already known).
- A way for a buyer to get tokens after payment (founder-rescue 4) — at minimum, explicit "save these before paying" UX acceptance; ideally tested as part of F1.

**2. Acceptable narrow-pilot risks**
- F5, F6, F8; no worker UID/network sandbox (test fixtures only); single bucket-wide key; synchronous job-in-request; single-process locking; disputes handled by owner; verify contention.

**3. Later scaling risks**
- Orders stored as JSON blobs with whole-table scans on a 1 s tick and no row locking — unsafe with >1 replica and during deploy overlap (stale overwrite of a concurrent webhook is possible in the overlap window).
- `reconcile_ledger` holds the global lock across up to 100 sequential Stripe calls.
- `max_concurrent_jobs=1`; `max_orders=100` lifetime-ish admission model.
- Frozen single ruleset pair; no ruleset update pipeline.

## Final answers

1. **Ready for external security/licence/privacy review?** Yes. Give reviewers the F1 unlock diff as the primary target; reviewing the locked build alone would miss the real attack surface.
2. **Technically ready for a three-customer £299 pilot, subject to that review?** Conditionally — after the F1 unlock acceptance pass, one full-size live run (F4) and deploying the two fixes. No redesign is required.
3. **Structured enough for Grok Bot / Claude Managed Agents via bounded connectors?** Yes for control (the authority boundary is genuinely enforced in code, not prompt). Not yet for diagnosis: add the read-only audit/event/error surface and the wedge-resolution verb first, or the manager will escalate to the owner for every exception.
4. **Largest remaining technical risk of founder intervention?** A permanently unreconcilable order (F7 class): one Stripe-side anomaly keeps every gate closed and only a DB admin can clear it.
5. **What should NOT be built before willingness to pay is tested?** Manager integration, extra rulesets/profiles, subscriptions, 2×2 attribution, acquisition/ads tooling, worker sandboxing/per-tenant IAM, multi-replica/queue architecture, report-bucket wiring, spend ingestion, improvement-task pipelines. Build only: the F1 unlock, the F4 run, and (cheap) the operator read surface.
