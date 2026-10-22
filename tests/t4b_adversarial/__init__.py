"""Adversarial T4B S1 tests: synthetic data only, skipped unless T4B_ADVERSARIAL=1.

They target PR #179 (strict policy, mail-resolution reader, append-only writer)
and need its modules plus tests/records fixtures on the import path.
"""
import os
import unittest


def require_enabled():
    if os.environ.get("T4B_ADVERSARIAL") != "1":
        raise unittest.SkipTest("set T4B_ADVERSARIAL=1 to run the T4B S1 adversarial tests")
    try:
        from campaign_tool.records.ledger import mail_resolution_policy  # noqa: F401
        from campaign_tool.records.ledger import original_resolution_mail  # noqa: F401
        from tests.records import test_original_resolution_mail  # noqa: F401
    except ImportError as exc:
        raise unittest.SkipTest("T4B S1 modules are not on this checkout: %s" % exc)
