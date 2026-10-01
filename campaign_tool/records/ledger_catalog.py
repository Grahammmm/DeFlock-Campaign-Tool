"""Bounded read-only canonical-ledger catalog and owner-private board exports.

WP1 and PR19 are runtime dependencies, not copied implementations. No inference,
review promotion, low-value closure, server, network, or publication is performed.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
import fcntl
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time

MAX_ORIGINALS = 10000
MAX_ROWS = 100000
MAX_BYTES = 64 * 1024 * 1024
MAX_DATABASE_BYTES = 8 * 1024 * 1024 * 1024
CAPTURE_SECONDS = 30
SCHEMA_VERSION = 2
MAX_FILTER_PAGES = 200
STAGES = ("preserve", "extract", "catalog", "detect", "review", "compare", "privacy")
TYPES = {"policy", "search-log", "detection-log", "audit", "sharing-list",
         "retention-setting", "training-roster", "contract", "invoice",
         "correspondence", "denial-or-extension", "transmittal", "other"}
HASH = re.compile(r"[a-f0-9]{64}\Z")
# WP1 writes "out_of_scope"; "excluded" is the legacy label. Both must exclude.
EXCLUDED_SCOPES = frozenset({"excluded", "out_of_scope"})
EXCLUDED_ROLE = "excluded_unrelated_personal"


def scope_excluded(row):
    """True when an original must never be shown or used as agency evidence."""
    return row["role"] == EXCLUDED_ROLE or row["scope"] in EXCLUDED_SCOPES


class CatalogError(ValueError):
    pass


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, allow_nan=False) + "\n").encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def _require(condition, code):
    if not condition:
        raise CatalogError(code)


def _safe_path(value, *, directory=False):
    path = Path(value)
    _require(path.is_absolute() and ".." not in path.parts, "noncanonical_path")
    _require(not any(p.is_symlink() for p in (path, *path.parents)), "symlink_path")
    info = path.stat()
    _require(info.st_uid == os.geteuid() and not info.st_mode & 0o077,
             "owner_only_path_required")
    _require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode),
             "wrong_path_type")
    return path


def _outside_repo(path):
    for parent in (path, *path.parents):
        _require(not (parent / ".git").exists(), "private_output_inside_repository")


def _rows(con, table, columns, *, limit=MAX_ROWS):
    # Table and columns are fixed code constants, never record data.
    rows = con.execute(f"SELECT {columns} FROM {table} ORDER BY 1 LIMIT ?", (limit + 1,)).fetchall()
    _require(len(rows) <= limit, "metadata_row_limit")
    return [dict(row) for row in rows]


def _json(value, code):
    try:
        result = json.loads(value)
    except (TypeError, ValueError) as error:
        raise CatalogError(code) from error
    return result


def validate_overlay(overlay, original_hashes):
    """Model/operator drafts remain candidate metadata, never accepted facts."""
    _require(type(overlay) is dict and len(overlay) <= MAX_ORIGINALS, "invalid_overlay")
    allowed = {"title", "doc_type", "date_from", "date_to", "summary", "parties",
               "value_score", "value_reason", "evidence", "low_value_reason"}
    result = {}
    for sha, draft in overlay.items():
        _require(sha in original_hashes and type(draft) is dict and not set(draft) - allowed,
                 "unknown_original_or_overlay_field")
        for field in ("title", "summary", "value_reason", "low_value_reason"):
            value = draft.get(field)
            _require(value is None or type(value) is str and len(value) <= 4000,
                     "invalid_metadata_text")
        _require(draft.get("doc_type") is None or draft["doc_type"] in TYPES, "invalid_doc_type")
        for field in ("date_from", "date_to"):
            value = draft.get(field)
            if value is not None:
                try:
                    _require(type(value) is str and date.fromisoformat(value).isoformat() == value,
                             "invalid_date")
                except (ValueError, TypeError) as error:
                    raise CatalogError("invalid_date") from error
        _require(not (draft.get("date_from") and draft.get("date_to")) or
                 draft["date_from"] <= draft["date_to"], "reversed_date_range")
        score = draft.get("value_score")
        _require(score is None or type(score) is int and 0 <= score <= 100, "invalid_value_score")
        _require(score is None or bool(draft.get("value_reason")), "score_reason_required")
        parties = draft.get("parties", [])
        _require(type(parties) is list and len(parties) <= 100 and
                 all(type(v) is str and len(v) <= 256 for v in parties), "invalid_parties")
        evidence = draft.get("evidence", [])
        _require(type(evidence) is list and len(evidence) <= 100, "invalid_evidence")
        for item in evidence:
            _require(type(item) is dict and set(item) == {"source_sha256", "locator"} and
                     item["source_sha256"] in original_hashes and
                     type(item["locator"]) is str and 0 < len(item["locator"]) <= 500,
                     "invalid_evidence")
        _require(not any(draft.get(k) is not None for k in
                         ("doc_type", "date_from", "date_to", "summary", "title")) or evidence,
                 "draft_evidence_required")
        result[sha] = dict(draft)
    _require(len(encoded(result)) <= MAX_BYTES, "overlay_too_large")
    return result


def project(con, counts, *, overlays=None, since=None, run_id=None, link_validator=None):
    """Project an immutable read snapshot; counts must be WP1 query_counts output.

    The injected link validator is PR19 _registry; injection is for dependency
    composition/testing only and never supplies authority to promote a stage.
    """
    if link_validator is None:
        from .catalog_links import _registry as link_validator
    run_binding = None
    if run_id is not None:
        _require(type(run_id) is str and 0 < len(run_id) <= 200, "invalid_run_id")
        run = con.execute("SELECT run_id,started_at,ended_at,engine_version,image_digest,config_sha256,coverage_cutoff,status FROM runs WHERE run_id=?", (run_id,)).fetchone()
        _require(run is not None, "unknown_run_id")
        run_binding = dict(run)
    originals = _rows(con, "originals", "sha256,bytes,mime_detected,first_seen_at,role,scope", limit=MAX_ORIGINALS)
    hashes = {row["sha256"] for row in originals}
    _require(all(type(v) is str and HASH.fullmatch(v) for v in hashes), "invalid_original_hash")
    _require(counts.get("originals") == len(originals), "count_denominator_mismatch")
    overlays = validate_overlay(overlays or {}, hashes)
    agencies = _rows(con, "agencies", "id,name,jurisdiction")
    requests = _rows(con, "requests", "id,agency_id,external_ref,submitted_at,determination_due_at,determination_at,production_at,status")
    joins = _rows(con, "joins", "id,original_sha256,agency_id,request_id,join_type,evidence,status,blocked_reason")
    states = _rows(con, "stage_state", "original_sha256,stage,status,owner,reason,receipt_sha256,updated_at", limit=MAX_ORIGINALS * 7)
    occurrences = _rows(con, "occurrences", "id,original_sha256,kind,parent_occurrence_id,acquired_at")
    prior = _rows(con, "digests", "id,original_sha256,author_id,coverage_declared,sha256,reference_status")
    proposals = _rows(con, "proposals", "id,tier,agency_id,owner_approval,public_content_sha256")
    receipts = _rows(con, "receipts", "sha256,subject_sha256,reviewer_id,role,verdict")
    _require(all(r["original_sha256"] in hashes for r in occurrences), "dangling_occurrence")
    occurrence_ids = {r["id"] for r in occurrences}
    _require(all(not r["parent_occurrence_id"] or r["parent_occurrence_id"] in occurrence_ids
                 for r in occurrences), "dangling_occurrence_parent")
    _require(all(r["reviewer_id"] and str(r["reviewer_id"]).strip() and
                 r["subject_sha256"] in hashes for r in receipts), "invalid_receipt_identity")
    state_map = {sha: {} for sha in hashes}
    tally = {stage: dict.fromkeys(("done", "in_progress", "pending", "blocked", "inapplicable"), 0) for stage in STAGES}
    for row in states:
        sha, stage = row["original_sha256"], row["stage"]
        _require(sha in hashes and stage in STAGES and stage not in state_map[sha] and
                 row["status"] in tally[stage], "invalid_stage_slot")
        state_map[sha][stage] = row
        tally[stage][row["status"]] += 1
    _require(all(set(value) == set(STAGES) for value in state_map.values()), "missing_stage_slots")
    _require(tally == counts.get("stages") and len(states) == counts.get("stage_slots_observed")
             and len(states) == counts.get("stage_slots_expected"), "stage_count_mismatch")
    agency_map = {row["id"]: row for row in agencies}
    request_map = {row["id"]: row for row in requests}
    _require(all(row["agency_id"] in agency_map for row in requests), "dangling_request_agency")
    by_join, by_occurrence, by_prior = ({sha: [] for sha in hashes} for _ in range(3))
    registry_links = []
    registry_keys = set()
    excluded = {r["sha256"] for r in originals if scope_excluded(r)}
    for row in joins:
        sha = row["original_sha256"]
        _require(sha in hashes, "dangling_join")
        item = dict(row)
        if row["status"] == "typed":
            _require(sha not in excluded, "source_scope_excluded")
            ev = _json(row["evidence"], "typed_join_evidence_required")
            _require(type(ev) is dict and ev.get("source_sha256") in hashes and
                     type(ev.get("locator")) is str and 0 < len(ev["locator"]) <= 500,
                     "typed_join_evidence_required")
            _require(row["agency_id"] in agency_map, "unknown_typed_agency")
            key = (sha, row["agency_id"], row["request_id"])
            if row["request_id"] and key not in registry_keys:
                registry_keys.add(key)
                registry_links.append({"source_sha256": sha, "agency_id": row["agency_id"],
                                       "request_id": row["request_id"]})
        else:
            _require(row["status"] in ("hinted", "blocked"), "invalid_join_status")
        # Do not export opaque evidence text: expose only explicit validated locators.
        item["evidence"] = {k: ev[k] for k in ("source_sha256", "locator")} if row["status"] == "typed" else None
        by_join[sha].append(item)
    original_map = {r["sha256"]: r for r in originals}
    link_cards = {sha: {"role": original_map[sha]["role"], "agency_status":
                        "scope_excluded" if sha in excluded else "unknown"}
                  for sha in hashes}
    binding = digest(encoded(sorted(hashes)))
    registry = {"schema_version": 1, "snapshot_id": binding, "catalog_sha256": binding,
                "agencies": [{"agency_id": a["id"]} for a in agencies],
                "requests": [{"request_id": r["id"], "agency_id": r["agency_id"]} for r in requests],
                "links": registry_links}
    validated = link_validator(encoded(registry), binding, binding, link_cards)
    for row in occurrences:
        by_occurrence[row["original_sha256"]].append(row)
    for row in prior:
        if row["original_sha256"] in hashes:
            by_prior[row["original_sha256"]].append(row)
    cards = []
    for original in originals:
        sha = original["sha256"]
        draft = overlays.get(sha, {})
        linked = by_join[sha]
        agencies_for_card = sorted({j["agency_id"] for j in linked if j["status"] == "typed"})
        blocked = [s for s in state_map[sha].values() if s["status"] == "blocked"]
        card = {**original, "agency_ids": agencies_for_card, "agency_status": "typed" if agencies_for_card else "unknown",
                "request_ids": sorted({j["request_id"] for j in linked if j["status"] == "typed" and j["request_id"]}),
                "joins": linked, "stages": state_map[sha], "occurrences": by_occurrence[sha],
                "prior_review": by_prior[sha], "metadata": draft,
                "doc_type": draft.get("doc_type"), "date_from": draft.get("date_from"),
                "date_to": draft.get("date_to"), "value_score": draft.get("value_score"),
                "metadata_status": "candidate" if draft else "unknown",
                "unknown_reasons": {field: "No supported catalog value supplied" for field in
                                    ("doc_type", "date_from", "date_to", "value_score") if draft.get(field) is None},
                "low_value_status": "proposed low-value" if draft.get("low_value_reason") else None,
                "status": "blocked" if blocked else state_map[sha]["catalog"]["status"],
                "publication_ready": False}
        cards.append(card)
    arrivals = None
    if since is not None:
        try:
            cutoff = datetime.fromisoformat(since)
            _require(cutoff.tzinfo is not None, "timezone_required")
            arrivals = []
            for card in cards:
                if card["first_seen_at"]:
                    timestamp = datetime.fromisoformat(card["first_seen_at"])
                    _require(timestamp.tzinfo is not None, "timezone_required")
                    if timestamp > cutoff:
                        arrivals.append(card["sha256"])
        except (ValueError, TypeError) as error:
            raise CatalogError("invalid_arrival_timestamp") from error
    metrics = {
        "originals": len(cards),
        "unknown_agency": sum(not c["agency_ids"] for c in cards),
        "unknown_document_type": sum(c["doc_type"] is None for c in cards),
        "unknown_date_range": sum(c["date_from"] is None or c["date_to"] is None for c in cards),
        "proposed_low_value": sum(c["low_value_status"] is not None for c in cards),
        "catalog_stage_done": counts["stages"]["catalog"]["done"],
        "review_stage_done": counts["stages"]["review"]["done"],
        "blocked_originals": sum(c["status"] == "blocked" for c in cards),
        "open_requests": sum(r["status"] != "closed" for r in requests),
        "arrival_window_originals": None if arrivals is None else len(arrivals),
        "weekly_throughput": None,
        "weekly_throughput_reason": "No historical accepted-run interval selected",
    }
    result = {"schema_version": SCHEMA_VERSION, "run_binding": run_binding, "metrics": metrics, "kind": "owner-private-candidate-catalog", "counts": counts,
              "cards": cards, "agencies": agencies, "requests": requests, "proposals": proposals,
              "arrivals": {"since": since, "originals": arrivals, "coverage": "ledger_only"},
              "link_validation": validated["summary"], "publication_ready": False,
              "limits": ["No document or review completion inferred from catalog presence.",
                         "Metadata overlays are candidates; prior labels retain their original meaning.",
                         "No low-value closure is performed by this read-only exporter.",
                         "Owner-private file permissions are not deployed Access verification."]}
    _require(len(encoded(result)) <= MAX_BYTES, "catalog_too_large")
    result["snapshot_id"] = digest(encoded(result))
    return result


@contextmanager
def _capture(database, private_scratch):
    database = _safe_path(database)
    root = _safe_path(private_scratch, directory=True)
    _outside_repo(root)
    with tempfile.TemporaryDirectory(prefix=".catalog-read-", dir=root) as temporary:
        target = Path(temporary) / "ledger.sqlite"
        # A SQLite backup captures WAL state consistently without writing source.
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5) as source:
            source.execute("PRAGMA query_only=ON")
            size = source.execute("PRAGMA page_count").fetchone()[0] * source.execute("PRAGMA page_size").fetchone()[0]
            _require(size <= MAX_DATABASE_BYTES, "database_capture_size_limit")
            deadline = time.monotonic() + CAPTURE_SECONDS
            def progress(status, remaining, total):
                _require(time.monotonic() <= deadline, "database_capture_timeout")
            with sqlite3.connect(target) as dest:
                source.backup(dest, pages=256, sleep=0.01, progress=progress)
        target.chmod(0o600)
        yield target


def build(database, private_scratch, *, overlays=None, since=None, run_id=None):
    from .ledger.stages import query_counts
    with _capture(database, private_scratch) as captured:
        # WP1 may install/check its own authority tables on this disposable copy.
        counts = query_counts(captured)
        with sqlite3.connect(captured) as con:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA query_only=ON")
            return project(con, counts, overlays=overlays, since=since, run_id=run_id)


def render_board(catalog, *, selection=None):
    """No scripts, raw record HTML, remote assets, or source/portal URLs."""
    esc = lambda value: html.escape(str(value), quote=True)
    filters = filter_pages(catalog)
    cards = catalog["cards"]
    if selection is not None:
        kind, value = selection
        cards = [card for card in cards if _matches(card, kind, value)]
    sections = ["<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">",
                "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
                "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src &apos;none&apos;; base-uri &apos;none&apos;; form-action &apos;none&apos;\">",
                "<title>Private records catalog</title><h1>Owner-private candidate catalog</h1>",
                "<p>No publication or review approval is implied.</p><h2>Canonical counts</h2><pre id=\"counts\">",
                esc(encoded(catalog["counts"]).decode()), "</pre><h2>Open requests</h2><ul>"]
    for request in catalog["requests"]:
        if request["status"] != "closed":
            sections.append("<li>" + esc(request["id"]) + " | determination due: " +
                            esc(request["determination_due_at"] or "unknown") + " | " + esc(request["status"]) + "</li>")
    sections.append("</ul><h2>Blocked by owner</h2><ul>")
    blocked = sorted((s["owner"], c["sha256"], s["stage"], s["reason"] or "unknown")
                     for c in catalog["cards"] for s in c["stages"].values() if s["status"] == "blocked")
    for owner, sha, stage, reason in blocked:
        sections.append("<li>" + esc(owner) + " | " + esc(sha) + " | " + esc(stage) + " | " + esc(reason) + "</li>")
    sections.append("</ul><h2>Proposals by owner state</h2><ul>")
    for proposal in sorted(catalog["proposals"], key=lambda p: (p["owner_approval"], p["id"])):
        sections.append("<li>" + esc(proposal["id"]) + " | " + esc(proposal["tier"]) + " | " + esc(proposal["owner_approval"]) + "</li>")
    sections.append("</ul><h2>New ledger arrivals</h2><pre>" + esc(encoded(catalog["arrivals"]).decode()) + "</pre><h2>Catalog</h2>")
    sections.append("<nav aria-label=\"Catalog filters\"><a href=\"index.html\">All originals</a> | ")
    for name, (kind, value) in filters.items():
        label = kind + ": " + ("unknown" if value is None else value)
        sections.append("<a href=\"" + name + "\">" + esc(label) + "</a> | ")
    sections.append("</nav><p>Visible cards: " + str(len(cards)) + ". Canonical counts above remain unfiltered.</p>")
    sections.append("<h2>Snapshot metrics</h2><pre>" + esc(encoded(catalog["metrics"]).decode()) + "</pre>")
    for card in cards:
        sections.append("<details id=\"doc-" + esc(card["sha256"]) + "\"><summary>" +
                        esc(card["metadata"].get("title") or card["sha256"]) + " | " + esc(card["status"]) + "</summary><pre>" +
                        esc(encoded(card).decode()) + "</pre></details>")
    sections.append("<h2>Limits</h2><pre>" + esc(encoded(catalog["limits"]).decode()) + "</pre></html>")
    return "".join(sections).encode()


def _matches(card, kind, value):
    if kind == "agency":
        return not card["agency_ids"] if value is None else value in card["agency_ids"]
    if kind == "type":
        return card["doc_type"] == value
    if kind == "year":
        return (card["date_from"][:4] if card["date_from"] else None) == value
    return False


def filter_pages(catalog):
    choices = set()
    for card in catalog["cards"]:
        choices.update(("agency", agency) for agency in (card["agency_ids"] or [None]))
        choices.add(("type", card["doc_type"]))
        choices.add(("year", card["date_from"][:4] if card["date_from"] else None))
    _require(len(choices) <= MAX_FILTER_PAGES, "filter_page_limit")
    return {"filter-" + digest(encoded([kind, value])) + ".html": (kind, value)
            for kind, value in sorted(choices, key=lambda pair: (pair[0], pair[1] or ""))}


def artifact_payloads(catalog):
    payloads = {"catalog.json": encoded(catalog), "counts.json": encoded(catalog["counts"]),
                "index.html": render_board(catalog)}
    total = sum(len(raw) for raw in payloads.values())
    _require(total <= MAX_BYTES, "export_total_byte_limit")
    for name, selection in filter_pages(catalog).items():
        rendered = render_board(catalog, selection=selection)
        total += len(rendered)
        _require(total <= MAX_BYTES, "export_total_byte_limit")
        payloads[name] = rendered
    payloads["artifact-hashes.json"] = encoded({name: digest(raw) for name, raw in payloads.items()})
    return payloads


def dependency_contract():
    """Verify the import surface in a composed package, never host path discovery."""
    from .ledger import stages
    from . import catalog_links
    _require(callable(getattr(stages, "query_counts", None)), "wp1_counts_dependency_missing")
    _require(catalog_links.SCHEMA_VERSION == 1 and callable(getattr(catalog_links, "_registry", None)),
             "pr19_dependency_incompatible")
    return {"wp5_schema_version": SCHEMA_VERSION, "wp1_counts_api": "query_counts",
            "pr19_schema_version": catalog_links.SCHEMA_VERSION,
            "pr19_source_sha256": digest(Path(catalog_links.__file__).read_bytes()),
            "source_copied_into_wp5": False}


def _write_new(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def export(catalog, private_output):
    """Immutable candidate export; never changes an accepted snapshot pointer."""
    root = _safe_path(private_output, directory=True)
    _outside_repo(root)
    expected_id = digest(encoded({k: v for k, v in catalog.items() if k != "snapshot_id"}))
    _require(catalog.get("snapshot_id") == expected_id, "stale_snapshot_binding")
    payloads = artifact_payloads(catalog)
    lock_path = root / "catalog-export.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077,
                 "unsafe_export_lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        destination = root / expected_id
        reused = destination.exists() or destination.is_symlink()
        if reused:
            _safe_path(destination, directory=True)
            _require({p.name for p in destination.iterdir()} == set(payloads), "existing_export_mismatch")
            for name, expected in payloads.items():
                path = _safe_path(destination / name)
                _require(path.stat().st_size == len(expected) and path.read_bytes() == expected,
                         "existing_export_mismatch")
        else:
            with tempfile.TemporaryDirectory(prefix=".board-export-", dir=root) as temporary:
                stage = Path(temporary)
                for name, raw in payloads.items():
                    _write_new(stage / name, raw)
                directory = os.open(stage, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                stage.rename(destination)
                # TemporaryDirectory only removes its now-absent staging path.
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.close(fd)
    return {"snapshot_id": expected_id, "reused": reused, "cards": len(catalog["cards"]),
            "accepted": False, "publication_ready": False}
