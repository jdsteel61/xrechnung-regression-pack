"""Durable local sandbox business state. Financial authority is outside models."""
import hashlib
import hmac
import itertools
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
import uuid

import xreg
from . import worker
from .launch_config import SAFE_DEFAULT, LaunchConfig
from .payments import PaymentError, verify_event
from .state import StateOwner, clean_scratch

POLICY = Path(__file__).with_name('policy.json')

GATES = ('delivery', 'safety', 'spending', 'acquisition')
PRICE_PENCE = 29900
# Pilot refund policy: full order value only, only for these reasons. There is
# no amount parameter and no discretionary reason anywhere in the code.
REFUND_REASONS = ('customer_request_within_7_days', 'consumer_cancellation_within_14_days', 'pack_not_deliverable')
ACTIVE_DISPUTE = ('warning_needs_response', 'warning_under_review', 'needs_response', 'under_review', 'lost')


class Rejected(ValueError):
    pass


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


# ASCII-anchored identifier shapes (finding A1-04): str.isalnum() also accepts
# Unicode letters/digits (e.g. Cyrillic homoglyphs), which must never become
# capability material or reach provider surfaces. Checkout keys allow dash and
# underscore; submit keys historically allow dash only; provider session ids
# follow the payments adapter's underscore form.
_CHECKOUT_KEY_SHAPE = re.compile(r'[A-Za-z0-9_-]+')
_SUBMIT_KEY_SHAPE = re.compile(r'[A-Za-z0-9-]+')
_SESSION_ID_SHAPE = re.compile(r'[A-Za-z0-9_]+')


class Service:
    def __init__(self, state, provider, webhook_secret, clock=time.time, database=None, raw_store=None,
                 launch=SAFE_DEFAULT, validator_probe=None):
        self.policy = json.loads(POLICY.read_text())
        gate = json.loads((worker.ROOT/'evidence/gate1/final/gate1.json').read_text())
        if gate['technical_gate'] != 'GO' or not gate['final_code_audit_passed']:
            raise Rejected('gate1_not_passed')
        # Source policy stays locked; production enablement arrives only as a
        # validated LaunchConfig (see launch_config.load), never a source edit.
        if any(self.policy[k] for k in ('live_payments', 'customer_uploads', 'advertising', 'managed_operator_enabled')):
            raise Rejected('construction_lock')
        if not isinstance(launch, LaunchConfig):
            raise Rejected('launch_config_required')
        if launch.live_payments and (provider.simulated or getattr(provider, 'live', False) is not True):
            raise Rejected('live_payments_require_live_provider')
        if not launch.live_payments and getattr(provider, 'live', False):
            raise Rejected('live_provider_requires_live_payments_config')
        # Webhook signing secret(s): one value, or several across a live
        # cutover so the TEST-era endpoint secret keeps verifying in-flight
        # events until it is retired at the provider. Capability derivations
        # below use a stable composite — the first secret — so a plain str
        # input derives exactly the tokens it always did.
        if isinstance(webhook_secret, (tuple, list)):
            secrets_tuple = tuple(webhook_secret)
            if not secrets_tuple or any(not isinstance(s, str) or not s for s in secrets_tuple):
                raise Rejected('invalid_webhook_secret')
        elif isinstance(webhook_secret, str) and webhook_secret:
            secrets_tuple = (webhook_secret,)
        else:
            raise Rejected('invalid_webhook_secret')
        self.secrets = secrets_tuple
        self.launch = launch
        self.livemode = launch.live_payments
        self.session_prefix = 'cs_live_' if self.livemode else 'cs_test_'
        # Dual-era providers (EraRoutingStripe) additionally accept TEST-era
        # provider objects, so carried-over orders keep reconciling after the
        # live cutover. Single-era providers keep exactly the one prefix they
        # always had; era consistency is enforced wherever a prefix is checked.
        self.session_prefixes = tuple(dict.fromkeys(
            getattr(provider, 'session_prefixes', ()) or (self.session_prefix,)))
        self.era_routing = bool(getattr(provider, 'era_routing', False))
        self.allowed_livemodes = (True, False) if self.era_routing else (self.livemode,)
        self.policy.update(live_payments=launch.live_payments, customer_uploads=launch.customer_uploads)
        self.validator_probe = validator_probe or (
            lambda: worker.validator_health(self.policy, self.state/'temporary'))
        self.state = Path(state).resolve()
        self.state.mkdir(parents=True, exist_ok=True)
        try:
            self.owner = StateOwner(self.state)
        except ValueError as exc:
            raise Rejected(str(exc)) from None
        (self.state/'temporary').mkdir(exist_ok=True)
        self.provider, self.secret, self.clock = provider, secrets_tuple[0], clock
        self.raw_store = raw_store
        self.lock, self.job_lock = threading.RLock(), threading.Lock()
        self.checkout_lock = threading.Lock()
        self.reconciliation_lock = threading.Lock()
        self.db = database or sqlite3.connect(self.state/'business.sqlite3', check_same_thread=False)
        if database is None:
            self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=DELETE;
            PRAGMA synchronous=FULL;
            PRAGMA secure_delete=ON;
            CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, at REAL, kind TEXT, order_id TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS webhooks(id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS gates(name TEXT PRIMARY KEY, healthy INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS reports(order_id TEXT, run INTEGER, name TEXT, content BLOB,
                expires REAL, PRIMARY KEY(order_id,run,name));
            CREATE TABLE IF NOT EXISTS maintenance(id INTEGER PRIMARY KEY, code TEXT UNIQUE, status TEXT, evidence TEXT);
            CREATE TABLE IF NOT EXISTS submissions(order_id TEXT, key TEXT, payload_hash TEXT, result TEXT,
                PRIMARY KEY(order_id,key));
            CREATE TABLE IF NOT EXISTS checkouts(key_hash TEXT PRIMARY KEY, payload_hash TEXT, order_id TEXT);
            CREATE TABLE IF NOT EXISTS gate_audit(id INTEGER PRIMARY KEY, at REAL, gate TEXT NOT NULL,
                action TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL, evidence TEXT);
            CREATE TABLE IF NOT EXISTS checkout_rate(client TEXT NOT NULL, at REAL NOT NULL);
        ''')
        for name in ('delivery', 'safety', 'spending', 'acquisition'):
            self.db.execute('INSERT OR IGNORE INTO gates VALUES (?,1)', (name,))
        self.db.commit()
        try:
            clean_scratch(self.state/'temporary')
            # A local state owner excludes another process. PostgreSQL-backed
            # Railway instances can overlap during deploy, so a new instance
            # must never sweep another instance's live raw namespace.
            if self.raw_store is not None and database is None:
                self.raw_store.sweep()
        except Exception:
            self.db.close()
            self.owner.close()
            raise Rejected('input_cleanup_failed') from None

        # A PostgreSQL process may start while the previous Railway instance
        # still serves requests. Recovery there is handled by the timed pass
        # under the cluster job lease, never by rewriting startup snapshots.
        if database is not None:
            return

        with self.lock, self.db:
            interrupted = self.db.execute("UPDATE maintenance SET status='failed', evidence=? WHERE status IN ('created','running')",
                (json.dumps({'passed': False, 'error': 'task_interrupted'}),)).rowcount
            if interrupted:
                self._set_gate('acquisition', 0, 'system', 'maintenance_interrupted', {'interrupted_tasks': interrupted})
            for row in self.db.execute('SELECT data FROM orders').fetchall():
                order = json.loads(row['data'])
                order.setdefault('reservation', 'held')
                order.setdefault('reservation_expires', order['created']+86400)
                order.setdefault('obligation_expires', order['created']+7*86400)
                order.setdefault('next_action', 'reconcile_reservations')
                order.setdefault('retry_at', 0)
                if order['job'] == 'running' and database is None:
                    order['job'] = 'failed'
                    order['error'] = 'worker_interrupted_resubmit_or_refund'
                    order.pop('job_deadline', None)
                    self.save(order)
                    self.event('worker_interrupted', order['id'])
                    self._set_gate('safety', 0, 'system', 'worker_interrupted', {'order_id': order['id']})
                self.save(order)
                # Legacy crash windows and interrupted attempts must have a durable
                # replay outcome before another request can consume a rerun.
                if order['job'] == 'running':
                    continue
                result = self.order_status(order)
                # Legacy rows have no execution identity linking them to the
                # latest order status. Never assign a later run's success to one.
                if result['job'] != 'failed':
                    result.update(job='failed', error='legacy_submission_outcome_unknown_check_status')
                self.db.execute('UPDATE submissions SET result=? WHERE order_id=? AND result IS NULL',
                    (json.dumps(result), order['id']))

    def execute_with_raw_storage(self, oid, decoded, executor):
        """Read exact order-scoped objects for execution, then delete all copies."""
        if self.raw_store is None:
            return executor(decoded, self.policy, self.state/'temporary')
        job = self.raw_store.new_job()
        with self.lock, self.db:
            order = self.get(oid)
            order['raw_job'] = job
            self.save(order)
        try:
            for index, row in enumerate(decoded):
                for side in ('old', 'new') if 'new' in row else ('old',):
                    self.raw_store.put(oid, job, index, side, row[side])
            staged = []
            for index, row in enumerate(decoded):
                record = {'id': row['id']}
                for side in ('old', 'new') if 'new' in row else ('old',):
                    record[side] = self.raw_store.get(oid, job, index, side)
                staged.append(record)
            return executor(staged, self.policy, self.state/'temporary')
        finally:
            try:
                self.raw_store.delete_job(oid, job)
                with self.lock, self.db:
                    order = self.get(oid)
                    if order.get('raw_job') == job:
                        order.pop('raw_job')
                        self.save(order)
            except Exception:
                self.pause('safety', 'system', 'raw_input_cleanup_failed',
                           {'order_id': oid, 'raw_job': job})
                raise Rejected('input_cleanup_failed') from None

    def reconcile_raw_objects(self):
        """Retry durable raw deletions and expire jobs past their deadline."""
        if self.raw_store is None:
            return {'cleaned': 0, 'unresolved': 0}
        cleaned = unresolved = 0
        with self.lock:
            rows = (json.loads(row['data']) for row in self.db.execute('SELECT data FROM orders'))
            candidates = (order for order in rows if order.get('raw_job') or
                          (order['job'] == 'running' and order.get('job_deadline', 0) <= self.clock()))
            for order in itertools.islice(candidates, 100):
                if order['job'] == 'running':
                    if order.get('job_deadline', 0) > self.clock():
                        continue
                    lease = None
                    if hasattr(self.db, 'claim_job'):
                        try:
                            lease = self.db.claim_job()
                        except Exception:
                            unresolved += 1
                            continue
                        if lease is None:
                            continue
                    try:
                        order = self.get(order['id'])
                        if (order['job'] != 'running' or
                                order.get('job_deadline', 0) > self.clock()):
                            continue
                        with self.db:
                            order.update(job='failed', error='worker_interrupted_resubmit_or_refund')
                            order.pop('job_deadline', None)
                            self.save(order)
                            self._set_gate('safety', 0, 'system', 'worker_interrupted',
                                           {'order_id': order['id'], 'source': 'job_deadline_recovery'})
                            self.db.execute('UPDATE submissions SET result=? WHERE order_id=? AND result IS NULL',
                                            (json.dumps(self.order_status(order)), order['id']))
                    finally:
                        if lease is not None:
                            lease.close()
                job = order.get('raw_job')
                if not job:
                    continue
                try:
                    self.raw_store.delete_job(order['id'], job)
                except Exception:
                    unresolved += 1
                    self.pause('safety', 'system', 'raw_object_deletion_unresolved',
                               {'order_id': order['id'], 'raw_job': job})
                    continue
                with self.db:
                    order = self.get(order['id'])
                    if order.get('raw_job') == job:
                        order.pop('raw_job')
                        self.save(order)
                        cleaned += 1
        return {'cleaned': cleaned, 'unresolved': unresolved}

    def reconcile_incomplete_submissions(self):
        """Close crash windows before/after the running transition."""
        if not self.job_lock.acquire(blocking=False):
            return {'recovered': 0, 'busy': True}
        lease = None
        try:
            if hasattr(self.db, 'claim_job'):
                lease = self.db.claim_job()
                if lease is None:
                    return {'recovered': 0, 'busy': True}
            recovered = 0
            with self.lock, self.db:
                pending = self.db.execute(
                    'SELECT order_id,key FROM submissions WHERE result IS NULL LIMIT 100').fetchall()
                for row in pending:
                    oid, key = row['order_id'], row['key']
                    order = self.get(oid)
                    if order['job'] == 'running':
                        order.update(job='failed', error='worker_interrupted_resubmit_or_refund')
                        order.pop('job_deadline', None)
                        self.save(order)
                        self._set_gate('safety', 0, 'system', 'worker_interrupted',
                                       {'order_id': oid, 'source': 'incomplete_submission_recovery'})
                    result = self.order_status(order)
                    # A NULL attempt result has no proof of delivery. An older
                    # delivered run may belong to a different submission key.
                    if result['job'] != 'failed':
                        result.update(job='failed', error='submission_interrupted_resubmit_or_refund')
                    self.db.execute('UPDATE submissions SET result=? WHERE order_id=? AND key=? AND result IS NULL',
                                    (json.dumps(result), oid, key))
                    self.event('submission_interrupted', oid)
                    recovered += 1
            return {'recovered': recovered, 'busy': False}
        finally:
            if lease is not None:
                lease.close()
            self.job_lock.release()

    def close(self):
        self.db.close()
        self.owner.close()

    def event(self, kind, oid=None, **data):
        self.db.execute('INSERT INTO events(at,kind,order_id,data) VALUES (?,?,?,?)',
                        (self.clock(), kind, oid, json.dumps(data, sort_keys=True)))

    def save(self, order):
        self.db.execute('INSERT OR REPLACE INTO orders VALUES (?,?)', (order['id'], json.dumps(order)))

    def get(self, oid):
        # RLock-serialised read: the order-status poll contract (202 + poll
        # while the job runs) lets a reader race the background submit thread
        # on the shared sqlite connection; unlocked concurrent use raises
        # sqlite3.InterfaceError intermittently and can fail the in-flight
        # job (reproduced 23 Sep 2026, test_long_submission_returns_202_then_
        # completes). The RLock is reentrant, so callers already holding it
        # (auth inside submit's idempotency block) are unaffected.
        with self.lock:
            row = self.db.execute('SELECT data FROM orders WHERE id=?', (oid,)).fetchone()
        if not row:
            raise Rejected('access_denied')
        return json.loads(row['data'])

    def auth(self, oid, token, recovery=False):
        order = self.get(oid)
        key = 'recovery_hash' if recovery else 'access_hash'
        if not isinstance(token, str) or not hmac.compare_digest(order[key], digest(token)):
            # Session-bound success-page capabilities (owner decision D.4)
            # authenticate identically until rotation revokes them. Like the
            # primary capabilities, only their hashes are ever stored.
            bound = order.get('success_hashes') or {}
            alternate = bound.get('recovery' if recovery else 'access')
            if bound.get('revoked') or not isinstance(alternate, str) \
                    or not hmac.compare_digest(alternate, digest(token)):
                raise Rejected('access_denied')
        return order

    def _audit(self, gate, action, actor, reason, evidence):
        # Append-only operator/system trail. The application role has no
        # UPDATE/DELETE on this table in PostgreSQL (see migrations/002).
        self.db.execute('INSERT INTO gate_audit(at,gate,action,actor,reason,evidence) VALUES (?,?,?,?,?,?)',
                        (self.clock(), gate, action, str(actor)[:80], str(reason)[:120],
                         json.dumps(evidence, sort_keys=True, default=str)))

    def _set_gate(self, gate, healthy, actor, reason, evidence=None):
        """The only gate-state write. Caller owns the surrounding transaction.

        Every actual close/reopen transition is audited with actor, reason and
        evidence; a write that does not change state records nothing.
        """
        if gate not in GATES:
            raise Rejected('unknown_gate')
        changed = self.db.execute('UPDATE gates SET healthy=? WHERE name=? AND healthy<>?',
                                  (1 if healthy else 0, gate, 1 if healthy else 0)).rowcount
        if not changed:
            return False
        action = 'reopened' if healthy else 'closed'
        self._audit(gate, action, actor, reason, evidence or {})
        self.event('gate_'+action, gate=gate, actor=actor, reason=reason)
        if not healthy:
            self.event('purchases_paused', gate=gate, actor=actor, reason=reason)
        return True

    def pause(self, gate, actor='system', reason='unspecified', evidence=None):
        with self.lock, self.db:
            self._set_gate(gate, 0, actor, reason, evidence)
        return {'paused': gate}

    def _gate_verification_evidence(self):
        """Deterministic proof set. Caller holds the job lock and cluster lease.

        Holding the cluster-wide advisory lease proves no submit can execute
        anywhere, so any 'running' order is stranded and is failed here with
        the same durable transition the timed recovery passes use. A gate can
        reopen only if every check below passes afterwards.
        """
        checks = {}
        # (1) No running/stranded jobs.
        stranded = []
        with self.lock, self.db:
            for row in self.db.execute('SELECT data FROM orders').fetchall():
                order = json.loads(row['data'])
                if order.get('job') == 'running':
                    stranded.append(order['id'])
            for oid in stranded:
                order = self.get(oid)
                order.update(job='failed', error='worker_interrupted_resubmit_or_refund')
                order.pop('job_deadline', None)
                self.save(order)
                self._set_gate('safety', 0, 'system', 'stranded_running_job_failed', {'order_id': oid})
                self.db.execute('UPDATE submissions SET result=? WHERE order_id=? AND result IS NULL',
                                (json.dumps(self.order_status(order)), oid))
                self.event('worker_interrupted', oid)
        checks['stranded_jobs_failed'] = stranded
        # (2) No interrupted submissions without a durable result.
        recovered = 0
        while True:
            with self.lock, self.db:
                pending = self.db.execute(
                    'SELECT order_id,key FROM submissions WHERE result IS NULL LIMIT 100').fetchall()
                if not pending:
                    break
                for row in pending:
                    oid, key = row['order_id'], row['key']
                    order = self.get(oid)
                    result = self.order_status(order)
                    # No proof of delivery exists for a NULL attempt result.
                    if result['job'] != 'failed':
                        result.update(job='failed', error='submission_interrupted_resubmit_or_refund')
                    self.db.execute('UPDATE submissions SET result=? WHERE order_id=? AND key=? AND result IS NULL',
                                    (json.dumps(result), oid, key))
                    self.event('submission_interrupted', oid)
                    recovered += 1
        with self.lock:
            checks['interrupted_submissions_recovered'] = recovered
            checks['interrupted_submissions_remaining'] = self.db.execute(
                'SELECT count(*) FROM submissions WHERE result IS NULL').fetchone()[0]
        # (3) No raw-object cleanup backlog (retry deletions, then prove both
        # the pointer set and the provider namespace are empty).
        raw = self.reconcile_raw_objects()
        passes = 0
        while raw['cleaned'] and passes < 10:
            raw = self.reconcile_raw_objects()
            passes += 1
        with self.lock:
            pointers = sorted(json.loads(row['data'])['id'] for row in self.db.execute('SELECT data FROM orders')
                              if json.loads(row['data']).get('raw_job'))
        bucket_keys = []
        if self.raw_store is not None:
            bucket_keys = self.raw_store.list_raw()
        checks['raw_cleanup'] = {'reconcile_passes': passes, 'unresolved': raw['unresolved'],
                                 'orders_with_raw_pointer': pointers,
                                 'bucket_raw_key_count': len(bucket_keys),
                                 'bucket_raw_key_sample': bucket_keys[:5]}
        # (4) Payment/refund ledger reconciles against the provider.
        cursor, pages, ledger_ok = '', 0, True
        unresolved_orders = []
        while True:
            page = self.reconcile_ledger(after=cursor)
            if 'error' in page:
                ledger_ok = False
                break
            ledger_ok = ledger_ok and bool(page.get('passed'))
            unresolved_orders.extend(page.get('unresolved', []))
            cursor = page.get('next_cursor')
            pages += 1
            if cursor is None:
                break
            if pages >= 100:
                ledger_ok = False
                break
        checks['ledger'] = {'pages': pages, 'passed': ledger_ok,
                            'unresolved': unresolved_orders[:10]}
        # (5) No unresolved reservations.
        with self.lock:
            failed_reservations = sorted(json.loads(row['data'])['id'] for row in self.db.execute('SELECT data FROM orders')
                                         if json.loads(row['data']).get('reservation') == 'reconciliation_failed')
        reservations = self.reconcile_reservations()
        checks['reservations'] = {'sweep_passed': bool(reservations.get('passed')),
                                  'sweep_unresolved': reservations.get('unresolved'),
                                  'failed_ids': failed_reservations[:10]}
        # (6) Required health checks.
        with self.lock:
            running_now = sum(json.loads(row['data']).get('job') == 'running'
                              for row in self.db.execute('SELECT data FROM orders'))
            interrupted_maintenance = self.db.execute(
                "SELECT count(*) FROM maintenance WHERE status IN ('created','running')").fetchone()[0]
        scratch_empty = not any((self.state/'temporary').iterdir())
        checks['health'] = {'database': 'reachable', 'scratch_empty': scratch_empty,
                            'interrupted_maintenance': interrupted_maintenance,
                            'running_jobs': running_now}
        passed = checks['interrupted_submissions_remaining'] == 0 \
            and running_now == 0 and raw['unresolved'] == 0 and not pointers and len(bucket_keys) == 0 \
            and ledger_ok and checks['reservations']['sweep_passed'] and not failed_reservations \
            and scratch_empty and interrupted_maintenance == 0
        return {'passed': passed, 'checks': checks, 'at': self.clock()}

    def verify_gate(self, gate, actor='operator'):
        """Request deterministic verification of a closed gate.

        The only path that can set a gate healthy again. Runs under the job
        lock and the cluster-wide advisory lease so no submission can execute
        concurrently; every outcome is audited with actor, reason and evidence.
        There is deliberately no parameter to skip or weaken a check.
        """
        if gate not in GATES:
            raise Rejected('unknown_gate')
        if not self.job_lock.acquire(blocking=False):
            with self.lock, self.db:
                self._audit(gate, 'verification_failed', actor, 'worker_busy',
                            {'passed': False, 'error': 'worker_busy_retry_later'})
            return {'passed': False, 'error': 'worker_busy_retry_later'}
        lease = None
        try:
            if hasattr(self.db, 'claim_job'):
                try:
                    lease = self.db.claim_job()
                except Exception:
                    with self.lock, self.db:
                        self._audit(gate, 'verification_failed', actor, 'worker_lease_unavailable',
                                    {'passed': False, 'error': 'worker_lease_unavailable'})
                    return {'passed': False, 'error': 'worker_lease_unavailable'}
                if lease is None:
                    with self.lock, self.db:
                        self._audit(gate, 'verification_failed', actor, 'worker_busy',
                                    {'passed': False, 'error': 'worker_busy_retry_later'})
                    return {'passed': False, 'error': 'worker_busy_retry_later'}
            evidence = self._gate_verification_evidence()
            if gate == 'delivery':
                # Reconciliation alone cannot show delivery works: a broken
                # executor passes every ledger check. Require a real canary run.
                try:
                    health = self.validator_probe()
                except Exception as exc:
                    health = {'passed': False, 'error': type(exc).__name__}
                evidence['checks']['validator_health'] = health
                evidence['passed'] = bool(evidence['passed'] and isinstance(health, dict)
                                          and health.get('passed') is True)
            with self.lock, self.db:
                was_healthy = bool(self.db.execute(
                    'SELECT healthy FROM gates WHERE name=?', (gate,)).fetchone()[0])
                evidence['gate'] = gate
                evidence['actor'] = actor
                if evidence['passed'] and not was_healthy:
                    self._set_gate(gate, 1, actor, 'deterministic_verification_passed', evidence)
                elif not evidence['passed']:
                    self._audit(gate, 'verification_failed', actor,
                                'deterministic_verification_failed', evidence)
            return evidence
        finally:
            if lease is not None:
                lease.close()
            self.job_lock.release()

    def checkout(self, product, attested, source='direct', request_id=None, client=None,
                 buyer_type='business', terms_accepted=True, immediate_performance_requested=False,
                 cancellation_acknowledged=False):
        # One provider intent per caller retry capability, including response loss.
        with self.checkout_lock:
            return self._checkout(product, attested, source, request_id, client, buyer_type,
                                  terms_accepted, immediate_performance_requested, cancellation_acknowledged)

    def _admit_checkout_client(self, client):
        """Per-client new-checkout rate limit. Caller holds lock and transaction.

        `client` is an opaque network identity from the HTTP edge; only its
        hash is stored and rows age out after the window. Rejection never
        closes a gate, so a flood cannot pause sales for legitimate buyers.
        """
        if client is None:
            return
        window = self.policy['checkout_rate_window_seconds']
        now = self.clock()
        self.db.execute('DELETE FROM checkout_rate WHERE at<=?', (now-window,))
        key = digest('checkout-client:'+str(client))
        recent = self.db.execute('SELECT count(*) FROM checkout_rate WHERE client=? AND at>?',
                                 (key, now-window)).fetchone()[0]
        if recent >= self.policy['checkout_rate_limit_per_client']:
            raise Rejected('checkout_rate_limited_retry_later')
        self.db.execute('INSERT INTO checkout_rate(client,at) VALUES (?,?)', (key, now))

    def _checkout(self, product, attested, source, request_id, client=None, buyer_type='business',
                  terms_accepted=True, immediate_performance_requested=False,
                  cancellation_acknowledged=False):
        if attested is not True or not isinstance(product, str) or not product or len(product) > 80:
            raise Rejected('representative_test_corpus_and_one_product_required')
        if source not in ('direct', 'search_exact_xrechnung', 'sample'):
            raise Rejected('unknown_attribution_code')
        if buyer_type not in ('business', 'consumer') or terms_accepted is not True:
            raise Rejected('buyer_type_and_terms_acceptance_required')
        if buyer_type == 'consumer' and (immediate_performance_requested is not True
                                          or cancellation_acknowledged is not True):
            raise Rejected('consumer_immediate_performance_acknowledgement_required')
        if request_id is not None and (not isinstance(request_id, str) or not 32 <= len(request_id) <= 128
                or not _CHECKOUT_KEY_SHAPE.fullmatch(request_id)):
            raise Rejected('invalid_checkout_idempotency_key')
        key_hash = digest(request_id) if request_id else None
        payload_hash = digest(json.dumps([product, attested, source, buyer_type, terms_accepted,
                                         immediate_performance_requested, cancellation_acknowledged]))
        cached = self.db.execute('SELECT * FROM checkouts WHERE key_hash=?', (key_hash,)).fetchone()
        if cached and cached['payload_hash'] != payload_hash:
            raise Rejected('idempotency_key_payload_conflict')
        # The caller retains a random capability. Store only its hash; derive
        # distinct access/recovery capabilities without persisting bearer
        # tokens. A client-chosen key is a public header value, so derivation
        # is keyed by the service secret with a domain-separated handle —
        # the same pattern _success_tokens uses (A1-01). Deterministic, so
        # same-key retries keep returning the same working tokens.
        if request_id:
            access = hmac.new(self.secret.encode(), ('checkout-access:'+request_id).encode(),
                              hashlib.sha256).hexdigest()
            recovery = hmac.new(self.secret.encode(), ('checkout-recovery:'+request_id).encode(),
                                hashlib.sha256).hexdigest()
        else:
            access, recovery = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        if cached:
            oid = cached['order_id']
            order = self.get(oid)
            if order['payment'] == 'expired':
                raise Rejected('checkout_expired_use_new_key')
            # A recover() rotation revoked every capability this key derives
            # (A1-05): refuse instead of returning credentials auth rejects.
            # The caller holds the original key and payload (hence the cached
            # row), so the refusal may point at their recovery token.
            if not hmac.compare_digest(order['access_hash'], digest(access)):
                raise Rejected('checkout_recovered_use_recovery_token')
        else:
            oid = self.create_checkout_order(product, source, access, recovery, key_hash, payload_hash, client,
                                             buyer_type, terms_accepted, immediate_performance_requested,
                                             cancellation_acknowledged)
        try:
            order = self.get(oid)
            session = self.provider.session(order['session']) if order['session'] else self.provider.checkout(oid)
            if session is None:
                raise PaymentError('checkout_absent')
            if not self._session_era_ok(session):
                raise PaymentError('sandbox_session_required')
            with self.lock, self.db:
                order = self.get(oid)
                self.validate_checkout(dict(order, session=session['id']), session)
                order['session'] = session['id']
                self.save(order)
        except Exception:
            with self.lock, self.db:
                self.event('checkout_provider_error', oid)
                self._set_gate('acquisition', 0, 'system', 'checkout_provider_error', {'order_id': oid})
            raise Rejected('checkout_unavailable') from None
        # A recover() rotation can land between the cached-branch check above
        # and here: the provider call runs outside the service lock. Re-verify
        # the stored access hash under the lock before responding (R-02), so
        # a replay is never handed capabilities a rotation just revoked.
        with self.lock, self.db:
            if not hmac.compare_digest(self.get(oid)['access_hash'], digest(access)):
                raise Rejected('checkout_recovered_use_recovery_token')
        return {'order_id': oid, 'access_token': access, 'recovery_token': recovery,
                'checkout_url': session['url'], 'session_id': session['id'], 'simulated': self.provider.simulated}

    def create_checkout_order(self, product, source, access, recovery, key_hash, payload_hash, client=None,
                              buyer_type='business', terms_accepted=True,
                              immediate_performance_requested=False, cancellation_acknowledged=False):
        with self.lock, self.db:
            if self.db.execute('SELECT COUNT(*) FROM gates WHERE healthy=0').fetchone()[0]:
                raise Rejected('new_purchases_paused')
            orders = [json.loads(r[0]) for r in self.db.execute('SELECT data FROM orders')]
            # Unpaid checkouts hold a bounded, short-lived slice of capacity:
            # they can never occupy the paid capacity, and each expires with
            # its provider session (reconcile_reservations releases it).
            unpaid = sum(o['payment'] == 'unpaid' and o.get('reservation', 'held') not in ('released', 'spent')
                         for o in orders)
            if unpaid >= self.policy['max_unpaid_checkouts']:
                raise Rejected('checkout_capacity_busy_retry_later')
            self._admit_checkout_client(client)
            count = sum(o.get('reservation', 'held') not in ('released', 'spent') for o in orders)
            reserved = sum(o['compute_reserve_pence'] for o in orders if o.get('reservation') != 'released')
            reserve = self.policy['simulated_compute_reserve_pence_per_order']
            if count >= self.policy['max_orders'] or reserved+reserve > self.policy['simulated_compute_budget_pence']:
                raise Rejected('spending_gate')
            oid = uuid.uuid4().hex
            order = {'id': oid, 'product_hash': digest(product), 'access_hash': digest(access),
                'recovery_hash': digest(recovery), 'session': None, 'payment_intent': None,
                 'payment': 'unpaid', 'refund': 'none', 'refund_id': None,
                 'buyer_type': buyer_type, 'terms_accepted': terms_accepted,
                 'immediate_performance_requested': immediate_performance_requested,
                 'cancellation_acknowledged': cancellation_acknowledged,
                'job': 'awaiting_payment', 'attempts': 0, 'runs': 0, 'first_delivery': None,
                'created': self.clock(), 'pair': worker.PAIR, 'fixture_ids_hash': None,
                'paired': None, 'error': None, 'source': source,
                'compute_reserve_pence': reserve, 'reservation': 'held',
                'reservation_expires': self.clock()+self.policy['unpaid_checkout_ttl_seconds'],
                'obligation_expires': self.clock()+7*86400,
                'next_action': 'reconcile_reservations', 'retry_at': 0}
            self.save(order)
            if key_hash:
                self.db.execute('INSERT INTO checkouts VALUES (?,?,?)', (key_hash, payload_hash, oid))
            self.event('corpus_confirmed', oid, source=source)
            self.event('checkout_started', oid, price_pence=29900, currency='gbp', simulated=self.provider.simulated)
            self.event('checkout_agreement', oid, buyer_type=buyer_type, terms_accepted=terms_accepted,
                       immediate_performance_requested=immediate_performance_requested,
                       cancellation_acknowledged=cancellation_acknowledged)
        return oid

    def webhook(self, raw, signature):
        event = verify_event(raw, signature, self.secrets, self.clock(), livemode=self.allowed_livemodes)
        with self.lock, self.db:
            if self.db.execute('SELECT 1 FROM webhooks WHERE id=?', (event['id'],)).fetchone():
                return {'duplicate': True}
            kind = event.get('type', '')
            obj = event.get('data', {}).get('object', {})
            if kind in ('checkout.session.completed', 'checkout.session.async_payment_succeeded',
                        'checkout.session.async_payment_failed', 'checkout.session.expired'):
                # Never trust browser redirect or stale event payload for current payment state.
                session = self.provider.session(obj['id'])
                if session is None:
                    raise PaymentError('checkout_absent')
                oid = session.get('metadata', {}).get('order_id')
                order = self.get(oid)
                if order['session'] is None:
                    self.validate_checkout(dict(order, session=session['id']), session)
                    order['session'] = session['id']
                    self.save(order)
                self.validate_checkout(order, session)
                if session.get('payment_status') == 'paid' and order['payment'] != 'paid':
                    pi = session.get('payment_intent')
                    if not isinstance(pi, str) or not pi.startswith('pi_'):
                        raise Rejected('missing_payment_intent')
                    expired = order['payment'] == 'expired'
                    order['payment'], order['payment_intent'] = 'paid', pi
                    order.setdefault('paid_at', self.clock())
                    if expired:
                        order.update(refund='requested', reservation='held', next_action='reconcile_reservations', retry_at=0)
                    order['job'] = 'awaiting_inputs' if order['refund'] == 'none' else 'refund_pending'
                    self.save(order)
                    self.event('payment_verified', oid, gross_pence=29900, tax_pence=0,
                               currency='gbp', simulated=self.provider.simulated)
                    try:
                        # A dispute/refund can reach the provider before this
                        # session event is processed (live Slot A trace,
                        # 21 Sep 2026): converge on provider money state now.
                        self.sync_payment_state(oid)
                    except PaymentError:
                        self.event('payment_state_deferred', oid)
                else:
                    self.event('payment_state_observed', oid, state=session.get('payment_status'))
            elif kind in ('refund.created', 'refund.updated', 'refund.failed'):
                refund = self.provider.refund_status(obj['id'])
                if refund.get('metadata', {}).get('order_id'):
                    self.record_refund(refund)
                else:
                    # Refund created outside the app (e.g. dashboard): converge
                    # on the provider's full money state for that PaymentIntent.
                    self._sync_by_payment_intent(refund.get('payment_intent'))
            elif kind in ('charge.refunded', 'charge.refund.updated', 'charge.dispute.created',
                          'charge.dispute.updated', 'charge.dispute.closed',
                          'charge.dispute.funds_withdrawn', 'charge.dispute.funds_reinstated'):
                # Never trust the event payload: re-read the provider state.
                pi = obj.get('payment_intent')
                if not pi and isinstance(obj.get('charge'), str):
                    # Dispute objects may reference only the charge.
                    pi = self.provider.payment_intent_for_charge(obj['charge'])
                self._sync_by_payment_intent(pi)
            self.db.execute('INSERT INTO webhooks VALUES (?)', (event['id'],))
            self.event('webhook_processed', event_type=kind, simulated=self.provider.simulated)
        return {'accepted': True}

    def status(self, oid, token):
        with self.lock:
            order = self.auth(oid, token)
            return self.order_status(order)

    @staticmethod
    def order_status(order):
        return {k: order[k] for k in ('id', 'payment', 'refund', 'job', 'runs', 'attempts', 'error', 'pair', 'first_delivery')}

    def finish_submission(self, order, request_id):
        # Caller owns the transaction containing the order and report writes.
        result = self.order_status(order)
        if request_id is not None:
            self.db.execute('UPDATE submissions SET result=? WHERE order_id=? AND key=?',
                (json.dumps(result), order['id'], request_id))
        return result

    def recover(self, oid, recovery):
        with self.lock, self.db:
            order = self.auth(oid, recovery, True)
            access, new_recovery = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            order['access_hash'], order['recovery_hash'] = digest(access), digest(new_recovery)
            # Rotation must also revoke everything the checkout-success page
            # can display: a leaked session id must not resurrect old tokens.
            order['success_hashes'] = {'session': (order.get('success_hashes') or {}).get('session'),
                                       'revoked': True}
            self.save(order)
            self.event('access_recovered', oid)
            return {'access_token': access, 'recovery_token': new_recovery}

    def _success_tokens(self, session_id):
        """Session-bound display capabilities for the checkout-success page.

        Derived with the same keyed-SHA-256 pattern the idempotent checkout
        path uses for its capabilities, over a domain-separated handle, so no
        raw bearer token is ever persisted — only hashes are stored on the
        order. The service secret keeps them unguessable from the session id
        alone (which the buyer's browser history retains).
        """
        access = hmac.new(self.secret.encode(), ('success-access:'+session_id).encode(),
                          hashlib.sha256).hexdigest()
        recovery = hmac.new(self.secret.encode(), ('success-recovery:'+session_id).encode(),
                            hashlib.sha256).hexdigest()
        return access, recovery

    def success_view(self, session_id):
        """Buyer self-serve token delivery bound to the verified Checkout
        success session (owner decision D.4).

        The session is re-read from the provider — never trusted from the URL
        — and fully re-validated with the existing checkout validation (order
        binding, amount, currency, mode, livemode) plus payment_status ==
        'paid'. Re-display is bounded: while the provider still returns the
        session, it is not expired, and the order is neither refunded nor
        expired. Unknown, unpaid, refunded, revoked and mismatched lookups are
        all indistinguishable; raw errors are never reflected.
        """
        unavailable = {'kind': 'unavailable'}
        # Same shape rules the adapter enforces, bounded in length so no
        # oversized parameter ever reaches the provider.
        if (not isinstance(session_id, str) or not 8 <= len(session_id) <= 128
                or not any(session_id.startswith(prefix) for prefix in self.session_prefixes)
                or not _SESSION_ID_SHAPE.fullmatch(session_id)):
            return unavailable
        try:
            session = self.provider.session(session_id)
        except PaymentError:
            return unavailable
        if session is None or session.get('id') != session_id:
            return unavailable
        oid = session.get('metadata', {}).get('order_id')
        if not isinstance(oid, str):
            return unavailable
        try:
            with self.lock, self.db:
                order = self.get(oid)
                # Substitute only for the not-yet-bound race (as the webhook
                # path does): a session id that does not match the order's
                # already-bound session must fail validation.
                observed = order if order['session'] else dict(order, session=session_id)
                self.validate_checkout(observed, session)
                if session.get('payment_status') != 'paid':
                    return unavailable
                if order['refund'] != 'none' or order.get('refunded_pence') \
                        or order['payment'] == 'expired':
                    return unavailable
                if (order.get('success_hashes') or {}).get('revoked'):
                    return unavailable
                expires = session.get('expires_at')
                if session.get('status') == 'expired' or (
                        isinstance(expires, (int, float)) and self.clock() > expires):
                    # Bounded re-display window closed: honest banner with the
                    # order id and the POST /recover fallback, nothing more.
                    return {'kind': 'window_closed', 'order_id': oid}
                access, recovery = self._success_tokens(session_id)
                if (order.get('success_hashes') or {}).get('session') != session_id:
                    order['success_hashes'] = {'session': session_id,
                                               'access': digest(access), 'recovery': digest(recovery)}
                    self.save(order)
                self.event('tokens_presented', oid)
                return {'kind': 'tokens', 'order_id': oid,
                        'access_token': access, 'recovery_token': recovery}
        except (Rejected, PaymentError):
            return unavailable

    def submit_admission_open(self, order):
        """R3-01: durable-admission mirror for the wide submit body window.

        service.submit refuses an order at admission — before the payload is
        decoded — unless every durable state check mirrored here holds:
        verified payment, no refund (requested, succeeded or partial
        external), no active or lost dispute, reservation not spent, the
        included rerun still available (runs and delivery window) and
        submission attempts remaining. pilot/server.py grants the wide
        (SUBMIT_BODY_WINDOW_SECONDS) submit-body read only when this returns
        True, so an order submit would refuse anyway can never hold dispatch
        slots behind the wide window. submit's own admission checks — and
        their error precedence — are unchanged; transient refusals (a job
        currently running, raw-input cleanup pending, a paused safety gate)
        are deliberately not mirrored: they clear on their own and gate no
        durable credential.
        """
        return (order['payment'] == 'paid' and order['refund'] == 'none'
                and order.get('reservation') != 'spent'
                and order.get('dispute') not in ACTIVE_DISPUTE
                and not order.get('refunded_pence', 0)
                and order['runs'] < 2
                and (order['first_delivery'] is None
                     or self.clock() <= order['first_delivery'] + self.policy['rerun_days']*86400)
                and order['attempts'] < self.policy['max_submission_attempts'])

    def submit(self, oid, token, payload, executor=worker.execute_job, request_id=None):
        # Explicit retry identity separates a network retry from the included rerun.
        # Direct internal callers may omit it; HTTP requires a caller-generated key.
        if request_id is not None:
            if not isinstance(request_id, str) or not 1 <= len(request_id) <= 80 or not _SUBMIT_KEY_SHAPE.fullmatch(request_id):
                raise Rejected('invalid_idempotency_key')
            payload_hash = digest(json.dumps(payload, sort_keys=True, separators=(',', ':')))
            with self.lock:
                self.auth(oid, token)
                cached = self.db.execute('SELECT payload_hash,result FROM submissions WHERE order_id=? AND key=?', (oid,request_id)).fetchone()
                if cached:
                    if cached['payload_hash'] != payload_hash:
                        raise Rejected('idempotency_key_payload_conflict')
                    if cached['result'] is None:
                        raise Rejected('submission_in_progress_or_interrupted_check_status')
                    result = json.loads(cached['result'])
                    if 'rejected' in result:
                        raise Rejected(result['rejected'])
                    return result
        if not self.job_lock.acquire(blocking=False):
            raise Rejected('worker_busy_retry_later')
        lease = None
        if hasattr(self.db, 'claim_job'):
            try:
                lease = self.db.claim_job()
            except Exception:
                self.job_lock.release()
                raise Rejected('worker_lease_unavailable') from None
            if lease is None:
                self.job_lock.release()
                raise Rejected('worker_busy_retry_later')
        started = time.monotonic()
        started_job = False
        try:
            with self.lock, self.db:
                order = self.auth(oid, token)
                # Another request may have finished between the first lookup
                # and acquiring the worker lock.
                if request_id is not None:
                    cached = self.db.execute('SELECT payload_hash,result FROM submissions WHERE order_id=? AND key=?',
                        (oid, request_id)).fetchone()
                    if cached:
                        if cached['payload_hash'] != payload_hash:
                            raise Rejected('idempotency_key_payload_conflict')
                        if cached['result'] is None:
                            raise Rejected('submission_in_progress_or_interrupted_check_status')
                        result = json.loads(cached['result'])
                        if 'rejected' in result:
                            raise Rejected(result['rejected'])
                        return result
                if (order['payment'] != 'paid' or order['refund'] != 'none' or order.get('reservation') == 'spent'
                        or order.get('dispute') in ACTIVE_DISPUTE or order.get('refunded_pence', 0)):
                    raise Rejected('verified_payment_required_or_refund_active')
                if order['job'] == 'running':
                    raise Rejected('submission_in_progress_or_interrupted_check_status')
                if order.get('raw_job'):
                    raise Rejected('raw_input_cleanup_pending')
                # Acquisition/spend pauses never cancel an existing paid obligation.
                if self.db.execute("SELECT healthy FROM gates WHERE name='safety'").fetchone()[0] != 1:
                    raise Rejected('processing_paused_refund_available')
                if order['runs'] >= 2 or (order['first_delivery'] is not None and
                        self.clock() > order['first_delivery']+self.policy['rerun_days']*86400):
                    raise Rejected('included_rerun_exhausted_or_expired')
                if order['attempts'] >= self.policy['max_submission_attempts']:
                    raise Rejected('input_attempt_limit_refund_available')
                order['attempts'] += 1
                self.save(order)
                if request_id is not None:
                    self.db.execute('INSERT INTO submissions VALUES (?,?,?,NULL)', (oid,request_id,payload_hash))
                self.event('submission_received', oid)
            try:
                decoded = worker.decode_submission(payload, self.policy)
            except (xreg.InputError, TypeError, ValueError) as exc:
                with self.lock, self.db:
                    order = self.get(oid)
                    order['error'] = str(exc) if isinstance(exc, xreg.InputError) else 'invalid_payload'
                    self.save(order)
                    self.event('input_rejected', oid, code=order['error'])
                raise Rejected(order['error']) from None
            identity = digest(json.dumps(sorted(r['id'] for r in decoded)))
            paired = 'new' in decoded[0]
            with self.lock, self.db:
                # Decode runs outside the transaction. A refund or safety pause
                # may have committed while it ran, so never persist this stale
                # pre-decode order or start a job against a refunded payment.
                order = self.get(oid)
                if (order['payment'] != 'paid' or order['refund'] != 'none'
                        or order.get('reservation') == 'spent' or order.get('dispute') in ACTIVE_DISPUTE
                        or order.get('refunded_pence', 0)):
                    raise Rejected('verified_payment_required_or_refund_active')
                if order.get('raw_job'):
                    raise Rejected('raw_input_cleanup_pending')
                if self.db.execute("SELECT healthy FROM gates WHERE name='safety'").fetchone()[0] != 1:
                    raise Rejected('processing_paused_refund_available')
                if order['runs'] >= 2 or (order['first_delivery'] is not None and
                        self.clock() > order['first_delivery']+self.policy['rerun_days']*86400):
                    raise Rejected('included_rerun_exhausted_or_expired')
                if order['fixture_ids_hash'] not in (None, identity) or order['paired'] not in (None, paired):
                    raise Rejected('rerun_requires_same_fixture_ids_and_comparison')
                order.update(fixture_ids_hash=identity, paired=paired, job='running', error=None,
                             job_deadline=self.clock()+self.policy['job_timeout_seconds'])
                self.save(order)
                self.event('job_started', oid, fixture_count=len(decoded), paired=paired)
                started_job = True
            try:
                pack, metrics = self.execute_with_raw_storage(oid, decoded, executor)
                if type(metrics['exit_code']) is not int or metrics['exit_code'] not in (0, 1, 2, 3):
                    raise Rejected('unknown_execution_exit_code')
                if set(pack) != {'results.json', 'junit.xml', 'report.html'}:
                    raise Rejected('incomplete_evidence_pack')
                if not metrics['temporary_inputs_and_intermediates_removed']:
                    self.pause('safety', 'system', 'scratch_cleanup_failed', {'order_id': oid})
                    raise Rejected('cleanup_failed')
            except Exception:
                with self.lock, self.db:
                    order = self.get(oid)
                    order['job'], order['error'] = 'failed', 'execution_failed_resubmit_or_refund'
                    order.pop('job_deadline', None)
                    self.save(order)
                    self._set_gate('delivery', 0, 'system', 'execution_failed', {'order_id': oid})
                    if any((self.state/'temporary').iterdir()):
                        self._set_gate('safety', 0, 'system', 'scratch_not_clean', {'order_id': oid})
                    self.event('job_failed', oid, wall_seconds=time.monotonic()-started,
                               temporary_directory_empty=not any((self.state/'temporary').iterdir()))
                    result = self.finish_submission(order, request_id)
                return result
            finally:
                # Drop references; this is not a claim of cryptographic RAM erasure.
                decoded = None
            with self.lock, self.db:
                order = self.get(oid)
                order.pop('job_deadline', None)
                order['job'] = 'failed' if metrics['exit_code'] == 2 else (
                    'delivered_incomplete' if metrics['exit_code'] == 3 else 'delivered')
                if metrics['exit_code'] == 2:
                    self._set_gate('delivery', 0, 'system', 'validator_incomplete_exit', {'order_id': oid})
                if metrics['exit_code'] != 2:
                    order['runs'] += 1
                    if order['first_delivery'] is None:
                        order['first_delivery'] = self.clock()
                run = order['runs'] if metrics['exit_code'] != 2 else -order['attempts']
                for name, data in pack.items():
                    self.db.execute('INSERT OR REPLACE INTO reports VALUES (?,?,?,?,?)',
                        (oid, run, name, data, self.clock()+self.policy['report_retention_days']*86400))
                self.save(order)
                self.event('evidence_ready', oid, run=run, state=order['job'], metrics=metrics)
                self.event('validation_compute', oid, wall_seconds=metrics['wall_seconds'],
                    cash_gbp=0, cash_basis='local_existing_capacity', cpu_seconds=None,
                    allocated_subscription_gbp=None, model_tokens=0, api_equivalent_usd=0)
                result = self.finish_submission(order, request_id)
            return result
        except Exception as exc:
            # A report/event persistence exception rolls back the whole final
            # transaction. Persist failure using the last committed run count.
            with self.lock, self.db:
                order = self.get(oid)
                running = order['job'] == 'running'
                if not running or not started_job:
                    if request_id is not None:
                        self.db.execute('UPDATE submissions SET result=? WHERE order_id=? AND key=? AND result IS NULL',
                            (json.dumps({'rejected': str(exc) if isinstance(exc, Rejected) else 'submission_failed'}), oid, request_id))
                else:
                    order.update(job='failed', error='persistence_failed_resubmit_or_refund')
                    order.pop('job_deadline', None)
                    self.save(order)
                    self._set_gate('delivery', 0, 'system', 'persistence_failed', {'order_id': oid})
                    result = self.finish_submission(order, request_id)
            if not running or not started_job:
                raise
            return result
        finally:
            if lease is not None:
                lease.close()
            self.job_lock.release()
            # Submit completion is the first safe point for a refund requested
            # during validation. The reservation sweep is the recovery backstop.
            if started_job:
                with self.lock:
                    order = self.get(oid)
                    pending_refund = order['refund'] == 'requested' and order['job'] != 'running'
                if pending_refund:
                    self.reconcile_refund(oid)

    def report(self, oid, token, name, run=None):
        if name not in ('results.json', 'junit.xml', 'report.html'):
            raise Rejected('unknown_report')
        with self.lock, self.db:
            self.auth(oid, token)
            self.purge_reports()
            if run is None:
                # Delivered runs are positive; failed-attempt packs are -attempts.
                # A later failed attempt must never mask delivered evidence.
                row = self.db.execute('SELECT content FROM reports WHERE order_id=? AND name=? '
                    'ORDER BY CASE WHEN run>0 THEN run ELSE 0 END DESC, ABS(run) DESC LIMIT 1', (oid, name)).fetchone()
            else:
                row = self.db.execute('SELECT content FROM reports WHERE order_id=? AND name=? AND run=?', (oid, name, run)).fetchone()
            if not row:
                raise Rejected('report_not_available_or_expired')
            self.event('report_downloaded', oid, format=name)
            return row['content']

    def purge_reports(self):
        # Only tool-created report rows. No path to caller originals exists.
        self.db.execute('DELETE FROM reports WHERE expires<=?', (self.clock(),))

    def operating_state(self, after='', limit=100):
        """Paginated allowlist for a manager connector; no raw evidence/errors."""
        if not isinstance(after, str) or type(limit) is not int or not 1 <= limit <= 100:
            raise Rejected('invalid_state_page')
        with self.lock:
            rows = self.db.execute('SELECT data FROM orders WHERE id>? ORDER BY id LIMIT ?', (after, limit)).fetchall()
            fields = ('id', 'payment', 'refund', 'job', 'job_deadline', 'reservation', 'next_action', 'retry_at',
                      'error', 'attempts', 'runs', 'dispute')
            orders = [{k: json.loads(r[0]).get(k) for k in fields} for r in rows]
            return {'snapshot': self.operator_snapshot(), 'orders': orders,
                    'next_cursor': orders[-1]['id'] if len(orders) == limit else None}

    # --- Read-only, bounded operator views. No report/fixture/customer content,
    # no tokens or hashes; codes and identifiers only. Every view is a page.

    @staticmethod
    def _page_args(after_id, limit):
        if type(after_id) is not int or after_id < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise Rejected('invalid_page')

    def operator_audit(self, after_id=0, limit=100):
        self._page_args(after_id, limit)
        with self.lock:
            rows = self.db.execute('SELECT id,at,gate,action,actor,reason,evidence FROM gate_audit '
                                   'WHERE id>? ORDER BY id LIMIT ?', (after_id, limit)).fetchall()
        items = [{'id': r['id'], 'at': r['at'], 'gate': r['gate'], 'action': r['action'], 'actor': r['actor'],
                  'reason': r['reason'], 'evidence': (r['evidence'] or '')[:2000]} for r in rows]
        return {'items': items, 'next_after_id': items[-1]['id'] if len(items) == limit else None}

    def operator_events(self, after_id=0, limit=100):
        self._page_args(after_id, limit)
        with self.lock:
            rows = self.db.execute('SELECT id,at,kind,order_id,data FROM events WHERE id>? ORDER BY id LIMIT ?',
                                   (after_id, limit)).fetchall()
        items = []
        for r in rows:
            data = json.loads(r['data'] or '{}')
            code = next((data[k] for k in ('code', 'reason', 'status', 'state', 'gate', 'event_type')
                         if isinstance(data.get(k), str)), None)
            items.append({'id': r['id'], 'at': r['at'], 'kind': r['kind'], 'order_id': r['order_id'],
                          'code': code[:120] if code else None})
        return {'items': items, 'next_after_id': items[-1]['id'] if len(items) == limit else None}

    def _order_page(self, after, limit, keep, fields):
        if not isinstance(after, str) or len(after) > 32 or type(limit) is not int or not 1 <= limit <= 100:
            raise Rejected('invalid_page')
        with self.lock:
            orders = [json.loads(r[0]) for r in
                      self.db.execute('SELECT data FROM orders WHERE id>? ORDER BY id', (after,)).fetchall()]
        items = [{k: o.get(k) for k in fields} for o in orders if keep(o)][:limit]
        return {'items': items, 'next_after': items[-1]['id'] if len(items) == limit else None}

    def operator_errors(self, after='', limit=100):
        return self._order_page(after, limit,
            lambda o: o.get('error') or o['job'] in ('failed', 'delivered_incomplete'),
            ('id', 'job', 'error', 'attempts', 'runs', 'payment', 'refund'))

    def operator_reconciliation(self, after='', limit=100):
        page = self._order_page(after, limit,
            lambda o: o['payment'] in ('paid', 'provider_absent') or o.get('reservation') == 'reconciliation_failed',
            ('id', 'payment', 'refund', 'refunded_pence', 'dispute', 'reservation', 'next_action', 'retry_at',
             'refund_reissues'))
        with self.lock:
            orders = [json.loads(r[0]) for r in self.db.execute('SELECT data FROM orders')]
        page['summary'] = {
            'reconciliation_failed': sum(o.get('reservation') == 'reconciliation_failed' for o in orders),
            'refunds_in_flight': sum(o['refund'] not in ('none', 'succeeded') for o in orders),
            'active_disputes': sum(o.get('dispute') in ACTIVE_DISPUTE for o in orders),
            'partial_refunds': sum(0 < o.get('refunded_pence', 0) < PRICE_PENCE for o in orders)}
        return page

    def operator_recovery(self):
        with self.lock:
            orders = [json.loads(r[0]) for r in self.db.execute('SELECT data FROM orders')]
            pending = self.db.execute('SELECT count(*) FROM submissions WHERE result IS NULL').fetchone()[0]
            maintenance = {r['code']: r['status'] for r in self.db.execute('SELECT code,status FROM maintenance')}
        now = self.clock()
        return {'raw_cleanup_pending': [o['id'] for o in orders if o.get('raw_job')][:100],
                'running_jobs': [{'id': o['id'], 'past_deadline': o.get('job_deadline', 0) <= now}
                                 for o in orders if o['job'] == 'running'][:100],
                'submissions_without_result': pending,
                'reservation_retry_scheduled': [{'id': o['id'], 'retry_at': o.get('retry_at')} for o in orders
                                                if o.get('reservation') == 'reconciliation_failed'][:100],
                'refunds_reissue_limited': [o['id'] for o in orders if o['refund'] in ('failed', 'canceled')
                                            and o.get('refund_reissues', 0) >= self.policy['max_refund_reissues']][:100],
                'maintenance': maintenance,
                'recoverable_by_operator': 'resolve_order'}

    def reconcile_ledger(self, after='', limit=100, only=None):
        """Compare a bounded page with current provider objects, repairing safe gaps.

        A provider error or contradictory payment remains unresolved and retains
        capacity. This pass never infers payment or refund success from a webhook.
        """
        if not isinstance(after, str) or type(limit) is not int or not 1 <= limit <= 100:
            raise Rejected('invalid_ledger_page')
        if not self.reconciliation_lock.acquire(blocking=False):
            return {'passed': False, 'error': 'reconciliation_running'}
        try:
            with self.lock:
                if only is not None:
                    ids = [self.get(only)['id']]
                    limit = 2  # a single-order pass never has a next page
                else:
                    rows = self.db.execute('SELECT data FROM orders WHERE id>? ORDER BY id LIMIT ?',
                                           (after, limit)).fetchall()
                    ids = [json.loads(row[0])['id'] for row in rows]
                unresolved = []
                repaired = 0
                for oid in ids:
                    order = self.get(oid)
                    try:
                        session = (self.provider.session(order['session']) if order['session'] else
                                   self.provider.find_checkout(oid, order['created']))
                        if session is None:
                            if order['payment'] in ('unpaid', 'expired'):
                                # Provider absence is authoritative for orders
                                # that were never charged; a paid order with no
                                # provider object stays unresolved by design.
                                continue
                            raise PaymentError('missing_paid_checkout')
                        observed = dict(order, session=session['id'])
                        self.validate_checkout(observed, session)
                        with self.db:
                            if order['session'] is None:
                                order['session'] = session['id']
                                self.save(order)
                                repaired += 1
                            if session.get('payment_status') == 'paid':
                                if order['payment'] != 'paid':
                                    expired = order['payment'] == 'expired'
                                    order.update(payment='paid', payment_intent=session['payment_intent'],
                                                 job='refund_pending' if expired else 'awaiting_inputs')
                                    order.setdefault('paid_at', self.clock())
                                    if expired:
                                        order.update(refund='requested', reservation='held',
                                                     next_action='reconcile_reservations', retry_at=0)
                                    self.save(order)
                                    self.event('payment_recovered', oid, simulated=self.provider.simulated)
                                    repaired += 1
                                elif order['payment_intent'] != session['payment_intent']:
                                    raise PaymentError('payment_intent_mismatch')
                            elif order['payment'] == 'paid':
                                raise PaymentError('paid_order_provider_unpaid')
                        order = self.get(oid)
                        if order['payment'] == 'paid':
                            # Provider money state (incl. out-of-band refunds and
                            # disputes) is the source of truth for the ledger.
                            with self.db:
                                if self.sync_payment_state(oid):
                                    repaired += 1
                    except Exception:
                        unresolved.append(oid)
                        with self.db:
                            order = self.get(oid)
                            if order['reservation'] not in ('spent', 'released'):
                                order.update(reservation='reconciliation_failed',
                                             next_action='reconcile_reservations', retry_at=self.clock()+300)
                                self.save(order)
                            self.event('ledger_reconciliation_failed', oid)
                return {'passed': not unresolved, 'checked': len(ids), 'repaired': repaired,
                        'unresolved': unresolved,
                        'next_cursor': ids[-1] if len(ids) == limit else None}
        finally:
            self.reconciliation_lock.release()

    def reconcile_reservations(self):
        """One bounded, restart/replay-safe sweep. No raw inputs are retained.

        Never release a potentially charged intent on timeout alone. Provider
        absence/expiry must be authoritative; uncertainty retains the reserve
        in an explicit failure state with a scheduled machine retry.
        """
        if not self.reconciliation_lock.acquire(blocking=False):
            return {'passed': False, 'error': 'reconciliation_running'}
        if not self.checkout_lock.acquire(blocking=False):
            self.reconciliation_lock.release()
            return {'passed': False, 'error': 'checkout_running'}
        try:
            with self.lock:
                candidates = [json.loads(r[0]) for r in self.db.execute('SELECT data FROM orders')]
                candidates = sorted((o for o in candidates if (o.get('reservation') not in ('released', 'spent')
                    or o['refund'] not in ('none', 'succeeded'))
                    and o.get('retry_at', 0) <= self.clock()), key=lambda o: (o.get('retry_at', 0), o['id']))[:100]
                for candidate in candidates:
                    oid = candidate['id']
                    order = self.get(oid)
                    if order['job'] == 'running':
                        continue  # The supervised worker owns the bounded job timeout.
                    try:
                        failed_here = False
                        if order['payment'] == 'unpaid':
                            if self.clock() < order['reservation_expires']:
                                continue
                            session = self.provider.session(order['session']) if order['session'] else self.provider.find_checkout(oid, order['created'])
                            if session:
                                # Commit recovered binding before observation/expiry.
                                with self.db:
                                    order['session'] = session['id']
                                    self.save(order)
                                session = self.provider.expire_session(session['id'])
                                self.validate_checkout(order, session)
                            if session and session.get('payment_status') == 'paid':
                                order.update(payment='paid', payment_intent=session['payment_intent'],
                                             job='refund_pending', refund='requested')
                            elif session is None or session.get('status') == 'expired':
                                order.update(payment='expired', job='expired', reservation='released',
                                             next_action='none', retry_at=0)
                            else:
                                raise PaymentError('checkout_expiry_unconfirmed')
                            with self.db:
                                self.save(order)
                        if order['payment'] == 'paid':
                            # Backstop: money state (out-of-band or raced
                            # disputes/refunds) converges with no webhook.
                            if self.sync_payment_state(oid):
                                order = self.get(oid)
                            if order['refund'] == 'none':
                                if order['runs']:
                                    if order['runs'] >= 2 or self.clock() >= order['first_delivery']+self.policy['rerun_days']*86400:
                                        order.update(reservation='spent', next_action='none', retry_at=0)
                                elif self.clock() >= order['obligation_expires'] or order['attempts'] >= self.policy['max_submission_attempts']:
                                    order.update(refund='requested', job='refund_pending')
                            with self.db:
                                self.save(order)
                            if order['refund'] != 'none' and order['job'] != 'running':
                                self.reconcile_refund(oid)
                                order = self.get(oid)
                                if order['refund'] == 'succeeded':
                                    # Delivered compute is still accounted for after a refund.
                                    order.update(reservation='spent' if order['attempts'] else 'released',
                                                 job='refunded', next_action='none', retry_at=0)
                                else:
                                    order.update(reservation='reconciliation_failed',
                                                 next_action='reconcile_reservations', retry_at=self.clock()+300)
                                    failed_here = True
                            with self.db:
                                self.save(order)
                        if order['reservation'] not in ('released', 'spent', 'reconciliation_failed'):
                            with self.db:
                                order.update(reservation='held', next_action='reconcile_reservations', retry_at=self.clock()+300)
                                self.save(order)
                        elif order['reservation'] == 'reconciliation_failed' and not failed_here:
                            # A stale failure mark whose sweep now completes
                            # cleanly is a reconciled reservation: hold it for
                            # the next designed transition instead of failing
                            # every future sweep on a superseded failure.
                            with self.db:
                                order.update(reservation='held', next_action='reconcile_reservations',
                                             retry_at=self.clock()+300)
                                self.save(order)
                    except Exception:
                        with self.db:
                            order = self.get(oid)
                            order.update(reservation='reconciliation_failed', next_action='reconcile_reservations', retry_at=self.clock()+300)
                            self.save(order)
                            self.event('reservation_reconciliation_failed', oid)
                with self.db:
                    self.purge_reports()
                unresolved = sum(json.loads(r[0]).get('reservation') == 'reconciliation_failed'
                                 for r in self.db.execute('SELECT data FROM orders'))
                return {'passed': unresolved == 0, 'unresolved': unresolved}
        finally:
            self.checkout_lock.release()
            self.reconciliation_lock.release()

    @staticmethod
    def _id_era(identifier):
        """Era (livemode) implied by an era-marked provider id; None otherwise.

        Only the explicit cs_/pi_/re_/ch_/dp_ + test_/live_ markers count: an
        unmarked id reveals nothing about its era, and callers fall back to the
        service's own single-era expectation, exactly as before dual-era.
        """
        if not isinstance(identifier, str):
            return None
        for kind in ('cs_', 'pi_', 're_', 'ch_', 'dp_'):
            if identifier.startswith(kind+'test_'):
                return False
            if identifier.startswith(kind+'live_'):
                return True
        return None

    def _session_era_ok(self, session):
        """Session-prefix membership plus era consistency.

        A cs_test_ object must be livemode False and a cs_live_ object True;
        cross-era mismatches, unmarked ids and foreign prefixes are refused.
        For a single-era provider this is exactly the previous check that the
        session carry the service's own prefix and livemode.
        """
        sid = session.get('id')
        for prefix in self.session_prefixes:
            if isinstance(sid, str) and sid.startswith(prefix):
                return session.get('livemode') is (prefix != 'cs_test_')
        return False

    def validate_checkout(self, order, session):
        oid = order['id']
        details = session.get('total_details', {})
        if (not self._session_era_ok(session) or session.get('mode') != 'payment'
            or session.get('client_reference_id') != oid or session.get('metadata', {}).get('order_id') != oid
            or session.get('id') != order['session'] or session.get('amount_subtotal') != 29900
            or session.get('amount_total') != 29900 or session.get('currency') != 'gbp'
            or any(details.get(k) != 0 for k in ('amount_tax', 'amount_discount', 'amount_shipping'))):
            raise Rejected('payment_scope_amount_or_currency_mismatch')
        if session.get('payment_status') == 'paid' and (
                not isinstance(session.get('payment_intent'), str) or not session['payment_intent'].startswith('pi_')):
            raise Rejected('missing_payment_intent')

    def _within_refund_window(self, order):
        paid_at = order.get('paid_at') or order['created']
        return self.clock() <= paid_at + self.policy['customer_refund_window_days']*86400

    def _refund_reason(self, order):
        if self._within_refund_window(order):
            return 'customer_request_within_7_days'
        paid_at = order.get('paid_at') or order['created']
        # Until product classification is independently reviewed, honour the
        # full consumer cancellation period even after report delivery.
        if order.get('buyer_type') == 'consumer' and self.clock() <= paid_at + 14*86400:
            return 'consumer_cancellation_within_14_days'
        return None

    def request_refund(self, oid, token):
        with self.lock, self.db:
            order = self.auth(oid, token)
            if order['payment'] != 'paid':
                raise Rejected('no_verified_payment')
            if order['refund'] != 'none':
                return {'refund': order['refund']}
            reason = self._refund_reason(order)
            if reason is None:
                raise Rejected('refund_window_closed')
            order.update(refund='requested', refund_requested_at=self.clock(),
                         refund_reason=reason, next_action='reconcile_reservations', retry_at=0)
            self.save(order)
            self.event('refund_requested', oid, reason=reason)
            if order['job'] == 'running':
                return {'refund': 'requested'}
        return self.reconcile_refund(oid)

    def operator_request_refund(self, oid, reason):
        """Full-value refund under a pre-defined policy reason only.

        There is no amount parameter (always the single £299 order value, which
        is the most that can ever be refunded) and no discretionary reason.
        """
        if reason not in REFUND_REASONS:
            raise Rejected('refund_reason_not_in_policy')
        with self.lock, self.db:
            order = self.get(oid)
            if order['payment'] != 'paid':
                raise Rejected('no_verified_payment')
            if order['refund'] == 'succeeded':
                return {'refund': 'succeeded'}
            if order['refund'] == 'none':
                if reason == 'customer_request_within_7_days':
                    eligible = self._within_refund_window(order)
                elif reason == 'consumer_cancellation_within_14_days':
                    eligible = self._refund_reason(order) == reason
                else:
                    eligible = order['job'] == 'failed'
                if not eligible:
                    raise Rejected('refund_reason_not_eligible')
                was_running = order['job'] == 'running'
                order.update(refund='requested', job='refund_pending',
                             refund_requested_at=self.clock(), refund_reason=reason,
                             next_action='reconcile_reservations', retry_at=0)
                if was_running:
                    order['job'] = 'running'
                self.save(order)
                self.event('operator_refund_requested', oid, reason=reason, amount_pence=PRICE_PENCE)
                if order['job'] == 'running':
                    return {'refund': 'requested'}
        return self.reconcile_refund(oid)

    def _order_for_payment_intent(self, pi):
        if not isinstance(pi, str) or not pi.startswith('pi_'):
            return None
        for row in self.db.execute('SELECT data FROM orders').fetchall():
            order = json.loads(row[0])
            if order.get('payment_intent') == pi:
                return order['id']
        return None

    def _sync_by_payment_intent(self, pi):
        """Caller holds lock + transaction. Unknown intents are logged, never 409'd."""
        oid = self._order_for_payment_intent(pi)
        if oid is None:
            self.event('unmatched_provider_event')
            return None
        return self.sync_payment_state(oid)

    def sync_payment_state(self, oid):
        """Converge one paid order on the provider's money state.

        Reads refunds and disputes for the order's PaymentIntent, including
        those created outside the app. Returns True when local state changed.
        Caller holds lock + transaction.
        """
        order = self.get(oid)
        pi = order['payment_intent']
        state = self.provider.payment_state(pi)
        # The era expected of the money state follows the intent's era marker.
        # An unmarked id (all real Stripe pi_ ids) carries no era: under an
        # era-routing provider the answering client IS the owner, so the
        # returned state's own livemode is authoritative; under a single-era
        # provider the service's own expectation stands, exactly as before.
        era = self._id_era(pi)
        if era is None:
            era = (state.get('livemode') if state is not None else self.livemode) \
                if getattr(self.provider, 'era_routing', False) else self.livemode
        if state is None or state.get('livemode') is not era or state.get('payment_intent') != pi:
            raise PaymentError('payment_state_unavailable')
        refunds = [r for r in state['refunds'] if r.get('payment_intent') == pi]
        if any(r.get('currency') != 'gbp' or type(r.get('amount')) is not int for r in refunds):
            raise Rejected('refund_scope_mismatch')
        refunded = sum(r['amount'] for r in refunds if r.get('status') == 'succeeded')
        if refunded > PRICE_PENCE:
            raise Rejected('refund_exceeds_order_value')
        before = json.dumps(order, sort_keys=True)
        if refunded or 'refunded_pence' in order:
            order['refunded_pence'] = refunded
        full =[r for r in refunds if r['amount'] == PRICE_PENCE]
        if refunded == PRICE_PENCE and order['refund'] != 'succeeded':
            ids = [r['id'] for r in refunds if r.get('status') == 'succeeded']
            order.update(refund='succeeded', refund_id=order['refund_id'] if order['refund_id'] in ids else ids[-1],
                         next_action='reconcile_reservations', retry_at=0)
            if order['job'] != 'running':
                order['job'] = 'refunded'
            self.event('refund_status', oid, status='succeeded', amount_pence=PRICE_PENCE,
                       source='provider_reconciliation')
        elif order['refund'] in ('none', 'requested', 'failed', 'canceled') and not refunded:
            pending = [r for r in full if r.get('status') == 'pending' and r['id'] != order['refund_id']]
            if pending and (order['refund_id'] is None or order['refund'] in ('failed', 'canceled')):
                order.update(refund='pending', refund_id=pending[-1]['id'], job='refund_pending',
                             next_action='reconcile_reservations', retry_at=0)
                self.event('refund_status', oid, status='pending', source='provider_reconciliation')
        if 0 < refunded < PRICE_PENCE and not order.get('partial_refund_observed'):
            order['partial_refund_observed'] = True
            self.event('partial_external_refund', oid, refunded_pence=refunded)
            self._set_gate('acquisition', 0, 'system', 'partial_external_refund', {'order_id': oid})
        disputes = state.get('disputes') or []
        dispute = next((d['status'] for d in disputes if d.get('status') in ACTIVE_DISPUTE),
                       disputes[-1]['status'] if disputes else None)
        if dispute != order.get('dispute'):
            order['dispute'] = dispute
            self.event('dispute_status', oid, status=dispute)
            if dispute in ACTIVE_DISPUTE:
                self._set_gate('acquisition', 0, 'system', 'dispute_observed', {'order_id': oid, 'status': dispute})
        changed = json.dumps(order, sort_keys=True) != before
        if changed:
            self.save(order)
        return changed

    def record_refund(self, refund):
        oid = refund.get('metadata', {}).get('order_id')
        if not oid:
            # Created outside the app: provider state decides, not this object.
            oid = self._order_for_payment_intent(refund.get('payment_intent'))
            if oid is None:
                raise Rejected('refund_scope_mismatch')
            self.sync_payment_state(oid)
            return {'refund': self.get(oid)['refund']}
        order = self.get(oid)
        # A refund's era follows its own id marker, then the linked intent's;
        # an unmarked refund under era routing carries its owner's livemode
        # (the wrapper fetched it from the owning credential), and a
        # single-era service keeps the previous check.
        era = self._id_era(refund.get('id'))
        if era is None:
            era = self._id_era(refund.get('payment_intent'))
        if era is None:
            era = refund.get('livemode') if getattr(self.provider, 'era_routing', False) \
                else self.livemode
        if (refund.get('livemode') is not era or refund.get('amount') != PRICE_PENCE
            or refund.get('currency') != 'gbp' or refund.get('payment_intent') != order['payment_intent']
            or order['refund_id'] not in (None, refund.get('id'))):
            raise Rejected('refund_scope_mismatch')
        status = refund.get('status')
        if status not in ('pending', 'requires_action', 'succeeded', 'failed', 'canceled'):
            raise Rejected('unknown_refund_status')
        # A succeeded refund is terminal, even under provider/event disorder.
        if order['refund'] != 'succeeded':
            order['refund'], order['refund_id'] = status, refund['id']
            self.save(order)
            self.event('refund_status', oid, status=status, amount_pence=PRICE_PENCE)
        if status in ('failed', 'requires_action', 'canceled'):
            self.pause('acquisition', 'system', 'refund_failed',
                       {'order_id': oid, 'refund_status': status})
        return {'refund': order['refund']}

    def reconcile_refund(self, oid):
        with self.lock:
            order = self.get(oid)
            if order['refund'] == 'none':
                raise Rejected('refund_not_requested')
            if order['job'] == 'running':
                return {'refund': 'requested'}
            try:
                refund = (self.provider.refund_status(order['refund_id']) if order['refund_id'] else
                          self.provider.refund(order['payment_intent'], oid, order.get('refund_reissues', 0)))
            except Exception:
                # e.g. the charge was already refunded from the dashboard: the
                # provider's money state resolves it without any DB repair.
                try:
                    with self.db:
                        self.sync_payment_state(oid)
                        if self.get(oid)['refund'] in ('succeeded', 'pending'):
                            return {'refund': self.get(oid)['refund']}
                except Exception:
                    pass
                with self.db:
                    self.event('refund_provider_unavailable', oid)
                return {'refund': order['refund']}
            with self.db:
                return self.record_refund(refund)

    def resolve_order(self, oid, actor='operator'):
        """Bounded recovery for one stuck order, with no shell or DB access.

        Retries raw-object cleanup and stranded-job recovery, re-issues a
        provider-failed refund (bounded), voids a 'paid' order only when the
        provider authoritatively has neither the session nor the payment, then
        re-runs ledger and reservation reconciliation for that order.
        """
        steps = {'raw_and_stranded': self.reconcile_raw_objects()}
        with self.lock:
            order = self.get(oid)
        if order['payment'] == 'paid':
            session = (self.provider.session(order['session']) if order['session'] else
                       self.provider.find_checkout(oid, order['created']))
            state = self.provider.payment_state(order['payment_intent']) if session is None else True
            with self.lock, self.db:
                order = self.get(oid)
                if session is None and state is None and order['job'] != 'running':
                    order.update(payment='provider_absent', job='void', reservation='released',
                                 next_action='none', retry_at=0)
                    self.save(order)
                    self.event('order_voided_provider_absent', oid, actor=str(actor)[:80])
                    steps['voided_provider_absent'] = True
                elif order['refund'] in ('failed', 'canceled'):
                    if order.get('refund_reissues', 0) >= self.policy['max_refund_reissues']:
                        steps['refund_reissue'] = 'limit_reached_owner_required'
                    else:
                        order['refund_reissues'] = order.get('refund_reissues', 0) + 1
                        order.update(refund='requested', refund_id=None, job='refund_pending',
                                     next_action='reconcile_reservations', retry_at=0)
                        self.save(order)
                        self.event('refund_reissued', oid, actor=str(actor)[:80], attempt=order['refund_reissues'])
                        steps['refund_reissue'] = order['refund_reissues']
            if steps.get('refund_reissue') not in (None, 'limit_reached_owner_required'):
                steps['refund'] = self.reconcile_refund(oid)
        if self.get(oid)['payment'] in ('paid', 'unpaid'):
            steps['ledger'] = self.reconcile_ledger(only=oid)
        with self.lock, self.db:
            order = self.get(oid)
            if order.get('reservation') == 'reconciliation_failed' and steps.get('ledger', {}).get('passed', True):
                order.update(reservation='held', retry_at=0)
                self.save(order)
        steps['reservations'] = self.reconcile_reservations()
        with self.db:
            self.event('order_recovery_requested', oid, actor=str(actor)[:80])
        order = self.get(oid)
        return {'order_id': oid, 'steps': steps,
                'order': {k: order.get(k) for k in ('payment', 'refund', 'job', 'reservation', 'dispute',
                                                    'refunded_pence', 'next_action')}}


    def support(self, oid, token, topic):
        started = time.monotonic()
        with self.lock:
            self.auth(oid, token)
        if topic == 'refund':
            response = self.request_refund(oid, token)
        elif topic == 'status':
            response = self.status(oid, token)
        else:
            answers = {
                'access': 'Use your separately retained recovery token at POST /recover to rotate access. Never send invoices to support.',
                'input': 'Use UTF-8 XRechnung test XML, unique opaque IDs, base64 JSON; no archives or paths. See /requirements. Retain your originals.',
                'rerun': 'Resubmit the same fixture IDs, product, pair and output mode within seven days of first evidence delivery. One included rerun.',
                'advice': 'Transformations, legal/tax certification, bespoke rules, corpus design and manual finding adjudication are outside this offer.'}
            if topic not in answers:
                raise Rejected('unsupported_support_topic_no_free_text')
            response = {'answer': answers[topic]}
        with self.lock, self.db:
            self.event('support_resolved', oid, topic=topic, automation_seconds=time.monotonic()-started,
                       model_tokens=0, human_minutes=0)
        return response

    def operator_snapshot(self):
        """Aggregate allowlist ONLY. No identities, product names, fixtures or reports."""
        with self.lock:
            orders = [json.loads(r[0]) for r in self.db.execute('SELECT data FROM orders')]
            return {'orders': len(orders), 'paid': sum(o['payment']=='paid' for o in orders),
                'failed_jobs': sum(o['job']=='failed' for o in orders),
                'pending_refunds': sum(o['refund'] not in ('none','succeeded') for o in orders),
                'gates': dict(self.db.execute('SELECT name,healthy FROM gates')),
                'simulated_reserved_pence': sum(o['compute_reserve_pence'] for o in orders if o.get('reservation') not in ('released', 'spent')),
                'additional_cash_budget_gbp': 0, 'advertising': 'PAUSED',
                'managed_operator': 'UNPROVISIONED',
                'support_count': self.db.execute("SELECT count(*) FROM events WHERE kind='support_resolved'").fetchone()[0]}
