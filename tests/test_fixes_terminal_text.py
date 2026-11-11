"""The offline `Stopped:` display shows non-LF nonprinting characters as visible escapes.

Synthetic temporary files only; nothing is sent or published.
"""
import contextlib
import errno
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import fixes

RECEIPT_FIX = "Fix: Choose a receipt path that does not exist yet.\n"


def emitted(exc):
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        fixes.emit(exc)
    return err.getvalue()


def tree_state(root):
    state = {}
    for path in sorted(Path(root).rglob("*")):
        st = path.lstat()
        data = path.read_bytes() if path.is_file() else None
        state[str(path.relative_to(root))] = (st.st_mode, st.st_mtime_ns, data)
    return state


class TerminalTextTests(unittest.TestCase):
    def test_nonprinting_character_policy_exhaustive(self):
        chars = [chr(i) for i in range(0x20) if i != 0x0A] + [chr(0x7F)] + [chr(i) for i in range(0x80, 0xA0)]
        chars += ["\u2028", "\u2029", "\u202e", "\u202a", "\u2066", "\u200b", "\u200f", "\ufeff", "\ud800", "\udbff", "\udc00", "\udfff"]
        for c in chars:
            out = fixes._display_text("a" + c + "b")
            self.assertEqual(out, "a" + ascii(c)[1:-1] + "b", hex(ord(c)))
            self.assertTrue(out.isascii(), hex(ord(c)))
        self.assertEqual(fixes._display_text("\x1b[2J"), "\\x1b[2J")
        self.assertEqual(fixes._display_text("a\r\nb\tc"), "a\\r\nb\\tc")
        self.assertEqual(fixes._display_text("\x9b2J\u2028\u202e\ud800"), "\\x9b2J\\u2028\\u202e\\ud800")

    def test_lf_and_printable_unicode_compatibility(self):
        self.assertEqual(emitted(ValueError("")), "Stopped: \n")
        self.assertEqual(emitted(KeyError("x")), "Stopped: 'x'\n")
        self.assertEqual(emitted(Exception("problem one\nproblem two")), "Stopped: problem one\nproblem two\n")
        self.assertEqual(emitted(Exception("\n\na\n\n")), "Stopped: \n\na\n\n\n")
        text = "caf\u00e9 \u6f22\u5b57 \U0001f600 \"q\" 'q' back\\slash"
        self.assertEqual(emitted(Exception(text)), "Stopped: " + text + "\n")
        # Intentional LF compatibility: apparent Fix prose after a raw LF stays inside the message.
        self.assertEqual(emitted(Exception("x\nFix: run it")), "Stopped: x\nFix: run it\n")

    def test_strict_utf8_stream_accepts_surrogate_exception(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="utf-8", errors="strict", newline="\n")
        saved = sys.stderr
        try:
            sys.stderr = stream
            fixes.emit(ValueError("lone \ud800 and \udc00 ok \u00e9"))
            stream.flush()
            data = raw.getvalue()
        finally:
            sys.stderr = saved
            stream.detach()
        self.assertEqual(data.decode("utf-8"), "Stopped: lone \\ud800 and \\udc00 ok \u00e9\n")

    def test_original_exception_controls_advice_not_escaped_text(self):
        samples = (FileNotFoundError(errno.ENOENT, "m", "/t/campaign.json"), ValueError("Missing campaign field: name"),
                   json.JSONDecodeError("m", "\n\nx", 2), PermissionError(errno.EACCES, "m", "/t"),
                   OSError(errno.ENOSPC, "m"), sqlite3.DatabaseError("file is not a database"),
                   ValueError("r.json exists; the drill never overwrites a receipt"))
        for exc in samples:
            before = fixes.fix_for(exc)
            self.assertIsNotNone(before, repr(exc))
            out = emitted(exc)
            self.assertTrue(out.endswith("Fix: " + before + "\n"), repr(exc))
            self.assertEqual(out.count("Fix: "), 1)
            self.assertEqual(fixes.fix_for(exc), before)
        unknown = ValueError("Missing campaign field: name\r")
        self.assertIsNone(fixes.fix_for(unknown))
        out = emitted(unknown)
        self.assertEqual(out, "Stopped: Missing campaign field: name\\r\n")
        err = OSError(errno.EIO, "m\x1b", "/t/\x1bx")
        args, filename = err.args, err.filename
        emitted(err)
        self.assertEqual((err.args, err.filename), (args, filename))


class ReceiptCliTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.archive = self.base / "absent.tar"

    def run_receipt(self, name):
        receipt = self.base / name
        receipt.write_text("{}")
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "backup-drill", "--file", str(self.archive),
                                 "--receipt", str(receipt)], cwd=REPO_ROOT, capture_output=True, text=True,
                                env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8"))
        return receipt, result

    def test_real_existing_receipt_preflight_escapes_before_archive_work(self):
        cases = {"r\x1b[2J.json": "r\\x1b[2J.json", "r\rx.json": "r\\rx.json", "r\u009b2J.json": "r\\x9b2J.json",
                 "r\u2028x.json": "r\\u2028x.json", "r\u202ex.json": "r\\u202ex.json", "ok.json": "ok.json"}
        for name, shown in cases.items():
            with self.subTest(name=name):
                before = tree_state(self.base)
                receipt, result = self.run_receipt(name)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, f"Stopped: {self.base}/{shown} exists; the drill never overwrites a receipt\n" + RECEIPT_FIX)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(self.archive.exists())
                after = tree_state(self.base)
                self.assertEqual({k: v for k, v in after.items() if k != name}, before)
                receipt.unlink()

    def test_refusal_display_is_read_only_and_nonsticky(self):
        receipt, result = self.run_receipt("r\x1b[2J.json")
        before = tree_state(self.base)
        self.assertIn("\\x1b[2J", result.stderr)
        emitted(ValueError(f"{receipt} exists; the drill never overwrites a receipt"))
        self.assertEqual(tree_state(self.base), before)
        self.assertEqual(emitted(ValueError("plain")), "Stopped: plain\n")


if __name__ == "__main__":
    unittest.main()
