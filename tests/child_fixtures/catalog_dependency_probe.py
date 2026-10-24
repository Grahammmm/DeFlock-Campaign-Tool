"""Child probe for tests/records/test_catalog_dependency_guard.py (CatalogDependencyGuardTests.probe).

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
