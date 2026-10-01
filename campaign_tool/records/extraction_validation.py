"""Installed, fail-closed functional extraction acceptance; never a review gate.

Only trusted startup calls install_extraction_validator. No CLI, submitted callback,
receipt-selected filesystem reference, or profile override is supported here.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import tempfile
import hashlib
import importlib
import importlib.metadata
import os
import stat
import sys

from . import extraction_ledger as enrollment
from . import extraction_routes as routes
from .intake import folder

ADAPTER_ID = "records-extract-complete-v1"
ENVELOPE_SCHEMA = "canonical-extraction-content-v1"
MAX_CONTENT = 4 * 1024 * 1024
MAX_ACCEPT_SOURCE = 8 * 1024 * 1024
MAX_ACCEPT_EVIDENCE = 64 * 1024 * 1024
MAX_ACCEPT_PAGES = 256
TEXT_FORMS = frozenset(("txt", "md", "log", "rst"))
REPARSE_FORMS = frozenset(("eml",))


def require(ok, reason):
    if not ok:
        raise enrollment.ExtractionBindingError(reason)


def encoded(value):
    return enrollment.canonical(value).encode("utf-8")


def _accepted_ocr_pages(receipt, ocr_ids):
    """page -> {receipt_id, receipt_sha256, text} for OCR pages whose receipts are bound and readable.

    Only ``ocr_text_unreviewed``/``visual_check_queued`` receipts with recorded tool versions are
    acceptable; a blocked or missing page refuses acceptance of the whole original.
    """
    entries = receipt.get("ocr_receipts")
    require(isinstance(entries, list) and entries and
            sorted(e.get("receipt_id") for e in entries if isinstance(e, dict)) == ocr_ids, "ocr_receipts_binding")
    root = receipt.get("ocr_output_root")
    require(isinstance(root, str) and os.path.isabs(root), "ocr_output_root_unbound")
    pages = {}
    for entry in entries:
        require(isinstance(entry, dict) and set(entry) == {"page", "receipt_id", "receipt_sha256", "status"} and
                type(entry["page"]) is int and entry["page"] > 0, "ocr_receipt_entry_shape")
        require(entry["status"] in routes.OCR_TEXT_STATES, "ocr_page_not_accepted:" + str(entry["status"]))
        try:
            loaded = routes.page_ocr.load_page_receipt(root, receipt["original_sha256"], entry["page"],
                                                       enrollment.checked_sha(entry["receipt_id"]))
        except (ValueError, OSError):
            raise enrollment.ExtractionBindingError("ocr_receipt_unverified") from None
        summary = routes.ocr_summary(loaded)
        require(loaded["receipt_sha256"] == entry["receipt_sha256"] and summary["status"] == entry["status"],
                "ocr_receipt_hash_mismatch")
        versions = loaded["receipt"].get("tool_versions")
        require(isinstance(versions, dict) and versions.get("tesseract"), "ocr_tool_versions_missing")
        sidecar = loaded.get("sidecar")
        require(sidecar is not None, "ocr_sidecar_missing")
        try:
            text = sidecar.decode("utf-8")
        except UnicodeError:
            raise enrollment.ExtractionBindingError("ocr_sidecar_encoding") from None
        pages[entry["page"]] = {"receipt_id": summary["receipt_id"], "receipt_sha256": summary["receipt_sha256"],
                                "text": text}
    return pages


def ocr_evidence_present(receipt):
    """Any OCR evidence keeps the visual-review hold: acceptance is refused."""
    pages = receipt.get("pages") if isinstance(receipt.get("pages"), list) else []
    return bool(receipt.get("ocr_receipts") or "ocr_derivative_sha256" in receipt or
                any(isinstance(page, dict) and page.get("ocr_receipt_id") for page in pages))


def code_identity():
    return {name: enrollment.sha(Path(module.__file__).read_bytes())
            for name, module in (("enrollment", enrollment), ("routes", routes),
                                 ("intake", folder))} | {
        "validator": enrollment.sha(Path(__file__).read_bytes())}


def _runtime_file_hash(path, budget):
    """Hash only trusted installed runtime inputs, never receipt-selected paths."""
    path = Path(path).resolve(strict=True)
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_size <= budget,
                "installed_runtime_file_bound")
        digest, total = hashlib.sha256(), 0
        while chunk := stream.read(min(65536, budget - total + 1)):
            total += len(chunk)
            require(total <= budget, "installed_runtime_file_bound")
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    require((before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
            (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) and
            total == after.st_size, "installed_runtime_changed")
    return digest.hexdigest(), total


def installed_parser_identity(form):
    components = {"intake": folder.VERSION + "/" + folder.ENGINE_REVISION}
    runtime = {"python_version": sys.version, "engine_code": code_identity()}
    runtime["python_executable_sha256"], used = _runtime_file_hash(sys.executable, MAX_ACCEPT_EVIDENCE)
    if form == "pdf":
        try:
            distribution = importlib.metadata.distribution("pypdf")
        except importlib.metadata.PackageNotFoundError:
            raise enrollment.ExtractionBindingError("installed_pdf_metadata_unavailable") from None
        metadata = distribution.read_text("METADATA")
        require(metadata and distribution.metadata.get("Name", "").lower() == "pypdf" and
                distribution.version not in ("", "unknown", "unavailable", "unverified"),
                "installed_pdf_metadata_unavailable")
        module = importlib.import_module("pypdf")
        package_files = sorted(str(item) for item in (distribution.files or ())
                               if str(item).startswith("pypdf/") and str(item).endswith(".py"))
        require(0 < len(package_files) <= 1024 and "pypdf/__init__.py" in package_files,
                "installed_pdf_files_unavailable")
        require(Path(module.__file__).resolve() ==
                Path(distribution.locate_file("pypdf/__init__.py")).resolve(),
                "installed_pdf_distribution_mismatch")
        files = {}
        for name in package_files:
            files[name], size = _runtime_file_hash(distribution.locate_file(name), MAX_ACCEPT_EVIDENCE - used)
            used += size
        components["pypdf"] = distribution.version
        runtime.update(pdf_distribution_metadata_sha256=enrollment.sha(metadata.encode()),
                       pdf_module_files=files)
    return components, enrollment.sha(encoded(runtime))


def _guarded_register_content(runner, subject, content):
    """Use WP1's writer transaction to guard registration against live claims.

    No separate lease preflight, replacement claim, callback injection or acceptance
    authority is introduced. This narrow helper uses the existing controller writer
    because set_content alone has no atomic lease-guard argument in this WP1 API.
    """
    from .ledger import stages
    digest = enrollment.sha(content)
    with stages._transaction(runner.database) as (connection, writer):
        runner._check(connection)
        _, states = stages._subject(connection, subject, "extract", runner.run_id)
        head = stages._head(connection, subject, "extract")
        same_content = bool(head and (head["content_sha256"], head["author_id"], head["tier"]) ==
                            (digest, runner.owner, "A"))
        now = datetime.now(timezone.utc)
        for dependent in stages.DEPENDENTS["extract"]:
            lease = connection.execute("SELECT * FROM work_leases WHERE item_key=?",
                                       ("stage:" + subject + ":" + dependent,)).fetchone()
            if lease and stages._time(lease["expires_at"]) > now:
                claim = connection.execute(
                    "SELECT * FROM stage_claims WHERE subject_sha256=? AND stage=? AND leased_at=?",
                    (subject, dependent, lease["leased_at"])).fetchone()
                stages._require(dependent == "extract" and same_content and claim is not None and
                    lease["owner"] == runner.owner and claim["owner"] == runner.owner and
                    claim["run_id"] == runner.run_id and claim["profile_sha256"] == runner.profile_sha256 and
                    claim["revision"] == head["revision"] and claim["content_sha256"] == digest,
                    "competing_live_lease")
        require(states["preserve"]["status"] == "done" and states["preserve"]["receipt_sha256"],
                "preservation_acceptance_required")
        if not runner.test_only:
            authority = connection.execute(
                "SELECT test_only FROM stage_validation_authority WHERE receipt_sha256=?",
                (states["preserve"]["receipt_sha256"],)).fetchone()
            stages._require(authority is not None and authority["test_only"] == 0,
                            "synthetic_prerequisite_not_production")
        if same_content:
            return {"reused": True, "revision": head["revision"]}
        require(states["extract"]["receipt_sha256"] is None, "explicit_supersession_required")
        stages._artifact(connection, writer, content, stages.MAX_CONTENT_BYTES)
        stages._invalidate(connection, writer, subject, "extract", runner.run_id,
                           "content_or_attribution_changed")
        revision = head["revision"] + 1 if head else 1
        writer.insert("stage_content", (subject, "extract", revision, digest, runner.owner, "A",
                                        runner.run_id, stages.store.now()))
        return {"reused": False, "revision": revision}


@dataclass(frozen=True)
class _InstalledExtractionValidator:
    database: Path
    evidence_root: Path
    original_root: Path

    @property
    def adapter_id(self):
        """Per-binding registry id: one process may serve several private roots."""
        binding = enrollment.canonical([str(self.database), str(self.evidence_root), str(self.original_root)])
        return ADAPTER_ID + ":" + enrollment.sha(binding.encode())[:24]

    def cas(self, digest, size=None, limit=MAX_ACCEPT_EVIDENCE):
        enrollment.checked_sha(digest)
        if size is not None:
            require(type(size) is int and 0 <= size <= limit, "invalid_evidence_size")
        raw = enrollment.read_file(self.evidence_root / digest, limit)
        require(enrollment.sha(raw) == digest, "evidence_hash_mismatch")
        require(size is None or len(raw) == size, "evidence_size_mismatch")
        return raw

    def evidence(self, import_id):
        """Read only canonical bindings and hash-addressed files in installed roots."""
        from .ledger import store
        enrollment.checked_sha(import_id)
        with store.ledger(self.database, readonly=True) as con:
            item = con.execute("SELECT * FROM extraction_adapter_imports WHERE id=?",
                               (import_id,)).fetchone()
            require(item is not None, "canonical_import_missing")
            item = dict(item)
            subject = item["original_sha256"]
            original = con.execute("SELECT * FROM originals WHERE sha256=?", (subject,)).fetchone()
            require(original is not None, "canonical_original_missing")
            original = dict(original)
            pointer = con.execute("SELECT import_id FROM extraction_adapter_current WHERE original_sha256=?",
                                  (subject,)).fetchone()
            require(pointer is not None and pointer[0] == import_id, "not_current_extraction")
            require(con.execute("SELECT 1 FROM occurrences WHERE original_sha256=?", (subject,)).fetchone(),
                    "canonical_occurrence_missing")
            run = con.execute("SELECT * FROM runs WHERE run_id=?", (item["run_id"],)).fetchone()
            require(run is not None and run["kind"] == "extraction_enrollment" and
                    run["status"] == "pending_stage_validation" and
                    run["summary"] == item["manifest_json"], "enrollment_run_unbound")
            manifest_raw = self.cas(item["manifest_sha256"], limit=MAX_CONTENT)
            require(manifest_raw == item["manifest_json"].encode(), "manifest_bytes_unbound")
            manifest = enrollment.decode(manifest_raw)
            require(isinstance(manifest, dict), "manifest_shape")
            require(manifest.get("adapter") == enrollment.VERSION and
                    manifest.get("adapter_code_sha256") == code_identity()["enrollment"] and
                    manifest.get("original_sha256") == subject and
                    manifest.get("original_bytes") == original["bytes"] and
                    manifest.get("receipt_sha256") == item["receipt_sha256"], "manifest_binding")
            require(import_id == enrollment.sha(encoded([enrollment.VERSION, subject, item["receipt_sha256"]])),
                    "import_identity_mismatch")
            require(item["run_id"] == "extraction-enroll:" + import_id, "import_run_identity")
            original_path = enrollment.checked_path(original["storage_path"])
            require(original_path.is_relative_to(self.original_root), "original_outside_installed_root")
            require(type(original["bytes"]) is int and 0 < original["bytes"] <= MAX_ACCEPT_SOURCE,
                    "source_acceptance_limit")
            raw_original = enrollment.read_file(original_path, MAX_ACCEPT_SOURCE)
            require(len(raw_original) == original["bytes"] and enrollment.sha(raw_original) == subject,
                    "canonical_original_bytes_mismatch")
            artifact_refs = manifest.get("artifacts")
            # Container formats (EML, archives) carry their members as ``child:<sha>`` blobs.
            # They are accepted only when every member is itself a preserved original, so the
            # member has its own lifecycle and nothing is reviewed through the container alone.
            require(isinstance(artifact_refs, dict) and
                    {"source_receipt", "parser_metadata", "derived_units"} <= set(artifact_refs) and
                    all(name in ("source_receipt", "parser_metadata", "derived_units",
                                 "pre_ocr_metadata", "native_units") or
                        (name.startswith("child:") and re.fullmatch(r"[0-9a-f]{64}", name[6:])) or
                        (name.startswith("ocr_receipt:") and re.fullmatch(r"[0-9a-f]{64}", name[12:]))
                        for name in artifact_refs), "unsupported_artifact_set")
            child_hashes = sorted(name[6:] for name in artifact_refs if name.startswith("child:"))
            ocr_ids = sorted(name[12:] for name in artifact_refs if name.startswith("ocr_receipt:"))
            require(bool(ocr_ids) == ("pre_ocr_metadata" in artifact_refs) == ("native_units" in artifact_refs),
                    "ocr_artifact_set_incomplete")
            artifacts = {}
            budget = MAX_ACCEPT_EVIDENCE
            for name, ref in artifact_refs.items():
                require(isinstance(ref, dict) and set(ref) == {"sha256", "path", "bytes"}, "artifact_shape")
                digest = enrollment.checked_sha(ref["sha256"])
                require(ref["path"] == str(self.evidence_root / digest), "artifact_path_not_canonical")
                artifacts[name] = self.cas(digest, ref["bytes"], limit=budget)
                budget -= len(artifacts[name])
            require(enrollment.sha(artifacts["source_receipt"]) == item["receipt_sha256"], "source_receipt_hash")
            receipt = enrollment.decode(artifacts["source_receipt"])
            metadata = enrollment.decode(artifacts["parser_metadata"])
            require(isinstance(receipt, dict) and isinstance(metadata, dict), "extraction_receipt_shape")
            require(receipt.get("schema") == "format-extraction-v1" and
                    receipt.get("original_sha256") == subject and receipt.get("original_changed") is False and
                    receipt.get("review_status") == "not_reviewed" and
                    receipt.get("parser_adapter") == routes.VERSION, "source_receipt_binding")
            ocr_pages = _accepted_ocr_pages(receipt, ocr_ids) if ocr_ids else {}
            if ocr_pages:
                # Per-page OCR: the source stays "partial" by construction (pages were not native
                # text). Acceptance records the visual-review hold per page; it never clears it.
                require(receipt.get("status") == metadata.get("stage") == manifest.get("source_status") == "partial",
                        "ocr_status_binding")
                allowed = {"ocr_needed_or_blank_page", "ocr_visual_review_required"}
                require(receipt.get("issues") == manifest.get("issues") and
                        all(isinstance(i, dict) and i.get("code") in allowed for i in receipt["issues"]) and
                        all(isinstance(i, dict) and i.get("code") in allowed for i in metadata.get("issues", [])),
                        "unresolved_extraction_evidence")
            else:
                require(receipt.get("status") == metadata.get("stage") == manifest.get("source_status") == "complete",
                        "extraction_incomplete")
                require(receipt.get("issues") == metadata.get("issues") == manifest.get("issues") == [] and
                        not ocr_evidence_present(receipt), "unresolved_extraction_evidence")
            declared = sorted({child.get("sha") for child in (receipt.get("children") or [])})
            require(declared == child_hashes and
                    sorted({child.get("sha") for child in (metadata.get("children") or [])}) == child_hashes,
                    "child_evidence_mismatch")
            for child_sha in child_hashes:
                preserved = con.execute("SELECT status FROM stage_state WHERE original_sha256=? AND stage='preserve'",
                                        (child_sha,)).fetchone()
                require(preserved is not None and preserved["status"] == "done", "child_member_not_preserved")
            parser, version = receipt.get("parser"), receipt.get("parser_version")
            require(isinstance(parser, str) and isinstance(version, str) and version not in
                    ("", "unknown", "unavailable", "unverified"), "unknown_parser")
            require(metadata.get("parser") == manifest.get("parser") == parser and
                    metadata.get("parser_version") == manifest.get("parser_version") == version and
                    metadata.get("parser_components", {}) == receipt.get("parser_components", {}),
                    "parser_binding")
            lines = artifacts["derived_units"].splitlines(keepends=True)
            require(0 < len(lines) <= enrollment.MAX_UNITS and all(len(line) <= enrollment.MAX_UNIT for line in lines),
                    "unit_acceptance_limit")
            units = [enrollment.decode(line) for line in lines]
            require(units == receipt.get("units"), "receipt_units_mismatch")
            counts = metadata.get("counts", {})
            require(isinstance(counts, dict) and type(counts.get("units")) is int and
                    counts["units"] == len(units), "parser_unit_count_mismatch")
            # With per-page OCR the parser metadata describes the native units; the derived
            # units are the verified merge of native text and OCR sidecars (checked below).
            source_units = artifacts["native_units"] if ocr_ids else artifacts["derived_units"]
            require(metadata.get("artifacts", {}).get("units.jsonl") == enrollment.sha(source_units),
                    "parser_units_hash_mismatch")
            require(isinstance(manifest.get("unit_ids"), list) and len(manifest["unit_ids"]) == len(units),
                    "canonical_unit_count_mismatch")
            # Reconcile all rows attributed to this current import, not just listed IDs.
            # Historical imports are retained. Unknown ownership fails closed rather than
            # silently excluding potentially current rows. Bound historical scanning too.
            candidates = con.execute("SELECT id,provenance_json FROM units WHERE original_sha256=? LIMIT ?",
                                     (subject, enrollment.MAX_UNITS + 1)).fetchall()
            require(len(candidates) <= enrollment.MAX_UNITS, "canonical_history_scan_bound")
            current_ids, provenance_bytes = [], 0
            for candidate in candidates:
                provenance_raw = candidate["provenance_json"].encode()
                provenance_bytes += len(provenance_raw)
                require(len(provenance_raw) <= 65536 and provenance_bytes <= MAX_ACCEPT_EVIDENCE,
                        "canonical_provenance_bound")
                provenance = enrollment.decode(provenance_raw)
                require(isinstance(provenance, dict), "canonical_import_ownership_unknown")
                owner_import = enrollment.checked_sha(provenance.get("extraction_import_id"))
                if owner_import == import_id:
                    current_ids.append(candidate["id"])
            require(len(current_ids) == len(manifest["unit_ids"]) and
                    set(current_ids) == set(manifest["unit_ids"]), "canonical_unit_set_mismatch")
            # No caller-chosen path from the source receipt is opened, including run_path.
            for ordinal, (unit, line) in enumerate(zip(units, lines), 1):
                require(isinstance(unit, dict) and isinstance(unit.get("text"), str), "unit_shape")
                digest = enrollment.sha(line)
                unit_id = enrollment.sha(encoded([import_id, ordinal, digest]))
                require(manifest["unit_ids"][ordinal - 1] == unit_id, "canonical_unit_identity")
                require(self.cas(digest, len(line), limit=enrollment.MAX_UNIT) == line, "unit_payload_mismatch")
                row = con.execute("SELECT * FROM units WHERE id=?", (unit_id,)).fetchone()
                require(row is not None, "canonical_unit_missing")
                provenance = {"extraction_import_id": import_id, "receipt_sha256": item["receipt_sha256"],
                    "payload_sha256": digest, "ordinal": ordinal,
                    "source_units_sha256": artifact_refs["derived_units"]["sha256"],
                    "parser_provenance": "bound_to_derivative_metadata",
                    "parser_components": receipt.get("parser_components", {}),
                    "ocr_receipt_ids": [entry["receipt_id"] for entry in receipt.get("ocr_receipts", [])],
                    "review_status": "not_reviewed"}
                expected = {"original_sha256": subject, "parser": parser, "parser_version": version,
                    "locator": enrollment.canonical(unit.get("locator")), "unit_type": unit.get("kind"),
                    "text_sha256": enrollment.sha(unit["text"].encode()),
                    "derived_path": str(self.evidence_root / digest), "status": "candidate_extracted",
                    "legacy_ordinal": ordinal, "provenance_json": enrollment.canonical(provenance)}
                require(all(row[key] == value for key, value in expected.items()), "canonical_unit_binding")
            histories = [enrollment.decode(row[0]) for row in con.execute(
                "SELECT page_json FROM extraction_adapter_pages WHERE import_id=? ORDER BY page_no", (import_id,))]
            pages = receipt.get("pages")
            require(histories == pages == manifest.get("pages"), "canonical_page_history_binding")
            actual_pages = [tuple(row) for row in con.execute(
                "SELECT page_no,method,method_version,derivative_sha256,confidence,needs_visual_review,status,reason "
                "FROM page_state WHERE original_sha256=? ORDER BY page_no", (subject,))]
            require(actual_pages == [(p["page_no"], p["method"], p["method_version"], p["derivative_sha256"],
                    p["confidence"], int(p["needs_visual_review"]), p["status"], p["reason"]) for p in pages],
                    "canonical_page_state_binding")
        # Independently establish denominator/content from canonical original bytes.
        form = parser.removeprefix("legacy-intake:")
        require(parser == "legacy-intake:" + form and form in TEXT_FORMS | {"pdf"} | REPARSE_FORMS,
                "unsupported_complete_parser")
        require(receipt.get("route") == routes.route(form), "parser_route_binding")
        components, runtime_hash = installed_parser_identity(form)
        require(receipt.get("parser_components") == metadata.get("parser_components") == components,
                "installed_parser_components_mismatch")
        # Legacy route receipts do not declare a runtime hash. The canonical content
        # always binds the validator-observed runtime; any supplied declaration must
        # agree in both sources and with that installed identity.
        if "parser_runtime_sha256" in receipt or "parser_runtime_sha256" in metadata:
            require(receipt.get("parser_runtime_sha256") == metadata.get("parser_runtime_sha256") == runtime_hash,
                    "installed_parser_runtime_mismatch")
        if form in TEXT_FORMS:
            require(version == folder.VERSION + "/" + folder.ENGINE_REVISION, "installed_parser_version_mismatch")
            text = raw_original.decode("utf-8", errors="strict")
            require("\ufffd" not in text, "replacement_text_unsupported")
            expected_units = [{"kind": "text_line", "locator": {"line": i}, "text": line, "data": {}}
                              for i, line in enumerate(text.splitlines(), 1)]
            require(units == expected_units and pages == [] and not counts.get("pages_expected"),
                    "source_text_denominator_or_content")
            denominator = {"kind": "items", "total": len(expected_units)}
        elif form in REPARSE_FORMS:
            # Container/structured formats: independently re-run the installed parser on the
            # canonical bytes and require identical units and member set. Members are accepted
            # earlier only when each is a preserved original in its own right.
            with tempfile.TemporaryDirectory(prefix="extract-validate-", dir=self.evidence_root) as scratch:
                checked = routes.extract(original_path, subject, Path(scratch), form=form, timeout=60)
            require(checked.get("status") == "complete" and checked.get("issues") == [] and
                    checked.get("parser") == parser and checked.get("parser_version") == version and
                    checked.get("parser_components", {}) == receipt.get("parser_components", {}),
                    "installed_reparse_incomplete")
            require(checked.get("units") == units and checked.get("pages") == pages == [], "reparse_content_mismatch")
            require(sorted({c.get("sha") for c in checked.get("children", [])}) == child_hashes, "reparse_children_mismatch")
            require(0 < len(units) <= enrollment.MAX_UNITS, "reparse_denominator")
            denominator = {"kind": "items", "total": len(units)}
        else:
            require(type(counts.get("pages_expected")) is int and
                    0 < counts["pages_expected"] <= MAX_ACCEPT_PAGES, "pdf_denominator_unavailable")
            # Reuse installed resource/time-bounded PDF parser. No receipt path, executable,
            # callback, form, timeout, or output directory controls this invocation.
            with tempfile.TemporaryDirectory(prefix="extract-validate-", dir=self.evidence_root) as scratch:
                checked = routes.extract(original_path, subject, Path(scratch), form="pdf", timeout=20)
            require(checked.get("parser") == parser and checked.get("parser_version") == version and
                    checked.get("parser_components", {}) == receipt.get("parser_components", {}),
                    "installed_pdf_reparse_incomplete")
            if ocr_pages:
                native = [enrollment.decode(line) for line in artifacts["native_units"].splitlines()]
                require(checked.get("status") == "partial" and checked.get("units") == native and
                        len(native) == len(pages) == len(checked.get("pages", [])) == counts["pages_expected"],
                        "pdf_native_reparse_mismatch")
                merged = routes.merge_ocr_units(native, {page: routes.ocr_unit(page, info["receipt_id"],
                                                                                info["receipt_sha256"], info["text"])
                                                         for page, info in ocr_pages.items()})
                require(enrollment.canonical(merged) == enrollment.canonical(units), "ocr_unit_merge_mismatch")
                for index, page in enumerate(pages, 1):
                    native_page = checked["pages"][index - 1]
                    require(page["page_no"] == index == native_page["page_no"], "pdf_page_order")
                    if index in ocr_pages:
                        require(native_page["status"] != "ok" and page["status"] == "partial" and
                                page.get("ocr_receipt_id") == ocr_pages[index]["receipt_id"] and
                                page.get("needs_visual_review") is True, "ocr_page_state_binding")
                    else:
                        require(page["status"] == "ok" and native_page["status"] == "ok", "pdf_missing_or_partial_pages")
            else:
                require(checked.get("status") == "complete" and checked.get("issues") == [],
                        "installed_pdf_reparse_incomplete")
                require(checked.get("units") == units and checked.get("pages") == pages and
                        len(units) == len(pages) == counts["pages_expected"], "pdf_denominator_or_content")
                require(all(p["status"] == "ok" and p["page_no"] == i for i, p in enumerate(pages, 1)),
                        "pdf_missing_or_partial_pages")
            denominator = {"kind": "pages", "total": len(pages)}
        coverage = {"denominator": denominator, "covered": denominator["total"], "scope": "full_text"}
        envelope = {"schema": ENVELOPE_SCHEMA, "original_sha256": subject, "import_id": import_id,
                    "source_receipt_sha256": item["receipt_sha256"], "manifest_sha256": item["manifest_sha256"],
                    "parser": parser, "parser_version": version, "unit_count": len(units),
                    "coverage": coverage, "installed_code": code_identity(),
                    "parser_components": components, "parser_runtime_sha256": runtime_hash}
        if ocr_ids:
            envelope["ocr"] = {"method": receipt.get("ocr_method"), "pages": sorted(ocr_pages),
                               "receipt_ids": ocr_ids, "visual_review_pending": True}
        raw = encoded(envelope)
        require(len(raw) <= MAX_CONTENT, "content_envelope_limit")
        return raw, original, coverage

    def __call__(self, context):
        require(context.get("stage") == "extract", "wrong_stage")
        raw = context.get("content")
        require(type(raw) is bytes and len(raw) <= MAX_CONTENT, "content_envelope_limit")
        content = enrollment.decode(raw)
        require(isinstance(content, dict) and content.get("schema") == ENVELOPE_SCHEMA, "content_envelope_schema")
        expected, original, coverage = self.evidence(content.get("import_id"))
        require(raw == expected and context.get("original") == original and
                context.get("subject_sha256") == original["sha256"], "canonical_content_binding")
        receipt = context.get("receipt", {})
        require(receipt.get("verdict") == "pass" and receipt.get("coverage") == coverage and
                receipt.get("model_or_tool") == ADAPTER_ID and
                receipt.get("locators") == ["extraction-import:" + content["import_id"]] and
                not receipt.get("holds") and not receipt.get("challenges"), "extraction_acceptance_scope")
        return True


def install_extraction_validator(*, database, evidence_root, original_root):
    """Trusted startup only. Install this fixed implementation, not a caller callback.

    WP1 currently exposes its registry to installed application modules. Fail on
    conflicting binding; one canonical database/root pair per process/adapter ID.
    Profile creation remains the application's explicit trusted-startup action.
    """
    from .ledger import stages
    binding = _InstalledExtractionValidator(enrollment.checked_path(database),
        enrollment.private_dir(evidence_root), enrollment.private_dir(original_root))
    prior = stages._INSTALLED_VALIDATORS.get(binding.adapter_id)
    require(prior is None or type(prior) is _InstalledExtractionValidator and prior == binding,
            "installed_extraction_binding_conflict")
    stages._INSTALLED_VALIDATORS[binding.adapter_id] = binding
    return binding.adapter_id


class ExtractionStageAdapter:
    """Opt-in combined-runner bridge. Inputs are canonical IDs, never paths/receipts.

    Enrollment is separate and remains pending. Run/owner/profile must come from
    authenticated trusted startup, with a previously configured installed profile.
    """
    def __init__(self, *, database, evidence_root, original_root, run_id, owner, profile_id):
        from .ledger import stages
        self.validator = _InstalledExtractionValidator(enrollment.checked_path(database),
            enrollment.private_dir(evidence_root), enrollment.private_dir(original_root))
        self.runner = stages.installed_runner(database, run_id=run_id, owner=owner, profile_id=profile_id)
        require(self.runner.validators.get("extract") == (self.validator.adapter_id, self.validator),
                "installed_profile_extract_binding")

    def accept(self, import_id):
        from .ledger import store
        content, original, coverage = self.validator.evidence(import_id)
        subject = original["sha256"]
        _guarded_register_content(self.runner, subject, content)
        with store.ledger(self.runner.database, readonly=True) as con:
            states = {row["stage"]: dict(row) for row in con.execute(
                "SELECT * FROM stage_state WHERE original_sha256=?", (subject,))}
            prior_hash = states["extract"]["receipt_sha256"]
            previous = con.execute("SELECT payload FROM stage_artifacts WHERE sha256=?", (prior_hash,)).fetchone()
        if states["extract"]["status"] == "done" and previous is not None:
            # Reverify CAS even on WP1's exact-receipt replay fast path.
            self.validator({"stage": "extract", "subject_sha256": subject, "original": original,
                            "content": content, "receipt": enrollment.decode(bytes(previous[0]))})
            return self.runner.promote(subject, "extract", bytes(previous[0]))
        require(states["preserve"]["status"] == "done" and states["preserve"]["receipt_sha256"],
                "preservation_acceptance_required")
        require(prior_hash is None, "explicit_supersession_required")
        claim = self.runner.claim(subject, "extract")
        receipt = {"schema": "ledger-stage-receipt-v1", "subject_sha256": subject,
            "stage": "extract", "content_sha256": enrollment.sha(content), "author_id": self.runner.owner,
            "tier": "A", "reviewer_id": self.runner.owner, "role": "extractor", "verdict": "pass",
            "coverage": coverage, "locators": ["extraction-import:" + import_id],
            "rationale": "Installed functional extraction check; no visual, legal, privacy or review acceptance.",
            "model_or_tool": ADAPTER_ID, "created_at_tz": datetime.now(timezone.utc).isoformat(),
            "input_hashes": {"original": subject, "preserve": states["preserve"]["receipt_sha256"]}}
        return self.runner.promote(subject, "extract", encoded(receipt), claim_id=claim["claim_id"])
