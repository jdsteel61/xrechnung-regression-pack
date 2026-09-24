"""Deterministic standing watchdog for the xreg operator surface.

Layer 1 of the "grok bot" (docs: .cache/prompts/grok-bot-wiring.md): a
no-model scheduler that sweeps the bounded operator HTTP API on a cadence,
raises alerts on anything outside the audited-expected envelope, and never
touches money or gates by itself. Layer 2 (a model agent) remains on-call,
not standing.

Design rules:
- stdlib only, like the rest of the app path;
- reads /health, the operator views (read token) and the `health` action
  (action token); performs NO mutations — the operator surface stays the
  only write path;
- a finding alerts on FIRST sighting; repeats inside DEDUPE_WINDOW become
  digest-only so a persistent condition cannot spam;
- INFO severity never alerts (e.g. acquisition paused is the designed owner
  state, not an incident — but it does mean advertising must stay off);
- `ads_should_pause()` exposes the kill-switch signal the ads manager will
  consume (docs/ads/OPERATING_MODEL.md §4): any ALERT pauses spend;
- sinks: console (default, dry-run) and SMTP (enabled only when the owner
  sets WATCHDOG_SMTP_* secrets — credentials never live in the repo).
"""
from __future__ import annotations

import argparse
import json
import os
import smtplib
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from pathlib import Path

INFO, WARN, ALERT = 'info', 'warn', 'alert'
DEDUPE_WINDOW_SECONDS = 6 * 3600
STATE_TTL_SECONDS = 7 * 24 * 3600


class Finding:
    __slots__ = ('code', 'severity', 'detail')

    def __init__(self, code, severity, detail=''):
        self.code, self.severity, self.detail = code, severity, str(detail)[:200]

    def __repr__(self):
        return f'<{self.severity}:{self.code} {self.detail}>'


class ConsoleSink:
    """Default dry-run sink: everything visible, nothing sent."""

    def emit(self, finding):
        print(f'[{finding.severity}] {finding.code}: {finding.detail}', flush=True)


class SmtpSink:
    """Email alerts to the support mailbox; configured entirely via env."""

    def __init__(self, host, port, user, password, from_addr, to_addr, use_tls=True):
        self.host, self.port, self.user, self.password = host, port, user, password
        self.from_addr, self.to_addr, self.use_tls = from_addr, to_addr, use_tls

    @classmethod
    def from_env(cls, environ=None):
        env = environ if environ is not None else os.environ
        required = ('WATCHDOG_SMTP_HOST', 'WATCHDOG_SMTP_USER', 'WATCHDOG_SMTP_PASSWORD',
                    'WATCHDOG_SMTP_FROM', 'WATCHDOG_SMTP_TO')
        missing = [k for k in required if not env.get(k)]
        if missing:
            raise ValueError('smtp_sink_missing_env:' + ','.join(missing))
        return cls(env['WATCHDOG_SMTP_HOST'], int(env.get('WATCHDOG_SMTP_PORT', '587')),
                   env['WATCHDOG_SMTP_USER'], env['WATCHDOG_SMTP_PASSWORD'],
                   env['WATCHDOG_SMTP_FROM'], env['WATCHDOG_SMTP_TO'])

    def emit(self, finding):
        message = EmailMessage()
        message['Subject'] = f'[xreg-watchdog] {finding.severity.upper()}: {finding.code}'
        message['From'], message['To'] = self.from_addr, self.to_addr
        message.set_content(
            f'xreg watchdog finding\n\nseverity: {finding.severity}\ncode:     {finding.code}\n'
            f'detail:   {finding.detail}\nat:       {time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}\n')
        with smtplib.SMTP(self.host, self.port, timeout=30) as server:
            if self.use_tls:
                server.starttls()
            server.login(self.user, self.password)
            server.send_message(message)


def _request(origin, path, token=None, body=None, timeout=30):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    request = urllib.request.Request(origin.rstrip('/') + path,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers=headers, method='POST' if body is not None else 'GET')
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def sweep(origin, read_token, action_token):
    """One pass over the public health surface; returns findings (no state)."""
    findings = []
    try:
        health = _request(origin, '/health')
        if health.get('status') != 'ok' or health.get('database') != 'reachable':
            findings.append(Finding('health_degraded', ALERT, json.dumps(health, sort_keys=True)))
    except Exception as exc:  # noqa: BLE001 - the watchdog's job is to survive and report
        return [Finding('health_unreachable', ALERT, repr(exc))]

    try:
        state = _request(origin, '/operator/action', action_token,
                         {'name': 'health', 'arguments': {}})
        gates = state.get('gates', {})
        if state.get('database') != 'reachable':
            findings.append(Finding('database_unreachable', ALERT, state.get('database')))
        for gate in ('delivery', 'safety', 'spending'):
            if gates.get(gate) != 1:
                findings.append(Finding(f'gate_{gate}_closed', ALERT, json.dumps(gates, sort_keys=True)))
        if gates.get('acquisition') == 0:
            findings.append(Finding('acquisition_paused', INFO, 'sales paused (owner state); ads stay off'))
        if state.get('pending_refunds'):
            findings.append(Finding('pending_refunds', WARN, f"pending_refunds={state['pending_refunds']}"))
    except Exception as exc:  # noqa: BLE001
        findings.append(Finding('operator_health_action_failed', ALERT, repr(exc)))

    try:
        errors = _request(origin, '/operator/errors', read_token)
        for item in errors.get('items', []):
            findings.append(Finding('error_item', WARN,
                                    f"id={str(item.get('id'))[:8]} job={item.get('job')} error={item.get('error')}"))
    except Exception as exc:  # noqa: BLE001
        findings.append(Finding('errors_view_failed', WARN, repr(exc)))
    return findings


def dedupe(findings, state, now=None):
    """Split findings into (fresh, repeated). Fresh = first sighting or re-seen
    after DEDUPE_WINDOW. Mutates and prunes state['seen']."""
    now = time.time() if now is None else now
    seen = state.setdefault('seen', {})
    fresh, repeated = [], []
    for finding in findings:
        key = finding.code + '|' + finding.detail
        last = seen.get(key)
        if finding.severity == INFO or last is None or now - last >= DEDUPE_WINDOW_SECONDS:
            fresh.append(finding)
        else:
            repeated.append(finding)
        seen[key] = now
    state['seen'] = {key: stamp for key, stamp in seen.items() if now - stamp < STATE_TTL_SECONDS}
    return fresh, repeated


def ads_should_pause(findings):
    """Kill-switch for the ads manager (OPERATING_MODEL §4): any ALERT — or
    the designed paused-sales state — means spend must be off."""
    return any(f.severity == ALERT for f in findings) or any(f.code == 'acquisition_paused' for f in findings)


def run_once(origin, read_token, action_token, state, sink=None):
    """Sweep + dedupe; returns (fresh, repeated). Fresh WARN/ALERT findings
    are emitted to the sink (INFO stays digest-only)."""
    sink = sink or ConsoleSink()
    findings = sweep(origin, read_token, action_token)
    fresh, repeated = dedupe(findings, state)
    for finding in fresh:
        if finding.severity != INFO:
            sink.emit(finding)
    return fresh, repeated


def load_state(path):
    state_file = Path(path)
    if state_file.exists():
        return json.loads(state_file.read_text(encoding='utf-8'))
    return {}


def save_state(path, state):
    Path(path).write_text(json.dumps(state, sort_keys=True), encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description='xreg standing watchdog')
    parser.add_argument('--origin', default=os.environ.get('XREG_ORIGIN', 'https://xregcheck.com'))
    parser.add_argument('--read-token', default=os.environ.get('OPERATOR_READ_TOKEN', ''))
    parser.add_argument('--action-token', default=os.environ.get('OPERATOR_ACTION_TOKEN', ''))
    parser.add_argument('--state', default=os.environ.get('WATCHDOG_STATE_PATH', 'watchdog-state.json'))
    parser.add_argument('--once', action='store_true', help='single sweep (cron mode) and exit')
    parser.add_argument('--loop', type=int, default=0, metavar='SECONDS', help='sweep every SECONDS')
    args = parser.parse_args(argv)
    if not args.read_token or not args.action_token:
        parser.error('operator tokens required (flags or OPERATOR_* env)')
    try:
        sink = SmtpSink.from_env()
    except ValueError:
        sink = ConsoleSink()
    if args.once or not args.loop:
        state = load_state(args.state)
        run_once(args.origin, args.read_token, args.action_token, state, sink)
        save_state(args.state, state)
        return 0
    while True:
        state = load_state(args.state)
        try:
            run_once(args.origin, args.read_token, args.action_token, state, sink)
        except Exception as exc:  # noqa: BLE001 - the loop must never die
            print(f'[watchdog] sweep crashed: {exc!r}', flush=True)
        save_state(args.state, state)
        time.sleep(args.loop)


if __name__ == '__main__':
    raise SystemExit(main())
