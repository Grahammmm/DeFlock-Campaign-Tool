"""Overdeep stdin JSON for the standalone map builder: fixed refusal, no traceback (synthetic data only)."""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import map_controller

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "examples/fictional-campaign/map-config.json").read_text())
FIXED_ERR = "Map build stopped: JSON nesting is too deep\n"
DEPTH = 10000
DEEP_ARRAY = "[" * DEPTH + "]" * DEPTH
SENTINEL = "SENTINEL-Zq9-private"


def run(argv, stdin):
    out, err = io.StringIO(), io.StringIO()
    source = io.TextIOWrapper(io.BytesIO(stdin))
    with mock.patch.object(sys, "stdin", source), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = map_controller.main(argv)
    return code, out.getvalue(), err.getvalue()


def envelope(**overrides):
    """Raw JSON text for a build payload; each override is already-serialized raw JSON text."""
    parts = {"config": json.dumps(CONFIG), "profile_renderer": '""'}
    parts.update(overrides)
    return "{" + ",".join(f'"{k}":{v}' for k, v in parts.items()) + "}"


DEEP_CASES = {
    "top-array": DEEP_ARRAY,
    "config": envelope(config=DEEP_ARRAY),
    "profile": envelope(profile_renderer=DEEP_ARRAY),
    "context": envelope(point_context_renderer=DEEP_ARRAY),
    "unknown-field": envelope(extra=DEEP_ARRAY),
    "object": envelope(config='{"a":' * DEPTH + "1" + "}" * DEPTH),
}
VALID = envelope().encode()


def child(mode, stdin):
    cmd = [sys.executable, "-B", "-m", "campaign_tool.map_controller"] if mode == "module" else \
        [sys.executable, "-B", str(ROOT / "campaign_tool" / "map_controller.py")]
    done = subprocess.run(cmd, input=stdin, capture_output=True, cwd=ROOT, check=False)
    return done.returncode, done.stdout.decode(), done.stderr.decode()


class DepthRefusalTests(unittest.TestCase):
    def test_real_nested_payloads_get_fixed_stdin_refusal(self):
        for name, text in DEEP_CASES.items():
            self.assertLess(len(text), map_controller.MAX_BYTES)
            with self.subTest(case=name):
                self.assertEqual(run([], text.encode()), (1, "", FIXED_ERR))

    def test_module_script_and_argv_none_match_depth_refusal(self):
        for name in ("top-array", "profile"):
            raw = DEEP_CASES[name].encode()
            with self.subTest(case=name):
                self.assertEqual(child("module", raw), (1, "", FIXED_ERR))
                self.assertEqual(child("script", raw), (1, "", FIXED_ERR))
                with mock.patch.object(sys, "argv", ["prog"]):
                    self.assertEqual(run(None, raw), (1, "", FIXED_ERR))
        ok = run([], VALID)
        self.assertEqual(ok[0], 0)
        self.assertEqual(child("module", VALID)[0], 0)
        self.assertEqual(child("script", VALID)[0], 0)
        self.assertEqual(child("module", VALID)[1], ok[1])

    def test_decoder_message_and_payload_marker_are_never_echoed(self):
        err_seam = RecursionError(SENTINEL + " maximum recursion depth exceeded")
        with mock.patch.object(map_controller.json, "loads", side_effect=err_seam), \
                mock.patch.object(map_controller, "render") as render:
            self.assertEqual(run([], b"{}"), (1, "", FIXED_ERR))
            render.assert_not_called()
        real = run([], envelope(extra='"%s"' % SENTINEL, config=DEEP_ARRAY).encode())
        self.assertEqual(real, (1, "", FIXED_ERR))

    def test_recursion_translation_is_decoder_local(self):
        with mock.patch.object(map_controller, "render", side_effect=RecursionError("render")):
            with self.assertRaises(RecursionError):
                run([], VALID)
        with mock.patch.object(map_controller.json, "dumps", side_effect=RecursionError("dump")):
            with self.assertRaises(RecursionError):
                run([], VALID)
        with mock.patch.object(map_controller.json, "loads", side_effect=AssertionError("other")):
            with self.assertRaises(AssertionError):
                run([], VALID)

    def test_read_cap_and_oversize_precedence_unchanged(self):
        reads = []

        class Stream:
            def __init__(self, data):
                self.buffer = self
                self.data = data

            def read(self, *args):
                reads.append(args)
                return self.data[:args[0]]

        for cap_data, expected_code in ((b"[" * 8, 1), (b"[" * 9, 1)):
            reads.clear()
            err = io.StringIO()
            with mock.patch.object(map_controller, "MAX_BYTES", 8), \
                    mock.patch.object(map_controller.json, "loads", wraps=map_controller.json.loads) as loads, \
                    mock.patch.object(sys, "stdin", Stream(cap_data)), \
                    contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(err):
                self.assertEqual(map_controller.main([]), expected_code)
            self.assertEqual(reads, [(9,)])
            self.assertEqual(out.getvalue(), "")
            if len(cap_data) > 8:
                self.assertEqual(err.getvalue(), "Map build stopped: Map input too large\n")
                loads.assert_not_called()
            else:
                loads.assert_called_once()
        # deep oversized prefix keeps the size diagnostic with the real decoder.
        with mock.patch.object(map_controller, "MAX_BYTES", 8000):
            self.assertEqual(run([], DEEP_ARRAY.encode()), (1, "", "Map build stopped: Map input too large\n"))

    def test_shallow_invalid_and_deep_decodable_values_keep_existing_diagnostics(self):
        self.assertEqual(run([], b"[]"), (1, "", "Map build stopped: Invalid map build payload\n"))
        self.assertEqual(run([], b'{"config": 1, "profile_renderer": ""}')[0], 1)
        self.assertEqual(run([], b'{"config": {}, "profile_renderer": []}')[0], 1)
        self.assertEqual(run([], envelope(extra="[[1]]").encode()),
                         (1, "", "Map build stopped: Invalid map build payload\n"))
        code, out, err = run([], b"{not json")
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("Map build stopped: "))
        self.assertNotIn("nesting", err)
        code, out, err = run([], b'{"a": "\xff"}')
        self.assertEqual((code, out), (1, ""))
        self.assertNotIn("nesting", err)
        # decodable nesting just below the interpreter limit keeps the ordinary shape refusal
        shallow = "[" * 50 + "]" * 50
        self.assertEqual(run([], envelope(extra=shallow).encode()),
                         (1, "", "Map build stopped: Invalid map build payload\n"))

    def test_check_and_usage_contracts_are_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deep.json"
            path.write_text(DEEP_ARRAY)
            before = path.read_bytes()
            with mock.patch.object(sys, "stdin", mock.Mock(buffer=mock.Mock(read=mock.Mock(side_effect=AssertionError)))):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = map_controller.main(["--check", str(path)])
            self.assertEqual(code, 2)
            self.assertEqual(out.getvalue(), "")
            self.assertEqual(err.getvalue(), "Map check stopped: config file missing, unreadable, over 2 MB, "
                                             "not UTF-8 JSON or not an object\n")
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["deep.json"])
        self.assertEqual(run(["--bogus"], b"")[0], 2)

    def test_successful_controller_bytes_and_inputs_are_unchanged(self):
        hook = "function(p){return '{{GROUP_SLUG}} \u00e9\u4e2d';}"
        for text in (envelope(), envelope(point_context_renderer=json.dumps(hook, ensure_ascii=False)),
                     envelope(profile_renderer=json.dumps(hook, ensure_ascii=False))):
            value = json.loads(text)
            with self.subTest(keys=sorted(value)):
                expected = json.dumps({"javascript": map_controller.render(
                    value["config"], value["profile_renderer"], value.get("point_context_renderer", ""))})
                self.assertEqual(run([], text.encode()), (0, expected, ""))
                self.assertEqual(child("module", text.encode()), (0, expected, ""))


if __name__ == "__main__":
    unittest.main()
