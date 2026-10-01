"""Durable private continuation around queue.py; WP1 owns claims and all stages."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import math
import multiprocessing
import os
from pathlib import Path
import re
import signal
import sqlite3
import stat
import tempfile
import time
import uuid

from . import queue

VERSION = "private-queue-runtime-v1"
MAX_PACKET = 1024 * 1024
MAX_CONTROL = 256 * 1024 * 1024
SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS attempts(
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,attempt_id TEXT UNIQUE NOT NULL,
 item_key TEXT NOT NULL,subject TEXT NOT NULL,stage TEXT NOT NULL,
 owner TEXT NOT NULL,run_id TEXT NOT NULL,profile TEXT NOT NULL,
 started_at TEXT NOT NULL,deadline_at TEXT NOT NULL,state TEXT NOT NULL,
 reason TEXT,item_json TEXT NOT NULL,claim_json TEXT,packet_json BLOB,packet_sha256 TEXT,packet_path TEXT);
CREATE TABLE IF NOT EXISTS fairness_cursor(item_key TEXT PRIMARY KEY,last_sequence INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS attempt_resume ON attempts(owner,run_id,profile,state,sequence);
"""


def require(value, reason):
    if not value:
        raise queue.QueueError(reason)


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def now():
    return datetime.now(timezone.utc)


def private(path, directory=False):
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts, "canonical_private_path_required")
    require(not any(p.is_symlink() for p in (path, *path.parents)), "symlink_private_path")
    info = path.stat()
    require(info.st_uid == os.geteuid() and not info.st_mode & 0o077, "owner_only_path_required")
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode), "private_path_type")
    return path


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_new(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _publish(path, raw):
    if path.exists() or path.is_symlink():
        private(path)
        require(path.stat().st_size == len(raw) and path.read_bytes() == raw, "packet_replay_conflict")
    else:
        with tempfile.TemporaryDirectory(prefix=".queue-packet-", dir=path.parent) as temporary:
            staged = Path(temporary) / "packet.json"
            _write_new(staged, raw)
            # Link exposes complete bytes without ever overwriting a committed packet.
            os.link(staged, path, follow_symlinks=False)
    sync_directory(path.parent)


def _prepare_child(adapter, item, sender, timeout):
    try:
        os.setsid()
        def expire(*_):
            os.killpg(os.getpid(), signal.SIGKILL)
        signal.signal(signal.SIGALRM, expire)
        signal.setitimer(signal.ITIMER_REAL, timeout)
        with open(os.devnull, "wb") as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
        result = adapter(item)
        raw = encoded(result)
        if len(raw) > MAX_PACKET:
            raw = encoded({"blocked_reason": "prepare_result_bound"})
        sender.send_bytes(raw)
    except BaseException:
        try:
            sender.send_bytes(encoded({"blocked_reason": "prepare_adapter_failed"}))
        except BaseException:
            pass
    finally:
        sender.close()


def _prepare(adapter, item, timeout):
    """Fixed trusted local adapter in a killable POSIX child, not a record command."""
    require(timeout > 0, "prepare_timeout")
    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    child = context.Process(target=_prepare_child, args=(adapter, item, sender, timeout))
    child.start()
    sender.close()
    try:
        if not receiver.poll(timeout):
            return {"blocked_reason": "prepare_timeout"}
        try:
            raw = receiver.recv_bytes(MAX_PACKET)
            result = json.loads(raw)
        except (EOFError, OSError, ValueError):
            return {"blocked_reason": "prepare_adapter_failed"}
        return result
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            if child.is_alive():
                child.kill()
        child.join(timeout=1)
        if child.is_alive():
            child.kill()
            child.join()
        receiver.close()
        child.close()


class QueueRuntime:
    """Trusted startup fixes StageRunner and local metadata adapter for this journal.

    prepare(item) returns only denominator, derived_text and page_images, or a
    stable blocked_reason code. It receives no runtime or mutation authority.
    It does not register stage content or invoke models.
    """
    def __init__(self, runner, private_root, *, prepare, adapter_id, adapter_version,
                 prepare_timeout_seconds=30):
        from .ledger import stages
        require(isinstance(runner, stages.StageRunner), "trusted_stage_runner_required")
        require(callable(prepare), "installed_prepare_adapter_required")
        require(all(type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value)
                    for value in (adapter_id, adapter_version)), "adapter_identity_required")
        require(type(prepare_timeout_seconds) in (int, float) and
                0 < prepare_timeout_seconds <= queue.LEASE_SECONDS, "prepare_time_bound")
        self.runner, self.prepare = runner, prepare
        self.adapter_id, self.adapter_version = adapter_id, adapter_version
        self.prepare_timeout = prepare_timeout_seconds
        self.root = private(private_root, True)
        require(not any((p / ".git").exists() for p in (self.root, *self.root.parents)),
                "control_root_inside_repository")
        self.packets = self.root / "packets"
        self.database = self.root / "queue-control.sqlite"
        with self._lock():
            if not self.packets.exists():
                self.packets.mkdir(mode=0o700)
            private(self.packets, True)
            fd = os.open(self.database, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                info = os.fstat(fd)
                require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                        and info.st_nlink == 1 and not info.st_mode & 0o077
                        and info.st_size <= MAX_CONTROL, "unsafe_control_database")
            finally:
                os.close(fd)
            self.db = sqlite3.connect(self.database, timeout=2)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA journal_mode=DELETE")
            self.db.executescript(SCHEMA)
            descriptor = encoded({"version": VERSION, "ledger": str(Path(runner.database).absolute()),
                "profile": runner.profile_sha256, "adapter_id": adapter_id, "adapter_version": adapter_version,
                "planner": queue.VERSION, "prepare_timeout_seconds": prepare_timeout_seconds}).decode()
            old = self.db.execute("SELECT value FROM runtime_meta WHERE key='binding'").fetchone()
            if old:
                require(old[0] == descriptor, "control_runtime_binding_changed")
            else:
                with self.db:
                    self.db.execute("INSERT INTO runtime_meta VALUES('binding',?)", (descriptor,))
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO runtime_meta VALUES('visit_sequence','0')")
            sync_directory(self.root)

    def close(self):
        self.db.close()

    @contextmanager
    def _lock(self):
        private(self.root, True)
        fd = os.open(self.root / ".queue.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
                    info.st_uid == os.geteuid() and not info.st_mode & 0o077, "unsafe_control_lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise queue.QueueError("control_busy") from error
            yield
        finally:
            os.close(fd)

    @contextmanager
    def _ledger(self):
        database = Path(self.runner.database).absolute()
        con = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA query_only=ON")
            con.execute("BEGIN")
            self.runner._check(con)
            yield con
        finally:
            con.close()

    def _candidates(self, facts):
        with self._ledger() as con:
            originals = [dict(row) for row in con.execute(
                "SELECT sha256,scope FROM originals ORDER BY sha256 LIMIT ?", (queue.MAX_ORIGINALS + 1,))]
            states = [dict(row) for row in con.execute(
                "SELECT * FROM stage_state LIMIT ?", (queue.MAX_ORIGINALS * 7 + 1,))]
            leases = [dict(row) for row in con.execute(
                "SELECT * FROM work_leases LIMIT ?", (queue.MAX_ORIGINALS * 7 + 1,))]
        # Full validation and denominator remain the unmodified planner's.
        stamp = now().isoformat()
        inventory = queue.plan(originals, states, leases, now=stamp, facts=facts)
        by_subject = {row["sha256"]: [] for row in originals}
        for row in states:
            by_subject[row["original_sha256"]].append(row)
        by_lease = {subject: [] for subject in by_subject}
        for row in leases:
            key = row.get("item_key", "")
            if key.startswith("stage:"):
                subject = key.split(":")[1]
                if subject in by_lease:
                    by_lease[subject].append(row)
        candidates = []
        # At most 20 originals per slice: planner top20 then contains every ready
        # item in that slice, without changing its eligibility or priority rules.
        for offset in range(0, len(originals), 20):
            chunk = originals[offset:offset + 20]
            subjects = {row["sha256"] for row in chunk}
            result = queue.plan(chunk,
                [row for subject in subjects for row in by_subject[subject]],
                [row for subject in subjects for row in by_lease[subject]],
                now=stamp, facts={k: v for k, v in (facts or {}).items() if k in subjects})
            candidates.extend(result["top"])
        cursors = self.db.execute("SELECT item_key,last_sequence FROM fairness_cursor LIMIT ?",
                                 (queue.MAX_ORIGINALS * 7 + 1,)).fetchall()
        require(len(cursors) <= queue.MAX_ORIGINALS * 7, "fairness_cursor_bound")
        visited = {row["item_key"]: row["last_sequence"] for row in cursors}
        candidates.sort(key=lambda item: (visited.get(item["original_sha256"] + ":" + item["stage"], 0),
                                         -item["score"], item["original_sha256"], queue.STAGES.index(item["stage"])))
        require(len(candidates) == inventory["eligible"], "fairness_denominator_mismatch")
        return candidates, inventory

    def _visit(self, key):
        self.db.execute("UPDATE runtime_meta SET value=cast(value AS INTEGER)+1 WHERE key='visit_sequence'")
        tick = int(self.db.execute("SELECT value FROM runtime_meta WHERE key='visit_sequence'").fetchone()[0])
        self.db.execute("INSERT INTO fairness_cursor VALUES(?,?) ON CONFLICT(item_key) "
                        "DO UPDATE SET last_sequence=excluded.last_sequence", (key, tick))

    def _reserve(self, item):
        require(self.database.stat().st_size <= MAX_CONTROL, "control_database_bound")
        instant = now()
        identity = uuid.uuid4().hex
        key = item["original_sha256"] + ":" + item["stage"]
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO attempts(attempt_id,item_key,subject,stage,owner,run_id,profile,started_at,deadline_at,state,item_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,'reserved',?)",
                (identity, key, item["original_sha256"], item["stage"], self.runner.owner, self.runner.run_id,
                 self.runner.profile_sha256, instant.isoformat(),
                 (instant + timedelta(seconds=queue.LEASE_SECONDS)).isoformat(), encoded(item).decode()))
            self._visit(key)
        return dict(self.db.execute("SELECT * FROM attempts WHERE attempt_id=?", (identity,)).fetchone())

    def _state(self, attempt, state, reason=None, **extra):
        values = {"state": state, "reason": reason, **extra}
        with self.db:
            self.db.execute("UPDATE attempts SET " + ",".join(key + "=?" for key in values) +
                            " WHERE attempt_id=?", (*values.values(), attempt["attempt_id"]))
        attempt.update(values)

    def _claim_context(self, attempt):
        from .ledger import stages
        claim = json.loads(attempt["claim_json"])
        with self._ledger() as con:
            _, states = stages._subject(con, attempt["subject"], attempt["stage"], self.runner.run_id)
            head = stages._head(con, attempt["subject"], attempt["stage"])
            require(head is not None, "current_content_required")
            inputs = stages._inputs(states, attempt["stage"], attempt["subject"])
            stages._check_claim(con, self.runner, attempt["subject"], attempt["stage"],
                                head, states, inputs, claim["claim_id"])
            return dict(head), inputs

    def _packet(self, attempt, item, prepared):
        from .ledger import stages
        require(type(prepared) is dict and set(prepared) == {"denominator", "derived_text", "page_images"},
                "prepare_packet_fields")
        denominator = prepared["denominator"]
        require(type(denominator) is dict and set(denominator) == {"kind", "total"}
                and denominator["kind"] in ("bytes", "pages", "rows", "sheets", "items")
                and type(denominator["total"]) is int and 0 <= denominator["total"] <= 1000000000,
                "explicit_denominator_required")
        refs = []
        for kind in ("derived_text", "page_images"):
            require(type(prepared[kind]) is list and len(prepared[kind]) <= 1000, "packet_reference_bound")
            for path in prepared[kind]:
                require(type(path) is str and len(path) <= 4096, "private_reference_required")
                path = private(path)
                info = path.stat()
                refs.append({"kind": kind, "path": str(path), "bytes": info.st_size,
                             "device": info.st_dev, "inode": info.st_ino,
                             "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns})
        head, inputs = self._claim_context(attempt)
        template = {"schema": "ledger-stage-receipt-v1", "subject_sha256": attempt["subject"],
                    "stage": attempt["stage"], "content_sha256": head["content_sha256"],
                    "tier": head["tier"], "author_id": head["author_id"], "role": stages.ROLES[attempt["stage"]],
                    "input_hashes": inputs, "reviewer_id": None, "verdict": None, "rationale": None,
                    "coverage": {"denominator": denominator, "covered": None, "scope": None},
                    "locators": [], "reviews": [], "model_or_tool": None, "created_at_tz": None}
        claimed = {**item, "claim": json.loads(attempt["claim_json"]),
                   "owner": self.runner.owner, "run_id": self.runner.run_id}
        packet = queue.packet(claimed, **prepared, receipt_template=template)
        packet.update(attempt_id=attempt["attempt_id"], deadline_at=attempt["deadline_at"],
                      reference_metadata=refs, adapter_id=self.adapter_id, adapter_version=self.adapter_version,
                      content_revision=head["revision"], stage_promotions=0,
                      reference_bytes_verified=False, runtime_version=VERSION)
        raw = encoded(packet)
        require(len(raw) <= MAX_PACKET, "packet_byte_bound")
        return raw

    def _step(self, attempt, item, end, fault):
        from .ledger.stages import StageError
        remaining = (queue.timestamp(attempt["deadline_at"]) - now()).total_seconds()
        if remaining <= 0:
            self._state(attempt, "blocked", "attempt_deadline_expired")
            return
        if time.monotonic() >= end:
            return
        if attempt["state"] == "reserved":
            try:
                claim = self.runner.claim(attempt["subject"], attempt["stage"],
                    ttl_seconds=max(1, min(queue.LEASE_SECONDS, math.ceil(remaining))))
            except StageError as error:
                reason = str(error)
                allowed = {"current_content_required", "lease_owned_elsewhere", "lease_run_mismatch",
                           "stale_lease_revision", "explicit_supersession_required", "original_out_of_scope"}
                self._state(attempt, "blocked", reason if reason in allowed or reason.startswith("prerequisite_")
                            else "wp1_authority_error")
                if reason not in allowed and not reason.startswith("prerequisite_"):
                    raise
                return
            if fault:
                fault("after_claim_before_journal")
            self._state(attempt, "claimed", claim_json=encoded(claim).decode())
        try:
            self._claim_context(attempt)
        except (StageError, queue.QueueError) as error:
            self._state(attempt, "blocked", str(error))
            return
        if attempt["state"] == "claimed":
            budget = min(self.prepare_timeout, remaining, end - time.monotonic())
            if budget <= 0:
                return
            prepared = _prepare(self.prepare, item, budget)
            if type(prepared) is dict and set(prepared) == {"blocked_reason"}:
                reason = prepared["blocked_reason"]
                if type(reason) is not str or not re.fullmatch(r"[a-z][a-z0-9_]{0,127}", reason):
                    reason = "invalid_prepare_block_reason"
                self._state(attempt, "blocked", reason)
                return
            try:
                raw = self._packet(attempt, item, prepared)
            except (queue.QueueError, StageError, OSError, TypeError, ValueError):
                self._state(attempt, "blocked", "invalid_or_stale_packet_metadata")
                return
            path = self.packets / (attempt["attempt_id"] + ".json")
            self._state(attempt, "packet_staged", packet_json=raw, packet_sha256=sha(raw), packet_path=str(path))
            if fault:
                fault("after_packet_journal")
        if attempt["state"] == "packet_staged":
            raw = bytes(attempt["packet_json"])
            require(sha(raw) == attempt["packet_sha256"], "journal_packet_changed")
            try:
                _publish(Path(attempt["packet_path"]), raw)
            except OSError:
                self._state(attempt, "packet_staged", "packet_write_incomplete")
                return
            if fault:
                fault("after_packet_exposure")
            self._state(attempt, "packet_ready")

    def advance(self, *, facts=None, max_attempts=20, budget_seconds=queue.LEASE_SECONDS, fault=None):
        """Continue the private journal; never promote a WP1 receipt."""
        require(type(max_attempts) is int and 1 <= max_attempts <= 20, "attempt_count_bound")
        require(type(budget_seconds) in (int, float) and 0 < budget_seconds <= queue.LEASE_SECONDS,
                "attempt_time_bound")
        require(fault is None or self.runner.test_only, "synthetic_fault_only")
        end = time.monotonic() + budget_seconds
        attempted = []
        with self._lock():
            candidates, inventory = self._candidates(facts)
            pending = self.db.execute(
                "SELECT * FROM attempts WHERE owner=? AND run_id=? AND profile=? "
                "AND state IN ('reserved','claimed','packet_staged') ORDER BY sequence LIMIT ?",
                (self.runner.owner, self.runner.run_id, self.runner.profile_sha256,
                 queue.MAX_ORIGINALS * 7 + 1)).fetchall()
            require(len(pending) <= queue.MAX_ORIGINALS * 7, "pending_attempt_bound")
            pending_keys = {row["item_key"] for row in pending}
            work = [(json.loads(row["item_json"]), dict(row)) for row in pending]
            work.extend(({**item, "plan_sha256": inventory["plan_sha256"]}, None) for item in candidates
                        if item["original_sha256"] + ":" + item["stage"] not in pending_keys)
            visits = {row["item_key"]: row["last_sequence"] for row in
                      self.db.execute("SELECT * FROM fairness_cursor")}
            work.sort(key=lambda pair: (visits.get(pair[0]["original_sha256"] + ":" + pair[0]["stage"], 0),
                       -pair[0]["score"], pair[0]["original_sha256"], queue.STAGES.index(pair[0]["stage"])))
            for item, existing in work:
                if len(attempted) >= max_attempts or time.monotonic() >= end:
                    break
                if existing is None:
                    attempt = self._reserve(item)
                else:
                    attempt = existing
                    with self.db:
                        self._visit(attempt["item_key"])
                self._step(attempt, item, end, fault)
                attempted.append(attempt)
            return {"runtime_version": VERSION, "snapshot_originals": inventory["snapshot_originals"],
                    "eligible": inventory["eligible"], "excluded": len(inventory["excluded_from_claims"]),
                    "cursor_sequence": int(self.db.execute("SELECT value FROM runtime_meta WHERE key='visit_sequence'").fetchone()[0]),
                    "attempts": [self._summary(row) for row in attempted],
                    "stage_promotions": 0, "publication_ready": False, "budget_seconds": budget_seconds}

    @staticmethod
    def _summary(row):
        return {key: row[key] for key in ("attempt_id", "sequence", "subject", "stage", "state", "reason",
                                          "deadline_at", "packet_path", "packet_sha256")}

    def read_packet(self, attempt_id):
        """Return an exact persisted packet only while its actual WP1 claim is current."""
        require(type(attempt_id) is str and re.fullmatch(r"[a-f0-9]{32}", attempt_id), "attempt_identity")
        with self._lock():
            row = self.db.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            require(row is not None and row["state"] == "packet_ready", "packet_not_ready")
            attempt = dict(row)
            require((attempt["owner"], attempt["run_id"], attempt["profile"]) ==
                    (self.runner.owner, self.runner.run_id, self.runner.profile_sha256), "packet_owner_run_mismatch")
            require(queue.timestamp(attempt["deadline_at"]) > now(), "attempt_deadline_expired")
            self._claim_context(attempt)
            path = private(attempt["packet_path"])
            require(path.parent == self.packets and path.stat().st_size <= MAX_PACKET, "packet_path_bound")
            raw = path.read_bytes()
            require(raw == bytes(attempt["packet_json"]) and sha(raw) == attempt["packet_sha256"],
                    "persisted_packet_changed")
            packet = json.loads(raw)
            for ref in packet["reference_metadata"]:
                info = private(ref["path"]).stat()
                require((info.st_size, info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns) ==
                        (ref["bytes"], ref["device"], ref["inode"], ref["mtime_ns"], ref["ctime_ns"]),
                        "packet_reference_changed")
            return packet
