"""Verified backup and restore of a campaign directory (docs/BACKUP.md).

``export(root, out_path)`` writes one uncompressed tar (originals are usually
already compressed; the archive is meant to be encrypted and stored by the
organizer) containing ``campaign.json``, ``content/``, ``kit/``,
``private/ledger.sqlite``, ``private/objects/**`` and a ``manifest.json`` with
the SHA-256 and size of every member, counts and the engine version.

``verify(path)`` re-hashes every member against the manifest and reports.
``restore(path, root)`` refuses a non-empty root, rejects unsafe member names,
extracts with private modes (0700 directories, 0600 files) and re-verifies the
files on disk.

``export_hosted(client, out_path)`` builds the same archive for a hosted
campaign from the workspace Worker: the D1 export (``/api/runner/export.json``)
becomes ``private/d1-export.json``, every ``original`` row is fetched by hash
and verified before it is stored under ``private/objects/<sha256>``, and
``campaign.json`` is derived from the campaign row. ``client`` needs a single
method ``get(path) -> bytes``; the runner supplies one bound to its token.
"""
import hashlib
import io
import json
import os
import shutil
import sqlite3
import stat
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from . import __version__

SCHEMA_VERSION = 1
MANIFEST = "manifest.json"
PRIVATE_PREFIX = "private/"
INCLUDED_DIRS = ("content", "kit")
MAX_MEMBER_BYTES = 2 * 1024 * 1024 * 1024
MAX_MEMBERS = 200_000
HEX64 = frozenset("0123456789abcdef")


class BackupError(ValueError):
    """Export, verification or restore refused; nothing partial is left behind."""


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _members(root):
    """Yield (archive_name, path) for every included regular file, sorted."""
    root = Path(root)
    wanted = []
    campaign = root / "campaign.json"
    if campaign.is_file() and not campaign.is_symlink():
        wanted.append(("campaign.json", campaign))
    for name in INCLUDED_DIRS:
        base = root / name
        if base.is_dir() and not base.is_symlink():
            for path in sorted(base.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    wanted.append((path.relative_to(root).as_posix(), path))
    ledger = root / "private" / "ledger.sqlite"
    if ledger.is_file() and not ledger.is_symlink():
        wanted.append(("private/ledger.sqlite", ledger))
    objects = root / "private" / "objects"
    if objects.is_dir():
        for path in sorted(objects.iterdir()):
            if path.is_file() and not path.is_symlink() and len(path.name) == 64 and set(path.name) <= HEX64:
                wanted.append(("private/objects/" + path.name, path))
    return wanted


def _ledger_counts(ledger_path):
    if not ledger_path or not Path(ledger_path).is_file():
        return {"objects": 0, "receipts": 0}
    db = sqlite3.connect(Path(ledger_path).as_uri() + "?mode=ro", uri=True)
    try:
        objects = db.execute("SELECT count(*) FROM objects").fetchone()[0]
        receipts = db.execute("SELECT count(*) FROM receipts").fetchone()[0]
    except sqlite3.Error:
        objects, receipts = 0, 0
    finally:
        db.close()
    return {"objects": objects, "receipts": receipts}


def _manifest(files, counts, source):
    return {"schema_version": SCHEMA_VERSION, "engine_version": __version__, "created_at": _now(),
            "source": source, "files": files,
            "counts": {**counts, "files": len(files), "bytes": sum(f["bytes"] for f in files.values())}}


def _add_bytes(tar, name, data, mode=0o600):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    info.mtime = int(datetime.now(timezone.utc).timestamp())
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    tar.addfile(info, io.BytesIO(data))


def _snapshot_ledger(ledger, tmpdir):
    """Consistent copy of the SQLite ledger via the backup API (no torn pages)."""
    target = Path(tmpdir) / "ledger.sqlite"
    source = sqlite3.connect(Path(ledger).as_uri() + "?mode=ro", uri=True)
    try:
        dest = sqlite3.connect(target)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    return target


def export(root, out_path):
    """Write the archive; returns the manifest dict. Refuses to overwrite ``out_path``."""
    root = Path(root).resolve()
    out_path = Path(out_path)
    if not (root / "campaign.json").is_file():
        raise BackupError("campaign.json not found; is this a campaign directory?")
    if out_path.exists():
        raise BackupError(f"{out_path} exists; choose a new file name")
    members = _members(root)
    import tempfile
    files = {}
    with tempfile.TemporaryDirectory(prefix="deflock-backup-") as tmp:
        staged = []
        for name, path in members:
            if name == "private/ledger.sqlite":
                path = _snapshot_ledger(path, tmp)
            size = path.stat().st_size
            if size > MAX_MEMBER_BYTES:
                raise BackupError(f"{name}: larger than the archive member limit")
            digest = _sha256(path)
            if name.startswith("private/objects/") and name[16:] != digest:
                raise BackupError(f"{name}: stored object does not match its hash; intake integrity problem")
            files[name] = {"sha256": digest, "bytes": size}
            staged.append((name, path))
        ledger = next((p for n, p in staged if n == "private/ledger.sqlite"), None)
        manifest = _manifest(files, _ledger_counts(ledger), {"kind": "local", "root_name": root.name})
        fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle, tarfile.open(fileobj=handle, mode="w", format=tarfile.PAX_FORMAT) as tar:
                _add_bytes(tar, MANIFEST, json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n")
                for name, path in staged:
                    info = tar.gettarinfo(str(path), arcname=name)
                    info.mode = 0o600
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    with open(path, "rb") as source:
                        tar.addfile(info, source)
        except BaseException:
            out_path.unlink(missing_ok=True)
            raise
    return manifest


def _safe_name(name):
    if not name or name.startswith("/") or name.startswith("\\") or "\x00" in name:
        return False
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return False
    if ":" in parts[0] and len(parts[0]) >= 2 and parts[0][1] == ":":
        return False
    return True


def _read_manifest(tar):
    try:
        member = tar.getmember(MANIFEST)
    except KeyError:
        raise BackupError("manifest.json missing from archive") from None
    if not member.isfile() or member.size > 64 * 1024 * 1024:
        raise BackupError("manifest.json is not a regular file of sane size")
    try:
        manifest = json.loads(tar.extractfile(member).read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError(f"manifest.json unreadable: {exc}") from None
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION \
            or not isinstance(manifest.get("files"), dict):
        raise BackupError("manifest.json has an unsupported shape")
    return manifest


def _check_member(member, expected):
    if member.islnk() or member.issym() or member.isdev() or member.isfifo():
        return "unsafe_member_type"
    if member.isdir():
        return None
    if not member.isfile():
        return "unsafe_member_type"
    if not _safe_name(member.name):
        return "unsafe_member_name"
    if member.name == MANIFEST:
        return None
    if member.name not in expected:
        return "not_in_manifest"
    if member.size != expected[member.name]["bytes"]:
        return "size_mismatch"
    return None


def verify(path):
    """Re-hash every member against the manifest. Returns a report; raises on any problem."""
    path = Path(path)
    problems = []
    with tarfile.open(path, mode="r:") as tar:
        manifest = _read_manifest(tar)
        expected = manifest["files"]
        seen = set()
        bad = set()
        for member in tar.getmembers():
            problem = _check_member(member, expected)
            if problem:
                problems.append({"member": member.name, "problem": problem})
                if problem == "size_mismatch":
                    # the member is present, just wrong: report once, not also as missing
                    seen.add(member.name)
                    bad.add(member.name)
                continue
            if not member.isfile() or member.name == MANIFEST:
                continue
            seen.add(member.name)
            digest = hashlib.sha256()
            source = tar.extractfile(member)
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
            if digest.hexdigest() != expected[member.name]["sha256"]:
                problems.append({"member": member.name, "problem": "hash_mismatch"})
                bad.add(member.name)
        for name in sorted(set(expected) - seen):
            problems.append({"member": name, "problem": "missing_member"})
    report = {"path": str(path), "ok": not problems, "files": len(expected), "verified": len(seen - bad), "problems": problems,
        "engine_version": manifest.get("engine_version"), "created_at": manifest.get("created_at"),
        "counts": manifest.get("counts", {})}
    if problems:
        raise BackupError("verification failed: " + "; ".join(f"{p['member']}: {p['problem']}" for p in problems[:10]))
    return report


def restore(path, root):
    """Extract into an empty ``root`` with private modes, then re-verify on disk."""
    path = Path(path)
    root = Path(root)
    if root.is_symlink():
        raise BackupError("restore target must not be a symlink")
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise BackupError(f"{root} is not an empty directory; restore refuses to overwrite")
    report = verify(path)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    try:
        with tarfile.open(path, mode="r:") as tar:
            manifest = _read_manifest(tar)
            for member in tar.getmembers():
                if not member.isfile() or member.name == MANIFEST:
                    continue
                target = root / member.name
                partial = root
                for part in Path(member.name).parts[:-1]:
                    partial = partial / part
                    partial.mkdir(exist_ok=True, mode=0o700)
                    os.chmod(partial, 0o700)
                source = tar.extractfile(member)
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    shutil.copyfileobj(source, handle)
                os.chmod(target, 0o600)
        (root / "private").mkdir(exist_ok=True, mode=0o700)
        os.chmod(root / "private", 0o700)
        mismatches = []
        for name, entry in manifest["files"].items():
            target = root / name
            if not target.is_file() or _sha256(target) != entry["sha256"]:
                mismatches.append(name)
            elif stat.S_IMODE(target.stat().st_mode) != 0o600:
                mismatches.append(name + " (mode)")
        if mismatches:
            raise BackupError("restored files failed re-verification: " + ", ".join(mismatches[:10]))
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    report["restored_to"] = str(root)
    return report


# --- hosted campaign ---------------------------------------------------------

EXPORT_PATH = "/api/runner/export.json"
ORIGINAL_PATH = "/api/runner/originals/{sha256}"


def export_hosted(client, out_path):
    """Archive a hosted campaign through the workspace runner API; returns the manifest."""
    out_path = Path(out_path)
    if out_path.exists():
        raise BackupError(f"{out_path} exists; choose a new file name")
    raw = client.get(EXPORT_PATH)
    try:
        export_doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError) as exc:
        raise BackupError(f"export.json unreadable: {exc}") from None
    rows = export_doc.get("rows") if isinstance(export_doc, dict) else None
    if not isinstance(rows, dict) or not rows.get("campaign"):
        raise BackupError("export.json has no campaign row")
    campaign = rows["campaign"][0]
    campaign_json = json.dumps({
        "schema_version": 1, "name": campaign.get("name"), "county": campaign.get("county_name"),
        "state": str(campaign.get("jurisdiction", "us-xx")).split("-")[-1].upper(), "country": "US",
        "jurisdiction_verified": False, "law_package_status": campaign.get("law_package_status", "unreviewed"),
        "external_sends": campaign.get("external_sends", "disabled"), "publication": "manual",
        "newsletter": {"mode": "not_configured"}, "hosted_campaign_id": campaign.get("campaign_id"),
    }, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    canonical_export = json.dumps(export_doc, indent=None, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    members = [("campaign.json", campaign_json), ("private/d1-export.json", canonical_export)]
    originals = rows.get("original") or []
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    files = {}
    try:
        with os.fdopen(fd, "wb") as handle, tarfile.open(fileobj=handle, mode="w", format=tarfile.PAX_FORMAT) as tar:
            # Manifest is written last in memory but must be first in the archive for
            # streaming verification, so members are buffered to a spool file.
            import tempfile
            with tempfile.TemporaryDirectory(prefix="deflock-hosted-") as tmp:
                staged = []
                for name, data in members:
                    files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                    staged.append((name, data))
                for row in originals:
                    sha = str(row.get("sha256", ""))
                    if len(sha) != 64 or not set(sha) <= HEX64:
                        raise BackupError(f"original row with invalid sha256 {sha!r}")
                    data = client.get(ORIGINAL_PATH.format(sha256=sha))
                    if hashlib.sha256(data).hexdigest() != sha:
                        raise BackupError(f"original {sha[:16]} did not match its hash when fetched; backup stopped")
                    spool = Path(tmp) / sha
                    spool.write_bytes(data)
                    files["private/objects/" + sha] = {"sha256": sha, "bytes": len(data)}
                    staged.append(("private/objects/" + sha, spool))
                counts = {"objects": len(originals), "receipts": len(rows.get("receipt_occurrence") or []),
                          "tables": len(rows)}
                manifest = _manifest(files, counts, {"kind": "hosted", "campaign_id": campaign.get("campaign_id"),
                                                     "exported_at": export_doc.get("exported_at")})
                _add_bytes(tar, MANIFEST, json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n")
                for name, item in staged:
                    if isinstance(item, bytes):
                        _add_bytes(tar, name, item)
                    else:
                        info = tar.gettarinfo(str(item), arcname=name)
                        info.mode = 0o600
                        info.uid = info.gid = 0
                        info.uname = info.gname = ""
                        with open(item, "rb") as source:
                            tar.addfile(info, source)
    except BaseException:
        out_path.unlink(missing_ok=True)
        raise
    return manifest
