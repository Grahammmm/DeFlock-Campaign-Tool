"""Pure search-before-training observations; use core.evaluate for blocked/skipped outcomes."""
from .core import evaluate


def detect(units, joins, rules, config):
    return evaluate("search-before-training", units, joins, rules, config)["hits"]
