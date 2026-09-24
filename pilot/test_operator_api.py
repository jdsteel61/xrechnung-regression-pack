"""Operator capability boundaries over synthetic, offline payments."""
import json
from pathlib import Path
import tempfile
import unittest

from . import operator
from .payments import OfflineStripe
from .service import Rejected, Service
from .test_pilot import paid


class OperatorAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='operator-api-')
        self.provider = OfflineStripe()
        self.service = Service(Path(self.temp.name)/'state', self.provider, 'synthetic', lambda: 1800000000.0)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_read_actions_are_aggregate_and_ruleset_is_immutable(self):
        order = paid(self.service)
        for name in ('health', 'spend', 'ruleset'):
            response = operator.dispatch(self.service, name, {})
            encoded = json.dumps(response)
            self.assertNotIn(order['order_id'], encoded)
            self.assertNotIn('opaque-product', encoded)
        self.assertEqual(operator.dispatch(self.service, 'spend', {})['additional_cash_budget_gbp'], 0)
        self.assertFalse(operator.dispatch(self.service, 'ruleset', {})['changes_allowed'])
        for name, args in (('spend', {'limit': 999}), ('ruleset', {'pair': 'other'}),
                           ('retry', {'code': 'shell'}), ('pause', {'gate': 'unknown'}),
                           ('refund', {'order_id': '../state'})):
            with self.assertRaises(Rejected):
                operator.dispatch(self.service, name, args)

    def test_bounded_recovery_pause_and_refund(self):
        order = paid(self.service)
        self.assertTrue(operator.dispatch(self.service, 'retry', {'code': 'ledger'})['passed'])
        self.assertTrue(operator.dispatch(self.service, 'retry', {'code': 'reservations'})['passed'])
        self.assertEqual(operator.dispatch(self.service, 'refund', {'order_id': order['order_id'], 'reason': 'customer_request_within_7_days'})['refund'], 'pending')
        self.assertEqual(operator.dispatch(self.service, 'refund', {'order_id': order['order_id'], 'reason': 'customer_request_within_7_days'})['refund'], 'pending')
        self.assertEqual(len(self.provider.refunds), 1)
        self.assertEqual(operator.dispatch(self.service, 'pause', {'gate': 'acquisition'}), {'paused': 'acquisition'})
        with self.assertRaises(Rejected):
            self.service.checkout('another', True)

    def test_refund_requires_verified_payment_and_queues_during_job(self):
        order = self.service.checkout('opaque', True)
        with self.assertRaisesRegex(Rejected, 'no_verified_payment'):
            operator.dispatch(self.service, 'refund', {'order_id': order['order_id'], 'reason': 'customer_request_within_7_days'})
        self.provider.sessions[order['session_id']]['payment_status'] = 'paid'
        from .test_pilot import signed
        self.service.webhook(*signed(self.service, 'checkout.session.completed',
                                    {'id': order['session_id']}, 'evt_'+order['order_id']))
        with self.service.db:
            row = self.service.get(order['order_id'])
            row['job'] = 'running'
            self.service.save(row)
        self.assertEqual(operator.dispatch(self.service, 'refund',
            {'order_id': order['order_id'], 'reason': 'customer_request_within_7_days'}),
            {'refund': 'requested'})
        self.assertEqual(self.provider.refund_calls, 0)
