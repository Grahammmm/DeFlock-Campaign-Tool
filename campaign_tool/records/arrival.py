"""Arrival stamps for lock-safe incremental records runs (REC-AUTO-1).

A stamp is a tiny owner-only JSON file ``arrival/<utc-ts>-<source>.json`` holding
exactly ``{"source", "ref", "seen_at"}``. ``ref`` is a caller-hashed opaque
64-hex identity; no subject, address, path or Message-ID is ever stored. The
run wrapper claims a fixed batch (``consume``) only after its preflights, moves
it under ``arrival/done/<run_id>/`` and records it on the run report.

Coordination state (metadata lock, ``arrival.pending`` marker, drop-folder
inventory) lives beside ``arrival/`` in the runtime directory so writing it
never wakes the path unit watching ``arrival/``. The metadata lock is distinct
from ``run-records.lock`` and is never held while records work or notify runs.

Nothing here calls a model, mailer, publication or approval interface, and the
drop scan never opens or parses record bytes.
"""
import argparse
import bisect
import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SOURCES = ("inbox-watch", "drop-folder")
REF_RE = re.compile(r"^[0-9a-f]{64}$")
RUN_ID_RE = re.compile(r"^run_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}$")
SEEN_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z$")
STAMP_NAME_RE = re.compile(r"^[0-9]{8}T[0-9]{6}\.[0-9]{6}Z-(inbox-watch|drop-folder)(-[0-9]{2})?\.json$")
MAX_STAMP_BYTES = 4096
MAX_STAMPS = 1000
LOCK_NAME = "arrival.lock"
MARKER_NAME = "arrival.pending"
INVENTORY_NAME = "arrival-drop.json"
MAX_INVENTORY_BYTES = 4 * 1024 * 1024
MAX_DROP_FILES = 5000
SETTLE_SECONDS = 2.0
TEMP_SUFFIXES = (".tmp", ".part", ".partial", ".crdownload", ".swp", "~")


class ArrivalError(Exception):
    """Carries a fixed code only, never a private value."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _utc_now():
    return datetime.now(timezone.utc)


def _seen_at(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _check_dir(path, create):
    """Return an fd-checked owner-only directory (0700), creating it if allowed."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if not create:
            return False
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        except OSError:
            raise ArrivalError("ARRIVAL_STATE_UNWRITABLE") from None
        info = os.lstat(path)
    except OSError:
        raise ArrivalError("ARRIVAL_STATE_UNSAFE") from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ArrivalError("ARRIVAL_STATE_UNSAFE")
    return True


def _paths(runtime_dir):
    root = Path(runtime_dir)
    if not root.is_absolute():
        raise ArrivalError("ARRIVAL_RUNTIME_INVALID")
    return root, root / "arrival", root / "arrival" / "done"


def _open_private(path, flags, mode=0o600):
    try:
        fd = os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    except FileNotFoundError:
        raise
    except OSError:
        raise ArrivalError("ARRIVAL_STATE_UNSAFE") from None
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        os.close(fd)
        raise ArrivalError("ARRIVAL_STATE_UNSAFE")
    return fd


def _read_bounded(path, limit):
    fd = _open_private(path, os.O_RDONLY)
    try:
        data = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise ArrivalError("ARRIVAL_STAMP_INVALID")
    return data


class _MetaLock:
    """Short metadata lock; distinct from run-records.lock."""

    def __init__(self, root, create):
        self.root, self.create, self.fd = root, create, None

    def __enter__(self):
        path = self.root / LOCK_NAME
        flags = os.O_RDWR | (os.O_CREAT if self.create else 0)
        try:
            self.fd = _open_private(path, flags)
        except FileNotFoundError:
            raise ArrivalError("ARRIVAL_STATE_MISSING") from None
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        os.close(self.fd)  # closing releases the flock
        return False


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def parse_seen_at(text):
    """Calendar-validate an arrival ``seen_at`` (UTC, optional fractional seconds)."""
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ" if "." in text else "%Y-%m-%dT%H:%M:%SZ"
    return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)


def validate_stamp(data):
    if not isinstance(data, dict) or set(data) != {"source", "ref", "seen_at"}:
        raise ArrivalError("ARRIVAL_STAMP_INVALID")
    if data["source"] not in SOURCES:
        raise ArrivalError("ARRIVAL_SOURCE_INVALID")
    if not isinstance(data["ref"], str) or not REF_RE.match(data["ref"]):
        raise ArrivalError("ARRIVAL_REF_INVALID")
    seen = data["seen_at"]
    if not isinstance(seen, str) or not SEEN_RE.match(seen):
        raise ArrivalError("ARRIVAL_SEEN_INVALID")
    try:
        parse_seen_at(seen)
    except ValueError:
        raise ArrivalError("ARRIVAL_SEEN_INVALID") from None
    return {"source": data["source"], "ref": data["ref"], "seen_at": seen}


def _load_stamp(path):
    try:
        data = json.loads(_read_bounded(path, MAX_STAMP_BYTES))
    except (ValueError, UnicodeDecodeError):
        raise ArrivalError("ARRIVAL_STAMP_INVALID") from None
    return validate_stamp(data)


def stamp(runtime_dir, source, ref, _held=False):
    """Durably record one arrival; return the stamp file name.

    Publication (link + unlink of the private temp name) runs under the short
    metadata lock, so a concurrent ``pending``/``consume`` never sees the
    transient second hard link.
    """
    if source not in SOURCES:
        raise ArrivalError("ARRIVAL_SOURCE_INVALID")
    if not isinstance(ref, str) or not REF_RE.match(ref):
        raise ArrivalError("ARRIVAL_REF_INVALID")
    root, arrival, done = _paths(runtime_dir)
    if not _check_dir(root, False):
        raise ArrivalError("ARRIVAL_RUNTIME_INVALID")
    _check_dir(arrival, True)
    _check_dir(done, True)
    moment = _utc_now()
    body = json.dumps({"source": source, "ref": ref, "seen_at": _seen_at(moment)},
                      sort_keys=True).encode("ascii") + b"\n"
    tmp = arrival / f".tmp-{os.getpid()}-{os.urandom(6).hex()}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            os.write(fd, body)
            os.fsync(fd)
        finally:
            os.close(fd)
        stem = moment.strftime("%Y%m%dT%H%M%S.%fZ")
        lock = contextlib.nullcontext() if _held else _MetaLock(root, True)
        with lock:
            for suffix in [""] + [f"-{n:02d}" for n in range(1, 100)]:
                final = arrival / f"{stem}-{source}{suffix}.json"
                try:
                    os.link(tmp, final)  # exclusive: never overwrites a same-clock stamp
                    break
                except FileExistsError:
                    continue
            else:
                raise ArrivalError("ARRIVAL_PUBLISH_FAILED")
            os.unlink(tmp)
            _fsync_dir(arrival)
    except ArrivalError:
        raise
    except OSError:
        raise ArrivalError("ARRIVAL_PUBLISH_FAILED") from None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return final.name


def _stamp_files(arrival):
    names = []
    for entry in sorted(os.listdir(arrival)):
        if entry == "done" or entry.startswith("."):
            continue
        if not STAMP_NAME_RE.match(entry):
            continue
        names.append(entry)
    if len(names) > MAX_STAMPS:
        raise ArrivalError("ARRIVAL_TOO_MANY")
    return names


def pending(runtime_dir):
    """Validated unconsumed stamps in filename order; never mutates."""
    _, arrival, _ = _paths(runtime_dir)
    if not _check_dir(arrival, False):
        return []
    root = arrival.parent
    lock = _MetaLock(root, False) if os.path.lexists(root / LOCK_NAME) else contextlib.nullcontext()
    with lock:
        return [_load_stamp(arrival / name) for name in _stamp_files(arrival)]


def consume(runtime_dir, run_id):
    """Claim a fixed snapshot for ``run_id``; idempotent and crash-recoverable."""
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
        raise ArrivalError("ARRIVAL_RUN_ID_INVALID")
    root, arrival, done = _paths(runtime_dir)
    if not _check_dir(arrival, False):
        return []
    _check_dir(done, True)
    batch_dir = done / run_id
    manifest = done / f".{run_id}.claim"
    with _MetaLock(root, True):
        if manifest.exists() or manifest.is_symlink():
            try:
                names = json.loads(_read_bounded(manifest, 64 * MAX_STAMPS))
            except ValueError:
                raise ArrivalError("ARRIVAL_STATE_UNSAFE") from None
            if (not isinstance(names, list) or len(names) > MAX_STAMPS
                    or not all(isinstance(n, str) and STAMP_NAME_RE.match(n) for n in names)):
                raise ArrivalError("ARRIVAL_STATE_UNSAFE")
        else:
            names = _stamp_files(arrival)
            for name in names:  # validate before claiming anything
                _load_stamp(arrival / name)
            tmp = done / f".{run_id}.claim.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                os.write(fd, json.dumps(names).encode("ascii"))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, manifest)
            _fsync_dir(done)
        _check_dir(batch_dir, True)
        for name in names:  # finish a partial move from an earlier crash
            src, dst = arrival / name, batch_dir / name
            if src.exists() and not dst.exists():
                os.rename(src, dst)
            elif src.exists() and dst.exists():
                raise ArrivalError("ARRIVAL_STATE_UNSAFE")
        _fsync_dir(batch_dir)
        _fsync_dir(arrival)
        return [_load_stamp(batch_dir / name) for name in names if (batch_dir / name).exists()]


# --- pending marker (set by an arrival that finds the records lock busy) ---------

def mark_pending(runtime_dir):
    root, _, _ = _paths(runtime_dir)
    if not _check_dir(root, False):
        raise ArrivalError("ARRIVAL_RUNTIME_INVALID")
    with _MetaLock(root, True):
        tmp = root / (MARKER_NAME + ".tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                os.write(fd, _seen_at(_utc_now()).encode("ascii") + b"\n")
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, root / MARKER_NAME)
            _fsync_dir(root)
        except OSError:
            raise ArrivalError("ARRIVAL_MARKER_FAILED") from None


def _marker_exists(root):
    return os.path.lexists(root / MARKER_NAME)


def has_work(runtime_dir, stamps_only=False):
    root, arrival, _ = _paths(runtime_dir)
    if not _check_dir(root, False):
        return False
    if _marker_exists(root) and not stamps_only:
        return True
    return bool(_check_dir(arrival, False) and _stamp_files(arrival))


def take_pending(runtime_dir):
    """Atomically remove the marker; True when it was set (clear-before-follow-up)."""
    root, _, _ = _paths(runtime_dir)
    if not _check_dir(root, False):
        return False
    with _MetaLock(root, True):
        if not _marker_exists(root):
            return False
        os.unlink(root / MARKER_NAME)
        _fsync_dir(root)
        return True


def claim(runtime_dir, run_id):
    """Run-start claim: consume the batch and drop the now-stale marker atomically."""
    root, _, _ = _paths(runtime_dir)
    batch = consume(runtime_dir, run_id)
    take_pending(runtime_dir)
    return batch


# --- drop folder -----------------------------------------------------------------

def drop_ref(name, size, mtime_ns):
    return hashlib.sha256(f"drop-v1\0{name}\0{size}\0{mtime_ns}".encode("utf-8", "surrogateescape")).hexdigest()


CURSOR_KEY = "_cursor"
CARRY_KEY = "_unsettled"


def scan_drop(runtime_dir, drop_dir, now=None):
    """Stamp new or changed completed files at the drop root; never read file bytes."""
    return scan_drop_status(runtime_dir, drop_dir, now)[0]


def scan_drop_status(runtime_dir, drop_dir, now=None):
    """Return (stamped, unsettled, more).

    unsettled counts regular final files still inside the settle window, so a
    caller can rescan instead of losing their only wake. At most MAX_DROP_FILES
    candidate names are examined per call, starting after a persisted cursor, so no tail of a large
    retained folder is starved; more is True while names after the cursor remain unexamined; the cursor resets at the end.
    """
    root, _, _ = _paths(runtime_dir)
    if not _check_dir(root, False):
        raise ArrivalError("ARRIVAL_RUNTIME_INVALID")
    drop = Path(drop_dir)
    if not drop.is_absolute():
        raise ArrivalError("ARRIVAL_DROP_INVALID")
    try:
        info = os.lstat(drop)
    except FileNotFoundError:
        return 0, 0, False
    except OSError:
        raise ArrivalError("ARRIVAL_DROP_INVALID") from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ArrivalError("ARRIVAL_DROP_INVALID")
    now = time.time() if now is None else now
    stamped = 0
    unsettled = 0
    with _MetaLock(root, True):
        inv_path = root / INVENTORY_NAME
        try:
            inventory = json.loads(_read_bounded(inv_path, MAX_INVENTORY_BYTES))
        except FileNotFoundError:
            inventory = {}
        except ValueError:
            raise ArrivalError("ARRIVAL_STATE_UNSAFE") from None
        if not isinstance(inventory, dict):
            raise ArrivalError("ARRIVAL_STATE_UNSAFE")
        # Staging names are dropped before the cap so they never consume it.
        candidates = sorted(
            n for n in os.listdir(drop) if not (n.startswith(".") or n.endswith(TEMP_SUFFIXES))
        )
        cursor = inventory.get(CURSOR_KEY)
        carried = inventory.get(CARRY_KEY)
        carried = carried if isinstance(carried, int) and not isinstance(carried, bool) and cursor is not None else 0
        start = bisect.bisect_right(candidates, cursor) if isinstance(cursor, str) else 0
        order = candidates[start:]
        names = order[:MAX_DROP_FILES]
        more = len(order) > MAX_DROP_FILES
        if more:
            inventory[CURSOR_KEY] = names[-1]
        else:
            inventory.pop(CURSOR_KEY, None)
            inventory.pop(CARRY_KEY, None)
        for name in names:
            try:
                st = os.lstat(drop / name)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue  # symlinks, directories and specials are never acknowledged
            if now - st.st_mtime < SETTLE_SECONDS:
                unsettled += 1
                continue  # still settling: left unacknowledged for a later wake
            ref = drop_ref(name, st.st_size, st.st_mtime_ns)
            key = hashlib.sha256(name.encode("utf-8", "surrogateescape")).hexdigest()
            if inventory.get(key) == ref:
                continue
            try:
                again = os.lstat(drop / name)
            except OSError:
                continue
            if (again.st_size, again.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
                continue  # mutating
            stamp(runtime_dir, "drop-folder", ref, _held=True)  # durable before the inventory
            inventory[key] = ref
            stamped += 1
        if more:
            # Earlier pages' unsettled files must survive pagination, or a
            # settled final page would end the wake and lose their only trigger.
            inventory[CARRY_KEY] = carried + unsettled
        else:
            unsettled += carried
        data = json.dumps(inventory, sort_keys=True).encode("ascii")
        tmp = root / (INVENTORY_NAME + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, inv_path)
    return stamped, unsettled, more


# --- command line ----------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(prog="records arrival", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("stamp", "list", "scan-drop", "claim", "mark-pending", "take-pending", "has-work"):
        item = sub.add_parser(name)
        item.add_argument("--runtime-dir", required=True)
        if name == "stamp":
            item.add_argument("--source", required=True)
            item.add_argument("--ref", required=True)
        if name == "has-work":
            item.add_argument("--stamps-only", action="store_true")
        if name == "scan-drop":
            item.add_argument("--drop-dir", required=True)
        if name == "claim":
            item.add_argument("--run-id", required=True)
            item.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "stamp":
            stamp(args.runtime_dir, args.source, args.ref)
        elif args.command == "list":
            print(json.dumps(pending(args.runtime_dir)))
        elif args.command == "scan-drop":
            done, waiting, more = scan_drop_status(args.runtime_dir, args.drop_dir)
            print(f"stamped={done} unsettled={waiting} more={int(more)}")
        elif args.command == "claim":
            batch = claim(args.runtime_dir, args.run_id)
            out = Path(args.out)
            tmp = out.with_name(out.name + ".tmp")
            tmp.write_text(json.dumps(batch) + "\n", encoding="utf-8")
            os.replace(tmp, out)
        elif args.command == "mark-pending":
            mark_pending(args.runtime_dir)
        elif args.command == "take-pending":
            return 0 if take_pending(args.runtime_dir) else 1
        elif args.command == "has-work":
            return 0 if has_work(args.runtime_dir, args.stamps_only) else 1
    except ArrivalError as exc:
        print(f"arrival: refused ({exc.code})", file=sys.stderr)
        return 2
    except OSError:
        print("arrival: refused (ARRIVAL_IO_FAILED)", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
