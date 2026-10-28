"""FC7-BOARD-UNICODE: the board refuses text that cannot be saved as UTF-8.

All catalogs are synthetic schema_version 1 inputs with runtime-derived hash
identities. No browser, server, real catalog, file or network is used.
"""
import copy
import hashlib
import inspect
import re
import unittest
from html.parser import HTMLParser

from campaign_tool.records import catalog_board
from campaign_tool.records.catalog_board import render_board


ERROR = "invalid_catalog_text"
MARKER = "FC7LEAKMARK"
# Unpaired surrogate shapes: lone high, lone low, and one embedded in ordinary
# text. A high-low sequence is also two separate code points in a str and
# cannot be strictly encoded either.
SURROGATES = {
    "high": "\ud800",
    "low": "\udfff",
    "embedded": f"{MARKER} before \ud83d after",
    "high_low_sequence": f"{MARKER}\ud83d\ude00",
}
NONASCII = "Comté de Fictión · 東京 · Ελληνικά · עברית · 📄🚓"


class Document(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.tags, self.text = [], []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)


def identity(index):
    return hashlib.sha256(f"fc7-board-unicode-{index}".encode("ascii")).hexdigest()


def catalog(count=1):
    return dict(
        schema_version=1, snapshot_id="synthetic-snapshot",
        summary={"total": 7, "extraction": {"partial": count}},
        cards=[dict(
            sha256=identity(i), agency_hints=["Fictional County"], format="pdf", bytes=1234,
            catalog_status="cataloged", extraction_stage="partial", analysis_state="not_reviewed",
            role="original", dates={"first_seen": "2026-01-01", "last_seen": "2026-01-02",
                                    "document_date": "2025-12-31"},
            value_score=40, value_score_basis=["policy metadata hint"],
            source_paths=["/synthetic/private/document.pdf"], parents=[identity(i + 1000)],
            flags=["date_unknown"], snapshot_declarations={"review": "Unverified declaration"},
        ) for i in range(count)])


def _set_top(key):
    def apply(data, value):
        data[key] = value
    return apply


def _set_card(key):
    def apply(data, value):
        data["cards"][-1][key] = value
    return apply


def _set_list(key):
    def apply(data, value):
        data["cards"][-1][key] = ["ordinary entry", value]
    return apply


def _set_date(key):
    def apply(data, value):
        data["cards"][-1]["dates"][key] = value
    return apply


def _set_declaration_key(data, value):
    data["cards"][-1]["snapshot_declarations"] = {value: "ordinary"}


def _set_declaration_value(data, value):
    data["cards"][-1]["snapshot_declarations"] = {"review": value}


def _set_role_counter_only(data, value):
    # Only the first card carries the bad role; the last card stays valid so
    # the per-card loop has already rendered a valid card before failing.
    data["cards"][0]["role"] = value


# Every metadata surface render_board emits as text. The keys are the exact
# allowlisted lookups in the renderer; test_high_low_* checks this map against
# the renderer source so a newly emitted field cannot be silently uncovered.
SURFACES = {
    "snapshot_id": _set_top("snapshot_id"),
    "format": _set_card("format"),
    "catalog_status": _set_card("catalog_status"),
    "extraction_stage": _set_card("extraction_stage"),
    "analysis_state": _set_card("analysis_state"),
    "role": _set_card("role"),
    "role_counter_first_card": _set_role_counter_only,
    "agency_hints": _set_list("agency_hints"),
    "value_score_basis": _set_list("value_score_basis"),
    "flags": _set_list("flags"),
    "source_paths": _set_list("source_paths"),
    "parents": _set_list("parents"),
    "document_date": _set_date("document_date"),
    "first_seen": _set_date("first_seen"),
    "last_seen": _set_date("last_seen"),
    "snapshot_declarations_key": _set_declaration_key,
    "snapshot_declarations_value": _set_declaration_value,
}
# Lookups that are emitted only after strict non-text validation.
NON_TEXT_LOOKUPS = {"sha256", "bytes", "value_score", "dates", "snapshot_declarations",
                    "cards", "summary", "schema_version"}


def agency_hint_surrogate_catalog():
    """The deciding negative-control input: one bad agency hint, all else valid."""
    data = catalog(2)
    _set_list("agency_hints")(data, SURROGATES["embedded"])
    return data


def accented_emoji_catalog():
    """Restored positive control for the negative mode."""
    data = catalog(2)
    data["cards"][0]["agency_hints"] = ["Comté de Fictión 📄"]
    data["snapshot_id"] = "instantané-🚓"
    return data


class Fc7BoardUnicodeTests(unittest.TestCase):
    def render_strict(self, data):
        output = render_board(data)
        self.assertIsInstance(output, str)
        encoded = output.encode("utf-8", "strict")
        self.assertEqual(encoded.decode("utf-8", "strict"), output)
        return output

    def check_refusal(self, data):
        """Assert the fixed refusal with no output and unchanged input."""
        original = copy.deepcopy(data)
        returned = []
        with self.assertRaises(ValueError) as caught:
            returned.append(render_board(data))
        self.assertEqual(returned, [])
        self.assertEqual(caught.exception.args, (ERROR,))
        self.assertEqual(str(caught.exception), ERROR)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertEqual(data, original)
        return caught.exception

    def test_ordinary_ascii_and_nonascii_html(self):
        ascii_output = self.render_strict(catalog(2))
        self.assertIn("synthetic-snapshot", ascii_output)
        data = catalog(2)
        for key, apply in SURFACES.items():
            if key not in {"snapshot_declarations_key", "role_counter_first_card"}:
                apply(data, f"{NONASCII} {key}")
        output = self.render_strict(data)
        text = "".join(Document(output).text)
        for key in SURFACES:
            if key not in {"snapshot_declarations_key", "role_counter_first_card"}:
                self.assertIn(f"{NONASCII} {key}", text)
        self.assertIn(NONASCII, output)  # literal UTF-8, not replaced or entity-encoded
        self.assertNotIn("\ufffd", output)
        self.assertIn('<meta charset="utf-8">', output)

    def test_high_low_embedded_surrogates_each_emitted_surface(self):
        source = inspect.getsource(catalog_board._card) + inspect.getsource(catalog_board.render_board)
        lookups = set(re.findall(r'\.get\("([a-z0-9_]+)"\)', source))
        covered = {k for k in SURFACES if not k.startswith(("role_counter", "snapshot_declarations_"))}
        self.assertEqual(lookups - NON_TEXT_LOOKUPS, covered)
        self.assertLessEqual(NON_TEXT_LOOKUPS - {"schema_version"}, lookups)
        for surface, apply in SURFACES.items():
            for shape, bad in SURROGATES.items():
                with self.subTest(surface=surface, shape=shape):
                    data = catalog(2)
                    apply(data, bad)
                    self.check_refusal(data)

    def test_unused_metadata_stays_ignored(self):
        bad = SURROGATES["embedded"]
        baseline = self.render_strict(catalog(2))
        data = catalog(2)
        data["raw_text"] = bad
        data["receipts"] = {"body": bad}
        data["summary"][bad] = 3  # counter keys outside the label grammar are ignored
        data["summary"]["nested"] = {bad: 4}
        card = data["cards"][-1]
        card.update(raw_text=bad, receipt_contents=bad)
        card["dates"]["raw"] = bad
        card["agency_hints"] = ["Fictional County", {"raw": bad}, [bad]]
        card["snapshot_declarations"][bad] = {"body": bad}
        card["snapshot_declarations"]["nested"] = [bad]
        output = self.render_strict(data)
        self.assertEqual(output.count("<article"), 2)
        self.assertNotIn(MARKER, output)
        # Ignored metadata leaves the board byte-for-byte unchanged.
        self.assertEqual(output, baseline)

    def test_html_hostile_text_remains_escaped(self):
        payload = f'</script><img src=x onerror="alert(1)"> & "{NONASCII}\''
        data = catalog(1)
        for key, apply in SURFACES.items():
            apply(data, payload)
        output = self.render_strict(data)
        doc = Document(output)
        self.assertNotIn(payload, output)
        self.assertIn("&lt;/script&gt;&lt;img src=x onerror=&quot;alert(1)&quot;&gt; &amp; &quot;", output)
        self.assertIn("&#x27;", output)
        self.assertEqual(sum(tag == "script" for tag, _ in doc.tags), 1)
        self.assertFalse(any(tag in {"img", "iframe", "a", "link", "object"} for tag, _ in doc.tags))
        self.assertFalse(any(key.startswith("on") for _, attrs in doc.tags for key in attrs))
        self.assertTrue(any(payload in text for text in doc.text))

    def test_exact_card_count_and_input_unchanged(self):
        data = catalog(60)
        data["cards"][7]["agency_hints"] = [NONASCII]
        original = copy.deepcopy(data)
        output = self.render_strict(data)
        doc = Document(output)
        identities = [attrs["data-sha256"] for tag, attrs in doc.tags if tag == "article"]
        self.assertEqual(identities, [card["sha256"] for card in data["cards"]])
        self.assertEqual(len(set(identities)), 60)
        self.assertIn('id="total-count">60</strong>', output)
        self.assertEqual(data, original)
        self.assertEqual(output, render_board(data))
        refused = catalog(60)
        refused["cards"][-1]["flags"] = [SURROGATES["low"]]
        self.check_refusal(refused)
        # Identity checks keep precedence and wording for invalid identities.
        duplicate = catalog(2)
        duplicate["cards"][1]["sha256"] = duplicate["cards"][0]["sha256"].upper()
        duplicate["cards"][1]["format"] = SURROGATES["high"]
        with self.assertRaisesRegex(ValueError, "^Duplicate SHA-256 card identity$"):
            render_board(duplicate)
        invalid = catalog(1)
        invalid["cards"][0]["sha256"] = "z" * 63 + SURROGATES["high"]
        with self.assertRaisesRegex(ValueError, "^Each card requires a SHA-256 identity$"):
            render_board(invalid)

    def test_fixed_error_no_value_echo(self):
        for surface, apply in SURFACES.items():
            with self.subTest(surface=surface):
                data = catalog(1)
                apply(data, SURROGATES["embedded"])
                error = self.check_refusal(data)
                rendered = repr(error) + str(error) + repr(error.args)
                self.assertNotIn(MARKER, rendered)
                self.assertNotIn("before", rendered)
                self.assertNotIn("\\ud83d", rendered)
                self.assertNotIn("\ud83d", rendered)


if __name__ == "__main__":
    unittest.main()
