"""kit-coverage: one read-only row per selected agency from draft to published finding. Synthetic, offline."""
import contextlib
import csv
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit_track  # noqa: E402

EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign" / "content"
H1, H2, H3 = ("a" * 64), ("b" * 64), ("c" * 64)
STAMP = "2026-03-01T12:00:00+00:00"
LONG = "Delta Police Department of a Very Long Synthetic Name That Runs On"


def run(*arguments):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def request(agency_id, sent_on, steps):
    return {"sent_on": sent_on, "channel": "email", "draft": f"requests/{agency_id}.md", "draft_sha256": H3,
            "recorded_at": STAMP, "steps": steps}


class KitCoverageTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-coverage-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.kit = self.root / "kit"
        (self.kit / "requests").mkdir(parents=True)
        agencies = [
            {"agency_id": "ca-a-sent-ack", "name": "Alpha Police", "selected": True},
            {"agency_id": "ca-b-none", "name": "=HYPERLINK(\"x\")", "selected": True},
            {"agency_id": "ca-c-drafted", "name": "Bravo, \"B\" Police", "selected": True},
            {"agency_id": "ca-d-produced", "name": LONG, "selected": True},
            {"agency_id": "ca-e-dropped", "name": "Echo Police", "selected": False},
        ]
        self.write(self.kit / "agencies.json", agencies)
        for agency_id in ("ca-a-sent-ack", "ca-c-drafted", "ca-d-produced", "ca-e-dropped"):
            (self.kit / "requests" / f"{agency_id}.md").write_text("synthetic draft\n")
        self.write(self.kit / "sent.json", {"schema_version": 1, "requests": {
            "ca-a-sent-ack": request("ca-a-sent-ack", "2026-03-02", [{"on": "2026-03-04", "kind": "acknowledged"}]),
            "ca-d-produced": request("ca-d-produced", "2026-03-01", [
                {"on": "2026-03-05", "kind": "acknowledged"},
                {"on": "2026-03-10", "kind": "produced", "records": [H1, H2]},
                {"on": "2026-03-12", "kind": "produced", "records": [H2]}]),
            "ca-e-dropped": request("ca-e-dropped", "2026-03-03", [])}})
        content = self.root / "content"
        (content / "findings").mkdir(parents=True)
        shutil.copy(EXAMPLE / "site.json", content / "site.json")
        finding = json.loads((EXAMPLE / "findings" / "cedar-policy-posted.json").read_text())
        self.write(content / "findings" / "delta.json", {**finding, "agency_id": "ca-d-produced"})
        source = json.loads((EXAMPLE / "sources.json").read_text())[0]
        self.write(content / "sources.json", [{**source, "agency_id": "ca-d-produced"},
                                              {**source, "agency_id": "ca-e-dropped", "fragment": "echo"}])
        (self.root / "private").mkdir()
        (self.root / "private" / "ledger.txt").write_text("synthetic private\n")

    @staticmethod
    def write(path, document):
        path.write_text(json.dumps(document))

    def coverage(self, *extra):
        code, out, err = run("kit-coverage", "--directory", self.root, *extra)
        self.assertEqual(code, 0, err)
        self.assertEqual(err.strip(), "Read-only. Nothing was sent.")
        return out

    def assert_stopped(self, *words):
        code, out, err = run("kit-coverage", "--directory", self.root)
        self.assertEqual(code, 1, out)
        self.assertTrue(err.startswith("Stopped: "), err)
        for word in words:
            self.assertIn(word, err)

    def test_rows_and_totals_with_unsent_agencies_first(self):
        report = kit_track.coverage(self.root)
        self.assertEqual([r["agency_id"] for r in report["rows"]],
                         ["ca-b-none", "ca-c-drafted", "ca-a-sent-ack", "ca-d-produced"])
        none, drafted, ack, produced = report["rows"]
        self.assertEqual(none, {"agency_id": "ca-b-none", "name": "=HYPERLINK(\"x\")", "drafted": False,
                                "sent_on": None, "channel": None, "last_step": None, "records": 0, "sources": 0,
                                "findings": 0, "fee_state": None, "scopes": None, "request": "main", "owner": None})
        self.assertEqual((drafted["drafted"], drafted["sent_on"], drafted["last_step"]), (True, None, None))
        self.assertEqual((ack["sent_on"], ack["channel"], ack["last_step"], ack["records"]),
                         ("2026-03-02", "email", {"on": "2026-03-04", "kind": "acknowledged"}, 0))
        self.assertEqual(produced["last_step"], {"on": "2026-03-12", "kind": "produced"})
        self.assertEqual((produced["records"], produced["sources"], produced["findings"]), (2, 1, 1))
        self.assertEqual(report["totals"], {"selected": 4, "drafted": 3, "sent": 2, "answered": 1,
                                            "with_records": 1, "with_findings": 1, "unselected_with_requests": 1,
                                            "fee_unpaid": 0, "requests": 4, "unassigned_requests": 4})
        self.assertNotIn("ca-e-dropped", json.dumps(report["rows"]))

    def test_unselected_agency_with_several_requests_counts_once(self):
        sent = json.loads((self.kit / "sent.json").read_text())
        contract = {**request("ca-e-dropped", "2026-03-04", []), "draft": "requests/ca-e-dropped--contract.md"}
        sent["requests"]["ca-e-dropped#contract"] = contract
        sent["requests"]["ca-f-gone#contract"] = {**contract, "draft": "requests/ca-f-gone--contract.md"}
        self.write(self.kit / "sent.json", sent)
        self.assertEqual(kit_track.coverage(self.root)["totals"]["unselected_with_requests"], 2)

    def test_table_cuts_names_and_dashes_nulls(self):
        out = self.coverage()
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("agency_id"))
        self.assertTrue(lines[1].startswith("ca-b-none"))
        self.assertEqual(lines[1].split()[-12:], ["no", "-", "-", "-", "-", "0", "0", "0", "-", "-", "main", "-"])
        self.assertIn(LONG[:40], out)
        self.assertNotIn(LONG[:41], out)
        self.assertIn("unselected_with_requests 1", out)
        self.assertNotIn("ca-e-dropped", out)

    def test_symlinked_draft_is_not_drafted(self):
        draft = self.kit / "requests" / "ca-c-drafted.md"
        draft.unlink()
        draft.symlink_to(self.kit / "requests" / "ca-a-sent-ack.md")
        rows = {r["agency_id"]: r for r in kit_track.coverage(self.root)["rows"]}
        self.assertFalse(rows["ca-c-drafted"]["drafted"])
        self.assertEqual(kit_track.coverage(self.root)["totals"]["drafted"], 2)

    def test_no_content_gives_zeros_and_a_note(self):
        shutil.rmtree(self.root / "content")
        report = kit_track.coverage(self.root)
        self.assertEqual({(r["sources"], r["findings"]) for r in report["rows"]}, {(0, 0)})
        self.assertEqual(report["totals"]["with_findings"], 0)
        self.assertIn("No content/", report["note"])
        self.assertIn("No content/", self.coverage())

    def test_invalid_content_stops(self):
        (self.root / "content" / "site.json").write_text("{}")
        self.assert_stopped("site.json")

    def test_missing_agencies_stops(self):
        (self.kit / "agencies.json").unlink()
        self.assert_stopped("run kit first: kit/agencies.json is missing")

    def test_corrupt_sent_json_stops_naming_it(self):
        (self.kit / "sent.json").write_text("{not json")
        self.assert_stopped("kit/sent.json unreadable")

    def test_csv_parses_back_and_guards_formulas(self):
        out = self.coverage("--csv")
        self.assertIn('"Bravo, ""B"" Police"', out)
        rows = list(csv.reader(io.StringIO(out)))
        self.assertEqual(rows[0], list(kit_track.CSV_HEADER))
        self.assertEqual(rows[1], ["ca-b-none", "'=HYPERLINK(\"x\")", "false", "", "", "", "", "0", "0", "0", "", "",
                                   "main", ""])
        self.assertEqual(rows[2][1], "Bravo, \"B\" Police")
        self.assertEqual(rows[4], ["ca-d-produced", LONG, "true", "2026-03-01", "email", "2026-03-12", "produced",
                                   "2", "1", "1", "", "not recorded", "main", ""])
        self.assertEqual(len(rows), 5)
        for cell in ("=1+1", "+1", "-1", "@SUM(A1)"):
            self.assertEqual(kit_track._csv_cell(cell), "'" + cell)
        self.assertEqual(kit_track._csv_cell("ca-x"), "ca-x")

    def test_read_only(self):
        def snapshot():
            return {str(p): (p.read_bytes() if p.is_file() else None, os.lstat(p).st_mtime_ns)
                    for top in ("kit", "content", "private") for p in sorted((self.root / top).rglob("*"))}
        before = snapshot()
        self.coverage()
        self.coverage("--csv")
        self.assertEqual(snapshot(), before)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["content", "kit", "private"])


if __name__ == "__main__":
    unittest.main()
