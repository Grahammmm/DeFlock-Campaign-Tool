"""RF0452-12: percent-encoded . / .. redirect source segments are refused structurally. Synthetic only."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from campaign_tool.content import ContentError
from campaign_tool.redirects import load_redirects, redirect_rows
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
PUBLISHED = "/findings/cedar-policy-posted.html"
BUILT = ["about.html", "agencies/cedar.html", "findings/cedar-policy-posted.html", "findings/other.html",
         "corrections.html", "index.html"]


def doc(*pairs):
    return {"schema_version": 1, "redirects": [{"from": a, "to": b} for a, b in pairs]}


def message(n, source):
    return (f"redirects.json: redirects[{n}]: from {source!r} must not contain percent-encoded . or .. "
            "path segments")


class EncodedDotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-encdot-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        self.path = self.root / "content" / "redirects.json"

    def write(self, *pairs):
        self.path.write_text(json.dumps(doc(*pairs)), encoding="utf-8")

    def tree(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def test_encoded_dot_segments_refuse_in_every_position(self):
        for source in ("/%2e", "/%2E/", "/%2e/about.html", "/old/%2e%2e/x", "/old/.%2e", "/old/%2E.", "/old/%2e%2E/",
                       "/old/%2E%2e", "/a/%2e/b/%2E%2E/c", "/zz/.%2E/"):
            for call in (lambda d: redirect_rows(d), lambda d: load_redirects(d, BUILT, set())):
                with self.assertRaises(ContentError) as caught:
                    call(doc((source, "/about.html")))
                self.assertEqual(str(caught.exception), message(0, source))

    def test_owned_page_and_withdrawn_aliases_refuse(self):
        for source in ("/%2e/about.html", "/old/%2e%2e/about.html", "/findings/.%2e/about.html"):
            with self.assertRaises(ContentError) as caught:
                load_redirects(doc((source, "/")), BUILT, set())
            self.assertEqual(str(caught.exception), message(0, source))
        for target in (PUBLISHED, "/findings/", "/corrections.html", "/findings/other.html"):
            with self.assertRaises(ContentError) as caught:
                load_redirects(doc(("/findings/%2E/old-cedar.html", target)), BUILT, {"old-cedar"})
            self.assertEqual(str(caught.exception), message(0, "/findings/%2E/old-cedar.html"))
        with self.assertRaises(ContentError) as caught:  # canonical spellings keep their original reasons
            load_redirects(doc(("/about.html", "/")), BUILT, set())
        self.assertIn("is a page this build writes", str(caught.exception))
        with self.assertRaises(ContentError) as caught:
            load_redirects(doc(("/findings/old-cedar.html", PUBLISHED)), BUILT, {"old-cedar"})
        self.assertIn("a withdrawn finding must not point", str(caught.exception))

    def test_non_dot_percent_segments_keep_original_bytes(self):
        pairs = [("/old/a%2eb", "/about.html"), ("/old/%2econfig", "/about.html"), ("/old/...", "/about.html"),
                 ("/old/%2e%2e%2e", "/about.html"), ("/old/%252e", "/about.html"), ("/old/%20report", "/about.html"),
                 ("/old/a%2Eb%2eC", "/about.html"), ("/old/%2x", "/about.html")]
        self.assertEqual(load_redirects(doc(*pairs), BUILT, set()),
                         "".join(f"{a} {b} 301\n" for a, b in sorted(pairs)))

    def test_legacy_precedence_and_mixed_rows_are_attributed(self):
        with self.assertRaises(ContentError) as caught:
            redirect_rows(doc(("/ok", "/"), ("/%2e/x", "/"), ("/a/../%2e", "/"), ("/%2e%2e/%20 ", "/"),
                               ("/b/%2E%2e", "/"), ("/fine%2e", "/"), ("/.%2e/./x", "/")))
        lines = str(caught.exception).splitlines()
        self.assertEqual(lines[0], message(1, "/%2e/x"))
        self.assertIn("must be an ASCII path", lines[1])  # literal .. keeps its old error
        self.assertIn("must be an ASCII path", lines[2])  # invalid space keeps its old error
        self.assertEqual(lines[3], message(4, "/b/%2E%2e"))
        self.assertEqual(lines[4], "redirects.json: redirects[6]: from '/.%2e/./x' must not contain . path segments")
        self.assertEqual(len(lines), 5)
        exact = "/" + "a" * 295 + "/%2e"  # 300 chars: still reaches the new check
        self.assertEqual(len(exact), 300)
        with self.assertRaises(ContentError) as caught:
            redirect_rows(doc((exact, "/")))
        self.assertEqual(str(caught.exception), message(0, exact))

    def test_content_check_and_failed_build_leave_everything_untouched(self):
        build_site(self.root)
        folder = self.root / "private" / "withdrawn"
        folder.mkdir(parents=True)
        (folder / "old-cedar.json").write_text(json.dumps({"id": "f-old-cedar", "slug": "old-cedar"}),
                                               encoding="utf-8")
        self.write(("/%2e/about.html", "/"), ("/findings/%2E/old-cedar.html", PUBLISHED))
        before = self.tree()
        expected = [message(0, "/%2e/about.html"), message(1, "/findings/%2E/old-cedar.html")]
        for extra in ([], ["--json"]):
            result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "content-check", "--directory",
                                     str(self.root), *extra], cwd=REPO_ROOT, capture_output=True, text=True,
                                    timeout=120)
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertEqual(result.stderr, "")
            for line in expected:
                self.assertIn(line.split("redirects.json: ", 1)[1], result.stdout)
        with self.assertRaises(ContentError) as caught:
            build_site(self.root)
        self.assertEqual(str(caught.exception).splitlines(), expected)
        self.assertEqual(self.tree(), before)
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".public")], [])
        self.write(("/old-about.html", "/about.html"))  # no sticky state after repair
        build_site(self.root)
        self.assertEqual((self.public / "_redirects").read_text(encoding="utf-8"), "/old-about.html /about.html 301\n")

    def test_failed_first_build_creates_no_public_tree(self):
        self.write(("/old/%2e%2e/about.html", "/"))
        with self.assertRaises(ContentError):
            build_site(self.root)
        self.assertFalse(self.public.exists())
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".public")], [])


if __name__ == "__main__":
    unittest.main()
