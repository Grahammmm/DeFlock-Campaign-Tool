"""Acceptance tests for ``run_report_review.py summary --dir --since/--until`` (RF1300-23); synthetic reports only."""
import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import run_report_review as rrr  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "run_report"
EXAMPLE = ROOT / "examples" / "run-report" / "records-run-report-v1.synthetic.json"
TAIL = " | machine-valid is not reviewed"
# No-window golden: int(sha256("<exit>\n<stdout>\n<stderr>")[:15], 16), captured from origin/main e3d9cfbb (main + RF1300-22)
# before this change (integers, not hex strings, so the secret scanner does not flag them).
GOLDEN = {
    "fixtures --dir": 907452926598807666, "fixtures --dir --json": 244418336415716575,
    "three --dir": 1104284421684734845, "three --dir --json": 581678136897286534,
    "pair --dir": 469028680066098503, "pair --dir --json": 943134862312289659,
    "single": 841104602051215918, "single --json": 669955548633236609,
}


def report(started, failed=False, tag="0a1b2c3d"):
    """The synthetic example re-timed to start at ``started`` (YYYY-MM-DDTHH:MM:SSZ); optionally a failed portal stage."""
    r = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    start = datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    r["started_at"], r["finished_at"] = started, (start + timedelta(seconds=450)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r["run_id"] = f"run_{start:%Y%m%dT%H%M%S}Z_{tag}"
    if failed:
        portal = next(s for s in r["stages"] if s["name"] == "portal")
        portal["status"], portal["error_code"], r["overall_status"] = "fail", "PORTAL_FETCH_BLOCKED", "fail"
    return r


OCT20, OCT27, NOV1 = "2026-10-20T09:00:00Z", "2026-10-27T09:00:00Z", "2026-11-01T09:00:00Z"
SINCE, UNTIL = "2026-10-26T00:00:00Z", "2026-10-28T00:00:00Z"


class RunReportReviewWindowTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def folder(self, name, files):
        d = self.tmp / name
        d.mkdir()
        for fname, content in files.items():
            (d / fname).write_bytes(content if isinstance(content, bytes) else json.dumps(content).encode())
        return d

    def three(self):  # the Oct 20 run failed
        return self.folder("three", {"a.json": report(OCT20, failed=True), "b.json": report(OCT27), "c.json": report(NOV1)})

    def pair(self):  # same run_id, contents differ: one copy started Oct 20 (outside the window), one Oct 27
        late, early = report(OCT27), report(OCT27)
        early["started_at"], early["finished_at"] = OCT20, "2026-10-20T09:07:30Z"
        return self.folder("pair", {"a.json": early, "b.json": late})

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = rrr.main(["summary", *map(str, argv)])
        return code, out.getvalue(), err.getvalue()

    def reviewed(self, out):
        return [line.split(" | ")[1] for line in out.splitlines() if line.endswith(" | VALID | yes")]

    # 1: since only; an old failed run outside the window no longer forces exit 1
    def test_since_excludes_old_failed_run(self):
        d = self.three()
        self.assertEqual(self.run_main("--dir", d)[0], 1)
        code, out, err = self.run_main("--dir", d, "--since", SINCE)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.reviewed(out), [report(OCT27)["run_id"], report(NOV1)["run_id"]])
        self.assertNotIn(report(OCT20)["run_id"], out)
        self.assertEqual(out.splitlines()[-1], "review | reports=2 valid=2 invalid=0 duplicates=0 excluded=1 "
                                               f"window={SINCE}..open{TAIL}")

    # 2: until; since is inclusive, until is exclusive
    def test_until_and_boundaries(self):
        code, out, _ = self.run_main("--dir", self.three(), "--since", SINCE, "--until", UNTIL)
        self.assertEqual((code, self.reviewed(out)), (0, [report(OCT27)["run_id"]]))
        self.assertEqual(out.splitlines()[-1], "review | reports=1 valid=1 invalid=0 duplicates=0 excluded=2 "
                                               f"window={SINCE}..{UNTIL}{TAIL}")
        d = self.folder("edges", {"at_since.json": report(SINCE), "at_until.json": report(UNTIL, failed=True),
                                  "before.json": report("2026-10-25T23:59:59Z", failed=True)})
        code, out, _ = self.run_main("--dir", d, "--since", SINCE, "--until", UNTIL)
        self.assertEqual((code, self.reviewed(out)), (0, [report(SINCE)["run_id"]]))
        self.assertIn(" excluded=2 ", out.splitlines()[-1])

    # 3: an invalid report is always shown and exits 1, whatever the window
    def test_invalid_report_always_shown(self):
        bad_time = report(OCT27)
        bad_time["started_at"] = "not a time"
        d = self.folder("bad", {"a.json": b"{not json", "b.json": bad_time, "c.json": report(OCT27)})
        for window in ((), ("--since", SINCE), ("--since", "2030-01-01T00:00:00Z"),
                       ("--since", "2000-01-01T00:00:00Z", "--until", "2000-01-02T00:00:00Z")):
            with self.subTest(window):
                code, out, _ = self.run_main("--dir", d, *window)
                self.assertEqual(code, 1)
                self.assertEqual(sum(line.split(" | ")[2:4] == ["VALID", "no"] for line in out.splitlines()), 2)
                self.assertIn(" invalid=2 ", out.splitlines()[-1])
        code, doc = self.json_doc("--dir", d, "--since", "2030-01-01T00:00:00Z")
        self.assertEqual((code, doc["counts"]), (1, {"reports": 2, "valid": 0, "invalid": 2, "duplicates": 0, "excluded": 1}))

    # 4: duplicate detection compares kept reports only
    def test_duplicate_outside_window_not_compared(self):
        d = self.pair()
        code, out, _ = self.run_main("--dir", d)
        self.assertEqual((code, out.count("| DUPLICATE | run_id | contents differ")), (1, 2))
        code, out, _ = self.run_main("--dir", d, "--since", SINCE)
        self.assertEqual(code, 0)
        self.assertNotIn("DUPLICATE", out)
        self.assertIn("duplicates=0 excluded=1 ", out.splitlines()[-1])
        code, out, _ = self.run_main("--dir", d, "--since", "2026-10-01T00:00:00Z")  # both kept: still a duplicate
        self.assertEqual((code, out.count("| DUPLICATE | run_id | contents differ")), (1, 2))
        self.assertIn("duplicates=2 excluded=0 ", out.splitlines()[-1])

    # 5: refusals, exit 2, stderr only
    def test_refused_windows(self):
        d, single = self.three(), self.tmp / "one.json"
        single.write_text(json.dumps(report(OCT27)), encoding="utf-8")
        cases = {
            "single since": ([single, "--since", SINCE], "--since and --until need --dir"),
            "single until": ([single, "--until", UNTIL], "--since and --until need --dir"),
            "calendar": (["--dir", d, "--since", "2026-02-30T00:00:00Z"], "--since is not a real UTC date and time"),
            "shape": (["--dir", d, "--since", "2026-10-26"], "--since must be YYYY-MM-DDTHH:MM:SSZ"),
            "until shape": (["--dir", d, "--since", SINCE, "--until", "2026-10-28T00:00:00+00:00"],
                            "--until must be YYYY-MM-DDTHH:MM:SSZ"),
            "equal": (["--dir", d, "--since", SINCE, "--until", SINCE], "--until must be after --since"),
            "before": (["--dir", d, "--since", UNTIL, "--until", SINCE], "--until must be after --since"),
            "until alone": (["--dir", d, "--until", UNTIL], "--until needs --since"),
        }
        for name, (argv, reason) in cases.items():
            for extra in ((), ("--json",)):
                with self.subTest(name, json=bool(extra)):
                    self.assertEqual(self.run_main(*argv, *extra), (2, "", f"review | refused | {reason}\n"))

    # 6: no window -> stdout, stderr and exit byte-identical to main + RF1300-22, text and JSON
    def test_no_window_golden(self):
        single = self.tmp / "one.json"
        single.write_text(json.dumps(report(OCT27)), encoding="utf-8")
        cases = {"fixtures --dir": ["--dir", FIXTURES], "three --dir": ["--dir", self.three()],
                 "pair --dir": ["--dir", self.pair()], "single": [single]}
        for name, argv in cases.items():
            for suffix, extra in (("", ()), (" --json", ("--json",))):
                with self.subTest(name + suffix):
                    code, out, err = self.run_main(*argv, *extra)
                    digest = int(hashlib.sha256(f"{code}\n{out}\n{err}".encode()).hexdigest()[:15], 16)
                    self.assertEqual(digest, GOLDEN[name + suffix], name + suffix)
                    if extra:
                        doc = json.loads(out)
                        self.assertNotIn("window", doc)
                        self.assertNotIn("excluded", doc["counts"])

    def json_doc(self, *argv):
        code, out, err = self.run_main(*argv, "--json")
        self.assertEqual(err, "")
        return code, json.loads(out)

    # 7: --json with a window
    def test_json_window(self):
        d = self.three()
        code, doc = self.json_doc("--dir", d, "--since", SINCE, "--until", UNTIL)
        self.assertEqual(code, 0)
        self.assertEqual(doc["window"], {"since": SINCE, "until": UNTIL})
        self.assertEqual(doc["counts"], {"reports": 1, "valid": 1, "invalid": 0, "duplicates": 0, "excluded": 2})
        self.assertEqual([r["run_id"] for r in doc["reports"]], [report(OCT27)["run_id"]])
        dumped = json.dumps(doc)
        for gone in (OCT20, NOV1):
            self.assertNotIn(report(gone)["run_id"], dumped)
        code, doc = self.json_doc("--dir", d, "--since", SINCE)
        self.assertEqual((code, doc["window"], doc["counts"]["excluded"]), (0, {"since": SINCE, "until": None}, 1))
        code, doc = self.json_doc("--dir", self.pair(), "--since", SINCE)
        self.assertEqual((code, doc["duplicates"], doc["counts"]["duplicates"]), (0, [], 0))


if __name__ == "__main__":
    unittest.main()
