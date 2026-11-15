import copy
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import shell

ROOT = Path(__file__).resolve().parents[1]
SLOTS = [("html", "head"), ("html", "header"), ("html", "main"), ("html", "footer"),
         ("styles", "font_family"), ("styles", "font_faces")]
SURROGATES = ["\ud800", "\udbff", "\udc00", "\udfff", "ok \ud800 \udfff \ud800\ud800 ok"]


def base():
    return {"schema_version": 1, "language": "en",
            "html": {"head": "", "header": "", "main": "", "footer": ""},
            "styles": {"font_family": "Georgia,serif", "font_faces": ""}}


def reason(section, key):
    return f"{section}.{key}: must be UTF-8 encodable"


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


class ShellUnicodeTests(unittest.TestCase):
    def test_each_known_slot_reports_high_and_low_surrogates(self):
        for section, key in SLOTS:
            for bad in SURROGATES:
                data = base()
                data[section][key] = bad
                before = copy.deepcopy(data)
                self.assertEqual(shell.problems(data), [reason(section, key)])
                with self.assertRaises(ValueError) as caught:
                    shell.render(data)
                self.assertEqual(str(caught.exception), reason(section, key))
                self.assertNotIsInstance(caught.exception, UnicodeEncodeError)
                self.assertEqual(data, before)

    def test_all_encoding_and_ordinary_problems_are_sorted_together(self):
        for flip in (False, True):
            html = {"head": "\ud800", "main": [], "a b": "v", "header": "\udc00"}
            styles = {"font_family": "Georgia", "font_faces": "a\udfffb"}
            sections = [("html", html), ("styles", styles)]
            data = {"schema_version": 2, "language": "\ud800"}
            for name, part in (reversed(sections) if flip else sections):
                data[name] = part
            expected = sorted([
                "html.head: must be UTF-8 encodable",
                "html.header: must be UTF-8 encodable",
                "html.main: must be a string",
                "missing field: html.footer",
                "unknown field: html.3 chars",
                "styles.font_faces: must be UTF-8 encodable",
                "schema_version: unsupported",
                "language: invalid tag",
            ])
            self.assertEqual(shell.problems(data), expected)
            with self.assertRaises(ValueError) as caught:
                shell.render(data)
            self.assertEqual(str(caught.exception), "; ".join(expected))
            self.assertNotIn("surrogate", str(caught.exception))
            self.assertNotIn("\\ud", str(caught.exception))
            self.assertNotIn("codec", str(caught.exception))

    def test_real_json_module_script_and_main_refuse_by_field(self):
        raw = (b'{"schema_version":1,"language":"en","html":{"head":"\\ud800","header":"",'
               b'"main":[],"footer":"x\\udfffy"},"styles":{"font_family":"G","font_faces":"\\udbff"}}')
        self.assertTrue(raw.isascii())
        fields = sorted([reason("html", "head"), "html.main: must be a string",
                         reason("html", "footer"), reason("styles", "font_faces")])
        message = "Shell build stopped: " + "; ".join(fields) + "\n"
        self.assertEqual(run_main(raw), (1, "", message))
        for module in (True, False):
            done = run_child(raw, module)
            self.assertEqual((done.returncode, done.stdout, done.stderr), (1, b"", message.encode()))
        single = base()
        single["html"]["main"] = "\udc00"
        done = run_child(json.dumps(single).encode(), True)
        self.assertEqual((done.returncode, done.stdout, done.stderr),
                         (1, b"", ("Shell build stopped: " + reason("html", "main") + "\n").encode()))

    def test_invalid_slot_does_not_mask_known_valid_byte_overflow(self):
        with mock.patch.object(shell, "MAX_INPUT", 20):
            data = base()
            data["html"]["head"] = "\ud800"
            data["html"]["main"] = "a" * 20
            data["styles"]["font_family"] = "b"
            self.assertEqual(shell.problems(data), sorted([reason("html", "head"), "input: too large"]))
            # An unencodable slot adds no bytes: the valid slots alone sit exactly on the cap.
            data["styles"]["font_family"] = ""
            self.assertEqual(shell.problems(data), [reason("html", "head")])
            # Count UTF-8 bytes, not code points or escape length: 3 + 4 + 13 = 20, then 21.
            ok = base()
            ok["html"]["head"] = "\u20ac"  # 3 bytes
            ok["html"]["header"] = "\U0001F600"  # 4 bytes
            ok["html"]["main"] = "a" * 13
            ok["styles"]["font_family"] = ""
            self.assertEqual(shell.problems(ok), [])
            ok["styles"]["font_faces"] = "a"
            self.assertEqual(shell.problems(ok), ["input: too large"])

    def test_raw_input_cap_keeps_precedence_over_slot_validation(self):
        filler = "a" * (shell.MAX_INPUT + 10)
        wire = ('{"schema_version":1,"language":"en","html":{"head":"\\ud800","header":"","main":"%s",'
                '"footer":""},"styles":{"font_family":"G","font_faces":""}}' % filler).encode()
        self.assertGreater(len(wire), shell.MAX_INPUT)
        self.assertEqual(run_main(wire), (1, "", "Shell build stopped: Shell input too large\n"))
        done = run_child(wire, True)
        self.assertEqual((done.returncode, done.stdout, done.stderr),
                         (1, b"", b"Shell build stopped: Shell input too large\n"))
        small = wire.replace(filler.encode(), b"a")
        self.assertEqual(run_main(small), (1, "", "Shell build stopped: " + reason("html", "head") + "\n"))

    def test_valid_unicode_and_decoded_surrogate_pairs_are_byte_identical(self):
        data = base()
        data["html"]["main"] = "caf\u00e9 e\u0301 \U0001F600 {{FOOTER}}"
        data["html"]["footer"] = "FOOT"
        data["styles"]["font_faces"] = "\U00010000"
        expected = shell.render(data)
        self.assertIn("{{FOOTER}}", expected["html"])
        self.assertIn("\U0001F600", expected["html"])
        escaped = json.dumps(data).replace("\U0001F600", "\\ud83d\\ude00").encode()
        self.assertIn(b"\\ud83d\\ude00", escaped)
        self.assertEqual(shell.problems(json.loads(escaped)), [])
        expected_bytes = json.dumps(expected, ensure_ascii=True)
        for raw in (json.dumps(data).encode(), json.dumps(data, ensure_ascii=False).encode("utf-8")):
            self.assertEqual(run_main(raw), (0, expected_bytes, ""))
        code, out, err = run_main(escaped)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, expected_bytes)
        self.assertFalse(out.endswith("\n"))
        done = run_child(escaped, False)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, expected_bytes.encode(), b""))
        # The same two code points as separate Python surrogates are refused, not joined.
        direct = copy.deepcopy(data)
        direct["html"]["main"] = "\ud83d\ude00"
        self.assertEqual(shell.problems(direct), [reason("html", "main")])

    def test_encoding_refusal_precedes_templates_without_widening_catches(self):
        bad = base()
        bad["html"]["main"] = "\ud800"
        trap = mock.MagicMock()
        trap.__truediv__.side_effect = AssertionError("template touched")
        with mock.patch.object(shell, "ASSETS", trap):
            with self.assertRaises(ValueError):
                shell.render(bad)
            self.assertEqual(run_main(json.dumps(bad).encode())[:2], (1, ""))
        # Ordinary template trouble keeps its current behaviour for a good payload.
        with mock.patch.object(shell, "ASSETS", ROOT / "no-such-assets"):
            code, out, err = run_main(json.dumps(base()).encode())
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("Shell build stopped: "))

        for exc in (RuntimeError, KeyboardInterrupt):
            class Boom(str):
                def encode(self, *args, **kwargs):
                    raise exc("x")

            odd = base()
            odd["html"]["main"] = Boom("x")
            with self.assertRaises(exc):
                shell.problems(odd)
        # No global state change: a good call right after a refusal still renders.
        before = copy.deepcopy(bad)
        with self.assertRaises(ValueError):
            shell.render(bad)
        self.assertEqual(bad, before)
        self.assertEqual(shell.problems(base()), [])
        self.assertEqual(sorted(shell.render(base())), ["css", "html"])


if __name__ == "__main__":
    unittest.main()
