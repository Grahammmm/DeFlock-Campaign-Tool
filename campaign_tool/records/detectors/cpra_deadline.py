"""Pure cpra-deadline observations; use core.evaluate for blocked/skipped outcomes."""
from .core import evaluate


def detect(units, joins, rules, config):
    return evaluate("cpra-deadline", units, joins, rules, config)["hits"]
