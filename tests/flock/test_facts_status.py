"""flock_facts status: review state per agency and ids to review; read-only, never a value or quote (RF1220-2)."""
import csv
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.flock.test_facts import TOOL, snapshot

_spec = importlib.util.spec_from_file_location("flock_facts_status_under_test", TOOL)
facts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(facts)

SHA = hashlib.sha256(b"synthetic status doc").hexdigest()
SECRETS = ["Acme Secret Value", "Quote nobody may print.", "REQ-SECRET-9", SHA, "status-tester"]


def fact(agency, kind, status, fact_type="vendor", value="Acme Secret Value", page="3"):
    row = dict(zip(facts.V1, [agency, "REQ-SECRET-9", fact_type, value, SHA, page,
                              "" if value == "UNREADABLE" else "Quote nobody may print."]))
    row.update(kind=kind, status=status, effective_from="", effective_to="", added_by="status-tester",
               added_at="2026-10-31")
    row["fact_id"] = facts.fact_id(row)
    return [row[k] for k in facts.V2]


ALPHA = [fact("Alpha", "record", "checked", page="1"), fact("Alpha", "policy", "unreviewed", page="2"),
         fact("Alpha", "law", "disputed", fact_type="retention", value="30 days", page="7")]
BETA = [fact("Beta", "public", "checked", value="UNREADABLE")]
ALPHA_LINE = "alpha | rows=3 | record=1 policy=1 law=1 public=0 | unreviewed=1 checked=1 disputed=1 | unreadable=0"
BETA_LINE = "beta | rows=1 | record=0 policy=0 law=0 public=1 | unreviewed=0 checked=1 disputed=0 | unreadable=1"


class FactsStatusTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.store = self.dir / "store"

    def write(self, path, rows, head=facts.V2, encoding="utf-8", tail=""):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding=encoding, newline="") as handle:
            csv.writer(handle, lineterminator="\r\n").writerows([head, *rows])
            handle.write(tail)
        return path

    def agency(self, slug, rows, store=None, **kw):
        return self.write((store or self.store) / "agencies" / slug / "FACTS.csv", rows, **kw)

    def golden(self):
        self.agency("alpha", ALPHA)
        self.agency("beta", BETA)

    def status(self, *args, store=None):
        before = snapshot(self.dir)
        cmd = [sys.executable, "-B", str(TOOL), "status", "--store", str(store or self.store), *args]
        done = subprocess.run(cmd, capture_output=True, text=True, check=False)
        self.assertEqual(snapshot(self.dir), before, "status changed the store")
        self.assertNotIn("Traceback", done.stderr)
        return done.returncode, done.stdout.splitlines(), done.stderr.strip()

    def test_golden_counts(self):
        self.golden()
        self.assertEqual(self.status(), (0, [ALPHA_LINE, BETA_LINE, "agencies=2 rows=4 unreviewed=1 disputed=1"], ""))

    def test_list_disputed_prints_ids_never_values(self):
        self.golden()
        code, out, err = self.status("--list", "disputed")
        listed = "alpha | %s | law | retention | page 7 | row 4" % ALPHA[2][facts.V2.index("fact_id")]
        self.assertEqual((code, out, err), (0, [ALPHA_LINE, BETA_LINE, listed, "agencies=2 rows=4 unreviewed=1 disputed=1"], ""))
        code, out, _ = self.status("--list", "unreviewed")
        self.assertEqual(out[2], "alpha | %s | policy | vendor | page 2 | row 3" % ALPHA[1][facts.V2.index("fact_id")])
        for secret in SECRETS + ["30 days"]:
            self.assertNotIn(secret, "\n".join(out))

    def test_list_unreviewed_with_none_and_agency_filter(self):
        self.golden()
        self.assertEqual(self.status("--agency", "beta", "--list", "unreviewed"),
                         (0, [BETA_LINE, "agencies=1 rows=1 unreviewed=0 disputed=0"], ""))
        self.assertEqual(self.status("--agency", "beta"), (0, [BETA_LINE, "agencies=1 rows=1 unreviewed=0 disputed=0"], ""))
        self.assertEqual(self.status("--agency", "gamma"), (1, [], "agency gamma: no FACTS.csv"))

    def test_bad_and_duplicate_rows_fail_without_a_table(self):
        self.agency("alpha", [fact("Alpha", "record", "pending")])
        self.agency("beta", BETA)
        self.assertEqual(self.status(), (1, [], "file 1: row 2: status is invalid"))
        self.agency("alpha", [ALPHA[0], ALPHA[0]])
        code, out, err = self.status("--list", "disputed")
        self.assertEqual((code, out, err), (1, [], "file 1: row 3: duplicate of row 2"))
        self.agency("alpha", [fact("Beta", "record", "checked")])
        self.assertEqual(self.status("--agency", "alpha"), (1, [], "file 1: row 2: agency is invalid"))

    def test_v1_header_is_refused(self):
        self.agency("alpha", [], head=facts.V1)
        self.assertEqual(self.status(), (1, [], "file 1: row 1: header is not an accepted header"))

    def test_symlinks_are_refused_and_never_followed(self):
        self.golden()
        other = self.dir / "other"
        self.agency("zeta", [["broken"]], store=other, head=["not", "a", "header"])
        os.symlink(other / "agencies" / "zeta", self.store / "agencies" / "zeta")
        for args in ((), ("--agency", "zeta")):
            self.assertEqual(self.status(*args), (1, [], "agencies/zeta: is a symlink; refusing"))
        linked = self.dir / "linked"
        linked.mkdir()
        os.symlink(self.store / "agencies", linked / "agencies")
        self.assertEqual(self.status(store=linked), (1, [], "agencies: is a symlink; refusing"))
        os.unlink(self.store / "agencies" / "zeta")
        os.unlink(self.store / "agencies" / "beta" / "FACTS.csv")
        os.symlink(other / "agencies" / "zeta" / "FACTS.csv", self.store / "agencies" / "beta" / "FACTS.csv")
        for args in ((), ("--agency", "beta")):
            code, out, err = self.status(*args)
            self.assertEqual((code, out, err), (1, [], "agencies/beta/FACTS.csv: is a symlink; refusing"))
            self.assertNotIn("header", err)

    def test_empty_or_missing_store(self):
        (self.store / "agencies").mkdir(parents=True)
        self.assertEqual(self.status(), (1, [], "no FACTS.csv files in store"))
        self.assertEqual(self.status(store=self.dir / "absent"), (1, [], "store: is not a folder"))

    def test_usage_errors_exit_2(self):
        self.golden()
        store = str(self.store)
        for argv in (["status"], ["status", "extra", "--store", store], ["status", "--store", store, "--list", "checked"],
                     ["status", "--store", store, "--check", store], ["status", "--store", store, "--agency", "!!"],
                     ["--check", store, "--list", "disputed"], ["status", "--store", store, "--list", "Disputed"]):
            done = subprocess.run([sys.executable, "-B", str(TOOL), *argv], capture_output=True, text=True, check=False)
            self.assertEqual((done.returncode, done.stdout), (2, ""), argv)
        done = subprocess.run([sys.executable, "-B", str(TOOL), "--help"], capture_output=True, text=True, check=False)
        self.assertIn("status --store DIR", done.stdout)

    def test_bom_and_trailing_blank_line(self):
        self.agency("alpha", ALPHA, encoding="utf-8-sig", tail="\r\n")
        self.assertEqual(self.status(), (0, [ALPHA_LINE, "agencies=1 rows=3 unreviewed=1 disputed=1"], ""))

    def test_invalid_utf8_is_an_io_error(self):
        path = self.agency("alpha", ALPHA)
        path.write_bytes(path.read_bytes() + b"\xff\xfe,\r\n")
        self.assertEqual(self.status(), (1, ["io: UnicodeDecodeError"], ""))


if __name__ == "__main__":
    unittest.main()
