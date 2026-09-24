# Slot A — live dispute drill (Stripe TEST, EU staging) — 21 September 2026

**Verdict: PASS after one real defect was found and fixed.**

Deployment under test: `REDACTED-DEPLOY-ID-7` (xreg-api, Railway eu-west, Postgres).
Fix tests: `pilot/test_preprod.py::LiveDisputeRaceTests` (full suite 170/5).

## Attempt 1 — found a live defect (the drill working as designed)

Owner paid order `order_V` with dispute card `4000 0000 0000 0259`. Event
ledger: `charge.dispute.created` + `charge.dispute.funds_withdrawn` were
processed **0.4 s before** `checkout.session.completed` → `_sync_by_payment_intent`
found no order carrying the PaymentIntent yet → both events recorded
`unmatched_provider_event`. The disputed order stayed `paid / awaiting_inputs`
with all gates healthy for 20+ minutes — a buyer with withdrawn funds could
have submitted and received delivery. The offline doubles never modeled this
event ordering (their dispute payloads embed the PaymentIntent and are
delivered after payment).

## Fix (three layers, deployed `REDACTED-DEPLOY-ID-7`)

1. **Race closed at source** — `checkout.session.completed` handling now calls
   `sync_payment_state` immediately after `payment_verified` (transient
   provider failures defer to the sweep via `payment_state_deferred`).
2. **Charge-only dispute events** — resolve PaymentIntent via
   `payment_intent_for_charge` (`charges/{id}` on the live adapter).
3. **Sweep backstop** — `reconcile_reservations`' paid branch now converges
   every paid order on provider money state each pass (~5 min cadence):
   disputes and out-of-band refunds converge **with no webhook at all**.

## Live proof #1 — backstop convergence (no webhook, no operator action)

After deploy, the wedged order from attempt 1 self-healed, ledger events 9–11:
`dispute_status needs_response` → `gate_closed dispute_observed` →
`purchases_paused`. Sales resumed only via audited `verify acquisition`
(passed). The disputed order remains blocked from fulfilment.
→ `backstop-convergence-live.json`

## Live proof #2 — the fixed race (fresh payment, same card)

Order `order_W`: dispute observed **at payment time**, acquisition gate
closed, order never delivered; `verify acquisition` passed and reopened.
Automated assertions: `PASS dispute_observed / acquisition_gate_closed /
order_not_delivered / audited_reopen_acquisition`.
→ `drill-1790010951.json`

## STATE.md gap #2 status: **CLOSED** — live dispute path proven on staging.
