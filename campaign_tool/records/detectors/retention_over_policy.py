"""Pure retention-over-policy observations; use core.evaluate for blocked/skipped outcomes."""
from .core import evaluate


def detect(units, joins, rules, config):
    return evaluate("retention-over-policy", units, joins, rules, config)["hits"]
