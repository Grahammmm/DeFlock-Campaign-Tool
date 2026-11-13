"""HOLD-INSPECT-1: ``records holds`` lists held extract documents from a read-only ledger snapshot."""
from contextlib import closing, redirect_stderr, redirect_stdout
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
from campaign_tool.records.ledger.migrations import v001  # noqa: E402

RUN = "run_20261113T150000Z_0a1b2c3d"
PRIVATE = "/home/coder/private/Agency Records/jane.doe@example.org Subject line.pdf"
REFUSED = "extract:{}:extraction_check_refused:accept_status_rejected"


def digest(n):
    return f"{n:x}" * 64  # n < 16: distinct 64-hex digests and distinct 12-hex prefixes


def make_project(root, docs):
    """docs: (n, preserved, alert_key_suffix or None, alert_count)."""
    processing = Path(root) / "processing-v1"
    processing.mkdir(parents=True)
    with closing(sqlite3.connect(processing / "ledger.sqlite")) as db:
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


if __name__ == "__main__":
    unittest.main()
