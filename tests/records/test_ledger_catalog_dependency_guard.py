"""Only an absent PR19 module may disable the canonical catalog harness.

Subprocesses keep the real WP1 and catalog code loaded, then isolate PR19
lookup to empty or deliberately broken synthetic import fixtures. No domain
validator is replaced to obtain a successful catalog or acceptance result.
"""
import json
import os
from pathlib import Path
import unittest

from tests.child_fixtures import run_fixture


class LedgerCatalogDependencyGuardTests(unittest.TestCase):
    def probe(self, mode):
        environment = dict(os.environ)
        environment['RECORDS_TEST_DEPENDENCIES'] = '[]'
        process = run_fixture('ledger_catalog_dependency_probe', mode,
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
