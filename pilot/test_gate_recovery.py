"""Bounded safety-gate recovery: audited closes and verified reopening only."""
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import xreg
from . import operator, worker
from .object_storage import S3RawFixtures
from .payments import OfflineStripe, PaymentError, StripeSandbox
from .service import Rejected, Service
from .test_object_storage import FakeS3
from .test_pilot import example_payload, fake_execute, paid


class GateRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gate-recovery-')
        self.now = 1800000000.0
        self.provider = OfflineStripe()
        self.client = FakeS3()
        self.store = S3RawFixtures(self.client, 'test-raw')
        self.service = Service(Path(self.temp.name)/'state', self.provider, 'synthetic',
                               clock=lambda: self.now, raw_store=self.store)
        data = base64.b64decode(example_payload()['fixtures'][0]['old'])
        self.allow = patch.object(worker, 'public_hashes', return_value={xreg.sha(data)})
        self.allow.start()

    def tearDown(self):
        self.allow.stop()
        self.service.close()
        self.temp.cleanup()

    def audit(self):
        with self.service.lock:
            return self.service.db.execute(
                'SELECT gate,action,actor,reason,evidence FROM gate_audit ORDER BY id').fetchall()

    def gates(self):
        with self.service.lock:
            return dict(self.service.db.execute('SELECT name,healthy FROM gates'))

    def interrupt_order(self, order, *, deadline=None, key='crashed-attempt'):
        """Durably reproduce a killed worker: running job, NULL submission, raw objects."""
        oid = order['order_id']
        job = self.store.new_job()
        self.store.put(oid, job, 0, 'old', b'synthetic-interrupted')
        with self.service.lock, self.service.db:
            row = self.service.get(oid)
            row.update(job='running', raw_job=job, error=None,
                       job_deadline=self.now + (6000 if deadline is None else deadline))
            row['attempts'] += 1
            self.service.save(row)
            self.service.db.execute('INSERT INTO submissions VALUES (?,?,?,NULL)',
                                    (oid, key, '0'*64))
        return job

    def test_close_records_actor_reason_and_evidence(self):
        order = paid(self.service)
        self.client.fail_delete = True
        result = self.service.submit(order['order_id'], order['access_token'], example_payload(), fake_execute)
        self.assertEqual(result['job'], 'failed')
        rows = [dict(r) for r in self.audit()]
        closers = [(r['gate'], r['reason']) for r in rows if r['action'] == 'closed']
        self.assertIn(('safety', 'raw_input_cleanup_failed'), closers)
        for row in rows:
            self.assertEqual(row['actor'], 'system')
            self.assertTrue(row['reason'])
            evidence = json.loads(row['evidence'])
            self.assertEqual(evidence.get('order_id'), order['order_id'])

    def test_verification_fails_while_cleanup_backlog_exists(self):
        order = paid(self.service)
        self.interrupt_order(order)
        self.service.pause('safety', 'system', 'worker_interrupted', {'order_id': order['order_id']})
        self.client.fail_delete = True
        evidence = self.service.verify_gate('safety', actor='operator')
        self.assertFalse(evidence['passed'])
        self.assertGreater(evidence['checks']['raw_cleanup']['bucket_raw_key_count'], 0)
        self.assertEqual(evidence['checks']['raw_cleanup']['unresolved'], 1)
        self.assertEqual(self.gates()['safety'], 0)
        actions = [(r['gate'], r['action']) for r in self.audit()]
        self.assertIn(('safety', 'verification_failed'), actions)
        self.assertNotIn(('safety', 'reopened'), actions)
        # The interrupted attempt is still durably terminal either way.
        row = self.service.db.execute('SELECT result FROM submissions').fetchone()
        self.assertEqual(json.loads(row['result'])['job'], 'failed')

    def test_verification_repairs_and_reopens_only_when_clean(self):
        order = paid(self.service)
        self.interrupt_order(order)
        self.service.pause('safety', 'system', 'worker_interrupted', {'order_id': order['order_id']})
        self.client.fail_delete = False
        evidence = self.service.verify_gate('safety', actor='operator')
        self.assertTrue(evidence['passed'], evidence)
        self.assertEqual(evidence['checks']['stranded_jobs_failed'], [order['order_id']])
        self.assertEqual(evidence['checks']['interrupted_submissions_remaining'], 0)
        self.assertEqual(evidence['checks']['raw_cleanup']['bucket_raw_key_count'], 0)
        self.assertEqual(self.gates()['safety'], 1)
        reopened = [r for r in self.audit() if r['action'] == 'reopened']
        self.assertEqual(len(reopened), 1)
        self.assertEqual(reopened[0]['actor'], 'operator')
        self.assertEqual(reopened[0]['reason'], 'deterministic_verification_passed')
        # The customer can resubmit on the reopened gate with a fresh key.
        again = self.service.submit(order['order_id'], order['access_token'], example_payload(),
                                    fake_execute, request_id='after-recovery')
        self.assertEqual(again['job'], 'delivered')

    def test_verification_fails_when_ledger_or_reservations_are_unresolved(self):
        order = paid(self.service)
        self.service.pause('delivery', 'system', 'execution_failed', {'order_id': order['order_id']})
        with patch.object(type(self.provider), 'session', side_effect=PaymentError('synthetic_outage')):
            evidence = self.service.verify_gate('delivery', actor='operator')
        self.assertFalse(evidence['passed'])
        self.assertFalse(evidence['checks']['ledger']['passed'])
        self.assertEqual(self.gates()['delivery'], 0)
        with self.service.lock, self.service.db:
            row = self.service.get(order['order_id'])
            row.update(reservation='reconciliation_failed', retry_at=self.now+300)
            self.service.save(row)
        with patch.object(type(self.provider), 'session', side_effect=PaymentError('synthetic_outage')):
            evidence = self.service.verify_gate('delivery', actor='operator')
        self.assertFalse(evidence['passed'])
        self.assertEqual(evidence['checks']['reservations']['failed_ids'], [order['order_id']])
        self.assertEqual(self.gates()['delivery'], 0)

    def test_verification_fails_when_scratch_or_maintenance_is_dirty(self):
        order = paid(self.service)
        self.service.pause('safety', 'system', 'worker_interrupted', {'order_id': order['order_id']})
        stray = self.service.state/'temporary'/'stray'
        stray.mkdir()
        evidence = self.service.verify_gate('safety', actor='operator')
        self.assertFalse(evidence['passed'])
        self.assertFalse(evidence['checks']['health']['scratch_empty'])
        self.assertEqual(self.gates()['safety'], 0)
        stray.rmdir()
        with self.service.lock, self.service.db:
            self.service.db.execute("INSERT INTO maintenance(code,status) VALUES ('verify_acceptance','running')")
        evidence = self.service.verify_gate('safety', actor='operator')
        self.assertFalse(evidence['passed'])
        self.assertEqual(evidence['checks']['health']['interrupted_maintenance'], 1)
        self.assertEqual(self.gates()['safety'], 0)
        with self.service.lock, self.service.db:
            self.service.db.execute("DELETE FROM maintenance")
        evidence = self.service.verify_gate('safety', actor='operator')
        self.assertTrue(evidence['passed'], evidence)
        self.assertEqual(self.gates()['safety'], 1)

    def test_operator_may_request_but_not_bypass_verification(self):
        order = paid(self.service)
        self.interrupt_order(order)
        self.service.pause('safety', 'system', 'worker_interrupted', {'order_id': order['order_id']})
        self.client.fail_delete = True
        # Operator pause of another gate records the operator as actor.
        self.assertEqual(operator.dispatch(self.service, 'pause', {'gate': 'acquisition'}),
                         {'paused': 'acquisition'})
        closed = [r for r in self.audit() if r['action'] == 'closed' and r['gate'] == 'acquisition']
        self.assertEqual(closed[0]['actor'], 'operator')
        # No operator action reopens without the deterministic checks passing.
        self.assertFalse(any(tool['name'] in ('reopen', 'resume', 'unpause') for tool in operator.TOOLS))
        for name, arguments in (('verify', {'gate': 'safety'}),
                                ('verify', {'gate': 'safety', 'force': True}),
                                ('verify', {'gate': 'unknown'}),
                                ('reopen', {'gate': 'safety'})):
            if name == 'verify' and arguments == {'gate': 'safety'}:
                evidence = operator.dispatch(self.service, name, arguments)
                self.assertFalse(evidence['passed'])
                continue
            with self.assertRaises(Rejected):
                operator.dispatch(self.service, name, arguments)
        self.assertEqual(self.gates()['safety'], 0)
        self.assertEqual(self.gates()['acquisition'], 0)

    def test_reopened_attempt_cannot_inherit_earlier_delivery(self):
        first = paid(self.service)
        oid = first['order_id']
        self.service.submit(oid, first['access_token'], example_payload(), fake_execute, request_id='ok')
        crash_order = paid(self.service)
        self.interrupt_order(crash_order)
        self.service.pause('safety', 'system', 'worker_interrupted', {'order_id': crash_order['order_id']})
        evidence = self.service.verify_gate('safety', actor='operator')
        self.assertTrue(evidence['passed'], evidence)
        recovered = self.service.db.execute(
            "SELECT result FROM submissions WHERE key='crashed-attempt'").fetchone()
        # A delivered run from a different order/attempt is never inherited.
        self.assertNotIn('"delivered"', recovered['result'])

    def test_deleted_checkout_session_is_authoritative_absence(self):
        # A checkout session the provider no longer knows (hard 404) surfaces
        # as absence, not an exception: never-charged orders reconcile and
        # release instead of wedging every gate verification forever. A paid
        # order with no provider object stays unresolved (fail-closed).
        import urllib.error
        sandbox = StripeSandbox('sk_test_SYNTHETIC_NOT_A_CREDENTIAL', 'https://example.test')
        missing = urllib.error.HTTPError('https://api.stripe.com', 404, 'Not Found', {}, None)
        with patch.object(sandbox, 'request', side_effect=missing):
            self.assertIsNone(sandbox.session('cs_test_gone'))
        broken = urllib.error.HTTPError('https://api.stripe.com', 502, 'Bad gateway', {}, None)
        with patch.object(sandbox, 'request', side_effect=broken):
            with self.assertRaises(urllib.error.HTTPError):
                sandbox.session('cs_test_broken')

        unpaid_order = self.service.checkout('opaque-product', True,
                                             request_id='absence-wedge-regression-unpaid-0001')
        paid_order = paid(self.service)
        with self.service.lock, self.service.db:
            row = self.service.get(unpaid_order['order_id'])
            row['reservation_expires'] = self.now - 1
            self.service.save(row)
        with patch.object(type(self.service.provider), 'session', return_value=None), \
             patch.object(type(self.service.provider), 'find_checkout', return_value=None):
            released = self.service.reconcile_reservations()
            ledger = self.service.reconcile_ledger()
        self.assertTrue(released['passed'])
        self.assertEqual(ledger['unresolved'], [paid_order['order_id']])
        row = self.service.get(unpaid_order['order_id'])
        self.assertEqual((row['payment'], row['reservation']), ('expired', 'released'))

    def test_verify_on_healthy_gate_records_no_reopen(self):
        order = paid(self.service)
        evidence = self.service.verify_gate('delivery', actor='operator')
        self.assertTrue(evidence['passed'])
        self.assertEqual([r for r in self.audit() if r['action'] == 'reopened'], [])


if __name__ == '__main__':
    unittest.main()
