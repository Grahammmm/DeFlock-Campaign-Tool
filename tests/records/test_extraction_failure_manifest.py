"""REC-AUTO-2: the exact-run, metadata-only extract failure manifest (synthetic fixtures only)."""
import hashlib
import json
import os
from pathlib import Path
import smtplib
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.request

from campaign_tool.records import extraction_failures as ef
from campaign_tool.records import native_extract, run_stages
from tests.records.test_native_extract import (HAVE_PARSERS, ExtractStageCase, corrupt_pdf, run_main, scanned_pdf,
                                               tiny_pdf)

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "extract_failure_manifest.py"
SHA = "a" * 64


def body(*rows, run_id="run-x"):
    return {"schema": ef.OUTCOMES_SCHEMA, "run_id": run_id, "documents": list(rows)}


def row(outcome, **extra):
    return {"original_sha256": SHA, "form": "pdf", "outcome": outcome, **extra}


class ProjectionTests(unittest.TestCase):
    def test_every_non_extracted_row_is_listed_and_counts_reconcile(self):
        rows = [row(o) for o in ef.OUTCOME_CODES] + [row("EXTRACTED")] * 3
        manifest = ef.build_manifest(body(*rows))
        self.assertEqual(manifest["schema"], "records-extract-failures-v1")
        self.assertEqual(manifest["documents_listed"], len(ef.OUTCOME_CODES) + 3)
        self.assertEqual(len(manifest["documents"]), len(ef.OUTCOME_CODES) - 1)
        self.assertEqual(sum(manifest["by_outcome"].values()), len(manifest["documents"]))
        self.assertEqual(sum(manifest["by_reason"].values()), len(manifest["documents"]))
        self.assertNotIn("EXTRACTED", manifest["by_outcome"])

    def test_legacy_error_and_not_accepted_without_detail_are_named_holds(self):
        manifest = ef.build_manifest(body(row("EXTRACT_ERROR"), row("EXTRACT_NOT_ACCEPTED")))
        self.assertEqual(manifest["by_reason"], {"legacy_failure_detail_unavailable": 2})
        self.assertEqual({d["disposition"] for d in manifest["documents"]}, {"hold"})

    def test_dispositions(self):
        got = {d["outcome"]: d["disposition"] for d in ef.build_manifest(body(
            row("DEFERRED_RUN_BUDGET"), row("PRESERVE_PENDING"), row("AWAITING_OCR"),
            row("EXTRACT_TIMEOUT"), row("CORRUPT_DOCUMENT"),
            row("EXTRACT_ERROR", refusal_code="unlisted", refusal_source="exception")))["documents"]}
        self.assertEqual(got, {"DEFERRED_RUN_BUDGET": "deferred", "PRESERVE_PENDING": "deferred",
                               "AWAITING_OCR": "hold", "EXTRACT_TIMEOUT": "retry", "CORRUPT_DOCUMENT": "hold",
                               "EXTRACT_ERROR": "retry"})

    def test_unknown_newline_html_and_path_like_codes_map_to_unlisted(self):
        bad = ["secret_token_here", "a\nb", "<b>x</b>", "/etc/passwd", "../x", "page_eligibility_unresolved\n", 7, None]
        for value in bad:
            with self.subTest(value=value):
                doc = ef.build_manifest(body(row("EXTRACT_NOT_ACCEPTED", refusal_code=value,
                                                 refusal_source="binding_error")))["documents"][0]
                self.assertEqual(doc["refusal_code"], "unlisted")
                self.assertEqual(doc["reason_code"], "unlisted")
        doc = ef.build_manifest(body(row("EXTRACT_INCOMPLETE", issue_codes=["good_looking_but_unknown", "page_raster_bound"])))["documents"][0]
        self.assertEqual(doc["issue_codes"], ["page_raster_bound", "unlisted"])
        self.assertEqual(doc["reason_code"], "page_raster_bound")

    def test_known_producer_and_accept_status_codes_pass(self):
        for code in ("page_eligibility_unresolved", "unresolved_extraction_evidence", "aggregate_evidence_bound",
                     "accept_status_refused", "accept_status_blocked"):
            doc = ef.build_manifest(body(row("EXTRACT_NOT_ACCEPTED", refusal_code=code, accept_status="refused",
                                             refusal_source="accept_status")))["documents"][0]
            self.assertEqual(doc["reason_code"], code)
        doc = ef.build_manifest(body(row("EXTRACT_NOT_ACCEPTED", refusal_code="accept_status_bogus",
                                         accept_status="bogus", refusal_source="nowhere")))["documents"][0]
        self.assertEqual((doc["refusal_code"], doc["accept_status"], doc["refusal_source"]),
                         ("unlisted", None, "unlisted"))

    def test_arbitrary_keys_and_free_text_are_never_copied(self):
        sentinel = "SENTINEL-/home/user/secret.pdf subject: leak"
        doc = ef.build_manifest(body(row("EXTRACT_ERROR", stderr=sentinel, path=sentinel, filename=sentinel,
                                         header=sentinel, error=sentinel, form="../../x")))
        self.assertNotIn("SENTINEL", json.dumps(doc))
        self.assertIsNone(doc["documents"][0]["form"])

    def test_page_progress_is_validated_not_double_counted(self):
        good = ef.build_manifest(body(row("DEFERRED_RUN_BUDGET", ocr_pages_done=3, ocr_pages_pending=[4, 5],
                                          issue_codes=["extraction_timeout"])))
        doc = good["documents"][0]
        self.assertEqual((doc["ocr_pages_done"], doc["ocr_pages_pending"], good["by_outcome"]),
                         (3, [4, 5], {"DEFERRED_RUN_BUDGET": 1}))
        for done, pending in ((-1, [1]), (True, [1]), ("3", [1]), (3, "x")):
            doc = ef.build_manifest(body(row("DEFERRED_RUN_BUDGET", ocr_pages_done=done, ocr_pages_pending=pending)))["documents"][0]
            self.assertNotIn("ocr_pages_done", doc)
        doc = ef.build_manifest(body(row("DEFERRED_RUN_BUDGET", ocr_pages_done=1,
                                         ocr_pages_pending=[2, -4, "x", True, 3.5])))["documents"][0]
        self.assertEqual(doc["ocr_pages_pending"], [2])

    def test_malformed_input_is_refused(self):
        for bad in (None, [], {"documents": "x"}, {"documents": [1]}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    ef.build_manifest(bad)

    def test_empty_run(self):
        manifest = ef.build_manifest(body())
        self.assertEqual((manifest["documents_listed"], manifest["documents"], manifest["by_outcome"]), (0, [], {}))


class WriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rec2-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "runs").mkdir(mode=0o700)

    def test_failures_prefix_run_id_cannot_collide_with_manifest_name(self):
        out = native_extract.write_private_list(self.root, "failures-x", [])
        self.assertEqual(out["run_id"], "failures-x")
        self.assertEqual(sorted(p.name for p in (self.root / "runs").iterdir()), ["extract-run-failures-x.json"])
        native_extract.write_failure_manifest(self.root, "failures-x", out)
        self.assertTrue((self.root / "runs" / "extract-failures-run-failures-x.json").is_file())

    def test_manifest_is_private_and_repeated_run_id_replaces_it(self):
        for rows in ([row("EXTRACT_ERROR")], []):
            document = native_extract.write_private_list(self.root, "r1", rows)
            native_extract.write_failure_manifest(self.root, "r1", document)
        target = self.root / "runs" / "extract-failures-r1.json"
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(json.loads(target.read_text())["documents"], [])
        self.assertEqual(sorted(p.name for p in (self.root / "runs").iterdir()),
                         ["extract-failures-r1.json", "extract-r1.json"])

    def test_write_failure_is_visible_and_leaves_no_partial_json(self):
        document = native_extract.write_private_list(self.root, "r2", [row("EXTRACT_ERROR")])
        with mock.patch("os.replace", side_effect=OSError("disk full /private/x")):
            with self.assertRaises(native_extract.ManifestWriteError) as caught:
                native_extract.write_failure_manifest(self.root, "r2", document)
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(sorted(p.name for p in (self.root / "runs").iterdir()), ["extract-r2.json"])

    def test_stage_reports_a_fixed_failure_when_the_manifest_cannot_be_written(self):
        class Args:
            project = str(self.root)
            timeout, run_id, max_documents, document_timeout = 60, "r3", 10, None
            workbook_policy = ocr = ocr_vision_config = ocr_page_timeout = ocr_vision_timeout = None

        with mock.patch.object(native_extract, "run", side_effect=native_extract.ManifestWriteError("x")):
            stage, code = run_stages.stage_extract(Args)
        self.assertEqual(code, 1)
        self.assertEqual((stage["status"], stage["error_code"]), ("fail", "EXTRACT_FAILURE_MANIFEST_UNWRITABLE"))


@unittest.skipUnless(HAVE_PARSERS, "UNSUPPORTED_RUNTIME: pypdf/openpyxl not in this interpreter")
class ExactRunScriptTests(ExtractStageCase):
    @property
    def attachments(self):
        return (("policy.pdf", tiny_pdf("Synthetic policy text.")), ("scan.pdf", scanned_pdf()),
                ("broken.pdf", corrupt_pdf()), ("blob.bin", b"\x00\x01\x02 not a known format"))

    def stage_done(self, subject):
        return self.query("SELECT status FROM stage_state WHERE original_sha256=? AND stage='extract'", (subject,))[0][0]

    def runs(self):
        return self.processing / "runs"

    def script(self, *extra):
        done = subprocess.run([sys.executable, "-B", str(SCRIPT), "--project", str(self.project), *extra],
                              capture_output=True, text=True, timeout=60)
        return done.returncode, json.loads(done.stdout)

    def test_stage_writes_a_manifest_that_accounts_for_every_attachment_and_leaks_nothing(self):
        code, parsed, raw = self.stage()
        self.assertContract(code, parsed, raw)
        target = self.runs() / "extract-failures-run-t11k.json"
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        manifest = json.loads(target.read_text())
        outcomes = json.loads((self.runs() / "extract-run-t11k.json").read_text())
        self.assertEqual(manifest["documents_listed"], len(outcomes["documents"]))
        self.assertEqual(sum(self.outcomes(parsed).values()) - self.outcomes(parsed)["EXTRACTED"],
                         len(manifest["documents"]))
        reasons = {d["outcome"]: d["reason_code"] for d in manifest["documents"]}
        self.assertEqual(reasons["AWAITING_OCR"], "ocr_runtime_unconfigured")
        self.assertIn("CORRUPT_DOCUMENT", reasons)
        self.assertNotIn("unlisted", manifest["by_reason"])
        self.assertNotIn(self.temp.name, target.read_text())
        for name in ("policy.pdf", "scan.pdf", "broken.pdf", "blob.bin"):
            self.assertNotIn(name, target.read_text())

    def test_exact_run_selection_default_shape_and_manifest_exclusion(self):
        self.assertEqual(self.stage()[0], 0)
        second = run_main(["extract", "--project", str(self.project), "--timeout", "120", "--run-id", "run-later"])
        self.assertEqual(second[0], 0)
        os.utime(self.runs() / "extract-run-later.json", (1, 1))  # the exact run is the OLDER file
        os.utime(self.runs() / "extract-run-t11k.json", (2, 2))
        newest = self.runs() / "extract-failures-run-t11k.json"
        os.utime(newest, (3_000_000_000, 3_000_000_000))  # a manifest is the newest file of all
        status, default = self.script()
        self.assertEqual(status, 0)
        self.assertEqual(default["extract_list_sha256"],
                         hashlib.sha256((self.runs() / "extract-run-t11k.json").read_bytes()).hexdigest())
        self.assertTrue(all("reason_code" not in d and "disposition" not in d for d in default["documents"]),
                        "default output keeps the legacy FAILED-only shape")
        self.assertEqual(set(default) , {"status", "extract_list_sha256", "documents_listed", "by_code", "documents"})
        self.assertNotIn("AWAITING_OCR", default["by_code"])
        status, exact = self.script("--run-id", "run-later", "--include-holds")
        self.assertEqual(status, 0)
        self.assertEqual(exact["run_id"], "run-later")
        self.assertEqual(exact["extract_list_sha256"],
                         hashlib.sha256((self.runs() / "extract-run-later.json").read_bytes()).hexdigest())
        self.assertEqual(exact["by_outcome"].get("AWAITING_OCR"), 1)
        self.assertTrue(all(d["reason_code"] and d["disposition"] for d in exact["documents"]))
        self.assertEqual(sum(exact["by_reason"].values()), len(exact["documents"]))

    def test_bad_run_ids_are_refused(self):
        self.assertEqual(self.stage()[0], 0)
        for run_id, expected in (("../run-t11k", "run_id_invalid"), ("a/b", "run_id_invalid"), ("..", "run_id_invalid"),
                                 ("run\nx", "run_id_invalid"), ("", "run_id_invalid"), ("nope", "run_id_missing"),
                                 ("failures-run-t11k", "run_id_missing")):
            with self.subTest(run_id=run_id):
                status, out = self.script("--run-id", run_id)
                self.assertEqual((status, out), (1, {"status": "refused", "code": expected}))
        # An outcome list whose body names a different run is a mismatch, never silently used.
        source = json.loads((self.runs() / "extract-run-t11k.json").read_text())
        source["run_id"] = "someone-else"
        (self.runs() / "extract-run-copy.json").write_text(json.dumps(source))
        self.assertEqual(self.script("--run-id", "run-copy"), (1, {"status": "refused", "code": "run_id_mismatch"}))

    def test_held_rerun_keeps_named_reasons_and_does_not_clear_alerts(self):
        self.assertEqual(self.stage()[0], 0)
        before = self.query("SELECT key,count,state FROM alerts ORDER BY key")
        self.assertTrue(before)
        code, parsed, raw = run_main(["extract", "--project", str(self.project), "--timeout", "120",
                                      "--run-id", "run-held"])
        self.assertContract(code, parsed, raw)
        after = self.query("SELECT key,state FROM alerts ORDER BY key")
        self.assertEqual([(k, s) for k, _, s in before], after, "no alert cleared, none added")
        manifest = json.loads((self.runs() / "extract-failures-run-held.json").read_text())
        reasons = {d["outcome"]: d["reason_code"] for d in manifest["documents"]}
        self.assertEqual(reasons["CORRUPT_DOCUMENT"], "extraction_error")
        self.assertEqual(reasons["AWAITING_OCR"], "ocr_runtime_unconfigured")
        # The healthy sibling stayed extracted; the failed ones did not abort or undo it.
        sibling = hashlib.sha256(self.payloads["policy.pdf"]).hexdigest()
        self.assertEqual(self.stage_done(sibling), "done")

    def test_no_outbound_side_effect(self):
        def boom(*args, **kwargs):
            raise AssertionError("outbound call during extraction")

        with mock.patch.object(smtplib.SMTP, "__init__", boom), mock.patch.object(smtplib.SMTP_SSL, "__init__", boom), \
                mock.patch.object(urllib.request, "urlopen", boom):
            code, parsed, raw = self.stage()
        self.assertContract(code, parsed, raw)


if __name__ == "__main__":
    unittest.main()
