# Privacy Notice — Release Regression Pack

**Website:** https://xregcheck.com — **Support / complaints:** support@xregcheck.com
**Controller:** [Seller name], sole trader, United Kingdom (a sole
trader, acting as the controller for the data described below).

This notice explains, in plain terms, what personal data we handle when you
use https://xregcheck.com, why we handle it, where it lives, and the choices
you have. It is written to work for both the EU GDPR and the UK GDPR, which
can both apply to our sales; where the two regimes differ in detail, we take
the more protective approach.

## 1. What we process and why

- **Order and access records** — the details needed to take your order,
  authenticate your report download, and honour your one rerun and refund
  rights. We keep these because we must be able to deliver what you paid for
  and account for the sale.
- **The XML test fixtures you upload** — processed solely to run your
  validation job and produce your report. These are your test documents; we
  make no other use of them.
- **Generated reports/evidence** — the JSON, JUnit and HTML outputs of your
  job, available to you by authenticated download.
- **Payment metadata** — your payment is handled by Stripe's hosted checkout.
  We receive from Stripe only what is needed to match a payment to an order
  and process refunds. We never see or store your full card details.
- **Support correspondence** — if you email support@xregcheck.com (including
  refund or privacy requests), we keep that correspondence so we can handle
  your request.
- **Minimal technical logs** — needed to operate the service securely and to
  diagnose faults.

## 2. No AI or model processing of your content

**No AI/model provider is in the customer path.** Validation runs as a local
software subprocess on our own infrastructure. No model ever sees your
invoice content or your reports, and your content is never sent to any
AI/model service.

## 3. Where your data is hosted

- Our application, database, and object storage run on **Railway in the
  EU West region (Frankfurt)** — all customer-path hosting has been in the EU
  (eu-west) since 21 September 2026.
- Payments are processed by **Stripe**, a company operating from the UK, as
  our payment processor.

## 4. Subprocessors

The only third parties with access to customer-path data, acting as our
subprocessors/processors, are:

| Subprocessor | Role | Location |
|---|---|---|
| Railway | Application, database and object storage hosting | EU West (Frankfurt) |
| Stripe | Payment processing (hosted checkout) | UK |

We do not use any other subprocessor for customer data, and we will update
this notice before adding one.

## 5. How long we keep data

- **Your uploaded fixtures are deleted when each job terminates** — they are
  not retained after your validation job has finished.
- **Report evidence logically expires after 8 days.**
- **Order, accounting and support records** are kept only as long as needed
  to account for the sale, handle support/refund requests, and meet
  bookkeeping and tax obligations.
- If a shorter retention for any category is workable, the shorter period
  applies.

## 6. Your rights

You can ask us, at any time, via **support@xregcheck.com**, to:

- tell you what personal data we hold about you and give you a copy;
- correct data that is wrong;
- delete data where we no longer need it or where deletion is required;
- restrict or object to particular processing; and
- receive your data in a portable form where that applies.

We will respond and act within a reasonable time, and we will tell you if we
cannot do what you ask and why.

## 7. Complaints

If you are unhappy with how we have handled your personal data, contact
**support@xregcheck.com** and we will investigate. You also have the right to
complain to a data protection supervisory authority — in the EU member state
where you are established, or to the UK supervisory authority for UK-matters.
We will help you identify the right authority if you are unsure.

## 8. Changes to this notice

We may update this notice as the service evolves. The version in force is the
one published at https://xregcheck.com when you used the service.

---

## TODO-LAWYER

1. State the precise lawful basis per data category (contract performance,
   legal obligation, legitimate interests) — currently implied by purpose;
   needs a confirmed mapping (L4).
2. Confirm UK GDPR vs. EU GDPR overlap wording and whether the notice should
   name the UK supervisory authority explicitly (no authorities are named
   here to avoid citing instruments the solicitor has not confirmed).
3. Confirm whether a data processing agreement or other contractual
   instrument is required with Railway and/or Stripe as drafted, or whether
   platform terms suffice (L4).
4. Confirm transfer-impact wording for Stripe (UK processor serving an
   EU-hosted, EU-customer service) — the notice currently only discloses
   location and role.
5. Confirm exact retention periods for order/accounting/support records
   ("as long as needed" is deliberately conservative and unquantified).
6. Confirm whether the "8-day evidence expiry" and "deletion at job
   termination" statements need any carve-out for backups or fault
   investigation, and align wording with actual backup behaviour.
7. Confirm the fixture-deletion commitment is compatible with the refund and
   rerun windows (inputs deleted at job termination while reruns use the
   buyer's re-supplied fixtures — verify operationally before publication).
8. Confirm whether a specific response timeframe for rights and complaints
   requests should be stated, instead of the current "within a reasonable
   time" (deliberately unquantified pending review).

Drafted with automated assistance and pending review by a qualified solicitor. Not legal advice.
