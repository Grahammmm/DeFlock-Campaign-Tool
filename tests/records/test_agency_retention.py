import contextlib
import csv
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from campaign_tool.records import agency_retention as ret

ROOT = Path(__file__).resolve().parents[2]


def row(**changes):
    return dict(dict(agency="alpha-pd", system="ALPR", vendor="flock", data_layer="detections",
                     stated_period="30 days", period_days="30", bound="exact", source_kind="order_form",
                     quote="Synthetic detections are retained for 30 days.", doc_sha256="a" * 64,
                     page="1", confidence="medium", checked="UNVERIFIED"), **changes)


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "retention.csv"

    def write(self, rows, header=None):
        with self.path.open("w", newline="") as out:
            writer = csv.writer(out)
            writer.writerow(ret.HEADER if header is None else header)
            writer.writerows([[r[k] for k in ret.HEADER] for r in rows])
        return ret.load(self.path)

    def test_shipped_rows_preserve_existing_citations(self):
        rows, problems = ret.load(ROOT / "data/slo-pilot/agency-retention.csv")
        self.assertEqual(problems, [])
        with (ROOT / "data/slo-pilot/agency-policies.csv").open(newline="") as src:
            originals = list(csv.DictReader(src))
        keys = lambda r: tuple(r[k] for k in ("agency", "quote", "doc_sha256", "page"))
        expected = {keys(r) for r in originals if "minimum of one year" in r["quote"]}
        self.assertTrue(expected)
        self.assertEqual({keys(r) for r in rows}, expected)
        for r in rows:
            self.assertEqual((r["source_kind"], r["confidence"], r["checked"], r["vendor"]),
                             ("policy", "medium", "UNVERIFIED", "unknown"))

    def test_row_problems(self):
        cases = [(field, "bogus") for field in ret.ENUMS]
        cases += [("quote", "q" * 401), ("stated_period", "p" * 81), ("doc_sha256", "bad"),
                  ("page", "0"), ("page", "1.2"), ("period_days", "-1"), ("period_days", "1.5"),
                  ("confidence", "high"), ("system", ""), ("agency", "")]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                rows, problems = self.write([row(**{field: value})])
                self.assertEqual(rows, [])
                self.assertTrue(problems)
                self.assertTrue(all(p["line"] == 2 for p in problems))
        self.assertFalse(self.write([row(confidence="high", checked="VERIFIED-ORIGINAL")])[1])
        self.assertFalse(self.write([row(period_days="", bound="unknown")])[1])

    def test_duplicate_key_and_valid_rows_survive(self):
        rows, problems = self.write([row(), row(page="01", doc_sha256="A" * 64), row(system="other")])
        self.assertEqual(len(rows), 2)
        self.assertEqual(problems, [{"line": 3, "field": "row", "reason": "duplicate key"}])

    def test_file_failures(self):
        self.assertTrue(ret.load(self.path)[1])
        self.assertTrue(self.write([], header=["wrong"])[1])
        for text in (",".join(ret.HEADER) + '\n"unterminated',
                     ",".join(ret.HEADER) + "\nwrong,width\n"):
            self.path.write_text(text)
            self.assertTrue(ret.load(self.path)[1])
        self.path.write_bytes(b"\xff")
        self.assertTrue(ret.load(self.path)[1])
        link = self.path.with_name("link.csv")
        link.symlink_to(self.path)
        self.assertTrue(ret.load(link)[1])

    def test_summary_keeps_sources_and_filters_agency(self):
        rows = [row(), row(data_layer="downloaded_data"), row(agency="beta-pd")]
        grouped = ret.summary(rows, "alpha-pd")
        self.assertEqual(grouped, {"detections": [rows[0]], "downloaded_data": [rows[1]]})
        self.assertEqual(ret.summary(rows, "absent"), {})

    def test_comparison_and_cautious_wording(self):
        policy = row(source_kind="policy", vendor="unknown", stated_period="minimum of one year",
                     period_days="365", bound="min", doc_sha256="b" * 64, page="2")
        scope = ret.compare([row(), dict(policy, data_layer="downloaded_data")], "alpha-pd")
        self.assertEqual(scope, ["flock detections: 30 days (order form). Policy, downloaded_data: minimum of one year. These cover different data; they differ in scope, not necessarily in conflict."])
        longer = ret.compare([row(), policy], "alpha-pd")
        self.assertEqual(longer, ["Policy period is longer than the Flock 30-day default for this data. Whether data is archived is not shown by these documents."])
        exact = ret.compare([row(), dict(policy, bound="exact")], "alpha-pd")
        self.assertIn(f"Different stated periods for the same data: 30 days ({'a' * 64} p.1) and minimum of one year ({'b' * 64} p.2). Review.", exact)
        self.assertEqual(ret.compare([], "alpha-pd"), ["No retention statement on file."])
        for lines in (scope, longer, exact, ret.compare([], "alpha-pd")):
            self.assertFalse(any(word in " ".join(lines).lower() for word in ("violat", "illegal", "conflict with the law", "settle")))
        for change in ({"system": "other"}, {"agency": "beta-pd"}, {"period_days": ""}, {"bound": "max"}):
            self.assertEqual(ret.compare([row(), dict(policy, **change)], "alpha-pd"), [])
        self.assertEqual(ret.compare([row(), row(doc_sha256="b" * 64)], "alpha-pd"), [])
        self.assertEqual(ret.compare([row(data_layer="unspecified"), dict(policy, data_layer="unspecified")], "alpha-pd"), [])

    def test_cli_valid_and_invalid(self):
        self.write([row()])
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "agency-retention", str(self.path)],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1 valid rows, 0 problems", result.stdout)
        self.write([row(confidence="high")])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(ret.main([str(self.path)]), 1)
        self.assertIn("line 2 checked", output.getvalue())
