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
from pathlib import Path
import re
import stat

SCHEMA = "records-analysis-ready-v1"
MAX_BYTES = 64 * 1024 * 1024
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
HASH = re.compile(r"[0-9a-f]{64}")
JURISDICTION = "us-ca"


def _read(path):
    """Bounded, no-follow regular-file read; (None, None) when absent."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
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


def write_run_analysis_ready(project, run_id):
    """Outputs-stage hook, called last. Raises OSError on any refusal; returns the report."""
    if not isinstance(run_id, str) or RUN_ID.fullmatch(run_id) is None:
        raise OSError("analysis_ready_run_id")
    outcomes = Path(os.path.abspath(project)) / "run-outputs" / run_id / "outcomes"
    run_dir = outcomes.parent
    try:
        inputs, loaded = {}, {}
        for name, base in (("received-items.json", outcomes), ("gap-proposals.json", outcomes),
                           ("text-state.json", run_dir)):
            data, digest = _read(base / name)
            if data is not None:
                if not isinstance(data, dict) or data.get("run_id") != run_id:
                    raise OSError("analysis_ready_input_stale")
                inputs[name], loaded[name] = digest, data
        states = {r["sha256"]: r["state"] for r in loaded.get("text-state.json", {}).get("records", [])
                  if isinstance(r, dict) and "sha256" in r and "state" in r}
        report = build(run_id, loaded.get("received-items.json"), loaded.get("gap-proposals.json"), states, inputs)
        encoded = (json.dumps(report, sort_keys=True, ensure_ascii=False) + "\n").encode()
        outcomes.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = outcomes / ".analysis-ready.json.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, outcomes / "analysis-ready.json")
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return report
    except (ValueError, KeyError, TypeError) as error:
        raise OSError("ANALYSIS_READY_UNAVAILABLE") from error
