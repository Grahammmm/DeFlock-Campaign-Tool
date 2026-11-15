"""DF2007-15: the advisory wording tier (`intent`, `undated-count`) beside the legal-claim guard.

Advice only: warnings name the place and the category with a fixed fix wording, never the text; they never enter
``legal_claims`` (so no refusal path can see them) and add nothing to text that has no advisory hit.
"""
import importlib.util
import json
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import legal_claims as lc

ROOT = Path(__file__).resolve().parents[1]
CORPUS = json.loads((ROOT / "tests" / "fixtures" / "flock" / "legal_claim_corpus.json").read_text(encoding="utf-8"))
FIX_INTENT = "Describe what the record says; do not infer intent."
FIX_COUNT = "Add a date or describe the count as documented or mapped."
GUARD_ERROR = "warning: wording advisory (guard-error). Fix: Review the wording manually."


def adv(text):
    return lc.legal_advisories(text)


class IntentTest(unittest.TestCase):
    def test_each_intent_phrase_requires_agency_or_official(self):
        positives = [
            "The agency secretly shared data.", "The Sheriff COVERTLY shared data.", "The police hid the readers.",
            "The city is hiding the contract.", "City staff covered up the error.", "The county did it in secret.",
            "The county did it IN   SECRET.", "The council covered up the vote.", "The mayor Covered   Up the vote.",
            "Sheriff Smith's office hid the logs.", "Morro Bay Police Department secretly kept a copy.",
            "The agencies covertly traded data.", "The chief secretly shared data.", "The officers hid the readers.",
            "An official hid the readers.", "The data was secretly shared by the sheriff.",
            "The error was covered up by city officials.", "The readers were hidden in secret by the department.",
            "The agency not only hid the readers but also shared data.",   # "not only" is emphasis, not negation
            "The agency collected plates and secretly shared them.",        # compound predicate of one subject
        ]
        for text in positives:
            with self.subTest(text=text):
                self.assertEqual(adv(text), ["intent"])
        negatives = [
            "A camera that hid a plate.", "A resident hiding a bicycle.", "He secretly smiled.", "Hiding is not the point.",
            "Hidden cameras exist.", "The agency can share data.", "The agency shared data.", "The plate was covered up by a tree.",
            "The agency shared data. It secretly kept a copy.",             # no cross-sentence pronoun identity
            "The agency shared data.\nIt hid a copy.",
            "The agencyx hid it.", "Secretly, a plate was shown.", "A hidden-camera show.",
        ]
        for text in negatives:
            with self.subTest(text=text):
                self.assertEqual(adv(text), [])

    def test_no_wider_synonym_list(self):
        for text in ("The agency concealed the readers.", "The agency quietly shared data.", "The agency hides the readers.",
                     "The agency covered the readers.", "The agency kept it hidden.", "The agency withheld the readers."):
            with self.subTest(text=text):
                self.assertEqual(adv(text), [])

    def test_quote_masks_and_adjacent_scope(self):
        quoted = [
            'The agency said "we secretly shared data" last week.', "The agency said “we hid it” last week.",
            "The agency said ‘we covered up the logs’ last week.", "The agency said 'we hid the readers' last week.",
            'The record reads "The sheriff covertly shared data."',
            'The agency wrote "we did it in secret" in the log.',
        ]
        for text in quoted:
            with self.subTest(text=text):
                self.assertEqual(adv(text), [])
        # an unquoted allegation next to a quotation still warns, in either order and across a newline
        for text in ('"Nothing to see." The agency secretly shared data.', 'The agency said "all fine" but the sheriff secretly shared data.',
                     'The record says "no violation".\nThe sheriff hid the readers.', "‘Open records’, yet the county hid the logs."):
            with self.subTest(text=text):
                self.assertEqual(adv(text), ["intent"])
        # apostrophes inside words are not quotation marks
        self.assertEqual(adv("The sheriff's staff aren't shy: the agency's staff hid the readers."), ["intent"])
        self.assertEqual(adv("It's the city's choice; the city's staff secretly shared data."), ["intent"])

    def test_complete_quotations_with_inner_apostrophe_or_newline_are_masked(self):
        quoted = [
            "'The agency's staff secretly shared data.'", "The record reads 'The agency's staff secretly shared data.' in full.",
            "The record reads ‘The agency’s staff covertly shared data.’ in full.",
            '"The agency secretly shared\ndata from 40 cameras."', "“The agency secretly shared\ndata from 40 cameras.”",
            "'The agency secretly shared\ndata from 40 cameras.'", "‘The agency hid\nthe readers.’",
            'The log says "The sheriff\'s office secretly shared data\nwith 40 cameras." and nothing else.',
        ]
        for text in quoted:
            with self.subTest(text=text):
                self.assertEqual(adv(text), [])
        # the sentence boundary and line breaks survive the mask: an unquoted claim after or beside the span still warns
        for text, expected in (
                ("'The agency's staff secretly shared data.' The sheriff hid the readers.", ["intent"]),
                ('"The agency secretly shared\ndata."\nThe sheriff hid the readers.', ["intent"]),
                ('"Quoted line one\nline two." There are 40 cameras.', ["undated-count"]),
                ("‘The agency’s staff hid it.’ The county did it in secret.", ["intent"])):
            with self.subTest(text=text):
                self.assertEqual(adv(text), expected)
        # a quotation never runs over a blank line, so a stray mark cannot hide later text
        self.assertEqual(adv('She said "we did it.\n\nThe agency secretly shared data.'), ["intent"])
        self.assertEqual(adv("The agency's staff secretly shared data."), ["intent"])

    def test_negation_capability_and_clause_boundaries(self):
        silent = [
            "The agency did not secretly share data.", "The agency never hid the readers.", "No agency covertly shared data.",
            "The agency didn't covertly share data.",
            "The agency can share data.", "The agency can choose to share data.",
            "If the agency secretly shared data, we will ask.", "Whether the agency hid the readers is unknown.",
            "Did the agency covertly share data?", "According to counsel, the agency secretly shared data.",
            "The complaint alleges the city hid the readers.", "The sheriff claims that staff hid nothing in secret.",
        ]
        for text in silent:
            with self.subTest(text=text):
                self.assertEqual(adv(text), [])
        warns = [
            "The agency can secretly share data.",                    # a capability hedge does not substantiate secrecy
            "The agency can choose to covertly share data.",
            "The agency did not respond to us last week and then secretly shared data.",   # distant denial
            "The agency did not respond and secretly shared data.",    # unrelated denial in an earlier clause
            "The city has no records, and its staff hid the readers.",
            "If asked, the agency secretly shared data.",              # a leading condition covers only its own segment
            "Whether we ask or not, the sheriff covertly shared data.",
            "The agency hid the readers, according to nobody.",         # the claim comes before the attribution cue
            "The agency alleges nothing and the sheriff hid the readers.",   # a joined affirmative claim is a new statement
        ]
        for text in warns:
            with self.subTest(text=text):
                self.assertEqual(adv(text), ["intent"])


class UndatedCountTest(unittest.TestCase):
    def test_count_qualification_and_dates(self):
        warns = [
            "There are 40 cameras.", "There are 1,200 cameras.", "About 2.5 ALPRs per mile.", "There are 40 READERS.", "40 ALPRs.",
            "The agency can share data from 40 cameras.",               # a capability hedge is not a date
            "There are 2026 cameras.",                                   # the count itself is not also a year
            "There are 2,026 cameras.", "In March 2026 cameras were counted: 2026 cameras.",
            "There are 40 cameras. It is documented elsewhere.",         # a cue in another sentence qualifies nothing
            "There are 40 cameras.\nAs of 2026-01-05 we checked.",
            "As of 2026-01-05 we checked. There are 40 cameras.",
            "There are 40 cameras on 2026-02-30.", "There are 40 cameras on 2026-02-29.", "There are 40 cameras on 2026-13-01.",
            "There are 40 cameras on 13/45/2026.", "There are 40 cameras on February 30, 2026.", "There are 40 cameras on 2/29/2026.",
            "In 1899 there were 40 cameras.", "In 2100 there were 40 cameras.", "There are 40 cameras per https://example.org/2026-01-01.",
            "There are 40 cameras (see www.example.org/2026).",
            'The note says "as of 2026-01-01" and there are 40 cameras.',   # quoted cue/date cannot qualify an unquoted claim
            'The vendor calls it "mapped" and there are 40 cameras.',
            "There were 3 cameras in the city and 12 ALPRs outside it.", "There are 40 cameras. As of 2026 it is 40 cameras.",
            "There are 40 cameras on Mar. 5, 2026.",           # the sentence contract ends a sentence at 'Mar.': documented limit
            "The agency can say it has 40 cameras.", "We counted 40 cameras, 7 readers, 12 ALPRs.",
        ]
        for text in warns:
            with self.subTest(text=text):
                self.assertEqual(adv(text), ["undated-count"])
        silent = [
            "There are 40 cameras as of 2026-01-05.", "As of January 2026 there are 40 cameras.", "There are 40 cameras, documented by the city.",
            "40 cameras were mapped by volunteers.", "Volunteers mapped 40 cameras.", "There are 40 cameras on 2026-01-05.",
            "There are 40 cameras on 1/5/2026.", "There are 40 cameras on 01/05/2026.", "There are 40 cameras on March 5, 2026.",
            "There are 40 cameras on March 5 2026.", "There are 40 cameras on Mar 5th, 2026.", "There are 40 cameras in March 2026.",
            "There are 40 cameras in Sept 2026.", "In 2026 there are 40 cameras.", "In 1999 there were 40 cameras.",
            "In 2099 there will be 40 cameras.", "There are 40 cameras on 2024-02-29.",          # a leap day is a real date
            "There were 40 cameras on 2026-01-05 and 12 ALPRs.", "mapped 40 cameras",
            "As of the last survey 12 ALPRs and 40 cameras.", "There are 40 cameras and 12 ALPRs as of 2026-01-05.",
        ]
        for text in silent:
            with self.subTest(text=text):
                self.assertEqual(adv(text), [])

    def test_shapes_nouns_negation_and_quotes(self):
        silent = [
            "There are not 40 cameras.", "There are no 40 cameras.", "There never were 40 cameras.", "There aren't 40 cameras.",
            "There are forty cameras.", "There is 1 camera.", "There are 40 camera.", "There are 40-60 cameras.",
            "There are 40 license plate readers.", "There are 40 cameras-worth of data.", "There are 40 readerships.",
            "There are 40cameras.", "We have many cameras.", "The 40 ALPR program.",
            'The city says "40 cameras" exist.', "The city says “there are 40 cameras” today.", "The city says ‘40 ALPRs’ exist.",
            "The city says '12 cameras' exist.",
            "Cameras: none.",
        ]
        for text in silent:
            with self.subTest(text=text):
                self.assertEqual(adv(text), [])
        # an unquoted count next to a quoted record still warns; not only is not a negation
        self.assertEqual(adv('The city says "all mapped". There are 40 cameras.'), ["undated-count"])
        self.assertEqual(adv("There are not only 40 cameras but also 3 ALPRs."), ["undated-count"])
        self.assertEqual(adv("The county's 40 cameras\nand 5 readers."), ["undated-count"])

    def test_no_clock_dependency(self):
        with mock.patch("campaign_tool.legal_claims.datetime") as clock:
            clock.date.side_effect = lambda *a: __import__("datetime").date(*a)
            self.assertEqual(adv("In 2026 there are 40 cameras."), [])
            self.assertEqual(adv("There are 40 cameras."), ["undated-count"])
            self.assertEqual(clock.date.today.called, False)


class ApiTest(unittest.TestCase):
    def test_both_categories_sorted_unique_and_non_string_input(self):
        text = "The agency secretly shared data from 40 cameras. The sheriff hid 12 ALPRs and the city hid more."
        self.assertEqual(adv(text), ["intent", "undated-count"])
        for value in (None, "", 7, b"x", ["The agency secretly shared data."], {"a": 1}):
            self.assertEqual(adv(value), [])
        self.assertEqual(lc.advisory_notes([("a", None), ("b", ""), ("c", 12)]), [])

    def test_notes_exact_wording_order_and_no_text(self):
        canary = "ZQXCANARY-777"
        items = [("line 2", "The agency secretly shared data from 40 cameras at " + canary + "."), ("line 1", "Clean text."),
                 ("line 3", "There are 12 ALPRs.")]
        notes = lc.advisory_notes(items)
        self.assertEqual(notes, [
            "warning: line 2: wording advisory (intent). Fix: " + FIX_INTENT,
            "warning: line 2: wording advisory (undated-count). Fix: " + FIX_COUNT,
            "warning: line 3: wording advisory (undated-count). Fix: " + FIX_COUNT])
        blob = "\n".join(notes)
        for leaked in (canary, "40", "12", "agency", "secretly"):
            self.assertNotIn(leaked, blob)
        self.assertEqual(lc.ADVISORY_GUARD_ERROR, GUARD_ERROR)

    def test_one_shot_iterables_are_read_once(self):
        def gen():
            yield ("x", "Agency X violated SB 34.")
            yield ("y", "There are 40 cameras.")
        self.assertEqual(lc.legal_notes(gen()), ["review-required: x: legal-claim wording (violation-verb)",
                                                 "warning: y: wording advisory (undated-count). Fix: " + FIX_COUNT])
        self.assertEqual(lc.advisory_notes(iter([("y", "There are 40 cameras.")])),
                         ["warning: y: wording advisory (undated-count). Fix: " + FIX_COUNT])

    def test_legal_notes_keep_legal_notes_first_then_advisories(self):
        items = [("a", "There are 40 cameras."), ("b", "Agency X violated SB 34."), ("c", "The sheriff secretly shared data."),
                 ("d", "The department is non-compliant and hid the readers.")]
        notes = lc.legal_notes(items)
        self.assertEqual(notes[:2], ["review-required: b: legal-claim wording (violation-verb)",
                                     "review-required: d: legal-claim wording (non-compliant)"])
        self.assertEqual([n.split(": ")[1] for n in notes[2:]], ["a", "c", "d"])
        self.assertTrue(all(n.startswith("warning: ") for n in notes[2:]))

    def test_guard_fault_is_an_advisory_and_keeps_the_legal_notes(self):
        with mock.patch.object(lc, "_blank_quotes", side_effect=RuntimeError("SECRETDETAIL")):
            self.assertEqual(adv("The agency secretly shared data."), ["guard-error"])
            self.assertEqual(adv(""), [])
            self.assertEqual(lc.advisory_notes([("a", "x"), ("b", "y")]), [GUARD_ERROR])      # once, no text, no exception text
            notes = lc.legal_notes([("a", "Agency X violated SB 34."), ("b", "Clean.")])
            self.assertEqual(notes, ["review-required: a: legal-claim wording (violation-verb)", GUARD_ERROR])
            self.assertEqual(lc.legal_claims("The agency secretly shared data."), [])           # the legal scanner is independent
        with mock.patch.object(lc, "legal_advisories", side_effect=RuntimeError("boom")):
            self.assertEqual(lc.advisory_notes([("a", "x")]), [GUARD_ERROR])
            self.assertEqual(lc.legal_notes([("a", "Agency X violated SB 34.")]),
                             ["review-required: a: legal-claim wording (violation-verb)", GUARD_ERROR])
        self.assertEqual(lc.advisory_notes(5), [GUARD_ERROR])                                   # not iterable
        self.assertEqual(lc.advisory_notes([("only-a-place",)]), [GUARD_ERROR])                # malformed item

    def test_tools_flock_privacy_exposes_the_tier_beside_not_through_legal_claims(self):
        spec = importlib.util.spec_from_file_location("_df2007_privacy", ROOT / "tools" / "flock_privacy.py")
        privacy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(privacy)
        self.assertEqual(privacy.legal_advisories("There are 40 cameras."), ["undated-count"])
        self.assertEqual(privacy.advisory_notes([("p", "The sheriff hid the logs.")]),
                         ["warning: p: wording advisory (intent). Fix: " + FIX_INTENT])
        self.assertEqual(privacy.legal_claims("The sheriff secretly hid 40 cameras."), [])
        self.assertEqual(privacy.categories("The sheriff secretly hid 40 cameras."), [])
        self.assertEqual(privacy.gated_notes([("p", "The sheriff secretly hid 40 cameras.")], __import__("re").compile("violat", 2)), [])


class RefusalInvarianceTest(unittest.TestCase):
    def test_legacy_refusals_and_feature_absent_bytes(self):
        # the old rule names are unchanged and no advisory name ever reaches the legal scanner
        self.assertEqual({name for name, _ in lc.RULES}, {"violation-verb", "violation-noun", "broke-the-law", "non-compliant", "is-illegal"})
        for text in ("The agency secretly shared data.", "The sheriff covertly shared data.", "There are 40 cameras.",
                     "The agency hid the readers.", "The agency can share data."):
            self.assertEqual(lc.legal_claims(text), [], text)
        self.assertEqual(lc.legal_claims("Agency X violated SB 34."), ["violation-verb"])
        self.assertEqual(lc.legal_claims("The agency secretly violated SB 34 at 40 cameras."), ["violation-verb"])
        # the whole old corpus: same legal notes as the pre-change expression, and no extra bytes where no advisory hit exists
        for expected, texts in (("flag", CORPUS["flag"]), ("pass", CORPUS["pass"])):
            for text in texts:
                legacy = ["review-required: %s: legal-claim wording (%s)" % ("here", ",".join(rules))] if (rules := lc.legal_claims(text)) else []
                notes = lc.legal_notes([("here", text)])
                self.assertEqual(notes[:len(legacy)], legacy, text)
                if not adv(text):
                    self.assertEqual(notes, legacy, text)
                else:
                    self.assertTrue(all(n.startswith("warning: here: wording advisory (") for n in notes[len(legacy):]), text)
                self.assertEqual(bool(legacy), expected == "flag", text)
        # guard-error behaviour of the legacy path is byte-identical
        self.assertEqual(lc.legal_notes([("a",)]), ["review-required: legal-claim wording (guard-error)"])
        self.assertEqual(lc.legal_notes(5), ["review-required: legal-claim wording (guard-error)"])
        self.assertEqual(lc.legal_notes([]), [])
        with mock.patch.object(lc, "SENTENCE_SPLIT", None):
            self.assertEqual(lc.legal_notes([("a", "x")]), ["review-required: a: legal-claim wording (guard-error)"])

    def test_wrappers_inherit_the_warnings_without_changing_their_clean_output(self):
        entries = [{"answer_md": "There are 40 cameras."}, {"answer_md": "SB 34 requires a policy."}]
        self.assertEqual(lc.faq_notes(entries), ["warning: faq.json.entries[0].answer_md: wording advisory (undated-count). Fix: " + FIX_COUNT])
        self.assertEqual(lc.faq_notes([entries[1]]), [])
        self.assertEqual(lc.elsewhere_notes([{"as_stated": "The sheriff hid the logs."}]),
                         ["warning: elsewhere.json[0].as_stated: wording advisory (intent). Fix: " + FIX_INTENT])
        self.assertEqual(lc.statement_notes([{"context_note": "There are 3 ALPRs."}]),
                         ["warning: statements.json[0].context_note: wording advisory (undated-count). Fix: " + FIX_COUNT])
        self.assertEqual(lc.elsewhere_notes([{"as_stated": "The agency can share data."}]), [])

    def test_documentation_names_the_tier_and_the_fixed_wording(self):
        doc = (ROOT / "docs" / "LEGAL-CLAIM-GUARD.md").read_text(encoding="utf-8")
        for needle in ("`intent`", "`undated-count`", FIX_INTENT, FIX_COUNT, GUARD_ERROR, "legal_advisories", "advisory_notes",
                       "never refuses", "exit code", "can secretly share", "tests/test_legal_claim_advisories.py"):
            self.assertIn(needle, doc)
        for old in ("Alert-only.", "DF2037-02", "quote-gated wording", "## Generated surfaces and coverage"):
            self.assertIn(old, doc)


class NotLawTest(unittest.TestCase):
    EXAMPLES = (
        ("sb-274-2025", "SB 274 requires a retention limit.", "SB 274 was vetoed."),
        ("sb-1013-2025", "SB 1013 bans sharing ALPR data.", "SB 1013 was not chaptered as of 2026-11-09."),
        ("carpenter-2018", "Carpenter requires a warrant for ALPR.", "Carpenter requires a warrant for cell-site records."),
        ("yang-2020", "Yang held ALPR is a search.", "Yang concerns standing on expired-rental facts."),
    )
    FIXES = (
        "SB 274 was vetoed; say what it would have done and that it is not law",
        "SB 1013 was not chaptered as of 2026-11-09; describe it as a bill, not law, and re-check its official status",
        "Carpenter concerns cell-site records, not an ALPR holding; do not say it requires an ALPR warrant or held ALPR is a search",
        "Yang is a standing holding on its facts; do not say it requires an ALPR warrant or held ALPR is a search",
    )

    def note(self, index, where="draft"):
        return "warning: %s: wording advisory (not-law). Fix: %s" % (where, self.FIXES[index])

    def test_not_law_positive_and_negative_per_table_row(self):
        for i, (_, positive, negative) in enumerate(self.EXAMPLES):
            for text in (positive, positive.swapcase()):
                with self.subTest(text=text):
                    self.assertEqual(adv(text), ["not-law"])
                    self.assertEqual(lc.advisory_notes([("draft", text)]), [self.note(i)])
                    self.assertEqual(lc.legal_claims(text), [])
            self.assertEqual(adv(negative), [])
        for label in ("SB 274", "sb274", "SB\t1013", "SB1013"):
            for predicate in ("requires", "limits", "bans", "is the law"):
                self.assertEqual(adv(label + " on ALPR " + predicate + " retention."), ["not-law"])
        for name in ("Carpenter", "Carpenter v. United States", "Yang", "United States v. Yang"):
            for predicate in ("requires a warrant for ALPR", "held ALPR is a search", "held that ALPR is a search"):
                self.assertEqual(adv(name + " " + predicate + "."), ["not-law"])
        for text in ("SB 34 requires a policy.", "SB 2740 requires retention.", "SB10130 bans sharing.",
                     "Carpenters held ALPR is a search.", "Yangs held ALPR is a search.",
                     "SB 274 prohibits sharing.", "SB 1013 mandates retention.", "See Carpenter and Yang.",
                     "SB 274; requires retention.", "SB 1013. It bans sharing."):
            self.assertEqual(adv(text), [], text)

    def test_not_law_quoted_and_conditional_wording(self):
        for _, positive, _ in self.EXAMPLES:
            for left, right in (('"', '"'), ('“', '”'), ("'", "'"), ('‘', '’')):
                self.assertEqual(adv(left + positive + right), [])
                self.assertEqual(adv(left + "The agency's note: " + positive.replace(" ", "\n", 1) + right), [])
                self.assertEqual(adv(left + "pending" + right + " " + positive), ["not-law"])
                self.assertEqual(adv(left + positive + right + " " + positive), ["not-law"])
            for prefix in ("If ", "Whether ", "Unless ", "According to counsel, ", "The complaint alleges ",
                           "Proposed ", "Pending "):
                self.assertEqual(adv(prefix + positive), [], prefix + positive)
            self.assertEqual(adv(positive.rstrip(".") + "?"), [])
        for text in ("SB 274 would have required retention.", "SB 1013 would have banned sharing.",
                     "SB 274 would have limits on sharing.", "SB 1013 is proposed and requires retention.",
                     "Carpenter would have held ALPR is a search.", "Yang never held ALPR is a search.",
                     "Yang has not held that ALPR is a search.", "SB 274 never requires retention.",
                     "SB 1013 does not limit sharing.", 'SB 274 "requires retention".',
                     'Yang "held ALPR is a search".'):
            self.assertEqual(adv(text), [], text)

    def test_not_law_scope_is_per_claim_not_document(self):
        warns = (
            "Pending review. SB 274 requires retention.",
            "SB 1013 is pending, but SB 274 requires retention.",
            "SB 1013 would have banned sharing and SB 274 requires retention.",
            "The agency did not respond and SB 274 requires retention.",
            "SB 274 requires retention if asked.",
            "According to counsel, SB 1013 bans sharing, and SB 274 requires retention.",
        )
        for text in warns:
            self.assertEqual(lc.advisory_notes([("draft", text)]), [self.note(0)], text)
        for text in ("SB 274 is pending and the agency requires retention.",
                     "Carpenter concerns cell-site records and Yang requires a warrant for cell-site records.",
                     "SB 274 is pending; SB 34 requires retention.",
                     "If SB 274 requires retention and SB 1013 bans sharing, we will ask."):
            self.assertEqual(adv(text), [], text)
        self.assertEqual(lc.advisory_notes([("draft", "SB 274 is pending and SB 1013 bans sharing.")]), [self.note(1)])

    def test_not_law_qualification_spans_citations_in_one_clause(self):
        for text in (
            "We ask whether SB 274 or SB 1013 bans sharing.",
            "We ask if SB 274 or SB 1013 bans sharing.",
            "The complaint alleges SB 274 or SB 1013 bans sharing.",
            "No SB 274 or SB 1013 bans sharing.",
            "Neither case is settled: not Carpenter or Yang held ALPR is a search.",
        ):
            with self.subTest(text=text):
                self.assertEqual(adv(text), [])
                self.assertEqual(lc.advisory_notes([("draft", text)]), [])
        for text in (
            "We ask whether SB 274 bans sharing; SB 1013 bans sharing.",
            "The complaint alleges SB 274 bans sharing but SB 1013 bans sharing.",
            "No SB 274 bans sharing, SB 1013 bans sharing.",
        ):
            with self.subTest(text=text):
                self.assertEqual(lc.advisory_notes([("draft", text)]), [self.note(1)])

    def test_not_law_sorted_category_and_row_fix_dedup(self):
        text = " ".join(row[1] for row in reversed(self.EXAMPLES)) + " SB 274 limits retention. CANARY-992."
        mixed = "The agency secretly shared data from 40 cameras. " + text
        self.assertEqual(adv(mixed), ["intent", "not-law", "undated-count"])
        expected = ["warning: draft: wording advisory (intent). Fix: " + FIX_INTENT]
        expected += [self.note(i) for i in range(4)]
        expected += ["warning: draft: wording advisory (undated-count). Fix: " + FIX_COUNT, self.note(0, "next")]
        items = [("draft", mixed), ("next", self.EXAMPLES[0][1])]
        self.assertEqual(lc.advisory_notes(iter(items)), expected)
        self.assertEqual(lc.advisory_notes(iter(items)), expected)
        self.assertNotIn("CANARY-992", " ".join(expected))
        notes = lc.legal_notes(iter([("legacy", "Agency X violated SB 34."), *items]))
        self.assertEqual(notes, ["review-required: legacy: legal-claim wording (violation-verb)"] + expected)
        for value in (None, "", 7, b"SB274 requires retention", []):
            self.assertEqual(adv(value), [])

    def test_not_law_table_and_documentation_are_dated(self):
        import datetime
        table = lc.NOT_LAW_TABLE
        self.assertEqual([r["id"] for r in table], [r[0] for r in self.EXAMPLES])
        self.assertEqual([r["kind"] for r in table], ["bill", "bill", "case", "case"])
        self.assertEqual([r["fix"] for r in table], list(self.FIXES))
        doc = (ROOT / "docs" / "LEGAL-CLAIM-GUARD.md").read_text(encoding="utf-8")
        for row in table:
            self.assertEqual(datetime.date.fromisoformat(row["read_date"]).isoformat(), "2026-11-09")
            for key in ("id", "label", "what_it_is", "source_url", "read_date", "fix"):
                self.assertTrue(row[key])
                self.assertIn(row[key], doc)
            self.assertRegex(row["source_url"], r"^https://(?:leginfo\.legislature\.ca\.gov|www\.supremecourt\.gov|cdn\.ca9\.uscourts\.gov)/")
        self.assertIn("the table must be re-checked against the official status page before each use", doc)
        self.assertIn("reread the official opinion/holding", doc)

    def test_not_law_fault_preserves_old_legal_and_advisory_notes(self):
        with mock.patch.object(lc, "_not_law_rows", side_effect=RuntimeError("CANARY-992")):
            self.assertEqual(adv(self.EXAMPLES[0][1]), ["guard-error"])
            self.assertEqual(lc.legal_notes([("x", "Agency X violated SB 34."), ("y", self.EXAMPLES[0][1])]),
                             ["review-required: x: legal-claim wording (violation-verb)", GUARD_ERROR])
        with mock.patch.object(lc, "ADVISORY_FIX", {}):
            self.assertEqual(lc.legal_notes([("x", "Agency X violated SB 34. The sheriff hid the logs.")]),
                             ["review-required: x: legal-claim wording (violation-verb)", GUARD_ERROR])


if __name__ == "__main__":
    unittest.main()
