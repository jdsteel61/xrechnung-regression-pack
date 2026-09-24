"""Remaining-blocker regressions. Synthetic local state, never real payments."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import xreg
from . import payments
from .service import Service, Rejected
from .test_pilot import paid, signed, example_payload, fake_execute


class RemainingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='remediation2-')
        self.state = Path(self.temp.name)/'state'
        self.now = 1800000000.0
        self.provider = payments.OfflineStripe()
        self.service = Service(self.state, self.provider, 'synthetic', lambda: self.now)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def restart(self):
        self.service.close()
        self.service = Service(self.state, self.provider, 'synthetic', lambda: self.now)

    def test_original_f5_orphan_and_wal_bytes(self):
        scratch = self.state/'temporary'/'synthetic-orphan'
        scratch.mkdir()
        (scratch/'input.xml').write_bytes(b'SYNTHETIC-RAW-INPUT')
        sentinel = b'REMEDIATION2-EXPIRED-REPORT-SENTINEL'
        with self.service.db:
            self.service.db.execute('INSERT INTO reports VALUES (?,?,?,?,?)', ('synthetic', 1, 'report.html', sentinel, 0))
        with self.service.db:
            self.service.purge_reports()
        retained = [p.name for p in self.state.glob('business.sqlite3*') if sentinel in p.read_bytes()]
        self.restart()
        self.assertFalse(scratch.exists(), 'F5: orphan survives restart')
        self.assertEqual(retained, [], 'F5: logically deleted report remains in database/journal')

    def test_exclusive_process_owner_before_janitor(self):
        with self.assertRaisesRegex(Rejected, 'state_already_owned'):
            Service(self.state, self.provider, 'synthetic')

    def test_original_f3_abandoned_unpaid_releases_capacity(self):
        order = self.service.checkout('opaque', True, request_id='a'*32)
        self.now += 86401
        result = self.service.reconcile_reservations()
        row = self.service.get(order['order_id'])
        self.assertEqual(row['reservation'], 'released')
        self.assertEqual(row['payment'], 'expired')
        self.assertEqual(self.service.operator_snapshot()['simulated_reserved_pence'], 0)
        self.assertEqual(self.service.reconcile_reservations(), result)
        with self.assertRaisesRegex(Rejected, 'checkout_expired'):
            self.service.checkout('opaque', True, request_id='a'*32)

    def test_lost_response_abandonment_reconciles_without_retry_key(self):
        original = self.provider.checkout
        def lost(oid):
            original(oid)
            raise OSError('response lost')
        with patch.object(self.provider, 'checkout', side_effect=lost):
            with self.assertRaises(Rejected):
                self.service.checkout('opaque', True)
        self.now += 86401
        self.restart()
        self.service.reconcile_reservations()
        row = self.service.get(self.service.db.execute('SELECT id FROM orders').fetchone()[0])
        self.assertEqual(row['reservation'], 'released')
        self.assertEqual(len(self.provider.sessions), 1)

    def test_provider_failure_has_bounded_machine_action_and_recovers(self):
        order = self.service.checkout('opaque', True)
        self.now += 86401
        with patch.object(self.provider, 'session', side_effect=OSError('private provider detail')):
            self.service.reconcile_reservations()
        row = self.service.get(order['order_id'])
        self.assertEqual(row['reservation'], 'reconciliation_failed')
        self.assertEqual(row['next_action'], 'reconcile_reservations')
        self.assertNotIn('private provider detail', json.dumps(self.service.operating_state()))
        self.now += 301
        self.restart()
        self.service.reconcile_reservations()
        self.assertEqual(self.service.get(order['order_id'])['reservation'], 'released')

    def test_paid_abandonment_refunds_and_duplicate_late_payment(self):
        order = paid(self.service)
        self.now += 8*86400
        self.service.reconcile_reservations()
        self.assertEqual(self.service.get(order['order_id'])['refund'], 'pending')
        self.provider.refunds['re_'+order['order_id']]['status'] = 'succeeded'
        self.now += 301
        self.service.reconcile_reservations()
        event = signed(self.service, 'checkout.session.completed', {'id': order['session_id']}, 'evt_late')
        self.service.webhook(*event)
        self.service.webhook(*event)
        self.assertEqual(self.service.get(order['order_id'])['reservation'], 'released')
        self.assertEqual(self.provider.refund_calls, 1)

    def test_cross_tenant_all_customer_actions_and_agent_redaction(self):
        a, b = paid(self.service), paid(self.service)
        for action in (lambda: self.service.status(b['order_id'], a['access_token']),
                       lambda: self.service.report(b['order_id'], a['access_token'], 'report.html'),
                       lambda: self.service.request_refund(b['order_id'], a['access_token']),
                       lambda: self.service.recover(b['order_id'], a['recovery_token']),
                       lambda: self.service.submit(b['order_id'], a['access_token'], example_payload())):
            with self.assertRaisesRegex(Rejected, 'access_denied'):
                action()
        context = json.dumps(self.service.operating_state())
        for forbidden in (a['access_token'], a['recovery_token'], 'Invoice', 'product_hash', 'content'):
            self.assertNotIn(forbidden, context)

    def test_crash_cleanup_actual_process_and_replay(self):
        order = paid(self.service)
        self.service.close()
        script = '''
import os, sys
from pathlib import Path
from pilot.service import Service
from pilot.payments import OfflineStripe
s = Service(sys.argv[1], OfflineStripe(), 'synthetic')
with s.db:
 o=s.get(sys.argv[2]); o['job']='running'; s.save(o)
p=s.state/'temporary'/'pilot-job-crash'; p.mkdir(); (p/'raw.xml').write_bytes(b'SYNTHETIC')
os._exit(93)
'''
        proc = subprocess.run([sys.executable, '-B', '-c', script, str(self.state), order['order_id']], capture_output=True, timeout=20)
        self.assertEqual(proc.returncode, 93, proc.stderr.decode())
        self.service = Service(self.state, self.provider, 'synthetic', lambda: self.now)
        self.assertEqual(list((self.state/'temporary').iterdir()), [])
        self.assertEqual(self.service.status(order['order_id'], order['access_token'])['job'], 'failed')

    def test_signed_payment_recovers_lost_binding_without_checkout_retry(self):
        original = self.provider.checkout
        def lost(oid):
            original(oid)
            raise OSError('response loss')
        with patch.object(self.provider, 'checkout', side_effect=lost), self.assertRaises(Rejected):
            self.service.checkout('opaque', True)
        sid = next(iter(self.provider.sessions))
        self.provider.set_payment_status(sid, 'paid')
        self.restart()
        event = signed(self.service, 'checkout.session.completed', {'id': sid})
        self.service.webhook(*event)
        self.assertEqual(self.service.webhook(*event), {'duplicate': True})
        self.assertEqual(self.service.get(sid.removeprefix('cs_test_'))['payment'], 'paid')

    def test_completed_rerun_capacity_retires_without_resetting_spend(self):
        order = paid(self.service)
        data = base64.b64decode(example_payload()['fixtures'][0]['old'])
        with patch('pilot.worker.public_hashes', return_value={xreg.sha(data)}):
            for key in ('one', 'two'):
                self.service.submit(order['order_id'], order['access_token'], example_payload(), fake_execute, key)
        self.service.reconcile_reservations()
        self.assertEqual(self.service.get(order['order_id'])['reservation'], 'spent')
        self.assertEqual(self.service.operator_snapshot()['simulated_reserved_pence'], 0)
        self.service.policy['simulated_compute_budget_pence'] = 200
        with self.assertRaisesRegex(Rejected, 'spending_gate'):
            self.service.checkout('next', True)

    def test_exhausted_invalid_inputs_refund_automatically(self):
        order = paid(self.service)
        for unused in range(self.service.policy['max_submission_attempts']):
            with self.assertRaises(Rejected):
                self.service.submit(order['order_id'], order['access_token'], {})
        self.service.reconcile_reservations()
        self.assertEqual(self.service.get(order['order_id'])['refund'], 'pending')
        self.assertEqual(list((self.state/'temporary').iterdir()), [])

    def test_validation_failure_retry_does_not_consume_attempt_again(self):
        order = paid(self.service)
        for unused in range(2):
            with self.assertRaises(Rejected):
                self.service.submit(order['order_id'], order['access_token'], {}, request_id='invalid-retry')
            self.restart()
        self.assertEqual(self.service.get(order['order_id'])['attempts'], 1)

    def test_refund_after_capacity_retired_still_reconciles(self):
        order = paid(self.service)
        with self.service.db:
            row = self.service.get(order['order_id'])
            row.update(reservation='spent', runs=2, attempts=2, first_delivery=self.now)
            self.service.save(row)
        self.service.request_refund(order['order_id'], order['access_token'])
        self.provider.refunds['re_'+order['order_id']]['status'] = 'succeeded'
        self.service.reconcile_reservations()
        self.assertEqual(self.service.get(order['order_id'])['refund'], 'succeeded')

    def test_timer_recovery_and_authorized_bounded_http(self):
        import threading
        import time
        import urllib.request
        import urllib.error
        from .server import make_server
        order = self.service.checkout('opaque', True)
        token = 'synthetic-operator-capability-0123456789abcdef'
        server = make_server(self.service, operator_token=token)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        origin = f'http://127.0.0.1:{server.server_port}'
        def request(route, capability, body=None):
            req = urllib.request.Request(origin+route, data=json.dumps(body).encode() if body is not None else None,
                headers={'Authorization': 'Bearer '+capability, 'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=3) as response:
                return json.load(response)
        try:
            with self.assertRaises(urllib.error.HTTPError) as denied:
                request('/operator/state', order['access_token'])
            self.assertEqual(denied.exception.code, 409)
            self.assertEqual(request('/operator/state', token)['orders'][0]['id'], order['order_id'])
            with self.assertRaises(urllib.error.HTTPError):
                request('/operator/action', token, {'name': 'shell', 'arguments': {}})
            result = request('/operator/action', token, {'name': 'maintenance', 'arguments': {'code': 'reconcile_reservations'}})
            self.assertTrue(result['passed'])
            self.now += 86401
            deadline = time.monotonic()+4
            while self.service.get(order['order_id'])['reservation'] != 'released' and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertEqual(self.service.get(order['order_id'])['reservation'], 'released')
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


class DurableProviderTests(unittest.TestCase):
    def test_original_f7_provider_restart_and_replay(self):
        with tempfile.TemporaryDirectory(prefix='provider-restart-') as folder:
            path = Path(folder)/'provider.sqlite3'
            provider = payments.DurableOfflineStripe(path)
            session = provider.checkout('synthetic-order')
            provider.set_payment_status(session['id'], 'paid')
            refund = provider.refund(session['payment_intent'], 'synthetic-order')
            provider.close()
            provider = payments.DurableOfflineStripe(path)
            try:
                self.assertEqual(provider.checkout('synthetic-order')['id'], session['id'])
                self.assertEqual(provider.session(session['id'])['payment_status'], 'paid')
                self.assertEqual(provider.refund(session['payment_intent'], 'synthetic-order'), refund)
                self.assertEqual(provider.refund_status(refund['id'])['id'], refund['id'])
            finally:
                provider.close()

    def test_process_crash_after_provider_commit_before_business_binding(self):
        with tempfile.TemporaryDirectory(prefix='provider-commit-crash-') as folder:
            state = Path(folder)
            script = '''
import os, sys
from pathlib import Path
from pilot.payments import DurableOfflineStripe
from pilot.service import Service
p=DurableOfflineStripe(Path(sys.argv[1])/'provider.sqlite3')
s=Service(Path(sys.argv[1])/'business',p,'synthetic')
original=p.checkout
def crash(oid):
 original(oid); os._exit(94)
p.checkout=crash
s.checkout('opaque',True,request_id='a'*32)
'''
            proc = subprocess.run([sys.executable, '-B', '-c', script, str(state)], capture_output=True, timeout=20)
            self.assertEqual(proc.returncode, 94, proc.stderr.decode())
            p = payments.DurableOfflineStripe(state/'provider.sqlite3')
            s = Service(state/'business', p, 'synthetic')
            try:
                order = s.checkout('opaque', True, request_id='a'*32)
                self.assertEqual(s.checkout('opaque', True, request_id='a'*32), order)
                self.assertEqual(p.db.execute('SELECT count(*) FROM objects').fetchone()[0], 1)
                p.set_payment_status(order['session_id'], 'paid')
                s.webhook(*signed(s, 'checkout.session.completed', {'id': order['session_id']}))
                s.request_refund(order['order_id'], order['access_token'])
            finally:
                s.close(); p.close()
            p = payments.DurableOfflineStripe(state/'provider.sqlite3')
            s = Service(state/'business', p, 'synthetic')
            try:
                s.reconcile_reservations()
                self.assertEqual(s.get(order['order_id'])['refund'], 'succeeded')
                self.assertEqual(s.get(order['order_id'])['reservation'], 'released')
            finally:
                s.close(); p.close()


@unittest.skipUnless(os.name == 'nt', 'Pinned runtime is Windows')
class ProcessContainmentTests(unittest.TestCase):
    def test_process_ownership_evidence(self):
        from .worker import contained_run
        with tempfile.TemporaryDirectory(prefix='process-evidence-') as folder:
            path = Path(folder)
            contained_run([sys.executable, '-c', 'pass'], cwd=path, scratch=path, timeout=5)
            self.assertTrue((path/'process.json').exists())

    def test_actual_java_parent_kill_then_restart_cleanup(self):
        import time
        import win32api
        import win32event
        with tempfile.TemporaryDirectory(prefix='java-crash-restart-') as folder:
            state = Path(folder)/'state'
            script = '''
import base64,sys
from pathlib import Path
from unittest.mock import patch
import xreg
from pilot.service import Service
from pilot.payments import OfflineStripe
from pilot.test_pilot import paid, example_payload
s=Service(Path(sys.argv[1]),OfflineStripe(),'synthetic')
o=paid(s)
(s.state/'order-id').write_text(o['order_id'])
data=base64.b64decode(example_payload()['fixtures'][0]['old'])
with patch('pilot.worker.public_hashes',return_value={xreg.sha(data)}):
 s.submit(o['order_id'],o['access_token'],example_payload(),request_id='crash')
'''
            process = subprocess.Popen([sys.executable, '-B', '-c', script, str(state)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            handle = None
            try:
                deadline = time.monotonic()+15
                marker = None
                while time.monotonic() < deadline and process.poll() is None:
                    candidates = list(state.glob('temporary/pilot-job-supervised-*/scratch/pilot-job-*/process.json'))
                    if candidates:
                        try:
                            marker = json.loads(candidates[0].read_text())
                            break
                        except (ValueError, OSError):
                            pass
                    time.sleep(.02)
                self.assertIsNotNone(marker, 'actual Java process never registered')
                self.assertEqual(marker['executable'], 'java.exe')
                handle = win32api.OpenProcess(0x100000, False, marker['pid'])
                process.kill()
                process.wait(timeout=5)
                self.assertEqual(win32event.WaitForSingleObject(handle, 10000), 0)
                s = Service(state, payments.OfflineStripe(), 'synthetic')
                try:
                    self.assertEqual(list((state/'temporary').iterdir()), [])
                    self.assertEqual(s.get((state/'order-id').read_text())['job'], 'failed')
                finally:
                    s.close()
            finally:
                if handle:
                    handle.Close()
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)

    def test_whole_job_deadline_cleans_raw_inputs(self):
        from .worker import execute_job
        with tempfile.TemporaryDirectory(prefix='whole-job-timeout-') as folder:
            policy = json.loads(Path('pilot/policy.json').read_text())
            policy['job_timeout_seconds'] = .001
            with self.assertRaises(subprocess.TimeoutExpired):
                execute_job([{'id': 'synthetic', 'old': b'<synthetic/>'}], policy, Path(folder))
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_timeout_terminates_descendant_before_cleanup(self):
        from .worker import contained_run
        with tempfile.TemporaryDirectory(prefix='tree-timeout-') as folder:
            path = Path(folder)
            script = "import subprocess,sys,time; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); Path('child.pid').write_text(str(p.pid)); time.sleep(60)"
            with self.assertRaises(subprocess.TimeoutExpired):
                contained_run([sys.executable, '-c', script], cwd=path, timeout=1, scratch=path)
            import win32api
            pid = int((path/'child.pid').read_text())
            try:
                handle = win32api.OpenProcess(0x1000, False, pid)
            except Exception:
                return
            try:
                import win32process
                self.assertNotEqual(win32process.GetExitCodeProcess(handle), 259)
            finally:
                handle.Close()

    def test_process_parent_death_kills_tree(self):
        with tempfile.TemporaryDirectory(prefix='tree-crash-') as folder:
            path = Path(folder)
            script = '''
import sys
from pathlib import Path
from pilot.worker import contained_run
contained_run([sys.executable,'-c',"import os,time; from pathlib import Path; Path('child.pid').write_text(str(os.getpid())); time.sleep(60)"],cwd=Path(sys.argv[1]),scratch=Path(sys.argv[1]),timeout=60)
'''
            process = subprocess.Popen([sys.executable, '-B', '-c', script, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                import time
                deadline = time.monotonic()+5
                while not (path/'child.pid').exists() and time.monotonic() < deadline and process.poll() is None:
                    time.sleep(.02)
                self.assertTrue((path/'child.pid').exists())
                pid = int((path/'child.pid').read_text())
                import win32api, win32event
                handle = win32api.OpenProcess(0x100000, False, pid)
                try:
                    process.kill()
                    process.wait(timeout=5)
                    self.assertEqual(win32event.WaitForSingleObject(handle, 5000), 0)
                finally:
                    handle.Close()
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)

    def test_disk_watchdog_kills_writer(self):
        from .worker import contained_run
        with tempfile.TemporaryDirectory(prefix='disk-watchdog-') as folder:
            with self.assertRaisesRegex(OSError, 'scratch_limit'):
                contained_run([sys.executable, '-c', "from pathlib import Path; import time; Path('output').write_bytes(b'x'*20000); time.sleep(60)"],
                    cwd=Path(folder), scratch=Path(folder), max_scratch_bytes=10000, timeout=5)


def _alive(pid):
    """True while a pid is still an *executable* process.

    A zombie (state Z) or dead (X/x) process is terminated — it cannot run or
    touch data — so it counts as not alive, matching the containment guarantee
    (and `process._pgid_alive`). On Railway the app is PID 1 and does not reap,
    so a PDEATHSIG-killed orphan whose reaper (the killed supervisor) is gone
    can linger as a zombie; that is still 'terminated' for our purposes.
    """
    try:
        with open('/proc/%d/stat' % pid, 'rb') as handle:
            content = handle.read()
    except (FileNotFoundError, ProcessLookupError):
        return False
    end = content.rfind(b')')
    if end < 0:
        return False
    fields = content[end + 2:].split()
    return bool(fields) and fields[0] not in (b'Z', b'X', b'x')


@unittest.skipUnless(os.name != 'nt', 'POSIX containment only on Linux')
class PosixProcessContainmentTests(unittest.TestCase):
    """Linux counterpart to ProcessContainmentTests: same job-lifetime guarantees
    proven with POSIX primitives (session/process-group kill + PR_SET_PDEATHSIG),
    so the Railway execution path is contained, not just the Windows benchmark."""

    def test_process_ownership_evidence(self):
        from .worker import contained_run
        with tempfile.TemporaryDirectory(prefix='posix-process-evidence-') as folder:
            path = Path(folder)
            contained_run([sys.executable, '-c', 'pass'], cwd=path, scratch=path, timeout=5)
            marker = json.loads((path/'process.json').read_text())
            self.assertEqual(marker['executable'], Path(sys.executable).name)
            self.assertIsInstance(marker['pid'], int)

    def test_timeout_terminates_descendant_before_cleanup(self):
        # A grandchild spawned by the contained process must be reaped by the
        # process-group kill before the caller is allowed to touch scratch.
        from .worker import contained_run
        with tempfile.TemporaryDirectory(prefix='posix-tree-timeout-') as folder:
            path = Path(folder)
            script = ("import subprocess,sys,time;from pathlib import Path;"
                      "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
                      "Path('child.pid').write_text(str(p.pid));time.sleep(60)")
            with self.assertRaises(subprocess.TimeoutExpired):
                contained_run([sys.executable, '-c', script], cwd=path, timeout=1, scratch=path)
            pid = int((path/'child.pid').read_text())
            # contained_run only returns after confirming the group is dead.
            self.assertFalse(_alive(pid), 'descendant survived process-group kill')

    def test_process_parent_death_kills_tree(self):
        # If the supervising worker dies (SIGKILL, no cleanup runs), the validator
        # child must still die via PR_SET_PDEATHSIG=SIGKILL.
        with tempfile.TemporaryDirectory(prefix='posix-tree-crash-') as folder:
            path = Path(folder)
            script = (
                "import sys\n"
                "from pathlib import Path\n"
                "from pilot.worker import contained_run\n"
                "contained_run([sys.executable,'-c',\"import os,time;from pathlib import Path;"
                "Path('child.pid').write_text(str(os.getpid()));time.sleep(60)\"],"
                "cwd=Path(sys.argv[1]),scratch=Path(sys.argv[1]),timeout=60)\n")
            process = subprocess.Popen([sys.executable, '-B', '-c', script, str(path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                import time
                deadline = time.monotonic()+10
                while not (path/'child.pid').exists() and time.monotonic() < deadline and process.poll() is None:
                    time.sleep(.02)
                self.assertTrue((path/'child.pid').exists(), 'validator child never registered')
                pid = int((path/'child.pid').read_text())
                self.assertTrue(_alive(pid))
                process.kill()
                process.wait(timeout=5)
                # PR_SET_PDEATHSIG fires when the direct parent dies.
                gone_by = time.monotonic()+10
                while _alive(pid) and time.monotonic() < gone_by:
                    time.sleep(.02)
                self.assertFalse(_alive(pid), 'child survived supervising-parent death')
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)

    def test_disk_watchdog_kills_writer(self):
        from .worker import contained_run
        with tempfile.TemporaryDirectory(prefix='posix-disk-watchdog-') as folder:
            with self.assertRaisesRegex(OSError, 'scratch_limit'):
                contained_run([sys.executable, '-c', "from pathlib import Path; import time; Path('output').write_bytes(b'x'*20000); time.sleep(60)"],
                    cwd=Path(folder), scratch=Path(folder), max_scratch_bytes=10000, timeout=5)

    def test_whole_job_deadline_cleans_raw_inputs(self):
        from .worker import execute_job
        with tempfile.TemporaryDirectory(prefix='posix-whole-job-timeout-') as folder:
            policy = json.loads(Path('pilot/policy.json').read_text())
            policy['job_timeout_seconds'] = .001
            with self.assertRaises(subprocess.TimeoutExpired):
                execute_job([{'id': 'synthetic', 'old': b'<synthetic/>'}], policy, Path(folder))
            self.assertEqual(list(Path(folder).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
