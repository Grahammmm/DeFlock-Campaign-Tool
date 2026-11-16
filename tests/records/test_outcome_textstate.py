"""Synthetic-only DOC-2 per-record text state and its outputs-stage wiring."""
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
from contextlib import closing
from unittest import mock

from campaign_tool.records import ocr_review, run_stages
from campaign_tool.records.extraction_ledger import ExtractionLedgerAdapter
from campaign_tool.records.ledger import store
from campaign_tool.records.ledger.migrations import v001
from campaign_tool.records.ocr_review import canonical
from campaign_tool.records.outcomes import textstate as ts
from tests.records import test_outcome_factsheets as _fs

FIXTURE = Path(__file__).parent / "fixtures" / "outcome_register" / "tracker.csv"
RETRY = ("could_not_be_read", "Could not be read this time: the reader failed or ran out of time "
         "(it retries; a reader that is not installed retries when it is).")
EXPECTED = {
    "AWAITING_OCR": ("awaiting_ocr", "A scan with no text yet; waiting for OCR."),
    "CORRUPT_DOCUMENT": ("could_not_be_read", "Could not be read: the file looks damaged."),
    "DECODER_NEEDED": ("could_not_be_read", "Could not be read: this file format needs a reader we do not have yet."),
    "WORKBOOK_POLICY_REQUIRED": ("could_not_be_read", "Could not be read: a spreadsheet needs an owner decision before it is opened."),
    "EXTRACT_NOT_ACCEPTED": ("could_not_be_read", "Could not be read: the extracted text failed our checks and was refused."),
    "EXTRACT_INCOMPLETE": RETRY, "EXTRACT_TIMEOUT": RETRY, "EXTRACT_ERROR": RETRY, "PARSER_MISSING": RETRY,
    "PRESERVE_PENDING": ("waiting", "Waiting its turn: not processed yet."),
    "DEFERRED_RUN_BUDGET": ("waiting", "Waiting its turn: not processed yet."),
    "EXTRACTED": ("text_reviewed", "Text extracted and passed the extraction checks."),
    "NEW_FUTURE_CODE": ("unknown_state", "State not recognised"),
}
PRIVATE_CANARY_TEXT = "Secret Synthetic Document Text"


def h(n):
    return f"{n:064x}"


class TextStateTests(unittest.TestCase):
    def setUp(self):
        work = Path(tempfile.gettempdir()) / "outcome-textstate" / "test-workspaces"
        work.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="textstate-", dir=work)
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.processing = self.project / "processing-v1"
        self.processing.mkdir(mode=0o700)
        (self.processing / "runs").mkdir(mode=0o700)
        with closing(sqlite3.connect(self.processing / "ledger.sqlite")) as con, con:
            con.executescript(v001.SQL)
        (self.project / "MASTER-REQUEST-TRACKER.csv").write_bytes(FIXTURE.read_bytes())
        (self.project / "portal-queue-v1").mkdir()
        with closing(sqlite3.connect(self.project / "portal-queue-v1" / "portal.sqlite")) as con, con:
            con.execute("CREATE TABLE portal_items(id TEXT)")

    def listing(self, run, documents, schema="records-extract-outcomes-v1"):
        path = self.processing / "runs" / f"extract-{run}.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump({"schema": schema, "run_id": run, "written_at": "2030-01-01T00:00:00+00:00",
                       "documents": documents}, stream)
        return path

    def doc(self, sha, outcome, **extra):
        return {"original_sha256": sha, "form": "pdf", "outcome": outcome, **extra}

    def stage(self, run):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = run_stages.main(["outputs", "--project", str(self.project), "--run-id", run])
        return code, json.loads(out.getvalue())

    def read(self, run):
        directory = self.project / "run-outputs" / run
        return (json.loads((directory / "text-state.json").read_text(encoding="utf-8")),
                (directory / "text-state.md").read_text(encoding="utf-8"))

    def test_every_outcome_maps_to_exact_state_and_words(self):
        outcomes = sorted(EXPECTED) + [None]
        self.listing("r1", [self.doc(h(i + 1), o) for i, o in enumerate(outcomes)])
        report = ts.write_run_textstate(self.project, "r1")
        self.assertEqual(report, self.read("r1")[0])
        got = {r["sha256"]: (r["state"], r["words"]) for r in report["records"]}
        for i, outcome in enumerate(outcomes):
            self.assertEqual(got[h(i + 1)], EXPECTED.get(outcome, EXPECTED["NEW_FUTURE_CODE"]), outcome)
        self.assertEqual(report["counts"], {"text_reviewed": 1, "ocr_flagged": 0, "ocr_unreviewed": 0, "awaiting_ocr": 1,
                                            "could_not_be_read": 8, "waiting": 2, "unknown_state": 2})
        self.assertEqual([r["sha256"] for r in report["records"]], sorted(got))
        self.assertEqual(report["schema"], "records-text-state-v1")

    def ocr_page(self, sha):
        with closing(sqlite3.connect(self.processing / "ledger.sqlite")) as con, con:
            con.execute("INSERT INTO page_state(original_sha256,page_no,confidence,derivative_sha256,"
                        "needs_visual_review,status) VALUES(?,1,70.0,?,1,'ok')", (sha, h(99)))

    def review(self, sha, *, broken=False):
        row = {"schema": "ocr-review-v1", "original_sha256": sha, "page_no": 1, "confidence": 70.0,
               "derivative_sha256": h(99), "decision": "confirmed", "reviewer_label": "organizer",
               "reviewed_at": "2030-01-02T00:00:00Z", "prev_line_sha256": h(5) if broken else None}
        folder = self.project / "ocr-review-v1"
        folder.mkdir(exist_ok=True)
        (folder / "reviews.jsonl").write_bytes(canonical(row))

    def test_pending_ocr_page_is_unreviewed_until_a_review_entry(self):
        self.ocr_page(h(1))
        docs = [self.doc(h(1), "EXTRACTED"), self.doc(h(2), "EXTRACTED")]
        self.listing("r1", docs)
        states = {r["sha256"]: r["state"] for r in ts.write_run_textstate(self.project, "r1")["records"]}
        self.assertEqual(states, {h(1): "ocr_unreviewed", h(2): "text_reviewed"})
        self.assertIn("has not been visually checked yet", self.read("r1")[1])
        self.review(h(1))
        self.listing("r2", docs)
        states = {r["sha256"]: r["state"] for r in ts.write_run_textstate(self.project, "r2")["records"]}
        self.assertEqual(states, {h(1): "text_reviewed", h(2): "text_reviewed"})
        # A broken review chain is never read as "reviewed".
        self.review(h(1), broken=True)
        self.listing("r3", docs)
        with self.assertRaises(OSError):
            ts.write_run_textstate(self.project, "r3")
        self.assertFalse((self.project / "run-outputs" / "r3").exists())

    @mock.patch("campaign_tool.records.followup_engine.run", new=lambda *args, **kwargs: None)  # needs full engine inputs; covered in test_outputs_followup_engine
    def test_outputs_carry_state_not_private_locators_and_keep_modes(self):
        absolute = str(self.project / "processing-v1" / "blobs" / "Secret.pdf")
        listed = self.listing("r1", [self.doc("a" * 64, "EXTRACT_NOT_ACCEPTED", path=absolute, storage_path=absolute,
                                              refusal_code="unlisted", note=PRIVATE_CANARY_TEXT),
                                     self.doc("b" * 64, "AWAITING_OCR", path=absolute)])
        (self.processing / "awaiting-ocr.jsonl").write_text(json.dumps({"path": absolute, "text": PRIVATE_CANARY_TEXT}) + "\n")
        code, line = self.stage("r1")
        self.assertEqual((code, line["status"], line["error_code"]), (0, "ok", None))
        report, text = self.read("r1")
        directory = self.project / "run-outputs" / "r1"
        for raw in ((directory / "text-state.json").read_text(), text):
            for leak in ("path", "storage_path", "form", "written_at", "pdf", "unlisted", absolute,
                         str(self.project), PRIVATE_CANARY_TEXT, "awaiting-ocr"):
                self.assertNotIn(leak, raw)
        self.assertEqual({k for r in report["records"] for k in r}, {"sha256", "state", "words"})
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for name in ts.FILES:
            self.assertEqual(stat.S_IMODE((directory / name).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(listed.stat().st_mode), 0o600)
        self.assertIn("Private and review-required", text)
        self.assertIn("a" * 12 + " - Could not be read: the extracted text failed our checks and was refused.", text)

    def test_unusable_list_warns_without_suppressing_other_outputs(self):
        def missing():
            pass

        def wrong_schema():
            self.listing("bad", [], schema="something-else")

        def symlinked():
            real = self.listing("elsewhere", [])
            os.symlink(real, self.processing / "runs" / "extract-bad.json")

        def not_json():
            (self.processing / "runs" / "extract-bad.json").write_text("{")

        def duplicate():
            self.listing("bad", [self.doc(h(1), "EXTRACTED"), self.doc(h(1), "AWAITING_OCR")])

        def oversize():
            self.listing("bad", [self.doc(h(1), "EXTRACTED")])
            patcher = mock.patch.object(ts, "MAX_LIST_BYTES", 16)
            patcher.start()
            self.addCleanup(patcher.stop)

        for index, make in enumerate((missing, wrong_schema, symlinked, not_json, duplicate, oversize)):
            with self.subTest(make.__name__):
                for leftover in (self.processing / "runs").iterdir():
                    leftover.unlink()
                make()
                run = f"bad-{index}"
                if make is not missing:
                    os.rename(self.processing / "runs" / "extract-bad.json",
                              self.processing / "runs" / f"extract-{run}.json")
                code, line = self.stage(run)
                self.assertEqual((code, line["status"], line["error_code"]), (0, "warn", "OUTPUTS_TEXTSTATE_UNAVAILABLE"))
                self.assertEqual(line["counts"], {"input": 1, "output": 1, "failed": 0, "skipped": 0})
                self.assertTrue((self.project / "run-outputs" / f"{run}.json").is_file())
                directory = self.project / "run-outputs" / run
                for name in ("agency-factsheets.json", "CURRENT-REQUEST-REGISTER.json"):
                    self.assertTrue((directory / name).is_file(), name)
                for name in ts.FILES:
                    self.assertFalse(os.path.lexists(directory / name))
                self.assertNotIn(str(self.project), json.dumps(line))

    def test_register_warning_keeps_its_own_code(self):
        self.listing("r1", [])
        from campaign_tool.records.outcomes import register
        with mock.patch.object(register, "write_for_run", side_effect=RuntimeError("inert")):
            code, line = self.stage("r1")
        self.assertEqual((code, line["error_code"]), (0, "OUTPUTS_REGISTER_UNAVAILABLE"))
        self.assertTrue((self.project / "run-outputs" / "r1" / "text-state.json").is_file())

    def test_replay_is_verified_noop_partial_refused_new_run_own_directory(self):
        self.listing("r1", [self.doc(h(1), "EXTRACTED")])
        ts.write_run_textstate(self.project, "r1")
        target = self.project / "run-outputs" / "r1" / "text-state.json"
        before = (target.read_bytes(), target.stat().st_ino, target.stat().st_mtime_ns)
        ts.write_run_textstate(self.project, "r1")
        self.assertEqual(before, (target.read_bytes(), target.stat().st_ino, target.stat().st_mtime_ns))
        self.listing("r1", [self.doc(h(1), "AWAITING_OCR")])  # changed input under the same run id
        with self.assertRaises(OSError):
            ts.write_run_textstate(self.project, "r1")
        self.assertEqual(target.read_bytes(), before[0])
        self.listing("r2", [self.doc(h(1), "AWAITING_OCR")])
        self.assertEqual(ts.write_run_textstate(self.project, "r2")["records"][0]["state"], "awaiting_ocr")
        self.assertEqual(target.read_bytes(), before[0])
        partial = self.project / "run-outputs" / "partial"
        partial.mkdir(mode=0o700)
        (partial / "text-state.md").write_text("stale")
        self.listing("partial", [])
        with self.assertRaises(OSError):
            ts.write_run_textstate(self.project, "partial")
        self.assertEqual(sorted(p.name for p in partial.iterdir()), ["text-state.md"])
        with self.assertRaises(OSError):
            ts.write_run_textstate(self.project, "../r1")

    def test_short_id_prefix_collision_uses_full_value(self):
        a, b, c = "abcdef012345" + "0" * 52, "abcdef012345" + "1" * 52, h(7)
        self.listing("r1", [self.doc(a, "EXTRACTED"), self.doc(b, "CORRUPT_DOCUMENT"), self.doc(c, "AWAITING_OCR")])
        ts.write_run_textstate(self.project, "r1")
        text = self.read("r1")[1]
        self.assertIn(a + " - Text extracted", text)
        self.assertIn(b + " - Could not be read: the file looks damaged.", text)
        self.assertIn("\n" + c[:12] + " - A scan", text)
        self.assertNotIn(c, text)

    @mock.patch("campaign_tool.records.followup_engine.run", new=lambda *args, **kwargs: None)  # needs full engine inputs; covered in test_outputs_followup_engine
    def test_empty_list_counts_zero_and_no_record_lines(self):
        self.listing("r1", [])
        code, line = self.stage("r1")
        self.assertEqual((code, line["status"]), (0, "ok"))
        report, text = self.read("r1")
        self.assertEqual((report["records"], set(report["counts"].values())), ([], {0}))
        self.assertEqual(text, ts.markdown(report))
        self.assertFalse([ln for ln in text.splitlines() if " - " in ln])


def finish_extract(database, digest):
    """Mark the extract stage of a fixture original done, as the extract stage does after a run."""
    receipt = hashlib.sha256(b"synthetic extract receipt " + digest.encode()).hexdigest()
    with store.ledger(database) as con:
        con.execute("INSERT INTO receipts(sha256,stage,subject_sha256,reviewer_id,role,verdict,coverage,locators,"
                    "rationale,model_or_tool,input_hashes,created_at_tz,path) "
                    "VALUES(?,'extract',?,'fixture','extract','pass','{}','[]','fixture','fixture','{}','2000','fixture')",
                    (receipt, digest))
        con.execute("UPDATE stage_state SET status='done',receipt_sha256=? WHERE original_sha256=? AND stage='extract'",
                    (receipt, digest))
        con.commit()


def write_extract_list(project, run, digests):
    """The extract stage's private list for `run`: only the documents that run touched."""
    path = Path(project) / "processing-v1" / "runs" / f"extract-{run}.json"
    path.parent.mkdir(mode=0o700, exist_ok=True)
    documents = [{"original_sha256": d, "form": "pdf", "outcome": "EXTRACTED"} for d in digests]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({"schema": "records-extract-outcomes-v1", "run_id": run, "written_at": "2030-01-01T00:00:00+00:00",
                   "documents": documents}, stream)


def mark_ocr(project, digest, decision):
    """The organizer's OCR check through the real ocr_review CLI (page 1 of the fixture PDF)."""
    with contextlib.redirect_stdout(io.StringIO()):
        return ocr_review.main(["mark", "--project", str(project), "--original-sha256", digest, "--page", "1",
                                "--decision", decision, "--reviewer-label", "organizer"])


class RelistTests(unittest.TestCase):
    """The run's text state covers every received record, not only the ones this run's extract listed."""
    private, document, enroll = (_fs.FactSheetsTests.private, _fs.FactSheetsTests.document,
                                 _fs.FactSheetsTests.enroll)

    def setUp(self):
        work = Path(tempfile.gettempdir()) / "outcome-textstate" / "test-workspaces"
        work.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="relist-", dir=work)
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir(mode=0o700)
        self.processing = self.project / "processing-v1"
        self.processing.mkdir(mode=0o700)
        self.database = self.processing / "ledger.sqlite"
        store.initialize(self.database)
        self.evidence = self.processing / "artifacts" / "evidence"
        self.evidence.parent.mkdir(mode=0o700)
        self.adapter = ExtractionLedgerAdapter(database=self.database, evidence_root=self.evidence)
        self.serial = 0
        with store.ledger(self.database) as con:
            con.execute("INSERT INTO runs VALUES('fixture','fixture','2041-01-01',NULL,'fixture',NULL,NULL,NULL,'fixture','{}')")
            con.execute("INSERT INTO agencies VALUES(?,?,NULL,NULL)", ("ag1", "Ag1"))
            con.commit()

    def pdf(self, **extra):
        digest, _ = self.document(["Synthetic page one"], "ag1", **extra)
        return digest

    def state(self, run, digests=()):
        """Write this run's extract list and text state; -> {sha256: state}."""
        write_extract_list(self.project, run, digests)
        report = ts.write_run_textstate(self.project, run)
        self.assertEqual(report, json.loads((self.project / "run-outputs" / run / "text-state.json").read_text()))
        return {r["sha256"]: r["state"] for r in report["records"]}

    def test_reviewed_pdf_is_text_reviewed_in_a_run_that_extracts_nothing_new(self):
        digest = self.pdf()
        finish_extract(self.database, digest)
        self.assertEqual(self.state("r1", [digest]), {digest: "ocr_unreviewed"})
        self.assertEqual(mark_ocr(self.project, digest, "confirmed"), 0)
        self.assertEqual(self.state("r2"), {digest: "text_reviewed"})

    def test_unreviewed_or_rejected_pdf_is_never_text_reviewed(self):
        for decision, expected in ((None, "ocr_unreviewed"), ("rejected", "could_not_be_read")):
            with self.subTest(decision=decision):
                digest = self.pdf()
                finish_extract(self.database, digest)
                run = f"r-{decision}"
                self.assertEqual(self.state(run + "-1", [digest])[digest], "ocr_unreviewed")
                if decision:
                    self.assertEqual(mark_ocr(self.project, digest, decision), 0)
                self.assertEqual(self.state(run + "-2")[digest], expected)
                self.assertEqual(self.state(run + "-3", [digest])[digest], expected)  # relisted: the same answer

    def test_record_extracted_two_runs_ago_and_reviewed_since_is_text_reviewed_now(self):
        digest = self.pdf()
        finish_extract(self.database, digest)
        self.assertEqual(self.state("r1", [digest])[digest], "ocr_unreviewed")
        self.assertEqual(self.state("r2")[digest], "ocr_unreviewed")
        self.assertEqual(mark_ocr(self.project, digest, "confirmed"), 0)
        self.assertEqual(self.state("r3")[digest], "text_reviewed")
        # The earlier runs keep what they said then.
        old = json.loads((self.project / "run-outputs" / "r1" / "text-state.json").read_text())
        self.assertEqual([r["state"] for r in old["records"]], ["ocr_unreviewed"])

    def test_record_extracted_before_this_change_is_covered_with_no_list_entry_ever(self):
        digest = self.pdf()
        finish_extract(self.database, digest)
        self.assertEqual(mark_ocr(self.project, digest, "confirmed"), 0)
        self.assertEqual(self.state("r1"), {digest: "text_reviewed"})
        report = json.loads((self.project / "run-outputs" / "r1" / "text-state.json").read_text())
        self.assertEqual(report["counts"]["text_reviewed"], 1)

    def test_only_received_records_with_a_done_extract_are_added(self):
        done = self.pdf()
        finish_extract(self.database, done)
        waiting = self.pdf()  # extract still pending: this run's extract owns it
        outside = self.pdf(scope="out_of_scope")
        finish_extract(self.database, outside)
        self.assertEqual(mark_ocr(self.project, done, "confirmed"), 0)
        self.assertEqual(self.state("r1"), {done: "text_reviewed"})
        self.assertNotIn(waiting, self.state("r2"))
        self.assertNotIn(outside, self.state("r3"))

    def test_a_listed_record_is_not_listed_twice_and_keeps_its_listed_outcome(self):
        digest = self.pdf()
        finish_extract(self.database, digest)
        write_extract_list(self.project, "r1", [])
        path = self.processing / "runs" / "extract-r1.json"
        data = json.loads(path.read_text())
        data["documents"] = [{"original_sha256": digest, "form": "pdf", "outcome": "CORRUPT_DOCUMENT"}]
        path.write_text(json.dumps(data))
        report = ts.write_run_textstate(self.project, "r1")
        self.assertEqual([(r["sha256"], r["state"]) for r in report["records"]], [(digest, "could_not_be_read")])

    def test_run_with_no_received_records_writes_an_empty_valid_text_state(self):
        self.assertEqual(self.state("r1"), {})
        directory = self.project / "run-outputs" / "r1"
        report = json.loads((directory / "text-state.json").read_text())
        self.assertEqual((report["schema"], report["run_id"], report["records"], set(report["counts"].values())),
                         ("records-text-state-v1", "r1", [], {0}))
        self.assertEqual((directory / "text-state.md").read_text(), ts.markdown(report))

    def test_a_broken_review_chain_still_writes_nothing(self):
        digest = self.pdf()
        finish_extract(self.database, digest)
        self.assertEqual(mark_ocr(self.project, digest, "confirmed"), 0)
        reviews = self.project / "ocr-review-v1" / "reviews.jsonl"
        reviews.write_bytes(reviews.read_bytes().replace(b'"prev_line_sha256":null', b'"prev_line_sha256":"' + b"5" * 64 + b'"'))
        write_extract_list(self.project, "r1", [])
        with self.assertRaises(OSError):
            ts.write_run_textstate(self.project, "r1")
        self.assertFalse((self.project / "run-outputs" / "r1").exists())


if __name__ == "__main__":
    unittest.main()
