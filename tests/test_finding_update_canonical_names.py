"""RF0640-1: canonically equivalent (NFC/NFD) spellings of an adjacent title-cased pair are refused in update notes.

Run from the repository root: python3 -B -m unittest tests.test_finding_update_canonical_names -q
"""
import json
import sys
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import finding_updates  # noqa: E402
from campaign_tool.content import ContentError, load_content  # noqa: E402
from tests import test_finding_updates as base  # noqa: E402  (module import: not re-collected here)

PRIVATE = "update note contains private information"
NFD_PAIR = "Jose\u0301 Garci\u0301a wrote back."
NFD_START = "E\u0301mile Dupont answered."
NFD_ONE_ACCENT = "O\u2019Brien-Smith Lo\u0301pez attended."
MIXED = "Jos\u00e9 Garci\u0301a wrote back."
ACCEPTED = ("the agency answered by le\u0301tter.", "Cafe\u0301 hours were mentioned by letter.",
            "Jose\u0301 answered by letter.", " the re\u0301sume\u0301 was sent. ")


class CanonicalNamePairTests(unittest.TestCase):
    def test_canonical_name_pairs_refuse_composed_decomposed_and_mixed_forms(self):
        for note in ("Jos\u00e9 Garc\u00eda wrote back.", NFD_PAIR, MIXED, "\u00c9mile Dupont answered.", NFD_START,
                     NFD_ONE_ACCENT, "Jane Doe replied by letter.", "Jose\u0301\u00a0Garci\u0301a wrote back."):
            with self.subTest(note=note), self.assertRaises(finding_updates.UpdateError) as caught:
                finding_updates.validate_update_note(note, "L[0].note")
            self.assertEqual(str(caught.exception), f"L[0].note: {PRIVATE}")

    def test_accepted_decomposed_text_is_not_normalized(self):
        for note in ACCEPTED:
            with self.subTest(note=note):
                self.assertEqual(finding_updates.validate_update_note(note, "n"), note)

    def test_raw_codepoint_limit_precedes_nfc_analysis(self):
        ok = "e\u0301" * 200
        self.assertEqual(finding_updates.validate_update_note(ok, "n"), ok)
        with self.assertRaises(finding_updates.UpdateError) as caught:
            finding_updates.validate_update_note("e\u0301" * 200 + "e", "n")
        self.assertIn("1-400 plain-text characters", str(caught.exception))

    def test_load_updates_refuses_the_same_pair_with_an_indexed_label(self):
        with self.assertRaises(finding_updates.UpdateError) as caught:
            finding_updates.load_updates([{"date": "2026-03-05", "note": base.PLAIN},
                                          {"date": "2026-03-05", "note": NFD_PAIR}],
                                         "2026-03-04T05:06:07Z", today=date(2026, 3, 10), label="L")
        self.assertEqual(str(caught.exception), f"L[1].note: {PRIVATE}")


class CanonicalNameStoredTests(base.FindingUpdateTests):
    """Reuses the synthetic published-finding fixture; its inherited tests are removed so they do not run twice."""
    for _name in [n for n in vars(base.FindingUpdateTests) if n.startswith("test_")]:
        locals()[_name] = None
    del _name

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in sorted(self.root.rglob("*")) if p.is_file()}

    def test_stored_content_refuses_the_canonical_pair_then_recovers(self):
        self.update()
        self.path.write_text(json.dumps(dict(self.original, updates=[{"date": "2026-03-05", "note": NFD_PAIR}])),
                             encoding="utf-8")
        with self.assertRaises(ContentError) as caught:
            load_content(self.root)
        self.assertIn(f"updates[0].note: {PRIVATE}", str(caught.exception))
        self.path.write_text(json.dumps(dict(self.original, updates=[{"date": "2026-03-05", "note": ACCEPTED[0]}])),
                             encoding="utf-8")
        load_content(self.root)

    def test_real_cli_refusal_has_no_output_or_persistent_mutation(self):
        before = self.snapshot()
        result = self.run_cli("finding", "update", "syn-pub-1", "--directory", self.root, "--on", "2026-03-05",
                              "--note", NFD_PAIR, "--approved-by", "Pat Organizer")
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn(f"--note: {PRIVATE}", result.stderr)
        self.assertNotIn("Garci", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.snapshot(), before)
        ok = self.run_cli("finding", "update", "syn-pub-1", "--directory", self.root, "--on", "2026-03-05",
                          "--note", ACCEPTED[0], "--approved-by", "Pat Organizer")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(self.document()["updates"], [{"date": "2026-03-05", "note": ACCEPTED[0]}])


if __name__ == "__main__":
    unittest.main()
