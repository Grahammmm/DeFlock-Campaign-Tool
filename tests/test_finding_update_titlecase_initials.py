"""RF1500-15: an adjacent pair whose initial is a Unicode titlecase letter (category Lt, e.g. U+01C5) is refused in
update notes like an uppercase-initial pair. Synthetic text only; no network.

Run from the repository root: python3 -B -m unittest tests.test_finding_update_titlecase_initials -q
"""
import json
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import finding_updates, findings  # noqa: E402
from campaign_tool.content import ContentError, load_content  # noqa: E402
from campaign_tool.newsletter.draft import DraftError, build_draft  # noqa: E402
from tests import test_finding_updates as base  # noqa: E402  (module import: not re-collected here)

PRIVATE = "update note contains private information"
LT_PAIRS = ("\u01c5uro \u01c5uri\u0107 answered.",  # both initials Lt (U+01C5)
            "\u01c5uro Smith answered.", "Jane \u01c5uri\u0107 answered.",  # Lt beside an uppercase initial
            "\u01c8ubo \u01cbeno answered.",  # U+01C8 and U+01CB
            "the reply from \u01c5uro-Smith O\u2019\u01c5uri\u0107 arrived.",  # hyphen and apostrophe words
            "\u01c5uro\u00a0\u01c5uri\u0107 answered.", "\u01c5uro\u2003\u01c5uri\u0107 answered.",  # non-ASCII spaces
            "\u01c5uro Z\u030civko answered.")  # decomposed accent: only the NFC view joins "Zivko" with a caron
UPPER_PAIRS = ("Jane Doe answered.", "Jos\u00e9 Garc\u00eda wrote back.", "Jose\u0301 Garci\u0301a wrote back.",
               "Jos\u00e9 Garci\u0301a wrote back.", "O'Brien-Smith Lopez attended.", "Sheriff Office answered.",
               "Officer Smith answered.", "reply came from someone@example.org")
ACCEPTED = ("\u01c5uro answered by letter.",  # one Lt word alone
            "\u01c6uro \u01c6uri\u0107 answered.",  # lower-case digraph pair (U+01C6)
            "\u01c4URO \u01c4URI\u0106 answered.", "JANE DOE answered.",  # whole words in capitals
            "\u01c5uro, \u01c5uri\u0107 and the agency answered.",  # punctuation-separated
            "\u01c5 \u01c5uri\u0107 answered.",  # single-character word
            "the agency answered by le\u0301tter.", "  \u01c5uro\u00a0answered\u2003by letter.  ")
CLI_OK = "\u01c5uro answered by letter."


class TitlecaseInitialTests(unittest.TestCase):
    def refuses(self, note, label="L[0].note"):
        with self.assertRaises(finding_updates.UpdateError) as caught:
            finding_updates.validate_update_note(note, label)
        self.assertEqual(str(caught.exception), f"{label}: {PRIVATE}")

    def test_lt_initial_in_either_or_both_adjacent_words_refuses(self):
        for note in LT_PAIRS:
            with self.subTest(note=note):
                self.refuses(note)
                with self.assertRaises(finding_updates.UpdateError) as caught:
                    finding_updates.load_updates([{"date": "2026-03-05", "note": base.PLAIN},
                                                  {"date": "2026-03-05", "note": note}],
                                                 "2026-03-04T05:06:07Z", today=date(2026, 3, 10), label="L")
                self.assertEqual(str(caught.exception), f"L[1].note: {PRIVATE}")

    def test_existing_uppercase_and_canonical_refusals_are_monotone(self):
        for note in UPPER_PAIRS:
            with self.subTest(note=note):
                self.refuses(note)

    def test_nonpairs_and_case_controls_keep_exact_text(self):
        for note in ACCEPTED:
            with self.subTest(note=note):
                self.assertEqual(finding_updates.validate_update_note(note, "n"), note)
        loaded = finding_updates.load_updates([{"date": "2026-03-05", "note": n} for n in ACCEPTED],
                                              "2026-03-04T05:06:07Z", today=date(2026, 3, 10), label="L")
        self.assertEqual([u["note"] for u in loaded], list(ACCEPTED))

    def test_plaintext_and_raw_length_errors_still_take_precedence(self):
        plain = "update note must be 1-400 plain-text characters"
        pair = LT_PAIRS[0]
        for note in (None, 7, "", "   ", pair + "\n", pair + "\u200b", "<b>" + pair + "</b>", pair + " " + "\u00e9" * 380):
            with self.subTest(note=note), self.assertRaises(finding_updates.UpdateError) as caught:
                finding_updates.validate_update_note(note, "n")
            self.assertEqual(str(caught.exception), f"n: {plain}")
        ok = "\u01c5uro " + "\u00e9" * 395
        self.assertEqual((len(ok), finding_updates.validate_update_note(ok, "n")), (400, ok))
        with self.assertRaises(finding_updates.UpdateError) as caught:
            finding_updates.validate_update_note(ok + "\u00e9", "n")
        self.assertEqual(str(caught.exception), f"n: {plain}")
        with self.assertRaises(finding_updates.UpdateError) as caught:  # an invalid date is reported before the note
            finding_updates.load_updates([{"date": "2026-02-30", "note": pair}], "2026-03-04T05:06:07Z",
                                         today=date(2026, 3, 10), label="L")
        self.assertEqual(str(caught.exception), "L[0].date: must be a real YYYY-MM-DD date")


class TitlecaseStoredTests(base.FindingUpdateTests):
    """Reuses the synthetic published-finding fixture; its inherited tests are removed so they do not run twice."""
    for _name in [n for n in vars(base.FindingUpdateTests) if n.startswith("test_")]:
        locals()[_name] = None
    del _name

    def snapshot(self):
        return {str(p.relative_to(self.base)): p.read_bytes() for p in sorted(self.base.rglob("*")) if p.is_file()}

    def test_stored_finding_refuses_titlecase_pair_without_repair(self):
        self.update()
        kept = self.document()["updates"]
        self.path.write_text(json.dumps(dict(self.original, updates=kept + [{"date": "2026-03-06", "note": LT_PAIRS[0]}])),
                             encoding="utf-8")
        before = self.snapshot()
        with self.assertRaises(ContentError) as caught:
            load_content(self.root)
        self.assertIn(f"updates[1].note: {PRIVATE}", str(caught.exception))
        self.assertNotIn("uri", str(caught.exception))
        self.assertEqual(self.snapshot(), before)  # refused as stored: never rewritten, redacted or dropped
        self.assertEqual(self.document()["updates"][1]["note"], LT_PAIRS[0])
        self.path.write_text(json.dumps(dict(self.original, updates=kept + [{"date": "2026-03-06", "note": CLI_OK}])),
                             encoding="utf-8")
        load_content(self.root)

    def test_actual_update_cli_refusal_has_no_persistent_mutation(self):
        before = self.snapshot()
        for note in (LT_PAIRS[0], LT_PAIRS[1]):
            with self.subTest(note=note):
                result = self.run_cli("finding", "update", "syn-pub-1", "--directory", self.root, "--on", "2026-03-05",
                                      "--note", note, "--approved-by", "Pat Organizer")
                self.assertEqual((result.returncode, result.stdout), (1, ""))
                self.assertIn(f"--note: {PRIVATE}", result.stderr)
                for leak in ("uro", "uri", "\\u01c5", "Traceback", "updated"):
                    self.assertNotIn(leak, result.stderr)
                self.assertEqual(self.snapshot(), before)
        ok = self.run_cli("finding", "update", "syn-pub-1", "--directory", self.root, "--on", "2026-03-05",
                          "--note", CLI_OK, "--approved-by", "Pat Organizer")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout.splitlines()[0]), {"updated": "synthetic-title", "updates": 1})
        document = self.document()
        self.assertEqual(document["updates"], [{"date": "2026-03-05", "note": CLI_OK}])
        self.assertEqual(findings.reviewed_hash(document), findings.reviewed_hash(self.original))


class TitlecaseNewsletterTests(unittest.TestCase):
    NOW = datetime(2026, 11, 1, 20, 0, tzinfo=timezone.utc)

    def manifest(self, note):
        return {"schema_version": 1, "campaign": {"name": "Synthetic"}, "since": "2026-10-01T00:00:00+00:00",
                "updates": [{"title": "Synthetic finding", "path": "/findings/x.html", "date": "2026-10-10", "note": note}]}

    def test_explicit_newsletter_update_uses_shared_fixed_refusal(self):
        for note in (LT_PAIRS[0], LT_PAIRS[3]):
            with self.subTest(note=note), self.assertRaises(DraftError) as caught:
                build_draft(self.manifest(note), self.NOW)
            self.assertEqual(str(caught.exception), f"updates[0].note: {PRIVATE}")
        for note in (ACCEPTED[0], ACCEPTED[1]):
            with self.subTest(note=note):
                draft = build_draft(self.manifest(note), self.NOW)
                self.assertIn(f"updated 2026-10-10 \u2014 {note} /findings/x.html", draft["text"])
                self.assertEqual(draft["counts"]["updates"], 1)


if __name__ == "__main__":
    unittest.main()
