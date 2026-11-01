"""fetch_osm_alprs safe write: campaign dir required, empty answer keeps a map, atomic no-follow (RF1730-11)."""
import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_fetch_osm_alprs_offline import golden
from tools import fetch_osm_alprs as tool

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "osm" / "overpass-small.json"
ELEMENTS = json.loads(FIXTURE.read_text(encoding="utf-8"))["elements"]
EMPTY = {"elements": []}
WRITE_FAILED = "fetch_osm_alprs: could not write cameras.geojson; it was left unchanged"


class SafeWriteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-osm-write-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.campaign = self.tmp / "campaign"
        self.campaign.mkdir()
        (self.campaign / "campaign.json").write_text("{}\n", encoding="utf-8")
        self.map_dir = self.campaign / "content" / "map"
        self.cameras = self.map_dir / "cameras.geojson"
        patcher = mock.patch.object(tool.urllib.request, "urlopen", side_effect=AssertionError("network used"))
        self.urlopen = patcher.start()
        self.addCleanup(patcher.stop)

    def saved(self, document=None):
        path = self.tmp / "saved.json"
        path.write_text(json.dumps(document if document is not None else {"elements": ELEMENTS}), encoding="utf-8")
        return path

    def run_tool(self, *extra, document=None, directory=None):
        argv = ["--directory", str(directory or self.campaign), "--from-file", str(self.saved(document)), *extra]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(argv)
        return code, out.getvalue(), err.getvalue()

    def reference_bytes(self):
        """RF1730-9's frozen golden output for the fixture."""
        return golden(ELEMENTS).encode("utf-8")

    def three_point_map(self):
        self.assertEqual(self.run_tool()[0], 0)
        return self.cameras.read_bytes()

    def temps(self):
        return sorted(p.name for p in self.map_dir.iterdir() if p.name.startswith(".cameras.geojson."))

    # 1. no campaign.json: refused before any read or network call, nothing created
    def test_missing_campaign_json_refused_without_network(self):
        bare = self.tmp / "typo"
        bare.mkdir()
        for argv in (["--from-file", str(self.saved())], ["--bbox", "0", "0", "1", "1"], ["--relation", "1"]):
            with self.subTest(argv=argv[0]):
                err = io.StringIO()
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    code = tool.main(["--directory", str(bare), *argv])
                self.assertEqual((code, err.getvalue().strip()), (2, tool.NOT_CAMPAIGN))
                self.assertEqual(list(bare.iterdir()), [])
        code, _, err = self.run_tool(directory=self.tmp / "absent")
        self.assertEqual((code, err.strip()), (2, tool.NOT_CAMPAIGN))
        self.assertFalse((self.tmp / "absent").exists())
        self.urlopen.assert_not_called()

    def test_campaign_check_precedes_saved_file_read(self):
        bare = self.tmp / "bare"
        bare.mkdir()
        with mock.patch.object(tool, "load_saved", side_effect=AssertionError("read")):
            code, _, err = self.run_tool(directory=bare)
        self.assertEqual((code, err.strip()), (2, tool.NOT_CAMPAIGN))

    # 2. campaign.json a symlink or a directory
    def test_campaign_json_symlink_or_directory_refused(self):
        real = self.tmp / "real.json"
        real.write_text("{}\n", encoding="utf-8")
        for label in ("symlink", "directory"):
            with self.subTest(label):
                campaign = self.tmp / label
                campaign.mkdir()
                if label == "symlink":
                    (campaign / "campaign.json").symlink_to(real)
                else:
                    (campaign / "campaign.json").mkdir()
                code, _, err = self.run_tool(directory=campaign)
                self.assertEqual((code, err.strip()), (2, tool.NOT_CAMPAIGN))
                self.assertFalse((campaign / "content").exists())

    # 3. content or content/map a symlink or non-directory
    def test_symlinked_or_file_map_dirs_refused(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "keep.txt").write_text("keep\n", encoding="utf-8")
        (self.campaign / "content").mkdir()
        self.map_dir.symlink_to(elsewhere, target_is_directory=True)
        code, out, err = self.run_tool()
        self.assertEqual((code, out), (2, ""))
        self.assertIn("is a symlink or not a directory", err)
        self.assertEqual(sorted(p.name for p in elsewhere.iterdir()), ["keep.txt"])
        self.map_dir.unlink()
        self.map_dir.write_text("not a dir\n", encoding="utf-8")
        self.assertEqual(self.run_tool()[0], 2)
        self.map_dir.unlink()
        (self.campaign / "content").rmdir()
        (self.campaign / "content").symlink_to(elsewhere, target_is_directory=True)
        self.assertEqual(self.run_tool()[0], 2)
        self.assertEqual(sorted(p.name for p in elsewhere.iterdir()), ["keep.txt"])

    # 4. cameras.geojson or attribution.txt a symlink: link and target unchanged
    def test_symlinked_cameras_refused(self):
        self.map_dir.mkdir(parents=True)
        target = self.tmp / "target.geojson"
        target.write_bytes(b"original\n")
        self.cameras.symlink_to(target)
        code, _, err = self.run_tool()
        self.assertEqual((code, err.strip()), (2, "fetch_osm_alprs: cameras.geojson is a symlink; not written"))
        self.assertTrue(self.cameras.is_symlink())
        self.assertEqual(target.read_bytes(), b"original\n")
        self.assertEqual(self.temps(), [])

    def test_symlinked_attribution_refused_after_cameras_written(self):
        self.map_dir.mkdir(parents=True)
        target = self.tmp / "target.txt"
        target.write_bytes(b"original\n")
        (self.map_dir / "attribution.txt").symlink_to(target)
        code, _, err = self.run_tool()
        self.assertEqual(code, 2)
        self.assertIn("attribution.txt is a symlink; not written (the new cameras.geojson was written)", err)
        self.assertEqual(target.read_bytes(), b"original\n")
        self.assertEqual(self.cameras.read_bytes(), self.reference_bytes())

    # 5. empty answer over a 3-point map: kept; --allow-empty replaces
    def test_empty_answer_keeps_non_empty_map(self):
        before = self.three_point_map()
        code, out, err = self.run_tool(document=EMPTY)
        self.assertEqual((code, out, err.strip()), (3, "", tool.EMPTY_KEPT))
        self.assertEqual(self.cameras.read_bytes(), before)
        # all-skipped answers are empty too
        code, _, _ = self.run_tool(document={"elements": [{"type": "way", "id": 1}]})
        self.assertEqual(code, 3)
        self.assertEqual(self.cameras.read_bytes(), before)
        self.assertEqual(self.temps(), [])

    def test_allow_empty_replaces(self):
        self.three_point_map()
        code, out, err = self.run_tool("--allow-empty", document=EMPTY)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("points=0 skipped=0", out)
        self.assertEqual(json.loads(self.cameras.read_text(encoding="utf-8"))["features"], [])

    # 6. empty answer with no current map, or an already-empty map
    def test_empty_answer_without_current_map_written(self):
        code, out, err = self.run_tool(document=EMPTY)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(self.cameras.read_text(encoding="utf-8"))["features"], [])
        self.assertEqual(self.run_tool(document=EMPTY)[0], 0)

    # 7. current map unreadable: replaced with a warning line
    def test_unreadable_current_map_replaced_with_warning(self):
        self.map_dir.mkdir(parents=True)
        for raw in (b"{half", b'{"features": 3}', b"[1, 2]", b"\xff\xfe"):
            with self.subTest(raw=raw):
                self.cameras.write_bytes(raw)
                code, _, err = self.run_tool(document=EMPTY)
                self.assertEqual(code, 0)
                self.assertEqual(err.strip(),
                                 "fetch_osm_alprs: warning: current cameras.geojson is unreadable; replacing it")
                self.assertEqual(json.loads(self.cameras.read_text(encoding="utf-8"))["features"], [])

    # 8. os.replace fails: exit 2, original bytes unchanged, no temp left
    def test_replace_failure_keeps_original_and_cleans_temp(self):
        before = self.three_point_map()
        document = {"elements": ELEMENTS[:1]}
        for failing in ("replace", "fsync"):
            with self.subTest(failing), mock.patch.object(tool.os, failing, side_effect=OSError("disk full")):
                code, out, err = self.run_tool(document=document)
            self.assertEqual((code, out, err.strip()), (2, "", WRITE_FAILED))
            self.assertEqual(self.cameras.read_bytes(), before)
            self.assertEqual(self.temps(), [])

    # 9. stale temp from an earlier crash: ignored, not reused, not removed
    def test_stale_temp_ignored(self):
        self.map_dir.mkdir(parents=True)
        stale = self.map_dir / ".cameras.geojson.abc123"
        stale.write_bytes(b"stale\n")
        code, _, _ = self.run_tool()
        self.assertEqual(code, 0)
        self.assertEqual(stale.read_bytes(), b"stale\n")
        self.assertEqual(self.temps(), [stale.name])
        self.assertNotEqual(self.cameras.read_bytes(), b"stale\n")

    # 10. normal run: mode 0644, bytes identical to RF1730-9 output
    def test_normal_run_mode_and_bytes(self):
        old = os.umask(0o077)
        try:
            code, out, err = self.run_tool()
        finally:
            os.umask(old)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("points=3 skipped=0", out)
        for name in ("cameras.geojson", "attribution.txt"):
            final = self.map_dir / name
            self.assertEqual(stat.S_IMODE(final.stat().st_mode), 0o644, name)
            self.assertFalse(final.is_symlink())
        self.assertEqual((self.map_dir / "attribution.txt").read_text(encoding="utf-8"), tool.ATTRIBUTION + "\n")
        self.assertEqual(self.cameras.read_bytes(), self.reference_bytes())
        self.assertEqual(self.temps(), [])
        self.urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
