"""kit-agency: the roster is read once and every check runs on the exact object returned. Synthetic data only."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit, kit_agencies  # noqa: E402

GOOD = [{"agency_id": "ca-fict-police", "name": "Fictional Police", "custom": {"a": [1]}},
        {"agency_id": "ca-fict-board", "name": "Fictional Board", "selected": False}]
BAD_ROWS = {
    "missing name": [GOOD[0], {"agency_id": "ca-fict-board"}],
    "integer name": [GOOD[0], {"agency_id": "ca-fict-board", "name": 77}],
    "null name": [GOOD[0], {"agency_id": "ca-fict-board", "name": None}],
    "non-object row": [GOOD[0], "ca-fict-board"],
    "bad id": [GOOD[0], {"agency_id": "Bad ID", "name": "X"}],
}


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "kit").mkdir()
        self.path = self.root / "kit" / "agencies.json"
        self.write(GOOD)
        (self.root / "campaign.json").write_text(json.dumps(
            {"schema_version": 1, "name": "Fict", "county": "Fict County", "state": "CA"}), encoding="utf-8")

    def write(self, value):
        self.path.write_text(json.dumps(value), encoding="utf-8")

    def swap_after_first_read(self, replacement):
        """Real reader, but the real file is replaced right after the first read returns."""
        real, calls = kit._read_json, []

        def hook(path):
            result = real(path)
            calls.append(path)
            if len(calls) == 1:
                self.write(replacement)
            return result
        return mock.patch.object(kit, "_read_json", hook), calls

    def test_replacement_after_first_real_read_cannot_supply_unvalidated_rows(self):
        for label, bad in BAD_ROWS.items():
            with self.subTest(label):
                self.write(GOOD)
                patch, calls = self.swap_after_first_read(bad)
                with patch:
                    result = kit_agencies.load_agencies(self.root)
                self.assertEqual(len(calls), 1)
                self.assertEqual(result, GOOD)

    def test_writers_use_only_the_validated_snapshot(self):
        for label, action in {
            "deselect": lambda: kit_agencies.set_selected(self.root, "ca-fict-police", False),
            "add": lambda: kit_agencies.add_agency(self.root, "ca-fict-new", "New Board", "other"),
        }.items():
            with self.subTest(label):
                self.write(GOOD)
                patch, calls = self.swap_after_first_read(BAD_ROWS["integer name"])
                with patch:
                    action()
                self.assertEqual(len(calls), 1)
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                self.assertNotIn(77, [row.get("name") for row in saved])
                self.assertEqual([row["agency_id"] for row in saved][:2], ["ca-fict-police", "ca-fict-board"])

    def test_one_read_malformed_roster_refuses_before_save(self):
        cases = dict(BAD_ROWS)
        cases["root dict"] = {"agency_id": "ca-fict-police"}
        cases["root scalar"] = 5
        for label, bad in cases.items():
            with self.subTest(label):
                self.write(bad)
                before = self.path.read_bytes()
                with mock.patch.object(kit_agencies, "_save", side_effect=AssertionError("saved")), \
                        self.assertRaises(ValueError):
                    kit_agencies.set_selected(self.root, "ca-fict-police", False)
                self.assertEqual(self.path.read_bytes(), before)

    def test_validation_phase_precedence_is_unchanged(self):
        dup_and_bad = [GOOD[0], GOOD[0], {"agency_id": "ca-fict-board", "name": 1}]
        self.write(dup_and_bad)
        with self.assertRaisesRegex(ValueError, "needs a name"):
            kit_agencies.load_agencies(self.root)
        dup_and_selected = [dict(GOOD[0], selected="yes"), dict(GOOD[0])]
        self.write(dup_and_selected)
        with self.assertRaisesRegex(ValueError, "duplicate agency_id"):
            kit_agencies.load_agencies(self.root)
        self.write([dict(GOOD[0], selected="yes")])
        with self.assertRaisesRegex(ValueError, "selected must be true or false"):
            kit_agencies.load_agencies(self.root)
        self.write([])
        self.assertEqual(kit_agencies.load_agencies(self.root), [])

    def test_missing_optional_fields_stay_missing(self):
        self.write(GOOD)
        rows = kit_agencies.load_agencies(self.root)
        self.assertNotIn("selected", rows[0])
        self.assertNotIn("kind", rows[0])
        before = self.path.read_bytes()
        self.assertFalse(kit_agencies.set_selected(self.root, "ca-fict-police", True)["changed"])
        self.assertEqual(self.path.read_bytes(), before)
        kit_agencies.set_selected(self.root, "ca-fict-board", True)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved[0], GOOD[0])
        self.assertEqual(list(saved[0]), list(GOOD[0]))
        self.assertEqual(saved[1]["selected"], True)


if __name__ == "__main__":
    unittest.main()
