import unittest

from campaign_tool.records.agency_run.lint_quotes import lint


class LintQuotesPartialWord(unittest.TestCase):
    def check_quote(self, quote, page_text, hits):
        pages = {("D1", 1): page_text}
        draft = f'"{quote}" (D1 p.1)'
        self.assertEqual(lint(draft, lambda doc, page: pages.get((doc, page))),
                         (hits, {"checked": 1, "messages": 0}))

    def test_rejects_partial_word_at_either_edge(self):
        # A substring alone does not establish that the quoted words appeared.
        for page in ("the response was completed.", "Lathe response was complete.",
                     "Lathe response was complete. The label reads: the response was completed."):
            with self.subTest(page=page):
                self.check_quote("the response was complete", page,
                                 [(1, 1, "Q1", "not_verbatim D1 p.1")])

    def test_accepts_exact_occurrence_next_to_punctuation(self):
        # A whitespace-only boundary check would reject valid punctuation.
        self.check_quote("the response was complete", "Note: (the response\nwas   complete).", [])

    def test_searches_past_partial_occurrences(self):
        # Stopping at the first occurrence, or skipping its full length, misses this overlap.
        self.check_quote("word a word", "sword a word a word.", [])
        self.check_quote("the response was complete",
                         "the response was completed. Note: (the response was complete).", [])

    def test_explicit_omission_preserves_substring_matching(self):
        # Applying the new edge checks to omission segments would break existing matches.
        for marker in ("[...]", "...", "…"):
            with self.subTest(marker=marker):
                self.check_quote(f"the response {marker} was complete",
                                 "the response eventually was completed.", [])
