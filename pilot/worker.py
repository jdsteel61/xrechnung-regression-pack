"""Bounded worker around the unchanged validated core; no model calls."""
import base64
import contextlib
import html
import io
import json
import os
from pathlib import Path
import re
import site
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

import xreg
from .process import contained_run

ROOT = Path(__file__).resolve().parents[1]
PAIR = 'xrechnung-2025-07-10_to_2026-01-31'
OLD = ROOT/'.deps/validator-configuration-xrechnung-release-2025-07-10'
NEW = ROOT/'.deps/validator-configuration-xrechnung-v2026-01-31'
ENGINE = ROOT/'.deps/validator-v1.6.3'
JAVA = ROOT/'.deps/java/jdk-21.0.8+9-jre/bin/java.exe'  # Windows pin (also used by inventory)
# Linux runtimes are pinned separately: the KoSIT engine/ruleset artefacts are
# byte-identical across OS (see tree_identity), but the Java launcher is not, so
# its digest + version are recorded in a dedicated Linux provenance file.
LINUX_RUNTIME_PROVENANCE = ROOT/'manifests/linux-runtime-provenance.json'


def java_binary():
    """Resolve the pinned Java launcher for this OS.

    Windows uses the vendored `java.exe`; Linux resolves `XREG_JAVA`, then
    `JAVA_HOME/bin/java`, then `PATH`. The chosen binary's sha256 is checked
    against the recorded provenance in `verify_pinned_core`.
    """
    if os.name == 'nt':
        return JAVA
    candidate = os.environ.get('XREG_JAVA')
    if not candidate:
        home = os.environ.get('JAVA_HOME')
        if home and (Path(home)/'bin'/'java').is_file():
            candidate = str(Path(home)/'bin'/'java')
    if not candidate:
        import shutil
        candidate = shutil.which('java')
    if not candidate:
        raise xreg.InputError('linux_java_runtime_not_found')
    return Path(candidate)


def _worker_env(work, *, java_hint=None):
    """Minimal, credential-free environment for a supervised subprocess.

    Never inherits API/provider credentials. On Windows it keeps the historic
    keys; on Linux it points temp at the owned scratch and, when supervising the
    Python job, passes the resolved Java path so the child needs no PATH.
    """
    if os.name == 'nt':
        return {'SystemRoot': os.environ.get('SystemRoot', r'C:\Windows'),
                'TEMP': str(work), 'TMP': str(work), 'LANG': 'C.UTF-8'}
    env = {'TMPDIR': str(work), 'HOME': str(work),
           'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8'}
    if java_hint:
        env['XREG_JAVA'] = str(java_hint)
    return env


def execute_job(decoded, policy, temp_parent):
    """Supervise the complete Python/Java job, including parsing/report rendering.

    The service retains no queued raw input. An interrupted job terminates and
    can be resubmitted with a new execution key or automatically refunded.
    """
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='pilot-job-supervised-', dir=temp_parent) as folder:
        work = Path(folder)
        rows = []
        for index, row in enumerate(decoded):
            record = {'id': row['id']}
            for side in ('old', 'new') if 'new' in row else ('old',):
                filename = f'{index:04d}-{side}.xml'
                (work/filename).write_bytes(row[side])
                record[side] = filename
            rows.append(record)
        (work/'job.json').write_text(json.dumps({'rows': rows, 'policy': policy}), encoding='utf-8')
        (work/'scratch').mkdir()
        job_env = _worker_env(work, java_hint=java_binary())
        if os.name == 'nt':
            job_env['PYTHONUSERBASE'] = site.USER_BASE
        result = contained_run([sys.executable, '-B', '-m', 'pilot.job', str(work)], cwd=ROOT,
            timeout=max(.001, policy['job_timeout_seconds']-(time.monotonic()-started)), scratch=work,
            env=job_env)
        if result.returncode != 0:
            raise OSError('job_process_failed')
        names = ('results.json', 'junit.xml', 'report.html')
        if sum((work/name).stat().st_size for name in names) > 128*1024*1024:
            raise OSError('output_limit')
        pack = {name: (work/name).read_bytes() for name in names}
        metrics = json.loads((work/'metrics.json').read_bytes())
    metrics['wall_seconds'] = time.monotonic()-started
    metrics['temporary_inputs_and_intermediates_removed'] = not work.exists() and not any(Path(temp_parent).iterdir())
    return pack, metrics


def _same_tree(got, expected):
    # tree_identity() sorts native Path objects, so its list order depends on the
    # host OS path semantics (Windows vs POSIX). The *identity* of an artefact tree
    # is the set of (relative-posix-path, sha256) pairs, which is byte-identical
    # across OS; only the serialization order differs. Compare on that canonical
    # set so the validated provenance verifies on both Windows and Linux without
    # rewriting a single stored digest. Every file content is still checked bit for
    # bit — a real drift (added/removed/changed file) still fails.
    key = lambda rows: sorted((r['path'], r['sha256']) for r in rows)
    return key(got) == key(expected)


def verify_pinned_core(java=None):
    java = Path(java) if java else java_binary()
    evidence = json.loads((ROOT/'evidence/gate1/verification-clean/maintenance/results.json').read_bytes())['provenance']
    # Engine + ruleset artefacts must be byte-identical on every OS — this is the
    # validated KoSIT provenance and is never platform-relaxed. The comparison is
    # order-independent (see _same_tree) so the Windows-generated provenance list
    # verifies unchanged on Linux; the (path, sha256) content check is exact.
    if (not _same_tree(xreg.tree_identity(ENGINE), evidence['engine_files']) or
        not _same_tree(xreg.tree_identity(OLD), evidence['bundles']['old_rules']) or
        not _same_tree(xreg.tree_identity(NEW), evidence['bundles']['new_rules'])):
        raise xreg.InputError('validated_dependency_drift')
    # The Java launcher differs per OS; check it against the OS-appropriate pin.
    if os.name == 'nt':
        expected = evidence['java_executable_sha256']
    else:
        pin = json.loads(LINUX_RUNTIME_PROVENANCE.read_text(encoding='utf-8'))
        expected = pin['java_executable_sha256']
    if xreg.sha(java.read_bytes()) != expected:
        raise xreg.InputError('validated_java_runtime_drift')


def public_hashes():
    manifest = json.loads((ROOT/'manifests/corpus.json').read_text())
    return {r['sha256'] for r in manifest['fixtures']}


def decode_submission(payload, policy):
    if not isinstance(payload, dict) or set(payload) - {'pair', 'fixtures'} or payload.get('pair') != PAIR:
        raise xreg.InputError('unsupported_pair_or_payload')
    rows = payload.get('fixtures')
    if not isinstance(rows, list) or not 1 <= len(rows) <= policy['max_fixture_ids']:
        raise xreg.InputError('fixture_count_limit')
    seen, decoded, total, paired = set(), [], 0, None
    allowed = public_hashes()
    for row in rows:
        if not isinstance(row, dict) or set(row) not in ({'id', 'old'}, {'id', 'old', 'new'}):
            raise xreg.InputError('invalid_fixture_fields')
        fid = row['id']
        if not isinstance(fid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', fid) or fid in seen:
            raise xreg.InputError('invalid_or_duplicate_fixture_id')
        seen.add(fid)
        this_paired = 'new' in row
        if paired is not None and paired != this_paired:
            raise xreg.InputError('every_id_requires_both_outputs')
        paired = this_paired
        result = {'id': fid}
        for side in ('old', 'new') if paired else ('old',):
            try:
                data = base64.b64decode(row[side], validate=True)
            except (ValueError, TypeError):
                raise xreg.InputError('invalid_base64') from None
            total += len(data)
            if total > policy['max_total_decoded_bytes']:
                raise xreg.InputError('total_input_limit')
            node = xreg.safe_xml(data, policy['max_file_bytes'])
            if node.tag not in ('{urn:oasis:names:specification:ubl:schema:xsd:Invoice-2}Invoice',
                                '{urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2}CreditNote',
                                '{urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100}CrossIndustryInvoice'):
                raise xreg.InputError('unsupported_xml_root')
            # Public-fixture lock unless the validated LaunchConfig enabled uploads.
            if not policy.get('customer_uploads') and xreg.sha(data) not in allowed:
                raise xreg.InputError('customer_uploads_disabled_public_fixture_required')
            result[side] = data
        decoded.append(result)
    return decoded


CANARY = Path(__file__).with_name('canary')/'cii-br-de-1-test.xml'
CANARY_SHA256 = '6d4ac67e1740174bf1482c2c3effed74a63f54e8e98c8456af6597a5c343c93d'


def validator_health(policy, temp_parent):
    """Run the real supervised validator on one pinned public canary.

    Required evidence before the delivery gate can reopen: a broken executor,
    runtime drift or dependency drift fails this even when the ledger is clean.
    """
    started = time.monotonic()
    try:
        data = CANARY.read_bytes()
        if xreg.sha(data) != CANARY_SHA256:
            return {'passed': False, 'error': 'canary_drift'}
        verify_pinned_core()
        pack, metrics = execute_job([{'id': 'canary', 'old': data}], dict(policy, job_timeout_seconds=300), temp_parent)
        summary = json.loads(pack['results.json'])['summary']
        passed = (metrics['exit_code'] in (0, 1) and set(pack) == {'results.json', 'junit.xml', 'report.html'}
                  and metrics['temporary_inputs_and_intermediates_removed'] and summary['execution_errors'] == 0)
        return {'passed': bool(passed), 'exit_code': metrics['exit_code'],
                'execution_errors': summary['execution_errors'], 'wall_seconds': round(time.monotonic()-started, 2)}
    except Exception as exc:
        return {'passed': False, 'error': type(exc).__name__, 'code': str(exc)[:80],
                'wall_seconds': round(time.monotonic()-started, 2)}


def report_html(results):
    """Escape ALL evidence, including IDs and validator-derived values."""
    esc = lambda value: html.escape(str(value), quote=True)
    summary = results['summary']
    incomplete = summary['execution_errors'] or summary['incomplete_comparisons'] or summary['unmatched_pairs']
    status = 'INCOMPLETE — not a clean pass' if incomplete else (
        'REGRESSION FLAGS' if summary['rule_regressions'] or summary['application_axis_regressions'] else 'NO POLICY REGRESSION DETECTED')
    rows = []
    for app, comparisons in results['rule_comparisons'].items():
        for fid, diff in comparisons.items():
            rows.append('<tr>'+''.join('<td>'+esc(v)+'</td>' for v in
                (app, fid, diff.get('outcome', 'execution error'), diff['regression'], diff['comparable']))+'</tr>')
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">'
        '<title>Release Regression Pack</title><style>body{font:16px system-ui;max-width:1100px;margin:3rem auto;padding:1rem}'
        'td,th{padding:.5rem;border:1px solid #aaa;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>'
        '<h1>Release Regression Pack</h1><h2>'+esc(status)+'</h2><p>'+PAIR+'</p>'
        '<p>Official validator findings and comparison evidence. Not legal/tax certification or proof of an application defect. '
        'A complete execution can contain rejected invoices. Different-byte finding correspondence can be incomplete.</p>'
        '<h2>Summary</h2><pre>'+esc(json.dumps(summary, indent=2))+'</pre><h2>Ruleset comparisons</h2>'
        '<table><tr><th>Output</th><th>Fixture ID</th><th>Verdict transition</th><th>Regression</th><th>Comparable</th></tr>'
        +''.join(rows)+'</table><h2>Complete evidence</h2><p>Includes paired application axes, findings and provenance.</p><pre>'
        +esc(json.dumps(results, ensure_ascii=False, indent=2))+'</pre></html>').encode('utf-8')


def execute(decoded, policy, temp_parent, *, java=None, runner=None):
    start = time.monotonic()
    java = Path(java) if java else java_binary()
    verify_pinned_core(java)
    deadline = start + policy['job_timeout_seconds']
    batch_records = []
    original = xreg.run_batch
    # No changes to the existing core: inject the bounded runner at its seam.
    def bounded(command, **kwargs):
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired('validator', policy['job_timeout_seconds'])
        kwargs['timeout'] = min(kwargs['timeout'], remaining)
        # Do not pass inherited API credentials to a validation process.
        kwargs['env'] = _worker_env(work)
        # Include JVM/native-library scratch in the same owned cleanup scope.
        command = [command[0], '-Djava.io.tmpdir='+str(work), '-XX:-UsePerfData', *command[1:]]
        if runner is None:
            return contained_run(command, scratch=work, **kwargs)
        return runner(command, **kwargs)

    def batch(*args, **kwargs):
        kwargs['runner'] = bounded
        result, operation = original(*args, **kwargs)
        batch_records.append(operation)
        return result, operation

    # The service serializes workers; this scoped injection is not thread-safe.
    with tempfile.TemporaryDirectory(prefix='pilot-job-', dir=temp_parent) as folder:
        work = Path(folder)
        manifests = {}
        for side in ('old', 'new') if 'new' in decoded[0] else ('old',):
            side_dir = work/side
            side_dir.mkdir()
            fixtures = []
            for index, row in enumerate(decoded):
                filename = f'{index:04d}.xml'
                (side_dir/filename).write_bytes(row[side])
                fixtures.append({'id': row['id'], 'source_path': filename, 'sha256': xreg.sha(row[side])})
            path = side_dir/'manifest.json'
            xreg.write_json(path, {'fixtures': fixtures})
            manifests[side] = path
        args = SimpleNamespace(corpus=manifests['old'], root=work/'old',
            new_corpus=manifests.get('new'), new_root=work/'new' if 'new' in manifests else None,
            old_rules=OLD, new_rules=NEW, engine=ENGINE, java=java, out=work/'out',
            provenance=ROOT/'manifests/maintenance-2025-07-10/provenance.json',
            batch_size=25, timeout=policy['batch_timeout_seconds'], temp_root=work)
        try:
            xreg.run_batch = batch
            with contextlib.redirect_stdout(io.StringIO()):
                code = xreg.execute(args)
        finally:
            xreg.run_batch = original
        results = json.loads((args.out/'results.json').read_bytes())
        pack = {'results.json': (args.out/'results.json').read_bytes(),
                'junit.xml': (args.out/'junit.xml').read_bytes(), 'report.html': report_html(results)}
    return pack, {'wall_seconds': time.monotonic()-start, 'exit_code': code,
                  'temporary_inputs_and_intermediates_removed': not work.exists() and not any(Path(temp_parent).iterdir()),
                  'batches': batch_records, 'validation_model_calls': 0,
                  'validation_model_tokens': 0, 'summary': results['summary']}
