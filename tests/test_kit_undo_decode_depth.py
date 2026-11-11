"""kit-response-undo refuses an archive the JSON decoder cannot finish (nesting exhaustion) without a traceback.
Offline, synthetic: invented agencies and dates; no socket may be opened and nothing is sent.

Fixtures are reused through a module alias (not subclassed), so the existing suite is not rerun here."""
import errno
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit, kit_undo  # noqa: E402
from tests import test_kit_response_undo as base  # noqa: E402

REASON_TEXT = "JSON nesting is too deep"
FIXED = "kit/sent-undone.json unreadable: " + REASON_TEXT
STDERR = "Stopped: " + FIXED + "\n"
DEPTH = 20000  # compact, far below the 16 MiB read cap, deeper than any supported decoder finishes
CANARY = "CANARY-private-archive-text"
real_open = os.open


def nesting(shape, depth=DEPTH):
    if shape == "arrays":
        return "[" * depth + "]" * depth
    if shape == "objects":
        return '{"k":' * depth + "1" + "}" * depth
    return '[{"k":' * (depth // 2) + "1" + "}]" * (depth // 2)  # alternating containers


def decoder_exhausts(shape):
    try:
        json.loads(nesting(shape))
    except RecursionError:
        return True
    return False


def decoder_raising(exc):
    def loads(text, **kwargs):
        raise exc
    return SimpleNamespace(loads=loads, dumps=json.dumps, JSONDecodeError=json.JSONDecodeError)


class KitUndoDecodeDepthTests(unittest.TestCase):
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

    def prior_receipt(self):  # one genuine applied receipt, the entry left with a step to undo again
        self.two_steps()
        self.archive.unlink(missing_ok=True)
        self.assertEqual(self.undo(reason="first")[0], 0)
        self.put([base.step("2026-03-05", "acknowledged"), base.step("2026-03-06", "extension")],
                 undo_ids=[self.receipts()[0]["undo_id"]])
        self.pristine = self.archive.read_text(encoding="utf-8")

    def deepen(self, text, where):
        """Write the retained archive with ``text`` as a root, receipt or archived-step extension (literal text)."""
        document = json.loads(self.pristine)
        if where == "root":
            document["x_root"] = "@@DEEP@@"
        elif where == "receipt":
            document["undos"][0]["ext"] = "@@DEEP@@"
        else:
            document["undos"][0]["step"]["ext"] = "@@DEEP@@"
        self.archive.write_text(json.dumps(document).replace('"@@DEEP@@"', text), encoding="utf-8")

    def cli(self):  # the actual command in a fresh interpreter, bytecode off, nothing written beside the campaign
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "kit-response-undo", "--directory",
                               str(self.root), "--agency", self.agency, "--reason", base.REASON],
                              cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)

    def assert_fixed(self, caught):
        self.assertEqual(str(caught.exception), FIXED)
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)

    # acceptance ----------------------------------------------------------------------------------------------
    def test_real_archive_decoder_exhaustion_arrays_objects_mixed(self):
        self.prior_receipt()
        for shape in ("arrays", "objects", "mixed"):
            if not decoder_exhausts(shape):
                self.skipTest("this interpreter's decoder finishes " + shape + " nesting at depth %d" % DEPTH)
            for where in ("root", "receipt", "step"):
                with self.subTest(shape=shape, where=where):
                    self.deepen(nesting(shape), where)
                    self.assertLess(self.archive.stat().st_size, kit_undo.ARCHIVE_MAX_BYTES)
                    before = self.files()
                    with self.assertRaises(ValueError) as caught:
                        kit_undo.read_archive(self.root / "kit")
                    self.assert_fixed(caught)
                    with self.assertRaises(ValueError) as caught:
                        kit_undo.undo_step(self.root, self.agency, reason="second")
                    self.assert_fixed(caught)
                    self.assertEqual(self.files(), before)
        # shallow control: the same placement is decoder-admitted and the undo succeeds
        self.deepen(nesting("mixed", 40), "receipt")
        self.assertEqual(len(kit_undo.read_archive(self.root / "kit")[1]), 1)
        self.assertEqual(self.undo(reason="second")[0], 0)
        self.assertEqual(len(self.receipts()), 2)

    def test_actual_cli_decode_refusal_has_exact_output(self):
        self.prior_receipt()
        for shape in ("arrays", "objects", "mixed"):
            if not decoder_exhausts(shape):
                self.skipTest("this interpreter's decoder finishes " + shape + " nesting at depth %d" % DEPTH)
            with self.subTest(shape=shape):
                self.deepen(nesting(shape), "receipt")
                before = self.files()
                done = self.cli()
                self.assertEqual((done.returncode, done.stdout, done.stderr), (1, "", STDERR))
                for word in ("Traceback", "Fix", "step(s)", "Recorded", CANARY, str(self.root)):
                    self.assertNotIn(word, done.stderr + done.stdout)
                self.assertEqual(self.files(), before)
                self.assertEqual(self.undo(reason=base.REASON), (1, "", STDERR))  # in-process dispatch matches
                self.assertEqual(self.files(), before)

    def test_decode_refusal_preserves_applied_and_attempted_receipts(self):
        self.prior_receipt()
        applied = self.receipts()[0]["undo_id"]
        (self.root / "kit" / "sentinel.txt").write_text("keep\n", encoding="utf-8")
        real = kit._write_json

        def fail_sent(path, doc):
            if Path(path).name == "sent.json":
                raise OSError(errno.EIO, "injected")
            real(path, doc)
        with mock.patch.object(kit, "_write_json", side_effect=fail_sent):
            self.assertEqual(self.undo(reason="attempt")[0], 1)  # a genuine attempted receipt
        self.assertEqual(len(self.receipts()), 2)
        if not decoder_exhausts("mixed"):
            self.skipTest("this interpreter's decoder finishes nesting at depth %d" % DEPTH)
        self.deepen(nesting("mixed"), "receipt")
        before = self.files()
        with mock.patch.object(kit_undo, "serialized_size", side_effect=AssertionError("sized")), \
                mock.patch.object(kit, "_write_json", side_effect=AssertionError("written")), \
                mock.patch.object(kit_undo, "_fsync", side_effect=AssertionError("synced")):
            self.assertEqual(self.undo(reason="third"), (1, "", STDERR))
        self.assertEqual(self.files(), before)
        self.assertEqual(self.entry()["undo_ids"], [applied])  # no attempt added, nothing applied
        self.assertEqual(len(self.entry()["steps"]), 2)

    def test_decoder_error_is_value_free_and_does_not_reach_writes(self):
        self.prior_receipt()
        before = self.files()
        for message in (CANARY, "", "/home/private/" + CANARY):
            with self.subTest(message=message):
                with mock.patch.object(kit_undo, "json", new=decoder_raising(RecursionError(message))), \
                        mock.patch.object(kit_undo, "serialized_size", side_effect=AssertionError("sized")), \
                        mock.patch.object(kit, "_write_json", side_effect=AssertionError("written")), \
                        mock.patch.object(kit_undo, "_fsync", side_effect=AssertionError("synced")), \
                        mock.patch("os.chmod", side_effect=AssertionError("chmod")), \
                        mock.patch("os.mkdir", side_effect=AssertionError("mkdir")):
                    for call in (lambda: kit_undo.read_archive(self.root / "kit"),
                                 lambda: kit_undo.undo_step(self.root, self.agency, reason="second")):
                        with self.assertRaises(ValueError) as caught:
                            call()
                        self.assert_fixed(caught)
                    self.assertEqual(self.undo(reason="second"), (1, "", STDERR))
                self.assertEqual(self.files(), before)
        # exhaustion raised from inside the decoder's own object-pairs hook is the same refusal
        with mock.patch.object(kit_undo, "_unique_members", side_effect=RecursionError(CANARY)), \
                self.assertRaises(ValueError) as caught:
            kit_undo.read_archive(self.root / "kit")
        self.assert_fixed(caught)

    def test_read_descriptor_is_closed_when_decoder_refuses(self):
        self.prior_receipt()
        opened = []

        def spy(path, *args, **kwargs):
            fd = real_open(path, *args, **kwargs)
            if Path(path).name == "sent-undone.json":
                opened.append(fd)
            return fd
        sentinel = real_open(os.devnull, os.O_RDONLY)
        self.addCleanup(os.close, sentinel)
        refusals = [("exhaustion", lambda: mock.patch.object(kit_undo, "json", new=decoder_raising(RecursionError()))),
                    ("syntax", lambda: mock.patch.object(kit_undo, "json", new=decoder_raising(
                        json.JSONDecodeError("x", "", 0))))]
        for name, patcher in refusals:
            with self.subTest(name=name):
                del opened[:]
                with patcher(), mock.patch("os.open", side_effect=spy), self.assertRaises(ValueError):
                    kit_undo.read_archive(self.root / "kit")
                self.assertEqual(len(opened), 1)
                with self.assertRaises(OSError) as caught:
                    os.fstat(opened[0])
                self.assertEqual(caught.exception.errno, errno.EBADF)
                os.fstat(sentinel)  # an unrelated descriptor is untouched
        # old controls: missing and bytes that are not UTF-8 still behave as before
        self.archive.write_bytes(b"\xff\xfe{")
        del opened[:]
        with mock.patch("os.open", side_effect=spy), self.assertRaisesRegex(ValueError, "unreadable: not JSON"):
            kit_undo.read_archive(self.root / "kit")
        with self.assertRaises(OSError):
            os.fstat(opened[0])
        self.archive.unlink()
        self.assertEqual(kit_undo.read_archive(self.root / "kit")[1], [])

    def test_exception_boundary_excludes_late_and_unexpected_failures(self):
        self.prior_receipt()
        for exc in (RuntimeError("boom"), MemoryError(), KeyboardInterrupt()):
            with self.subTest(exc=type(exc).__name__):
                with mock.patch.object(kit_undo, "json", new=decoder_raising(exc)), self.assertRaises(type(exc)):
                    kit_undo.read_archive(self.root / "kit")
        # a RecursionError outside the decode/load call is not relabelled as a decoder refusal
        before = self.files()
        with mock.patch("os.read", side_effect=RecursionError("read")), self.assertRaises(RecursionError):
            kit_undo.read_archive(self.root / "kit")
        with mock.patch.object(kit, "_write_json", side_effect=RecursionError("late")), \
                self.assertRaises(RecursionError):
            kit_undo.undo_step(self.root, self.agency, reason="second")
        self.assertEqual(len(self.receipts()), 1)  # failed before the archive was replaced
        self.assertEqual(self.files(), before)
        # the encoder refusal keeps its own message
        with mock.patch.object(kit_undo, "serialized_size", side_effect=RecursionError("encode")), \
                self.assertRaisesRegex(ValueError, "cannot be serialized; nothing was changed"):
            kit_undo.undo_step(self.root, self.agency, reason="second")
        # a late sent.json failure keeps the attempted receipt
        real = kit._write_json

        def fail_sent(path, doc):
            if Path(path).name == "sent.json":
                raise OSError(errno.EIO, "injected")
            real(path, doc)
        with mock.patch.object(kit, "_write_json", side_effect=fail_sent):
            code, out, err = self.undo(reason="second")
        self.assertEqual(code, 1)
        self.assertNotIn(REASON_TEXT, err)
        self.assertEqual(len(self.receipts()), 2)

    def test_ordinary_nested_append_and_refusal_categories_stay_compatible(self):
        self.prior_receipt()
        document = json.loads(self.archive.read_text(encoding="utf-8"))
        document["undos"][0]["ext"] = {"a": {"k": [1, [2, {"k": "\u00e9\u4e2d"}]]}, "b": {"k": [1, [2]]}}
        document["x_root"] = {"deep": [[[[[1]]]]]}
        self.archive.write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(self.undo(reason="second")[0], 0)
        self.assertEqual(self.receipts()[0], document["undos"][0])
        self.assertEqual(self.archive.read_text(encoding="utf-8"),
                         json.dumps({"schema_version": 1, "undos": [document["undos"][0], self.receipts()[1]]},
                                    indent=2, ensure_ascii=False) + "\n")
        self.assertEqual(oct(self.archive.stat().st_mode & 0o777), oct(0o600))
        for contents in (None, '{"schema_version": 1, "undos": []}\n'):  # missing and empty archives succeed
            self.two_steps()
            if contents is None:
                self.archive.unlink()
            else:
                self.archive.write_text(contents, encoding="utf-8")
            self.assertEqual(self.undo(reason="control")[0], 0)
            self.assertEqual(len(self.receipts()), 1)
        # the other refusals keep their exact messages
        undo_id = "0123456789abcdef0123456789abcdef"
        for text, reason in (('{"schema_version": 1, "undos": [], "undos": []}', "duplicate JSON keys"),
                             ("{not json", "not JSON"),
                             ('{"schema_version": 1, "undos": [{"undo_id": "%s"}, {"undo_id": "%s"}]}'
                              % (undo_id, undo_id), "every receipt needs its own undo_id")):
            with self.subTest(reason=reason):
                self.two_steps()
                self.archive.write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError) as caught:
                    kit_undo.read_archive(self.root / "kit")
                self.assertEqual(str(caught.exception), "kit/sent-undone.json unreadable: " + reason)
                self.assertEqual(self.undo(reason="x"), (1, "", "Stopped: kit/sent-undone.json unreadable: "
                                                         + reason + "\n"))


if __name__ == "__main__":
    unittest.main()
