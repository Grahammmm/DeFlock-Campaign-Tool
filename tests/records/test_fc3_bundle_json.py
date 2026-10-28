"""FC3-BUNDLE-JSON: review_bundle.decode must never yield a nonfinite float.

Synthetic inputs only; no models, network, corpus, ledger or publication.
Every test class carries a CASE label naming the frozen case it covers, so the
acceptance runner can prove complete case coverage from actual results.
"""
import builtins
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest import mock

from campaign_tool.records import review_bundle as bundle

# Literals whose ordinary float conversion overflows to +inf. Variants cover
# exponent forms, long mantissas, fractional mantissas and the first value past
# the largest double's rounding midpoint.
POSITIVE_OVERFLOW = (
    "1e999", "1E999", "1e+999", "1.0e309", "1.8e308", "2e308",
    "1.7976931348623159e308", "180e306", "0.0000001e316",
    "1" + "0" * 309 + ".0", "9" * 400 + ".5", "1e99999999999999999999",
)
NEGATIVE_OVERFLOW = tuple("-" + text for text in POSITIVE_OVERFLOW)
OVERFLOW = POSITIVE_OVERFLOW + NEGATIVE_OVERFLOW

# Finite controls, including the largest double and values rounding onto it.
FINITE = (
    "0", "-0", "7", "-7", "1.5", "-1.5", "0.1", "3.141592653589793", "1e308", "-1e308",
    "1.7976931348623157e308", "-1.7976931348623157e308", "1.7976931348623158e308",
    "123e306", "4.9e-324", "1e-5", "2.5E+10", "123456789012345678901234567890",
    "-" + "9" * 400,
)
UNDERFLOW = ("1e-999", "-1e-999", "2e-324", "-2e-324", "0.0", "-0.0", "0e999", "-0e999")


def nested_forms(literal):
    """Containers around one number literal, from shallow to deep."""
    deep_array = "[" * 40 + literal + "]" * 40
    deep_object = '{"k":' * 40 + literal + "}" * 40
    return (
        "[" + literal + "]",
        '{"a":' + literal + "}",
        "[1,2.5," + literal + "]",
        '{"ok":1.5,"x":[true,null,' + literal + "]}",
        '{"a":[1,{"b":' + literal + "}]}",
        '[{"a":{"b":[{"c":' + literal + "}]}}]",
        '{"title":"t","claim":"c","limitations":["l"],"n":' + literal + "}",
        deep_array,
        deep_object,
    )


def as_bytes(text):
    return text.encode("utf-8")


class DecodeAssertions(unittest.TestCase):
    def assert_invalid_json(self, raw, hidden=None, maximum=None):
        """Typed refusal with the fixed public reason and no echoed source value."""
        try:
            if maximum is None:
                result = bundle.decode(raw)
            else:
                result = bundle.decode(raw, maximum)
        except bundle.BundleError as error:
            self.assertEqual(error.args, ("invalid_json",))
            self.assertEqual(str(error), "invalid_json")
            if hidden is not None:
                self.assertNotIn(hidden, str(error))
                self.assertNotIn(hidden, repr(error))
                self.assertNotIn(hidden, repr(error.__cause__))
            return error
        self.fail("FC3 typed refusal missing: decode returned %s"
                  % type(result).__name__)

    def assert_matches_stdlib(self, raw):
        """Valid input: same value and types as plain json.loads, and re-encodable."""
        value = bundle.decode(raw)
        reference = json.loads(raw)
        self.assertEqual(json.dumps(value, sort_keys=True), json.dumps(reference, sort_keys=True))
        self.assertEqual(self.shape(value), self.shape(reference))
        again = bundle.decode(bundle.encoded(value))
        self.assertEqual(self.shape(again), self.shape(value))
        return value

    def shape(self, value):
        if type(value) is dict:
            return {key: self.shape(item) for key, item in value.items()}
        if type(value) is list:
            return [self.shape(item) for item in value]
        if type(value) is float:
            return ("float", value.hex())
        return (type(value).__name__, value)


class FixtureSanity(unittest.TestCase):
    CASE = "positive_and_negative_overflow"

    def test_overflow_fixtures_really_overflow_ordinary_float(self):
        for text in OVERFLOW:
            with self.subTest(text=text[:40]):
                self.assertTrue(math.isinf(float(text)))
                self.assertEqual(math.copysign(1.0, float(text)), -1.0 if text[0] == "-" else 1.0)

    def test_finite_fixtures_are_finite(self):
        # JSON integers stay Python ints, so only non-integer literals are floats.
        for text in FINITE + UNDERFLOW:
            with self.subTest(text=text[:40]):
                value = json.loads(text)
                self.assertTrue(type(value) is int or math.isfinite(value))


class FiniteScalarAndNested(DecodeAssertions):
    CASE = "finite_scalar_and_nested"

    def test_finite_scalars_decode_like_stdlib(self):
        for text in FINITE:
            with self.subTest(text=text[:40]):
                self.assert_matches_stdlib(as_bytes(text))

    def test_finite_values_in_every_nested_form(self):
        for text in FINITE:
            for form in nested_forms(text):
                with self.subTest(text=text[:40], form=form[:60]):
                    self.assert_matches_stdlib(as_bytes(form))

    def test_integers_stay_integers_and_largest_double_survives(self):
        value = bundle.decode(b'{"i":123456789012345678901234567890,"f":1.7976931348623157e308,"g":1.0}')
        self.assertIs(type(value["i"]), int)
        self.assertEqual(value["f"], sys.float_info.max)
        self.assertIs(type(value["g"]), float)

    def test_ordinary_objects_arrays_and_utf8(self):
        samples = (
            b'{}', b'[]', b'{"a":[1,2,{"b":null,"c":true,"d":false}],"e":"x"}',
            '{"t":"caf\u00e9 \u6771\u4eac \U0001F600"}'.encode("utf-8"),
            b'{"t":"caf\\u00e9 \\ud83d\\ude00"}',
            b'\xef\xbb\xbf{"bom":1.25}',
            b'  [ 1 , 2.5 ]  ',
        )
        for raw in samples:
            with self.subTest(raw=raw[:40]):
                self.assert_matches_stdlib(raw)

    def test_invalid_utf8_and_syntax_still_rejected(self):
        for raw in (b'"\xff"', b'{"a":"\xc3"}', b"", b"{", b"[1,]", b"01", b"1.", b".5", b"+1"):
            with self.subTest(raw=raw):
                self.assert_invalid_json(raw)


class PositiveAndNegativeOverflow(DecodeAssertions):
    CASE = "positive_and_negative_overflow"

    def test_positive_overflow_scalars_rejected(self):
        for text in POSITIVE_OVERFLOW:
            with self.subTest(text=text[:40]):
                self.assert_invalid_json(as_bytes(text), hidden=text)

    def test_negative_overflow_scalars_rejected(self):
        for text in NEGATIVE_OVERFLOW:
            with self.subTest(text=text[:40]):
                self.assert_invalid_json(as_bytes(text), hidden=text)

    def test_overflow_with_surrounding_whitespace_rejected(self):
        for text in ("1e999", "-1e999"):
            with self.subTest(text=text):
                self.assert_invalid_json(as_bytes(" \n\t" + text + " \r\n"), hidden=text)


class NestedOverflow(DecodeAssertions):
    CASE = "nested_overflow"

    def test_every_overflow_in_every_nested_form_rejected(self):
        for text in OVERFLOW:
            for form in nested_forms(text):
                with self.subTest(text=text[:40], form=form[:60]):
                    self.assert_invalid_json(as_bytes(form), hidden=text)

    def test_overflow_after_many_valid_items_rejected(self):
        items = ",".join("1.5" for _ in range(500))
        for text in ("1e999", "-1e999"):
            with self.subTest(text=text):
                self.assert_invalid_json(as_bytes("[" + items + "," + text + "]"))
                self.assert_invalid_json(as_bytes('{"z":[' + items + '],"y":' + text + "}"))


class SyntheticBundleBoundary(unittest.TestCase):
    """Real bundle entry points around decode, with synthetic authority only."""
    CASE = "nested_overflow"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fc3-bundle-json-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        self.output = self.root / "private-bundles"
        self.output.mkdir(mode=0o700)
        self.context = object()
        principals = {"author": {"identity": "synthetic-author", "provider": "synthetic-provider",
                                 "model": "synthetic-model", "roles": ["author"]}}
        key = hashlib.sha256(b"fc3 synthetic signing fixture label").digest()
        self.authority = bundle.Authority(
            profile_id="synthetic-fc3-profile", principals=principals,
            authenticate=lambda context: "author" if context is self.context else None,
            signing_key=key)
        bindings = {kind: [] for kind in bundle.KINDS}
        for kind in ("originals", "units", "digests", "sources"):
            bindings[kind] = [self.artifact(kind, ("synthetic " + kind).encode())]
        original, unit = bindings["originals"][0], bindings["units"][0]
        self.proposal = {
            "proposal_id": "synthetic-fc3-proposal", "revision": 1, "tier": "A",
            "agency": "Synthetic agency", "classification": "CONFIRMED_DOCUMENTARY_FACT",
            "next_action": "Await independent review", "event_date": None,
            "primary_evidence": [{"path": original["path"], "sha256": original["sha256"],
                                  "locator": "line:1", "observation": "Synthetic observation",
                                  "coverage": "selected"}],
            "rules": [], "counterevidence": [], "missing_evidence": [], "exceptions_checked": False,
            "bindings": bindings,
            "coverage": [{"original_sha256": original["sha256"], "unit_sha256": unit["sha256"],
                          "locator": "line:1", "scope": "selected"}], "supersedes": None}

    def artifact(self, name, raw):
        path = self.root / (name + ".txt")
        path.write_bytes(raw)
        path.chmod(0o600)
        return {"id": name, "path": str(path), "sha256": bundle.sha(raw), "bytes": len(raw)}

    def create(self, public):
        return bundle.create_bundle(self.output, public, self.proposal,
                                    authority=self.authority, auth_context=self.context)

    def public(self, title_literal='"Synthetic title"', limitation_literal='"Synthetic limit"'):
        return ('{"claim":"Synthetic claim","limitations":[' + limitation_literal
                + '],"title":' + title_literal + "}\n").encode()

    def test_finite_public_content_control_creates_bundle(self):
        result = self.create(self.public())
        self.assertFalse(result["reused"])
        self.assertTrue((self.output / result["bundle_id"] / "manifest.json").is_file())

    def test_overflow_public_content_refused_before_any_write(self):
        for text in ("1e999", "-1e999", "1.8e308"):
            for public in (self.public(title_literal=text), self.public(limitation_literal=text)):
                with self.subTest(text=text, public=public[:60]):
                    try:
                        self.create(public)
                    except bundle.BundleError as error:
                        if error.args != ("invalid_json",):
                            self.fail("FC3 typed refusal missing: reason %r" % (error.args,))
                    else:
                        self.fail("FC3 typed refusal missing: bundle created")
                    self.assertEqual(sorted(os.listdir(self.output)), [])

    def test_planted_overflow_receipt_refused_as_invalid_json(self):
        identity = self.create(self.public())["bundle_id"]
        signature = hashlib.sha256(b"fc3 planted receipt label").hexdigest()
        for text in ("1e999", "-1e999"):
            raw = ('{"body":{"kind":"review","n":[' + text + ']},"signature":"'
                   + signature + '"}\n').encode()
            reviews = self.output / identity / "reviews"
            planted = reviews / (bundle.sha(raw) + ".json")
            planted.write_bytes(raw)
            planted.chmod(0o600)
            with self.subTest(text=text):
                try:
                    bundle.assess_bundle(self.output, identity, authority=self.authority)
                except bundle.BundleError as error:
                    self.assertEqual(error.args, ("invalid_json",))
                    self.assertNotIn(text, str(error))
                except Exception as error:  # an untyped escape is the regression
                    self.fail("FC3 typed refusal missing: untyped %s escaped" % type(error).__name__)
                else:
                    self.fail("FC3 typed refusal missing: planted receipt accepted")
            # Move (never delete) the plant out of the receipt inventory.
            planted.rename(self.root / ("retired-planted-receipt" + text + ".json"))


class ExplicitNonfiniteTokens(DecodeAssertions):
    CASE = "explicit_nonfinite_tokens"

    def test_nan_and_infinity_tokens_rejected_scalar_and_nested(self):
        for token in ("NaN", "Infinity", "-Infinity"):
            for form in (token,) + nested_forms(token):
                with self.subTest(form=form[:60]):
                    self.assert_invalid_json(as_bytes(form))

    def test_non_json_spellings_rejected(self):
        for token in ("nan", "inf", "-inf", "+Infinity", "-NaN", "Inf", "infinity"):
            with self.subTest(token=token):
                self.assert_invalid_json(as_bytes("[" + token + "]"))


class DuplicateKeysStillRejected(DecodeAssertions):
    CASE = "duplicate_keys_still_rejected"

    def test_duplicate_keys_rejected_at_every_level(self):
        for raw in (b'{"a":1,"a":1}', b'{"a":1.5,"a":2.5}', b'{"x":{"a":1,"b":2,"a":3}}',
                    b'[{"a":null},{"b":1,"b":1}]', b'{"a":[{"k":1e5,"k":1e5}]}'):
            with self.subTest(raw=raw):
                error = self.assert_invalid_json(raw)
                self.assertIs(type(error.__cause__), bundle.BundleError)
                self.assertEqual(error.__cause__.args, ("duplicate_json_key",))

    def test_distinct_keys_control(self):
        self.assert_matches_stdlib(b'{"a":1,"b":{"a":2,"c":[{"a":3}]}}')


class ByteBoundExactAndOver(DecodeAssertions):
    CASE = "byte_bound_exact_and_over"

    def test_default_bound_exact_accepted_and_one_over_refused(self):
        body = b'{"pad":"' + b"x" * (bundle.MAX_JSON - 10) + b'"}'
        self.assertEqual(len(body), bundle.MAX_JSON)
        self.assertEqual(len(bundle.decode(body)["pad"]), bundle.MAX_JSON - 10)
        with self.assertRaises(bundle.BundleError) as caught:
            bundle.decode(body + b" ")
        self.assertEqual(caught.exception.args, ("json_byte_bound",))

    def test_explicit_bound_with_numbers(self):
        for text in ("1.5", "1e999"):
            raw = as_bytes("[" + text + "]")
            with self.subTest(text=text):
                with self.assertRaises(bundle.BundleError) as caught:
                    bundle.decode(raw, len(raw) - 1)
                self.assertEqual(caught.exception.args, ("json_byte_bound",))
        self.assertEqual(bundle.decode(b"[1.5]", 5), [1.5])
        self.assert_invalid_json(b"[1e999]", hidden="1e999", maximum=7)

    def test_non_bytes_input_still_byte_bound_refusal(self):
        for raw in ("[1.5]", "[1e999]", bytearray(b"[1.5]"), memoryview(b"[1.5]"), None, 15):
            with self.subTest(kind=type(raw).__name__):
                with self.assertRaises(bundle.BundleError) as caught:
                    bundle.decode(raw)
                self.assertEqual(caught.exception.args, ("json_byte_bound",))


class FiniteUnderflowAndSignedZero(DecodeAssertions):
    CASE = "finite_underflow_and_signed_zero"

    def test_underflow_and_signed_zero_keep_python_semantics(self):
        for text in UNDERFLOW:
            for form in (text, "[" + text + "]", '{"z":[' + text + "]}"):
                with self.subTest(form=form):
                    value = self.assert_matches_stdlib(as_bytes(form))
                    while type(value) in (list, dict):
                        value = value[0] if type(value) is list else value["z"]
                    self.assertIs(type(value), float)
                    self.assertEqual(value, 0.0)
                    self.assertEqual(math.copysign(1.0, value), -1.0 if text[0] == "-" else 1.0)

    def test_negative_integer_zero_stays_integer(self):
        self.assertIs(type(bundle.decode(b"-0")), int)

    def test_signed_zero_reencodes_exactly(self):
        self.assertEqual(bundle.encoded(bundle.decode(b"[-0.0,0.0,1e-999,-1e-999]")),
                         b"[-0.0,0.0,0.0,-0.0]\n")


class NoAuthorityOrIoRequired(DecodeAssertions):
    CASE = "no_authority_or_io_required"

    def refuse(self, *args, **kwargs):
        raise AssertionError("decode attempted authority, file or network access")

    def test_decode_needs_no_authority_files_or_network(self):
        with mock.patch.object(builtins, "open", self.refuse), \
                mock.patch.object(bundle.os, "open", self.refuse), \
                mock.patch.object(bundle.os, "stat", self.refuse), \
                mock.patch.object(bundle, "Authority", self.refuse), \
                mock.patch.object(bundle, "_read", self.refuse), \
                mock.patch.object(bundle, "_private", self.refuse), \
                mock.patch.object(socket, "socket", self.refuse), \
                mock.patch.object(socket, "create_connection", self.refuse):
            for text in OVERFLOW:
                with self.subTest(text=text[:40]):
                    self.assert_invalid_json(as_bytes(text), hidden=text)
                    self.assert_invalid_json(as_bytes('{"a":[' + text + "]}"), hidden=text)
            for text in FINITE:
                with self.subTest(text=text[:40]):
                    self.assertEqual(bundle.decode(as_bytes(text)), json.loads(text))


if __name__ == "__main__":
    unittest.main()
