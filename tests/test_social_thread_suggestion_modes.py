"""Finding-thread suggestion folders are private when created, even if the write that follows fails."""
import os
import stat
import unittest
from unittest import mock

from campaign_tool import social
from campaign_tool.social import _write_drafts
from campaign_tool.social_threads import build_thread_social
from tests import test_social_threads as base

FID, SLUG = base.FID, base.SLUG


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


class SuggestionModes(base.ThreadCase):
    def setUp(self):
        super().setUp()
        self.save(summary="Short summary.", limitations=[], corrections=[], sources=[self.cite(base.CAFE)])
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)

    def edit(self, platform=None):
        out = self.threads / platform if platform else self.threads.parent / "threads"
        build_thread_social(self.root, FID, [platform] if platform else None)
        draft = (self.threads / platform if platform else self.threads) / f"{SLUG}.md"
        draft.write_text("organizer edit\n", encoding="utf-8")
        self.save(summary="Changed summary.")
        return draft

    def suggested(self, platform):
        return self.threads / (platform + ".suggested") if platform else self.root / "kit" / "social" / "threads.suggested"

    def observing(self, seen, fail_on=None, fail_json=None):
        real_text, real_json = social._write_text, social._write_json

        def text(path, body):
            if path.parent.name.endswith(".suggested"):
                seen.append(mode(path.parent))
                if fail_on:
                    raise fail_on
            return real_text(path, body)

        def js(path, doc):
            if fail_json and path.name == ".written.json":
                raise fail_json
            return real_json(path, doc)
        return mock.patch.multiple(social, _write_text=text, _write_json=js)

    def test_thread_suggestion_is_private_before_first_write(self):
        for platform in (None, "x", "bluesky", "mastodon"):
            for mask in (0o000, 0o022):
                with self.subTest(platform=platform, umask=mask):
                    os.umask(mask)
                    self.setUp_clean()
                    draft = self.edit(platform)
                    seen = []
                    with self.observing(seen):
                        build_thread_social(self.root, FID, [platform] if platform else None)
                    self.assertEqual([m & 0o077 for m in seen], [0])
                    self.assertEqual(draft.read_text(encoding="utf-8"), "organizer edit\n")
                    self.assertEqual(mode(self.suggested(platform) / f"{SLUG}.md"), 0o600)

    def setUp_clean(self):
        import shutil
        shutil.rmtree(self.root / "kit", ignore_errors=True)

    def test_failed_suggestion_write_leaves_private_directory(self):
        for platform in (None, "mastodon"):
            with self.subTest(platform=platform):
                self.setUp_clean()
                draft = self.edit(platform)
                record = (draft.parent / ".written.json").read_bytes()
                seen, boom = [], OSError("synthetic suggestion write failure")
                with self.observing(seen, fail_on=boom):
                    with self.assertRaises(OSError) as ctx:
                        build_thread_social(self.root, FID, [platform] if platform else None)
                self.assertIs(ctx.exception, boom)
                self.assertEqual(seen[0] & 0o077, 0)
                self.assertEqual(mode(self.suggested(platform)) & 0o077, 0)
                self.assertEqual(draft.read_text(encoding="utf-8"), "organizer edit\n")
                self.assertEqual((draft.parent / ".written.json").read_bytes(), record)
                report = build_thread_social(self.root, FID, [platform] if platform else None)
                self.assertEqual(draft.read_text(encoding="utf-8"), "organizer edit\n")
                self.assertTrue((self.suggested(platform) / f"{SLUG}.md").is_file())
                self.assertFalse(report["posted"])

    def test_late_record_failure_does_not_skip_privacy(self):
        draft = self.edit("mastodon")
        seen, boom = [], OSError("synthetic record failure")
        with self.observing(seen, fail_json=boom):
            with self.assertRaises(OSError) as ctx:
                build_thread_social(self.root, FID, ["mastodon"])
        self.assertIs(ctx.exception, boom)
        self.assertEqual([m & 0o077 for m in seen], [0])
        self.assertEqual(mode(self.suggested("mastodon")) & 0o077, 0)
        self.assertEqual(draft.read_text(encoding="utf-8"), "organizer edit\n")

    def test_existing_suggestion_modes_are_not_changed(self):
        for existing in (0o700, 0o750):
            for fail in (False, True):
                with self.subTest(existing=oct(existing), fail=fail):
                    self.setUp_clean()
                    draft = self.edit("mastodon")
                    folder = self.suggested("mastodon")
                    folder.mkdir()
                    os.chmod(folder, existing)
                    seen = []
                    with self.observing(seen, fail_on=OSError("x") if fail else None):
                        if fail:
                            with self.assertRaises(OSError):
                                build_thread_social(self.root, FID, ["mastodon"])
                        else:
                            build_thread_social(self.root, FID, ["mastodon"])
                    self.assertEqual(seen, [existing])
                    self.assertEqual(mode(folder), existing)

    def test_no_suggestion_needed_creates_no_suggestion_folder(self):
        build_thread_social(self.root, FID, ["x", "x", "mastodon"])
        build_thread_social(self.root, FID, ["x", "mastodon"])
        self.assertEqual(sorted(p.name for p in self.threads.iterdir()), ["mastodon", "x"])
        (self.threads / "x" / f"{SLUG}.md").write_text("organizer edit\n", encoding="utf-8")
        self.save(summary="Changed summary.")
        build_thread_social(self.root, FID, ["x", "mastodon"])
        self.assertEqual(sorted(p.name for p in self.threads.iterdir()), ["mastodon", "x", "x.suggested"])

    def test_preflight_refusals_still_have_no_side_effects(self):
        with self.assertRaises(ValueError):
            build_thread_social(self.root, FID, ["nonsense"])
        self.assertFalse((self.root / "kit").exists())
        self.edit("mastodon")
        (self.threads / "bluesky.suggested").write_text("a file", encoding="utf-8")
        before = sorted(str(p) for p in self.threads.rglob("*"))
        with self.assertRaises(ValueError):
            build_thread_social(self.root, FID, ["mastodon", "bluesky"])
        self.assertEqual(sorted(str(p) for p in self.threads.rglob("*")), before)
        self.assertFalse(self.suggested("mastodon").exists())

    def test_shared_writer_default_and_explicit_mode(self):
        for kwargs, expect in (({}, 0o755), ({"suggested_mode": 0o700}, 0o700)):
            with self.subTest(kwargs=kwargs):
                out, sug = self.base_dir / ("o%o" % expect), self.base_dir / ("s%o" % expect)
                _write_drafts(out, sug, {"a": "first draft\n"}, **kwargs)
                (out / "a.md").write_text("edit\n", encoding="utf-8")
                report = _write_drafts(out, sug, {"a": "second draft\n"}, **kwargs)
                self.assertEqual(report["kept_edited"], ["a"])
                self.assertEqual(mode(sug), expect)


if __name__ == "__main__":
    unittest.main()
