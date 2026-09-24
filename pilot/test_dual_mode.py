"""Dual-era provider routing: TEST-era orders reconcile while the service runs LIVE.

Regression pack for EraRoutingStripe (pilot/payments.py) and the era-aware
service checks: provider ids route strictly by era prefix, one era's id never
reaches the other era's credential, a TEST-era id fails closed when no legacy
client is configured, both endpoint secrets verify during the cutover window,
and a carried-over TEST-era paid order (cs_test_ session, pi_test_ intent)
reconciles green through the legacy double while the provider is era-routing.
New purchases remain strictly live.
"""
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error

from .launch_config import LaunchConfig
from .payments import (EraRoutingStripe, OfflineStripe, PaymentError,
                       StripeSandbox, verify_event)
from .service import Rejected, Service

TAX = 'no_vat_charged_seller_not_registered'
STAMP = '1800000000'


def _absent():
    """A 404 shaped like the real transport raises for provider absence."""
    return urllib.error.HTTPError('GET', 404, 'resource_missing', None, None)


class FakeEraClient:
    """Minimal single-era provider double; EraRoutingStripe does the routing.

    Not a Stripe transport: every object it returns is a synthetic fixture at
    the exact shape pilot/service.py validates, so no synthetic credential or
    network is involved. Records every call for routing assertions.
    """

    def __init__(self, era):
        self.era, self.live = era, era == 'live'
        self.prefix = 'cs_%s_' % era
        self.sessions, self.refunds, self.charges = {}, {}, {}
        self.calls = []

    def checkout(self, order):
        self.calls.append(('checkout', order))
        sid = self.prefix + order
        self.sessions[sid] = {'id': sid, 'livemode': self.live, 'mode': 'payment',
            'status': 'open', 'amount_subtotal': 29900, 'amount_total': 29900,
            'currency': 'gbp', 'total_details': {'amount_tax': 0, 'amount_discount': 0, 'amount_shipping': 0},
            'payment_status': 'unpaid', 'payment_intent': 'pi_%s_%s' % (self.era, order),
            'client_reference_id': order, 'metadata': {'order_id': order},
            'url': 'https://checkout.example.test/'+sid}
        return dict(self.sessions[sid])

    def mark_paid(self, sid):
        self.sessions[sid].update(payment_status='paid', status='complete')

    def session(self, sid):
        self.calls.append(('session', sid))
        found = self.sessions.get(sid)
        return dict(found) if found is not None else None

    def find_checkout(self, order, created=None):
        self.calls.append(('find_checkout', order))
        found = [s for s in self.sessions.values() if s['metadata']['order_id'] == order]
        return dict(found[0]) if found else None

    def expire_session(self, sid):
        self.calls.append(('expire_session', sid))
        session = self.sessions[sid]
        if session['payment_status'] != 'paid':
            session['status'] = 'expired'
        return dict(session)

    def payment_state(self, payment_intent):
        self.calls.append(('payment_state', payment_intent))
        known = any(s.get('payment_intent') == payment_intent for s in self.sessions.values()) \
            or any(r['payment_intent'] == payment_intent for r in self.refunds.values())
        if not known:
            return None  # real StripeSandbox: 404 -> authoritative absence
        return {'payment_intent': payment_intent, 'livemode': self.live, 'amount_received': 29900,
                'refunds': [dict(r) for r in self.refunds.values() if r['payment_intent'] == payment_intent],
                'disputes': []}

    def charge_for(self, payment_intent):
        cid = 'ch_%s_%d' % (self.era, len(self.charges))
        self.charges[cid] = payment_intent
        return cid

    def payment_intent_for_charge(self, charge_id):
        self.calls.append(('payment_intent_for_charge', charge_id))
        if charge_id not in self.charges:
            raise _absent()  # real StripeSandbox: raw 404 for a foreign charge
        return self.charges[charge_id]

    def refund(self, payment_intent, order, attempt=0):
        self.calls.append(('refund', payment_intent))
        rid = 're_%s_%s%s' % (self.era, order, '_%d' % attempt if attempt else '')
        self.refunds[rid] = {'id': rid, 'livemode': self.live, 'status': 'pending',
            'payment_intent': payment_intent, 'amount': 29900, 'currency': 'gbp',
            'metadata': {'order_id': order}}
        return dict(self.refunds[rid])

    def refund_status(self, refund_id):
        self.calls.append(('refund_status', refund_id))
        if refund_id not in self.refunds:
            raise _absent()  # real StripeSandbox: raw 404 for a foreign refund
        return dict(self.refunds[refund_id])


def signed_with(secret, kind, obj, eid, livemode):
    raw = json.dumps({'id': eid, 'livemode': livemode, 'type': kind,
                      'data': {'object': obj}}).encode()
    signature = hmac.new(secret.encode(), STAMP.encode()+b'.'+raw, hashlib.sha256).hexdigest()
    return raw, 't='+STAMP+',v1='+signature


class EraRoutingUnitTests(unittest.TestCase):
    def setUp(self):
        self.live, self.test = FakeEraClient('live'), FakeEraClient('test')
        self.provider = EraRoutingStripe(self.live, self.test)

    def args(self, client, method):
        return [a for m, a in client.calls if m == method]

    def test_construction_era_requirements(self):
        self.assertIs(self.provider.live, True)
        self.assertIs(self.provider.simulated, False)
        self.assertEqual(self.provider.session_prefixes, ('cs_live_', 'cs_test_'))
        with self.assertRaisesRegex(PaymentError, 'primary_provider_must_be_live'):
            EraRoutingStripe(FakeEraClient('test'))  # a TEST-era client cannot be primary
        with self.assertRaisesRegex(PaymentError, 'primary_provider_must_be_live'):
            EraRoutingStripe(OfflineStripe())  # never a simulator behind a live facade
        class SimulatedLive:  # belt and braces: live flag alone is not enough
            simulated, live = True, True
        with self.assertRaisesRegex(PaymentError, 'primary_provider_must_be_live'):
            EraRoutingStripe(SimulatedLive())
        with self.assertRaisesRegex(PaymentError, 'legacy_provider_must_be_test_era'):
            EraRoutingStripe(self.live, FakeEraClient('live'))

    def test_checkout_always_routes_to_primary(self):
        session = self.provider.checkout('order1')
        self.assertTrue(session['id'].startswith('cs_live_'))
        self.assertEqual(self.args(self.live, 'checkout'), ['order1'])
        self.assertEqual(self.test.calls, [])

    def test_session_and_expiry_route_by_prefix(self):
        self.live.checkout('o1')
        self.test.checkout('o2')
        self.assertIs(self.provider.session('cs_live_o1')['livemode'], True)
        self.assertIs(self.provider.session('cs_test_o2')['livemode'], False)
        self.provider.expire_session('cs_test_o2')
        self.assertIn(('expire_session', 'cs_test_o2'), self.test.calls)
        # Never cross-era: each client only ever saw its own era's ids.
        self.assertTrue(all(not str(a).startswith('cs_test_') for _, a in self.live.calls))
        self.assertTrue(all(not str(a).startswith('cs_live_') for _, a in self.test.calls))
        for bad in ('cs_prod_x', 'cs_live_../x', 'CS_LIVE_x', 5, 'cs_live_'+'x'*300):
            with self.assertRaises(PaymentError):
                self.provider.session(bad)

    def test_payment_state_and_refund_route_by_intent_prefix(self):
        self.provider.payment_state('pi_live_o1')
        self.provider.refund('pi_test_o2', 'o2')
        self.provider.refund('pi_live_o1', 'o1')
        self.provider.refund_status('re_test_o2')
        self.provider.refund_status('re_live_o1')
        self.assertIn(('payment_state', 'pi_live_o1'), self.live.calls)
        self.assertIn(('refund_status', 're_live_o1'), self.live.calls)
        self.assertIn(('refund', 'pi_test_o2'), self.test.calls)
        self.assertIn(('refund_status', 're_test_o2'), self.test.calls)
        self.assertTrue(all(not str(a).startswith(('pi_test_', 're_test_')) for _, a in self.live.calls))
        self.assertTrue(all(not str(a).startswith(('pi_live_', 're_live_')) for _, a in self.test.calls))
        # Unmarked ids are resolved by authoritative absence, not refused:
        # an unknown intent is absent everywhere (None), a refund of it fails
        # closed, and a foreign refund id raises the provider's own 404.
        self.assertIsNone(self.provider.payment_state('pi_plain'))
        with self.assertRaisesRegex(PaymentError, 'payment_intent_absent'):
            self.provider.refund('pi_plain', 'o')
        with self.assertRaises(urllib.error.HTTPError):
            self.provider.refund_status('re_plain')
        for call in (lambda: self.provider.payment_state('../pi'),
                     lambda: self.provider.refund_status('re-')):
            with self.assertRaisesRegex(PaymentError, 'invalid_'):
                call()

    def test_charge_resolution_routing(self):
        # A foreign charge is the provider's own 404, never a None or a cross-era guess.
        with self.assertRaises(urllib.error.HTTPError):
            self.provider.payment_intent_for_charge('ch_test_1')
        self.live.charges['ch_live_1'] = 'pi_live_o1'
        self.provider.payment_intent_for_charge('ch_live_1')
        self.assertIn(('payment_intent_for_charge', 'ch_live_1'), self.live.calls)
        with self.assertRaises(PaymentError):
            self.provider.payment_intent_for_charge('../ch')

    def test_find_checkout_searches_both_eras_and_refuses_duplicates(self):
        self.live.checkout('only-live')
        self.test.checkout('only-test')
        self.test.checkout('both')
        self.assertEqual(self.provider.find_checkout('only-live', 1000)['id'], 'cs_live_only-live')
        self.assertEqual(self.provider.find_checkout('only-test', 1000)['id'], 'cs_test_only-test')
        self.assertIsNone(self.provider.find_checkout('neither', 1000))
        self.live.checkout('both')
        with self.assertRaisesRegex(PaymentError, 'duplicate_checkout_sessions'):
            self.provider.find_checkout('both', 1000)
        for bad in (lambda: self.provider.find_checkout('../o', 1000),
                    lambda: self.provider.find_checkout('o', -1)):
            with self.assertRaises(PaymentError):
                bad()

    def test_unmarked_intent_resolves_by_authoritative_absence(self):
        # Real Stripe payment-intent ids carry no era marker; the owning client
        # is resolved by query, primary first, legacy on the primary's None.
        ghost_pi = 'pi_1GONEghi789'
        self.live.checkout('liveown')
        self.live.mark_paid('cs_live_liveown')
        self.test.checkout('testown')
        self.test.mark_paid('cs_test_testown')
        self.assertEqual(self.provider.payment_state('pi_live_liveown')['payment_intent'], 'pi_live_liveown')
        # unmarked ids: known to exactly one era
        self.assertEqual(self.provider.payment_state('pi_live_liveown')['payment_intent'], 'pi_live_liveown')
        self.assertEqual(self.provider.payment_state('pi_test_testown')['payment_intent'], 'pi_test_testown')
        unmarked_live = self.live.sessions['cs_live_liveown']['payment_intent'].replace('pi_live_', 'pi_3U')
        unmarked_test = self.test.sessions['cs_test_testown']['payment_intent'].replace('pi_test_', 'pi_3Q')
        self.live.sessions['cs_live_liveown']['payment_intent'] = unmarked_live
        self.test.sessions['cs_test_testown']['payment_intent'] = unmarked_test
        self.assertEqual(self.provider.payment_state(unmarked_live)['payment_intent'], unmarked_live)
        self.assertEqual(self.provider.payment_state(unmarked_test)['payment_intent'], unmarked_test)
        self.assertIn(('payment_state', unmarked_test), self.test.calls)
        # absent everywhere -> None, exactly like a single-era 404
        self.assertIsNone(self.provider.payment_state(ghost_pi))

    def test_refund_resolves_era_before_any_write(self):
        self.test.checkout('legacyown')
        self.test.mark_paid('cs_test_legacyown')
        unmarked = self.test.sessions['cs_test_legacyown']['payment_intent'].replace('pi_test_', 'pi_3Q')
        self.test.sessions['cs_test_legacyown']['payment_intent'] = unmarked
        result = self.provider.refund(unmarked, 'legacyown')
        self.assertEqual(result['payment_intent'], unmarked)
        # the live client may probe to resolve, but must never POST a refund
        self.assertFalse(any(c[0] == 'refund' for c in self.live.calls))
        self.assertTrue(any(c[0] == 'refund' and c[1] == unmarked for c in self.test.calls))
        with self.assertRaisesRegex(PaymentError, 'payment_intent_absent'):
            self.provider.refund('pi_1GONExyz000', 'ghost')

    def test_unmarked_refund_and_charge_fall_back_on_absence(self):
        self.test.checkout('t')
        self.test.mark_paid('cs_test_t')
        cid = self.test.charge_for('pi_test_t')
        rid = self.test.refund('pi_test_t', 't')['id']
        self.assertEqual(self.provider.payment_intent_for_charge(cid), 'pi_test_t')
        self.assertEqual(self.provider.refund_status(rid)['id'], rid)
        self.assertIn(('payment_intent_for_charge', cid), self.test.calls)
        self.assertIn(('refund_status', rid), self.test.calls)
        # live-owned objects stay live; a foreign id without a legacy client
        # propagates the 404 exactly like a single-era client
        self.live.checkout('l')
        self.live.mark_paid('cs_live_l')
        lcid = self.live.charge_for('pi_live_l')
        self.assertEqual(self.provider.payment_intent_for_charge(lcid), 'pi_live_l')
        solo = EraRoutingStripe(FakeEraClient('live'))
        with self.assertRaises(urllib.error.HTTPError):
            solo.payment_intent_for_charge('ch_1FOREIGNa1')
        with self.assertRaises(urllib.error.HTTPError):
            solo.refund_status('re_1FOREIGNa1')

    def test_fail_closed_without_legacy_client(self):
        solo = EraRoutingStripe(FakeEraClient('live'))
        for call in (lambda: solo.session('cs_test_o1'),
                     lambda: solo.expire_session('cs_test_o1'),
                     lambda: solo.payment_state('pi_test_o1'),
                     lambda: solo.refund('pi_test_o1', 'o1'),
                     lambda: solo.refund_status('re_test_o1'),
                     lambda: solo.payment_intent_for_charge('ch_test_1')):
            with self.assertRaisesRegex(PaymentError, 'legacy_provider_unavailable'):
                call()
        # Live-era ids keep working; find_checkout degrades to the live era only.
        session = solo.primary.checkout('x')
        self.assertEqual(solo.session(session['id'])['id'], session['id'])
        self.assertIsNone(solo.find_checkout('nothing-here', 1000))


class VerifyEventEraTests(unittest.TestCase):
    def test_single_secret_and_livemode_behaviour_is_unchanged(self):
        raw, sig = signed_with('s3cret', 'x', {}, 'evt_1', False)
        self.assertEqual(verify_event(raw, sig, 's3cret', float(STAMP))['id'], 'evt_1')
        for bad in (signed_with('other', 'x', {}, 'evt_2', False)[1],
                    't='+STAMP, 't='+STAMP+',v1=deadbeef'):
            with self.assertRaises(PaymentError):
                verify_event(raw, bad, 's3cret', float(STAMP))
        live_raw, live_sig = signed_with('s3cret', 'x', {}, 'evt_3', True)
        with self.assertRaises(PaymentError):  # livemode flag is still strict
            verify_event(live_raw, live_sig, 's3cret', float(STAMP))
        self.assertEqual(verify_event(live_raw, live_sig, 's3cret', float(STAMP), livemode=True)['id'], 'evt_3')

    def test_secret_tuple_verifies_with_any_member(self):
        raw, sig = signed_with('second', 'x', {}, 'evt_t', False)
        self.assertEqual(verify_event(raw, sig, ('first', 'second'), float(STAMP))['id'], 'evt_t')
        with self.assertRaises(PaymentError):
            verify_event(raw, sig, ('first', 'third'), float(STAMP))

    def test_allowed_livemodes_accept_either_era(self):
        for livemode in (True, False):
            raw, sig = signed_with('s', 'x', {}, 'evt_%s' % livemode, livemode)
            self.assertEqual(verify_event(raw, sig, 's', float(STAMP),
                                           livemode=(True, False))['livemode'], livemode)
            with self.assertRaises(PaymentError):
                verify_event(raw, sig, 's', float(STAMP), livemode=(not livemode,))


class DualModeServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='dual-mode-')
        self.now = float(STAMP)
        self.live, self.test = FakeEraClient('live'), FakeEraClient('test')
        self.provider = EraRoutingStripe(self.live, self.test)
        self.secrets = ('live-endpoint-signing-secret', 'test-endpoint-signing-secret')
        self.service = Service(Path(self.temp.name)/'state', self.provider, self.secrets,
                               lambda: self.now,
                               launch=LaunchConfig(live_payments=True, tax_treatment=TAX),
                               validator_probe=lambda: {'passed': True, 'test_double': True})

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def signed(self, secret, kind, obj, eid, livemode):
        return signed_with(secret, kind, obj, eid, livemode)

    def legacy_paid_order(self):
        """A carried-over TEST-era order: checkout bound to the legacy era."""
        oid = self.service.create_checkout_order('opaque-product', 'direct', 'a'*64, 'r'*64, None, None)
        session = self.test.checkout(oid)
        self.test.mark_paid(session['id'])
        return oid, session

    def test_webhook_secret_normalisation_and_stable_derivation(self):
        self.assertEqual(self.service.secrets, self.secrets)
        self.assertEqual(self.service.secret, self.secrets[0])  # stable composite
        self.assertTrue(self.service.era_routing)
        self.assertEqual(self.service.session_prefixes, ('cs_live_', 'cs_test_'))
        self.assertEqual(self.service.allowed_livemodes, (True, False))
        for index, bad_secret in enumerate(((), ('ok', ''), ('ok', 5), None, 12)):
            with self.assertRaisesRegex(Rejected, 'invalid_webhook_secret'):
                Service(Path(self.temp.name)/('bad-secret-%d' % index), self.provider, bad_secret,
                        launch=LaunchConfig(live_payments=True, tax_treatment=TAX))

    def test_idempotent_checkout_derivations_are_stable_under_secret_tuple(self):
        first = self.service.checkout('opaque-product', True, request_id='key'*11, client='c')
        second = self.service.checkout('opaque-product', True, request_id='key'*11, client='c')
        self.assertEqual((first['access_token'], first['recovery_token']),
                         (second['access_token'], second['recovery_token']))
        self.assertTrue(first['session_id'].startswith('cs_live_'))

    def test_live_checkout_remains_strictly_live(self):
        order = self.service.checkout('opaque-product', True, client='buyer')
        sid = order['session_id']
        self.assertTrue(sid.startswith('cs_live_'))
        self.assertIn(('checkout', order['order_id']), self.live.calls)
        self.assertEqual([c for c in self.test.calls if c[0] == 'checkout'], [])
        row = self.service.get(order['order_id'])
        # Cross-era mismatches stay refused in both directions: a cs_live_
        # object must be livemode True, and a cs_test_-shaped binding must
        # carry TEST-era livemode False (checked via the legacy order below).
        with self.assertRaises(Rejected):
            self.service.validate_checkout(row, dict(self.live.sessions[sid], livemode=False))
        as_test_era = dict(self.live.sessions[sid], id='cs_test_'+sid[8:])
        with self.assertRaises(Rejected):
            self.service.validate_checkout(dict(row, session=as_test_era['id']), as_test_era)

    def test_webhook_accepts_either_secret_and_either_era(self):
        # Live era, live endpoint secret.
        order = self.service.checkout('opaque-product', True, client='buyer')
        self.live.mark_paid(order['session_id'])
        self.service.webhook(*self.signed(self.secrets[0], 'checkout.session.completed',
                                          {'id': order['session_id']}, 'evt_live_paid', True))
        self.assertEqual(self.service.get(order['order_id'])['payment'], 'paid')
        # TEST era, TEST-era endpoint secret.
        oid, session = self.legacy_paid_order()
        self.service.webhook(*self.signed(self.secrets[1], 'checkout.session.completed',
                                          {'id': session['id']}, 'evt_test_paid', False))
        row = self.service.get(oid)
        self.assertEqual((row['payment'], row['payment_intent'], row['session']),
                         ('paid', 'pi_test_'+oid, session['id']))
        # A signature under neither secret is still refused.
        with self.assertRaises(PaymentError):
            self.service.webhook(*self.signed('wrong-secret', 'checkout.session.completed',
                                              {'id': session['id']}, 'evt_bad', False))

    def test_test_era_paid_order_reconciles_green_through_legacy(self):
        oid, session = self.legacy_paid_order()
        self.service.webhook(*self.signed(self.secrets[1], 'checkout.session.completed',
                                          {'id': session['id']}, 'evt_test_paid', False))
        page = self.service.reconcile_ledger()
        self.assertTrue(page['passed'], page)
        self.assertTrue(self.service.reconcile_reservations()['passed'])
        row = self.service.get(oid)
        self.assertEqual((row['payment'], row['reservation']), ('paid', 'held'))
        # Every provider touch for this order went to the legacy client.
        self.assertIn(('session', session['id']), self.test.calls)
        self.assertIn(('payment_state', 'pi_test_'+oid), self.test.calls)
        self.assertTrue(all(not str(a).startswith(('cs_test_', 'pi_test_', 're_test_'))
                            for _, a in self.live.calls))
        # A lost session binding is recovered by era-routed find_checkout.
        with self.service.lock, self.service.db:
            lost = self.service.get(oid)
            lost['session'] = None
            self.service.save(lost)
        repaired = self.service.reconcile_ledger(only=oid)
        self.assertTrue(repaired['passed'], repaired)
        self.assertEqual(repaired['repaired'], 1)
        self.assertIn(('find_checkout', oid), self.test.calls)
        self.assertEqual(self.service.get(oid)['session'], session['id'])

    def test_stale_reconciliation_failure_mark_clears_on_clean_sweep(self):
        # Regression (live cutover): a paid TEST-era order whose reservation
        # was marked reconciliation_failed during the broken-key era must not
        # fail every future sweep when its clean pass transitions nothing
        # (refund blocked by dispute, obligation not yet expired).
        oid, session = self.legacy_paid_order()
        self.service.webhook(*self.signed(self.secrets[1], 'checkout.session.completed',
                                          {'id': session['id']}, 'evt_stale_1', False))
        with self.service.lock, self.service.db:
            row = self.service.get(oid)
            row.update(reservation='reconciliation_failed',
                       next_action='reconcile_reservations', retry_at=0)
            self.service.save(row)
        sweep = self.service.reconcile_reservations()
        self.assertTrue(sweep['passed'], sweep)
        row = self.service.get(oid)
        self.assertEqual((row['payment'], row['reservation']), ('paid', 'held'))
        self.assertTrue(self.service.reconcile_reservations()['passed'])

    def test_test_era_refund_routes_to_legacy_client(self):
        oid, session = self.legacy_paid_order()
        self.service.webhook(*self.signed(self.secrets[1], 'checkout.session.completed',
                                          {'id': session['id']}, 'evt_test_paid', False))
        self.assertEqual(self.service.request_refund(oid, 'a'*64)['refund'], 'pending')
        rid = 're_test_'+oid
        self.assertIn(('refund', 'pi_test_'+oid), self.test.calls)
        self.assertEqual([c for c in self.live.calls if c[0] == 'refund'], [])
        self.test.refunds[rid]['status'] = 'succeeded'
        self.assertEqual(self.service.reconcile_refund(oid)['refund'], 'succeeded')
        self.assertIn(('refund_status', rid), self.test.calls)

    def test_test_era_order_fails_closed_without_legacy_client(self):
        oid, session = self.legacy_paid_order()
        with self.service.lock, self.service.db:
            carried = self.service.get(oid)
            carried.update(session=session['id'], payment='paid', payment_intent='pi_test_'+oid)
            self.service.save(carried)
        solo = EraRoutingStripe(FakeEraClient('live'))
        self.service.provider = solo  # live cutover without the TEST key
        page = self.service.reconcile_ledger()
        self.assertFalse(page['passed'])
        self.assertIn(oid, page['unresolved'])
        self.assertEqual(self.service.get(oid)['reservation'], 'reconciliation_failed')
        self.assertEqual([c for c in solo.primary.calls], [])  # never crossed eras to look
