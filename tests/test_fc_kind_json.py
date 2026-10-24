"""FC-KIND-JSON: malformed Unicode / nonfinite JSON refusals in kind classification.

Synthetic-only. Every case runs through the real ``kind.classify`` API on exact
receipt bytes. Invalid payloads are repeated with a decisive title cue and with
no cue at all: refusal must not depend on whether a candidate would have been
produced. No filesystem, network, subprocess, real record or stage is used.
"""
import hashlib
import sys
import unittest
from unittest.mock import patch

from campaign_tool.records.understanding import kind

SUBJECT = hashlib.sha256(b"fc-kind-json synthetic original identity label").hexdigest()
RUN_ID = "fc-kind-json-synthetic-run"
# Stable refusal vocabulary this suite expects; fixed here, not read from kind.py.
CODES = {"invalid_json", "invalid_unicode_json", "nonfinite_json", "duplicate_json_key"}
LONE = ("\ud800", "\udbff", "\udc00", "\udfff")
OVERFLOW = (b"1e309", b"-1e309", b"1E400", b"-1.5e+999", b"123456789e305",
            b"1" + b"0" * 400 + b".0", b"-0.1e310")
FINITE = (b"1e308", b"-1e308", b"1e-400", b"-1e-400", b"2.5E+10", b"0.0", b"-0",
          repr(sys.float_info.max).encode(), b"5e-324", b"9" * 100, b"-12", b"3.25")
POSITIVES = {
    "search-log": "Search log", "sharing-list": "Sharing list",
    "network-search": "Network search", "retention-setting": "Retention settings",
    "audit": "Audit log", "request": "Public records request",
    "training-roster": "Training roster", "policy": "ALPR policy",
    "contract": "Service agreement", "correspondence": "Email correspondence",
    "invoice": "Invoice", "denial-or-extension": "Records request denial",
}
TOKEN = "@@FC-KIND-JSON-TOKEN@@"


def unit(text, *, locator=None, data=None, kind_name="pdf_page"):
    return {"kind": kind_name, "locator": {"page": 1} if locator is None else locator,
            "text": text, "data": {} if data is None else data}


def native(*units, status="complete", issues=None, **extra):
    receipt = {"schema": "format-extraction-v1", "original_sha256": SUBJECT,
               "original_changed": False, "review_status": "not_reviewed",
               "status": status, "units": list(units), "pages": [],
               "issues": [] if issues is None else issues,
               "route": {"route": "extract:pdf", "format": "pdf"},
               "parser_adapter": "synthetic-producer-not-qualified",
               "parser": "synthetic-fixture", "parser_version": "1"}
    receipt.update(extra)
    return receipt


def raw_of(receipt, token=None):
    """Canonical receipt bytes; a quoted TOKEN placeholder becomes a raw JSON token."""
    raw = kind.canonical_bytes(receipt)
    if token is not None:
        marker = ('"' + TOKEN + '"').encode()
        if raw.count(marker) != 1:
            raise ValueError("fixture must place exactly one token")
        raw = raw.replace(marker, token)
    return raw


def call(raw, **extra):
    return kind.classify(run_id=RUN_ID, artifact_sha256=SUBJECT,
                         derived_receipt_sha256=hashlib.sha256(raw).hexdigest(),
                         derived_receipt=raw, **extra)


def cue_and_no_cue(bad_unit=None, **extra):
    """(label, receipt, filename) with a decisive title and with no cue at all."""
    units = [] if bad_unit is None else [bad_unit]
    return (("title", native(unit("Search log\nsynthetic"), *units, **extra), "search-log.pdf"),
            ("no_cue", native(*units, **extra), "opaque.bin"))


class Refusals(unittest.TestCase):
    def refuse(self, raw, code, *, filename="", forbidden=()):
        self.assertIn(code, CODES)
        with self.assertRaises(kind.KindError) as raised:
            call(raw, filename=filename)
        error = raised.exception
        self.assertEqual(str(error), code)
        self.assertEqual(error.args, (code,))
        # Stable typed refusal only: no chained parser exception carrying payload.
        self.assertIsNone(error.__cause__)
        self.assertTrue(error.__context__ is None or error.__suppress_context__)
        for text in forbidden:
            self.assertNotIn(text, repr(error.args))

    def accept(self, raw, *, filename=""):
        output = call(raw, filename=filename)
        self.assertIn(output["kind"], kind.KINDS)
        self.assertIs(output["review_required"], True)
        for flag in ("qualified", "activation_allowed", "publication_ready"):
            self.assertIs(output[flag], False)
        self.assertEqual((output["stage_promotions"], output["semantic_assessment"]),
                         (0, "NOT_ASSESSED"))
        return output

    def expect_valid(self, label, output):
        if label == "title":
            self.assertEqual((output["kind"], output["reason"]), ("search-log", "explicit_cues"))
        else:
            self.assertEqual((output["kind"], output["confidence"], output["reason"]),
                             ("other", "low", "no_decisive_cues"))


class LoneSurrogateTests(Refusals):
    def test_lone_high_and_low_surrogate_in_unit_text(self):
        for lone in LONE:
            for text in ("Search log\nsynthetic " + lone, lone + " opaque", lone):
                for label, receipt, name in cue_and_no_cue(unit(text, locator={"page": 2})):
                    with self.subTest(lone=hex(ord(lone)), text=len(text), cue=label):
                        self.refuse(raw_of(receipt), "invalid_unicode_json", filename=name,
                                    forbidden=(lone, "synthetic", "opaque"))
            # The surrogate-bearing unit itself carries the decisive title.
            self.refuse(raw_of(native(unit("Search log\n" + lone))), "invalid_unicode_json",
                        filename="search-log.pdf")

    def test_lone_surrogate_in_locator_key_and_value(self):
        for lone in LONE:
            for locator in ({"page": 1, lone: 1}, {"page": 1, "tag": lone},
                            {"page": 2, "nested": {"k": [lone]}}):
                for label, receipt, name in cue_and_no_cue(unit("opaque", locator=locator)):
                    with self.subTest(lone=hex(ord(lone)), locator=sorted(map(repr, locator)), cue=label):
                        self.refuse(raw_of(receipt), "invalid_unicode_json", filename=name)

    def test_lone_surrogate_in_nested_data_key_and_value(self):
        for lone in LONE:
            for data in ({"cells": [{"cell": "A1", "value": lone, "type": "s"}]},
                         {"a": {"b": {lone: True}}}, {"list": [[["x", lone]]]}):
                for label, receipt, name in cue_and_no_cue(unit("opaque", data=data)):
                    with self.subTest(lone=hex(ord(lone)), cue=label):
                        self.refuse(raw_of(receipt), "invalid_unicode_json", filename=name)

    def test_lone_surrogate_anywhere_else_in_the_receipt(self):
        lone = "\udc00"
        for extra in ({"parser": lone}, {"issues_note": [lone]}, {lone: "x"},
                      {"route": {"route": lone, "format": "pdf"}}):
            for label, receipt, name in cue_and_no_cue(**extra):
                with self.subTest(extra=sorted(map(repr, extra)), cue=label):
                    self.refuse(raw_of(receipt), "invalid_unicode_json", filename=name)
        self.refuse(raw_of(native(unit("x", kind_name=lone))), "invalid_unicode_json")

    def test_lone_surrogate_inside_decoded_header_text(self):
        # Receipt bytes are valid; the row-1 header text itself carries the escape.
        for escape in ("\\ud800", "\\udfff"):
            row = unit('[{"cell":"A1","type":"s","value":"' + escape + '"}]',
                       kind_name="workbook_row", locator={"sheet": "Search log", "row": 1})
            self.refuse(raw_of(native(row)), "invalid_unicode_json")
            row["locator"]["sheet"] = "opaque"
            self.refuse(raw_of(native(row)), "invalid_unicode_json")
            header = unit('["event_id","searched_at","' + escape + '"]', kind_name="csv_header",
                          locator={"record": 1})
            for label, receipt, name in cue_and_no_cue(header):
                with self.subTest(escape=escape, cue=label):
                    self.refuse(raw_of(receipt), "invalid_unicode_json", filename=name)

    def test_utf8_encoded_surrogate_bytes_rejected(self):
        # CESU-style ED A0 80 bytes would decode to a lone surrogate (surrogatepass).
        for label, receipt, name in cue_and_no_cue(unit("PLACEHOLDER-SURROGATE-BYTES")):
            raw = raw_of(receipt).replace(b"PLACEHOLDER-SURROGATE-BYTES", b"\xed\xa0\x80")
            with self.subTest(cue=label):
                self.refuse(raw, "invalid_unicode_json", filename=name)

    def test_valid_paired_surrogate_and_astral_characters_accepted(self):
        astral = "\U0001F600 \U00010348 \u00e9 \ufffd \u4e2d"
        for label, receipt, name in cue_and_no_cue(
                unit(astral, locator={"page": 2, "\U0001F600": astral},
                     data={"\U00010348": [astral, {"k": astral}]})):
            escaped = raw_of(receipt)
            self.assertIn(b"\\ud83d\\ude00", escaped)  # Paired escape in canonical bytes.
            literal = escaped.replace(b"\\ud83d\\ude00", "\U0001F600".encode("utf-8"))
            for raw in (escaped, literal):
                with self.subTest(cue=label, literal=raw is literal):
                    self.expect_valid(label, self.accept(raw, filename=name))
        output = self.accept(raw_of(native(unit("Search log\n" + astral))))
        self.assertEqual((output["kind"], output["confidence"]), ("search-log", "medium"))

    def test_astral_text_byte_bound_unchanged(self):
        text = "\U0001F600" * 4  # 16 UTF-8 bytes.
        raw = raw_of(native(unit(text, locator={"page": 2})))
        with patch.object(kind, "MAX_TEXT_BYTES", 16):
            self.accept(raw)
        with patch.object(kind, "MAX_TEXT_BYTES", 15):
            self.refuse_code(raw, "unit_text_bound")

    def refuse_code(self, raw, code):
        with self.assertRaises(kind.KindError) as raised:
            call(raw)
        self.assertEqual(str(raised.exception), code)


class NumericTests(Refusals):
    def placements(self):
        """Receipts that put the raw TOKEN in text-adjacent, locator, data and top level."""
        return (("locator", unit("opaque", locator={"page": 2, "scale": TOKEN})),
                ("data", unit("opaque", data={"v": TOKEN})),
                ("nested", unit("opaque", data={"a": [[{"b": TOKEN}]]})),
                ("top", None))

    def receipts(self):
        for where, bad in self.placements():
            extra = {"metric": TOKEN} if bad is None else {}
            for label, receipt, name in cue_and_no_cue(bad, **extra):
                yield where, label, receipt, name

    def test_positive_and_negative_overflow_exponents_rejected(self):
        for token in OVERFLOW:
            for where, label, receipt, name in self.receipts():
                with self.subTest(token=token[:12], where=where, cue=label):
                    self.refuse(raw_of(receipt, token), "nonfinite_json", filename=name)

    def test_overflow_inside_decoded_header_text_rejected(self):
        for token in (b"1e309", b"-1e309"):
            text = '["event_id","searched_at",' + token.decode() + ']'
            header = unit(text, kind_name="csv_header", locator={"record": 1})
            for label, receipt, name in cue_and_no_cue(header):
                with self.subTest(token=token, cue=label):
                    # nonfinite_json, not a later header_shape or accepted candidate.
                    self.refuse(raw_of(receipt), "nonfinite_json", filename=name)
            row = unit('[{"cell":"A1","type":"n","value":' + token.decode() + '}]',
                       kind_name="workbook_row", locator={"sheet": "Search log", "row": 1})
            self.refuse(raw_of(native(row)), "nonfinite_json")

    def test_ordinary_finite_exponents_accepted(self):
        for token in FINITE:
            for where, label, receipt, name in self.receipts():
                with self.subTest(token=token[:12], where=where, cue=label):
                    self.expect_valid(label, self.accept(raw_of(receipt, token), filename=name))
        row = unit('[{"cell":"A1","type":"n","value":1e308}]', kind_name="workbook_row",
                   locator={"sheet": "opaque", "row": 1})
        self.assertEqual(self.accept(raw_of(native(row)))["reason"], "no_decisive_cues")

    def test_explicit_nan_and_infinity_rejected(self):
        for token in (b"NaN", b"Infinity", b"-Infinity"):
            for where, label, receipt, name in self.receipts():
                with self.subTest(token=token, where=where, cue=label):
                    self.refuse(raw_of(receipt, token), "nonfinite_json", filename=name)
            header = unit('["event_id",' + token.decode() + ']', kind_name="csv_header",
                          locator={"record": 1})
            self.refuse(raw_of(native(header)), "nonfinite_json")

    def test_oversized_integer_literal_is_typed_refusal(self):
        token = b"7" * 5000  # Beyond the interpreter's int string-conversion limit.
        for where, label, receipt, name in self.receipts():
            with self.subTest(where=where, cue=label):
                self.refuse(raw_of(receipt, token), "invalid_json", filename=name)


class RetainedChecksTests(Refusals):
    def test_duplicate_json_keys_rejected_with_and_without_cue(self):
        # The token closes the "x" value and re-adds an existing key ("page" / "x").
        dup_locator = b'1,"page":2'
        dup_data = b'1,"x":{"b":1,"b":1}'
        for where, token in (("locator", dup_locator), ("data", dup_data)):
            for label, receipt, name in cue_and_no_cue(
                    unit("opaque", locator={"page": 2, "x": TOKEN} if where == "locator" else None,
                         data={"x": TOKEN} if where == "data" else None)):
                with self.subTest(where=where, cue=label):
                    self.refuse(raw_of(receipt, token), "duplicate_json_key", filename=name)
        for label, receipt, name in cue_and_no_cue(metric=TOKEN):
            raw = raw_of(receipt, b'1,"status":"complete"')
            self.refuse(raw, "duplicate_json_key", filename=name)
        header = unit('[{"cell":"A1","cell":"A1","type":"s","value":"event_id"}]',
                      kind_name="workbook_row", locator={"sheet": "Search log", "row": 1})
        self.refuse(raw_of(native(header)), "duplicate_json_key")

    def test_malformed_bytes_rejected(self):
        for bad in (b"\xff", b"\xc0\xaf", b"\xe2\x82", b"\xf8\x88\x80\x80\x80"):
            for label, receipt, name in cue_and_no_cue(unit("PLACEHOLDER-BYTES")):
                raw = raw_of(receipt).replace(b"PLACEHOLDER-BYTES", bad)
                with self.subTest(bad=bad, cue=label):
                    self.refuse(raw, "invalid_json", filename=name)
        for raw in (b"", b"{", b"not-json", b'{"schema":"format-extraction-v1"', b"[" * 200000):
            with self.subTest(raw=raw[:8]):
                self.refuse(raw, "invalid_json")

    def test_empty_units_and_empty_text_are_ordinary_other(self):
        for receipt in (native(), native(unit("")), native(unit("   \n\t ")),
                        native(unit("", kind_name="text_line", locator={"line": 1}))):
            output = self.accept(raw_of(receipt))
            self.assertEqual((output["kind"], output["confidence"], output["reason"]),
                             ("other", "low", "no_decisive_cues"))
            self.assertEqual(output["matched_cues"], [])

    def test_all_existing_kind_positives_still_classify(self):
        self.assertEqual(kind.KINDS, set(POSITIVES) | {"other"})
        for expected, title in POSITIVES.items():
            for body in ("SYNTHETIC", "SYNTHETIC \U0001F600 1e308"):
                with self.subTest(kind=expected, body=body):
                    output = self.accept(raw_of(native(unit(title + "\n" + body))),
                                         filename=expected + ".pdf")
                    self.assertEqual((output["kind"], output["reason"]), (expected, "explicit_cues"))
                    self.assertIn(output["confidence"], ("medium", "high"))

    def test_hash_and_upstream_checks_precede_or_survive_new_gates(self):
        raw = raw_of(native(unit("Search log\n\ud800")))
        with self.assertRaises(kind.KindError) as raised:
            kind.classify(run_id=RUN_ID, artifact_sha256=SUBJECT,
                          derived_receipt_sha256=hashlib.sha256(raw + b" ").hexdigest(),
                          derived_receipt=raw)
        self.assertEqual(str(raised.exception), "derived_receipt_hash_mismatch")
        with self.assertRaises(kind.KindError) as raised:
            kind.classify(run_id=RUN_ID, artifact_sha256=SUBJECT,
                          derived_receipt_sha256=hashlib.sha256(raw).hexdigest(),
                          derived_receipt=raw + b" " * 8, filename="")
        self.assertEqual(str(raised.exception), "derived_receipt_hash_mismatch")
        with patch.object(kind, "MAX_RECEIPT_BYTES", 10):
            with self.assertRaises(kind.KindError) as raised:
                call(raw)
            self.assertEqual(str(raised.exception), "derived_receipt_bound")
        for status, issues in (("partial", []), ("complete", [{"code": "visual_review_pending"}])):
            output = self.accept(raw_of(native(unit("Search log\n\U0001F600"), status=status,
                                               issues=issues)))
            self.assertEqual((output["kind"], output["reason"]), ("other", "upstream_not_clear"))

    def test_refusal_independent_of_unit_order(self):
        bad = unit("opaque \udfff", locator={"page": 3})
        good = unit("Search log")
        for units in ((good, bad), (bad, good), (bad,)):
            self.refuse(raw_of(native(*units)), "invalid_unicode_json", filename="search-log.pdf")


if __name__ == "__main__":
    unittest.main()
