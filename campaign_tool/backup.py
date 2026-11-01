"""Verified backup and restore of a campaign directory (docs/BACKUP.md).

``export(root, out_path)`` writes one uncompressed tar (originals are usually
already compressed; the archive is meant to be encrypted and stored by the
organizer) containing ``campaign.json``, ``content/``, ``kit/``,
``private/ledger.sqlite``, ``private/objects/**`` and a ``manifest.json`` with
the SHA-256 and size of every member, counts and the engine version.

``verify(path, expected_manifest_sha256=None)`` re-hashes every member against
the manifest and reports. The manifest is inside the archive, so on its own
this is an integrity (corruption and consistency) check, not tamper
detection: whoever can rewrite a member can rewrite ``manifest.json`` too.
``export`` therefore returns ``manifest_sha256`` (the digest of the manifest
bytes as written); record it somewhere the archive is not, and pass it to
``verify`` to detect a rewritten archive.
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
import re
import shutil
import sqlite3
import stat
import tarfile
import tempfile
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
    hosted = root / "private" / "d1-export.json"
    if hosted.is_file() and not hosted.is_symlink():
        wanted.append(("private/d1-export.json", hosted))
    objects = root / "private" / "objects"
    if objects.is_dir() and not objects.is_symlink():
        for path in sorted(objects.iterdir()):
            if path.is_file() and not path.is_symlink() and len(path.name) == 64 and set(path.name) <= HEX64:
                wanted.append(("private/objects/" + path.name, path))
    return wanted


def _valid_sha(value):
    return isinstance(value, str) and len(value) == 64 and set(value) <= HEX64


def _valid_size(value):
    return type(value) is int and 0 <= value <= MAX_MEMBER_BYTES


def _unique_json(raw, label):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise BackupError(f"{label}: duplicate_json_key")
            result[key] = value
        return result

    try:
        return json.loads(raw, object_pairs_hook=unique)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, RecursionError) as exc:
        raise BackupError(f"{label}: unreadable JSON ({type(exc).__name__})") from None


def _originals(rows, label):
    originals = {}
    for sha, size in rows:
        if not _valid_sha(sha) or not _valid_size(size):
            raise BackupError(f"{label}: invalid_object_metadata")
        if sha in originals:
            raise BackupError(f"{label}: duplicate_object_metadata")
        originals[sha] = size
        if len(originals) + 1 > MAX_MEMBERS:
            raise BackupError("member_limit_exceeded")
    return originals


def _reference(sha, originals, label):
    if not _valid_sha(sha):
        raise BackupError(f"{label}: invalid_object_reference")
    if sha not in originals:
        raise BackupError(f"{label}: dangling_object_reference {sha}")


def _ledger_metadata(ledger_path):
    """Read closure from a closed, standalone snapshot, never the live ledger."""
    db = None
    try:
        db = sqlite3.connect(Path(ledger_path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        db.execute("PRAGMA trusted_schema=OFF")
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise BackupError("invalid_ledger: integrity check failed")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"objects", "receipts"} <= tables:
            raise BackupError("invalid_ledger: missing objects or receipts table")
        originals = _originals(db.execute("SELECT sha256, byte_count FROM objects"), "invalid_ledger")
        receipts = 0
        for sha, in db.execute("SELECT object_sha256 FROM receipts"):
            _reference(sha, originals, "invalid_ledger")
            receipts += 1
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BackupError("invalid_ledger: foreign_key_check failed")
    except sqlite3.Error as exc:
        raise BackupError(f"invalid_ledger: {exc}") from None
    finally:
        if db is not None:
            db.close()
    return originals, {"objects": len(originals), "receipts": receipts}


def _hosted_metadata(export_doc):
    if not isinstance(export_doc, dict) or type(export_doc.get("schema_version")) is not int \
            or export_doc["schema_version"] != 1:
        raise BackupError("invalid_d1_export: unsupported shape")
    rows, tables = export_doc.get("rows"), export_doc.get("tables")
    if not isinstance(rows, dict) or not isinstance(tables, list) \
            or not all(isinstance(t, str) for t in tables) or len(set(tables)) != len(tables) \
            or set(tables) != set(rows) or not {"campaign", "original", "receipt_occurrence"} <= set(rows):
        raise BackupError("invalid_d1_export: incomplete table inventory")
    if any(not isinstance(items, list) or any(not isinstance(r, dict) for r in items) for items in rows.values()):
        raise BackupError("invalid_d1_export: invalid rows")
    if len(rows["campaign"]) != 1 or not isinstance(export_doc.get("campaign_id"), str) \
            or not export_doc["campaign_id"] or rows["campaign"][0].get("campaign_id") != export_doc["campaign_id"]:
        raise BackupError("invalid_d1_export: invalid campaign row")
    for items in rows.values():
        if any("campaign_id" in r and r["campaign_id"] != export_doc["campaign_id"] for r in items):
            raise BackupError("invalid_d1_export: campaign mismatch")
    originals = _originals(((r.get("sha256"), r.get("byte_count")) for r in rows["original"]), "invalid_d1_export")
    for table, column in (("receipt_occurrence", "sha256"), ("extraction", "sha256"),
                          ("digest", "sha256"), ("correspondence", "raw_sha256")):
        for row in rows.get(table, []):
            sha = row.get(column)
            if table == "correspondence" and sha is None:
                continue
            _reference(sha, originals, "invalid_d1_export: " + table)
    for row in rows.get("finding", []):
        finding = _unique_json(row.get("finding_json"), "invalid_d1_export: finding_json")
        # Draft findings may not have any sources yet. Preserve them while
        # checking every source reference that has actually been declared.
        if not isinstance(finding, dict) or not isinstance(finding.get("sources", []), list):
            raise BackupError("invalid_d1_export: invalid finding sources")
        for source in finding.get("sources", []):
            _reference(source.get("sha256") if isinstance(source, dict) else None,
                       originals, "invalid_d1_export: finding source")
    return originals, {"objects": len(originals), "receipts": len(rows["receipt_occurrence"]), "tables": len(rows)}


def _metadata(paths):
    ledger, hosted = paths.get("private/ledger.sqlite"), paths.get("private/d1-export.json")
    if ledger and hosted:
        raise BackupError("ambiguous_metadata: both local ledger and D1 export present")
    if ledger:
        return _ledger_metadata(ledger)
    if hosted:
        return _hosted_metadata(_unique_json(Path(hosted).read_bytes(), "invalid_d1_export"))
    return {}, {"objects": 0, "receipts": 0}


def _closure(files, originals, counts, manifest_counts=None, *, has_metadata):
    if not has_metadata and any(name.startswith("private/objects/") for name in files):
        raise BackupError("missing_metadata: objects require a ledger or D1 export")
    for sha, size in originals.items():
        name = "private/objects/" + sha
        if name not in files:
            raise BackupError(f"{name}: missing_object")
        if files[name]["sha256"] != sha:
            raise BackupError(f"{name}: object_hash_mismatch")
        if files[name]["bytes"] != size:
            raise BackupError(f"{name}: object_size_mismatch")
    if manifest_counts is not None:
        for key, value in counts.items():
            if manifest_counts.get(key) != value:
                raise BackupError(f"{key}: count_mismatch")


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
    source = None
    try:
        source = sqlite3.connect(Path(ledger).as_uri() + "?mode=ro", uri=True)
        dest = sqlite3.connect(target)
        try:
            source.backup(dest)
            # A standalone archive carries no WAL/SHM sidecars. Finalize the
            # snapshot's journal format while it is still open, before hashing.
            if dest.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
                raise BackupError("invalid_ledger: snapshot journal was not finalized")
        finally:
            dest.close()
    except sqlite3.Error as exc:
        raise BackupError(f"invalid_ledger: {exc}") from None
    finally:
        if source is not None:
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
    if (root / "private").is_symlink():
        raise BackupError("unsafe_private_directory: private must not be a symlink")
    files = {}
    with tempfile.TemporaryDirectory(prefix="deflock-backup-") as tmp:
        ledger = root / "private" / "ledger.sqlite"
        snapshot = None
        if ledger.exists() or ledger.is_symlink():
            if not ledger.is_file() or ledger.is_symlink() or ledger.parent.is_symlink():
                raise BackupError("invalid_ledger: expected a regular non-symlink file")
            snapshot = _snapshot_ledger(ledger, tmp)
        # Take the database snapshot before enumerating mutable source paths.
        # Its references, not the later live database, define the required bytes.
        members = [(n, p) for n, p in _members(root) if n != "private/ledger.sqlite"]
        if snapshot:
            members.append(("private/ledger.sqlite", snapshot))
        if len(members) + 1 > MAX_MEMBERS:
            raise BackupError("member_limit_exceeded")
        paths = dict(members)
        originals, counts = _metadata(paths)
        staged = []
        for name, path in members:
            size = path.stat().st_size
            if size > MAX_MEMBER_BYTES:
                raise BackupError(f"{name}: larger than the archive member limit")
            digest = _sha256(path)
            if name.startswith("private/objects/") and name[16:] != digest:
                raise BackupError(f"{name}: stored object does not match its hash; intake integrity problem")
            files[name] = {"sha256": digest, "bytes": size}
            staged.append((name, path))
        _closure(files, originals, counts, has_metadata=bool(snapshot or paths.get("private/d1-export.json")))
        manifest = _manifest(files, counts, {"kind": "local", "root_name": root.name})
        manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        manifest["manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
        fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle, tarfile.open(fileobj=handle, mode="w", format=tarfile.PAX_FORMAT) as tar:
                _add_bytes(tar, MANIFEST, manifest_bytes)
                for name, path in staged:
                    info = tar.gettarinfo(str(path), arcname=name)
                    info.mode = 0o600
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    with open(path, "rb") as source:
                        tar.addfile(info, source)
            # Detect source files changing between hashing and archive writing.
            verify(out_path, expected_manifest_sha256=manifest["manifest_sha256"])
        except BaseException:
            out_path.unlink(missing_ok=True)
            raise
    return manifest


DIFF_NAME_CAP = 200


def diff(archive, root, expected_manifest_sha256=None):
    """Names added/removed/changed in ``root`` since the backup; re-verifies first, writes nothing in ``root``."""
    report = verify(archive, expected_manifest_sha256=expected_manifest_sha256)
    with tarfile.open(archive, mode="r:") as tar:
        manifest = _read_manifest(tar)
    if manifest["manifest_sha256"] != report["manifest_sha256"]:
        raise BackupError("manifest_digest_mismatch: archive changed during diff")
    root = Path(root).resolve()
    if not (root / "campaign.json").is_file():
        raise BackupError("campaign.json not found; is this a campaign directory?")
    if (root / "private").is_symlink():
        raise BackupError("unsafe_private_directory: private must not be a symlink")
    with tempfile.TemporaryDirectory(prefix="deflock-diff-") as tmp:
        members = dict(_members(root))
        ledger = root / "private" / "ledger.sqlite"
        if ledger.exists() or ledger.is_symlink():
            if not ledger.is_file() or ledger.is_symlink():
                raise BackupError("invalid_ledger: expected a regular non-symlink file")
            # Same rule as export: hash a consistent temp snapshot, never the live file.
            members["private/ledger.sqlite"] = _snapshot_ledger(ledger, tmp)
        live = {name: {"sha256": _sha256(path), "bytes": path.stat().st_size} for name, path in members.items()}
    old = manifest["files"]
    lists = {"added": sorted(set(live) - set(old)), "removed": sorted(set(old) - set(live)),
             "changed": sorted(n for n in set(old) & set(live)
                               if (old[n]["sha256"], old[n]["bytes"]) != (live[n]["sha256"], live[n]["bytes"]))}
    result = {"archive_created_at": manifest.get("created_at"), "current": not any(lists.values())}
    result.update({key: names[:DIFF_NAME_CAP] for key, names in lists.items()})
    result["unchanged"] = len(set(old) & set(live)) - len(lists["changed"])
    more = {key: len(names) - DIFF_NAME_CAP for key, names in lists.items() if len(names) > DIFF_NAME_CAP}
    if more:
        result["more"] = more
    return result


DRILL_STEPS = ("verify", "restore", "store_check", "doctor")


_ABS_PATH = re.compile(r"(?<![\w>.<])/[^\s'\"<>:;,)]+")


def _drill_detail(exc, tmp, archive=None):
    """Failure text for a receipt: no absolute paths, no temp path, no member digests, bounded."""
    text = f"{type(exc).__name__}: {exc}".replace(tmp, "<copy>")
    if archive is not None:
        archive = Path(archive)
        for shown in {str(archive), str(archive.absolute()), str(archive.resolve())}:
            text = text.replace(shown, archive.name)
    text = _ABS_PATH.sub("<path>", text)
    return re.sub(r"[0-9a-fA-F]{64}", "<sha256>", text)[:300]


def drill(archive, expected_manifest_sha256=None, *, doctor=None, store_check=None, tmp_parent=None):
    """Restore ``archive`` into a fresh temp dir, check the copy, remove it; returns a receipt.

    Only the archive is read: the source campaign directory is never opened.
    """
    if store_check is None:
        from .store_check import check_store as store_check
    if doctor is None:
        from .cli import doctor_report as doctor
    archive = Path(archive)
    if not archive.is_file():
        raise BackupError(f"{archive.name}: archive not found")
    steps = []
    receipt = {"schema": "deflock-backup-drill-v1", "archive_name": archive.name, "manifest_sha256": None,
               "created_at": None, "drilled_at": _now(), "engine_version": None, "files": None,
               "passed": False, "steps": steps}
    with tempfile.TemporaryDirectory(prefix="deflock-drill-", dir=tmp_parent) as tmp:
        copy = Path(tmp) / "campaign"

        def run_verify():
            report = verify(archive, expected_manifest_sha256=expected_manifest_sha256)
            receipt.update({key: report[key] for key in ("manifest_sha256", "created_at", "engine_version", "files")})
            return f"{report['verified']} of {report['files']} files re-hashed"

        def run_store_check():
            count = len(store_check(copy)["problems"])
            return count == 0, f"{count} problem" + ("" if count == 1 else "s")

        def run_doctor():
            report = doctor(copy)
            return report.get("config_readable") is True, {
                "law_package_status": report.get("law_package_status"),
                "agencies_selected": report.get("agencies_selected")}

        actions = {"verify": lambda: (True, run_verify()),
                   "restore": lambda: (True, f"{restore(archive, copy)['files']} files restored to a temp copy"),
                   "store_check": run_store_check, "doctor": run_doctor}
        for name in DRILL_STEPS:
            if steps and steps[-1]["ok"] is not True:
                steps.append({"step": name, "ok": None, "detail": "not run"})
                continue
            try:
                ok, detail = actions[name]()
            except Exception as exc:  # a drill records every failure instead of stopping
                ok, detail = False, _drill_detail(exc, tmp, archive)
            steps.append({"step": name, "ok": ok, "detail": detail})
    receipt["passed"] = all(step["ok"] is True for step in steps)
    return receipt


def _safe_name(name):
    if not isinstance(name, str) or not name or name.startswith("/") or "\\" in name or "\x00" in name:
        return False
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return False
    if ":" in parts[0] and len(parts[0]) >= 2 and parts[0][1] == ":":
        return False
    return True


def _archive_members(tar):
    members, seen = [], set()
    for member in tar:
        if len(members) >= MAX_MEMBERS:
            raise BackupError("member_limit_exceeded")
        if member.name in seen:
            raise BackupError(f"{member.name}: duplicate_member")
        seen.add(member.name)
        members.append(member)
    return members


def _read_manifest(tar, members=None):
    members = _archive_members(tar) if members is None else members
    member = next((m for m in members if m.name == MANIFEST), None)
    if member is None:
        raise BackupError("manifest.json missing from archive") from None
    if not member.isfile() or member.size > 64 * 1024 * 1024:
        raise BackupError("manifest.json is not a regular file of sane size")
    raw = tar.extractfile(member).read()
    manifest = _unique_json(raw, "invalid_manifest")
    if not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int \
            or manifest["schema_version"] != SCHEMA_VERSION \
            or not isinstance(manifest.get("files"), dict):
        raise BackupError("invalid_manifest: unsupported shape")
    files = manifest["files"]
    if len(files) + 1 > MAX_MEMBERS:
        raise BackupError("member_limit_exceeded")
    for name, entry in files.items():
        if not _safe_name(name) or name == MANIFEST:
            raise BackupError("invalid_manifest: unsafe_member_name")
        if not isinstance(entry, dict) or not _valid_sha(entry.get("sha256")) or not _valid_size(entry.get("bytes")):
            raise BackupError(f"{name}: invalid_manifest entry")
        if name.startswith("private/objects/") and (not _valid_sha(name[16:]) or entry["sha256"] != name[16:]):
            raise BackupError(f"{name}: object_hash_mismatch")
        if any(parent.as_posix() in files for parent in Path(name).parents if parent != Path(".")):
            raise BackupError("invalid_manifest: conflicting_member_paths")
    if "campaign.json" not in files:
        raise BackupError("invalid_manifest: campaign.json missing")
    counts = manifest.get("counts")
    if not isinstance(counts, dict) or not {"files", "bytes", "objects", "receipts"} <= counts.keys() \
            or any(type(value) is not int or value < 0 for value in counts.values()):
        raise BackupError("invalid_manifest: invalid counts")
    if counts["files"] != len(files) or counts["bytes"] != sum(e["bytes"] for e in files.values()):
        raise BackupError("manifest.json: count_mismatch")
    source = manifest.get("source")
    if not isinstance(source, dict) or source.get("kind") not in ("local", "hosted"):
        raise BackupError("invalid_manifest: invalid source")
    if source["kind"] == "hosted" and "private/d1-export.json" not in files:
        raise BackupError("missing_metadata: hosted archive requires D1 export")
    manifest["manifest_sha256"] = hashlib.sha256(raw).hexdigest()
    return manifest


def _check_member(member, expected):
    if not _safe_name(member.name):
        return "unsafe_member_name"
    if member.islnk() or member.issym() or member.isdev() or member.isfifo():
        return "unsafe_member_type"
    if not member.isfile():
        return "unsafe_member_type"
    if member.size < 0 or member.size > MAX_MEMBER_BYTES:
        return "member_size_limit_exceeded"
    if member.name == MANIFEST:
        return None
    if member.name not in expected:
        return "not_in_manifest"
    if member.size != expected[member.name]["bytes"]:
        return "size_mismatch"
    return None


def verify(path, expected_manifest_sha256=None):
    """Re-hash every member against the manifest. Returns a report; raises on any problem.

    With ``expected_manifest_sha256`` (recorded out of band at export time) a
    rewritten manifest is detected too; without it this is an integrity check.
    """
    path = Path(path)
    problems = []
    with tarfile.open(path, mode="r:") as tar:
        members = _archive_members(tar)
        manifest = _read_manifest(tar, members)
        if expected_manifest_sha256 and manifest["manifest_sha256"] != expected_manifest_sha256.strip().lower():
            problems.append({"member": MANIFEST, "problem": "manifest_digest_mismatch"})
        expected = manifest["files"]
        seen = set()
        bad = set()
        for member in members:
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
        if not problems:
            with tempfile.TemporaryDirectory(prefix="deflock-verify-") as tmp:
                paths = {}
                for member in members:
                    if member.name in ("private/ledger.sqlite", "private/d1-export.json"):
                        target = Path(tmp) / Path(member.name).name
                        with tar.extractfile(member) as incoming, target.open("xb") as outgoing:
                            shutil.copyfileobj(incoming, outgoing)
                        paths[member.name] = target
                originals, counts = _metadata(paths)
                _closure(expected, originals, counts, manifest["counts"], has_metadata=bool(paths))
    report = {"path": str(path), "ok": not problems, "files": len(expected), "verified": len(seen - bad), "problems": problems,
        "manifest_sha256": manifest["manifest_sha256"],
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
            members = _archive_members(tar)
            manifest = _read_manifest(tar, members)
            if manifest["manifest_sha256"] != report["manifest_sha256"]:
                raise BackupError("manifest_digest_mismatch: archive changed before restore")
            for member in members:
                problem = _check_member(member, manifest["files"])
                if problem:
                    raise BackupError(f"{member.name}: {problem}")
            for member in members:
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
            if not target.is_file() or target.stat().st_size != entry["bytes"] or _sha256(target) != entry["sha256"]:
                mismatches.append(name)
            elif stat.S_IMODE(target.stat().st_mode) != 0o600:
                mismatches.append(name + " (mode)")
        if mismatches:
            raise BackupError("restored files failed re-verification: " + ", ".join(mismatches[:10]))
        paths = {name: root / name for name in manifest["files"]
                 if name in ("private/ledger.sqlite", "private/d1-export.json")}
        originals, counts = _metadata(paths)
        _closure(manifest["files"], originals, counts, manifest["counts"], has_metadata=bool(paths))
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
    if not isinstance(raw, bytes) or len(raw) > MAX_MEMBER_BYTES:
        raise BackupError("invalid_d1_export: export must be bounded bytes")
    export_doc = _unique_json(raw, "invalid_d1_export")
    originals, counts = _hosted_metadata(export_doc)
    rows = export_doc["rows"]
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
    if len(originals) + len(members) + 1 > MAX_MEMBERS:
        raise BackupError("member_limit_exceeded")
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    files = {}
    try:
        with os.fdopen(fd, "wb") as handle, tarfile.open(fileobj=handle, mode="w", format=tarfile.PAX_FORMAT) as tar:
            # Manifest is written last in memory but must be first in the archive for
            # streaming verification, so members are buffered to a spool file.
            with tempfile.TemporaryDirectory(prefix="deflock-hosted-") as tmp:
                staged = []
                for name, data in members:
                    if len(data) > MAX_MEMBER_BYTES:
                        raise BackupError(f"{name}: member_size_limit_exceeded")
                    files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                    staged.append((name, data))
                for sha, size in originals.items():
                    data = client.get(ORIGINAL_PATH.format(sha256=sha))
                    if hashlib.sha256(data).hexdigest() != sha:
                        raise BackupError(f"original {sha[:16]} did not match its hash when fetched; backup stopped")
                    if len(data) != size:
                        raise BackupError(f"original {sha}: object_size_mismatch")
                    spool = Path(tmp) / sha
                    spool.write_bytes(data)
                    files["private/objects/" + sha] = {"sha256": sha, "bytes": len(data)}
                    staged.append(("private/objects/" + sha, spool))
                _closure(files, originals, counts, has_metadata=True)
                manifest = _manifest(files, counts, {"kind": "hosted", "campaign_id": campaign.get("campaign_id"),
                                                     "exported_at": export_doc.get("exported_at")})
                manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n"
                manifest["manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
                _add_bytes(tar, MANIFEST, manifest_bytes)
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
        verify(out_path, expected_manifest_sha256=manifest["manifest_sha256"])
    except BaseException:
        out_path.unlink(missing_ok=True)
        raise
    return manifest
