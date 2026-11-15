"""RF1036-3: ``content elsewhere remove`` scans dot-prefixed ``content/meetings/*.json`` like the build loader does.

Offline and synthetic only: temp copies of examples/fictional-campaign; nothing is fetched or sent.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

from campaign_tool.content import load_content
from campaign_tool.elsewhere_cmd import ElsewhereError, remove_entry

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 11, 1)
PROBE = "ZQ-PRIVATE-9d2"
NO_CITATIONS = "No staged meetings cite the same place, body and date."


def entry(n, **extra):
    row = {"place": f"Springfield {n}", "state": "OR", "body": "Springfield City Council", "decided_on": "2026-03-10",
           "kind": "paused", "as_stated": f"The council voted to pause contract {n}.",
           "source_url": f"https://records.example.test/minutes/{n}", "checked_on": "2026-09-15"}
    row.update(extra)
    return row


def warning(name):
    return (f"Warning: meetings/{name} cites the same place, body and date; "
            "review and restage with meetings-stage --replace.\n")


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-elsewhere-hidden-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content = self.root / "content"
        self.meetings = self.content / "meetings"
        self.path = self.content / "elsewhere.json"
        self.rows = [entry(1), entry(2)]
        self.put(self.rows)

    def put(self, rows):
        self.path.write_text(json.dumps(rows), encoding="utf-8")

    def tree(self):
        return {str(p.relative_to(self.root)): (p.read_bytes() if p.is_file() and not p.is_symlink() else None,
                                                 p.lstat().st_mode, p.lstat().st_mtime_ns)
                for p in sorted(self.root.rglob("*"))}

    def meeting(self, name, *snapshots, meeting_id="synthetic-citation", **extra):
        base = json.loads((self.meetings / "2026-10-cedar-council.json").read_text(encoding="utf-8"))
        base["cited_elsewhere"], base["id"] = list(snapshots), meeting_id
        base.update(extra)
        (self.meetings / name).write_text(json.dumps(base), encoding="utf-8")

    def cli(self, index):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "content", "elsewhere", "remove",
                               "--directory", str(self.root), "--index", str(index)],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)

    def assert_refused(self, index, pattern):
        before = self.tree()
        with self.assertRaisesRegex(ElsewhereError, pattern) as caught:
            remove_entry(self.root, index, today=TODAY)
        self.assertEqual(self.tree(), before)
        self.assertNotIn(PROBE, str(caught.exception))
        self.assertNotIn(str(self.root), str(caught.exception))
        done = self.cli(index)
        self.assertEqual((done.returncode, done.stdout), (1, ""), done.stderr)
        self.assertIn("Stopped: ", done.stderr)
        self.assertNotIn(PROBE, done.stderr)
        self.assertEqual(self.tree(), before)


class HiddenMeetingTests(Base):
    def test_hidden_valid_citation_is_build_input_and_warned_by_api_and_cli(self):
        for label, rows, index in (("last entry", [entry(1)], 1), ("nonfinal entry", [entry(1), entry(2)], 1)):
            with self.subTest(label):
                shutil.rmtree(self.meetings)
                shutil.copytree(EXAMPLE / "content" / "meetings", self.meetings)
                self.put(rows)
                self.meeting(".retained.json", entry(1))
                self.assertIn("synthetic-citation", [m["id"] for m in load_content(self.root, today=TODAY)["meetings"]])
                kit = (self.meetings / ".retained.json").read_bytes()
                self.assertEqual(remove_entry(self.root, index, today=TODAY)["citing"], ["meetings/.retained.json"])
                self.assertEqual((self.meetings / ".retained.json").read_bytes(), kit)
                self.put(rows)  # restore the store for the CLI path
                done = self.cli(index)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertIn(warning(".retained.json"), done.stdout)
                self.assertNotIn(NO_CITATIONS, done.stdout)
                self.assertEqual((self.meetings / ".retained.json").read_bytes(), kit)

    def test_hidden_broken_json_refuses_both_remove_commit_paths(self):
        for label, payload in (("truncated", b"{"), ("invalid utf-8", b"\xff\xfe\x00{")):
            for rows, index in (([entry(1)], 1), ([entry(1), entry(2)], 1)):
                with self.subTest(label=label, entries=len(rows), visible=False):
                    self.put(rows)
                    (self.meetings / ".broken.json").write_bytes(payload)
                    self.assert_refused(index, r"^content/meetings/\.broken\.json: not valid UTF-8 JSON|\.broken\.json")
                    (self.meetings / ".broken.json").unlink()
                with self.subTest(label=label, entries=len(rows), visible=True):
                    self.put(rows)
                    (self.meetings / "broken.json").write_bytes(payload)
                    self.assert_refused(index, r"broken\.json")
                    (self.meetings / "broken.json").unlink()

    def test_hidden_schema_and_duplicate_member_failures_are_private(self):
        cases = {
            "bad field": lambda: self.meeting(".bad.json", entry(1), starts_at=PROBE),
            "bad snapshot": lambda: self.meeting(".bad.json", dict(entry(1), **{PROBE: "x"})),
            "future checked_on": lambda: self.meeting(".bad.json", entry(1, checked_on="2099-01-01")),
        }
        for label, make in cases.items():
            for rows in ([entry(1)], [entry(1), entry(2)]):
                with self.subTest(label=label, entries=len(rows)):
                    self.put(rows)
                    make()
                    self.assert_refused(1, r"^[^']*\.bad\.json\b[^']*; ")
                    (self.meetings / ".bad.json").unlink()
        with self.subTest("duplicate member"):
            (self.meetings / ".dup.json").write_text(
                '{"id": "a", "id": "b", "body": "x"}', encoding="utf-8")
            self.assert_refused(1, r"\.dup\.json")

    def test_hidden_unsafe_candidates_reuse_no_follow_bounded_reader(self):
        outside = Path(self.root.parent) / "outside-sentinel.json"
        outside.write_text(json.dumps({"id": PROBE}), encoding="utf-8")
        outside_before = outside.read_bytes()
        makers = {
            "symlink": lambda p: p.symlink_to(outside),
            "dangling symlink": lambda p: p.symlink_to(Path(self.root.parent) / "absent-target.json"),
            "fifo": lambda p: os.mkfifo(p),
            "folder": lambda p: p.mkdir(),
            "oversize": lambda p: p.write_bytes(b" " * (2 * 1024 * 1024 + 1)),
        }
        for label, make in makers.items():
            for rows in ([entry(1)], [entry(1), entry(2)]):
                with self.subTest(label=label, entries=len(rows)):
                    self.put(rows)
                    target = self.meetings / f".{label.replace(' ', '-')}.json"
                    make(target)
                    self.assert_refused(1, r"\.json")
                    if target.is_dir() and not target.is_symlink():
                        target.rmdir()
                    else:
                        target.unlink()
        self.assertEqual(outside.read_bytes(), outside_before)

    def test_mixed_hidden_visible_matches_are_sorted_and_deduplicated(self):
        self.meeting("z-visible.json", entry(1), meeting_id="z-visible")
        self.meeting(".b-hidden.json", entry(2), entry(1), dict(entry(1), kind="other"), meeting_id="b-hidden")
        self.meeting("a-kind.json", dict(entry(1), kind="other"), meeting_id="a-kind")
        self.meeting(".a-hidden-other.json", entry(1, decided_on="2026-03-11"), entry(1, body="Springfield Board"),
                     entry(2), meeting_id="a-other")
        done = self.cli(1)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(
            done.stdout.splitlines()[1:4],
            [warning(".b-hidden.json").strip(), warning("a-kind.json").strip(), warning("z-visible.json").strip()])
        self.assertEqual(done.stdout.count("Warning: "), 3)
        self.assertNotIn(NO_CITATIONS, done.stdout)

    def test_only_direct_case_sensitive_json_candidates_are_scanned(self):
        for name in (".scratch", ".draft.json.tmp", "UPPER.JSON", "notes.txt"):
            (self.meetings / name).write_text("{", encoding="utf-8")
        (self.meetings / "nested").mkdir()
        (self.meetings / "nested" / "inner.json").write_text("{", encoding="utf-8")
        (self.meetings / "nested" / ".inner.json").write_text("{", encoding="utf-8")
        self.assertEqual(remove_entry(self.root, 1, today=TODAY)["citing"], [])
        self.put(self.rows)
        self.meeting(".draft.json", entry(1))
        self.assertEqual(remove_entry(self.root, 1, today=TODAY)["citing"], ["meetings/.draft.json"])
        self.put(self.rows)
        shutil.rmtree(self.meetings)
        done = self.cli(1)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(NO_CITATIONS, done.stdout)

    def test_visible_only_success_bytes_and_store_mode_stay_compatible(self):
        self.meeting("visible.json", entry(1), meeting_id="visible")
        self.path.chmod(0o600)
        done = self.cli(1)
        self.assertEqual((done.returncode, done.stdout), (0, (
            "Elsewhere entry removed: index 1.\n" + warning("visible.json") +
            "Later stored positions shift down by one; run content elsewhere list before choosing --elsewhere again.\n"
            "Content only; nothing was fetched, built, sent or deployed.\n")), done.stderr)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), [entry(2)])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.put([entry(2)])
        done = self.cli(1)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(NO_CITATIONS, done.stdout)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
