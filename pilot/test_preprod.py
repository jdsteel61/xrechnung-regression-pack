"""Regression tests for the final pre-production hardening pass (PREPROD_GATE.md)."""
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import xreg
from . import launch_config, operator, railway_app, worker
from .launch_config import LaunchConfig, LaunchConfigError
from .payments import OfflineStripe, PaymentError, StripeSandbox
from .server import make_server
from .service import Rejected, Service
from .test_pilot import example_payload, fake_execute, paid, signed

ACK = launch_config.PRODUCTION_ACK
READ = 'read-only-operator-credential-0123456789'
ACTION = 'action-operator-credential-abcdef0123456789'


class Base(unittest.TestCase):
    probe = staticmethod(lambda: {'passed': True, 'test_double': True})

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='preprod-')
        self.now = 1800000000.0
        self.provider = OfflineStripe()
        self.service = Service(Path(self.temp.name)/'state', self.provider, 'synthetic-signing', lambda: self.now,
                               validator_probe=lambda: self.probe())

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def submit(self, order, executor=fake_execute):
        payload = example_payload()
        with patch('pilot.worker.public_hashes', return_value={xreg.sha(base64.b64decode(payload['fixtures'][0]['old']))}):
            return self.service.submit(order['order_id'], order['access_token'], payload, executor)

    def gates(self):
        return dict(self.service.db.execute('SELECT name,healthy FROM gates'))

    def pi(self, order):
        return self.service.get(order['order_id'])['payment_intent']


class LaunchConfigTests(unittest.TestCase):
    def test_defaults_are_off(self):
        self.assertEqual(launch_config.load({}), LaunchConfig(False, False, None))
        self.assertEqual(launch_config.load({'XREG_LIVE_PAYMENTS': 'false', 'XREG_CUSTOMER_UPLOADS': ''}),
                         LaunchConfig(False, False, None))

    def test_invalid_or_incomplete_config_prevents_launch(self):
        live = {'XREG_LIVE_PAYMENTS': 'true', 'XREG_PRODUCTION_ACK': ACK, 'STRIPE_LIVE_KEY': 'sk_live_x',
                'STRIPE_LIVE_WEBHOOK_SECRET': 'whsec_x', 'XREG_TAX_TREATMENT': launch_config.TAX_TREATMENTS[0]}
        for env in ({'XREG_LIVE_PAYMENTS': 'yes'}, {'XREG_CUSTOMER_UPLOADS': '1'}, {'XREG_LIVE_PAYMENTS': 'TRUE'},
                    {'XREG_CUSTOMER_UPLOADS': 'true'},  # no ack
                    {'XREG_CUSTOMER_UPLOADS': 'true', 'XREG_PRODUCTION_ACK': 'reviewed'},
                    dict(live, STRIPE_LIVE_KEY='sk_test_x'), dict(live, STRIPE_LIVE_WEBHOOK_SECRET=''),
                    dict(live, XREG_TAX_TREATMENT='zero'), dict(live, XREG_ALLOW_SIMULATED_PAYMENTS='1')):
            with self.subTest(env=env), self.assertRaises(LaunchConfigError):
                launch_config.load(env)
        self.assertTrue(launch_config.load(live).live_payments)
        self.assertTrue(launch_config.load({'XREG_CUSTOMER_UPLOADS': 'true', 'XREG_PRODUCTION_ACK': ACK}).customer_uploads)

    def test_deploy_entrypoint_validates_before_any_connection(self):
        with patch.dict(os.environ, {'XREG_LIVE_PAYMENTS': 'maybe'}):
            with self.assertRaises(LaunchConfigError):
                railway_app.build_service()

    def test_provider_mode_must_match_config(self):
        with self.assertRaises(PaymentError):
            StripeSandbox('sk_test_x', 'https://example.test', live=True)
        with self.assertRaises(PaymentError):
            StripeSandbox('sk_live_x', 'https://example.test')
        live_provider = StripeSandbox('sk_live_x', 'https://example.test', live=True)
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(Rejected, 'live_payments_require_live_provider'):
                Service(Path(folder)/'a', OfflineStripe(), 's', launch=LaunchConfig(live_payments=True))
            with self.assertRaisesRegex(Rejected, 'live_provider_requires_live_payments_config'):
                Service(Path(folder)/'b', live_provider, 's')
            service = Service(Path(folder)/'c', live_provider, 's',
                              launch=LaunchConfig(live_payments=True, tax_treatment='x'))
            self.assertTrue(service.livemode)
            # A TEST-mode session is refused by a live service and vice versa.
            with self.assertRaises(Rejected):
                service.validate_checkout({'id': 'o', 'session': 'cs_test_1'}, {'livemode': False})
            service.close()

    def test_customer_uploads_follow_config_not_source(self):
        payload = example_payload()
        with self.assertRaisesRegex(xreg.InputError, 'customer_uploads_disabled'):
            worker.decode_submission(payload, {**json.loads(Path(worker.__file__).with_name('policy.json').read_text())})
        with tempfile.TemporaryDirectory() as folder:
            service = Service(Path(folder)/'s', OfflineStripe(), 's',
                              launch=LaunchConfig(customer_uploads=True))
            self.assertTrue(service.policy['customer_uploads'])
            self.assertEqual(len(worker.decode_submission(payload, service.policy)), 1)
            # O5 pre-flight (23 Sep 2026): the full submit path — decode,
            # admission, job execution, delivery — on a fixture OUTSIDE the
            # frozen public corpus, with no allowlist patch. This is exactly
            # what XREG_CUSTOMER_UPLOADS=true enables in production, proven
            # here before the owner flips it.
            order = paid(service)
            self.assertEqual(
                service.submit(order['order_id'], order['access_token'], payload,
                               fake_execute)['job'], 'delivered')
            service.close()
            default = Service(Path(folder)/'d', OfflineStripe(), 's')
            with self.assertRaises(xreg.InputError):
                worker.decode_submission(payload, default.policy)
            default.close()

    def test_operator_tokens_split_and_validated(self):
        with self.assertRaisesRegex(ValueError, 'legacy'):
            railway_app.operator_tokens({'OPERATOR_TOKEN': ACTION})
        with self.assertRaisesRegex(ValueError, 'differ'):
            railway_app.operator_tokens({'OPERATOR_READ_TOKEN': ACTION, 'OPERATOR_ACTION_TOKEN': ACTION})
        with self.assertRaisesRegex(ValueError, 'invalid_characters'):
            railway_app.operator_tokens({'OPERATOR_READ_TOKEN': '﻿'+READ})
        with self.assertRaisesRegex(ValueError, 'short'):
            railway_app.operator_tokens({'OPERATOR_READ_TOKEN': 'short'})
        self.assertEqual(railway_app.operator_tokens({'OPERATOR_READ_TOKEN': READ, 'OPERATOR_ACTION_TOKEN': ACTION}),
                         (READ, ACTION))


class CheckoutFloodTests(Base):
    def test_anonymous_flood_cannot_hold_capacity_and_expires(self):
        # Many distinct clients, one checkout each: capped unpaid slots, no gate closes.
        for i in range(self.service.policy['max_unpaid_checkouts']):
            self.service.checkout('p', True, client='10.0.0.%d' % i)
        with self.assertRaisesRegex(Rejected, 'checkout_capacity_busy'):
            self.service.checkout('p', True, client='10.0.1.1')
        self.assertTrue(all(self.gates().values()))
        # Paid capacity is untouched; after the provider-session TTL every
        # unpaid reservation is released deterministically.
        self.now += self.service.policy['unpaid_checkout_ttl_seconds'] + 1
        self.assertTrue(self.service.reconcile_reservations()['passed'])
        orders = [json.loads(r[0]) for r in self.service.db.execute('SELECT data FROM orders')]
        self.assertTrue(all(o['reservation'] == 'released' for o in orders))
        # A legitimate buyer can now check out and pay.
        order = paid(self.service)
        self.assertEqual(self.service.get(order['order_id'])['payment'], 'paid')

    def test_per_client_rate_limit(self):
        limit = self.service.policy['checkout_rate_limit_per_client']
        for _ in range(limit):
            self.service.checkout('p', True, client='203.0.113.9')
        with self.assertRaisesRegex(Rejected, 'checkout_rate_limited'):
            self.service.checkout('p', True, client='203.0.113.9')
        self.assertTrue(all(self.gates().values()))
        self.service.checkout('p', True, client='198.51.100.7')  # other client unaffected
        self.now += self.service.policy['checkout_rate_window_seconds'] + 1
        self.service.checkout('p', True, client='203.0.113.9')
        stored = [r[0] for r in self.service.db.execute('SELECT client FROM checkout_rate')]
        self.assertNotIn('203.0.113.9', stored)  # only hashes persist

    def test_unpaid_reservation_is_short_lived(self):
        order = self.service.checkout('p', True, client='x')
        row = self.service.get(order['order_id'])
        self.assertEqual(row['reservation_expires'] - row['created'], 1860)


class RefundPolicyTests(Base):
    def test_customer_window_is_seven_days(self):
        order = paid(self.service)
        self.now += 7*86400 + 1
        with self.assertRaisesRegex(Rejected, 'refund_window_closed'):
            self.service.request_refund(order['order_id'], order['access_token'])
        within = paid(self.service)
        self.now += 86400
        self.assertEqual(self.service.request_refund(within['order_id'], within['access_token'])['refund'], 'pending')

    def test_operator_refund_only_for_policy_reasons(self):
        order = paid(self.service)
        for args in ({'order_id': order['order_id']}, {'order_id': order['order_id'], 'reason': 'goodwill'},
                     {'order_id': order['order_id'], 'reason': 'customer_request_within_7_days', 'amount': 100}):
            with self.assertRaises(Rejected):
                operator.dispatch(self.service, 'refund', args)
        with self.assertRaisesRegex(Rejected, 'refund_reason_not_eligible'):
            operator.dispatch(self.service, 'refund', {'order_id': order['order_id'], 'reason': 'pack_not_deliverable'})
        self.now += 8*86400
        with self.assertRaisesRegex(Rejected, 'refund_reason_not_eligible'):
            operator.dispatch(self.service, 'refund', {'order_id': order['order_id'],
                                                       'reason': 'customer_request_within_7_days'})
        self.assertEqual(self.service.get(order['order_id'])['refund'], 'none')

    def test_pack_not_deliverable_refund_and_automatic_refund(self):
        order = paid(self.service)
        other = paid(self.service)
        def broken(*a):
            raise OSError('synthetic')
        self.assertEqual(self.submit(order, broken)['job'], 'failed')
        result = operator.dispatch(self.service, 'refund', {'order_id': order['order_id'],
                                                           'reason': 'pack_not_deliverable'})
        self.assertEqual(result['refund'], 'pending')
        self.assertEqual(self.provider.refunds['re_'+order['order_id']]['amount'], 29900)
        # Automatic full refund when the obligation lapses undelivered.
        self.now += 7*86400 + 1
        self.service.reconcile_reservations()
        self.assertEqual(self.service.get(other['order_id'])['refund'], 'pending')


class StripeReconciliationTests(Base):
    def test_dashboard_refund_converges_without_db_repair(self):
        order = paid(self.service)
        self.provider.external_refund(self.pi(order))
        self.service.webhook(*signed(self.service, 'charge.refunded', {'id': 'ch_1', 'payment_intent': self.pi(order)},
                                     'evt_charge_refunded'))
        row = self.service.get(order['order_id'])
        self.assertEqual((row['refund'], row['refunded_pence'], row['job']), ('succeeded', 29900, 'refunded'))
        with self.assertRaises(Rejected):
            self.submit(order)
        self.assertTrue(self.service.reconcile_ledger()['passed'])
        self.assertTrue(self.service.reconcile_reservations()['passed'])

    def test_original_f7_wedge_self_resolves(self):
        # Dashboard refund with NO webhook delivered, then an automatic refund:
        # Stripe rejects the second refund; provider state must still converge.
        order = paid(self.service)
        self.provider.external_refund(self.pi(order))
        self.assertEqual(self.service.request_refund(order['order_id'], order['access_token'])['refund'], 'succeeded')
        self.service.reconcile_reservations()
        row = self.service.get(order['order_id'])
        self.assertNotEqual(row['reservation'], 'reconciliation_failed')
        self.service.pause('delivery', 'system', 'synthetic')
        evidence = self.service.verify_gate('delivery')
        self.assertTrue(evidence['passed'], evidence)

    def test_ledger_pass_finds_refund_with_no_event_at_all(self):
        order = paid(self.service)
        self.provider.external_refund(self.pi(order))
        page = self.service.reconcile_ledger()
        self.assertTrue(page['passed'])
        self.assertEqual(page['repaired'], 1)
        self.assertEqual(self.service.get(order['order_id'])['refund'], 'succeeded')

    def test_refund_event_without_metadata(self):
        order = paid(self.service)
        rid = self.provider.external_refund(self.pi(order), status='pending')
        self.service.webhook(*signed(self.service, 'refund.created', {'id': rid}, 'evt_ext_refund'))
        row = self.service.get(order['order_id'])
        self.assertEqual((row['refund'], row['refund_id']), ('pending', rid))

    def test_dispute_observed_blocks_fulfilment_and_is_recoverable(self):
        order = paid(self.service)
        self.provider.external_dispute(self.pi(order))
        self.service.webhook(*signed(self.service, 'charge.dispute.created',
                                     {'id': 'dp_0', 'payment_intent': self.pi(order)}, 'evt_dispute'))
        self.assertEqual(self.service.get(order['order_id'])['dispute'], 'needs_response')
        self.assertEqual(self.gates()['acquisition'], 0)
        with self.assertRaisesRegex(Rejected, 'refund_active'):
            self.submit(order)
        # Ledger is consistent with Stripe, so verification may reopen sales.
        self.assertTrue(self.service.verify_gate('acquisition')['passed'])
        self.assertEqual(self.gates()['acquisition'], 1)

    def test_partial_external_refund_recorded_and_flagged(self):
        order = paid(self.service)
        self.provider.external_refund(self.pi(order), amount=10000)
        self.service.reconcile_ledger()
        row = self.service.get(order['order_id'])
        self.assertEqual((row['refunded_pence'], row['refund']), (10000, 'none'))
        self.assertEqual(self.gates()['acquisition'], 0)
        with self.assertRaises(Rejected):
            self.submit(order)

    def test_unmatched_event_is_accepted_not_retried_forever(self):
        self.assertEqual(self.service.webhook(*signed(
            self.service, 'charge.refunded', {'id': 'ch_x', 'payment_intent': 'pi_unknown'}, 'evt_unknown')),
            {'accepted': True})


class LiveDisputeRaceTests(Base):
    """Regression for the live Slot A trace (21 Sep 2026): Stripe delivered
    charge.dispute.* before checkout.session.completed with charge-only
    payloads, and nothing converged for 20+ minutes on staging."""

    def test_dispute_preceding_session_completion_converges_at_payment(self):
        order = self.service.checkout('opaque-product', True)
        sid, pi = order['session_id'], 'pi_' + order['order_id']
        self.provider.sessions[sid]['payment_status'] = 'paid'
        did = self.provider.external_dispute(pi)
        charge = self.provider.charge_for(pi)
        self.service.webhook(*signed(self.service, 'charge.dispute.created',
                                     {'id': did, 'charge': charge}, 'evt_race_dispute'))
        self.assertIsNone(self.service.get(order['order_id']).get('dispute'))
        self.service.webhook(*signed(self.service, 'checkout.session.completed',
                                     {'id': sid}, 'evt_race_session'))
        row = self.service.get(order['order_id'])
        self.assertEqual(row['payment'], 'paid')
        self.assertEqual(row['dispute'], 'needs_response')
        self.assertEqual(self.gates()['acquisition'], 0)
        with self.assertRaisesRegex(Rejected, 'refund_active'):
            self.submit(order)
        # Sales may resume once the ledger is consistent, but the disputed
        # order itself stays blocked from fulfilment.
        self.assertTrue(self.service.verify_gate('acquisition')['passed'])
        with self.assertRaisesRegex(Rejected, 'refund_active'):
            self.submit(order)

    def test_sweep_converges_dispute_with_no_webhook(self):
        order = paid(self.service)
        self.provider.external_dispute(self.pi(order))
        self.assertTrue(self.service.reconcile_reservations()['passed'])
        row = self.service.get(order['order_id'])
        self.assertEqual(row['dispute'], 'needs_response')
        self.assertEqual(self.gates()['acquisition'], 0)
        with self.assertRaisesRegex(Rejected, 'refund_active'):
            self.submit(order)


class ValidatorHealthGateTests(Base):
    def test_delivery_reopen_requires_validator_health(self):
        self.probe = lambda: {'passed': False, 'error': 'OSError'}
        self.service.pause('delivery', 'system', 'execution_failed')
        evidence = self.service.verify_gate('delivery')
        self.assertFalse(evidence['passed'])
        self.assertFalse(evidence['checks']['validator_health']['passed'])
        self.assertEqual(self.gates()['delivery'], 0)
        def raising():
            raise RuntimeError('probe crashed')
        self.probe = raising
        self.assertFalse(self.service.verify_gate('delivery')['passed'])
        self.probe = lambda: {'passed': True}
        self.assertTrue(self.service.verify_gate('delivery')['passed'])
        self.assertEqual(self.gates()['delivery'], 1)

    def test_other_gates_do_not_run_probe(self):
        self.probe = lambda: {'passed': False}
        self.service.pause('safety', 'system', 'x')
        self.assertTrue(self.service.verify_gate('safety')['passed'])

    @unittest.skipUnless(os.name == 'nt' or os.environ.get('XREG_JAVA'), 'pinned Java runtime required')
    def test_real_canary_passes_on_pinned_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            result = worker.validator_health(self.service.policy, Path(folder))
            self.assertTrue(result['passed'], result)
            self.assertFalse(any(Path(folder).iterdir()))


class OrderRecoveryTests(Base):
    def test_failed_refund_reissued_within_bound(self):
        order = paid(self.service)
        self.service.request_refund(order['order_id'], order['access_token'])
        rid = 're_'+order['order_id']
        self.provider.refunds[rid]['status'] = 'failed'
        self.service.reconcile_refund(order['order_id'])
        self.assertEqual(self.service.get(order['order_id'])['refund'], 'failed')
        result = operator.dispatch(self.service, 'resolve_order', {'order_id': order['order_id']})
        self.assertEqual(result['steps']['refund_reissue'], 1)
        self.assertEqual(result['order']['refund'], 'pending')
        for attempt in (1, 2):
            self.provider.refunds['re_%s_%d' % (order['order_id'], attempt)]['status'] = 'failed'
            self.service.reconcile_refund(order['order_id'])
            result = self.service.resolve_order(order['order_id'])
        self.assertEqual(result['steps']['refund_reissue'], 'limit_reached_owner_required')

    def test_provider_absent_paid_order_voided_only_when_both_absent(self):
        order = paid(self.service)
        with patch.object(OfflineStripe, 'payment_state', return_value=None):
            with patch.object(OfflineStripe, 'session', return_value={'id': 'cs'}):
                # session still exists -> never voided
                self.service.resolve_order(order['order_id'])
            self.assertEqual(self.service.get(order['order_id'])['payment'], 'paid')
            with patch.object(OfflineStripe, 'session', return_value=None):
                result = self.service.resolve_order(order['order_id'])
        self.assertTrue(result['steps']['voided_provider_absent'])
        self.assertEqual((result['order']['payment'], result['order']['reservation']), ('provider_absent', 'released'))
        kinds = [r[0] for r in self.service.db.execute('SELECT kind FROM events')]
        self.assertIn('order_voided_provider_absent', kinds)

    def test_transient_provider_error_stays_unresolved(self):
        order = paid(self.service)
        with patch.object(OfflineStripe, 'session', side_effect=PaymentError('outage')):
            with self.assertRaises(PaymentError):
                self.service.resolve_order(order['order_id'])
        self.assertEqual(self.service.get(order['order_id'])['payment'], 'paid')


class OperatorHttpTests(Base):
    def setUp(self):
        super().setUp()
        self.server = make_server(self.service, operator_token=ACTION, operator_read_token=READ)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.origin = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        super().tearDown()

    def call(self, route, token, body=None):
        req = urllib.request.Request(self.origin+route, data=json.dumps(body).encode() if body is not None else None,
                                     headers={'Authorization': 'Bearer '+token, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_read_token_cannot_mutate(self):
        order = paid(self.service)
        money = lambda: {k: self.service.get(order['order_id'])[k] for k in ('payment', 'refund', 'refund_id', 'job')}
        before = money()
        for name, args in (('refund', {'order_id': order['order_id'], 'reason': 'customer_request_within_7_days'}),
                           ('pause', {'gate': 'acquisition'}), ('verify', {'gate': 'delivery'}),
                           ('resolve_order', {'order_id': order['order_id']}), ('retry', {'code': 'ledger'}),
                           ('maintenance', {'code': 'reconcile_reservations'}), ('pause_acquisition', {})):
            status, body = self.call('/operator/action', READ, {'name': name, 'arguments': args})
            self.assertEqual((status, body['error']), (409, 'operator_read_only_credential'), name)
        self.assertEqual(money(), before)
        self.assertTrue(all(self.gates().values()))
        self.assertEqual(self.call('/operator/action', READ, {'name': 'health', 'arguments': {}})[0], 200)
        status, body = self.call('/operator/action', ACTION, {'name': 'pause', 'arguments': {'gate': 'acquisition'}})
        self.assertEqual((status, self.gates()['acquisition']), (200, 0))

    def test_bounded_read_views(self):
        order = paid(self.service)
        self.service.pause('delivery', 'system', 'execution_failed', {'order_id': order['order_id']})
        with self.service.lock, self.service.db:
            row = self.service.get(order['order_id'])
            row.update(job='failed', error='execution_failed_resubmit_or_refund')
            self.service.save(row)
        for route in ('/operator/audit', '/operator/events', '/operator/errors',
                      '/operator/reconciliation', '/operator/recovery', '/operator/state'):
            for token in (READ, ACTION):
                status, body = self.call(route, token)
                self.assertEqual(status, 200, route)
                encoded = json.dumps(body)
                for secret in (order['access_token'], order['recovery_token'], 'opaque-product',
                               self.service.get(order['order_id'])['access_hash']):
                    self.assertNotIn(secret, encoded)
        self.assertEqual(self.call('/operator/audit', READ)[1]['items'][0]['reason'], 'execution_failed')
        errors = self.call('/operator/errors', READ)[1]['items']
        self.assertEqual(errors[0]['error'], 'execution_failed_resubmit_or_refund')
        self.assertIn('summary', self.call('/operator/reconciliation', READ)[1])
        self.assertEqual(self.call('/operator/events?after_id=1', READ)[0], 200)
        for bad in ('/operator/events?after_id=-1', '/operator/audit?after=x', '/operator/unknown',
                    '/operator/events?after_id=1&after_id=2'):
            self.assertEqual(self.call(bad, READ)[0], 409, bad)
        self.assertEqual(self.call('/operator/audit', order['access_token'])[0], 409)

    def test_long_submission_returns_202_then_completes(self):
        import time
        order = paid(self.service)
        slow_server = make_server(self.service, submit_wait_seconds=0.2)
        thread = threading.Thread(target=slow_server.serve_forever)
        thread.start()
        original = self.service.submit
        def slow(*args, **kwargs):
            def executor(*a):
                time.sleep(1)
                return fake_execute(*a)
            return original(*args, executor=executor, **kwargs)
        payload = example_payload()
        origin = f'http://127.0.0.1:{slow_server.server_port}'
        def post():
            req = urllib.request.Request(origin+'/orders/%s/submit' % order['order_id'], data=json.dumps(payload).encode(),
                headers={'Authorization': 'Bearer '+order['access_token'], 'Content-Type': 'application/json',
                         'Idempotency-Key': 'long-run-1'})
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, json.load(response)
        try:
            with patch.object(self.service, 'submit', side_effect=slow), \
                    patch('pilot.worker.public_hashes',
                          return_value={xreg.sha(base64.b64decode(payload['fixtures'][0]['old']))}):
                status, body = post()
                self.assertEqual((status, body['job']), (202, 'running'))
                deadline = time.monotonic() + 10
                while self.service.get(order['order_id'])['job'] == 'running' and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertEqual(self.service.get(order['order_id'])['job'], 'delivered')
                status, body = post()  # same key -> durable cached result
                self.assertEqual((status, body['job'], body['runs']), (200, 'delivered', 1))
        finally:
            slow_server.shutdown()
            thread.join(timeout=5)
            slow_server.server_close()

    def test_audit_page_is_bounded(self):
        for i in range(120):
            self.service.pause('acquisition', 'system', 'r%d' % i)
            with self.service.lock, self.service.db:
                self.service._set_gate('acquisition', 1, 'test', 'reopen')
        page = self.service.operator_audit()
        self.assertEqual(len(page['items']), 100)
        self.assertEqual(len(self.service.operator_audit(after_id=page['next_after_id'])['items']), 100)


class TokenDeliveryTests(Base):
    """Sprint 1 (owner decision D.4): both capabilities are delivered from the
    verified checkout-success session, with bounded re-display, an honest
    banner once the window closes, and indistinguishable failed lookups."""

    def setUp(self):
        super().setUp()
        self.server = make_server(self.service)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.origin = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        super().tearDown()

    def request(self, path, headers=None, body=None):
        req = urllib.request.Request(self.origin+path, data=body, headers=headers or {},
                                     method='POST' if body is not None else 'GET')
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def display(self, sid, accept='application/json'):
        return self.request('/success?session_id='+sid, {'Accept': accept})

    def test_paid_session_displays_both_tokens_usable_against_orders(self):
        order = paid(self.service)
        status, headers, body = self.display(order['session_id'])
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), 'application/json')
        view = json.loads(body)
        self.assertEqual(view['order_id'], order['order_id'])
        for key in ('access_token', 'recovery_token'):
            # Same opaque keyed-hash capability format as the checkout path.
            self.assertRegex(view[key], r'^[0-9a-f]{64}$')
            self.assertNotEqual(view[key], order[key])
        self.assertNotEqual(view['access_token'], view['recovery_token'])
        # Both displayed tokens authenticate as real order capabilities.
        status, _, body = self.request('/orders/'+order['order_id'],
                                       {'Authorization': 'Bearer '+view['access_token']})
        self.assertEqual((status, json.loads(body)['payment']), (200, 'paid'))
        # The original checkout-response tokens keep working alongside them.
        status, _, _ = self.request('/orders/'+order['order_id'],
                                    {'Authorization': 'Bearer '+order['access_token']})
        self.assertEqual(status, 200)
        # The displayed recovery token drives the existing POST /recover flow.
        status, _, body = self.request('/recover', {'Content-Type': 'application/json'},
                                       json.dumps({'order_id': order['order_id'],
                                                   'recovery_token': view['recovery_token']}).encode())
        self.assertEqual(status, 200)
        status, _, _ = self.request('/orders/'+order['order_id'],
                                    {'Authorization': 'Bearer '+json.loads(body)['access_token']})
        self.assertEqual(status, 200)

    def test_html_variant_displays_tokens(self):
        order = paid(self.service)
        status, headers, body = self.display(order['session_id'], 'text/html')
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), 'text/html')
        view = json.loads(self.display(order['session_id'])[2])
        for token in (view['access_token'], view['recovery_token']):
            self.assertIn(token.encode(), body)
        self.assertIn(order['order_id'].encode(), body)

    def test_redisplay_while_session_live_same_tokens_and_one_event_per_display(self):
        order = paid(self.service)
        first = json.loads(self.display(order['session_id'])[2])
        second = json.loads(self.display(order['session_id'])[2])
        self.assertEqual(first, second)  # deterministic within the live window
        kinds = [r[0] for r in self.service.db.execute(
            "SELECT kind FROM events WHERE kind='tokens_presented'")]
        self.assertEqual(len(kinds), 2)  # duplicate display is safe: no corruption
        # Displays never touch gates, checkout rate limits or capacity.
        self.assertTrue(all(self.gates().values()))
        self.assertEqual(self.service.db.execute('SELECT count(*) FROM checkout_rate').fetchone()[0], 0)

    def test_after_provider_session_expiry_banner_only(self):
        order = paid(self.service)
        shown = json.loads(self.display(order['session_id'])[2])
        for closure in ({'status': 'expired'}, {'expires_at': int(self.now)-1}):
            session = self.provider.sessions[order['session_id']]
            restore = {k: session.get(k) for k in closure}
            session.update(closure)
            status, headers, body = self.display(order['session_id'])
            self.assertEqual(status, 200)
            self.assertIn(b'no access or fulfilment', body.lower())
            self.assertIn(order['order_id'].encode(), body)
            self.assertIn(b'/recover', body)
            for token in (shown['access_token'], shown['recovery_token']):
                self.assertNotIn(token.encode(), body)
            session.update(restore)
        # A capability already displayed outlives the display window.
        status, _, _ = self.request('/orders/'+order['order_id'],
                                    {'Authorization': 'Bearer '+shown['access_token']})
        self.assertEqual(status, 200)

    def test_failed_lookups_are_indistinguishable(self):
        refunded = paid(self.service)
        self.service.request_refund(refunded['order_id'], refunded['access_token'])
        foreign = paid(self.service)
        forged = 'cs_test_'+'ab'*16  # a real order's session served under another id
        self.provider.sessions[forged] = dict(self.provider.sessions[foreign['session_id']], id=forged)
        tampered = paid(self.service)
        self.provider.sessions[tampered['session_id']]['amount_total'] = 100
        unpaid = self.service.checkout('opaque-product', True, client='buyer')
        cases = ('cs_test_'+'0'*32, unpaid['session_id'], refunded['session_id'], forged,
                 tampered['session_id'], '', 'cs_live_'+'0'*16, 'CS_TEST_'+'0'*16,
                 '../orders', 'cs_test_'+'0'*200)
        responses = set()
        for sid in cases:
            status, _, body = self.display(sid)
            self.assertEqual(status, 404, sid)
            responses.add((status, body))
        self.assertEqual(len(responses), 1)  # byte-identical: no class information leaks
        self.assertEqual(json.loads(responses.pop()[1]), {'error': 'not_found'})
        for path in ('/success?session_id=a&session_id=b', '/success?session_id=x&extra=1',
                     '/success?session_id'):
            status, _, body = self.request(path)
            self.assertEqual((status, json.loads(body)), (404, {'error': 'not_found'}), path)

    def test_no_token_material_in_events_or_audit(self):
        order = paid(self.service)
        view = json.loads(self.display(order['session_id'])[2])
        material = (order['access_token'], order['recovery_token'],
                    view['access_token'], view['recovery_token'])
        events = self.service.db.execute('SELECT * FROM events').fetchall()
        audit = self.service.db.execute('SELECT * FROM gate_audit').fetchall()
        self.assertTrue(events)
        for row in list(events)+list(audit):
            encoded = json.dumps([str(value) for value in row])
            for secret in material:
                self.assertNotIn(secret, encoded)
        # Only hashes of the display capabilities are stored, never the tokens.
        stored = self.service.get(order['order_id'])['success_hashes']
        self.assertEqual(set(stored), {'session', 'access', 'recovery'})
        self.assertEqual(stored['session'], order['session_id'])
        for key, token in (('access', view['access_token']), ('recovery', view['recovery_token'])):
            self.assertEqual(stored[key], hashlib.sha256(token.encode()).hexdigest())

    def test_recover_rotates_capabilities_and_revokes_success_display(self):
        order = paid(self.service)
        view = json.loads(self.display(order['session_id'])[2])
        rotated = self.service.recover(order['order_id'], order['recovery_token'])
        for token in (order['access_token'], view['access_token']):  # everything old is dead
            with self.assertRaises(Rejected):
                self.service.status(order['order_id'], token)
        self.assertEqual(self.service.success_view(order['session_id']), {'kind': 'unavailable'})
        status, _, body = self.display(order['session_id'])
        self.assertEqual((status, json.loads(body)), (404, {'error': 'not_found'}))
        status, _, _ = self.request('/orders/'+order['order_id'],
                                    {'Authorization': 'Bearer '+rotated['access_token']})
        self.assertEqual(status, 200)

    def test_redirect_beating_webhook_still_displays_from_provider_state(self):
        order = self.service.checkout('opaque-product', True)
        sid = order['session_id']
        self.provider.sessions[sid]['payment_status'] = 'paid'
        # The browser lands on /success before the signed event is delivered.
        status, _, body = self.display(sid)
        view = json.loads(body)
        self.assertEqual((status, view['order_id']), (200, order['order_id']))
        status, _, body = self.request('/orders/'+order['order_id'],
                                       {'Authorization': 'Bearer '+view['access_token']})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['payment'], 'unpaid')  # event not yet processed
        # The signed event then verifies payment exactly as before.
        self.service.webhook(*signed(self.service, 'checkout.session.completed', {'id': sid}, 'evt_late'))
        self.assertEqual(self.service.get(order['order_id'])['payment'], 'paid')


if __name__ == '__main__':
    unittest.main()
