"""Atlas CSV header validation: linear work, unchanged diagnostics and precedence. Synthetic CSVs only."""
import csv
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from campaign_tool import kit_atlas  # noqa: E402
from campaign_tool.kit_atlas import AtlasError  # noqa: E402

BASE = ["Agency", "City", "County", "State", "Technology", "Vendor"]
ROW = ["Fictional PD", "Town", "Fictional", "CA", "Automated License Plate Readers", "Acme"]


def write(header, rows=(), raw=None):
    handle = tempfile.NamedTemporaryFile("wb", suffix=".csv", delete=False)
    if raw is None:
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
        raw = out.getvalue().encode("utf-8")
    handle.write(raw)
    handle.close()
    return handle.name


def error_of(header, rows=()):
    with unittest.TestCase().assertRaises(AtlasError) as ctx:
        kit_atlas.read_atlas_csv(write(header, rows))
    return str(ctx.exception)


class Counting(str):
    calls = 0

    def __eq__(self, other):
        type(self).calls += 1
        return str.__eq__(self, other)

    __hash__ = str.__hash__


class HeaderTests(unittest.TestCase):
    def comparisons(self, extras):
        header = [Counting(name) for name in BASE + ["x%d" % i for i in range(extras)]]
        Counting.calls = 0
        with mock.patch.object(kit_atlas, "_records", return_value=[(1, header)]):
            self.assertEqual(kit_atlas.read_atlas_csv(write(BASE)), [])
        return Counting.calls, len(header)

    def test_distinct_header_work_is_linear_not_one_full_scan_per_name(self):
        for extras in (256, 512):
            calls, width = self.comparisons(extras)
            self.assertLessEqual(calls, 30 * width, (extras, calls))

    def test_real_wide_extra_header_is_accepted_without_new_cap(self):
        extras = ["x%d" % i for i in range(16000)] + ["", "Ünï"]
        header = list(reversed(BASE)) + extras
        cells = list(reversed(ROW)) + ["z"] * len(extras)
        self.assertEqual(kit_atlas.read_atlas_csv(write(header)), [])
        rows = kit_atlas.read_atlas_csv(write(header, [cells]))
        self.assertEqual(rows, [{"line": 2, "Agency".lower(): "Fictional PD", "city": "Town", "county": "Fictional",
                                 "state": "CA", "technology": "Automated License Plate Readers", "vendor": "Acme"}])

    def test_duplicate_choice_uses_first_occurrence_not_first_repeat(self):
        self.assertEqual(error_of(BASE + ["State", "Agency"]), "Atlas CSV: duplicate column: Agency")
        self.assertEqual(error_of(["State", "Agency", "City", "County", "Technology", "Vendor", "Agency", "State"]),
                         "Atlas CSV: duplicate column: State")
        self.assertEqual(error_of(["zz"] + BASE + ["State", "zz"]), "Atlas CSV: duplicate column: (an extra column)")
        self.assertEqual(error_of(BASE + ["zz", "zz", "City"]), "Atlas CSV: duplicate column: City")

    def test_header_refusal_priority_and_private_names(self):
        self.assertEqual(error_of(["Agency", "SECRET-COL", "SECRET-COL"]), "Atlas CSV: duplicate column: (an extra column)")
        self.assertEqual(error_of(["Agency", "Company", "Vendor", "Agency"]), "Atlas CSV: duplicate column: Agency")
        self.assertTrue(error_of(["Agency", "Agency2"]).startswith("Atlas CSV: missing required column: City"))
        self.assertEqual(error_of(BASE + ["Company"]), "Atlas CSV: use one vendor column: Company or Vendor")
        self.assertEqual(error_of(BASE + ["", ""]), "Atlas CSV: duplicate column: (an extra column)")
        self.assertEqual(kit_atlas.read_atlas_csv(write(BASE + [""])), [])
        # earlier parser refusals still win over header validation
        bad = write(None, raw=b'Agency,Agency\n"unterminated\n')
        with self.assertRaisesRegex(AtlasError, "line 2: malformed quoting"):
            kit_atlas.read_atlas_csv(bad)
        with self.assertRaisesRegex(AtlasError, "NUL"):
            kit_atlas.read_atlas_csv(write(None, raw=b"Agency,Agency\x00\n"))

    def test_company_vendor_and_required_index_mapping_are_unchanged(self):
        header = ["Technology", "Company", "State", "agency", "County", "City", "Agency"]
        cells = ["T", "V", "CA", "ignored", "C", "Town", "A"]
        rows = kit_atlas.read_atlas_csv(write(header, [cells]))
        self.assertEqual(rows, [{"line": 2, "technology": "T", "vendor": "V", "state": "CA", "county": "C",
                                 "city": "Town", "agency": "A"}])
        rows = kit_atlas.read_atlas_csv(write(BASE[:5], [ROW[:5]]))
        self.assertEqual(rows[0]["vendor"], "")


if __name__ == "__main__":
    unittest.main()
