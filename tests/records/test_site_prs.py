"""DF2007-09 (CON-SITEPR-1): the offline site draft PR snapshot importer and reader. Synthetic data only; no network."""
import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from campaign_tool.records import site_prs

REPO = "example-org/example-site"
NOW = datetime(2026, 11, 15, 12, 0, 0, tzinfo=timezone.utc)


def pr(number=7, **over):
    row = {"number": number, "title": "Add agency page", "isDraft": True,
           "url": "https://github.com/%s/pull/%d" % (REPO, number), "headRefName": "page/agency-%d" % number,
           "updatedAt": "2026-11-14T12:00:00Z"}
    row.update(over)
    return row


def state(root):
    out = []
    for name in sorted(os.listdir(root)):
        info = os.lstat(os.path.join(root, name))
        out.append((name, info.st_mode, info.st_mtime_ns, Path(root, name).read_bytes()))
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="synthetic-site-prs-"))
        self.root = self.tmp / "root"
        self.root.mkdir(mode=0o700)
        self.source = self.tmp / "capture.json"

    def capture(self, rows, raw=None):
        self.source.write_bytes(raw if raw is not None else json.dumps(rows).encode())

    def run_import(self, now=NOW):
        return site_prs.snapshot(str(self.source), str(self.root), REPO, now)

    def refused(self, code=None):
        before = state(self.root)
        with self.assertRaises(site_prs.SitePrError) as caught:
            self.run_import()
        if code:
            self.assertEqual(str(caught.exception), code)
        self.assertEqual(state(self.root), before)


class Importer(Base):
    def test_valid_capture_imports_0600_and_stores_only_the_allowed_fields(self):
        self.capture([pr(9, title="Second \x00\x1b[31m<script>x</script>"), pr(7)])
        self.assertEqual(self.run_import(), 2)
        dest = self.root / "site-prs.json"
        self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o600)
        doc = json.loads(dest.read_text())
        self.assertEqual((doc["schema"], doc["repository"], doc["captured_at"]), (site_prs.SCHEMA, REPO, "2026-11-15T12:00:00Z"))
        self.assertEqual([p["number"] for p in doc["prs"]], [7, 9])
        self.assertTrue(all(set(p) == site_prs.FIELDS for p in doc["prs"]))
        self.assertEqual(doc["prs"][1]["title"], "Second [31m<script>x</script>")
        self.assertEqual(os.listdir(self.root), ["site-prs.json"])
        model = site_prs.load(str(dest), NOW + timedelta(hours=3, minutes=5))
        self.assertEqual((model["state"], model["hours"], model["stale"]), ("ok", 3, False))

    def test_utf8_capture_round_trips_at_maximum_rows_and_title_length(self):
        rows = [pr(n, title="界" * site_prs.MAX_TEXT) for n in range(1, site_prs.MAX_ROWS + 1)]
        raw = json.dumps(rows, ensure_ascii=False).encode("utf-8")
        self.assertLessEqual(len(raw), site_prs.MAX_BYTES)
        self.capture(rows, raw=raw)
        self.assertEqual(self.run_import(), site_prs.MAX_ROWS)
        dest = self.root / site_prs.NAME
        model = site_prs.load(str(dest), NOW)
        self.assertEqual(model["state"], "ok")
        self.assertEqual(model["prs"], rows)
        self.assertLessEqual(dest.stat().st_size, site_prs.MAX_BYTES)
        self.assertIn("50 captured open drafts", site_prs.home_card(model))
        self.assertIn("界" * site_prs.MAX_TEXT, site_prs.render(model, NOW))

    def test_escaped_surrogates_remain_json_escaped_in_utf8_storage(self):
        rows = [pr(title="Synthetic " + chr(0xD800), headRefName="branch-" + chr(0xDFFF))]
        self.capture(rows)
        self.assertEqual(self.run_import(), 1)
        model = site_prs.load(str(self.root / site_prs.NAME), NOW)
        self.assertEqual(model["state"], "ok")
        self.assertEqual(model["prs"], rows)

    def test_encoded_output_over_reader_cap_keeps_the_old_snapshot(self):
        self.capture([pr()])
        self.run_import()
        rows = [pr(n, title="😀" * 1005, headRefName="a" * site_prs.MAX_TEXT)
                for n in range(1, site_prs.MAX_ROWS + 1)]
        raw = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.assertLessEqual(len(raw), site_prs.MAX_BYTES)
        self.capture(rows, raw=raw)
        self.refused("too_large")
        model = site_prs.load(str(self.root / site_prs.NAME), NOW)
        self.assertEqual(model["state"], "ok")
        self.assertEqual(model["prs"], [pr()])

    def test_bad_url_or_unknown_field_writes_nothing(self):
        self.capture([pr()])
        self.run_import()
        good = state(self.root)
        bad = [
            [pr(url="https://evil.example/%s/pull/7" % REPO)],
            [pr(url="http://github.com/%s/pull/7" % REPO)],
            [pr(url="https://github.com.evil.example/%s/pull/7" % REPO)],
            [pr(url="https://user@github.com/%s/pull/7" % REPO)],
            [pr(url="https://github.com:443/%s/pull/7" % REPO)],
            [pr(url="https://github.com/other/repo/pull/7")],
            [pr(url="https://github.com/%s/pull/8" % REPO)],
            [pr(url="https://github.com/%s/pull/7?x=1" % REPO)],
            [pr(url="https://github.com/%s/pull/7/files" % REPO)],
            [{**pr(), "author": "someone"}],
            [{k: v for k, v in pr().items() if k != "title"}],
            [pr(isDraft=False)], [pr(isDraft=1)], [pr(number=True)], [pr(number=0)], [pr(title=5)],
            [pr(updatedAt="2026-11-14 12:00:00")], [pr(updatedAt="2026-02-30T12:00:00Z")], [pr(updatedAt="2026-11-14T12:00:00+02:00")],
            [pr(headRefName="")], [pr(headRefName="a\nb")], [pr(title="x" * 1025)],
            [pr(7), pr(7)], {"number": 7}, [pr(n) for n in range(1, 52)],
        ]
        for rows in bad:
            self.capture(rows)
            self.refused()
        for raw in (b'[{"number": 7, "number": 8}]', b"[NaN]", b"\xff\xfe", b"not json", b"[" * 5000):
            self.capture(None, raw)
            self.refused()
        self.capture(None, b" " * (site_prs.MAX_BYTES + 1) + b"[]")
        self.refused("too_large")
        self.assertEqual(state(self.root), good)

    def test_empty_and_fifty_are_valid(self):
        self.capture([])
        self.assertEqual(self.run_import(), 0)
        model = site_prs.load(str(self.root / "site-prs.json"), NOW)
        self.assertEqual((model["state"], model["prs"]), ("ok", []))
        self.capture([pr(n) for n in range(1, 51)])
        self.assertEqual(self.run_import(), 50)

    def test_rerun_replaces_and_tightens_mode_and_failure_keeps_the_old_snapshot(self):
        self.capture([pr(7)])
        self.run_import()
        dest = self.root / "site-prs.json"
        dest.chmod(0o644)
        self.capture([pr(8)])
        self.run_import(NOW + timedelta(hours=1))
        doc = json.loads(dest.read_text())
        self.assertEqual(([p["number"] for p in doc["prs"]], doc["captured_at"], stat.S_IMODE(dest.stat().st_mode)),
                         ([8], "2026-11-15T13:00:00Z", 0o600))
        before = state(self.root)
        with mock.patch.object(os, "replace", side_effect=OSError("synthetic")):
            with self.assertRaises(site_prs.SitePrError):
                self.run_import()
        self.assertEqual(state(self.root), before)  # old bytes kept and the writer's own temp removed

    def test_links_and_bad_roots_are_refused(self):
        self.capture([pr()])
        link = self.tmp / "link.json"
        link.symlink_to(self.source)
        with self.assertRaises(site_prs.SitePrError):
            site_prs.snapshot(str(link), str(self.root), REPO, NOW)
        with self.assertRaises(site_prs.SitePrError):
            site_prs.snapshot(str(self.tmp / "missing.json"), str(self.root), REPO, NOW)
        (self.tmp / "elsewhere").mkdir()
        (self.tmp / "rootlink").symlink_to(self.tmp / "elsewhere")
        with self.assertRaises(site_prs.SitePrError):
            site_prs.snapshot(str(self.source), str(self.tmp / "rootlink"), REPO, NOW)
        with self.assertRaises(site_prs.SitePrError):
            site_prs.snapshot(str(self.source), str(self.tmp / "nodir"), REPO, NOW)
        (self.root / "site-prs.json").symlink_to(self.source)
        self.refused("destination_invalid")
        for bad in ("a", "a/b/c", "../b", "a/..", "a b/c", ""):
            with self.assertRaises(site_prs.SitePrError):
                site_prs.snapshot(str(self.source), str(self.root), bad, NOW)
        self.assertEqual(os.listdir(self.tmp / "elsewhere"), [])

    def test_cli_prints_refusal_without_input_text_and_exits_2(self):
        self.capture([pr(url="https://evil.example/SECRET-SENTINEL")])
        out, err = io.StringIO(), io.StringIO()
        argv = ["snapshot", "--from", str(self.source), "--export-root", str(self.root), "--repository", REPO]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(site_prs.main(argv), 2)
        self.assertNotIn("SECRET-SENTINEL", out.getvalue() + err.getvalue())
        self.assertEqual(os.listdir(self.root), [])
        self.capture([pr()])
        with contextlib.redirect_stdout(out):
            self.assertEqual(site_prs.main(argv), 0)
        self.assertTrue((self.root / "site-prs.json").is_file())


class Reader(Base):
    def test_missing_snapshot_is_unavailable_never_zero(self):
        path = self.root / "site-prs.json"
        self.assertEqual(site_prs.load(str(path), NOW), {"state": "unavailable", "code": "site_prs_missing"})
        path.write_text("{not json")
        self.assertEqual(site_prs.load(str(path), NOW)["state"], "unavailable")
        path.unlink()
        path.symlink_to(self.tmp / "nowhere")
        self.assertEqual(site_prs.load(str(path), NOW)["code"], "site_prs_missing")
        path.unlink()
        path.write_bytes(b" " * (site_prs.MAX_BYTES + 1))
        self.assertEqual(site_prs.load(str(path), NOW)["code"], "too_large")
        path.write_text(json.dumps({"schema": site_prs.SCHEMA, "repository": REPO, "captured_at": "2026-11-15T12:00:00Z"}))
        self.assertEqual(site_prs.load(str(path), NOW)["state"], "unavailable")
        for model in (site_prs.load(str(path), NOW), site_prs.load(str(self.root / "gone"), NOW)):
            self.assertNotIn("prs", model)

    def test_age_and_stale_threshold_are_capture_based(self):
        self.capture([pr(updatedAt="2025-01-01T00:00:00Z")])  # an old PR in a fresh capture
        self.run_import()
        path = str(self.root / "site-prs.json")
        cases = ((timedelta(seconds=0), 0, False), (timedelta(hours=23, minutes=59, seconds=59), 23, False),
                 (timedelta(hours=24), 24, False), (timedelta(hours=24, seconds=1), 24, True), (timedelta(hours=50), 50, True))
        for delta, hours, stale in cases:
            model = site_prs.load(path, NOW + delta)
            self.assertEqual((model["hours"], model["stale"]), (hours, stale), delta)
        self.assertEqual(site_prs.load(path, NOW - timedelta(seconds=1)), {"state": "unavailable", "code": "site_prs_future"})
        self.assertEqual(site_prs.age("2026-11-15T13:00:00Z", NOW), "update age unknown (clock ahead)")
        self.assertEqual(site_prs.age("2026-11-15T09:00:00Z", NOW), "3 hours")
        self.assertEqual(site_prs.age("2026-11-10T12:00:00Z", NOW), "5 days")


if __name__ == "__main__":
    unittest.main()
