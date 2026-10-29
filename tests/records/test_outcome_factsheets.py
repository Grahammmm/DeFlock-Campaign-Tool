"""Synthetic-only producer-shaped canonical extraction -> outputs integration."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records import extraction_routes as routes, run_stages
from campaign_tool.records.extraction_ledger import ExtractionLedgerAdapter, canonical, sha
from campaign_tool.records.ledger import store
from campaign_tool.records.outcomes import factsheets as fs

FIXTURE = Path(__file__).parent / "fixtures" / "outcome_factsheets" / "produced.json"


class FactSheetsTests(unittest.TestCase):
    def setUp(self):
        # Keep generated synthetic work inside this clone, outside frozen fixtures.
        workspaces = Path(__file__).resolve().parents[2] / "build" / "outcome-factsheets" / "test-workspaces"
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
        with sqlite3.connect(portal / "portal.sqlite") as con:
            con.execute("CREATE TABLE portal_items(id TEXT)")

    def private(self, path, raw):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(raw)
        path.chmod(0o600)

    def document(self, pages, agency, *, status="typed", role="agency_original", scope="in_scope", locator=None):
        self.serial += 1
        source = self.project / ("original-" + str(self.serial))
        # Synthetic source bytes; no real PDF parser or OCR qualification claim.
        self.private(source, ("SYNTHETIC RECEIPT FIXTURE " + str(self.serial) + "\n" + "\n".join(pages)).encode())
        subject = sha(source.read_bytes())
        with store.ledger(self.database) as con:
            con.execute("INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (subject, source.stat().st_size, None, None, role, scope, str(source), "pending", None, "{}"))
            con.execute("INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?)", (subject, subject, "local", "synthetic", None, None, "fixture", "{}"))
            con.execute("INSERT INTO joins VALUES(?,?,?,?,?,?,?,?)", (subject, subject, agency, None, "fixture_production", "{}", status, None))
            for stage in store.STAGES:
                con.execute("INSERT INTO stage_state VALUES(?,?,?,?,?,?,?,?)", (subject, stage, "pending", None, "fixture", "2041-01-01", "fixture", None))
            con.commit()
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
                self.assertIn(statement["quote"], source_pages[cite["page"] - 1])
                self.assertTrue(all(fs.is_hash(cite[k]) for k in ("document_sha256", "unit_id", "text_sha256", "payload_sha256")))
        self.assertEqual(sheet["fields"]["cost"]["statements"][0]["quote"], "Annual fee: $12,345.67.")
        self.assertEqual(sheet["fields"]["camera_count"]["statements"][0]["quote"], "The department operates 7 ALPR cameras.")

    def test_missing_fields_questions_headings_never_invent_values(self):
        report = self.collect()
        fields = self.sheet(report, "cedar")["fields"]
        for field in ("vendor", "contract", "cost", "camera_count", "camera_locations", "outside_sharing", "policy_in_force", "search_logs"):
            self.assertEqual(fields[field], {"status": "NOT FOUND", "statements": []}, field)
        self.assertTrue(all(f["status"] == "NOT FOUND" for f in self.sheet(report, "empty")["fields"].values()))
        self.assertEqual(len(fields["retention"]["statements"]), 2, "no latest-value inference")
        self.assertEqual(fields["audits"]["statements"][0]["quote"], "No responsive audit records were located.")

    def test_negation_proposal_and_policy_dates_preserved_not_resolved(self):
        report = self.collect()
        fields = self.sheet(report)["fields"]
        self.assertTrue(fields["outside_sharing"]["statements"][0]["quote"].startswith("No outside"))
        self.assertIn("NOT executed", fields["contract"]["statements"][-1]["quote"])
        self.assertEqual(fields["policy_in_force"]["statements"][0]["quote"], "Policy MV-17 is effective 2041-03-17.")
        self.assertIn("not resolved automatically", fs.markdown(report))

    def test_stage_writes_both_formats_snapshot_unchanged_and_readonly_sources(self):
        before = self.tree()
        code, result = self.stage()
        self.assertEqual((code, result["status"], result["counts"]["output"]), (0, "ok", 1))
        output = self.project / "run-outputs" / "test-run"
        # The per-run directory also carries the comparison and request-register pairs.
        own = {"agency-factsheets.json", "agency-factsheets.md"}
        self.assertEqual({p.name for p in output.iterdir()} - {
            "comparison.json", "comparison.md",
            "CURRENT-REQUEST-REGISTER.json", "CURRENT-REQUEST-REGISTER.md"}, own)
        report = json.loads((output / "agency-factsheets.json").read_text())
        text = (output / "agency-factsheets.md").read_text()
        self.assertEqual(text, fs.markdown(report))
        for sheet in report["agencies"]:
            for value in sheet["fields"].values():
                for statement in value["statements"]:
                    cite = statement["citation"]
                    self.assertIn("document sha256 " + cite["document_sha256"] + ", page " + str(cite["page"]), text)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
        for path in output.iterdir():
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
        with patch.object(fs, "read_pinned", wraps=fs.read_pinned) as reader:
            with self.assertRaisesRegex(ValueError, "factsheet_payload_path"):
                self.collect()
        self.assertFalse(any(str(call.args[0]) == "/not-admitted/source" for call in reader.call_args_list))

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


if __name__ == "__main__":
    unittest.main()
