"""Synthetic-only producer-shaped canonical extraction -> outputs integration."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records import approval_queue, console_cited_pages as cp, console_export, console_snapshot
from campaign_tool.records import extraction_routes as routes, ocr_review, run_stages
from campaign_tool.records.extraction_ledger import ExtractionLedgerAdapter, canonical, sha
from campaign_tool.records.ledger import store
from campaign_tool.records.outcomes import factsheets as fs, received_items as ri
from contextlib import closing
from tests.records import ocr_fixtures

FIXTURE = Path(__file__).parent / "fixtures" / "outcome_factsheets" / "produced.json"


class FactSheetsTests(unittest.TestCase):
    def setUp(self):
        # Keep synthetic writes outside the clone's preservation snapshot.
        workspaces = Path(tempfile.gettempdir()) / "outcome-factsheets" / "test-workspaces"
        workspaces.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="factsheets-", dir=workspaces)
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.processing = self.project / "processing-v1"
        self.processing.mkdir(mode=0o700)
        self.database = self.processing / "ledger.sqlite"
        store.initialize(self.database)
        self.evidence = self.processing / "artifacts" / "evidence"
        self.evidence.parent.mkdir(mode=0o700)
        self.adapter = ExtractionLedgerAdapter(database=self.database, evidence_root=self.evidence)
        self.fixture = json.loads(FIXTURE.read_text())
        self.serial = 0
        with store.ledger(self.database) as con:
            con.execute("INSERT INTO runs VALUES('fixture','fixture','2041-01-01',NULL,'fixture',NULL,NULL,NULL,'fixture','{}')")
            for agency in self.fixture["agencies"]:
                con.execute("INSERT INTO agencies VALUES(?,?,NULL,NULL)", (agency["id"], agency["name"]))
            con.commit()
        self.docs = [self.document(d["pages"], d["agency"]) for d in self.fixture["documents"]]
        portal = self.project / "portal-queue-v1"
        portal.mkdir(mode=0o700)
        with closing(sqlite3.connect(portal / "portal.sqlite")) as con, con:
            con.execute("CREATE TABLE portal_items(id TEXT)")

    def private(self, path, raw):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(raw)
        path.chmod(0o600)

    def document(self, pages, agency, *, status="typed", role="agency_original", scope="in_scope", locator=None, raw=None):
        self.serial += 1
        source = self.project / ("original-" + str(self.serial))
        # Synthetic source bytes; no real PDF parser or OCR qualification claim. `raw` (a synthetic PDF) is enrolled by the caller.
        self.private(source, raw or ("SYNTHETIC RECEIPT FIXTURE " + str(self.serial) + "\n" + "\n".join(pages)).encode())
        subject = sha(source.read_bytes())
        with store.ledger(self.database) as con:
            con.execute("INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (subject, source.stat().st_size, None, None, role, scope, str(source), "pending", None, "{}"))
            con.execute("INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?)", (subject, subject, "local", "synthetic", None, None, "fixture", "{}"))
            con.execute("INSERT INTO joins VALUES(?,?,?,?,?,?,?,?)", (subject, subject, agency, None, "fixture_production", "{}", status, None))
            for stage in store.STAGES:
                con.execute("INSERT INTO stage_state VALUES(?,?,?,?,?,?,?,?)", (subject, stage, "pending", None, "fixture", "2041-01-01", "fixture", None))
            con.commit()
        if raw is None:
            self.enroll(source, pages, locator=locator)
        return subject, source

    def enroll(self, source, pages, *, locator=None):
        self.serial += 1
        run = self.project / ("extraction-" + str(self.serial))
        units = [{"kind": "pdf_page", "locator": locator if locator is not None else {"page": n},
                  "text": text, "data": {}} for n, text in enumerate(pages, 1)]
        parser, version = "fixture-parser", "synthetic-1"
        metadata = {"stage": "complete", "parser": parser, "parser_version": version,
                    "counts": {"pages_expected": len(pages)}, "children": []}
        self.private(run / "input", source.read_bytes())
        self.private(run / "derived" / "digest.json", canonical(metadata).encode())
        self.private(run / "derived" / "units.jsonl", b"".join((canonical(u) + "\n").encode() for u in units))
        subject = sha(source.read_bytes())
        page_manifest = routes.page_manifest(units, len(pages), parser, version)
        receipt = {"schema": "format-extraction-v1", "original_sha256": subject,
                   "route": routes.route("pdf"), "parser_adapter": routes.VERSION,
                   "status": "partial", "units": units, "pages": page_manifest, "issues": [],
                   "review_status": "not_reviewed", "original_changed": False, "run_path": str(run),
                   "parser": parser, "parser_version": version, "parser_components": {}, "children": []}
        self.private(run / "extraction.json", (canonical(receipt) + "\n").encode())
        return self.adapter.enroll(original_path=source, receipt_path=run / "extraction.json",
                                   receipt_sha256=sha((run / "extraction.json").read_bytes()))

    def collect(self):
        with run_stages.snapshot_ro(self.database) as con:
            return fs.collect(con, self.processing, "test-run")

    def sheet(self, report, agency="maple"):
        return next(a for a in report["agencies"] if a["agency_id"] == agency)

    def stage(self, run="test-run"):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = run_stages.main(["outputs", "--project", str(self.project), "--run-id", run])
        return code, json.loads(stdout.getvalue())

    def tree(self):
        return {str(p.relative_to(self.processing)): (p.read_bytes(), p.stat().st_ino)
                for p in self.processing.rglob("*") if p.is_file()}

    def statements(self, sheet, field):
        return sheet["fields"][field]["statements"]

    def assert_rederivable(self, sheet, pages):
        # Contract: a quote is exactly its cited unit lines joined by single spaces.
        # Bug caught: a quote built another way (or off-by-one bounds) cannot be re-derived.
        for field, value in sheet["fields"].items():
            for s in value["statements"]:
                cite = s["citation"]
                lines = pages[cite["page"] - 1].splitlines()
                self.assertTrue(1 <= cite["line_start"] <= cite["line_end"] <= len(lines), field)
                self.assertEqual(s["quote"], " ".join(x.strip() for x in lines[cite["line_start"] - 1:cite["line_end"]]), field)

    def test_all_fields_exact_citations_and_all_agencies(self):
        report = self.collect()
        self.assertEqual([a["agency_id"] for a in report["agencies"]], ["cedar", "empty", "maple"])
        self.assertTrue(report["review_required"])
        self.assertFalse(report["semantic_acceptance"] or report["publication_ready"])
        sheet = self.sheet(report)
        self.assertEqual(set(sheet["fields"]), set(fs.FIELDS))
        self.assertEqual(len(sheet["fields"]), 10)
        source_pages = self.fixture["documents"][0]["pages"]
        for field, value in sheet["fields"].items():
            self.assertTrue(value["statements"], field)
            for statement in value["statements"]:
                cite = statement["citation"]
                self.assertEqual(cite["document_sha256"], self.docs[0][0])
                self.assertTrue(all(fs.is_hash(cite[k]) for k in ("document_sha256", "unit_id", "text_sha256", "payload_sha256")))
                # Existing keys are unchanged (v1 readers); only the line range is added.
                self.assertEqual(set(cite), {"document_sha256", "page", "unit_id", "text_sha256", "payload_sha256", "line_start", "line_end"})
        self.assert_rederivable(sheet, source_pages)
        # Single-line statements stay byte-identical to main, with equal bounds.
        cost = sheet["fields"]["cost"]["statements"][0]
        self.assertEqual(cost["quote"], "Annual fee: $12,345.67.")
        self.assertEqual((cost["citation"]["line_start"], cost["citation"]["line_end"]), (3, 3))
        count = sheet["fields"]["camera_count"]["statements"][0]
        self.assertEqual(count["quote"], "The department operates 7 ALPR cameras.")
        self.assertEqual((count["citation"]["line_start"], count["citation"]["line_end"]), (4, 4))
        # The fixture wraps this sentence over two lines: one whole quote, not the old prefix.
        retention = self.statements(sheet, "retention")[0]
        self.assertEqual(retention["quote"], "Retention is 21 days unless evidence is preserved.")
        self.assertEqual((retention["citation"]["line_start"], retention["citation"]["line_end"]), (6, 7))
        self.assertEqual([s["quote"] for s in self.statements(sheet, "retention")],
                         ["Retention is 21 days unless evidence is preserved.", "Search logs are retained for 13 months."])

    def test_wrapped_run_on_obeys_statement_length_limit(self):
        # Contract: 2000 joined characters is a statement; 2001 gives the existing gap and no
        # prefix/suffix statement. Bug caught: a limit applied per physical line, or a truncated quote.
        def run_on(total):
            head = "Retention is "
            return [head + "x" * 40, "y" * (total - len(head) - 41)]
        pages = ["\n".join(run_on(2000)), "\n".join(run_on(2001))]
        self.assertEqual([len(" ".join(p.splitlines())) for p in pages], [2000, 2001])
        self.document(pages, "empty")
        sheet = self.sheet(self.collect(), "empty")
        found = self.statements(sheet, "retention")
        self.assertEqual([(s["citation"]["page"], s["citation"]["line_start"], s["citation"]["line_end"]) for s in found], [(1, 1, 2)])
        self.assert_rederivable(sheet, pages)
        gaps = [g for g in sheet["gaps"] if g["code"] == "statement_too_long"]
        self.assertEqual(len(gaps), 1)
        self.assertEqual(sheet["gaps"], gaps)

    def test_missing_fields_questions_headings_never_invent_values(self):
        report = self.collect()
        fields = self.sheet(report, "cedar")["fields"]
        for field in ("vendor", "contract", "cost", "camera_count", "camera_locations", "outside_sharing", "policy_in_force", "search_logs"):
            self.assertEqual(fields[field], {"status": "NOT FOUND", "statements": []}, field)
        self.assertTrue(all(f["status"] == "NOT FOUND" for f in self.sheet(report, "empty")["fields"].values()))
        self.assertEqual(len(fields["retention"]["statements"]), 2, "no latest-value inference")
        self.assertEqual(fields["audits"]["statements"][0]["quote"], "No responsive audit records were located.")

    def test_wrapped_statements_stop_at_headings_questions_blank_lines_and_units(self):
        # Contract: joining never absorbs a heading, a question, text after a blank line or the next unit.
        # Bug caught: a naive join of all non-blank lines (heading prefix, swallowed paragraph, cross-page sentence).
        pages = ["Contract\nThe contract is signed\nand filed.\n\nAudits are reviewed\n\nevery quarter.\n"
                 "Is retention 30 days\nor longer?\nRetention is 5 days.\n\nAudits are quarterly.\n\nAudits are quarterly.\n"
                 "Data are retained for 9 days and",
                 "then deleted."]
        self.document(pages, "empty")
        sheet = self.sheet(self.collect(), "empty")
        self.assert_rederivable(sheet, pages)
        got = {f: [(s["quote"], s["citation"]["page"], s["citation"]["line_start"], s["citation"]["line_end"])
                   for s in v["statements"]] for f, v in sheet["fields"].items() if v["statements"]}
        self.assertEqual(got, {
            "contract": [("The contract is signed and filed.", 1, 2, 3)],
            "audits": [("Audits are reviewed", 1, 5, 5), ("Audits are quarterly.", 1, 12, 12)],
            "retention": [("Retention is 5 days.", 1, 10, 10), ("Data are retained for 9 days and", 1, 15, 15)]})
        self.assertEqual(sheet["gaps"], [])

    def test_negation_proposal_and_policy_dates_preserved_not_resolved(self):
        report = self.collect()
        fields = self.sheet(report)["fields"]
        self.assertTrue(fields["outside_sharing"]["statements"][0]["quote"].startswith("No outside"))
        self.assertIn("NOT executed", fields["contract"]["statements"][-1]["quote"])
        self.assertEqual(fields["policy_in_force"]["statements"][0]["quote"], "Policy MV-17 is effective 2041-03-17.")
        self.assertIn("not resolved automatically", fs.markdown(report))

    def test_stage_writes_both_formats_snapshot_unchanged_and_readonly_sources(self):
        # The extract stage's per-run list (input to the DOC-2 text-state pair).
        (self.processing / "runs").mkdir(mode=0o700)
        (self.processing / "runs" / "extract-test-run.json").write_text(json.dumps(
            {"schema": "records-extract-outcomes-v1", "run_id": "test-run", "documents": []}))
        before = self.tree()
        code, result = self.stage()
        self.assertEqual((code, result["status"], result["counts"]["output"]), (0, "ok", 1))
        output = self.project / "run-outputs" / "test-run"
        # The per-run directory also carries the comparison, request-register and text-state pairs.
        own = {"agency-factsheets.json", "agency-factsheets.md"}
        self.assertEqual({p.name for p in output.iterdir()} - {
            "comparison.json", "comparison.md", "text-state.json", "text-state.md",
            "CURRENT-REQUEST-REGISTER.json", "CURRENT-REQUEST-REGISTER.md", "outcomes"}, own)
        report = json.loads((output / "agency-factsheets.json").read_text())
        text = (output / "agency-factsheets.md").read_text()
        self.assertEqual(text, fs.markdown(report))
        for sheet in report["agencies"]:
            for value in sheet["fields"].values():
                for statement in value["statements"]:
                    cite = statement["citation"]
                    self.assertIn("document sha256 " + cite["document_sha256"] + ", page " + str(cite["page"])
                                  + ", lines " + str(cite["line_start"]) + "–" + str(cite["line_end"]) + "]", text)
        # Independently expected rendering of the wrapped fixture sentence (lines 6-7 of page 1).
        self.assertIn("<code>Retention is 21 days unless evidence is preserved.</code> [document sha256 "
                      + self.docs[0][0] + ", page 1, lines 6–7]", text)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
        for path in output.iterdir():
            if path.name == "outcomes":  # received-items directory (REC-AUTO-3)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
                self.assertEqual(sorted(p.name for p in path.iterdir()), ["analysis-ready.json", "gap-proposals.json", "received-items.json", "release-links.json", "response-items.json", "timeline.json", "unmatched-messages.json"])
                for name in ("received-items.json", "release-links.json", "response-items.json", "timeline.json", "unmatched-messages.json"):
                    self.assertEqual(stat.S_IMODE((path / name).stat().st_mode), 0o600)
                    json.loads((path / name).read_text())
                continue
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        snapshot = json.loads((output.parent / "test-run.json").read_text())
        self.assertEqual(snapshot["originals"], 2)
        self.assertEqual(set(snapshot), {"snapshot_version", "run_id", "stage_state", "mail_messages", "originals", "portal_items", "portal_refusals"})
        self.assertEqual(self.tree(), before)

    def test_replay_and_new_run_use_distinct_outputs(self):
        self.assertEqual(self.stage()[0], 0)
        path = self.project / "run-outputs" / "test-run" / "agency-factsheets.json"
        identity = path.stat().st_ino
        self.assertEqual(self.stage()[0], 0)
        self.assertEqual(path.stat().st_ino, identity)
        self.assertEqual(self.stage("second-run")[0], 0)
        self.assertTrue((self.project / "run-outputs" / "second-run" / path.name).is_file())

    def test_shared_run_directory_sibling_outputs_and_partial_pair(self):
        # Sibling outcome writers share run-outputs/<run>/: their files never
        # block a fact-sheet replay, while a partial own pair is never repaired.
        self.assertEqual(self.stage()[0], 0)
        output = self.project / "run-outputs" / "test-run"
        self.assertIn("comparison.json", {p.name for p in output.iterdir()})
        identity = (output / "agency-factsheets.md").stat().st_ino
        self.assertEqual(self.stage()[0], 0)
        self.assertEqual((output / "agency-factsheets.md").stat().st_ino, identity)
        sibling = self.project / "run-outputs" / "sibling-run"
        sibling.mkdir(mode=0o700, parents=True)
        (sibling / "foreign.txt").write_text("sibling writer")
        self.assertEqual(self.stage("sibling-run")[0], 0)
        self.assertTrue((sibling / "agency-factsheets.json").is_file())
        self.assertEqual((sibling / "foreign.txt").read_text(), "sibling writer")
        partial = self.project / "run-outputs" / "partial-run"
        partial.mkdir(mode=0o700)
        (partial / "agency-factsheets.md").write_text("prior half")
        code, result = self.stage("partial-run")
        self.assertEqual((code, result["error_code"]), (1, "OUTPUTS_WRITE_FAILED"))
        self.assertFalse((partial / "agency-factsheets.json").exists())
        self.assertEqual((partial / "agency-factsheets.md").read_text(), "prior half")

    def test_changed_existing_output_refused_without_overwrite(self):
        self.assertEqual(self.stage()[0], 0)
        output = self.project / "run-outputs"
        old = (output / "test-run.json").read_bytes()
        path = output / "test-run" / "agency-factsheets.json"
        path.write_text("prior evidence")
        code, result = self.stage()
        self.assertEqual((code, result["error_code"]), (1, "OUTPUTS_WRITE_FAILED"))
        self.assertEqual(path.read_text(), "prior evidence")
        self.assertEqual((output / "test-run.json").read_bytes(), old)

    def test_hints_blocked_excluded_and_project_artifacts_are_not_facts(self):
        for kwargs in ({"status": "hinted"}, {"status": "blocked"}, {"scope": "out_of_scope"},
                       {"scope": "excluded"}, {"role": "excluded_unrelated_personal"}, {"role": "project_artifact"}):
            self.document(["Vendor: Must Not Appear."], "empty", **kwargs)
        sheet = self.sheet(self.collect(), "empty")
        self.assertEqual(sheet["document_sha256s"], [])
        self.assertEqual(sheet["fields"]["vendor"]["status"], "NOT FOUND")

    def test_ambiguous_agency_attribution_stays_unknown(self):
        subject = self.docs[0][0]
        with store.ledger(self.database) as con:
            con.execute("INSERT INTO joins VALUES('second',?,'cedar',NULL,'fixture','{}','typed',NULL)", (subject,))
            con.commit()
        report = self.collect()
        self.assertEqual(self.sheet(report)["fields"]["cost"]["status"], "NOT FOUND")
        self.assertIn("ambiguous_agency_attribution", json.dumps(self.sheet(report)["gaps"]))

    def test_current_extraction_only_no_stale_facts(self):
        subject, source = self.docs[0]
        self.enroll(source, ["Retention is 42 days."])
        sheet = self.sheet(self.collect())
        self.assertEqual(sheet["fields"]["vendor"]["status"], "NOT FOUND")
        self.assertEqual([s["quote"] for s in sheet["fields"]["retention"]["statements"]], ["Retention is 42 days."])

    def test_unpaginated_units_are_explicit_gaps_no_fake_page(self):
        self.document(["Vendor: Imaginary Systems."], "empty", locator={"line": 1})
        sheet = self.sheet(self.collect(), "empty")
        self.assertEqual(sheet["fields"]["vendor"]["status"], "NOT FOUND")
        self.assertIn("page_locator_unavailable", json.dumps(sheet["gaps"]))

    def test_empty_canonical_ledger_outputs_valid_not_found_sheets(self):
        fresh = self.project / "fresh"
        fresh.mkdir(mode=0o700)
        store.initialize(fresh / "ledger.sqlite")
        with run_stages.snapshot_ro(fresh / "ledger.sqlite") as con:
            report = fs.collect(con, fresh, "empty")
        self.assertEqual(report["agencies"], [])
        self.assertIn("current_extraction_unavailable", report["gaps"])

    def test_missing_current_extraction_for_agency_is_explicit(self):
        with patch.object(fs.store, "effective_original", side_effect=ValueError("unavailable")):
            report = self.collect()
        self.assertIn("effective_original_unavailable", json.dumps(self.sheet(report)["gaps"]))
        self.assertEqual(self.sheet(report)["fields"]["cost"]["status"], "NOT FOUND")

    def test_payload_corruption_fails_before_any_outputs(self):
        with store.ledger(self.database, readonly=True) as con:
            row = con.execute("SELECT derived_path FROM units ORDER BY id LIMIT 1").fetchone()
        path = Path(row[0])
        path.chmod(0o600)
        path.write_bytes(b"corrupt")
        code, result = self.stage()
        self.assertEqual((code, result["error_code"]), (1, "OUTPUTS_WRITE_FAILED"))
        self.assertFalse((self.project / "run-outputs" / "test-run").exists())

    def test_ledger_wal_is_not_read_and_no_output_written(self):
        wal = Path(str(self.database) + "-wal")
        wal.write_bytes(b"synthetic pending writer")
        before = self.tree()
        code, result = self.stage()
        self.assertEqual((code, result["error_code"]), (0, "OUTPUTS_LEDGER_BUSY"))
        self.assertEqual(self.tree(), before)
        self.assertFalse((self.project / "run-outputs").exists())

    def test_symlink_output_and_evidence_parent_are_refused(self):
        output = self.project / "run-outputs"
        output.mkdir(mode=0o700)
        outside = self.project / "outside"
        outside.mkdir(mode=0o700)
        (output / "test-run").symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.stage()[1]["error_code"], "OUTPUTS_WRITE_FAILED")
        self.assertEqual(list(outside.iterdir()), [])
        # A separate evidence ancestor, without changing any existing payload.
        renamed = self.evidence.with_name("evidence-kept")
        self.evidence.rename(renamed)
        self.evidence.symlink_to(renamed, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink_artifact"):
            self.collect()

    def test_markdown_renders_record_markup_inert(self):
        self.document(["Vendor: <script>alert(1)</script> [click](https://example.invalid) ![image](x) `code`."], "empty")
        text = fs.markdown(self.collect())
        self.assertNotIn("<script>", text)
        self.assertNotIn("[click](", text)
        self.assertNotIn("![image]", text)
        self.assertIn("&lt;script&gt;", text)

    def test_limits_fail_closed_including_output_limit(self):
        for key, value, reason in (("MAX_ROWS", 1, "factsheet_metadata_bound"),
                                   ("MAX_TEXT_BYTES", 1, "factsheet_text_bound")):
            with self.subTest(key=key), patch.object(fs, key, value):
                with self.assertRaisesRegex(ValueError, reason):
                    self.collect()
        with patch.object(fs, "MAX_OUTPUT_BYTES", 1):
            self.assertEqual(self.stage()[1]["error_code"], "OUTPUTS_WRITE_FAILED")
        self.assertFalse((self.project / "run-outputs" / "test-run").exists())

    def test_text_budget_exact_boundary_and_one_over(self):
        with store.ledger(self.database, readonly=True) as con:
            size = sum(Path(r[0]).stat().st_size for r in con.execute("SELECT derived_path FROM units"))
        with patch.object(fs, "MAX_TEXT_BYTES", size):
            self.assertTrue(self.sheet(self.collect())["fields"]["vendor"]["statements"])
        with patch.object(fs, "MAX_TEXT_BYTES", size - 1):
            with self.assertRaisesRegex(ValueError, "factsheet_text_bound"):
                self.collect()

    def test_output_budget_exact_boundary_and_one_over(self):
        report = self.collect()
        size = len((json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode()) + len(fs.markdown(report).encode())
        output = self.project / "run-outputs"
        output.mkdir(mode=0o700)
        with patch.object(fs, "MAX_OUTPUT_BYTES", size - 1):
            with self.assertRaises(OSError):
                fs.write_run_factsheets(self.processing, output / "test-run", "test-run")
        self.assertFalse((output / "test-run").exists())
        with patch.object(fs, "MAX_OUTPUT_BYTES", size):
            fs.write_run_factsheets(self.processing, output / "test-run", "test-run")
        self.assertTrue((output / "test-run" / "agency-factsheets.json").is_file())

    def test_wrong_text_hash_and_untrusted_payload_path_fail_closed(self):
        subject = self.docs[0][0]
        with store.ledger(self.database) as con:
            con.execute("UPDATE units SET text_sha256=? WHERE original_sha256=?", ("0" * 64, subject))
            con.commit()
        with self.assertRaisesRegex(ValueError, "factsheet_unit_binding"):
            self.collect()
        with store.ledger(self.database) as con:
            con.execute("UPDATE units SET derived_path='/not-admitted/source' WHERE original_sha256=?", (subject,))
            con.commit()
        # An off-root payload path is no longer a writer failure: it is an unread item-level gap.
        with patch.object(fs, "read_pinned", wraps=fs.read_pinned) as reader:
            report = self.collect()
        self.assertFalse(any(str(call.args[0]) == "/not-admitted/source" for call in reader.call_args_list))
        self.assertEqual(self.sheet(report)["gaps"], [
            {"document_sha256": subject, "code": "current_extraction_unavailable"},
            {"document_sha256": subject, "code": "payload_outside_project_evidence"}])

    def test_offroot_current_unit_is_an_unread_item_gap_not_a_writer_failure(self):
        subject, _ = self.docs[0]
        other = self.docs[1][0]
        with store.ledger(self.database) as con:
            # Page 1 carries statements; a later off-root unit must discard them too.
            unit = con.execute("SELECT id,derived_path FROM units WHERE original_sha256=? "
                               "AND legacy_ordinal=2", (subject,)).fetchone()
            expected_path = Path(unit["derived_path"])
            offroot_path = self.project / "retained-units" / expected_path.name
            self.private(offroot_path, expected_path.read_bytes())
            con.execute("UPDATE units SET derived_path=? WHERE id=?", (str(offroot_path), unit["id"]))
            con.commit()
        reads = []
        read_pinned = fs.read_pinned

        def guarded_read(path, digest, limit):
            reads.append(Path(path))
            return read_pinned(path, digest, limit)

        with patch.object(fs, "read_pinned", side_effect=guarded_read):
            report = self.collect()
        self.assertNotIn(offroot_path, reads)
        self.assertNotIn(expected_path, reads)
        sheet = self.sheet(report)
        self.assertEqual(sheet["document_sha256s"], [subject])
        self.assertEqual(sheet["gaps"], [
            {"document_sha256": subject, "code": "current_extraction_unavailable"},
            {"document_sha256": subject, "code": "payload_outside_project_evidence"}])
        for field, value in sheet["fields"].items():
            self.assertEqual((value["status"], value["statements"]), ("NOT FOUND", []), field)
        control = self.sheet(report, "cedar")
        self.assertEqual(control["gaps"], [])
        self.assertTrue(any(v["statements"] for v in control["fields"].values()))
        self.assertTrue(all(s["citation"]["document_sha256"] == other
                            for v in control["fields"].values() for s in v["statements"]))
        # The outputs stage writes both files instead of failing with FACTSHEETS_UNAVAILABLE.
        output = self.project / "run-outputs" / "test-run"
        output.parent.mkdir(mode=0o700)
        fs.write_run_factsheets(self.processing, output, "test-run")
        written = json.loads((output / "agency-factsheets.json").read_text())
        self.assertEqual(self.sheet(written)["gaps"], sheet["gaps"])
        self.assertIn("payload&#95;outside&#95;project&#95;evidence", (output / "agency-factsheets.md").read_text())

    def test_typed_request_mismatch_does_not_cross_agencies(self):
        with store.ledger(self.database) as con:
            con.execute("INSERT INTO requests VALUES('wrong','cedar','SYNTH',NULL,NULL,NULL,NULL,'produced')")
            con.execute("UPDATE joins SET request_id='wrong' WHERE original_sha256=?", (self.docs[0][0],))
            con.commit()
        with self.assertRaisesRegex(ValueError, "factsheet_typed_join_mismatch"):
            self.collect()

    def test_write_failure_reports_failure_preserves_partial_evidence(self):
        real_open = fs.os.open
        def fail_second(path, flags, *args, **kwargs):
            if str(path).endswith("agency-factsheets.md") and flags & os.O_WRONLY:
                raise OSError("synthetic write failure")
            return real_open(path, flags, *args, **kwargs)
        with patch.object(fs.os, "open", side_effect=fail_second):
            code, result = self.stage()
        self.assertEqual((code, result["error_code"]), (1, "OUTPUTS_WRITE_FAILED"))
        output = self.project / "run-outputs"
        self.assertTrue((output / "test-run" / "agency-factsheets.json").is_file())
        self.assertFalse((output / "test-run.json").exists())
        self.assertEqual(self.stage()[1]["error_code"], "OUTPUTS_WRITE_FAILED", "partial evidence never silently repaired")

    def test_invalid_run_ids_refused_without_path_escape(self):
        output = self.project / "run-outputs"
        output.mkdir(mode=0o700)
        for value in ("../escape", ".", "..", "", " space", "a/b"):
            with self.subTest(value=value), self.assertRaises(OSError):
                fs.write_run_factsheets(self.processing, output / "safe", value)
        self.assertEqual(list(output.iterdir()), [])

    def test_negative_control_vendor_rule_is_observable(self):
        # Deliberately remove a routing gate within this synthetic-only invocation.
        # The same assertion must turn red; restored production code stays green.
        original = fs.PATTERNS["vendor"]
        import re
        with patch.dict(fs.PATTERNS, {"vendor": re.compile(r"(?!)")}):
            with self.assertRaises(AssertionError):
                self.assertTrue(self.sheet(self.collect())["fields"]["vendor"]["statements"])
        self.assertIs(fs.PATTERNS["vendor"], original)
        self.assertTrue(self.sheet(self.collect())["fields"]["vendor"]["statements"])

    # ---- DF2307-07: the text of every cited page, for the private console export ----------------------------------------

    def ocr_document(self, agency, native_text, confidence):
        """A synthetic two-page PDF (page 1 native text, page 2 OCR text) enrolled through the real extraction route."""
        try:
            raw = ocr_fixtures.mixed_pdf(native_text)
        except ImportError:
            self.skipTest("pypdf optional parser unavailable")
        subject, source = self.document([], agency, raw=raw)
        out, root = (self.project / (name + str(self.serial)) for name in ("pdf-derived-", "page-ocr-"))
        out.mkdir(mode=0o700)
        root.mkdir(mode=0o700)
        result = routes.extract(source, subject, out, "pdf", timeout=30, ocr_output_root=str(root), ocr_tools=ocr_fixtures.TOOLS,
                                tool_signature="stub-v1", ocr_runner=ocr_fixtures.FakeOCRRunner(confidence=confidence))
        receipt = Path(result["run_path"]) / "extraction.json"
        self.adapter.enroll(original_path=source, receipt_path=receipt, receipt_sha256=sha(receipt.read_bytes()))
        return subject

    def write_citations(self, facts=(), gaps=(), deadlines=(), when=None):
        """This run's three citation files, schema-shaped and hash-bound: (sha, page) pairs; deadlines are (sha, page, date)."""
        shas = sorted({c[0] for c in (*facts, *gaps, *deadlines)})
        items = [{"item_id": ri.item_id(s), "original_sha256": s, "agency_id": "maple", "received_at": (when or {}).get(s),
                  "deadlines": [{"page": p, "date": d} for t, p, d in deadlines if t == s]} for s in shas]
        received = (ri._canon({"schema": ri.SCHEMA, "run_id": "test-run", "items": items}) + "\n").encode()
        proposals = [{"proposal_id": "p-%d" % n, "evidence_kind": "document", "item_id": ri.item_id(s), "page": p,
                      "type": "policy_gap"} for n, (s, p) in enumerate(gaps, 1)] + [{"proposal_id": "p-ledger", "evidence_kind": "ledger_field"}]
        sheet = {"schema": "records-agency-factsheets-v1", "run_id": "test-run", "agencies": [{"agency_id": "maple", "fields": {
            "cost": {"statements": [{"citation": {"document_sha256": s, "page": p}} for s, p in facts]}}}]}
        gap = {"schema": "records-gap-proposals-v1", "run_id": "test-run", "received_items_sha256": sha(received),
               "agencies": [{"requests": [{"proposals": proposals}]}]}
        run = self.project / "run-outputs" / "test-run"
        self.private(run / "agency-factsheets.json", json.dumps(sheet).encode())
        self.private(run / "outcomes" / "gap-proposals.json", json.dumps(gap).encode())
        self.private(run / "outcomes" / "received-items.json", received)

    def test_cited_pages_hold_current_text_route_and_citing_ids(self):
        # Contract: one row per distinct cited (original, page) with its exact current text, route and every citing id.
        # Bugs caught: an uncited page exported, a page cited by all three files listed three times, an OCR page shown as
        # native, or an OCR review state guessed from confidence instead of the recorded decision.
        d, first, second = self.docs[0][0], self.ocr_document("maple", "First native", "42"), self.ocr_document("cedar", "Second native", "90")
        self.write_citations(facts=[(d, 1), (first, 2), (second, 2)], gaps=[(first, 1), (first, 2)],
                             deadlines=[(d, 2, "2041-02-01"), (first, 2, None)], when={d: "2041-01-02T03:04:05Z"})
        doc = cp.read(self.project, "test-run")
        self.assertEqual((doc["state"], doc["not_exported"]), ("ok", []))
        pages = {(r["original_sha256"], r["page"]): r for r in doc["pages"]}
        self.assertEqual(sorted(pages), sorted([(d, 1), (d, 2), (first, 1), (first, 2), (second, 2)]))  # no uncited page
        self.assertEqual(list(pages), sorted(pages))
        source = self.fixture["documents"][0]["pages"]
        self.assertEqual((pages[d, 1]["text"], pages[d, 2]["text"]), (source[0], source[1]))
        for row in pages.values():
            self.assertEqual(row["text_sha256"], hashlib.sha256(row["text"].encode()).hexdigest())
        self.assertEqual([pages[k]["route"] for k in ((d, 1), (first, 1), (first, 2), (second, 2))], ["native", "native", "ocr", "ocr"])
        self.assertEqual({k: (pages[k]["confidence_band"], pages[k]["review_state"]) for k in ((d, 1), (first, 2), (second, 2))},
                         {(d, 1): ("not_applicable", "not_applicable"), (first, 2): ("below_80", "pending"),
                          (second, 2): ("at_least_80", "pending")})  # 90 is not a confirmation
        self.assertEqual(pages[first, 2]["text"], "synthetic OCR text\n")
        self.assertEqual((pages[d, 1]["agency"], pages[d, 1]["received_at"], pages[first, 2]["received_at"]), ("maple", "2041-01-02T03:04:05Z", None))
        self.assertEqual(pages[first, 2]["citations"], [
            {"kind": "deadline", "id": ri.item_id(first) + "/1", "label": "Stated deadline"},
            {"kind": "fact", "id": "maple/cost/2", "label": "cost"}, {"kind": "gap", "id": "p-2", "label": "policy_gap"}])
        self.assertEqual(pages[d, 2]["citations"], [{"kind": "deadline", "id": ri.item_id(d) + "/1", "label": "2041-02-01"}])
        reviews = self.project / "ocr-review-v1" / "reviews.jsonl"
        with run_stages.snapshot_ro(self.database) as con:
            ocr_review.mark(con, reviews, second, 2, "confirmed", "tester")
            ocr_review.mark(con, reviews, first, 2, "rejected", "tester")
        pages = {(r["original_sha256"], r["page"]): r for r in cp.read(self.project, "test-run")["pages"]}
        self.assertEqual((pages[second, 2]["review_state"], pages[first, 2]["review_state"]), ("confirmed", "rejected"))

    def test_cited_pages_bounds_are_counted_never_dropped(self):
        # Contract: a page over a bound is listed in not_exported with its reason; text is never truncated and the
        # serialized model never exceeds the total. Bug caught: bytes counted as characters, or a silent drop.
        d, (multi, _) = self.docs[0][0], self.document(["é" * 40], "empty")  # 40 characters, 80 UTF-8 bytes
        self.write_citations(facts=[(d, 1), (d, 2)], gaps=[(multi, 1)])
        full = cp.read(self.project, "test-run")
        keys = sorted([(d, 1), (d, 2), (multi, 1)])
        self.assertEqual([(r["original_sha256"], r["page"]) for r in full["pages"]], keys)

        def run(**limits):
            with patch.multiple(cp, **limits):
                doc = cp.read(self.project, "test-run")
            return (doc, [(r["original_sha256"], r["page"]) for r in doc.get("pages", [])],
                    {(r["original_sha256"], r["page"]): r["reason"] for r in doc.get("not_exported", [])})
        _, kept, omitted = run(MAX_PAGE_BYTES=80)
        self.assertEqual((kept, omitted), ([(multi, 1)], {(d, 1): "page_bytes", (d, 2): "page_bytes"}))
        _, kept, omitted = run(MAX_PAGE_BYTES=79)
        self.assertEqual((kept, omitted), ([], dict.fromkeys(keys, "page_bytes")))
        _, kept, omitted = run(MAX_PAGES=1)
        self.assertEqual((kept, omitted), (keys[:1], dict.fromkeys(keys[1:], "page_count")))
        size = len(cp.encode(full))
        self.assertEqual(run(MAX_TOTAL_BYTES=size)[0], full)
        doc, kept, omitted = run(MAX_TOTAL_BYTES=size - 1)
        self.assertLessEqual(len(cp.encode(doc)), size - 1)
        self.assertEqual(sorted(kept + list(omitted)), keys)
        self.assertTrue(omitted and set(omitted.values()) == {"total_bytes"})
        self.assertEqual(run(MAX_TOTAL_BYTES=50)[0], {"state": "unavailable", "code": "cited_pages_metadata_bound"})

    def test_cited_pages_unreadable_unit_or_input_is_unavailable_never_partial(self):
        # Contract: a unit or input that cannot be checked gives state unavailable with a stable code, in the export too; only
        # a readable input with no page citations is an empty ok. Bug caught: an empty list or partial text on a read failure.
        d = self.docs[0][0]
        self.write_citations()
        empty = cp.read(self.project, "test-run")
        self.assertEqual((empty["state"], empty["pages"], empty["not_exported"]), ("ok", [], []))
        gap = self.project / "run-outputs" / "test-run" / "outcomes" / "gap-proposals.json"
        for label, mutate, code in (("page without a unit", lambda: self.write_citations(facts=[(d, 99)]), "cited_pages_unit"),
                                    ("missing input", gap.unlink, "cited_pages_unreadable")):
            with self.subTest(label):
                self.write_citations(facts=[(d, 1)])
                mutate()
                self.assertEqual(cp.read(self.project, "test-run"), {"state": "unavailable", "code": code})
        self.write_citations(facts=[(d, 1)])
        with store.ledger(self.database, readonly=True) as con:
            path = Path(con.execute("SELECT derived_path FROM units WHERE original_sha256=? ORDER BY legacy_ordinal", (d,)).fetchone()[0])
        path.chmod(0o600)
        path.write_bytes(b"corrupt")
        self.assertEqual(cp.read(self.project, "test-run"), {"state": "unavailable", "code": "artifact_hash_mismatch"})
        with tempfile.TemporaryDirectory() as dest:
            os.chmod(dest, 0o700)
            console_export.build(str(self.project), "test-run", dest)
            raw = Path(dest, "console-model", "cited-pages.json").read_bytes()
        self.assertEqual(json.loads(raw), {"schema": cp.SCHEMA, "source_project_id": approval_queue._project_id(self.project),
                                           "state": "unavailable", "code": "artifact_hash_mismatch"})

    def test_export_lists_cited_pages_with_its_hash_and_the_snapshot_reads_it(self):
        d = self.docs[0][0]
        self.write_citations(facts=[(d, 1)], deadlines=[(d, 2, None)])
        expected, pid = cp.read(self.project, "test-run"), approval_queue._project_id(self.project)
        with tempfile.TemporaryDirectory() as dest:
            os.chmod(dest, 0o700)
            manifest = console_export.build(str(self.project), "test-run", dest)
            raw = Path(dest, "console-model", "cited-pages.json").read_bytes()
            self.assertEqual(manifest["files"]["console-model/cited-pages.json"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(json.loads(Path(dest, "EXPORT.json").read_text())["files"], manifest["files"])
            self.assertEqual(raw, cp.encode(expected))  # the staged copy gives the live answer, in the one serialization
            source = console_snapshot.SnapshotSource(console_snapshot.Pin(dest, "test-run", pid, "name", "t"))
            self.assertEqual(source.cited_pages("test-run"), expected)
        self.assertEqual(console_snapshot.LiveSource(str(self.project)).cited_pages("test-run"), expected)
        self.assertEqual([r["page"] for r in expected["pages"]], [1, 2])


if __name__ == "__main__":
    unittest.main()
