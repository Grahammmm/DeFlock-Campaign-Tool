import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import shell

ROOT = Path(__file__).resolve().parents[1]
FIXED = "Shell build stopped: duplicate JSON object member names\n"
HEALTHY = ('{"schema_version": 1, "language": "en", "html": {"head": "", "header": "", '
           '"main": "M", "footer": ""}, "styles": {"font_family": "Georgia,serif", "font_faces": ""}}')


def run_main(raw):
    out, err = io.StringIO(), io.StringIO()
    stdin = mock.Mock()
    stdin.buffer = io.BytesIO(raw)
    with mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", out), \
            mock.patch.object(sys, "stderr", err):
        code = shell.main()
    return code, out.getvalue(), err.getvalue()


def run_child(raw, module):
    cmd = [sys.executable, "-B"] + (["-m", "campaign_tool.shell"] if module
                                   else [str(ROOT / "campaign_tool" / "shell.py")])
    return subprocess.run(cmd, input=raw, capture_output=True, cwd=ROOT, timeout=60)


def expected(text):
    return json.dumps(shell.render(json.loads(text)), ensure_ascii=True)


class ShellDuplicateMemberTests(unittest.TestCase):
    def refused(self, text):
        with mock.patch.object(shell, "render") as render, \
                mock.patch.object(Path, "read_text") as read:
            code, out, err = run_main(text.encode() if isinstance(text, str) else text)
        self.assertEqual((code, out, err), (1, "", FIXED))
        render.assert_not_called()
        read.assert_not_called()

    def test_duplicate_top_level_and_slot_members_refuse_before_render(self):
        for text in (
            HEALTHY.replace('"schema_version": 1', '"schema_version": false, "schema_version": 1'),
            HEALTHY.replace('"main": "M"', '"main": [], "main": ""'),
            HEALTHY.replace('"main": "M"', '"main": "", "main": []'),
            HEALTHY.replace('"main": "M"', '"main": "M", "main": "M"'),
            HEALTHY.replace('"html": {', '"html": {}, "html": {', 1),
            HEALTHY.replace('"styles": {', '"styles": {}, "styles": {', 1),
        ):
            self.refused(text)

    def test_escaped_decoded_keys_and_nested_unknown_objects_refuse(self):
        self.refused(HEALTHY.replace('"main": "M"', '"main": "", "m\\u0061in": "M"'))
        self.refused(HEALTHY[:-1] + ', "extra": 1, "extra": 2}')
        self.refused(HEALTHY[:-1] + ', "extra": {"CANARYKEY": 1, "CANARYKEY": "CANARYVAL"}}')

    def test_distinct_objects_and_distinct_unicode_keys_are_not_duplicates(self):
        text = HEALTHY[:-1] + ', "extra": {"a": 1}, "other": {"a": 2}}'
        code, out, err = run_main(text.encode())
        self.assertEqual((code, out), (1, ""))
        self.assertIn("unknown field", err)
        self.assertNotIn("duplicate", err)
        code, out, err = run_main(HEALTHY[:-1].encode() + b', "\xc3\xa9": 1, "e\xcc\x81": 2}')
        self.assertEqual(code, 1)
        self.assertNotIn("duplicate", err)

    def test_cap_depth_and_unrelated_errors_keep_existing_precedence(self):
        big = b'{"a": 1, "a": 2, "pad": "' + b"x" * shell.MAX_INPUT + b'"}'
        self.assertEqual(run_main(big), (1, "", "Shell build stopped: Shell input too large\n"))
        deep = b"[" * 100000 + b"]" * 100000
        self.assertEqual(run_main(deep), (1, "", "Shell build stopped: JSON nesting is too deep\n"))
        for exc in (RuntimeError, KeyboardInterrupt):
            with mock.patch.object(shell, "render", side_effect=exc):
                with self.assertRaises(exc):
                    run_main(HEALTHY.encode())
        with mock.patch.object(shell, "render", side_effect=RecursionError):
            with self.assertRaises(RecursionError):
                run_main(HEALTHY.encode())

    def test_module_script_and_main_have_identical_duplicate_refusal(self):
        dup = HEALTHY.replace('"main": "M"', '"main": "", "main": "M"').encode()
        for module in (True, False):
            bad = run_child(dup, module)
            self.assertEqual((bad.returncode, bad.stdout, bad.stderr.decode()), (1, b"", FIXED))
            good = run_child(HEALTHY.encode(), module)
            self.assertEqual((good.returncode, good.stdout.decode()), (0, expected(HEALTHY)))

    def test_unique_member_payloads_keep_exact_bytes(self):
        text = HEALTHY.replace('"main": "M"', '"main": "q\\" \\ud83d\\ude00 é {{MAIN}}"')
        code, out, _ = run_main(text.encode())
        self.assertEqual((code, out), (0, expected(text)))
        self.assertTrue(out.isascii() and not out.endswith("\n"))
        for enc in ("utf-8-sig", "utf-16"):
            code, out, _ = run_main(HEALTHY.encode(enc))
            self.assertEqual((code, out), (0, expected(HEALTHY)))
        bad = HEALTHY.replace('"main": "M"', '"main": "\\ud800"')
        code, out, err = run_main(bad.encode())
        self.assertEqual((code, out), (1, ""))
        self.assertIn("html.main: must be UTF-8 encodable", err)


if __name__ == "__main__":
    unittest.main()
