"""Redaction never leaks a plate from a table of synthetic plates, in prose or in cells."""
import itertools
import json
import random
import unittest

from campaign_tool.digest.redact import redact
from campaign_tool.digest.build import redact_units


def synthetic_plates(n=50, seed=7):
    rng = random.Random(seed)
    letters = "ABCDEFGHJKLMNPRSTUVWXYZ"  # pragma: allowlist secret (plate alphabet, synthetic)
    plates = set()
    while len(plates) < n:
        style = rng.choice(("ca", "abc1234", "mixed"))
        if style == "ca":
            plates.add(f"{rng.randint(1, 9)}{''.join(rng.choice(letters) for _ in range(3))}{rng.randint(100, 999)}")
        elif style == "abc1234":
            plates.add(f"{''.join(rng.choice(letters) for _ in range(3))}{rng.randint(1000, 9999)}")
        else:
            chars = [rng.choice(letters) for _ in range(4)] + [str(rng.randint(0, 9)) for _ in range(3)]
            rng.shuffle(chars)
            plates.add("".join(chars))
    return sorted(plates)


class RedactTableTests(unittest.TestCase):
    def test_no_plate_survives_in_prose_or_table(self):
        plates = synthetic_plates()
        self.assertEqual(len(plates), 50)
        prose = "Synthetic search log. " + " ".join(f"Hit on plate {p} at 03:00;" for p in plates)
        lower = " ".join(p.lower() for p in plates)
        spaced = " ".join(p[:3] + " " + p[3:] if p[:3].isalpha() and p[3:].isdigit() else p for p in plates)
        table = "\n".join(f"{i},{p},vehicle_search,fictional" for i, p in enumerate(plates, 1))
        for blob in (prose, lower, spaced, table):
            result = redact(blob)
            for plate in plates:
                self.assertNotIn(plate, result.text, plate)
                self.assertNotIn(plate.lower(), result.text.lower(), plate)
            self.assertGreaterEqual(result.counts.get("PLATE", 0), 50)

    def test_units_share_one_placeholder_table(self):
        plates = synthetic_plates(5)
        units = [{"locator": {"sheet": "log", "row": i}, "text": json.dumps([str(i), p, "search"])} for i, p in enumerate(plates, 1)]
        units.append({"locator": {"page": 1}, "text": "Summary mentions " + plates[0] + " twice: " + plates[0]})
        redacted, counts = redact_units(units)
        self.assertEqual(len(redacted), len(units))
        self.assertEqual(counts["PLATE"], 7)
        self.assertIn("[PLATE-1]", redacted[0]["text"])
        self.assertEqual(redacted[-1]["text"].count("[PLATE-1]"), 2)
        for unit in redacted:
            for plate in plates:
                self.assertNotIn(plate, unit["text"])
        self.assertEqual([u["locator"] for u in redacted], [u["locator"] for u in units])

    def test_citations_and_ordinary_words_survive(self):
        text = "Civ. Code 1798.90.51(b) and Veh. Code 2413; SB 34; retained 60 days; 2026-09-30; Example County; version demo-2"
        result = redact(text)
        self.assertEqual(result.text, text)
        self.assertEqual(result.total, 0)

    def test_labelled_and_titled_names_are_redacted(self):
        text = "Requester: John Smith wrote. Sincerely, Maria Lopez. cc: Ann Lee. Officer Dan Jones and Mr. Lee reviewed."
        out = redact(text).text
        for name in ("John Smith", "Maria Lopez", "Ann Lee", "Dan Jones", "Mr. Lee"):
            self.assertNotIn(name, out)
        self.assertIn("wrote", out)
        self.assertIn("reviewed", out)
        self.assertEqual(redact(text).counts["NAME"], 5)

    def test_existing_placeholders_are_reserved_never_reused(self):
        out = redact("[PLATE-1] seen; plate 7ABC123 and [PLATE-1] again").text
        self.assertEqual(out, "[PLATE-1] seen; plate [PLATE-2] and [PLATE-1] again")
        twice = redact(out).text
        self.assertEqual(twice, out, "re-running over redacted text changes nothing")

    def test_units_are_redacted_separately_and_locators_never_carry_names(self):
        units = [{"text": "meet at 123", "locator": {"page": 1}},
                 {"text": "Main Street tomorrow", "locator": {"page": 2, "member": "Deputy_Smith_7ABC123.csv", "scope": "attachment"}}]
        redacted, counts = redact_units(units)
        self.assertEqual([u["text"] for u in redacted], ["meet at 123", "Main Street tomorrow"])
        self.assertEqual([u["locator"] for u in redacted], [{"page": 1}, {"page": 2, "member": "Deputy_Smith_7ABC123.csv", "scope": "attachment"}])
        self.assertEqual([u["model_locator"] for u in redacted], [{"unit": 0}, {"unit": 1}])

    def test_no_mapping_is_exposed(self):
        result = redact("plate 7ABC123")
        self.assertEqual(set(vars(result)), {"text", "counts"})
        self.assertNotIn("7ABC123", repr(result))

    def test_multiline_names_preserve_physical_line_numbers(self):
        separators = ("\n", "\r\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")
        for separator in separators:
            text = "Deputy Alice" + separator + "Smith arrived." + separator + "Data retained for 30 days."
            with self.subTest(separator=repr(separator)):
                result = redact(text)
                self.assertEqual(result.text, "Deputy [NAME-1]" + separator + "  arrived." + separator + "Data retained for 30 days.")
                self.assertEqual(result.counts, {"NAME": 1})
                self.assertEqual(result.text.splitlines()[2], "Data retained for 30 days.")
                self.assertEqual(redact(result.text).text, result.text)

    def test_multiline_denylist_preserves_each_original_separator(self):
        phrase = "Synthetic\nPrivate\r\nPhrase"
        text = "Start " + phrase + " ends.\nEvidence follows."
        result = redact(text, denylist=(phrase,))
        self.assertEqual(result.text, "Start [DENYLIST-1]\n \r\n  ends.\nEvidence follows.")
        self.assertEqual(result.counts, {"DENYLIST": 1})
        self.assertEqual(len(result.text.splitlines()), len(text.splitlines()))
        self.assertNotIn("Private", result.text)
        self.assertEqual(redact(result.text, denylist=(phrase,)).text, result.text)

    def test_multiline_redaction_does_not_coalesce_distinct_cr_and_lf(self):
        for prefix, phrase, denylist in (("Name: ", "Alice\rBeth\nSmith", ()),
                                        ("", "Synthetic\rPrivate\nPhrase", ("Synthetic\rPrivate\nPhrase",))):
            text = prefix + phrase + " left.\nEvidence follows."
            with self.subTest(prefix=prefix):
                result = redact(text, denylist=denylist)
                self.assertEqual(len(result.text.splitlines()), 4)
                self.assertEqual(result.text.splitlines()[3], "Evidence follows.")
                self.assertIn("\r \n ", result.text)
                self.assertEqual(redact(result.text, denylist=denylist).text, result.text)
                self.assertEqual(result.total, 1)

    def test_final_redacted_continuation_line_remains_present(self):
        result = redact("Deputy Alice\nSmith")
        self.assertEqual(result.text, "Deputy [NAME-1]\n ")
        self.assertEqual(len(result.text.splitlines()), 2)
        self.assertEqual(redact(result.text).text, result.text)
        # A separator that originally ended the span needs no extra line.
        result = redact("Synthetic\n", denylist=("Synthetic\n",))
        self.assertEqual(result.text, "[DENYLIST-1]\n")
        self.assertEqual(len(result.text.splitlines()), 1)

    def test_denylist_span_cannot_split_crlf_at_its_boundary(self):
        for text, phrase in (("Prefix\r\nSynthetic", "\nSynthetic"),
                             ("Synthetic\r\nSuffix", "Synthetic\r")):
            with self.subTest(text=text):
                result = redact(text, denylist=(phrase,))
                self.assertIn("\r\n", result.text)
                self.assertEqual(len(result.text.splitlines()), len(text.splitlines()))
                self.assertNotIn("Synthetic", result.text)
                self.assertEqual(redact(result.text, denylist=(phrase,)).text, result.text)

    def test_all_mixed_separator_pairs_preserve_following_evidence_line(self):
        separators = ("\n", "\r\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")
        for first, second in itertools.product(separators, repeat=2):
            phrase = "Synthetic" + first + "Private" + second + "Phrase"
            text = phrase + " ends.\nEvidence follows."
            with self.subTest(first=repr(first), second=repr(second)):
                result = redact(text, denylist=(phrase,))
                self.assertEqual(len(result.text.splitlines()), len(text.splitlines()))
                self.assertEqual(result.text.splitlines()[3], "Evidence follows.")


if __name__ == "__main__":
    unittest.main()
