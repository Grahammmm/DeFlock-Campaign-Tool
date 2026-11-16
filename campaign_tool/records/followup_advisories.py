"""Advice-only wording notes for new follow-up drafts (clock and engine).

Reuses the shared DF2007-15/DF0652-11 scanners and the DF0652-08 former-CPRA check. Notes are fixed text
(never submitted text or an exception) and never block, edit, sign, send or publish anything.
"""
from .. import legal_claims
from . import denial_reply_check

WHERE = "follow-up draft"
FORMER_CODE = "former_cpra_citation"
FORMER_GUARD_ERROR = "warning: %s: %s (guard-error). Review citations manually." % (WHERE, FORMER_CODE)


def notes(body):
    """-> ordered, de-duplicated list of fixed advisory strings for a follow-up body ([] when clean)."""
    out = []
    try:
        shared = legal_claims.legal_notes([(WHERE, body)])
    except Exception:  # noqa: BLE001 - advice only
        shared = [legal_claims.ADVISORY_GUARD_ERROR]
    try:
        former = ["warning: %s: %s. %s" % (WHERE, w["code"], w["message"])
                  for w in denial_reply_check.check(body, subject_agency="") if w.get("code") == FORMER_CODE]
    except Exception:  # noqa: BLE001 - advice only
        former = [FORMER_GUARD_ERROR]
    for note in list(shared) + former:
        if isinstance(note, str) and note not in out:
            out.append(note)
    return out
