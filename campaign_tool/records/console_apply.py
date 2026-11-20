"""Apply the console decision journal to the live project inside the records run (DFGL-16C).

``records decisions-apply --project P --from-journal FILE|-`` runs in the container under the run's lock. Each journal entry
(DFGL-16B) goes through the one existing function the same organizer action calls on the live console: the approval queue,
the packet writer or the public-updates oversight writer. Those functions still verify G's signature, enforce transitions and
revisions on the live project. Every final outcome is receipted in ``P/approvals/decisions-applied.jsonl`` (0600, append-only);
a refused entry is never retried. An unreadable store is not a refusal: its entries are deferred (unreceipted) and tried again.
No key, verifier, switch or send is added here, and nothing is placed in a mailbox.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

from . import approval_queue as aq, console_journal as cj, console_ocr, draft_templates, ocr_review, public_packets as pp, public_updates as pu
from .run_stages import LedgerBusy, ledger_ro
from .console_oversight import _journal_actor
from .outcomes import unmatched

FILE = "decisions-applied.jsonl"
INTENT = "INTENT"  # write-ahead receipt line (never an outcome): recorded before an entry's live mutation, see ``settle``
QUEUE_ACTIONS = ("approve", "reject", "edit", "phone_approve")
PHONE_APPROVE = "phone_approve"  # A6: value is the canonical JSON tap (campaign_tool.phone_approval.TAP_FIELDS)
OCR_ACTIONS = {"ocr_confirm": "confirmed", "ocr_reject": "rejected"}  # OCR-REVIEW-ROUTE-1: the existing ocr_review.mark
TRANSIENT = {"approval_unavailable", "queue_busy", "queue_io", "journal_busy", "ocr_ledger_busy", "message_link_ledger_busy"}  # not final: the entry stays unreceipted and is tried at the next run
# A store that cannot be read at all (a moved or restored project whose approvals name another path, a damaged journal) says
# nothing about the decision itself: the entry is deferred like a transient fault, never receipted as a final refusal.
STORE_FAULTS = {"queue_invalid", "journal_invalid", "packets_unavailable", "switch_unavailable", "file_invalid",
                "file_too_large", "project_invalid", "ocr_ledger_unreadable", "ocr_review_chain_broken"}
NOT_FINAL = TRANSIENT | STORE_FAULTS
CODES = {"approval_unverified": "INVALID_TRANSITION", "stale_revision": "STALE_REVISION", "packet_stale": "STALE_REVISION", "approval_required": "SIGNATURE_REFUSED",
         "invalid_transition": "INVALID_TRANSITION", "packet_not_open": "INVALID_TRANSITION"}


def ledger(project):
    """({entry id: final outcome}, {entry id: base of an INTENT with no final outcome yet}) in receipt order. Read-only; an
    unreadable file raises ApprovalQueueError(receipts_invalid)."""
    with aq._Store(project, False) as store:
        fd = None if store.dir_fd is None else aq._open_file(store.dir_fd, FILE, os.O_RDONLY)
        if fd is None:
            return {}, {}
        try:
            raw = aq._read_capped(fd, cj.MAX_JOURNAL)
        finally:
            os.close(fd)
    aq._need(len(raw) <= cj.MAX_JOURNAL and (not raw or raw.endswith(b"\n")), "receipts_invalid")
    found, intents = {}, {}
    for line in raw.split(b"\n")[:-1]:
        row = aq._strict_load(line, "receipts_invalid")
        aq._need(isinstance(row, dict) and {"entry_id", "outcome", "at"} <= set(row) <= {"entry_id", "outcome", "at", "base"}
                 and all(isinstance(row[k], str) for k in ("entry_id", "outcome", "at")), "receipts_invalid")
        if row["outcome"] == INTENT:
            base = row.get("base")
            aq._need(set(row) == {"entry_id", "outcome", "at", "base"} and (base is None or type(base) is int and base >= 0),
                     "receipts_invalid")
            if row["entry_id"] not in found:
                intents[row["entry_id"]] = base
            continue
        aq._need("base" not in row, "receipts_invalid")
        found[row["entry_id"]] = row["outcome"]
        intents.pop(row["entry_id"], None)
    return found, intents


def receipts(project):
    """{entry id: final outcome} in receipt order (an INTENT is not an outcome). Read-only; see ``ledger``."""
    return ledger(project)[0]


def _receipt(project, ident, outcome, base=None):
    """One append-only line: a final outcome, or (INTENT) the write-ahead record taken before the live mutation."""
    row = {"entry_id": ident, "outcome": outcome, "at": aq._now()}
    if outcome == INTENT:
        row["base"] = base
    data = (aq._canon(row) + "\n").encode()
    with aq._Store(project, True) as store:
        flags = os.O_WRONLY | os.O_APPEND
        fd = aq._open_file(store.dir_fd, FILE, flags) or aq._open_file(store.dir_fd, FILE, flags, create=True)
        aq._need(fd is not None, "queue_io")
        try:
            aq._need(os.fstat(fd).st_size + len(data) <= cj.MAX_JOURNAL, "receipts_invalid")
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)


def _link_choice(project, ident, entry):
    row = next((r for r in aq.load_queue(project) if r["id"] == ident), None)
    return unmatched.choose(row, entry.get("choice")) if row and row["kind"] == draft_templates.MESSAGE_LINK else None


def _append_link(project, ident, link):  # as the console does after a message-link approve; the mail-draft placement hook is not run
    done = next(r for r in aq.load_queue(project) if r["id"] == ident)
    aq._need(done.get("status") == "approved" and done.get("approved_choice") == link[1], "approval_required")  # the signed choice only
    # a replacement names only the fingerprint inside its signed evidence, never one taken from the journal entry
    unmatched.append_link(project, done["item_id"], link[0], link[1], done["approved_by"], supersedes=aq.link_supersedes(done))


def _precheck_relink(project, ident, link):
    """A replacement is refused before its approval is recorded when the current link line is no longer the one it replaces."""
    row = next((r for r in aq.load_queue(project) if r["id"] == ident), None)
    supersedes = aq.link_supersedes(row)
    if row is not None and supersedes is not None and row["status"] == "pending":
        unmatched.require_current(project, row["item_id"], supersedes, link)


def _queue_relink(project, entry):
    """The queue-only action: queue the replacement decision for a tie marked wrong. It appends no link and approves nothing."""
    sha, supersedes, run_id = entry["target_id"], entry["value"], entry.get("export_run_id")
    if not (isinstance(sha, str) and isinstance(supersedes, str) and unmatched.HEX64.fullmatch(sha) and unmatched.HEX64.fullmatch(supersedes)
            and isinstance(run_id, str)):
        raise ValueError("relink_entry_invalid")
    unmatched.queue_relink(project, run_id, sha, supersedes)


def _apply_ocr(project, entry):
    """One OCR page decision, through ocr_review.mark. The page must still be the one the person saw (derivative hash and
    confidence); an already identical decision is a replay and counts as applied, a different one is refused. The page
    review file holds the fixed reviewer label, never the actor."""
    sha, _, number = entry["target_id"].rpartition("-")
    decision = OCR_ACTIONS[entry["action"]]
    try:
        seen = json.loads(entry["value"])
        page = int(number)
    except (TypeError, ValueError):
        raise ValueError("ocr_entry_invalid") from None
    if not (isinstance(seen, dict) and set(seen) == {"confidence", "derivative_sha256"} and console_ocr.SHA.fullmatch(sha)
            and page > 0):
        raise ValueError("ocr_entry_invalid")
    root = Path(project)
    reviews = root / "ocr-review-v1" / "reviews.jsonl"
    try:
        with ledger_ro(root / "processing-v1") as db:
            row = next((r for r in ocr_review.pages(db) if (r["original_sha256"], r["page_no"]) == (sha, page)), None)
            if row is None:
                raise ValueError("ocr_page_unknown")
            if (row["confidence"], row["derivative_sha256"]) != (seen["confidence"], seen["derivative_sha256"]):
                raise ValueError("ocr_page_changed")
            if {ocr_review.binding(r): r["decision"] for r in ocr_review.read_reviews(reviews)}.get(ocr_review.binding(row)) == decision:
                return
            ocr_review.mark(db, reviews, sha, page, decision, console_ocr.LABEL)
    except LedgerBusy:
        raise ValueError("ocr_ledger_busy") from None
    except sqlite3.Error:
        raise ValueError("ocr_ledger_unreadable") from None


def _evidence(entry):
    """The approve evidence an approve entry carries: the typed SSH signature, or a phone tap (strict JSON object)."""
    if entry["action"] == "approve":
        return {"kind": "ssh", "signature": entry["value"]}
    tap = aq._strict_load(entry["value"].encode(), "phone_entry_invalid") if isinstance(entry["value"], str) else None
    if not isinstance(tap, dict):
        raise ValueError("phone_entry_invalid")
    return {"kind": "phone", "tap": tap}


def _apply(project, entry):
    """Call the existing function for the entry's action with the recorded value; its refusal propagates."""
    action, ident, revision, value = entry["action"], entry["target_id"], entry["revision_seen"], entry["value"]
    actor = entry["actor"]
    if action in ("approve", PHONE_APPROVE):
        link = _link_choice(project, ident, entry)
        if link:
            _precheck_relink(project, ident, link)  # stale before anything is recorded: neither approval nor link
        aq.approve(project, ident, _evidence(entry), choice=entry.get("choice"))
        if link:
            _append_link(project, ident, link)
    elif action == "message_link_relink":
        _queue_relink(project, entry)
    elif action == "reject":
        aq.reject(project, ident, actor, value)
    elif action == "edit":
        aq.edit(project, ident, actor, value, expected_revision=revision)
    elif action == "packet_approve":
        pp.approve(project, ident, value, revision=revision)
    elif action == "packet_reject":
        pp.decide(project, ident, "rejected", _journal_actor(actor), revision=revision, reason=value)
    elif action == "retract":
        pu.retract(project, ident, _journal_actor(actor))
    elif action == cj.LINE_APPROVAL_ACTION:  # DF2007-10: records the signed approval only; never advances or posts
        pu.approve_line(project, ident, value, revision=revision)
    elif action in ("off", "kill"):
        (pu.turn_off if action == "off" else pu.kill)(project, _journal_actor(actor))
    elif action in OCR_ACTIONS:
        _apply_ocr(project, entry)
    else:
        raise ValueError("unknown_action")


def _events(module, project):
    """The append-only journal events of the packet or public-updates store (read-only; no lock file -> none)."""
    try:
        with module._Locked(project, read_only=True) as lock:
            return lock.events()
    except FileNotFoundError:
        return []


def _mark(project, entry):
    """Where the target's history stood before the mutation: the queue row's revision, or the packet / public-updates
    journal length. None when there is nothing to compare (an action whose replay is harmless, or an unknown target)."""
    action, ident = entry["action"], entry["target_id"]
    if action in QUEUE_ACTIONS:
        row = next((r for r in aq.load_queue(project) if r["id"] == ident), None)
        return row["revision"] if row else None
    if action in ("packet_approve", "packet_reject"):
        return len(_events(pp, project))
    if action in ("retract", cj.LINE_APPROVAL_ACTION):
        return len(_events(pu, project))
    return None


def _landed(project, entry, base):
    """True when the live stores hold this entry's own mutation made after ``base`` (identity: target, revision, actor and
    the recorded value), i.e. an earlier run applied it and was interrupted before its outcome was receipted."""
    action, ident, revision, value = entry["action"], entry["target_id"], entry["revision_seen"], entry["value"]
    actor = entry["actor"]
    if base is None:
        return False
    if action in QUEUE_ACTIONS:
        with aq._Store(project, False) as store:
            raw = store.raw()
        for line in raw.split(b"\n")[:-1]:
            ev = aq._strict_load(line, "queue_invalid")
            if ev.get("id") != ident or ev.get("revision") != base + 1:
                continue
            if action in ("approve", PHONE_APPROVE):  # the signed choice is part of the approval: a substituted one is not this entry's mutation
                ap = ev.get("approval") or {}
                ours = (ap.get("signature") == value and aq.PHONE_FIELD not in ap if action == "approve"
                        else ap.get(aq.PHONE_FIELD) == _evidence(entry)["tap"].get("envelope"))
                return ev.get("event") == "approve" and ours and ap.get("choice") == entry.get("choice")
            if action == "reject":
                return ev.get("event") == "reject" and ev.get("reason") == value and ev.get("actor") == actor
            return ev.get("event") == "edited" and ev.get("body") == value and ev.get("actor") == actor
        return False
    who = _journal_actor(actor)
    if action == "packet_approve":
        return any(e.get("event") == "approved" and e.get("id") == ident and e.get("revision") == revision
                   and e.get("signature") == value for e in _events(pp, project)[base:])
    if action == "packet_reject":
        return any(e.get("event") == "rejected" and e.get("id") == ident and e.get("revision") == revision
                   and e.get("actor") == who and e.get("reason") == value for e in _events(pp, project)[base:])
    if action == "retract":
        return any(e.get("event") == "retract" and e.get("id") == ident and e.get("actor") == who
                   for e in _events(pu, project)[base:])
    if action == cj.LINE_APPROVAL_ACTION:
        return any(e.get("event") == "line_approval" and e.get("id") == ident and e.get("revision") == revision
                   and e.get("signature") == value for e in _events(pu, project)[base:])
    return False


def _defer(entry, code, trust_deferred=None):
    """None (leave the entry unreceipted for the next run); a store fault is logged on stderr with its code."""
    if code == "approval_unavailable" and trust_deferred is not None:
        trust_deferred.append(code)
    if code in STORE_FAULTS:
        print("decisions-apply: %s deferred (store unreadable: %s)" % (str(entry.get("entry_id"))[:12], code), file=sys.stderr)
    return None


def settle(project, entry, intents, trust_deferred=None):
    """The final outcome of one unreceipted entry, or None to leave it for the next run.

    Write-ahead: an INTENT line (with the target's ``base``) is receipted before the live mutation, so a run that dies after
    the mutation but before its outcome line is reconciled on replay: when the live stores show this entry's own mutation
    the outcome is APPLIED instead of re-executing it against a revision that it has itself moved past."""
    ident = entry["entry_id"]
    known = ident in intents
    write = True
    try:
        if known and _landed(project, entry, intents[ident]):
            if entry["action"] in ("approve", PHONE_APPROVE):  # the message-link step after the approve is idempotent
                link = _link_choice(project, entry["target_id"], entry)
                if link:  # reverified against the signed revision and this choice before any append (same API as a fresh apply)
                    if entry["action"] == "approve":  # a phone nonce approves once; its stored proof is re-verified by the fold
                        aq.approve(project, entry["target_id"], {"kind": "ssh", "signature": entry["value"]}, choice=entry.get("choice"))
                    _append_link(project, entry["target_id"], link)
            return "APPLIED"
        base = _mark(project, entry)  # nothing of ours landed since the old base: this attempt starts from where the target is now
    except (aq.ApprovalQueueError, pp.PacketError, pu.PublicUpdatesError) as error:
        if error.code in NOT_FINAL:
            return _defer(entry, error.code, trust_deferred)  # before any write-ahead line: nothing is recorded for an unreadable store
        base = None  # a per-entry refusal: outcome() below refuses with the same code
        write = not known  # an existing INTENT keeps its base rather than losing it
    except OSError:
        return None
    except (KeyError, TypeError, AttributeError, ValueError):
        base = None  # a malformed entry: outcome() refuses it with a code
        write = not known
    if write and not (known and base == intents[ident]):  # a retry whose target moved on gets a fresh write-ahead base
        try:
            _receipt(project, ident, INTENT, base)
        except (aq.ApprovalQueueError, OSError) as error:  # no mutation without its write-ahead line
            if isinstance(error, OSError) or error.code in TRANSIENT:
                return None
            raise
    return outcome(project, entry, trust_deferred)


def outcome(project, entry, trust_deferred=None):
    """APPLIED, STALE_REVISION, SIGNATURE_REFUSED, INVALID_TRANSITION or REFUSED <code>; None for a transient or store fault."""
    try:
        _apply(project, entry)
    except (aq.ApprovalQueueError, pp.PacketError, pu.PublicUpdatesError) as error:
        code = error.code
    except ValueError as error:
        code = re.sub(r"[^a-z_]", "", str(error)) or "entry_invalid"
    except (KeyError, TypeError, AttributeError):
        code = "entry_invalid"
    except OSError:
        code = "queue_io"
    else:
        return "APPLIED"
    return _defer(entry, code, trust_deferred) if code in NOT_FINAL else CODES.get(code) or "REFUSED " + code


def main(argv=None):
    parser = argparse.ArgumentParser(prog="records decisions-apply", description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--from-journal", required=True, metavar="FILE|-")
    args = parser.parse_args(argv)
    project = os.path.abspath(args.project)
    try:
        if args.from_journal == "-":
            raw = aq._read_capped(sys.stdin.fileno(), cj.MAX_JOURNAL)
        else:
            fd = os.open(args.from_journal, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                raw = aq._read_capped(fd, cj.MAX_JOURNAL)
            finally:
                os.close(fd)
        aq._need(len(raw) <= cj.MAX_JOURNAL, "journal_invalid")
        entries, (done, intents), pid = cj.parse(raw), ledger(project), aq._project_id(project)
        trust_deferred = []
        for entry in entries:
            if entry.get("kind") == cj.FINDING_KIND:
                result = "ANALYSIS_TICK_REQUIRED"  # DF2007-06: left for `records analysis-tick --from-journal`; never receipted here
            elif entry.get("action") == cj.REDACTION_ACTION:
                result = "SIGNOFF_APPLY_REQUIRED"  # DF2007-08: left for `tools/flock_redact.py signoff-apply`; never receipted here
            elif entry["entry_id"] in done:
                result = "ALREADY"
            elif entry.get("source_project_id") != pid:
                result = "PROJECT_MISMATCH"  # nothing applied and no receipt: another project (or this one moved) owns it
            else:
                result = settle(project, entry, intents, trust_deferred)
                if result is None:
                    result = "DEFERRED"

                else:
                    _receipt(project, entry["entry_id"], result)
                    done[entry["entry_id"]] = result
            print("decisions-apply: %s %s %s" % (entry["entry_id"][:12], str(entry.get("action"))[:20], result))
        if trust_deferred:
            print("decisions-apply: approval trust unavailable (anchor or verifier unavailable)")
    except (cj.JournalError, aq.ApprovalQueueError, OSError) as error:
        code = getattr(error, "code", None) or (str(error) if isinstance(error, cj.JournalError) else type(error).__name__)
        print("decisions-apply: refused (%s)" % code, file=sys.stderr)
        return 1
    return 0
