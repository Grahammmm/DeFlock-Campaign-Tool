"""Only an absent PR19 module may disable the canonical catalog harness.

Subprocesses keep the real WP1 and catalog code loaded, then isolate PR19
lookup to empty or deliberately broken synthetic import fixtures. No domain
validator is replaced to obtain a successful catalog or acceptance result.
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
from pathlib import Path
import sys
import tempfile
import unittest
import campaign_tool.records as records
from campaign_tool.records import ledger_catalog, ledger_catalog_acceptance

mode = sys.argv[1]
fixtures = {
    'absent': None,
    'module-import-error': "raise ImportError('synthetic PR19 module failure')\n",
    'package-import-error': "raise ImportError('synthetic PR19 package failure')\n",
    'same-name-error': "raise ModuleNotFoundError('synthetic PR19 self-named failure', name='campaign_tool.records.catalog_links')\n",
    'transitive-error': 'import synthetic_missing_pr19_transitive_package\n',
    'runtime-error': "raise RuntimeError('synthetic PR19 runtime failure')\n",
    'missing-registry': 'SCHEMA_VERSION = 1\n',
}
with tempfile.TemporaryDirectory(prefix='pr19-import-boundary-') as temporary:
    root = Path(temporary)
    body = fixtures[mode]
    if body is not None:
        if mode == 'package-import-error':
            package = root / 'catalog_links'
            package.mkdir()
            (package / '__init__.py').write_text(body)
        else:
            (root / 'catalog_links.py').write_text(body)
    for name in tuple(sys.modules):
        if name == 'campaign_tool.records.catalog_links' or name.startswith('campaign_tool.records.catalog_links.'):
            del sys.modules[name]
    if hasattr(records, 'catalog_links'):
        delattr(records, 'catalog_links')
    # These real modules and the real ledger stay loaded; only PR19 lookup moves.
    records.__path__ = [str(root)]
    try:
        fixture = importlib.import_module('tests.records.test_ledger_catalog')
        importlib.import_module('tests.records.test_ledger_catalog_acceptance')
    except (ImportError, RuntimeError) as error:
        print(json.dumps({'loaded': False, 'error_class': type(error).__name__,
            'error_name': getattr(error, 'name', None), 'error': str(error)}))
    else:
        names = [
            'tests.records.test_ledger_catalog.OverlayTests.test_empty_unknown_allowed',
            'tests.records.test_ledger_catalog.CanonicalCatalogTests.test_every_original_unknown_card',
            'tests.records.test_ledger_catalog_acceptance.SnapshotAcceptanceTests.test_transactional_pointer_exact_bindings',
        ]
        if mode == 'missing-registry':
            names = ['tests.records.test_ledger_catalog_acceptance.SnapshotAcceptanceTests.test_dependency_contract_preserves_pr19']
        suite = unittest.defaultTestLoader.loadTestsFromNames(names)
        result = unittest.TestResult()
        suite.run(result)
        print(json.dumps({'loaded': True, 'tests': result.testsRun,
            'skips': len(result.skipped), 'errors': len(result.errors),
            'failures': len(result.failures),
            'error_details': [detail.splitlines()[-1] for _, detail in result.errors]}))
'''


class LedgerCatalogDependencyGuardTests(unittest.TestCase):
    def probe(self, mode):
        environment = dict(os.environ)
        environment['RECORDS_TEST_DEPENDENCIES'] = '[]'
        process = subprocess.run([sys.executable, '-B', '-c', _PROBE, mode],
            cwd=Path(__file__).resolve().parents[2], env=environment,
            capture_output=True, text=True, timeout=20, check=True)
        return json.loads(process.stdout)

    def test_absent_pr19_skips_only_canonical_and_acceptance_cases(self):
        self.assertEqual(self.probe('absent'), {
            'loaded': True, 'tests': 3, 'skips': 2, 'errors': 0,
            'failures': 0, 'error_details': [],
        })

    def assert_import_failure(self, mode, error_class, message, error_name=None):
        result = self.probe(mode)
        self.assertFalse(result['loaded'])
        self.assertEqual(result['error_class'], error_class)
        self.assertEqual(result['error_name'], error_name)
        self.assertIn(message, result['error'])

    def test_installed_module_import_error_is_not_skipped(self):
        self.assert_import_failure('module-import-error', 'ImportError',
            'synthetic PR19 module failure')

    def test_installed_package_import_error_is_not_skipped(self):
        self.assert_import_failure('package-import-error', 'ImportError',
            'synthetic PR19 package failure')

    def test_installed_self_named_missing_module_error_is_not_absence(self):
        self.assert_import_failure('same-name-error', 'ModuleNotFoundError',
            'synthetic PR19 self-named failure', 'campaign_tool.records.catalog_links')

    def test_transitive_dependency_error_is_not_skipped(self):
        self.assert_import_failure('transitive-error', 'ModuleNotFoundError',
            'synthetic_missing_pr19_transitive_package', 'synthetic_missing_pr19_transitive_package')

    def test_installed_runtime_error_still_propagates(self):
        self.assert_import_failure('runtime-error', 'RuntimeError',
            'synthetic PR19 runtime failure')

    def test_missing_export_remains_a_genuine_acceptance_error(self):
        result = self.probe('missing-registry')
        self.assertTrue(result['loaded'])
        self.assertEqual((result['tests'], result['skips'], result['errors'], result['failures']),
            (1, 0, 1, 0))
        self.assertEqual(result['error_details'],
            ['campaign_tool.records.ledger_catalog.CatalogError: pr19_dependency_incompatible'])
