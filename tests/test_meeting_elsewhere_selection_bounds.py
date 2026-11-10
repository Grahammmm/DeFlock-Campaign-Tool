"""RF1155-1: malformed --elsewhere selections are refused with a fixed, bounded diagnostic.

Synthetic only: temp copies of examples/fictional-campaign; nothing is fetched or sent.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from campaign_tool.content import ContentError
from campaign_tool.meeting_elsewhere import choose
from campaign_tool.meeting_stage import MeetingStageError, stage

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
MID = "legistar-examplecity-3001"
SHAPE = "--elsewhere: give 1 to 3 whole numbers"
LIMIT = 192


def entry(n):
    return {"place": f"Springfield {n}", "state": "OR", "body": "Springfield City Council", "decided_on": "2026-03-10",
            "kind": "paused", "as_stated": f"The council voted to pause contract {n}.",
            "source_url": f"https://records.example.test/minutes/{n}", "checked_on": "2026-09-15"}


def nested(depth):  # built iteratively: never recursed over, so no runtime limit is touched
    value = []
    for _ in range(depth):
        value = [value]
    return value


def selfish():
    value = []
    value.append(value)
    return value


def bad_selections():
    canary = "CANARY-" + "x" * 5000
    return [None, "1", b"1", {"1": 1}, 1, [], [1, 2, 3, 4], (1, 2, 3, 4), [True], [1.0], [canary], [1, canary],
            [[1]], nested(20000), [nested(20000)], selfish(), [selfish()], [canary] * 10000, [1] * 10000,
            [10 ** 5000, 1, 2, 3]]


class ChooseTests(unittest.TestCase):
    def setUp(self):
        self.entries = [entry(n) for n in range(1, 101)]

    def message(self, indices, entries=...):
        with self.assertRaises(ContentError) as caught:
            choose(self.entries if entries is ... else entries, indices)
        return str(caught.exception)

    def test_invalid_selection_shape_never_formats_contents(self):
        for number, bad in enumerate(bad_selections()):
            text = self.message(bad)
            self.assertEqual(text, SHAPE, f"selection case {number}")
        self.assertEqual(self.message(None, entries=None), SHAPE)  # shape wins over a missing source

    def test_large_flat_rejection_has_constant_diagnostic(self):
        long = "y" * 100000
        self.assertEqual(self.message([long] * 10000), self.message([long]))
        self.assertEqual(self.message([1, 2, long]), SHAPE)

    def test_integer_display_bound_before_decimal_conversion(self):
        count = len(self.entries)
        omitted = f"--elsewhere: index out of range (content/elsewhere.json has {count} entries; supplied integer omitted)"
        for index in (-999999, 999999):
            self.assertEqual(self.message([index]), f"--elsewhere {index}: out of range (content/elsewhere.json has {count} entries)")
        for index in (-1000000, 1000000, 10 ** 5000, -10 ** 5000):
            self.assertEqual(self.message([index]), omitted)
            self.assertLessEqual(len(self.message([1, index])), LIMIT)
        self.assertEqual(self.message([2, 2 * 10 ** 5000, 2]), omitted)  # first failing position wins
        self.assertEqual(self.message([10 ** 5000, 1, 2, 3]), SHAPE)  # shape wins over per-index checks
        self.assertEqual(self.message([10 ** 5000, 1, 2, 3, 4]), SHAPE)

    def test_normal_selection_order_duplicates_and_absence_are_unchanged(self):
        chosen = choose(self.entries, [2, 1])
        self.assertEqual(chosen, [self.entries[1], self.entries[0]])
        chosen[0]["place"] = "changed"
        self.assertEqual(self.entries[1]["place"], "Springfield 2")
        count = len(self.entries)
        for index in (0, -1, count + 1):
            self.assertEqual(self.message([index]), f"--elsewhere {index}: out of range (content/elsewhere.json has {count} entries)")
        self.assertEqual(self.message([3, 3]), "--elsewhere 3: given more than once")
        self.assertEqual(self.message([1], entries=None), "--elsewhere: content/elsewhere.json not found; nothing to cite")
        self.assertEqual(self.message([True]), SHAPE)


class StageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-meeting-elsewhere-bounds-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        (self.root / "kit").mkdir(exist_ok=True)
        (self.root / "kit" / "meetings.json").write_text(json.dumps({"schema_version": 1, "meetings": [
            {"id": MID, "body": "Example City Council", "starts_at": "2026-11-18T18:00:00-08:00"}]}), encoding="utf-8")
        (self.root / "content" / "elsewhere.json").write_text(json.dumps([entry(1), entry(2)]), encoding="utf-8")
        self.target = self.root / "content" / "meetings" / f"{MID}.json"

    def tree(self):
        rows = {}
        for path in sorted(self.root.rglob("*")):
            info = path.lstat()
            data = path.read_bytes() if path.is_file() and not path.is_symlink() else None
            rows[str(path)] = (info.st_mode, info.st_mtime_ns, data)
        return rows

    def refused(self, selection):
        with self.assertRaises(MeetingStageError) as caught:
            stage(self.root, MID, replace=self.target.exists(), elsewhere_indices=selection)
        return str(caught.exception)

    def check_refusals(self):
        before = self.tree()
        for selection, expected in ((nested(20000), SHAPE), ([nested(20000)], SHAPE), ([None] * 10000, SHAPE),
                                    ([10 ** 5000], "--elsewhere: index out of range (content/elsewhere.json has 2 entries; "
                                                   "supplied integer omitted)")):
            self.assertEqual(self.refused(selection), expected)
        self.assertEqual(self.tree(), before)

    def test_stage_first_and_replace_contain_rejected_selection_without_writes(self):
        self.check_refusals()
        self.assertFalse(self.target.exists())
        stage(self.root, MID, elsewhere_indices=[2, 1])
        self.assertTrue(self.target.is_file())
        self.check_refusals()
        self.assertEqual([e["place"] for e in json.loads(self.target.read_text(encoding="utf-8"))["cited_elsewhere"]],
                         ["Springfield 2", "Springfield 1"])

    def run_cli(self, *extra):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "meetings-stage", "--directory", str(self.root),
                               "--id", MID, *extra], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120, env=env)

    def test_actual_cli_long_decimal_refuses_with_bounded_stderr(self):
        token = "9" * 4000
        for replace in ((), ("--replace",)):
            if replace:
                self.assertEqual(self.run_cli("--elsewhere", "1").returncode, 0)
            before = self.tree()
            done = self.run_cli("--elsewhere", token, *replace)
            self.assertEqual(done.returncode, 1, done.stderr[:300])
            self.assertEqual(done.stdout, "")
            lines = done.stderr.strip().splitlines()
            self.assertEqual(len(lines), 1, done.stderr[:300])
            self.assertTrue(lines[0].startswith("Stopped"), lines[0][:100])
            self.assertLessEqual(len(done.stderr), 256)
            for forbidden in (token[:50], "Traceback", "set_int_max_str_digits", "Wrote", "Staged"):
                self.assertNotIn(forbidden, done.stderr)
            self.assertEqual(self.tree(), before)
        small = self.run_cli("--elsewhere", "7", "--replace")  # ordinary small refusal keeps naming the value
        self.assertEqual(small.returncode, 1)
        self.assertIn("--elsewhere 7: out of range (content/elsewhere.json has 2 entries)", small.stderr)


if __name__ == "__main__":
    unittest.main()
