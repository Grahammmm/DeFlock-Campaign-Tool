"""Pure purpose-quality observations; use core.evaluate for blocked/skipped outcomes."""
from .core import evaluate


def detect(units, joins, rules, config):
    return evaluate("purpose-quality", units, joins, rules, config)["hits"]
