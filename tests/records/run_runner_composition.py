"""Required offline WP2 composition suite with an explicit WP1 source overlay.

Execute this file, not stdin, so multiprocessing spawn can reload its guarded
entry point. No tests execute when a spawned interpreter imports the launcher.
"""
import importlib
import os
from pathlib import Path
import sys
import unittest

REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY))
TEST_MODULES = (
    'tests.records.test_mail_delta',
    'tests.records.test_runner',
    'tests.records.test_export_proof',
    'tests.records.test_runner_wp1_overlay',
    'tests.records.test_runner_reliability',
)


def require_origin(module, expected):
    source = getattr(module, '__file__', None)
    if source is None or Path(source).resolve() != expected.resolve():
        raise RuntimeError('composition module origin mismatch: ' + module.__name__)


def check_loaded_origins(overlay):
    for name, module in tuple(sys.modules.items()):
        if not (name == 'campaign_tool' or name.startswith('campaign_tool.')
                or name == 'tests' or name.startswith('tests.records')):
            continue
        source = getattr(module, '__file__', None)
        if source is None:
            raise RuntimeError('composition module has no source origin: ' + name)
        expected_root = (overlay / 'campaign_tool' / 'records' / 'ledger'
                         if name == 'campaign_tool.records.ledger'
                         or name.startswith('campaign_tool.records.ledger.')
                         else REPOSITORY)
        if not Path(source).resolve().is_relative_to(expected_root.resolve()):
            raise RuntimeError('composition module outside expected source: ' + name)


def main():
    configured = os.environ.get('WP1_OVERLAY')
    if not configured:
        raise RuntimeError('WP1_OVERLAY is required; integration must not skip')
    overlay = Path(configured).resolve(strict=True)
    if overlay == REPOSITORY:
        raise RuntimeError('WP1 must be a separate explicit source dependency')
    source_root = os.environ.get('RECORDS_WP1_SOURCE_ROOT')
    if source_root and Path(source_root).resolve(strict=True) != overlay:
        raise RuntimeError('WP1 source-root configuration mismatch')
    os.environ['RECORDS_WP1_SOURCE_ROOT'] = str(overlay)
    records = importlib.import_module('campaign_tool.records')
    test_records = importlib.import_module('tests.records')
    require_origin(records, REPOSITORY / 'campaign_tool' / 'records' / '__init__.py')
    require_origin(test_records, REPOSITORY / 'tests' / 'records' / '__init__.py')
    records.__path__.append(str(overlay / 'campaign_tool' / 'records'))
    test_records.__path__.append(str(overlay / 'tests' / 'records'))
    for name in ('campaign_tool.records.ledger',
                 'campaign_tool.records.ledger.store',
                 'campaign_tool.records.ledger.stages'):
        module = importlib.import_module(name)
        suffix = name.split('.')[3:]
        expected = overlay / 'campaign_tool' / 'records' / 'ledger'
        expected = expected.joinpath(*suffix).with_suffix('.py') if suffix else expected / '__init__.py'
        require_origin(module, expected)
    for name in TEST_MODULES:
        require_origin(importlib.import_module(name),
                       REPOSITORY.joinpath(*name.split('.')).with_suffix('.py'))
    check_loaded_origins(overlay)
    suite = unittest.defaultTestLoader.loadTestsFromNames(TEST_MODULES)
    if suite.countTestCases() == 0:
        raise RuntimeError('composition suite is empty')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    check_loaded_origins(overlay)
    if result.skipped or not result.wasSuccessful():
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
