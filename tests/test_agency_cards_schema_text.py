"""RF0210-2: schemas/agency-cards.schema.json enforces the renderer's plain-text policy.

The schema leaf rules are checked by a small test-only evaluator (type, pattern, minLength, maxLength; regex
*search*, as JSON Schema does). It is not a Draft 2020-12 validator and never calls agency_cards.text: the
production helpers (text, visible, problems, render) are the independent comparison target. Complete synthetic
configs additionally go through campaign_tool.law.validate, which supports type/pattern/minLength only (no maxLength).
All inputs are synthetic and in memory; sockets are blocked.
"""
import copy
import functools
import json
import re
import socket
import sys
import unicodedata
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import agency_cards, law  # noqa: E402

SCHEMA = json.loads((REPO_ROOT / "schemas" / "agency-cards.schema.json").read_text(encoding="utf-8"))
CONFIG = json.loads((REPO_ROOT / "examples" / "fictional-campaign" / "agency-cards.json").read_text(encoding="utf-8"))
SCALARS = ("name", "vendor", "status", "summary", "cameraCount", "open")
LISTS = ("facts", "flags")
SLOTS = [(f, None) for f in SCALARS] + [(f, 0) for f in LISTS] + [("source_link_label", None)]
ANNOTATIONS = {"$ref", "description", "title", "$comment"}
VALIDATING = {"type", "pattern", "minLength", "maxLength"}
EMPTY_SOCKET = socket.socket
EMPTY_CREATE_CONNECTION = socket.create_connection


def setUpModule():
    def refuse(*args, **kwargs):
        raise AssertionError("network access is not allowed in this test")
    socket.socket = refuse
    socket.create_connection = refuse


def tearDownModule():
    socket.socket = EMPTY_SOCKET
    socket.create_connection = EMPTY_CREATE_CONNECTION


@functools.lru_cache(maxsize=None)
def compiled(pattern):
    return re.compile(pattern)


class Rule:
    """Effective string rule of a schema node: local $ref resolved, siblings ANDed with the referenced rule."""

    def __init__(self, node, root):
        self.types, self.patterns, self.min_length, self.max_length = [], [], 0, None
        self._add(node, root)

    def _add(self, node, root):
        for key in node:
            if key not in ANNOTATIONS | VALIDATING:
                raise AssertionError(f"unsupported keyword in a text leaf: {key}")
        if "$ref" in node:
            target = root
            if not node["$ref"].startswith("#/"):
                raise AssertionError("only local $ref is supported: " + node["$ref"])
            for part in node["$ref"][2:].split("/"):
                target = target[part]
            self._add(target, root)
        if "type" in node:
            self.types.append(node["type"])
        if "pattern" in node:
            self.patterns.append(node["pattern"])
        if "minLength" in node:
            self.min_length = max(self.min_length, node["minLength"])
        if "maxLength" in node:
            self.max_length = node["maxLength"] if self.max_length is None else min(self.max_length, node["maxLength"])

    def accepts(self, value):
        if not isinstance(value, str) or any(t != "string" for t in self.types) or not self.types:
            return False
        if len(value) < self.min_length or (self.max_length is not None and len(value) > self.max_length):
            return False
        return all(compiled(p).search(value) for p in self.patterns)


def slot_node(schema, field, items):
    if field == "source_link_label":
        return schema["properties"]["source_link_label"]
    node = schema["properties"]["county_profile"]["properties"][field]
    return node["items"] if items is not None else node


def rule_for(field, items, schema=SCHEMA):
    return Rule(slot_node(schema, field, items), schema)


def config_with(field, items, value):
    config = copy.deepcopy(CONFIG)
    if field == "source_link_label":
        config[field] = value
    elif items is None:
        config["county_profile"][field] = value
    else:
        config["county_profile"][field] = [value]
    return config


def expected_problem(field, items):
    if field == "source_link_label":
        return "source_link_label: blank or not plain text"
    if items is None:
        return f"county_profile.{field}: not plain text"
    return f"county_profile.{field}: item 0 not plain text"


def schema_accepts_config(config):
    try:
        law.validate(config, SCHEMA)
    except ValueError:
        return False
    return True


def database_version():
    return tuple(int(x) for x in unicodedata.unidata_version.split("."))


def recorded_version():
    found = re.search(r"Unicode (\d+)\.(\d+)", SCHEMA["$defs"]["plainText"]["description"])
    assert found, "the plainText description must record the Unicode database version"
    return tuple(int(x) for x in found.groups())


def forbidden_codepoints():
    """Every code point the runtime refuses, from the production helpers, plus the markup brackets."""
    return [c for c in range(0x110000) if not agency_cards.text(chr(c))]


def drift(rule, expected):
    """Code points where the schema rule disagrees with the production helper, as (schema_accepts, point)."""
    return [(accepted, c) for c in range(0x110000)
            if (accepted := rule.accepts(chr(c))) != expected(chr(c))]


class SchemaTextPolicy(unittest.TestCase):
    PROBES = {
        "bidi-summary": ("summary", "Reviewed copy \u202e reversed"),
        "zero-width-fact": ("facts", "A fact\u200bwith a zero-width space"),
        "control-name": ("name", "Name\x07bell"),
        "blank-label": ("source_link_label", "   "),
    }

    def test_schema_rejects_existing_probe_cases_in_every_text_slot(self):
        bad = {"bidi": "Reviewed copy \u202e reversed", "zero-width": "A\u200bB", "control": "N\x07B", "blank": "   "}
        for field, items in SLOTS:
            rule = rule_for(field, items)
            for case, value in bad.items():
                if case == "blank" and field != "source_link_label":
                    continue  # profile text may be blank; only the label may not
                with self.subTest(field=field, case=case):
                    config = config_with(field, items, value)
                    self.assertFalse(rule.accepts(value))
                    self.assertFalse(schema_accepts_config(config))
                    self.assertEqual(agency_cards.problems(config), [expected_problem(field, items)])
                    with self.assertRaises(ValueError):
                        agency_cards.render(config)
        for name, (field, value) in self.PROBES.items():
            items = 0 if field in LISTS else None
            with self.subTest(probe=name):
                self.assertFalse(schema_accepts_config(config_with(field, items, value)))

    def test_schema_rejects_all_control_and_format_codepoints(self):
        points = [c for c in forbidden_codepoints()
                  if unicodedata.category(chr(c)) in ("Cc", "Cf") or chr(c) in "<>\u2028\u2029"]
        keep = {0x09, 0x0A}
        self.assertFalse(keep & set(points))
        for needed in (0x00, 0x1F, 0x7F, 0x85, 0xAD, 0x200B, 0x202E, 0x2066, 0xFEFF, 0x2028, 0x2029,
                       ord("<"), ord(">"), 0xE0001, 0xE0020, 0x13430, 0x1D173):
            self.assertIn(needed, points)
        self.assertTrue(any(c > 0xFFFF for c in points))
        for field, items in SLOTS:
            rule = rule_for(field, items)
            for c in points:
                for text in (chr(c) + "ab", "ab" + chr(c), "ab" + chr(c) + "cd"):
                    if not rule.accepts(text):
                        continue
                    self.fail(f"{field}: schema accepts U+{c:04X} in {text!r}")
            for c in (0x09, 0x0A):
                self.assertTrue(rule.accepts("ab" + chr(c) + "cd"), (field, c))

    def test_schema_matches_runtime_for_every_codepoint(self):
        """Drift: a newer Unicode database that adds a Cc/Cf character fails here until the ranges are regenerated.

        Regenerate: list the code points agency_cards.visible refuses (unicodedata Cc/Cf), coalesce them into ranges
        in $defs.plainText.pattern and in the label pattern, update the Unicode version in the descriptions, review the diff.
        """
        recorded, current = recorded_version(), database_version()
        checked = {}
        for field, items in SLOTS:
            rule = rule_for(field, items)
            signature = (tuple(rule.patterns), rule.min_length, rule.max_length, tuple(rule.types))
            if signature in checked:  # identical effective rule (the six scalars and both item schemas)
                continue
            checked[signature] = field
            if field == "source_link_label":
                expected = lambda s: bool(agency_cards.text(s)) and bool(s.strip())  # noqa: E731
            else:
                expected = lambda s: bool(agency_cards.text(s))  # noqa: E731
            extra = drift(rule, expected)
            # An interpreter older than the recorded database may still call a newer Cf character unassigned (Cn),
            # so the schema may refuse it; the schema must never accept what the runtime refuses.
            allowed = [(acc, c) for acc, c in extra
                       if recorded > current and not acc and unicodedata.category(chr(c)) == "Cn"]
            self.assertEqual([x for x in extra if x not in allowed], [],
                             f"{field}: schema and runtime disagree (schema accepts?, code point); regenerate the ranges")
        self.assertEqual(sorted(checked.values()), ["name", "source_link_label"])

    def test_schema_preserves_allowed_reviewed_text(self):
        corpus = ["", "plain", "Line one\nLine two\tTabbed", "Caf\u00e9 \u201cquoted\u201d \u2018apostrophe\u2019 \u2014 dash",
                  "\u0391\u03b8\u03ae\u03bd\u03b1 \u6f22\u5b57 \u0645\u0631\u062d\u0628\u0627", "emoji \U0001F600\U0001F4F7 \U00010348",
                  "{{SOURCE_ROUTE}} ${x} %s {0}", "\u00a0nbsp \u3000ideographic", "x" * 20000]
        for field, items in SLOTS:
            rule = rule_for(field, items)
            for value in corpus:
                if field == "source_link_label" and not value.strip():
                    continue
                with self.subTest(field=field, value=value[:20]):
                    config = config_with(field, items, value)
                    self.assertTrue(rule.accepts(value))
                    self.assertTrue(schema_accepts_config(config))
                    self.assertEqual(agency_cards.problems(config), [])
                    quoted = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                    self.assertIn(quoted, agency_cards.render(config))  # not normalized or trimmed
        # Empty profile strings and list items stay accepted, as today.
        empty = copy.deepcopy(CONFIG)
        for field in SCALARS:
            empty["county_profile"][field] = ""
        for field in LISTS:
            empty["county_profile"][field] = [""]
        self.assertTrue(schema_accepts_config(empty))
        self.assertEqual(agency_cards.problems(empty), [])
        self.assertTrue(schema_accepts_config(CONFIG))
        self.assertEqual(agency_cards.render(CONFIG), agency_cards.render(copy.deepcopy(CONFIG)))

    def test_source_label_requires_nonwhitespace_without_stripping(self):
        rule = rule_for("source_link_label", None)
        spaces = [chr(c) for c in range(0x110000) if chr(c).isspace()]
        self.assertIn(" ", spaces)
        self.assertIn("\u3000", spaces)
        for value in ["", " ", "\n", "\t", " \n\t ", *spaces, *[s * 3 for s in spaces]]:
            with self.subTest(value=value.encode("unicode_escape")):
                self.assertFalse(rule.accepts(value))
                self.assertFalse(schema_accepts_config(config_with("source_link_label", None, value)))
                self.assertEqual(agency_cards.problems(config_with("source_link_label", None, value)),
                                 ["source_link_label: blank or not plain text"])
        wrapped = " \t\nReviewed sources\u00a0\u3000 "
        config = config_with("source_link_label", None, wrapped)
        self.assertTrue(rule.accepts(wrapped))
        self.assertTrue(schema_accepts_config(config))
        self.assertEqual(agency_cards.problems(config), [])
        self.assertIn(json.dumps(wrapped, ensure_ascii=False, separators=(",", ":")), agency_cards.render(config))
        for bad in (" \u200b ", " x\u202e ", "\n<b>\n", " \x07 ", "\u00a0\u2028\u00a0"):
            with self.subTest(value=bad.encode("unicode_escape")):
                self.assertFalse(rule.accepts(bad))
                self.assertFalse(schema_accepts_config(config_with("source_link_label", None, bad)))
        # The label's own pattern (without the referenced plainText rule) still carries both checks.
        own = compiled(SCHEMA["properties"]["source_link_label"]["pattern"])
        self.assertIsNone(own.search("   "))
        self.assertIsNone(own.search("ok\u200b"))
        self.assertIsNotNone(own.search(" ok "))

    def test_text_schema_length_boundaries_are_real(self):
        for field, items in SLOTS:
            rule = rule_for(field, items)
            self.assertEqual(rule.max_length, 20000, field)
            for unit in ("a", "\U0001F600"):
                with self.subTest(field=field, unit=unit.encode("unicode_escape")):
                    self.assertTrue(rule.accepts(unit * 20000))
                    self.assertFalse(rule.accepts(unit * 20001))
                    self.assertTrue(agency_cards.text(unit * 20000))
                    self.assertFalse(agency_cards.text(unit * 20001))
        label = rule_for("source_link_label", None)
        self.assertEqual(label.min_length, 1)
        self.assertTrue(label.accepts(" " * 19999 + "a"))
        self.assertFalse(label.accepts(" " * 20000 + "a"))
        self.assertFalse(label.accepts(""))
        self.assertTrue(agency_cards.problems(config_with("source_link_label", None, "a" * 20001)))
        self.assertEqual(agency_cards.problems(config_with("source_link_label", None, " " * 19999 + "a")), [])

    def test_every_text_slot_reuses_or_preserves_the_policy(self):
        profile = SCHEMA["properties"]["county_profile"]
        self.assertEqual(set(profile["properties"]), agency_cards.PROFILE_FIELDS)
        self.assertEqual(set(profile["required"]), agency_cards.PROFILE_FIELDS)
        self.assertEqual(set(SCHEMA["required"]), agency_cards.KEYS)
        self.assertEqual({f for f, i in SLOTS if f != "source_link_label"}, agency_cards.PROFILE_FIELDS)
        for field in LISTS:
            self.assertEqual(profile["properties"][field]["maxItems"], 100)
            self.assertEqual(profile["properties"][field]["type"], "array")
        plain = SCHEMA["$defs"]["plainText"]
        self.assertEqual(plain["maxLength"], 20000)
        for field, items in SLOTS:
            node = slot_node(SCHEMA, field, items)
            rule = rule_for(field, items)
            with self.subTest(field=field):
                self.assertEqual(rule.types, ["string"] * len(rule.types))
                self.assertIn(plain["pattern"], rule.patterns)
                self.assertEqual(node["$ref"], "#/$defs/plainText")
                self.assertEqual(rule.min_length, 1 if field == "source_link_label" else 0)
        # The loader refuses unsupported keywords and a missing definition.
        with self.assertRaises(AssertionError):
            Rule({"type": "string", "format": "email"}, SCHEMA)
        with self.assertRaises(KeyError):
            Rule({"$ref": "#/$defs/missing"}, SCHEMA)
        # An accidental nonblank-only label override or a dropped reference is caught by the same checks.
        for broken in ("label-nonblank-only", "label-minlength-only", "name-unreferenced"):
            mutated = copy.deepcopy(SCHEMA)
            if broken == "label-nonblank-only":
                mutated["properties"]["source_link_label"] = {"type": "string", "minLength": 1, "maxLength": 20000,
                                                              "pattern": "[^ ]"}
            elif broken == "label-minlength-only":
                mutated["properties"]["source_link_label"] = {"type": "string", "minLength": 1, "maxLength": 20000}
            else:
                mutated["properties"]["county_profile"]["properties"]["name"] = {
                    "type": "string", "maxLength": 20000, "pattern": "^[^<>]*$"}
            field = "name" if broken == "name-unreferenced" else "source_link_label"
            with self.subTest(broken=broken):
                rule = rule_for(field, None, mutated)
                self.assertTrue(rule.accepts("ok\u200b"), "mutated schema should be the weak one")
                self.assertFalse(rule_for(field, None).accepts("ok\u200b"))
                self.assertNotIn(plain["pattern"], rule.patterns)

    def test_runtime_diagnostics_and_example_output_unchanged(self):
        marker = "synthetic-diagnostic-marker"
        cases = [("summary", None, "A\u202e" + marker), ("facts", 0, marker + "\u200b"), ("name", None, "\x07" + marker),
                 ("source_link_label", None, " \t ")]
        for field, items, value in cases:
            config = config_with(field, items, value)
            with self.subTest(field=field):
                found = agency_cards.problems(config)
                self.assertEqual(found, [expected_problem(field, items)])
                self.assertNotIn(marker, "".join(found))
                with self.assertRaises(ValueError) as caught:
                    agency_cards.render(config)
                self.assertEqual(str(caught.exception), "; ".join(found))
        self.assertEqual(agency_cards.problems(CONFIG), [])
        self.assertTrue(schema_accepts_config(CONFIG))
        # The schema is still schema_version 1 with the same top-level key set and no new keys.
        self.assertEqual(SCHEMA["properties"]["schema_version"], {"type": "integer", "const": 1})
        self.assertFalse(SCHEMA["additionalProperties"])
        self.assertEqual(set(SCHEMA["properties"]), agency_cards.KEYS)


if __name__ == "__main__":
    unittest.main()
