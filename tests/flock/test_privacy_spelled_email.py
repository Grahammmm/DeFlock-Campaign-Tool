"""RF1730-12: the shared privacy EMAIL pattern also catches bracketed spelled-out addresses (`jdoe [at] slo [dot] gov`), so the
report, page and publish lints refuse them by category name only, while ordinary prose is not flagged. Synthetic addresses only."""
import time, unittest

from tests.flock import test_privacy_patterns as base

privacy = base.privacy
SPELLED = ("jdoe [at] slo [dot] gov", "jdoe(at)slo(dot)gov", "JDOE {AT} MorroBayCA {DOT} Gov", "jdoe@slo[dot]gov",
           "j.doe [ at ] co.slo.ca.us", "jdoe [at] slo.ca.gov",
           # bypass by wording: no spaces, mixed brackets, padded brackets, many labels, extra subdomain before [dot]
           "jdoe[AT]slo[DOT]gov", "jdoe (at) slo [dot] gov", "jdoe {  at  } slo ( dot ) gov",
           "jdoe [at] co [dot] slo [dot] ca [dot] us", "jdoe@mail.slo(dot)ca(dot)gov", "Write to jdoe [at] slo [dot] gov today.")
NOT_SPELLED = ("meet at city hall", "jdoe at slo dot gov", "Section 3 [a]", "see [at] the meeting", "(at) 5pm", "@handle",
               "user@localhost", "price [dot] 5",
               # near misses: no tld, 1-letter tld, words instead of brackets, a bracketed word that is not at/dot
               "jdoe [at] slo", "jdoe [at] slo [dot] g", "jdoe [at] slo dot gov", "jdoe [a] slo [dot] gov",
               "jdoe [att] slo [dot] gov", "jdoe@slo", "jdoe@slo [dotted] gov")


class SpelledEmailTest(unittest.TestCase):
    # Reuse the shared lists' entry points exactly as test_privacy_patterns.py drives them (story built once per class).
    setUpClass, tearDownClass = base.PrivacyPatternsTest.__dict__["setUpClass"], base.PrivacyPatternsTest.__dict__["tearDownClass"]
    report_lint, page_lint, publish_lint = (base.PrivacyPatternsTest.report_lint, base.PrivacyPatternsTest.page_lint,
                                            base.PrivacyPatternsTest.publish_lint)
    lints = base.PrivacyPatternsTest.lints

    def test_spelled_forms_match_and_prose_does_not(self):
        for text in SPELLED:
            with self.subTest(text=text):
                self.assertIsNotNone(privacy.EMAIL.search(text))
                self.assertEqual(privacy.categories(text), ["email"])
        for text in NOT_SPELLED + ("[at] slo [dot] gov",):  # no local part (only standalone: lints prefix a word)
            with self.subTest(missed=text):
                self.assertIsNone(privacy.EMAIL.search(text))
                self.assertEqual(privacy.categories(text), [])

    def test_existing_plain_lists_behave_identically(self):
        for text in base.EMAILS:
            with self.subTest(text=text):
                self.assertEqual(privacy.EMAIL.search(text).group(0), text)
        for text in base.NOT_EMAILS + base.PHONES + base.NOT_PHONES + base.PLATES + base.NOT_PLATES:
            with self.subTest(missed=text):
                self.assertIsNone(privacy.EMAIL.search(text))

    def test_one_shared_object_and_categories_unchanged(self):
        self.assertEqual([name for name, _ in privacy.PATTERNS], ["email", "phone", "plate"])
        self.assertIs(privacy.PATTERNS[0][1], privacy.EMAIL)
        self.assertEqual(privacy.categories("jdoe [at] slo [dot] gov 805-555-1234"), ["email", "phone"])
        self.assertEqual(privacy.categories("jdoe@slo[dot]gov 7ABC123"), ["email", "plate"])

    def test_every_lint_refuses_spelled_addresses_without_echo(self):
        for name, lint, line in self.lints("email"):
            for text in SPELLED:
                with self.subTest(lint=name, text=text):
                    errs = lint(text)
                    self.assertIn(line, errs)
                    self.assertNotIn("jdoe", "\n".join(errs).lower())
            for text in NOT_SPELLED:
                with self.subTest(lint=name, missed=text):
                    self.assertNotIn(line, lint(text))

    def test_report_lint_refuses_inside_a_citation_group(self):
        errs = self.report_lint("Reads are kept [jdoe [at] slo [dot] gov].")
        self.assertIn("line 1: email-like string", errs)
        self.assertNotIn("jdoe", "\n".join(errs))

    def test_long_bracket_lines_finish_fast(self):
        for line in (("[at] [dot] " * 20000)[:200_000], ("(at){dot}" * 25000)[:200_000], ("a [at] b [dot] " * 14000)[:200_000],
                     "x@" + ("a [dot] " * 25000)[:200_000]):
            with self.subTest(start=line[:12]):
                began = time.monotonic()
                privacy.EMAIL.search(line)
                privacy.categories(line)
                self.assertLess(time.monotonic() - began, 1.0)


if __name__ == "__main__":
    unittest.main()
