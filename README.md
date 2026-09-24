# XRechnung Release Regression Pack

It was a hosted check that showed which XRechnung test invoices validate differently under two releases of the German e-invoicing rules. The official KoSIT validator decided every result, not a model. Uploading your own invoices was built but never switched on, so the public site accepted only a frozen set of public test invoices.

The comparison was the easy part. Most of the work went into taking money safely: Stripe checkout, refunds and disputes re-read from Stripe before any payment decision, and gates that close when something is uncertain and reopen only after a full re-check. I killed the validator mid-run on staging to prove it.

I priced it at £299 and never sold one. A business review, run in a separate AI model session, found no buyer. The market research behind it judged that a customer could script the same comparison against the free validator, so I shut it down on 25 September 2026.

I built it on my own, with AI coding agents writing most of the code. `docs/` has the reviews as they were written. The project is archived and not maintained; this is a cleaned export of the private repository, without credentials, account identifiers or its git history.

xregcheck.com is offline, so links to it in this repository are historical.

## What it was

A hosted regression checker for the German **XRechnung** e-invoicing format:
a buyer would submit up to 250 XML test fixtures and receive a comparison of
how those fixtures validate under two pinned XRechnung ruleset releases,
using the official **KoSIT XML Validator 1.6.3** — never a model — as the
sole source of truth. Output was delivered as JSON, JUnit XML and an HTML
report. It was priced as a **£299 one-off** ("Release Regression Pack")
through a hosted Stripe checkout.

The build-out was run as an **agent-operated pilot**: an Anthropic model
session planned and reviewed the work, execution was delegated to headless
coding-agent runs in git worktrees, and the operational surface (pausing
sales, verifying gates, issuing refunds, reading aggregate state) was
designed from the start to be exercised by a bounded HTTP API rather than
by a human with infrastructure credentials. `docs/engineering/` and
`docs/postmortem/` are the record of that process, including an
adversarial review by a separate AI model and the eventual go/no-go decision.

## Architecture overview

- **Python HTTP service with no web framework.** `pilot/server.py` is built
  on the standard library's `http.server`. The deployed service also uses
  `psycopg` for Postgres and, when object storage is configured, `boto3` for
  S3-compatible storage. `pywin32` is used only for Windows process
  containment.
- **Supervised validator subprocesses.** `pilot/worker.py` and
  `pilot/process.py` run the pinned, unmodified KoSIT validator as a
  contained child process — a new POSIX session/process group plus
  `PR_SET_PDEATHSIG`, or Windows job objects, depending on OS — with a credential-free,
  allowlisted environment, output/size bounds and pinned-artefact hash
  verification (`worker.verify_pinned_core`) before every run.
- **State.** `pilot/postgres_state.py` (Railway Postgres in production) and
  `pilot/state.py` (SQLite locally) hold orders, gate state and an
  append-only gate-audit ledger; `pilot/migrations/` are the corresponding
  schema migrations.
- **Payments.** `pilot/payments.py` wraps Stripe hosted Checkout
  (`StripeSandbox`, TEST and live modes, with an era-routing wrapper so
  TEST-era orders kept reconciling after the live cutover) plus an offline
  double (`OfflineStripe`/`DurableOfflineStripe`) used for fault-injection
  tests. `pilot/launch_config.py` fail-closed-gates any path to live
  payments behind explicit environment acknowledgements. The live money
  path was proven once end to end: a real £299 test purchase of my own through the
  customer flow, a signed live webhook, token delivery, and an operator
  refund that reconciled cleanly.
- **Webhook-verified, provider-as-source-of-truth reconciliation.** Payment
  and refund state is never client-asserted: a Stripe-signed webhook or a
  periodic reconciliation sweep re-reads the provider (Checkout session,
  PaymentIntent, refunds, disputes) before any payment decision, per
  `docs/engineering/PREPROD_GATE.md` and `docs/engineering/
  FABLE_ADVERSARIAL_REVIEW.md`.
- **Refund and dispute state machines.** `pilot/service.py` implements
  bounded customer- and operator-initiated refunds, retry with a capped
  number of provider-object recovery attempts, and gate closure on
  unreconciled state.
- **Audited fail-closed gates.** Safety/acceptance gates close on any
  uncertain outcome and can only be reopened by `verify_gate`, which always
  re-runs the full deterministic verification — there is no manual
  "reopen" or "skip" primitive anywhere in the code.
- **A bounded operator HTTP API for AI-agent operation.** `pilot/operator.py`
  exposes a small, fixed verb set (`health`, `spend`, `pause`, `verify`,
  `refund`, a handful of `maintenance` codes, and paginated read-only views)
  behind split read/action tokens, deliberately excluding raw
  Railway/Postgres/Stripe credentials, webhook secrets, bucket keys or
  report/fixture content from the surface an agent can ever reach — see
  `docs/engineering/TENANT_ISOLATION.md` for the credential-boundary map.
- **Fault-injection drills.** `docs/engineering/STAGING_GATE.md` §F and
  `evidence/staging-gate/fault-injection-20260917/` record four live
  scenarios: the validator JVM was SIGKILLed mid-run, the service was
  restarted during input staging, the process crashed in the report phase,
  and a job hit its deadline. They were driven against the deployed staging
  service to confirm every gate fails closed and reopens only through the
  audited path.

## Running the local sandbox

```powershell
python -m pilot.server --state .pilot-demo-1 --port 8765
```

Then open `http://127.0.0.1:8765`. See `pilot/README.md` for the full
authenticated workflow (checkout → submit → results) and its requirements;
in short, this needs Python 3.13, the `pywin32`/`psycopg`/`boto3` packages
in `requirements.txt`/`pilot/requirements.txt`, and — only if you want the
real KoSIT validator to run rather than exercising the HTTP/payment
surface alone — the pinned Java + validator bundles described below. The
server binds to loopback only and simulates Stripe by default; nothing in
this sandbox talks to a live payment account.

### Obtaining the pinned third-party bundles

This repository does **not** vendor the KoSIT validator, the XRechnung
ruleset bundles, or the Java runtime it validates with (see `NOTICE`).
`setup.py` downloads exactly the pinned artefacts named in
`manifests/downloads.json`, verifies each against its recorded SHA-256
hash, and extracts them into `.deps/` (git-ignored):

```powershell
python setup.py
```

`manifests/provenance.json` records what was pinned (KoSIT Validator
1.6.3, XRechnung configuration bundles, the exact Java build) and why, and
`licences/` retains the upstream licence/NOTICE text for each pinned
component. The scripts that originally *discovered* and recorded those
pinned versions (`research_inputs.py`, `pin_provenance.py` in the private
repository) are not part of this export; `setup.py` and `manifests/` are
the reproducible, current-state path.

## Running the tests

```powershell
python -m unittest -q pilot.test_pilot pilot.test_finance_recovery pilot.test_object_storage pilot.test_operator_api pilot.test_gate_recovery pilot.test_remediation pilot.test_remediation2 pilot.test_fable_review pilot.test_preprod pilot.test_review_remediation pilot.test_benchmark_compat test_xreg test_location_matching pilot.test_legal_pages pilot.test_dual_mode pilot.test_watchdog pilot.test_checkout_refunds
```

The last recorded run of this 17-module suite was on the private working
tree on 24 September 2026: **237 tests, 232 passed and 5 skipped** (evidence
log at the end of `docs/postmortem/business-review.md`).
Some tests that exercise the real KoSIT validator need the pinned
dependencies above (`.deps/`); the rest run against SQLite/offline doubles
with no external dependency.

## Why it was archived

Short version: the product never found a buyer. The market research behind
the business review judged that the underlying task — diffing validator
output across two ruleset releases — is something a target customer can
script against the free, open-source KoSIT validator themselves. See:

- `docs/postmortem/business-review.md` — the go/no-go review, run in a
  separate AI model session, that led to the shutdown decision (product,
  legal, payments and channel findings).
- `docs/engineering/FABLE_ADVERSARIAL_REVIEW.md`,
  `docs/engineering/PREPROD_GATE.md`, `docs/engineering/STAGING_GATE.md`,
  `docs/engineering/TENANT_ISOLATION.md` — the technical hardening and
  acceptance record for the parts of the system that *did* ship, which is
  most of what's worth reading in this repository.

---

Author: [@jdsteel61](https://github.com/jdsteel61)
