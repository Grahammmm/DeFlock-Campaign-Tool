import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import shell
from campaign_tool.shell import render

ROOT = Path(__file__).resolve().parents[1]
MESSAGE = "Shell build stopped: JSON nesting is too deep\n"
DEPTH = 10000


def payload():
    return {"schema_version": 1, "language": "en",
            "html": {"head": "", "header": "", "main": "", "footer": ""},
            "styles": {"font_family": "Georgia,serif", "font_faces": ""}}


def deep_array():
    return b"[" * DEPTH + b"]" * DEPTH


def deep_object():
    return b'{"a":' * DEPTH + b"0" + b"}" * DEPTH


def deep_in_slot(slot_section, slot):
    body = json.dumps(payload())
    needle = '"%s": ""' % slot
    head, sep, tail = body.partition('"%s": {' % slot_section)
    idx = tail.index(needle)
    tail = tail[:idx] + ('"%s": ' % slot).encode().decode() + "[" * DEPTH + "]" * DEPTH + tail[idx + len(needle):]
    return (head + sep + tail).encode()


def run_main(raw):
    out, err = io.StringIO(), io.StringIO()
    stdin = mock.Mock()
    stdin.buffer = io.BytesIO(raw)
    with mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", out), \
            mock.patch.object(sys, "stderr", err):
        code = shell.main()
    return code, out.getvalue(), err.getvalue()


def run_child(raw, module=True):
    cmd = [sys.executable, "-B"] + (["-m", "campaign_tool.shell"] if module
                                   else [str(ROOT / "campaign_tool" / "shell.py")])
    return subprocess.run(cmd, input=raw, capture_output=True, cwd=ROOT, timeout=60)


class ShellDepthTests(unittest.TestCase):
    def test_real_overdeep_arrays_and_objects_refuse_cleanly(self):
        unknown = json.dumps(payload()).encode()[:-1] + b',"x":' + b"[" * DEPTH + b"]" * DEPTH + b"}"
        cases = [deep_array(), deep_object(), deep_in_slot("html", "main"),
                 deep_in_slot("styles", "font_faces"), unknown]
        for raw in cases:
            self.assertLess(len(raw), shell.MAX_INPUT)
            code, out, err = run_main(raw)
            self.assertEqual((code, out, err), (1, "", MESSAGE))

    def test_module_and_script_entrypoints_match_main(self):
        for module in (True, False):
            done = run_child(deep_array(), module)
            self.assertEqual((done.returncode, done.stdout, done.stderr),
                             (1, b"", MESSAGE.encode()))
            ok = run_child(json.dumps(payload()).encode(), module)
            self.assertEqual(ok.returncode, 0)
            self.assertEqual(ok.stderr, b"")
            self.assertEqual(json.loads(ok.stdout), render(payload()))

    def test_decoder_message_is_not_echoed_and_render_is_not_called(self):
        with mock.patch.object(shell.json, "loads", side_effect=RecursionError("SECRET-MARKER /path")), \
                mock.patch.object(shell, "render") as rendered:
            code, out, err = run_main(b"{}")
        self.assertEqual((code, out, err), (1, "", MESSAGE))
        rendered.assert_not_called()

    def test_translation_is_decoder_local(self):
        with mock.patch.object(shell, "render", side_effect=RecursionError("x")):
            with self.assertRaises(RecursionError):
                run_main(b"{}")
        for exc in (RuntimeError, KeyboardInterrupt):
            with mock.patch.object(shell.json, "loads", side_effect=exc("x")):
                with self.assertRaises(exc):
                    run_main(b"{}")
        stdin = mock.Mock()
        stdin.buffer.read.side_effect = RecursionError("x")
        with mock.patch.object(sys, "stdin", stdin):
            with self.assertRaises(RecursionError):
                shell.main()

    def test_cap_read_and_oversize_precedence_are_unchanged(self):
        reads = []

        class Buf(io.BytesIO):
            def read(self, n=-1):
                reads.append(n)
                return super().read(n)

        stdin = mock.Mock()
        stdin.buffer = Buf(deep_array())
        with mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", io.StringIO()), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            shell.main()
        self.assertEqual(reads, [shell.MAX_INPUT + 1])
        with mock.patch.object(shell, "MAX_INPUT", 100), \
                mock.patch.object(shell.json, "loads") as loads:
            code, out, err = run_main(b"[" * 101)
            self.assertEqual((code, out, err), (1, "", "Shell build stopped: Shell input too large\n"))
            loads.assert_not_called()
            code, out, err = run_main(b"[" * 100)
        self.assertEqual(code, 1)
        code, out, err = run_main(b"[" * (shell.MAX_INPUT + 1))
        self.assertEqual((code, out, err), (1, "", "Shell build stopped: Shell input too large\n"))

    def test_decodable_nested_wrong_types_keep_field_diagnostics(self):
        data = payload()
        data["html"]["main"] = [[1]]
        code, out, err = run_main(json.dumps(data).encode())
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("Shell build stopped: "))
        self.assertNotIn("nesting", err)
        code, out, err = run_main(b"[[[]]]")
        self.assertEqual((code, out, err), (1, "", "Shell build stopped: payload: must be an object\n"))
        code, out, err = run_main(b"{not json")
        self.assertEqual((code, out), (1, ""))
        self.assertNotIn("nesting", err)

    def test_success_bytes_and_read_only_behavior_are_unchanged(self):
        data = payload()
        data["html"]["main"] = "caf\u00e9 \U0001F600 {{FOOTER}}"
        raw = json.dumps(data).encode()
        code, out, err = run_main(raw)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, json.dumps(render(data), ensure_ascii=True))
        self.assertFalse(out.endswith("\n"))


if __name__ == "__main__":
    unittest.main()
