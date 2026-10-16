"""Bound stored context artifact reads by the existing stage-content limit.

This bounds returned payload bytes, not snapshot construction or SQLite's total
memory/time. Oversized or non-blob rows fail closed; no truncated snapshot is
eligible for hashing, decoding or reuse.
"""
from .ledger.stages import MAX_CONTENT_BYTES


def read_artifact(connection, digest):
    row = connection.execute(
        "SELECT CASE WHEN typeof(payload)='blob' AND length(payload)<=? "
        "THEN payload ELSE NULL END FROM stage_artifacts WHERE sha256=?",
        (MAX_CONTENT_BYTES, digest)).fetchone()
    if row is None:
        return None
    if type(row[0]) is not bytes or len(row[0]) > MAX_CONTENT_BYTES:
        raise ValueError("context_snapshot_bound")
    return row[0]
