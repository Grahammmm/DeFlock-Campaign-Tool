"""REC-AUTO-11: the e2e-proof replay runs every stage with no manual step and sends nothing."""
import contextlib
import io
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from campaign_tool.records import e2e_proof, native_extract, notify_text, run_stages
from tests.records.test_run_slice import synthetic_email


def png_scan():
    """A tiny synthetic gray PNG with no text layer: an image-only scan the OCR stage has not read yet."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(b"\x00" + bytes((x * 7 + y * 13) % 256 for x in range(16)) for y in range(16))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 16, 8, 0, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


class E2EProofTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="e2e-proof-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        content = self.tmp / "campaign" / "content"
        content.mkdir(parents=True)
        (content / "agencies.json").write_text(json.dumps(
            [{"agency_id": "ca-agency", "name": "Agency", "profile": {"status": "Records requested", "summary": "s"}}]))
        self.campaign = self.tmp / "campaign"
        self.message = self.tmp / "m.eml"
        self.message.write_bytes(synthetic_email())

    def test_replay_passes_every_stage_twice_without_manual_steps(self):
        first = e2e_proof.replay(self.message, self.tmp / "out", self.campaign, label="one")
        second = e2e_proof.replay(self.message, self.tmp / "out", self.campaign, label="two")
        for report in (first, second):
            self.assertEqual(report["result"], "PASS")
            self.assertEqual(report["manual_steps"], [])
            self.assertIs(report["sent"], False)
            self.assertIs(report["published"], False)
            self.assertEqual([s["stage"] for s in report["stages"]], [
                "arrival_trigger", "save_mail", "extract", "digest_gates", "outputs", "next_steps",
                "telegram_text", "console"])
        self.assertTrue((self.tmp / "out" / "one" / "telegram-summary.txt").is_file())
        self.assertTrue((self.tmp / "out" / "one" / "proof-report.md").is_file())

    def test_replay_refuses_existing_output_dir(self):
        e2e_proof.replay(self.message, self.tmp / "out", self.campaign, label="one")
        with self.assertRaises(e2e_proof.ProofError) as caught:
            e2e_proof.replay(self.message, self.tmp / "out", self.campaign, label="one")
        self.assertEqual(caught.exception.code, "OUT_EXISTS")

    def test_message_required(self):
        with self.assertRaises(e2e_proof.ProofError) as caught:
            e2e_proof.pick_message(None, None, 1)
        self.assertEqual(caught.exception.code, "MESSAGE_REQUIRED")

    def _replay_with_outputs_warning(self, code):
        real = e2e_proof._adapter

        def adapter(name, project, run_id, *extra):
            status, got, detail = real(name, project, run_id, *extra)
            return ("warn", code, detail) if name == "outputs" else (status, got, detail)
        with mock.patch.object(e2e_proof, "_adapter", adapter):
            return e2e_proof.replay(self.message, self.tmp / "out", self.campaign, label=code)

    def test_scan_only_attachment_fails_naming_awaiting_ocr(self):
        scan = self.tmp / "scan.eml"
        scan.write_bytes(synthetic_email(attachment=png_scan(), filename="scan.png"))
        report = e2e_proof.replay(scan, self.tmp / "out", self.campaign, label="scan")
        extract = next(s for s in report["stages"] if s["stage"] == "extract")
        self.assertIn({"code": "AWAITING_OCR", "count": 1}, extract["detail"]["outcomes"])
        self.assertEqual(report["result"], "FAIL")
        self.assertIn({"stage": "extract", "why": "extract:AWAITING_OCR"}, report["manual_steps"])
        self.assertIn("extract:AWAITING_OCR", (self.tmp / "out" / "scan" / "proof-report.md").read_text())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = e2e_proof.main(["--message", str(scan), "--campaign-root", str(self.campaign),
                                   "--out", str(self.tmp / "out"), "--label", "scan-cli"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["result"], "FAIL")

    def _extract_argv(self, ocr):
        seen = []

        def stage(argv):
            seen.append(argv)
            return 0, {"status": "ok", "counts": {}, "outcomes": []}
        with mock.patch.object(e2e_proof, "_stage", stage):
            report = e2e_proof.replay(self.message, self.tmp / "out", self.campaign, label="o%d" % ocr, ocr=ocr)
        return next(a for a in seen if a[0] == "extract"), report

    def test_ocr_extract_budget_fits_one_scanned_page(self):
        argv, report = self._extract_argv(True)
        args = run_stages.parser().parse_args(argv)
        self.assertTrue(args.ocr)
        self.assertFalse(run_stages.config_invalid(args))
        # the budget native_extract.run gives a document: min(document_timeout, stage time left - 5)
        budget = min(args.document_timeout, args.timeout - native_extract.DEADLINE_MARGIN - 5)
        self.assertGreaterEqual(budget, 2 * args.ocr_page_timeout)  # pdftoppm child + tesseract child
        self.assertTrue(report["ocr"])
        # the old plan (120 s stage, default document timeout) cut such a page
        self.assertLess(min(native_extract.DEFAULT_DOCUMENT_TIMEOUT, 120 - native_extract.DEADLINE_MARGIN - 5),
                        2 * args.ocr_page_timeout)

    def test_default_extract_has_no_ocr(self):
        argv, report = self._extract_argv(False)
        self.assertNotIn("--ocr", argv)
        self.assertFalse(report["ocr"])

    def test_ocr_flag_reaches_replay(self):
        scan = self.tmp / "scan.eml"
        scan.write_bytes(synthetic_email(attachment=png_scan(), filename="scan.png"))
        with mock.patch.object(e2e_proof, "replay", return_value={"result": "PASS"}) as replay:
            with contextlib.redirect_stdout(io.StringIO()):
                code = e2e_proof.main(["--message", str(scan), "--campaign-root", str(self.campaign),
                                       "--out", str(self.tmp / "o"), "--ocr"])
        self.assertEqual(code, 0)
        self.assertTrue(replay.call_args.kwargs["ocr"])

    def test_outputs_unavailable_warning_is_a_manual_step(self):
        for code in ("OUTPUTS_NEXT_STEPS_UNAVAILABLE", "OUTPUTS_DIGEST_UNRESOLVED"):
            with self.subTest(code=code):
                report = self._replay_with_outputs_warning(code)
                self.assertEqual(report["result"], "FAIL")
                self.assertEqual(report["manual_steps"], [{"stage": "outputs", "why": "outputs:" + code}])

    def test_no_next_steps_warning_stays_a_pass(self):
        with mock.patch.object(e2e_proof.approval_queue, "load_queue", return_value=[]):
            report = e2e_proof.replay(self.message, self.tmp / "out", self.campaign, label="none")
        step = next(s for s in report["stages"] if s["stage"] == "next_steps")
        self.assertEqual((step["status"], step["code"]), ("warn", "NO_NEXT_STEPS_FOR_THIS_MESSAGE"))
        self.assertEqual((report["result"], report["manual_steps"]), ("PASS", []))

    def test_unfinished_work_codes_and_clean_counts(self):
        def extract(**counts):
            return {"stage": "extract", "status": "warn", "code": None, "detail": {
                "outcomes": [{"code": k, "count": v} for k, v in counts.items()]}}
        self.assertEqual(e2e_proof._unfinished_work(extract(EXTRACTED=2, EXTRACT_TIMEOUT=1, EXTRACT_ERROR=1)),
                         ["extract:EXTRACT_ERROR", "extract:EXTRACT_TIMEOUT"])
        self.assertEqual(e2e_proof._unfinished_work(extract(EXTRACT_NOT_ACCEPTED=1)), ["extract:EXTRACT_NOT_ACCEPTED"])
        self.assertEqual(e2e_proof._unfinished_work(extract(EXTRACTED=3, EXTRACT_ERROR=0)), [])
        outputs = {"stage": "outputs", "status": "warn", "code": "OUTPUTS_REGISTER_UNAVAILABLE", "detail": {}}
        self.assertEqual(e2e_proof._unfinished_work(outputs), ["outputs:OUTPUTS_REGISTER_UNAVAILABLE"])
        self.assertEqual(e2e_proof._unfinished_work(dict(outputs, code="OUTPUTS_SOMETHING_ELSE")), [])

    def test_notification_composer_is_inside_the_installed_package(self):
        # The installed engine ships campaign_tool only; the composer must not be read from deploy/.
        self.assertEqual(Path(notify_text.__file__).resolve().parent, Path(e2e_proof.__file__).resolve().parent)
        self.assertNotIn("records-runtime", Path(e2e_proof.__file__).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
