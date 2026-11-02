"""Private per-letter exports and approvals; never a batch send."""
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


def _write(fd, name, data):
    with os.fdopen(os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                           0o600, dir_fd=fd), "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)


def _masked(row):
    draft = json.loads(row["draft_json"])
    text = preview(row["draft_json"])
    names = [row["agency_id"], draft.get("extra", {}).get("agency_name", "")]
    for name in sorted({n for n in names if isinstance(n, str) and n}, key=len, reverse=True):
        text = text.replace(name, "<AGENCY>")
    return text.splitlines()


def export(box, out_dir, agencies=None):
    """Write exact proposed payloads and a readable index into an empty directory."""
    rows = sorted((r for r in box.rows("proposed") if agencies is None or r["agency_id"] in agencies),
                  key=lambda r: (r["agency_id"], r["idempotency_key"]))
    path = Path(out_dir)
    try:
        if not path.exists() and not path.is_symlink():
            if not rows:
                return {"written": 0}
            path.mkdir(mode=0o700)
        fd = _directory(path)
        try:
            if os.listdir(fd):
                raise OutboxError("output directory must be empty")
            if not rows:
                return {"written": 0}
            os.fchmod(fd, 0o700)
            index = ["Read every file before signing. Signing a file approves exactly that letter.", "",
                     "| Agency | Kind | Channel | Fee cap (cents) | Payload sha256 | File |",
                     "| --- | --- | --- | --- | --- | --- |"]
            for row in rows:
                key = row["idempotency_key"]
                if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
                    raise OutboxError("invalid outbox key")
                data = box.approval_payload(key)
                _write(fd, key + ".txt", data)
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
            _write(fd, "INDEX.md", ("\n".join(index) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError as exc:
        raise OutboxError(str(exc)) from exc
    return {"written": len(rows), "dir": str(path)}


def _read(fd, name, limit=None):
    with os.fdopen(os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise OutboxError("not a regular file: " + name)
        if limit is not None and info.st_size > limit:
            raise OutboxError("file too large: " + name)
        data = stream.read() if limit is None else stream.read(limit + 1)
        if limit is not None and len(data) > limit:
            raise OutboxError("file too large: " + name)
        return data


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
                data = _read(fd, name)
                box.db.execute("BEGIN IMMEDIATE")
                try:
                    row = box.row(key)
                    if row is None:
                        result["state"] = "unknown_key"
                    elif row["state"] != "proposed":
                        result["state"] = "not_proposed"
                    elif data != box.approval_payload(key):
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
