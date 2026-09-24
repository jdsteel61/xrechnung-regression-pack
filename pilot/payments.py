"""Stripe sandbox adapter and deliberately separate offline test double.

No environment or credential files are read. The caller must explicitly supply
test credentials after owner authorisation. The local server uses OfflineStripe.
"""
import hashlib
import hmac
import json
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


class PaymentError(ValueError):
    pass


# ASCII-anchored provider identifier shapes (finding A1-04, kept consistent
# with pilot/service.py): str.isalnum() also accepts Unicode, and these ids
# flow into provider URL paths. Provider ids (pi_/ch_/re_/cs_) use the
# underscore form; order ids follow the dashed client_reference_id form.
_PROVIDER_ID_SHAPE = re.compile(r'[A-Za-z0-9_]+')
_ORDER_ID_SHAPE = re.compile(r'[A-Za-z0-9-]+')
# Length bounds beside the shape checks (R-04): real provider ids are short,
# and refusing oversized ids before any request keeps a webhook-supplied
# identifier from building huge provider URL paths (defense-in-depth behind
# the 256 KiB webhook body cap).
_PROVIDER_ID_MAX = 255
_ORDER_ID_MAX = 255


def verify_event(raw, signature, secret, now=None, livemode=False):
    # `secret` and `livemode` each accept a single value or a tuple: a
    # dual-era (live-cutover) deployment verifies events signed with either
    # era's endpoint secret and accepts either era's livemode flag. Single
    # values keep exactly the previous behaviour, including the identity
    # (`is`) semantics of the livemode check.
    secrets_ = tuple(secret) if isinstance(secret, (tuple, list)) else (secret,)
    allowed = tuple(livemode) if isinstance(livemode, (tuple, list, set, frozenset)) else (livemode,)
    try:
        fields = [part.split('=', 1) for part in signature.split(',')]
        stamps = [v for k, v in fields if k == 't']
        if len(stamps) != 1:
            raise ValueError()
        stamp = int(stamps[0])
        if abs((time.time() if now is None else now) - stamp) > 300:
            raise ValueError()
        signed = [v for k, v in fields if k == 'v1']
        if not any(hmac.compare_digest(
                hmac.new(s.encode(), str(stamp).encode()+b'.'+raw, hashlib.sha256).hexdigest(), v)
                for s in secrets_ for v in signed):
            raise ValueError()
        event = json.loads(raw)
        if not any(event.get('livemode') is mode for mode in allowed) or not isinstance(event.get('id'), str):
            raise ValueError()
        return event
    except (ValueError, TypeError, KeyError):
        raise PaymentError('invalid_sandbox_webhook') from None


class StripeSandbox:
    """Supported HTTPS API, hosted checkout, GBP 299, zero tax ONLY in sandbox.

    Tax configuration is a launch blocker; this adapter cannot accept live keys.
    """
    simulated = False

    def __init__(self, test_key, public_origin, live=False):
        # live=True is reachable only through a validated LaunchConfig; the
        # key prefix must then match, so a test key can never run live or vice versa.
        prefix = 'sk_live_' if live else 'sk_test_'
        if not test_key.startswith(prefix) or not public_origin.startswith('https://'):
            raise PaymentError('test_key_and_https_required')
        self.key, self.origin, self.live = test_key, public_origin.rstrip('/'), live
        self.session_prefix = 'cs_live_' if live else 'cs_test_'

    def request(self, method, path, fields=None, idempotency=None, raw=False):
        headers = {'Authorization': 'Bearer '+self.key}
        if idempotency:
            headers['Idempotency-Key'] = idempotency
        if method == 'GET' and fields:
            path += '?' + urllib.parse.urlencode(fields)
            data = None
        else:
            data = urllib.parse.urlencode(fields).encode() if fields is not None else None
        req = urllib.request.Request('https://api.stripe.com/v1/'+path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.load(response)
        if raw:
            return result
        if result.get('object') == 'refund':
            # Stripe's Refund schema has no livemode field. Verify the linked
            # PaymentIntent through the same test credential before normalizing.
            pi = result.get('payment_intent')
            if not isinstance(pi, str) or not pi.startswith('pi_') or len(pi) > _PROVIDER_ID_MAX \
                    or not _PROVIDER_ID_SHAPE.fullmatch(pi):
                raise PaymentError('refund_missing_payment_intent')
            intent = self.request('GET', 'payment_intents/'+pi)
            if intent.get('id') != pi:
                raise PaymentError('refund_payment_intent_mismatch')
            result = dict(result, livemode=self.live)
        if result.get('object') == 'list':
            if not isinstance(result.get('data'), list) or any(
                    item.get('livemode') is not self.live for item in result['data']):
                raise PaymentError('not_sandbox_object')
        elif result.get('livemode') is not self.live:
            raise PaymentError('not_sandbox_object')
        return result

    def payment_state(self, payment_intent):
        """Provider source of truth for money movement on one PaymentIntent.

        Includes refunds and disputes created outside this application (e.g.
        the Stripe dashboard), so the ledger can converge without DB edits.
        """
        if not isinstance(payment_intent, str) or not payment_intent.startswith('pi_') \
                or len(payment_intent) > _PROVIDER_ID_MAX \
                or not _PROVIDER_ID_SHAPE.fullmatch(payment_intent):
            raise PaymentError('invalid_payment_intent')
        try:
            intent = self.request('GET', 'payment_intents/'+payment_intent)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None  # authoritative provider absence
            raise
        if intent.get('id') != payment_intent:
            raise PaymentError('payment_intent_mismatch')
        refunds =self.request('GET', 'refunds', {'payment_intent': payment_intent, 'limit': 100}, raw=True)
        disputes = self.request('GET', 'disputes', {'payment_intent': payment_intent, 'limit': 100})
        if refunds.get('object') != 'list' or refunds.get('has_more') or disputes.get('has_more'):
            raise PaymentError('payment_state_incomplete')
        items = []
        for refund in refunds['data']:
            if refund.get('object') != 'refund' or refund.get('payment_intent') != payment_intent:
                raise PaymentError('refund_payment_intent_mismatch')
            items.append(dict(refund, livemode=self.live))
        return {'payment_intent': payment_intent, 'livemode': self.live,
                'amount_received': intent.get('amount_received'), 'refunds': items,
                'disputes': [{'id': d['id'], 'status': d.get('status'), 'amount': d.get('amount')}
                             for d in disputes['data']]}

    def payment_intent_for_charge(self, charge_id):
        """Resolve a charge-referenced event (e.g. a dispute) to its intent."""
        if not isinstance(charge_id, str) or not charge_id.startswith('ch_') \
                or len(charge_id) > _PROVIDER_ID_MAX \
                or not _PROVIDER_ID_SHAPE.fullmatch(charge_id):
            raise PaymentError('invalid_charge')
        return self.request('GET', 'charges/'+charge_id).get('payment_intent')

    # Stripe's minimum Checkout lifetime (30 min) plus margin. An unpaid session
    # self-expires at the provider, so an abandoned reservation is short-lived.
    session_ttl_seconds = 1860

    def checkout(self, order):
        return self.request('POST', 'checkout/sessions', {
            'expires_at': int(time.time()) + self.session_ttl_seconds,
            # Sandboxes default to Managed Payments, which rejects an explicit
            # payment_method_types. Disable it per-request to keep the
            # deterministic card-only sandbox scope this adapter asserts.
            'mode': 'payment', 'managed_payments[enabled]': 'false',
            'payment_method_types[0]': 'card',
            'line_items[0][price_data][currency]': 'gbp',
            'line_items[0][price_data][unit_amount]': 29900,
            'line_items[0][price_data][product_data][name]': 'Release Regression Pack',
            'line_items[0][quantity]': 1, 'client_reference_id': order,
            'metadata[order_id]': order,
            'success_url': self.origin+'/success?session_id={CHECKOUT_SESSION_ID}', 'cancel_url': self.origin+'/',
        }, 'checkout:'+order)

    def session(self, session_id):
        if not session_id.startswith(self.session_prefix) or len(session_id) > _PROVIDER_ID_MAX \
                or not _PROVIDER_ID_SHAPE.fullmatch(session_id):
            raise PaymentError('invalid_session_id')
        try:
            return self.request('GET', 'checkout/sessions/'+session_id)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                # A well-formed sandbox session the provider no longer knows is
                # provider absence, not an error: reconciliation treats absence
                # as authoritative for orders that were never charged.
                return None
            raise

    def find_checkout(self, order, created=None):
        """Recover a lost Checkout response without treating a partial list as absence."""
        if not isinstance(order, str) or len(order) > _ORDER_ID_MAX \
                or not _ORDER_ID_SHAPE.fullmatch(order):
            raise PaymentError('invalid_order_id')
        if type(created) not in (int, float) or created < 0:
            raise PaymentError('checkout_creation_time_required')
        fields = {'created[gte]': max(0, int(created)-300),
                  'created[lte]': int(created)+86400, 'limit': 100}
        found = None
        for _ in range(10):
            page = self.request('GET', 'checkout/sessions', fields)
            if page.get('object') != 'list' or not isinstance(page.get('data'), list):
                raise PaymentError('invalid_checkout_list')
            for session in page['data']:
                if (session.get('client_reference_id') == order
                        and session.get('metadata', {}).get('order_id') == order):
                    if found is not None and found['id'] != session['id']:
                        raise PaymentError('duplicate_checkout_sessions')
                    found = session
            if not page.get('has_more'):
                return found
            if not page['data'] or not isinstance(page['data'][-1].get('id'), str):
                raise PaymentError('invalid_checkout_cursor')
            fields['starting_after'] = page['data'][-1]['id']
        raise PaymentError('checkout_search_incomplete')

    def expire_session(self, session_id):
        session = self.session(session_id)
        if session is None:
            raise PaymentError('checkout_absent')
        if session.get('payment_status') == 'paid' or session.get('status') == 'expired':
            return session
        if session.get('status') != 'open':
            raise PaymentError('checkout_expiry_unconfirmed')
        try:
            return self.request('POST', 'checkout/sessions/'+session_id+'/expire', {},
                                'expire:'+session_id)
        except urllib.error.HTTPError:
            # A concurrent payment or expiry can win the race. Observe the
            # provider's current state before changing the local obligation.
            session = self.session(session_id)
            if session is None:
                raise PaymentError('checkout_absent')
            if session.get('payment_status') == 'paid' or session.get('status') == 'expired':
                return session
            raise PaymentError('checkout_expiry_unconfirmed') from None

    def refund(self, payment_intent, order, attempt=0):
        # Each bounded re-issue after a provider-failed refund needs a new key.
        key = 'refund:'+order+(':%d' % attempt if attempt else '')
        return self.request('POST', 'refunds', {'payment_intent': payment_intent,
                            'metadata[order_id]': order}, key)

    def refund_status(self, refund_id):
        if not refund_id.startswith('re_') or len(refund_id) > _PROVIDER_ID_MAX \
                or not _PROVIDER_ID_SHAPE.fullmatch(refund_id):
            raise PaymentError('invalid_refund_id')
        return self.request('GET', 'refunds/'+refund_id)


class EraRoutingStripe:
    """Dual-era routing wrapper: a LIVE primary client plus an optional
    TEST-era legacy client for orders predating the live cutover.

    The production database carries TEST-era Stripe orders (cs_test_ sessions,
    unmarked pi_ intents) while the service now runs with a LIVE key. Checkout
    session ids carry a real era marker (cs_test_/cs_live_) and route by
    prefix; every other Stripe object id is era-unmarked in reality, so the
    owning client is resolved by authoritative absence (a 404 from one
    credential means the other owns the id) before any request with side
    effects is issued. A TEST-era id with no legacy client fails closed
    (legacy_provider_unavailable) instead of crossing eras.
    """
    simulated = False
    live = True
    era_routing = True
    session_prefixes = ('cs_live_', 'cs_test_')

    def __init__(self, primary, legacy=None):
        # The primary must be the LIVE-era client, and the legacy must never be
        # live. Wrapping a simulator here would present simulated money as a
        # live obligation, so both are refused at construction.
        if getattr(primary, 'simulated', False) or getattr(primary, 'live', False) is not True:
            raise PaymentError('primary_provider_must_be_live')
        if legacy is not None and (getattr(legacy, 'simulated', False)
                                   or getattr(legacy, 'live', False) is True):
            raise PaymentError('legacy_provider_must_be_test_era')
        self.primary, self.legacy = primary, legacy

    def _legacy_client(self):
        if self.legacy is None:
            raise PaymentError('legacy_provider_unavailable')
        return self.legacy

    def _route(self, identifier, kind, error):
        """Route one era-marked provider id to its era's client, fail closed.

        Refuses oversized or malformed ids before any client call, mirroring
        the shape checks of StripeSandbox (A1-04/R-04), so a webhook-supplied
        identifier can never build a provider path on the wrong credential.
        A well-formed id without an era marker is ambiguous, not live.
        """
        if not isinstance(identifier, str) or not identifier.startswith(kind) \
                or len(identifier) > _PROVIDER_ID_MAX or not _PROVIDER_ID_SHAPE.fullmatch(identifier):
            raise PaymentError(error)
        if identifier.startswith(kind+'test_'):
            return self._legacy_client()
        if identifier.startswith(kind+'live_'):
            return self.primary
        raise PaymentError(error)

    def checkout(self, order):
        # New purchases are always the live era.
        return self.primary.checkout(order)

    def session(self, session_id):
        return self._route(session_id, 'cs_', 'invalid_session_id').session(session_id)

    def expire_session(self, session_id):
        return self._route(session_id, 'cs_', 'invalid_session_id').expire_session(session_id)

    def find_checkout(self, order, created=None):
        """Search the live era first, then the legacy era if present.

        A match in BOTH eras is ambiguous evidence for one order's money and
        is refused (duplicate_checkout_sessions), exactly as two same-era
        sessions are by StripeSandbox.find_checkout.
        """
        if not isinstance(order, str) or len(order) > _ORDER_ID_MAX \
                or not _ORDER_ID_SHAPE.fullmatch(order):
            raise PaymentError('invalid_order_id')
        if type(created) not in (int, float) or created < 0:
            raise PaymentError('checkout_creation_time_required')
        found = self.primary.find_checkout(order, created)
        if self.legacy is not None:
            legacy_found = self.legacy.find_checkout(order, created)
            if legacy_found is not None:
                if found is not None:
                    raise PaymentError('duplicate_checkout_sessions')
                found = legacy_found
        return found

    def payment_state(self, payment_intent):
        # Explicitly era-marked intents route directly; real Stripe pi_ ids
        # carry no era marker, so absence resolves the owning client: the id
        # exists in exactly one era, and a 404 from the live credential is
        # authoritative evidence the TEST-era client owns it.
        client = self._intent_client(payment_intent, 'invalid_payment_intent')
        if client is None:
            return None  # absent in every era consulted
        return client.payment_state(payment_intent)

    def _intent_client(self, payment_intent, error):
        """Resolve the client owning one payment intent, never guessing.

        Era-marked ids (pi_test_/pi_live_) route by marker. Unmarked ids are
        resolved by query: primary first, then legacy on the primary's
        authoritative absence. Returns None only when every era reports
        absence; a missing legacy client means the primary's answer stands.
        """
        if not isinstance(payment_intent, str) or not payment_intent.startswith('pi_') \
                or len(payment_intent) > _PROVIDER_ID_MAX or not _PROVIDER_ID_SHAPE.fullmatch(payment_intent):
            raise PaymentError(error)
        if payment_intent.startswith('pi_test_'):
            return self._legacy_client()
        if payment_intent.startswith('pi_live_'):
            return self.primary
        state = self.primary.payment_state(payment_intent)
        if state is not None or self.legacy is None:
            return self.primary
        if self.legacy.payment_state(payment_intent) is not None:
            return self.legacy
        return None

    def payment_intent_for_charge(self, charge_id):
        # Era-marked charges (ch_test_) route by marker; every other
        # well-formed charge id is resolved by the primary credential first
        # and falls back to the legacy only on its authoritative 404.
        if not isinstance(charge_id, str) or not charge_id.startswith('ch_') \
                or len(charge_id) > _PROVIDER_ID_MAX or not _PROVIDER_ID_SHAPE.fullmatch(charge_id):
            raise PaymentError('invalid_charge')
        if charge_id.startswith('ch_test_'):
            return self._legacy_client().payment_intent_for_charge(charge_id)
        try:
            return self.primary.payment_intent_for_charge(charge_id)
        except urllib.error.HTTPError as exc:
            if exc.code != 404 or self.legacy is None:
                raise
            return self.legacy.payment_intent_for_charge(charge_id)

    def refund(self, payment_intent, order, attempt=0):
        # Resolve the owning era BEFORE any write: a POST must never carry an
        # idempotency key to the wrong credential.
        client = self._intent_client(payment_intent, 'invalid_payment_intent')
        if client is None:
            raise PaymentError('payment_intent_absent')
        return client.refund(payment_intent, order, attempt)

    def refund_status(self, refund_id):
        # Real Stripe refund ids carry no era marker: try the live credential,
        # fall back to legacy only on its authoritative 404.
        if not isinstance(refund_id, str) or not refund_id.startswith('re_') \
                or len(refund_id) > _PROVIDER_ID_MAX or not _PROVIDER_ID_SHAPE.fullmatch(refund_id):
            raise PaymentError('invalid_refund_id')
        if refund_id.startswith('re_test_'):
            return self._legacy_client().refund_status(refund_id)
        if refund_id.startswith('re_live_'):
            return self.primary.refund_status(refund_id)
        try:
            return self.primary.refund_status(refund_id)
        except urllib.error.HTTPError as exc:
            if exc.code != 404 or self.legacy is None:
                raise
            return self.legacy.refund_status(refund_id)


class OfflineStripe:
    """In-memory transport double, never represented as Stripe-hosted checkout."""
    simulated = True

    def __init__(self):
        self.sessions, self.refunds = {}, {}
        self.refund_calls = 0

    def checkout(self, order):
        sid = 'cs_test_'+order
        self.sessions.setdefault(sid, {'id': sid, 'livemode': False, 'mode': 'payment',
            'amount_subtotal': 29900, 'amount_total': 29900, 'currency': 'gbp',
            'total_details': {'amount_tax': 0, 'amount_discount': 0, 'amount_shipping': 0},
            'payment_status': 'unpaid', 'payment_intent': 'pi_'+order,
            'client_reference_id': order, 'metadata': {'order_id': order},
            'url': '/simulated-checkout/'+sid})
        return dict(self.sessions[sid])

    def session(self, sid):
        if sid not in self.sessions:
            raise PaymentError('unknown_session')
        return dict(self.sessions[sid])

    def find_checkout(self, order, created=None):
        return self.session('cs_test_'+order) if 'cs_test_'+order in self.sessions else None

    def expire_session(self, sid):
        session = self.sessions[sid]
        if session['payment_status'] != 'paid':
            session['status'] = 'expired'
        return dict(session)

    def set_payment_status(self, sid, status):
        if self.sessions[sid].get('status') == 'expired':
            raise PaymentError('session_expired')
        self.sessions[sid]['payment_status'] = status

    def refund(self, payment_intent, order, attempt=0):
        self.refund_calls += 1
        if any(r['payment_intent'] == payment_intent and r['status'] in ('pending', 'succeeded')
               and r['id'] != 're_'+order+('_%d' % attempt if attempt else '')
               for r in self.refunds.values()):
            raise PaymentError('charge_already_refunded')
        rid = 're_'+order+('_%d' % attempt if attempt else '')
        self.refunds.setdefault(rid, {'id': rid, 'livemode': False, 'status': 'pending',
            'payment_intent': payment_intent, 'amount': 29900, 'currency': 'gbp',
            'metadata': {'order_id': order}})
        return dict(self.refunds[rid])

    def refund_status(self, rid):
        return dict(self.refunds[rid])

    # Test doubles for money movement initiated outside the application.
    disputes = None
    charges = None

    def external_refund(self, payment_intent, amount=29900, status='succeeded'):
        rid = 're_external_%d' % len(self.refunds)
        self.refunds[rid] = {'id': rid, 'livemode': False, 'status': status, 'payment_intent': payment_intent,
                             'amount': amount, 'currency': 'gbp', 'metadata': {}}
        return rid

    def external_dispute(self, payment_intent, status='needs_response'):
        self.disputes = self.disputes or {}
        did = 'dp_%d' % len(self.disputes)
        self.disputes[did] = {'id': did, 'status': status, 'amount': 29900, 'payment_intent': payment_intent}
        return did

    def charge_for(self, payment_intent):
        """Test double: the charge id Stripe references in dispute events."""
        self.charges = self.charges or {}
        for cid, pi in self.charges.items():
            if pi == payment_intent:
                return cid
        cid = 'ch_%d' % len(self.charges)
        self.charges[cid] = payment_intent
        return cid

    def payment_intent_for_charge(self, charge_id):
        return (self.charges or {}).get(charge_id)

    def payment_state(self, payment_intent):
        return {'payment_intent': payment_intent, 'livemode': False, 'amount_received': 29900,
                'refunds': [dict(r) for r in self.refunds.values() if r['payment_intent'] == payment_intent],
                'disputes': [{k: d[k] for k in ('id', 'status', 'amount')}
                             for d in (self.disputes or {}).values() if d['payment_intent'] == payment_intent]}


class DurableOfflineStripe:
    """Restart-durable local simulator, NOT provider sandbox acceptance.

    Independent database models the provider's commit/response boundary. Each
    side effect and its idempotency identity commit together. No network calls.
    """
    simulated = True

    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript('''PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS objects(id TEXT PRIMARY KEY, kind TEXT, data TEXT NOT NULL);''')

    def close(self):
        self.db.close()

    def _get(self, identity):
        row = self.db.execute('SELECT data FROM objects WHERE id=?', (identity,)).fetchone()
        if not row:
            raise PaymentError('unknown_provider_object')
        return json.loads(row[0])

    def _save(self, obj, kind):
        self.db.execute('INSERT OR REPLACE INTO objects VALUES (?,?,?)', (obj['id'], kind, json.dumps(obj)))

    def checkout(self, order):
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            existing = self.find_checkout(order)
            if existing:
                return existing
            obj = OfflineStripe().checkout(order)
            obj['status'] = 'open'
            self._save(obj, 'checkout')
            return obj

    def find_checkout(self, order, created=None):
        with self.lock:
            row = self.db.execute('SELECT data FROM objects WHERE id=?', ('cs_test_'+order,)).fetchone()
            return json.loads(row[0]) if row else None

    def session(self, sid):
        with self.lock:
            return self._get(sid)

    def set_payment_status(self, sid, status):
        if status not in ('paid', 'unpaid'):
            raise PaymentError('invalid_simulated_payment_status')
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            obj = self._get(sid)
            if obj['status'] == 'expired':
                raise PaymentError('session_expired')
            if obj['payment_status'] != 'paid':
                obj['payment_status'] = status
                obj['status'] = 'complete' if status == 'paid' else 'open'
                self._save(obj, 'checkout')

    def expire_session(self, sid):
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            obj = self._get(sid)
            if obj['payment_status'] != 'paid':
                obj['status'] = 'expired'
                self._save(obj, 'checkout')
            return obj

    def payment_state(self, payment_intent):
        with self.lock:
            refunds = [json.loads(r[0]) for r in self.db.execute("SELECT data FROM objects WHERE kind='refund'")]
        return {'payment_intent': payment_intent, 'livemode': False, 'amount_received': 29900,
                'refunds': [r for r in refunds if r['payment_intent'] == payment_intent], 'disputes': []}

    def refund(self, payment_intent, order, attempt=0):
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT data FROM objects WHERE id=?', ('re_'+order,)).fetchone()
            if row:
                return json.loads(row[0])
            session = self._get('cs_test_'+order)
            if session['payment_status'] != 'paid' or session['payment_intent'] != payment_intent:
                raise PaymentError('no_simulated_payment')
            obj = OfflineStripe().refund(payment_intent, order)
            self._save(obj, 'refund')
            return obj

    def refund_status(self, rid):
        # Deterministic simulator settlement on the next observation, no owner.
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            obj = self._get(rid)
            if obj['status'] == 'pending':
                obj['status'] = 'succeeded'
                self._save(obj, 'refund')
            return obj
