"""`content init`: minimal valid content/ tree for the content site (synthetic campaigns only)."""
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import content_init
from campaign_tool.content import load_content

REPO_ROOT = Path(__file__).resolve().parents[1]
REFUSED = "content/ already exists; nothing was written"


def cli(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=120)


def snapshot(root):
    """Recursive listing with modes, link targets and file hashes (links never followed)."""
    entries = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            info = path.lstat()
            if path.is_symlink():
                detail = "link:" + os.readlink(path)
            elif path.is_file():
                detail = hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                detail = "dir"
            entries[str(path.relative_to(root))] = (oct(info.st_mode), detail)
    return entries


class ContentInitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-content-init-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"

    def init_campaign(self, name="Synthetic Cedar Watch", county="Cedar", state="CA"):
        result = cli("init", "--directory", str(self.root), "--name", name, "--county", county, "--state", state)
        self.assertEqual(result.returncode, 0, result.stderr)

    def content_init(self):
        return cli("content", "init", "--directory", str(self.root))

    def assertNothingLeft(self):
        self.assertFalse(os.path.lexists(self.root / "content"))
        leftovers = list(self.root.glob(".content.new-*")) if self.root.exists() else []
        self.assertEqual(leftovers, [])

    def test_fresh_campaign_builds_the_content_site_and_passes_check(self):
        self.init_campaign()
        result = self.content_init()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout[:result.stdout.rindex("}") + 1])
        self.assertEqual(report["written"], ["content/findings/", "content/meetings/", "content/site.json",
                                             "content/sources.json"])
        self.assertIn('Replace every "[EDIT ME: ...]" before you publish; nothing was built or deployed.',
                      result.stdout)
        content = self.root / "content"
        self.assertEqual(sorted(p.name for p in content.iterdir()), ["findings", "meetings", "site.json", "sources.json"])
        self.assertEqual(list((content / "findings").iterdir()), [])
        self.assertEqual(list((content / "meetings").iterdir()), [])
        self.assertEqual((content / "sources.json").read_text(encoding="utf-8"), "[]\n")
        self.assertTrue((content / "site.json").read_text(encoding="utf-8").endswith("}\n"))
        model = load_content(self.root)
        self.assertIsNotNone(model)
        assert model is not None
        self.assertEqual((model["findings"], model["meetings"], model["sources"]), ([], [], []))
        build = cli("build", "--directory", str(self.root), "--check")
        self.assertEqual(build.returncode, 0, build.stderr)
        self.assertIn("Site written to", build.stdout)
        self.assertIn("Public tree check passed", build.stdout)
        self.assertTrue((self.root / "public" / "index.html").is_file())
        findings = (self.root / "public" / "findings" / "index.html").read_text(encoding="utf-8")
        self.assertIn("No findings have been published yet", findings)

    def test_every_placeholder_is_marked_and_counted(self):
        self.init_campaign()
        result = self.content_init()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout[:result.stdout.rindex("}") + 1])
        text = (self.root / "content" / "site.json").read_text(encoding="utf-8")
        site = json.loads(text)
        self.assertEqual(report["placeholders"], text.count("[EDIT ME: "))
        self.assertEqual(report["placeholders"], 4)
        self.assertTrue(site["tagline"].startswith("[EDIT ME: ") and site["tagline"].endswith("]"))
        self.assertTrue(site["mission_md"].startswith("[EDIT ME: ") and site["mission_md"].endswith("]"))
        prose = [line for line in site["about_md"].split("\n") if line and not line.startswith("## ")]
        self.assertEqual(len(prose), 2)
        for line in prose:
            self.assertTrue(line.startswith("[EDIT ME: ") and line.endswith("]"), line)
        self.assertIn("who runs Synthetic Cedar Watch in Cedar, CA]", site["about_md"])
        self.assertEqual((site["contact_email"], site["signup"], site["language"]), (None, {"mode": "none"}, "en"))

    def test_existing_content_in_any_form_is_refused_and_tree_unchanged(self):
        def make_dir(path):
            path.mkdir()
            (path / "site.json").write_text('{"organizer": "kept"}\n', encoding="utf-8")

        cases = {"directory": make_dir,
                 "file": lambda path: path.write_text("not a directory\n", encoding="utf-8"),
                 "dangling symlink": lambda path: path.symlink_to(self.root / "no-such-target"),
                 "empty directory": lambda path: path.mkdir()}
        self.init_campaign()
        for label, make in cases.items():
            with self.subTest(label):
                make(self.root / "content")
                before = snapshot(self.root)
                result = self.content_init()
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("Stopped: " + REFUSED, result.stderr)
                self.assertEqual(snapshot(self.root), before)
                target = self.root / "content"
                if target.is_dir() and not target.is_symlink():
                    for child in target.iterdir():
                        child.unlink()
                    target.rmdir()
                else:
                    target.unlink()

    def test_missing_or_invalid_campaign_json_stops_without_writing(self):
        self.root.mkdir()
        result = self.content_init()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Stopped:", result.stderr)
        self.assertNothingLeft()
        for document in ({"schema_version": 1, "name": "Synthetic", "county": "Cedar", "state": "California"},
                         {"schema_version": 1, "name": "Synthetic", "county": "Cedar"}):
            with self.subTest(document=document):
                (self.root / "campaign.json").write_text(json.dumps(document), encoding="utf-8")
                result = self.content_init()
                self.assertEqual(result.returncode, 1)
                self.assertIn("Stopped:", result.stderr)
                self.assertNothingLeft()
        (self.root / "campaign.json").write_text("{not json", encoding="utf-8")
        result = self.content_init()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Stopped:", result.stderr)
        self.assertNothingLeft()

    def test_hostile_campaign_name_is_written_as_inert_text(self):
        hostile = "<script>alert(1)</script> [x](javascript:alert(1)) **Cedar_Watch**"
        self.init_campaign(name=hostile)
        result = self.content_init()
        self.assertEqual(result.returncode, 0, result.stderr)
        site = json.loads((self.root / "content" / "site.json").read_text(encoding="utf-8"))
        line = next(part for part in site["about_md"].split("\n") if "who runs" in part)
        sentence = line[len("[EDIT ME: "):-1]
        self.assertEqual([char for char in "<>[]*_`" if char in sentence], [], sentence)
        self.assertIn("scriptalert(1)/script", sentence)
        build = cli("build", "--directory", str(self.root), "--check")
        self.assertEqual(build.returncode, 0, build.stderr)
        for page in (self.root / "public").rglob("*.html"):
            text = page.read_text(encoding="utf-8")
            self.assertNotIn("<script>alert", text, page)
            self.assertNotIn('href="javascript', text.lower(), page)

    def test_failure_after_site_json_leaves_no_content_and_no_temp_dir(self):
        self.init_campaign()
        seen = []
        real_write = content_init._write

        def spy(path, text):
            real_write(path, text)
            seen.append(path.name)

        with mock.patch.object(content_init, "_write", side_effect=spy), \
                mock.patch.object(content_init, "_rename_noreplace", side_effect=OSError("injected rename failure")):
            with self.assertRaisesRegex(OSError, "injected rename failure"):
                content_init.init_content(self.root)
        self.assertEqual(seen, ["site.json", "sources.json"])
        self.assertNothingLeft()
        # The refusal is re-checked just before the rename: content/ appearing mid-run is never replaced.
        with mock.patch.object(content_init, "_write", side_effect=lambda path, text: (
                real_write(path, text), path.name == "sources.json" and (self.root / "content").mkdir())):
            with self.assertRaisesRegex(ValueError, REFUSED):
                content_init.init_content(self.root)
        self.assertEqual(list((self.root / "content").iterdir()), [])
        self.assertEqual(list(self.root.glob(".content.new-*")), [])

    def test_content_created_at_the_rename_boundary_is_never_replaced(self):
        # An empty content/ appears after every check, immediately before the publishing rename itself
        # (plain rename(2) would silently replace it); covered with renameat2(RENAME_NOREPLACE) and with
        # the exclusive-mkdir fallback.
        self.init_campaign()
        real_publish = content_init._rename_noreplace
        content = self.root / "content"

        def racing_publish(source, target):
            os.mkdir(target)
            return real_publish(source, target)

        for forced_fallback in (False, True):
            with self.subTest(forced_fallback=forced_fallback):
                fallback = (mock.patch.object(content_init, "_renameat2", return_value=None)
                            if forced_fallback else contextlib.nullcontext())
                with mock.patch.object(content_init, "_rename_noreplace", side_effect=racing_publish), fallback:
                    with self.assertRaisesRegex(ValueError, REFUSED):
                        content_init.init_content(self.root)
                self.assertEqual(list(content.iterdir()), [])
                self.assertEqual(list(self.root.glob(".content.new-*")), [])
                content.rmdir()

    def test_fallback_publication_writes_the_same_private_tree(self):
        self.init_campaign()
        with mock.patch.object(content_init, "_renameat2", return_value=None):
            result = content_init.init_content(self.root)
        content = self.root / "content"
        self.assertEqual(sorted(str(p.relative_to(content)) for p in content.rglob("*")),
                         ["findings", "meetings", "site.json", "sources.json"])
        self.assertEqual(result["written"], ["content/findings/", "content/meetings/", "content/site.json",
                                             "content/sources.json"])
        self.assertEqual(list(self.root.glob(".content.new-*")), [])
        for path in [content, *content.rglob("*")]:
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600, path)

    def test_modes_are_private_under_the_cli_umask(self):
        self.init_campaign()
        result = self.content_init()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = self.root / "content"
        for path in [content, *content.rglob("*")]:
            mode = path.stat().st_mode & 0o777
            self.assertEqual(mode, 0o700 if path.is_dir() else 0o600, path)


if __name__ == "__main__":
    unittest.main()
