"""Pure, supplied-evidence triage detectors. No legal findings or persistence."""
from .core import DETECTORS, evaluate, run_all
__all__ = ["DETECTORS", "evaluate", "run_all"]
