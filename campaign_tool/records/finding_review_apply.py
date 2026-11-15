"""Apply journaled console finding decisions at the next analysis tick (DF2007-06).

The console (`console_findings`) only appends a ``finding_review`` entry to its decision journal. ``records analysis-tick
--root ROOT --from-journal FILE`` calls ``apply_journal`` first, before any agency DAG advance or packet staging: each entry
for this analysis root goes through the one DF2007-05 writer (`finding_review.approve` / `reject`, with ``expected_hash`` and the
journal ``entry_id``), and its final outcome is receipted in ``ROOT/.analysis/finding-decisions-applied.jsonl`` (0600,
append-only, locked, fsynced; entry id, UTC time and outcome only, never a signature, claim or reason).

Outcomes: APPLIED, STALE_FINDING (the finding changed since the page), SIGNATURE_REFUSED, ``REFUSED <code>`` (final);
ALREADY (a receipt exists). A missing trust anchor, a busy or unreadable review store and I/O faults are deferred: nothing is
receipted and the next tick tries again. An unreadable receipt file refuses the whole pass. Nothing here signs, verifies a
signature itself, sends or publishes.
"""
import fcntl
import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

from . import console_journal as cj, finding_review as fr

RECEIPTS = (".analysis", "finding-decisions-applied.jsonl")
FINAL = {"APPLIED", "STALE_FINDING", "SIGNATURE_REFUSED"}
DEFERRED_CODES = {fr.DEFERRED, fr.UNAVAILABLE, "finding_inputs_unavailable"}
OUTCOMES = {"finding_stale": "STALE_FINDING", "finding_signature_refused": "SIGNATURE_REFUSED"}


class ApplyError(Exception):
    """The pass cannot be trusted (unreadable receipts or review journal): nothing is applied or staged."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def root_binding(root):
    """SHA-256 of the canonical resolved absolute analysis-root path (an identity check, not an authorization)."""
    return hashlib.sha256(os.path.realpath(root).encode("utf-8")).hexdigest()


def receipts_path(root):
    return Path(root).joinpath(*RECEIPTS)


def read_receipts(root):
    """{entry_id: outcome}. Absent file -> {}. Read-only: nothing is created. Anything unreadable raises ApplyError."""
    path = receipts_path(root)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    except OSError:
        raise ApplyError("finding_receipts_unreadable") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o177 or st.st_size > cj.MAX_JOURNAL:
            raise ApplyError("finding_receipts_unreadable")
        raw = os.pread(fd, st.st_size, 0)
    except OSError:
        raise ApplyError("finding_receipts_unreadable") from None
    finally:
        os.close(fd)
    found = {}
    if raw and not raw.endswith(b"\n"):
        raise ApplyError("finding_receipts_unreadable")
    for line in raw.split(b"\n")[:-1]:
        try:
            row = json.loads(line)
        except ValueError:
            row = None
        if not (isinstance(row, dict) and set(row) == {"entry_id", "at", "outcome"} and all(isinstance(v, str) for v in row.values())):
            raise ApplyError("finding_receipts_unreadable")
        found[row["entry_id"]] = row["outcome"]
    return found


def _receipt(root, entry_id, outcome):
    row = {"entry_id": entry_id, "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "outcome": outcome}
    data = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(receipts_path(root), os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size + len(data) > cj.MAX_JOURNAL:
            raise OSError("receipts_unusable")
        done = 0
        while done < len(data):
            done += os.write(fd, data[done:])
        os.fsync(fd)
    finally:
        os.close(fd)


def _landed(root, entry_id):
    """True when the review journal already holds this console entry's own event (a receipt write was interrupted)."""
    try:
        return any(e.get("entry_id") == entry_id for e in fr._read(root)[0])
    except fr.FindingReviewError:
        raise ApplyError("finding_review_unavailable") from None


def _decide(root, entry, value):
    agency, _, finding_id = entry["target_id"].partition(":")
    if entry["action"] == "finding_approve":
        fr.approve(root, agency, finding_id, value["signature"], expected_hash=entry["revision_seen"], entry_id=entry["entry_id"])
    else:
        fr.reject(root, agency, finding_id, value["reason"], expected_hash=entry["revision_seen"], entry_id=entry["entry_id"])


def _overtaken(root, entry, value):
    """APPLIED / SUPERSEDED when the review journal already holds a decision for this exact finding made at or after the entry was
    journaled and the entry's own event is absent (an idempotent parent return appends nothing, so it cannot leave its entry_id).

    That decision wins: it is APPLIED when it is the entry's own decision, SUPERSEDED when it is a different one, so a failed receipt
    can be retried without ever replaying the old decision over a later one. None: nothing later exists, apply it.
    """
    agency, _, finding_id = entry["target_id"].partition(":")
    try:
        events = fr._read(root)[0]
    except fr.FindingReviewError:
        return None
    later = [e for e in events if (e["agency"], e["finding_id"], e["finding_sha256"]) == (agency, finding_id, entry["revision_seen"])
             and e["at"] >= entry["at"]]
    if not later:
        return None
    latest = later[-1]
    if entry["action"] == "finding_approve":
        same = latest["decision"] == "approve" and latest.get("signature") == value["signature"]
    else:
        same = latest["decision"] == "reject" and latest.get("reason") == value["reason"]
    return "APPLIED" if same else "SUPERSEDED"


def _would(root, entry):
    agency, _, finding_id = entry["target_id"].partition(":")
    try:
        row = next((r for r in fr.list_findings(root, agency=agency) if r["finding_id"] == finding_id), None)
    except fr.FindingReviewError as exc:
        return "WOULD_DEFER " + exc.code
    if row is None or not row.get("finding_sha256"):
        return "WOULD_REFUSE finding_unknown"
    return "WOULD_APPLY" if row["finding_sha256"] == entry["revision_seen"] else "WOULD_STALE_FINDING"


def apply_journal(root, raw, *, dry_run=False):
    """-> [(entry_id, action, result)] for each finding_review entry of this root in journal order; ApplyError refuses the pass.

    Entries of other kinds and of another root are left alone (never receipted here). dry_run writes nothing at all.
    """
    root = fr._admit(root)
    try:
        entries = cj.parse(raw)
    except cj.JournalError:
        raise ApplyError("finding_journal_invalid") from None
    mine = root_binding(root)
    done, results = read_receipts(root), []
    for entry in entries:
        if entry.get("kind") != cj.FINDING_KIND:
            continue
        ident = entry["entry_id"]
        try:
            value = cj.finding_value(entry)
        except ValueError:
            value = None
        if value is not None and value["analysis_root_sha256"] != mine:
            continue  # another analysis root owns it
        if ident in done:
            results.append((ident, entry["action"], "ALREADY"))
        elif value is None:  # unreachable through parse(); kept so a bad envelope is never mistaken for another root's
            results.append((ident, entry["action"], "REFUSED finding_entry_invalid"))
        elif dry_run:
            results.append((ident, entry["action"], _would(root, entry)))
        else:
            result = _settle(root, entry, value)
            if result is not None:
                _receipt(root, ident, result)
                done[ident] = result
            results.append((ident, entry["action"], result or "DEFERRED"))
    return results


def _settle(root, entry, value):
    """The final outcome of one entry, or None to leave it for the next tick."""
    try:
        if _landed(root, entry["entry_id"]):  # its own event is already in the review journal: never append twice
            return "APPLIED"
        overtaken = _overtaken(root, entry, value)
        if overtaken:
            return overtaken
        _decide(root, entry, value)
    except fr.FindingReviewError as exc:
        if exc.code in DEFERRED_CODES:
            return None
        return OUTCOMES.get(exc.code) or "REFUSED " + exc.code
    except OSError:
        return None
    return "APPLIED"
