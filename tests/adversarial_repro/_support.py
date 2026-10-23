"""Shared helpers for the adversarial reproductions. Synthetic data only; loopback sockets at most.

Every test asserts the CORRECT behaviour, so a failing test is the proof of a defect
on the current source. All tests are skipped unless ADVERSARIAL_REPRO=1.
"""
import os
import unittest

ENABLED = True  # admitted regression tests are always enabled
requires_repro = unittest.skipUnless(ENABLED, "set ADVERSARIAL_REPRO=1 to run adversarial reproductions")


def call(case, function, *args, **kwargs):
    """Run ``function``; an exception it should have handled becomes a clear test failure."""
    try:
        return function(*args, **kwargs)
    except Exception as error:  # noqa: BLE001 - the point is to catch what escapes
        case.fail(f"unhandled {type(error).__name__} escaped: {str(error)[:120]!r}")
