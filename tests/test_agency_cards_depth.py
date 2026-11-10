"""Decoder-depth refusals for the standalone builder; synthetic, offline inputs only."""
import contextlib
import copy
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import agency_cards as cards

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "examples/fictional-campaign/agency-cards.json").read_text())
DEPTH_ERROR = "Agency-card build stopped: JSON nesting is too deep\n"
CHECK_ERROR = ("Agency-card check stopped: config file missing, unreadable, over 2 MB, "
               "not UTF-8 JSON or not an object\n")
CANARY = "Fictional-depth-canary-é"


def deep_inputs():
    array = b"[" * 10000 + json.dumps(CANARY).encode() + b"]" * 10000
    obj = b'{"a":' * 10000 + b"0" + b"}" * 10000
    return (array, obj, b'{"county_profile":' + array + b"}",
            b'{"county_profile":{"facts":' + array + b"}}",
            json.dumps(CONFIG).encode()[:-1] + b',"extra":' + array + b"}")


class BinaryInput:
    def __init__(self, raw):
        self.buffer = mock.Mock(wraps=io.BytesIO(raw))


class NoInput:
    @property
    def buffer(self):
        raise AssertionError("unexpected stdin access")


def run(raw=b"", argv=(), source=None):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "stdin", source if source is not None else BinaryInput(raw)), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status = cards.main(None if argv is None else list(argv))
    return status, out.getvalue(), err.getvalue()


class AgencyCardDepthTests(unittest.TestCase):
    def test_real_overdeep_json_refuses_before_render(self):
        for raw in deep_inputs():
            with self.subTest(prefix=raw[:30]):
                self.assertLess(len(raw), cards.MAX_BYTES)
                with self.assertRaises(RecursionError):
                    json.loads(raw)
                with mock.patch.object(cards, "render") as render, \
                        mock.patch.object(Path, "read_text") as template:
                    self.assertEqual(run(raw), (1, "", DEPTH_ERROR))
                render.assert_not_called()
                template.assert_not_called()

    def test_module_script_and_argv_none_agree(self):
        success = json.dumps({"javascript": cards.render(CONFIG)}).encode()
        cases = [(deep_inputs()[0], 1, b"", DEPTH_ERROR.encode()),
                 (json.dumps(CONFIG).encode(), 0, success, b""),
                 (b"[]", 1, b"", b"Agency-card build stopped: config: must be an object\n")]
        for raw, status, out, err in cases:
            with mock.patch.object(sys, "argv", ["agency_cards"]):
                self.assertEqual(run(raw, None), (status, out.decode(), err.decode()))
                self.assertEqual(run(raw), (status, out.decode(), err.decode()))
            for entry in (["-m", "campaign_tool.agency_cards"], ["campaign_tool/agency_cards.py"]):
                with self.subTest(entry=entry, status=status):
                    done = subprocess.run([sys.executable, "-B", *entry], cwd=ROOT,
                                          input=raw, capture_output=True, timeout=30)
                    self.assertEqual((done.returncode, done.stdout, done.stderr), (status, out, err))

    def test_decoder_failure_message_and_input_canary_not_echoed(self):
        with mock.patch.object(cards.json, "loads", side_effect=RecursionError(CANARY)) as loads, \
                mock.patch.object(cards, "render") as render:
            self.assertEqual(run(CANARY.encode()), (1, "", DEPTH_ERROR))
        loads.assert_called_once_with(CANARY.encode())
        render.assert_not_called()

    def test_recursion_catch_is_decoder_local(self):
        source = BinaryInput(b"{}")
        source.buffer.read.side_effect = RecursionError("read seam")
        with self.assertRaisesRegex(RecursionError, "read seam"):
            run(source=source)
        valid_raw = json.dumps(CONFIG).encode()
        for target in ("render", "json.dumps"):
            with self.subTest(target=target), \
                    mock.patch("campaign_tool.agency_cards." + target,
                               side_effect=RecursionError("output seam")), \
                    self.assertRaisesRegex(RecursionError, "output seam"):
                run(valid_raw)
        for exc in (AssertionError, KeyboardInterrupt, SystemExit):
            with self.subTest(exc=exc), mock.patch.object(cards.json, "loads", side_effect=exc), \
                    self.assertRaises(exc):
                run(b"{}")

    def test_bounded_read_and_oversize_precedence(self):
        raw = json.dumps(CONFIG).encode()
        source = BinaryInput(raw)
        with mock.patch.object(cards, "MAX_BYTES", len(raw)):
            self.assertEqual(run(source=source), (0, json.dumps({"javascript": cards.render(CONFIG)}), ""))
        source.buffer.read.assert_called_once_with(len(raw) + 1)
        for raw in (raw + b" ", deep_inputs()[0]):
            source = BinaryInput(raw)
            with mock.patch.object(cards, "MAX_BYTES", 64), \
                    mock.patch.object(cards.json, "loads") as loads, \
                    mock.patch.object(cards, "render") as render:
                self.assertEqual(run(source=source),
                                 (1, "", "Agency-card build stopped: Agency-card input too large\n"))
            source.buffer.read.assert_called_once_with(65)
            loads.assert_not_called()
            render.assert_not_called()

    def test_shallow_or_decodable_nested_invalid_values_keep_diagnostics(self):
        cases = [(b"[]", "config: must be an object")]
        for field, value, message in (
                ("county_profile", [], "county_profile: must be an object"),
                ("source_aliases", [], "source_aliases: must be an object of at most 100 pairs"),
                ("extra", [[[]]], "unknown field: extra")):
            cases.append((json.dumps({**CONFIG, field: value}).encode(), message))
        value = copy.deepcopy(CONFIG)
        value["county_profile"]["facts"] = [["nested"]]
        cases.append((json.dumps(value).encode(), "county_profile.facts: item 0 not plain text"))
        value = {**CONFIG, "default_fragment": "BAD", "extra": "synthetic"}
        cases.append((json.dumps(value).encode(), "default_fragment: invalid slug; unknown field: extra"))
        for raw, message in cases:
            with self.subTest(message=message):
                self.assertEqual(run(raw), (1, "", "Agency-card build stopped: " + message + "\n"))
        with self.assertRaises(ValueError) as caught:
            json.loads(b"{")
        self.assertEqual(run(b"{"), (1, "", "Agency-card build stopped: " + str(caught.exception) + "\n"))
        for exc in (ValueError("value seam"), TypeError("type seam"), OSError("I/O seam")):
            with self.subTest(exc=exc), mock.patch.object(cards, "render", side_effect=exc):
                self.assertEqual(run(b"{}"), (1, "", "Agency-card build stopped: " + str(exc) + "\n"))

    def test_existing_byte_encoding_duplicate_and_nonfinite_semantics(self):
        text = json.dumps(CONFIG)
        expected = (0, json.dumps({"javascript": cards.render(CONFIG)}), "")
        for raw in (text.encode("utf-16"), text.encode("utf-32"), b"\xef\xbb\xbf" + text.encode(),
                    b'{"schema_version":99,' + text.encode()[1:]):
            with self.subTest(prefix=raw[:20]):
                self.assertEqual(run(raw), expected)
        for token in (b"NaN", b"Infinity", b"-Infinity"):
            raw = text.encode().replace(b'"schema_version": 1', b'"schema_version": ' + token)
            self.assertEqual(run(raw), (1, "", "Agency-card build stopped: schema_version: unsupported schema version\n"))

    def test_check_and_usage_modes_are_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic.json"
            for raw, expected in (
                    (deep_inputs()[0], (2, "", CHECK_ERROR)),
                    (json.dumps(CONFIG).encode(), (0, "problems=0\n", "")),
                    (json.dumps({**CONFIG, "extra": "synthetic"}).encode(),
                     (1, "unknown field: extra\nproblems=1\n", ""))):
                path.write_bytes(raw)
                before = path.stat().st_mtime_ns
                self.assertEqual(run(argv=["--check", str(path)], source=NoInput()), expected)
                self.assertEqual(path.read_bytes(), raw)
                self.assertEqual(path.stat().st_mtime_ns, before)
                self.assertEqual(list(Path(tmp).iterdir()), [path])
            for argv in (["--check"], ["--check", str(path), "extra"], ["--bogus"], [str(path)]):
                self.assertEqual(run(argv=argv, source=NoInput()),
                                 (2, "", "usage: agency_cards [--check CONFIG.json]  (no arguments: build from stdin)\n"))

    def test_successful_javascript_and_framing_are_unchanged(self):
        value = copy.deepcopy(CONFIG)
        value["county_profile"]["name"] = 'County "quoted" {{SOURCE_ROUTE}} café 🐦\n\t'
        value["county_profile"]["summary"] = ""
        value["county_profile"]["facts"] = [""]
        before = copy.deepcopy(value)
        javascript = cards.render(value)
        expected = (0, json.dumps({"javascript": javascript}), "")
        self.assertEqual(run(json.dumps(value, ensure_ascii=False).encode()), expected)
        self.assertEqual(value, before)
        self.assertIn('{{SOURCE_ROUTE}} café 🐦', javascript)
        self.assertFalse(expected[1].endswith("\n"))
        for entry in (["-m", "campaign_tool.agency_cards"], ["campaign_tool/agency_cards.py"]):
            done = subprocess.run([sys.executable, "-B", *entry], cwd=ROOT,
                                  input=json.dumps(value).encode(), capture_output=True, timeout=30)
            self.assertEqual((done.returncode, done.stdout.decode(), done.stderr.decode()), expected)


if __name__ == "__main__":
    unittest.main()
