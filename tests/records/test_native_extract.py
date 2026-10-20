"""T11k: the Phase 1 extract stage really extracts native text (synthetic fixtures only).

Every fixture is synthetic: a born-digital PDF, XLSX and DOCX, a scanned
(image-only, no text layer) PDF and a corrupt PDF, each attached to a synthetic
e-mail and preserved through the engine's canonical mail backend into a records
root laid out like a records project's ``processing-v1``. The stage under test
is ``run_stages extract`` (``native_extract``). It runs the engine's existing
extraction, enrollment and installed extraction check, so these tests assert
persisted ledger state, not counts alone.

The parser-dependent tests need pypdf/openpyxl in the interpreter. That is the
campaign-tool parser venv in production, and CI installs
``requirements-records-parsers.txt``. Without them those tests skip with
UNSUPPORTED_RUNTIME, and the records-run workflow fails on any skip.

Phase 1 acceptance as built: PDF (native text pages) and DOCX pass the installed
extraction check, and their text lands in the ledger. The engine accepts a
workbook only under the pinned native workbook policy. Without one the XLSX is
held as ``WORKBOOK_POLICY_REQUIRED`` (counted, with an owner action item). With
``--workbook-policy`` (operator stage plan) it goes through the installed check
under that policy. Those tests use a temporary synthetic helper with a patched pin,
the same explicitly test-only pattern as ``tests/records/test_workbook_policy.py``.
It is never the private AgentOrange component and grants no production approval.
"""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import unittest
from unittest import mock
import zipfile
import zlib

from campaign_tool.records import native_extract, run_orchestrator, run_report, run_stages
from campaign_tool.records.run import Pipeline, root_lock
from tests.records.test_run_slice import synthetic_email
from tests.runner.helpers import tiny_pdf

REPO = Path(__file__).resolve().parents[2]
SCHEMA = run_orchestrator.load_stage_result_schema()
HAVE_PARSERS = not native_extract.missing_parsers()
PDF_TEXT = "Synthetic ALPR policy. Retained for 30 days. Shared with federal agencies."
DOCX_TEXT = "Synthetic retention schedule: detections purged after 30 days."


def tiny_docx(text=DOCX_TEXT):
    """Minimal WordprocessingML package; the stdlib docx route reads word/document.xml."""
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>')
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="word/document.xml"/></Relationships>')
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f'<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>')
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", content_types)
        package.writestr("_rels/.rels", rels)
        package.writestr("word/document.xml", document)
    return out.getvalue()


def tiny_xlsx():
    from openpyxl import Workbook
    book = Workbook()
    sheet = book.active
    sheet.title = "log"
    sheet.append(["event_id", "purpose"])
    sheet.append(["DEMO-1", "synthetic hotlist check"])
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def scanned_pdf(width=16, height=16):
    """One page that is only an image (a gray bitmap), no text layer: a scan."""
    data = zlib.compress(bytes((x * 13 + y * 7) % 256 for y in range(height) for x in range(width)))
    content = b"q 200 0 0 200 100 400 cm /Im0 Do Q"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /XObject << /Im0 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        (f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} /ColorSpace /DeviceGray "
         f"/BitsPerComponent 8 /Filter /FlateDecode /Length {len(data)} >>\nstream\n").encode()
        + data + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def corrupt_pdf():
    """A PDF header followed by damaged bytes: no objects, no xref, no trailer."""
    return b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + bytes(range(256)) * 4 + b"\n(truncated transfer)"


def run_main(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = run_stages.main(argv)
    raw = out.getvalue()
    return code, run_orchestrator.parse_stage_result(raw.encode(), SCHEMA), raw


class ExtractStageCase(unittest.TestCase):
    """A records project whose processing-v1 holds preserved synthetic originals."""

    attachments = ()

    def setUp(self):
        os.umask(0o077)
        self.temp = tempfile.TemporaryDirectory(prefix="t11k-")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        os.chmod(base, 0o700)
        self.project = base / "records-project"
        self.processing = self.project / "processing-v1"
        self.project.mkdir(mode=0o700)
        inbox = base / "inbox"
        inbox.mkdir(mode=0o700)
        self.names = {}
        self.payloads = dict(self.attachments)  # generated once: XLSX bytes carry a timestamp
        for index, (name, payload) in enumerate(self.payloads.items()):
            raw = synthetic_email(attachment=payload, filename=name,
                                  message_id=f"<t11k-{index}@agency.example.invalid>")
            (inbox / f"{index:02d}.eml").write_bytes(raw)
        report = Pipeline(self.processing).ingest_inbox(inbox)
        self.assertEqual(len(report["failures"]), 0, report)

    def stage(self, *extra, timeout="120"):
        return run_main(["extract", "--project", str(self.project), "--timeout", timeout,
                         "--run-id", "run-t11k", *extra])

    def query(self, sql, values=()):
        con = sqlite3.connect(f"file:{self.processing / 'ledger.sqlite'}?mode=ro", uri=True)
        try:
            return con.execute(sql, values).fetchall()
        finally:
            con.close()

    def outcomes(self, parsed):
        return {item["code"]: item["count"] for item in parsed.get("outcomes", [])}

    def extract_dirs(self):
        return sorted(p.name for p in (self.processing / "extract").iterdir())

    def assertContract(self, code, parsed, raw):
        self.assertIsNotNone(parsed, "stage line must validate as records-stage-result-v1: " + raw)
        self.assertEqual(len(raw.splitlines()), 1, "exactly one stdout line")
        self.assertNotIn(self.temp.name, raw, "no paths in the result line")
        for (sha,) in self.query("SELECT sha256 FROM originals"):
            self.assertNotIn(sha, raw, "no document identities in the result line")
        self.assertEqual(code, 1 if parsed["status"] == "fail" else 0)
        total = sum(self.outcomes(parsed).values())
        self.assertEqual(total, parsed["counts"]["input"], "one outcome code per document")


@unittest.skipUnless(HAVE_PARSERS, "UNSUPPORTED_RUNTIME: pypdf/openpyxl not in this interpreter")
class NativeExtractionTests(ExtractStageCase):
    @property
    def attachments(self):
        return (("policy.pdf", tiny_pdf(PDF_TEXT)), ("log.xlsx", tiny_xlsx()),
                ("schedule.docx", tiny_docx()), ("scan.pdf", scanned_pdf()),
                ("broken.pdf", corrupt_pdf()))

    def subject(self, name):
        payload = self.payloads[name]
        subject = hashlib.sha256(payload).hexdigest()
        self.assertEqual(self.query("SELECT COUNT(*) FROM originals WHERE sha256=?", (subject,)), [(1,)], name)
        return subject

    def stage_status(self, subject, stage="extract"):
        return self.query("SELECT status FROM stage_state WHERE original_sha256=? AND stage=?",
                          (subject, stage))[0][0]

    def unit_texts(self, subject):
        texts = []
        for (path,) in self.query("SELECT derived_path FROM units WHERE original_sha256=?", (subject,)):
            texts.append(json.loads(Path(path).read_bytes())["text"])
        return texts

    EXPECTED_FIRST = {"EXTRACTED": 7, "AWAITING_OCR": 1, "WORKBOOK_POLICY_REQUIRED": 1, "CORRUPT_DOCUMENT": 1}
    EXPECTED_HELD = {"AWAITING_OCR": 1, "WORKBOOK_POLICY_REQUIRED": 1, "CORRUPT_DOCUMENT": 1}

    def test_native_documents_are_extracted_and_held_documents_counted(self):
        code, parsed, raw = self.stage()
        self.assertContract(code, parsed, raw)
        # 5 messages + the PDF and DOCX are extracted and accepted. The workbook was parsed but is
        # held for the native workbook policy. The scan waits for OCR. The corrupt file is coded.
        self.assertEqual(self.outcomes(parsed), self.EXPECTED_FIRST)
        self.assertEqual(parsed["counts"], {"input": 10, "output": 7, "failed": 1, "skipped": 2})
        self.assertEqual((parsed["status"], parsed["error_code"]), ("warn", "EXTRACT_DOCUMENT_FAILURES"))
        items = {item["code"]: (item["severity"], item["count"]) for item in parsed["needs_g"]}
        self.assertEqual(items, {"AWAITING_OCR": ("info", 1), "EXTRACT_CORRUPT_DOCUMENT": ("action", 1),
                                 "EXTRACT_WORKBOOK_POLICY_REQUIRED": ("action", 1)})
        self.assertGreater(parsed["new_data"]["extracted_units"], 0)
        self.assertGreaterEqual(parsed["new_data"]["pages"], 1)

        # Persisted: extract done through the installed check, units bound, review still ahead.
        pdf, docx = self.subject("policy.pdf"), self.subject("schedule.docx")
        for subject in (pdf, docx):
            self.assertEqual(self.stage_status(subject), "done")
            self.assertEqual(self.stage_status(subject, "catalog"), "pending", "no promotion past extract")
            self.assertTrue(self.query("SELECT 1 FROM receipts WHERE subject_sha256=? AND stage='extract'",
                                       (subject,)))
            self.assertEqual({r[0] for r in self.query("SELECT status FROM units WHERE original_sha256=?",
                                                        (subject,))}, {"candidate_extracted"})
        self.assertIn(PDF_TEXT, " ".join(self.unit_texts(pdf)))
        self.assertIn(DOCX_TEXT, " ".join(self.unit_texts(docx)))
        self.assertEqual(self.query("SELECT page_no,status FROM page_state WHERE original_sha256=?", (pdf,)),
                         [(1, "ok")])
        # Held, never dropped: still pending, with the coded alert that marks them.
        scan, broken, xlsx = self.subject("scan.pdf"), self.subject("broken.pdf"), self.subject("log.xlsx")
        for subject, code_ in ((scan, "ocr_runtime_unconfigured"), (broken, "extraction_error"),
                               (xlsx, "workbook_cached_values_and_objects_not_reviewed")):
            self.assertEqual(self.stage_status(subject), "pending")
            keys = [r[0] for r in self.query("SELECT key FROM alerts WHERE state='open' AND key LIKE ?",
                                             (f"extract:{subject}:%",))]
            self.assertEqual(len(keys), 1)
            self.assertIn(code_, keys[0])
        self.assertFalse(self.query("SELECT 1 FROM units WHERE original_sha256 IN (?,?,?)", (scan, broken, xlsx)))
        self.assertEqual(self.query("SELECT COUNT(*) FROM proposals"), [(0,)], "nothing accepted for review")

        # Private per-document list: every document, one code each, owner-only.
        listing = self.processing / "runs" / "extract-run-t11k.json"
        self.assertEqual(stat.S_IMODE(listing.stat().st_mode), 0o600)
        documents = json.loads(listing.read_text())["documents"]
        self.assertEqual(len(documents), 10)
        by_subject = {d["original_sha256"]: (d["form"], d["outcome"]) for d in documents}
        self.assertEqual(by_subject[scan], ("pdf", "AWAITING_OCR"))
        self.assertEqual(by_subject[broken], ("pdf", "CORRUPT_DOCUMENT"))
        self.assertEqual(by_subject[xlsx], ("xlsx", "WORKBOOK_POLICY_REQUIRED"))
        self.assertEqual(by_subject[docx], ("docx", "EXTRACTED"))
        self.assertQueue([scan])
        # Writer settled: the next count-only stage can read the ledger.
        wal = self.processing / "ledger.sqlite-wal"
        self.assertFalse(wal.exists() and wal.stat().st_size)
        gcode, gates, graw = run_main(["digest_gates", "--project", str(self.project), "--timeout", "60"])
        self.assertIsNotNone(gates, graw)
        self.assertNotEqual(gates["error_code"], "GATES_LEDGER_BUSY")

    def test_rerun_counts_held_documents_without_reparsing(self):
        self.stage()
        dirs, receipts = self.extract_dirs(), self.query("SELECT COUNT(*) FROM receipts")
        alerts = self.query("SELECT key,count FROM alerts ORDER BY key")
        code, parsed, raw = self.stage()
        self.assertContract(code, parsed, raw)
        self.assertEqual(self.outcomes(parsed), self.EXPECTED_HELD)
        self.assertEqual(parsed["counts"], {"input": 3, "output": 0, "failed": 1, "skipped": 2})
        self.assertNotIn("new_data", parsed)
        self.assertEqual(self.extract_dirs(), dirs, "held documents are not parsed again")
        self.assertEqual(self.query("SELECT COUNT(*) FROM receipts"), receipts)
        self.assertEqual(self.query("SELECT key,count FROM alerts ORDER BY key"), alerts)
        self.assertEqual(self.query("SELECT COUNT(*) FROM stage_state WHERE stage='extract' AND status='done'"),
                         [(7,)])
        self.assertQueue([self.subject("scan.pdf")])  # held scans stay queued on every run

    def assertQueue(self, subjects):
        """awaiting-ocr-v1 (the OCR stage's queue input): owner-only, exact keys, pinned bytes."""
        queue = self.processing / native_extract.AWAITING_OCR_QUEUE
        self.assertEqual(stat.S_IMODE(queue.stat().st_mode), 0o600)
        lines = [json.loads(line) for line in queue.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([line["sha256"] for line in lines], sorted(subjects))
        for line in lines:
            self.assertEqual(set(line), {"document_id", "sha256", "path"})
            self.assertRegex(line["document_id"], r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
            path = Path(line["path"])
            self.assertTrue(path.is_absolute())
            self.assertTrue(path.resolve().is_relative_to(self.processing.resolve()), "under the records root")
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), line["sha256"])
            self.assertTrue(path.read_bytes().startswith(b"%PDF-"))

    def test_missing_parsers_code_pdf_and_xlsx_without_attempting_them(self):
        with mock.patch.object(native_extract, "missing_parsers", return_value=["pypdf", "openpyxl"]):
            code, parsed, raw = self.stage()
        self.assertContract(code, parsed, raw)
        self.assertEqual(self.outcomes(parsed), {"EXTRACTED": 6, "PARSER_MISSING": 4})
        self.assertEqual((parsed["status"], parsed["error_code"]), ("warn", "EXTRACT_NATIVE_PARSER_MISSING"))
        self.assertEqual(len(self.extract_dirs()), 6, "only the stdlib-parseable documents were attempted")
        for name in ("policy.pdf", "log.xlsx", "scan.pdf", "broken.pdf"):
            self.assertEqual(self.stage_status(self.subject(name)), "pending")
        # Once the parsers are there, the same documents advance on the next run.
        code, parsed, raw = self.stage()
        self.assertEqual(self.outcomes(parsed), {"EXTRACTED": 1, **self.EXPECTED_HELD})

    def test_document_cap_defers_the_rest_and_later_runs_continue(self):
        code, parsed, raw = self.stage("--max-documents", "3")
        self.assertContract(code, parsed, raw)
        self.assertEqual(self.outcomes(parsed)["DEFERRED_RUN_BUDGET"], 7)
        self.assertEqual(parsed["counts"]["input"], 10)
        for _ in range(4):  # 3 + 3 + 1 fresh attempts left; held documents do not use the per-run budget
            code, parsed, raw = self.stage("--max-documents", "3")
            self.assertContract(code, parsed, raw)
        # Every document has been attempted once: the last run only counts the held ones.
        # (Fixture order follows sha256, and the XLSX bytes carry a timestamp.)
        self.assertEqual(self.outcomes(parsed), self.EXPECTED_HELD)

    def test_stage_deadline_defers_instead_of_starting_work(self):
        code, parsed, raw = self.stage(timeout="20")  # 20 s - margin leaves < MIN_DOCUMENT_SECONDS
        self.assertContract(code, parsed, raw)
        self.assertEqual(self.outcomes(parsed), {"DEFERRED_RUN_BUDGET": 10})
        self.assertEqual((parsed["status"], parsed["error_code"]), ("warn", "EXTRACT_BACKLOG_DEFERRED"))
        self.assertEqual(self.extract_dirs(), [])

    def test_unexpected_failure_of_every_attempt_fails_the_stage_and_retries_are_bounded(self):
        failing = mock.patch.object(Pipeline, "stage_extract", side_effect=RuntimeError("boom /private/x"))
        for attempt in range(1, native_extract.MAX_ATTEMPTS + 1):
            with failing as stage_extract:
                code, parsed, raw = self.stage()
            self.assertContract(code, parsed, raw)
            self.assertEqual((parsed["status"], parsed["error_code"]), ("fail", "EXTRACT_ALL_ATTEMPTS_FAILED"))
            self.assertEqual(self.outcomes(parsed), {"EXTRACT_ERROR": 10})
            self.assertEqual(stage_extract.call_count, 10, f"attempt {attempt} retries every document")
            self.assertNotIn("boom", raw)
        # After MAX_ATTEMPTS runs the documents are held: counted, not tried again.
        with failing as stage_extract:
            code, parsed, raw = self.stage()
        self.assertEqual(stage_extract.call_count, 0)
        self.assertEqual(self.outcomes(parsed), {"EXTRACT_ERROR": 10})
        self.assertEqual((parsed["status"], parsed["error_code"]), ("warn", "EXTRACT_DOCUMENT_FAILURES"))
        # The stage-runner rows are closed, not left running.
        self.assertEqual(self.query("SELECT COUNT(*) FROM runs WHERE kind='stage-runner' AND status='running'"),
                         [(0,)])

    def test_refused_documents_are_held_until_the_alert_is_resolved(self):
        from campaign_tool.records.extraction_ledger import ExtractionBindingError
        with mock.patch.object(Pipeline, "stage_extract", side_effect=ExtractionBindingError("x")):
            code, parsed, raw = self.stage()
        self.assertEqual(self.outcomes(parsed), {"EXTRACT_NOT_ACCEPTED": 10})
        self.assertEqual(parsed["error_code"], "EXTRACT_ALL_ATTEMPTS_FAILED")
        with mock.patch.object(Pipeline, "stage_extract") as stage_extract:
            code, parsed, raw = self.stage()
        self.assertEqual(stage_extract.call_count, 0, "refused documents are not parsed and enrolled again")
        self.assertEqual(self.outcomes(parsed), {"EXTRACT_NOT_ACCEPTED": 10})
        # Resolving the alerts (an operator action) re-queues them.
        con = sqlite3.connect(self.processing / "ledger.sqlite")
        con.execute("UPDATE alerts SET state='resolved' WHERE key LIKE 'extract:%'")
        con.commit()
        con.close()
        code, parsed, raw = self.stage()
        self.assertEqual(self.outcomes(parsed), self.EXPECTED_FIRST)

    def test_orchestrator_folds_outcomes_into_a_valid_run_report(self):
        work = Path(self.temp.name) / "work"
        work.mkdir(mode=0o700)
        fixture = work / "stub.json"
        fixture.write_text(json.dumps({"stub_fixture_version": 1, "stages": {}}), encoding="utf-8")
        stages = []
        for name in run_orchestrator.WORK_STAGES:
            if name == "extract":
                argv = [sys.executable, "-B", "-m", "campaign_tool.records.run_stages", "extract",
                        "--project", str(self.project), "--timeout", "110", "--run-id", "{run_id}"]
            else:
                argv = [sys.executable, "-B", "-m", "campaign_tool.records.run_orchestrator", "stub-stage",
                        "--stage", name, "--fixture", str(fixture)]
            stages.append({"name": name, "argv": argv, "timeout_seconds": 120, "on_fail": "stop"})
        config = run_orchestrator.validate_target_config(
            {"target_config_version": 1, "target": "TEST", "mode": "test", "stages": stages})
        report_path = work / "report.json"
        env = dict(os.environ, PYTHONPATH=str(REPO))
        run_id = "run_20261019T120000Z_0123abcd"
        import time
        exit_code = run_orchestrator.execute(config, run_id, "test", run_orchestrator.utc_now(),
                                             time.time() + 600, report_path, work, env=env)
        report = json.loads(report_path.read_text())
        run_report.validate_run_report(report)
        extract = next(stage for stage in report["stages"] if stage["name"] == "extract")
        self.assertEqual(exit_code, 0)
        self.assertEqual({o["code"]: o["count"] for o in extract["outcomes"]}, self.EXPECTED_FIRST)
        self.assertEqual((extract["status"], extract["error_code"]), ("warn", "EXTRACT_DOCUMENT_FAILURES"))
        self.assertGreater(report["new_data"]["extracted_units"], 0)
        needs = {(item["code"], item["stage"]) for item in report["needs_g"]}
        self.assertIn(("AWAITING_OCR", "extract"), needs)
        self.assertIn(("EXTRACT_CORRUPT_DOCUMENT", "extract"), needs)
        self.assertNotIn(self.temp.name, report_path.read_text())


class StageEdgeTests(ExtractStageCase):
    """No parser needed: text attachments only, or no root at all."""

    attachments = (("note.txt", b"Synthetic plain-text note about retention."),)

    def test_root_locked_is_a_retryable_warning(self):
        with root_lock(self.processing):
            code, parsed, raw = self.stage()
        self.assertContract(code, parsed, raw)
        self.assertEqual((parsed["status"], parsed["error_code"]), ("warn", "EXTRACT_ROOT_LOCKED"))
        self.assertEqual(self.query("SELECT COUNT(*) FROM stage_state WHERE stage='extract' AND status='done'"),
                         [(0,)])

    def test_text_documents_extract_without_native_parsers(self):
        with mock.patch.object(native_extract, "missing_parsers", return_value=["pypdf", "openpyxl"]):
            code, parsed, raw = self.stage()
        self.assertContract(code, parsed, raw)
        self.assertEqual(self.outcomes(parsed), {"EXTRACTED": 2})
        self.assertEqual((parsed["status"], parsed["error_code"]), ("ok", None))

    def test_uninitialized_root_fails_and_creates_nothing(self):
        empty = Path(self.temp.name) / "empty-project"
        (empty / "processing-v1").mkdir(parents=True, mode=0o700)
        code, parsed, raw = run_main(["extract", "--project", str(empty), "--timeout", "60"])
        self.assertIsNotNone(parsed, raw)
        self.assertEqual((parsed["status"], parsed["error_code"]), ("fail", "EXTRACT_ROOT_NOT_INITIALIZED"))
        self.assertEqual(code, 1)
        self.assertEqual(list((empty / "processing-v1").iterdir()), [])

    def test_invalid_budget_arguments_are_refused(self):
        for extra in (("--max-documents", "0"), ("--document-timeout", "5")):
            code, parsed, raw = self.stage(*extra)
            self.assertEqual(parsed["error_code"], "STAGE_CONFIG_INVALID")


def tiny_png():
    """A valid 1x1 gray PNG (an image original: Phase 1 counts it as AWAITING_OCR)."""
    import struct

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\x80")) + chunk(b"IEND", b""))


class BudgetFairnessTests(ExtractStageCase):
    """Sol must-fix 1: documents that need no parse never use the per-run parse budget.

    Images (AWAITING_OCR), unsupported forms (DECODER_NEEDED) and missing-parser
    documents (PARSER_MISSING) are classified and counted on every run. Before the
    fix they took the cap slots on every run, in the same order, so a parseable
    document behind them was deferred forever. No native parser is needed here.
    """

    attachments = (("scan-1.png", tiny_png()), ("photo-2.png", tiny_png() + b"\x00"),
                   ("memo.rtf", b"{\\rtf1 synthetic unsupported memo}"),
                   ("policy.pdf", tiny_pdf(PDF_TEXT)), ("note.txt", b"Synthetic plain-text retention note."))
    # 5 synthetic messages (stdlib eml route) + note.txt are parseable; 2 PNG, 1 RTF and
    # (with parsers masked) 1 PDF are classified without a parse.
    PARSEABLE = 6

    def run_capped(self, cap):
        with mock.patch.object(native_extract, "missing_parsers", return_value=["pypdf", "openpyxl"]), \
             mock.patch.object(Pipeline, "stage_extract", autospec=True,
                               side_effect=Pipeline.stage_extract) as stage_extract:
            code, parsed, raw = self.stage("--max-documents", str(cap))
        self.assertContract(code, parsed, raw)
        return parsed, stage_extract.call_count

    def test_unparsed_prefixes_cannot_starve_parseable_documents(self):
        for cap in (1, 2):
            with self.subTest(cap=cap):
                if cap == 2:
                    self.setUp()  # fresh records root for the second cap
                done = 0
                for run in range(1, 10):
                    parsed, parses = self.run_capped(cap)
                    outcomes = self.outcomes(parsed)
                    # The classified documents are counted on every run, never parsed, never deferred.
                    self.assertEqual((outcomes.get("AWAITING_OCR"), outcomes.get("DECODER_NEEDED"),
                                      outcomes.get("PARSER_MISSING")), (2, 1, 1), run)
                    # Exactly `cap` parses per run while parseable work remains: bounded and advancing.
                    expected = min(cap, self.PARSEABLE - done)
                    self.assertEqual(parses, expected, run)
                    self.assertEqual(outcomes.get("EXTRACTED", 0), expected, run)
                    self.assertEqual(outcomes.get("DEFERRED_RUN_BUDGET", 0), self.PARSEABLE - done - expected, run)
                    self.assertEqual(sum(outcomes.values()), parsed["counts"]["input"])
                    done += expected
                    if done == self.PARSEABLE and not expected:
                        break
                self.assertEqual(done, self.PARSEABLE, "every parseable document was reached")
                self.assertEqual(self.query("SELECT COUNT(*) FROM stage_state WHERE stage='extract' "
                                            "AND status='done'"), [(self.PARSEABLE,)])
                # Classified documents are never dropped and never held by an alert.
                self.assertEqual(self.query("SELECT COUNT(*) FROM stage_state WHERE stage='extract' "
                                            "AND status='pending'"), [(4,)])
                self.assertEqual(self.query("SELECT COUNT(*) FROM alerts WHERE key LIKE 'extract:%'"), [(0,)])

    @unittest.skipUnless(HAVE_PARSERS, "UNSUPPORTED_RUNTIME: pypdf/openpyxl not in this interpreter")
    def test_parser_missing_documents_are_eligible_once_parsers_are_installed(self):
        for _ in range(self.PARSEABLE):
            parsed, _ = self.run_capped(1)
        self.assertEqual(self.outcomes(parsed).get("PARSER_MISSING"), 1)
        code, parsed, raw = self.stage("--max-documents", "1")  # parsers present now
        self.assertContract(code, parsed, raw)
        self.assertEqual(self.outcomes(parsed), {"EXTRACTED": 1, "AWAITING_OCR": 2, "DECODER_NEEDED": 1})
        pdf = hashlib.sha256(self.payloads["policy.pdf"]).hexdigest()
        self.assertEqual(self.query("SELECT status FROM stage_state WHERE original_sha256=? AND stage='extract'",
                                    (pdf,)), [("done",)])


def synthetic_workbook_component(directory):
    """TEST-ONLY stand-in for the private pinned workbook accounting helper.

    It models the documented ``inspect``/``account`` interface by recounting the
    package with openpyxl the way the intake route serializes rows (non-empty cells
    per row). It is written 0400 and its digest is patched into the pin for the test.
    """
    source = b'''"""Synthetic test-only workbook helper; never production policy."""
import io
import zipfile


def _book(package):
    from openpyxl import load_workbook
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as copy:
        for info in package.infolist():
            copy.writestr(info, package.read(info))
    return load_workbook(io.BytesIO(out.getvalue()), read_only=True, data_only=False, keep_links=False)


def inspect(package):
    return {"contract": "plain-workbook-native-rows-v3", "package_parts": len(package.infolist()),
            "scope": "all_native_sheet_rows_only", "gaps": [], "visual_review": False,
            "semantic_acceptance": False}


def account(package):
    book = _book(package)
    sheets = []
    for index, sheet in enumerate(book.worksheets, 1):
        sheet.reset_dimensions()
        rows = cells = formulas = 0
        for row in sheet.iter_rows():
            values = [c for c in row if c.value is not None]
            rows += 1
            cells += len(values)
            formulas += sum(c.data_type == "f" for c in values)
        sheets.append({"name": sheet.title, "sheet_id": str(index), "part": "xl/worksheets/sheet%d.xml" % index,
                       "state": sheet.sheet_state, "serialized_rows": rows, "serialized_cells": cells,
                       "explicit_blank_cells": 0, "formula_cells": formulas})
    book.close()
    return {"schema": "workbook-native-accounting-v1",
            "denominator": {"declared_sheets": len(sheets), "accounted_sheets": len(sheets),
                            "serialized_rows": sum(s["serialized_rows"] for s in sheets),
                            "serialized_cells": sum(s["serialized_cells"] for s in sheets),
                            "formula_cells": sum(s["formula_cells"] for s in sheets)},
            "sheets": sheets, "gaps": [], "coverage": "all_serialized_native_rows_and_cells",
            "sparse_grid_blanks_materialized": False, "formula_execution": False,
            "visual_review": False, "semantic_acceptance": False}
'''
    path = Path(directory) / "synthetic-workbook-component.py"
    path.write_bytes(source)
    path.chmod(0o400)
    return path, hashlib.sha256(source).hexdigest()


def formula_xlsx():
    from openpyxl import Workbook
    book = Workbook()
    book.active.title = "calc"
    book.active.append(["total", "=1+1"])
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


@unittest.skipUnless(HAVE_PARSERS, "UNSUPPORTED_RUNTIME: pypdf/openpyxl not in this interpreter")
class WorkbookPolicyStageTests(ExtractStageCase):
    """Sol must-fix 2: XLSX native text through the existing trusted workbook policy path."""

    @property
    def attachments(self):
        return (("log.xlsx", tiny_xlsx()), ("calc.xlsx", formula_xlsx()))

    def setUp(self):
        super().setUp()
        from campaign_tool.records import workbook_policy
        self.component, digest = synthetic_workbook_component(self.temp.name)
        pin = mock.patch.object(workbook_policy, "COMPONENT_SHA256", digest)
        pin.start()
        self.addCleanup(pin.stop)

    def subject(self, name):
        return hashlib.sha256(self.payloads[name]).hexdigest()

    def with_policy(self, *extra):
        return self.stage("--workbook-policy", str(self.component), *extra)

    def test_plain_workbook_text_and_locators_persist_through_the_installed_check(self):
        code, parsed, raw = self.with_policy()
        self.assertContract(code, parsed, raw)
        # 2 messages + the plain workbook extracted; the formula workbook is refused by the policy.
        self.assertEqual(self.outcomes(parsed), {"EXTRACTED": 3, "WORKBOOK_POLICY_REQUIRED": 1})
        xlsx = self.subject("log.xlsx")
        self.assertEqual(self.query("SELECT status FROM stage_state WHERE original_sha256=? AND stage='extract'",
                                    (xlsx,)), [("done",)])
        self.assertEqual(self.query("SELECT status FROM stage_state WHERE original_sha256=? AND stage='catalog'",
                                    (xlsx,)), [("pending",)], "no promotion past extract")
        rows = self.query("SELECT status,derived_path FROM units WHERE original_sha256=?", (xlsx,))
        self.assertEqual({status for status, _ in rows}, {"candidate_extracted"})
        units = [json.loads(Path(path).read_bytes()) for _, path in rows]
        by_kind = {}
        for unit in units:
            by_kind.setdefault(unit["kind"], []).append(unit)
        self.assertEqual([u["locator"] for u in by_kind["workbook_sheet"]], [{"sheet": "log"}])
        rows_by_locator = {(u["locator"]["sheet"], u["locator"]["row"]): u["text"] for u in by_kind["workbook_row"]}
        self.assertEqual(set(rows_by_locator), {("log", 1), ("log", 2)})
        self.assertIn("synthetic hotlist check", rows_by_locator[("log", 2)])
        self.assertIn("DEMO-1", rows_by_locator[("log", 2)])
        self.assertIn("event_id", rows_by_locator[("log", 1)])
        # The extract receipt binds the policy's review holds: no visual or semantic approval.
        content = self.query("SELECT a.payload FROM stage_content c JOIN stage_artifacts a "
                             "ON a.sha256=c.content_sha256 WHERE c.subject_sha256=? AND c.stage='extract'",
                             (xlsx,))
        self.assertEqual(len(content), 1)
        workbook = json.loads(bytes(content[0][0]))["workbook"]
        self.assertTrue(workbook["visual_review_pending"])
        self.assertFalse(workbook["semantic_acceptance"])
        # The formula workbook is enrolled at most once and held with a workbook outcome.
        calc = self.subject("calc.xlsx")
        self.assertEqual(self.query("SELECT status FROM stage_state WHERE original_sha256=? AND stage='extract'",
                                    (calc,)), [("pending",)])
        keys = [r[0] for r in self.query("SELECT key FROM alerts WHERE state='open' AND key LIKE ?",
                                         (f"extract:{calc}:%",))]
        self.assertEqual(keys, [f"extract:{calc}:workbook_policy_refused"])

    def test_replay_does_not_reparse_or_duplicate(self):
        self.with_policy()
        dirs, receipts = self.extract_dirs(), self.query("SELECT COUNT(*) FROM receipts")
        units = self.query("SELECT COUNT(*) FROM units")
        alerts = self.query("SELECT key,count FROM alerts ORDER BY key")
        code, parsed, raw = self.with_policy()
        self.assertContract(code, parsed, raw)
        self.assertEqual(self.outcomes(parsed), {"WORKBOOK_POLICY_REQUIRED": 1})
        self.assertEqual(self.extract_dirs(), dirs)
        self.assertEqual(self.query("SELECT COUNT(*) FROM receipts"), receipts)
        self.assertEqual(self.query("SELECT COUNT(*) FROM units"), units)
        self.assertEqual(self.query("SELECT key,count FROM alerts ORDER BY key"), alerts)

    def test_without_the_policy_workbooks_stay_held_and_are_not_enrolled(self):
        code, parsed, raw = self.stage()
        self.assertContract(code, parsed, raw)
        self.assertEqual(self.outcomes(parsed), {"EXTRACTED": 2, "WORKBOOK_POLICY_REQUIRED": 2})
        self.assertFalse(self.query("SELECT 1 FROM units WHERE original_sha256 IN (?,?)",
                                    (self.subject("log.xlsx"), self.subject("calc.xlsx"))))

    def test_unpinned_or_writable_component_fails_the_stage_without_fallback(self):
        other = Path(self.temp.name) / "other.py"
        other.write_bytes(b"def inspect(p): return {}\ndef account(p): return {}\n")
        other.chmod(0o400)
        writable = Path(self.temp.name) / "writable.py"
        writable.write_bytes(self.component.read_bytes())
        writable.chmod(0o600)
        for path in (other, writable, Path(self.temp.name) / "absent.py"):
            with self.subTest(path=path.name):
                code, parsed, raw = self.stage("--workbook-policy", str(path))
                self.assertIsNotNone(parsed, raw)
                self.assertEqual((code, parsed["status"], parsed["error_code"]),
                                 (1, "fail", "EXTRACT_WORKBOOK_POLICY_INVALID"))
                self.assertNotIn(self.temp.name, raw)
        self.assertEqual(self.query("SELECT COUNT(*) FROM stage_state WHERE stage='extract' AND status='done'"),
                         [(0,)], "nothing ran")


class OutcomeContractTests(unittest.TestCase):
    def line(self, outcomes, counts):
        return json.dumps({"stage_result_version": 1, "status": "ok", "error_code": None,
                           "counts": counts, "outcomes": outcomes}).encode()

    def test_outcomes_must_account_for_every_input(self):
        counts = {"input": 3, "output": 2, "failed": 0, "skipped": 1}
        good = [{"code": "EXTRACTED", "count": 2}, {"code": "AWAITING_OCR", "count": 1}]
        self.assertIsNotNone(run_orchestrator.parse_stage_result(self.line(good, counts), SCHEMA))
        short = [{"code": "EXTRACTED", "count": 2}]
        self.assertIsNone(run_orchestrator.parse_stage_result(self.line(short, counts), SCHEMA))
        duplicate = [{"code": "EXTRACTED", "count": 2}, {"code": "EXTRACTED", "count": 1}]
        self.assertIsNone(run_orchestrator.parse_stage_result(self.line(duplicate, counts), SCHEMA))
        lower = [{"code": "extracted", "count": 3}]
        self.assertIsNone(run_orchestrator.parse_stage_result(self.line(lower, counts), SCHEMA))

    def test_run_report_rejects_unbalanced_outcomes(self):
        stages = [run_orchestrator.stage_entry(name, "ok", seconds=0) for name in run_orchestrator.WORK_STAGES]
        stages[4]["counts"] = {"input": 2, "output": 2, "failed": 0, "skipped": 0}
        stages[4]["outcomes"] = [{"code": "EXTRACTED", "count": 2}]
        stages.append(run_orchestrator.pending_notify())
        report = run_orchestrator.build_report("run_20261019T120000Z_0123abcd", "test",
                                               run_orchestrator.utc_now(), stages, {}, 0, [])
        self.assertEqual(report["stages"][4]["outcomes"], [{"code": "EXTRACTED", "count": 2}])
        report["stages"][4]["outcomes"] = [{"code": "EXTRACTED", "count": 1}]
        with self.assertRaises(ValueError):
            run_report.validate_run_report(report)

    def test_classify_codes(self):
        classify = native_extract.classify_codes
        self.assertEqual(classify({"ocr_runtime_unconfigured", "ocr_needed_or_blank_page"}), "AWAITING_OCR")
        self.assertEqual(classify({"decoder_unavailable_or_failed"}), "CORRUPT_DOCUMENT")
        self.assertEqual(classify({"extraction_error"}), "CORRUPT_DOCUMENT")
        self.assertEqual(classify({"extraction_timeout"}), "EXTRACT_TIMEOUT")
        self.assertEqual(classify({"decoder_needed"}), "DECODER_NEEDED")
        self.assertEqual(classify({"workbook_cached_values_and_objects_not_reviewed"}), "WORKBOOK_POLICY_REQUIRED")
        self.assertEqual(classify({"extraction_check_refused"}), "EXTRACT_NOT_ACCEPTED")
        self.assertEqual(classify({"extraction_stage_error"}), "EXTRACT_ERROR")
        self.assertEqual(classify({"expansion_limit"}), "EXTRACT_INCOMPLETE")
        self.assertEqual(classify(set()), "EXTRACT_INCOMPLETE")


if __name__ == "__main__":
    unittest.main()
