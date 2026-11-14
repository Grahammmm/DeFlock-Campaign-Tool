"""kit-response-undo refuses an archive holding a JSON integer the interpreter will not convert, as an archive
refusal with one fixed message, before any write. Offline, synthetic: invented agencies and dates; no socket is opened.

Fixtures are reused through a module alias (not subclassed), so the existing suite is not rerun here. Integer tokens
are literal JSON text; no over-limit Python integer is ever built to be serialized."""
import errno
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit, kit_undo  # noqa: E402
from tests import test_kit_response_undo as base  # noqa: E402

FIXED = "kit/sent-undone.json unreadable: JSON integer cannot be decoded"
STDERR = "Stopped: " + FIXED + "\n"
LIMIT = sys.get_int_max_str_digits()
LONG = "9" * (LIMIT + 201)  # a syntactically valid JSON integer the interpreter refuses to convert
CANARY = "CANARY-private-archive-text"
real_open = os.open


def refused_by_interpreter(token):
    try:
        int(token)
    except ValueError:
        return True
    return False


def envelope(extension, undos="[]"):  # literal archive text; the extension is an ignored root member
    return '{"schema_version": 1, "undos": %s, "x_root": %s}' % (undos, extension)


class KitUndoIntegerTests(unittest.TestCase):
    setUp = base.KitResponseUndoTests.setUp
    load, put, entry, undo, receipts, snapshot = (getattr(base.KitResponseUndoTests, n) for n in (
        "load", "put", "entry", "undo", "receipts", "snapshot"))
    two_steps = base.KitResponseUndoTests.two_steps

    def files(self):  # membership, bytes and metadata of every path (never a recursive Python comparison)
        result = {}
        for path in sorted(self.root.rglob("*")):
            info = path.lstat()
            result[str(path.relative_to(self.root))] = (
                path.read_bytes() if path.is_file() else None, info.st_mode, info.st_ino, info.st_mtime_ns)
        return result

    def kit_dir(self):
        return self.root / "kit"

    def prior_receipt(self):  # one genuine applied receipt, the entry left with a step to undo again
        self.two_steps()
        self.archive.unlink(missing_ok=True)
        self.assertEqual(self.undo(reason="first")[0], 0)
        self.put([base.step("2026-03-05", "acknowledged"), base.step("2026-03-06", "extension")],
                 undo_ids=[self.receipts()[0]["undo_id"]])
        self.pristine = self.archive.read_text(encoding="utf-8")

    def write_archive(self, text):
        self.archive.write_text(text, encoding="utf-8")

    def cli(self):  # the actual command in a fresh interpreter, bytecode off, from a file-based entry point
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "kit-response-undo", "--directory",
                               str(self.root), "--agency", self.agency, "--reason", base.REASON],
                              cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)

    def assert_fixed(self, caught):
        text = str(caught.exception)
        self.assertEqual(text, FIXED)
        for word in ("4300", "digits", "set_int_max_str_digits", "limit", LONG[:20]):
            self.assertNotIn(word, text)
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)

    def setUpLong(self):
        if not refused_by_interpreter(LONG):
            self.skipTest("this interpreter does not limit integer string conversion")

    # acceptance ----------------------------------------------------------------------------------------------
    def test_default_limit_positive_and_negative_tokens_are_archive_scoped(self):
        self.setUpLong()
        self.two_steps()
        for token in (LONG, "-" + LONG):
            with self.subTest(token_sign=token[0]):
                self.archive.unlink(missing_ok=True)
                self.write_archive(envelope(token))
                self.assertLess(self.archive.stat().st_size, kit_undo.ARCHIVE_MAX_BYTES)
                before = self.files()
                with self.assertRaises(ValueError) as caught:
                    kit_undo.read_archive(self.kit_dir())
                self.assert_fixed(caught)
                with self.assertRaises(ValueError) as caught:
                    kit_undo.undo_step(self.root, self.agency, reason="second")
                self.assert_fixed(caught)
                self.assertEqual(self.files(), before)
        self.assertEqual(sys.get_int_max_str_digits(), LIMIT)  # the safety limit stays in effect

    def test_integer_refusal_inside_existing_receipt_preserves_every_receipt(self):
        self.setUpLong()
        self.prior_receipt()
        document = json.loads(self.pristine)
        document["undos"][0]["ext"] = "@@LONG@@"
        document["undos"][0]["step"]["ext2"] = ["@@LONG@@"]
        self.write_archive(json.dumps(document).replace('"@@LONG@@"', LONG))
        applied = self.entry()["undo_ids"]
        before = self.files()
        with self.assertRaises(ValueError) as caught:
            kit_undo.undo_step(self.root, self.agency, reason="second")
        self.assert_fixed(caught)
        self.assertEqual(self.files(), before)
        self.assertEqual(self.entry()["undo_ids"], applied)  # no attempted receipt, nothing replaced by []
        self.assertEqual(len(self.entry()["steps"]), 2)
        self.assertEqual(self.undo(reason="second"), (1, "", STDERR))
        self.assertEqual(self.files(), before)

    def test_actual_cli_refuses_without_success_or_store_changes(self):
        self.setUpLong()
        self.two_steps()
        self.write_archive(envelope(LONG))
        before = self.files()
        for attempt in range(2):  # repeated refusal changes nothing
            done = self.cli()
            self.assertEqual((done.returncode, done.stdout, done.stderr), (1, "", STDERR))
            self.assertEqual(self.undo(reason=base.REASON), (1, "", STDERR))  # in-process dispatch matches
            for word in ("Traceback", "digits", "Recorded", str(self.root)):
                self.assertNotIn(word, done.stderr)
            self.assertEqual(self.files(), before)
        self.assertEqual(len(self.entry()["steps"]), 2)
        self.assertFalse([p for p in self.root.rglob("*") if ".tmp" in p.name])
        # a fresh valid retry after a fixture-only repair succeeds and records exactly one receipt
        self.write_archive(envelope("123"))
        done = self.cli()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(len(self.receipts()), 1)
        self.assertEqual(len(self.entry()["steps"]), 1)

    def test_only_integer_conversion_valueerror_is_translated(self):
        self.setUpLong()
        with self.assertRaises(kit_undo._IntegerRefused):  # the real failing conversion
            kit_undo._parse_int(LONG)
        self.assertEqual(kit_undo._parse_int("-17"), -17)
        self.two_steps()
        self.write_archive(envelope("1"))
        for exc in (RuntimeError("boom"), MemoryError(), KeyboardInterrupt()):
            with self.subTest(exc=type(exc).__name__):
                with mock.patch.object(kit_undo, "int", create=True, side_effect=exc), self.assertRaises(type(exc)):
                    kit_undo.read_archive(self.kit_dir())
        # an injected conversion ValueError is the same value-free refusal, whatever its text
        for message in (CANARY, "", "/home/private/" + CANARY):
            with self.subTest(message=message):
                with mock.patch.object(kit_undo, "int", create=True, side_effect=ValueError(message)), \
                        self.assertRaises(ValueError) as caught:
                    kit_undo.read_archive(self.kit_dir())
                self.assert_fixed(caught)
                self.assertNotIn(CANARY, str(caught.exception))
        # ValueErrors raised after decoding keep their own diagnostics
        before = self.files()
        with mock.patch.object(kit_undo, "serialized_size", side_effect=ValueError("late-size")), \
                self.assertRaisesRegex(ValueError, "^late-size$"):
            kit_undo.undo_step(self.root, self.agency, reason="second")
        with mock.patch.object(kit, "_write_json", side_effect=ValueError("late-write")), \
                self.assertRaisesRegex(ValueError, "^late-write$"):
            kit_undo.undo_step(self.root, self.agency, reason="second")
        self.assertEqual(self.files(), before)
        # duplicate members and decoder recursion are never relabelled as an integer refusal
        self.write_archive('{"schema_version": 1, "undos": [], "a": {"k": 1, "k": 2}, "b": %s}' % LONG)
        with self.assertRaisesRegex(ValueError, "^kit/sent-undone.json unreadable: duplicate JSON keys$"):
            kit_undo.read_archive(self.kit_dir())
        self.write_archive(envelope("1"))
        with mock.patch.object(kit_undo, "_unique_members", side_effect=RecursionError(CANARY)), \
                self.assertRaisesRegex(ValueError, "^kit/sent-undone.json unreadable: JSON nesting is too deep$"):
            kit_undo.read_archive(self.kit_dir())

    def test_existing_refusal_precedence_and_archive_reader_resources(self):
        self.setUpLong()
        self.two_steps()
        prefix = "kit/sent-undone.json unreadable: "
        cases = [("not JSON", envelope("1").encode()[:-1] + b"[]"),
                 ("not JSON", b"\xff\xfe" + LONG.encode()),
                 ("duplicate JSON keys", ('{"schema_version": 1, "undos": [], "a": {"k": 1, "k": 2}, "b": %s}'
                                          % LONG).encode()),
                 ("expected schema_version 1 with an undos list", ("[%s]" % LONG).encode())]
        for reason, data in cases:
            with self.subTest(reason=reason, head=data[:12]):
                self.archive.write_bytes(data)
                # decoding precedes envelope validation: a long integer in an otherwise invalid envelope is still the
                # integer refusal; syntax, UTF-8 and duplicate-member refusals are decided first where they come first
                expected = FIXED if reason.startswith("expected") else prefix + reason
                with self.assertRaisesRegex(ValueError, "^" + re.escape(expected) + "$"):
                    kit_undo.read_archive(self.kit_dir())
        # byte overflow is decided before any decoding
        self.write_archive(envelope(LONG))
        with mock.patch.object(kit_undo, "ARCHIVE_MAX_BYTES", 100), \
                self.assertRaisesRegex(ValueError, "^" + prefix + "too large$"):
            kit_undo.read_archive(self.kit_dir())
        # missing means no receipts; symlink and directory keep their refusals
        self.archive.unlink()
        self.assertEqual(kit_undo.read_archive(self.kit_dir())[1], [])
        (self.root / "elsewhere.json").write_text(envelope(LONG), encoding="utf-8")
        self.archive.symlink_to(self.root / "elsewhere.json")
        with self.assertRaisesRegex(ValueError, "^" + prefix + "not a regular file$"):
            kit_undo.read_archive(self.kit_dir())
        self.archive.unlink()
        self.archive.mkdir()
        with self.assertRaisesRegex(ValueError, "^" + prefix + "not a regular file$"):
            kit_undo.read_archive(self.kit_dir())
        self.archive.rmdir()
        # the one acquired descriptor is released on an integer refusal; an unrelated descriptor is untouched
        self.write_archive(envelope(LONG))
        opened = []

        def spy(path, *args, **kwargs):
            fd = real_open(path, *args, **kwargs)
            if Path(path).name == "sent-undone.json":
                opened.append(fd)
            return fd
        sentinel = real_open(os.devnull, os.O_RDONLY)
        self.addCleanup(os.close, sentinel)
        with mock.patch("os.open", side_effect=spy), self.assertRaises(ValueError):
            kit_undo.read_archive(self.kit_dir())
        self.assertEqual(len(opened), 1)
        with self.assertRaises(OSError) as caught:
            os.fstat(opened[0])
        self.assertEqual(caught.exception.errno, errno.EBADF)
        os.fstat(sentinel)

    def test_accepted_integers_and_json_number_forms_keep_exact_semantics(self):
        near = "9" * LIMIT  # the largest token the runtime still converts
        self.prior_receipt()
        document = json.loads(self.pristine)
        prior = document["undos"][0]
        prior["ext"] = "@@NUMBERS@@"
        numbers = ["0", "-0", "7", "-7", near, "-" + near, "1.5", "1e5", "-2.25E-3"]
        text = json.dumps(document).replace('"@@NUMBERS@@"', "[" + ", ".join(numbers) + "]")
        text = text.replace('"schema_version": 1', '"x_root": 123, "schema_version": 1', 1)
        self.write_archive(text)
        receipts = kit_undo.read_archive(self.kit_dir())[1]
        decoded = receipts[0]["ext"]
        self.assertEqual([type(v) is int for v in decoded], [True] * 6 + [False] * 3)
        self.assertEqual(decoded[:5], [0, 0, 7, -7, int(near)])
        self.assertTrue(decoded[5] == -int(near) and decoded[6:] == [1.5, 100000.0, -0.00225])
        self.assertEqual(self.undo(reason="second")[0], 0)
        after = self.receipts()
        self.assertEqual(len(after), 2)
        self.assertEqual(after[0], receipts[0])  # unknown receipt fields retained
        self.assertEqual(self.archive.read_text(encoding="utf-8"),
                         json.dumps({"schema_version": 1, "undos": after}, indent=2, ensure_ascii=False) + "\n")
        self.assertEqual(oct(self.archive.stat().st_mode & 0o777), oct(0o600))

    def test_child_local_numeric_limit_never_changes_parent_policy(self):
        child_limit = 700
        script = Path(self.tmp) / "child_limit.py"
        script.write_text(
            "import json, sys\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from campaign_tool import kit_undo\n"
            "sys.set_int_max_str_digits(%d)\n"
            "directory = Path(sys.argv[2])\n"
            "result = {}\n"
            "for name, digits in (('control', %d), ('over', %d)):\n"
            "    (directory / 'sent-undone.json').write_text('{\"schema_version\": 1, \"undos\": [], \"x\": ' + "
            "'9' * digits + '}', encoding='utf-8')\n"
            "    try:\n"
            "        result[name] = len(kit_undo.read_archive(directory)[1])\n"
            "    except ValueError as exc:\n"
            "        result[name] = str(exc)\n"
            "result['limit'] = sys.get_int_max_str_digits()\n"
            "print(json.dumps(result))\n" % (child_limit, child_limit, child_limit + 1), encoding="utf-8")
        scratch = Path(self.tmp) / "child_kit"
        scratch.mkdir()
        before = sys.get_int_max_str_digits()
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        done = subprocess.run([sys.executable, "-B", str(script), str(REPO_ROOT), str(scratch)],
                              capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), {"control": 0, "over": FIXED, "limit": child_limit})
        self.assertEqual(sys.get_int_max_str_digits(), before)


if __name__ == "__main__":
    unittest.main()
