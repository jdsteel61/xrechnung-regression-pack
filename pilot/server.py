"""Loopback-only HTTP demonstration. Not an Internet deployment server."""
import argparse
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import ipaddress
import json
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import urlsplit, parse_qs

from .payments import DurableOfflineStripe, PaymentError
from .service import Rejected, Service
from .worker import ROOT, PAIR, report_html

REQUIREMENTS = '''Release Regression Pack: £299 before applicable taxes, one-off.
One software product; XRechnung only; KoSIT 1.6.3 with full configuration bundles
2025-07-10 (Schematron 2.4.0) → 2026-01-31 (Schematron 2.5.0).
Up to 250 fixture IDs; optional paired old/new application outputs; JSON, JUnit
and readable HTML evidence; one rerun for the same comparison within seven days.
You must already have a representative supported test corpus. Customer-owned
test fixtures only in the future pilot; production invoice archives are excluded.
Retain your original inputs; the rerun requires resubmission with the same IDs,
product, ruleset pair and paired/unpaired mode. XML must be UTF-8/ASCII, at most
5 MiB per file and 16 MiB combined; submit base64 JSON, never ZIP/TAR or file paths.
No transformations, legal/tax certification, bespoke rules, corpus-design
consulting or manual finding adjudication. Different-byte application comparisons
may be INCOMPLETE; neither that nor execution failure means a clean pass.
Reports may be confidential. Authenticated downloads expire after eight days;
provider logs/backups may persist longer. No zero-retention promise.
__MODE_NOTE__
Card payment, where enabled, is processed by Stripe Checkout; we do not see or
store your full card details. Support: support@xregcheck.com.
'''

# The checkout-reality note appended to /requirements and the landing page's
# full-text block. The construction-sandbox clause is a property of the local
# SIMULATION, not of the product: it must never appear on a deployment that
# can take real payment, and live copy must never appear on a simulation.
SANDBOX_NOTE = ('CONSTRUCTION SANDBOX: only allowlisted public fixtures are accepted. '
                'Customer uploads, real checkout, live payments and advertising are '
                'disabled. Simulated checkout is not Stripe-hosted checkout.')
TEST_NOTE = ('Stripe TEST mode: checkout opens a real Stripe-hosted TEST session '
             '(test cards only); no money moves. Only allowlisted public fixtures '
             'are accepted.')
LIVE_NOTE = ('Live sales: checkout opens a Stripe-hosted card payment of £299 before '
             'applicable taxes. Only allowlisted public fixtures are accepted; '
             'customer-owned uploads are not yet enabled.')
PAUSE_NOTE = (' Purchases are temporarily paused: new orders cannot be started right '
              'now; paid orders, support and refunds are unaffected.')


def payment_mode(service):
    """Which checkout reality this deployment presents: simulated / test / live."""
    if service.provider.simulated:
        return 'simulated'
    if getattr(service.provider, 'live', False) is True:
        return 'live'
    return 'test'


def sales_open(service):
    """True exactly when create_checkout_order would admit a new order: no
    unhealthy gate. The landing page must never advertise an open purchase
    path the service would refuse, nor call a live path a simulation."""
    with service.lock:
        return service.db.execute('SELECT COUNT(*) FROM gates WHERE healthy=0').fetchone()[0] == 0


def requirements_text(mode, open_sales=True):
    note = {'simulated': SANDBOX_NOTE, 'test': TEST_NOTE, 'live': LIVE_NOTE}[mode]
    if mode == 'live' and not open_sales:
        note += PAUSE_NOTE
    return REQUIREMENTS.replace('__MODE_NOTE__', note)

# Landing page. A static template with checkout-reality tokens: the banner,
# the how-it-works pay step, the order-section note and the order button are
# chosen per request from payment_mode()/sales_open(), so the page can never
# contradict what /checkout would actually do. The report-preview rows are
# taken verbatim from the public sample report (evidence/gate1 verification,
# served at /sample); no customer data appears anywhere on this page.
_PAGE_TEMPLATE = '''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>XRechnung Release Regression Pack — official validator evidence | xregcheck.com</title>
<meta name="description" content="One-off XRechnung regression evidence: your fixtures validated with the official KoSIT validator 1.6.3 across pinned ruleset releases 2025-07-10 (Schematron 2.4.0) and 2026-01-31 (Schematron 2.5.0). JSON, JUnit and HTML reports. GBP 299 before applicable taxes.">
<link rel="stylesheet" href="/style.css">
<body>
<a class="skip" href="#main">Skip to content</a>
<header class="site"><div class="wrap bar">
<p class="brand"><a href="/">xregcheck.com</a></p>
<nav class="top" aria-label="Page sections"><a href="#get">What you get</a> <a href="#report">The report</a> <a href="#scope">Scope</a> <a href="#faq">FAQ</a> <a href="/terms">Terms</a></nav>
</div></header>
__BANNER__
<main id="main">
<section class="hero" aria-labelledby="hero-h">
<div class="wrap hero-grid">
<div class="hero-copy">
<p class="eyebrow">Evidence for your XRechnung engine upgrade</p>
<h1 id="hero-h">Release Regression Pack</h1>
<p class="lede">Your own test fixtures, validated with the official KoSIT XRechnung validator 1.6.3 across two pinned ruleset releases — delivered as a per-fixture before/after report for your release decision.</p>
<p class="price">£299 <span>one-off · before applicable taxes</span></p>
<p class="pairline"><strong>2025-07-10</strong> · Schematron 2.4.0&nbsp;&nbsp;→&nbsp;&nbsp;<strong>2026-01-31</strong> · Schematron 2.5.0</p>
<p class="actions"><a class="cta" href="#order">Start an order</a> <a class="cta ghost" href="#report">See the report</a></p>
<p class="micro">One software product · up to 250 fixture IDs · JSON, JUnit XML and readable HTML · one rerun within seven days</p>
</div>
<div class="window" id="report">
<div class="window-top"><span class="wd" aria-hidden="true"></span><span class="wd" aria-hidden="true"></span><span class="wd" aria-hidden="true"></span><span class="wtitle">report.html — public sample</span><span class="badge">REGRESSION FLAGS</span></div>
<div class="window-body">
<p class="wpair">KoSIT validator 1.6.3 · pair <code>__PAIR__</code></p>
<ul class="chips" role="list">
<li><strong>500</strong> processed</li>
<li><strong>234</strong> passes</li>
<li><strong>266</strong> validation failures</li>
<li class="bad"><strong>3</strong> rule regressions</li>
<li><strong>0</strong> execution errors</li>
</ul>
<div class="tablewrap">
<table>
<caption class="sr">Verdict-transition rows taken verbatim from the public sample report</caption>
<thead><tr><th scope="col">Output</th><th scope="col">Fixture ID</th><th scope="col">Verdict transition</th><th scope="col">Regression</th><th scope="col">Comparable</th></tr></thead>
<tbody>
<tr><td>old_app</td><td>…/cii/cii-br-de-1-test.xml</td><td>pass→pass</td><td>—</td><td>yes</td></tr>
<tr class="flagged"><td>old_app</td><td>…/cii/cii-br-de-25-test-bg-19-with-bt-86-1.xml</td><td>pass→fail</td><td>flagged</td><td>yes</td></tr>
<tr><td>old_app</td><td>…/cii/cii-br-de-18-case-sensitive-keyword-2.xml</td><td>fail→fail</td><td>—</td><td>yes</td></tr>
</tbody>
</table>
</div>
<p class="wfoot">…247 more fixture rows in the full report. <a href="/sample">Open the public sample report</a></p>
</div>
</div>
</div>
</section>
<section id="get" aria-labelledby="get-h">
<div class="wrap">
<h2 id="get-h">What you get</h2>
<div class="cards">
<div class="card"><h3>Pinned pair, official validator</h3><p>Every fixture runs twice through the official KoSIT validator 1.6.3 with full configuration bundles: release 2025-07-10 (Schematron 2.4.0) and v2026-01-31 (Schematron 2.5.0). The pair is fixed for your order.</p></div>
<div class="card"><h3>Per-fixture verdict transitions</h3><p>For each fixture ID: the old→new validation verdict, regression flags and comparability — plus optional paired old/new application-output comparison. Delivered as JSON, JUnit XML for CI, and readable HTML.</p></div>
<div class="card"><h3>Rerun and retention, stated plainly</h3><p>One rerun of the same comparison within seven days (resubmit the same IDs, product, pair and mode). Authenticated downloads expire after eight days; provider logs may persist longer. No zero-retention promise.</p></div>
</div>
</div>
</section>
<section id="how" class="alt" aria-labelledby="how-h">
<div class="wrap">
<h2 id="how-h">How it works</h2>
<ol class="steps">
<li><strong>Attest.</strong> Confirm at order time that you already have a representative supported test corpus. Production invoice archives are excluded; test fixtures only.</li>
<li><strong>Pay.</strong> __PAY_STEP__</li>
<li><strong>Submit.</strong> Send fixture IDs with base64-encoded XML — never ZIP/TAR archives or file paths. XML must be UTF-8/ASCII, at most 5 MiB per file and 16 MiB combined. Retain your originals; the rerun requires resubmission.</li>
<li><strong>Download.</strong> Collect your token-protected JSON, JUnit XML and HTML evidence by authenticated download, within eight days of it becoming available.</li>
</ol>
</div>
</section>
<section id="scope" aria-labelledby="scope-h">
<div class="wrap">
<h2 id="scope-h">Scope and exclusions</h2>
<div class="cards two">
<div class="card"><h3>Included</h3><ul>
<li>XRechnung only; one software product per order</li>
<li>Up to 250 fixture IDs per order</li>
<li>Optional paired old/new application outputs</li>
<li>JSON, JUnit XML and readable HTML evidence</li>
<li>One rerun of the same comparison within seven days</li>
</ul></div>
<div class="card"><h3>Not included</h3><ul>
<li>Certification, conformity marks or legal/tax advice</li>
<li>Transformations or bespoke rules</li>
<li>Corpus-design consulting</li>
<li>Manual adjudication of findings</li>
<li>Production invoice archives</li>
</ul></div>
</div>
<p class="note">A different-byte application comparison may be INCOMPLETE, and neither an INCOMPLETE comparison nor an execution failure means a clean pass — both are reported as what they are.</p>
</div>
</section>
<section id="requirements" class="alt" aria-labelledby="requirements-h">
<div class="wrap">
<h2 id="requirements-h">Requirements and limitations — full text</h2>
<details open>
<summary>Show the complete requirements and limitations</summary>
<pre>__REQUIREMENTS__</pre>
</details>
</div>
</section>
<section id="order" aria-labelledby="order-h">
<div class="wrap">
<h2 id="order-h">Start an order</h2>
<p class="order-note">__ORDER_NOTE__</p>
<fieldset>
<legend>Order details</legend>
<label for="product">Product identifier — one opaque label for the software product under test</label>
<input type="text" id="product" value="sample-product" maxlength="80" autocomplete="off">
<label class="attest" for="confirm"><input type="checkbox" id="confirm"> I already have a representative supported test corpus and I understand the scope and limitations above.</label>
<label for="buyer-type">Buying as</label>
<select id="buyer-type" required><option value="">Choose one</option><option value="business">A business</option><option value="consumer">An individual consumer</option></select>
<label class="attest" for="terms-confirm"><input type="checkbox" id="terms-confirm"> I accept the <a href="/terms">Service Terms</a> and have read the <a href="/refund">Refund Policy</a>.</label>
<div id="consumer-consent" hidden><label class="attest" for="start-now"><input type="checkbox" id="start-now"> I expressly request that the service and any digital report begin before the 14-day cancellation period ends.</label>
<label class="attest" for="cancel-ack"><input type="checkbox" id="cancel-ack"> I understand that starting digital delivery may end my statutory cancellation right. The separate 7-day any-reason refund still applies.</label></div>
<button id="checkout" type="button"__BUTTON_ATTRS__>__ORDER_BUTTON__</button>
</fieldset>
<pre id="result" aria-live="polite"></pre>
<p>Save the access token and the recovery token separately; neither is emailed, placed in a URL or re-sent. Use the authenticated workflow in pilot/README.md to submit public fixtures and download evidence.</p>
</div>
</section>
<section id="faq" class="alt" aria-labelledby="faq-h">
<div class="wrap">
<h2 id="faq-h">Frequently asked questions</h2>
<details>
<summary>What counts as a fixture?</summary>
<p>A fixture is one of your XRechnung test documents, identified by an opaque, non-sensitive ID. You submit complete exact XML bytes, base64-encoded in JSON — never ZIP/TAR archives or file paths. An order covers 1–250 fixture IDs, each file at most 5 MiB, 16 MiB combined. Only byte hashes from the frozen public-corpus allowlist are accepted while customer uploads remain disabled; declaring data public does not bypass this lock.</p>
</details>
<details>
<summary>What does an INCOMPLETE comparison mean?</summary>
<p>Different-byte application comparisons may be INCOMPLETE. Neither an INCOMPLETE comparison nor an execution failure means a clean pass — both are reported as what they are, not as success.</p>
</details>
<details>
<summary>How is confidentiality handled?</summary>
<p>Reports may be confidential. Authenticated downloads expire after eight days; provider logs and backups may persist longer, and no zero-retention promise is made. Your uploaded fixtures are processed solely to run your validation job and produce your report, and are deleted when the job terminates. See the <a href="/privacy">privacy notice</a> for full detail.</p>
</details>
<details>
<summary>How do refunds work?</summary>
<p>We offer a 7-day, any-reason, full refund. Consumers may also have a 14-day cancellation right; see the <a href="/refund">refund policy</a>. Email support@xregcheck.com with your order reference, or use your order's authenticated refund route. We initiate eligible refunds promptly and within 14 days; your card provider controls when the credit appears.</p>
</details>
<details>
<summary>Is VAT added to the price?</summary>
<p>The seller is a UK sole trader who is not registered for VAT, so no VAT is added to the £299 price. If this treatment ever changes, the price and any tax shown at checkout will state so before you pay.</p>
</details>
<details>
<summary>Does any AI model see my data?</summary>
<p>No. No AI or model provider is in the customer path. Validation runs as a local software subprocess on our own infrastructure; no model ever sees your invoice content or your reports, and your content is never sent to any AI or model service.</p>
</details>
<details>
<summary>How do I get support?</summary>
<p>Email <a href="mailto:support@xregcheck.com">support@xregcheck.com</a> for reruns, refunds, access problems and data requests. Quote your order reference; never paste access or recovery tokens into support email.</p>
</details>
</div>
</section>
</main>
<footer>
<div class="wrap">
<nav aria-label="Legal and policy pages">
<a href="/terms">Terms</a> <a href="/refund">Refund policy</a> <a href="/privacy">Privacy notice</a> <a href="/notices">Third-party notices</a>
</nav>
<p>Support: <a href="mailto:support@xregcheck.com">support@xregcheck.com</a></p>
<p>This page loads no third-party content — no external fonts, images, scripts, analytics or tracking.</p>
</div>
</footer>
<script src="/app.js"></script>
</body></html>'''

# Checkout-reality copy per mode: banner (css class, html), how-it-works pay
# step, order-section note, and (label, extra attrs) for the order button.
# None of these strings may ever cross modes: a simulation must say
# simulation, live must say live, and a paused live service must say paused
# (and disable the button /checkout would refuse anyway).
_LANDING = {
    'simulated': {
        'banner': ('sandbox', '<strong>Construction sandbox — purchases are not open.</strong> '
                   'Only allowlisted public fixtures are accepted; the checkout on this page is simulated, '
                   'not Stripe-hosted checkout.'),
        'pay': '£299 once, before applicable taxes — but in this sandbox the checkout is '
               'simulated and no payment is taken.',
        'note': 'In this construction sandbox the checkout is simulated and no payment is taken.',
        'button': ('Start simulated checkout', ''),
    },
    'test': {
        'banner': ('test', '<strong>Stripe TEST mode.</strong> Checkout opens a real hosted TEST session '
                   'with test cards only; no money moves.'),
        'pay': '£299 once, before applicable taxes. In TEST mode the hosted session takes a '
               'test card; no money moves.',
        'note': 'Checkout runs in Stripe TEST mode — use test cards; no real payment is taken.',
        'button': ('Start TEST checkout', ''),
    },
    'live_open': {
        'banner': ('open', '<strong>Sales are open.</strong> £299 one-off per order, paid by card '
                   'through Stripe’s hosted checkout; we never see your card details.'),
        'pay': '£299 once, before applicable taxes. Payment is taken through Stripe’s hosted '
               'checkout; we do not see or store your full card details.',
        'note': '£299 one-off before applicable taxes, paid through Stripe’s hosted checkout. '
                'We do not see or store your full card details.',
        'button': ('Start checkout — £299', ''),
    },
    'live_paused': {
        'banner': ('paused', '<strong>Purchases are temporarily paused.</strong> New orders cannot be '
                   'started right now. Paid orders, support and refunds are unaffected.'),
        'pay': '£299 once, before applicable taxes — but purchases are temporarily paused and '
               'no new order can be started right now.',
        'note': 'Purchases are temporarily paused: starting an order will be refused until '
                'sales reopen. Paid orders, support and refunds are unaffected.',
        'button': ('Checkout temporarily unavailable', ' disabled'),
    },
}


def landing_page(service):
    """Render the landing page for this deployment's actual checkout reality."""
    mode = payment_mode(service)
    open_sales = sales_open(service)
    if mode == 'live':
        variant = _LANDING['live_paused' if not open_sales else 'live_open']
    else:
        variant = _LANDING[mode]
    cls, text = variant['banner']
    banner = '<div class="notice ' + cls + '"><div class="wrap">' + text + '</div></div>'
    return (_PAGE_TEMPLATE
            .replace('__BANNER__', banner)
            .replace('__PAY_STEP__', variant['pay'])
            .replace('__ORDER_NOTE__', variant['note'])
            .replace('__ORDER_BUTTON__', variant['button'][0])
            .replace('__BUTTON_ATTRS__', variant['button'][1])
            .replace('__PAIR__', html.escape(PAIR))
            .replace('__REQUIREMENTS__', html.escape(requirements_text(mode, open_sales))))

SCRIPT = '''const buyer=document.getElementById('buyer-type');
buyer.onchange=()=>{document.getElementById('consumer-consent').hidden=buyer.value!=='consumer';};
document.getElementById('checkout').onclick=async()=>{
 const payload={product:document.getElementById('product').value,attested:document.getElementById('confirm').checked,source:'direct',buyer_type:buyer.value,terms_accepted:document.getElementById('terms-confirm').checked,immediate_performance_requested:document.getElementById('start-now').checked,cancellation_acknowledged:document.getElementById('cancel-ack').checked};
 if(!payload.buyer_type||!payload.terms_accepted||(payload.buyer_type==='consumer'&&(!payload.immediate_performance_requested||!payload.cancellation_acknowledged))){document.getElementById('result').textContent='Choose a buyer type and complete the required acknowledgements.';return;}
 const fingerprint=JSON.stringify(payload);let retry=null;
 try{retry=JSON.parse(sessionStorage.getItem('checkout-retry')||'null');}catch(e){retry=null;}
 if(!retry||retry.fingerprint!==fingerprint){retry={key:crypto.randomUUID(),fingerprint};sessionStorage.setItem('checkout-retry',JSON.stringify(retry));}
 const r=await fetch('/checkout',{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':retry.key},body:fingerprint});
 const d=await r.json();document.getElementById('result').textContent=JSON.stringify(d,null,2);
 if(r.ok){const a=document.createElement('a');a.href=d.checkout_url;a.textContent='Open simulated payment page';document.getElementById('result').appendChild(a);}
};'''


OPERATOR_VIEWS = {
    '/operator/state': ('after', lambda s, q: s.operating_state(after=q.get('after', ''))),
    '/operator/audit': ('after_id', lambda s, q: s.operator_audit(after_id=q.get('after_id', 0))),
    '/operator/events': ('after_id', lambda s, q: s.operator_events(after_id=q.get('after_id', 0))),
    '/operator/errors': ('after', lambda s, q: s.operator_errors(after=q.get('after', ''))),
    '/operator/reconciliation': ('after', lambda s, q: s.operator_reconciliation(after=q.get('after', ''))),
    '/operator/recovery': (None, lambda s, q: s.operator_recovery()),
}

# --- Request admission (finding A1-02) ------------------------------------
# Request slots gate request *dispatch*, never connections: a stalled or
# trickling handshake cannot occupy capacity that answered requests need.
# Platform health probes and webhook delivery are exempt from slots — the
# webhook body is separately size-capped below and signature-verified — so
# sales-route saturation cannot silence health checks or money events.
SLOT_EXEMPT_PATHS = frozenset(('/health', '/healthz', '/webhook'))
# Window for the request line, headers and request bodies from callers that
# present no Bearer credential. All such bodies are small by design, so a
# trickled or stalled upload frees its dispatch slot quickly.
UNAUTHENTICATED_WINDOW_SECONDS = 10
# Per-read window for a *validated* submit upload (large legitimate bodies).
REQUEST_TIMEOUT_SECONDS = 30
# Total deadline for a validated submit body (R-01): even a caller holding a
# real credential cannot trickle a dispatch slot indefinitely; comfortably
# under the platform edge's 300 s request kill.
SUBMIT_BODY_WINDOW_SECONDS = 240

# --- Public legal pages ---------------------------------------------------
# The four finalized legal drafts (terms, refund policy, privacy notice,
# third-party notices), packaged under pilot/legal/ so they deploy with the
# app (.cache/ is excluded from deploys). Read from disk at request time.
# Ordinary GET dispatch only: unauthenticated, served inside the existing
# origin/write guard, taking a normal request slot (no exemption added),
# and writing NO events rows (A2-01 discipline — not even sampled ones).
LEGAL_DIR = Path(__file__).resolve().parent / 'legal'
LEGAL_PAGES = {'/terms': 'terms.md', '/refund': 'refund.md',
               '/privacy': 'privacy.md', '/notices': 'notices.md'}

# --- Anonymous view telemetry sampling (finding A2-01) --------------------
# Unauthenticated page views are sampled, not recorded per view: at most one
# durable `events` row per kind per window, so anonymous traffic cannot grow
# the table (and contend on the service lock) without bound.
VIEW_EVENT_WINDOW_SECONDS = 60.0
_view_event_seen = {}


def _view_event_due(kind):
    now = time.monotonic()
    if now - _view_event_seen.get(kind, 0.0) < VIEW_EVENT_WINDOW_SECONDS:
        return False
    _view_event_seen[kind] = now
    return True


def check_operator_tokens(read_token, action_token):
    """Startup validation: each credential is strong, and they must differ."""
    for token in (read_token, action_token):
        if token is not None and (not isinstance(token, str) or len(token) < 32):
            raise ValueError('operator_token_too_short')
        # e.g. a BOM/newline injected by a shell pipe: fail at startup, not per request.
        if token is not None and not re.fullmatch(r'[A-Za-z0-9_-]+', token):
            raise ValueError('operator_token_invalid_characters')
    if read_token is not None and read_token == action_token:
        raise ValueError('operator_tokens_must_differ')


def make_server(service, port=0, *, operator_token=None, operator_read_token=None, deploy=False,
                submit_wait_seconds=240):
    # operator_token is the ACTION credential (mutations); operator_read_token
    # can read bounded views only. Either absent => that surface is closed.
    check_operator_tokens(operator_read_token, operator_token)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log URLs, auth, request bodies or reports.

        def setup(self):
            super().setup()
            # The request line and headers must arrive promptly; on timeout
            # handle_one_request closes the connection, so a trickling peer
            # frees its thread quickly without ever taking a request slot.
            self.connection.settimeout(UNAUTHENTICATED_WINDOW_SECONDS)

        def send(self, status, body, content_type='application/json'):
            if not isinstance(body, bytes):
                body = json.dumps(body).encode() if content_type=='application/json' else body.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', content_type+'; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def token(self):
            header = self.headers.get('Authorization', '')
            return header[7:] if header.startswith('Bearer ') else ''

        def operator_role(self):
            supplied = self.token()
            if operator_token and hmac.compare_digest(supplied, operator_token):
                return 'action'
            if operator_read_token and hmac.compare_digest(supplied, operator_read_token):
                return 'read'
            raise Rejected('operator_access_denied')

        def submit_bounded(self, oid, body, key):
            """Answer before the platform's 300 s edge limit; the job continues.

            A full 250-pair pack takes ~8 min. Past the wait, reply 202 with a
            poll URL; the non-daemon worker thread finishes (server shutdown
            waits for it) and the same Idempotency-Key later returns the result.
            """
            token, outcome, done = self.token(), {}, threading.Event()
            def run():
                try:
                    outcome['result'] = service.submit(oid, token, body, request_id=key)
                except BaseException as exc:
                    outcome['error'] = exc
                finally:
                    done.set()
            worker_thread = threading.Thread(target=run, name='submission', daemon=False)
            worker_thread.start()
            if not done.wait(submit_wait_seconds):
                return self.send(202, {'accepted': True, 'job': 'running', 'order_id': oid,
                                       'poll': '/orders/'+oid,
                                       'retry': 'repeat this request with the same Idempotency-Key for the result'})
            if 'error' in outcome:
                raise outcome['error']
            return self.send(200, outcome['result'])

        def client_identity(self):
            # Railway's edge sets X-Real-IP to the connecting client address.
            # Trust it only when it is a well-formed IP address (A1-06): a
            # spoofable free-text value would mint a fresh per-client budget
            # (and a fresh durable checkout_rate row) per request. The peer
            # address alone is not used behind the proxy either: it is the
            # edge's address and would share one budget across all clients.
            if deploy:
                candidate = self.headers.get('X-Real-IP')
                if candidate:
                    try:
                        ipaddress.ip_address(candidate)
                        return candidate
                    except ValueError:
                        pass
            return self.client_address[0]

        def with_request_slot(self, handler):
            """Admission for request dispatch (A1-02): the slot is acquired
            only after the request line and headers are parsed, never per
            connection, and exhaustion answers 503 immediately instead of
            queueing. Health probes and webhook delivery stay reachable
            regardless of sales-route saturation."""
            if urlsplit(self.path).path not in SLOT_EXEMPT_PATHS:
                if not self.server.request_slots.acquire(blocking=False):
                    self.close_connection = True
                    return self.send(503, {'error': 'service_busy_retry_later'})
                try:
                    handler()
                finally:
                    self.server.request_slots.release()
            else:
                handler()

        def read_body(self, size, wide=False):
            """Read exactly `size` body bytes inside a bounded window.

            Every read that has not been positively validated first gets the
            short *total* anonymous deadline (A1-02), regardless of any
            presented Authorization header (R-01): an unvalidated Bearer
            value must never widen the window, or trickling fake-credential
            requests could hold dispatch slots indefinitely. The wide
            per-read window exists for large legitimate submit uploads and
            is itself bounded by a total deadline for the same reason.
            """
            per_read = REQUEST_TIMEOUT_SECONDS if wide else UNAUTHENTICATED_WINDOW_SECONDS
            total = SUBMIT_BODY_WINDOW_SECONDS if wide else UNAUTHENTICATED_WINDOW_SECONDS
            deadline = time.monotonic() + total
            self.connection.settimeout(per_read)
            chunks, remaining = [], size
            try:
                while remaining:
                    chunk = self.rfile.read1(min(remaining, 65536))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                    if remaining and time.monotonic() > deadline:
                        break
                return b''.join(chunks)
            finally:
                self.connection.settimeout(UNAUTHENTICATED_WINDOW_SECONDS)

        def validated_submit_body(self):
            """R-01/R2-01/R3-01: the wide body window is granted only on the
            one route with legitimately large bodies — order submit — only
            after the presented credential actually authenticates for that
            order (the credential travels in a header, so it can be checked
            before the body is read), and only when the order would pass
            service.submit's durable admission checks, mirrored by
            service.submit_admission_open: verified payment, no refund
            (including partial external), no active or lost dispute,
            reservation not spent, and rerun window and attempts remaining.
            service.submit refuses every other of those states with
            verified_payment_required_or_refund_active (or the rerun/attempt
            limits) before the payload is decoded, so the wide window can
            never serve an order submit refuses — whose token (freely minted
            unpaid, or paid then disputed/refunded/exhausted) would otherwise
            hold dispatch slots indefinitely by cycling trickling submits
            (auth() is payment-blind by design and is unchanged for reads).
            Mere presence of a Bearer value, any submit-refused order state
            and anything unexpected all keep the anonymous total deadline."""
            match = re.fullmatch(r'/orders/([a-f0-9]{32})/submit', self.path)
            if not match:
                return False
            try:
                with service.lock:
                    order = service.auth(match[1], self.token())
                return service.submit_admission_open(order)
            except (Rejected, KeyError):
                return False

        def view_event(self, kind, **data):
            # Sampled anonymous-view telemetry (A2-01): the skip path takes
            # no service lock and writes no row.
            if _view_event_due(kind):
                with service.lock, service.db:
                    service.event(kind, **data)

        def guarded(self, action):
            if deploy:
                # Staging (public domain behind Railway's TLS proxy): keep a
                # same-origin write guard for browsers, but accept requests with
                # no Origin (server-to-server, health/probe, curl). A present
                # Origin must match the request Host over http or https.
                host = self.headers.get('Host', '')
                origin = self.headers.get('Origin')
                if origin is not None and origin not in ('http://'+host, 'https://'+host):
                    self.send(403, {'error': 'cross_origin_write_denied'})
                    return
            else:
                # DNS-rebinding and browser cross-origin write guard on local demo.
                valid_host = f'127.0.0.1:{self.server.server_port}'
                if self.headers.get('Host') != valid_host or self.headers.get('Origin', 'http://'+valid_host) != 'http://'+valid_host:
                    self.send(403, {'error': 'loopback_same_origin_required'})
                    return
            try:
                action()
            except (Rejected, PaymentError) as exc:
                self.send(409, {'error': str(exc)})
            except (ValueError, KeyError, TypeError):
                self.send(400, {'error': 'invalid_request'})
            except Exception:
                self.send(503, {'error': 'service_unavailable_no_success_claim'})

        def do_GET(self):
            self.with_request_slot(self._do_get)

        def _do_get(self):
            if deploy and self.path in ('/health', '/healthz'):
                return self.health()
            self.guarded(self.get)

        def health(self):
            # Unauthenticated liveness/readiness probe for the platform health
            # check. Confirms the durable business database answers a trivial
            # query; never exposes business data.
            try:
                with service.lock:
                    service.db.execute('SELECT 1').fetchone()
                return self.send(200, {'status': 'ok', 'database': 'reachable'})
            except Exception:
                return self.send(503, {'status': 'degraded', 'database': 'unreachable'})

        def success(self):
            """Checkout-success token delivery (owner decision D.4).

            The verified provider session id in the redirect URL is the buyer's
            proof of purchase, presented over HTTPS with no auth header and
            re-verified server-side against the provider. Unknown, unpaid,
            refunded, expired and mismatched sessions all answer with one
            indistinguishable response; raw errors are never reflected. Token
            material appears only in the response body — never in a URL, log
            or report.
            """
            query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
            if not query:
                return self.send(200, {'message': 'This page grants no access or fulfilment. '
                                                  'Await signed, server-verified payment.'})
            if set(query) != {'session_id'} or len(query['session_id']) != 1:
                return self.send(404, {'error': 'not_found'})
            view = service.success_view(query['session_id'][0])
            as_html = 'text/html' in self.headers.get('Accept', '')
            if view['kind'] == 'tokens':
                if as_html:
                    oid = html.escape(view['order_id'])
                    page = ('<!doctype html><html lang="en"><meta charset="utf-8">'
                            '<meta name="viewport" content="width=device-width">'
                            '<title>Payment verified — sandbox</title>'
                            '<link rel="stylesheet" href="/style.css"><main>'
                            '<h1>Payment verified</h1><p>Order '+oid+'</p>'
                            '<p>Save both capabilities now, separately. They are displayed only while the '
                            'payment session is live; they are never emailed, placed in a URL or re-sent.</p>'
                            '<p>Access token</p><pre>'+view['access_token']+'</pre>'
                            '<p>Recovery token</p><pre>'+view['recovery_token']+'</pre>'
                            '<p>Use <code>Authorization: Bearer</code> with the access token for every order '
                            'request. See pilot/README.md for the authenticated workflow.</p></main></html>')
                    return self.send(200, page, 'text/html')
                return self.send(200, {'order_id': view['order_id'], 'access_token': view['access_token'],
                                       'recovery_token': view['recovery_token']})
            if view['kind'] == 'window_closed':
                guidance = 'POST /recover with your separately retained recovery token.'
                if as_html:
                    oid = html.escape(view['order_id'])
                    page = ('<!doctype html><html lang="en"><meta charset="utf-8">'
                            '<meta name="viewport" content="width=device-width">'
                            '<title>No access or fulfilment — sandbox</title>'
                            '<link rel="stylesheet" href="/style.css"><main>'
                            '<h1>No access or fulfilment</h1>'
                            '<p>This page grants no access or fulfilment. Await signed, server-verified payment.</p>'
                            '<p>Order '+oid+': the payment session is no longer available for token display.</p>'
                            '<p>'+guidance+'</p></main></html>')
                    return self.send(200, page, 'text/html')
                return self.send(200, {'message': 'This page grants no access or fulfilment. '
                                                  'Await signed, server-verified payment.',
                                       'order_id': view['order_id'], 'recovery': guidance})
            return self.send(404, {'error': 'not_found'})

        def get(self):
            if self.path.startswith('/operator/'):
                self.operator_role()  # read or action credential; both may read
                url = urlsplit(self.path)
                if url.path not in OPERATOR_VIEWS:
                    raise Rejected('unknown_operator_route')
                param, view = OPERATOR_VIEWS[url.path]
                query = parse_qs(url.query)
                if set(query) - ({param} if param else set()) or any(len(v) != 1 for v in query.values()):
                    raise Rejected('invalid_state_page')
                args = {k: v[0] for k, v in query.items()}
                if 'after_id' in args:
                    if not args['after_id'].isdigit() or len(args['after_id']) > 18:
                        raise Rejected('invalid_page')
                    args['after_id'] = int(args['after_id'])
                return self.send(200, view(service, args))
            if self.path == '/':
                self.view_event('scope_viewed', source='direct')
                return self.send(200, landing_page(service), 'text/html')
            if self.path == '/requirements':
                self.view_event('requirements_viewed')
                return self.send(200, requirements_text(payment_mode(service),
                                                       sales_open(service)), 'text/plain')
            if self.path == '/style.css':
                css = '''*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
:root{--ink:#0f172a;--body:#334155;--muted:#475569;--accent:#4338ca;--accent-strong:#3730a3;--line:#e5e7eb;--line-strong:#cbd5e1;--surface:#f7f7f9;--dark:#0b1220;--mono:ui-monospace,SFMono-Regular,Consolas,Menlo,monospace;--s1:.5rem;--s2:1rem;--s3:1.5rem;--s4:2.5rem;--s5:4.5rem}
body{margin:0;font:16px/1.65 system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;background:#fff;color:var(--body);-webkit-font-smoothing:antialiased}
main{max-width:none;margin:0;padding:0}
.wrap{max-width:1120px;margin:0 auto;padding:0 var(--s3)}
.skip{position:absolute;left:-9999px}.skip:focus{position:static;display:inline-block;padding:.4rem .7rem;background:#fff;border-radius:6px;box-shadow:0 0 0 2px var(--accent)}
.sr{position:absolute;left:-9999px}
a{color:var(--accent);text-underline-offset:.18em}a:hover{color:var(--accent-strong)}
a:focus-visible,button:focus-visible,input:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:3px;border-radius:4px}
h1,h2,h3{color:var(--ink)}
h1{margin:0 0 var(--s2);font-size:clamp(1.6rem,3vw,2.2rem);line-height:1.15;letter-spacing:-.02em}
h2{font-size:clamp(1.55rem,3vw,2.05rem);line-height:1.15;letter-spacing:-.025em;margin:0 0 var(--s3);font-weight:650}
header.site{border-bottom:1px solid var(--line);background:#fff}
.bar{display:flex;align-items:center;justify-content:space-between;gap:var(--s2);padding-top:.85rem;padding-bottom:.85rem}
.brand{margin:0;font:600 .95rem var(--mono);letter-spacing:-.01em}.brand a{color:var(--ink);text-decoration:none}
nav.top{display:flex;gap:1.4rem;flex-wrap:wrap}
nav.top a{color:var(--muted);text-decoration:none;font-size:.9rem}
nav.top a:hover{color:var(--ink);text-decoration:underline}
.notice{border-bottom:1px solid transparent}
.notice .wrap{padding-top:.6rem;padding-bottom:.6rem;font-size:.9rem}
.notice.sandbox{background:#fdf3d7;border-color:#d9b441;color:#5c4a12}
.notice.sandbox a,.notice.test a,.notice.open a,.notice.paused a{color:inherit;font-weight:600}
.notice.test{background:#e6efff;border-color:#9db9ec;color:#1e3f7d}
.notice.open{background:#e9f9ef;border-color:#93d4ac;color:#14532d}
.notice.paused{background:#fdebd2;border-color:#e0a458;color:#7c3a12}
section{padding:var(--s5) 0}
section.alt{background:var(--surface);border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.hero{padding:var(--s5) 0 calc(var(--s5) - 1rem);background:#fff}
.hero-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.05fr);gap:var(--s4);align-items:center}
.eyebrow{margin:0 0 var(--s2);font:500 .78rem var(--mono);letter-spacing:.04em;text-transform:uppercase;color:var(--accent)}
.hero-copy h1{font-size:clamp(2.5rem,6.2vw,4.1rem);line-height:1.02;letter-spacing:-.04em;font-weight:700;margin-bottom:var(--s3)}
.lede{margin:0 0 var(--s3);max-width:32rem;color:var(--muted);font-size:1.12rem;line-height:1.6}
.price{margin:0 0 .35rem;font-size:1.9rem;font-weight:700;letter-spacing:-.02em;color:var(--ink)}
.price span{font-size:.9rem;font-weight:400;letter-spacing:0;color:var(--muted)}
.pairline{margin:0 0 var(--s3);color:var(--muted);font:.84rem/1.5 var(--mono)}
.pairline strong{color:var(--ink);font-weight:600}
.actions{margin:0 0 var(--s2);display:flex;flex-wrap:wrap;gap:.75rem;align-items:center}
.cta{display:inline-block;padding:.72rem 1.4rem;border-radius:6px;background:var(--accent);color:#fff;font-weight:600;text-decoration:none;border:1px solid var(--accent)}
.cta:hover{background:var(--accent-strong);border-color:var(--accent-strong);color:#fff}
.cta.ghost{background:transparent;color:var(--ink);border-color:var(--line-strong)}
.cta.ghost:hover{background:var(--surface);color:var(--ink);border-color:var(--ink)}
.micro{margin:0;color:var(--muted);font-size:.84rem}
.window{border-radius:10px;border:1px solid #1e293b;background:var(--dark);color:#dbe4f3;box-shadow:0 1px 2px rgba(15,23,42,.08),0 24px 48px -12px rgba(15,23,42,.28);overflow:hidden}
.window-top{display:flex;align-items:center;gap:.4rem;padding:.65rem 1rem;border-bottom:1px solid #1e293b;background:#0e1729}
.wd{width:.6rem;height:.6rem;border-radius:50%;background:#334155;display:inline-block}
.wtitle{margin-left:.4rem;margin-right:auto;font:.76rem var(--mono);color:#8fa1bd}
.badge{font:700 .68rem var(--mono);letter-spacing:.06em;padding:.16rem .5rem;border-radius:4px;background:#33270a;color:#f5c542;border:1px solid #6b5210}
.window-body{padding:1.1rem 1.15rem 1.15rem}
.wpair{margin:0 0 .85rem;font-size:.82rem;color:#a9b8d0}
.wpair code{color:#cbd5f0;font-size:.8rem}
.chips{list-style:none;display:flex;flex-wrap:wrap;gap:.4rem;margin:0 0 1rem;padding:0}
.chips li{font:.76rem var(--mono);color:#a9b8d0;background:#141f36;border:1px solid #24324d;border-radius:4px;padding:.2rem .6rem}
.chips strong{color:#e7edf7}
.chips li.bad{border-color:#6b5210;color:#f5c542}.chips li.bad strong{color:#ffd978}
.tablewrap{overflow-x:auto}
.window table{border-collapse:collapse;width:100%;font:.75rem/1.5 var(--mono)}
.window th,.window td{padding:.45rem .6rem;border-bottom:1px solid #1c2a44;text-align:left;white-space:nowrap}
.window th{color:#8fa1bd;font-weight:600;border-bottom:1px solid #2a3a5c}
.window tr.flagged td{color:#f5c542}
.wfoot{margin:.9rem 0 0;font-size:.8rem;color:#8fa1bd}
.wfoot a{color:#a5b4fc}
.cards{display:grid;grid-template-columns:repeat(3,1fr);border:1px solid var(--line);border-radius:10px;background:#fff;overflow:hidden}
.cards.two{grid-template-columns:1fr 1fr}
.card{padding:var(--s3) var(--s3) calc(var(--s3) + .25rem);border-left:1px solid var(--line)}
.card:first-child{border-left:0}
.card h3{margin:0 0 .6rem;font-size:1rem;font-weight:650;letter-spacing:-.005em}
.card ul{margin:.2rem 0 0;padding-left:1.15rem;color:var(--muted)}
.card li{margin:.45rem 0}
.card p{margin:0;color:var(--muted)}
.steps{list-style:none;counter-reset:step;padding:0;margin:0;border-top:1px solid var(--line-strong)}
.steps li{counter-increment:step;position:relative;padding:1.2rem 0 1.2rem 3.4rem;border-bottom:1px solid var(--line);color:var(--muted);max-width:52rem}
.steps li strong{color:var(--ink)}
.steps li::before{content:"0" counter(step);position:absolute;left:0;top:1.2rem;font:500 .85rem/1.65 var(--mono);color:var(--accent)}
.note{margin:var(--s3) 0 0;padding:.85rem 1.05rem;border-left:2px solid var(--accent);background:var(--surface);color:var(--muted);font-size:.93rem;max-width:52rem}
details{border-bottom:1px solid var(--line);padding:0;margin:0;max-width:52rem}
section#faq details:first-of-type{border-top:1px solid var(--line-strong)}
details p{margin:0 0 1.1rem;color:var(--muted)}
summary{cursor:pointer;font-weight:600;color:var(--ink);padding:1rem 0}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#fff;border:1px solid var(--line);border-radius:8px;padding:var(--s2);margin:.4rem 0 var(--s2);font:.8rem/1.55 var(--mono);color:var(--body)}
code{font-family:var(--mono);font-size:.92em}
.order-note{color:var(--muted);max-width:46rem;margin:0 0 var(--s2)}
fieldset{border:1px solid var(--line-strong);border-radius:10px;background:#fff;padding:var(--s2) var(--s3) var(--s3);margin:0;max-width:46rem}
legend{font-weight:650;color:var(--ink);padding:0 .4rem}
label{display:block;margin:.8rem 0 .3rem;font-weight:600;color:var(--ink)}
 input[type=text],select{display:block;width:100%;max-width:26rem;padding:.6rem .7rem;font:inherit;border:1px solid #94a3b8;border-radius:6px;background:#fff;color:var(--ink)}
label.attest{font-weight:400;color:var(--body);max-width:44rem;margin:1rem 0 var(--s2)}
label.attest input{margin-right:.5rem}
button{background:var(--accent);color:#fff;border:0;border-radius:6px;padding:.75rem 1.4rem;font:inherit;font-weight:600;cursor:pointer}
button:hover:not([disabled]){background:var(--accent-strong)}
button[disabled]{background:#98a3b8;cursor:not-allowed}
#result{min-height:1rem;margin-top:var(--s2)}#result a{display:inline-block;margin-top:.6rem}
#order p:last-child{max-width:46rem;color:var(--muted);font-size:.93rem}
footer{background:var(--dark);padding:var(--s4) 0;color:#94a3b8}
footer .wrap p{margin:.4rem 0;font-size:.88rem}
footer nav{margin-bottom:var(--s2);padding-bottom:var(--s2);border-bottom:1px solid #1e293b}footer nav a{display:inline-block;margin:0 1.4rem .3rem 0;color:#e2e8f0;text-decoration:none;font-weight:500}
footer nav a:hover,footer a:hover{color:#fff;text-decoration:underline}
footer a[href^="mailto"]{color:#c7d2fe}
footer a:focus-visible{outline-color:#a5b4fc}
@media (max-width:900px){.hero-grid{grid-template-columns:minmax(0,1fr)}.cards,.cards.two{grid-template-columns:1fr}.card{border-left:0;border-top:1px solid var(--line)}.card:first-child{border-top:0}:root{--s5:3rem}}
@media (max-width:560px){.bar{flex-direction:column;align-items:flex-start;gap:.5rem}.wrap{padding:0 1rem}.cta{flex:1 1 100%;text-align:center}.steps li{padding-left:2.6rem}}'''
                return self.send(200, css, 'text/css')
            if self.path == '/app.js':
                return self.send(200, SCRIPT, 'application/javascript')
            if self.path == '/sample':
                self.view_event('sample_viewed')
                sample = ROOT/'evidence/gate1/verification-clean/maintenance/results.json'
                return self.send(200, report_html(json.loads(sample.read_bytes())), 'text/html')
            if self.path == '/success' or self.path.startswith('/success?'):
                return self.success()
            match = re.fullmatch(r'/simulated-checkout/(cs_test_[a-f0-9]{32})', self.path)
            if match and service.provider.simulated:
                service.provider.session(match[1])
                page = '<h1>SIMULATION ONLY — no Stripe payment</h1><form method="post" action="/simulate-payment/'+match[1]+'"><button>Simulate signed payment event</button></form>'
                return self.send(200, page, 'text/html')
            match = re.fullmatch(r'/orders/([a-f0-9]{32})(?:/(results.json|junit.xml|report.html))?', self.path)
            if match:
                oid, name = match.groups()
                if name:
                    mime = {'results.json':'application/json','junit.xml':'application/xml','report.html':'text/html'}[name]
                    return self.send(200, service.report(oid, self.token(), name), mime)
                return self.send(200, service.status(oid, self.token()))
            legal = LEGAL_PAGES.get(urlsplit(self.path).path)
            if legal:
                # Public legal page: verbatim packaged markdown, no event row.
                return self.send(200, (LEGAL_DIR/legal).read_bytes(), 'text/markdown')
            return self.send(404, {'error': 'not_found'})

        def do_POST(self):
            self.with_request_slot(self._do_post)

        def _do_post(self):
            self.guarded(self.post)

        def post(self):
            if self.headers.get('Transfer-Encoding'):
                raise Rejected('chunked_requests_disabled')
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 <= size <= service.policy['max_request_bytes']:
                self.close_connection = True
                return self.send(413, {'error': 'request_size_limit'})
            if urlsplit(self.path).path == '/webhook' and size > 262144:
                # Per-route cap hoisted above the read (A2-04): an oversized
                # webhook is refused before its body is buffered at all.
                self.close_connection = True
                return self.send(409, {'error': 'webhook_size_limit'})
            raw = self.read_body(size, wide=self.validated_submit_body())
            if len(raw) != size:
                self.close_connection = True
                raise Rejected('truncated_request')
            match = re.fullmatch(r'/simulate-payment/(cs_test_[a-f0-9]{32})', self.path)
            if match and service.provider.simulated:
                import hashlib, uuid
                sid = match[1]
                service.provider.set_payment_status(sid, 'paid')
                event = json.dumps({'id': 'evt_'+uuid.uuid4().hex, 'livemode': False,
                    'type': 'checkout.session.completed', 'data': {'object': {'id': sid}}}).encode()
                stamp = str(int(service.clock()))
                sig = hmac.new(service.secret.encode(), stamp.encode()+b'.'+event, hashlib.sha256).hexdigest()
                service.webhook(event, 't='+stamp+',v1='+sig)
                # Mirror the hosted-Checkout success redirect (D.4): the buyer
                # returns to the success page carrying the provider session id.
                page = ('<!doctype html><html lang="en"><meta charset="utf-8">'
                        '<meta http-equiv="refresh" content="0;url=/success?session_id='+sid+'">'
                        '<meta name="viewport" content="width=device-width">'
                        '<title>SIMULATION ONLY — payment simulated</title>'
                        '<link rel="stylesheet" href="/style.css"><main>'
                        '<h1>SIMULATION ONLY — no Stripe payment</h1>'
                        '<p>Signed event delivered. Returning to the payment-success page…</p>'
                        '<p><a href="/success?session_id='+sid+'">Open the success page</a></p></main></html>')
                return self.send(200, page, 'text/html')
            if self.path == '/webhook':
                return self.send(200, service.webhook(raw, self.headers.get('Stripe-Signature', '')))
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise Rejected('json_only_no_archives')
            body = json.loads(raw)
            if self.path == '/operator/action':
                role = self.operator_role()
                if set(body) != {'name', 'arguments'}:
                    raise Rejected('operator_action_forbidden')
                from .operator import READ_ONLY, dispatch
                if role != 'action' and body['name'] not in READ_ONLY:
                    raise Rejected('operator_read_only_credential')
                return self.send(200, dispatch(service, body['name'], body['arguments']))
            if self.path == '/checkout':
                key = self.headers.get('Idempotency-Key')
                if not key:
                    raise Rejected('checkout_idempotency_key_required')
                if body.get('buyer_type') not in ('business', 'consumer') or body.get('terms_accepted') is not True:
                    raise Rejected('buyer_type_and_terms_acceptance_required')
                return self.send(201, service.checkout(body['product'], body['attested'], body.get('source','direct'),
                                                       request_id=key, client=self.client_identity(),
                                                       buyer_type=body['buyer_type'], terms_accepted=body['terms_accepted'],
                                                       immediate_performance_requested=body.get('immediate_performance_requested', False),
                                                       cancellation_acknowledged=body.get('cancellation_acknowledged', False)))
            if self.path == '/recover':
                return self.send(200, service.recover(body['order_id'], body['recovery_token']))
            match = re.fullmatch(r'/orders/([a-f0-9]{32})/(submit|support)', self.path)
            if match:
                oid, action = match.groups()
                if action == 'submit':
                    key = self.headers.get('Idempotency-Key')
                    if not key:
                        raise Rejected('idempotency_key_required')
                    return self.submit_bounded(oid, body, key)
                if set(body) != {'topic'}:
                    raise Rejected('support_topics_only_no_customer_content')
                return self.send(200, service.support(oid, self.token(), body['topic']))
            return self.send(404, {'error': 'not_found'})

    class Server(ThreadingHTTPServer):
        # Wait for request threads on shutdown; do not close durable state under a job.
        daemon_threads = False

        def __init__(self, *args, **kwargs):
            # Bounds concurrent request *dispatch* (A1-02); connections and
            # idle keep-alives are never counted against it.
            self.request_slots = threading.BoundedSemaphore(8)
            super().__init__(*args, **kwargs)

        # No per-connection slot acquisition (A1-02): process_request and
        # process_request_thread use the socketserver defaults, so stalled
        # handshakes cannot occupy dispatch capacity.

        def serve_forever(self, poll_interval=.5):
            stop = threading.Event()
            def recover():
                while not stop.is_set():
                    try:
                        service.reconcile_incomplete_submissions()
                        service.reconcile_reservations()
                        service.reconcile_raw_objects()
                    except Exception:
                        # Disk/provider outages cannot turn an obligation into success.
                        # Retry on the next timer tick, including after process restart.
                        pass
                    stop.wait(1)
            recovery = threading.Thread(target=recover, name='reservation-recovery')
            recovery.start()
            try:
                super().serve_forever(poll_interval)
            finally:
                stop.set()
                recovery.join()

    return Server(('0.0.0.0' if deploy else '127.0.0.1', port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    args.state.mkdir(parents=True, exist_ok=True)
    provider = DurableOfflineStripe(args.state/'provider.sqlite3')
    service = Service(args.state, provider, secrets.token_urlsafe(32))
    server = make_server(service, args.port)
    print(f'Public sandbox: http://127.0.0.1:{server.server_port} — simulation only', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        service.close()
        provider.close()


if __name__ == '__main__':
    main()
