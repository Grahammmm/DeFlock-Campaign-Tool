#!/usr/bin/env python3
"""Offline FC7 wrapper acceptance. Machine success remains REVIEW REQUIRED.

No native/private qualification, semantic acceptance or release authority.
Safety hooks live only in dedicated acceptance processes, never test imports.
"""
import argparse
import ast
import contextlib
import hashlib
import importlib
import importlib.abc
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
import unittest

CONTRACT = 'FC7-WORKBOOK-SHEET-SHAPE/A1-A6/v1'

REPO = Path(__file__).resolve().parents[1]
CHANGED = ('campaign_tool/records/workbook_policy.py',
           'tests/records/test_fc7_workbook_sheet_shape.py',
           'tools/accept_fc7_workbook_sheet_shape.py')
FOCUSED = 'tests.records.test_fc7_workbook_sheet_shape.WorkbookSheetShapeTests'
COMPATIBILITY = ('tests.records.test_workbook_policy.WorkbookPolicyTests',
                 'tests.records.test_workbook_policy.InstalledParserIdentityTests')
DEFERRED = tuple('campaign_tool.records.intake.' + name for name in
                 ('mail_wire', 'native_msg', 'native_receipt', 'rfc822_inventory', 'wire_rfc822'))


def require(value, reason):
    if not value:
        raise RuntimeError(reason)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=True) + '\n')


class Tee:
    def __init__(self, original, path):
        self.original = original
        self.stream = path.open('x')

    def write(self, text):
        self.original.write(text)
        return self.stream.write(text)

    def flush(self):
        self.original.flush()
        self.stream.flush()


def inside(path, root):
    return path == root or root in path.parents


def audit_closure(repo, destination):
    """Inventory eager local imports, selected deferred code_identity imports,
    and every call/import site (including non-selected function bodies).
    No application code executes during this inspection.
    """
    pending = [FOCUSED.rsplit('.', 1)[0], 'tests.records.test_workbook_policy',
               'campaign_tool.records.public_scan', *DEFERRED]
    seen = {}
    def locate(name):
        p = repo.joinpath(*name.split('.'))
        if p.with_suffix('.py').is_file():
            return p.with_suffix('.py')
        if (p / '__init__.py').is_file():
            return p / '__init__.py'
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        path = locate(name)
        if path is None:
            continue
        tree = ast.parse(path.read_text())
        package = name if path.name == '__init__.py' else name.rpartition('.')[0]
        def eager(nodes):
            for node in nodes:
                if isinstance(node, ast.Import):
                    pending.extend(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom):
                    base = importlib.util.resolve_name('.' * node.level + (node.module or ''), package) if node.level else node.module
                    if base:
                        pending.append(base)
                        pending.extend(base + '.' + a.name for a in node.names)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                elif isinstance(node, ast.ClassDef):
                    eager(node.body)
                else:
                    eager([n for n in ast.iter_child_nodes(node) if isinstance(n, ast.stmt)])
        eager(tree.body)
        seen[name] = {
            'path': str(path), 'sha256': digest(path),
            'imports': [{'line': n.lineno, 'source': ast.unparse(n)} for n in ast.walk(tree)
                        if isinstance(n, (ast.Import, ast.ImportFrom))],
            'calls': [{'line': n.lineno, 'source': ast.unparse(n)} for n in ast.walk(tree)
                      if isinstance(n, ast.Call)],
            'module_statements': [ast.unparse(n) for n in tree.body
                                  if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))],
        }
        parts = name.split('.')
        pending.extend('.'.join(parts[:i]) for i in range(1, len(parts)))
    save(destination, seen)
    return seen


class NoModelImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'openai', 'anthropic', 'ollama', 'litellm',
                                      'smtplib', 'imaplib', 'poplib'}:
            raise RuntimeError('offline model/mail import refused: ' + fullname)
        return None


def install_guards(repo, run, allowed_children=()):
    """Before application imports: no sockets/models; explicit DB/child paths.
    These hooks are accident sentinels, not a hostile-code sandbox.
    """
    repo, run = Path(repo).resolve(), Path(run).resolve()
    sys.dont_write_bytecode = True
    scratch = run / 'tmp'
    scratch.mkdir(exist_ok=True)
    tempfile.tempdir = str(scratch)
    for key in ('TMPDIR', 'TMP', 'TEMP'):
        os.environ[key] = str(scratch)
    sys.meta_path.insert(0, NoModelImports())
    allowed = {(tuple(argv), str(Path(cwd).resolve())) for argv, cwd in allowed_children}
    # Installed runtime includes stdlib, dynamic linker and scanner data only;
    # no broad home-directory, credential-store or mailbox read permission.
    read_roots = [repo, run, Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve(),
                  Path('/usr/lib'), Path('/usr/share'), Path('/lib'), Path('/lib64')]
    read_files = {Path('/dev/null'), Path('/dev/urandom'), Path('/etc/mime.types'),
                  Path('/etc/localtime'), Path('/etc/ld.so.cache')}
    events = []
    def hook(event, args):
        if event.startswith('socket.'):
            raise RuntimeError('offline socket refused: ' + event)
        if event == 'sqlite3.connect':
            value = os.fsdecode(args[0])
            if value != ':memory:':
                require(not value.startswith('file:'), 'SQLite URI not admitted')
                p = Path(value).absolute()
                require(inside(p.resolve(), scratch) and p.resolve() == p,
                        'database outside exclusive synthetic tmp')
            events.append({'event': event, 'database': value})
        if event == 'subprocess.Popen':
            _, argv, cwd, _ = args
            require((tuple(argv), str(Path(cwd or Path.cwd()).resolve())) in allowed,
                    'undeclared child process')
            events.append({'event': event, 'argv': list(argv), 'cwd': str(cwd)})
        if event in {'os.system', 'os.fork', 'os.forkpty', 'os.posix_spawn', 'os.exec', 'pty.spawn'}:
            raise RuntimeError('alternate process API refused: ' + event)
        if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
            p = Path(os.fsdecode(args[0])).absolute().resolve()
            mode, flags = args[1], args[2]
            writing = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or \
                      (isinstance(flags, int) and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)))
            if writing:
                require(inside(p, run) or p == Path('/dev/null'), 'write outside run directory: ' + str(p))
            else:
                require(p in read_files or any(inside(p, r) for r in read_roots),
                        'read outside admitted source/runtime: ' + str(p))
    sys.addaudithook(hook)
    return events


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def run_suite(selectors, label, run, expected_count):
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromNames(list(selectors))
    ids = [t.id() for t in flatten(suite)]
    require(not loader.errors and len(ids) == expected_count and len(set(ids)) == len(ids),
            'missing/duplicate/unexpected selected tests')
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    (run / (label + '.stdout')).write_text(stream.getvalue())
    report = {'selectors': list(selectors), 'ids': ids, 'count': result.testsRun,
              'failures': result.failures, 'errors': result.errors, 'skips': result.skipped,
              'unexpected_successes': [t.id() for t in result.unexpectedSuccesses],
              'expected_failures': [(t.id(), text) for t, text in result.expectedFailures],
              'success': result.wasSuccessful() and not result.skipped and not result.expectedFailures}
    # TestCase objects in failures/errors are converted to exact IDs, not reprs.
    for key in ('failures', 'errors', 'skips'):
        report[key] = [(t.id(), text) for t, text in report[key]]
    save(run / (label + '.json'), report)
    print(stream.getvalue(), end='')
    require(report['count'] == expected_count and report['success'], label + ' tests failed')
    return report


def loaded_paths(repo, closure, run):
    loaded = {}
    for name, module in sorted(sys.modules.items()):
        if name == 'campaign_tool' or name.startswith('campaign_tool.') or name == 'tests' or name.startswith('tests.'):
            path = getattr(module, '__file__', None)
            if path:
                p = Path(path).resolve()
                synthetic = (name == 'campaign_tool.records._qualified_workbook_scope' and
                             p.name == 'synthetic.py' and inside(p, run / 'tmp'))
                # The unchanged compatibility fixture creates this exact test-only
                # helper under the redirected tmp root and cleans it afterwards.
                require(synthetic or (inside(p, repo) and name in closure),
                        'uninspected/wrong-prefix module: ' + name)
                loaded[name] = str(p)
    save(run / 'loaded-modules.json', loaded)
    return loaded


def negative(run):
    from campaign_tool.records import workbook_policy as policy
    from tests.records.test_fc7_workbook_sheet_shape import WorkbookSheetShapeTests
    tree = ast.parse((REPO / CHANGED[0]).read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'NativeWorkbookPolicy')
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'qualify')
    matches = [n for n in ast.walk(method) if isinstance(n, ast.Call) and
               isinstance(n.func, ast.Name) and n.func.id == 'require' and len(n.args) == 2 and
               isinstance(n.args[1], ast.Constant) and n.args[1].value == 'workbook_unit_sheet_unbound']
    require(len(matches) == 1 and isinstance(matches[0].args[0], ast.BoolOp), 'new exact-string guard absent')
    original_condition = ast.unparse(matches[0].args[0])
    require(original_condition == 'type(name) is str and bool(name) and (name in by_name)',
            'unexpected deciding guard; mutation not qualified')
    matches[0].args[0] = ast.parse('name in by_name', mode='eval').body
    mutant_tree = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
    namespace = dict(policy.__dict__)
    exec(compile(mutant_tree, '<synthetic-sheet-guard-bypass>', 'exec'), namespace)
    stream = io.StringIO()
    class MutantTypedErrorTests(unittest.TestCase):
        def check_locator(self, value):
            fixture = WorkbookSheetShapeTests('test_typed_error_before_membership')
            try:
                fixture.setUp()
                fixture.units[0]['locator']['sheet'] = value
                fixture.typed_error(fixture.units, 'workbook_unit_sheet_unbound')
            finally:
                fixture.doCleanups()

        def test_mutant_list(self):
            self.check_locator([])

        def test_mutant_dict(self):
            self.check_locator({})

    suite = unittest.TestSuite((MutantTypedErrorTests('test_mutant_list'),
                               MutantTypedErrorTests('test_mutant_dict')))
    ids = [test.id() for test in flatten(suite)]
    from unittest.mock import patch
    with patch.object(policy.NativeWorkbookPolicy, 'qualify', namespace['qualify']):
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    (run / 'mutant.stdout').write_text(stream.getvalue())
    report = {'ids': ids, 'count': result.testsRun, 'errors': [(t.id(), text) for t, text in result.errors],
              'failure_count': len(result.failures), 'skips': len(result.skipped),
              'original_condition': original_condition, 'mutant_condition': 'name in by_name'}
    save(run / 'mutant.json', report)
    print(stream.getvalue(), end='')
    require(result.testsRun == 2 and len(result.errors) == 2 and not result.failures and not result.skipped,
            'mutant not detected by both typed-error assertions')
    require(all("TypeError: unhashable type: '" + kind + "'" in text
                for (_, text), kind in zip(result.errors, ('list', 'dict'))), 'wrong mutant failure')
    run_suite((FOCUSED,), 'restored-positive', run, 6)


def bootstrap(path, repo, run, mode, target=None, argv=None):
    """A script-file entry point; child sentinels precede every application import."""
    source = (
        'import importlib.util\nimport json\nfrom pathlib import Path\nimport runpy\nimport sys\n'
        + 'repo = Path(' + repr(str(repo)) + ')\n'
        + 'run = Path(' + repr(str(run)) + ')\n'
        + 'spec = importlib.util.spec_from_file_location("fc7_accept", repo / "tools/accept_fc7_workbook_sheet_shape.py")\n'
        + 'runner = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(runner)\n'
        + 'closure = runner.audit_closure(repo, run / "closure.json")\n'
        + 'children = [(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], repo)]\n'
        + 'events = runner.install_guards(repo, run, children)\nsys.path.insert(0, str(repo))\n'
    )
    if mode == 'focused':
        source += ('runner.run_suite((runner.FOCUSED,), "focused", run, 6)\n'
                   'runner.loaded_paths(repo, closure, run)\n')
    else:
        source += 'sys.argv = ' + repr([str(target), *(argv or [])]) + '\n'
        source += ('exit_code = 0\ntry:\n    runpy.run_path(' + repr(str(target)) + ', run_name="__main__")\n'
                   'except SystemExit as error:\n    exit_code = error.code if isinstance(error.code, int) else (0 if error.code is None else 1)\n    raise\n'
                   'except BaseException:\n    exit_code = 1\n    raise\n'
                   'finally:\n    runner.save(run / "guard-events.json", events)\n'
                   '    runner.save(run / "tool-invocation.json", {"argv": [sys.executable, *sys.argv], "cwd": str(Path.cwd()), "exit": exit_code, "execution": "unchanged script via runpy.run_path after pre-import sentinels", "source_sha256": runner.digest(sys.argv[0])})\n')
    if mode == 'focused':
        source += 'runner.save(run / "guard-events.json", events)\n'
    path.write_text(source)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', required=True, choices=('focused', 'compatibility', 'negative',
                                                          'portability', 'credentials', 'public-tree'))
    parser.add_argument('--output-root', required=True, type=Path)
    args = parser.parse_args()
    candidate = REPO.parent.resolve()
    output = args.output_root.absolute()
    require(output.resolve() == output and inside(output, candidate) and not inside(output, REPO),
            'output root must be unsymlinked and outside repo within candidate')
    output.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix=args.check + '-', dir=output))
    stdout, stderr = sys.stdout, sys.stderr
    sys.stdout = Tee(stdout, run / 'acceptance.stdout')
    sys.stderr = Tee(stderr, run / 'acceptance.stderr')
    manifest = json.loads((candidate / 'SOURCE-MANIFEST.json').read_text())
    # The operator-staged, handoff-hash-bound manifest is the sole base pin;
    # do not duplicate it as a scanner-flagged high-entropy string in source.
    base = manifest['head']
    receipt = {'contract': CONTRACT, 'check': args.check, 'argv': [sys.executable, *sys.argv],
               'cwd': str(Path.cwd()), 'run': str(run), 'base': manifest.get('head'),
               'candidate_head': None, 'source_hashes': {p: digest(REPO / p) for p in CHANGED},
               'manifest_sha256': digest(candidate / 'SOURCE-MANIFEST.json'), 'status': 'FAIL', 'exit': 1}
    commands = []
    events = []
    def child(argv, cwd, label, success=True):
        completed = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=180)
        stdout, stderr = run / (label + '.stdout'), run / (label + '.stderr')
        stdout.write_text(completed.stdout)
        stderr.write_text(completed.stderr)
        row = {'argv': list(argv), 'cwd': str(cwd), 'exit': completed.returncode,
               'stdout': str(stdout), 'stdout_sha256': digest(stdout),
               'stderr': str(stderr), 'stderr_sha256': digest(stderr)}
        commands.append(row)
        save(run / 'commands.json', commands)
        if success:
            require(completed.returncode == 0, label + ' child failed: ' + completed.stderr)
        return completed
    try:
        require(Path.cwd().resolve() == REPO, 'cwd must equal repo')
        require(manifest['schema'] == 'agentorange.source-manifest/v1' and
                manifest['card'] == 'FC7-WORKBOOK-SHEET-SHAPE' and
                type(base) is str and re.fullmatch('[0-9a-f]{40}', base) is not None and
                manifest['repo'] == str(REPO) and manifest['write_root'] == str(candidate) and
                manifest['clean'] is True and manifest['inputs'] == [], 'staging declaration mismatch')
        runtime = manifest['runtime']
        require((REPO / '.venv').resolve() == Path(runtime['repo_venv']).resolve(), 'venv mismatch')
        expected_python = Path(runtime['credential_scan_python3']) if args.check == 'credentials' else REPO / '.venv/bin/python3'
        require(Path(sys.executable).absolute() == expected_python.absolute(), 'interpreter argv mismatch')
        require(Path(runtime['detect_secrets']).is_file() and Path(runtime['credential_scan_python3']).is_file(),
                'scanner runtime unavailable')
        head = child(['git', 'rev-parse', 'HEAD'], REPO, 'head').stdout.strip()
        receipt['candidate_head'] = head
        require(child(['git', 'rev-parse', base + '^{tree}'], REPO, 'base-tree').stdout.strip() == manifest['tree'], 'base tree mismatch')
        require(child(['git', 'remote', 'get-url', 'origin'], REPO, 'remote').stdout.strip() == manifest['remote'], 'remote mismatch')
        child(['git', 'merge-base', '--is-ancestor', base, head], REPO, 'ancestry')
        delta = child(['git', 'diff', '--name-only', base], REPO, 'changed-paths').stdout.splitlines()
        untracked = child(['git', 'ls-files', '--others', '--exclude-standard'], REPO, 'untracked-paths').stdout.splitlines()
        require(set(delta + untracked) <= set(CHANGED), 'out-of-scope changes')
        closure = audit_closure(REPO, run / 'closure.json')
        children = []
        boot = run / 'bootstrap.py'
        copied = run / 'second-prefix' / 'source'
        portable_run = run / 'portable-evidence'
        scanner_argv = [runtime['detect_secrets'], 'scan', '--all-files', '--exclude-files', '^.git/', '--no-verify']
        checker_argv = [sys.executable, str(REPO / 'tools/check_secret_report.py'), str(run / 'secret-scan.json')]
        if args.check in ('portability', 'credentials', 'public-tree'):
            boot = run / 'bootstrap.py'
            if args.check == 'portability':
                copied = run / 'second-prefix' / 'source'
                copied.mkdir(parents=True)
                # Copy source only, not runtime links, .git or generated evidence.
                for package in ('campaign_tool', 'tests'):
                    for path in (REPO / package).rglob('*.py'):
                        require(not path.is_symlink(), 'source symlink')
                        target = copied / path.relative_to(REPO)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(path, target)
                target = copied / CHANGED[2]
                target.parent.mkdir(parents=True)
                shutil.copyfile(REPO / CHANGED[2], target)
                portable_run = run / 'portable-evidence'
                portable_run.mkdir()
                bootstrap(boot, copied, portable_run, 'focused')
                children.append(([sys.executable, str(boot)], copied))
            elif args.check == 'public-tree':
                target = REPO / 'tools/check_public_tree.py'
                child_run = run / 'scanner-evidence'
                child_run.mkdir()
                bootstrap(boot, REPO, child_run, 'tool', target, [])
                children.append(([sys.executable, str(boot)], REPO))
            else:
                # These exact installed scanner/stdlib-only checker processes do
                # not import application code. The installed scanner's reviewed
                # closure uses multiprocessing workers and --no-verify disables
                # verifier calls. Do not replace its implementation or argv.
                children.extend(((scanner_argv, REPO), (checker_argv, REPO)))
        events = install_guards(REPO, run, children)
        sys.path.insert(0, str(REPO))
        if args.check == 'focused':
            run_suite((FOCUSED,), 'focused', run, 6)
        elif args.check == 'compatibility':
            run_suite(COMPATIBILITY, 'compatibility', run, 9)
        elif args.check == 'negative':
            negative(run)
        elif args.check == 'portability':
            run_suite((FOCUSED,), 'original-prefix', run, 6)
            child([sys.executable, str(boot)], copied, 'second-prefix')
            loaded = json.loads((portable_run / 'loaded-modules.json').read_text())
            require(loaded and all(inside(Path(p), copied) for p in loaded.values()), 'portability loaded wrong source')
        elif args.check == 'credentials':
            require(Path(runtime['detect_secrets']).read_text().splitlines()[0] == '#!' + runtime['credential_scan_python3'],
                    'scanner shebang runtime mismatch')
            scan = child(scanner_argv, REPO, 'secret-scan')
            (run / 'secret-scan.json').write_text(scan.stdout)
            require(type(json.loads(scan.stdout).get('results')) is dict, 'invalid scanner output')
            child(checker_argv, REPO, 'secret-report-check')
        elif args.check == 'public-tree':
            child([sys.executable, str(boot)], REPO, 'public-tree')
        loaded_paths(REPO, closure, run)
        require(receipt['source_hashes'] == {p: digest(REPO / p) for p in CHANGED},
                'source bytes changed during acceptance')
        receipt.update(status='REVIEW_REQUIRED', exit=0, semantic_acceptance=False,
                       visual_review_pending=True, shipping_obligations='OWED: operator exact-head full discovery/native/integration and CI')
    except Exception as error:
        receipt['error'] = str(error)
        (run / 'failure.stderr').write_text(traceback.format_exc())
        print(traceback.format_exc(), file=sys.stderr)
    finally:
        receipt['commands'] = commands
        save(run / 'guard-events.json', events)
        save(run / 'receipt.json', receipt)
        print(json.dumps({'status': receipt['status'], 'exit': receipt['exit'],
                          'receipt': str(run / 'receipt.json')}))
        sys.stdout.flush()
        sys.stderr.flush()
        sys.stdout.stream.close()
        sys.stderr.stream.close()
        sys.stdout, sys.stderr = stdout, stderr
        save(run / 'invocation.json', {
            'argv': receipt['argv'], 'cwd': receipt['cwd'], 'exit': receipt['exit'],
            'stdout': str(run / 'acceptance.stdout'), 'stdout_sha256': digest(run / 'acceptance.stdout'),
            'stderr': str(run / 'acceptance.stderr'), 'stderr_sha256': digest(run / 'acceptance.stderr')})
    return receipt['exit']


if __name__ == '__main__':
    raise SystemExit(main())
