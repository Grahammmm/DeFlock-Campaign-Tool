"""kit-agency path admission: a symlinked or non-folder kit/ is refused before the roster is read or edited.

Synthetic campaigns only. Real filesystem effects are paired with narrow seam assertions, because an unchanged
external file alone would not prove it was never opened.
"""
import contextlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit, kit_agencies, kit_atlas  # noqa: E402
from tests.test_kit_agency_atlas import SEED, csv_text, row  # noqa: E402  (helpers only; no test class imported)

SYMLINK = "kit/ must not be a symlink"
NOT_FOLDER = "kit/ is not a folder"
MISSING = "run kit first: kit/agencies.json is missing"
ENTRIES = [
    {"agency_id": "ca-fict-county-sheriff", "name": "Fictional County Sheriff", "kind": "sheriff", "selected": True,
     "custom_field": "kept"},
    {"agency_id": "ca-fict-north-police", "name": "North Fictional Police", "kind": "police", "selected": False}]
EXISTING, OFF = ENTRIES[0]["agency_id"], ENTRIES[1]["agency_id"]
NEW_ID = "ca-fict-new-board"
CSV = "agency_id,name,kind\nca-fict-csv-board,Fictional CSV Board,other\n"


def cli(*arguments):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, arguments)],
                          cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)


def snapshot(path):
    """Type, mode, size, mtime, inode and bytes of everything under (or at) path, without following links."""
    path, result = Path(path), {}
    for item in [path] + (sorted(path.rglob("*")) if path.is_dir() and not path.is_symlink() else []):
        info = item.lstat()
        kind = "link" if stat.S_ISLNK(info.st_mode) else "dir" if stat.S_ISDIR(info.st_mode) else "file"
        result[str(item)] = (kind, stat.S_IMODE(info.st_mode), info.st_mtime_ns, info.st_ino,
                             item.read_bytes() if kind == "file" else b"")
    return result


@contextlib.contextmanager
def no_roster_io():
    """Fail the test if either shared kit JSON seam, the writer or chmod is reached."""
    def fail(*_args, **_kwargs):
        raise AssertionError("roster IO reached")
    with contextlib.ExitStack() as stack:
        for target in ("campaign_tool.kit._read_json", "campaign_tool.kit._load_existing",
                       "campaign_tool.kit._write_json", "os.chmod"):
            stack.enter_context(mock.patch(target, side_effect=fail))
        yield


class PathCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-agency-paths-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        self.root.mkdir()
        (self.root / "campaign.json").write_text(json.dumps(
            {"schema_version": 1, "name": "Synthetic", "county": "Fictional", "state": "CA"}), encoding="utf-8")
        self.kit = self.root / "kit"
        self.external = self.tmp / "external-kit"
        self.external.mkdir()
        self.roster = self.external / "agencies.json"
        self.roster.write_text(json.dumps(ENTRIES, indent=2) + "\n", encoding="utf-8")
        os.chmod(self.roster, 0o640)
        (self.external / "suggestion.txt").write_text("sentinel", encoding="utf-8")
        self.csv = self.tmp / "roster.csv"
        self.csv.write_text(CSV, encoding="utf-8")

    def real_kit(self, mode=0o600):
        self.kit.mkdir()
        path = self.kit / "agencies.json"
        path.write_text(json.dumps(ENTRIES, indent=2) + "\n", encoding="utf-8")
        os.chmod(path, mode)
        return path

    def link_kit(self, target=None):
        self.kit.symlink_to(self.external if target is None else target)

    def agency(self, *arguments):
        return cli("kit-agency", arguments[0], "--directory", self.root, *arguments[1:])

    def assert_stopped(self, result, message):
        self.assertEqual((result.returncode, result.stdout), (1, ""), result.stderr)
        self.assertEqual(result.stderr, "Stopped: " + message + "\n")
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("Nothing was sent", result.stdout)

    def refused(self, call, message):
        with self.assertRaises(ValueError) as caught:
            call()
        self.assertEqual(str(caught.exception), message)


class LinkedKitTests(PathCase):
    def test_linked_kit_is_refused_before_any_roster_read(self):
        inside = self.root / "inside-folder"
        inside.mkdir()
        (inside / "agencies.json").write_text(json.dumps(ENTRIES), encoding="utf-8")
        plain = self.tmp / "plain-file"
        plain.write_text("not a folder", encoding="utf-8")
        cases = {"absolute external": self.external, "relative external": Path("..") / "external-kit",
                 "internal directory": Path("inside-folder"), "dangling": self.tmp / "nowhere", "file target": plain}
        for label, target in cases.items():
            with self.subTest(label):
                self.kit.symlink_to(target)
                before = snapshot(self.tmp)
                with no_roster_io():
                    self.refused(lambda: kit_agencies.load_agencies(self.root), SYMLINK)
                self.assertEqual(snapshot(self.tmp), before)
                self.kit.unlink()
        # a regular roster file behind a directory link is still not reachable
        self.link_kit()
        self.assertTrue((self.external / "agencies.json").is_file())
        with no_roster_io():
            self.refused(lambda: kit_agencies.load_agencies(self.root), SYMLINK)

    def test_mutating_cli_cannot_edit_another_kits_roster(self):
        self.link_kit()
        before = snapshot(self.external)
        commands = {"deselect": ("deselect", "--id", EXISTING), "select": ("select", "--id", OFF),
                    "add": ("add", "--id", NEW_ID, "--name", "Fictional New Board", "--kind", "other"),
                    "import": ("import", "--csv", self.csv)}
        for label, arguments in commands.items():
            with self.subTest(label):
                self.assert_stopped(self.agency(*arguments), SYMLINK)
                self.assertEqual(snapshot(self.external), before)
        self.assertEqual(sorted(p.name for p in self.external.iterdir()), ["agencies.json", "suggestion.txt"])
        self.assertEqual(json.loads(self.roster.read_text())[0]["selected"], True)

    def test_import_check_and_selected_noop_do_not_accept_linked_kit(self):
        self.link_kit()
        before = snapshot(self.external)
        self.assert_stopped(self.agency("import", "--csv", self.csv, "--check"), SYMLINK)
        self.assert_stopped(self.agency("select", "--id", EXISTING), SYMLINK)  # already selected: would be a no-op
        self.assert_stopped(self.agency("deselect", "--id", OFF), SYMLINK)  # already deselected: would be a no-op
        self.assertEqual(snapshot(self.external), before)
        # direct calls refuse before the CSV is read or a writer is reached
        with no_roster_io(), mock.patch.object(kit_agencies, "_read_csv", side_effect=AssertionError("csv read")):
            self.refused(lambda: kit_agencies.import_csv(self.root, self.csv, check=True), SYMLINK)
            self.refused(lambda: kit_agencies.import_csv(self.root, self.csv), SYMLINK)
            self.refused(lambda: kit_agencies.set_selected(self.root, EXISTING, True), SYMLINK)
            self.refused(lambda: kit_agencies.set_selected(self.root, OFF, False), SYMLINK)
            self.refused(lambda: kit_agencies.add_agency(self.root, NEW_ID, "Fictional New Board", "other"), SYMLINK)
        self.assertEqual(snapshot(self.external), before)

    def test_save_rechecks_parent_before_mode_or_write(self):
        real = self.real_kit()
        moved = self.tmp / "moved-kit"
        read = kit._read_json
        seen = []

        def read_then_swap(path):
            data = read(path)
            if not seen:  # first (and only) load has finished; now the kit entry becomes a link
                seen.append(path)
                os.replace(self.kit, moved)
                self.kit.symlink_to(self.external)
            return data

        before = snapshot(self.external)
        with mock.patch("campaign_tool.kit._read_json", side_effect=read_then_swap), \
                mock.patch("campaign_tool.kit._write_json", side_effect=AssertionError("written")), \
                mock.patch("os.chmod", side_effect=AssertionError("chmod")):
            self.refused(lambda: kit_agencies.set_selected(self.root, EXISTING, False), SYMLINK)
        self.assertEqual(seen, [real])
        self.assertEqual(snapshot(self.external), before)
        self.assertEqual(json.loads((moved / "agencies.json").read_text())[0]["selected"], True)

    def test_read_only_consumer_inherits_unsafe_kit_refusal(self):
        self.link_kit()
        atlas = self.tmp / "atlas.csv"
        atlas.write_text(csv_text([row("Fictional County Sheriff")]), encoding="utf-8")
        before = snapshot(self.external)
        with self.assertRaises(kit_atlas.AtlasError) as caught:
            kit_atlas.compare_atlas(self.root, atlas, seed=SEED)
        self.assertEqual(str(caught.exception), SYMLINK)
        self.assertEqual(snapshot(self.external), before)
        # the same synthetic setup succeeds against a real kit folder
        self.kit.unlink()
        self.real_kit()
        self.assertIn("ca-fict-county-sheriff", json.dumps(kit_atlas.compare_atlas(self.root, atlas, seed=SEED)))


class NonFolderKitTests(PathCase):
    def test_missing_and_non_directory_kit_never_create_or_open(self):
        with no_roster_io():
            self.refused(lambda: kit_agencies.load_agencies(self.root), MISSING)
        self.assertFalse(self.kit.exists() or self.kit.is_symlink())
        self.assert_stopped(self.agency("add", "--id", NEW_ID, "--name", "Fictional New Board", "--kind", "other"),
                            MISSING)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["campaign.json"])
        self.kit.write_text("not a folder", encoding="utf-8")
        with no_roster_io():
            self.refused(lambda: kit_agencies.load_agencies(self.root), NOT_FOLDER)
        self.assert_stopped(self.agency("deselect", "--id", EXISTING), NOT_FOLDER)
        self.assertEqual(self.kit.read_text(), "not a folder")
        self.kit.unlink()
        os.mkfifo(self.kit)
        with no_roster_io():
            self.refused(lambda: kit_agencies.load_agencies(self.root), NOT_FOLDER)  # returns, never blocks
        self.assert_stopped(self.agency("import", "--csv", self.csv, "--check"), NOT_FOLDER)
        self.assertTrue(stat.S_ISFIFO(self.kit.lstat().st_mode))

    def test_inspection_failure_is_a_fixed_message_not_a_missing_roster(self):
        self.real_kit()
        real_lstat = os.lstat

        def deny(path, *args, **kwargs):
            if Path(path) == self.kit:
                raise PermissionError(13, "SECRET-PATH-TEXT", str(path))
            return real_lstat(path, *args, **kwargs)

        with no_roster_io(), mock.patch("os.lstat", side_effect=deny):
            with self.assertRaises(ValueError) as caught:
                kit_agencies.load_agencies(self.root)
        self.assertEqual(str(caught.exception), "kit/ cannot be inspected")
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)


class LegacyContractTests(PathCase):
    def test_existing_leaf_refusals_remain_intact(self):
        path = self.real_kit()
        (self.kit / "other.txt").write_text("adjacent", encoding="utf-8")
        real = self.kit / "real-agencies.json"
        os.replace(path, real)
        before = (snapshot(self.kit / "other.txt"), snapshot(real))  # the folder's own mtime moves with each leaf swap
        self.refused(lambda: kit_agencies.load_agencies(self.root), MISSING)  # missing leaf
        path.symlink_to(real)
        self.refused(lambda: kit_agencies.load_agencies(self.root), MISSING)  # symlink leaf
        path.unlink()
        path.symlink_to(self.tmp / "nowhere")
        self.refused(lambda: kit_agencies.load_agencies(self.root), MISSING)  # dangling leaf
        path.unlink()
        path.mkdir()
        self.refused(lambda: kit_agencies.load_agencies(self.root), MISSING)  # directory leaf
        path.rmdir()
        os.mkfifo(path)
        self.refused(lambda: kit_agencies.load_agencies(self.root), MISSING)  # FIFO leaf, never opened
        path.unlink()
        self.assertEqual((snapshot(self.kit / "other.txt"), snapshot(real)), before)
        path.write_text("[{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            kit_agencies.load_agencies(self.root)
        self.assertEqual(path.read_text(), "[{not json")

    def test_real_directory_and_root_alias_preserve_legacy_contract(self):
        path = self.real_kit(0o640)
        alias = self.tmp / "alias"
        alias.symlink_to(self.root)  # the caller-supplied root may itself be a link; only kit/ is checked
        self.assertEqual(kit_agencies.load_agencies(alias)[0]["custom_field"], "kept")
        before_bytes, before_mtime = path.read_bytes(), path.stat().st_mtime_ns
        self.assertFalse(kit_agencies.set_selected(self.root, EXISTING, True)["changed"])  # no-op writes nothing
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), (before_bytes, before_mtime))
        self.assertEqual(kit_agencies.set_selected(self.root, EXISTING, False),
                         {"agency_id": EXISTING, "selected": False, "changed": True})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        data = json.loads(path.read_text())
        self.assertEqual(list(data[0]), ["agency_id", "name", "kind", "selected", "custom_field"])
        self.assertEqual(data[0]["custom_field"], "kept")
        self.assertEqual(path.read_text(), json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        # check accepts a real folder and writes nothing; apply through the alias and the CLI appends in one write
        before = snapshot(self.kit)
        self.assertEqual(kit_agencies.import_csv(alias, self.csv, check=True),
                         {"added": ["ca-fict-csv-board"], "count": 1, "written": False})
        self.assertEqual(snapshot(self.kit), before)
        self.assertEqual(kit_agencies.import_csv(alias, self.csv)["written"], True)
        result = self.agency("add", "--id", NEW_ID, "--name", "Fictional New Board", "--kind", "other")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.endswith("Nothing was sent.\n"))
        self.assertEqual([a["agency_id"] for a in json.loads(path.read_text())],
                         [EXISTING, OFF, "ca-fict-csv-board", NEW_ID])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        # validation still refuses as before
        self.refused(lambda: kit_agencies.add_agency(self.root, EXISTING, "Fictional County Sheriff", "sheriff"),
                     f"agency_id {EXISTING} already exists in kit/agencies.json")
        self.assertEqual(sorted(p.name for p in self.kit.iterdir()), ["agencies.json"])


if __name__ == "__main__":
    unittest.main()
