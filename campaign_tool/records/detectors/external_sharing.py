"""Pure external-sharing observations; use core.evaluate for blocked/skipped outcomes."""
from .core import evaluate


def detect(units, joins, rules, config):
    return evaluate("external-sharing", units, joins, rules, config)["hits"]
