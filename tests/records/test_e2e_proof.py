"""REC-AUTO-11: the e2e-proof replay runs every stage with no manual step and sends nothing."""
import contextlib
import hashlib
import io
import json
import shutil
import sqlite3
import struct
import tempfile
import unittest
import zlib
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path
from unittest import mock

from campaign_tool.records import approval_queue, e2e_proof, native_extract, notify_text
from tests.records.test_run_slice import synthetic_email


def png_scan():
    """A tiny synthetic gray PNG with no text layer: an image-only scan the OCR stage has not read yet."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(b"\x00" + bytes((x * 7 + y * 13) % 256 for x in range(16)) for y in range(16))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 16, 8, 0, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


HAVE_PARSERS = not native_extract.missing_parsers()
FIXTURE = Path(__file__).parent / "fixtures" / "e2e-campaign"
PAGE_LINES = ("Fictional Valley ALPR Usage Policy", "Audit log of each search.", "Shared with ICE.",
              "Attached are the responsive records.")


def text_pdf(lines=PAGE_LINES):
    """A one-page text-layer PDF, one line per Tj, built at test time (no binary in the repo)."""
    stream = ("BT /F1 12 Tf 72 720 Td 16 TL " + " T* ".join("(%s) Tj" % line for line in lines) + " ET").encode()
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
               b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    return bytes(out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref))


def pdf_email():
    message = EmailMessage()
    message["From"], message["To"] = "records@fictional-valley.example.invalid", "requests@campaign.example.invalid"
    message["Subject"], message["Date"] = "Response to records request FV-2026-0001", "Tue, 15 Sep 2026 10:00:00 -0700"
    message["Message-ID"] = "<fv1@fictional-valley.example.invalid>"
    message.set_content("The responsive policy is attached.\n")
    message.add_attachment(text_pdf(), maintype="application", subtype="pdf", filename="policy.pdf")
    return message.as_bytes(policy=SMTP)


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


class SeededReplayTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="e2e-seed-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.campaign = self.tmp / "campaign"
        shutil.copytree(FIXTURE / "content", self.campaign / "content")
        # The fact sheet cites the attachment by its sha256, which only exists once the PDF is built.
        self.seed = self.tmp / "seed.json"
        self.seed.write_text((FIXTURE / "seed.json").read_text().replace(
            "@POLICY_PDF_SHA256@", hashlib.sha256(text_pdf()).hexdigest()))
        self.message = self.tmp / "m.eml"
        self.message.write_bytes(pdf_email())

    def replay(self, label, seed=None):
        return e2e_proof.replay(self.message, self.tmp / "out", self.campaign, label=label, seed=seed or self.seed)

    def outcome(self, label, name):
        files = list((self.tmp / "out" / label / "project" / "run-outputs").glob("*/" + name))
        self.assertEqual(len(files), 1)
        return json.loads(files[0].read_text())

    def ledger(self, label):
        return sqlite3.connect(str(self.tmp / "out" / label / "project" / "processing-v1" / "ledger.sqlite"))

    @unittest.skipUnless(HAVE_PARSERS, "UNSUPPORTED_RUNTIME: pypdf/openpyxl not in this interpreter")
    def test_seeded_replay_proves_agency_request_comparison_gap_and_probe(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            report = self.replay("seeded")
        self.assertEqual((report["result"], report["manual_steps"]), ("PASS", []), report["stages"])
        names = [s["stage"] for s in report["stages"]]
        self.assertEqual(names[:4], ["arrival_trigger", "save_mail", "seed", "extract"])
        seed = report["stages"][2]
        self.assertEqual((seed["status"], seed["detail"]["agencies"], seed["detail"]["requests"],
                          seed["detail"]["fact_sheets"]), ("ok", 1, 1, 1))
        items = self.outcome("seeded", "outcomes/received-items.json")["items"]
        self.assertTrue(any(i.get("agency_id") == "ca-fictional-valley-pd" and i.get("request_id") == "fv-req-1" for i in items))
        comparison = self.outcome("seeded", "comparison.json")
        self.assertGreaterEqual(comparison["agency_count"], 1)
        self.assertEqual(comparison["input_status"], "fact_sheets_supplied")
        gaps = self.outcome("seeded", "outcomes/gap-proposals.json")
        proposals = [p for a in gaps["agencies"] if a["agency_id"] == "ca-fictional-valley-pd"
                     for r in a["requests"] for p in r["proposals"]]
        # Machine-extracted text is never "reviewed", so a policy gap (high) is lowered one step to medium.
        self.assertTrue(any(p["confidence"] == "medium" and p["type"] == "policy_gap" for p in proposals), proposals)
        self.assertEqual(report["stages"][names.index("next_steps")]["status"], "ok")
        probes = next(s for s in report["stages"] if s["stage"] == "next_steps")["detail"]["queued_by_kind"]
        self.assertGreaterEqual(probes["next-followup-probe"], 1)
        queue = {r["kind"]: r["status"] for r in approval_queue.load_queue(str(self.tmp / "out" / "seeded" / "project"))}
        self.assertEqual(queue["next-followup-probe"], "rejected")
        console = next(s for s in report["stages"] if s["stage"] == "console")
        self.assertEqual(console["detail"]["decision"]["denied"], 1)
        timeline = self.outcome("seeded", "outcomes/timeline.json")
        self.assertEqual((timeline["request_count"], sum(timeline["unmatched"].values())), (1, 0), timeline["unmatched"])
        self.assertNotIn("Audit log", stdout.getvalue())

    def test_seed_naming_an_unknown_request_fails_and_inserts_nothing(self):
        doc = json.loads(self.seed.read_text())
        doc["join"]["request_id"] = "no-such-request"
        bad = self.tmp / "bad.json"
        bad.write_text(json.dumps(doc))
        report = self.replay("bad", seed=bad)
        stage = next(s for s in report["stages"] if s["stage"] == "seed")
        self.assertEqual((stage["status"], stage["code"]), ("fail", "SEED_INVALID"))
        self.assertEqual(report["result"], "FAIL")
        self.assertEqual(next(s for s in report["stages"] if s["stage"] == "extract")["status"], "not_run")
        db = self.ledger("bad")
        self.addCleanup(db.close)
        for table in ("agencies", "requests", "joins"):
            self.assertEqual(db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)
        self.assertFalse((self.tmp / "out" / "bad" / "project" / "agency-fact-sheets.json").exists())

    def test_apply_seed_refuses_a_directory_without_the_sandbox_marker(self):
        project = self.tmp / "live" / "project"
        (project / "processing-v1").mkdir(parents=True)
        (project / "processing-v1" / "ledger.sqlite").write_bytes(b"not touched")

        def tree():
            return sorted((str(p.relative_to(self.tmp)), hashlib.sha256(p.read_bytes()).hexdigest())
                          for p in self.tmp.rglob("*") if p.is_file())
        before = tree()
        with self.assertRaises(e2e_proof.ProofError) as caught:
            e2e_proof._apply_seed(project, self.seed)
        self.assertEqual(caught.exception.code, "SEED_NOT_SANDBOX")
        self.assertEqual(tree(), before)

    @unittest.skipUnless(HAVE_PARSERS, "UNSUPPORTED_RUNTIME: pypdf/openpyxl not in this interpreter")
    def test_cli_seed_prints_no_fixture_text_and_unseeded_has_no_seed_stage(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = e2e_proof.main(["--message", str(self.message), "--campaign-root", str(self.campaign),
                                   "--out", str(self.tmp / "out"), "--label", "cli", "--seed", str(self.seed)])
        self.assertEqual(code, 0, out.getvalue())
        self.assertNotIn("Audit log", out.getvalue())
        plain = e2e_proof.replay(self.message, self.tmp / "out", self.campaign, label="plain")
        self.assertNotIn("seed", [s["stage"] for s in plain["stages"]])


if __name__ == "__main__":
    unittest.main()
