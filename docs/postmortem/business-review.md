# Independent business review — Release Regression Pack

**Date:** 24 September 2026 · **Reviewer:** Opus 5.5 session (independent of the
implementer/advocate role)
**Scope:** working tree on branch `ui-polish` (HEAD `19fa632` + uncommitted
checkout/refund changes) compared with what `https://xregcheck.com` serves today.
**Not legal, tax or accounting advice.** §7 lists the questions for a qualified
professional.

### How evidence is labelled

| Tag | Meaning |
|---|---|
| **[LIVE]** | Observed on the public site today (read-only GET requests and one browser view, 24 Sep ~14:37Z) |
| **[TREE]** | Read in the working-tree code or docs (file:line) |
| **[PROBE]** | Throwaway local probe against the working tree (OfflineStripe, temp state; repo untouched). Script: session scratchpad `probe_review.py` |
| **[SUITE]** | Full 17-module suite on the working tree: **237 passed, 5 skipped**, 155 s (24 Sep) |
| **[DOC]** | A design or state document in the repo; a claim, not proof |
| **[EXT]** | External primary source, accessed 24 Sep 2026 (URL given) |
| **[INF]** | My inference or estimate |

### Deployed revision vs working tree

The live site matches **HEAD `19fa632`**:
- The live `/style.css` is byte-identical to HEAD's stylesheet [LIVE].
- The template head matches.
- `/app.js` is the *old* script (no buyer-type or consent fields).
- The four legal pages are byte-identical to HEAD, not to the working tree.

So none of the checkout/refund/legal changes under review is deployed. The live
landing shows the **paused** banner with the button disabled; `/health` is
200 [LIVE].

---

## 1. Executive decision

| Question | Verdict | What decides it |
|---|---|---|
| **(a) First outside sale** | **NO-GO today → CONDITIONAL GO** once the conditions below close | See below |
| **(b) Customer uploads** (`XREG_CUSTOMER_UPLOADS=true`) | **CONDITIONAL GO**, coupled to (a) | See below |
| **(c) Paid Ads** | **NO-GO** | See below |

**(a) First outside sale**

1. **The product cannot do its job for a buyer today.**
   - Intake accepts only the frozen public corpus, yet the hero promises
     "Your own test fixtures" (F2).
   - After payment, buyers are told to read `pilot/README.md`, which sits in a
     private repo (F3).
   - The live flow calls itself "simulated" and "sandbox" at the payment
     moment (F4).
2. **The offer is stale.** The "new" side of the only pair
   (`v2026-01-31`) was superseded by **`v2026-08-31` on 2 Sep 2026**. That
   later step is where the verdict-changing rules sit. KoSIT also says bugfix
   bundles are not mandatory (F1).
3. **Trader identity is missing.** Terms and refund still carry address
   placeholders. The legal pages are served as raw markdown, with the
   internal TODO-LAWYER lists visible to the public (F5).
4. **The new consumer option adds exposure the business has not priced**
   (F6, §7). The working tree adds a "Buying as: consumer" path.
   - **VAT:** an automated validation report looks like an EU
     "electronically supplied service". For a non-EU seller that means EU VAT
     is due from the **first** EU consumer sale, with no threshold, so the
     price's "no VAT" line becomes wrong for that buyer.
   - **German consumer rules:** consumers keep their home-country rules
     (Rome I art 6). For Germany that includes an electronic withdrawal button
     required since 19 Jun 2026 (§356a BGB).
   - **EU consumer information:** EU consumer law adds a mandatory phone
     number.
   - Nothing in this product needs consumers.

**Conditions for (a):** F1 decision, F2–F5, the F6 decision, F7, F8, F10.
Then a **hand-held first sale to a buyer found by conversation** (not by
ads) who has confirmed the pair is useful to them.

**(b) Customer uploads** — without uploads there is nothing to sell (F2).
Conditions:
- Fix the privacy/processor posture: F9, plus a DPA/AVV answer.
- Measure the worst case on Railway at the byte limits (F11).
- Run one end-to-end job through the *public* docs with synthetic,
  non-corpus fixtures (F11).
- Put external uptime alerting in place (F12).

**(c) Paid Ads — NO-GO**

1. **The measurement design is obsolete.**
   - Since **15 Jun 2026** the Google Ads API rejects click-conversion uploads
     from integrations that had never uploaded before. Google now points to
     the Data Manager API [EXT, developers.google.com/google-ads/api/docs/conversions/upload-clicks].
   - That API cannot retract refunded conversions.
   - EEA/UK/CH uploads without consent signals may go unreported.
   - No capture code exists at all (F21).
2. **There is no demand evidence.**
   - No keyword volumes were obtainable.
   - Buyers are DACH, but the copy is English-only.
   - The pair is stale.
   - Fulfilment is API-only.
   - At £5–10/day the stop rules cannot tell a working campaign from a
     failing one (§5).
3. **The pause rules are not implemented.** The watchdog is dark and runs
   inside the process it watches. Any action-token holder can reopen sales
   (F12, F13).

**Revisit Ads** after all of the following:
- three or more paid customers from direct channels;
- a buyer-validated pair;
- own-ledger attribution in place.

---

## 2. Finding ledger

Severity: **Blocker** (must close before the named gate) · **High** · **Medium** · **Low**.
Each finding lists evidence, consequence, confidence and the smallest concrete fix.

### 2.1 Launch blockers

**F1 — Blocker (acquisition), High (first sale) — The only pair sold is one release behind and not mandatory.**

Evidence [EXT]:
- KoSIT configuration releases: `v2026-08-31` published 2 Sep 2026
  (CEN 1.3.16, validator 1.6.3); `v2026-01-31` published 5 Feb 2026.
  Source: <https://github.com/itplr-kosit/validator-configuration-xrechnung/releases>
  (verified directly).
- Schematron 2.6.0 (2 Sep 2026) raises BR-TMP-2 to fatal.
  `v2026-08-31` sets CII-SR-465/466 to error.
  Source: <https://github.com/itplr-kosit/xrechnung-schematron/releases/tag/v2.6.0>
- KoSIT, 5 Feb 2026: "Aktuell besteht keine Implementierungspflicht für
  Bugfix Releases".
  Source: <https://xeinkauf.de/aktuelles/xrechnung/xrechnung-bugfix-winter-202526/>
- XRechnung 3.0 stays in force until at least 31 Jul 2027.
  A 4.0 pre-version was published 15 Sep 2026; the final bundle is expected
  spring 2027.
  Source: <https://xeinkauf.de/xrechnung/versionen-und-bundles/>

[DOC] `LAUNCH_PACKET.md:11` states this is "a deliberately frozen historical
comparison … No other pair is sold". `pilot/policy.json` locks `pair`.

Consequence [INF]:
- A team upgrading today targets 2026-08-31, not 2026-01-31.
- The question the pack answers ("what changes if I move from 2025-07-10 to
  2026-01-31?") mostly concerns the CII BR-DE-25-b fix and CVD-only
  UBL-CR-646. That is narrow and past.
- Every future release repeats this problem, so a fixed-pair product has a
  shelf life.

Confidence: high on the facts; medium on buyer impact.

Smallest fix (owner decision):
- Re-pin the "new" side to `v2026-08-31`. Better still, sell
  "your current pin (2025-07-10 or 2026-01-31) → latest".
- This is a `policy.json` pair change plus a Gate-1-style parity/benchmark
  run and manifest pins. The `v2026-08-31` bundle is already downloaded in
  `.deps/` and held in the pinned licence inventory, so the engineering
  delta is small.
- Do not spend on acquisition before it.
- Treat XRechnung 4.0 (spring 2027) as the next natural trigger.

**F2 — Blocker (first sale) — With uploads off, a paying buyer can only validate public corpus files.**

Evidence:
- [LIVE] `/requirements`: "Only allowlisted public fixtures are accepted;
  customer-owned uploads are not yet enabled."
- [TREE] The hero at `pilot/server.py:103` says "Your own test fixtures".

Consequence: any sale today ends in a refund (with a £4.69 fee lost), or
a buyer who feels misled.

Confidence: high.

Fix: flip O5 only after F3, F9 and F11. Until then, keep acquisition paused.
The live banner already does this.

**F3 — Blocker (first sale) — No buyer-facing instructions exist for submitting fixtures.**

Evidence:
- [LIVE] The order section reads "Use the authenticated workflow in
  pilot/README.md to submit public fixtures". Also at [TREE]
  `pilot/server.py:205`.
- The `/success` page says "See pilot/README.md" (`:640`).
- The repo is private (`SESSION.md`, 23 Sep).
- There is no upload UI and no public API page. Support accepts enumerated
  topics only, with no free text (`service.py:1658-1678`).

Consequence: the buyer who has just paid £299 must hand-build base64 JSON
with a Bearer token and an Idempotency-Key, then poll on 202. They have no
documentation. This is the most likely abandonment → refund → dispute point.

Confidence: high.

Smallest fix:
- Serve `/docs`: the README workflow, rewritten for customers, with one
  worked `curl` example.
- Serve `/client.py`: a stdlib script that base64-encodes a folder of
  fixtures, submits, polls and downloads all three reports.
- Link both from the landing and the success page.

**F4 — Blocker (first sale) — Live mode still says "simulated" and "sandbox" at the payment moment.**

Evidence:
- [LIVE] `/app.js`: the link text is `'Open simulated payment page'`. Same in
  the working tree at `server.py:327`.
- [TREE] The `/success` HTML titles are "Payment verified — sandbox" (`:632`)
  and "No access or fulfilment — sandbox" (`:650`).
- [DOC] The 23 Sep preflight recorded "zero 'simulated' mentions"
  (`.cache/activation/preflight/2026-09-23/preflight.json`). It scanned the
  HTML but not the separately served script or the success page.

Consequence: a first-time buyer is told the payment page is simulated, just
before entering a real card. This is a trust failure and a dispute risk.

Confidence: high.

Fix:
- Choose these strings per mode.
- Add a test that fetches `/`, `/app.js`, `/requirements`, and `/success`
  (tokens and window-closed variants) under a live-mode provider double, and
  asserts no `simulat`/`sandbox`.

**F5 — Blocker (first sale) — Trader identity and the presentation of the legal pages.**

Evidence:
- [LIVE] `/terms` and `/refund` show `[PLACEHOLDER: business address]` and
  `[PLACEHOLDER: any registration/reference details]`.
- In the browser, the pages render as raw markdown source (`**`, `##`) in a
  monospace text view.
- They publish the full "TODO-LAWYER" lists, including "these terms
  currently grant no usage licence…".
- [TREE] `server.py:807-810` serves them as `text/markdown`.

Consequence:
- The seller cannot be contacted at a geographic address (see §7 for the
  duty).
- A procurement or finance reader sees an unfinished legal draft.

Confidence: high.

Fix:
- Office Clerk address, once website use is confirmed (owner).
- Render the pages as HTML.
- Strip the TODO-LAWYER blocks from what is served; keep them in the repo.

**F6 — Blocker (the decision before the first sale) — The consumer path in the working tree creates unpriced exposure. Recommend B2B-only.**

What the working tree adds [TREE]:
- A "Buying as: business / individual consumer" selector (`server.py:197-201`).
- Consumer consent checkboxes.
- `terms.md` §3/§6 and `refund.md`: a contractual 14-day consumer refund
  "even if early digital delivery affects a statutory cancellation right".
- Backend: `_refund_reason` gives 14 days for `buyer_type=='consumer'`
  (`service.py:1402-1410`).

Gaps [TREE]:
- **No confirmation on a durable medium is sent.** The service sends no email,
  and Stripe Checkout here sends only whatever receipt the dashboard is set
  to send.
- **No model cancellation form.**
- **No complete pre-contract trader information** (F5).
- **Buyer type is self-declared.** Anyone can tick "consumer" to get 14 days.
- **The acknowledgement contradicts the policy.** The consumer is asked to
  acknowledge losing a right that the policy then contractually restores.

Consequence: accepting EU consumers is likely to trigger the following
(§7 Q1–Q6):

- **EU VAT from the first sale [EXT].**
  - Services "automatically generated from a computer via the Internet …
    in response to specific data input by the recipient" are ESS
    (Reg. 282/2011 art 7 and Annex I).
  - B2C ESS are taxed where the consumer lives (Directive art 58). The €10k
    threshold covers only sellers established in the EU.
  - HMRC tells UK sellers to register for non-Union OSS or in each state
    (<https://www.gov.uk/guidance/the-vat-rules-if-you-supply-digital-services-to-private-consumers>).
  - EU business buyers who give **no VAT number** may likewise be treated as
    non-taxable (Reg. 282/2011 art 18).
- **CCR reg 16 confirmation.** It must be on a durable medium "before
  performance begins of any service". No such email exists. If the
  cancellation information is missing, the period can extend by up to 12
  months (reg 31).
- **Home-country consumer law (Rome I art 6).** Google Ads targeted at DE/AT
  is a strong "directing activities" factor (Pammer). German consumers bring
  §356a BGB (the withdrawal button, mandatory since 19 Jun 2026) and a
  mandatory phone number (CRD art 6(1)(c)).
- **Geo-blocking Reg. 2018/302.** It appears to reach third-country traders
  "operating within the Union". Refusing only *EU* consumers while accepting
  UK ones may be residence discrimination. A uniform business-only rule is
  not based on residence.

Confidence: medium on the code/design gap. The legal specifics are for
counsel; the research is primary-sourced but not advice.

Smallest fix (owner decision):
- **Sell to businesses only, everywhere.** Replace the selector with a
  required "I am buying for my business or trade" declaration.
- Show a clear "not available to consumers" line.
- Collect **and VIES-check** a VAT ID from EU buyers (Stripe
  `tax_id_collection`; §7 Q4).
- Keep the voluntary 7-day any-reason refund. Drop the 14-day consumer
  machinery.
- If consumers must stay, there are two routes:
  - Register for non-Union OSS, and add a confirmation email, a cancellation
    form, a phone number and the German withdrawal button.
  - Or use a merchant of record. Stripe Managed Payments accepts GB sellers
    of automated digital products, reportedly at about +3.5%; the code
    currently sends `managed_payments[enabled]=false`.

  Either route is an accountant-plus-counsel decision.

**F7 — Blocker (first sale) — The refund copy promises more than the backend enforces.**

Probes against the working tree:
- **[PROBE P2]** An email request received on day 6.9 and processed by the
  operator on day 7.1 → `REJECTED: refund_reason_not_eligible`.
- **[PROBE P3]** A consumer request made on calendar day 14, one hour later in
  the day than the purchase time → `REJECTED: refund_window_closed`.
  Windows are counted in seconds from `paid_at` (`service.py:1398-1410`), not
  as calendar days.

What the copy says [TREE `refund.md`]:
- "A request made before the deadline remains timely even if we process it
  afterwards."
- "We initiate eligible refunds promptly and within 14 days."

What's missing:
- There is no record of *when* an email request arrived.
- Nothing tracks the 14-day initiation deadline.
- If the provider fails, a refund stays `requested` and is retried every
  5 minutes. That emits only a `refund_provider_unavailable` event, with no
  alert (`service.py:1591-1603`).
- The watchdog is not deployed (F12).
- Email arrives only in the owner's personal Gmail, via Cloudflare routing,
  which the bot cannot read (F23).

Consequence: a timely request can be refused by the system, and no one is
told when the promised initiation deadline is at risk.

Confidence: high.

Smallest fix:
1. Make the **authenticated refund route** (`POST /orders/{id}/support
   {"topic":"refund"}`) the primary route in the copy. It is instant and
   timestamped. Keep email as a fallback.
2. Runbook rule: an email request received in time but processed late is
   refunded by the owner in the Stripe dashboard. The ledger already
   converges out-of-band refunds (`sync_payment_state`).
3. Count windows to the end of the UTC (or London) calendar day.
4. Watchdog alert when any refund has been `requested` for more than 72 hours.

**F8 — Blocker (DACH business buyers) — No invoice, VAT ID or billing address is collected at checkout.**

Evidence:
- [TREE] `payments.py:159-172`: the Checkout session sets only mode, card,
  line item, metadata and URLs. No `invoice_creation`, no
  `tax_id_collection`, no `billing_address_collection`, no
  `consent_collection`.
- [EXT] German business buyers account for UK-supplied services under the
  reverse charge (§13b UStG), so they need a proper invoice showing the
  supplier's name and address.
  Source: <https://www.gesetze-im-internet.de/ustg_1980/__13b.html>

Consequence:
- Finance teams cannot book the purchase cleanly, which is a procurement
  blocker. Every order becomes a manual invoice email to the owner.
- Without a VAT ID on file, an EU buyer "may" be treated as a non-taxable
  person (Reg. 282/2011 art 18), which pulls in the F6 VAT exposure.

Confidence: medium-high.

Fix (after F5): enable post-payment invoices plus tax-ID collection in
Checkout (check the Stripe Invoicing fee). Wording for an invoice from a
non-VAT-registered seller is §7 Q9.

**F9 — Blocker (uploads) — The privacy notice misstates hosting and subprocessors.**

Evidence:
- [TREE] `privacy.md` §3 says "Railway in the EU West region (Frankfurt)".
  [EXT] Railway EU West is **Amsterdam** (`europe-west4-drams3a`).
  Source: <https://docs.railway.com/reference/deployment-regions>
- §4 lists only Railway and Stripe. Support correspondence runs through
  **Cloudflare Email Routing → Gmail (Google)** (`SESSION.md` 22 Sep).
  Railway is a US company, and no transfer disclosure is given.
- §3 claims "object storage". [DOC] The S3 variables were found empty on the
  EU deployment on 21 Sep (`evidence/sprint0-review-pack/notes.md` §4). No
  record shows them restored, so fixtures run through container scratch only.
- The notice calls the seller the **controller** of uploaded fixtures.
  Business buyers will expect a processor posture and an Art. 28 DPA/AVV
  ([EXT] a competitor, e-rechnung-validator.de, bundles one).
- Upstream paperwork is incomplete [EXT]:
  - Railway's DPA (with SCCs and the UK Addendum) binds only once signed via
    DocuSign (<https://railway.com/legal/dpa>).
  - A free Gmail mailbox may lack processor terms. Google's
    "Workspace (Personal)" terms exist, but coverage is unclear.
  - The ICO fee is probably payable, since service delivery is not an
    exempt purpose (Tier 1 £52) (SI 2018/480).
  - An EU Art. 27 representative is arguable for business-only sales and
    likely if EU consumers are accepted.

Consequence: the notice is inaccurate on its face, and a DACH buyer's DPO
will stop the purchase.

Confidence: high (location); medium (roles).

Fix:
- Correct the text.
- Owner decision: **retire** object storage formally, or restore it.
  Recommend retire: less data at rest, and the drills already prove the
  scratch path.
- Publish a short DPA template, or state "synthetic data only — no personal
  data".
- Owner: sign the Railway DPA and pay the ICO fee.

**F10 — Blocker (cheap) — A buyer who returns to their tab after 31 minutes hits a dead end.**

Evidence:
- **[PROBE P5]** Same retry key, same payload, after the hosted session
  expired → `REJECTED: checkout_expired_use_new_key`.
- The working-tree script reuses the stored key for an identical payload
  (`server.py:322-325`), so every click in that tab fails.
- The live script reuses one key for *any* payload, which also produces
  `idempotency_key_payload_conflict`.

Consequence: a buyer who leaves to fetch a company card is locked out and
shown raw JSON.

Confidence: high.

Fix: on either error code, drop the stored key and retry once.

**F11 — Blocker (uploads) — The customer path has never run end to end with non-corpus inputs, and the worst case is unmeasured.**

Evidence:
- [DOC] The live smoke skipped optional step 6 (fixture submission)
  (`.cache/activation/smoke/smoke-summary.md`).
- The upload flip is proven only by a local test (`SESSION.md`).
- `LAUNCH_PACKET.md:52-54`: "worst-case byte-limit performance remain
  unknown".

Consequence:
- 5 MiB files, or 16 MiB in paired mode, could exceed the 1,200 s deadline.
- Every execution failure closes the delivery gate, which pauses all sales
  (`service.py:1002`).

Confidence: medium.

Fix, one TEST-mode session:
- A max-size synthetic pack submitted via `/client.py` (F3).
- Two submits at once, to observe `worker_busy_retry_later` handling.
- A rerun.
- Record the timings.

**F12 — Blocker (acquisition); owner-watch is acceptable for a single hand-held first sale — Monitoring is dark, in-process and narrow.**

Evidence [TREE]:
- `railway_app.py:101-140` starts the watchdog **inside xreg-api**, and only
  if `WATCHDOG_ENABLED=true`. It is off.
- `watchdog.py:101-134` checks only health, gates, the pending-refund count
  and the errors view. The design in `.cache/prompts/grok-bot-wiring.md`
  also called for:
  - `unmatched_provider_event`;
  - `reconciliation_failed`;
  - stuck refunds;
  - delivery deadlines;
  - checkout 5xx.
- It demands the **action** token, although `health` is in `READ_ONLY`
  (`operator.py:65`; `watchdog.py:112-113`).
- An in-process watchdog cannot report its own container's death.
- Alerts go to the same Gmail inbox as customer refunds.

Consequence: a closed gate (for example after one failed job) silently
pauses sales. A Railway outage or a stuck refund goes unnoticed.

Confidence: high.

Fix:
- A free external uptime check on `/health`, pushing to the owner's phone.
- Railway deploy-failure notifications.
- Watchdog on the read token only, with the missing checks added.
- Run it outside xreg-api (`xreg-ops` or the owner's machine).

### 2.2 High / Medium — close before scaling to ten customers or switching on Ads

**F13 — High (Ads, autonomy) — Anyone holding the action token can reopen sales.**

Evidence [TREE]:
- `verify_gate` (`service.py:485`) is the only reopen path. It checks
  technical health, not business authorisation.
- The owner's "sales paused pending Office Clerk" rests on the same gate the
  bot can reopen with `verify {gate: acquisition}`.

Consequence: once Ads are wired to "sales open", a bot or implementer can
restart spend and sales without the owner.

Fix: an owner-held hold, `XREG_SALES_HOLD` set as an environment variable,
that `verify` cannot override. The landing page and the Ads kill switch
should read it.

**F14 — Medium (latent blocker around order 90–100) — A lifetime order cap looks like an open shop.**

Evidence:
- **[PROBE P4]** Order 101 is refused with `spending_gate` while all four gates
  are healthy. `reserved` counts `spent` reservations forever
  (`service.py:661`; policy budget 20,000p ÷ 200p = 100). TEST drill orders
  already count toward it.
- `sales_open()` (`server.py:65`) looks only at gates, so the landing page
  says "Sales are open" while checkout refuses.

Fix: count only active reservations, or roll the budget monthly (owner
authorises the policy meaning). Make `sales_open()` reflect spend and
unpaid capacity too.

**F15 — Medium — Ten abandoned checkouts block every new buyer.**

Evidence: **[PROBE P6]** Ten unpaid checkouts from four IP addresses
(3/hour/IP limit) → the next genuine buyer gets
`checkout_capacity_busy_retry_later` for about 31 minutes
(`policy.max_unpaid_checkouts=10`, `service.py:657`).

Fix: raise the cap or shorten the TTL for unpaid sessions (owner policy),
and show the honest state on the landing page.

**F16 — Medium (commercial) — "Use and refund" is built in.**

Evidence:
- **[PROBE P1]** A refund requested mid-run returns `requested`. The job still
  finishes `delivered`, and the report **remains downloadable after the
  refund**. `report()` never checks refund state (`service.py:1066-1082`).
- The 7-day any-reason refund applies after full delivery of a consumable
  deliverable.

Consequence: the offer is effectively a free trial at a £4.69 cost to the
seller per use. It is also a cheap way to extract the report and then
dispute or refund.

Fix (owner decision):
- Keep the policy as a trust signal and accept the leakage.
- Or narrow it to "before first delivery, or if the report is defective or
  delivery fails".
- Either way, revoke report access once a refund is requested.

**F17 — Medium (autonomy) — Refund reasons are self-asserted, and the operator and implementer roles are blurred.**

Evidence [TREE]:
- `operator_request_refund` accepts `customer_request_within_7_days` for any
  in-window order, with no customer-originated request record.
- `pack_not_deliverable` now fires for any `job=='failed'`, including after an
  earlier delivery (`service.py:1444-1452`).
- [DOC] `ACTIVATION.md` Appendix gives the "agent" Railway CLI deploy access.
  That can read every service variable, including the live Stripe key. This
  contradicts the bounded-operator rule if the same agent is the standing
  business operator.

Consequence: money only ever returns to the payer, so there is no theft
path. But revenue decisions are discretionary for whoever holds the token.

Fix:
- The standing operator holds the operator tokens only.
- Deploys stay with owner or implementer sessions.
- Require an authenticated customer `refund_requested` event, or an
  owner-flagged email origin, before a self-asserted reason is accepted.

**Verdict on model-run operations [INF].** Through the bounded API, with the
**read** token, a model can safely do all of the following. It sees no
secrets, no report content and no money:
- monitor health and gates;
- read reconciliation and recovery;
- triage incidents;
- draft owner escalations.

With the **action** token it can also safely `pause`, `retry` and
`resolve_order`. It should not be trusted with the other two action powers
until the fixes land:
- `refund` — self-asserted reasons, until F17's customer-request binding
  exists;
- `verify acquisition` — can reopen sales, until F13's owner hold exists.

**F18 — Medium — No automated database backup or restore drill.**

Evidence [DOC]: `SESSION.md` 23 Sep: "No `pg_dump` on container + no CLI
backup verb → Postgres dump is an owner-dashboard item".

Consequence: Stripe keeps the money truth, but tokens, entitlements, rerun
state and refund intents live only in Postgres.

Fix: enable Railway backups (owner) and run one restore drill (agent).

**F19 — Medium — The terms and landing copy disagree with the code.**

Evidence [TREE]:
- **Rerun window.** Terms §1/§5 say one rerun "within seven days of your
  order". The code counts from **first delivery** (`service.py:943-944`).
- **Reruns by support.** Terms and FAQ say "contact support … for reruns",
  but support cannot rerun.
- **Silent auto-refund.** An order with no delivery is auto-refunded at
  `created+7d` (`service.py:1309-1310`). This is not disclosed, and no
  message is sent.
- **Two token pairs.** The buyer sees two different pairs: the raw checkout
  JSON in `<pre id="result">`, and the `/success` page.

Fix: copy edits, plus one "which token to keep" sentence (or stop printing
tokens in the checkout response on the page).

**F20 — Low — The accepted terms version is not recorded.**

Evidence: the terms say the version "published … when you placed it"
governs (§9), but the order stores only `terms_accepted: true`
(`service.py:666-671`).

Fix: store a SHA-256 of `terms.md` + `refund.md` on the order and in the
`checkout_agreement` event.

**F21 — High (measurement) — The first-party funnel cannot attribute or count reliably.**

Evidence [TREE]:
- **Page views are sampled.** Anonymous view events are written at most once
  per kind per 60 s (`server.py:369-382`).
- **Attribution is fixed.** The landing script always sends
  `source:'direct'` (`server.py:320`). The `search_exact_xrechnung` code
  exists but nothing can set it.
- **No Ads plumbing.** No `gclid`, spend import or refund-adjusted revenue
  view exists (grep: 0 matches in `pilot/`).

Fix, cheapest credible version:
- The ad's final URL carries `?src=gads_exact_v1`; the landing maps it to an
  enumerated `source`.
- Keep unsampled daily aggregate counters.
- Compute CPA = spend read from the Ads UI ÷ attributed *net* paid orders from
  the ledger.
- No upload to Google, no consent question.

**F22 — Medium — One failed job pauses all sales until someone runs `verify`.**

Evidence [TREE]: execution failure closes the delivery gate
(`service.py:1002`). `verify` legitimately needs retries (CLAUDE.md).

Consequence: this is safe by design, but availability depends on F12.

Fix: F12 alerts, plus a documented `verify` retry procedure.

**F23 — Medium — The refund email channel is a silent single point of failure.**

Evidence [DOC]: Cloudflare Email Routing → personal Gmail. Nothing monitors
deliverability, and the bot has no access.

Consequence: if routing breaks, timely cancellations are lost without trace.

Fix: F7(1) makes the in-product route primary. Send a monthly test email to
`support@`, noted in the digest.

**F24 — Low — Stale docstrings and states.**

Evidence [TREE]:
- `railway_app.py:8-10` says the validator "cannot run on Railway's Linux
  containers". It does; the canary runs there.
- `policy.json` `"mode": "local-public-sandbox"` is informational only.
- `pilot/README.md` still describes the loopback sandbox.

Fix: doc hygiene when F3 rewrites the buyer docs.

**F25 — Low — The notices page names the wrong CEN version and leaves EUPL source access open.**

Evidence:
- [TREE] `notices.md` §4 names "CEN distribution 1.3.16". The in-service
  new-side bundle `v2026-01-31` uses CEN **1.3.15**
  (`.deps/validator-configuration-xrechnung-v2026-01-31/CHANGELOG.md:21`).
  1.3.16 belongs to `v2026-08-31`.
- [EXT] EUPL-1.2 defines communication as including "providing access to its
  essential functionalities", and requires source access or a repository
  link
  (<https://raw.githubusercontent.com/ConnectingEurope/eInvoicing-EN16931/master/LICENSE.txt>).

Fix:
- List the exact in-service versions.
- Add upstream repository links for the EUPL artefacts. It's cheap and
  settles the likely duty.
- Counsel confirms (§7 Q13).

### 2.3 Speculative ideas (not required)

- **S1 — Time-boxed offers per release.** "Your pin → latest" now; an
  "XRechnung 4.0 readiness pack" in spring 2027. Each offer comes with a free
  public rule-diff write-up as content, e.g. "what changes verdicts in
  2026-01-31 → 2026-08-31: BR-TMP-2 fatal, CII-SR-465/466 error".
- **S2 — CI-shaped distribution.** A GitHub Action or `client.py` that runs in
  the buyer's CI and calls the API. It cuts setup burden, and the
  "regression" buyer lives in CI anyway.
- **S3 — DACH-native presentation.** A German landing page, an Impressum-style
  block, a DPA/AVV, a reverse-charge invoice and a EUR price point (e.g.
  €349). Only after interviews confirm DACH demand.
- **S4 — Invoice/bank-transfer option** for German buyers who won't use a
  card (owner time cost; see §4).

---

## 3. Journey and failure maps

### 3.1 Buyer journey (first-time, qualified buyer)

Columns: **Who acts** · **Abandonment or trust risk** · **Unowned obligation**.

**1. Discover the product**
- Who acts: buyer.
- Risk: no channel exists yet; the product is English-only with no German
  presence.
- Unowned: no acquisition owner.

**2. Landing page**
- Who acts: buyer.
- Risk: the hero promises your own fixtures, but live copy says public only
  (F2). The pair is stale (F1).
- Unowned: none.

**3. `/sample` report**
- Who acts: buyer.
- Risk: 3.2 MB HTML with raw JSON in the summary. Useful but heavy on
  mobile [LIVE].
- Unowned: none.

**4. `/terms`, `/refund`, `/privacy`**
- Who acts: buyer (and the buyer's finance/DPO).
- Risk: raw markdown, placeholders and TODO-LAWYER (F5). Wrong hosting city
  (F9). No DPA (F9).
- Unowned: counsel review, deferred.

**5. Order form**
- Who acts: buyer.
- Risk: `product` field is pre-filled "sample-product". Corpus
  attestation. Buyer type and consent (F6).
- Unowned: none.

**6. `POST /checkout`**
- Who acts: buyer.
- Risk: the response dumps raw JSON with tokens. The link says "simulated"
  (F4).
- Unowned: none.

**7. Stripe hosted checkout (expires after 31 min)**
- Who acts: buyer.
- Risk: expiry leads to a dead end in the same tab (F10). Card only; no
  invoice or VAT ID (F8).
- Unowned: invoice issuance.

**8. `/success`**
- Who acts: buyer.
- Risk: "sandbox" title. A second token pair. Points to a private README
  (F3, F4, F19).
- Unowned: no receipt or confirmation email from the service.

**9. Prepare the submission**
- Who acts: buyer (engineer).
- Risk: base64 JSON of at most 24 MiB, no docs, Bearer and Idempotency-Key
  headers. **Highest abandonment point** (F3).
- Unowned: support can't answer free-text questions.

**10. Submit; wait up to about 7–20 min**
- Who acts: buyer.
- Risk: a 202 reply plus polling. `worker_busy_retry_later` if another job is
  running.
- Unowned: queue transparency (ROADMAP Sprint 5, not built).

**11. Download three reports within 8 days**
- Who acts: buyer.
- Risk: no reminder email.
- Unowned: none.

**12. Rerun within 7 days of first delivery**
- Who acts: buyer.
- Risk: the terms state the window differently (F19).
- Unowned: none.

**13. Support**
- Who acts: buyer → deterministic topics, or email → owner.
- Risk: no free text in-product; email reaches the owner's Gmail only.
- Unowned: email SLA.

**14. Refund**
- Who acts: buyer (authenticated route) or email → owner → operator.
- Risk: late-processed email is rejected (F7).
- Unowned: 14-day initiation deadline tracking.

**15. Token loss (both tokens)**
- Who acts: buyer → email.
- Risk: no identity-based recovery; refund only (`pilot/README.md` step 8).
- Unowned: a recovery policy.

### 3.2 Operations failure paths

Columns: **Detection** · **Automatic response** · **Bounded operator action** · **Human dependency**.

**Webhook lost or delayed**
- Detection: reconciliation sweep (about 5 min).
- Automatic: converges from provider state.
- Operator: `retry ledger`.
- Human: none.

**Checkout provider error**
- Detection: `checkout_provider_error` event.
- Automatic: acquisition gate closes; sales pause.
- Operator: `verify acquisition`.
- Human: **someone must notice** (watchdog dark, F12).

**Execution failure / validator exit 2**
- Detection: `job_failed`.
- Automatic: delivery gate closes; buyer may resubmit (up to 6 attempts).
- Operator: `verify delivery` (with retries).
- Human: noticing (F12, F22).

**Crash mid-job**
- Detection: job-deadline recovery (up to 20 min).
- Automatic: job failed; safety gate closes.
- Operator: `verify safety`, `resolve_order`.
- Human: noticing.

**Refund request mid-run**
- Detection: `refund_requested`.
- Automatic: job completes, then the refund fires (P1).
- Operator: `retry refunds`.
- Human: none — but the report is kept (F16).

**Refund provider unavailable**
- Detection: `refund_provider_unavailable` event only.
- Automatic: retries every 5 min, indefinitely.
- Operator: `retry refunds`, `resolve_order`.
- Human: **no deadline alert** (F7).

**Refund failed / requires action**
- Detection: `refund_failed`.
- Automatic: acquisition closes.
- Operator: `resolve_order` (reissue at most 2 times, then owner).
- Human: owner in the Stripe dashboard.

**Dispute opened**
- Detection: `dispute_status`.
- Automatic: acquisition closes.
- Operator: none that answers the dispute.
- Human: **owner must submit evidence in Stripe** within the network deadline.

**Partial external refund**
- Detection: `partial_external_refund`.
- Automatic: acquisition closes.
- Operator: `verify`.
- Human: owner.

**Email refund request**
- Detection: owner reads Gmail.
- Automatic: none.
- Operator: `refund` (within the window).
- Human: **owner relay**; late ones need a Stripe dashboard refund (F7).

**Email routing breaks**
- Detection: none.
- Automatic: none.
- Operator: none.
- Human: **unowned** (F23).

**Railway outage / container death**
- Detection: none (in-process watchdog).
- Automatic: Railway restarts.
- Operator: none.
- Human: **unowned** (F12).

**Postgres loss**
- Detection: `/health` 503.
- Automatic: none.
- Operator: none.
- Human: **no backup** (F18).

**Order 101**
- Detection: none (sales look open).
- Automatic: checkout refuses.
- Operator: none.
- Human: owner policy change (F14).

**Ten abandoned checkouts**
- Detection: none.
- Automatic: blocks new checkouts for about 31 min.
- Operator: none.
- Human: none (F15).

**Stripe account restriction / payout hold**
- Detection: Stripe emails the owner.
- Automatic: none.
- Operator: none.
- Human: owner.

**New KoSIT release**
- Detection: none.
- Automatic: none.
- Operator: none.
- Human: **owner product decision** (F1).

**Office Clerk lapses**
- Detection: none.
- Automatic: none.
- Operator: none.
- Human: owner.

**Owner away for more than 7 days**
- Detection: n/a.
- Automatic: sweeps keep running.
- Operator: the bot can process in-window refunds if told.
- Human: **email refunds, disputes and invoices wait** (§4.4).

---

## 4. Unit economics (assumptions visible — not a forecast)

### 4.1 Inputs

**Measured**
- **Price:** £299, no VAT charged.
- **Stripe fee on a UK standard card:** **£4.69** (1.5% + 20p). Measured on
  the 23 Sep live smoke.
- **Fee on refund:** **not returned**. Live smoke net was −£4.69.
- **Compute per 250-fixture pack:** about **$0.013** (STATE.md, Railway).

**Published** (<https://stripe.com/gb/pricing>, 24 Sep)
- **EEA card:** 2.5% + 20p = **£7.68**.
- **International card:** 3.15% + 20p = **£9.62**.
- **Dispute fee:** **£20** when received, plus £20 to counter it (refunded
  only if you win).
- **Ads country surcharges** on ads served (Ads only): UK 2%, Austria 5%.
  Source: <https://support.google.com/google-ads/answer/9750227>

**Assumed**
- **Fixed platform costs (F):** £30 / **£45** / £75 per month. Railway is
  budgeted at ≤£30 but the actual figure is unmeasured. Office Clerk and
  accountant costs are unknown. Domain is about £1/month.
- **Refund rate r:** best 5% · **base 15%** · worst 40%. This includes failed
  deliveries and "use and refund" (F16).
- **Chargeback rate d:** best 0% · **base 0.5%** · worst 2%. Chargebacks are
  assumed lost.
- **Card mix:** best UK · **base 50/50 UK/EEA** · worst international.
- **One-off costs:** legal review £300–600 (deferred); invoice tooling fees
  unknown.

### 4.2 Contribution per order, before acquisition

Formula: `(1 − r − d) × 299 − fee − 20·d − compute`

| Case | r | d | Fee | **Contribution per order** |
|---|---|---|---|---|
| Best | 5% | 0% | £4.68 | **£279** |
| Base | 15% | 0.5% | £6.18 | **£246** |
| Worst | 40% | 2% | £9.62 | **£163** |

These are the **break-even CPA** values, before fixed costs and owner time.

### 4.3 Monthly view

Base case per order, fixed costs F = £45 (£30–75 range in brackets). No ad
spend.

| Customers/month | Base (F = £45) | Worst case (F = £75) |
|---|---|---|
| 0 | **−£45** (−£30…−£75) | −£75 |
| 1 | **+£201** (£171–216) | +£88 |
| 3 | **+£694** | +£414 |
| 10 | **+£2,419** | +£1,556 |

Cash contribution is positive from the first sale. **Owner time and deferred
legal/accounting costs are what sink it** (§4.4).

**CPA sensitivity** (CPA = CPC ÷ conversion rate; CPCs are unknown and assumed):

| CPC ↓ / Conversion → | 0.5% | 1% | 2% | 3% |
|---|---|---|---|---|
| £1 | £200 | £100 | £50 | £33 |
| £2 | £400 | £200 | £100 | £67 |
| £4 | £800 | £400 | £200 | £133 |
| £6 | £1,200 | £600 | £300 | £200 |

**Maximum CPC that breaks even:**

| Case | 0.5% | 1% | 2% | 3% |
|---|---|---|---|---|
| Base | £1.23 | £2.46 | £4.93 | £7.39 |
| Worst | £0.82 | £1.63 | £3.26 | £4.89 |

**Maximum rational test budget.**
- Treat the first paid test as buying information, capped at about the
  contribution of **one** base-case sale (**≈£150–250 total**, including
  surcharges).
- A rational CPA ceiling that still pays for fixed costs and about 1 hour of
  owner time per order is **~£120–150**.
- The operating model's £100 anchor is prudent. Its "≥2 conversions at
  CPA ≤ £100" gate is reasonable, but it will take many months to reach at
  these budgets (§5).

### 4.4 Owner workload (estimates [INF])

**At 1 customer/month: about 45–75 min/week**
- Daily support-inbox glance, needed because of the refund windows: 5 min on
  about 5 days.
- Weekly Stripe/dashboard check: 10 min.
- First-order hand-holding, since fulfilment is API-only: 60–120 min.
- One manual invoice: 10–15 min.

**At 10 customers/month: about 2–4 h/week**
- The same fixed load.
- Per order about 20–40 min: onboarding questions, invoices, the odd refund.
- Gate reopenings after failed jobs: 10–20 min each.
- Dispute evidence, if any: 1–2 h each.

**Constraints on time away**
- An absence longer than about 7 days needs a named delegate, or a
  pre-announced support pause. Email-origin refunds and dispute deadlines
  keep running while acquisition is paused.
- This contradicts the zero-operator-time goal. It also contradicts ROADMAP
  §D's "~65 min of browser slots + ~40 min of setup, spread over the whole
  road to M2", which counts setup only, never running the business. That
  remains true until the following are done:
  - in-product refunds are the primary route (F7);
  - invoices are automatic (F8);
  - buyer docs exist (F3);
  - external alerting works (F12).

**Single points of failure**
- The owner: email, Stripe, Railway dashboard, card, Office Clerk.
- One Railway service, region and replica.
- One Stripe account.
- One email route (F23).
- One pinned validator/pair (F1).

---

## 5. Channel verdict

**Exact-intent Search is not worth money yet.** A tiny experiment
(≤£150–250 total) becomes worth running only after all of the following:

1. The pair is buyer-validated or re-pinned (F1).
2. Buyers can self-serve: F3 docs and the F10 fix.
3. Own-ledger attribution exists (F21 `?src=`).
4. An owner-held sales hold exists and the kill switch is implemented
   (F12, F13).
5. At least one direct sale shows the flow converts at all.

**Why not now [EXT + INF]**
- **Measurement.** Click-conversion upload is closed to new integrations; the
  Data Manager API is the path. It cannot retract refunds, and EEA/UK/CH
  conversions without consent signals may go unreported. With
  `?src=` + ledger, none of that is needed. Smart bidding isn't feasible at
  these volumes anyway (tROAS needs ≥15 conversions per 30 days).
- **Statistical power.** At £5/day (≈£152/month) and £2 CPC you get about 76
  clicks/month. At a true 1% conversion rate, **P(zero sales in 75 clicks) =
  47%**. The Phase-2 stop at "£150 or 250 clicks" binds on spend whenever
  CPC > £0.60; at £3 CPC it pauses a genuinely 2% campaign **36%** of the
  time. Telling 1% from 3% against a benchmark needs about 240–300 clicks
  (≈£900 at £3 CPC).
- **Intent mismatch.**
  - The launch packet's exact terms (`[xrechnung regression testing]`, …)
    likely have near-zero volume. That is unverified: Keyword Planner needs
    an account with billing.
  - The later wiring prompt broadens to "XRechnung validator/validation",
    where free validators dominate. That is the wrong buyer for a £299
    corpus pack.
  - `OPERATING_MODEL.md` names no keywords at all.
- **Geography and language.**
  - Google removed campaign-level language targeting for Search in September
    2026; ads now match by ad-copy language
    (<https://support.google.com/google-ads/answer/1722078>).
  - English ads to "UK+DACH+EU" will reach German searchers only where Google
    infers English.
  - The UK has no XRechnung mandate. UK-to-German-B2B suppliers have no
    e-invoice duty (BMF 15 Oct 2025); UK suppliers to German federal bodies
    probably do.
  - Non-DACH EU has little XRechnung need.
  - If search is ever tried: **DACH only, German ad copy, "Presence"
    location, Search partners off, AI Max off, Manual CPC.**

**Do these first (cheapest evidence per £):**
1. **5–10 buyer interviews** with DACH e-invoicing software teams: ERP or
   billing SaaS, access points, integrators. Find them via LinkedIn,
   communities and conference or meetup circles; check UWG §7 before any
   unsolicited email. Ask:
   - Do you already run KoSIT in CI?
   - Which pin are you on today, and which will you move to?
   - Would a "current → latest" evidence report save you a day?
   - Who signs off £299, and do you need an invoice or a DPA?
2. **A free, useful public artefact.** A rule-diff note for
   2026-01-31 → 2026-08-31 (and later 3.0 → 4.0), produced by this very
   engine on the public test suite. It is linkable, searchable and on-topic
   in the places these engineers read (GitHub discussions where relevant,
   LinkedIn, German e-invoicing blogs).
3. **Partnerships and referrals** with validator operators and consultancies
   that already sell adjacent work (e.g. Bonpago-style regression testing;
   e-rechnung-validator.de-type APIs). The pack is a one-off complement to
   their monthly APIs.
4. **Organic search** on the stale-pair and 4.0 topics via that content. Free,
   slow, compounding.

**What to measure before any spend:**
- landing → sample → order-form → checkout → paid → submitted → delivered
  → refunded;
- counts by `source`, unsampled;
- interview outcomes;
- one real delivery timing.

---

## 6. Sequenced closure list

### 6.1 Owner decisions and actions (in order)

**1. Pair: keep, or re-pin to `→ v2026-08-31`?** Recommend re-pin.
- Cheapest evidence: 3 interview answers, plus the KoSIT changelog.
- Authorises a `policy.json` pair change.

**2. B2B-only, or keep the consumer path (F6)?** Recommend B2B-only.
- Cheapest evidence: §7 Q1–Q6 answered (accountant 30 min if consumers
  stay).

**3. Office Clerk address plus website-use permission (F5).**
- Cheapest evidence: the issued address and permission in writing.

**4. Object storage: retire (recommended) or restore (F9).**
- Cheapest evidence: a one-line decision; then a privacy text fix.

**5. Refund stance on "use and refund" (F16).**
- Cheapest evidence: a one-line decision.

**6. Invoice route: Stripe post-payment invoice, or manual (F8).**
- Cheapest evidence: a Stripe TEST checkout showing the invoice PDF.

**7. External uptime monitor and phone alert channel (F12).**
- Cheapest evidence: a test alert received.

**8. Railway backups on (F18).**
- Cheapest evidence: a dashboard screenshot and one restore drill.

**8a. Paperwork (F9).**
- Sign Railway's DPA (DocuSign).
- Pay the ICO fee.
- Decide whether support moves from free Gmail to a mailbox with processor
  terms.
- Cheapest evidence: signed PDF, ICO registration number, a one-line
  decision.

**9. Interviews, and the first buyer by conversation (§5).**
- Cheapest evidence: 5 call notes.

**10. Flip `XREG_CUSTOMER_UPLOADS=true`** only after agent items A1–A8.
- Cheapest evidence: the A8 TEST run report.

**11. Reopen acquisition via `verify`.** Hand-held first sale only.
- Cheapest evidence: the first sale watched end to end.

**12. Ads stay off.** Revisit against the §5 criteria.
- Cheapest evidence: 3 direct sales.

### 6.2 Agent implementation (sequenced, suite before and after each)

**A1 — Live-mode copy (F4).**
- Change: mode-aware link and title strings.
- Test: every live-mode response, including `/app.js` and `/success`,
  contains no `simulat` or `sandbox`.

**A2 — Buyer docs (F3, F19).**
- Change: `/docs` page plus a stdlib `/client.py` (folder → base64 → submit →
  poll → download). Link from landing and `/success`. Remove the
  `pilot/README.md` references. Explain which token to keep.

**A3 — Legal pages (F5).**
- Change: render as HTML, strip TODO-LAWYER blocks, insert the address
  (after owner step 3).

**A4 — Checkout declaration (F6, F20).**
- Change: per owner step 2 — business-only declaration, or the complete
  consumer path. Store `terms_sha256` on the order.

**A5 — Refund routes (F7).**
- Change: in-product route is primary in the copy; calendar-day windows;
  runbook for late email requests; refund-age alert.

**A6 — Checkout retry (F10).**
- Change: the script drops the stored key on
  `checkout_expired_use_new_key` or `idempotency_key_payload_conflict`, then
  retries once.

**A7 — Stripe Checkout (F8).**
- Change: `invoice_creation`, `tax_id_collection`, billing address. The
  existing `validate_checkout` amount checks are unchanged (tax stays 0).

**A8 — TEST-mode end-to-end run (F11).**
- Change: max-size synthetic non-corpus pack via `client.py` with uploads on
  in TEST; concurrent submits; a rerun. Record timings under
  `.cache/activation/`.

**A9 — Privacy text (F9).**
- Change: Amsterdam; Cloudflare and Google rows; Railway US parent;
  processor role; DPA link (owner approves the wording).

**A10 — Watchdog (F12).**
- Change: read-token only; add unmatched events, reconciliation failures,
  disputes, refund age and checkout errors; run outside xreg-api.

**A11 — Owner sales hold (F13).**
- Change: `XREG_SALES_HOLD` that `verify` cannot override; the landing page
  and the Ads kill switch read it.

**A12 — Capacity honesty (F14, F15).**
- Change: `sales_open()` also reflects the spend cap and unpaid capacity.
  Accounting change to the lifetime cap only as the owner authorises.

**A13 — Attribution (F21).**
- Change: `?src=` → enumerated source; unsampled daily counters; a weekly
  net-revenue-by-source view.

**A14 — Report access after refund (F16).**
- Change: per owner step 5, revoke report access when a refund is
  requested.

### 6.3 Changes to `docs/ads/OPERATING_MODEL.md`

The thresholds (CTR ≥ 2%, CPA £60–100, "one conversion per 2–4 weeks") are
**unsupported hypotheses**. There is no account data; label them as such.

1. **Measurement.** Replace "UploadClickConversions" with own-ledger
   attribution via `?src=` (recommended). The alternative is the Data Manager
   API plus a real consent capture, noting it cannot retract refunds. Make the
   app ledger the CPA/ROAS source of truth.
2. **Budget.** £10/day allows up to £304/month (the 30.4× rule), not £250.
   Use about £8.22/day or a campaign total budget. Add the UK 2% and AT 5%
   surcharges.
3. **Stop rules.**
   - Drop the ROAS < 2.0 rule: it is implied by CPA > £149.50, so the £120
     CPA rule always fires first.
   - Treat Phase 2's £150 as a **loss limit, not a verdict**.
   - Report cumulative conversions with intervals, not rolling 30-day ROAS
     on 0–5 events.
   - Never auto-pause a keyword that has converted.
4. **Kill switches.**
   - Add the owner sales hold (F13), the spend cap and unpaid capacity (F14,
     F15), and the refund-age rule.
   - All must exist in code before any spend.
   - `ads_should_pause` has no Ads client to act on today.
5. **Geography and language.** DACH-first with German copy, or don't run.
   Drop generic "EU". Use "Presence" location, Search partners off, AI Max
   off, Manual CPC.
6. **Autonomy.** Down-only is fine. Google roles cannot enforce it; the
   server proxy must. The bot must never hold the Google credential. Plan for
   advertiser identity verification, where a missed deadline pauses the
   account.
7. **Consent is not "not needed".**
   - The model's claim that server-side gclid needs no consent is
     contestable. The ICO applies PECR reg 6 when the identifier is stored on
     or read from the device. The EDPB treats collecting a tracking-link
     identifier as "gaining access" under ePrivacy art 5(3).
   - Google's own EU User Consent Policy covers EEA, UK and CH users.
   - `?src=` (a campaign code, no personal identifier, nothing sent to
     Google) sidesteps all of this.
   - Any gclid upload needs counsel sign-off (§7 Q10) and a privacy-notice
     row.

---

## 7. Questions for a qualified solicitor / accountant

An agent review is **not legal clearance**. The questions are grouped by
decision. Primary sources were read on 24 Sep 2026; the legal research is
summarised in F6, F8, F9 and F25.

### Offer classification and consumers

1. Is the pack a "service", "digital content" or both under the CCRs 2013,
   and a "digital service" under the amended CRD? When is it "fully
   performed", given the 7-day rerun right? (CCRs regs 4–5, 36–37; CRD art
   16(a)/(m).)
2. If consumers stay, does the working-tree checkout satisfy the following?
   (`server.py:197-201`: the "I expressly request …" and "I understand …"
   checkboxes.)
   - CCRs reg 36 and reg 37 at the same time.
   - The reg 16 durable-medium confirmation "before performance begins".
   - CRD art 16(a)/(m).

   Is it misleading to pair the loss-of-right acknowledgement with an
   unconditional contractual 14-day refund?
3. **Business-only.** Does a uniform "business buyers only" rule, with a
   self-declaration plus a VIES-checked VAT ID for EU buyers, adequately
   exclude consumers? (Implementing Reg. 282/2011 art 18.)
   - What if a buyer misdeclares?
   - How should German Kleinunternehmer with no USt-IdNr be handled?
4. Does the Geo-blocking Regulation (2018/302, art 4(1)(b)) apply to a UK
   trader after Brexit? Is a business-only rule compatible with it?
5. Does targeting Google Ads at DE/AT/CH amount to "directing activities"
   (Rome I art 6; Brussels I bis art 17–18; Pammer), even with an
   English-only GBP site?
6. For consumer disputes, what must the complaint-outcome message say about
   ADR under DMCC 2024 s.308 (in force 6 Apr 2026)?

### Trader identity

7. Can the Office Clerk virtual address serve as each of the following?
   - (a) The Companies Act 2006 s.1201–1204 address for a sole trader using a
     business name.
   - (b) The E-Commerce Regs 2002 reg 6 "geographic address at which the
     service provider is established".
   - (c) The DMCC s.230 "principal place of business" for consumer
     invitations.
   - (d) A German Impressum address under DDG §5. Case law is split:
     OLG München 29 U 8/17 against virtual offices; OLG Frankfurt 6 U 150/19
     accepting a letterbox address.

   Can the home address lawfully stay unpublished?
8. Does DDG §5 apply to a UK provider that targets Germany? (The §3
   country-of-origin protection covers EU-established providers only.) What
   warning-letter (Abmahnung) exposure remains after UWG §13(4)?

### Tax (accountant)

9. **Invoices.**
   - What invoice wording should a non-VAT-registered UK sole trader use for
     German, Austrian and Swiss business buyers?
   - Does UStG §14a(5) ("Steuerschuldnerschaft des Leistungsempfängers")
     bind a UK supplier?
   - What evidence of business status should we keep? (HMRC Notice 741A
     treats these supplies as outside the scope of UK VAT.)
10. If any EU consumer sales are accepted, what is the cheapest compliant
    route? Options: non-Union OSS registration; a merchant of record
    (Stripe Managed Payments, reportedly about +3.5%); or refusing
    consumers. How should the price display change ("£299, no VAT")?
11. Does reverse-charged Google Ads spend count toward the £90k UK
    registration threshold (HMRC VATREG16150)? It is immaterial at pilot
    scale but should be recorded. Also confirm the Self Assessment
    registration trigger (trading allowance £1,000) and Making Tax Digital
    timing.

### Data protection and marketing

12. For uploaded fixtures, is the seller a controller or a processor? Is a
    published Art. 28 DPA/AVV required for business buyers, alongside a
    "synthetic data only" rule?
13. Is an Art. 27 EU representative required for business-only sales? And
    if EU consumers are accepted?
14. Is a free Gmail mailbox acceptable for customer correspondence?
    - Is signing Railway's DPA enough?
    - Do Stripe and Cloudflare self-serve terms suffice?
    - What transfer wording should cover Railway (US parent; hosting in
      Amsterdam)?
15. Is the ICO fee payable (SI 2018/480)?
16. **Ads data.**
    - Is server-side gclid capture subject to PECR reg 6 or ePrivacy art
      5(3) (ICO storage-and-access guidance; EDPB Guidelines 2/2023 paras
      50–51)?
    - Is uploading conversions to Google lawful on legitimate interests
      (with an LIA), or does it need consent?
    - Should EEA/UK traffic be excluded from uploads?
    - Does a `?src=` campaign code avoid personal-data processing entirely?

### Contract terms

17. **Refunds.**
    - Is the refund-initiation promise ("promptly and within 14 days of
      receiving the request") compatible with CCRs reg 34 for consumers?
    - How should an email request that arrives before a deadline but is
      processed after it be handled contractually?
    - Should windows be counted in calendar days?
18. Is the liability cap (the price paid, excluding indirect loss) valid for
    B2B buyers in the UK, DE and AT? Are the 8-day evidence expiry and the
    rerun terms fair?
19. Is England and Wales law with a consumer carve-out adequate? What about
    Swiss consumers (PILA art 120; not verified)?

### Licences and naming

20. Does hosted EN 16931 schematron validation count as "communication"
    under EUPL-1.2 (the "providing access to its essential functionalities"
    wording)? What notice and source-link wording is required?
21. What obligations follow from the other components in hosted use?
    - KoSIT validator and configuration: Apache-2.0.
    - Saxon-HE: MPL-2.0.
    - Temurin: GPLv2 + Classpath Exception.
22. Is "XRechnung" a registered mark? Are "xregcheck.com" and "XRechnung" in
    ad copy fair descriptive use (EUTMR art 14)? Trademark status was not
    checked in this review.

---

## Appendix — evidence log

- **Live fetches:** 24 Sep 2026 ~14:37Z (`/`, `/app.js`, `/terms`, `/refund`,
  `/privacy`, `/notices`, `/health`, `/requirements`, `/sample`,
  `/style.css`, `/success`). GET only; saved to the session scratchpad.
- **Browser:** one view of `/terms` and `/` (read-only). The mobile-width
  resize did not take effect, so **mobile rendering remains unverified in a
  real browser**. The 23 Sep a11y audit was computed without a browser and
  predates the new form controls.
- **Suite:** full 17-module list including `pilot.test_checkout_refunds` →
  `Ran 237 tests … OK (skipped=5)`.
- **Probes P1–P6:** results quoted in F7, F10 and F14–F16. Output:

```
P1 refund mid-run: {'refund':'requested'} → job delivered, runs 1, report downloadable after refund
P2 email request processed day 7.1: REJECTED refund_reason_not_eligible
P3 consumer, day 14 + 1 h: REJECTED refund_window_closed
P4 order #101: REJECTED spending_gate with all gates healthy
P5 same key after session expiry: REJECTED checkout_expired_use_new_key
P6 10 abandoned checkouts (4 IPs): next buyer REJECTED checkout_capacity_busy_retry_later
```

- **External research:** three independent research passes (market,
  legal/tax, Google Ads) on 24 Sep. The two decisive claims were re-verified
  directly: the KoSIT `v2026-08-31` release, and the Google Ads
  click-conversion allowlisting since 15 Jun 2026.
- **No live mutation.** No deployment, purchase, refund, customer upload,
  Ads change, variable read or credential access occurred.
