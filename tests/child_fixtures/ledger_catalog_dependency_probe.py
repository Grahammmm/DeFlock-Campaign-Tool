"""Child probe for tests/records/test_ledger_catalog_dependency_guard.py (LedgerCatalogDependencyGuardTests.probe).

Moved verbatim from its inline source; arguments arrive in sys.argv order.
"""
import sys
if not sys.flags.isolated: sys.path[0] = ''


if __name__ == "__main__":
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
