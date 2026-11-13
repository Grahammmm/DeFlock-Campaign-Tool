"""Offline, read-only bridge from written public packets to a host export.

Only journal-hash-bound current written changes cross this boundary. No record
objects, journal, signatures or configuration are copied; exports remain private.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from . import public_packets as pp

MANIFEST = "changes-manifest.json"
MANIFEST_SCHEMA = "records-site-change-export-v1"
MAX_CHANGE = 128 * 1024
FIELDS = {"schema", "packet", "revision", "signed_by", "approved_at", "kind", "agency_key", "request_id",
          "title", "text", "documents", "citations", "target", "note"}


def directory(path, *, create=False):
    """Reject symlink components, including parents, before creating a directory."""
    path = Path(os.path.abspath(path))
    for part in [*reversed(path.parents), path]:
        try:
            mode = part.lstat().st_mode
        except FileNotFoundError:
            pp._need(create, "directory_missing")
            part.mkdir(mode=0o700)
            continue
        pp._need(stat.S_ISDIR(mode) and not stat.S_ISLNK(mode), "directory_invalid")
    return path


def public_change(change):
    """Validate the producer contract; return only public content, never signer metadata.

    This structural check is NOT authentication, redaction review or publication
    approval. The export manifest is meaningful only from the trusted host exporter.
    """
    pp._need(isinstance(change, dict) and FIELDS <= change.keys()
             and change.keys() <= FIELDS | {"campaign"}, "change_invalid")
    ident, rev = change["packet"], change["revision"]
    pp._need(change["schema"] == pp.CHANGE_SCHEMA and isinstance(ident, str)
             and pp.PACKET_ID.fullmatch(ident) and type(rev) is int and 0 <= rev <= 1_000_000, "change_invalid")
    stamp = change["approved_at"]
    try:
        from datetime import datetime
        pp._need(isinstance(stamp, str) and datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").strftime(
            "%Y-%m-%dT%H:%M:%SZ") == stamp, "change_invalid")
    except (ValueError, TypeError):
        raise pp.PacketError("change_invalid") from None
    pp._need(isinstance(change["signed_by"], str) and 0 < len(change["signed_by"]) <= 256, "change_invalid")
    packet = pp.clean({**change, "source": "exported-change", "finding_state": pp.PUBLIC_STATE})
    for field in ("documents", "citations"):
        pp._need(packet[field] == change[field], "change_invalid")
    pp._need(change["target"] == f"content/updates/{packet['agency_key']}/{stamp[:10]}-{ident}.json", "change_invalid")
    # No signer, campaign config, arbitrary note, input target, private provenance or
    # record bytes enter the site. A hash is a locator, never a download clearance.
    return {"schema": "records-public-update-v1", "packet": ident, "revision": rev, "approved_at": stamp,
            **{key: packet[key] for key in ("kind", "agency_key", "request_id", "title", "text", "documents", "citations")}}


def export_changes(project, out):
    project = directory(project)
    out_path = Path(os.path.abspath(out))
    pp._need(not out_path.is_relative_to(project) and not project.is_relative_to(out_path), "export_boundary")
    for parent in [out_path, *out_path.parents]:
        pp._need(not (parent / ".git").exists(), "export_inside_git")
    prepared = []

    def collect(lock):
        pp._need(not os.path.lexists(project / pp.KILL), "killed")
        source = project / pp.OUT
        for row in pp.fold(lock.events()).values():
            if row["status"] != "written":
                continue
            pp._need(isinstance(row["id"], str) and pp.PACKET_ID.fullmatch(row["id"])
                     and type(row["revision"]) is int and 0 <= row["revision"] <= 1_000_000, "change_invalid")
            directory(source)
            name = f"{row['id']}-r{row['revision']}.json"
            pp._need(row["file"] == f"{pp.OUT}/{name}", "change_invalid")
            raw = pp._read(source / name, MAX_CHANGE)
            pp._need(raw is not None and hashlib.sha256(raw).hexdigest() == row["file_sha256"], "change_hash_mismatch")
            change = pp._json(raw, "change_invalid")
            public_change(change)
            pp._need((change["packet"], change["revision"]) == (row["id"], row["revision"]), "change_invalid")
            prepared.append((name, raw))

    pp._reading(str(project), True, collect, lambda: None)
    out_path = directory(out_path, create=True)
    pp._need(not out_path.stat().st_mode & 0o077, "export_not_private")
    # Preflight collisions before writing anything. No overwrite on same id+rev.
    new = []
    for name, raw in sorted(prepared):
        existing = pp._read(out_path / name, MAX_CHANGE)
        pp._need(existing is None or existing == raw, "export_conflict")
        if existing is None:
            new.append((name, raw))
    for name, raw in new:
        fd = os.open(out_path / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    manifest = {"schema": MANIFEST_SCHEMA, "changes": [
        {"file": name, "sha256": hashlib.sha256(raw).hexdigest()} for name, raw in sorted(prepared)]}
    # An active manifest excludes old exports after supersession/cancellation. An
    # interrupted export is resumable; the previous complete manifest stays valid.
    pp._replace(str(out_path / MANIFEST), (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode())
    return {"exported": len(new), "existing": len(prepared) - len(new), "active": len(prepared)}


def read_export(root):
    root = directory(root)
    raw = pp._read(root / MANIFEST, MAX_CHANGE)
    manifest = pp._json(raw or b"", "manifest_invalid")
    pp._need(isinstance(manifest, dict) and set(manifest) == {"schema", "changes"}
             and manifest["schema"] == MANIFEST_SCHEMA and isinstance(manifest["changes"], list), "manifest_invalid")
    changes, seen = [], set()
    for item in manifest["changes"]:
        pp._need(isinstance(item, dict) and set(item) == {"file", "sha256"}, "manifest_invalid")
        name, digest = item["file"], item["sha256"]
        pp._need(isinstance(name, str) and re.fullmatch(r"[0-9a-f]{16}-r[0-9]+\.json", name)
                 and isinstance(digest, str) and pp.SHA.fullmatch(digest), "manifest_invalid")
        raw = pp._read(root / name, MAX_CHANGE)
        pp._need(raw is not None and hashlib.sha256(raw).hexdigest() == digest, "change_hash_mismatch")
        change = public_change(pp._json(raw, "change_invalid"))
        pp._need(name == f"{change['packet']}-r{change['revision']}.json" and change["packet"] not in seen, "manifest_invalid")
        seen.add(change["packet"])
        changes.append(change)
    return changes
