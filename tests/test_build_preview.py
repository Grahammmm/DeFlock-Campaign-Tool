"""DF2022-07: `build --preview` names what a rebuild would add, remove or change in public/ and writes nothing.

Synthetic content only (a temporary copy of the fictional example); no network.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool.site import build_site, preview_site
from tests.test_site_build import EXAMPLE, REPO_ROOT

FINDING = "content/findings/cedar-policy-posted.json"


def snapshot(folder):
    """Every entry under folder with its mtime and bytes (symlinks by target name, never followed)."""
    result = {}
    for base, dirs, names in os.walk(folder):
        for name in dirs + names:
            path = Path(base) / name
            data = os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None
            result[path.relative_to(folder).as_posix()] = (path.lstat().st_mtime_ns, data)
    return result


class BuildPreviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-build-preview-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"

    def preview(self, *extra):
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "build", "--directory", str(self.root),
                                 "--preview", *extra], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
        report = {}
        if result.returncode in (0, 3):
            body, _, last = result.stdout.rstrip("\n").rpartition("\n")
            self.assertEqual(last, "Preview only; public/ was not changed.")
            report = json.loads(body)
        return result, report

    def edit(self, relative, change):
        path = self.root / relative
        data = json.loads(path.read_text(encoding="utf-8"))
        change(data)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def built_sizes(self):
        return {p.relative_to(self.public).as_posix(): p.stat().st_size for p in self.public.rglob("*") if p.is_file()}

    def test_fresh_copy_lists_every_planned_file_as_added_and_creates_nothing(self):
        before = snapshot(self.root)
        result, report = self.preview()
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(snapshot(self.root), before)  # no public/, no .public.new-*/.public.old-*
        self.assertFalse(report["public_exists"])
        self.assertEqual((report["removed"], report["changed"], report["unchanged"], report["unreadable"]),
                         ([], [], 0, []))
        build_site(self.root)
        self.assertEqual({e["path"]: e["bytes"] for e in report["added"]}, self.built_sizes())
        self.assertEqual([e["path"] for e in report["added"]], sorted(e["path"] for e in report["added"]))

    def test_no_change_after_build_is_empty_and_exits_zero(self):
        build_site(self.root)
        result, report = self.preview()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(report["public_exists"])
        self.assertEqual((report["added"], report["removed"], report["changed"], report["unreadable"]), ([], [], [], []))
        self.assertEqual(report["unchanged"], len(self.built_sizes()))

    def test_edited_summary_lists_changed_pages_with_deltas_and_public_untouched(self):
        build_site(self.root)
        before_sizes = self.built_sizes()
        before = snapshot(self.public)
        self.edit(FINDING, lambda d: d.update(summary=d["summary"] + " A synthetic follow-up sentence was added."))
        result, report = self.preview()
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(snapshot(self.public), before)  # bytes and mtimes identical
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.name.startswith(".public.")), [])
        changed = {e["path"]: e for e in report["changed"]}
        for page in ("findings/cedar-policy-posted.html", "findings/index.html", "index.html", "feed.xml"):
            self.assertIn(page, changed)
        self.assertEqual((report["added"], report["removed"]), ([], []))
        build_site(self.root)
        after_sizes = self.built_sizes()
        differing = {p for p in after_sizes if (self.public / p).read_bytes() != before[p][1]}
        self.assertEqual(set(changed), differing)
        for path, entry in changed.items():
            self.assertEqual((entry["bytes_before"], entry["bytes_after"], entry["delta"]),
                             (before_sizes[path], after_sizes[path], after_sizes[path] - before_sizes[path]))
        self.assertEqual(report["unchanged"], len(after_sizes) - len(changed))

    def test_same_size_edit_is_changed_by_hash(self):
        build_site(self.root)
        self.edit(FINDING, lambda d: d.update(summary=d["summary"].replace("two-page", "two-pagf")))
        result, report = self.preview()
        self.assertEqual(result.returncode, 3, result.stderr)
        entry = next(e for e in report["changed"] if e["path"] == "findings/cedar-policy-posted.html")
        self.assertEqual(entry["delta"], 0)

    def test_removed_finding_and_added_asset(self):
        second = self.root / "content/findings/cedar-second.json"
        shutil.copyfile(self.root / FINDING, second)
        self.edit("content/findings/cedar-second.json",
                  lambda d: d.update(id="fnd_0000000000000002", slug="cedar-second", title="A second synthetic finding"))
        build_site(self.root)
        self.assertTrue((self.public / "findings/cedar-second.html").is_file())
        second.unlink()
        asset = self.root / "content/public-assets/flyer.pdf"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b"%PDF-1.4 synthetic flyer\n")
        result, report = self.preview()
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("findings/cedar-second.html", [e["path"] for e in report["removed"]])
        self.assertIn({"path": "flyer.pdf", "bytes": asset.stat().st_size}, report["added"])
        self.assertFalse((self.public / "flyer.pdf").exists())

    def test_stray_file_in_public_is_removed(self):
        build_site(self.root)
        (self.public / "notes.txt").write_text("not produced by a build\n")
        result, report = self.preview()
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(report["removed"], [{"path": "notes.txt", "bytes": 24}])
        self.assertTrue((self.public / "notes.txt").is_file())

    def test_symlink_inside_public_is_unreadable_and_not_followed(self):
        build_site(self.root)
        outside = Path(self.root.parent) / "outside.txt"
        outside.write_text("private target\n")
        (self.public / "linked.txt").symlink_to(outside)
        (self.public / "linked-dir").symlink_to(self.root / "content", target_is_directory=True)
        (self.public / "index.html").unlink()
        (self.public / "index.html").symlink_to(outside)
        real, reads = Path.read_bytes, []

        def spy(path):
            reads.append(Path(path))
            return real(path)
        with mock.patch.object(Path, "read_bytes", spy):
            report = preview_site(self.root)
        self.assertEqual(report["unreadable"], ["index.html", "linked-dir", "linked.txt"])
        self.assertNotIn(outside, reads)
        self.assertFalse(any("linked" in p.as_posix() for p in reads))
        self.assertEqual([e["path"] for e in report["added"]], ["index.html"])  # the build would replace the link
        self.assertEqual(report["removed"], [])

    def test_symlinked_public_is_refused(self):
        target = self.root.parent / "elsewhere"
        target.mkdir()
        self.public.symlink_to(target, target_is_directory=True)
        result, report = self.preview()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Stopped: public/ must not be a symlink", result.stderr)
        self.assertEqual(list(target.iterdir()), [])

    def test_no_content_is_refused(self):
        shutil.rmtree(self.root / "content")
        before = snapshot(self.root)
        result, _ = self.preview()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Stopped: build --preview needs content/; the starter has nothing to compare", result.stderr)
        self.assertEqual(snapshot(self.root), before)

    def test_invalid_content_is_refused_and_writes_nothing(self):
        build_site(self.root)
        (self.root / FINDING).write_text("{ not json", encoding="utf-8")
        before = snapshot(self.root)
        result, _ = self.preview()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Stopped:", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(snapshot(self.root), before)

    def test_preview_with_check_is_an_argument_error(self):
        result, _ = self.preview("--check")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not allowed with argument", result.stderr)
        self.assertFalse(self.public.exists())


if __name__ == "__main__":
    unittest.main()
