"""Private per-run analysis-ready manifest (DFGL-05).

Reads only this run's outcome files (received-items.json, gap-proposals.json,
text-state.json) and writes run-outputs/<run>/outcomes/analysis-ready.json: per
agency and request with at least one received item of kind "records", the record
sha256s, text state, comparison finding ids and ratings, gap-proposal ids and the
law package id/status. Ids, hashes and fixed words only: no text, quotes or names.
Nothing reads it unattended yet; it starts nothing. Review-required.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat

SCHEMA = "records-analysis-ready-v1"
MAX_BYTES = 64 * 1024 * 1024
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
HASH = re.compile(r"[0-9a-f]{64}")
JURISDICTION = "us-ca"


def _open_dir(parent, name, create=False):
    """Descriptor for a child directory, never through a symlink; None when absent and not created."""
    for attempt in (0, 1):
        try:
            return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        except FileNotFoundError:
            if not create or attempt:
                return None
            os.mkdir(name, 0o700, dir_fd=parent)
        except OSError:
            raise OSError("analysis_ready_unsafe_path") from None


def _chain(project, run_id, create):
    """Descriptors for run-outputs, run-outputs/<run> and its outcomes directory, in that order.

    Each step is opened relative to its parent with O_NOFOLLOW, so a symlinked
    run-outputs, run directory or outcomes directory is refused and nothing is read or
    written through it. A missing step ends the chain early (reads) or is created (writes)."""
    fds = [os.open(os.path.abspath(project), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)]
    try:
        for name in ("run-outputs", run_id, "outcomes"):
            child = _open_dir(fds[-1], name, create)
            if child is None:
                break
            fds.append(child)
        return fds[1:], fds[0]
    except BaseException:
        for fd in fds:
            os.close(fd)
        raise


def _read(directory_fd, name):
    """Bounded, no-follow regular-file read; (None, None) when absent."""
    if directory_fd is None:
        return None, None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory_fd)
    except FileNotFoundError:
        return None, None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError("analysis_ready_not_regular")
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise OSError("analysis_ready_input_too_large")
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def _law_package():
    from ...law import load_package
    package = load_package(JURISDICTION)
    return {"id": package.get("jurisdiction", JURISDICTION), "status": package.get("status") or "draft",
            "revision": package.get("revision")}


def build(run_id, received, proposals, states, inputs):
    entries = {}
    for item in (received or {}).get("items", []):
        digest = item.get("original_sha256")
        if item.get("kind") != "records" or item.get("agency_id") is None or not (
                isinstance(digest, str) and HASH.fullmatch(digest)):
            continue
        entry = entries.setdefault((item["agency_id"], item.get("request_id")), {
            "agency_id": item["agency_id"], "request_id": item.get("request_id"), "records": [],
            "comparison_rows": [], "gap_proposal_ids": []})
        entry["records"].append({"sha256": digest, "text_state": states.get(digest, "unknown")})
        entry["comparison_rows"] += [{"finding_id": f.get("finding_id"), "rating": f.get("rating")}
                                     for f in item.get("findings", []) if isinstance(f, dict)]
    for agency in (proposals or {}).get("agencies", []):
        for request in agency.get("requests", []):
            entry = entries.get((agency.get("agency_id"), request.get("request_id")))
            if entry is not None:
                entry["gap_proposal_ids"] += [p["proposal_id"] for p in request.get("proposals", [])]
    law = _law_package() if entries else None
    for entry in entries.values():
        entry["records"].sort(key=lambda r: r["sha256"])
        entry["comparison_rows"].sort(key=lambda r: (str(r["finding_id"]), str(r["rating"])))
        entry["gap_proposal_ids"] = sorted(set(entry["gap_proposal_ids"]))
        entry["law_package"] = law
    ordered = sorted(entries.values(), key=lambda e: (e["agency_id"], e["request_id"] or ""))
    return {"schema": SCHEMA, "run_id": run_id, "review_required": True, "agencies": ordered, "inputs": inputs}


def _load_inputs(project, run_id):
    fds, root = _chain(project, run_id, False)
    try:
        run_fd = fds[1] if len(fds) > 1 else None
        out_fd = fds[2] if len(fds) > 2 else None
        inputs, loaded = {}, {}
        for name, base in (("received-items.json", out_fd), ("gap-proposals.json", out_fd), ("text-state.json", run_fd)):
            data, digest = _read(base, name)
            if data is not None:
                if not isinstance(data, dict) or data.get("run_id") != run_id:
                    raise OSError("analysis_ready_input_stale")
                inputs[name], loaded[name] = digest, data
        return inputs, loaded
    finally:
        for fd in (*fds, root):
            os.close(fd)


def write_run_analysis_ready(project, run_id):
    """Outputs-stage hook, called last. Raises OSError on any refusal; returns the report."""
    if not isinstance(run_id, str) or RUN_ID.fullmatch(run_id) is None:
        raise OSError("analysis_ready_run_id")
    try:
        inputs, loaded = _load_inputs(project, run_id)
        states = {r["sha256"]: r["state"] for r in loaded.get("text-state.json", {}).get("records", [])
                  if isinstance(r, dict) and "sha256" in r and "state" in r}
        report = build(run_id, loaded.get("received-items.json"), loaded.get("gap-proposals.json"), states, inputs)
        encoded = (json.dumps(report, sort_keys=True, ensure_ascii=False) + "\n").encode()
        fds, root = _chain(project, run_id, True)
        try:
            # Fresh 0600 temp (a leftover one is removed, never reused), fsync, atomic rename.
            temporary = ".analysis-ready.json.tmp"
            try:
                os.unlink(temporary, dir_fd=fds[2])
            except FileNotFoundError:
                pass
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                         dir_fd=fds[2])
            try:
                with os.fdopen(fd, "wb") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, "analysis-ready.json", src_dir_fd=fds[2], dst_dir_fd=fds[2])
            finally:
                try:
                    os.unlink(temporary, dir_fd=fds[2])
                except FileNotFoundError:
                    pass
        finally:
            for fd in (*fds, root):
                os.close(fd)
        return report
    except (ValueError, KeyError, TypeError) as error:
        raise OSError("ANALYSIS_READY_UNAVAILABLE") from error
