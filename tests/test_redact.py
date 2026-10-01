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

    def test_no_mapping_is_exposed(self):
        result = redact("plate 7ABC123")
        self.assertEqual(set(vars(result)), {"text", "counts"})
        self.assertNotIn("7ABC123", repr(result))


if __name__ == "__main__":
    unittest.main()
