"""Local independent-review regressions; payment operations use only a double."""
import base64
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

import xreg
from . import operator
from .service import Service, Rejected
from . import test_pilot
from .test_pilot import paid, example_payload, fake_execute


class RemediationTests(unittest.TestCase):
    setUp = test_pilot.PilotTests.setUp
    tearDown = test_pilot.PilotTests.tearDown

    def restart(self):
        state = self.service.state
        self.service.close()
        self.service = Service(state, self.provider, 'synthetic-test-signing-value', lambda: self.now)

    def submit(self, order, executor=fake_execute):
        data = base64.b64decode(example_payload()['fixtures'][0]['old'])
        with patch('pilot.worker.public_hashes', return_value={xreg.sha(data)}):
            return self.service.submit(order['order_id'], order['access_token'],
                                       example_payload(), executor, request_id='retry-1')

    def test_f1_all_unsuccessful_refunds_remain_unresolved(self):
        order = paid(self.service)
        oid = order['order_id']
        self.service.request_refund(oid, order['access_token'])
        for state in ('pending', 'requires_action', 'failed', 'canceled'):
            with self.subTest(state=state):
                self.provider.refunds['re_'+oid]['status'] = state
                self.service.reconcile_refund(oid)
                self.restart()
                evidence = operator.dispatch(self.service, 'maintenance', {'code': 'reconcile_refunds'})
                self.assertEqual(evidence['checked'], 1)
                self.assertEqual(evidence['unresolved'], 1)
                self.assertEqual(self.service.operator_snapshot()['pending_refunds'], 1)
                task = self.service.db.execute("SELECT status FROM maintenance WHERE code='reconcile_refunds'").fetchone()[0]
                self.assertEqual(task, 'blocked')
        self.provider.refunds['re_'+oid]['status'] = 'succeeded'
        evidence = operator.dispatch(self.service, 'maintenance', {'code': 'reconcile_refunds'})
        self.assertEqual(evidence['unresolved'], 0)
        self.assertEqual(self.service.operator_snapshot()['pending_refunds'], 0)
        self.assertEqual(self.provider.refund_calls, 1)

    def test_f2_acceptance_exception_is_durable_failure(self):
        for error in (subprocess.TimeoutExpired('test', 180), OSError('launch failed')):
            with self.subTest(error=type(error).__name__):
                with patch('pilot.operator.subprocess.run', side_effect=error):
                    result = operator.dispatch(self.service, 'maintenance', {'code': 'verify_acceptance'})
                self.assertIs(result['passed'], False)
                self.restart()
                task = self.service.db.execute("SELECT status FROM maintenance WHERE code='verify_acceptance'").fetchone()[0]
                self.assertEqual(task, 'failed')
                with self.assertRaisesRegex(Rejected, 'new_purchases_paused'):
                    self.service.checkout('product', True)

    def test_f2_restart_fails_interrupted_task(self):
        with self.service.db:
            self.service.db.execute("INSERT INTO maintenance(code,status) VALUES ('verify_acceptance','running')")
        self.restart()
        self.assertEqual(self.service.db.execute('SELECT status FROM maintenance').fetchone()[0], 'failed')
        self.assertEqual(self.service.operator_snapshot()['gates']['acquisition'], 0)

    def test_f3_checkout_lost_provider_response_and_client_response(self):
        checkout = self.provider.checkout
        def lost(oid):
            checkout(oid)
            raise OSError('lost response')
        key = 'synthetic-checkout-retry-identity-0001'
        with patch.object(self.provider, 'checkout', side_effect=lost):
            with self.assertRaisesRegex(Rejected, 'checkout_unavailable'):
                self.service.checkout('product', True, request_id=key)
        self.restart()
        first = self.service.checkout('product', True, request_id=key)
        self.restart()
        self.assertEqual(self.service.checkout('product', True, request_id=key), first)
        self.assertEqual(self.service.operator_snapshot()['orders'], 1)
        self.assertEqual(len(self.provider.sessions), 1)
        self.assertEqual(self.service.status(first['order_id'], first['access_token'])['payment'], 'unpaid')
        with self.assertRaisesRegex(Rejected, 'idempotency_key_payload_conflict'):
            self.service.checkout('changed-product', True, request_id=key)

    def test_f4_status_response_failure_does_not_lose_replay(self):
        order = paid(self.service)
        with patch.object(self.service, 'status', side_effect=OSError('response lost')):
            try:
                self.submit(order)
            except OSError:
                pass
        self.restart()
        with patch('pilot.test_remediation.fake_execute', side_effect=AssertionError('duplicate execution')):
            result = self.submit(order, executor=lambda *a: self.fail('duplicate execution'))
        self.assertEqual(result['job'], 'delivered')
        self.assertEqual(result['runs'], 1)
        self.assertEqual(self.service.db.execute('SELECT count(*) FROM reports').fetchone()[0], 3)

    def test_f4_restart_repairs_interrupted_submission_as_failure(self):
        order = paid(self.service)
        def interrupted(*args):
            raise SystemExit('process interruption')
        with self.assertRaises(SystemExit):
            self.submit(order, executor=interrupted)
        self.restart()
        result = self.submit(order, executor=lambda *a: self.fail('duplicate execution'))
        self.assertEqual(result['job'], 'failed')
        self.assertEqual(result['runs'], 0)

    def test_f4_legacy_unlinked_retry_never_inherits_later_success(self):
        order = paid(self.service)
        self.submit(order)
        payload_hash = xreg.sha(json.dumps(example_payload(), sort_keys=True, separators=(',', ':')).encode())
        with self.service.db:
            self.service.db.execute('INSERT INTO submissions VALUES (?,?,?,NULL)',
                (order['order_id'], 'legacy-failed', payload_hash))
        self.restart()
        result = self.service.submit(order['order_id'], order['access_token'], example_payload(),
            executor=lambda *a: self.fail('duplicate execution'), request_id='legacy-failed')
        self.assertEqual(result['job'], 'failed')

    def test_job_unknown_exit_never_fulfils(self):
        order = paid(self.service)
        def crashed(*args):
            pack, metrics = fake_execute(*args)
            metrics['exit_code'] = -9
            return pack, metrics
        result = self.submit(order, executor=crashed)
        self.assertEqual(result['job'], 'failed')
        self.assertEqual(result['runs'], 0)

    def test_f4_report_commit_failure_is_explicit_and_atomic(self):
        order = paid(self.service)
        event = self.service.event
        def fail_commit(kind, *args, **kwargs):
            if kind == 'evidence_ready':
                raise OSError('injected persistence failure')
            return event(kind, *args, **kwargs)
        with patch.object(self.service, 'event', side_effect=fail_commit):
            result = self.submit(order)
        self.assertEqual(result['job'], 'failed')
        self.assertEqual(result['runs'], 0)
        self.assertEqual(self.service.db.execute('SELECT count(*) FROM reports').fetchone()[0], 0)
        self.assertEqual(self.submit(order, executor=lambda *a: self.fail('duplicate execution')), result)

    def test_f4_process_exit_at_commit_boundaries(self):
        # Each child owns its disposable service. os._exit bypasses finally and
        # connection close, exercising SQLite crash recovery, not exception mocks.
        script = '''
import os, sys
from unittest.mock import patch
from pilot.test_remediation import RemediationTests
t = RemediationTests(); t.temp = None
from pilot.service import Service
from pilot.payments import OfflineStripe
from pilot.test_pilot import fake_execute
t.service = Service(sys.argv[1], OfflineStripe(), 'synthetic-test-signing-value')
order = {'order_id': sys.argv[2], 'access_token': sys.argv[3]}
stage = sys.argv[4]
event = t.service.event
def terminate(kind, *args, **kwargs):
    event(kind, *args, **kwargs)
    if kind == stage: os._exit(91)
with patch.object(t.service, 'event', side_effect=terminate):
    def executor(*args):
        if stage == 'executor': os._exit(91)
        return fake_execute(*args)
    t.submit(order, executor=executor)
os._exit(91)
'''
        for stage in ('executor', 'evidence_ready', 'response'):
            with self.subTest(stage=stage):
                # Prior interrupted cases close safety; use a fresh state each time.
                state = self.service.state.parent / ('crash-'+stage)
                self.service.close()
                self.service = Service(state, self.provider, 'synthetic-test-signing-value', lambda: self.now)
                order = paid(self.service)
                self.service.close()
                proc = subprocess.run([sys.executable, '-B', '-c', script, str(state),
                    order['order_id'], order['access_token'], stage], capture_output=True, timeout=20)
                self.service = Service(state, self.provider, 'synthetic-test-signing-value', lambda: self.now)
                self.assertEqual(proc.returncode, 91, proc.stderr.decode())
                result = self.submit(order, executor=lambda *a: self.fail('duplicate execution'))
                delivered = stage == 'response'
                self.assertEqual(result['job'], 'delivered' if delivered else 'failed')
                self.assertEqual(result['runs'], int(delivered))
                self.assertEqual(self.service.db.execute('SELECT count(*) FROM reports').fetchone()[0], 3 if delivered else 0)

    def test_f6_spaced_unsupported_declaration(self):
        for space in (' ', '\t', '\r\n'):
            for quote in ('"', "'"):
                data = (f'<?xml version="1.0" encoding{space}={space}{quote}iso-8859-1{quote}?><R>é</R>').encode()
                with self.subTest(space=repr(space), quote=quote):
                    with self.assertRaisesRegex(xreg.InputError, 'unsupported_xml_encoding'):
                        xreg.safe_xml(data)

    def test_f6_encoding_consistency(self):
        for bom in (b'', b'\xef\xbb\xbf'):
            for encoding in ('UTF-8', 'utf-8'):
                data = bom + (f'<?xml version="1.0" encoding = "{encoding}"?><R>é</R>').encode()
                self.assertEqual(xreg.safe_xml(data).text, 'é')
        for data in (b'<?xml version="1.0" encoding = "ascii"?><R>\xc3\xa9</R>',
                     b'\xef\xbb\xbf<?xml version="1.0" encoding = "iso-8859-1"?><R>\xc3\xa9</R>',
                     b'<R>\xc3</R>', '<R>é</R>'.encode('utf-16'), '<R/>'.encode('utf-32'),
                     b'<?xml version="1.0" encoding = utf-8?><R/>'):
            with self.subTest(data_hash=xreg.sha(data)), self.assertRaises(xreg.InputError):
                xreg.safe_xml(data)

    def test_f6_worker_rejects_encoding_before_java_and_cleans(self):
        from . import worker
        data = b'<?xml version="1.0" encoding = "iso-8859-1"?><R>\xc3\xa9</R>'
        with patch('subprocess.run', side_effect=AssertionError('unsupported bytes reached Java')) as runner:
            pack, metrics = worker.execute([{'id': 'encoding', 'old': data}], self.service.policy,
                self.service.state/'temporary', runner=runner)
        runner.assert_not_called()
        self.assertEqual(metrics['exit_code'], 2)
        self.assertTrue(metrics['temporary_inputs_and_intermediates_removed'])
        self.assertIn(b'unsupported_xml_encoding', pack['results.json'])
        self.assertIn(b'<error', pack['junit.xml'])

    def test_f2_process_exit_during_task_closes_admission(self):
        state = self.service.state
        self.service.close()
        script = '''
import os, sys
from unittest.mock import patch
from pilot.service import Service
from pilot.payments import OfflineStripe
from pilot.operator import dispatch
s = Service(sys.argv[1], OfflineStripe(), 'synthetic-test-signing-value')
with patch('pilot.operator.subprocess.run', side_effect=lambda *a, **k: os._exit(92)):
    dispatch(s, 'maintenance', {'code': 'verify_acceptance'})
'''
        proc = subprocess.run([sys.executable, '-B', '-c', script, str(state)], capture_output=True, timeout=20)
        self.service = Service(state, self.provider, 'synthetic-test-signing-value', lambda: self.now)
        self.assertEqual(proc.returncode, 92, proc.stderr.decode())
        self.assertEqual(self.service.db.execute('SELECT status FROM maintenance').fetchone()[0], 'failed')
        self.assertEqual(self.service.operator_snapshot()['gates']['acquisition'], 0)


if __name__ == '__main__':
    unittest.main()
