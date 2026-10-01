"""Bind explicit agency/request context candidates to a private catalog snapshot.

No catalog mutation, model inference, verified attribution, or publication.
Inputs and outputs are private; the public repository contains only this engine.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

from .agency_reconciliation import canonical, checked, private_output, valid_hash

SCHEMA_VERSION = 1
AGENCY_ID = re.compile(r"agency:[a-z][a-z0-9-]{0,63}\Z")
REQUEST_ID = re.compile(r"request:[A-Za-z0-9][A-Za-z0-9._:-]{0,95}\Z")
MAX_INPUT_BYTES = 64 * 1024 * 1024


class LinkError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _strict_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise LinkError("duplicate_json_key")
        result[key] = value
    return result


def _decode(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(LinkError("invalid_json")))
    except LinkError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LinkError("invalid_json") from error


def _checked_input(path: Path) -> Path:
    try:
        return checked(path)
    except LinkError:
        raise
    except (OSError, ValueError) as error:
        raise LinkError("unsafe_or_missing_input") from error


def _read(path: Path) -> bytes:
    try:
        path = _checked_input(path)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_INPUT_BYTES:
                raise LinkError("invalid_input_file")
            data = source.read(MAX_INPUT_BYTES + 1)
            after = os.fstat(source.fileno())
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        if len(data) > MAX_INPUT_BYTES or identity(before) != identity(after):
            raise LinkError("source_changed_or_oversize")
        return data
    except LinkError:
        raise
    except (OSError, ValueError) as error:
        raise LinkError("unsafe_or_missing_input") from error


def _keys(value, expected: set[str], code: str):
    if type(value) is not dict or set(value) != expected:
        raise LinkError(code)


def _snapshot(snapshot: Path):
    try:
        snapshot = checked(snapshot, directory=True)
    except (OSError, ValueError) as error:
        raise LinkError("unsafe_or_missing_snapshot") from error
    catalog_bytes = _read(snapshot / "catalog.json")
    manifest_bytes = _read(snapshot / "input-manifest.json")
    artifacts_bytes = _read(snapshot / "artifact-hashes.json")
    catalog, manifest, artifacts = map(_decode, (catalog_bytes, manifest_bytes, artifacts_bytes))
    if (type(catalog) is not dict or type(manifest) is not dict or type(artifacts) is not dict
            or type(catalog.get("schema_version")) is not int or catalog["schema_version"] != 1
            or type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1
            or type(catalog.get("cards")) is not list):
        raise LinkError("invalid_catalog_schema")
    snapshot_id = _sha(canonical(manifest).encode("utf-8"))
    if catalog.get("snapshot_id") != snapshot_id:
        raise LinkError("stale_snapshot_binding")
    if (artifacts.get("catalog.json") != _sha(catalog_bytes)
            or artifacts.get("input-manifest.json") != _sha(manifest_bytes)):
        raise LinkError("stale_snapshot_artifact")
    cards = {}
    for card in catalog["cards"]:
        if (type(card) is not dict or type(card.get("sha256")) is not str
                or not valid_hash(card["sha256"]) or card["sha256"] in cards):
            raise LinkError("invalid_catalog_identity")
        cards[card["sha256"]] = card
    return snapshot, snapshot_id, cards, {
        "snapshot_id": snapshot_id,
        "catalog_sha256": _sha(catalog_bytes),
        "manifest_sha256": _sha(manifest_bytes),
        "artifact_inventory_sha256": _sha(artifacts_bytes),
    }


def _registry(raw: bytes, snapshot_id: str, catalog_sha: str, cards: dict):
    registry = _decode(raw)
    _keys(registry, {"schema_version", "snapshot_id", "catalog_sha256",
                     "agencies", "requests", "links"}, "invalid_registry_schema")
    if type(registry["schema_version"]) is not int or registry["schema_version"] != SCHEMA_VERSION:
        raise LinkError("invalid_registry_schema")
    if registry["snapshot_id"] != snapshot_id or registry["catalog_sha256"] != catalog_sha:
        raise LinkError("stale_registry_binding")
    if any(type(registry[key]) is not list for key in ("agencies", "requests", "links")):
        raise LinkError("invalid_registry_schema")
    agencies = set()
    for item in registry["agencies"]:
        _keys(item, {"agency_id"}, "invalid_agency_schema")
        agency_id = item["agency_id"]
        if type(agency_id) is not str or not AGENCY_ID.fullmatch(agency_id):
            raise LinkError("invalid_agency_id")
        if agency_id in agencies:
            raise LinkError("duplicate_agency_id")
        agencies.add(agency_id)
    requests = set()
    request_ids = set()
    for item in registry["requests"]:
        _keys(item, {"request_id", "agency_id"}, "invalid_request_schema")
        request_id, agency_id = item["request_id"], item["agency_id"]
        if type(request_id) is not str or not REQUEST_ID.fullmatch(request_id):
            raise LinkError("invalid_request_id")
        if type(agency_id) is not str:
            raise LinkError("invalid_agency_id")
        if agency_id not in agencies:
            raise LinkError("unknown_agency_id")
        key = (agency_id, request_id)
        if key in requests:
            raise LinkError("duplicate_request_id")
        requests.add(key)
        request_ids.add(request_id)
    links = []
    seen = set()
    source_hashes = set()
    for item in registry["links"]:
        _keys(item, {"source_sha256", "agency_id", "request_id"}, "invalid_link_schema")
        sha, agency_id, request_id = item["source_sha256"], item["agency_id"], item["request_id"]
        if type(sha) is not str or not valid_hash(sha):
            raise LinkError("invalid_source_sha")
        if sha not in cards:
            raise LinkError("unknown_source_sha")
        card = cards[sha]
        excluded_role = card.get("role") == "excluded_unrelated_personal"
        excluded_status = card.get("agency_status") == "scope_excluded"
        if excluded_role != excluded_status:
            raise LinkError("inconsistent_source_scope")
        if excluded_role:
            raise LinkError("source_scope_excluded")
        if type(agency_id) is not str:
            raise LinkError("invalid_agency_id")
        if agency_id not in agencies:
            raise LinkError("unknown_agency_id")
        if type(request_id) is not str or not REQUEST_ID.fullmatch(request_id):
            raise LinkError("invalid_request_id")
        if (agency_id, request_id) not in requests:
            raise LinkError("request_agency_mismatch" if request_id in request_ids
                            else "unknown_request_id")
        key = (sha, agency_id, request_id)
        if key in seen:
            raise LinkError("duplicate_link")
        seen.add(key)
        source_hashes.add(sha)
        links.append({"source_sha256": sha, "agency_id": agency_id,
                      "request_id": request_id, "status": "candidate_only",
                      "verified": False, "publication_ready": False})
    links.sort(key=lambda item: (item["source_sha256"], item["agency_id"], item["request_id"]))
    return {"schema_version": SCHEMA_VERSION, "candidates": links,
            "summary": {"candidate_links": len(links),
                        "source_hashes": len(source_hashes),
                        "registry_agencies": len(agencies),
                        "registry_requests": len(requests)},
            "limits": ["Explicit context candidates only; no verified attribution.",
                       "Catalog and historical review states are unchanged."]}


def _output_root(output: Path, snapshot: Path, registry: Path) -> Path:
    output = Path(os.path.abspath(output))
    if any(part.is_symlink() for part in (output, *output.parents)):
        raise LinkError("unsafe_output")
    if (output == snapshot or snapshot in output.parents or output in snapshot.parents
            or output == registry or output in registry.parents):
        raise LinkError("output_overlaps_input")
    try:
        for directory in reversed((output, *output.parents)):
            if directory.is_symlink():
                raise LinkError("unsafe_output")
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                if not directory.is_dir():
                    raise LinkError("unsafe_output")
            else:
                _owner_only(directory, directory=True)
        private_output(output, directory=True)
        _owner_only(output, directory=True)
    except LinkError:
        raise
    except (OSError, ValueError) as error:
        raise LinkError("unsafe_output") from error
    _sync_output_path(output)
    return output


def _owner_only(path: Path, *, directory: bool = False):
    info = path.stat(follow_symlinks=False)
    if (info.st_uid != os.geteuid() or info.st_mode & 0o077
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise LinkError("unsafe_output")


def _lock(output: Path):
    path = output / "writer.lock"
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        os.fchmod(fd, 0o600)
    except FileExistsError:
        try:
            fd = os.open(path, flags)
        except OSError as error:
            raise LinkError("unsafe_writer_lock") from error
    info = os.fstat(fd)
    try:
        saved = private_output(path).stat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
                or (info.st_dev, info.st_ino) != (saved.st_dev, saved.st_ino)):
            raise LinkError("unsafe_writer_lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except (OSError, ValueError) as error:
        os.close(fd)
        if isinstance(error, LinkError):
            raise
        raise LinkError("unsafe_writer_lock") from error

def _fsync_directory(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sync_directory(path: Path):
    try:
        _fsync_directory(path)
    except OSError as error:
        raise LinkError("output_sync_failed") from error


def _sync_output_path(output: Path):
    # Bottom-up syncing persists new directory entries, including partial setup
    # left by an interrupted prior attempt, before any result can be returned.
    for directory in (output, *output.parents):
        if directory.is_symlink():
            raise LinkError("unsafe_output")
        _sync_directory(directory)


def _write_new(path: Path, data: bytes):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as target:
        os.fchmod(target.fileno(), 0o600)
        target.write(data)
        target.flush()
        os.fsync(target.fileno())


def _reuse(destination: Path, expected: dict[str, bytes]):
    try:
        private_output(destination, directory=True)
        _owner_only(destination, directory=True)
        if {path.name for path in destination.iterdir()} != set(expected):
            raise LinkError("existing_output_mismatch")
        for name, data in expected.items():
            path = private_output(destination / name)
            _owner_only(path)
            if _read(path) != data:
                raise LinkError("existing_output_mismatch")
    except LinkError:
        raise
    except (OSError, ValueError) as error:
        raise LinkError("existing_output_mismatch") from error


def run(snapshot: str | Path, registry: str | Path, output: str | Path) -> dict:
    """Write immutable, candidate-only link output; return no private paths."""
    snapshot, snapshot_id, cards, bindings = _snapshot(Path(snapshot))
    registry_path = Path(registry)
    registry_bytes = _read(registry_path)
    registry_path = _checked_input(registry_path)
    result = _registry(registry_bytes, snapshot_id, bindings["catalog_sha256"], cards)
    result_bytes = (canonical(result) + "\n").encode("utf-8")
    bindings["registry_sha256"] = _sha(registry_bytes)
    bindings["implementation_sha256"] = _sha(Path(__file__).read_bytes())
    run_id = _sha(canonical({"input_bindings": bindings,
                             "result_sha256": _sha(result_bytes)}).encode("utf-8"))
    receipt = {"schema_version": SCHEMA_VERSION, "run_id": run_id,
               "input_bindings": bindings, "result_sha256": _sha(result_bytes),
               "summary": result["summary"], "verified_attributions": 0,
               "scope": "Candidate links only; no catalog mutation or publication approval."}
    expected = {"candidates.json": result_bytes,
                "receipt.json": (canonical(receipt) + "\n").encode("utf-8")}
    if any(len(data) > MAX_INPUT_BYTES for data in expected.values()):
        raise LinkError("output_too_large")
    output = _output_root(Path(output), snapshot, registry_path)
    fd = _lock(output)
    try:
        destination = output / run_id
        if destination.exists() or destination.is_symlink():
            _reuse(destination, expected)
            _sync_directory(destination)
            _sync_directory(output)
            reused = True
        else:
            stage = Path(tempfile.mkdtemp(prefix=".links-stage-", dir=output))
            try:
                for name, data in expected.items():
                    _write_new(stage / name, data)
                _sync_directory(stage)
                stage.rename(destination)
                _sync_directory(output)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
            reused = False
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return {"run_id": run_id, "reused": reused, "summary": result["summary"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = run(args.snapshot, args.registry, args.output)
    except LinkError as error:
        print(canonical({"status": "blocked", "code": error.code}))
        return 2
    except Exception:
        print(canonical({"status": "blocked", "code": "internal_error"}))
        return 2
    print(canonical(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
