"""Pure volume-anomaly observations; use core.evaluate for blocked/skipped outcomes."""
from .core import evaluate


def detect(units, joins, rules, config):
    return evaluate("volume-anomaly", units, joins, rules, config)["hits"]
