"""Buyer declaration and refund eligibility across the public order boundary."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from .payments import OfflineStripe
from .server import make_server
from .service import Rejected, Service
from .test_pilot import signed


class CheckoutRefundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='checkout-refunds-')
        self.now = 1800000000.0
        self.provider = OfflineStripe()
        self.service = Service(Path(self.temp.name) / 'state', self.provider,
                               'synthetic-test-signing-value', lambda: self.now)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def paid(self, buyer_type='business'):
        order = self.service.checkout('opaque-product', True, buyer_type=buyer_type,
            terms_accepted=True, immediate_performance_requested=buyer_type == 'consumer',
            cancellation_acknowledged=buyer_type == 'consumer')
        self.provider.sessions[order['session_id']]['payment_status'] = 'paid'
        self.service.webhook(*signed(self.service, 'checkout.session.completed',
                                    {'id': order['session_id']}, 'evt_' + order['order_id']))
        return order

    def test_consumer_window_and_business_boundary(self):
        consumer = self.paid('consumer')
        business = self.paid()
        stored = self.service.get(consumer['order_id'])
        self.assertTrue(stored['immediate_performance_requested'])
        self.assertTrue(stored['cancellation_acknowledged'])
        self.now += 7 * 86400 + 1
        with self.assertRaisesRegex(Rejected, 'refund_window_closed'):
            self.service.request_refund(business['order_id'], business['access_token'])
        self.assertEqual(self.service.request_refund(consumer['order_id'],
                         consumer['access_token'])['refund'], 'pending')
        later = self.paid('consumer')
        self.now += 14 * 86400
        self.assertEqual(self.service.request_refund(later['order_id'],
                         later['access_token'])['refund'], 'pending')
        too_late = self.paid('consumer')
        self.now += 14 * 86400 + 1
        with self.assertRaisesRegex(Rejected, 'refund_window_closed'):
            self.service.request_refund(too_late['order_id'], too_late['access_token'])

    def test_running_request_is_durable_until_job_stops(self):
        order = self.paid()
        oid = order['order_id']
        with self.service.lock, self.service.db:
            row = self.service.get(oid)
            row['job'] = 'running'
            self.service.save(row)
        self.assertEqual(self.service.request_refund(oid, order['access_token']),
                         {'refund': 'requested'})
        self.assertEqual(self.provider.refund_calls, 0)
        with self.service.lock, self.service.db:
            row = self.service.get(oid)
            row['job'] = 'failed'
            self.service.save(row)
        self.assertEqual(self.service.reconcile_refund(oid)['refund'], 'pending')
        self.assertEqual(self.provider.refund_calls, 1)

    def test_failed_rerun_can_be_refunded(self):
        order = self.paid()
        oid = order['order_id']
        with self.service.lock, self.service.db:
            row = self.service.get(oid)
            row.update(runs=1, first_delivery=self.now, job='failed')
            self.service.save(row)
        self.assertEqual(self.service.operator_request_refund(oid, 'pack_not_deliverable')['refund'], 'pending')

    def test_public_checkout_requires_declaration_and_acknowledgement(self):
        server = make_server(self.service)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            def checkout(body, key):
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
                try:
                    conn.request('POST', '/checkout', json.dumps(body),
                                 {'Content-Type': 'application/json', 'Idempotency-Key': key})
                    response = conn.getresponse()
                    return response.status, json.loads(response.read())
                finally:
                    conn.close()
            base = {'product': 'opaque-product', 'attested': True}
            self.assertNotEqual(checkout(base, 'checkout-key-00000000000000000001')[0], 201)
            consumer = dict(base, buyer_type='consumer', terms_accepted=True)
            self.assertNotEqual(checkout(consumer, 'checkout-key-00000000000000000002')[0], 201)
            consumer.update(immediate_performance_requested=True, cancellation_acknowledged=True)
            status, response = checkout(consumer, 'checkout-key-00000000000000000003')
            self.assertEqual(status, 201, response)
            self.assertEqual(self.service.get(response['order_id'])['buyer_type'], 'consumer')
        finally:
            server.shutdown()
            thread.join(timeout=10)
            server.server_close()


if __name__ == '__main__':
    unittest.main()
