"""Regression tests for the Sprint-2 Tier-A review remediation, batch 1.

Covers the accepted findings in .cache/review/merged-findings.md:

- A1-02 request-slot exhaustion (health/webhook survive; fast 503; slots
  recycle within the anonymous body deadline),
- A2-01 unbounded anonymous page-view event writes (sampled, at most one
  durable row per kind per window),
- A2-04 webhook bodies buffered before the 256 KiB cap (cap now enforced
  from Content-Length, before any read),
- A1-06 deploy-mode client identity trusting a spoofable X-Real-IP
  (shape-validated; malformed values fall back to the peer address),
- A1-04 Unicode-permissive isalnum() identifier shapes (ASCII-anchored in
  service.py and payments.py, fail-closed and indistinguishable).

Batch 2 additions:

- A1-01/A2-02 keyed-checkout capabilities now HMAC-derived with the service
  secret (same-key retry semantics, hashes-only persistence preserved),
- A1-05 keyed-checkout replay after recover() rotation refuses with a
  bounded checkout_recovered_use_recovery_token error (no stale tokens).

Loopback-only: OfflineStripe doubles, 127.0.0.1 servers, no network beyond
loopback, no live mode, no staging, no secrets.
"""
import base64
import hashlib
import hmac
import http.client
import json
import re
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

import xreg

from . import server as server_module
from .payments import OfflineStripe, PaymentError, StripeSandbox
from .server import make_server
from .service import Rejected, Service, digest
from .test_pilot import example_payload, fake_execute, signed

VIEW_KINDS = ('scope_viewed', 'requirements_viewed', 'sample_viewed')


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='review-remediation-')
        self.now = 1800000000.0
        self.provider = OfflineStripe()
        self.service = Service(Path(self.temp.name)/'state', self.provider,
                               'synthetic-test-signing-value', lambda: self.now)
        # Deterministic view-event sampling regardless of earlier modules.
        server_module._view_event_seen.clear()

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def events(self, kind):
        return self.service.db.execute('SELECT count(*) FROM events WHERE kind=?',
                                       (kind,)).fetchone()[0]


class ServerBase(Base):
    def setUp(self):
        super().setUp()
        self.server = make_server(self.service, deploy=self.deploy)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self):
        for sock in getattr(self, 'hogs', []):
            try:
                sock.close()
            except OSError:
                pass
        self.server.shutdown()
        self.thread.join(timeout=10)
        self.server.server_close()
        super().tearDown()

    deploy = False

    def get(self, path):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        try:
            conn.request('GET', path)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()


class SlotExhaustionTests(ServerBase):
    """A1-02: request slots are taken at dispatch, health and webhook delivery
    are exempt, exhaustion answers 503 immediately, and a stalled anonymous
    body frees its slot within the (shortened) body deadline."""

    deploy = True

    def start_hog(self, index):
        """Hold a dispatch slot: declare a large body, send a trickle, stall."""
        sock = socket.create_connection(('127.0.0.1', self.port), timeout=30)
        sock.sendall(('POST /checkout HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n'
                      'Content-Type: application/json\r\nIdempotency-Key: hog-%d\r\n'
                      'Content-Length: 20971520\r\n\r\n' % (self.port, index)).encode())
        sock.sendall(b'{"product":"h"')  # partial body, then stall
        return sock

    def signed_webhook(self):
        raw, signature = signed(self.service, 'ping', {}, 'evt-slot-%f' % time.time())
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        try:
            conn.request('POST', '/webhook', body=raw,
                         headers={'Content-Type': 'application/json',
                                  'Stripe-Signature': signature})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_health_and_webhook_survive_slot_exhaustion(self):
        with patch.object(server_module, 'UNAUTHENTICATED_WINDOW_SECONDS', 1.0):
            self.hogs = [self.start_hog(i) for i in range(8)]
            time.sleep(0.4)  # all eight hogs are now parked inside read_body
            # Health probe: exempt from slots, answered while all 8 are busy.
            status, body = self.get('/healthz')
            self.assertEqual((status, json.loads(body)['database']), (200, 'reachable'))
            # Signed webhook delivery: exempt and still processed.
            status, body = self.signed_webhook()
            self.assertEqual((status, body), (200, {'accepted': True}))
            # A non-exempt route answers immediately instead of hanging: the
            # pre-fix behaviour dropped these connections entirely.
            status, body = self.get('/')
            self.assertEqual((status, json.loads(body)),
                             (503, {'error': 'service_busy_retry_later'}))
            # The stalled anonymous bodies recycle their slot within the
            # (patched) body deadline; sales routes answer again.
            deadline = time.monotonic() + 8.0
            status = 0
            while time.monotonic() < deadline:
                status, body = self.get('/')
                if status == 200:
                    break
                time.sleep(0.2)
            self.assertEqual(status, 200)


class ViewEventSamplingTests(ServerBase):
    """A2-01: anonymous page views no longer write one durable event row each."""

    def fetch_page(self, path):
        with urllib.request.urlopen(f'http://127.0.0.1:{self.port}'+path, timeout=10) as response:
            self.assertEqual(response.status, 200)
            response.read()

    def test_anonymous_views_do_not_write_unbounded_events(self):
        before = {kind: self.events(kind) for kind in VIEW_KINDS}
        with patch.object(server_module, 'VIEW_EVENT_WINDOW_SECONDS', 2.0):
            for _ in range(25):
                self.fetch_page('/')
            for _ in range(2):
                self.fetch_page('/requirements')
                self.fetch_page('/sample')
            sampled = {kind: self.events(kind) for kind in VIEW_KINDS}
            for kind in VIEW_KINDS:
                self.assertLessEqual(sampled[kind] - before[kind], 1, kind)
            # A second burst after the window produces exactly one more row.
            time.sleep(2.3)
            for _ in range(5):
                self.fetch_page('/')
            after = self.events('scope_viewed')
            self.assertEqual(after - sampled['scope_viewed'], 1)
            # Skipped views never touch the checkout-rate table either.
            self.assertEqual(self.service.db.execute(
                'SELECT count(*) FROM checkout_rate').fetchone()[0], 0)


class WebhookSizeCapTests(ServerBase):
    """A2-04: the 256 KiB webhook cap is enforced from Content-Length, before
    the body is buffered."""

    def test_webhook_size_cap_enforced_before_body_read(self):
        sock = socket.create_connection(('127.0.0.1', self.port), timeout=10)
        try:
            request = ('POST /webhook HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n'
                       'Content-Type: application/json\r\nStripe-Signature: t=1,v1=bad\r\n'
                       'Content-Length: 300000\r\n\r\n' % self.port).encode()
            sock.sendall(request)  # headers only: the 300000-byte body never arrives
            started = time.monotonic()
            data = b''
            while b'\r\n\r\n' not in data:
                data += sock.recv(4096)
            head, _, body = data.partition(b'\r\n\r\n')
            status = int(head.split(b' ')[1])
            length = int(re.search(rb'Content-Length: (\d+)', head).group(1))
            while len(body) < length:
                body += sock.recv(4096)
            elapsed = time.monotonic() - started
            self.assertEqual((status, json.loads(body)), (409, {'error': 'webhook_size_limit'}))
            # Answered from the declared size alone: no waiting for the body
            # (the pre-fix server blocked in rfile.read for the full window).
            self.assertLess(elapsed, 5.0)
            # The server declines to drain the declared body: it closes the
            # connection (FIN or reset) rather than holding it open.
            sock.settimeout(5)
            try:
                closed = sock.recv(1) == b''
            except OSError:
                closed = True  # reset while garbage was still in flight
            self.assertTrue(closed)
        finally:
            sock.close()
        # The boundary itself is still read and verified: an exactly-cap body
        # proceeds to signature verification (and fails closed there).
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        try:
            conn.request('POST', '/webhook', body=b'x'*262144,
                         headers={'Content-Type': 'application/json',
                                  'Stripe-Signature': 't=1,v1=bad'})
            response = conn.getresponse()
            self.assertEqual((response.status, json.loads(response.read())),
                             (409, {'error': 'invalid_sandbox_webhook'}))
        finally:
            conn.close()


class ClientIdentityTests(ServerBase):
    """A1-06: malformed X-Real-IP values do not mint fresh per-client budgets."""

    deploy = True

    def checkout(self, key, real_ip=None):
        headers = {'Content-Type': 'application/json', 'Idempotency-Key': key}
        if real_ip is not None:
            headers['X-Real-IP'] = real_ip
        req = urllib.request.Request(f'http://127.0.0.1:{self.port}/checkout',
                                      data=json.dumps({'product': 'opaque-product',
                                                       'attested': True, 'buyer_type': 'business',
                                                       'terms_accepted': True}).encode(),
                                     headers=headers, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_client_identity_falls_back_on_malformed_real_ip(self):
        limit = self.service.policy['checkout_rate_limit_per_client']
        garbage = ('not-an-ip', '203.0.113.9; inject', '999.999.999.999',
                   '1.2.3.4.5', '<script>', '\u00e9xample', '')
        # Rotating garbage from one socket shares the single real peer budget.
        for i in range(limit):
            status, _ = self.checkout('garbage-%d-%s' % (i, 'k'*30), garbage[i % len(garbage)])
            self.assertEqual(status, 201, garbage[i % len(garbage)])
        status, body = self.checkout('garbage-more-'+'k'*30, garbage[-2])
        self.assertEqual((status, body['error']),
                         (409, 'checkout_rate_limited_retry_later'))
        # Well-formed distinct addresses still get separate budgets.
        status, _ = self.checkout('valid-one-'+'v'*30, '203.0.113.9')
        self.assertEqual(status, 201)
        for i in range(limit):
            status, _ = self.checkout('valid-two-%d-' % i + 'w'*30, '198.51.100.7')
            self.assertEqual(status, 201)
        status, _ = self.checkout('valid-two-more-'+'w'*30, '198.51.100.7')
        self.assertEqual(status, 409)


class IdentifierShapeTests(Base):
    """A1-04: identifier shape checks are ASCII-anchored and fail closed."""

    def test_unicode_identifier_shapes_rejected(self):
        cyrillic = 't' + '\u0441'*31 + '0'*8  # passes str.isalnum(), length 40
        self.assertTrue(cyrillic.replace('-', '').replace('_', '').isalnum())  # the bug
        with self.assertRaisesRegex(Rejected, 'invalid_checkout_idempotency_key'):
            self.service.checkout('opaque-product', True, request_id=cyrillic)
        # ASCII keys (dash and underscore) keep working at checkout.
        order = self.service.checkout('opaque-product', True, request_id='ok-key_'+('a'*30))
        self.assertIn('order_id', order)
        # Submit: same uniform denial for Unicode, underscore still excluded.
        with self.assertRaisesRegex(Rejected, 'invalid_idempotency_key'):
            self.service.submit('0'*32, 'token', example_payload(), request_id=cyrillic[:40])
        with self.assertRaisesRegex(Rejected, 'invalid_idempotency_key'):
            self.service.submit('0'*32, 'token', example_payload(), request_id='under_score'+('a'*30))
        # Provider-facing ids are refused by the adapter before any request.
        provider = StripeSandbox('sk_test_x', 'https://example.test')
        with self.assertRaisesRegex(PaymentError, 'invalid_payment_intent'):
            provider.payment_state('pi_'+'\u043a\u043b\u044e\u0447')
        with self.assertRaisesRegex(PaymentError, 'invalid_session_id'):
            provider.session('cs_test_'+'\u00e9'+'0'*30)
        with self.assertRaisesRegex(PaymentError, 'invalid_charge'):
            provider.payment_intent_for_charge('ch_'+'\u043a\u043b\u044e\u0447')
        with self.assertRaisesRegex(PaymentError, 'invalid_refund_id'):
            provider.refund_status('re_'+'\u043a\u043b\u044e\u0447')
        with self.assertRaisesRegex(PaymentError, 'invalid_order_id'):
            provider.find_checkout('\u043e\u0440\u0434\u0435\u0440-1', 0)

    def test_unicode_session_id_at_success_is_indistinguishable(self):
        # Service level: uniform unavailability, no provider call.
        self.assertEqual(self.service.success_view('cs_test_'+'\u00e9'+'0'*30),
                         {'kind': 'unavailable'})
        # HTTP level: byte-identical 404s, exactly like other malformed ids.
        server = make_server(self.service)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()

        def fetch(url):
            try:
                with urllib.request.urlopen(url, timeout=10) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read()

        try:
            port = server.server_port
            responses = set()
            for sid in ('cs_test_'+'\u00e9'+'0'*31, 'cs_test_'+'\u0441'*32, '../orders',
                        'cs_test_'+'0'*200, 'cs_live_'+'0'*32):
                status, body = fetch(f'http://127.0.0.1:{port}/success?session_id='
                                     + urllib.parse.quote(sid, safe=''))
                self.assertEqual(status, 404, sid)
                responses.add(body)
            self.assertEqual(responses, {b'{"error": "not_found"}'})
        finally:
            server.shutdown()
            thread.join(timeout=10)
            server.server_close()


class KeyedCheckoutCapabilityTests(Base):
    """A1-01/A2-02: keyed-checkout access/recovery capabilities are derived
    with the service secret (HMAC over a domain-separated handle) instead of
    a public digest of the client-chosen Idempotency-Key — while preserving
    the documented semantics: deterministic same-key replay, hashes-only
    persistence, no shared capability across different secrets."""

    def test_keyed_checkout_capabilities_are_secret_derived(self):
        key = 'c' * 40
        first = self.service.checkout('opaque-product', True, request_id=key)
        oid = first['order_id']
        # Not a public function of the key alone (the old plain digest):
        self.assertNotEqual(first['access_token'], digest('checkout-access:' + key))
        self.assertNotEqual(first['recovery_token'], digest('checkout-recovery:' + key))
        # But the documented server-secret-keyed HMAC derivation:
        for token, handle in ((first['access_token'], 'checkout-access:'),
                              (first['recovery_token'], 'checkout-recovery:')):
            expected = hmac.new(self.service.secret.encode(), (handle + key).encode(),
                                hashlib.sha256).hexdigest()
            self.assertEqual(token, expected)
        # Same key + payload replay is idempotent: identical working tokens.
        replay = self.service.checkout('opaque-product', True, request_id=key)
        self.assertEqual(replay['order_id'], oid)
        self.assertEqual((replay['access_token'], replay['recovery_token']),
                         (first['access_token'], first['recovery_token']))
        self.assertEqual(self.service.status(oid, replay['access_token'])['id'], oid)
        # No raw token material is persisted anywhere (hashes only).
        needles = (first['access_token'], first['recovery_token'])
        for table in ('orders', 'events', 'checkouts', 'submissions', 'webhooks',
                      'gate_audit'):
            for row in self.service.db.execute('SELECT * FROM %s' % table).fetchall():
                for cell in row:
                    text = cell.decode('utf-8', 'ignore') if isinstance(cell, bytes) \
                        else str(cell)
                    for needle in needles:
                        self.assertNotIn(needle, text)
        # A different service secret derives different capabilities from the
        # same client-chosen key: the capability is not key-public.
        other = Service(Path(self.temp.name) / 'other', OfflineStripe(),
                        'a-different-signing-secret', lambda: self.now)
        try:
            second = other.checkout('opaque-product', True, request_id=key)
            self.assertNotEqual(second['access_token'], first['access_token'])
            self.assertNotEqual(second['recovery_token'], first['recovery_token'])
        finally:
            other.close()


class KeyedCheckoutRotationTests(Base):
    """A1-05: a keyed-checkout replay after recover() rotation is refused
    with a bounded recovery pointer instead of echoing now-stale derived
    tokens the API itself revoked."""

    def test_keyed_checkout_replay_after_rotation_is_refused(self):
        key = 'r' * 40
        first = self.service.checkout('opaque-product', True, request_id=key)
        oid = first['order_id']
        rotated = self.service.recover(oid, first['recovery_token'])
        # Pre-rotation capabilities are dead, access and recovery alike.
        with self.assertRaisesRegex(Rejected, 'access_denied'):
            self.service.status(oid, first['access_token'])
        with self.assertRaisesRegex(Rejected, 'access_denied'):
            self.service.recover(oid, first['recovery_token'])
        # Same-key replay is refused with the bounded error instead of
        # returning credentials auth rejects (the MERGE-B wart).
        with self.assertRaisesRegex(Rejected, 'checkout_recovered_use_recovery_token'):
            self.service.checkout('opaque-product', True, request_id=key)
        # The rotated-in tokens keep working.
        self.assertEqual(self.service.status(oid, rotated['access_token'])['id'], oid)
        # Payload-conflict detection still fires before the rotation refusal.
        with self.assertRaisesRegex(Rejected, 'idempotency_key_payload_conflict'):
            self.service.checkout('other-product', True, request_id=key)
        # A fresh key is unaffected by the rotation.
        fresh = self.service.checkout('opaque-product', True, request_id='s' * 40)
        self.assertNotEqual(fresh['order_id'], oid)
        self.assertEqual(
            self.service.status(fresh['order_id'], fresh['access_token'])['id'],
            fresh['order_id'])


# ===========================================================================
# Iteration-2 residuals (reattack-all.md): R-01, R-02, R-04
# ===========================================================================


class FakeBearerSlotHogTests(ServerBase):
    """R-01: the anonymous total body deadline must not depend on an
    unvalidated Authorization header. Requests that merely present a Bearer
    value are read under the anonymous deadline exactly like header-less
    ones, so trickling fake-credential bodies cannot hold dispatch slots
    past it; only a credential that actually authenticates on the submit
    route earns the wider (itself total-bounded) window."""

    deploy = True

    def start_hog(self, index, bearer):
        """Hold a dispatch slot: submit route, declared 20 MiB body,
        partial bytes sent, then a trickle that never stops."""
        request = ('POST /orders/%s/submit HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n'
                   'Content-Type: application/json\r\nIdempotency-Key: hog-%d\r\n'
                   % ('a' * 32, self.port, index))
        if bearer:
            request += 'Authorization: Bearer not-a-real-credential-%d\r\n' % index
        request += 'Content-Length: 20971520\r\n\r\n'
        sock = socket.create_connection(('127.0.0.1', self.port), timeout=30)
        sock.sendall(request.encode())
        sock.sendall(b'{"product":"h"')
        stop = threading.Event()

        def trickle():
            while not stop.is_set():
                try:
                    sock.sendall(b'x')
                except OSError:
                    return
                time.sleep(0.15)

        threading.Thread(target=trickle, daemon=True).start()
        return sock, stop

    def run_hogs(self, bearer):
        hogs = [self.start_hog(i, bearer) for i in range(8)]  # exactly the slots
        self.hogs = [sock for sock, _ in hogs]
        try:
            time.sleep(0.6)  # all eight are parked inside read_body now
            early_health = self.get('/healthz')[0]
            early_root = self.get('/')[0]
            # The hogs keep trickling: slots must free at the (patched)
            # anonymous total deadline without any attacker cooperation.
            deadline, late_root = time.monotonic() + 10.0, 0
            while time.monotonic() < deadline:
                late_root = self.get('/')[0]
                if late_root == 200:
                    break
                time.sleep(0.2)
            late_health = self.get('/healthz')[0]
            return early_health, early_root, late_health, late_root
        finally:
            for _, stop in hogs:
                stop.set()
            for sock in self.hogs:
                try:
                    sock.close()
                except OSError:
                    pass

    def test_fake_bearer_hogs_free_slots_at_anonymous_deadline(self):
        with patch.object(server_module, 'UNAUTHENTICATED_WINDOW_SECONDS', 1.0):
            early_health, early_root, late_health, late_root = self.run_hogs(True)
        # Exempt routes answer while the hogs hold every dispatch slot...
        self.assertEqual((early_health, late_health), (200, 200))
        # ...sales routes get the immediate 503 while saturated...
        self.assertEqual(early_root, 503)
        # ...and recycle at the anonymous deadline even though every hog
        # presents a Bearer credential and never stops trickling body bytes
        # (pre-fix, the fake header widened the window to 30 s per read with
        # no total deadline, holding all slots until the attacker closed).
        self.assertEqual(late_root, 200)

    def test_control_headerless_hogs_free_slots_at_anonymous_deadline(self):
        with patch.object(server_module, 'UNAUTHENTICATED_WINDOW_SECONDS', 1.0):
            early_health, early_root, late_health, late_root = self.run_hogs(False)
        # Control: without the header the behaviour is identical — the
        # remediated A1-02 guarantee, now independent of header presence.
        self.assertEqual((early_health, late_health), (200, 200))
        self.assertEqual(early_root, 503)
        self.assertEqual(late_root, 200)

    def test_validated_submit_still_reads_its_body(self):
        # The wide window is still granted where it is legitimate: a real
        # credential on the submit route reads its full body and reaches the
        # submit logic (which rejects this malformed Idempotency-Key fast).
        order = self.service.checkout('opaque-product', True, request_id='w' * 40)
        body = json.dumps(example_payload()).encode()
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        try:
            conn.request('POST', '/orders/%s/submit' % order['order_id'], body=body,
                         headers={'Content-Type': 'application/json',
                                  'Authorization': 'Bearer ' + order['access_token'],
                                  'Idempotency-Key': 'under_score'})
            response = conn.getresponse()
            status, payload = response.status, json.loads(response.read())
        finally:
            conn.close()
        self.assertEqual((status, payload['error']), (409, 'invalid_idempotency_key'))


class RotationTOCTOUTests(Base):
    """R-02: a recover() rotation landing between the cached-branch check
    and the response must not yield now-revoked tokens. The replay refuses
    with the bounded recovery pointer — not checkout_unavailable, no
    provider-error event, no gate closure."""

    def test_recover_during_cached_replay_refuses_instead_of_stale_tokens(self):
        key = 't' * 42
        first = self.service.checkout('opaque-product', True, request_id=key)
        oid = first['order_id']
        original = self.provider.session

        def hooked(session_id):
            result = original(session_id)
            self.service.recover(oid, first['recovery_token'])  # rotate mid-flight
            return result

        self.provider.session = hooked
        provider_errors = self.events('checkout_provider_error')
        # The mid-flight rotation must turn the replay into the bounded
        # refusal, never a response carrying now-revoked capabilities
        # (pre-fix, the replay returned first['access_token'], dead on
        # arrival at status()).
        with self.assertRaisesRegex(Rejected, 'checkout_recovered_use_recovery_token'):
            self.service.checkout('opaque-product', True, request_id=key)
        # The rotation really happened: the originally issued tokens are dead.
        with self.assertRaisesRegex(Rejected, 'access_denied'):
            self.service.status(oid, first['access_token'])
        # It is the plain rotation refusal: no provider-error event was
        # written and the acquisition gate was not closed.
        self.assertEqual(self.events('checkout_provider_error'), provider_errors)
        self.assertEqual(self.service.db.execute(
            "SELECT healthy FROM gates WHERE name='acquisition'").fetchone()[0], 1)
        # The stable state afterwards is the same bounded refusal.
        with self.assertRaisesRegex(Rejected, 'checkout_recovered_use_recovery_token'):
            self.service.checkout('opaque-product', True, request_id=key)


class ProviderIdLengthTests(Base):
    """R-04: provider identifier shape checks bound total length, so ids
    from signed webhook bodies (or durable state) cannot build huge provider
    URL paths. Network calls are stubbed; every oversized id is refused
    before any request is attempted."""

    def test_oversized_provider_ids_rejected_before_any_request(self):
        provider = StripeSandbox('sk_test_stubbed', 'https://example.test')
        calls = []

        def recorder(method, path, *args, **kwargs):
            calls.append((method, path))
            raise RuntimeError('network-stubbed')

        provider.request = recorder
        # Oversized but otherwise shape-valid ids never reach the provider
        # (pre-fix, a 100 KB id was concatenated straight into the URL path).
        with self.assertRaisesRegex(PaymentError, 'invalid_session_id'):
            provider.session('cs_test_' + 'a' * 100000)
        with self.assertRaisesRegex(PaymentError, 'invalid_payment_intent'):
            provider.payment_state('pi_' + 'a' * 100000)
        with self.assertRaisesRegex(PaymentError, 'invalid_charge'):
            provider.payment_intent_for_charge('ch_' + 'a' * 100000)
        with self.assertRaisesRegex(PaymentError, 'invalid_refund_id'):
            provider.refund_status('re_' + 'a' * 100000)
        with self.assertRaisesRegex(PaymentError, 'invalid_order_id'):
            provider.find_checkout('a' * 100000, 0)
        self.assertEqual(calls, [])
        # The ASCII shape rejection (A1-04) is unchanged.
        with self.assertRaisesRegex(PaymentError, 'invalid_session_id'):
            provider.session('cs_test_' + '\u0441' * 10)
        self.assertEqual(calls, [])
        # Boundary-length ids still pass the guard and fail only at the
        # (stubbed) network layer — no over-rejection of real shapes.
        with self.assertRaisesRegex(RuntimeError, 'network-stubbed'):
            provider.session('cs_test_' + 'a' * 240)  # 248 chars total
        self.assertEqual(calls, [('GET', 'checkout/sessions/cs_test_' + 'a' * 240)])
        with self.assertRaisesRegex(RuntimeError, 'network-stubbed'):
            provider.find_checkout('ord-' + 'a' * 251, 0)  # 255 chars total
        self.assertEqual(calls[-1], ('GET', 'checkout/sessions'))


# ===========================================================================
# Iteration-2 re-attack residual (reattack-iter2.md): R2-01
# ===========================================================================


class UnpaidCredentialCycleTests(ServerBase):
    """R2-01: the wide (240 s) submit body window is earned only by a
    verifiably PAID order. service.auth is payment-blind (stored hash only),
    so the access token of a freely minted unpaid checkout authenticates —
    but service.submit refuses verified_payment_required_or_refund_active
    before the payload is decoded, so no legitimate unpaid caller ever
    needs a wide body read. Inverted port of the re-attack's
    R01FreeCredentialCycleTests demonstration: trickling submits with a
    genuine unpaid token must free their slots at the anonymous total
    deadline — and re-arming the wave with the same free credential must
    not change that — while a paid order's token still gets the wide
    window, itself bounded by the total submit deadline."""

    deploy = True

    def start_hog(self, path, token, index):
        """Hold a dispatch slot on `path` with a genuine credential: a
        declared 20 MiB body, a partial first chunk, then a trickle that
        never stops (the attacker wants to keep every slot)."""
        request = ('POST %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n'
                   'Content-Type: application/json\r\nIdempotency-Key: hog-%d\r\n'
                   'Authorization: Bearer %s\r\n'
                   'Content-Length: 20971520\r\n\r\n' % (path, self.port, index, token))
        sock = socket.create_connection(('127.0.0.1', self.port), timeout=30)
        sock.sendall(request.encode())
        sock.sendall(b'{"pair":')
        stop = threading.Event()

        def trickle():
            while not stop.is_set():
                try:
                    sock.sendall(b'x')
                except OSError:
                    return
                time.sleep(0.15)

        threading.Thread(target=trickle, daemon=True).start()
        return sock, stop

    def settle_hogs(self, hogs):
        for _, stop in hogs:
            stop.set()
        for sock in self.hogs:
            try:
                sock.close()
            except OSError:
                pass

    def test_unpaid_credential_frees_slots_at_anonymous_deadline(self):
        order = self.service.checkout('opaque-product', True, request_id='f' * 40)  # free
        oid, tok = order['order_id'], order['access_token']
        # The token is genuine — auth() is payment-blind and still accepts
        # it for reads — and the order is unpaid: exactly the R2-01 credential.
        self.assertEqual(self.service.status(oid, tok)['payment'], 'unpaid')
        path = '/orders/%s/submit' % oid
        with patch.object(server_module, 'UNAUTHENTICATED_WINDOW_SECONDS', 1.0), \
                patch.object(server_module, 'SUBMIT_BODY_WINDOW_SECONDS', 6.0):
            for base in (0, 100):  # wave, then the demo's re-arm wave
                hogs = [self.start_hog(path, tok, base + i) for i in range(8)]
                self.hogs = [sock for sock, _ in hogs]
                try:
                    time.sleep(0.6)  # all eight are parked inside read_body
                    # Pre-fix (the demonstration), all 8 slots were held here
                    # (503) until the 6 s submit deadline; now they free at the
                    # 1 s anonymous deadline. The poll budget stays strictly
                    # inside the submit window, so a wide-window hold fails.
                    deadline, root = time.monotonic() + 4.0, 0
                    while time.monotonic() < deadline:
                        root = self.get('/')[0]
                        if root == 200:
                            break
                        time.sleep(0.2)
                    health = self.get('/healthz')[0]
                finally:
                    self.settle_hogs(hogs)
                self.assertEqual((root, health), (200, 200), 'wave %d' % base)

    def test_paid_credential_retains_wide_submit_window(self):
        order = self.service.checkout('opaque-product', True)
        sid = order['session_id']
        self.service.provider.sessions[sid]['payment_status'] = 'paid'
        self.service.webhook(*signed(self.service, 'checkout.session.completed',
                                     {'id': sid}, 'evt_' + order['order_id']))
        oid, tok = order['order_id'], order['access_token']
        self.assertEqual(self.service.status(oid, tok)['payment'], 'paid')
        path = '/orders/%s/submit' % oid
        with patch.object(server_module, 'UNAUTHENTICATED_WINDOW_SECONDS', 1.0), \
                patch.object(server_module, 'SUBMIT_BODY_WINDOW_SECONDS', 6.0):
            hogs = [self.start_hog(path, tok, i) for i in range(8)]
            self.hogs = [sock for sock, _ in hogs]
            try:
                # Past the 1 s anonymous deadline but inside the 6 s submit
                # window: the paid credential still holds every slot (the
                # demonstration's phase A) — the wide window survives where
                # it is legitimate, and exempt routes answer throughout.
                time.sleep(2.5)
                self.assertEqual(self.get('/')[0], 503)
                self.assertEqual(self.get('/healthz')[0], 200)
                # The demonstration's phase B: the total submit deadline
                # frees every slot on the next trickle chunk, without any
                # attacker cooperation.
                deadline, root = time.monotonic() + 10.0, 0
                while time.monotonic() < deadline:
                    root = self.get('/')[0]
                    if root == 200:
                        break
                    time.sleep(0.2)
                self.assertEqual(root, 200)
            finally:
                self.settle_hogs(hogs)


# ===========================================================================
# Iteration-3 re-attack residual (reattack-R2-01.md): R3-01
# ===========================================================================


class RefusedStateCredentialTests(ServerBase):
    """R3-01: the wide submit-body window must mirror service.submit's
    durable admission, not merely payment=='paid' and refund=='none'. A
    once-paid order submit refuses forever — active or lost dispute, partial
    external refund, runs exhausted — never flips payment/refund, so the
    narrower grant predicate still handed its (paid-for) token the wide
    window: the R2-01 cyclic slot saturation re-entered through the state of
    a completed order. Inverted port of the re-attack's DisputeMismatchTests
    rows and lost-dispute cycle: every submit-refused state now gets the
    narrow anonymous window, its trickling hogs free all slots at the
    anonymous deadline (first wave and re-arm wave alike), while a genuinely
    submittable paid order — including a *won* dispute, which submit admits
    — keeps the wide window (bounded by the total submit deadline)."""

    deploy = True

    # --- state engineering via the public API + signed webhooks only ----
    def checkout(self, key):
        return self.service.checkout('opaque-product', True, request_id=key)

    @staticmethod
    def pi(order):
        return 'pi_' + order['order_id']

    def pay(self, order, tag):
        self.provider.sessions[order['session_id']]['payment_status'] = 'paid'
        self.service.webhook(*signed(self.service, 'checkout.session.completed',
                                     {'id': order['session_id']}, 'evt_' + tag + order['order_id']))
        return order

    def paid(self, key):
        return self.pay(self.checkout(key), key)

    def dispute(self, order, status, kind='charge.dispute.created'):
        self.provider.external_dispute(self.pi(order), status=status)
        self.service.webhook(*signed(self.service, kind,
                                     {'payment_intent': self.pi(order)}, 'evt_d' + order['order_id']))

    def partial_external_refund(self, order, amount=15000):
        self.provider.external_refund(self.pi(order), amount=amount)
        self.service.webhook(*signed(self.service, 'charge.refunded',
                                     {'payment_intent': self.pi(order)}, 'evt_p' + order['order_id']))

    def admitted(self, order):
        """service.submit admission outcome via the suite's fake-executor
        pattern. worker_busy_retry_later is designed-transient (the server's
        1 s reservation-recovery tick holds job_lock); it is retried."""
        deadline = time.monotonic() + 20.0
        while True:
            try:
                with patch('pilot.worker.public_hashes',
                           return_value={xreg.sha(base64.b64decode(
                               example_payload()['fixtures'][0]['old']))}):
                    self.service.submit(order['order_id'], order['access_token'],
                                        example_payload(), executor=fake_execute)
                return True
            except Rejected as exc:
                if str(exc) == 'worker_busy_retry_later' and time.monotonic() < deadline:
                    time.sleep(0.5)
                    continue
                return str(exc)

    def runs_exhausted(self, key):
        """The natural terminal state of a fulfilled customer: both included
        runs consumed, then the reservation sweep marks the order spent."""
        order = self.paid(key)
        self.assertEqual(self.admitted(order), True)   # run 1
        self.assertEqual(self.admitted(order), True)   # the included rerun
        self.service.reconcile_reservations()          # reservation -> spent
        self.assertEqual(self.service.get(order['order_id']).get('reservation'), 'spent')
        return order

    def start_hog(self, path, token, index):
        """Hold a dispatch slot with a genuine credential: a declared 20 MiB
        submit body, a partial first chunk, then a trickle that never stops."""
        request = ('POST %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n'
                   'Content-Type: application/json\r\nIdempotency-Key: hog-%d\r\n'
                   'Authorization: Bearer %s\r\nContent-Length: 20971520\r\n\r\n'
                   % (path, self.port, index, token))
        sock = socket.create_connection(('127.0.0.1', self.port), timeout=30)
        sock.sendall(request.encode())
        sock.sendall(b'{"pair":')
        stop = threading.Event()

        def trickle():
            while not stop.is_set():
                try:
                    sock.sendall(b'x')
                except OSError:
                    return
                time.sleep(0.15)

        threading.Thread(target=trickle, daemon=True).start()
        return sock, stop

    def settle_hogs(self, hogs):
        for _, stop in hogs:
            stop.set()
        for sock in self.hogs:
            try:
                sock.close()
            except OSError:
                pass

    def probe_grant(self, oid, token, declared=1000000):
        """Single-connection grant oracle: how long the server keeps reading
        a trickled submit body before answering truncated_request — close to
        the (patched) anonymous window means a narrow grant, close to the
        (patched) submit window a wide one."""
        self._probe = getattr(self, '_probe', 0) + 1
        head = ('POST /orders/%s/submit HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n'
                'Content-Type: application/json\r\nIdempotency-Key: probe-%d\r\n'
                'Authorization: Bearer %s\r\nContent-Length: %d\r\n\r\n'
                % (oid, self.port, self._probe, token, declared))
        sock = socket.create_connection(('127.0.0.1', self.port), timeout=40)
        sock.sendall(head.encode() + b'{"pair":')
        stop = threading.Event()

        def trickle():
            while not stop.is_set():
                try:
                    sock.sendall(b'x')
                except OSError:
                    return
                time.sleep(0.1)

        threading.Thread(target=trickle, daemon=True).start()
        start, data = time.monotonic(), b''
        try:
            while time.monotonic() - start < 20:
                try:
                    chunk = sock.recv(4096)
                except OSError:
                    break
                if chunk:
                    data += chunk
                    if b'\r\n\r\n' in data:
                        break
                else:
                    break
        finally:
            stop.set()
            try:
                sock.close()
            except OSError:
                pass
        status = int(data.split(b' ')[1]) if data.startswith(b'HTTP') else None
        return time.monotonic() - start, status

    def test_submit_refused_states_get_the_anonymous_window(self):
        # All orders are minted and paid up front: dispute and partial-refund
        # states close the acquisition gate by design, pausing later checkouts.
        fresh = self.paid('r3a' + 'x' * 36)
        won = self.paid('r3b' + 'x' * 36)
        active = self.paid('r3c' + 'x' * 36)
        lost = self.paid('r3d' + 'x' * 36)
        partial = self.paid('r3e' + 'x' * 36)
        exhausted = self.runs_exhausted('r3f' + 'x' * 36)
        self.dispute(won, 'won', kind='charge.dispute.closed')  # control: admitted
        self.dispute(active, 'needs_response')
        self.dispute(lost, 'lost', kind='charge.dispute.closed')
        self.partial_external_refund(partial)
        # Premise (the R3-01 blind spot): none of these states flips
        # payment/refund, and submit refuses each of them at admission.
        for order in (active, lost, partial, exhausted):
            row = self.service.get(order['order_id'])
            self.assertEqual((row['payment'], row['refund']), ('paid', 'none'))
            self.assertEqual(self.admitted(order), 'verified_payment_required_or_refund_active')
        with patch.object(server_module, 'UNAUTHENTICATED_WINDOW_SECONDS', 1.0), \
                patch.object(server_module, 'SUBMIT_BODY_WINDOW_SECONDS', 6.0):
            # Inverted re-attack rows: each submit-refused paid order now
            # gets the narrow anonymous window (pre-fix: wide, ~6 s).
            rows = ((active, 'dispute needs_response'), (lost, 'dispute lost'),
                    (partial, 'partial external refund'), (exhausted, 'runs exhausted'))
            for order, name in rows:
                elapsed, status = self.probe_grant(order['order_id'], order['access_token'])
                self.assertLess(elapsed, 3.5, '%s: %.2fs' % (name, elapsed))
                self.assertEqual(status, 409, name)
            # Controls: genuinely submittable paid orders keep the wide
            # window (a won dispute is not an ACTIVE_DISPUTE status).
            for order, name in ((fresh, 'paid runs=0'), (won, 'paid dispute won')):
                elapsed, status = self.probe_grant(order['order_id'], order['access_token'])
                self.assertGreaterEqual(elapsed, 3.5, '%s: %.2fs' % (name, elapsed))
                self.assertEqual(status, 409, name)
        # And the controls really are submittable, not merely unblocked.
        self.assertEqual(self.admitted(fresh), True)
        self.assertEqual(self.admitted(won), True)

    def test_refused_state_hogs_free_slots_at_anonymous_deadline(self):
        lost = self.paid('r3g' + 'x' * 36)
        partial = self.paid('r3h' + 'x' * 36)
        exhausted = self.runs_exhausted('r3i' + 'x' * 36)
        self.dispute(lost, 'lost', kind='charge.dispute.closed')
        self.partial_external_refund(partial)
        for order in (lost, partial, exhausted):
            self.assertEqual(self.admitted(order), 'verified_payment_required_or_refund_active')
        with patch.object(server_module, 'UNAUTHENTICATED_WINDOW_SECONDS', 1.0), \
                patch.object(server_module, 'SUBMIT_BODY_WINDOW_SECONDS', 6.0):
            # Two waves with the lost-dispute credential (the re-attack's
            # re-arm cycle), then one wave each for partial external refund
            # and runs exhausted: all 8 slots must free at the anonymous
            # deadline, strictly inside the submit window — the pre-fix tree
            # held them (503) until the ~6 s total deadline, every wave.
            waves = ((0, lost), (100, lost), (200, partial), (300, exhausted))
            for base, order in waves:
                path = '/orders/%s/submit' % order['order_id']
                hogs = [self.start_hog(path, order['access_token'], base + i) for i in range(8)]
                self.hogs = [sock for sock, _ in hogs]
                try:
                    time.sleep(0.6)  # all eight are parked inside read_body
                    deadline, root = time.monotonic() + 4.0, 0
                    while time.monotonic() < deadline:
                        root = self.get('/')[0]
                        if root == 200:
                            break
                        time.sleep(0.2)
                    health = self.get('/healthz')[0]
                finally:
                    self.settle_hogs(hogs)
                self.assertEqual((root, health), (200, 200), 'wave %d' % base)

    def test_admission_mirror_covers_attempts_and_rerun_window(self):
        # The remaining durable admission checks submit enforces — attempts
        # remaining and the included-rerun window — are mirrored by the
        # grant predicate too (direct pins; the HTTP rows above cover the
        # reachable state transitions).
        order = self.paid('r3j' + 'x' * 36)
        row = self.service.get(order['order_id'])
        self.assertTrue(self.service.submit_admission_open(row))
        max_attempts = self.service.policy['max_submission_attempts']
        window = self.service.policy['rerun_days'] * 86400
        # Attempts exhausted: submit refuses input_attempt_limit_refund_available.
        self.assertFalse(self.service.submit_admission_open(dict(row, attempts=max_attempts)))
        # Runs exhausted, or the rerun window passed: submit refuses
        # included_rerun_exhausted_or_expired.
        self.assertFalse(self.service.submit_admission_open(dict(row, runs=2)))
        self.assertFalse(self.service.submit_admission_open(
            dict(row, runs=1, first_delivery=self.now - window - 1)))
        # One delivered run still inside the window stays wide-window eligible.
        self.assertTrue(self.service.submit_admission_open(
            dict(row, runs=1, first_delivery=self.now - window + 1)))


if __name__ == '__main__':
    unittest.main()
