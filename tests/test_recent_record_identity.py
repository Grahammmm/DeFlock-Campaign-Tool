"""Recent report counts distinct full source hashes before shortening them for display (RF1511-9). Synthetic, offline."""
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from campaign_tool import cli, recent  # noqa: E402

TODAY = date(2026, 11, 4)
A = "a" * 64
C = "c" * 64
PRE = "a" * 12


def collide(n):
    """n distinct full hashes sharing the 12-character prefix a*12."""
    return [PRE + format(i, "x").rjust(52, "0") for i in range(n)]


def run(*arguments):
    out, err, code = io.StringIO(), io.StringIO(), 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            mock.patch.object(recent, "_today", return_value=TODAY), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def snapshot(root):
    found = {}
    for base, dirs, files in os.walk(root):
        for name in dirs + files:
            path = os.path.join(base, name)
            info = os.lstat(path)
            found[path] = (info.st_mode, info.st_mtime_ns, Path(path).read_bytes() if stat.S_ISREG(info.st_mode) else None)
    return found


class RecordIdentityTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-recent-id-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.root = self.tmp / "campaign"
        (self.root / "content").mkdir(parents=True)
        (self.root / "campaign.json").write_text(json.dumps(
            {"schema_version": 1, "name": "Synthetic", "county": "San Luis Obispo", "state": "CA"}), encoding="utf-8")

    def sources(self, rows):
        entries = [{"sha256": h, "title": "Canary title T1", "agency_id": "ca-one", "received_at": at,
                    "public_url": None, "fragment": f"f{i}"} for i, (h, at) in enumerate(rows)]
        (self.root / "content" / "sources.json").write_text(json.dumps(entries), encoding="utf-8")

    def what(self, *extra):
        code, out, err = run("recent", "--directory", self.root, "--since", "2026-11-01", "--json", *extra)
        self.assertEqual((code, err), (0, ""))
        return [e["what"] for e in json.loads(out)["events"] if e["area"] == "Records"] if "events" in json.loads(out) \
            else [e["what"] for e in json.loads(out) if e["area"] == "Records"]

    def test_distinct_hashes_sharing_prefix_count_as_two_records(self):
        self.sources([(A, "2026-11-03"), (PRE + "b" * 52, "2026-11-03")])
        expected = f"2 records added to the library: {PRE}, {PRE}"
        self.assertEqual(self.what(), [expected])
        code, out, _ = run("recent", "--directory", self.root, "--since", "2026-11-01")
        self.assertEqual(code, 0)
        self.assertIn(expected, out)
        self.assertNotIn("b" * 13, out)
        self.assertNotIn("Canary title", out)
        events = recent.report(self.root, "2026-11-01", None, TODAY)
        self.assertEqual([e["what"] for e in events if e["area"] == "Records"], [expected])

    def test_fragment_duplicates_still_count_once_per_day(self):
        other = PRE + "b" * 52
        self.sources([(A, "2026-11-03"), (A, "2026-11-03T08:00:00Z"), (other, "2026-11-03"), (A, "2026-11-02")])
        self.assertEqual(sorted(self.what()), [f"1 record added to the library: {PRE}",
                                                f"2 records added to the library: {PRE}, {PRE}"])

    def test_collision_groups_obey_eight_record_display_cap(self):
        for n, expected in ((7, f"7 records added to the library: {', '.join([PRE] * 7)}"),
                            (8, f"8 records added to the library: {', '.join([PRE] * 8)}"),
                            (9, f"9 records added to the library: {', '.join([PRE] * 8)} (+1 more)")):
            self.sources([(h, "2026-11-03") for h in collide(n)])
            self.assertEqual(self.what(), [expected])

    def test_mixed_prefix_groups_and_input_order_are_deterministic(self):
        rows = [(C, "2026-11-03"), (A, "2026-11-03"), (PRE + "b" * 52, "2026-11-03")]
        expected = [f"3 records added to the library: {PRE}, {PRE}, {'c' * 12}"]
        self.sources(rows)
        self.assertEqual(self.what(), expected)
        self.sources(rows[::-1])
        self.assertEqual(self.what(), expected)

    def test_markdown_view_keeps_suffixes_hidden_and_file_private(self):
        self.sources([(h, "2026-11-03") for h in collide(9)])
        target = self.tmp / "out.md"
        code, out, err = run("recent", "--directory", self.root, "--since", "2026-11-01", "--md", target)
        self.assertEqual((code, err), (0, f"Wrote {target}\n"))
        text = target.read_text(encoding="utf-8")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        for view in (text, out):
            self.assertIn("9 records added to the library", view)
            self.assertTrue("(+1 more)" in view or "\\(\\+1 more\\)" in view)
            self.assertNotIn(PRE + "0" * 13, view)
            self.assertNotIn("Canary title", view)

    def test_healthy_noncollision_and_empty_outputs_are_unchanged(self):
        self.sources([(A, "2026-11-03"), (C, "2026-11-03"), (A, "2026-11-10"), (C, "2026-10-31")])
        self.assertEqual(self.what(), [f"2 records added to the library: {PRE}, {'c' * 12}"])
        code, out, _ = run("recent", "--directory", self.root, "--since", "2026-11-03", "--until", "2026-11-03", "--json")
        self.assertEqual(code, 0)
        self.sources([])
        self.assertEqual(self.what(), [])

    def test_report_does_not_mutate_campaign(self):
        self.sources([(A, "2026-11-03"), (PRE + "b" * 52, "2026-11-03")])
        before = snapshot(self.root)
        self.what()
        self.assertEqual(snapshot(self.root), before)


if __name__ == "__main__":
    unittest.main()
