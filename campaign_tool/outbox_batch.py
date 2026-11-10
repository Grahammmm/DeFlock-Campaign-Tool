"""Private per-letter exports and approvals; never a batch send."""
from contextlib import contextmanager
import difflib
import hashlib
import json
import os
from pathlib import Path
import stat

from campaign_tool import approval
from campaign_tool.outbox import OutboxError, preview


def _directory(path):
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


_CHUNK = 64 * 1024
_ROLLBACK_ERROR = "export failed; partial local output remains"


def _write(fd, name, data):
    raw = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                  0o600, dir_fd=fd)
    identity = None
    try:
        info = os.fstat(raw)
        identity = (info.st_dev, info.st_ino)
        stream = os.fdopen(raw, "wb")
        raw = None  # The stream now owns the descriptor, including on close failure.
        with stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
        return identity
    except Exception as exc:
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != identity:
                raise OutboxError(_ROLLBACK_ERROR)
            os.unlink(name, dir_fd=fd)
        except FileNotFoundError:
            pass
        except Exception:
            raise OutboxError(_ROLLBACK_ERROR) from exc
        raise
    finally:
        if raw is not None:
            os.close(raw)


_EXTRA_ERROR = "draft extra must be an object for batch comparison"


def _extra(draft):
    """Pure admission of the optional comparison metadata: absent means ``{}``; anything else must be an object."""
    extra = draft.get("extra", {})
    if not isinstance(extra, dict):
        raise OutboxError(_EXTRA_ERROR) from None
    return extra


def _preflight(rows):
    """Refuse the whole selection for malformed metadata before any output or payload effect."""
    for row in rows:
        draft = json.loads(row["draft_json"])
        if isinstance(draft, dict):  # other top-level shapes keep their existing later failure
            _extra(draft)


def _masked(row):
    draft = json.loads(row["draft_json"])
    text = preview(row["draft_json"])
    names = [row["agency_id"], _extra(draft).get("agency_name", "")]
    for name in sorted({n for n in names if isinstance(n, str) and n}, key=len, reverse=True):
        text = text.replace(name, "<AGENCY>")
    return text.splitlines()


def export(box, out_dir, agencies=None):
    """Write exact proposed payloads and a readable index into an empty directory."""
    rows = sorted((r for r in box.rows("proposed") if agencies is None or r["agency_id"] in agencies),
                  key=lambda r: (r["agency_id"], r["idempotency_key"]))
    _preflight(rows)
    path = Path(out_dir)
    fd = None
    created = None
    original = None
    owned = {}
    started = False
    try:
        if not path.exists() and not path.is_symlink():
            if not rows:
                return {"written": 0}
            path.mkdir(mode=0o700)
            info = path.lstat()
            created = (info.st_dev, info.st_ino)
        fd = _directory(path)
        original = os.fstat(fd)
        if created is not None and created != (original.st_dev, original.st_ino):
            raise OutboxError(_ROLLBACK_ERROR)
        try:
            if os.listdir(fd):
                raise OutboxError("output directory must be empty")
            if not rows:
                return {"written": 0}
            started = True
            os.fchmod(fd, 0o700)
            index = ["Read every file before signing. Signing a file approves exactly that letter.", "",
                     "| Agency | Kind | Channel | Fee cap (cents) | Payload sha256 | File |",
                     "| --- | --- | --- | --- | --- | --- |"]
            for row in rows:
                key = row["idempotency_key"]
                if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
                    raise OutboxError("invalid outbox key")
                data = box.approval_payload(key)
                owned[key + ".txt"] = _write(fd, key + ".txt", data)
                cells = [row["agency_id"], row["kind"], row["channel"], str(row["fee_cap_cents"]),
                         hashlib.sha256(data).hexdigest()[:12], key + ".txt"]
                index.append("| " + " | ".join(c.replace("|", "\\|").replace("\n", " ").replace("\r", " ")
                                               for c in cells) + " |")
            index += ["", "## How these letters differ"]
            first = _masked(rows[0])
            for row in rows[1:]:
                diff = list(difflib.unified_diff(first, _masked(row), fromfile=rows[0]["idempotency_key"] + ".txt",
                                               tofile=row["idempotency_key"] + ".txt", lineterm=""))
                index += ["", "### " + row["idempotency_key"] + ".txt", "```diff", *(diff[:200] or ["no differences"]), "```"]
                if len(diff) > 200:
                    index.append(f"… {len(diff) - 200} more lines; read the file")
            index += ["", "ssh-keygen -Y sign -n deflock-approval -f <your key> *.txt"]
            owned["INDEX.md"] = _write(fd, "INDEX.md", ("\n".join(index) + "\n").encode("utf-8"))
        except Exception as exc:
            incomplete = False
            for name, identity in owned.items():
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == identity:
                        os.unlink(name, dir_fd=fd)
                    else:
                        incomplete = True
                except FileNotFoundError:
                    pass
                except Exception:
                    incomplete = True
            if started:
                try:
                    info = path.lstat()
                    if (info.st_dev, info.st_ino) != (original.st_dev, original.st_ino):
                        incomplete = True
                    elif created is None:
                        os.fchmod(fd, stat.S_IMODE(original.st_mode))
                    if os.listdir(fd):
                        incomplete = True
                except Exception:
                    incomplete = True
            if incomplete:
                raise OutboxError(_ROLLBACK_ERROR) from exc
            raise
    except Exception as exc:
        if created is not None:
            try:
                info = path.lstat()
                if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != created:
                    raise OutboxError(_ROLLBACK_ERROR)
                path.rmdir()  # Empty only; never traverse or remove foreign entries.
            except Exception:
                raise OutboxError(_ROLLBACK_ERROR) from exc
        if isinstance(exc, OSError):
            raise OutboxError(str(exc)) from exc
        raise
    finally:
        if fd is not None:
            os.close(fd)
    return {"written": len(rows), "dir": str(path)}


@contextmanager
def _admit(fd, name):
    """Open one file relative to ``fd`` once and require a regular file before any read."""
    raw = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        stream = os.fdopen(raw, "rb")
    except BaseException:
        os.close(raw)
        raise
    with stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise OutboxError("not a regular file: " + name)
        yield stream, info


def _read(fd, name, limit):
    """Read a whole small file; ``limit`` is a required finite byte ceiling."""
    with _admit(fd, name) as (stream, info):
        if info.st_size > limit:
            raise OutboxError("file too large: " + name)
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise OutboxError("file too large: " + name)
        return data


def _matches(stream, info, expected):
    """True when the opened file is exactly ``expected``, reading at most len(expected)+1 bytes."""
    if info.st_size > len(expected):
        return False  # Optimization only; the read budget below is still enforced.
    view = memoryview(expected)
    budget = len(expected) + 1  # One byte past the end detects growth.
    offset = 0
    while offset < budget:
        chunk = stream.read(min(_CHUNK, budget - offset))
        if not chunk:
            break
        end = offset + len(chunk)
        if end > len(expected) or chunk != view[offset:end]:
            return False
        offset = end
    return offset == len(expected)


def approve_all(box, directory):
    """Record each file's own signature, isolating failures and never sending."""
    try:
        fd = _directory(directory)
    except OSError as exc:
        raise OutboxError(str(exc)) from exc
    results = []
    try:
        for name in sorted(n for n in os.listdir(fd) if n.endswith(".txt")):
            key = name[:-4]
            result = {"idempotency_key": key}
            try:
                # The descriptor stays open through classification; payload bytes are read only
                # for a proposed row, and only up to the exact expected length plus one.
                with _admit(fd, name) as (stream, info):
                    box.db.execute("BEGIN IMMEDIATE")
                    try:
                        row = box.row(key)
                        if row is None:
                            result["state"] = "unknown_key"
                        elif row["state"] != "proposed":
                            result["state"] = "not_proposed"
                        elif not _matches(stream, info, box.approval_payload(key)):
                            result["state"] = "changed"
                        else:
                            try:
                                signature = _read(fd, name + ".sig", approval.MAX_SIGNATURE).decode("utf-8")
                            except FileNotFoundError:
                                result["state"] = "no_signature"
                            else:
                                approved = box.approve(key, {"kind": "ssh", "signature": signature})
                                result.update(state="approved", approved_by=approved["approved_by"])
                        box.db.execute("COMMIT")
                    except BaseException:
                        box.db.execute("ROLLBACK")
                        raise
            except (OutboxError, OSError, UnicodeError) as exc:
                result.update(state="refused", reason=str(exc))
            results.append(result)
    finally:
        os.close(fd)
    return results
