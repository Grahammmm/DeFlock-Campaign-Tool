#!/usr/bin/env python3
"""Flock privacy patterns: the report, page and publish lints share the one copy of the email, phone and plate regexes.
The patterns moved to campaign_tool/privacy_patterns.py (G decision 2026-11-04: it is in the wheel, tools/ is not); this file loads
that module by path (importlib, like the other siblings) and re-exports EMAIL, PHONE, PLATE, PATTERNS and categories(text), so
every caller keeps its import. It has no CLI and no side effects. Lints only refuse: callers report the category name, never the
matched text. The pattern rules (what each category matches and deliberately does not) are documented in that module.
"""
import importlib.util
from pathlib import Path

_pat = importlib.util.spec_from_file_location("_df_privacy_patterns", Path(__file__).resolve().parents[1] / "campaign_tool" / "privacy_patterns.py")
_patterns = importlib.util.module_from_spec(_pat)
_pat.loader.exec_module(_patterns)
EMAIL, PHONE, PLATE, PATTERNS, categories = _patterns.EMAIL, _patterns.PHONE, _patterns.PLATE, _patterns.PATTERNS, _patterns.categories


# --- DF-4 legal-claim wording guard (ALERT-ONLY) ------------------------------------------------------------------------
# The rules (and the boundary between asserting a violation and stating a requirement or a fact) live in
# campaign_tool/legal_claims.py so the packaged newsletter and site build use the same single copy; they are exposed here, beside
# the email/phone/plate patterns, for the report, page and publish lints. Callers print review-required notes and carry on.
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("_df4_legal_claims", Path(__file__).resolve().parents[1] / "campaign_tool" / "legal_claims.py")
_legal = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_legal)
legal_claims, legal_notes = _legal.legal_claims, _legal.legal_notes


def gated_notes(items, gated):
    """items: iterable of (where, text); gated: flock_compare.GATED -> ["review-required: <where>: quote-gated wording", ...].
    DF-4 operator ruling 1 (2026-11-02): the older quote-gated word list is ALERT-ONLY like the guard above, on every lint that
    used to refuse it. Callers print these notes and carry on: never an error, a refusal or an exit code. Never the text."""
    try:
        return ["review-required: %s: quote-gated wording" % where for where, text in items if isinstance(text, str) and gated.search(text)]
    except Exception:  # noqa: BLE001 - alert-only: a broken scan asks for a human, it never stops the run
        return ["review-required: quote-gated wording (guard-error)"]


def review_notes(items, gated):
    """legal_notes then gated_notes over the same (where, text) items: the one call the report, page and publish lints make."""
    items = list(items)
    return legal_notes(items) + gated_notes(items, gated)
