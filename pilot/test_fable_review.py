"""Regression tests for defects reproduced in FABLE_ADVERSARIAL_REVIEW.md."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from . import railway_app
from .payments import PaymentError
from . import test_pilot
from .test_pilot import fake_execute, paid


def incomplete_execute(decoded, policy, directory):
    pack, metrics = fake_execute(decoded, policy, directory)
    return dict(pack, **{'results.json': b'{"marker":"failed-attempt"}'}), dict(metrics, exit_code=2)


class ReportSelectionTests(unittest.TestCase):
    setUp, tearDown, submit = (
        test_pilot.PilotTests.setUp, test_pilot.PilotTests.tearDown, test_pilot.PilotTests.submit)

    def test_failed_attempt_pack_never_masks_delivered_report(self):
        order = paid(self.service)
        self.assertEqual(self.submit(order)['job'], 'delivered')
        delivered = self.service.report(order['order_id'], order['access_token'], 'results.json')
        self.now += 60
        self.assertEqual(self.submit(order, executor=incomplete_execute)['job'], 'failed')
        self.assertEqual(self.service.report(order['order_id'], order['access_token'], 'results.json'), delivered)
        # The failed attempt's evidence stays addressable by its explicit run.
        self.assertIn(b'failed-attempt', self.service.report(
            order['order_id'], order['access_token'], 'results.json', run=-2))

    def test_failed_attempt_pack_served_when_nothing_delivered(self):
        order = paid(self.service)
        self.submit(order, executor=incomplete_execute)
        self.assertIn(b'failed-attempt', self.service.report(
            order['order_id'], order['access_token'], 'results.json'))


class DeployProviderTests(unittest.TestCase):
    def test_deploy_never_silently_falls_back_to_simulated_payments(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(os.environ, {'STRIPE_TEST_KEY': '', 'XREG_ALLOW_SIMULATED_PAYMENTS': ''}):
                with self.assertRaises(PaymentError):
                    railway_app.build_provider(Path(folder))
            with patch.dict(os.environ, {'STRIPE_TEST_KEY': '', 'XREG_ALLOW_SIMULATED_PAYMENTS': '1'}):
                provider = railway_app.build_provider(Path(folder))
                self.assertTrue(provider.simulated)
                provider.close()


if __name__ == '__main__':
    unittest.main()
