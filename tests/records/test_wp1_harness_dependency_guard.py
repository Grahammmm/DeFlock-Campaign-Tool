"""Absent WP1 may skip integration; broken installed WP1 must stay an error.

Each child imports the real extraction and portal test harnesses, then isolates
only ledger lookup. No extraction, portal fetch or domain validator runs in
these setup-boundary probes.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


_PROBE = r'''
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import campaign_tool.records as records

mode = sys.argv[1]
targets = {
    'extraction': ('tests.records.test_extraction_ledger', 'ExtractionLedgerTests'),
    'portal': ('tests.test_records_portal_wp1', 'WP1PortalTests'),
}
classes = {name: getattr(importlib.import_module(module), cls)
    for name, (module, cls) in targets.items()}
repository = Path(records.__file__).resolve().parents[2]
os.environ.pop('RECORDS_WP1_SOURCE_ROOT', None)
fixtures = {
    'absent': None,
    'missing-store': {'__init__.py': ''},
    'package-self-named': {'__init__.py': "raise ModuleNotFoundError('synthetic WP1 package failure', name='campaign_tool.records.ledger')\n"},
    'store-self-named': {'__init__.py': '', 'store.py': "raise ModuleNotFoundError('synthetic WP1 store failure', name='campaign_tool.records.ledger.store')\n"},
    'transitive-error': {'__init__.py': '', 'store.py': 'import synthetic_missing_wp1_harness_transitive\n'},
    'import-error': {'__init__.py': '', 'store.py': "raise ImportError('synthetic WP1 initialization failure')\n"},
    'runtime-error': {'__init__.py': '', 'store.py': "raise RuntimeError('synthetic WP1 runtime failure')\n"},
    'invalid-overlay': None,
}
with tempfile.TemporaryDirectory(prefix='wp1-harness-import-') as temporary:
    root = Path(temporary)
    if mode != 'actual':
        files = fixtures[mode]
        if files is not None:
            package = root / 'ledger'
            package.mkdir()
            for name, body in files.items():
                (package / name).write_text(body)
        for name in tuple(sys.modules):
            if name == 'campaign_tool.records.ledger' or name.startswith('campaign_tool.records.ledger.'):
                del sys.modules[name]
        if hasattr(records, 'ledger'):
            delattr(records, 'ledger')
        records.__path__ = [str(root)]
    if mode == 'invalid-overlay':
        os.environ['RECORDS_WP1_SOURCE_ROOT'] = str(root)
    outcomes = {}
    for name, cls in classes.items():
        try:
            cls.setUpClass()
        except unittest.SkipTest as error:
            outcomes[name] = {'status': 'skipped', 'message': str(error)}
        except (ImportError, RuntimeError) as error:
            outcomes[name] = {'status': 'error', 'error_class': type(error).__name__,
                'error_name': getattr(error, 'name', None), 'message': str(error)}
        else:
            expected = repository / 'campaign_tool/records/ledger/store.py'
            outcomes[name] = {'status': 'ready',
                'actual_store_origin': Path(cls.store.__file__).resolve() == expected}
    print(json.dumps(outcomes))
'''


class WP1HarnessDependencyGuardTests(unittest.TestCase):
    def probe(self, mode):
        environment = dict(os.environ)
        environment['RECORDS_TEST_DEPENDENCIES'] = '[]'
        environment.pop('RECORDS_WP1_SOURCE_ROOT', None)
        process = subprocess.run([sys.executable, '-B', '-c', _PROBE, mode],
            cwd=Path(__file__).resolve().parents[2], env=environment,
            capture_output=True, text=True, timeout=20, check=True)
        return json.loads(process.stdout)

    def test_actual_wp1_setup_uses_the_checkout_store(self):
        self.assertEqual(self.probe('actual'), {
            'extraction': {'status': 'ready', 'actual_store_origin': True},
            'portal': {'status': 'ready', 'actual_store_origin': True},
        })

    def test_genuinely_absent_wp1_keeps_explicit_skips(self):
        self.assertEqual(self.probe('absent'), {
            'extraction': {'status': 'skipped', 'message': 'explicit WP1 dependency required'},
            'portal': {'status': 'skipped', 'message': 'WP1 dependency unavailable; explicit source overlay required'},
        })

    def assert_errors(self, mode, error_class, message, error_name=None):
        outcomes = self.probe(mode)
        self.assertEqual(set(outcomes), {'extraction', 'portal'})
        for name, result in outcomes.items():
            with self.subTest(harness=name):
                self.assertEqual(result['status'], 'error')
                self.assertEqual(result['error_class'], error_class)
                self.assertEqual(result['error_name'], error_name)
                self.assertIn(message, result['message'])

    def test_installed_package_without_store_is_not_skipped(self):
        self.assert_errors('missing-store', 'ModuleNotFoundError',
            'campaign_tool.records.ledger.store', 'campaign_tool.records.ledger.store')

    def test_package_self_named_error_is_not_absence(self):
        self.assert_errors('package-self-named', 'ModuleNotFoundError',
            'synthetic WP1 package failure', 'campaign_tool.records.ledger')

    def test_store_self_named_error_is_not_absence(self):
        self.assert_errors('store-self-named', 'ModuleNotFoundError',
            'synthetic WP1 store failure', 'campaign_tool.records.ledger.store')

    def test_missing_transitive_dependency_is_not_skipped(self):
        self.assert_errors('transitive-error', 'ModuleNotFoundError',
            'synthetic_missing_wp1_harness_transitive', 'synthetic_missing_wp1_harness_transitive')

    def test_installed_import_error_still_propagates(self):
        self.assert_errors('import-error', 'ImportError', 'synthetic WP1 initialization failure')

    def test_installed_runtime_error_still_propagates(self):
        self.assert_errors('runtime-error', 'RuntimeError', 'synthetic WP1 runtime failure')

    def test_invalid_explicit_overlay_is_not_optional_absence(self):
        outcomes = self.probe('invalid-overlay')
        for name, message in (
                ('extraction', 'invalid explicit overlay'),
                ('portal', 'WP1 is in tree; unset RECORDS_WP1_SOURCE_ROOT')):
            with self.subTest(harness=name):
                self.assertEqual(outcomes[name], {'status': 'error',
                    'error_class': 'RuntimeError', 'error_name': None, 'message': message})
