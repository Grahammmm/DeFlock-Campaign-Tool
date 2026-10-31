"""RF1105-27: build_site checks every copy before public/ changes and swaps in a complete folder (synthetic fixture)."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import site as site_module
from campaign_tool.content import ContentError, load_content
from campaign_tool.site import ASSET_MAX_BYTES, VENDOR_MAX_BYTES, build_site
from tests.test_site_build import EXAMPLE


def snapshot(folder):
    return {p.relative_to(folder).as_posix(): (p.stat().st_mode, p.read_bytes() if p.is_file() else None)
            for p in sorted(folder.rglob("*"))}


def sparse(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        handle.truncate(size)


def fail_on_call(target, name, n, calls):
    real = getattr(target, name)

    def flaky(*args, **kwargs):
        calls.append(args)
        if len(calls) == n:
            raise OSError("synthetic failure")
        return real(*args, **kwargs)
    return mock.patch.object(target, name, flaky)


class AtomicSiteBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-site-atomic-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        self.assets = self.root / "content" / "public-assets"

    def leftovers(self):
        return sorted(p.name for p in self.root.iterdir() if p.name.startswith((".public.new-", ".public.old-")))

    def first_build(self):
        build_site(self.root)
        (self.public / "marker.txt").write_text("previous site\n")  # proves public/ was not rebuilt
        return snapshot(self.public)

    def assert_refused(self, before, error: type[Exception] = ContentError, mkdtemp_called=False):
        with mock.patch.object(site_module.tempfile, "mkdtemp", wraps=tempfile.mkdtemp) as mkdtemp:
            with self.assertRaises(error) as caught:
                build_site(self.root)
        self.assertEqual(mkdtemp.called, mkdtemp_called)  # False: refused before any filesystem change
        if before is None:
            self.assertFalse(self.public.exists())
        else:
            self.assertEqual(snapshot(self.public), before)
        self.assertEqual(self.leftovers(), [])
        return str(caught.exception)

    def with_model(self, change):
        def patched(root):
            model = load_content(root)
            change(model)
            return model
        return mock.patch.object(site_module, "load_content", patched)

    def test_happy_path_twice_same_files_bytes_modes_and_no_temp(self):
        written = build_site(self.root)
        first = snapshot(self.public)
        self.assertTrue(all(p.is_file() and p.resolve().is_relative_to(self.public.resolve()) for p in written))
        (self.root / "fresh").mkdir()
        self.assertEqual(self.public.stat().st_mode, (self.root / "fresh").stat().st_mode)
        self.assertEqual(sorted(build_site(self.root)), sorted(written))
        self.assertEqual(snapshot(self.public), first)
        self.assertEqual(self.leftovers(), [])

    def test_oversized_asset_vendor_and_map_files_refused_previous_site_identical(self):
        before, big = self.first_build(), self.root / "big.geojson"
        sparse(self.assets / "big.png", ASSET_MAX_BYTES + 1)
        self.assertIn("5 MiB copy limit", self.assert_refused(before))
        (self.assets / "big.png").unlink()
        sparse(self.root / "content" / "vendor" / "maplibre-gl.js", VENDOR_MAX_BYTES + 1)
        self.assertIn("4 MiB copy limit", self.assert_refused(before))
        (self.root / "content" / "vendor" / "maplibre-gl.js").unlink()
        sparse(big, 8 * 1024 * 1024 + 1)
        with self.with_model(lambda model: model["map"].update(cameras_path=big)):
            self.assertIn("8 MiB copy limit", self.assert_refused(before))
        shutil.copyfile(big, self.root / "content" / "map" / "cameras.geojson")  # the loader refuses first
        self.assertIn("too large", self.assert_refused(before))

    def test_symlink_bad_extension_and_generated_name_collision_refused_before_any_change(self):
        before = self.first_build()
        sparse(self.root / "outside.png", 1)
        self.assets.mkdir()
        (self.assets / "link.png").symlink_to(self.root / "outside.png")
        # The loader skips symlinks; inject one as if it was swapped in after loading.
        with self.with_model(lambda model: model["public_assets"].append(self.assets / "link.png")):
            self.assertIn("symlinks are not copied", self.assert_refused(before))
        sparse(self.assets / "notes.txt", 1)
        self.assertIn("extension not allowed", self.assert_refused(before))
        (self.assets / "notes.txt").unlink()
        sparse(self.assets / "data" / "cameras.png", 1)
        self.assertIn("collides with a generated path", self.assert_refused(before))

    def test_copy_or_swap_failure_leaves_previous_site_and_no_temp(self):
        before, copies, renames = self.first_build(), [], []
        with fail_on_call(site_module.shutil, "copyfile", 2, copies):
            self.assert_refused(before, OSError, mkdtemp_called=True)
        self.assertEqual(len(copies), 2)
        with fail_on_call(os, "rename", 2, renames):
            self.assert_refused(before, OSError, mkdtemp_called=True)
        self.assertEqual([Path(target).name for _, target in renames[1:]], ["public", "public"])  # swap, put back

    def test_first_build_bad_asset_or_copy_failure_creates_no_public(self):
        sparse(self.assets / "notes.txt", 1)
        self.assert_refused(None)
        (self.assets / "notes.txt").unlink()
        with fail_on_call(site_module.shutil, "copyfile", 1, []):
            self.assert_refused(None, OSError, mkdtemp_called=True)

    def test_symlinked_public_still_refused(self):
        sparse(self.root / "elsewhere" / "keep.txt", 1)
        self.public.symlink_to(self.root / "elsewhere", target_is_directory=True)
        self.assertIn("must not be a symlink", self.assert_refused(snapshot(self.public)))
        self.assertTrue(self.public.is_symlink())
        self.assertEqual(sorted(p.name for p in (self.root / "elsewhere").iterdir()), ["keep.txt"])
