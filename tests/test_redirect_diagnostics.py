"""RF1025-2: redirect refusals stay one terminal-safe, UTF-8 encodable line per problem. Synthetic content only; no network."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from campaign_tool.content import ContentError
from campaign_tool.content_check import content_problems
from campaign_tool.redirects import load_redirects
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
PUBLISHED = "/findings/cedar-policy-posted.html"
NOT_A_PAGE = "is not a page this build writes"
NAMED = {"LF": "\n", "CR": "\r", "CRLF": "\r\n", "VT": "\x0b", "FF": "\x0c", "FS": "\x1c", "GS": "\x1d",
         "RS": "\x1e", "NEL": "\x85", "LS": "\u2028", "PS": "\u2029"}
UNSAFE = ["\x00", "\t", "\x1b", "\x7f", "\x9b", "\u202a", "\u202e", "\u2066", "\u2069", "\u200f", "\ud800", "\udc00",
          "\udc00\ud800", "\ud800\ud800"]  # JSON joins "\ud800\udc00" into one printable character, kept as is


def unsafe_chars(text):
    return [c for c in text if not c.isprintable() and c != "\n"]


class RedirectDiagnosticTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-redirect-diagnostics-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        self.path = self.root / "content" / "redirects.json"

    def write(self, *pairs):
        self.path.write_text(json.dumps({"schema_version": 1, "redirects": [{"from": a, "to": b} for a, b in pairs]}),
                             encoding="utf-8")

    def message(self, *pairs):
        """The ContentError text real load_redirects raises for ``pairs`` against the build's own page set."""
        self.write(*pairs)
        with self.assertRaises(ContentError) as caught:
            build_site(self.root)
        self.assertFalse(self.public.exists())
        return str(caught.exception)

    def cli(self, *flags):
        env = {**os.environ, "PYTHONIOENCODING": "utf-8:strict", "TMPDIR": str(self.tmp), "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "content-check", *flags,
                               "--directory", str(self.root)], cwd=REPO_ROOT, capture_output=True, env=env, timeout=120)

    def snapshot(self):
        found = {}
        for base in (self.root, *self.root.rglob("*")):
            info = base.stat()
            found[base.relative_to(self.root).as_posix()] = (
                info.st_mode, info.st_mtime_ns, base.read_bytes() if base.is_file() else None)
        return found

    def test_unknown_target_cannot_split_or_forge_a_problem(self):
        for name, char in NAMED.items():
            with self.subTest(name):
                target = f"/missing{char}site.json: FORGED"
                text = self.message(("/a", target))
                escaped = ascii(char)[1:-1]
                self.assertEqual(text, f"redirects.json: redirects[0]: to /missing{escaped}site.json: FORGED {NOT_A_PAGE}")
                self.assertEqual(text.splitlines(), [text])
                self.assertEqual(unsafe_chars(text), [])
                problems = content_problems(self.root, limit=None)
                self.assertEqual(problems, [text])
                self.assertTrue(problems[0].startswith("redirects.json: redirects[0]: "))

    def test_chain_onward_target_uses_the_same_safe_boundary(self):
        target = "/missing\n\x1b[2J\u2028redirects.json: redirects[9]: FORGED\ud800"
        text = self.message(("/first", "/old"), ("/old", target))
        shown = "/missing\\n\\x1b[2J\\u2028redirects.json: redirects[9]: FORGED\\ud800"
        self.assertEqual(text.split("\n"), [
            f"redirects.json: redirects[0] -> redirects[1]: a chain (/first -> /old -> {shown}); "
            "point redirects[0] at the final page",
            f"redirects.json: redirects[1]: to {shown} {NOT_A_PAGE}"])
        self.assertEqual(len(text.splitlines()), 2)
        self.assertEqual(unsafe_chars(text), [])
        text.encode("utf-8")
        self.assertEqual(content_problems(self.root, limit=None), text.split("\n"))

    def test_terminal_controls_bidi_and_lone_surrogates_are_visible(self):
        for char in UNSAFE:
            with self.subTest(ascii(char)):
                text = self.message(("/a", "/x" + char + "y"))
                self.assertEqual(text, "redirects.json: redirects[0]: to /x" + ascii(char)[1:-1] + f"y {NOT_A_PAGE}")
                self.assertEqual(len(text.splitlines()), 1)
                self.assertEqual(unsafe_chars(text), [])
                text.encode("utf-8")
        # an unsafe source is refused by syntax, with the same safety
        text = self.message(("/a\x1b\ud800\n", "/about.html"))
        self.assertEqual(len(text.splitlines()), 1)
        self.assertEqual(unsafe_chars(text), [])
        text.encode("utf-8")
        # a nonstring or externally-shaped target is also safe
        text = self.message(("/a", "x\u202e\ud800"))
        self.assertEqual(text, "redirects.json: redirects[0]: to 'x\\u202e\\ud800' is external; only pages this build "
                               "writes are targets")

    def test_text_cli_malformed_target_stays_exit_three(self):
        for pairs, count in ((( ("/a", "/missing\nsite.json: FORGED\ud800"),), 1),
                             ((("/first", "/old"), ("/old", "/missing\r\x1b[31m\ud800")), 2)):
            self.write(*pairs)
            first = self.cli()
            self.assertEqual(first.returncode, 3, first.stderr)
            self.assertEqual(first.stderr, b"")
            text = first.stdout.decode("utf-8")  # strict
            lines = text.split("\n")
            self.assertEqual(lines[0], f"content/: {count} problem(s)")
            problems = [line for line in lines[1:] if line and not line.startswith("law package ")]
            self.assertEqual(len(problems), count, problems)
            self.assertTrue(all(line.startswith("redirects.json: redirects[") for line in problems), problems)
            self.assertEqual(unsafe_chars(text), [])
            self.assertNotIn("Stopped", text)
            self.assertNotIn("Traceback", text)
            self.assertEqual(self.cli().stdout, first.stdout)

    def test_json_cli_keeps_file_attribution_and_encodes_surrogates(self):
        for pairs, count in (((("/a", "/missing\nsite.json: FORGED\ud800\udc00"),), 1),
                             ((("/first", "/old"), ("/old", "/missing\u2029\ud800")), 2)):
            self.write(*pairs)
            result = self.cli("--json")
            self.assertEqual(result.returncode, 3)
            self.assertEqual(result.stderr, b"")
            raw = result.stdout.decode("utf-8")
            self.assertEqual(len(raw.splitlines()), 1)
            document = json.loads(raw)
            self.assertEqual(document["schema"], "content-check-v1")
            self.assertEqual(document["count"], count)
            self.assertEqual(len(document["problems"]), count)
            self.assertFalse(document["truncated"])
            self.assertEqual({p["file"] for p in document["problems"]}, {"redirects.json"})
            self.assertTrue(all(p["message"].startswith("redirects[") for p in document["problems"]))
            self.assertEqual(unsafe_chars(raw), [])
        self.write(("/a", "/missing\nsite.json: FORGED"))
        message = json.loads(self.cli("--json").stdout)["problems"][0]["message"]
        self.assertEqual(message, f"redirects[0]: to /missing\\nsite.json: FORGED {NOT_A_PAGE}")

    def test_checks_and_failed_builds_preserve_local_state(self):
        self.write(("/a", "/missing\nsite.json: FORGED\ud800"))
        before = self.snapshot()
        self.assertEqual(self.cli().returncode, 3)
        self.assertEqual(self.cli("--json").returncode, 3)
        self.assertEqual(self.snapshot(), before)
        with self.assertRaises(ContentError):
            build_site(self.root)
        self.assertFalse(self.public.exists())
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".public")], [])
        self.assertEqual(self.snapshot(), before)
        # an existing public tree survives a refused build untouched
        self.write(("/old-site/report", PUBLISHED))
        build_site(self.root)
        self.write(("/a", "/missing\nsite.json: FORGED\ud800"))
        before = self.snapshot()
        with self.assertRaises(ContentError):
            build_site(self.root)
        self.assertEqual(self.cli().returncode, 3)
        self.assertEqual(self.snapshot(), before)

    def test_printable_diagnostics_and_valid_redirect_bytes_are_unchanged(self):
        external = "is external; only pages this build writes are targets"
        text = self.message(("/a", "https://x.example/\\n"), ("/b", "/nope\\n.html"), ("/c", "/caf\u00e9 \u201cq\u201d"),
                            ("/d", "/x"), ("/x", PUBLISHED), ("/e", "/e"), ("/f", "/g"), ("/g", "/f"),
                            ("/dup", "/about.html"), ("/dup/", "/about.html"), ("/sources.html", "/about.html"))
        self.assertEqual(text.split("\n"), [  # a literal backslash-n is kept as typed, never escaped twice
            "redirects.json: redirects[9]: from /dup/ repeats redirects[8]",
            "redirects.json: redirects[0]: to 'https://x.example/\\\\n' " + external,
            "redirects.json: redirects[1]: to /nope\\n.html " + NOT_A_PAGE,
            "redirects.json: redirects[2]: to /caf\u00e9 \u201cq\u201d " + NOT_A_PAGE,
            f"redirects.json: redirects[3] -> redirects[4]: a chain (/d -> /x -> {PUBLISHED}); "
            "point redirects[3] at the final page",
            "redirects.json: redirects[5]: from /e points at itself (a loop)",
            "redirects.json: redirects[6] and redirects[7]: a loop between /f and /g; loops are refused",
            "redirects.json: redirects[10]: from /sources.html is a page this build writes"])
        # accepted rows: sorted 301 bytes, empty rows and no file
        self.write(("/old-site/report", PUBLISHED), ("/campaign/", "/"))
        build_site(self.root)
        self.assertEqual((self.public / "_redirects").read_bytes(),
                         b"/campaign/ / 301\n/old-site/report /findings/cedar-policy-posted.html 301\n")
        self.assertEqual(content_problems(self.root), [])
        shutil.rmtree(self.public)
        self.write()
        build_site(self.root)
        self.assertFalse((self.public / "_redirects").exists())
        self.path.unlink()
        shutil.rmtree(self.public)
        build_site(self.root)
        self.assertFalse((self.public / "_redirects").exists())


if __name__ == "__main__":
    unittest.main()
