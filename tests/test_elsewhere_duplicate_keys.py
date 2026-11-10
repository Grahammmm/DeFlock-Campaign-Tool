"""RF2115-1: content/elsewhere.json with a repeated JSON member name is refused by build, content-check and meetings-stage.

Synthetic only: temp copies of examples/fictional-campaign, reserved .test hosts, no network. Inputs are written as raw JSON
text (never a serialized dict, which would erase the repeated member) and independently of the code under test.
"""
import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool.content import ContentError, load_content
from campaign_tool.content_check import content_problems, run_cli
from campaign_tool.content_elsewhere import read_elsewhere
from campaign_tool.elsewhere_cmd import ElsewhereError, add_entry, list_entries, remove_entry
from campaign_tool.meeting_stage import MeetingStageError, stage
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
DAY = date(2026, 10, 1)
MID = "legistar-examplecity-3001"
REFUSAL = r"^elsewhere\.json: a JSON object repeats a key$"
KEY_SENTINEL, VALUE_ONE, VALUE_TWO = "ZQ-KEY-SENTINEL", "ZQ-VALUE-ONE", "ZQ-VALUE-TWO"
FIELDS = ("place", "state", "body", "decided_on", "kind", "as_stated", "source_url", "checked_on", "news_url")


def members(number=1, **override):
    """The raw ``"name": value`` member texts of one valid entry; ``name=None`` drops, a string replaces the JSON value text."""
    base = {"place": f'"Springfield {number}"', "state": '"OR"', "body": '"Springfield City Council"',
            "decided_on": '"2026-03-10"', "kind": '"paused"', "as_stated": '"The council continued the contract."',
            "source_url": f'"https://records.example.test/minutes/{number}"', "checked_on": '"2026-09-15"'}
    base.update(override)
    return [f'"{name}": {value}' for name, value in base.items() if value is not None]


def obj(parts):
    return "{" + ", ".join(parts) + "}"


def doc(*objects):
    return "[" + ", ".join(objects) + "]"


CONFLICT = doc(obj(members() + ['"as_stated": "The council voted to pause the contract."']))
VALID = doc(obj(members(1)), obj(members(2, decided_on='"2026-04-11"')))


class Case(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-elsewhere-dup-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.path = self.root / "content" / "elsewhere.json"
        self.public = self.root / "public"
        socket_guard = mock.patch("socket.socket", side_effect=AssertionError("network access attempted"))
        socket_guard.start()
        self.addCleanup(socket_guard.stop)

    def put(self, text):
        self.path.write_bytes(text.encode("utf-8"))

    def snapshot(self, base):
        return {p.relative_to(base).as_posix(): (None if p.is_dir() else p.read_bytes(), p.lstat().st_mode & 0o7777)
                for p in sorted(base.rglob("*"))}

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args, "--directory", str(self.root)],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=300)


class ReaderTests(Case):
    def test_conflicting_claim_duplicate_refused_before_projection(self):
        self.put(CONFLICT)
        with self.assertRaisesRegex(ContentError, REFUSAL):
            read_elsewhere(self.root, DAY)

    def test_repeated_member_matrix_includes_equal_values_and_escaped_names(self):
        ok = members(1, news_url='"https://news.example.test/1"')
        for field in FIELDS:
            row = next(m for m in ok if m.startswith(f'"{field}":'))
            name, value = row.split(": ", 1)
            for label, repeat in (("conflicting", f'{name}: "different"'), ("identical", row)):
                with self.subTest(field=field, repeat=label):
                    self.put(doc(obj(ok + [repeat])))
                    with self.assertRaisesRegex(ContentError, REFUSAL):
                        read_elsewhere(self.root, DAY)
        cases = {"escaped name": doc(obj(members() + ['"as\\u005fstated": "The council voted to pause the contract."'])),
                 "later entry": doc(obj(members(1)), obj(members(2, decided_on='"2026-04-11"') + ['"state": "OR"'])),
                 "nested unknown object": doc(obj(members() + ['"extra": {"a": 1, "a": 2}'])),
                 "nested in array": doc(obj(members() + ['"extra": [{"b": 1, "b": 1}]']))}
        for label, text in cases.items():
            with self.subTest(case=label):
                self.put(text)
                with self.assertRaisesRegex(ContentError, REFUSAL):
                    read_elsewhere(self.root, DAY)

    def test_duplicate_error_does_not_echo_untrusted_key_or_either_value(self):
        text = doc(obj(members() + [f'"extra": {{"{KEY_SENTINEL}": "{VALUE_ONE}", "{KEY_SENTINEL}": "{VALUE_TWO}"}}']))
        self.put(text)
        with self.assertRaises(ContentError) as caught:
            read_elsewhere(self.root, DAY)
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)
        shown = [str(caught.exception)]
        for argv in (("content-check",), ("content-check", "--json")):
            result = self.cli(*argv)
            self.assertEqual(result.returncode, 3, result.stderr)
            shown += [result.stdout, result.stderr]
        result = self.cli("build")
        self.assertNotEqual(result.returncode, 0)
        shown += [result.stdout, result.stderr]
        for output in shown:
            for sentinel in (KEY_SENTINEL, VALUE_ONE, VALUE_TWO):
                self.assertNotIn(sentinel, output)

    def test_valid_unicode_order_and_no_file_compatibility(self):
        self.put(doc(obj(members(1, place='"Z\\u00fcrich \u2013 caf\u00e9"')),
                     obj(reversed(members(2, decided_on='"2026-04-11"')))))  # reversed member order, shared field names
        accepted = read_elsewhere(self.root, DAY)
        self.assertEqual([e["place"] for e in accepted], ["Z\u00fcrich \u2013 caf\u00e9", "Springfield 2"])
        self.assertEqual(accepted[1]["decided_on"], "2026-04-11")
        self.path.unlink()
        self.assertIsNone(read_elsewhere(self.root, DAY))


class ConsumerTests(Case):
    def test_checker_text_json_and_collecting_reader_agree(self):
        self.put(CONFLICT)
        collected = []
        self.assertIsNone(read_elsewhere(self.root, DAY, collected))
        self.assertEqual(collected, ["elsewhere.json: a JSON object repeats a key"])
        self.assertEqual([p for p in content_problems(self.root) if p.startswith("elsewhere.json")], collected)
        for as_json in (False, True):
            out, code = io.StringIO(), 0
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                try:
                    run_cli(self.root, as_json=as_json)
                except SystemExit as exc:
                    code = exc.code
            self.assertEqual(code, 3)
            if as_json:
                record = json.loads(out.getvalue())
                self.assertEqual([r["file"] for r in record["problems"] if r["file"].startswith("elsewhere.json")],
                                 ["elsewhere.json"])
            else:
                self.assertNotIn("content/: 0 problem", out.getvalue())
                self.assertIn("elsewhere.json: a JSON object repeats a key", out.getvalue())

    def test_failed_build_preserves_good_public_tree_and_source(self):
        self.put(VALID)
        build_site(self.root, today=DAY)
        public_before = self.snapshot(self.public)
        self.assertIn(b"Springfield 1", (self.public / "elsewhere.html").read_bytes())
        self.put(CONFLICT)
        source_before = (self.path.read_bytes(), self.path.stat().st_mode)
        with self.assertRaisesRegex(ContentError, REFUSAL):
            build_site(self.root, today=DAY)
        self.assertEqual(self.snapshot(self.public), public_before)
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_mode), source_before)
        self.assertEqual(list(self.root.glob(".public.*")), [])
        shutil.rmtree(self.public)  # a campaign with no prior public tree stays without one
        with self.assertRaisesRegex(ContentError, REFUSAL):
            build_site(self.root, today=DAY)
        self.assertFalse(self.public.exists())
        self.assertEqual(list(self.root.glob(".public.*")), [])

    def test_stage_duplicate_source_never_creates_or_replaces_snapshot(self):
        (self.root / "kit").mkdir(exist_ok=True)
        (self.root / "kit" / "meetings.json").write_text(json.dumps({"schema_version": 1, "meetings": [
            {"id": MID, "body": "Example City Council", "starts_at": "2026-11-18T18:00:00-08:00"}]}), encoding="utf-8")
        target = self.root / "content" / "meetings" / f"{MID}.json"
        self.put(CONFLICT)
        before = self.snapshot(self.root)
        for kwargs in ({"elsewhere_indices": [1]}, {}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex((ContentError, MeetingStageError), REFUSAL):
                stage(self.root, MID, **kwargs)
        self.assertFalse(target.exists())
        self.assertEqual(self.snapshot(self.root), before)
        self.put(VALID)
        stage(self.root, MID, elsewhere_indices=[1])
        staged = (target.read_bytes(), target.stat().st_mode)
        self.put(CONFLICT)
        with self.assertRaisesRegex((ContentError, MeetingStageError), REFUSAL):
            stage(self.root, MID, replace=True, elsewhere_indices=[1])
        self.assertEqual((target.read_bytes(), target.stat().st_mode), staged)

    def test_maintenance_already_refuses_same_bytes(self):
        self.put(CONFLICT)
        before = self.snapshot(self.root)
        row = dict(place="Springfield 9", state="OR", body="Springfield City Council", decided_on="2026-03-10", kind="paused",
                   as_stated="The council paused contract 9.", source_url="https://records.example.test/minutes/9",
                   checked_on="2026-09-15")
        for call in (lambda: list_entries(self.root, today=DAY), lambda: add_entry(self.root, **row, today=DAY),
                     lambda: remove_entry(self.root, 1, today=DAY)):
            with self.assertRaisesRegex(ElsewhereError, r"a JSON object repeats a key; .*nothing was written"):
                call()
        self.assertEqual(self.snapshot(self.root), before)


if __name__ == "__main__":
    unittest.main()
