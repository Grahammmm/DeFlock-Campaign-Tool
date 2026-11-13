"""HOLD-INSPECT-1: ``records holds`` lists held extract documents from a read-only ledger snapshot."""
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from campaign_tool.records import holds  # noqa: E402
from campaign_tool.records import native_extract as ne  # noqa: E402
from campaign_tool.records import run as records_run  # noqa: E402
from campaign_tool.records.ledger import store  # noqa: E402
from campaign_tool.records.ledger.migrations import v001  # noqa: E402

RUN = "run_20261113T150000Z_0a1b2c3d"
PRIVATE = "/srv/records/Agency Records/jane.doe@example.org Subject line.pdf"
REFUSED = "extract:{}:extraction_check_refused:accept_status_rejected"


def digest(n):
    return f"{n:x}" * 64  # n < 16: distinct 64-hex digests and distinct 12-hex prefixes


def make_project(root, docs, initialized=False):
    """docs: (n, preserved, alert_key_suffix or None, alert_count); ``initialized``: a store.initialize ledger."""
    processing = Path(root) / "processing-v1"
    processing.mkdir(parents=True, mode=0o700)
    if initialized:
        mask = os.umask(0o077)
        try:
            store.initialize(processing / "ledger.sqlite")
        finally:
            os.umask(mask)
    with closing(sqlite3.connect(processing / "ledger.sqlite")) as db:
        if not initialized:
            db.executescript(v001.SQL)
        db.execute("INSERT INTO runs VALUES ('r1','records-runner','2026-11-01T00:00:00Z',NULL,'e',NULL,NULL,"
                   "NULL,'completed','{}')")
        for n, preserved, suffix, count in docs:
            sha = digest(n)
            db.execute("INSERT INTO originals(sha256,bytes,first_seen_at,role,scope,storage_path,"
                       "preservation_status,provenance_json) VALUES (?,1,?,'agency_original','in_scope',?,?,?)",
                       (sha, "2026-10-01T00:00:00+00:00", PRIVATE, "preserved" if preserved else "pending",
                        json.dumps({"subject": PRIVATE})))
            db.execute("INSERT INTO stage_state VALUES (?,'extract','pending',NULL,'runtime','t','r1',NULL)", (sha,))
            db.execute("INSERT INTO stage_state VALUES (?,'preserve',?,?,'runtime','t','r1',NULL)",
                       (sha, "done" if preserved else "pending", "x" if preserved else None))
            if suffix:
                key = suffix.format(sha) if "{}" in suffix else f"extract:{sha}:{suffix}"
                db.execute("INSERT INTO alerts VALUES (?,?,?,?,?,'runtime','open')",
                           (sha[-32:], key, "2026-11-01T07:00:00+00:00", "2026-11-13T07:00:00+00:00", count))
        db.commit()
    return processing


def write_report(path, needs):
    rows = [{"code": c, "stage": s, "severity": "info", "count": n} for s, c, n in needs]
    Path(path).write_text(json.dumps({"run_id": RUN, "needs_g": rows}), encoding="utf-8")


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = holds.main(argv)
    return code, out.getvalue(), err.getvalue()


DOCS = [(1, True, "extraction_stage_error", 3), (2, True, "extraction_stage_error", 3),
        (3, True, REFUSED, 1), (4, False, None, 0), (5, False, None, 0), (6, False, None, 0),
        (7, True, "ocr_runtime_unconfigured", 1)]


class HoldsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.project = self.root / "project"
        make_project(self.project, DOCS)

    def test_counts_dispositions_and_metadata_only(self):
        code, out, _ = run_main(["--project", str(self.project), "--json"])
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(doc["schema"], "records-holds-v1")
        self.assertEqual(doc["counts"], {"EXTRACT_ERROR": 2, "EXTRACT_NOT_ACCEPTED": 1,
                                         "PRESERVE_PENDING": 3, "AWAITING_OCR": 1})
        by_code = {}
        for item in doc["items"]:
            by_code.setdefault(item["code"], set()).add(item["disposition"])
        self.assertEqual(by_code, {"EXTRACT_ERROR": {"retryable"}, "EXTRACT_NOT_ACCEPTED": {"review_required"},
                                   "PRESERVE_PENDING": {"custodian"}, "AWAITING_OCR": {"retryable"}})
        errors = [i for i in doc["items"] if i["code"] == "EXTRACT_ERROR"]
        self.assertEqual([i["attempts"] for i in errors], [3, 3])
        refused = next(i for i in doc["items"] if i["code"] == "EXTRACT_NOT_ACCEPTED")
        self.assertEqual(refused["detail_code"], "accept_status_rejected")
        self.assertEqual(sum(not i["preserved"] for i in doc["items"]), 3)
        self.assertTrue(all(len(i["sha256_prefix"]) == 12 for i in doc["items"]))
        # Acceptance 2: no path, subject or address in the output.
        self.assertFalse(any("/" in json.dumps(item) for item in doc["items"]))
        for text in (out, run_main(["--project", str(self.project)])[1]):
            self.assertNotIn("@", text)
            self.assertNotIn("Subject line", text)
            self.assertNotIn(digest(1), text)

    def test_ledger_opened_read_only(self):
        real = sqlite3.connect
        with mock.patch("sqlite3.connect", side_effect=real) as connect:
            code, _, _ = run_main(["--project", str(self.project), "--json"])
        self.assertEqual(code, 0)
        self.assertTrue(connect.call_args_list)
        for call in connect.call_args_list:
            self.assertIn("mode=ro", call.args[0])
            self.assertTrue(call.kwargs.get("uri"))

    def test_count_mismatch_against_report(self):
        report = self.root / "run-report.json"
        write_report(report, [("extract", "EXTRACT_ERROR", 3), ("extract", "EXTRACT_NOT_ACCEPTED", 1),
                              ("extract", "EXTRACT_PRESERVE_PENDING", 3), ("extract", "AWAITING_OCR", 1),
                              ("adoption", "ADOPTION_CODED_GAPS", 120)])
        code, out, _ = run_main(["--project", str(self.project), "--report", str(report)])
        self.assertEqual(code, 0)
        self.assertIn("COUNT_MISMATCH EXTRACT_ERROR 2 3", out.splitlines())
        self.assertEqual(sum(line.startswith("COUNT_MISMATCH") for line in out.splitlines()), 1)
        self.assertIn("NOT_IN_LEDGER ADOPTION_CODED_GAPS 120", out)
        _, out, err = run_main(["--project", str(self.project), "--report", str(report), "--json"])
        self.assertEqual(json.loads(out)["report"]["mismatches"],
                         [{"code": "EXTRACT_ERROR", "ledger": 2, "report": 3}])
        self.assertIn("COUNT_MISMATCH EXTRACT_ERROR 2 3", err)

    def test_run_list_codes_an_unalerted_pending_document_and_code_filter(self):
        # 8: image without an alert; 9: deferred; 10: retryable alert below MAX_ATTEMPTS, deferred this run.
        docs = [(8, True, None, 0), (9, True, None, 0), (10, True, "extraction_stage_error", 1)]
        make_project(self.root / "p2", docs)
        runs = self.root / "p2" / "processing-v1" / "runs"
        runs.mkdir()
        (runs / f"extract-{RUN}.json").write_text(json.dumps({
            "schema": "records-extract-outcomes-v1", "run_id": RUN, "documents": [
                {"original_sha256": digest(8), "form": "png", "outcome": "AWAITING_OCR"},
                {"original_sha256": digest(9), "form": "pdf", "outcome": "DEFERRED_RUN_BUDGET"},
                {"original_sha256": digest(10), "form": "pdf", "outcome": "DEFERRED_RUN_BUDGET"}]}))
        report = self.root / "r2.json"
        write_report(report, [("extract", "AWAITING_OCR", 1), ("extract", "EXTRACT_DEFERRED", 2)])
        _, out, err = run_main(["--project", str(self.root / "p2"), "--report", str(report), "--json"])
        doc = json.loads(out)
        self.assertEqual(doc["counts"], {"AWAITING_OCR": 1, "DEFERRED_RUN_BUDGET": 2})
        self.assertEqual([(i["code"], i["attempts"]) for i in doc["items"]],
                         [("AWAITING_OCR", 0), ("DEFERRED_RUN_BUDGET", 0), ("DEFERRED_RUN_BUDGET", 1)])
        self.assertEqual(err, "")
        _, out, _ = run_main(["--project", str(self.root / "p2"), "--report", str(report),
                              "--code", "AWAITING_OCR", "--json"])
        self.assertEqual([i["code"] for i in json.loads(out)["items"]], ["AWAITING_OCR"])

    def test_project_only_uses_newest_run_list_and_reconciles(self):
        # No --report: 8 is an image with no alert (AWAITING_OCR in the newest list); 11 is a held error;
        # 12 was extracted since that run, so the run counted 2 AWAITING_OCR and the ledger holds 1.
        make_project(self.root / "p3", [(8, True, None, 0), (11, True, "extraction_stage_error", 3)])
        runs = self.root / "p3" / "processing-v1" / "runs"
        runs.mkdir()
        old = "run_20261112T150000Z_0a1b2c3d"
        for run_id, docs in ((old, [(8, "DEFERRED_RUN_BUDGET")]),
                             (RUN, [(8, "AWAITING_OCR"), (11, "EXTRACT_ERROR"), (12, "AWAITING_OCR")])):
            (runs / f"extract-{run_id}.json").write_text(json.dumps({
                "schema": "records-extract-outcomes-v1", "run_id": run_id, "documents": [
                    {"original_sha256": digest(n), "form": "png", "outcome": o} for n, o in docs]}))
        (runs / f"extract-failures-{RUN}.json").write_text("{}")
        code, out, err = run_main(["--project", str(self.root / "p3"), "--json"])
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(doc["counts"], {"AWAITING_OCR": 1, "EXTRACT_ERROR": 1})
        self.assertEqual((doc["report"]["source"], doc["report"]["run_id"]), ("run_list", RUN))
        self.assertEqual(doc["report"]["mismatches"], [{"code": "AWAITING_OCR", "ledger": 1, "report": 2}])
        self.assertEqual(err.splitlines(), ["COUNT_MISMATCH AWAITING_OCR 1 2"])
        _, out, _ = run_main(["--project", str(self.root / "p3")])
        self.assertIn("COUNT_MISMATCH AWAITING_OCR 1 2", out.splitlines())
        # No outcome list and no report: said so, never a silent complete listing.
        code, _, err = run_main(["--project", str(self.project), "--json"])
        self.assertEqual((code, err.splitlines()), (0, [
            "RUN_LIST_MISSING (unalerted pending documents are not coded)",
            "NOT_RECONCILED (no run report and no run outcome list)"]))

    def test_unreadable_ledger_exits_2(self):
        code, out, err = run_main(["--project", str(self.root / "absent")])
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(err.strip(), "LEDGER_UNREADABLE missing")
        wal = self.project / "processing-v1" / "ledger.sqlite-wal"
        wal.write_bytes(b"x")
        self.assertEqual(run_main(["--project", str(self.project)])[2].strip(), "LEDGER_UNREADABLE busy")

    def test_cli_dispatch_help(self):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "holds", "--help"],
                              cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("--project", done.stdout)


NOW = datetime(2026, 11, 13, 16, 0, tzinfo=timezone.utc)  # one hour after RUN


def tree(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(Path(root).rglob("*")) if p.is_file()}


class ReleaseTests(unittest.TestCase):
    """HOLD-RELEASE-1: ``records holds release`` resets retryable attempt counters with a receipt."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.project = self.root / "project"
        self.processing = make_project(self.project, [(1, True, "extraction_stage_error", 3),
                                                      (2, True, "extraction_stage_error", 3),
                                                      (3, True, "extraction_stage_error", 3), (4, True, REFUSED, 1),
                                                      (5, False, None, 0)], initialized=True)
        (self.processing / "runs").mkdir()
        (self.processing / "runs" / f"extract-{RUN}.json").write_text(json.dumps({
            "schema": "records-extract-outcomes-v1", "run_id": RUN, "documents": []}))
        self.report = self.root / "run-report.json"
        write_report(self.report, [("extract", "EXTRACT_ERROR", 3)])
        self.receipts = self.root / "receipts"

    def apply(self, limit, **kwargs):
        options = dict(apply=True, limit=limit, receipt=self.receipts, report=self.report, now=NOW)
        return holds.release(self.project, "EXTRACT_ERROR", **{**options, **kwargs})

    def counts(self):
        with closing(sqlite3.connect(self.processing / "ledger.sqlite")) as db:
            return {key.split(":")[1][:1]: n for key, n in db.execute("SELECT key,count FROM alerts ORDER BY key")}

    def refused(self, status, code, **kwargs):
        before = tree(self.project)
        with self.assertRaises(holds.Refused) as caught:
            self.apply(**{"limit": 2, **kwargs})
        self.assertEqual(caught.exception.args, (status, code))
        self.assertEqual(tree(self.project), before)

    def test_dry_run_lists_and_changes_no_byte(self):
        before = tree(self.project)
        code, out, err = run_main(["release", "--project", str(self.project), "--code", "EXTRACT_ERROR"])
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual([line.split()[1:] for line in lines[1:4]], [["EXTRACT_ERROR", "3"]] * 3)
        self.assertIn("eligible 3 selected 3 held_by_other_code 0", lines)
        self.assertTrue(lines[-1].startswith("DRY_RUN nothing changed"))
        self.assertEqual(tree(self.project), before)
        self.assertFalse(self.receipts.exists())

    def test_apply_resets_at_most_max_with_a_receipt_then_the_rest(self):
        doc = self.apply(2)
        self.assertEqual((doc["released"], doc["eligible"]), (2, 3))
        self.assertEqual(self.counts(), {"1": 0, "2": 0, "3": 3, "4": 1})
        receipt = (self.receipts / doc["receipt_file"]).read_text()
        rows = [json.loads(line) for line in receipt.splitlines()]
        self.assertEqual([r["sha256_prefix"] for r in rows], [digest(1)[:12], digest(2)[:12]])
        self.assertEqual({(r["code"], r["attempts_before"], r["released_at"]) for r in rows},
                         {("EXTRACT_ERROR", 3, NOW.isoformat())})
        self.assertEqual(rows[0]["run_report_sha256"], hashlib.sha256(self.report.read_bytes()).hexdigest())
        self.assertNotIn("/", receipt)
        self.assertNotIn(digest(1), receipt)
        self.assertEqual(oct(self.receipts.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct((self.receipts / doc["receipt_file"]).stat().st_mode & 0o777), "0o600")
        second = self.apply(2)
        self.assertEqual((second["released"], [i["sha256_prefix"] for i in second["items"]]), (1, [digest(3)[:12]]))
        self.assertEqual(self.counts(), {"1": 0, "2": 0, "3": 0, "4": 1})
        self.assertEqual((self.apply(2)["released"], len(list(self.receipts.iterdir()))), (0, 2))

    def test_refusals_change_nothing(self):
        for argv, code in ((["--code", "EXTRACT_NOT_ACCEPTED"], "CODE_NOT_RETRYABLE"),
                           (["--code", "EXTRACT_INCOMPLETE"], "CODE_NOT_RETRYABLE"),
                           (["--code", "PRESERVE_PENDING"], "CODE_CUSTODIAN"),
                           (["--code", "EXTRACT_ERROR", "--apply"], "MAX_REQUIRED"),
                           (["--code", "EXTRACT_ERROR", "--apply", "--max", "26"], "MAX_OUT_OF_RANGE")):
            with self.subTest(code=code):
                self.assertEqual(run_main(["release", "--project", str(self.project), *argv])[::2], (2, code + "\n"))
        self.refused(2, "REPORT_REQUIRED", report=None)
        self.refused(2, "REPORT_STALE", now=NOW + timedelta(hours=25))
        write_report(self.root / "old.json", [])
        old = json.loads((self.root / "old.json").read_text())
        (self.root / "old.json").write_text(json.dumps({**old, "run_id": "run_20261113T080000Z_0a1b2c3d"}))
        self.refused(2, "REPORT_NOT_LATEST", report=self.root / "old.json")
        self.refused(2, "RECEIPT_UNSAFE", receipt=self.project / "receipts")
        stale = [(digest(1), [(f"extract:{digest(1)}:extraction_stage_error", 2)])]  # the ledger moved since the read
        with mock.patch.object(holds, "candidates", return_value=(stale, 0)):
            self.refused(3, "LEDGER_CHANGED")
        self.assertEqual(list(self.receipts.iterdir()), [])  # the staged receipt was removed
        self.assertEqual(self.counts(), {"1": 3, "2": 3, "3": 3, "4": 1})

    def test_held_locks_refuse_ledger_busy(self):
        lock = self.root / "run-records.lock"
        lock.touch()
        for path in (lock, self.processing / "run.lock"):
            with self.subTest(lock=path.name), open(path, "a") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                self.refused(3, "LEDGER_BUSY", runtime_lock=lock)
        self.refused(2, "RUNTIME_LOCK_UNREADABLE", runtime_lock=self.root / "absent.lock")
        self.assertEqual(self.apply(1, runtime_lock=lock)["runtime_lock"], "held")
        self.assertEqual(sorted(self.counts().values()), [0, 1, 3, 3])

    def test_another_holding_code_is_not_released(self):
        with closing(sqlite3.connect(self.processing / "ledger.sqlite")) as db:
            db.execute("INSERT INTO alerts VALUES ('x',?,'t','t',1,'runtime','open')", (REFUSED.format(digest(1)),))
            db.commit()
        doc = holds.release(self.project, "EXTRACT_ERROR")
        self.assertEqual((doc["eligible"], doc["held_by_other_code"]), (2, 1))

    def test_a_released_document_is_retried_then_held_again(self):
        self.apply(1)
        pipeline = records_run.Pipeline.__new__(records_run.Pipeline)
        pipeline.root = records_run.Root(self.processing)
        subject = digest(1)
        for attempt in range(ne.MAX_ATTEMPTS):
            self.assertIsNone(ne.held_outcome(pipeline, subject), attempt)  # parsed again on this run
            ne._hold(pipeline, subject, ne.ERROR_CODE)  # ... and failed again
        self.assertEqual(ne.held_outcome(pipeline, subject), ne.ERROR)  # held after MAX_ATTEMPTS: no loop
        self.assertEqual(holds.release(self.project, "EXTRACT_ERROR")["eligible"], 3)


if __name__ == "__main__":
    unittest.main()
