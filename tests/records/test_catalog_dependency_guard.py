"""Only a genuinely absent WP1 package may skip catalog tests.

Each subprocess loads the real catalog harness with an isolated ledger search
path. The empty path really has no ledger; broken installed packages are tiny
synthetic import fixtures, not replacement domain validators.
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
from campaign_tool.records import catalog_stage

mode = sys.argv[1]
fixtures = {
    'absent': None,
    'package-import-error': {'__init__.py': "raise ImportError('synthetic installed package failure')\n"},
    'missing-store': {'__init__.py': '', 'stages.py': ''},
    'missing-stages': {'__init__.py': '', 'store.py': ''},
    'store-module-error': {'__init__.py': '', 'store.py': "raise ModuleNotFoundError('synthetic store failure', name='campaign_tool.records.ledger.store')\n", 'stages.py': ''},
    'stages-module-error': {'__init__.py': '', 'store.py': '', 'stages.py': "raise ModuleNotFoundError('synthetic stages failure', name='campaign_tool.records.ledger.stages')\n"},
    'transitive-error': {'__init__.py': '', 'store.py': 'import synthetic_wp1_missing_transitive_package\n', 'stages.py': ''},
}
with tempfile.TemporaryDirectory(prefix='catalog-import-boundary-') as temporary:
    root = Path(temporary)
    files = fixtures[mode]
    if files is not None:
        package = root / 'ledger'
        package.mkdir()
        for name, text in files.items():
            (package / name).write_text(text)
    # Keep the real already-loaded catalog module, but isolate only WP1 lookup.
    for name in tuple(sys.modules):
        if name == 'campaign_tool.records.ledger' or name.startswith('campaign_tool.records.ledger.'):
            del sys.modules[name]
    if hasattr(records, 'ledger'):
        delattr(records, 'ledger')
    records.__path__ = [str(root)]
    try:
        fixture = importlib.import_module('tests.records.test_catalog_stage')
    except ImportError as error:
        print(json.dumps({'loaded': False, 'error_class': type(error).__name__, 'error_name': getattr(error, 'name', None)}))
    else:
        suite = unittest.defaultTestLoader.loadTestsFromNames([
            'tests.records.test_catalog_stage.CatalogStageTests.test_schema_only_card_stays_pending',
            'tests.records.test_catalog_stage_drift.CatalogDriftTests',
        ])
        result = unittest.TestResult()
        suite.run(result)
        print(json.dumps({'loaded': True, 'tests': result.testsRun, 'skips': len(result.skipped),
            'errors': len(result.errors), 'failures': len(result.failures)}))
'''


class CatalogDependencyGuardTests(unittest.TestCase):
    def probe(self, mode):
        environment = dict(os.environ)
        environment['RECORDS_TEST_DEPENDENCIES'] = '[]'
        process = subprocess.run([sys.executable, '-B', '-c', _PROBE, mode],
            cwd=Path(__file__).resolve().parents[2], env=environment,
            capture_output=True, text=True, timeout=20, check=True)
        return json.loads(process.stdout)

    def test_actual_absent_package_skips_base_and_all_drift_cases(self):
        self.assertEqual(self.probe('absent'),
            {'loaded': True, 'tests': 6, 'skips': 6, 'errors': 0, 'failures': 0})

    def assert_import_error(self, mode, error_class, error_name=None):
        result = self.probe(mode)
        self.assertFalse(result['loaded'])
        self.assertEqual(result['error_class'], error_class)
        if error_name is not None:
            self.assertEqual(result['error_name'], error_name)

    def test_broken_installed_package_import_error_is_not_skipped(self):
        self.assert_import_error('package-import-error', 'ImportError')

    def test_missing_store_in_installed_package_is_not_skipped(self):
        self.assert_import_error('missing-store', 'ImportError')

    def test_missing_stages_in_installed_package_is_not_skipped(self):
        self.assert_import_error('missing-stages', 'ImportError')

    def test_store_module_failure_is_not_skipped(self):
        self.assert_import_error('store-module-error', 'ImportError')

    def test_stages_module_failure_is_not_skipped(self):
        self.assert_import_error('stages-module-error', 'ImportError')

    def test_transitive_dependency_failure_is_not_skipped(self):
        self.assert_import_error('transitive-error', 'ModuleNotFoundError', 'synthetic_wp1_missing_transitive_package')
