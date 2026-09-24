"""One bounded operating-agent route. No paid API calls in construction.

The managed runner supplies a Responses API client only after provisioning.
Deterministic tool permissions are enforced here, not by the model prompt.
"""
import json
import re
import subprocess
import sys
import time

from .worker import ROOT
from .service import GATES, REFUND_REASONS, Rejected
from .worker import PAIR

TASK_CODES = ('verify_acceptance', 'reconcile_refunds', 'expire_reports', 'reconcile_reservations')
TOOLS = [
    {'type': 'function', 'name': 'maintenance', 'description': 'Create and verify one bounded maintenance task.',
     'strict': True, 'parameters': {'type': 'object', 'properties': {'code': {'type': 'string', 'enum': list(TASK_CODES)}},
                                   'required': ['code'], 'additionalProperties': False}},
    {'type': 'function', 'name': 'pause_acquisition', 'description': 'Block new purchases while retaining paid obligations.',
     'strict': True, 'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}}
]
# A future manager can receive a subset of these deterministic controls. No
# model route is enabled by construction policy and the HTTP gateway authenticates
# the separate operator capability before dispatch.
TOOLS.extend([
    {'type': 'function', 'name': name, 'description': description, 'strict': True,
     'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}}
    for name, description in (
        ('health', 'Read database and gate health.'),
        ('spend', 'Read aggregate reserved spend and fixed cash budget.'),
        ('ruleset', 'Read the fixed ruleset pair.'))
])
TOOLS.extend([
    {'type': 'function', 'name': 'retry', 'description': 'Run one bounded reconciliation pass.',
     'strict': True, 'parameters': {'type': 'object', 'properties': {
         'code': {'type': 'string', 'enum': ['ledger', 'reservations', 'refunds']}},
         'required': ['code'], 'additionalProperties': False}},
    {'type': 'function', 'name': 'pause', 'description': 'Close one named gate.',
     'strict': True, 'parameters': {'type': 'object', 'properties': {
         'gate': {'type': 'string', 'enum': list(GATES)}},
         'required': ['gate'], 'additionalProperties': False}},
    {'type': 'function', 'name': 'verify', 'description':
        'Request deterministic verification of one gate. Reopens it only if every '
        'reconciliation check passes; verification failure must remain failure.',
     'strict': True, 'parameters': {'type': 'object', 'properties': {
         'gate': {'type': 'string', 'enum': list(GATES)}},
         'required': ['gate'], 'additionalProperties': False}},
    {'type': 'function', 'name': 'refund', 'description':
        'Request the full £299 refund of one verified order under a pre-defined policy reason. '
        'No amount, credit or discretionary reason exists.',
     'strict': True, 'parameters': {'type': 'object', 'properties': {
         'order_id': {'type': 'string', 'pattern': '^[a-f0-9]{32}$'},
         'reason': {'type': 'string', 'enum': list(REFUND_REASONS)}},
         'required': ['order_id', 'reason'], 'additionalProperties': False}},
    {'type': 'function', 'name': 'resolve_order', 'description':
        'Bounded recovery of one stuck order: retry raw cleanup/stranded job, re-issue a '
        'provider-failed refund (max 2), void only on authoritative provider absence, re-reconcile.',
     'strict': True, 'parameters': {'type': 'object', 'properties': {
         'order_id': {'type': 'string', 'pattern': '^[a-f0-9]{32}$'}},
         'required': ['order_id'], 'additionalProperties': False}},
])
# Read-only credential may call only these; every other name mutates state.
READ_ONLY = frozenset({'health', 'spend', 'ruleset'})
INSTRUCTIONS = '''Operate the fixed £299 XRechnung pilot. Use only the aggregate
snapshot and bounded functions. Monitor job failures, support counts, payments,
refunds, acquisition PAUSED state and reserved costs. Create maintenance tasks
when needed. After an incident, request gate verification; it reopens a closed
gate only when deterministic reconciliation passes and cannot be bypassed.
Never request invoice/report content or customer messages. Never
change financial limits, gates, offer, rules or model/tool permissions. If unsafe,
pause acquisition. Verification failure must remain failure. No general shell,
web access, customer uploads, advertising or owner impersonation is available.'''


def dispatch(service, name, arguments):
    if name == 'maintenance' and hasattr(service.db, 'claim_maintenance'):
        lease = service.db.claim_maintenance()
        if lease is None:
            return {'passed': False, 'error': 'task_running'}
        try:
            return _dispatch(service, name, arguments, recovered_lease=True)
        finally:
            lease.close()
    return _dispatch(service, name, arguments)


def _dispatch(service, name, arguments, recovered_lease=False):
    if not isinstance(arguments, dict):
        raise Rejected('operator_action_forbidden')
    if name == 'health' and arguments == {}:
        with service.lock:
            service.db.execute('SELECT 1').fetchone()
            gates = dict(service.db.execute('SELECT name,healthy FROM gates'))
        return {'database': 'reachable', 'gates': gates,
                'pending_refunds': service.operator_snapshot()['pending_refunds']}
    if name == 'spend' and arguments == {}:
        snapshot = service.operator_snapshot()
        return {key: snapshot[key] for key in
                ('simulated_reserved_pence', 'additional_cash_budget_gbp', 'advertising')}
    if name == 'ruleset' and arguments == {}:
        return {'pair': PAIR, 'configured_pair': service.policy['pair'],
                'changes_allowed': False}
    if name == 'pause' and set(arguments) == {'gate'} and arguments['gate'] in GATES:
        service.pause(arguments['gate'], actor='operator', reason='operator_requested')
        return {'paused': arguments['gate']}
    if name == 'verify' and set(arguments) == {'gate'} and arguments['gate'] in GATES:
        # The only operator-reachable reopen path: it always runs the full
        # deterministic reconciliation and cannot be weakened or bypassed.
        return service.verify_gate(arguments['gate'], actor='operator')
    if name == 'retry' and set(arguments) == {'code'}:
        if arguments['code'] == 'ledger':
            return service.reconcile_ledger()
        if arguments['code'] == 'reservations':
            return service.reconcile_reservations()
        if arguments['code'] == 'refunds':
            return dispatch(service, 'maintenance', {'code': 'reconcile_refunds'})
        raise Rejected('operator_action_forbidden')
    if name == 'refund' and set(arguments) == {'order_id', 'reason'}:
        oid = arguments['order_id']
        if not isinstance(oid, str) or not re.fullmatch(r'[a-f0-9]{32}', oid) \
                or arguments['reason'] not in REFUND_REASONS:
            raise Rejected('operator_action_forbidden')
        return service.operator_request_refund(oid, arguments['reason'])
    if name == 'resolve_order' and set(arguments) == {'order_id'}:
        oid = arguments['order_id']
        if not isinstance(oid, str) or not re.fullmatch(r'[a-f0-9]{32}', oid):
            raise Rejected('operator_action_forbidden')
        return service.resolve_order(oid, actor='operator')
    if name == 'pause_acquisition' and arguments == {}:
        service.pause('acquisition', actor='operator', reason='operator_requested')
        return {'paused': True}
    if name != 'maintenance' or set(arguments) != {'code'} or arguments['code'] not in TASK_CODES:
        raise Rejected('operator_action_forbidden')
    code = arguments['code']
    with service.lock, service.db:
        row = service.db.execute('SELECT status FROM maintenance WHERE code=?', (code,)).fetchone()
        if row and row['status'] == 'running':
            if not recovered_lease:
                return {'passed': False, 'error': 'task_running'}
            service.db.execute("UPDATE maintenance SET status='failed',evidence=? WHERE code=?",
                (json.dumps({'passed': False, 'error': 'task_interrupted'}), code))
            service._set_gate('acquisition', 0, 'system', 'maintenance_interrupted', {'code': code})
        service.db.execute('INSERT INTO maintenance(code,status,evidence) VALUES (?,\'running\',NULL) '
            'ON CONFLICT(code) DO UPDATE SET status=\'running\',evidence=NULL', (code,))
        if code == 'verify_acceptance':
            # Persist the admission barrier before launching the verifier.
            service._set_gate('acquisition', 0, 'system', 'acceptance_verification_started', {'code': code})
        service.event('maintenance_started', code=code)
    started = time.monotonic()
    try:
        if code == 'verify_acceptance':
            proc = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'test_xreg', 'test_location_matching',
                'pilot.test_pilot', 'pilot.test_finance_recovery', 'pilot.test_object_storage',
                'pilot.test_operator_api', 'pilot.test_remediation', 'pilot.test_remediation2',
                'pilot.test_gate_recovery', 'pilot.test_fable_review', 'pilot.test_preprod',
                'pilot.test_review_remediation', 'pilot.test_benchmark_compat'],
                cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
            evidence = {'passed': proc.returncode == 0, 'exit_code': proc.returncode}
        elif code == 'reconcile_reservations':
            evidence = service.reconcile_reservations()
        elif code == 'reconcile_refunds':
            with service.lock:
                ids = [r[0] for r in service.db.execute('SELECT id FROM orders')
                       if service.get(r[0])['refund'] not in ('none', 'succeeded')]
            results = [service.reconcile_refund(oid)['refund'] for oid in ids[:100]]
            unresolved = service.operator_snapshot()['pending_refunds']
            evidence = {'checked': len(results), 'unresolved': unresolved, 'passed': unresolved == 0,
                        'resolution': 'provider_or_authorised_operator_required' if unresolved else 'complete'}
        else:
            with service.lock, service.db:
                service.purge_reports()
                remaining = service.db.execute(
                    'SELECT count(*) FROM reports WHERE expires<=?', (service.clock(),)).fetchone()[0]
                evidence = {'expired_report_rows_remaining': remaining, 'passed': remaining == 0}
    except Exception as exc:
        evidence = {'passed': False, 'error': type(exc).__name__}
    with service.lock, service.db:
        status = 'verified' if evidence['passed'] else ('blocked' if evidence.get('unresolved') else 'failed')
        if status != 'verified':
            service._set_gate('acquisition', 0, 'system', 'maintenance_'+status, {'code': code, 'evidence': evidence})
        service.db.execute('UPDATE maintenance SET status=?,evidence=? WHERE code=?', (status, json.dumps(evidence), code))
        service.event('shared_maintenance', code=code, wall_seconds=time.monotonic()-started,
                      evidence=evidence, actual_cash_gbp=0, subscription_allocation_gbp=None,
                      api_equivalent_usd=None, operator_interventions=0)
    return evidence


def tick(service, responses_client=None):
    if not service.policy['managed_operator_enabled'] or responses_client is None:
        return {'status': 'LAUNCH_BLOCKER', 'reason': 'managed_route_unprovisioned_and_cash_budget_zero',
                'snapshot': service.operator_snapshot()}
    # Intentionally unreachable under shipped immutable policy. Provisioning must
    # add a durable per-call spend reservation before enabling this route.
    raise Rejected('managed_spend_reservation_and_access_acceptance_required')


def request_spec(service):
    """Reviewable supported Responses API request, not a claim of a deployed agent."""
    return {'model': 'gpt-5-mini', 'store': False, 'max_output_tokens': 2000,
            'instructions': INSTRUCTIONS, 'input': json.dumps(service.operator_snapshot()),
            'tools': TOOLS, 'parallel_tool_calls': False}
