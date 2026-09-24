"""HTTP end-to-end evidence with real Java and OFFLINE simulated payments."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import platform
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import setup
import xreg
from .payments import OfflineStripe
from .server import make_server
from .service import Service
from .worker import ROOT, PAIR


def _row_dict(row):
    """Adapter-safe row->dict; PostgresState.Row iterates values, not pairs."""
    names = row.names if hasattr(row, 'names') else row.keys()
    return {name: row[name] for name in names}


def run(out, count):
    if out.exists():
        raise RuntimeError('Choose fresh evidence output; existing evidence is never overwritten')
    out.mkdir(parents=True)
    start = time.monotonic()
    original_hashes = {name:xreg.sha((ROOT/name).read_bytes()) for name in
        ('xreg.py','location_matching.py','test_xreg.py','test_location_matching.py','GATE1.md')}
    pilot_hashes = {p.relative_to(ROOT).as_posix():xreg.sha(p.read_bytes()) for p in (ROOT/'pilot').glob('*.py')}
    checks = {}
    with (out/'tests.txt').open('w', encoding='utf-8') as stream:
        suite = unittest.defaultTestLoader.loadTestsFromNames(['test_xreg','test_location_matching','pilot.test_pilot','pilot.test_remediation','pilot.test_remediation2','pilot.test_gate_recovery'])
        test = unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
        checks['tests'] = {'run':test.testsRun, 'failures':len(test.failures), 'errors':len(test.errors)}
    if not test.wasSuccessful():
        raise RuntimeError('tests failed; see tests.txt')
    with tempfile.TemporaryDirectory(prefix='pilot-e2e-') as folder:
        work = Path(folder)
        corpus = setup.materialise_corpus(work/'caller-owned-public')
        source = json.loads((ROOT/'manifests/corpus.json').read_text())['fixtures'][:count]
        # A paired maximum-cardinality pack; same bytes avoid unsupported identity claims.
        fixtures, originals = [], {}
        for index, row in enumerate(source):
            path = corpus/row['source_path']
            data = path.read_bytes()
            originals[str(path)] = xreg.sha(data)
            encoded = base64.b64encode(data).decode()
            fixtures.append({'id':f'f{index:03d}','old':encoded,'new':encoded})
        payload = {'pair':PAIR,'fixtures':fixtures}
        service = Service(work/'state', OfflineStripe(), 'local-synthetic-webhook-value')
        server = make_server(service)
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        origin = f'http://127.0.0.1:{server.server_port}'
        def request(path, body=None, token=None, key=None):
            headers = {'Content-Type':'application/json'}
            if token:
                headers['Authorization'] = 'Bearer '+token
            if key:
                headers['Idempotency-Key'] = key
            req = urllib.request.Request(origin+path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=1800) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read()
        try:
            checks['scope_http'] = request('/')[0]
            checks['requirements_http'] = request('/requirements')[0]
            status, sample = request('/sample')
            checks['sample_http'] = status
            (out/'public-sample.html').write_bytes(sample)
            status, body = request('/checkout',{'product':'public-example','attested':True,'source':'direct'},key='public-benchmark-checkout-retry-0001')
            assert status == 201
            order = json.loads(body)
            oid, token = order['order_id'], order['access_token']
            checks['redirect_does_not_fulfil'] = request('/success')[0] == 200 and json.loads(request('/orders/'+oid,token=token)[1])['payment']=='unpaid'
            checks['unauthenticated_denied'] = request('/orders/'+oid)[0] == 409
            assert request('/simulate-payment/'+order['session_id'],{})[0] == 200
            checks['delivery'] = []
            for run_number in (1,2):
                tick = time.monotonic()
                status, body = request('/orders/'+oid+'/submit',payload,token,'run-'+str(run_number))
                result = json.loads(body)
                checks['delivery'].append({'run':run_number,'http':status,'status':result,'wall_seconds':time.monotonic()-tick})
                assert status == 200 and result['job']=='delivered' and result['runs']==run_number
                repeat_status, repeat_body = request('/orders/'+oid+'/submit',payload,token,'run-'+str(run_number))
                assert repeat_status == 200 and json.loads(repeat_body)==result
                dest = out/f'run-{run_number}'
                dest.mkdir()
                for name in ('results.json','junit.xml','report.html'):
                    status, content = request('/orders/'+oid+'/'+name,token=token)
                    assert status == 200
                    (dest/name).write_bytes(content)
            checks['third_run_denied'] = request('/orders/'+oid+'/submit',payload,token,'run-3')[0] == 409
            checks['idempotent_submission_verified'] = True
            checks['support'] = {topic:request('/orders/'+oid+'/support',{'topic':topic},token)[0]
                                 for topic in ('access','status','input','rerun','advice')}
            checks['refund_request'] = json.loads(request('/orders/'+oid+'/support',{'topic':'refund'},token)[1])
            service.provider.refunds['re_'+oid]['status'] = 'succeeded'
            checks['refund_final'] = service.reconcile_refund(oid)
            checks['originals_preserved'] = all(Path(p).exists() and xreg.sha(Path(p).read_bytes())==h for p,h in originals.items())
            checks['worker_temp_empty'] = not any((service.state/'temporary').iterdir())
            events = [_row_dict(row) for row in service.db.execute('SELECT at,kind,order_id,data FROM events')]
            for event in events:
                event['data'] = json.loads(event['data'])
            xreg.write_json(out/'events.json', events)
            checks['determinism'] = {name:(out/'run-1'/name).read_bytes()==(out/'run-2'/name).read_bytes()
                                     for name in ('results.json','junit.xml','report.html')}
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
            service.close()
    checks['e2e_tool_created_directory_removed'] = not work.exists()
    checks['unchanged_core'] = {name:xreg.sha((ROOT/name).read_bytes())==value for name,value in original_hashes.items()}
    checks['pilot_source_hashes'] = pilot_hashes
    checks['pilot_source_unchanged_during_run'] = all(xreg.sha((ROOT/name).read_bytes())==value for name,value in pilot_hashes.items())
    checks.update(fixture_ids=count, validations=count*8, pair=PAIR, wall_seconds=time.monotonic()-start,
        platform=platform.platform(), additional_cash_gbp=0, payment_transport='OFFLINE TEST DOUBLE; not Stripe sandbox verification',
        managed_operator='UNPROVISIONED', customer_data_processed=False, cloud_isolation_tested=False,
        allocated_subscription_cost_gbp=None, model_api_equivalent_usd=None)
    required = ('redirect_does_not_fulfil','unauthenticated_denied','third_run_denied','originals_preserved',
                'worker_temp_empty','e2e_tool_created_directory_removed')
    checks['passed'] = all(checks[k] for k in required) and all(checks['determinism'].values()) and all(checks['unchanged_core'].values()) and checks['pilot_source_unchanged_during_run']
    xreg.write_json(out/'summary.json',checks)
    print(json.dumps({'passed':checks['passed'],'wall_seconds':checks['wall_seconds'],'fixture_ids':count,'evidence':str(out)}))
    return 0 if checks['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--count',type=int,choices=range(1,251),default=250)
    args = parser.parse_args()
    raise SystemExit(run(args.out.resolve(),args.count))
