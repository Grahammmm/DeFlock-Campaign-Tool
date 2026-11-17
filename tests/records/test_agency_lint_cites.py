"""REC-LINT-CITE-1: synthetic drafts and a synthetic package only; no network."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

from campaign_tool.records.agency_run import lint_cites

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = {"records_law": {"citation": "Gov. Code § 7920.000 et seq.",
                           "sources": [{"title": "Gov. Code § 7922.535 (clock)"}]},
           "rules": [{"citation": "Civ. Code § 1798.90.5"}]}
CANARY = "ZQXCANARY"


class LintCitesTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.pkg = self.dir / "package.json"
        self.pkg.write_text(json.dumps(PACKAGE), encoding="utf-8")

    def run_lint(self, text, *extra, package=None):
        draft = self.dir / "draft.md"
        draft.write_text(text, encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = lint_cites.main(["--draft", str(draft), "--package", str(package or self.pkg), *extra])
        return code, out.getvalue(), err.getvalue()

    def codes(self, text):
        code, out, _ = self.run_lint(text, "--json")
        return code, [(h["line"], h["code"]) for h in json.loads(out)["hits"]]

    def test_each_failure_once_gives_one_hit_each(self):
        text = "\n".join([
            "Gov. Code § 7922.535 sets the clock.",                      # 1 missing label
            "Gov. Code § 7922.600 applies, VERIFIED-IN-PACKAGE.",        # 2 mismatch
            "See Gov. Code § 7920.000 et seq. VERIFIED-IN-PACKAGE.",              # 3 et seq
            "We wrote on 2026-03-04 about it.",                          # 4 unsourced
            "",
            "The reply is overdue.",                                     # 6 overdue
        ])
        code, hits = self.codes(text)
        self.assertEqual(code, 1)
        self.assertEqual(hits, [(1, "C1_label_missing"), (2, "C2_label_mismatch"),
                                (3, "C3_et_seq"), (4, "C4_unsourced_fact"), (6, "C5_overdue_wording")])

    def test_clean_file_exits_zero(self):
        text = "\n".join([
            "Gov. Code § 7922.535 sets the clock, VERIFIED-IN-PACKAGE.",
            "Gov. Code § 7922.600 is cited UNVERIFIED.",
            "The 2026-03-04 letter is recorded in D3 p.10 and events.tsv.",
            "On our records the reply is overdue; this is not a legal finding.",
        ])
        code, out, _ = self.run_lint(text)
        self.assertEqual(code, 0, out)

    def test_label_direction_both_ways(self):
        text = ("Gov. Code § 7922.600 VERIFIED-IN-PACKAGE.\nGov. Code § 7922.535 UNVERIFIED.\n"
                "Civ. Code § 1798.90.5(a) VERIFIED-IN-PACKAGE.\n")
        self.assertEqual(self.codes(text)[1], [(1, "C2_label_mismatch"), (2, "C2_label_mismatch")])

    def test_wrapped_citation_labels_and_boundaries(self):
        c1, c2 = "C1_label_missing", "C2_label_mismatch"
        cases = [
            ("Gov. Code § 7922.535\nVERIFIED-IN-PACKAGE.", []),
            ("Gov. Code § 7922.535\nUNVERIFIED.", [(1, c2)]),
            ("Gov. Code § 7922.600\nVERIFIED-IN-PACKAGE.", [(1, c2)]),
            ("Gov. Code § 7922.535 is cited.\nVERIFIED-IN-PACKAGE.", [(1, c1)]),
            ("- Gov. Code § 7922.535\n- VERIFIED-IN-PACKAGE.", [(1, c1)]),
            ("Gov. Code § 7922.535\n\nVERIFIED-IN-PACKAGE.", [(1, c1)]),
            ("Gov. Code § 7922.535\nGov. Code § 7922.600 UNVERIFIED.", [(1, c1)]),
            ("Gov. Code § 7922.535\n" + "x" * 79 + " VERIFIED-IN-PACKAGE", [(1, c1)]),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual([hit for hit in self.codes(text)[1] if hit[1].startswith(('C1_', 'C2_'))], expected)

    def test_et_seq_and_ff_variants(self):
        text = "et seq\net seq.\nsee 5 ff.\nthe staff. Ff here\n```\net seq. ff.\n```\n"
        self.assertEqual([h for h in self.codes(text)[1] if h[1] == "C3_et_seq"],
                         [(1, "C3_et_seq"), (2, "C3_et_seq"), (3, "C3_et_seq")])

    def test_fact_sentence_boundaries(self):
        self.assertEqual(self.codes("Sent 2026-03-04 INFERRED.")[0], 0)
        self.assertEqual(self.codes("We hold 12 of them.")[1], [(1, "C4_unsourced_fact")])
        self.assertEqual(self.codes("Gov. Code § 1798.90.51 UNVERIFIED.")[0], 0)
        self.assertEqual(self.codes("- Fine.\n- Got 44 items.\n")[1], [(2, "C4_unsourced_fact")])

    def test_wrapped_fact_sentence_locations_and_boundaries(self):
        c4 = "C4_unsourced_fact"
        cases = [
            ("We received five\ndocuments.", [(1, c4)]),
            ("The letter was sent March\n4.", [(1, c4)]),
            ("We sent 12 documents\naccording to D3.", []),
            ("Introduction. We received\nfive documents. Then D3.", [(2, c4)]),
            ("- We received 12 documents\n- D3 confirms it.", [(1, c4)]),
            ("We received 12 documents\n\nD3 confirms it.", [(1, c4)]),
            ("We received 12 documents\n```\nD3\n```\nD3.", [(1, c4)]),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual([hit for hit in self.codes(text)[1] if hit[1] == c4], expected)

    def test_overdue_whole_word_and_paragraph(self):
        self.assertEqual(self.codes("We translate the latest note.")[0], 0)
        self.assertEqual(self.codes("It is past due.\nOn our records. Also not a legal finding.")[0], 0)
        self.assertEqual(self.codes("It is late.\nOn our records only.")[1], [(1, "C5_overdue_wording")])

    def test_prints_place_and_rule_never_text(self):
        text = f"We saw 2026-03-04 {CANARY} here.\n"
        _, out, err = self.run_lint(text)
        _, jout, _ = self.run_lint(text, "--json")
        self.assertIn("draft line 1: C4_unsourced_fact", out)
        self.assertNotIn(CANARY, out + err + jout)

    def test_refusals_and_guards(self):
        bad = self.dir / "bad.json"
        bad.write_text("{}", encoding="utf-8")
        code, _, err = self.run_lint("x\n", package=bad)
        self.assertEqual((code, err.strip()), (2, "agency-lint-cites: package_invalid"))
        self.assertEqual(self.run_lint("Gov. Code § 7922.535 UNVERIFIED? no.\n", package=self.dir)[0], 1)
        target = self.dir / "target.md"
        target.write_text("x\n", encoding="utf-8")
        link = self.dir / "link.md"
        link.symlink_to(target)
        big = self.dir / "big.md"
        big.write_bytes(b"a" * (512 * 1024 + 1))
        for path in (link, big):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(lint_cites.main(["--draft", str(path), "--package", str(self.pkg)]), 2)
            self.assertIn("draft_invalid", err.getvalue())
        before = (self.pkg.read_bytes(), os.stat(self.pkg).st_mtime_ns)
        self.run_lint("x\n")
        self.assertEqual(before, (self.pkg.read_bytes(), os.stat(self.pkg).st_mtime_ns))

    def test_repo_us_ca_package_loads(self):
        code, out, err = self.run_lint("Hello.\n", package=ROOT / "jurisdictions" / "us-ca")
        self.assertEqual((code, err), (0, ""), out)


if __name__ == "__main__":
    unittest.main()
