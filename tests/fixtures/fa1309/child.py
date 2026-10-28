"""File-launched inert suite child; fixed modes, no inline source execution."""
import json
from pathlib import Path
import sys
import unittest

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
from tests.fixtures.fa1309 import support

COMPATIBILITY = [
    'tests.records.test_ocr.OCRTests.test_text_and_confidence_come_from_one_tesseract_run',
    'tests.records.test_ocr.OCRTests.test_low_confidence_receipt_index_and_idempotent_rerun',
    'tests.records.test_ocr.OCRTests.test_high_confidence_remains_unreviewed',
    'tests.records.test_ocr.OCRTests.test_no_confidence_no_text_and_missing_dependency',
]


def main():
    if len(sys.argv) != 3:
        return 2
    mode, root = sys.argv[1], support.safe_directory(sys.argv[2])
    selectors = {
        'behavior': ['tests.records.test_fa1309.Behavior'],
        'compatibility': COMPATIBILITY,
        'negative-control': ['tests.records.test_fa1309.GateControl'],
        'isolation': ['tests.records.test_fa1309.Isolation'],
        'deciding': ['tests.records.test_fa1309.Behavior.test_duplicate_conf_low_high',
                     'tests.records.test_fa1309.Behavior.test_unique_headers_and_producer_shape'],
    }
    with support.boundary(root):
        if mode == 'probe':
            import tempfile
            from campaign_tool.records.extract import ocr
            probe = {'module': str(Path(ocr.__file__).absolute()),
                     'attempts': list(support.ATTEMPTS), 'bytecode_disabled': sys.dont_write_bytecode,
                     'default_runner_guarded': ocr.extract_image_only_pages.__kwdefaults__['runner'] is __import__('subprocess').run,
                     'tempdir': tempfile.gettempdir()}
            (root / 'probe.json').write_text(json.dumps(probe, indent=2) + '\n')
            return int(bool(probe['attempts']))
        if mode not in selectors:
            return 2
        suite = unittest.defaultTestLoader.loadTestsFromNames(selectors[mode])
        selected = suite.countTestCases()
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        summary = {'selected': selected, 'tests': result.testsRun,
                   'failures': [t.id() for t, _ in result.failures],
                   'errors': [t.id() for t, _ in result.errors],
                   'skipped': [t.id() for t, _ in result.skipped],
                   'unexpected_successes': [t.id() for t in result.unexpectedSuccesses],
                   'expected_failures': [t.id() for t, _ in result.expectedFailures],
                   'attempts': support.ATTEMPTS}
        (root / 'result.json').write_text(json.dumps(summary, indent=2) + '\n')
        return int(not (selected > 0 and result.testsRun == selected and result.wasSuccessful()
                       and not result.skipped and not result.expectedFailures and not support.ATTEMPTS))


if __name__ == '__main__':
    raise SystemExit(main())
