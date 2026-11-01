"""flock_requests.py reconcile STORE --report REPORT.json: REQUESTS rows against the records owner's correspondence report
export; disagreements, unmatched rows and report-only counts named, exit 3 on any; read-only (DF1537-05). Synthetic only."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "flock_requests.py"
HEADER = ("request_id,agency,sent,receipt_evidence,receipt_date,determination_date,extension_notice_date,status,"
          "source_sha256")
SNAP = "ab" * 32
TAIL = f" | report as_of 2026-10-20T00:00:00+00:00 snapshot {SNAP[:12]}"
SENTINEL = "ZQXSENTINEL"


def run(*args):
    return subprocess.run([sys.executable, "-B", str(TOOL), *args], capture_output=True, text=True, check=False)


def row(rid, agency, sent, status="open", det=""):
    return ",".join((rid, agency, sent, "", "", det, "", status, ""))


def agency(aid, name):
    return {"id": aid, "name": name, "jurisdiction": "Sample County"}


def req(lid, aid, ref, submitted, status="open", det=None):
    return {"id": lid, "agency_id": aid, "external_ref": ref, "submitted_at": submitted, "determination_due_at": None,
            "determination_at": det, "production_at": None, "status": status, "source": {"table": "requests", "id": lid},
            "typed_join_ids": [], "review": None}


def total(rows, matched=0, d=0, u=0, r=0, x=0):
    return f"rows={rows} matched={matched} disagreements={d} unmatched={u} report_only={r} unresolved={x}" + TAIL


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        self.store = self.root / "store"

    def rows(self, slug, *rows):
        path = self.store / "agencies" / slug / "REQUESTS.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join((HEADER, *rows)) + "\n", encoding="utf-8")

    def report(self, agencies, requests, name="report.json", **top):
        data = {"schema": "correspondence-report-v1", "as_of": "2026-10-20T00:00:00+00:00", "snapshot_sha256": SNAP,
                "agencies": agencies, "requests": requests, **top}
        path = self.root / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def go(self, path, code):
        done = run("reconcile", str(self.store), "--report", str(path))
        self.assertEqual((done.returncode, done.stderr), (code, ""), done.stdout + done.stderr)
        return done.stdout.splitlines()

    def refused(self, path, message, store=None):
        done = run("reconcile", str(store or self.store), "--report", str(path))
        self.assertEqual((done.returncode, done.stdout), (1, ""), done.stderr)
        self.assertIn(message, done.stderr)
        self.assertNotIn(SENTINEL, done.stderr)

    def test_exact_match_prints_only_the_count_and_writes_nothing(self):
        self.rows("alpha-city-police", row("26-001", "Alpha City Police", "2026-10-01"))
        path = self.report([agency(7, "Alpha City Police")], [req(70, 7, " 26-001 ", "2026-10-01")])
        before = sorted((str(p), p.stat().st_mtime_ns) for p in self.root.rglob("*"))
        self.assertEqual(self.go(path, 0), [total(1, matched=1)])
        self.assertEqual(sorted((str(p), p.stat().st_mtime_ns) for p in self.root.rglob("*")), before)

    def test_aware_timestamp_compared_on_its_pacific_day(self):
        self.rows("alpha-city-police", row("26-001", "Alpha City Police", "2026-10-01"))
        same = self.report([agency(7, "Alpha City Police")], [req(70, 7, "26-001", "2026-10-02T06:30:00+00:00")])
        self.assertEqual(self.go(same, 0), [total(1, matched=1)])
        later = self.report([agency(7, "Alpha City Police")], [req(70, 7, "26-001", "2026-10-02T07:30:00Z")], "b.json")
        self.assertEqual(self.go(later, 3), ["reconcile | alpha-city-police | 26-001 | sent 2026-10-01 vs submitted 2026-10-02",
                                             total(1, matched=1, d=1)])

    def test_naive_or_junk_timestamp_is_unresolved_never_a_disagreement(self):
        self.rows("alpha-city-police", row("26-001", "Alpha City Police", "2026-10-01"),
                  row("26-002", "Alpha City Police", "2026-10-03"))
        path = self.report([agency(7, "Alpha City Police")], [req(70, 7, "26-001", "2026-10-01T10:00:00"),
                                                              req(71, 7, "26-002", "2026-10-03", det="soon")])
        self.assertEqual(self.go(path, 0), [
            "reconcile | alpha-city-police | 26-001 | sent 2026-10-01 vs submitted unresolved",
            "reconcile | alpha-city-police | 26-002 | determination - vs unresolved",
            total(2, matched=2, x=2)])

    def test_determination_and_status_disagreements_both_ways(self):
        self.rows("alpha-city-police", row("26-001", "Alpha City Police", "2026-10-01"),
                  row("26-002", "Alpha City Police", "2026-10-01", "determined", "2026-10-09"))
        path = self.report([agency(7, "Alpha City Police")], [
            req(70, 7, "26-001", "2026-10-01", "determined", det="2026-10-05T12:00:00-07:00"),
            req(71, 7, "26-002", "2026-10-01", "open")])
        self.assertEqual(self.go(path, 3), [
            "reconcile | alpha-city-police | 26-001 | determination - vs 2026-10-05",
            "reconcile | alpha-city-police | 26-001 | status open vs determined",
            "reconcile | alpha-city-police | 26-002 | determination 2026-10-09 vs -",
            "reconcile | alpha-city-police | 26-002 | status determined vs open",
            total(2, matched=2, d=4)])

    def test_status_outside_order_is_not_comparable_and_not_counted(self):
        self.rows("alpha-city-police", row("26-001", "Alpha City Police", "2026-10-01"),
                  row("26-002", "Alpha City Police", "2026-10-01"))
        path = self.report([agency(7, "Alpha City Police")], [req(70, 7, "26-001", "2026-10-01", "pending_review"),
                                                              req(71, 7, "26-002", "2026-10-01", f"Open {SENTINEL}!")])
        self.assertEqual(self.go(path, 0), ["reconcile | alpha-city-police | 26-001 | status not comparable (pending_review)",
                                            "reconcile | alpha-city-police | 26-002 | status not comparable (other)",
                                            total(2, matched=2)])

    def test_not_in_report_and_report_only_counted_without_ids(self):
        self.rows("alpha-city-police", row("26-001", "Alpha City Police", "2026-10-01"),
                  row("26-002", "Alpha City Police", "2026-10-02"))
        self.rows("beta-sheriff", row("B-1", "Beta Sheriff", "2026-10-02"))
        path = self.report([agency(7, "Alpha City Police"), agency(8, "Gamma Office")], [
            req(70, 7, "26-001", "2026-10-01"), req(71, 7, "R-901", "2026-09-01"), req(72, 7, None, "2026-09-02"),
            req(80, 8, "G-1", "2026-09-03")])  # an agency REQUESTS does not track is never counted
        lines = self.go(path, 3)
        self.assertEqual(lines, ["reconcile | alpha-city-police | 26-002 | not in report",
                                 "reconcile | alpha-city-police | report-only requests=2",
                                 "reconcile | beta-sheriff | B-1 | not in report", total(3, matched=1, u=2, r=2)])
        self.assertNotIn("R-901", "\n".join(lines))

    def test_duplicate_external_ref_in_one_agency_is_unmatched_not_guessed(self):
        self.rows("alpha-city-police", row("26-001", "Alpha City Police", "2026-10-01"))
        path = self.report([agency(7, "Alpha City Police")], [req(70, 7, "26-001", "2026-10-01"),
                                                              req(71, 7, "26-001 ", "2026-10-04", "closed")])
        self.assertEqual(self.go(path, 3), ["reconcile | alpha-city-police | 26-001 | report request ambiguous",
                                            total(1, u=1)])

    def test_two_report_agencies_with_one_slug_are_ambiguous_and_skipped(self):
        self.rows("alpha-city-police", row("26-001", "Alpha City Police", "2026-10-01"),
                  row("26-002", "Alpha City Police", "2026-10-01"))
        self.rows("beta-sheriff", row("B-1", "Beta Sheriff", "2026-10-02"))
        path = self.report([agency(7, "Alpha City Police"), agency(9, "ALPHA  city-police"), agency(8, "Beta Sheriff")],
                           [req(70, 7, "26-001", "2026-10-01"), req(90, 9, "26-002", "2026-10-01"),
                            req(80, 8, "B-1", "2026-10-02")])
        self.assertEqual(self.go(path, 3), ["reconcile | alpha-city-police | report agency ambiguous for alpha-city-police",
                                            total(3, matched=1, u=2)])

    def test_bad_reports_refused_with_nothing_on_stdout(self):
        self.rows("alpha-city-police", row("26-001", "Alpha City Police", "2026-10-01"))
        good = self.report([agency(7, "Alpha City Police")], [req(70, 7, "26-001", "2026-10-01")])
        self.refused(self.report([], [], "s.json", schema=f"correspondence-report-v2 {SENTINEL}"),
                     "report: schema is not correspondence-report-v1")
        link = self.root / "link.json"
        link.symlink_to(good)
        self.refused(link, "report: must not be a symlink")
        self.refused(TOOL, "report: is inside the repository")
        bad = self.root / "bad.json"
        bad.write_text('{"schema": "correspondence-report-v1", "' + SENTINEL, encoding="utf-8")
        self.refused(bad, "report: unreadable or not JSON (JSONDecodeError)")
        self.refused(self.report([], {"0": req(70, 7, "26-001", "2026-10-01")}, "r.json"),
                     "report: agencies and requests must be lists of objects")
        self.refused(self.report(None, [], "a.json"), "report: agencies and requests must be lists of objects")
        self.refused(self.report([], ["26-001"], "o.json"), "report: agencies and requests must be lists of objects")
        self.refused(self.root, "report: must be a regular file of at most 64 MiB")
        self.refused(self.root / "missing.json", "report: must be a regular file of at most 64 MiB")
        big = self.root / "big.json"
        with open(big, "wb") as fh:  # sparse: no 64 MiB written
            fh.truncate((64 << 20) + 1)
        self.refused(big, "report: must be a regular file of at most 64 MiB")
        fifo = self.root / "fifo.json"
        os.mkfifo(fifo)
        self.refused(fifo, "report: must be a regular file of at most 64 MiB")

    def test_requests_check_error_refused_before_any_line(self):
        self.rows("alpha-city-police", row("26-001", "Alpha City Police", "2026-10-01"),
                  row("26-002", "Alpha City Police", "2026-10-01", "pending"))
        path = self.report([agency(7, "Alpha City Police")], [req(70, 7, "26-001", "2026-10-02")])
        self.refused(path, "alpha-city-police: row 3: status must be one of")
        (self.root / "empty").mkdir()
        self.refused(path, "store: no agencies/*/REQUESTS.csv found", store=self.root / "empty")

    def test_report_text_fields_never_printed(self):
        self.rows("alpha-city-police", row("26-001", "Alpha City Police", "2026-10-01"))
        q = req(f"L-{SENTINEL}", f"A-{SENTINEL}", "26-001", "2026-10-03T09:00:00+00:00", "closed")
        q.update(typed_join_ids=[f"J-{SENTINEL}"], review={"reviewed_by": f"R {SENTINEL}", "authority": SENTINEL,
                                                          "status": SENTINEL}, source={"id": SENTINEL})
        other = req(f"M-{SENTINEL}", f"A-{SENTINEL}", f"X-{SENTINEL}", "2026-10-01")
        a = {"id": f"A-{SENTINEL}", "name": "Alpha City Police", "jurisdiction": SENTINEL}
        path = self.report([a], [q, other], as_of=SENTINEL, snapshot_sha256=SENTINEL, ledger_meta={"x": SENTINEL})
        done = run("reconcile", str(self.store), "--report", str(path))
        self.assertEqual(done.returncode, 3)
        self.assertNotIn(SENTINEL, done.stdout + done.stderr)
        self.assertEqual(done.stdout.splitlines(), [
            "reconcile | alpha-city-police | 26-001 | sent 2026-10-01 vs submitted 2026-10-03",
            "reconcile | alpha-city-police | 26-001 | status open vs closed",
            "reconcile | alpha-city-police | report-only requests=1",
            "rows=1 matched=1 disagreements=2 unmatched=0 report_only=1 unresolved=0 | report as_of unresolved snapshot -"])

    def test_usage_and_help(self):
        self.assertIn("reconcile STORE --report REPORT.json", run("--help").stdout)
        for args in (["reconcile", "S"], ["reconcile", "S", "--reprot", "R"], ["reconcile", "S", "--report", "R", "x"]):
            done = run(*args)
            self.assertEqual((done.returncode, done.stdout), (2, ""), args)
            self.assertTrue(done.stderr.startswith("usage: flock_requests.py"), args)


if __name__ == "__main__":
    unittest.main()
