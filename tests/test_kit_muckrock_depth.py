"""Real saved-JSON decoder failures remain offline, pre-history, no-write refusals."""
import json
import subprocess
import sys
import unittest
from unittest import mock

from campaign_tool import kit_muckrock, kit_track
from tests import test_kit_muckrock as base
from tests import test_kit_muckrock_bounded_read as bounded


class DepthTests(unittest.TestCase):
    # Reuse fixture operations without inheriting/discovering the original suite twice.
    setUp = base.KitMuckrockTests.setUp
    send = base.KitMuckrockTests.send
    filed = base.KitMuckrockTests.filed
    saved = base.KitMuckrockTests.saved
    imp = base.KitMuckrockTests.imp
    steps = base.KitMuckrockTests.steps

    def deep(self, shape="array", status="processed", top=False):
        depth = max(10_000, sys.getrecursionlimit() * 3)
        opening, closing = ("[", "]") if shape == "array" else ('{"nested":', "}")
        nested = opening * depth + '"SYNTHETIC-PAYLOAD-CANARY"' + closing * depth
        text = nested if top else (json.dumps({"id": 90001, "status": status,
                                              "datetime_updated": "2026-03-10T12:00:00Z"})[:-1]
                                   + ',"ignored":' + nested + "}")
        path = self.tmp / "deep.json"
        path.write_text(text, encoding="utf-8")
        self.assertLess(path.stat().st_size, kit_muckrock.MAX_BYTES)
        return path

    def snapshot(self):
        return {str(p.relative_to(self.tmp)): (p.stat().st_mode, p.stat().st_mtime_ns,
                                               p.read_bytes() if p.is_file() else None)
                for p in [self.tmp, *sorted(self.tmp.rglob("*"))]}

    def refusal(self, path, call):
        before = self.snapshot()
        with self.assertRaises(ValueError) as caught:
            call()
        self.assertEqual(str(caught.exception), f"{path}: JSON nesting is too deep")
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertEqual(self.snapshot(), before)

    def test_real_deep_array_object_and_top_array_refuse_with_closed_bounded_reader(self):
        for shape, top in (("array", False), ("object", False), ("array", True)):
            with self.subTest(shape=shape, top=top):
                path = self.deep(shape, top=top)
                # Establish that the actual decoder, rather than the fixture serializer, fails.
                with self.assertRaises(RecursionError):
                    json.loads(path.read_text(encoding="utf-8"))
                stream = bounded.Growing()
                with mock.patch.object(kit_muckrock, "open", stream, create=True):
                    self.refusal(path, lambda: kit_muckrock._load(path))
                self.assertEqual((stream.opens, stream.requests, stream.returned, stream.closed),
                                 (1, [kit_muckrock.MAX_BYTES + 1], [path.stat().st_size], 1))

    def test_all_public_callers_refuse_before_history_writer_and_shortcuts(self):
        self.filed([("acknowledged", "2026-03-10")])  # processed would be an exact replay
        for stored in (True, False):
            if not stored:
                self.sent.unlink()
            for shape in ("array", "object"):
                for status in ("processed", "started"):
                    path = self.deep(shape, status)
                    with self.subTest(stored=stored, shape=shape, status=status), \
                            mock.patch.object(kit_track, "read_sent", side_effect=AssertionError("history")), \
                            mock.patch.object(kit_track, "record_step", side_effect=AssertionError("writer")):
                        for call in (lambda: kit_muckrock._load(path),
                                     lambda: kit_muckrock.propose(self.root, self.agency, path),
                                     lambda: kit_muckrock.run(self.root, self.agency, path),
                                     lambda: kit_muckrock.run(self.root, self.agency, path, apply=True)):
                            self.refusal(path, call)

    def test_all_cli_modes_refuse_repeatably_without_partial_output_or_writes(self):
        self.filed()
        for stored in (True, False):
            if not stored:
                self.sent.unlink()
            for shape in ("array", "object"):
                path = self.deep(shape)
                for flags in ((), ("--json",), ("--apply",), ("--json", "--apply")):
                    with self.subTest(stored=stored, shape=shape, flags=flags), \
                            mock.patch.object(kit_track, "read_sent", side_effect=AssertionError("history")), \
                            mock.patch.object(kit_track, "record_step", side_effect=AssertionError("writer")):
                        before = self.snapshot()
                        for _ in range(2):
                            self.assertEqual(self.imp(path, *flags),
                                             (1, "", f"Stopped: {path}: JSON nesting is too deep\n"))
                            self.assertEqual(self.snapshot(), before)

    def test_fresh_cli_subprocess_has_exact_refusal_not_traceback(self):
        self.filed()
        for shape in ("array", "object"):
            path = self.deep(shape)
            for flags in ((), ("--json",), ("--apply",), ("--json", "--apply")):
                with self.subTest(shape=shape, flags=flags):
                    before = self.snapshot()
                    result = subprocess.run(
                        [sys.executable, "-B", "-m", "campaign_tool", "kit-muckrock-import",
                         "--directory", str(self.root), "--agency", self.agency, "--from-json", str(path), *flags],
                        cwd=base.REPO_ROOT, capture_output=True, text=True, timeout=60)
                    self.assertEqual((result.returncode, result.stdout, result.stderr),
                                     (1, "", f"Stopped: {path}: JSON nesting is too deep\n"))
                    self.assertEqual(self.snapshot(), before)

    def test_reader_error_categories_and_byte_priority_stay_unchanged(self):
        path = self.tmp / "control.json"
        for raw, message in ((b"\xff", "not UTF-8 JSON"), (b"{", "not UTF-8 JSON"),
                             (b"[1]", "expected one JSON object (a single request)")):
            path.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(ValueError) as caught:
                kit_muckrock._load(path)
            self.assertEqual(str(caught.exception), f"{path}: {message}")
        path = self.deep()
        with path.open("ab") as handle:
            handle.write(b" " * kit_muckrock.MAX_BYTES)
        with self.assertRaises(ValueError) as caught:
            kit_muckrock._load(path)
        self.assertEqual(str(caught.exception), f"{path}: larger than 1 MiB")
        path = self.deep()
        with mock.patch.object(kit_muckrock, "open", bounded.Growing(kit_muckrock.MAX_BYTES + 1), create=True), \
                mock.patch.object(kit_muckrock.json, "loads", side_effect=AssertionError("parsed")):
            with self.assertRaises(ValueError) as caught:
                kit_muckrock._load(path)
        self.assertEqual(str(caught.exception), f"{path}: larger than 1 MiB")
        path = self.saved()
        link = self.tmp / "link.json"
        link.symlink_to(path)
        for target in (link, self.tmp, self.tmp / "missing.json"):
            with self.subTest(target=target), self.assertRaises(ValueError) as caught:
                kit_muckrock._load(target)
            self.assertEqual(str(caught.exception), f"{target}: not a regular file (symlinks are refused)")

    def test_read_and_unrelated_parser_exceptions_are_not_translated(self):
        path = self.saved()
        # In particular a RecursionError from reading is NOT a decoder-depth refusal.
        for error in (OSError("read"), RecursionError("read")):
            stream = bounded.Growing(read_error=error)
            with mock.patch.object(kit_muckrock, "open", stream, create=True):
                with self.assertRaises(type(error)) as caught:
                    kit_muckrock._load(path)
            self.assertIs(caught.exception, error)
            self.assertEqual(stream.closed, 1)
        for error in (TypeError("programming"), RuntimeError("programming"), KeyboardInterrupt(), SystemExit(7)):
            with mock.patch.object(kit_muckrock.json, "loads", side_effect=error):
                with self.assertRaises(type(error)) as caught:
                    kit_muckrock._load(path)
            self.assertIs(caught.exception, error)

    def test_decoder_accepted_metadata_preserves_mapping_apply_and_replay(self):
        for status, kind, fields in (("processed", "acknowledged", {}),
                                     ("payment", "fee_estimate", {"price": "245.50"}),
                                     ("no_docs", "closed", {"datetime_done": "2026-03-12"})):
            with self.subTest(status=status):
                self.sent.unlink(missing_ok=True)
                self.filed([] if status == "processed" else [("acknowledged", "2026-03-03")])
                path = self.saved(status=status, ignored={"nested": [1, {"shallow": [True, None]}]}, **fields)
                before = self.snapshot()
                code, out, err = self.imp(path)
                self.assertEqual((code, err), (0, ""))
                self.assertIn("Dry run: nothing written", out)
                self.assertEqual(self.snapshot(), before)
                code, out, err = self.imp(path, "--json")
                self.assertEqual((code, err), (0, ""))
                result, _ = json.JSONDecoder().raw_decode(out)
                self.assertEqual(result["step"]["kind"], kind)
                self.assertFalse(result["applied"])
                self.assertEqual(self.snapshot(), before)
                count = len(self.steps())
                code, out, err = self.imp(path, "--apply")
                self.assertEqual((code, err), (0, ""))
                self.assertIn("Recorded.", out)
                self.assertEqual(len(self.steps()), count + 1)
                self.assertEqual(self.steps()[-1]["kind"], kind)
                after = self.snapshot()
                code, out, err = self.imp(path, "--json", "--apply")
                self.assertEqual((code, err), (0, ""))
                self.assertIn("already recorded", out)
                self.assertEqual(self.snapshot(), after)
        # Accepted nested metadata still goes through ordinary timeline validation.
        self.sent.unlink()
        self.filed([("acknowledged", "2026-03-10")])
        path = self.saved(status="rejected", datetime_done="2026-03-05", ignored=[{"ok": True}])
        before = self.snapshot()
        code, _, err = self.imp(path, "--apply")
        self.assertEqual(code, 1)
        self.assertIn("before the previous step", err)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
