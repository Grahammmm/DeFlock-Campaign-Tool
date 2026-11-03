"""flock_facts status --json: one flock-facts-status-v1 document; text, refusals and exit codes unchanged (RF0740-10)."""
import json
import os
import re
import subprocess
import sys
import unittest

from tests.flock import test_facts_status as base
from tests.flock.test_facts import TOOL, snapshot
from tests.flock.test_facts_status import ALPHA, ALPHA_LINE, BETA, BETA_LINE, SECRETS, fact, facts

FID = facts.V2.index("fact_id")
# Captured from main (18e46d12) `status --store S --list disputed` for the golden store: text must stay byte-identical.
MAIN_TEXT = "%s\n%s\nalpha | %s | law | retention | page 7 | row 4\nagencies=2 rows=4 unreviewed=1 disputed=1\n" % (
    ALPHA_LINE, BETA_LINE, ALPHA[2][FID])


def counts(line):
    return {k: int(v) for k, v in re.findall(r"([a-z]+)=(\d+)", line)}


class FactsStatusJsonTest(unittest.TestCase):
    T = base.FactsStatusTest
    setUp, write, agency, golden = T.setUp, T.write, T.agency, T.golden

    def raw(self, *args, store=None):
        before = snapshot(self.dir)
        cmd = [sys.executable, "-B", str(TOOL), "status", "--store", str(store or self.store), *args]
        done = subprocess.run(cmd, capture_output=True, text=True, check=False)
        self.assertEqual(snapshot(self.dir), before, "status changed the store")
        self.assertNotIn("Traceback", done.stderr)
        return done

    def doc(self, *args):
        done = self.raw("--json", *args)
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertTrue(done.stdout.endswith("}\n"))
        data = json.loads(done.stdout)
        self.assertEqual(list(data), ["schema", "agencies", "listed", "totals", "list"])
        self.assertEqual(data["schema"], "flock-facts-status-v1")
        for secret in SECRETS + ["30 days", "UNREADABLE"]:
            self.assertNotIn(secret, done.stdout)
        self.assertIsNone(re.search(r"[0-9a-f]{64}", done.stdout))
        return data, done.stdout

    def test_two_agencies_match_text_counts(self):
        self.golden()
        data, _ = self.doc()
        self.assertEqual(data["agencies"], [
            {"agency": "alpha", "rows": 3, "kinds": {"record": 1, "policy": 1, "law": 1, "public": 0},
             "statuses": {"unreviewed": 1, "checked": 1, "disputed": 1}, "unreadable": 0},
            {"agency": "beta", "rows": 1, "kinds": {"record": 0, "policy": 0, "law": 0, "public": 1},
             "statuses": {"unreviewed": 0, "checked": 1, "disputed": 0}, "unreadable": 1}])
        self.assertEqual((data["listed"], data["list"]), ([], None))
        self.assertEqual(data["totals"], {"agencies": 2, "rows": 4, "unreviewed": 1, "disputed": 1})
        text = self.raw().stdout.splitlines()
        for line, a in zip(text, data["agencies"]):
            flat = dict(rows=a["rows"], unreadable=a["unreadable"], **a["kinds"], **a["statuses"])
            self.assertEqual((line.split(" | ")[0], counts(line)), (a["agency"], flat))
        self.assertEqual(counts(text[-1]), data["totals"])

    def test_agency_filter_and_listed_ids(self):
        self.golden()
        data, _ = self.doc("--agency", "beta")
        self.assertEqual([a["agency"] for a in data["agencies"]], ["beta"])
        self.assertEqual(data["totals"], {"agencies": 1, "rows": 1, "unreviewed": 0, "disputed": 0})
        data, _ = self.doc("--list", "unreviewed")
        self.assertEqual((data["list"], data["listed"]), ("unreviewed", [
            {"agency": "alpha", "fact_id": ALPHA[1][FID], "kind": "policy", "fact_type": "vendor", "page": 2, "row": 3}]))
        data, _ = self.doc("--list", "disputed", "--agency", "alpha")
        self.assertEqual(data["listed"], [
            {"agency": "alpha", "fact_id": ALPHA[2][FID], "kind": "law", "fact_type": "retention", "page": 7, "row": 4}])

    def test_list_disputed_with_none_disputed(self):
        self.golden()
        data, _ = self.doc("--agency", "beta", "--list", "disputed")
        self.assertEqual((data["list"], data["listed"]), ("disputed", []))

    def test_non_ascii_fact_type_is_escaped_and_round_trips(self):
        self.agency("alpha", [fact("Alpha", "policy", "unreviewed", fact_type="Überwachung café", page="12")])
        data, raw = self.doc("--list", "unreviewed")
        self.assertTrue(raw.isascii())
        self.assertIn("\\u00dcberwachung caf\\u00e9", raw)
        self.assertEqual((data["listed"][0]["fact_type"], data["listed"][0]["page"]), ("Überwachung café", 12))

    def test_store_errors_print_no_json(self):
        self.agency("alpha", [fact("Alpha", "record", "pending")])
        self.agency("beta", BETA)
        done = self.raw("--json")
        self.assertEqual((done.returncode, done.stdout, done.stderr.strip()), (1, "", "file 1: row 2: status is invalid"))
        self.agency("alpha", ALPHA)
        other = self.dir / "other"
        self.agency("zeta", BETA, store=other)
        os.symlink(other / "agencies" / "zeta", self.store / "agencies" / "zeta")
        for args in ((), ("--agency", "zeta"), ("--list", "disputed")):
            done = self.raw("--json", *args)
            self.assertEqual((done.returncode, done.stdout, done.stderr.strip()),
                             (1, "", "agencies/zeta: is a symlink; refusing"), args)

    def test_empty_store_prints_no_json(self):
        (self.store / "agencies").mkdir(parents=True)
        done = self.raw("--json")
        self.assertEqual((done.returncode, done.stdout, done.stderr.strip()), (1, "", "no FACTS.csv files in store"))

    def test_json_with_other_commands_is_a_usage_error(self):
        self.golden()
        store = str(self.store)
        for argv in (["unreadable", "--store", store, "--out", store],
                     ["stale", "--store", store, "--date", "2026-11-01"], ["import", store, "--store", store, "--added-by", "x"],
                     ["--check", store], ["reviews", "--store", store, "--agency", "alpha"]):
            done = subprocess.run([sys.executable, "-B", str(TOOL), *argv, "--json"], capture_output=True, text=True, check=False)
            self.assertEqual((done.returncode, done.stdout), (2, ""), argv)
            self.assertIn("--json belongs to status or coverage", done.stderr)

    def test_text_without_json_is_byte_identical_to_main(self):
        self.golden()
        done = self.raw("--list", "disputed")
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, MAIN_TEXT, ""))


if __name__ == "__main__":
    unittest.main()
