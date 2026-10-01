"""File-based WP9/WP8 synthetic composition launcher; no installs or live effects.

In-tree mode: when this checkout contains WP8_MARKER, --wp8-root must be this
checkout itself; no overlay path is inserted and the separate-checkout guard is
skipped for WP8 only. Without the marker a separate pinned WP8 checkout is
still required. The exact count, zero skips and source-origin checks apply in
both modes.
"""
import argparse
import importlib
import os
from pathlib import Path
import sys
import unittest

EXPECTED_TESTS = 61
WP8_MARKER = "campaign_tool/records/review_bundle.py"


def require_origin(module_name, expected_file):
    module = importlib.import_module(module_name)
    actual = Path(module.__file__).resolve(strict=True)
    expected = expected_file.resolve(strict=True)
    if actual != expected:
        raise RuntimeError(f"Wrong dependency source for {module_name}")
    return module


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wp8-root", required=True, type=Path,
                        help="Actual WP8 checkout; CI must pin its reviewed commit.")
    args = parser.parse_args(argv)
    candidate = Path(__file__).resolve().parents[2]
    dependency = args.wp8_root.resolve(strict=True)
    in_tree = (candidate / WP8_MARKER).is_file()
    if in_tree and dependency != candidate:
        raise RuntimeError("WP8 is in tree; pass this checkout as --wp8-root")
    if not in_tree and dependency == candidate:
        raise RuntimeError("A separate explicitly pinned WP8 checkout is required")
    print("WP8 source: " + ("in-tree" if in_tree else "pinned checkout") + f" {dependency}",
          flush=True)
    os.environ["REQUIRE_WP8_INTEGRATION"] = "1"
    sys.path.insert(0, str(candidate))

    records = require_origin("campaign_tool.records",
                             candidate / "campaign_tool/records/__init__.py")
    tests = require_origin("tests.records", candidate / "tests/records/__init__.py")
    if not in_tree:
        records.__path__.insert(0, str(dependency / "campaign_tool/records"))
        tests.__path__.insert(0, str(dependency / "tests/records"))

    for name, relative in (
        ("campaign_tool.records.review_bundle",
         "campaign_tool/records/review_bundle.py"),
        ("campaign_tool.records.gates.validate_findings",
         "campaign_tool/records/gates/validate_findings.py"),
        ("tests.records.test_review_bundle",
         "tests/records/test_review_bundle.py"),
    ):
        require_origin(name, dependency / relative)

    for name, relative in (
        ("campaign_tool.records.recovery.publication",
         "campaign_tool/records/recovery/publication.py"),
        ("campaign_tool.records.recovery.review_gate",
         "campaign_tool/records/recovery/review_gate.py"),
        ("tests.records.test_recovery", "tests/records/test_recovery.py"),
        ("tests.records.test_publication_review_bridge",
         "tests/records/test_publication_review_bridge.py"),
    ):
        require_origin(name, candidate / relative)

    suite = unittest.defaultTestLoader.loadTestsFromNames([
        "tests.records.test_recovery",
        "tests.records.test_publication_review_bridge",
    ])
    discovered = suite.countTestCases()
    if discovered != EXPECTED_TESTS:
        raise RuntimeError(
            f"Focused scope changed: expected {EXPECTED_TESTS}, found {discovered}"
        )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    passed = (result.wasSuccessful() and not result.skipped
              and result.testsRun == EXPECTED_TESTS)
    print(f"WP9 composition ({'in-tree' if in_tree else 'pinned'} WP8): tests={result.testsRun}, "
          f"failures={len(result.failures)}, errors={len(result.errors)}, "
          f"skips={len(result.skipped)}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
