"""RF1420-4: a literal single-dot redirect source segment is refused before owned-page/withdrawn checks. Synthetic only."""
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


def doc(*pairs):
    return {"schema_version": 1, "redirects": [{"from": a, "to": b} for a, b in pairs]}


def message(n, source):
    return f"redirects.json: redirects[{n}]: from {source!r} must not contain . path segments"


class DotSegmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-dotseg-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        self.path = self.root / "content" / "redirects.json"

    def write(self, *pairs):
        self.path.write_text(json.dumps(doc(*pairs)), encoding="utf-8")

    def withdraw(self, slug):
        folder = self.root / "private" / "withdrawn"
        folder.mkdir(parents=True)
        (folder / f"{slug}.json").write_text(json.dumps({"id": "f-" + slug, "slug": slug}), encoding="utf-8")

    def test_literal_dot_segments_are_refused_in_every_position(self):
        for source in ("/.", "/./", "/./about.html", "/old/./name", "/old/.", "/old/./", "/./old/./name"):
            with self.assertRaises(ContentError) as caught:
                redirect_rows(doc((source, "/about.html")))
            self.assertEqual(str(caught.exception), message(0, source))
            with self.assertRaises(ContentError) as caught:
                load_redirects(doc((source, "/about.html")), ["about.html"], set())
            self.assertEqual(str(caught.exception), message(0, source))

    def test_aliases_of_owned_and_withdrawn_paths_refuse_structurally(self):
        built = ["about.html", "agencies/cedar.html", "findings/cedar-policy-posted.html", "index.html"]
        for source in ("/./about.html", "/./agencies/cedar.html", "/findings/./cedar-policy-posted.html"):
            with self.assertRaises(ContentError) as caught:
                load_redirects(doc((source, "/")), built, set())
            self.assertEqual(str(caught.exception), message(0, source))
        built.append("findings/other.html")
        for target in (PUBLISHED, "/findings/", "/corrections.html", "/findings/other.html"):
            with self.assertRaises(ContentError) as caught:
                load_redirects(doc(("/findings/./old-cedar.html", target)), built + ["corrections.html"], {"old-cedar"})
            self.assertEqual(str(caught.exception), message(0, "/findings/./old-cedar.html"))
        with self.assertRaises(ContentError) as caught:  # canonical control keeps its original reason
            load_redirects(doc(("/findings/old-cedar.html", PUBLISHED)), built, {"old-cedar"})
        self.assertIn("a withdrawn finding must not point", str(caught.exception))

    def test_dot_is_a_segment_not_any_period(self):
        built = ["about.html", "findings/cedar-policy-posted.html"]
        pairs = [("/v1.2/report.html", "/about.html"), ("/.well-known/old", "/about.html"),
                 ("/old/.../page", "/about.html"), ("/findings/old.html", PUBLISHED)]
        self.assertEqual(load_redirects(doc(*pairs), built, set()),
                         "".join(f"{a} {b} 301\n" for a, b in sorted(pairs)))

    def test_existing_syntax_errors_keep_precedence_and_mixed_rows_list_every_bad_row(self):
        with self.assertRaises(ContentError) as caught:
            redirect_rows(doc(("/a/./../b", "/"), ("/ok", "/"), ("/x/./y", "/"), ("/caf\u00e9/.", "/"),
                               ("/" + "a" * 299, "/"), ("/." + "a" * 300, "/")))
        lines = str(caught.exception).splitlines()
        self.assertEqual([line.split(": from")[0] for line in lines],
                         [f"redirects.json: redirects[{n}]" for n in (0, 2, 3, 5)])
        self.assertIn("must be an ASCII path", lines[0])
        self.assertEqual(lines[1], message(2, "/x/./y"))
        self.assertIn("must be an ASCII path", lines[2])
        self.assertIn("must be an ASCII path", lines[3])  # >300 chars keeps the old length refusal
        exact = "/." + "a" * 296 + "/."  # 300 chars exactly
        self.assertEqual(len(exact), 300)
        with self.assertRaises(ContentError) as caught:
            redirect_rows(doc((exact, "/")))
        self.assertEqual(str(caught.exception), message(0, exact))

    def test_real_content_check_and_failed_build_leave_everything_untouched(self):
        build_site(self.root)
        self.withdraw("old-cedar")
        self.write(("/./about.html", "/"), ("/findings/./old-cedar.html", PUBLISHED))
        before = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        expected = [message(0, "/./about.html"), message(1, "/findings/./old-cedar.html")]
        for extra in ([], ["--json"]):
            result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "content-check", "--directory",
                                     str(self.root), *extra], cwd=REPO_ROOT, capture_output=True, text=True,
                                    timeout=120)
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertEqual(result.stderr, "")
            for line in expected:
                self.assertTrue(line.split("redirects.json: ", 1)[1] in result.stdout, result.stdout)
        with self.assertRaises(ContentError) as caught:
            build_site(self.root)
        self.assertEqual(str(caught.exception).splitlines(), expected)
        self.assertEqual({p.relative_to(self.root).as_posix(): p.read_bytes()
                          for p in self.root.rglob("*") if p.is_file()}, before)
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".public")], [])
        self.write(("/old-about.html", "/about.html"))  # no sticky state after repair
        build_site(self.root)
        self.assertEqual((self.public / "_redirects").read_text(encoding="utf-8"), "/old-about.html /about.html 301\n")


if __name__ == "__main__":
    unittest.main()
