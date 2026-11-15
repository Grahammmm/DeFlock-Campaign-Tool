"""DF2007-11: the e2e proof lists every original extract did not read and fails on tool capability gaps."""
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool.records import e2e_proof, native_extract, run as records_run
from tests.records.test_e2e_proof import HAVE_PARSERS, pdf_email, png_scan
from tests.records.test_run_slice import synthetic_email

SENTINEL = "SENTINEL-private-subject-and-filename"
# Stand-in OCR tool map (absolute paths, never executed): the test must not depend on tesseract/poppler being installed.
TOOLS = ({"tesseract": {"path": "/usr/bin/tesseract", "version": "stub"},
          "pdftoppm": {"path": "/usr/bin/pdftoppm", "version": "stub"}}, "stub")


def digest(seed):
    return hashlib.sha256(seed.encode()).hexdigest()


def doc(seed, form, outcome, **extra):
    return {"original_sha256": digest(seed), "form": form, "outcome": outcome, **extra}


class UnreadBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="e2e-unread-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        content = self.tmp / "campaign" / "content"
        content.mkdir(parents=True)
        (content / "agencies.json").write_text(json.dumps(
            [{"agency_id": "ca-agency", "name": "Agency", "profile": {"status": "Records requested", "summary": "s"}}]))
        self.campaign = self.tmp / "campaign"
        self.message = self.tmp / "m.eml"
        self.original = synthetic_email()
        self.message.write_bytes(self.original)

    def eml(self, name, raw):
        path = self.tmp / name
        path.write_bytes(raw)
        return path

    def replay_real(self, message, label, **kwargs):
        return e2e_proof.replay(message, self.tmp / "out", self.campaign, label=label, **kwargs)

    def replay_listed(self, label, documents, line=None):
        """Replay with the real stages, but replace extract's per-original list with ``documents``.

        Extract really runs first, so the ledger and every later stage stay genuine. With ``line`` set the extract
        stage reports that failure instead and leaves no list, as a stage that died before classifying would.
        """
        real = e2e_proof._stage

        def stage(argv):
            code, got = real(argv)
            if argv[0] != "extract":
                return code, got
            project, run_id = Path(argv[argv.index("--project") + 1]), argv[argv.index("--run-id") + 1]
            target = project / "processing-v1" / "runs" / ("extract-%s.json" % native_extract.safe_run_name(run_id))
            if line is not None:
                target.unlink()
                return 1, line
            native_extract.write_private_list(project / "processing-v1", run_id, documents)
            held = {}
            for d in documents:
                held[d["outcome"]] = held.get(d["outcome"], 0) + 1
            unread = any(d["outcome"] != "EXTRACTED" for d in documents)
            return 0, {"status": "warn" if unread else "ok", "error_code": "EXTRACT_DOCUMENT_FAILURES" if unread else None,
                       "counts": {}, "outcomes": [{"code": k, "count": v} for k, v in sorted(held.items())]}
        with mock.patch.object(e2e_proof, "_stage", stage):
            return e2e_proof.replay(self.message, self.tmp / "out", self.campaign, label=label)

    def sandbox(self, label):
        return self.tmp / "out" / label

    def list_path(self, label, report):
        return (self.sandbox(label) / "project" / "processing-v1" / "runs"
                / ("extract-%s.json" % native_extract.safe_run_name(report["run_id"])))


class RealReplayTests(UnreadBase):
    def no_pillow(self):
        real = native_extract._importable
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(
            native_extract, "_importable", side_effect=lambda name: False if name == "PIL.Image" else real(name)))
        stack.enter_context(mock.patch.object(records_run, "local_ocr_tools", return_value=TOOLS))
        return stack

    def test_picture_without_pillow_is_fail_and_listed(self):
        picture = self.eml("pic.eml", synthetic_email(attachment=png_scan(), filename="scan.png"))
        with self.no_pillow():
            report = self.replay_real(picture, "pic", ocr=True)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = e2e_proof.main(["--message", str(picture), "--campaign-root", str(self.campaign),
                                       "--out", str(self.tmp / "out"), "--label", "pic-cli", "--ocr"])
        self.assertEqual(report["result"], "FAIL")
        self.assertIn({"stage": "extract", "why": "extract:PARSER_MISSING"}, report["manual_steps"])
        png = hashlib.sha256(png_scan()).hexdigest()
        self.assertEqual(report["unread"], [{"sha256_prefix": png[:12], "kind": "png", "outcome": "PARSER_MISSING"}])
        markdown = (self.sandbox("pic") / "proof-report.md").read_text()
        self.assertIn("extract:PARSER_MISSING", markdown)
        self.assertIn("| %s | png | PARSER_MISSING | - |" % png[:12], markdown)
        self.assertEqual(json.loads((self.sandbox("pic") / "proof-report.json").read_text())["unread"], report["unread"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["unread"], report["unread"])

    @unittest.skipUnless(HAVE_PARSERS, "UNSUPPORTED_RUNTIME: pypdf/openpyxl not in this interpreter")
    def test_corrupt_pdf_is_agency_side_pass(self):
        bad = b"%PDF-1.4\nnot a document"
        corrupt = self.eml("bad.eml", synthetic_email(attachment=bad, filename="bad.pdf"))
        report = self.replay_real(corrupt, "bad")
        self.assertEqual((report["result"], report["manual_steps"]), ("PASS", []), report["stages"])
        self.assertEqual(report["unread"], [{"sha256_prefix": hashlib.sha256(bad).hexdigest()[:12], "kind": "pdf",
                                             "outcome": "CORRUPT_DOCUMENT"}])
        markdown = (self.sandbox("bad") / "proof-report.md").read_text()
        self.assertIn("| %s | pdf | CORRUPT_DOCUMENT | agency-side |" % report["unread"][0]["sha256_prefix"], markdown)
        self.assertIn("Result: **PASS**", markdown)

    @unittest.skipUnless(HAVE_PARSERS, "UNSUPPORTED_RUNTIME: pypdf/openpyxl not in this interpreter")
    def test_clean_text_pdf_has_empty_unread(self):
        report = self.replay_real(self.eml("pdf.eml", pdf_email()), "clean")
        self.assertEqual((report["result"], report["manual_steps"], report["unread"]), ("PASS", [], []))
        self.assertEqual([s["stage"] for s in report["stages"]], [
            "arrival_trigger", "save_mail", "extract", "digest_gates", "outputs", "next_steps", "telegram_text", "console"])
        self.assertEqual(set(report), {"schema", "run_id", "sandbox", "message_sha256", "sent", "published", "stages",
                                       "manual_steps", "total_seconds", "result", "ocr", "unread"})
        markdown = (self.sandbox("clean") / "proof-report.md").read_text()
        self.assertIn("Unread originals", markdown)
        self.assertIn("\n- none\n", markdown.split("Unread originals", 1)[1])

    def test_existing_output_refusal_and_repeat_do_not_mutate_sources(self):
        first = self.replay_listed("one", [doc("a", "pdf", "CORRUPT_DOCUMENT")])

        def tree():
            return sorted((str(p.relative_to(self.tmp)), p.read_bytes()) for p in (self.tmp / "out").rglob("*")
                          if p.is_file())
        before = tree()
        with self.assertRaises(e2e_proof.ProofError) as caught:
            self.replay_listed("one", [doc("a", "pdf", "CORRUPT_DOCUMENT")])
        self.assertEqual(caught.exception.code, "OUT_EXISTS")
        self.assertEqual(tree(), before)
        self.assertEqual(self.message.read_bytes(), self.original)
        second = self.replay_listed("two", [doc("a", "pdf", "CORRUPT_DOCUMENT")])
        self.assertEqual(first["unread"], second["unread"])


class PolicyMatrixTests(UnreadBase):
    def test_decoder_and_workbook_capability_holds_fail(self):
        for code in ("DECODER_NEEDED", "WORKBOOK_POLICY_REQUIRED", "PARSER_MISSING",
                     "AWAITING_OCR", "EXTRACT_ERROR", "EXTRACT_TIMEOUT", "EXTRACT_NOT_ACCEPTED"):
            with self.subTest(code=code, held=True):
                report = self.replay_listed("hold-" + code, [doc("x1", "bin", code), doc("x2", "xlsx", code),
                                                           doc("x3", "pdf", "EXTRACTED")])
                self.assertEqual(report["result"], "FAIL")
                self.assertEqual([m for m in report["manual_steps"] if m["why"].startswith("extract:")],
                                 [{"stage": "extract", "why": "extract:" + code}])
                self.assertEqual({u["outcome"] for u in report["unread"]}, {code})
                self.assertEqual(len(report["unread"]), 2)
            with self.subTest(code=code, held=False):
                report = self.replay_listed("none-" + code, [doc("x3", "pdf", "EXTRACTED")])
                self.assertEqual((report["result"], report["manual_steps"], report["unread"]), ("PASS", [], []))

    def test_incomplete_document_is_listed_without_tool_failure(self):
        report = self.replay_listed("inc", [doc("i1", "pdf", "EXTRACT_INCOMPLETE"), doc("i2", "pdf", "CORRUPT_DOCUMENT")])
        self.assertEqual((report["result"], report["manual_steps"]), ("PASS", []))
        self.assertEqual({u["outcome"] for u in report["unread"]}, {"EXTRACT_INCOMPLETE", "CORRUPT_DOCUMENT"})
        markdown = (self.sandbox("inc") / "proof-report.md").read_text()
        self.assertEqual(markdown.count("| agency-side |"), 2)
        mixed = self.replay_listed("mix", [doc("i1", "pdf", "EXTRACT_INCOMPLETE"), doc("m1", "png", "PARSER_MISSING")])
        self.assertEqual(mixed["result"], "FAIL")
        self.assertEqual([m["why"] for m in mixed["manual_steps"]], ["extract:PARSER_MISSING"])
        self.assertEqual(len(mixed["unread"]), 2)
        markdown = (self.sandbox("mix") / "proof-report.md").read_text()
        self.assertEqual(markdown.count("| agency-side |"), 1)

    def test_unfinished_codes_are_the_old_four_plus_three(self):
        self.assertEqual(e2e_proof.EXTRACT_UNFINISHED, (
            "AWAITING_OCR", "EXTRACT_ERROR", "EXTRACT_TIMEOUT", "EXTRACT_NOT_ACCEPTED",
            "PARSER_MISSING", "DECODER_NEEDED", "WORKBOOK_POLICY_REQUIRED"))
        self.assertEqual(e2e_proof.AGENCY_SIDE, ("CORRUPT_DOCUMENT", "EXTRACT_INCOMPLETE"))

    def test_inventory_missing_malformed_or_wrong_run_never_claims_clean(self):
        good = [doc("g", "pdf", "CORRUPT_DOCUMENT")]
        schema = "records-extract-outcomes-v1"
        cases = {
            "missing": None,
            "invalid_json": "{not json " + SENTINEL,
            "wrong_schema": {"schema": "other", "run_id": "@RUN", "documents": good},
            "wrong_run": {"schema": schema, "run_id": "run_other", "documents": good},
            "documents_not_list": {"schema": schema, "run_id": "@RUN", "documents": {"a": SENTINEL}},
            "document_not_dict": {"schema": schema, "run_id": "@RUN", "documents": [SENTINEL]},
            "bad_hash": {"schema": schema, "run_id": "@RUN", "documents": [dict(good[0], original_sha256="ABC" + SENTINEL)]},
            "bad_outcome": {"schema": schema, "run_id": "@RUN", "documents": [dict(good[0], outcome=SENTINEL)]},
            "bad_form": {"schema": schema, "run_id": "@RUN", "documents": [dict(good[0], form="../" + SENTINEL)]},
        }
        for name, body in cases.items():
            with self.subTest(case=name):
                label = "inv-" + name

                real = e2e_proof._stage

                def stage(argv, body=body, real=real):
                    code, got = real(argv)  # real extract, then damage its list the way this case describes
                    if argv[0] == "extract":
                        project, run_id = Path(argv[argv.index("--project") + 1]), argv[argv.index("--run-id") + 1]
                        target = project / "processing-v1" / "runs" / ("extract-%s.json" % native_extract.safe_run_name(run_id))
                        if body is None:
                            target.unlink()
                        else:
                            target.write_text(body if isinstance(body, str) else json.dumps(body).replace("@RUN", run_id))
                    return code, got
                with mock.patch.object(e2e_proof, "_stage", stage):
                    report = e2e_proof.replay(self.message, self.tmp / "out", self.campaign, label=label)
                self.assertEqual(report["result"], "FAIL")
                # Later stages that read the same list may add their own reasons; the proof's own one must be there.
                self.assertIn({"stage": "extract", "why": e2e_proof.EXTRACT_LIST_UNAVAILABLE}, report["manual_steps"])
                self.assertEqual(report["unread"], [])
                for name_ in ("proof-report.json", "proof-report.md"):
                    self.assertNotIn(SENTINEL, (self.sandbox(label) / name_).read_text())

    def test_failed_extract_keeps_its_own_reason_and_exposes_empty_unread(self):
        report = self.replay_listed("failed", None, line={"status": "fail", "error_code": "EXTRACT_RUN_FAILED", "counts": {}})
        extract = next(s for s in report["stages"] if s["stage"] == "extract")
        self.assertEqual((extract["status"], extract["code"]), ("fail", "EXTRACT_RUN_FAILED"))
        self.assertEqual(report["result"], "FAIL")
        self.assertNotIn(e2e_proof.EXTRACT_LIST_UNAVAILABLE, [m["why"] for m in report["manual_steps"]])
        self.assertIn({"stage": "extract", "why": "EXTRACT_RUN_FAILED"}, report["manual_steps"])
        self.assertEqual(report["unread"], [])

    def test_unread_is_original_scoped_deterministic_and_private(self):
        shared = "0123456789ab"
        first, second = "%s%s" % (shared, "f" * 52), "%s%s" % (shared, "0" * 52)
        documents = [
            {"original_sha256": first, "form": "png", "outcome": "PARSER_MISSING", "filename": SENTINEL + ".png",
             "detail": SENTINEL, "path": "/private/" + SENTINEL},
            {"original_sha256": second, "form": "pdf", "outcome": "CORRUPT_DOCUMENT", "subject": SENTINEL},
            doc("eml", "eml", "DEFERRED_RUN_BUDGET", account=SENTINEL),
            doc("held", None, "PRESERVE_PENDING"),
            doc("ok", "pdf", "EXTRACTED", filename=SENTINEL),
        ]
        report = self.replay_listed("scoped", documents)
        rows = report["unread"]
        self.assertEqual(len(rows), 4)  # EXTRACTED excluded; the shared prefix keeps both rows
        full = [d["original_sha256"] for d in documents if d["outcome"] != "EXTRACTED"]
        by_row = {(r["sha256_prefix"], r["outcome"]): r for r in rows}
        self.assertEqual(len(by_row), 4)
        self.assertEqual([r["sha256_prefix"] for r in rows], [h[:12] for h in sorted(full)])
        self.assertEqual({r["outcome"] for r in rows}, {"PARSER_MISSING", "CORRUPT_DOCUMENT", "DEFERRED_RUN_BUDGET",
                                                        "PRESERVE_PENDING"})
        self.assertIn("unknown", {r["kind"] for r in rows})
        self.assertIn("eml", {r["kind"] for r in rows})
        for row in rows:
            self.assertEqual(set(row), {"sha256_prefix", "kind", "outcome"})
        # Only the seven capability codes fail the proof: PRESERVE_PENDING/DEFERRED_RUN_BUDGET stay listed only.
        self.assertEqual([m["why"] for m in report["manual_steps"]], ["extract:PARSER_MISSING"])
        again = self.replay_listed("scoped2", list(reversed(documents)))
        self.assertEqual(again["unread"], rows)
        for label in ("scoped", "scoped2"):
            for name in ("proof-report.json", "proof-report.md"):
                self.assertNotIn(SENTINEL, (self.sandbox(label) / name).read_text())

    def test_render_markdown_marks_only_the_two_agency_codes(self):
        report = self.replay_listed("md", [doc("a", "pdf", "CORRUPT_DOCUMENT"), doc("b", "pdf", "EXTRACT_TIMEOUT")])
        lines = (self.sandbox("md") / "proof-report.md").read_text().splitlines()
        rows = {l.split("|")[3].strip(): l.split("|")[4].strip() for l in lines if l.startswith("| ") and "pdf" in l}
        self.assertEqual(rows, {"CORRUPT_DOCUMENT": "agency-side", "EXTRACT_TIMEOUT": "-"})


if __name__ == "__main__":
    unittest.main()
