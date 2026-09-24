"""Synthetic Railway bucket probe; prints booleans only, never credentials."""
import os
import uuid

import boto3
from botocore.exceptions import ClientError

from .object_storage import S3RawFixtures


def main():
    required = ('S3_RAW_BUCKET', 'S3_ENDPOINT', 'AWS_ACCESS_KEY_ID',
                'AWS_SECRET_ACCESS_KEY', 'AWS_DEFAULT_REGION', 'REPORT_BUCKET',
                'REPORT_S3_ENDPOINT', 'REPORT_S3_ACCESS_KEY_ID',
                'REPORT_S3_SECRET_ACCESS_KEY', 'REPORT_S3_REGION')
    if not all(os.environ.get(key) for key in required):
        raise ValueError('missing_storage_env')
    input_client = boto3.client('s3', endpoint_url=os.environ['S3_ENDPOINT'])
    report_client = boto3.client('s3', endpoint_url=os.environ['REPORT_S3_ENDPOINT'],
        aws_access_key_id=os.environ['REPORT_S3_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['REPORT_S3_SECRET_ACCESS_KEY'],
        region_name=os.environ['REPORT_S3_REGION'])
    store = S3RawFixtures(input_client, os.environ['S3_RAW_BUCKET'])
    a, b, job = (uuid.uuid4().hex for _ in range(3))
    result = {}
    try:
        a_key = store.put(a, job, 0, 'old', b'synthetic-A')
        b_key = store.put(b, job, 0, 'old', b'synthetic-B')
        result['scoped_prefixes'] = (a_key.startswith('raw/'+a+'/') and
                                      b_key.startswith('raw/'+b+'/'))
        result['round_trip'] = (store.get(a, job, 0, 'old') == b'synthetic-A' and
                                store.get(b, job, 0, 'old') == b'synthetic-B')
        try:
            report_client.get_object(Bucket=store.bucket, Key=b_key)
            result['report_principal_denied_input'] = False
        except ClientError as exc:
            result['report_principal_denied_input'] = (
                exc.response.get('Error', {}).get('Code') in ('AccessDenied', '403'))
        store.delete_job(a, job)
        result['a_deleted_b_present'] = (not input_client.list_objects_v2(
            Bucket=store.bucket, Prefix='raw/'+a+'/'+job+'/').get('Contents') and
            bool(input_client.list_objects_v2(
                Bucket=store.bucket, Prefix='raw/'+b+'/'+job+'/').get('Contents')))
    finally:
        store.delete_job(a, job)
        store.delete_job(b, job)
    result['both_deleted'] = (not input_client.list_objects_v2(
        Bucket=store.bucket, Prefix='raw/'+a+'/'+job+'/').get('Contents') and
        not input_client.list_objects_v2(
            Bucket=store.bucket, Prefix='raw/'+b+'/'+job+'/').get('Contents'))
    print(result)
    if not all(result.values()):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
