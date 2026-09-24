"""Synthetic staging HTTP order capability probe; never prints tokens."""
import json
import secrets
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ORIGIN = 'https://REDACTED-STAGING-HOST.up.railway.app'


def request(path, *, body=None, token=None, key=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if key:
        headers['Idempotency-Key'] = key
    req = Request(ORIGIN+path, data=json.dumps(body).encode() if body is not None else None,
                  headers=headers)
    try:
        with urlopen(req, timeout=20) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, json.load(exc)


def main():
    orders = []
    for _ in range(2):
        status, body = request('/checkout', body={'product': 'synthetic-storage-scope',
            'attested': True, 'source': 'direct'}, key=secrets.token_hex(16))
        if status != 201:
            raise RuntimeError('checkout_probe_failed_'+str(status))
        orders.append(body)
    a, b = orders
    own, _ = request('/orders/'+a['order_id'], token=a['access_token'])
    cross, cross_body = request('/orders/'+b['order_id'], token=a['access_token'])
    anonymous, anonymous_body = request('/orders/'+b['order_id'])
    result = {'own_status_200': own == 200,
              'cross_denied': cross == 409 and cross_body.get('error') == 'access_denied',
              'anonymous_denied': anonymous == 409 and anonymous_body.get('error') == 'access_denied'}
    print(result)
    if not all(result.values()):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
