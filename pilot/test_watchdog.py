"""Watchdog tests: sweep logic against a real local operator surface."""
import json
import tempfile
import threading
import unittest
from pathlib import Path

from . import watchdog
from .payments import OfflineStripe
from .server import make_server
from .service import Service

READ = 'read-only-operator-credential-0123456789'
ACTION = 'action-operator-credential-abcdef0123456789'


class CaptureSink:
    def __init__(self):
        self.emitted = []

    def emit(self, finding):
        self.emitted.append(finding)


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='watchdog-')
        self.service = Service(Path(self.temp.name) / 'state', OfflineStripe(),
                               'synthetic-test-signing-value')
        self.server = make_server(self.service, operator_read_token=READ, operator_token=ACTION,
                                   deploy=True)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.sink = CaptureSink()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.service.close()
        self.temp.cleanup()

    def test_healthy_surface_alerts_nothing(self):
        self.service.pause('acquisition', 'owner', 'launch_gated')  # designed owner state
        fresh, repeated = watchdog.run_once(self.origin, READ, ACTION, {}, self.sink)
        self.assertEqual([f for f in fresh if f.severity != watchdog.INFO], [])
        self.assertIn('acquisition_paused', [f.code for f in fresh])  # digest-only info
        self.assertEqual(self.sink.emitted, [])

    def test_closed_delivery_gate_alerts(self):
        self.service.pause('delivery', 'operator', 'test')
        fresh, _ = watchdog.run_once(self.origin, READ, ACTION, {}, self.sink)
        codes = [f.code for f in fresh]
        self.assertIn('gate_delivery_closed', codes)
        emitted = [f.code for f in self.sink.emitted]
        self.assertIn('gate_delivery_closed', emitted)
        self.assertTrue(watchdog.ads_should_pause(fresh))

    def test_repeat_findings_dedupe_and_realert_after_window(self):
        self.service.pause('spending', 'operator', 'test')
        state = {}
        fresh1, repeated1 = watchdog.run_once(self.origin, READ, ACTION, state, self.sink)
        fresh2, repeated2 = watchdog.run_once(self.origin, READ, ACTION, state, self.sink)
        self.assertIn('gate_spending_closed', [f.code for f in fresh1])
        self.assertEqual(repeated1, [])
        self.assertEqual([f.code for f in fresh2 if f.code == 'gate_spending_closed'], [])
        self.assertIn('gate_spending_closed', [f.code for f in repeated2])
        # re-seen after the dedupe window alerts again
        state['seen'] = {k: t - (watchdog.DEDUPE_WINDOW_SECONDS + 1) for k, t in state['seen'].items()}
        fresh3, _ = watchdog.run_once(self.origin, READ, ACTION, state, self.sink)
        self.assertIn('gate_spending_closed', [f.code for f in fresh3])

    def test_unreachable_origin_is_a_single_alert(self):
        fresh, _ = watchdog.run_once('http://127.0.0.1:1', READ, ACTION, {}, self.sink)
        self.assertEqual([f.code for f in fresh], ['health_unreachable'])
        self.assertEqual(fresh[0].severity, watchdog.ALERT)
        self.assertTrue(watchdog.ads_should_pause(fresh))

    def test_healthy_open_sales_do_not_pause_ads(self):
        fresh, _ = watchdog.run_once(self.origin, READ, ACTION, {}, self.sink)
        self.assertFalse(watchdog.ads_should_pause(fresh))


class SmtpSinkTests(unittest.TestCase):
    def test_missing_env_raises_listing_keys(self):
        with self.assertRaises(ValueError) as raised:
            watchdog.SmtpSink.from_env(environ={})
        self.assertIn('WATCHDOG_SMTP_HOST', str(raised.exception))

    def test_message_reaches_starttls_login_send(self):
        import smtplib
        from unittest.mock import patch
        sink = watchdog.SmtpSink('smtp.test', 587, 'u', 'p', 'from@test', 'to@test')
        sent = {}
        class FakeServer:
            def __init__(self, host, port, timeout=None): sent['host'] = host
            def __enter__(self): return self
            def __exit__(self, *exc): sent['closed'] = True
            def starttls(self): sent['tls'] = True
            def login(self, user, password): sent['login'] = user
            def send_message(self, message): sent['subject'] = message['Subject']
        with patch.object(smtplib, 'SMTP', FakeServer):
            sink.emit(watchdog.Finding('gate_delivery_closed', watchdog.ALERT, 'test'))
        self.assertEqual(sent['host'], 'smtp.test')
        self.assertTrue(sent['tls'] and sent['closed'])
        self.assertEqual(sent['login'], 'u')
        self.assertEqual(sent['subject'], '[xreg-watchdog] ALERT: gate_delivery_closed')


class StarterTests(unittest.TestCase):
    """The railway_app bootstrap is env-gated and never runs by default."""

    def test_disabled_by_default(self):
        from . import railway_app
        self.assertIsNone(railway_app.start_watchdog(env={}))

    def test_enabled_runs_console_loop_once_then_sleeps(self):
        import threading
        import time
        from unittest.mock import patch
        from . import railway_app
        calls = []
        done = threading.Event()
        real_sleep = time.sleep

        def fake_run_once(origin, read_token, action_token, state, sink=None):
            calls.append(origin)
            return [], []

        def fake_sleep(seconds):
            done.set()
            real_sleep(0.1)
            raise KeyboardInterrupt  # exit the loop after one sweep

        env = {"WATCHDOG_ENABLED": "true", "WATCHDOG_ORIGIN": "https://example.test",
               "XREG_STATE": self.id()}
        with patch("pilot.watchdog.run_once", fake_run_once), \
                patch("time.sleep", fake_sleep), \
                patch.object(railway_app, "public_origin", return_value="https://example.test"):
            thread = railway_app.start_watchdog(env=env, read_token="r", action_token="a")
            self.assertIsNotNone(thread)
            self.assertTrue(done.wait(timeout=5))
        self.assertEqual(calls, ["https://example.test"])


if __name__ == '__main__':
    unittest.main()
