"""Private append-only review journal for drafted records replies. Nothing here sends anything.

Store: ``<project>/approvals/queue.jsonl`` (dir 0700, journal 0600, lock ``.lock``). Events are only
appended; the current row of each draft is the fold of its events. Approval is an SSH signature
(``campaign_tool.approval``) over ``approval_payload``; it marks the draft approved-to-send for a
human and authorizes no outbound action by itself. Refusals raise ApprovalQueueError(code) with a
content-free message.
"""
import argparse
import difflib
from itertools import islice
import fcntl
import hashlib
import json
import os
import stat
import sys
import time
from datetime import datetime, timezone

from .. import approval
from . import draft_templates

SCHEMA = "records-approval-event-v1"
SCOPE = "records-draft-review-v1"
MAX_EVENT = 256 * 1024
MAX_BODY = draft_templates.MAX_BODY_BYTES
MAX_JOURNAL = 64 * 1024 * 1024
MAX_SOURCE = 64 * 1024 * 1024
LOCK_SECONDS = 5.0
PIPELINE_ACTOR = "records-pipeline"


class ApprovalQueueError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _need(condition, code):
    if not condition:
        raise ApprovalQueueError(code)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canon(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def draft_id(item_id, kind):
    return hashlib.sha256(_canon([item_id, kind]).encode()).hexdigest()


def _strict_load(raw, code):
    def pairs(items):
        keys = [k for k, _ in items]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate key")
        return dict(items)

    def refuse(_):
        raise ValueError("nonfinite")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=refuse)
    except (ValueError, UnicodeDecodeError, RecursionError) as error:
        raise ApprovalQueueError(code) from error


# ---- private filesystem access -------------------------------------------------------------

def _open_chain(path):
    """Open ``path`` as a directory fd, refusing a symlink at any component."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in [p for p in os.path.abspath(path).split("/") if p]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except OSError as error:
        os.close(fd)
        raise ApprovalQueueError("queue_io") from error


def _private(info, directory):
    kind_ok = stat.S_ISDIR(info.st_mode) if directory else (stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
    return kind_ok and info.st_uid == os.geteuid() and not info.st_mode & 0o077


def _open_approvals(root_fd, create):
    try:
        if create:
            try:
                os.mkdir("approvals", 0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
        fd = os.open("approvals", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ApprovalQueueError("queue_invalid") from error
    if not _private(os.fstat(fd), True):
        os.close(fd)
        raise ApprovalQueueError("queue_invalid")
    return fd


def _open_file(dir_fd, name, flags, create=False):
    """Open without ever blocking (a FIFO would otherwise stall the open), then validate the descriptor."""
    try:
        fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_CREAT | os.O_EXCL if create else 0),
                     0o600, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as error:  # includes ENXIO for a write-open of a FIFO that has no reader
        raise ApprovalQueueError("queue_invalid") from error
    try:
        private = _private(os.fstat(fd), False)
    except OSError as error:
        os.close(fd)
        raise ApprovalQueueError("queue_invalid") from error
    if not private:
        os.close(fd)
        raise ApprovalQueueError("queue_invalid")
    return fd


def _read_capped(fd, cap):
    """Read until EOF, requesting at most the bytes still allowed; returns at most cap + 1 bytes."""
    chunks, consumed = [], 0
    while consumed <= cap:
        chunk = os.read(fd, min(1 << 20, cap + 1 - consumed))
        if not chunk:
            break
        chunks.append(chunk)
        consumed += len(chunk)
    return b"".join(chunks)


def _lock(dir_fd, exclusive):
    """Return a locked fd (None for a reader of an absent store)."""
    flags = os.O_RDWR
    fd = _open_file(dir_fd, ".lock", flags)
    if fd is None:
        if not exclusive:
            return None
        try:
            fd = _open_file(dir_fd, ".lock", flags, create=True)
        except ApprovalQueueError:
            fd = _open_file(dir_fd, ".lock", flags)  # lost a creation race
        _need(fd is not None, "queue_io")
    deadline = time.monotonic() + LOCK_SECONDS
    while True:
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() > deadline:
                os.close(fd)
                raise ApprovalQueueError("queue_busy")
            time.sleep(0.02)
        except OSError as error:
            os.close(fd)
            raise ApprovalQueueError("queue_io") from error


class _Store:
    """Context manager: root dir + approvals dir + lock held; read() folds, append() adds events."""

    def __init__(self, root, write):
        self.root, self.write, self.fds = os.path.abspath(root), write, []
        self.dir_fd = self.lock_fd = None

    def __enter__(self):
        try:
            root_fd = _open_chain(self.root)
            self.fds.append(root_fd)
            self.dir_fd = _open_approvals(root_fd, self.write)
            if self.dir_fd is None:
                return self
            self.fds.append(self.dir_fd)
            self.lock_fd = _lock(self.dir_fd, self.write)
            if self.lock_fd is not None:
                self.fds.append(self.lock_fd)
            return self
        except BaseException:
            self.__exit__()
            raise

    def __exit__(self, *exc):
        for fd in reversed(self.fds):
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds = []
        return False

    def raw(self):
        if self.dir_fd is None:
            return b""
        fd = _open_file(self.dir_fd, "queue.jsonl", os.O_RDONLY)
        if fd is None:
            return b""
        try:
            _need(os.fstat(fd).st_size <= MAX_JOURNAL, "queue_invalid")
            raw = _read_capped(fd, MAX_JOURNAL)  # bytes actually consumed bind, not only the size seen above
            _need(len(raw) <= MAX_JOURNAL, "queue_invalid")
            return raw
        except OSError as error:
            raise ApprovalQueueError("queue_io") from error
        finally:
            os.close(fd)

    def append(self, events):
        data = b"".join((_canon(e) + "\n").encode("utf-8") for e in events)
        _need(all(len(_canon(e).encode("utf-8")) <= MAX_EVENT for e in events), "invalid_draft")
        fd = _open_file(self.dir_fd, "queue.jsonl", os.O_WRONLY | os.O_APPEND)
        if fd is None:
            fd = _open_file(self.dir_fd, "queue.jsonl", os.O_WRONLY | os.O_APPEND, create=True)
        try:
            _need(os.fstat(fd).st_size + len(data) <= MAX_JOURNAL, "queue_invalid")
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        except OSError as error:
            raise ApprovalQueueError("queue_io") from error
        finally:
            os.close(fd)


# ---- event fold ----------------------------------------------------------------------------

def _project_id(root):
    return hashlib.sha256(os.path.realpath(root).encode()).hexdigest()[:16]


def _review_text(root, row):
    return "\n".join([f"project: {_project_id(root)}", f"id: {row['id']}", f"agency: {row['agency_id']}",
                      f"request: {row['request_id']}", f"item: {row['item_id']}", f"kind: {row['kind']}",
                      "body:", row["body"], "evidence:", _canon(row["evidence"])])


def _payload(root, row, revision):
    return approval.payload(SCOPE, row["id"], _review_text(root, row), round_=revision)


def _public(row):
    out = {k: row[k] for k in ("id", "item_id", "kind", "agency_id", "request_id", "body", "evidence",
                               "source_run_id", "status", "revision", "created_at", "updated_at", "actor")}
    if row["status"] == "approved":
        out["approved_by"] = row["approved_by"]
        out["approval_evidence"] = row["approval_evidence"]
    if row.get("reason") and row["status"] == "rejected":
        out["reason"] = row["reason"]
    return out


def _fold(root, raw, verify=True):
    rows = {}
    if not raw:
        return rows
    _need(raw.endswith(b"\n"), "queue_invalid")
    for line in raw.split(b"\n")[:-1]:
        _need(0 < len(line) <= MAX_EVENT, "queue_invalid")
        ev = _strict_load(line, "queue_invalid")
        _need(isinstance(ev, dict) and ev.get("schema") == SCHEMA and isinstance(ev.get("id"), str)
              and type(ev.get("revision")) is int and isinstance(ev.get("at"), str)
              and isinstance(ev.get("actor"), str), "queue_invalid")
        kind, ident, row = ev.get("event"), ev["id"], rows.get(ev["id"])
        if kind == "draft":
            snap = ev.get("snapshot")
            _need(row is None and ev["revision"] == 0 and isinstance(snap, dict), "queue_invalid")
            _need(draft_id(snap.get("item_id"), snap.get("kind")) == ident, "queue_invalid")
            row = {"id": ident, **snap, "status": "pending", "revision": 0, "created_at": ev["at"],
                   "updated_at": ev["at"], "actor": ev["actor"], "origin_evidence": snap.get("evidence")}
            rows[ident] = row
            continue
        _need(row is not None and ev["revision"] == row["revision"] + 1, "queue_invalid")
        if kind == "edited":
            _need(isinstance(ev.get("body"), str), "queue_invalid")
            row.update(body=ev["body"], status="pending", reason=None, actor=ev["actor"])
            row.pop("approved_by", None)
            row.pop("approval_evidence", None)
        elif kind == "reject":
            _need(isinstance(ev.get("reason"), str) and row["status"] == "pending", "queue_invalid")
            row.update(status="rejected", reason=ev["reason"], actor=ev["actor"])
        elif kind == "approve":
            ap = ev.get("approval")
            _need(isinstance(ap, dict) and row["status"] == "pending" and ap.get("signed_revision") == row["revision"]
                  and ap.get("project") == _project_id(root) and isinstance(ap.get("signature"), str)
                  and isinstance(ap.get("principal"), str), "queue_invalid")
            valid = False
            if verify:
                try:
                    valid = approval.verify(_payload(root, row, row["revision"]), ap["signature"]) == ap["principal"]
                except approval.ApprovalError:
                    valid = False
            if valid:  # a forged or unverifiable approval never materializes as approved
                row.update(status="approved", actor=ap["principal"], approved_by=ap["principal"],
                           approval_evidence={"signed_revision": ap["signed_revision"], "project": ap["project"],
                                              "signature_sha256": hashlib.sha256(ap["signature"].encode()).hexdigest()})
        else:
            raise ApprovalQueueError("queue_invalid")
        row["revision"] = ev["revision"]
        row["updated_at"] = ev["at"]
    return rows


def _event(kind, ident, revision, actor, **extra):
    return {"schema": SCHEMA, "event": kind, "id": ident, "revision": revision, "at": _now(), "actor": actor, **extra}


def _label(actor):
    _need(isinstance(actor, str) and actor.strip() and len(actor) <= 80
          and not any(ord(c) < 32 or ord(c) == 127 for c in actor), "invalid_actor")
    return actor


# ---- public API ----------------------------------------------------------------------------

def load_queue(root):
    """Current rows in id order. Read-only: creates nothing."""
    with _Store(root, False) as store:
        rows = _fold(root, store.raw())
    return [_public(rows[k]) for k in sorted(rows)]


def history(root, id):
    """Original and edited bodies, hashes and bounded diff from the journal; creates nothing."""
    with _Store(root, False) as store:
        raw = store.raw()
        rows = _fold(root, raw)
        _need(isinstance(id, str) and id in rows, "unknown_id")
        row = rows[id]
        original, edits, current = None, [], None
        for line in raw.splitlines():
            ev = _strict_load(line, "queue_invalid")
            if ev["id"] != id or ev["event"] not in ("draft", "edited"):
                continue
            body = ev["snapshot"].get("body") if ev["event"] == "draft" else ev["body"]
            _need(isinstance(body, str), "queue_invalid")
            try:
                data = body.encode("utf-8")
            except UnicodeEncodeError as error:
                raise ApprovalQueueError("queue_invalid") from error
            entry = {"body": body, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                     "revision": ev["revision"], "at": ev["at"], "actor": ev["actor"]}
            if original is None:
                original = {k: entry[k] for k in ("body", "sha256", "bytes", "at", "actor")}
            else:
                edits.append(entry)
            current = {k: entry[k] for k in ("body", "sha256", "bytes", "revision")}
        diff = list(islice(difflib.unified_diff(original["body"].splitlines(), current["body"].splitlines(),
                                               "original", f"revision {current['revision']}", lineterm=""), 401))
        return {"id": id, "kind": row["kind"], "status": row["status"], "revision": row["revision"],
                "original": original, "current": current, "edits": edits,
                "changed": original["body"] != current["body"], "diff": diff[:400],
                "diff_truncated": len(diff) > 400}


def _row(root, store, ident):
    rows = _fold(root, store.raw())
    _need(isinstance(ident, str) and ident in rows, "unknown_id")
    return rows[ident]


def _check_draft(item_id, kind, agency_id, request_id, body, evidence, source_run_id):
    _need(kind in draft_templates.KINDS, "invalid_draft")
    _need(all(isinstance(v, str) and v for v in (item_id, agency_id, request_id, source_run_id)), "invalid_draft")
    _need(isinstance(body, str) and body.strip() and len(body.encode("utf-8")) <= MAX_BODY, "invalid_draft")
    _need(isinstance(evidence, dict), "invalid_draft")
    try:
        _canon(evidence)
    except (TypeError, ValueError) as error:
        raise ApprovalQueueError("invalid_draft") from error


RUN_PROVENANCE = ("report_sha256", "row_index")  # comparison_ref keys that change every run


def _norm(evidence):
    """Compare stable evidence only; run provenance in comparison_ref is not evidence identity."""
    ev = json.loads(_canon(evidence))
    if ev.get("source") == "register":  # clock identity only; the run and row locators change every run
        ev.pop("run_id", None)
        ev.pop("register_sources", None)
    if isinstance(ev.get("findings"), list):
        for f in ev["findings"]:
            ref = f.get("comparison_ref") if isinstance(f, dict) else None
            if isinstance(ref, dict):
                f["comparison_ref"] = {k: v for k, v in ref.items() if k not in RUN_PROVENANCE}
        ev["findings"] = sorted(ev["findings"], key=_canon)
    if isinstance(ev.get("deadlines"), list):
        ev["deadlines"] = sorted(ev["deadlines"], key=_canon)
    return _canon(ev)


def _same_source(row, draft):
    """Existing draft matches only on normalized evidence AND the immutable original attribution."""
    return (_norm(row["origin_evidence"]) == _norm(draft["evidence"])
            and row["agency_id"] == draft["agency_id"] and row["request_id"] == draft["request_id"])


def _add(root, store, rows, draft):
    """Append-or-return one draft under the writer lock; returns (row, created)."""
    ident = draft_id(draft["item_id"], draft["kind"])
    if ident in rows:
        _need(_same_source(rows[ident], draft), "source_conflict")
        return rows[ident], False
    store.append([_event("draft", ident, 0, PIPELINE_ACTOR, snapshot=draft)])
    rows.update(_fold(root, store.raw(), verify=False))
    return rows[ident], True


def add_draft(root, *, item_id, kind, agency_id, request_id, body, evidence, source_run_id):
    _check_draft(item_id, kind, agency_id, request_id, body, evidence, source_run_id)
    draft = {"item_id": item_id, "kind": kind, "agency_id": agency_id, "request_id": request_id, "body": body,
             "evidence": evidence, "source_run_id": source_run_id}
    with _Store(root, True) as store:
        rows = _fold(root, store.raw(), verify=False)
        row, _ = _add(root, store, rows, draft)
        rows = _fold(root, store.raw())
        return _public(rows[row["id"]])


def approval_payload(root, id):
    with _Store(root, False) as store:
        row = _row(root, store, id)
    return _payload(root, row, row["revision"])


def approve(root, id, actor):
    if not (isinstance(actor, dict) and actor.get("kind") == "ssh" and isinstance(actor.get("signature"), str)):
        raise ApprovalQueueError("approval_required")
    with _Store(root, True) as store:
        row = _row(root, store, id)
        if row["status"] == "approved" and row["approval_evidence"]["signature_sha256"] == \
                hashlib.sha256(actor["signature"].encode()).hexdigest():
            return _public(row)
        _need(row["status"] == "pending", "invalid_transition")
        try:
            principal = approval.verify(_payload(root, row, row["revision"]), actor["signature"])
        except approval.ApprovalError as error:
            raise ApprovalQueueError("approval_required") from error
        store.append([_event("approve", id, row["revision"] + 1, principal, approval={
            "signed_revision": row["revision"], "project": _project_id(root), "principal": principal,
            "signature": actor["signature"]})])
        return _public(_row(root, store, id))


def reject(root, id, actor, reason):
    label = _label(actor)
    _need(isinstance(reason, str) and reason.strip() and len(reason) <= 2000, "invalid_draft")
    with _Store(root, True) as store:
        row = _row(root, store, id)
        if row["status"] == "rejected" and row.get("reason") == reason:
            return _public(row)
        _need(row["status"] == "pending", "invalid_transition")
        store.append([_event("reject", id, row["revision"] + 1, label, reason=reason)])
        return _public(_row(root, store, id))


def edit(root, id, actor, new_body):
    label = _label(actor)
    _need(isinstance(new_body, str) and new_body.strip() and len(new_body.encode("utf-8")) <= MAX_BODY, "invalid_draft")
    with _Store(root, True) as store:
        row = _row(root, store, id)
        store.append([_event("edited", id, row["revision"] + 1, label, body=new_body)])
        return _public(_row(root, store, id))


def _read_input(path, cap):
    """Bounded nonblocking read of one regular input file (source, body or signature): cap + 1 bytes at most."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise ApprovalQueueError("source_invalid") from error
    try:
        info = os.fstat(fd)
        _need(stat.S_ISREG(info.st_mode) and info.st_size <= cap, "source_invalid")
        raw = _read_capped(fd, cap)
    except OSError as error:
        raise ApprovalQueueError("source_invalid") from error
    finally:
        os.close(fd)
    _need(len(raw) <= cap, "source_invalid")
    return raw


def _read_source(path):
    return _read_input(path, MAX_SOURCE)


def queue_received_items(root, run_id, received_path):
    """Draft from exactly this run's received-items file. Metadata-only summary."""
    doc = _strict_load(_read_source(received_path), "source_invalid")
    _need(isinstance(doc, dict) and doc.get("schema") == "records-received-item-v1" and doc.get("run_id") == run_id
          and isinstance(doc.get("items"), list), "source_invalid")
    wanted, seen, skipped = {}, {}, 0
    for item in doc["items"]:
        try:
            draft_templates.validate_item(item)
            rendered = draft_templates.render(item)
        except draft_templates.DraftSourceError as error:
            raise ApprovalQueueError("source_invalid") from error
        identity = _canon(item)
        if item["item_id"] in seen:
            _need(seen[item["item_id"]] == identity, "source_conflict")  # identical duplicates collapse
            continue
        seen[item["item_id"]] = identity
        if rendered is None:
            skipped += 1
            continue
        kind, body, evidence = rendered
        draft = {"item_id": item["item_id"], "kind": kind, "agency_id": item["agency_id"],
                 "request_id": item["request_id"], "body": body, "evidence": evidence, "source_run_id": run_id}
        _check_draft(**draft)
        wanted[draft_id(item["item_id"], kind)] = draft
    created = existing = 0
    with _Store(root, True) as store:
        rows = _fold(root, store.raw(), verify=False)
        new = []
        for ident, draft in wanted.items():
            if ident in rows:
                _need(_same_source(rows[ident], draft), "source_conflict")
                existing += 1
            else:
                new.append(_event("draft", ident, 0, PIPELINE_ACTOR, snapshot=draft))
        if new:
            store.append(new)  # one write: all or nothing is reported
        created = len(new)
    return {"created": created, "existing": existing, "skipped": skipped}


# ---- command line --------------------------------------------------------------------------

def _file(path):
    raw = _read_input(path, MAX_BODY)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ApprovalQueueError("source_invalid") from error


def main():
    parser = argparse.ArgumentParser(prog="records approvals", description="Review queue for drafted replies. Sends nothing.")
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("list", "payload", "approve", "reject", "edit", "history"):
        p = sub.add_parser(name)
        p.add_argument("--project", required=True)
        if name == "list":
            p.add_argument("--status", choices=("pending", "approved", "rejected"))
            continue
        if name == "history":
            p.add_argument("--id", required=True)
        else:
            p.add_argument("id")
        if name == "approve":
            p.add_argument("--signature", required=True, help="file holding the detached SSH signature text")
        if name in ("reject", "edit"):
            p.add_argument("--actor", required=True)
        if name == "reject":
            p.add_argument("--reason", required=True)
        if name == "edit":
            p.add_argument("--body-file", required=True)
    args = parser.parse_args()
    try:
        if args.action == "list":
            rows = load_queue(args.project)
            print(json.dumps([r for r in rows if not args.status or r["status"] == args.status], indent=1,
                             ensure_ascii=False))
        elif args.action == "history":
            print(json.dumps(history(args.project, args.id), indent=1, ensure_ascii=False))
        elif args.action == "payload":
            sys.stdout.buffer.write(approval_payload(args.project, args.id))
            sys.stdout.buffer.flush()
        elif args.action == "approve":
            print(json.dumps(approve(args.project, args.id, {"kind": "ssh", "signature": _file(args.signature)}),
                             indent=1, ensure_ascii=False))
        elif args.action == "reject":
            print(json.dumps(reject(args.project, args.id, args.actor, args.reason), indent=1, ensure_ascii=False))
        else:
            print(json.dumps(edit(args.project, args.id, args.actor, _file(args.body_file)), indent=1, ensure_ascii=False))
    except ApprovalQueueError as error:
        print(error.code, file=sys.stderr)
        return 1
    return 0
