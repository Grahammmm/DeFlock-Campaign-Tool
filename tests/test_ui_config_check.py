"""--check CONFIG.json for the map-controller and agency-card builders (synthetic configs only)."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import agency_cards, map_controller

ROOT = Path(__file__).resolve().parents[1]
SENTINEL = "SENTINEL-Zq9-value"
MAP = {"schema_version": 1, "group_slug": "test-area", "candidate_property": "testCandidate",
       "bounding_box_fallbacks": ["alpha"], "source_map_url": "https://example.invalid/map",
       "city_boundaries_url": "test-boundaries.geojson", "initial_point_count": 2,
       "candidate_count_suffix": " synthetic points"}
CARDS = {"schema_version": 1,
         "county_profile": {"name": "Test County", "vendor": "Synthetic", "status": "Test", "summary": "None.",
                            "cameraCount": "", "facts": ["Synthetic."], "open": "None.", "flags": ["Test"]},
         "source_page": "sources.html", "default_fragment": "library", "source_aliases": {"a-area": "b-area"},
         "source_link_label": "Evidence notes"}
# module, valid config, bad config, expected sorted problem lines, extra trailer lines
CASES = (
    (map_controller, MAP,
     {**{k: v for k, v in MAP.items() if k != "initial_point_count"}, "group_slug": SENTINEL, "extra": SENTINEL},
     ["group_slug: invalid slug", "missing field: initial_point_count", "unknown field: extra"],
     ["renderers: not checked"]),
    (agency_cards, CARDS,
     {**{k: v for k, v in CARDS.items() if k != "source_page"}, "default_fragment": SENTINEL, "extra": SENTINEL},
     ["default_fragment: invalid slug", "missing field: source_page", "unknown field: extra"], []),
)


class NoStdin:
    @property
    def buffer(self):
        raise AssertionError("--check read stdin")

    def read(self, *args):
        raise AssertionError("--check read stdin")


def run(module, argv, stdin=None):
    out, err = io.StringIO(), io.StringIO()
    source = NoStdin() if stdin is None else io.TextIOWrapper(io.BytesIO(stdin))
    with mock.patch.object(sys, "stdin", source), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = module.main(argv)
    return code, out.getvalue(), err.getvalue()


class ConfigCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def write(self, data, name="config.json"):
        path = self.dir / name
        path.write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode())
        return str(path)

    def test_valid_config_reports_zero_problems(self):
        for module, valid, _bad, _lines, trailer in CASES:
            with self.subTest(module=module.__name__):
                self.assertEqual(run(module, ["--check", self.write(valid)]),
                                 (0, "\n".join(trailer + ["problems=0"]) + "\n", ""))

    def test_every_problem_listed_sorted_without_values(self):
        for module, _valid, bad, lines, trailer in CASES:
            with self.subTest(module=module.__name__):
                code, out, err = run(module, ["--check", self.write(bad)])
                self.assertEqual((code, err), (1, ""))
                self.assertEqual(out.splitlines(), lines + trailer + ["problems=3"])
                self.assertNotIn(SENTINEL, out)
                self.assertNotIn("SENTINEL", out + err)

    def test_odd_unknown_key_shown_by_length_only_via_cli(self):
        odd = "bad key!" * 20
        for module, valid, _bad, _lines, trailer in CASES:
            with self.subTest(module=module.__name__):
                path = self.write({**valid, odd: SENTINEL})
                done = subprocess.run([sys.executable, "-B", "-m", module.__name__, "--check", path], cwd=ROOT,
                                      stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
                self.assertEqual((done.returncode, done.stderr), (1, ""))
                self.assertEqual(done.stdout.splitlines(), ["unknown field: 160 chars"] + trailer + ["problems=1"])
                self.assertNotIn("bad key", done.stdout)
                self.assertNotIn(SENTINEL, done.stdout)

    def assert_stopped(self, result, module):
        code, out, err = result
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(len(err.splitlines()), 1, err)
        self.assertTrue(err.startswith(("Map check stopped:", "Agency-card check stopped:")), err)
        self.assertNotIn("Traceback", err)
        self.assertNotIn("SENTINEL", err)

    def test_unreadable_inputs_exit_2_with_one_line(self):
        for module, valid, _bad, _lines, _trailer in CASES:
            inputs = {
                "missing": str(self.dir / "absent-SENTINEL.json"),
                "directory": str(self.dir),
                "invalid utf-8": self.write(b'{"extra": "SENTINEL\xff"}', "utf8.json"),
                "NaN": self.write(b'{"schema_version": NaN, "extra": "SENTINEL"}', "nan.json"),
                "Infinity": self.write(b'{"initial_point_count": -Infinity}', "inf.json"),
                "not JSON": self.write(b'{"extra": SENTINEL', "broken.json"),
                "top-level list": self.write([valid], "list.json"),
                "empty": self.write(b"", "empty.json"),
            }
            for label, path in inputs.items():
                with self.subTest(module=module.__name__, input=label):
                    self.assert_stopped(run(module, ["--check", path]), module)
            with self.subTest(module=module.__name__, input="over cap"), mock.patch.object(module, "MAX_BYTES", 64):
                self.assert_stopped(run(module, ["--check", self.write(valid, "big.json")]), module)
            with self.subTest(module=module.__name__, input="at cap"):
                path = self.write(valid, "exact.json")
                with mock.patch.object(module, "MAX_BYTES", os.path.getsize(path)):
                    self.assertEqual(run(module, ["--check", path])[0], 0)

    def test_usage_errors_exit_2(self):
        path = self.write(MAP)
        for module, *_rest in CASES:
            for argv in (["--check"], ["--check", path, "extra"], ["--bogus"], [path], ["--check=" + path]):
                with self.subTest(module=module.__name__, argv=argv):
                    code, out, err = run(module, argv)
                    self.assertEqual((code, out), (2, ""))
                    self.assertTrue(err.startswith("usage: ") and len(err.splitlines()) == 1, err)

    def test_check_never_reads_stdin_or_writes_files(self):
        for module, valid, bad, _lines, _trailer in CASES:
            with self.subTest(module=module.__name__):
                paths = [self.write(valid, "ok.json"), self.write(bad, "bad.json")]
                before = sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in self.dir.iterdir())
                for path in paths:
                    run(module, ["--check", path])  # NoStdin raises if stdin is touched
                after = sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in self.dir.iterdir())
                self.assertEqual(before, after)

    def test_no_argument_stdin_mode_unchanged(self):
        map_bad = {"config": {**MAP, "group_slug": "BAD"}, "profile_renderer": ""}
        cards_bad = {**CARDS, "default_fragment": "BAD"}
        cases = (
            (map_controller, {"config": MAP, "profile_renderer": ""},
             (0, json.dumps({"javascript": map_controller.render(MAP, "")}), "")),
            (map_controller, map_bad, (1, "", "Map build stopped: group_slug: invalid slug\n")),
            (map_controller, [], (1, "", "Map build stopped: Invalid map build payload\n")),
            (agency_cards, CARDS, (0, json.dumps({"javascript": agency_cards.render(CARDS)}), "")),
            (agency_cards, cards_bad, (1, "", "Agency-card build stopped: default_fragment: invalid slug\n")),
        )
        for module, payload, expected in cases:
            with self.subTest(module=module.__name__, payload=str(payload)[:40]):
                self.assertEqual(run(module, [], json.dumps(payload).encode()), expected)
                with mock.patch.object(sys, "argv", ["prog"]):
                    self.assertEqual(run(module, None, json.dumps(payload).encode()), expected)


if __name__ == "__main__":
    unittest.main()
