"""DF2037-16: content/redirects.json -> public/_redirects, 301 only. Synthetic content only; no network."""
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
from campaign_tool.site import build_site
from tests.test_site_build import FILES
from tools.check_public_tree import violations

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
PUBLISHED = "/findings/cedar-policy-posted.html"


class SiteRedirectTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-redirects-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        self.path = self.root / "content" / "redirects.json"

    def write(self, *pairs, **document):
        value = {"schema_version": 1, "redirects": [{"from": a, "to": b} for a, b in pairs], **document}
        self.path.write_text(json.dumps(value), encoding="utf-8")

    def tree(self):
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}

    def withdraw(self, slug):
        folder = self.root / "private" / "withdrawn"
        folder.mkdir(parents=True)
        (folder / f"{slug}.json").write_text(json.dumps({"id": "f-" + slug, "slug": slug}), encoding="utf-8")

    def refused(self, *pairs, **document):
        """Build refuses with every line naming a row, writes nothing; content-check lists exactly the same lines."""
        if pairs or document:
            self.write(*pairs, **document)
        with self.assertRaises(ContentError) as caught:
            build_site(self.root)
        self.assertFalse(self.public.exists())
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".public")], [])
        lines = str(caught.exception).splitlines()
        self.assertTrue(all(line.startswith("redirects.json: ") for line in lines), lines)
        self.assertEqual(content_problems(self.root, limit=None), sorted(lines))
        return lines

    def test_two_rows_written_sorted_301_and_nothing_else_changes(self):
        build_site(self.root)
        before = self.tree()
        shutil.rmtree(self.public)
        self.withdraw("old-cedar")
        self.write(("/old-site/report", PUBLISHED), ("/findings/old-cedar.html", "/findings/"))
        written = {p.relative_to(self.public).as_posix() for p in build_site(self.root)}
        self.assertEqual(written, set(FILES) | {"_redirects"})
        after = self.tree()
        text = after.pop("_redirects")
        self.assertEqual(text, b"/findings/old-cedar.html /findings/ 301\n"
                               b"/old-site/report /findings/cedar-policy-posted.html 301\n")
        self.assertEqual(after, before)
        self.assertEqual(violations("public/_redirects", text), [])
        self.assertEqual(content_problems(self.root), [])

    def test_no_file_or_no_rows_writes_no_redirects_and_identical_tree(self):
        build_site(self.root)
        before = self.tree()
        self.assertEqual(set(before), set(FILES))  # the same file list main's build writes
        shutil.rmtree(self.public)
        self.write()
        build_site(self.root)
        self.assertEqual(self.tree(), before)
        self.assertEqual(content_problems(self.root), [])

    def test_renamed_slug_folder_slash_and_300_character_from_accepted(self):
        long = "/" + "a" * 299
        self.write(("/findings/renamed.html", PUBLISHED), ("/meetings/old.html", "/meetings.html"),
                   ("/campaign/", "/"), (long, "/about.html"))
        build_site(self.root)
        self.assertEqual((self.public / "_redirects").read_text(encoding="utf-8"), (
            f"{long} /about.html 301\n/campaign/ / 301\n/findings/renamed.html {PUBLISHED} 301\n"
            "/meetings/old.html /meetings.html 301\n"))

    def test_external_protocol_relative_and_unknown_targets_refused_by_row(self):
        lines = self.refused(("/a", "https://x.example/"), ("/b", "//x.example/a"), ("/c", "/nope.html"),
                             ("/d", "findings/"), ("/e", "/404.html"), ("/f", "/findings/index.html"))
        external = "is external; only pages this build writes are targets"
        self.assertEqual(lines, [
            f"redirects.json: redirects[0]: to 'https://x.example/' {external}",
            f"redirects.json: redirects[1]: to '//x.example/a' {external}",
            "redirects.json: redirects[2]: to /nope.html is not a page this build writes",
            f"redirects.json: redirects[3]: to 'findings/' {external}",
            "redirects.json: redirects[4]: to /404.html is not a page this build writes",
            "redirects.json: redirects[5]: to /findings/index.html is not a page this build writes"])

    def test_sources_owned_by_the_build_refused(self):
        froms = ["/sources.html", PUBLISHED, "/data/x", "/", "/agencies.html/", "/about", "/findings",
                 "/findings/agency/cedar.xml", "/vendor/old.js", "/_redirects", "/meetings/cedar-council-2026-10.html"]
        lines = self.refused(*[(source, "/about.html") for source in froms])
        self.assertEqual(lines, [f"redirects.json: redirects[{n}]: from {source} is a page this build writes"
                                 for n, source in enumerate(froms)])

    def test_chain_loop_self_loop_and_duplicate_refused_naming_rows(self):
        self.assertEqual(self.refused(("/a", "/b"), ("/b", PUBLISHED)), [
            f"redirects.json: redirects[0] -> redirects[1]: a chain (/a -> /b -> {PUBLISHED}); "
            "point redirects[0] at the final page"])
        self.assertEqual(self.refused(("/a", "/b/"), ("/b", "/a")), [
            "redirects.json: redirects[0] and redirects[1]: a loop between /a and /b; loops are refused"])
        self.assertEqual(self.refused(("/a", "/a")), [
            "redirects.json: redirects[0]: from /a points at itself (a loop)"])
        self.assertEqual(self.refused(("/a", PUBLISHED), ("/a/", "/findings/")), [
            "redirects.json: redirects[1]: from /a/ repeats redirects[0]"])

    def test_bad_from_syntax_refused(self):
        froms = ["/a/../b", "/a?q=1", "/a#x", "/old/*", "/old/:splat", "/old page", "/caf\u00e9", "/" + "a" * 300,
                 "old", "//x.example/a", ""]
        lines = self.refused(*[(source, "/about.html") for source in froms])
        self.assertEqual(len(lines), len(froms))
        for n, (line, source) in enumerate(zip(lines, froms)):
            self.assertTrue(line.startswith(f"redirects.json: redirects[{n}]: from {source!r} must be an ASCII path"),
                            line)

    def test_withdrawn_finding_into_another_finding_refused_index_and_corrections_allowed(self):
        self.withdraw("old-cedar")
        message = ("a withdrawn finding must not point at a different finding (use /findings/ or /corrections.html)")
        self.assertEqual(self.refused(("/findings/old-cedar.html", PUBLISHED)), [
            f"redirects.json: redirects[0]: from /findings/old-cedar.html: {message}"])
        self.assertEqual(self.refused(("/findings/old-cedar.html/", PUBLISHED)), [
            f"redirects.json: redirects[0]: from /findings/old-cedar.html/: {message}"])
        for target in ("/findings/", "/corrections.html"):
            self.write(("/findings/old-cedar.html", target))
            build_site(self.root)
            self.assertEqual((self.public / "_redirects").read_text(encoding="utf-8"),
                             f"/findings/old-cedar.html {target} 301\n")
            shutil.rmtree(self.public)

    def test_content_check_cli_lists_the_build_refusals_exit_3_and_writes_nothing(self):
        self.write(("/a", "https://x.example/"), ("/sources.html", "/b"))
        before = sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*"))
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "content-check", "--directory",
                                 str(self.root)], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 3, result.stderr)
        expected = self.refused()
        self.assertEqual(len(expected), 3)
        self.assertIn("content/: 3 problem(s)", result.stdout)
        for line in expected:
            self.assertIn(line + "\n", result.stdout)
        self.assertEqual(sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*")), before)

    def test_document_shape_refused(self):
        self.assertEqual(self.refused(extra=1), [
            'redirects.json: expected exactly {"schema_version": 1, "redirects": [...]} (unknown keys are refused)'])
        for text in ("null", "[]"):  # a present file parsed to null or a list is refused, never read as absent
            self.path.write_text(text, encoding="utf-8")
            self.assertEqual(self.refused(), [
                'redirects.json: expected exactly {"schema_version": 1, "redirects": [...]} (unknown keys are refused)'])
        self.assertEqual(self.refused(schema_version=2), ["redirects.json: schema_version must be 1"])
        self.assertEqual(self.refused(*[(f"/old/{n}", "/about.html") for n in range(501)]), [
            "redirects.json: redirects must be a list of at most 500 rows"])
        rows = [{"from": "/a", "to": "/about.html", "status": 302}, "/b", {"from": 1, "to": "/"}]
        self.assertEqual(self.refused(redirects=rows), [
            "redirects.json: redirects[0]: keys must be exactly from and to (unknown keys are refused)",
            "redirects.json: redirects[1]: keys must be exactly from and to (unknown keys are refused)",
            "redirects.json: redirects[2]: from and to must be strings"])
        self.write(*[(f"/old/{n}", "/about.html") for n in range(500)])
        build_site(self.root)
        self.assertEqual(len((self.public / "_redirects").read_text(encoding="utf-8").splitlines()), 500)
        shutil.rmtree(self.public)
        target = self.root / "elsewhere.json"
        self.path.rename(target)
        os.symlink(target, self.path)
        self.assertEqual(self.refused(), ["redirects.json: symlinks are not read"])


if __name__ == "__main__":
    unittest.main()
