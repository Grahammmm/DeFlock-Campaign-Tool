"""Per-request pinned console export and the nonmutating readers over its console-model (DFGL-16A).

``LiveSource`` calls the existing live readers unchanged. ``SnapshotSource`` reads only the plain
``console-model/*.json`` files of one pinned export (DFGL-16R); it never locks, folds, verifies a signature,
recomputes a project id or creates anything. A model file that is missing, invalid, unavailable or bound to
another project raises the error class the live reader would, so every existing diagnostic path shows
"unavailable: <code>" and never an empty list or a zero.
"""
import hashlib
import os
import re
import stat
import sys
import threading
from collections import namedtuple

from . import approval_queue as aq, console_export as ce, mail_drafts, mail_send, public_packets as pp, public_updates as pu

MAX_MODEL = 64 * 1024 * 1024
MAX_MANIFEST = 8 * 1024 * 1024
LINK = re.compile(r"exports/(" + ce.NAME.pattern + ")")
PROJECT_ID = re.compile(r"[0-9a-f]{16}")
Pin = namedtuple("Pin", "path run_id source_project_id name exported_at")


class SnapshotError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _need(ok, code):
    if not ok:
        raise SnapshotError(code)


def _private_dir(info):
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077


def _read_file(dir_fd, name, cap):
    """Bytes of one regular file below the pinned dir_fd (never a symlink, never blocking); None when absent."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    try:
        _need(stat.S_ISREG(os.fstat(fd).st_mode), "export_not_regular")
        raw = aq._read_capped(fd, cap)
    finally:
        os.close(fd)
    _need(len(raw) <= cap, "export_oversize")
    return raw


def _sha256(dir_fd, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    try:
        _need(stat.S_ISREG(os.fstat(fd).st_mode), "export_not_regular")
        digest = hashlib.sha256()
        for block in iter(lambda: os.read(fd, 1 << 20), b""):
            digest.update(block)
        return digest.hexdigest()
    finally:
        os.close(fd)


class Pinner:
    """Pins DIR/current to one real export per request. A verified export is cached by directory identity
    (exports are immutable); a failing pin falls back to the last good one, and to none before any."""

    def __init__(self, root):
        self.root, self.good, self.verified, self.last_code = root, None, set(), None
        self.lock = threading.Lock()

    def pin(self):
        with self.lock:
            try:
                pin = self._pin()
            except (SnapshotError, ce.ExportError, aq.ApprovalQueueError, OSError, ValueError, KeyError,
                    TypeError, AttributeError) as error:
                code = getattr(error, "code", None) or (str(error) if isinstance(error, ce.ExportError) else "export_unreadable")
                if code != self.last_code:
                    print("export_pin_failed: " + code, file=sys.stderr, flush=True)
                self.last_code = code
                if self.good is None:
                    raise SnapshotError("export_unavailable") from None
                return self.good
            self.good, self.last_code = pin, None
            return pin

    def _pin(self):
        match = LINK.fullmatch(os.readlink(os.path.join(self.root, "current")))
        _need(match, "current_invalid")
        name = match.group(1)
        root_fd, fd = aq._open_chain(self.root), None
        try:
            _need(_private_dir(os.fstat(root_fd)), "export_root_unsafe")
            fd = ce._open_chain(root_fd, ["exports", name])
            _need(fd is not None, "export_missing")
            info = os.fstat(fd)
            _need(_private_dir(info), "export_unsafe")
            raw = _read_file(fd, ce.MANIFEST, MAX_MANIFEST)
            _need(raw is not None, "export_manifest_invalid")
            manifest = aq._strict_load(raw, "export_manifest_invalid")
            run_id, pid, files = manifest["run_id"], manifest["source_project_id"], manifest["files"]
            _need(manifest["schema"] == ce.SCHEMA and isinstance(run_id, str) and ce.RUN_ID.fullmatch(run_id)
                  and name[17:] == run_id and isinstance(pid, str) and PROJECT_ID.fullmatch(pid)
                  and isinstance(files, dict) and all(isinstance(v, str) and ce.HEX.fullmatch(v) for v in files.values()),
                  "export_manifest_invalid")
            key = (info.st_dev, info.st_ino, info.st_mtime_ns)
            if key not in self.verified:
                self._verify(fd, files)
                self.verified.add(key)
            exported_at = manifest.get("exported_at")
            return Pin(os.path.join(self.root, "exports", name), run_id, pid, name,
                       exported_at if isinstance(exported_at, str) else "unknown time")
        finally:
            os.close(root_fd)
            if fd is not None:
                os.close(fd)

    @staticmethod
    def _verify(fd, files):
        found = {}
        for base, _, names, dir_fd in os.fwalk(".", dir_fd=fd, follow_symlinks=False):
            for name in names:
                found[os.path.normpath(os.path.join(base, name))] = _sha256(dir_fd, name)
        found.pop(ce.MANIFEST, None)
        _need(found == files, "export_hash_mismatch")


class LiveSource:
    """The existing live readers, unchanged."""
    snapshot = False

    def __init__(self, project):
        self.project = project

    def queue_rows(self):
        return aq.load_queue(self.project)

    def queue_payload(self, ident):
        return aq.approval_payload(self.project, ident)

    def queue_history(self, ident):
        return aq.history(self.project, ident)

    def packets_pending(self, now=None):
        return pp.pending(self.project, now)

    def packets_status(self, now=None):
        return pp.status(self.project, now)

    def packet_payload(self, ident):
        return pp.payload(self.project, ident)

    def packets_switch(self):
        return pp.switch_state(self.project, read_only=True)

    def public_updates(self):
        from . import console_oversight
        return console_oversight._load_public(self.project)

    def public_updates_switch(self):
        return pu.switch_state(self.project, read_only=True)

    def mail_drafts(self):
        return mail_drafts.placements(self.project)

    def send_results(self):
        return mail_send.results(self.project, strict=True)


class SnapshotSource:
    """The same methods over <pin.path>/console-model/*.json; one instance serves one request."""
    snapshot = True

    def __init__(self, pin):
        self.pin, self._docs = pin, {}

    def _load(self, name):
        """The parsed ok model file, or the code it is unavailable with."""
        root = model = None
        try:
            root = aq._open_chain(self.pin.path)
            model = ce._open_chain(root, [ce.MODEL_DIR])
            raw = None if model is None else _read_file(model, name, MAX_MODEL)
            if raw is None:
                return "console_model_missing"
            doc = aq._strict_load(raw, "console_model_invalid")
        except (aq.ApprovalQueueError, ce.ExportError, SnapshotError, OSError):
            return "console_model_invalid"
        finally:
            for fd in (root, model):
                if fd is not None:
                    os.close(fd)
        if type(doc) is not dict or doc.get("schema") != ce.MODEL_SCHEMA or doc.get("state") not in ("ok", "unavailable"):
            return "console_model_invalid"
        if doc.get("source_project_id") != self.pin.source_project_id:
            return "console_model_mismatch"
        if doc["state"] == "unavailable":
            return doc["code"] if isinstance(doc.get("code"), str) and doc["code"] else "console_model_invalid"
        return doc

    def _doc(self, name, error, key=None, kind=None):
        if name not in self._docs:
            self._docs[name] = self._load(name)
        doc = self._docs[name]
        if isinstance(doc, str):
            raise error(doc)
        value = doc if key is None else doc.get(key)
        if key is not None and type(value) is not kind:
            raise error("console_model_invalid")
        return value

    def queue_rows(self):
        return self._doc("queue.json", aq.ApprovalQueueError, "rows", list)

    def _queue_item(self, key, ident):
        found = self._doc("queue.json", aq.ApprovalQueueError, key, dict)
        if ident not in found:
            raise aq.ApprovalQueueError("unknown_id")
        return found[ident]

    def queue_payload(self, ident):
        text = self._queue_item("payloads", ident)
        if type(text) is not str:
            raise aq.ApprovalQueueError("console_model_invalid")
        return text.encode("utf-8")

    def queue_history(self, ident):
        return self._queue_item("history", ident)

    # The exported packet rows were computed at export time; their ages are the ages then.
    def packets_pending(self, now=None):
        return self._doc("packets.json", pp.PacketError, "pending", list)

    def packets_status(self, now=None):
        return self._doc("packets.json", pp.PacketError, "status", dict)

    def packet_payload(self, ident):
        text = self._doc("packets.json", pp.PacketError, "payloads", dict).get(ident)
        if type(text) is not str:
            raise pp.PacketError("packet_unknown")
        return text.encode("utf-8")

    def packets_switch(self):
        return self._doc("packets.json", pp.PacketError, "switch", str)

    def public_updates(self):
        name, error = "public-updates.json", pu.PublicUpdatesError
        try:
            return {"state": "ok", "lines": self._doc(name, error, "lines", list),
                    "switch": self._doc(name, error, "switch", str), "alerts": self._doc(name, error, "alerts", list)}
        except error as failure:
            return {"state": "unavailable", "code": failure.code}

    def public_updates_switch(self):
        return self._doc("public-updates.json", pu.PublicUpdatesError, "switch", str)

    def mail_drafts(self):
        return self._doc("mail-drafts.json", mail_drafts.MailDraftError, "rows", list)

    def send_results(self):
        return self._doc("sends.json", mail_send.MailSendError, "rows", list)
