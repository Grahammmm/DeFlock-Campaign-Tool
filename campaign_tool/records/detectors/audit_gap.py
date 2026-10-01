"""Pure audit-gap observations; use core.evaluate for blocked/skipped outcomes."""
from .core import evaluate


def detect(units, joins, rules, config):
    return evaluate("audit-gap", units, joins, rules, config)["hits"]
