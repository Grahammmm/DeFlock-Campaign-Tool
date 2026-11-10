"""kit-response-undo turns an archive encoder RecursionError into one fixed refusal before any write.
Offline, synthetic: invented agencies and dates; no socket may be opened and nothing is sent.

Fixtures are reused through a module alias (not subclassed), so the existing suite is not rerun here."""
import errno
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit, kit_undo  # noqa: E402
from tests import test_kit_response_undo as base  # noqa: E402

FIXED = "kit/sent-undone.json cannot be serialized; nothing was changed"
STDERR = "Stopped: " + FIXED + "\n"
DEPTH = 400  # compact JSON every supported decoder admits (3.11 refuses decoding near 1000 levels)
BUDGET = 150  # frames the indented encoder may use above its caller; DEPTH levels need more
CANARY = "CANARY-private-archive-text"
real_size = kit_undo.serialized_size
# In the CLI subprocess the encoder is replaced by the RecursionError it raises on an over-deep archive.
CLI_RUNNER = ("import sys\nfrom campaign_tool import cli, kit_undo\n"
              "def fake(document):\n    if 'undos' in document:\n        raise RecursionError('maximum recursion depth exceeded')\n"
              "    raise AssertionError('active preflight reached')\n"
              "kit_undo.serialized_size = fake\ncli.main()\n")


def stack_depth():
    count, frame = 0, sys._getframe()
    while frame is not None:
        count, frame = count + 1, frame.f_back
    return count


class recursion_budget:
    """Lower the interpreter recursion limit to BUDGET frames above the caller (so only the encoder is squeezed)."""

    def __enter__(self):
        self.old = sys.getrecursionlimit()
        sys.setrecursionlimit(stack_depth() + BUDGET)

    def __exit__(self, *exc):
        sys.setrecursionlimit(self.old)


def _encoder_honors_budget():
    """3.13+ no longer bounds the pure-Python encoder's generator nesting by the recursion limit; 3.11/3.12 do."""
    document = json.loads("[" * DEPTH + "]" * DEPTH)
    try:
        with recursion_budget():
            real_size(document)
    except RecursionError:
        return True
    return False


ENCODER_HONORS_BUDGET = _encoder_honors_budget()


def deep_size(document):
    """serialized_size where the deep receipt really overflows the encoder (simulated where the interpreter cannot)."""
    if "undos" not in document:
        return real_size(document)
    if not ENCODER_HONORS_BUDGET:
        raise RecursionError("maximum recursion depth exceeded")
    with recursion_budget():
        return real_size(document)


def nesting(shape):
    if shape == "arrays":
        return "[" * DEPTH + "]" * DEPTH
    if shape == "objects":
        return '{"k":' * DEPTH + "1" + "}" * DEPTH
    return '[{"k":' * (DEPTH // 2) + "1" + "}]" * (DEPTH // 2)  # alternating containers


class KitUndoSerializationTests(unittest.TestCase):
    setUp = base.KitResponseUndoTests.setUp
    load, put, entry, undo, receipts = (getattr(base.KitResponseUndoTests, n)
                                        for n in ("load", "put", "entry", "undo", "receipts"))
    two_steps = base.KitResponseUndoTests.two_steps

    def files(self):  # membership, bytes and metadata of every file (never a recursive Python comparison)
        result = {}
        for path in sorted(self.root.rglob("*")):
            info = path.lstat()
            result[str(path.relative_to(self.root))] = (
                path.read_bytes() if path.is_file() else None, info.st_mode, info.st_ino, info.st_mtime_ns)
        return result

    def deepen(self, shape, extra=None):
        """Put a compact deep extension on the first retained receipt (written as text, never as a Python object)."""
        document = json.loads(self.archive.read_text(encoding="utf-8"))
        document["undos"][0]["ext"] = "@@DEEP@@"
        text = json.dumps(document).replace('"@@DEEP@@"', nesting(shape) if extra is None else extra)
        self.archive.write_text(text, encoding="utf-8")

    def prior_receipt_then_deep(self, shape="arrays"):
        self.two_steps()
        self.archive.unlink(missing_ok=True)
        self.assertEqual(self.undo(reason="first")[0], 0)
        self.put([base.step("2026-03-05", "acknowledged"), base.step("2026-03-06", "extension")],
                 undo_ids=[self.receipts()[0]["undo_id"]])
        self.deepen(shape)

    def cli(self):
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run([sys.executable, "-B", "-c", CLI_RUNNER, "kit-response-undo", "--directory",
                               str(self.root), "--agency", self.agency, "--reason", base.REASON],
                              cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)

    def test_reader_admitted_receipt_arrays_objects_and_mixed_nesting_refuse_cleanly(self):
        for shape in ("arrays", "objects", "mixed"):
            with self.subTest(shape=shape):
                self.prior_receipt_then_deep(shape)
                self.assertEqual(len(kit_undo.read_archive(self.root / "kit")[1]), 1)  # the decoder admits it
                before = self.files()
                with mock.patch.object(kit_undo, "serialized_size", new=deep_size), \
                        self.assertRaises(ValueError) as caught:
                    kit_undo.undo_step(self.root, self.agency, reason="second")
                self.assertEqual(str(caught.exception), FIXED)
                self.assertIsNone(caught.exception.__cause__)
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertEqual(self.files(), before)
                done = self.cli()
                self.assertEqual((done.returncode, done.stdout, done.stderr), (1, "", STDERR))
                self.assertEqual(self.files(), before)

    def test_archive_preflight_recursion_error_is_fixed_and_value_free(self):
        self.two_steps()
        self.assertEqual(self.undo(reason="first")[0], 0)
        self.put([base.step("2026-03-05", "acknowledged"), base.step("2026-03-06", "extension")],
                 undo_ids=[self.receipts()[0]["undo_id"]])
        before = self.files()
        for message in (CANARY, "", "/home/private/" + CANARY):
            with self.subTest(message=message):
                def fake(document):
                    if "undos" in document:  # only the archive candidate fails; the active store is not reached
                        raise RecursionError(message)
                    raise AssertionError("active preflight reached")
                with mock.patch.object(kit_undo, "serialized_size", side_effect=fake), \
                        mock.patch.object(kit, "_write_json", side_effect=AssertionError("written")), \
                        mock.patch.object(kit_undo, "_fsync", side_effect=AssertionError("synced")):
                    with self.assertRaises(ValueError) as caught:
                        kit_undo.undo_step(self.root, self.agency, reason="second")
                    self.assertEqual(str(caught.exception), FIXED)
                    self.assertIsNone(caught.exception.__cause__)
                    self.assertTrue(caught.exception.__suppress_context__)
                    code, out, err = self.undo(reason="second")  # the actual CLI dispatch in-process
                self.assertEqual((code, out, err), (1, "", STDERR))
                self.assertEqual(self.files(), before)

    def test_refusal_preserves_applied_and_attempted_receipts_and_both_stores(self):
        self.two_steps()
        document = self.load()
        document["requests"][self.agency + "#second"] = {**document["requests"][self.agency],
                                                         "draft": f"requests/{self.agency}--second.md"}
        document["requests"][self.agency + "#second"]["steps"] = [base.step("2026-03-06", "acknowledged")]
        self.sent.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        (self.root / "kit" / "sentinel.txt").write_text("keep\n", encoding="utf-8")
        self.assertEqual(self.undo(reason="first")[0], 0)  # a genuine applied receipt
        applied = self.receipts()[0]["undo_id"]
        self.put([base.step("2026-03-05", "acknowledged"), base.step("2026-03-06", "extension")],
                 undo_ids=[applied])
        real = kit._write_json

        def fail_sent(path, doc):
            if Path(path).name == "sent.json":
                raise OSError(errno.EIO, "injected")
            real(path, doc)
        with mock.patch.object(kit, "_write_json", side_effect=fail_sent):
            self.assertEqual(self.undo(reason="attempt")[0], 1)  # a genuine attempted receipt
        self.assertEqual(len(self.receipts()), 2)
        self.deepen("mixed")
        before = self.files()
        with mock.patch.object(kit_undo, "serialized_size", new=deep_size), \
                mock.patch.object(kit, "_write_json", side_effect=AssertionError("written")), \
                mock.patch.object(kit_undo, "_fsync", side_effect=AssertionError("synced")), \
                mock.patch("os.chmod", side_effect=AssertionError("chmod")), \
                mock.patch("os.mkdir", side_effect=AssertionError("mkdir")):
            self.assertEqual(self.undo(reason="third"), (1, "", STDERR))
        self.assertEqual(self.files(), before)
        self.assertEqual(self.entry()["undo_ids"], [applied])
        self.assertEqual(len(self.load()["requests"][self.agency + "#second"]["steps"]), 1)

    def test_ordinary_nested_receipts_and_root_extension_behavior_stay_compatible(self):
        self.two_steps()
        self.assertEqual(self.undo(reason="first")[0], 0)
        document = json.loads(self.archive.read_text(encoding="utf-8"))
        document["undos"][0]["ext"] = {"a": {"k": [1, [2, {"k": "\u00e9\u4e2d"}]]}, "b": {"k": [1, [2]]}}
        document["x_root"] = {"deep": [[[[[1]]]]]}
        self.archive.write_text(json.dumps(document), encoding="utf-8")
        self.put([base.step("2026-03-05", "acknowledged"), base.step("2026-03-06", "extension")],
                 undo_ids=[document["undos"][0]["undo_id"]])
        code, out, err = self.undo(reason="second")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("2 step(s) undone", out)
        self.assertIn("Recorded only. Nothing was sent.", out)
        written = self.archive.read_text(encoding="utf-8")
        self.assertEqual(written, json.dumps({"schema_version": 1, "undos": [document["undos"][0], self.receipts()[1]]},
                                             indent=2, ensure_ascii=False) + "\n")
        self.assertEqual(self.receipts()[0], document["undos"][0])
        self.assertEqual(oct(self.archive.stat().st_mode & 0o777), oct(0o600))
        # missing and empty archives remain successful controls
        for contents in (None, '{"schema_version": 1, "undos": []}\n'):
            self.two_steps()
            if contents is None:
                self.archive.unlink()
            else:
                self.archive.write_text(contents, encoding="utf-8")
            self.assertEqual(self.undo(reason="control")[0], 0)
            self.assertEqual(len(self.receipts()), 1)

    def test_other_archive_refusal_messages_are_unchanged(self):
        self.two_steps()
        self.assertEqual(self.undo(reason="first")[0], 0)
        self.put([base.step("2026-03-05", "acknowledged"), base.step("2026-03-06", "extension")],
                 undo_ids=[self.receipts()[0]["undo_id"]])
        document = json.loads(self.archive.read_text(encoding="utf-8"))
        document["undos"][0]["ext"] = "\ud800"  # reader-admitted, not encodable as UTF-8
        self.archive.write_text(json.dumps(document), encoding="ascii")
        before = self.files()
        code, out, err = self.undo(reason="second")
        self.assertEqual((code, out, err), (1, "", "Stopped: kit/sent-undone.json cannot be written as UTF-8; "
                                                    "nothing was changed\n"))
        self.assertEqual(self.files(), before)
        document["undos"][0]["ext"] = "x"
        self.archive.write_text(json.dumps(document), encoding="utf-8")
        with mock.patch.object(kit_undo, "ARCHIVE_MAX_BYTES", 10):  # a size overflow keeps its own message
            self.assertEqual(self.undo(reason="second")[0], 1)
            with self.assertRaisesRegex(ValueError, "unreadable: too large"):
                kit_undo.read_archive(self.root / "kit")

    def test_exception_translation_is_not_a_whole_command_catch(self):
        self.two_steps()

        def failing(exc):
            def fake(document):
                if "undos" in document:
                    raise exc
                return real_size(document)
            return fake
        for exc in (RuntimeError("boom"), KeyboardInterrupt(), MemoryError()):
            with self.subTest(exc=type(exc).__name__):
                before = self.files()
                with mock.patch.object(kit_undo, "serialized_size", side_effect=failing(exc)):
                    with self.assertRaises(type(exc)):
                        kit_undo.undo_step(self.root, self.agency, reason="x")
                self.assertEqual(self.files(), before)
        # a late write failure (RecursionError included) is not rewritten as "nothing was changed"
        with mock.patch.object(kit, "_write_json", side_effect=RecursionError("late")):
            with self.assertRaises(RecursionError):
                kit_undo.undo_step(self.root, self.agency, reason="x")
        # a late fsync failure keeps the attempted receipt and its own diagnostic
        with mock.patch("os.fsync", side_effect=OSError(errno.EIO, "injected sync")):
            code, out, err = self.undo(reason="synced")
        self.assertEqual(code, 1)
        self.assertIn("injected sync", err)
        self.assertNotIn("nothing was changed", err)
        self.assertEqual(len(self.receipts()), 1)
        self.assertNotIn("undo_ids", self.entry())


if __name__ == "__main__":
    unittest.main()
