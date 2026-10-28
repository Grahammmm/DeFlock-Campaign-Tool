"""FC12 audit/hearing family independence and absence-marker matrix.

Test-only. Fictional in-memory text runs through the real digest detectors.
Mentions are textual leads and absences are extraction warnings; neither proves
that an audit or hearing did or did not occur. Ordinary discovery is inert:
product imports and offline patches are scoped to setUp/cleanup. A script child
sets its own temporary root before importing product code.
"""
import argparse
import ast
from contextlib import contextmanager, ExitStack
import copy
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
from typing import cast
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / 'tests/fixtures/fc12-audit-hearing/cases.json'
# Frozen independently of product regexes; the fixture must agree exactly.
AUDIT_TERMS = ('audit', 'audited', 'auditing', 'access log', 'query log', 'search log',
               'record of each access', 'log of each search')
HEARING_TERMS = ('public comment', 'public hearing', 'regularly scheduled public meeting',
                 'board of supervisors meeting', 'city council meeting', 'open meeting', 'agenda item')
DIMENSIONS = ('isolated_positive', 'repeated_two_unit_positive', 'mixed_audit_hearing', 'empty_neutral',
              'word_boundary_near_miss', 'absent_package_rules')
FAMILIES = {
    'audit': {'function': 'detect_audit_log', 'mention': 'audit_log_mention', 'absent': 'audit_log_absent'},
    'hearing': {'function': 'detect_public_hearing', 'mention': 'public_hearing_mention',
                'absent': 'public_hearing_absent'},
}
HIT_KEYS = {'detector', 'detector_version', 'rule_id', 'citation', 'locator', 'excerpt', 'kind', 'detail'}
FORBIDDEN_KEYS = re.compile(r'(?i)confidence|complian')
CONTROLS = ('audit-never', 'hearing-never', 'swapped-names', 'spurious-absence')


def load_spec():
    return json.loads(FIXTURE.read_text())


@contextmanager
def offline_scope():
    """Scoped guard for this pure call graph, not a hostile-code sandbox."""
    def forbidden(*args, **kwargs):
        raise RuntimeError('fc12 offline call graph violation')

    with ExitStack() as stack:
        stack.enter_context(patch.object(subprocess, 'Popen', forbidden))
        stack.enter_context(patch.object(socket.socket, 'connect', forbidden))
        stack.enter_context(patch.object(socket.socket, 'connect_ex', forbidden))
        stack.enter_context(patch.object(socket.socket, 'bind', forbidden))
        stack.enter_context(patch.object(socket, 'create_connection', forbidden))
        stack.enter_context(patch.object(socket, 'getaddrinfo', forbidden))
        yield


def all_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from all_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from all_keys(item)


class AuditHearingTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(offline_scope())
        from campaign_tool.digest import detectors
        self.d = detectors
        self.spec = load_spec()
        self.assertEqual(self.spec['schema'], 'fc12-audit-hearing/v1')
        self.assertEqual(self.spec['authority'], 'test_only')

    def units(self, case):
        if 'text' in case:
            return self.d.units_from_text(case['text'])
        return copy.deepcopy(case['units'])

    def expected_hit(self, family, case, mention):
        rule = self.spec['expected_rules'][case['package']][family]
        base = {'detector_version': self.spec['detector_version'], 'rule_id': rule['rule_id'],
                'citation': rule['citation']}
        if mention is None:
            return {**base, 'detector': FAMILIES[family]['absent'], 'locator': {'scope': 'document'},
                    'excerpt': '', 'kind': 'absence', 'detail': {'note': self.spec['absence_notes'][family]}}
        return {**base, 'detector': FAMILIES[family]['mention'], 'locator': mention['locator'],
                'excerpt': mention['excerpt'], 'kind': 'mention', 'detail': {}}

    def check(self, case, family):
        names = FAMILIES[family]
        other = {v for k, f in FAMILIES.items() if k != family for v in (f['mention'], f['absent'])}
        units = self.units(case)
        before = copy.deepcopy(units)
        package = copy.deepcopy(self.spec['packages'][case['package']])
        # Module attribute lookup at call time: real API unless a declared control patched it.
        hits = getattr(self.d, names['function'])(units, package)
        print('SUBCASE ' + json.dumps({'case': case['id'], 'family': family, 'detectors': [h['detector'] for h in hits]},
                                      sort_keys=True), flush=True)
        self.assertEqual(units, before, 'input units must not be mutated')
        detected = [hit['detector'] for hit in hits]
        self.assertFalse(set(detected) & other, 'DECIDE family-isolation:' + family)
        expected = case['expect'][family]
        if expected:
            self.assertIn(names['mention'], detected, 'DECIDE mention-emitted:' + family)
            self.assertNotIn(names['absent'], detected, 'DECIDE no-spurious-absence:' + family)
            self.assertEqual(detected, [names['mention']] * len(expected), 'exact mention count/order')
            self.assertEqual(hits, [self.expected_hit(family, case, m) for m in expected], 'exact mention hits')
        else:
            self.assertEqual(hits, [self.expected_hit(family, case, None)], 'DECIDE absence-marker:' + family)
        for hit in hits:
            self.assertEqual(set(hit), HIT_KEYS, 'exact hit fields')
            self.assertFalse([k for k in all_keys(hit) if FORBIDDEN_KEYS.search(k)], 'no confidence/compliance')

    def test_vocabulary_frozen(self):
        self.assertEqual(self.spec['audit_terms'], list(AUDIT_TERMS))
        self.assertEqual(self.spec['hearing_terms'], list(HEARING_TERMS))
        isolated = {c['term']: c for c in self.spec['cases'] if c['dimension'] == 'isolated_positive'}
        self.assertEqual(set(isolated), set(AUDIT_TERMS) | set(HEARING_TERMS))
        for term in (*AUDIT_TERMS, *HEARING_TERMS):
            with self.subTest(term=term):
                case = isolated[term]
                family, other = ('audit', 'hearing') if term in AUDIT_TERMS else ('hearing', 'audit')
                self.assertEqual(len(case['expect'][family]), 1)
                self.assertEqual(case['expect'][other], [])
                self.assertIn(term, case['expect'][family][0]['excerpt'].lower())

    def test_dimensions_covered(self):
        self.assertEqual(self.spec['dimensions'], list(DIMENSIONS))
        self.assertEqual({c['dimension'] for c in self.spec['cases']}, set(DIMENSIONS))
        self.assertEqual(sorted({c['package'] for c in self.spec['cases']}), ['empty', 'none', 'synthetic', 'unrelated'])
        ids = [c['id'] for c in self.spec['cases']]
        self.assertEqual(len(ids), len(set(ids)))

    def test_run_detectors_composition(self):
        """run_detectors carries each family's own result, unchanged and in DETECTORS order."""
        wanted = {v for f in FAMILIES.values() for v in (f['mention'], f['absent'])}
        for case in self.spec['cases']:
            with self.subTest(case=case['id']):
                package = self.spec['packages'][case['package']]
                composed = [h for h in self.d.run_detectors(self.units(case), package) if h['detector'] in wanted]
                direct = (self.d.detect_audit_log(self.units(case), package)
                          + self.d.detect_public_hearing(self.units(case), package))
                self.assertEqual(composed, direct)
                self.assertEqual(self.d.DETECTOR_VERSION, self.spec['detector_version'])


def case_test(case_id, family):
    def test(self):
        case = next(c for c in self.spec['cases'] if c['id'] == case_id)
        self.check(case, family)
    test.__doc__ = 'Frozen case ' + case_id + ' / ' + family
    return test


CASE_FAMILY = [(c['id'], family) for c in load_spec()['cases'] for family in FAMILIES]
for _case, _family in CASE_FAMILY:
    setattr(AuditHearingTests, 'test_case_' + _case + '__' + _family, case_test(_case, _family))


def derived_function(detectors, name, old, new):
    """Copy one staged product function mechanically and change only the named seam."""
    source = Path(detectors.__file__).read_text()
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == name)
    segment = ast.get_source_segment(source, node)
    if segment is None or segment.count(old) != 1:
        raise RuntimeError('control seam not unique in ' + name)
    namespace = dict(vars(detectors))
    exec(compile(segment.replace(old, new), detectors.__file__ + '#fc12-control-' + name, 'exec'), namespace)
    return namespace[name], {'function': name, 'old': old, 'new': new}


def install_control(stack, detectors, control):
    never = re.compile(r'(?!)')
    if control == 'audit-never':
        stack.enter_context(patch.object(detectors, 'AUDIT', never))
        return [{'pattern': 'AUDIT', 'new': never.pattern}]
    if control == 'hearing-never':
        stack.enter_context(patch.object(detectors, 'HEARING', never))
        return [{'pattern': 'HEARING', 'new': never.pattern}]
    if control == 'swapped-names':
        seams = [('detect_audit_log', '"audit_log_mention"', '"public_hearing_mention"'),
                 ('detect_public_hearing', '"public_hearing_mention"', '"audit_log_mention"')]
    else:
        seams = [('detect_audit_log', '    if not hits:', '    if True:'),
                 ('detect_public_hearing', '    if not hits:', '    if True:')]
    replaced = {}
    described = []
    for name, old, new in seams:
        function, info = derived_function(detectors, name, old, new)
        replaced[getattr(detectors, name)] = function
        stack.enter_context(patch.object(detectors, name, function))
        described.append(info)
    stack.enter_context(patch.object(detectors, 'DETECTORS', [replaced.get(f, f) for f in detectors.DETECTORS]))
    return described


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.started_ids = []

    def startTest(self, test):
        self.started_ids.append(test.id())
        super().startTest(test)


def run_suite(suite, summary_path, control=None):
    result = cast(RecordingResult, unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(suite))
    names = {test_id.rsplit('.', 1)[-1] for test_id in result.started_ids}
    cells = [[c, f] for c, f in CASE_FAMILY if 'test_case_' + c + '__' + f in names]
    spec = load_spec()
    dimensions = sorted({c['dimension'] for c in spec['cases'] if any(cell[0] == c['id'] for cell in cells)})
    summary = {'tests_run': result.testsRun, 'test_ids': result.started_ids,
               'failures': [{'id': t.id(), 'traceback': tb} for t, tb in result.failures],
               'errors': [{'id': t.id(), 'traceback': tb} for t, tb in result.errors],
               'skipped': [[t.id(), reason] for t, reason in result.skipped],
               'successful': result.wasSuccessful(), 'case_family_cells': cells,
               'dimensions': [d for d in DIMENSIONS if d in dimensions], 'control': control}
    Path(summary_path).write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n')
    return 0 if result.wasSuccessful() and not result.skipped else 1


def script_main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--temporary-root', type=Path, required=True)
    parser.add_argument('--summary', type=Path, required=True)
    parser.add_argument('--mode', choices=('full', 'discovery', 'compatibility'), default='full')
    parser.add_argument('--control', choices=CONTROLS)
    args = parser.parse_args()
    root = args.temporary_root
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ValueError('existing child-local temporary root required')
    # Isolated script process only; ordinary discovery performs none of this.
    tempfile.tempdir = str(root)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(REPO))
    with offline_scope(), ExitStack() as stack:
        from campaign_tool.digest import detectors
        control = None
        if args.control:
            control = {'name': args.control, 'seams': install_control(stack, detectors, args.control)}
            print('MUTATION ' + json.dumps(control, sort_keys=True), flush=True)
        loader = unittest.TestLoader()
        if args.mode == 'compatibility':
            suite = loader.loadTestsFromName('tests.runner.test_digest.DetectorTests')
        elif args.mode == 'discovery':
            import os
            before = (dict(os.environ), tempfile.tempdir, socket.socket.connect, subprocess.Popen,
                      detectors.AUDIT, detectors.HEARING, list(detectors.DETECTORS))
            events = []

            class Sentinel(unittest.TestCase):
                def runTest(self):
                    self.assertEqual(before, (dict(os.environ), tempfile.tempdir, socket.socket.connect,
                                              subprocess.Popen, detectors.AUDIT, detectors.HEARING,
                                              list(detectors.DETECTORS)))
                    events.append('sentinel')

            if run_suite(unittest.TestSuite([Sentinel()]), root / 'sentinel-before.json'):
                return 1
            suite = loader.discover(str(REPO / 'tests/records'), pattern=Path(__file__).name,
                                    top_level_dir=str(REPO))
            if before != (dict(os.environ), tempfile.tempdir, socket.socket.connect, subprocess.Popen,
                          detectors.AUDIT, detectors.HEARING, list(detectors.DETECTORS)):
                raise RuntimeError('discovery contaminated ambient state')
            result = run_suite(unittest.TestSuite([*suite, Sentinel()]), args.summary)
            if events != ['sentinel', 'sentinel']:
                raise RuntimeError('sentinels did not execute')
            return result
        else:
            suite = loader.loadTestsFromTestCase(AuditHearingTests)
        return run_suite(suite, args.summary, control)


if __name__ == '__main__':
    raise SystemExit(script_main())
