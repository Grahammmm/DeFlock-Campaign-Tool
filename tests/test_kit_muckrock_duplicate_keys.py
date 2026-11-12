"""Saved MuckRock JSON with a repeated decoded member name at any object depth is refused before history, shortcuts or
writes, instead of the last occurrence silently winning. Literal wire text, real files and CLI. Offline, synthetic."""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from campaign_tool import kit_muckrock, kit_track
from tests import test_kit_muckrock as base
from tests import test_kit_muckrock_bounded_read as bounded

REFUSAL = "duplicate JSON keys"
CANARY_KEY, CANARY_VALUE = "SYNTHETIC-SECRET-KEY", "SYNTHETIC-SECRET-VALUE"
UPDATED = '"datetime_updated":"2026-03-10T14:30:00Z"'
DONE = '"datetime_done":"2026-03-12T23:59:00Z"'


def wire(*members):  # literal JSON text, so repeated members survive exactly as written
    return "{" + ",".join(members) + "}"


class DuplicateKeyTests(unittest.TestCase):
    def setUp(self):  # compose the existing fixture operations; do not inherit (and so re-collect) its tests
        self.fx = base.KitMuckrockTests("test_request_url_is_recorded_for_muckrock")
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.tmp, self.root, self.sent, self.agency = self.fx.tmp, self.fx.root, self.fx.sent, self.fx.agency

    def put(self, text, name="saved.json"):
        path = self.tmp / name
        path.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
        return path

    def refused(self, path):
        return f"{path}: {REFUSAL}"

    def assert_load_refused(self, path):
        with self.assertRaises(ValueError) as caught:
            kit_muckrock._load(path)
        self.assertEqual(str(caught.exception), self.refused(path))
        self.assertIsNone(caught.exception.__cause__)

    def snapshot(self):
        seen = {}
        for base_dir in (self.root, self.tmp):
            for item in sorted(base_dir.rglob("*")):
                info = item.lstat()
                seen[str(item)] = (item.read_bytes() if item.is_file() and not item.is_symlink() else None,
                                   info.st_mode, info.st_mtime_ns)
        return seen

    def cli(self, path, *flags):
        return self.fx.imp(path, *flags)

    def test_wire_duplicates_cannot_hide_request_status_dates_or_price(self):
        self.fx.filed([("acknowledged", "2026-03-10")])
        cases = {  # field -> (valid text, invalid text) as they appear on the wire
            "id": ("90001", "90002"), "status": ('"payment"', '"SYNTHETIC-INVALID"'),
            "datetime_updated": ('"2026-03-10T14:30:00Z"', '"last week"'),
            "datetime_done": ('"2026-03-12T23:59:00Z"', '"last week"'), "price": ('"245.50"', '"-1"')}
        for field, (good, bad) in cases.items():
            for first, second in ((bad, good), (good, bad), (good, good), (bad, bad)):
                members = [m for m in ('"id":90001', '"status":"payment"', UPDATED, DONE, '"price":"245.50"')
                           if not m.startswith(f'"{field}"')]
                path = self.put(wire(*members, f'"{field}":{first}', f'"{field}":{second}'))
                with self.subTest(field=field, first=first, second=second):
                    before = self.snapshot()
                    with self.assertRaises(ValueError) as caught:
                        kit_muckrock.propose(self.root, self.agency, path)
                    self.assertEqual(str(caught.exception), self.refused(path))
                    code, out, err = self.cli(path, "--apply")
                    self.assertEqual((code, out, err), (1, "", f"Stopped: {self.refused(path)}\n"))
                    self.assertEqual(self.snapshot(), before)

    def test_decoded_key_collisions_and_per_object_scope(self):
        refused = (  # repeated after decoding, in the top object, nested objects and array elements
            ('"status":"processed"', '"statu\\u0073":"fix"'), ('"caf\\u00e9":1', '"café":2'), ('"":1', '"":2'),
            ('"\\ud83d\\ude00":1', '"😀":1'), ('"x":1', '"x":1'),
            ('"status":"processed"', '"meta":{"x":1,"x":2}'), ('"status":"processed"', '"list":[{"x":1},{"y":1,"y":2}]'),
            ('"status":"processed"', '"deep":{"a":[{"b":{"c":1,"c":1}}]}'))
        for members in refused:
            path = self.put(wire('"id":90001', UPDATED, *members))
            with self.subTest(members=members):
                self.assert_load_refused(path)
        accepted = (
            '"a":{"x":1},"b":{"x":2},"list":[{"x":1},{"x":2}]', '"Status":"fix","STATUS":"x"',
            '"caf\\u00e9":1,"cafe\\u0301":2', '"é":1,"e":2', '"":1,"\\u0000":2')
        for extra in accepted:
            path = self.put(f'{{"id":90001,"status":"processed",{UPDATED},{extra}}}')
            with self.subTest(extra=extra):
                self.assertEqual(kit_muckrock._load(path)["status"], "processed")

    def test_ambiguous_no_step_and_replay_refuse_before_history_or_writer(self):
        texts = {"waiting": wire('"id":90001', '"status":"started"', '"id":90001'),
                 "manual": wire('"id":90001', '"status":"appealing"', '"status":"appealing"'),
                 "replay": wire('"id":90001', '"status":"processed"', UPDATED, '"status":"processed"'),
                 "hidden-mismatch": wire('"id":90002', '"status":"started"', '"id":90001')}
        for stored in (True, False):
            if stored:
                self.fx.filed([("acknowledged", "2026-03-10")])  # "processed" would be an exact replay
            for request in ("main", "second"):
                for label, text in texts.items():
                    path = self.put(text)
                    with self.subTest(stored=stored, request=request, label=label), \
                            mock.patch.object(kit_track, "read_sent", side_effect=AssertionError("history")), \
                            mock.patch.object(kit_track, "record_step", side_effect=AssertionError("writer")):
                        calls = (lambda: kit_muckrock._load(path),
                                 lambda: kit_muckrock.propose(self.root, self.agency, path, request),
                                 lambda: kit_muckrock.run(self.root, self.agency, path, request),
                                 lambda: kit_muckrock.run(self.root, self.agency, path, request, apply=True))
                        for _ in range(2):
                            for call in calls:
                                with self.assertRaises(ValueError) as caught:
                                    call()
                                self.assertEqual(str(caught.exception), self.refused(path))
            self.sent.unlink(missing_ok=True)  # the second pass runs with no history at all

    def test_cli_four_modes_have_exact_value_free_refusal(self):
        self.fx.filed()
        path = self.put(wire('"id":90001', '"status":"processed"', UPDATED,
                             f'"{CANARY_KEY}\\u0007\\u001b[31m":"x"', f'"{CANARY_KEY}\\u0007\\u001b[31m":"{CANARY_VALUE}\\n"'))
        expected = f"Stopped: {self.refused(path)}\n"
        for flags in ((), ("--json",), ("--apply",), ("--json", "--apply")):
            with self.subTest(flags=flags):
                before = self.snapshot()
                for _ in range(2):
                    self.assertEqual(self.cli(path, *flags), (1, "", expected))
                result = subprocess.run(
                    [sys.executable, "-B", "-m", "campaign_tool", "kit-muckrock-import", "--directory", str(self.root),
                     "--agency", self.agency, "--from-json", str(path), *flags],
                    cwd=base.REPO_ROOT, capture_output=True, text=True, timeout=60)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (1, "", expected))
                for leaked in (CANARY_KEY, CANARY_VALUE, "Traceback", "Nothing was fetched", "Proposed"):
                    self.assertNotIn(leaked, result.stdout + result.stderr)
                self.assertEqual(self.snapshot(), before)

    def test_refusal_preserves_saved_history_and_campaign_artifacts(self):
        path = self.put(wire('"id":90001', '"status":"processed"', UPDATED, '"status":"processed"'))
        for filed in (False, True):  # absent-history control, then a real recorded request
            if filed:
                self.fx.filed()
            before = self.snapshot()
            archive = {p for p in self.root.rglob("*")}
            for flags in ((), ("--apply",), ("--json", "--apply")):
                self.assertEqual(self.cli(path, *flags)[0], 1)
                with self.assertRaises(ValueError):
                    kit_muckrock.run(self.root, self.agency, path, apply=True)
            self.assertEqual(self.snapshot(), before)
            self.assertEqual({p for p in self.root.rglob("*")}, archive)
        self.assertEqual(self.fx.steps(), [])
        # no socket or urlopen may have been touched: the fixture patches both to raise, so reaching here proves it

    def test_duplicate_refusal_closes_one_bounded_admitted_stream(self):
        path = self.put(wire('"id":90001', '"status":"processed"', '"id":90001'))
        stream = bounded.Growing()
        with stream.patching():
            self.assert_load_refused(path)
        self.assertEqual((stream.opens, stream.closed), (1, 1))
        self.assertEqual(stream.requests, [kit_muckrock.MAX_BYTES + 1])
        grown = bounded.Growing(kit_muckrock.MAX_BYTES + 1)  # grows past the cap after the descriptor check
        with grown.patching(), mock.patch.object(kit_muckrock.json, "loads", side_effect=AssertionError("parsed")):
            with self.assertRaises(ValueError) as caught:
                kit_muckrock._load(path)
        self.assertEqual(str(caught.exception), f"{path}: larger than 1 MiB")
        self.assertEqual((grown.opens, grown.closed), (1, 1))
        categories = []
        link = self.tmp / "link.json"
        link.symlink_to(path)
        categories.append((link, "not a regular file (symlinks are refused)"))
        categories.append((self.tmp, "not a regular file (symlinks are refused)"))
        categories.append((self.put(b'{"a":1,"a":2,\xff}', "bad.json"), "not UTF-8 JSON"))
        categories.append((self.put('{"a":1,"a":', "bad2.json"), "not UTF-8 JSON"))
        categories.append((self.put("[" * 100000 + "]" * 100000, "deep.json"), "JSON nesting is too deep"))
        categories.append((self.put('[{"a":1},{"a":2}]', "array.json"), "expected one JSON object (a single request)"))
        for target, message in categories:
            with self.subTest(target=target.name), self.assertRaises(ValueError) as caught:
                kit_muckrock._load(target)
            self.assertEqual(str(caught.exception), f"{target}: {message}")

    def test_unique_controls_still_preview_apply_replay_and_recover(self):
        unique = ('"meta":{"x":1,"y":{"x":2}},"other":{"x":3},"list":[{"x":1},{"x":2}],"Status":"ignored"')
        cases = (("processed", "acknowledged", "2026-03-10", None), ("fix", "clarification_requested", "2026-03-10", None),
                 ("payment", "fee_estimate", "2026-03-10", 24550), ("rejected", "denied", "2026-03-12", None),
                 ("no_docs", "closed", "2026-03-12", None), ("partial", "produced", "2026-03-12", None),
                 ("done", "produced", "2026-03-12", None), ("abandoned", "closed", "2026-03-12", None))
        for status, kind, on, cents in cases:
            with self.subTest(status=status):
                self.sent.unlink(missing_ok=True)
                self.fx.filed([] if status == "processed" else [("acknowledged", "2026-03-03")])
                before = len(self.fx.steps())
                text = wire('"id":90001', f'"status":"{status}"', UPDATED, DONE, '"price":"245.50"', unique)
                ambiguous = self.put(text.replace('"Status":"ignored"', '"Status":"ignored","id":90001'))
                self.assertEqual(self.cli(ambiguous, "--apply")[0], 1)  # ambiguity first: nothing recorded
                self.assertEqual(len(self.fx.steps()), before)
                path = self.put(text)  # the clean replacement of the same file
                snapshot = self.sent.read_bytes()
                code, out, err = self.cli(path, "--json")
                self.assertEqual(code, 0, err)
                step = json.loads(out[:out.rindex("}") + 1])["step"]
                self.assertEqual((step["kind"], step["on"]), (kind, on))
                self.assertEqual(self.sent.read_bytes(), snapshot)
                self.assertEqual(self.cli(path, "--apply")[0], 0)
                self.assertEqual(len(self.fx.steps()), before + 1)
                last = self.fx.steps()[-1]
                self.assertEqual((last["kind"], last["on"], last.get("amount_cents"), bool(last.get("partial"))),
                                 (kind, on, cents, status == "partial"))
                code, out, _ = self.cli(path, "--apply")
                self.assertEqual(code, 0)
                self.assertIn("already recorded", out)
                self.assertEqual(len(self.fx.steps()), before + 1)
        self.sent.unlink()
        self.fx.filed([("acknowledged", "2026-03-03")])
        code, out, _ = self.cli(self.put(wire('"id":90001', '"status":"payment"', UPDATED, '"price":null')), "--apply")
        self.assertEqual(code, 0)
        self.assertIn("no price in the JSON", out)
        code, out, _ = self.cli(self.put(wire('"id":90001', '"status":"started"', '"a":{"k":1},"b":{"k":2}')), "--apply")
        self.assertIn("waiting for the agency", out)
        code, _, err = self.cli(self.put(wire('"id":90001', '"status":"fix"', '"datetime_updated":"2026-03-01T00:00:00Z"')),
                                "--apply")  # before the request was sent: the normal timeline rule refuses
        self.assertEqual(code, 1)
        self.assertNotIn(REFUSAL, err)
        self.assertEqual(len(self.fx.steps()), 1)


if __name__ == "__main__":
    unittest.main()
