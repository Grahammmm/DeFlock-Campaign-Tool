"""DF0237-15: examples/pages/help-map.json, the draft resident camera-mapping page that a fresh `content init` seeds.

Everything runs on temporary synthetic campaigns; sockets are blocked and nothing is fetched or sent.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import content_init
from campaign_tool.content import load_pages
from campaign_tool.content_check import content_problems
from campaign_tool.legal_claims import legal_claims, legal_notes
from campaign_tool.markdown_lite import lint
from campaign_tool.site import build_site

REPO = Path(__file__).resolve().parents[1]
STARTER = REPO / "examples" / "pages" / "help-map.json"
MIRROR = REPO / "campaign_tool" / "_resources" / "examples" / "pages" / "help-map.json"
MARKER = "[EDIT ME: verify for this campaign] "
HEADINGS = ["Recognize a possible plate reader", "Stay safe and protect privacy", "Add a missing camera",
            "Correct an existing point", "Understand ownership unconfirmed", "Understand campaign point checks"]
NO_SOCKETS = mock.patch("socket.socket", side_effect=AssertionError("network access attempted"))


def starter():
    return json.loads(STARTER.read_text(encoding="utf-8"))


def sections(document):
    parts = re.split(r"^## (.+)$", document["body_md"], flags=re.M)
    return list(zip(parts[1::2], (part.strip() for part in parts[2::2])))


def cli(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, args)], cwd=REPO,
                          capture_output=True, text=True, timeout=120)


class HelpMapCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-help-map-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        NO_SOCKETS.start()
        self.addCleanup(NO_SOCKETS.stop)

    def campaign(self, name="campaign", state="CA"):
        root = self.tmp / name
        result = cli("init", "--directory", root, "--name", "Synthetic Cedar Watch", "--county", "Cedar", "--state", state)
        self.assertEqual(result.returncode, 0, result.stderr)
        return root


class StarterTests(HelpMapCase):
    def test_six_sections_tags_sources_and_safe_wording(self):
        document = starter()
        self.assertEqual(list(document), ["title", "description", "body_md", "nav"])
        self.assertEqual(document["title"], "Help map the cameras")
        self.assertEqual(document["description"], "How to contribute careful camera observations to the community map.")
        self.assertIs(document["nav"], True)
        found = sections(document)
        self.assertEqual([name for name, _ in found], HEADINGS)
        for name, prose in found:
            self.assertTrue(prose.startswith(MARKER), name)
            self.assertEqual(prose.count("[EDIT ME: "), 1, name)
            sentence_count = len(re.findall(r"[.!?](?:\s|$)", prose[len(MARKER):]))
            self.assertTrue(2 <= sentence_count <= 4, (name, sentence_count))
        self.assertLessEqual(len(document["body_md"].split()), 1800)
        body = document["body_md"]
        self.assertIn("Stay on public property, do not touch equipment, and never photograph people or license plates.", body)
        self.assertIn("`man_made=surveillance`", body)
        self.assertIn("`surveillance:type=ALPR`", body)
        self.assertIn("https://wiki.openstreetmap.org/wiki/Tag:surveillance:type=ALPR", body)
        self.assertIn("https://wiki.openstreetmap.org/wiki/Tag:man_made=surveillance", body)
        self.assertIn("https://deflock.me/report", body)
        self.assertIn("Community-mapped points; ownership unconfirmed.", body)
        for code in ("seen_in_person", "in_agency_record", "not_found_on_site", "duplicate"):
            self.assertIn(f"`{code}`", body)
        self.assertIn("`not_found_on_site` records that an organizer did not find a camera at the surveyed location", body)
        self.assertIn("`duplicate` flags that the point appears to be a duplicate of another mapped point", body)
        self.assertIn("neither check removes the point: both keep it on the map and in every count.", body)
        self.assertEqual(re.findall(r"\]\((?!https://)[^)]*\)", body), [])  # markdown_lite drops relative links
        self.assertIn("methods.html", body)  # named in words, not linked
        self.assertNotIn("cameras.html", body)
        self.assertNotRegex(body, r"(?i)\b(?:every|all) roadside cameras?\b|trespass|conceal|interfer|<img|<form")

    def test_starter_uses_real_validators_and_guard(self):
        document = starter()
        self.assertEqual(lint(document["body_md"]), [])
        root = self.tmp / "content-tree"
        (root / "pages").mkdir(parents=True)
        (root / "pages" / "help-map.json").write_text(json.dumps(document), encoding="utf-8")
        pages = load_pages(root, set())
        self.assertEqual([(page["slug"], page["nav"]) for page in pages], [("help-map", True)])
        self.assertEqual(legal_notes([(f"pages/help-map.json.{key}", document[key]) for key in ("title", "description", "body_md")]), [])
        # positive control: an unquoted accusation in the same field is flagged
        self.assertIn("violation-verb", legal_claims(document["body_md"] + "\n\nThe synthetic agency violated the law."))


class FreshInitTests(HelpMapCase):
    def test_fresh_init_build_check_links_and_markers(self):
        root = self.campaign()
        report = content_init.init_content(root)
        self.assertEqual(report["written"], sorted(report["written"]))
        self.assertIn("content/pages/", report["written"])
        self.assertIn("content/pages/help-map.json", report["written"])
        content = root / "content"
        self.assertEqual((content / "pages" / "help-map.json").read_bytes().decode("utf-8"),
                         json.dumps(starter(), indent=2, ensure_ascii=False) + "\n")
        texts = [(content / name).read_text(encoding="utf-8") for name in ("site.json", "faq.json", "pages/help-map.json", "pages/background.json")]
        self.assertEqual(report["placeholders"], sum(text.count("[EDIT ME: ") for text in texts))
        self.assertEqual(texts[2].count("[EDIT ME: "), 6)
        self.assertEqual(content_problems(root), [])
        build_site(root, today=date(2026, 10, 1))
        public = root / "public"
        page = (public / "help-map.html").read_text(encoding="utf-8")
        self.assertEqual(page.count("[EDIT ME: verify for this campaign]"), 6)  # markers stay visible
        self.assertFalse((public / "cameras.html").exists())
        self.assertNotIn('href="/cameras.html"', page)
        self.assertTrue((public / "methods.html").is_file())
        self.assertIn('href="/help-map.html"', (public / "index.html").read_text(encoding="utf-8"))  # nav slot
        build = cli("build", "--directory", root, "--check")
        self.assertEqual(build.returncode, 0, build.stderr)

    def test_installed_init_without_checkout(self):
        self.assertEqual(MIRROR.read_bytes(), STARTER.read_bytes())
        self.assertEqual(content_init.HELP_MAP_STARTER, MIRROR)  # the runtime reads the packaged copy
        real_read_text = Path.read_text

        def no_checkout_examples(path, *args, **kwargs):
            if (REPO / "examples") in Path(path).parents:
                raise AssertionError("runtime read the checkout examples directory")
            return real_read_text(path, *args, **kwargs)

        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        previous = Path.cwd()
        os.chdir(elsewhere)
        try:
            with mock.patch.object(Path, "read_text", no_checkout_examples):
                document = content_init.help_map_document()
        finally:
            os.chdir(previous)
        self.assertEqual(document, starter())
        document["title"] = "changed"  # a caller's edits never reach the next read
        document["nav"] = False
        self.assertEqual(content_init.help_map_document(), starter())

    def test_no_replace_atomic_cleanup_and_modes(self):
        def tree(root):
            return sorted((str(path.relative_to(root)), path.is_symlink(), path.read_bytes() if path.is_file() and not path.is_symlink() else None)
                          for path in root.rglob("*"))

        for label, make in (("directory", lambda path: path.mkdir()),
                            ("file", lambda path: path.write_text("kept\n", encoding="utf-8")),
                            ("symlink", lambda path: path.symlink_to(self.tmp / "nowhere"))):
            with self.subTest(label):
                root = self.campaign("existing-" + label)
                make(root / "content")
                before = tree(root)
                with self.assertRaisesRegex(ValueError, "content/ already exists; nothing was written"):
                    content_init.init_content(root)
                self.assertEqual(tree(root), before)
                self.assertEqual(list(root.glob(".content.new-*")), [])
        root = self.campaign("failures")
        before = tree(root)
        real_write = content_init._write

        def failing(path, text):
            if path.name == "help-map.json":
                raise OSError("injected page write failure")
            return real_write(path, text)

        for label, patches in (("write", mock.patch.object(content_init, "_write", side_effect=failing)),
                               ("load", mock.patch.object(content_init, "load_pages", return_value=[])),
                               ("starter", mock.patch.object(content_init, "HELP_MAP_STARTER", self.tmp / "missing.json"))):
            with self.subTest(label), patches:
                with self.assertRaises((OSError, RuntimeError)):
                    content_init.init_content(root)
            self.assertFalse(os.path.lexists(root / "content"))
            self.assertEqual(list(root.glob(".content.new-*")), [])
            self.assertEqual(tree(root), before)
        with mock.patch.object(content_init, "_renameat2", return_value=None):  # fallback publication
            content_init.init_content(root)
        content = root / "content"
        for path in [content, *content.rglob("*")]:
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600, path)

    def test_from_preserves_source_page_and_does_not_inject(self):
        old, new = self.campaign("old"), self.campaign("new", "OR")
        (old / "content" / "pages").mkdir(parents=True)
        (old / "content" / "site.json").write_text(json.dumps(
            {"schema_version": 1, "tagline": "A tagline", "mission_md": "A mission", "about_md": "About"}), encoding="utf-8")
        report = content_init.init_content(new, source_root=old)
        self.assertFalse((new / "content" / "pages" / "help-map.json").exists())  # a missing source page stays missing
        self.assertNotIn("content/pages/help-map.json", report["copied"])
        (old / "content" / "pages" / "help-map.json").write_text(json.dumps(
            {"title": "Our own page", "body_md": "Organizer text."}), encoding="utf-8")
        newer = self.campaign("newer", "OR")
        content_init.init_content(newer, source_root=old)
        copied = json.loads((newer / "content" / "pages" / "help-map.json").read_text(encoding="utf-8"))
        self.assertEqual(copied["title"], "Our own page")  # the source page is copied, never replaced by the starter


if __name__ == "__main__":
    unittest.main()
