import base64
import copy
import hashlib
import hmac
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import xreg
from . import operator, worker
from .payments import OfflineStripe, PaymentError, StripeSandbox, verify_event
from .service import Rejected, Service


def signed(service, kind, obj, eid='evt_test', stamp=None):
    raw = json.dumps({'id': eid, 'livemode': False, 'type': kind, 'data': {'object': obj}}).encode()
    stamp = str(int(service.clock()) if stamp is None else stamp)
    signature = hmac.new(service.secret.encode(), stamp.encode()+b'.'+raw, hashlib.sha256).hexdigest()
    return raw, 't='+stamp+',v1='+signature


def paid(service):
    order = service.checkout('opaque-product', True)
    service.provider.sessions[order['session_id']]['payment_status'] = 'paid'
    service.webhook(*signed(service, 'checkout.session.completed', {'id': order['session_id']}, 'evt_'+order['order_id']))
    return order


def example_payload():
    # Synthetic XML for security/unit cases only. Public-data allowlist is patched
    # explicitly in individual tests; shipping intake cannot accept this payload.
    data = b'<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"/>'
    return {'pair': worker.PAIR, 'fixtures': [{'id': 'f001', 'old': base64.b64encode(data).decode()}]}


def fake_execute(decoded, policy, directory):
    sample = json.loads((worker.ROOT/'evidence/gate1/verification-clean/maintenance/results.json').read_bytes())
    return {'results.json': xreg.canonical(sample), 'junit.xml': xreg.junit(sample), 'report.html': worker.report_html(sample)}, {
        'exit_code': 1, 'wall_seconds': 0.01, 'temporary_inputs_and_intermediates_removed': True,
        'summary': sample['summary'], 'test_double': True}


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pilot-test-')
        self.now = 1800000000.0
        self.provider = OfflineStripe()
        self.service = Service(Path(self.temp.name)/'state', self.provider, 'synthetic-test-signing-value', lambda:self.now)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def submit(self, order, payload=None, executor=fake_execute):
        with patch('pilot.worker.public_hashes', return_value={xreg.sha(base64.b64decode(example_payload()['fixtures'][0]['old']))}):
            return self.service.submit(order['order_id'], order['access_token'], payload or example_payload(), executor)

    def test_gate_and_live_locks(self):
        with self.assertRaises(PaymentError):
            StripeSandbox('sk_live_forbidden', 'https://example.test')
        self.assertFalse(self.service.policy['customer_uploads'])
        self.assertEqual(self.service.policy['additional_cash_budget_gbp'], 0)

    def test_attestation_and_purchase_gates(self):
        with self.assertRaises(Rejected):
            self.service.checkout('product', False)
        order = paid(self.service)
        for gate in ('delivery', 'spending', 'acquisition'):
            self.service.pause(gate)
        with self.assertRaises(Rejected):
            self.service.checkout('product', True)
        self.assertEqual(self.submit(order)['job'], 'delivered')

    def test_safety_pause_refund_still_available(self):
        order = paid(self.service)
        self.service.pause('safety')
        with self.assertRaises(Rejected):
            self.submit(order)
        self.assertEqual(self.service.request_refund(order['order_id'], order['access_token'])['refund'], 'pending')

    def test_unsigned_stale_tampered_live_events(self):
        order = self.service.checkout('product', True)
        raw, sig = signed(self.service, 'checkout.session.completed', {'id': order['session_id']})
        for data, signature in ((raw, ''), (raw+b' ', sig), signed(self.service, 'x', {}, stamp=int(self.now-301))):
            with self.assertRaises(PaymentError):
                self.service.webhook(data, signature)
        with self.assertRaises(PaymentError):
            verify_event(raw.replace(b'false', b'true'), sig, self.service.secret, self.now)

    def test_payment_checks_and_duplicate_out_of_order(self):
        order = self.service.checkout('product', True)
        session = self.provider.sessions[order['session_id']]
        session['payment_status'] = 'paid'
        for key, invalid in (('currency','eur'), ('amount_total', 299), ('amount_subtotal',1), ('mode','subscription'), ('livemode',True)):
            original = session[key]
            session[key] = invalid
            with self.assertRaises(Rejected):
                self.service.webhook(*signed(self.service, 'checkout.session.completed', {'id': session['id']}))
            session[key] = original
        raw, sig = signed(self.service, 'checkout.session.async_payment_succeeded', {'id':session['id']})
        self.service.webhook(raw, sig)
        self.assertEqual(self.service.webhook(raw, sig), {'duplicate': True})
        self.service.webhook(*signed(self.service, 'checkout.session.completed', {'id':session['id']}, 'evt_late_completed'))
        self.service.webhook(*signed(self.service, 'checkout.session.async_payment_failed', {'id':session['id']}, 'evt_older_failure'))
        count = self.service.db.execute("SELECT count(*) FROM events WHERE kind='payment_verified'").fetchone()[0]
        self.assertEqual(count, 1)

    def test_no_payment_no_fulfilment(self):
        order = self.service.checkout('product', True)
        with self.assertRaises(Rejected):
            self.submit(order)
        self.service.webhook(*signed(self.service, 'checkout.session.completed', {'id':order['session_id']}))
        self.assertEqual(self.service.status(order['order_id'], order['access_token'])['payment'], 'unpaid')

    def test_authentication_and_recovery(self):
        order = paid(self.service)
        with self.assertRaises(Rejected):
            self.service.status(order['order_id'], 'incorrect')
        new = self.service.recover(order['order_id'], order['recovery_token'])
        with self.assertRaises(Rejected):
            self.service.status(order['order_id'], order['access_token'])
        with self.assertRaises(Rejected):
            self.service.recover(order['order_id'], order['recovery_token'])
        self.assertEqual(self.service.status(order['order_id'], new['access_token'])['payment'], 'paid')

    def test_rerun_and_expiry_and_scope(self):
        order = paid(self.service)
        self.submit(order)
        different = example_payload()
        different['fixtures'][0]['id'] = 'f002'
        with self.assertRaises(Rejected):
            self.submit(order, different)
        self.assertEqual(self.submit(order)['runs'], 2)
        with self.assertRaises(Rejected):
            self.submit(order)
        another = paid(self.service)
        self.submit(another)
        self.now += 7*86400+1
        with self.assertRaises(Rejected):
            self.submit(another)

    def test_input_security_and_public_only(self):
        cases = []
        original = example_payload()
        cases.append(original)
        for fid in ('../secret', 'C:\\secret', '<script>', 'a/b', ''):
            payload = copy.deepcopy(original)
            payload['fixtures'][0]['id'] = fid
            cases.append(payload)
        for data in (b'<!DOCTYPE a [<!ENTITY e SYSTEM "file:///secret">]><a>&e;</a>', b'PK\x03\x04archive', b'<broken', b'\xff\xfe<\x00a\x00/\x00>\x00'):
            payload = copy.deepcopy(original)
            payload['fixtures'][0]['old'] = base64.b64encode(data).decode()
            cases.append(payload)
        payload = copy.deepcopy(original)
        payload['fixtures'] *= 251
        cases.append(payload)
        payload = copy.deepcopy(original)
        payload['fixtures'][0]['path'] = '../outside'
        cases.append(payload)
        payload = copy.deepcopy(original)
        payload['pair'] = 'other'
        cases.append(payload)
        for payload in cases:
            with self.subTest(payload_type=list(payload)):
                with self.assertRaises(xreg.InputError):
                    worker.decode_submission(payload, self.service.policy)
        self.assertEqual(list((self.service.state/'temporary').iterdir()), [])

    def test_refund_duplicate_pending_success_out_of_order(self):
        order = paid(self.service)
        oid, token = order['order_id'], order['access_token']
        self.assertEqual(self.service.request_refund(oid, token)['refund'], 'pending')
        self.service.request_refund(oid, token)
        self.assertEqual(self.provider.refund_calls, 1)
        with self.assertRaises(Rejected):
            self.submit(order)
        rid = 're_'+oid
        self.provider.refunds[rid]['status'] = 'succeeded'
        # Event says failed, latest provider state says succeeded.
        self.service.webhook(*signed(self.service, 'refund.failed', {'id':rid,'status':'failed'}, 'evt_refund'))
        self.assertEqual(self.service.status(oid, token)['refund'], 'succeeded')
        self.provider.refunds[rid]['status'] = 'pending'
        self.service.reconcile_refund(oid)
        self.assertEqual(self.service.status(oid, token)['refund'], 'succeeded')

    def test_refund_network_failure_keeps_request_and_idempotency(self):
        order = paid(self.service)
        method = self.provider.refund
        def lost_response(pi, oid):
            method(pi, oid)
            raise OSError('lost response')
        with patch.object(self.provider, 'refund', lost_response):
            self.assertEqual(self.service.request_refund(order['order_id'],order['access_token'])['refund'], 'requested')
        self.assertEqual(self.service.reconcile_refund(order['order_id'])['refund'], 'pending')
        self.assertEqual(len(self.provider.refunds), 1)

    def test_refund_failure_blocks_purchases(self):
        order = paid(self.service)
        self.service.request_refund(order['order_id'],order['access_token'])
        self.provider.refunds['re_'+order['order_id']]['status'] = 'failed'
        self.service.reconcile_refund(order['order_id'])
        with self.assertRaises(Rejected):
            self.service.checkout('product', True)

    def test_report_confidentiality_injection_and_expiry(self):
        order = paid(self.service)
        self.submit(order)
        with self.assertRaises(Rejected):
            self.service.report(order['order_id'], 'bad', 'report.html')
        sample = json.loads(self.service.report(order['order_id'], order['access_token'], 'results.json'))
        sample['untrusted'] = '<script>alert(1)</script><img src="https://evil">'
        rendered = worker.report_html(sample)
        self.assertNotIn(b'<script>', rendered)
        self.assertIn(b'&lt;script&gt;', rendered)
        self.now += 8*86400+1
        with self.assertRaises(Rejected):
            self.service.report(order['order_id'], order['access_token'], 'report.html')

    def test_execution_failure_and_timeout_cleanup(self):
        data = base64.b64decode(example_payload()['fixtures'][0]['old'])
        original = Path(self.temp.name)/'caller-original.xml'
        original.write_bytes(data)
        for runner in (lambda *a,**k: (_ for _ in ()).throw(OSError('launch failed')),
                       lambda *a,**k: (_ for _ in ()).throw(subprocess.TimeoutExpired('java',0.001))):
            pack, metrics = worker.execute([{'id':'f001','old':data}], self.service.policy, self.service.state/'temporary', runner=runner)
            self.assertEqual(metrics['exit_code'], 2)
            self.assertTrue(metrics['temporary_inputs_and_intermediates_removed'])
            self.assertIn(b'INCOMPLETE', pack['report.html'])
            self.assertIn(b'<error', pack['junit.xml'])
            self.assertEqual(original.read_bytes(), data)
            self.assertEqual(list((self.service.state/'temporary').iterdir()), [])

    def test_worker_unexpected_exception_cleanup(self):
        data = base64.b64decode(example_payload()['fixtures'][0]['old'])
        with patch('xreg.execute', side_effect=RuntimeError('injected')):
            with self.assertRaises(RuntimeError):
                worker.execute([{'id':'f001','old':data}], self.service.policy, self.service.state/'temporary')
        self.assertEqual(list((self.service.state/'temporary').iterdir()), [])

    def test_operator_content_boundary_and_authority(self):
        order = paid(self.service)
        self.service.support(order['order_id'],order['access_token'],'advice')
        spec = operator.request_spec(self.service)
        self.assertNotIn(order['order_id'], json.dumps(spec))
        self.assertNotIn('opaque-product', json.dumps(spec))
        self.assertEqual(operator.tick(self.service)['status'], 'LAUNCH_BLOCKER')
        for name, args in (('raise_budget', {'amount':1000}), ('maintenance', {'code':'shell'}), ('pause_acquisition', {'gate':'safety'})):
            with self.assertRaises(Rejected):
                operator.dispatch(self.service, name, args)
        self.assertEqual(operator.dispatch(self.service,'maintenance',{'code':'expire_reports'})['expired_report_rows_remaining'], 0)

    def test_durable_state_restart(self):
        order = paid(self.service)
        self.service.close()
        self.service = Service(Path(self.temp.name)/'state', self.provider, 'synthetic-test-signing-value', lambda:self.now)
        self.assertEqual(self.service.status(order['order_id'],order['access_token'])['payment'], 'paid')

    def test_submission_retry_is_not_rerun(self):
        order = paid(self.service)
        data = base64.b64decode(example_payload()['fixtures'][0]['old'])
        with patch('pilot.worker.public_hashes',return_value={xreg.sha(data)}):
            call = lambda p: self.service.submit(order['order_id'],order['access_token'],p,fake_execute,request_id='retry-1')
            first = call(example_payload())
            self.assertEqual(call(example_payload()),first)
            self.assertEqual(first['runs'],1)
            changed = example_payload()
            changed['fixtures'][0]['id'] = 'changed'
            with self.assertRaises(Rejected):
                call(changed)

    def test_incomplete_delivery_never_clean(self):
        order = paid(self.service)
        def incomplete(*args):
            pack, metrics = fake_execute(*args)
            metrics['exit_code'] = 3
            return pack,metrics
        self.assertEqual(self.submit(order,executor=incomplete)['job'],'delivered_incomplete')

    def test_spending_reservations_are_durable(self):
        self.service.policy['simulated_compute_budget_pence'] = 200
        self.service.checkout('product',True)
        with self.assertRaises(Rejected):
            self.service.checkout('product',True)
        self.assertEqual(self.service.operator_snapshot()['simulated_reserved_pence'],200)

    def test_job_bound_and_cleanup_after_real_process_timeout(self):
        import sys
        data = base64.b64decode(example_payload()['fixtures'][0]['old'])
        policy = dict(self.service.policy, job_timeout_seconds=.15, batch_timeout_seconds=.1)
        def sleeping_process(command, **kwargs):
            # Real child process, forcibly timed out by subprocess.run. No Java
            # invocation or private content; tests supervisor/cleanup semantics.
            return subprocess.run([sys.executable,'-c','import time; time.sleep(10)'],**kwargs)
        pack,metrics = worker.execute([{'id':'f','old':data}],policy,self.service.state/'temporary',runner=sleeping_process)
        self.assertEqual(metrics['exit_code'],2)
        self.assertTrue(metrics['temporary_inputs_and_intermediates_removed'])
        self.assertIn(b'INCOMPLETE',pack['report.html'])

    def test_delivery_failure_closes_acquisition(self):
        order = paid(self.service)
        def failure(*args):
            raise RuntimeError('injected')
        self.assertEqual(self.submit(order,executor=failure)['job'],'failed')
        with self.assertRaises(Rejected):
            self.service.checkout('new-product',True)
        self.assertEqual(self.submit(order)['job'],'delivered')

    def test_dependency_drift_fails_closed(self):
        with patch('xreg.tree_identity', return_value=[]):
            with self.assertRaises(xreg.InputError):
                worker.verify_pinned_core()

    def test_stripe_refund_schema_checks_linked_sandbox_intent(self):
        import io
        provider = StripeSandbox('sk_test_SYNTHETIC_NOT_A_CREDENTIAL','https://example.test')
        refund = {'object':'refund','id':'re_demo','payment_intent':'pi_demo','amount':29900,
                  'currency':'gbp','status':'pending','metadata':{'order_id':'demo'}}
        for live in (False,True):
            responses = [io.BytesIO(json.dumps(refund).encode()),
                         io.BytesIO(json.dumps({'id':'pi_demo','object':'payment_intent','livemode':live}).encode())]
            with patch('urllib.request.urlopen',side_effect=responses):
                if live:
                    with self.assertRaises(PaymentError):
                        provider.refund_status('re_demo')
                else:
                    self.assertIs(provider.refund_status('re_demo')['livemode'],False)

    def test_stripe_lost_checkout_response_requires_complete_search(self):
        provider = StripeSandbox('sk_test_SYNTHETIC_NOT_A_CREDENTIAL','https://example.test')
        match = {'id':'cs_test_match','client_reference_id':'order1',
                 'metadata':{'order_id':'order1'},'livemode':False}
        pages = [
            {'object':'list','data':[{'id':'cs_test_other','livemode':False}],
             'has_more':True},
            {'object':'list','data':[match],'has_more':False},
        ]
        with patch.object(provider, 'request', side_effect=pages) as request:
            self.assertEqual(provider.find_checkout('order1', 1000)['id'],'cs_test_match')
            self.assertEqual(request.call_args_list[0].args[2]['created[gte]'], 700)
            self.assertEqual(request.call_args_list[1].args[2]['starting_after'],
                             'cs_test_other')
        with patch.object(provider, 'request', return_value={
                'object':'list','data':[{'id':'cs_test_next','livemode':False}],
                'has_more':True}):
            with self.assertRaisesRegex(PaymentError, 'checkout_search_incomplete'):
                provider.find_checkout('order1', 1000)

    def test_stripe_expiry_race_observes_paid_session(self):
        import urllib.error
        provider = StripeSandbox('sk_test_SYNTHETIC_NOT_A_CREDENTIAL','https://example.test')
        open_session = {'id':'cs_test_demo','status':'open','payment_status':'unpaid'}
        paid_session = dict(open_session, status='complete', payment_status='paid')
        race = urllib.error.HTTPError('https://api.stripe.com', 400, 'already complete',
                                     {}, None)
        with patch.object(provider, 'session', side_effect=[open_session, paid_session]):
            with patch.object(provider, 'request', side_effect=race):
                self.assertEqual(provider.expire_session('cs_test_demo'), paid_session)


if __name__ == '__main__':
    unittest.main()
