"""FA102 acceptance launcher: receipt install verifies rp_ schema objects, not only markers.

Exactly one --case per invocation; every invocation creates a fresh, uniquely named child of
--output-root (absolute, outside this source tree, no symlinked component) and never touches
prior evidence there.

  behavior          tests.records.test_fa102.Behavior                                   (A1)
  compatibility     tests.records.test_ledger_receipts, tests.records.test_receipt_projection_audit (A2)
  negative-control  tests.records.test_fa102.GateControl; exit 0 only when the marker-only
                    control is detected AND the restored exact-schema positive passes  (A3)
  credential-scan   SOURCE-MANIFEST runtime detect-secrets, then the unchanged
                    tools/check_secret_report.py with the same interpreter             (A4)
  public-tree       the unchanged tools/check_public_tree.py with this interpreter      (A5)

Test cases run in a fresh file-launched child (this file, --child-spec). Before any
application import the child disables bytecode writes, points tempfile/TMPDIR/HOME below its
own run directory and installs an audit-hook guard refusing network, child processes and
writes (including SQLite connections) outside that directory; it self-tests the guard and
checks the ledger modules' globals and bound defaults for host paths. Only the named
selectors are loaded; there is no discovery. The guard is an offline guard for trusted
synthetic tests, not a hostile-code sandbox.

A pass is REVIEW REQUIRED: scoped evidence, not full-repository CI, semantic, legal, human,
stage or release acceptance.
"""
import sys

sys.dont_write_bytecode = True

import argparse  # noqa: E402 - bytecode writes are disabled before any further import
import datetime  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
from pathlib import Path  # noqa: E402
import re  # noqa: E402
import secrets  # noqa: E402
import stat  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402

SOURCE = Path(__file__).resolve().parents[1]
CANDIDATE = SOURCE.parent
SELF = 'tools/accept_fa102.py'
FA102 = 'tests.records.test_fa102'
SELECTORS = {
    'behavior': (FA102 + '.Behavior',),
    'compatibility': ('tests.records.test_ledger_receipts', 'tests.records.test_receipt_projection_audit'),
    'negative-control': (FA102 + '.GateControl',),
}
CASES = ('behavior', 'compatibility', 'negative-control', 'credential-scan', 'public-tree')
REQUIRED_TESTS = {
    'behavior': {FA102 + '.Behavior.' + name for name in (
        'test_reference_inventory_is_the_unchanged_migration',
        'test_fresh_install_creates_exact_objects_and_markers',
        'test_exact_reopen_and_replay_are_unchanged',
        'test_valid_new_project_after_reopen_captures_and_verifies',
        'test_historical_base_only_upgrade_preserves_evidence',
        'test_each_removed_base_trigger_is_refused_on_guarded_reopen',
        'test_each_changed_base_trigger_is_refused_on_guarded_reopen',
        'test_altered_table_constraints_are_refused',
        'test_each_omitted_table_is_refused',
        'test_unknown_rp_namespace_objects_are_refused',
        'test_base_only_reopen_refuses_before_guard_append',
        'test_missing_source_guard_keeps_existing_refusal',
        'test_changed_source_guard_keeps_existing_refusal',
        'test_unknown_marker_identity_keeps_existing_refusal',
        'test_unrelated_canonical_objects_are_not_false_positives')},
    'negative-control': {FA102 + '.GateControl.test_marker_only_install_is_detected_and_exact_schema_positive_passes'},
}
EXPECTED_CONTROL = {
    ('marker_only', 'removed_guard_regression'): 'regression_failed',
    ('marker_only', 'append_only_update_after_reopen'): 'update_applied',
    ('staged', 'removed_guard_regression'): 'regression_passed',
    ('marker_only', 'exact_schema_positive'): 'positive_passed',
    ('marker_only', 'append_only_update_on_exact_schema'): 'update_refused:append-only evidence',
    ('staged', 'exact_schema_positive'): 'positive_passed',
    ('staged', 'append_only_update_on_exact_schema'): 'update_refused:append-only evidence',
}
HASHED = ('campaign_tool/records/ledger/receipt_projection.py',
          'campaign_tool/records/ledger/migrations/receipt_projection_v001.py',
          'campaign_tool/records/ledger/migrations/receipt_source_guard_v001.py',
          'campaign_tool/records/ledger/store.py',
          'tests/records/test_fa102.py', 'tests/records/test_ledger_receipts.py',
          'tests/records/test_receipt_projection_audit.py', SELF,
          'tools/check_secret_report.py', 'tools/check_public_tree.py')
LEDGER_MODULES = ('campaign_tool.records.ledger.store', 'campaign_tool.records.ledger.receipt_projection',
                  'campaign_tool.records.ledger.migrations.receipt_projection_v001',
                  'campaign_tool.records.ledger.migrations.receipt_source_guard_v001')
LABEL = 'REVIEW REQUIRED - scoped offline acceptance, not full-repository CI or semantic acceptance'
DROPPED_ENV = ('PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP', 'PYTHONINSPECT', 'PYTHONSAFEPATH')
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(Path(path).read_bytes())


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')


# ----------------------------------------------------------------------------- child

class Guard:
    NETWORK = ('socket.connect', 'socket.getaddrinfo', 'socket.gethostbyname', 'socket.gethostbyaddr',
               'socket.bind', 'socket.sendto', 'socket.sendmsg', 'urllib.Request', 'http.client.connect',
               'ftplib.connect', 'smtplib.connect', 'imaplib.open', 'poplib.connect')
    PROCESS = ('subprocess.Popen', 'os.system', 'os.posix_spawn', 'os.fork', 'os.forkpty', 'pty.spawn')
    # event -> ((path argument index, dir_fd argument index or None), ...)
    WRITES = {'os.mkdir': ((0, 2),), 'os.remove': ((0, 1),), 'os.rmdir': ((0, 1),),
              'os.rename': ((0, 2), (1, 3)), 'os.replace': ((0, 2), (1, 3)), 'os.chmod': ((0, 2),),
              'os.chown': ((0, 3),), 'os.symlink': ((1, 2),), 'os.link': ((0, 2), (1, 3)),
              'os.truncate': ((0, None),), 'os.utime': ((0, 3),), 'shutil.rmtree': ((0, 1),)}

    def __init__(self, root):
        self.root = Path(os.path.realpath(root))
        self.active = False
        self.phase = 'setup'
        self.refusals = []

    def absolute(self, value, dir_fd=None):
        text = os.fsdecode(value)
        if not os.path.isabs(text):
            base = os.readlink('/proc/self/fd/%d' % dir_fd) if isinstance(dir_fd, int) and dir_fd >= 0 else os.getcwd()
            text = os.path.join(base, text)
        head, leaf = os.path.split(os.path.normpath(text))
        return Path(os.path.realpath(head)) / leaf

    def inside(self, path):
        return path == self.root or path.is_relative_to(self.root) or str(path) == os.devnull

    def refuse(self, event, detail):
        self.refusals.append({'phase': self.phase, 'event': event, 'detail': str(detail)[:240]})
        raise PermissionError('FA102 offline guard refused ' + event)

    def __call__(self, event, args):
        if not self.active:
            return
        if event in self.NETWORK:
            self.refuse(event, '')
        elif event in self.PROCESS or event.startswith('os.exec') or event.startswith('os.spawn'):
            self.refuse(event, repr(args[:1])[:120])
        elif event == 'open':
            path, mode, flags = args
            if path is None or isinstance(path, int):
                return
            writing = any(c in mode for c in 'wax+') if isinstance(mode, str) else bool((flags or 0) & WRITE_FLAGS)
            if writing and not self.inside(self.absolute(path)):
                self.refuse(event, path)
        elif event == 'sqlite3.connect':
            database = os.fsdecode(args[0]) if args else ''
            if database in ('', ':memory:'):
                return
            if database.startswith('file:'):
                database = database[5:].split('?', 1)[0]
            if not self.inside(self.absolute(database)):
                self.refuse(event, database)
        elif event in self.WRITES:
            for index, fd_index in self.WRITES[event]:
                if index >= len(args) or args[index] is None or isinstance(args[index], int):
                    continue
                dir_fd = args[fd_index] if fd_index is not None and fd_index < len(args) else None
                path = self.absolute(args[index], dir_fd)
                if event == 'os.mkdir' and os.path.lexists(path):
                    continue
                if not self.inside(path):
                    self.refuse(event, path)


def guard_self_test(guard, probe):
    """Each probe must be refused by the guard before any effect (probe paths are absent)."""
    import socket
    import sqlite3
    guard.phase = 'guard-self-test'
    outcomes = {}

    def attempt(name, action):
        before = len(guard.refusals)
        try:
            action()
            outcomes[name] = 'not_refused'
        except PermissionError:
            outcomes[name] = 'refused' if len(guard.refusals) == before + 1 else 'refused_without_record'
        except Exception as error:
            outcomes[name] = type(error).__name__

    def network():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.connect(('127.0.0.1', 9))
    attempt('network', network)
    attempt('process', lambda: subprocess.Popen([str(probe)]))
    attempt('write_outside', lambda: open(probe, 'xb'))
    attempt('sqlite_outside', lambda: sqlite3.connect(str(probe)))
    attempt('mkdir_outside', lambda: os.mkdir(probe))
    guard.phase = 'tests'
    return outcomes


def host_path_bindings(root):
    """Absolute-path strings in ledger module globals or bound defaults that leave the run root."""
    import importlib
    found = []

    def scan(where, value):
        if isinstance(value, (str, bytes, os.PathLike)) and not isinstance(value, bytes):
            text = os.fspath(value)
            if text.startswith('/') and '\n' not in text and not Path(text).is_relative_to(root):
                found.append({'where': where, 'value': text[:200]})
    for name in LEDGER_MODULES:
        module = importlib.import_module(name)
        for key, value in vars(module).items():
            if key.startswith('__') and key.endswith('__'):
                continue  # import metadata (__file__, __cached__), not a live-path binding
            scan(name + '.' + key, value)
            for default in (getattr(value, '__defaults__', None) or ()):
                scan(name + '.' + key + '.__defaults__', default)
            for kw, default in (getattr(value, '__kwdefaults__', None) or {}).items():
                scan(name + '.' + key + '.__kwdefaults__.' + kw, default)
    return found


def iter_tests(suite):
    import unittest
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from iter_tests(item)
        else:
            yield item


def child_main(spec_path):
    spec = json.loads(Path(spec_path).read_text(encoding='utf-8'))
    root = Path(spec['child_root'])
    tmp, home = root / 'tmp', root / 'home'
    tmp.mkdir(mode=0o700)
    home.mkdir(mode=0o700)
    tempfile.tempdir = str(tmp)
    for key in ('TMPDIR', 'TEMP', 'TMP'):
        os.environ[key] = str(tmp)
    os.environ['HOME'] = str(home)
    for key in ('XDG_CACHE_HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME'):
        os.environ[key] = str(home / key.lower())
    repo_loaded = sorted(n for n in sys.modules if n.split('.')[0] in ('campaign_tool', 'tests'))
    guard = Guard(root)
    sys.addaudithook(guard)
    guard.active = True
    facts = {'case': spec['case'], 'executable': sys.executable, 'python': sys.version, 'cwd': os.getcwd(),
             'source_root': str(SOURCE), 'guard_installed_before_app_import': repo_loaded == [],
             'tempdir': tempfile.gettempdir(), 'home': os.environ['HOME']}
    probe = Path(spec['probe_target'])
    facts['probe_parent_absent_before'] = not os.path.lexists(probe.parent)
    facts['guard_self_test'] = guard_self_test(guard, probe)
    while str(SOURCE) in sys.path:
        sys.path.remove(str(SOURCE))
    sys.path.insert(0, str(SOURCE))
    import unittest
    facts['host_path_bindings'] = host_path_bindings(root)
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.loadTestsFromName(name) for name in spec['selectors'])
    selected = [t.id() for t in iter_tests(suite)]
    sys.stderr.write('selected %d tests from %s\n' % (len(selected), ', '.join(spec['selectors'])))
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(suite)
    guard.phase = 'after-tests'
    counts = {'tests_run': result.testsRun, 'selected': selected,
              'failures': [[t.id(), text[-2000:]] for t, text in result.failures],
              'errors': [[t.id(), text[-2000:]] for t, text in result.errors],
              'skipped': [[t.id(), reason] for t, reason in result.skipped],
              'expected_failures': [t.id() for t, _ in result.expectedFailures],
              'unexpected_successes': [t.id() for t in result.unexpectedSuccesses]}
    modules = {n: os.path.realpath(getattr(sys.modules[n], '__file__', '') or '') for n in list(sys.modules)
               if n.split('.')[0] in ('campaign_tool', 'tests') and getattr(sys.modules[n], '__file__', None)}
    fa102 = sys.modules.get(FA102)
    guard.active = False
    test_refusals = [r for r in guard.refusals if r['phase'] != 'guard-self-test']
    facts.update({'counts': counts, 'modules_outside_source': sorted(
                      n for n, f in modules.items() if not Path(f).is_relative_to(SOURCE)),
                  'guard_refusals': guard.refusals,
                  'control_outcomes': list(getattr(fa102, 'CONTROL_OUTCOMES', [])) if fa102 else []})
    facts['child_ok'] = bool(
        result.testsRun > 0 and result.testsRun == len(selected) and not counts['failures']
        and not counts['errors'] and not counts['skipped'] and not counts['expected_failures']
        and not counts['unexpected_successes'] and not test_refusals and not facts['modules_outside_source']
        and facts['guard_installed_before_app_import'] and facts['probe_parent_absent_before']
        and set(facts['guard_self_test'].values()) == {'refused'} and not facts['host_path_bindings']
        and Path(facts['tempdir']).is_relative_to(root))
    Path(spec['result']).write_text(json.dumps(facts, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    sys.stdout.write(json.dumps({'case': spec['case'], 'child_ok': facts['child_ok'], 'tests_run': result.testsRun},
                                sort_keys=True) + '\n')
    return 0 if facts['child_ok'] else 1


# ----------------------------------------------------------------------------- parent

def git(*args):
    done = subprocess.run(['git', *args], cwd=SOURCE, capture_output=True, check=False)
    if done.returncode != 0:
        raise RuntimeError('git ' + args[0] + ' failed')
    return done.stdout


def untracked_bytes(path):
    # The operator-staged .venv link is untracked; hash a link by its target text, never follow it.
    if path.is_symlink():
        return b'symlink->' + os.fsencode(os.readlink(path))
    return path.read_bytes()


def identity():
    status = git('status', '--porcelain', '--untracked-files=all').decode()
    untracked = sorted(line[3:] for line in status.splitlines() if line.startswith('?? '))
    dirty = git('diff', 'HEAD', '--binary') + b''.join(
        name.encode() + b'\0' + untracked_bytes(SOURCE / name) for name in untracked)
    manifest = CANDIDATE / 'SOURCE-MANIFEST.json'
    return {'head': git('rev-parse', 'HEAD').decode().strip(), 'clean_tree': status == '',
            'dirty_files': status.splitlines(), 'dirty_diff_sha256': sha256_bytes(dirty),
            'source_manifest_sha256': sha256_file(manifest) if manifest.is_file() else None,
            'file_sha256': {p: sha256_file(SOURCE / p) for p in HASHED if (SOURCE / p).is_file()}}


def safe_root(raw):
    root = Path(raw)
    if not root.is_absolute():
        raise ValueError('output root must be absolute')
    root = Path(os.path.normpath(root))
    if root == SOURCE or root.is_relative_to(SOURCE) or SOURCE.is_relative_to(root):
        raise ValueError('output root must be outside (and not an ancestor of) the source tree')
    current = Path(root.anchor)
    for part in root.parts[1:]:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            os.mkdir(current, 0o700)
            info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError('output root has a linked or non-directory component: ' + str(current))
    return root


def tree_hashes(parent, skip):
    found = {}
    for dirpath, dirnames, filenames in os.walk(parent):
        if Path(dirpath) == skip or Path(dirpath).is_relative_to(skip):
            dirnames[:] = []
            continue
        for name in filenames:
            path = Path(dirpath) / name
            found[str(path.relative_to(parent))] = '<symlink>' if path.is_symlink() else sha256_file(path)
    return found


def run_logged(run_dir, label, argv, stdout_path=None):
    """Literal argv, no shell; raw stdout/stderr retained with hashes."""
    out_path = stdout_path or run_dir / (label + '-stdout.txt')
    err_path = run_dir / (label + '-stderr.txt')
    env = {k: v for k, v in os.environ.items() if k not in DROPPED_ENV}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    with out_path.open('xb') as out, err_path.open('xb') as err:
        done = subprocess.run(argv, cwd=SOURCE, env=env, stdout=out, stderr=err, check=False, timeout=1700)
    return {'label': label, 'argv': argv, 'cwd': str(SOURCE), 'exit': done.returncode,
            'stdout': str(out_path), 'stdout_sha256': sha256_file(out_path), 'stdout_bytes': out_path.stat().st_size,
            'stderr': str(err_path), 'stderr_sha256': sha256_file(err_path)}


def run_tests(case, run_dir, probe, record, log):
    child_root = run_dir / 'child'
    child_root.mkdir(mode=0o700)
    spec = {'case': case, 'selectors': list(SELECTORS[case]), 'child_root': str(child_root),
            'result': str(child_root / 'child-result.json'), 'probe_target': str(probe)}
    spec_path = run_dir / 'child-spec.json'
    spec_path.write_text(json.dumps(spec, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    child = run_logged(run_dir, 'child', [sys.executable, '-B', str(SOURCE / SELF), '--child-spec', str(spec_path)])
    child['spec_sha256'] = sha256_file(spec_path)
    record['children'].append(child)
    try:
        result = json.loads(Path(spec['result']).read_text(encoding='utf-8'))
        child['result_sha256'] = sha256_file(spec['result'])
    except (OSError, ValueError) as error:
        log.append('child result unavailable: ' + type(error).__name__)
        return False
    counts = result['counts']
    child['result'] = result
    record['counts'] = {'executed': counts['tests_run'], 'selected': len(counts['selected']),
                        'failures': len(counts['failures']), 'errors': len(counts['errors']),
                        'skipped': len(counts['skipped'])}
    log.append('child exit %d: %s' % (child['exit'], json.dumps(record['counts'], sort_keys=True)))
    passed = child['exit'] == 0 and result.get('child_ok') is True
    required = REQUIRED_TESTS.get(case, set())
    missing = sorted(required - set(counts['selected']))
    if missing:
        log.append('required tests missing from selection: ' + ', '.join(missing))
        passed = False
    if case == 'compatibility':
        for module in SELECTORS[case]:
            if not any(t.startswith(module + '.') for t in counts['selected']):
                log.append('compatibility selector executed no tests: ' + module)
                passed = False
    if case == 'negative-control':
        outcomes = result.get('control_outcomes', [])
        observed = {(o['module'], o['case']): o['outcome'] for o in outcomes}
        record['control'] = {'outcomes': outcomes, 'expected': {'%s/%s' % k: v for k, v in EXPECTED_CONTROL.items()},
                             'defect_detected': observed.get(('marker_only', 'removed_guard_regression')) == 'regression_failed',
                             'restored_positive': observed.get(('staged', 'removed_guard_regression')) == 'regression_passed'}
        failing = [o for o in outcomes if o['outcome'] == 'regression_failed']
        (run_dir / 'control-failing-output.txt').write_text(
            ''.join('%s/%s: %s\n' % (o['module'], o['case'], o['detail']) for o in failing), encoding='utf-8')
        log.append('control outcomes: ' + json.dumps({'%s/%s' % k: v for k, v in observed.items()}, sort_keys=True))
        passed = passed and len(outcomes) == len(EXPECTED_CONTROL) and observed == EXPECTED_CONTROL
    return passed


def run_credentials(run_dir, record, log):
    manifest = json.loads((CANDIDATE / 'SOURCE-MANIFEST.json').read_text(encoding='utf-8'))
    runtime = manifest.get('runtime', {})
    python, scanner = runtime.get('credential_scan_python3'), runtime.get('detect_secrets')
    record['credential_runtime'] = {'credential_scan_python3': python, 'detect_secrets': scanner}
    if not (python and scanner and os.access(python, os.X_OK) and Path(scanner).is_file()):
        log.append('declared credential-scan runtime missing; nothing installed or substituted')
        return False
    report = run_dir / 'secret-scan.json'
    scan = run_logged(run_dir, 'detect-secrets',
                      [python, scanner, 'scan', '--all-files', '--exclude-files', '^.git/', '--no-verify'], report)
    check = run_logged(run_dir, 'check-secret-report', [python, str(SOURCE / 'tools/check_secret_report.py'), str(report)])
    record['children'] += [scan, check]
    try:
        raw_hits = sum(len(v) for v in json.loads(report.read_bytes())['results'].values())
    except (ValueError, KeyError, TypeError, AttributeError):
        raw_hits = None
    match = re.search(rb'potential secrets: (\d+);', Path(check['stdout']).read_bytes())
    potential = int(match.group(1)) if match else None
    record['credential'] = {'raw_hits': raw_hits, 'potential_secrets': potential}
    log.append('detect-secrets exit %d; raw hits %s; checker exit %d; potential secrets %s'
               % (scan['exit'], raw_hits, check['exit'], potential))
    return scan['exit'] == 0 and check['exit'] == 0 and raw_hits is not None and potential == 0


def run_public_tree(run_dir, record, log):
    child = run_logged(run_dir, 'check-public-tree', [sys.executable, str(SOURCE / 'tools/check_public_tree.py')])
    record['children'].append(child)
    text = Path(child['stdout']).read_text(encoding='utf-8', errors='replace')
    record['public_tree'] = {'stdout_lines': len(text.splitlines())}
    log.append('check_public_tree exit %d; stdout lines %d' % (child['exit'], len(text.splitlines())))
    return child['exit'] == 0


def parent_main(case, output_root):
    root = safe_root(output_root)
    token = secrets.token_hex(4)
    run_dir = root / '-'.join((case, stamp(), str(os.getpid()), token))
    run_dir.mkdir(mode=0o700)
    prior = tree_hashes(root, run_dir)
    start = identity()
    probe = root.parent / ('fa102-absent-' + token) / 'probe'
    record = {'label': LABEL, 'case': case, 'argv': [sys.executable, *sys.argv], 'cwd': os.getcwd(),
              'interpreter': sys.executable, 'python': sys.version, 'run_dir': str(run_dir),
              'selectors': list(SELECTORS.get(case, ())), 'identity_before': start, 'children': []}
    log = []
    try:
        if case == 'credential-scan':
            passed = run_credentials(run_dir, record, log)
        elif case == 'public-tree':
            passed = run_public_tree(run_dir, record, log)
        else:
            passed = run_tests(case, run_dir, probe, record, log)
    except Exception as error:  # recorded and failed closed
        log.append('unexpected runner fault: %s: %s' % (type(error).__name__, str(error)[:300]))
        record['runner_fault'] = type(error).__name__
        passed = False
    end = identity()
    record['identity_after'] = end
    after = tree_hashes(root, run_dir)
    record['prior_evidence_files'] = len(prior)
    record['prior_evidence_unchanged'] = all(after.get(k) == v for k, v in prior.items())
    record['probe_absent_after'] = not os.path.lexists(probe.parent)
    record['source_unchanged'] = start == end
    passed = bool(passed and record['prior_evidence_unchanged'] and record['probe_absent_after'] and start == end)
    record['exit'] = 0 if passed else 1
    summary = {'case': case, 'exit': record['exit'], 'run_dir': str(run_dir), 'head': end['head'],
               'clean_tree': end['clean_tree'], 'dirty_diff_sha256': end['dirty_diff_sha256'],
               'counts': record.get('counts'), 'label': LABEL}
    for key in ('control', 'credential', 'public_tree'):
        if key in record:
            summary[key] = {k: v for k, v in record[key].items() if k != 'outcomes'}
    log_bytes = ('\n'.join(log) + '\n').encode()
    (run_dir / 'runner-log.txt').write_bytes(log_bytes)
    out = (json.dumps(summary, sort_keys=True) + '\n').encode()
    (run_dir / 'summary.json').write_bytes(out)
    record['summary_sha256'] = sha256_bytes(out)
    (run_dir / 'receipt.json').write_text(json.dumps(record, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    sys.stderr.write(log_bytes.decode())
    sys.stdout.write(out.decode())
    return record['exit']


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--case', choices=CASES)
    parser.add_argument('--output-root')
    parser.add_argument('--child-spec', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child_spec:
        if args.case or args.output_root:
            parser.error('--child-spec is used alone')
        return child_main(args.child_spec)
    if not args.case or not args.output_root:
        parser.error('--case and --output-root are required')
    return parent_main(args.case, args.output_root)


if __name__ == '__main__':
    raise SystemExit(main())
