"""kit-agency add|select|deselect maintains kit/agencies.json by command. Offline seed, fictional added agencies."""
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit, kit_agencies  # noqa: E402

ADDED = "ca-fictional-county-sheriff"


def cli(*arguments):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, arguments)],
                          cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)


def tree_hash(path):
    digest = hashlib.sha256()
    for item in sorted(Path(path).rglob("*")) if Path(path).is_dir() else [Path(path)]:
        digest.update(str(item.relative_to(path.parent)).encode())
        digest.update(item.read_bytes() if item.is_file() else b"<dir>")
    return digest.hexdigest()


class KitAgencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-agency-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        result = cli("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                     "--state", "CA")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.kit = self.root / "kit"
        self.path = self.kit / "agencies.json"
        self.build()
        self.build()  # second run writes agencies.suggested.json
        self.target = next(a["agency_id"] for a in self.agencies() if a["kind"] in kit.LAW_ENFORCEMENT)
        self.draft = self.kit / "requests" / (self.target + ".md")

    def build(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return kit.build_kit(self.root)

    def agencies(self):
        return json.loads(self.path.read_text())

    def entry(self, agency_id):
        return next(a for a in self.agencies() if a["agency_id"] == agency_id)

    def agency(self, *arguments):
        return cli("kit-agency", arguments[0], "--directory", self.root, *arguments[1:])

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.rstrip().endswith("Nothing was sent."), result.stdout)
        return json.loads(result.stdout.rsplit("Nothing was sent.", 1)[0])

    def assert_refused(self, result, *words):
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(result.stderr.startswith("Stopped: "), result.stderr)
        for word in words:
            self.assertIn(word, result.stderr)

    def add(self, agency_id=ADDED, name="Fictional County Sheriff", kind="sheriff", *extra):
        return self.agency("add", "--id", agency_id, "--name", name, "--kind", kind, *extra)

    def test_deselect_then_select_round_trip_through_kit(self):
        self.assertTrue(self.draft.is_file())
        self.assertEqual(self.assert_ok(self.agency("deselect", "--id", self.target)),
                         {"agency_id": self.target, "selected": False, "changed": True})
        self.assertIs(self.entry(self.target)["selected"], False)
        before, mtime = self.path.read_bytes(), self.path.stat().st_mtime_ns
        self.assertFalse(self.assert_ok(self.agency("deselect", "--id", self.target))["changed"])
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns), (before, mtime))
        summary = self.build()
        self.assertFalse(self.draft.exists(), "an unedited draft for a deselected agency is removed")
        self.assertTrue(summary["agencies_preserved"])
        self.assertIs(self.entry(self.target)["selected"], False, "kit re-run keeps the change")
        self.assertTrue(self.assert_ok(self.agency("select", "--id", self.target))["changed"])
        self.build()
        self.assertTrue(self.draft.is_file())
        self.assertFalse(self.assert_ok(self.agency("select", "--id", self.target))["changed"])

    def test_untouched_entries_keep_key_order_and_mode(self):
        os.chmod(self.path, 0o644)
        others = [a for a in self.agencies() if a["agency_id"] != self.target]
        self.assert_ok(self.agency("deselect", "--id", self.target))
        self.assertEqual([list(a) for a in self.agencies() if a["agency_id"] != self.target], [list(a) for a in others])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o644)

    def test_add_sheriff_is_drafted_and_survives_rerun(self):
        result = self.assert_ok(self.add(ADDED, "Fictional County Sheriff", "sheriff", "--note", "Seen at a meeting"))
        expected = {"agency_id": ADDED, "name": "Fictional County Sheriff", "kind": "sheriff", "selected": True,
                    "verified": False, "added_by": "organizer", "added_on": date.today().isoformat(),
                    "note": "Seen at a meeting"}
        self.assertEqual(result, expected)
        self.assertEqual(self.agencies()[-1], expected)
        self.build()
        self.assertTrue((self.kit / "requests" / (ADDED + ".md")).is_file())
        after_first = self.path.read_bytes()
        self.build()
        self.assertEqual(self.path.read_bytes(), after_first)
        self.assertEqual(self.entry(ADDED), expected)

    def test_add_bad_fields_refused_by_name_file_unchanged(self):
        self.assert_ok(self.add())
        before = self.path.read_bytes()
        cases = [(self.add(), ("already exists", ADDED)),
                 (self.add("nv-fictional-police"), ("agency_id", "'ca-'")),
                 (self.add("CA-Fictional-Police"), ("agency_id", "lowercase")),
                 (self.add("ca-fictional-two", "Fictional Two", "constable"), ("kind",)),
                 (self.add("ca-fictional-two", "Fx"), ("name",)),
                 (self.add("ca-fictional-two", "Fictional\nTwo"), ("name", "control")),
                 (self.add("ca-fictional-two", "Fictional Two", "police", "--note", "x" * 301), ("note", "300"))]
        for result, words in cases:
            with self.subTest(words=words):
                self.assert_refused(result, *words)
        self.assertEqual(self.path.read_bytes(), before)
        self.assert_ok(self.add("ca-fictional-two", "Fictional Two", "police", "--note", "x" * 300))

    def test_unknown_duplicate_and_non_boolean_selected_refused(self):
        self.assert_refused(self.agency("select", "--id", "ca-nowhere-police"), "ca-nowhere-police", "found 0")
        agencies = self.agencies()
        self.path.write_text(json.dumps(agencies + [dict(agencies[0])], indent=2) + "\n")
        before = self.path.read_bytes()
        for command in ("select", "deselect"):
            self.assert_refused(self.agency(command, "--id", self.target), "duplicate agency_id " + agencies[0]["agency_id"])
        self.assert_refused(self.add(), "duplicate agency_id")
        self.assertEqual(self.path.read_bytes(), before)
        agencies[0]["selected"] = "no"
        self.path.write_text(json.dumps(agencies, indent=2) + "\n")
        before = self.path.read_bytes()
        for command in ("select", "deselect"):
            self.assert_refused(self.agency(command, "--id", agencies[-1]["agency_id"]),
                                agencies[0]["agency_id"] + ": selected must be true or false")
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_symlink_directory_and_invalid_json_refused(self):
        real = self.kit / "real-agencies.json"
        os.replace(self.path, real)
        self.assert_refused(self.agency("select", "--id", self.target), "run kit first")
        self.assertFalse(self.path.exists())
        self.path.symlink_to(real)
        self.assert_refused(self.agency("deselect", "--id", self.target), "run kit first")
        self.assert_refused(self.add(), "run kit first")
        self.assertIn(self.target, real.read_text())
        self.assertNotIn(ADDED, real.read_text())
        self.path.unlink()
        self.path.mkdir()
        self.assert_refused(self.agency("select", "--id", self.target), "run kit first")
        self.assertEqual(list(self.path.iterdir()), [])
        self.path.rmdir()
        self.path.write_text("[{not json")
        self.assert_refused(self.agency("deselect", "--id", self.target))
        self.assertEqual(self.path.read_text(), "[{not json")
        bare = Path(tempfile.mkdtemp(prefix="synthetic-kit-agency-bare-", dir=self.root.parent))
        self.assert_refused(cli("kit-agency", "add", "--directory", bare, "--id", ADDED, "--name", "Fictional",
                                "--kind", "sheriff"), "run kit first")
        self.assertEqual(list(bare.iterdir()), [])

    def test_write_failure_leaves_file_and_no_temp(self):
        before = self.path.read_bytes()
        for call in (lambda: kit_agencies.set_selected(self.root, self.target, False),
                     lambda: kit_agencies.add_agency(self.root, ADDED, "Fictional County Sheriff", "sheriff")):
            with mock.patch("os.replace", side_effect=OSError("injected")), self.assertRaises(OSError):
                call()
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(sorted(p.name for p in self.kit.iterdir() if p.name.endswith(".tmp")), [])

    def test_suggested_and_requests_never_touched(self):
        suggested, requests = self.kit / "agencies.suggested.json", self.kit / "requests"
        self.assertTrue(suggested.is_file())
        before = (tree_hash(suggested), tree_hash(requests))
        self.assert_ok(self.agency("deselect", "--id", self.target))
        self.assert_ok(self.add())
        self.assert_ok(self.agency("select", "--id", self.target))
        self.assert_refused(self.add())
        self.assertEqual((tree_hash(suggested), tree_hash(requests)), before)

    def test_help_names_next_kit_run(self):
        result = cli("kit-agency", "add", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("next kit run drafts", " ".join(result.stdout.split()))


if __name__ == "__main__":
    unittest.main()
