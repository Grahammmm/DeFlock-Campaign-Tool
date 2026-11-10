"""map_controller refuses decoded text that cannot become UTF-8 output, naming the field (synthetic fixtures only)."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import map_controller
from campaign_tool.content import load_map
from campaign_tool.content_check import content_problems
from campaign_tool.map_controller import MAX_BYTES, problems, render

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
CONFIG = json.loads((EXAMPLE / "map-config.json").read_text())
SUFFIX_ROW = "candidate_count_suffix: not valid UTF-8 text"
HOOK_ROWS = ["point_context_renderer: invalid trusted renderer", "profile_renderer: invalid trusted renderer"]
SURROGATES = ("\ud800", "\udbff", "\udc00", "\udfff")
MARKER = "zqmarker"


def with_suffix(suffix):
    return {**CONFIG, "candidate_count_suffix": suffix}


def run_cli(args, stdin=b""):
    result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.map_controller", *args], cwd=REPO_ROOT,
                            input=stdin, capture_output=True, timeout=120)
    return result.returncode, result.stdout.decode(), result.stderr.decode()


class SurrogateSuffixTests(unittest.TestCase):
    def test_surrogate_suffix_refuses_all_public_entry_points(self):
        for bad in SURROGATES:
            for suffix in (MARKER + bad, bad + MARKER, MARKER + bad + MARKER):
                with self.subTest(suffix=ascii(suffix)):
                    config = with_suffix(suffix)
                    self.assertEqual(problems(config), [SUFFIX_ROW])
                    with self.assertRaises(ValueError) as caught:
                        render(config, "")
                    self.assertEqual(str(caught.exception), SUFFIX_ROW)
                    payload = json.dumps({"config": config, "profile_renderer": ""}).encode("ascii")  # ASCII \udXXX escapes
                    self.assertEqual(run_cli([], payload), (1, "", "Map build stopped: " + SUFFIX_ROW + "\n"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "map.json"
            path.write_bytes(json.dumps(with_suffix(MARKER + "\ud800")).encode("ascii"))
            self.assertEqual(run_cli(["--check", str(path)]),
                             (1, SUFFIX_ROW + "\nrenderers: not checked\nproblems=1\n", ""))

    def test_suffix_type_length_and_missing_diagnostics_are_unchanged(self):
        length = "candidate_count_suffix: must be 1-200 characters"
        for value in (None, True, 5, ["x"], {"x": 1}, "", "x" * 201, "\ud800" * 201):
            with self.subTest(value=repr(value)[:20]):
                self.assertEqual(problems(with_suffix(value)), [length])
        for value in ("x", "x" * 200, "é" * 200, "\U0001f6b2" * 200):
            with self.subTest(length=len(value)):
                self.assertEqual(problems(with_suffix(value)), [])
        self.assertEqual(problems({k: v for k, v in CONFIG.items() if k != "candidate_count_suffix"}),
                         ["missing field: candidate_count_suffix"])


class HookTests(unittest.TestCase):
    def test_both_bad_hooks_preserve_complete_sorted_diagnostics(self):
        config = {**{k: v for k, v in CONFIG.items() if k != "initial_point_count"}, "group_slug": "Bad_Name"}
        expected = sorted(["group_slug: invalid slug", "missing field: initial_point_count", *HOOK_ROWS])
        self.assertEqual(problems(config, "a\udc00", "b\ud800"), expected)
        with self.assertRaises(ValueError) as caught:
            render(config, "a\udc00", "b\ud800")
        self.assertEqual(str(caught.exception), "; ".join(expected))
        self.assertEqual(problems(CONFIG, "a\ud800"), ["profile_renderer: invalid trusted renderer"])
        self.assertEqual(problems(CONFIG, "", "a\ud800"), ["point_context_renderer: invalid trusted renderer"])
        self.assertEqual(problems(with_suffix("x\ud800"), "\ud800", "\ud800"), sorted([SUFFIX_ROW, *HOOK_ROWS]))

    def test_renderer_limit_is_strict_encoded_bytes(self):
        with mock.patch.object(map_controller, "MAX_BYTES", 8):
            for slot in (0, 1):
                def call(value):
                    return problems(CONFIG, *(("", value) if slot else (value, "")))
                name = HOOK_ROWS[1 - slot]
                for ok in ("", "x" * 8, "é" * 4, "\U0001f6b2" * 2, "ab\u20acab"):  # 8 bytes at most
                    with self.subTest(slot=slot, ok=ascii(ok)):
                        self.assertEqual(call(ok), [])
                for bad in ("x" * 9, "é" * 5, "\U0001f6b2" * 2 + "x", "\u20ac" * 3, "\ud800", None, 5, b"x"):
                    with self.subTest(slot=slot, bad=ascii(bad)):
                        self.assertEqual(call(bad), [name])
        self.assertEqual(problems(CONFIG, "x" * MAX_BYTES, ""), [])
        self.assertEqual(problems(CONFIG, "x" * (MAX_BYTES + 1), ""), [HOOK_ROWS[1]])

    def test_invalid_text_refuses_before_template_access(self):
        guard = mock.Mock()
        guard.read_text.side_effect = AssertionError("template read")
        with mock.patch.object(map_controller, "ASSET", guard):
            for args in ((with_suffix("\ud800"), ""), (CONFIG, "\ud800"), (CONFIG, "", "\udfff")):
                with self.subTest(args=ascii(args[1:])), self.assertRaises(ValueError):
                    render(*args)
            guard.read_text.assert_not_called()


class CompatibilityTests(unittest.TestCase):
    def test_valid_unicode_and_json_surrogate_pair_remain_accepted(self):
        pair = json.loads('"\\ud83d\\udeb2"')
        self.assertEqual(pair, "\U0001f6b2")
        suffixes = ("caf\u00e9 ✓", "e\u0301 \u4e2d\u6587", "\U0001f6b2 bike", pair)
        for suffix in suffixes:
            with self.subTest(suffix=ascii(suffix)):
                code = render(with_suffix(suffix), "", "function(p){return 'é';}")
                self.assertEqual(code.encode("utf-8").decode("utf-8"), code)
                self.assertIn(json.dumps(suffix, ensure_ascii=False), code)  # byte-for-byte, not normalized
        code = render(CONFIG, "// é\U0001f6b2\n", "function(p){return 'é';}")
        self.assertIn("é\U0001f6b2", code)
        self.assertNotEqual(render(with_suffix("e\u0301"), ""), render(with_suffix("\u00e9"), ""))
        # explicit adjacent surrogate code units are refused, never combined
        self.assertEqual(problems(with_suffix("\ud83d\udeb2")), [SUFFIX_ROW])

    def test_legacy_render_bytes_hooks_and_relabel_unchanged(self):
        hook = "function(p){return mapText(p.context||'{{GROUP_SLUG}}');}"
        before = json.dumps(CONFIG, sort_keys=True)
        self.assertEqual(render(CONFIG, ""), render(CONFIG, "", ""))
        self.assertIn("+(" + hook + ")(p)", render(CONFIG, "", hook))
        self.assertIn("{{GROUP_SLUG}}", render(CONFIG, "// {{GROUP_SLUG}}\n"))
        self.assertIn('LABELS["js.popup_title"]', render(CONFIG, "", relabel=True))
        self.assertEqual(run_cli([], json.dumps({"config": CONFIG, "profile_renderer": "", "point_context_renderer": hook}).encode()),
                         (0, json.dumps({"javascript": render(CONFIG, "", hook)}), ""))
        self.assertEqual(json.dumps(CONFIG, sort_keys=True), before)


class DiagnosticsAndCallerTests(unittest.TestCase):
    def test_all_diagnostics_are_value_free_and_modes_read_only(self):
        bad = MARKER + "\ud800" + MARKER
        payload = json.dumps({"config": with_suffix(bad), "profile_renderer": bad, "point_context_renderer": bad}).encode("ascii")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "map.json"
            path.write_bytes(json.dumps(with_suffix(bad)).encode("ascii"))
            before = sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in Path(tmp).iterdir())
            outputs = [run_cli([], payload), run_cli(["--check", str(path)], b"stdin is never read here")]
            self.assertEqual(sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in Path(tmp).iterdir()), before)
        self.assertEqual([o[0] for o in outputs], [1, 1])
        self.assertEqual(outputs[0][1], "")
        for _, out, err in outputs:
            for text in (out, err):
                for leak in (MARKER, "ud800", "\\ud800", "codec", "Traceback", "surrogate", "position", str(tmp)):
                    self.assertNotIn(leak, text)
        self.assertEqual(outputs[1][2], "")
        self.assertEqual(outputs[0][2], "Map build stopped: " + "; ".join(sorted([SUFFIX_ROW, *HOOK_ROWS])) + "\n")

    def test_real_content_map_and_content_check_report_suffix_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            shutil.copytree(EXAMPLE, root)
            config_path = root / "content" / "map" / "map-config.json"
            config = json.loads(config_path.read_text())
            config["candidate_count_suffix"] = MARKER + "\ud800"
            config_path.write_bytes(json.dumps(config).encode("ascii"))  # ASCII JSON with an escaped lone surrogate
            snapshot = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}
            tree = sorted(root.rglob("*"))
            with self.assertRaises(ValueError) as caught:
                load_map(root / "content")
            self.assertIn(SUFFIX_ROW, str(caught.exception))
            self.assertNotIn(MARKER, str(caught.exception))
            found = content_problems(root)
            self.assertTrue(any(SUFFIX_ROW in line and line.startswith("map/map-config.json") for line in found), found)
            self.assertFalse(any(MARKER in line for line in found), found)
            self.assertEqual(snapshot, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()})
            self.assertEqual(tree, sorted(root.rglob("*")))


if __name__ == "__main__":
    unittest.main()
