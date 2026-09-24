"""Public legal pages (/terms, /refund, /privacy, /notices).

The four finalized legal drafts are packaged under pilot/legal/ and served
verbatim at request time: correct status and content type, byte-identical
bodies carrying the pending-solicitor-review disclaimer, ordinary 404s for
unknown paths, zero events rows for page views (A2-01 discipline — not even
sampled ones), and healthy behaviour for concurrent GETs under the existing
request-slot design (either served in full or answered with the designed
immediate busy signal, never hung or partial).
"""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from .payments import OfflineStripe
from .server import LEGAL_PAGES, make_server
from .service import Service

DISCLAIMER = b'pending review by a qualified solicitor'
LEGAL_DIR = Path(__file__).resolve().parent / 'legal'


class LegalPageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='legal-pages-')
        self.now = 1800000000.0
        self.provider = OfflineStripe()
        self.service = Service(Path(self.temp.name) / 'state', self.provider,
                               'synthetic-test-signing-value', lambda: self.now)
        self.server = make_server(self.service)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=10)
        self.server.server_close()
        self.service.close()
        self.temp.cleanup()

    def get(self, path):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        try:
            conn.request('GET', path)
            response = conn.getresponse()
            return response.status, response.getheader('Content-Type'), response.read()
        finally:
            conn.close()

    def events_count(self):
        return self.service.db.execute('SELECT count(*) FROM events').fetchone()[0]

    def packaged(self, route):
        return (LEGAL_DIR / LEGAL_PAGES[route]).read_bytes()

    def test_routes_serve_packaged_drafts_verbatim(self):
        for route in LEGAL_PAGES:
            status, content_type, body = self.get(route)
            self.assertEqual(status, 200, route)
            self.assertEqual(content_type, 'text/markdown; charset=utf-8', route)
            self.assertEqual(body, self.packaged(route), route)
            self.assertIn(DISCLAIMER, body, route)

    def test_unknown_paths_404_as_usual(self):
        for path in ('/no-such-page', '/terms.md', '/legal/terms', '/terms/'):
            status, content_type, body = self.get(path)
            self.assertEqual(status, 404, path)
            self.assertTrue(content_type.startswith('application/json'), path)
            self.assertEqual(json.loads(body), {'error': 'not_found'}, path)

    def test_page_views_write_no_event_rows(self):
        before = self.events_count()
        for _ in range(3):
            for route in LEGAL_PAGES:
                status, _, _ = self.get(route)
                self.assertEqual(status, 200, route)
        self.assertEqual(self.events_count(), before)

    def test_concurrent_gets_stay_healthy_under_slot_design(self):
        routes = list(LEGAL_PAGES)
        workers = 24  # three times the eight dispatch slots
        barrier = threading.Barrier(workers)
        results, failures = [], []

        def worker(index):
            route = routes[index % len(routes)]
            try:
                barrier.wait(timeout=10)
                status, content_type, body = self.get(route)
                if status == 200:
                    if content_type != 'text/markdown; charset=utf-8' or body != self.packaged(route):
                        failures.append((route, 'bad_200_body'))
                elif status == 503:
                    # Designed immediate admission answer under saturation;
                    # never a hang, never a partial page.
                    if json.loads(body) != {'error': 'service_busy_retry_later'}:
                        failures.append((route, 'bad_503_body'))
                else:
                    failures.append((route, 'unexpected_status_%d' % status))
                results.append(status)
            except BaseException as exc:  # noqa: BLE001 - recorded and asserted below
                failures.append((None, repr(exc)))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(failures, [])
        self.assertTrue(results and all(status in (200, 503) for status in results))
        # Slots are released immediately: sequential page views all succeed,
        # unknown paths still 404, and the burst wrote no events rows.
        for route in LEGAL_PAGES:
            status, content_type, body = self.get(route)
            self.assertEqual((status, content_type, body),
                             (200, 'text/markdown; charset=utf-8', self.packaged(route)), route)
        self.assertEqual(self.get('/unknown-legal-path')[0], 404)
        self.assertEqual(self.events_count(), 0)


if __name__ == '__main__':
    unittest.main()
