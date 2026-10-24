"""Only a genuinely absent WP1 package may skip catalog tests.

Each subprocess loads the real catalog harness with an isolated ledger search
path. The empty path really has no ledger; broken installed packages are tiny
synthetic import fixtures, not replacement domain validators.
"""
import json
import os
from pathlib import Path
import unittest

from tests.child_fixtures import run_fixture


class CatalogDependencyGuardTests(unittest.TestCase):
    def probe(self, mode):
        environment = dict(os.environ)
        environment['RECORDS_TEST_DEPENDENCIES'] = '[]'
        process = run_fixture('catalog_dependency_probe', mode,
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
