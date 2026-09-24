"""Synthetic S3 contract checks; live provider IAM needs separate acceptance."""
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import xreg
from .object_storage import ObjectScopeError, S3RawFixtures
from .payments import OfflineStripe
from .service import Rejected, Service
from .test_pilot import example_payload, fake_execute, paid
from . import worker


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.fail_delete = False

    def put_object(self, *, Bucket, Key, Body, **kwargs):
        self.objects[(Bucket, Key)] = Body

    def get_object(self, *, Bucket, Key):
        import io
        return {'Body': io.BytesIO(self.objects[Bucket, Key])}

    def list_objects_v2(self, *, Bucket, Prefix, **kwargs):
        return {'Contents': [{'Key': key} for bucket, key in self.objects if bucket == Bucket and key.startswith(Prefix)],
                'IsTruncated': False}

    def delete_objects(self, *, Bucket, Delete):
        if self.fail_delete:
            return {'Errors': [{'Code': 'Denied'}]}
        for row in Delete['Objects']:
            self.objects.pop((Bucket, row['Key']), None)
        return {}


class RawStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='object-storage-')
        self.state = Path(self.temp.name)/'state'
        self.provider = OfflineStripe()
        self.client = FakeS3()
        self.store = S3RawFixtures(self.client, 'test-raw')
        self.service = Service(self.state, self.provider, 'synthetic', raw_store=self.store)
        data = base64.b64decode(example_payload()['fixtures'][0]['old'])
        self.allow = patch.object(worker, 'public_hashes', return_value={xreg.sha(data)})
        self.allow.start()

    def tearDown(self):
        self.allow.stop()
        self.service.close()
        self.temp.cleanup()

    def test_order_scope_and_success_cleanup(self):
        a, b = paid(self.service), paid(self.service)
        with self.assertRaisesRegex(Rejected, 'access_denied'):
            self.service.submit(b['order_id'], a['access_token'], example_payload(), fake_execute)
        with self.assertRaisesRegex(ObjectScopeError, 'invalid_object_scope'):
            self.store.get('../other', '0'*32, 0, 'old')
        observed = []
        def execute(decoded, policy, directory):
            observed.extend(self.client.objects)
            return fake_execute(decoded, policy, directory)
        result = self.service.submit(a['order_id'], a['access_token'], example_payload(), execute)
        self.assertEqual(result['job'], 'delivered')
        self.assertTrue(observed)
        self.assertTrue(all(key.startswith('raw/'+a['order_id']+'/') for _, key in observed))
        self.assertFalse(self.client.objects)

    def test_failure_timeout_and_retry_cleanup(self):
        order = paid(self.service)
        def fail(*args):
            self.assertTrue(self.client.objects)
            raise TimeoutError('synthetic timeout')
        first = self.service.submit(order['order_id'], order['access_token'], example_payload(), fail, request_id='first')
        self.assertEqual(first['job'], 'failed')
        self.assertFalse(self.client.objects)
        with self.service.db:
            self.service.db.execute("UPDATE gates SET healthy=1 WHERE name='delivery'")
        second = self.service.submit(order['order_id'], order['access_token'], example_payload(), fake_execute, request_id='retry')
        self.assertEqual(second['job'], 'delivered')
        self.assertFalse(self.client.objects)

    def test_restart_sweeps_interrupted_raw_job(self):
        self.store.put('a'*32, 'b'*32, 0, 'old', b'synthetic')
        self.service.close()
        self.service = Service(self.state, self.provider, 'synthetic', raw_store=self.store)
        self.assertFalse(self.client.objects)

    def test_deletion_failure_closes_safety_gate(self):
        order = paid(self.service)
        self.client.fail_delete = True
        result = self.service.submit(order['order_id'], order['access_token'], example_payload(), fake_execute)
        self.assertEqual(result['job'], 'failed')
        self.assertEqual(dict(self.service.db.execute('SELECT name,healthy FROM gates'))['safety'], 0)
        self.assertTrue(self.client.objects)
        self.assertIn('raw_job', self.service.get(order['order_id']))
        self.client.fail_delete = False
        self.assertEqual(self.service.reconcile_raw_objects(), {'cleaned': 1, 'unresolved': 0})
        self.assertFalse(self.client.objects)
        self.assertNotIn('raw_job', self.service.get(order['order_id']))

    def test_cleanup_reaches_actionable_order_after_historical_rows(self):
        with self.service.db:
            for index in range(105):
                oid = f'{index:032x}'
                self.service.db.execute('INSERT INTO orders VALUES (?,?)',
                                        (oid, json.dumps({'id': oid, 'job': 'expired'})))
            oid, job = 'f'*32, 'a'*32
            self.service.db.execute('INSERT INTO orders VALUES (?,?)',
                                    (oid, json.dumps({'id': oid, 'job': 'failed', 'raw_job': job})))
        self.store.put(oid, job, 0, 'old', b'synthetic')
        self.assertEqual(self.service.reconcile_raw_objects(), {'cleaned': 1, 'unresolved': 0})
        self.assertFalse(self.client.objects)

    def test_rejected_submit_preserves_preexisting_job_and_raw_cleanup(self):
        order = paid(self.service)
        oid, old_job = order['order_id'], 'e'*32
        self.store.put(oid, old_job, 0, 'old', b'old synthetic')
        with self.service.db:
            row = self.service.get(oid)
            row.update(job='running', raw_job=old_job,
                       job_deadline=self.service.clock()+1200)
            self.service.save(row)
        with self.assertRaisesRegex(Rejected, 'submission_in_progress'):
            self.service.submit(oid, order['access_token'], example_payload(),
                                fake_execute, request_id='fresh-after-crash')
        self.assertEqual(self.service.get(oid)['job'], 'running')
        self.assertEqual(self.service.get(oid)['raw_job'], old_job)
        with self.service.db:
            row = self.service.get(oid)
            row['job'] = 'failed'
            self.service.save(row)
        with self.assertRaisesRegex(Rejected, 'raw_input_cleanup_pending'):
            self.service.submit(oid, order['access_token'], example_payload(),
                                fake_execute, request_id='second-after-crash')
        self.assertEqual(self.service.reconcile_raw_objects()['cleaned'], 1)
        self.assertFalse(self.client.objects)
