"""flock_facts unreadable: private (agency, sha256, page) list of UNREADABLE rows for the extraction owner (DF1537-15)."""
import contextlib
import csv
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.flock.test_facts import ROOT, TOOL, snapshot

_spec = importlib.util.spec_from_file_location("flock_facts_unreadable_under_test", TOOL)
facts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(facts)

SHA_A = hashlib.sha256(b"synthetic unreadable doc a").hexdigest()
SHA_B = hashlib.sha256(b"synthetic unreadable doc b").hexdigest()
SECRETS = ["Acme Secret Value", "Quote nobody may print.", "REQ-SECRET-9", "unreadable-tester"]
HEAD = "agency,source_sha256,page,fact_rows"


def fact(agency, page, value="UNREADABLE", sha=SHA_A, fact_type="vendor", quote=None):
    row = dict(zip(facts.V1, [agency, "REQ-SECRET-9", fact_type, value, sha, page,
                              ("" if value.strip() == "UNREADABLE" else "Quote nobody may print.") if quote is None else quote]))
    row.update(kind="record", status="unreviewed", effective_from="", effective_to="", added_by="unreadable-tester",
               added_at="2026-10-31")
    row["fact_id"] = facts.fact_id(row)
    return [row[k] for k in facts.V2]


ALPHA = [fact("Alpha", "2", fact_type="t1"), fact("Alpha", "10"), fact("Alpha", "2", fact_type="t2"),
         fact("Alpha", "3", value="Acme Secret Value"), fact("Alpha", "2", fact_type="t3")]
BETA = [fact("Beta", "5", sha=SHA_B)]


class FactsUnreadableTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.store = self.dir / "store"
        (self.dir / "handoff").mkdir()
        self.out = self.dir / "handoff" / "out.csv"

    def agency(self, slug, rows, store=None):
        path = (store or self.store) / "agencies" / slug / "FACTS.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            csv.writer(handle, lineterminator="\r\n").writerows([facts.V2, *rows])

    def golden(self):
        self.agency("beta", BETA)
        self.agency("alpha", ALPHA)

    def run_tool(self, *args, out=None, command="unreadable"):
        before = snapshot(self.store) if self.store.is_dir() else {}
        cmd = [sys.executable, "-B", str(TOOL), command, "--store", str(self.store), "--out", str(out or self.out), *args]
        done = subprocess.run(cmd, capture_output=True, text=True, check=False)
        self.assertEqual(snapshot(self.store) if self.store.is_dir() else {}, before, "unreadable changed the store")
        self.assertNotIn("Traceback", done.stderr)
        return done.returncode, done.stdout.splitlines(), done.stderr.strip()

    def lines(self, path=None):
        data = (path or self.out).read_bytes()
        self.assertNotIn(b"\r", data)
        self.assertTrue(data.endswith(b"\n"))
        return data.decode("utf-8").splitlines()

    @staticmethod
    def summary(agencies, pages, documents, rows, name="out.csv"):
        return ("unreadable | agencies=%d pages=%d documents=%d rows=%d written=%s | hand-off for the extraction owner;"
                " not a review verdict" % (agencies, pages, documents, rows, name))

    def test_golden_rows_numeric_page_and_agency_order(self):
        self.golden()
        self.assertEqual(self.run_tool(), (0, [self.summary(2, 3, 2, 5)], ""))
        self.assertEqual(self.lines(), [HEAD, "alpha,%s,2,3" % SHA_A, "alpha,%s,10,1" % SHA_A, "beta,%s,5,1" % SHA_B])
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in self.out.parent.iterdir()), ["out.csv"], "temp file left behind")

    def test_no_value_quote_request_or_hash_leaks(self):
        self.golden()
        code, out, err = self.run_tool()
        printed, written = "\n".join(out) + err, self.out.read_text(encoding="utf-8")
        for secret in SECRETS:
            self.assertNotIn(secret, printed)
            self.assertNotIn(secret, written)
        self.assertIsNone(re.search(r"[0-9a-f]{64}", printed))
        self.assertNotIn("page", printed.replace("pages=", ""))

    def test_agency_filter_and_unknown_agency(self):
        self.golden()
        self.assertEqual(self.run_tool("--agency", "Beta"), (0, [self.summary(1, 1, 1, 1)], ""))
        self.assertEqual(self.lines(), [HEAD, "beta,%s,5,1" % SHA_B])
        other = self.dir / "handoff" / "gamma.csv"
        self.assertEqual(self.run_tool("--agency", "gamma", out=other), (1, [], "agency gamma: no FACTS.csv"))
        self.assertFalse(os.path.lexists(other))

    def test_none_unreadable_writes_header_only(self):
        self.agency("alpha", [fact("Alpha", "3", value="Acme Secret Value")])
        self.assertEqual(self.run_tool(), (0, [self.summary(1, 0, 0, 0)], ""))
        self.assertEqual(self.lines(), [HEAD])

    def test_marker_rule_matches_coverage(self):
        self.agency("alpha", [fact("Alpha", "4", value=" UNREADABLE "), fact("Alpha", "6", value="unreadable"),
                              fact("Alpha", "7", value="UNREADABLE page"), fact("Alpha", "8", value="Unreadable")])
        self.assertEqual(self.run_tool(), (0, [self.summary(1, 1, 1, 1)], ""))
        self.assertEqual(self.lines(), [HEAD, "alpha,%s,4,1" % SHA_A])

    def test_check_errors_refuse_without_writing(self):
        self.golden()
        self.agency("beta", BETA + [fact("Beta", "0", sha=SHA_B)])
        self.assertEqual(self.run_tool(), (1, [], "facts: 1 errors (run --check)"))
        self.agency("beta", BETA + BETA)  # duplicate rows are check errors too
        self.assertEqual(self.run_tool(), (1, [], "facts: 1 errors (run --check)"))
        self.assertFalse(os.path.lexists(self.out))

    def test_out_refusals_write_nothing(self):
        self.golden()
        self.out.write_text("keep\n", encoding="utf-8")
        dangling = self.dir / "handoff" / "dangling.csv"
        dangling.symlink_to(self.dir / "handoff" / "nowhere.csv")
        (self.dir / "into-agencies").symlink_to(self.store / "agencies")
        cases = [(self.out, "--out: exists or is a link; not overwritten"),
                 (dangling, "--out: exists or is a link; not overwritten"),
                 (ROOT / "tests" / "df1537-15-never.csv", "--out: is inside the repository"),
                 (self.store / "agencies" / "x.csv", "--out: is inside the store's agencies folder"),
                 (self.store / "agencies" / "alpha" / ".." / "y.csv", "--out: is inside the store's agencies folder"),
                 (self.dir / "into-agencies" / "z.csv", "--out: is inside the store's agencies folder"),
                 (self.dir / "missing" / "x.csv", "--out: parent folder is missing")]
        before = snapshot(self.dir)
        for out, message in cases:
            with self.subTest(out=str(out)):
                self.assertEqual(self.run_tool(out=out), (1, [], message))
        self.assertEqual(snapshot(self.dir), before)
        self.assertFalse(os.path.lexists(ROOT / "tests" / "df1537-15-never.csv"))
        self.assertFalse(os.path.lexists(self.dir / "handoff" / "nowhere.csv"))
        self.assertEqual(self.out.read_text(encoding="utf-8"), "keep\n")

    def test_out_in_store_outside_agencies_is_allowed(self):
        self.golden()
        out = self.store / "handoff.csv"
        before = snapshot(self.store / "agencies")  # run_tool's whole-store snapshot would see the new hand-off file
        done = subprocess.run([sys.executable, "-B", str(TOOL), "unreadable", "--store", str(self.store), "--out", str(out)],
                              capture_output=True, text=True, check=False)
        self.assertEqual((done.returncode, done.stdout.splitlines()), (0, [self.summary(2, 3, 2, 5, "handoff.csv")]))
        self.assertEqual(snapshot(self.store / "agencies"), before)
        self.assertEqual(len(self.lines(out)), 4)

    def test_symlinked_store_parts_are_refused(self):
        self.golden()
        outside = self.dir / "outside"
        self.agency("evil", [fact("Evil", "9")], store=outside)
        (self.store / "agencies" / "evil").symlink_to(outside / "agencies" / "evil")
        self.assertEqual(self.run_tool(), (1, [], "agencies/evil: is a symlink; refusing"))
        self.assertEqual(self.run_tool("--agency", "evil"), (1, [], "agencies/evil: is a symlink; refusing"))
        self.assertFalse(os.path.lexists(self.out))
        moved = self.dir / "real-agencies"
        (self.store / "agencies").rename(moved)
        (self.store / "agencies").symlink_to(moved)
        self.assertEqual(self.run_tool(), (1, [], "agencies: is a symlink; refusing"))
        self.store = self.dir / "handoff" / "not-a-store"
        self.store.write_text("x", encoding="utf-8")
        self.assertEqual(self.run_tool(), (1, [], "store: is not a folder"))
        self.assertFalse(os.path.lexists(self.out))

    def test_link_failure_leaves_no_out_and_no_temp(self):
        self.golden()
        printed = io.StringIO()
        with mock.patch.object(facts.os, "link", side_effect=OSError("injected")), contextlib.redirect_stdout(printed):
            code = facts.main(["unreadable", "--store", str(self.store), "--out", str(self.out)])
        self.assertEqual((code, printed.getvalue()), (1, "io: OSError\n"))
        self.assertEqual(list(self.out.parent.iterdir()), [])

    def test_usage_errors(self):
        self.golden()
        for argv in (["unreadable", "--store", str(self.store)], ["unreadable", "--store", str(self.store), "--out", ""],
                     ["unreadable", "x", "--store", str(self.store), "--out", str(self.out)],
                     ["unreadable", "--store", str(self.store), "--out", str(self.out), "--date", "2026-10-31"],
                     ["status", "--store", str(self.store), "--out", str(self.out)]):
            with self.subTest(argv=argv[:2]):
                done = subprocess.run([sys.executable, "-B", str(TOOL), *argv], capture_output=True, text=True, check=False)
                self.assertEqual(done.returncode, 2)
        self.assertFalse(os.path.lexists(self.out))


if __name__ == "__main__":
    unittest.main()
