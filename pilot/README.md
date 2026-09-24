# Local public-data sandbox

Run from the repository root with the existing Python and pinned Windows Java dependencies:

```powershell
python -m pilot.server --state .pilot-demo-1 --port 8765
```

Open http://127.0.0.1:8765. Reuse the same state directory to resume after restart. The directory must reside on persistent local storage, with one service process; ephemeral volumes and shared/network filesystems are unsupported. The Windows worker requires the existing `pywin32==311` dependency listed in `requirements.txt`. No credentials or paid APIs are used. The payment page explicitly simulates Stripe; it is not hosted by Stripe. The genuine test-only HTTPS adapter is in `payments.py`, but credential injection and real provider acceptance remain blocked. The server deliberately binds only to loopback and refuses foreign Host/Origin values. It is not a production HTTP server.

The scope page includes the price, pair, corpus attestation, public sample, requirements and exclusions. Save the checkout response's access token and recovery token separately. Neither is placed in a URL, report or event log. These are generated sandbox capabilities, not credentials from an existing account. Click the simulated payment link; completing payment returns to `/success?session_id=…`, which displays both capabilities again while the payment session is live. `/success` alone never pays or fulfils anything.

Public legal pages: unauthenticated `GET /terms`, `GET /refund`, `GET /privacy` and `GET /notices` serve the packaged drafts in `pilot/legal/` verbatim as `text/markdown`, writing no event rows.

## Authenticated workflow

Use `Authorization: Bearer ACCESS_TOKEN` for every order request. Avoid putting tokens on shared command lines or pasting them into support. A local client should keep them in memory or its approved credential store.

1. `POST /checkout`, JSON `{"product":"opaque-product","attested":true,"source":"direct"}`, with an `Idempotency-Key` header containing a fresh random UUID. Retain this key privately and reuse it with the identical body after response loss; it is a recovery capability. Returns order ID, capabilities and checkout URL. Only one product is bound; its identifier is hashed in state. Source is an enumerated attribution code, not an arbitrary URL. The browser retains its key in session storage. Provider failure pauses new purchases; retries of the existing intent remain available. Internal callers that omit the key cannot recover a lost checkout response.
2. Complete simulated checkout. Actual Stripe deployment must complete hosted Checkout and receive its signed server event. Payment is independently retrieved and verified. Sandbox tax is zero; future tax configuration requires a new approved acceptance run.
3. `GET /success?session_id=…` — the hosted Checkout success redirect — delivers both capabilities with no operator step and no auth header: the session id is re-read from the provider, re-validated against the order (binding, amount, currency, mode, live mode) and must report `paid`; the redirect URL alone is never trusted. The response carries the order id, access token and recovery token as JSON, or a minimal HTML page for browsers. Re-display is bounded to the live payment session (sessions self-expire after roughly 31 minutes) and ends on refund or order expiry; after that the page returns only the no-access banner with the order id and the `POST /recover` flow. Unknown, unpaid, refunded, expired and mismatched session ids receive one indistinguishable response. Token material never appears in a URL, log or report; only capability hashes are stored, and `POST /recover` rotation revokes everything the success page can still display.
4. `POST /orders/ORDER_ID/submit` with the payload below and an `Idempotency-Key` header (a new random UUID for each intended execution). Reuse that key and identical payload after a network failure; use a different key for the included rerun. The construction server accepts only byte hashes from `manifests/corpus.json`. Declaring data public does not bypass this lock. Do not submit customer data.
5. `GET /orders/ORDER_ID` for state. Download each of `GET /orders/ORDER_ID/results.json`, `/junit.xml`, `/report.html` with the same authorization header. Keep all three together. A delivered pack can contain regressions or invalid invoices. `delivered_incomplete` is explicitly not a clean comparison; `failed` remains a failure.
6. Resubmit retained inputs with the same fixture IDs, product, pair and output mode for one rerun within seven days of first delivery. Changed XML is allowed in the future pilot; this construction version accepts only frozen public bytes. A failed execution does not consume the included rerun. Six submission attempts bound abuse; after that refund support remains available.
7. `POST /orders/ORDER_ID/support`, JSON `{"topic":"status"}`. Topics: `access`, `status`, `input`, `rerun`, `refund`, `advice`. No free text or attachments. Refund responses distinguish requested/pending/requires_action/failed/canceled/succeeded. Refunding stops further processing; funds are not described as returned until Stripe reports success.
8. `POST /recover`, JSON with `order_id` and separately retained `recovery_token`, rotates both capabilities and invalidates the old ones. Loss of both capabilities is an unresolved access-recovery launch blocker; no identity claim is trusted by support.

Submission structure (base64 represents complete exact UTF-8 XML):

```json
{
  "pair": "xrechnung-2025-07-10_to_2026-01-31",
  "fixtures": [
    {"id": "f001", "old": "BASE64_XML", "new": "OPTIONAL_BASE64_XML"}
  ]
}
```

Omit `new` on every row for a rules-only pack; otherwise supply both outputs on every row. IDs: 1–80 ASCII letters/digits/underscore/hyphen, unique and non-sensitive. 1–250 IDs, 5 MiB/file, 16 MiB total decoded, 24 MiB HTTP body. No filenames, file paths, ZIP/TAR, external entities or transformations. The tool generates safe numeric temporary filenames. Preserve customer originals; only the tool's own copies are removed.

## Reproduce evidence

```powershell
python -B -m unittest test_xreg test_location_matching pilot.test_pilot pilot.test_remediation pilot.test_remediation2 -v
python -m pilot.benchmark --out evidence/pilot/reproduction-1 --count 250
python -m pilot.inventory --out evidence/pilot/inventory-reproduction-1.json
```

Evidence outputs must be fresh. The benchmark downloads the existing hash-pinned public source archive, runs the HTTP flow and real Java validator, includes one rerun, verifies original-file preservation, and removes its own temporary corpus/state. Payment transport is offline. It never logs tokens. The final public reports are intentionally retained as evidence.

The local state contains report blobs and payment metadata; it is not an anonymised analytics database. Eight-day report expiry uses SQLite secure deletion and DELETE journaling; regression tests scan the active database/journal files for retained synthetic bytes. This does not erase filesystem snapshots, backups, SSD remnants or process memory. The managed design still requires separate private object storage and a metadata database. Do not deploy local SQLite on ephemeral Cloud Run storage.

## Recovery and future manager interface

The runnable simulator persists provider objects independently in `provider.sqlite3` and business state in `business.sqlite3`. The in-memory `OfflineStripe` remains solely for fault-injection tests. Simulator refunds settle deterministically on the next provider observation; that is not evidence of a genuine refund.

The HTTP service runs deterministic maintenance without a model every second. Each sweep considers at most 100 due reservations. Unpaid checkouts expire after 24 hours only after provider expiry/absence is confirmed. Paid orders without a first delivery refund after seven days or exhaustion of submission attempts. Delivered entitlements retire after both runs or the seven-day rerun window; simulated compute remains accounted for rather than being recycled as free budget. Refunds remain actionable even after capacity retires. Provider uncertainty has persistent `reconciliation_failed`, `next_action=reconcile_reservations` and a five-minute retry time. Existing safety/acquisition gates still require acceptance before reopening; this release does not claim unattended commercial operation.

Adapters must implement `checkout`, `session`, `find_checkout`, `expire_session`, `refund` and `refund_status`. Lookup must be authoritative without repeating an expired idempotency key; expiry must atomically refuse already-paid sessions. `StripeSandbox` does not yet meet that full recovery contract and must not be substituted into a launch deployment.

Future connectors can call `Service.operating_state(after, limit)` and `operator.dispatch(service, name, arguments)` directly. `make_server(..., operator_token=...)` optionally exposes authenticated `GET /operator/state?after=CURSOR` and `POST /operator/action` with `{"name":"retry","arguments":{"code":"ledger"}}`. A separate capability is required; customer capabilities cannot call these endpoints. The CLI leaves these endpoints disabled. An eventual authenticated gateway must supply the capability; no manager is integrated. State pages expose opaque order IDs and enumerated states/actions only, with no report access, raw errors, product identifiers or invoice data. Action names are `health`, `spend`, `ruleset` (read only); `retry` with `ledger`, `reservations`, or `refunds`; `refund` with an opaque order ID; `pause` with an enumerated gate; and the original fixed `maintenance` actions. The refund action requires verified payment and refuses a running job. Retry performs one bounded reconciliation pass; it cannot resubmit customer inputs. No arbitrary shell, source edits, gate reopening, ruleset changes or spending capability exists.

Windows Job Objects atomically contain each child at creation, kill descendants on parent death, and wait for termination before handled-path cleanup. The service supervises the complete Python/Java job, including report rendering, with a persisted job deadline. Scratch is swept under an exclusive process lock on restart. A polling disk watchdog, a 128 MiB delivered-pack limit and a 2 GiB job memory limit bound ordinary resource failures; the disk watchdog is not a hard filesystem quota. This is lifetime containment, not an OS data/network sandbox. Private uploads and commercial launch remain locked pending the full isolation and external durability evidence described in `../REMEDIATION2.md`.
