"""RF0535-1: speaker planning refuses an invalid staged meeting with a fixed message, never the validator's input-derived
text (an unknown key can hold pasted private text). Synthetic, offline.

Run from the repository root: python3 -B -m unittest tests.test_meeting_speakers_schema -q
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from campaign_tool import content, meeting_speakers as ms
from tests.test_meeting_speakers import KIT, NOW, PART, REPO, START, Base, run

KEY = "SYNTHETIC-PRIVATE-KEY-0535"
UGLY_KEY = "SYNTHETIC-PRIVATE-0535\nline\u202e\u00e9\x07"
FIXED = "content/meetings/m-a.json is not valid staged meeting content"
COMMENT = "Please pause the synthetic contract."
SUBMITTED = {"on": "2026-12-04", "channel": "portal", "text": COMMENT,
             "sha256": hashlib.sha256(COMMENT.encode("utf-8")).hexdigest()}
OUTCOME = {"result": "approved", "on": "2026-12-10", "source_url": "https://agenda.synthetic.example/m",
           "verified_by": "Pat Organizer", "verified_at": "2026-12-10"}
CASES = {
    "top-level": {KEY: "x"},
    "participation": {"participation": {**PART, KEY: 1}},
    "participation-remote": {"participation": {**PART, "remote": {"available": True, KEY: 1}}},
    "outcome": {"outcome": {**OUTCOME, KEY: 1}},
    "submitted_comment": {"submitted_comment": {**SUBMITTED, KEY: 1}},
}
SCHEMA = {
    "missing-body": {"body": None},
    "bad-start": {"starts_at": "not-a-date"},
    "wrong-type": {"agency_id": 5},
    "bad-limit": {"participation": {**PART, "speaker_minutes": 99}},
    "bad-nested-comment": {"submitted_comment": {**SUBMITTED, "sha256": "0" * 64}},
}


class SchemaBase(Base):
    def put(self, **extra):
        path = self.root / "content" / "meetings" / "m-a.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        for key, value in extra.items():
            if value is None:
                document.pop(key, None)
            else:
                document[key] = value
        path.write_text(json.dumps(document), encoding="utf-8")

    def apis(self):
        r = self.root
        return {"points": lambda: ms.meeting_points(r, "m-a", now=NOW),
                "list": lambda: ms.list_plan(r, "m-a", now=NOW),
                "add": lambda: ms.update_plan(r, "m-a", add="Ann", point="ask:1", now=NOW),
                "remove": lambda: ms.update_plan(r, "m-a", remove="Nobody", now=NOW)}

    def clis(self):
        return {"list": ("--list",), "add": ("--add", "Ann", "--point", "ask:1"), "remove": ("--remove", "Nobody")}

    def cli(self, *more):
        return run("meetings-speakers", "--directory", self.root, "--id", "m-a", *more)

    def snapshot(self):
        return sorted((str(p.relative_to(self.root)), p.is_dir(), p.stat().st_mode,
                       p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None) for p in self.root.rglob("*"))

    def assert_fixed(self, call, label):
        with self.assertRaises(ms.MeetingSpeakersError) as caught:
            call()
        self.assertEqual(str(caught.exception), FIXED, label)
        self.assertTrue(caught.exception.__suppress_context__, label)
        self.assertIsNone(caught.exception.__cause__, label)
        for secret in (KEY, "unknown field", "meetings/m-a.json."):
            self.assertNotIn(secret, str(caught.exception) + repr(caught.exception), label)


class FutureBase(SchemaBase):
    def setUp(self):
        super().setUp()
        # The CLI has no clock override: use a far-future start so the real clock never trips the time gate.
        self.future = "2099-01-01T10:00:00-08:00"
        self.stage(starts_at=self.future)


class ApiTests(SchemaBase):
    def test_valid_controls_reach_each_nested_validator(self):
        for name, extra in (("submitted_comment", {"submitted_comment": SUBMITTED}), ("participation", {"participation": PART})):
            with self.subTest(name):
                self.put(**extra)
                self.assertEqual([p["point"] for p in ms.meeting_points(self.root, "m-a", now=NOW)],
                                 ["finding:f_one", "ask:1", "ask:2"])

    def test_unknown_keys_are_refused_with_the_fixed_message_by_every_api(self):
        for name, extra in CASES.items():
            self.put(**extra)
            for api, call in self.apis().items():
                with self.subTest(name, api=api):
                    self.assert_fixed(call, f"{name}/{api}")
            self.put(**{key: None for key in extra})

    def test_other_schema_errors_are_still_refused_with_one_fixed_message(self):
        for name, extra in SCHEMA.items():
            self.put(**extra)
            for api, call in self.apis().items():
                with self.subTest(name, api=api):
                    self.assert_fixed(call, f"{name}/{api}")
            self.stage()  # restore the valid staged document

    def test_shared_validator_still_reports_its_own_error_and_other_errors_pass_through(self):
        document = json.loads((self.root / "content" / "meetings" / "m-a.json").read_text(encoding="utf-8"))
        with self.assertRaises(content.ContentError) as caught:
            content.load_meeting({**document, KEY: 1}, "meetings/m-a.json")
        self.assertIn(KEY, str(caught.exception))
        for failure in (RuntimeError("boom"), OSError("io"), ValueError("plain")):
            with mock.patch.object(ms, "load_meeting", side_effect=failure):
                with self.assertRaises(type(failure)) as raised:
                    ms.meeting_points(self.root, "m-a", now=NOW)
                self.assertIs(raised.exception, failure)

    def test_existing_domain_refusals_keep_their_messages(self):
        self.refused(ms.NOT_STAGED, ms.meeting_points, "absent", now=NOW)
        path = self.root / "content" / "meetings" / "m-a.json"
        path.write_text("{not json", encoding="utf-8")
        self.refused("content/meetings/m-a.json is not a regular UTF-8 JSON file up to 4 MiB", ms.meeting_points, "m-a", now=NOW)
        self.stage(ident="m-b")
        (self.root / "content" / "meetings" / "m-a.json").write_text(
            json.dumps({"id": "m-b", "body": "x", "starts_at": START}), encoding="utf-8")
        self.refused("content/meetings/m-a.json: id must match the file name", ms.meeting_points, "m-a", now=NOW)
        self.stage()
        self.put(status="cancelled")
        self.refused(ms.CANCELLED, ms.meeting_points, "m-a", now=NOW)
        self.put(status="scheduled")
        (self.root / "kit").mkdir()
        (self.root / "kit" / "meetings.json").write_text(json.dumps({"meetings": [{"id": "m-a", "status": "cancelled"}]}),
                                                          encoding="utf-8")
        self.refused(ms.CANCELLED, ms.meeting_points, "m-a", now=NOW)
        (self.root / "kit" / "meetings.json").unlink()
        self.refused(ms.PAST, ms.meeting_points, "m-a", now=NOW.replace(year=2027))
        self.stage(comment_kit_md="## Sources\n- **x** y\n")
        self.refused(ms.UNAVAILABLE, ms.meeting_points, "m-a", now=NOW)
        self.stage()
        self.refused(ms.UNKNOWN_POINT, ms.update_plan, "m-a", add="Ann", point="ask:3", now=NOW)

    def test_valid_planner_output_and_stale_plan_are_unchanged(self):
        self.assertEqual(self.add(" Ann  Lee ", "ask:1"), {"id": "m-a", "changed": True, "assignments": 1})
        self.assertEqual(self.add("Ann Lee", "ask:1"), {"id": "m-a", "changed": False, "assignments": 1})
        self.assertEqual(stat_mode(self.store()), 0o600)
        listing = ms.list_plan(self.root, "m-a", now=NOW)
        self.assertEqual(listing["uncovered"], ["finding:f_one", "ask:2"])
        self.assertEqual(ms.update_plan(self.root, "m-a", remove="Ann Lee", now=NOW)["assignments"], 0)
        self.add("Ann", "ask:1")
        self.stage(comment_kit_md=KIT.replace("Hello.", "Changed."))
        self.refused(ms.STALE, ms.list_plan, "m-a", now=NOW)


def stat_mode(path):
    return os.stat(path).st_mode & 0o777


class NoWriteTests(SchemaBase):
    def corrupt(self):
        self.put(**CASES["participation"])

    def test_refusal_leaves_an_absent_plan_tree_and_a_populated_plan_untouched(self):
        for populated in (False, True):
            with self.subTest(populated=populated):
                self.stage()
                if populated:
                    self.add("Ann", "ask:1")
                self.corrupt()
                before = self.snapshot()
                with mock.patch.object(ms, "_write", side_effect=AssertionError("write")), \
                        mock.patch.object(ms, "read_plan", side_effect=AssertionError("read_plan")), \
                        mock.patch.object(os, "replace", side_effect=AssertionError("replace")):
                    for api, call in self.apis().items():
                        self.assert_fixed(call, api)
                self.assertEqual(self.snapshot(), before)
                if not populated:
                    self.assertFalse((self.root / "kit").exists())
                else:
                    self.assertTrue(self.store().is_file())
                self.stage()


class CliTests(FutureBase):
    def test_cli_emits_one_fixed_stderr_line_and_nothing_else(self):
        for name, extra in {**CASES, "ugly-key": {UGLY_KEY: "x"}}.items():
            self.put(**extra)
            for action, args in self.clis().items():
                with self.subTest(name, action=action):
                    code, out, err = self.cli(*args)
                    self.assertEqual((code, out, err), (1, "", "Stopped: " + FIXED + "\n"))
                    for secret in (KEY, "SYNTHETIC-PRIVATE-0535", "unknown", "Traceback", "Recorded"):
                        self.assertNotIn(secret, err)
            self.stage(starts_at=self.future)
        self.assertFalse((self.root / "kit").exists())

    def test_subprocess_cli_physical_streams(self):
        self.put(**{UGLY_KEY: "x"})
        with tempfile.TemporaryDirectory(prefix="synthetic-speakers-tmp-") as scratch:
            env = {**os.environ, "TMPDIR": scratch, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(REPO)}
            done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "meetings-speakers", "--directory",
                                   str(self.root), "--id", "m-a", "--list"], cwd=REPO, env=env, capture_output=True)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (1, b"", ("Stopped: " + FIXED + "\n").encode()))

    def test_valid_cli_json_is_unchanged(self):
        code, out, err = self.cli("--add", " Ann  Lee ", "--point", "ask:1")
        self.assertEqual((code, json.loads(out), err.strip()),
                         (0, {"id": "m-a", "changed": True, "assignments": 1}, "Recorded only. Nothing was sent."))
        code, out, _ = self.cli("--list")
        self.assertEqual((code, json.loads(out)["uncovered"]), (0, ["finding:f_one", "ask:2"]))


if __name__ == "__main__":
    unittest.main()
