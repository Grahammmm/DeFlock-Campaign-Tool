"""Durable publication hold for retirement of cached automated review.

Callers hold the existing root lock. Only protected issuer/authority metadata
selects work; no record text, schema migration or new validation grant is needed.
Current done review states form the retry queue, retaining immutable history.
"""
import json

KEY = "review-authority-retirement-v1"
LIMIT = 400
PENDING = json.dumps({"schema": KEY, "state": "pending"}, sort_keys=True)
COMPLETE = json.dumps({"schema": KEY, "state": "complete"}, sort_keys=True)
ELIGIBLE = (
    "FROM stage_state s LEFT JOIN receipts r ON r.sha256=s.receipt_sha256 "
    "LEFT JOIN stage_validation_authority v ON v.receipt_sha256=s.receipt_sha256 "
    "WHERE s.stage='review' AND s.status='done' "
    "AND (r.reviewer_id LIKE 'challenge:%' OR v.test_only=1)"
)


class RetirementError(ValueError):
    pass


def pending(con):
    row = con.execute("SELECT value FROM ledger_meta WHERE key=?", (KEY,)).fetchone()
    if row is None:
        return False
    if row[0] not in (PENDING, COMPLETE):
        raise RetirementError("review_authority_retirement_state_invalid")
    return row[0] == PENDING


def _write(con, value):
    con.execute("INSERT INTO ledger_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (KEY, value))


def arm(con):
    """Commit a hold before selection limits, cancellation, intake or row work.

    An existing pending hold can only be cleared by finish(), which rechecks
    the protected queue under a write transaction. Failures leave it pending.
    """
    held = pending(con)
    if con.execute("SELECT 1 " + ELIGIBLE + " LIMIT 1").fetchone() is not None:
        _write(con, PENDING)
        con.commit()
        return True
    return held


def finish(con):
    """Clear the hold only after every eligible current review has reopened."""
    con.execute("BEGIN IMMEDIATE")
    pending(con)  # Invalid metadata never gets silently replaced.
    if con.execute("SELECT 1 " + ELIGIBLE + " LIMIT 1").fetchone() is not None:
        con.rollback()
        return False
    if con.execute("SELECT 1 FROM ledger_meta WHERE key=?", (KEY,)).fetchone() is not None:
        _write(con, COMPLETE)
    con.commit()
    return True
