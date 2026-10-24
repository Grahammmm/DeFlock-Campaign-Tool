"""Child probe for tests/records/test_wp1_harness_dependency_guard.py (WP1HarnessDependencyGuardTests.probe).

Moved verbatim from its inline source; arguments arrive in sys.argv order.
"""
import sys
if not sys.flags.isolated: sys.path[0] = ''


if __name__ == "__main__":
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
