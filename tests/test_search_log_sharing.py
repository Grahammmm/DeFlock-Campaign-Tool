"""kit-search-log --against-sharing: names in a saved log against the recorded sharing list. Synthetic fixtures; nothing is fetched."""
import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import registry, search_log  # noqa: E402
from campaign_tool.cli import main  # noqa: E402

AID = "ca-synthetic-police"
OWN = "Synthetic Police Department"
WARNING = "Names differ between systems; a mismatch is a lead, not evidence"
HEADER = "Timestamp,Purpose,Organization,State"
ROWS = [
    "2026-01-05,theft,Alpha PD,CA",
    "2026-01-05,theft,Alpha PD,CA",
    "2026-01-06,theft,alpha   pd.,CA",
    "2026-02-01,theft,Gamma PD,NV",
    "2026-02-02,theft,Gamma PD,",
    f"2026-02-03,theft,{OWN},CA",
    f"2026-02-04,theft,{OWN.lower()},CA",
    "2026-02-05,theft,Caf\u00e9 PD,CA",
    "2026-03-01,theft,,CA",
    "not-a-date,theft,Zed PD,CA",
    "2026-03-02,theft,Two Dots PD..,CA",
]
RECIPIENTS = [
    {"name": "Alpha PD", "state": "CA", "kind": "local"},
    {"name": "Beta SO", "state": "NV", "kind": "local"},
    {"name": "Cafe PD", "state": "CA", "kind": "local"},
    {"name": "Two Dots PD.", "state": "CA", "kind": "local"},
    {"name": OWN, "state": "CA", "kind": "local"},
]
SOURCE = "https://transparency.flocksafety.com/synthetic"


def sharing(recipients=RECIPIENTS, checked_on="2026-02-01"):
    return {"checked_on": checked_on, "source_url": SOURCE, "recipients": recipients}


def tree(root):
    return {str(p): (p.read_bytes(), p.stat().st_mode) if p.is_file() else None for p in sorted(Path(root).rglob("*"))}


class AgainstSharingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-search-sharing-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(os.path.realpath(temporary.name))
        self.root, self.files = self.base / "campaign", self.base / "files"
        (self.root / "kit").mkdir(parents=True)
        (self.root / "content").mkdir()
        self.files.mkdir()
        (self.root / "campaign.json").write_text(json.dumps(
            {"schema_version": 1, "name": "Synthetic", "county": "Synthetic", "state": "CA"}), encoding="utf-8")
        (self.root / "kit" / "agencies.json").write_text(json.dumps(
            [{"agency_id": AID, "name": OWN, "kind": "police"}]), encoding="utf-8")
        self.write_content(sharing())
        patch = mock.patch.dict(os.environ, {registry.ENV: str(self.base / "config" / "campaigns.json")})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(registry.release)

    def write_content(self, share, raw=None):
        entry = {"agency_id": AID, "name": OWN, "kind": "police"}  # type: dict
        if share is not None:
            entry["profile"] = {"sharing": share}
        text = raw if raw is not None else json.dumps([entry])
        (self.root / "content" / "agencies.json").write_text(text, encoding="utf-8")

    def csv(self, rows=ROWS, header=HEADER):
        path = self.files / "log.csv"
        path.write_bytes(("\r\n".join([header, *rows]) + "\r\n").encode("utf-8"))
        return path

    def run_cli(self, *args):
        argv = ["campaign_tool", "kit-search-log", "--directory", str(self.root), "--agency", AID, *map(str, args)]
        out, err, code = io.StringIO(), io.StringIO(), 0
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                main()
            except SystemExit as exc:
                code = exc.code or 0
            finally:
                registry.release()
        return code, out.getvalue(), err.getvalue()

    def report(self, rows=ROWS):
        code, out, err = self.run_cli("--csv", self.csv(rows), "--against-sharing", "--json")
        self.assertEqual((code, err), (0, ""), out)
        return json.loads(out)

    def refused(self, *words, rows=ROWS, header=HEADER, extra=()):
        csv_path = self.csv(rows, header)
        before = tree(self.base)
        code, out, err = self.run_cli("--csv", csv_path, "--against-sharing", *extra)
        self.assertEqual((code, out), (1, ""), err)
        self.assertTrue(err.startswith("Stopped: "), err)
        self.assertIn("--against-sharing", err)
        for word in words:
            self.assertIn(word, err)
        self.assertEqual(before, tree(self.base))  # nothing created or changed, the CSV included
        return err

    def test_exact_normalization_not_fuzzy(self):
        norm = search_log.normalize_org_name
        self.assertEqual(norm("  Alpha\u00a0 \tPD. "), "alpha pd")
        self.assertEqual(norm("ALPHA PD"), norm("alpha pd."))
        self.assertEqual(norm("Alpha PD.."), "alpha pd.")  # exactly one period
        self.assertEqual(norm("."), "")
        self.assertNotEqual(norm("Caf\u00e9 PD"), norm("Cafe PD"))
        self.assertNotEqual(norm("Cafe\u0301 PD"), norm("Caf\u00e9 PD"))  # decomposed vs composed
        for a, b in (("Alpha Police Department", "Alpha PD"), ("Alpha PD Inc", "Alpha PD"), ("Alpha, PD", "Alpha PD")):
            self.assertNotEqual(norm(a), norm(b))
        # name only: the same name in another state still matches
        done = search_log.compare_sharing(
            {"period": {"first_date": "2026-01-01", "last_date": "2026-01-02"}, "agency_name": None,
             "input": {"columns": {"org": "Organization"}},
             "organizations": {"unattributed": 0, "rows": [{"name": "Alpha PD", "state": "NV", "count": 1},
                                                           {"name": ".", "state": None, "count": 2},
                                                           {"name": " ", "state": None, "count": 1}]}},
            {"checked_on": "2026-02-01", "recipients": [{"name": "Alpha PD", "state": "CA", "kind": "local"},
                                                         {"name": ".", "state": "CA", "kind": "local"}]})
        self.assertEqual([r["name"] for r in done["not_on_recorded_list"]["rows"]], [" ", "."])  # empty keys never match
        self.assertEqual(done["without_observed_search"]["rows"], [{"name": ".", "state": "CA", "kind": "local"}])

    def test_originals_arrays_dates_and_text(self):
        got = self.report()
        sharing_part = got["sharing"]
        self.assertEqual(list(got)[-1], "sharing")
        self.assertEqual(got["schema_version"], 1)
        self.assertEqual(sharing_part, {
            "warning": WARNING,
            "log_period": {"first_date": "2026-01-05", "last_date": "2026-03-02"},
            "checked_on": "2026-02-01",
            "own_searches_excluded": 1,
            "unattributed_searches": 1,
            "not_on_recorded_list": {"warning": WARNING, "rows": [
                {"name": "Caf\u00e9 PD", "state": "CA", "count": 1},
                {"name": "Gamma PD", "state": None, "count": 1},
                {"name": "Gamma PD", "state": "NV", "count": 1},
                {"name": "Two Dots PD..", "state": "CA", "count": 1}]},
            "without_observed_search": {"warning": WARNING, "rows": [
                {"name": "Beta SO", "state": "NV", "kind": "local"},
                {"name": "Cafe PD", "state": "CA", "kind": "local"},
                {"name": "Two Dots PD.", "state": "CA", "kind": "local"}]},
        })
        code, out, err = self.run_cli("--csv", self.csv(), "--against-sharing")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.count(WARNING), 2)
        self.assertIn("== Searching organizations not on the recorded sharing list ==", out)
        self.assertIn("== Recorded sharing entries with no search in this log's date range ==", out)
        self.assertEqual(out.count("the log covers 2026-01-05 to 2026-03-02 (counted rows); the recorded sharing list was checked on 2026-02-01"), 2)
        self.assertIn("Two Dots PD.. [CA]: 1", out)
        self.assertIn("Beta SO [NV, local]", out)
        self.assertIn("not 'never searched'", out)
        self.assertIn("== Searches by purpose category == A count is a lead, not evidence", out)  # base sections kept
        self.assertNotIn("alpha pd", out.replace("alpha   pd.", ""))  # no normalized names in the output

    def test_own_search_exclusion_is_literal_only(self):
        rows = [f"2026-01-05,theft,{OWN},CA", f"2026-01-06,theft,{OWN.upper()},CA", f"2026-01-07,theft,{OWN}.,CA",
                f"2026-01-08,theft,{OWN} ,CA"]
        got = self.report(rows)["sharing"]
        self.assertEqual(got["own_searches_excluded"], 1)
        # the variants stay retained and match the own-named recipient by normalized name
        self.assertEqual(got["not_on_recorded_list"]["rows"], [])
        # with only the exact own name retained, the own-named recipient has no observed search
        got = self.report([f"2026-01-05,theft,{OWN},CA", "2026-01-06,theft,Alpha PD,CA"])["sharing"]
        self.assertEqual(got["own_searches_excluded"], 1)
        self.assertIn({"name": OWN, "state": "CA", "kind": "local"}, got["without_observed_search"]["rows"])

    def test_refusals_by_name_with_empty_output(self):
        self.write_content(None)
        self.refused("content/agencies.json:%s.profile.sharing" % AID)
        (self.root / "content" / "agencies.json").unlink()
        self.refused("content/agencies.json:%s.profile.sharing" % AID)
        self.write_content(sharing(), raw=json.dumps([{"agency_id": "ca-other", "name": "Other", "kind": "police"}]))
        self.refused("content/agencies.json:%s.profile.sharing" % AID)
        self.write_content(sharing(), raw=json.dumps([{"agency_id": AID, "name": OWN, "kind": "police", "selected": False,
                                                       "profile": {"sharing": sharing()}}]))
        self.refused("content/agencies.json:%s.profile.sharing" % AID)
        self.write_content(None, raw="[SECRET-SENTINEL not json")
        err = self.refused("content/agencies.json is not valid")
        self.assertNotIn("SECRET-SENTINEL", err)
        self.write_content(sharing(checked_on="2999-01-01"))
        self.refused("content/agencies.json is not valid")
        self.write_content(sharing(), raw=None)
        target = self.base / "elsewhere.json"
        target.write_bytes((self.root / "content" / "agencies.json").read_bytes())
        (self.root / "content" / "agencies.json").unlink()
        (self.root / "content" / "agencies.json").symlink_to(target)
        self.refused("content/agencies.json:%s.profile.sharing" % AID)
        (self.root / "content" / "agencies.json").unlink()
        self.write_content(sharing())
        # log side: no organization column, no counted dated row
        self.refused("--org-col", header="Timestamp,Purpose", rows=["2026-01-05,theft"])
        self.refused("counted", rows=["not-a-date,theft,Alpha PD,CA"])
        self.refused("counted", rows=[])

    def test_privacy_lint_of_recorded_names_and_no_leak(self):
        bad = [{"name": "Alpha PD", "state": "CA", "kind": "local"},
               {"name": "reach me at someone@example.org", "state": "CA", "kind": "local"}]
        self.write_content(sharing(bad))
        err = self.refused("recipients[1].name")
        self.assertNotIn("someone@example.org", err)
        sentinel = "SENTUSER-LEAK"
        self.write_content(sharing())
        code, out, err = self.run_cli("--csv", self.csv(["2026-01-05,%s,Alpha PD,CA" % sentinel]), "--against-sharing", "--json")
        self.assertEqual(code, 0, err)
        self.assertNotIn(sentinel, out)

    def test_flag_off_is_identical_and_content_not_opened(self):
        csv_path = self.csv()
        plain = self.run_cli("--csv", csv_path, "--json")
        for broken in (None, "{not json"):
            if broken is None:
                (self.root / "content" / "agencies.json").unlink()
            else:
                self.write_content(None, raw=broken)
            self.assertEqual(self.run_cli("--csv", csv_path, "--json"), plain)
        self.assertEqual(plain[0], 0)
        self.assertNotIn("sharing", json.loads(plain[1]))
        self.assertNotIn(WARNING, self.run_cli("--csv", csv_path)[1])

    def test_no_mutation_no_network(self):
        csv_path = self.csv()
        before = tree(self.base)

        def boom(*a, **k):
            raise AssertionError("network")
        with mock.patch.object(socket, "socket", boom), mock.patch.object(socket, "create_connection", boom):
            for extra in ((), ("--json",)):
                code, out, err = self.run_cli("--csv", csv_path, "--against-sharing", *extra)
                self.assertEqual((code, err), (0, ""))
        self.assertEqual(before, tree(self.base))


if __name__ == "__main__":
    unittest.main()
