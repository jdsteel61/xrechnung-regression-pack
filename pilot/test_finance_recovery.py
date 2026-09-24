"""Provider ledger and restart recovery with synthetic, cost-free objects."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from . import worker
from .payments import DurableOfflineStripe
from .service import Rejected, Service
from .test_pilot import signed


class FinanceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='finance-recovery-')
        self.root = Path(self.temp.name)
        self.provider = DurableOfflineStripe(self.root/'provider.sqlite3')
        self.now = 1800000000.0
        self.service = Service(self.root/'state', self.provider, 'synthetic', lambda: self.now)

    def tearDown(self):
        self.service.close()
        self.provider.close()
        self.temp.cleanup()

    def restart(self):
        self.service.close()
        self.provider.close()
        self.provider = DurableOfflineStripe(self.root/'provider.sqlite3')
        self.service = Service(self.root/'state', self.provider, 'synthetic', lambda: self.now)

    def paid(self):
        checkout = self.service.checkout('opaque-product', True)
        self.provider.set_payment_status(checkout['session_id'], 'paid')
        self.service.webhook(*signed(self.service, 'checkout.session.completed',
                                    {'id': checkout['session_id']}, 'evt_'+checkout['order_id']))
        return checkout

    def test_crash_running_submission_replays_failure_without_consuming_rerun(self):
        order = self.paid()
        oid = order['order_id']
        with self.service.db:
            row = self.service.get(oid)
            row.update(job='running', job_deadline=self.now+10, attempts=1)
            self.service.save(row)
            self.service.db.execute('INSERT INTO submissions VALUES (?,?,?,NULL)',
                                    (oid, 'crashed', 'synthetic-hash'))
        self.restart()
        row = self.service.get(oid)
        self.assertEqual((row['job'], row['runs']), ('failed', 0))
        self.assertNotIn('job_deadline', row)
        replay = json.loads(self.service.db.execute(
            'SELECT result FROM submissions WHERE order_id=? AND key=?',
            (oid, 'crashed')).fetchone()[0])
        self.assertEqual(replay['job'], 'failed')
        self.assertEqual(replay['runs'], 0)
        self.assertEqual(row['reservation'], 'held')

    def test_pending_key_before_running_transition_recovers_without_run(self):
        order = self.paid()
        oid = order['order_id']
        with self.service.db:
            row = self.service.get(oid)
            row['attempts'] = 1
            self.service.save(row)
            self.service.db.execute('INSERT INTO submissions VALUES (?,?,?,NULL)',
                                    (oid, 'before-running', 'synthetic-hash'))
        result = self.service.reconcile_incomplete_submissions()
        self.assertEqual(result, {'recovered': 1, 'busy': False})
        replay = json.loads(self.service.db.execute(
            'SELECT result FROM submissions WHERE order_id=? AND key=?',
            (oid, 'before-running')).fetchone()[0])
        self.assertEqual((replay['job'], replay['runs']), ('failed', 0))
        self.assertEqual(self.service.get(oid)['job'], 'awaiting_inputs')
        self.assertEqual(self.service.reconcile_incomplete_submissions()['recovered'], 0)

    def test_pending_rerun_key_never_inherits_prior_delivery(self):
        order = self.paid()
        oid = order['order_id']
        with self.service.db:
            row = self.service.get(oid)
            row.update(job='delivered', runs=1, first_delivery=self.now, attempts=2)
            self.service.save(row)
            self.service.db.execute('INSERT INTO submissions VALUES (?,?,?,NULL)',
                                    (oid, 'interrupted-rerun', 'synthetic-hash'))
        self.assertEqual(self.service.reconcile_incomplete_submissions()['recovered'], 1)
        replay = json.loads(self.service.db.execute(
            'SELECT result FROM submissions WHERE order_id=? AND key=?',
            (oid, 'interrupted-rerun')).fetchone()[0])
        self.assertEqual((replay['job'], replay['runs']), ('failed', 1))
        self.assertEqual(self.service.get(oid)['job'], 'delivered')

    def test_ledger_recovers_payment_after_response_loss(self):
        checkout = self.service.checkout('opaque', True)
        self.provider.set_payment_status(checkout['session_id'], 'paid')
        self.restart()
        result = self.service.reconcile_ledger()
        self.assertEqual((result['passed'], result['repaired']), (True, 1))
        self.assertEqual(self.service.get(checkout['order_id'])['job'], 'awaiting_inputs')
        self.assertEqual(self.service.reconcile_ledger()['repaired'], 0)

    def test_ledger_reconciles_refund_after_provider_commit_and_restart(self):
        checkout = self.paid()
        oid = checkout['order_id']
        with self.service.db:
            row = self.service.get(oid)
            row['refund'] = 'requested'
            self.service.save(row)
        refund = self.provider.refund('pi_'+oid, oid)
        self.restart()
        # Lost provider response: the same idempotent refund intent is replayed.
        self.assertEqual(self.service.reconcile_refund(oid)['refund'], 'pending')
        self.provider.refund_status(refund['id'])
        result = self.service.reconcile_ledger()
        self.assertTrue(result['passed'])
        self.assertEqual(self.service.get(oid)['refund'], 'succeeded')
        self.service.reconcile_reservations()
        self.assertEqual(self.service.get(oid)['reservation'], 'released')

    def test_provider_uncertainty_keeps_reservation_and_reports_unresolved(self):
        checkout = self.paid()
        with patch.object(self.provider, 'session', side_effect=OSError('provider down')):
            result = self.service.reconcile_ledger()
        self.assertFalse(result['passed'])
        self.assertEqual(result['unresolved'], [checkout['order_id']])
        row = self.service.get(checkout['order_id'])
        self.assertEqual(row['reservation'], 'reconciliation_failed')
        self.assertEqual(row['next_action'], 'reconcile_reservations')
        self.assertEqual(self.service.reconcile_ledger()['unresolved'], [])

    def test_ledger_recovers_late_payment_as_refund_obligation(self):
        checkout = self.service.checkout('opaque', True)
        oid = checkout['order_id']
        with self.service.db:
            row = self.service.get(oid)
            row.update(payment='expired', job='expired', reservation='released')
            self.service.save(row)
        self.provider.set_payment_status(checkout['session_id'], 'paid')
        result = self.service.reconcile_ledger()
        self.assertTrue(result['passed'])
        row = self.service.get(oid)
        self.assertEqual((row['refund'], row['job'], row['reservation']),
                         ('requested', 'refund_pending', 'held'))
        self.service.reconcile_reservations()
        self.assertEqual(self.service.get(oid)['refund'], 'pending')

    def test_ledger_detects_contradictory_payment_without_releasing_capacity(self):
        checkout = self.paid()
        original = self.provider.session
        def contradictory(sid):
            session = original(sid)
            session['payment_status'] = 'unpaid'
            return session
        with patch.object(self.provider, 'session', side_effect=contradictory):
            result = self.service.reconcile_ledger()
        self.assertEqual(result['unresolved'], [checkout['order_id']])
        self.assertEqual(self.service.get(checkout['order_id'])['reservation'],
                         'reconciliation_failed')

    def test_refund_during_decode_cannot_be_overwritten_by_submission(self):
        checkout = self.paid()
        oid = checkout['order_id']
        def decode(_payload, _policy):
            self.service.request_refund(oid, checkout['access_token'])
            return [{'id': 'f001', 'old': b'<Invoice/>'}]
        def must_not_execute(*_args):
            self.fail('execution started after refund')
        with patch.object(worker, 'decode_submission', side_effect=decode):
            with self.assertRaisesRegex(Rejected, 'verified_payment_required_or_refund_active'):
                self.service.submit(oid, checkout['access_token'], {'pair': worker.PAIR},
                                    executor=must_not_execute, request_id='refund-race')
        row = self.service.get(oid)
        self.assertEqual(row['refund'], 'pending')
        self.assertEqual(row['runs'], 0)
        self.assertIsNotNone(row['refund_id'])


if __name__ == '__main__':
    unittest.main()
