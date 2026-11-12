"""Malformed source-map URL ports are refused with a field-only diagnostic (synthetic fixtures only)."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import content, map_controller
from campaign_tool.content_check import content_problems

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "fictional-campaign"
CONFIG = json.loads((EXAMPLE / "map-config.json").read_text())
ROW = "source_map_url: invalid port in URL"
STOPPED = "Map build stopped: " + ROW + "\n"
CANARY = "PORTCANARY9"
BAD_PORT_URLS = (
    "https://example.invalid:abc/map",
    "https://example.invalid:-1/map",
    "https://example.invalid:1.5/map",
    "https://example.invalid:443:80/map",
    "https://example.invalid:65536/map",
    "https://example.invalid:99999999999999999999999/map",
)


def with_url(url, **more):
    return {**CONFIG, "source_map_url": url, **more}


def run_check(path):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = map_controller.main(["--check", str(path)])
    return code, out.getvalue(), err.getvalue()


def commands(*args):
    return ([sys.executable, "-B", "-m", "campaign_tool.map_controller", *args],
            [sys.executable, "-B", str(ROOT / "campaign_tool" / "map_controller.py"), *args])


def snapshot(directory):
    result = {}
    for path in sorted(Path(directory).rglob("*")):
        info = path.stat()
        result[str(path.relative_to(directory))] = (
            path.read_bytes() if path.is_file() else None, info.st_mode, info.st_mtime_ns)
    return result


class PortProblemTests(unittest.TestCase):
    def test_impossible_authority_ports_are_field_problems(self):
        for url in BAD_PORT_URLS:
            with self.subTest(url=url):
                self.assertEqual(map_controller.problems(with_url(url)), [ROW])
                with self.assertRaises(ValueError) as caught:
                    map_controller.render(with_url(url), "")
                self.assertEqual(str(caught.exception), ROW)

    def test_bad_port_value_is_never_echoed(self):
        url = f"https://example.invalid:{CANARY}/secretpath"
        found = map_controller.problems(with_url(url))
        self.assertEqual(found, [ROW])
        with self.assertRaises(ValueError) as caught:
            map_controller.render(with_url(url), "")
        self.assertNotIn(CANARY, str(caught.exception))
        self.assertNotIn("secretpath", str(caught.exception))

    def test_valid_port_boundaries_and_written_urls_are_preserved(self):
        for url in ("https://example.invalid/map", "https://example.invalid:/map", "https://example.invalid:0/map",
                    "https://example.invalid:1/map", "https://example.invalid:443/map",
                    "https://example.invalid:65535/map", "https://example.invalid:00443/map#frag"):
            with self.subTest(url=url):
                self.assertEqual(map_controller.problems(with_url(url)), [])
                self.assertIn(url, map_controller.render(with_url(url), ""))
        self.assertIn(CONFIG["source_map_url"], map_controller.render(CONFIG, ""))

    def test_port_looking_path_query_and_fragment_are_not_ports(self):
        for url in ("https://example.invalid/x:65536", "https://example.invalid/map?port=abc:65536",
                    "https://example.invalid:443/x?a=b:c#abc:443:80"):
            with self.subTest(url=url):
                self.assertEqual(map_controller.problems(with_url(url)), [])
                self.assertIn(url, map_controller.render(with_url(url), ""))
        for url in ("https://example.invalid:abc/x:1", "https://example.invalid:65536?ok=1"):
            with self.subTest(url=url):
                self.assertEqual(map_controller.problems(with_url(url)), [ROW])

    def test_port_error_aggregates_without_regressing_other_url_diagnostics(self):
        bad = with_url(BAD_PORT_URLS[0], group_slug="Bad_Slug", initial_point_count=-1)
        want = sorted([ROW, "group_slug: invalid slug", "initial_point_count: out of range",
                       "profile_renderer: invalid trusted renderer"])
        bad_hook = "\ud800"  # a lone surrogate is not a trusted renderer
        self.assertEqual(map_controller.problems(bad, profile_renderer=bad_hook), want)
        with self.assertRaises(ValueError) as caught:
            map_controller.render(bad, bad_hook)
        self.assertEqual(str(caught.exception), "; ".join(want))
        self.assertEqual(map_controller.problems({k: v for k, v in CONFIG.items() if k != "source_map_url"}),
                         ["missing field: source_map_url"])
        for url, row in ((None, "source_map_url: not a public https URL"),
                         ("http://example.invalid:abc/map", "source_map_url: not a public https URL"),
                         ("https://example.invalid:abc/m ap", "source_map_url: not a public https URL"),
                         ("https://user:pw@example.invalid:abc/map", "source_map_url: not a public https URL"),  # pragma: allowlist secret -- synthetic rejected URL
                         ("https://:abc/map", "source_map_url: credentials or no host in URL")):
            with self.subTest(url=url):
                self.assertEqual(map_controller.problems(with_url(url)), [row])


class PortCommandTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.config = self.dir / "map.json"
        self.config.write_text(json.dumps(with_url(f"https://example.invalid:{CANARY}/secretpath")))

    def test_bad_port_refuses_before_template_or_javascript_emission(self):
        tripwire = mock.Mock()
        tripwire.read_text.side_effect = AssertionError("template read")
        with mock.patch.object(map_controller, "ASSET", tripwire):
            with self.assertRaises(ValueError):
                map_controller.render(with_url(BAD_PORT_URLS[4]), "")
        payload = json.dumps({"config": with_url(f"https://example.invalid:{CANARY}/secretpath"),
                              "profile_renderer": ""})
        for cmd in commands():
            proc = subprocess.run(cmd, cwd=ROOT, input=payload, capture_output=True, text=True, timeout=60)
            self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (1, "", STOPPED))
        healthy = json.dumps({"config": CONFIG, "profile_renderer": ""})
        for cmd in commands():
            proc = subprocess.run(cmd, cwd=ROOT, input=healthy, capture_output=True, text=True, timeout=60)
            self.assertEqual((proc.returncode, proc.stderr), (0, ""))
            self.assertEqual(json.loads(proc.stdout), {"javascript": map_controller.render(CONFIG, "")})

    def test_check_port_problem_is_exit_one_read_only_and_stdin_blind(self):
        want = (1, ROW + "\nrenderers: not checked\nproblems=1\n", "")
        before = snapshot(self.dir)
        blind = mock.Mock()
        type(blind).buffer = mock.PropertyMock(side_effect=AssertionError("stdin read"))
        with mock.patch.object(sys, "stdin", blind):
            self.assertEqual(run_check(self.config), want)
        for cmd in commands("--check", str(self.config)):
            proc = subprocess.run(cmd, cwd=ROOT, input="irrelevant", capture_output=True, text=True, timeout=60)
            self.assertEqual((proc.returncode, proc.stdout, proc.stderr), want)
        self.assertEqual(snapshot(self.dir), before)
        self.assertNotIn(CANARY, want[1])
        self.config.write_text("{not json")
        for cmd in commands("--check", str(self.config)):
            proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, "")


class ContentPortTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root)
        self.config = self.root / "content" / "map" / "map-config.json"
        self.good = self.config.read_text()

    def set_url(self, url):
        document = json.loads(self.good)
        document["source_map_url"] = url
        self.config.write_text(json.dumps(document))

    def test_content_loader_and_content_check_inherit_port_refusal(self):
        self.assertEqual(content_problems(self.root), [])
        self.set_url(f"https://example.invalid:{CANARY}/map")
        (self.root / "content" / "findings" / "broken.json").write_text("{not json")
        before = snapshot(self.root)
        with mock.patch.object(content, "_validate_cameras", side_effect=AssertionError("later map asset")):
            with self.assertRaises(ValueError) as caught:
                content.load_map(self.root / "content")
        self.assertEqual(str(caught.exception), ROW)
        found = content_problems(self.root)  # independent healthy content is still checked after the map refusal
        self.assertIn("map/map-config.json: " + ROW, found)
        self.assertTrue(any("broken.json" in line for line in found), found)
        self.assertFalse(any(CANARY in line for line in found), found)
        self.assertEqual(snapshot(self.root), before)
        (self.root / "content" / "findings" / "broken.json").unlink()
        self.assertEqual(content_problems(self.root), ["map/map-config.json: " + ROW])
        self.config.write_text(self.good)  # no sticky state
        self.assertEqual(content_problems(self.root), [])
        self.assertIsNotNone(content.load_map(self.root / "content"))


if __name__ == "__main__":
    unittest.main()
