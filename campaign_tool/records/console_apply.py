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
TRANSIENT = {"queue_busy", "queue_io", "journal_busy"}  # not final: the entry stays unreceipted and is tried at the next run
CODES = {"stale_revision": "STALE_REVISION", "packet_stale": "STALE_REVISION", "approval_required": "SIGNATURE_REFUSED",
         "invalid_transition": "INVALID_TRANSITION", "packet_not_open": "INVALID_TRANSITION"}


def receipts(project):
    """{entry id: outcome} in receipt order. Read-only; an unreadable file raises ApprovalQueueError(receipts_invalid)."""
    with aq._Store(project, False) as store:
        fd = None if store.dir_fd is None else aq._open_file(store.dir_fd, FILE, os.O_RDONLY)
        if fd is None:
            return {}
        try:
            raw = aq._read_capped(fd, cj.MAX_JOURNAL)
        finally:
            os.close(fd)
    aq._need(len(raw) <= cj.MAX_JOURNAL and (not raw or raw.endswith(b"\n")), "receipts_invalid")
    found = {}
    for line in raw.split(b"\n")[:-1]:
        row = aq._strict_load(line, "receipts_invalid")
        aq._need(isinstance(row, dict) and set(row) == {"entry_id", "outcome", "at"}
                 and all(isinstance(v, str) for v in row.values()), "receipts_invalid")
        found[row["entry_id"]] = row["outcome"]
    return found


def _receipt(project, ident, outcome):
    data = (aq._canon({"entry_id": ident, "outcome": outcome, "at": aq._now()}) + "\n").encode()
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


def _apply(project, entry):
    """Call the existing function for the entry's action with the recorded value; its refusal propagates."""
    action, ident, revision, value = entry["action"], entry["target_id"], entry["revision_seen"], entry["value"]
    actor = entry["actor"]
    if action == "approve":
        row = next((r for r in aq.load_queue(project) if r["id"] == ident), None)
        link = unmatched.choose(row, entry.get("choice")) if row and row["kind"] == draft_templates.MESSAGE_LINK else None
        aq.approve(project, ident, {"kind": "ssh", "signature": value})
        if link:  # as the console does after a message-link approve; the mail-draft placement hook is not run
            done = next(r for r in aq.load_queue(project) if r["id"] == ident)
            unmatched.append_link(project, done["item_id"], link[0], link[1], done["approved_by"])
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
        entries, done, pid = cj.parse(raw), receipts(project), aq._project_id(project)
        for entry in entries:
            if entry["entry_id"] in done:
                result = "ALREADY"
            elif entry.get("source_project_id") != pid:
                result = "PROJECT_MISMATCH"  # nothing applied and no receipt: another project (or this one moved) owns it
            else:
                result = outcome(project, entry)
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
