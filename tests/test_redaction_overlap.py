"""Overlapping redaction matches remove their complete union (synthetic data only).

A later match that starts inside an earlier one and runs past its end used to
be discarded, so its uncovered suffix ("Carol", "-555-0100") survived into
redacted text and model input. These tests pin the union behaviour at the
redactor and at every model-input boundary that reuses it. No sockets, real
model or publication: the model transport is an in-memory opener.
"""
from copy import deepcopy
import io
import json
import unittest

from campaign_tool import law
from campaign_tool.digest.build import build_digest, redact_units
from campaign_tool.digest.model import ModelConfig
from campaign_tool.digest.model_input import ModelSources
from campaign_tool.digest.redact import LINE_SEPARATOR, Redaction, Redactor, _collect, redact

PRIVATE_LOCATOR = {"member": "Synthetic_Overlap_attachment.csv", "nested": {"path": "/private/overlap/file"}}
# Every str.splitlines() separator the redactor preserves, CRLF as one pair.
SEPARATORS = ["\n", "\r", "\r\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]


def coverage(intervals):
    return {position for start, end in intervals for position in range(start, end)}


def legacy_coverage(candidates):
    """The pre-fix resolver over the same sorted candidates (discard any later overlap)."""
    cursor, kept = 0, []
    for start, end in sorted(candidates, key=lambda s: (s[0], -(s[1] - s[0]))):
        if start >= cursor:
            kept.append((start, end))
            cursor = end
    return coverage(kept)


class OverlapUnionTests(unittest.TestCase):
    def assert_union(self, text, candidates, allow=(), deny=()):
        """Hand-computed oracle: chosen spans cover exactly the union of all candidates."""
        chosen = _collect(text, {a.lower() for a in allow}, list(deny))
        for (start, end, _category, value), following in zip(chosen, chosen[1:] + [None]):
            self.assertEqual(value, text[start:end])  # never a shorter member value
            if following is not None:
                self.assertLessEqual(end, following[0])  # disjoint, sorted components
        covered = coverage((start, end) for start, end, _c, _v in chosen)
        self.assertEqual(covered, coverage(candidates))
        self.assertLessEqual(legacy_coverage(candidates), covered)  # no new unredaction
        return chosen

    def test_overlap_pair_and_pattern_denylist_remove_full_suffix(self):
        cases = (("Alice Beth Carol", ["Alice Beth", "Beth Carol"], "[DENYLIST-1]", {"DENYLIST": 1},
                  [(0, 10), (6, 16)]),
                 ("contact +1 805-555-0100", ["contact +1 805"], "[DENYLIST-1]", {"DENYLIST": 1},
                  [(0, 14), (8, 23)]),
                 ("Officer Alice Beth Carol", ["Beth Carol"], "Officer [NAME-1]", {"NAME": 1},
                  [(8, 18), (14, 24)]))
        for text, deny, expected, counts, candidates in cases:
            with self.subTest(text=text):
                result = redact(text, denylist=deny)
                self.assertEqual(result.text, expected)
                self.assertEqual(result.counts, counts)
                self.assertNotIn("Carol", result.text)
                self.assertNotIn("555-0100", result.text)
                self.assert_union(text, candidates, deny=deny)

    def test_transitive_nested_equal_start_and_adjacent_components(self):
        cases = (
            # C overlaps B beyond A's end; B bridges A to C.
            ("Ann Bo Cy Di", ["Ann Bo", "Bo Cy", "Cy Di"], "[DENYLIST-1]", {"DENYLIST": 1},
             [(0, 6), (4, 9), (7, 12)]),
            # Fully contained member.
            ("Zed Alpha Yew", ["Alpha", "Zed Alpha Yew"], "[DENYLIST-1]", {"DENYLIST": 1}, [(0, 13), (4, 9)]),
            # Equal start: longest wins and the component keeps its full value.
            ("Zed Alpha here", ["Zed", "Zed Alpha"], "[DENYLIST-1] here", {"DENYLIST": 1}, [(0, 3), (0, 9)]),
            # Equal interval: earlier category wins on the tie.
            ("call 805-555-0100 now", ["805-555-0100"], "call [PHONE-1] now", {"PHONE": 1}, [(5, 17), (5, 17)]),
            # Disjoint and merely touching spans stay separate replacements.
            ("Ann Bo, Cy Di", ["Ann Bo", "Cy Di"], "[DENYLIST-1], [DENYLIST-2]", {"DENYLIST": 2},
             [(0, 6), (8, 13)]),
            ("AnnBo", ["Ann", "Bo"], "[DENYLIST-1][DENYLIST-2]", {"DENYLIST": 2}, [(0, 3), (3, 5)]),
        )
        for text, deny, expected, counts, candidates in cases:
            for order in (deny, list(reversed(deny))):
                with self.subTest(text=text, order=order):
                    result = redact(text, denylist=order)
                    self.assertEqual((result.text, result.counts), (expected, counts))
                    self.assert_union(text, candidates, deny=order)

    def test_allowlist_does_not_override_overlapping_denylist(self):
        text = "Officer Alice Beth Carol"
        self.assertEqual(redact("Officer Alice Beth", allowlist=["Alice Beth"]).text, "Officer Alice Beth")
        for allow in (["Alice Beth"], ["Alice Beth", "Beth Carol", "Alice Beth Carol"]):
            with self.subTest(allow=allow):
                result = redact(text, allowlist=allow, denylist=["Beth Carol"])
                self.assertEqual((result.text, result.counts), ("Officer Alice [DENYLIST-1]", {"DENYLIST": 1}))
                self.assert_union(text, [(14, 24)], allow=allow, deny=["Beth Carol"])
        for plain in ("", "Plain words only, nothing to remove."):
            result = redact(plain, denylist=["Beth Carol"])
            self.assertEqual((result.text, result.counts), (plain, {}))

    def test_union_token_identity_reserved_and_repeated_units(self):
        deny = ["Alice Beth", "Beth Carol"]
        shared = Redactor(denylist=deny)
        self.assertEqual(shared.redact("Alice Beth Carol").text, "[DENYLIST-1]")
        self.assertEqual(shared.redact("again Alice Beth Carol").text, "again [DENYLIST-1]")
        # A different union that shares the first member gets its own identity, in either order.
        self.assertEqual(shared.redact("Alice Beth Zed").text, "[DENYLIST-2] Zed")
        reverse = Redactor(denylist=deny)
        self.assertEqual(reverse.redact("Alice Beth Zed").text, "[DENYLIST-1] Zed")
        self.assertEqual(reverse.redact("Alice Beth Carol").text, "[DENYLIST-2]")
        # Literal placeholders are reserved, never reused for a new union.
        reserved = redact("[DENYLIST-1] then Alice Beth Carol", denylist=deny)
        self.assertEqual((reserved.text, reserved.counts), ("[DENYLIST-1] then [DENYLIST-2]", {"DENYLIST": 1}))
        again = redact(reserved.text, denylist=deny)
        self.assertEqual((again.text, again.counts), (reserved.text, {}))
        # The result carries text and counts only: no mapping, spans or values.
        self.assertEqual(set(vars(reserved)), {"text", "counts"})
        self.assertIsInstance(reserved, Redaction)
        self.assertNotIn("Carol", repr(reserved))
        original = [{"locator": {"page": n}, "text": text} for n, text in
                    enumerate(["Alice Beth Carol", "x Alice Beth Carol", "Alice Beth Zed"])]
        before = deepcopy(original)
        units, counts = redact_units(original, denylist=deny)
        self.assertEqual([u["text"] for u in units], ["[DENYLIST-1]", "x [DENYLIST-1]", "[DENYLIST-2] Zed"])
        self.assertEqual(counts, {"DENYLIST": 3})
        self.assertEqual(original, before)

    def test_multiline_overlap_keeps_original_line_provenance(self):
        tail = "Data retained for 30 days."
        for sep in SEPARATORS:
            text = f"Officer Alice{sep}Beth Carol{sep}{tail}"
            with self.subTest(sep=repr(sep)):
                result = redact(text, denylist=["Beth Carol"])
                self.assertEqual(result.text, f"Officer [NAME-1]{sep} {sep}{tail}")
                self.assertEqual(result.text.splitlines()[2], tail)
                self.assertEqual([m.group() for m in LINE_SEPARATOR.finditer(result.text)], [sep, sep])
        # Mixed CR/LF, and a component ending inside a CRLF pair: the pair stays intact.
        mixed = redact(f"Officer Alice\r\nBeth Carol\r{tail}", denylist=["Beth Carol"])
        self.assertEqual(mixed.text, f"Officer [NAME-1]\r\n \r{tail}")
        edge = redact(f"Officer Alice\r\nBeth Carol\r\n{tail}", denylist=["Beth Carol\r"])
        self.assertEqual(edge.text, f"Officer [NAME-1]\r\n \r\n{tail}")
        self.assertEqual(edge.text.splitlines(), ["Officer [NAME-1]", " ", tail])
        # Units are never concatenated: a phrase split across units does not match.
        units, counts = redact_units([{"text": "Alice Beth"}, {"text": "Carol"}], denylist=["Beth Carol"])
        self.assertEqual(([u["text"] for u in units], counts), (["Alice Beth", "Carol"], {}))

    def test_primary_and_challenge_payloads_do_not_expose_overlap_tails(self):
        package = law.load_package("us-ca")
        text = "Officer Alice Beth Carol\ncontact +1 805-555-0100\nData retained for 30 days."
        deny = ("Beth Carol", "contact +1 805")
        expected = "Officer [NAME-1]\n[DENYLIST-1]\nData retained for 30 days."
        leaks = ("Carol", "Beth", "555-0100", "Synthetic_Overlap", "/private/overlap")
        original = [{"locator": deepcopy(PRIVATE_LOCATOR), "text": text}]
        before = deepcopy(original)

        sources = ModelSources(original, denylist=deny)
        source_payload = sources.source_payload()
        self.assertEqual(source_payload["record_text"], ['{"unit": 0}\t' + expected])
        challenge = {"scope": "Officer Alice Beth Carol", "actors": ["Alice Beth Carol"],
                     "dates": [{"date": "contact +1 805-555-0100", "note": "Beth Carol",
                                "locator": deepcopy(PRIVATE_LOCATOR)}],
                     "statements": [{"text": "Officer Alice Beth Carol", "locator": deepcopy(PRIVATE_LOCATOR)}],
                     "omissions": [{"text": "contact +1 805-555-0100", "locator": {"scope": "document"}}],
                     "counterevidence": [{"text": "Alice Beth Carol", "locator": None}],
                     "conclusions": [{"text": "Officer Alice Beth Carol", "confidence": "needs_attorney_review",
                                      "sources": [{"locator": deepcopy(PRIVATE_LOCATOR), "rule_id": None}]}]}
        challenge_before = deepcopy(challenge)
        payload = sources.digest_payload(challenge)
        self.assertEqual(payload["scope"], "Officer [NAME-1]")
        self.assertEqual(payload["dates"][0]["date"], "[DENYLIST-1]")
        self.assertEqual(payload["statements"][0], {"text": "Officer [NAME-1]", "locator": {"unit": 0}})
        self.assertEqual(payload["omissions"][0]["text"], "[DENYLIST-1]")
        encoded = json.dumps([source_payload, payload])
        for leak in leaks:
            self.assertNotIn(leak, encoded)
        self.assertEqual(challenge, challenge_before)
        self.assertEqual(sources.restore_output(payload)["statements"][0]["locator"], PRIVATE_LOCATOR)

        captured = []

        def opener(request, **kwargs):
            captured.append(json.loads(request.data))
            content = json.dumps({"scope": "Synthetic source", "actors": [], "dates": [], "omissions": [],
                                  "counterevidence": [],
                                  "statements": [{"text": "Data retained.", "locator": {"unit": 0, "line": 3}}],
                                  "conclusions": [{"text": "Retention is described.", "confidence": "needs_attorney_review",
                                                   "sources": [{"locator": {"unit": 0, "line": 3}, "rule_id": None}]}]})
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": content}}]}).encode())

        digest = build_digest("a" * 64, original, package, "us-ca", "draft", "redacted_cloud",
                              model_config=ModelConfig("https://model.example.invalid/v1"), opener=opener,
                              denylist=deny)
        self.assertEqual(len(captured), 1)
        request = json.loads(captured[0]["messages"][1]["content"])
        self.assertEqual(request["record_text"], ['{"unit": 0}\t' + expected])
        retention = next(hit for hit in request["detector_hits"] if hit["detector"] == "retention_period")
        self.assertEqual(retention["locator"], {"unit": 0, "line": 3})
        for leak in leaks:
            self.assertNotIn(leak, json.dumps(captured))
        self.assertEqual(digest["redaction_counts"], {"NAME": 1, "DENYLIST": 1})
        self.assertEqual(digest["statements"][0]["locator"], {**PRIVATE_LOCATOR, "line": 3})
        self.assertEqual(original, before)


if __name__ == "__main__":
    unittest.main()
