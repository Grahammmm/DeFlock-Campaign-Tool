"""Apply the console decision journal to the live project inside the records run (DFGL-16C).

``records decisions-apply --project P --from-journal FILE|-`` runs in the container under the run's lock. Each journal entry
(DFGL-16B) goes through the one existing function the same organizer action calls on the live console: the approval queue,
the packet writer or the public-updates oversight writer. Those functions still verify G's signature, enforce transitions and
revisions on the live project. Every final outcome is receipted in ``P/approvals/decisions-applied.jsonl`` (0600, append-only);
a refused entry is never retried. No key, verifier, switch or send is added here, and nothing is placed in a mailbox.
"""
import argparse
import os
import re
import sys

from . import approval_queue as aq, console_journal as cj, draft_templates, public_packets as pp, public_updates as pu
from .console_oversight import _journal_actor
from .outcomes import unmatched

FILE = "decisions-applied.jsonl"
INTENT = "INTENT"  # write-ahead receipt line (never an outcome): recorded before an entry's live mutation, see ``settle``
QUEUE_ACTIONS = ("approve", "reject", "edit")
TRANSIENT = {"queue_busy", "queue_io", "journal_busy"}  # not final: the entry stays unreceipted and is tried at the next run
CODES = {"stale_revision": "STALE_REVISION", "packet_stale": "STALE_REVISION", "approval_required": "SIGNATURE_REFUSED",
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
    unmatched.append_link(project, done["item_id"], link[0], link[1], done["approved_by"])


def _apply(project, entry):
    """Call the existing function for the entry's action with the recorded value; its refusal propagates."""
    action, ident, revision, value = entry["action"], entry["target_id"], entry["revision_seen"], entry["value"]
    actor = entry["actor"]
    if action == "approve":
        link = _link_choice(project, ident, entry)
        aq.approve(project, ident, {"kind": "ssh", "signature": value})
        if link:
            _append_link(project, ident, link)
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
    elif action in ("off", "kill"):
        (pu.turn_off if action == "off" else pu.kill)(project, _journal_actor(actor))
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
    if action == "retract":
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
            if action == "approve":
                return ev.get("event") == "approve" and (ev.get("approval") or {}).get("signature") == value
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
    return False


def settle(project, entry, intents):
    """The final outcome of one unreceipted entry, or None to leave it for the next run.

    Write-ahead: an INTENT line (with the target's ``base``) is receipted before the live mutation, so a run that dies after
    the mutation but before its outcome line is reconciled on replay: when the live stores show this entry's own mutation
    the outcome is APPLIED instead of re-executing it against a revision that it has itself moved past."""
    ident = entry["entry_id"]
    try:
        if ident in intents:
            if _landed(project, entry, intents[ident]):
                if entry["action"] == "approve":  # the message-link step after the approve is idempotent
                    link = _link_choice(project, entry["target_id"], entry)
                    if link:
                        _append_link(project, entry["target_id"], link)
                return "APPLIED"
            base = None
        else:
            base = _mark(project, entry)
    except (aq.ApprovalQueueError, pp.PacketError, pu.PublicUpdatesError) as error:
        if error.code in TRANSIENT:
            return None
        base = None  # unreadable state: outcome() below refuses with the same code
    except OSError:
        return None
    except (KeyError, TypeError, AttributeError, ValueError):
        base = None  # a malformed entry: outcome() refuses it with a code
    if ident not in intents:
        try:
            _receipt(project, ident, INTENT, base)
        except (aq.ApprovalQueueError, OSError) as error:  # no mutation without its write-ahead line
            if isinstance(error, OSError) or error.code in TRANSIENT:
                return None
            raise
    return outcome(project, entry)


def outcome(project, entry):
    """APPLIED, STALE_REVISION, SIGNATURE_REFUSED, INVALID_TRANSITION or REFUSED <code>; None for a transient failure."""
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
    return None if code in TRANSIENT else CODES.get(code) or "REFUSED " + code


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
        for entry in entries:
            if entry["entry_id"] in done:
                result = "ALREADY"
            elif entry.get("source_project_id") != pid:
                result = "PROJECT_MISMATCH"  # nothing applied and no receipt: another project (or this one moved) owns it
            else:
                result = settle(project, entry, intents)
                if result is None:
                    result = "DEFERRED"
                else:
                    _receipt(project, entry["entry_id"], result)
                    done[entry["entry_id"]] = result
            print("decisions-apply: %s %s %s" % (entry["entry_id"][:12], str(entry.get("action"))[:20], result))
    except (cj.JournalError, aq.ApprovalQueueError, OSError) as error:
        code = getattr(error, "code", None) or (str(error) if isinstance(error, cj.JournalError) else type(error).__name__)
        print("decisions-apply: refused (%s)" % code, file=sys.stderr)
        return 1
    return 0
