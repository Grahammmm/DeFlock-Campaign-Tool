"""Agency policy register loader and validator, on synthetic agencies."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from campaign_tool.records import agency_policies as ap

SHA = "a" * 64
HEAD = ",".join(ap.HEADER)


def row(**kw):
    r = {"agency": "example-pd", "policy_id": "Policy 900", "section": "900.5(e)",
         "quote": "audits at the first of each year", "doc_sha256": SHA, "page": "2",
         "adopted_date": "2024-01-15"}
    r.update(kw)
    return ",".join('"' + r[c] + '"' for c in ap.HEADER)


class RegisterTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR")))

    def write(self, *lines, name="agency-policies.csv"):
        p = self.d / name
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    def test_valid_rows_load_and_lookup(self):
        p = self.write(HEAD, row(), row(agency="other-sheriff", section="1.1", adopted_date=""))
        rows, problems = ap.load(p)
        self.assertEqual(problems, [])
        self.assertEqual(len(rows), 2)
        self.assertEqual(ap.find(rows, "example-pd", "Policy 900", "900.5(e)")["page"], "2")  # type: ignore[index]
        self.assertIsNone(ap.find(rows, "example-pd", "Policy 900", "nope"))
        self.assertEqual(ap.sections(rows, "unknown"), [])
        self.assertEqual(ap.cite(rows[0]), "Policy 900 900.5(e), p. 2")

    def test_bad_header_refused(self):
        rows, problems = ap.load(self.write("agency,section", "x,y"))
        self.assertEqual(rows, [])
        self.assertEqual(problems[0]["field"], "header")

    def test_field_failures_name_line_and_field(self):
        p = self.write(HEAD, row(doc_sha256="XYZ"), row(page="0"), row(page="two"),
                       row(adopted_date="01/02/2024"), row(adopted_date="2999-01-01"),
                       row(agency="Bad Agency"), row(quote="q" * 401), row(section=""))
        rows, problems = ap.load(p)
        self.assertEqual(rows, [])
        got = {(x["line"], x["field"]) for x in problems}
        self.assertEqual(got, {(2, "doc_sha256"), (3, "page"), (4, "page"), (5, "adopted_date"),
                               (6, "adopted_date"), (7, "agency"), (8, "quote"), (9, "section")})

    def test_duplicate_and_wrong_width_and_blank_lines(self):
        p = self.write(HEAD, row(), "", row(), "a,b")
        rows, problems = ap.load(p)
        self.assertEqual(len(rows), 1)
        self.assertEqual([(x["line"], x["field"]) for x in problems], [(4, "row"), (5, "row")])
        self.assertIn("duplicate of line 2", problems[0]["reason"])

    def test_same_section_different_page_is_not_duplicate(self):
        rows, problems = ap.load(self.write(HEAD, row(), row(page="3")))
        self.assertEqual((len(rows), problems), (2, []))

    def test_missing_symlink_and_non_utf8_files(self):
        self.assertEqual(ap.load(self.d / "none.csv")[1][0]["field"], "file")
        real = self.write(HEAD, row())
        link = self.d / "link.csv"
        link.symlink_to(real)
        self.assertEqual(ap.load(link)[1][0]["reason"], "not a regular file")
        bad = self.d / "bad.csv"
        bad.write_bytes(b"\xff\xfe\x00")
        self.assertEqual(ap.load(bad)[0], [])

    def test_cli_exit_codes_and_json(self):
        good = self.write(HEAD, row())
        bad = self.write(HEAD, row(page="x"), name="bad.csv")
        self.assertEqual(ap.main([str(good)]), 0)
        self.assertEqual(ap.main([str(bad)]), 1)
        self.assertEqual(ap.report(good)["agencies"], ["example-pd"])
        json.dumps(ap.report(bad))

    def test_file_sha256(self):
        f = self.d / "doc.bin"
        f.write_bytes(b"abc")
        self.assertEqual(ap.file_sha256(f),
                         "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")


if __name__ == "__main__":
    unittest.main()
