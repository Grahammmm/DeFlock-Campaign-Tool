"""Synthetic board metadata, coverage and injection regressions."""
import base64
import copy
import hashlib
import re
import unittest
from html.parser import HTMLParser
from unittest.mock import patch
from campaign_tool.records.catalog_board import render_board


class Document(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.tags, self.text = [], []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)


def catalog(count=1):
    return dict(schema_version=1, snapshot_id="synthetic-snapshot",
                summary={"total":999999, "extraction":{"partial":count}},
                cards=[dict(sha256=hashlib.sha256(str(i).encode()).hexdigest(),
                agency_hints=["Fictional County"], format="pdf", bytes=1234,
                catalog_status="cataloged", extraction_stage="partial", analysis_state="not_reviewed",
                role="original", dates={"first_seen":"2026-01-01", "last_seen":"2026-01-02", "document_date":None},
                value_score=40, value_score_basis=["policy metadata hint"],
                source_paths=["/synthetic/private/document.pdf"], parents=[], flags=["date_unknown"],
                snapshot_declarations={"review":"Unverified historical declaration"}) for i in range(count)])


class BoardTests(unittest.TestCase):
    def test_all_1623_identities_once_and_exact_total(self):
        source = catalog(1623)
        output = render_board(source)
        doc = Document(output)
        identities = [attrs["data-sha256"] for tag, attrs in doc.tags if tag == "article"]
        self.assertEqual(identities, [card["sha256"] for card in source["cards"]])
        self.assertEqual(len(set(identities)), 1623)
        self.assertIn('id="total-count">1623</strong>', output)
        self.assertIn("999999", doc.text)

    def test_empty(self):
        output = render_board(catalog(0))
        self.assertIn('id="total-count">0</strong>', output)
        self.assertNotIn("<article", output)

    def test_validation(self):
        for version in [None, True, 2, "1"]:
            data = catalog(); data["schema_version"] = version
            with self.subTest(version=version), self.assertRaises(ValueError):
                render_board(data)
        for sha in [None, "short", '" onload="alert(1)', "z"*64]:
            data = catalog(); data["cards"][0]["sha256"] = sha
            with self.subTest(sha=sha), self.assertRaises(ValueError):
                render_board(data)
        data = catalog()
        data["cards"].append(copy.deepcopy(data["cards"][0]))
        data["cards"][1]["sha256"] = data["cards"][1]["sha256"].upper()
        with self.assertRaises(ValueError):
            render_board(data)
        with self.assertRaises(ValueError):
            render_board(dict(schema_version=1, cards={}))

    def test_injection_is_text_in_every_field(self):
        payload = '</script><img src="https://invalid.example/x" onerror="alert(1)"><script>evil()</script> & "'
        data = catalog(); data["snapshot_id"] = payload; card = data["cards"][0]
        for key in ["format","catalog_status","extraction_stage","analysis_state","role"]:
            card[key] = payload
        for key in ["agency_hints","value_score_basis","source_paths","parents","flags"]:
            card[key] = [payload]
        card["dates"] = {key:payload for key in ["document_date","first_seen","last_seen"]}
        card["snapshot_declarations"] = {payload:payload}
        output = render_board(data); doc = Document(output)
        self.assertNotIn(payload, output)
        self.assertIn("&lt;/script&gt;", output)
        self.assertEqual(sum(tag == "script" for tag, _ in doc.tags), 1)
        self.assertFalse(any(tag in {"img","iframe","a","link","object"} for tag, _ in doc.tags))
        self.assertFalse(any(key.startswith("on") for _, attrs in doc.tags for key in attrs))
        self.assertTrue(any(payload in text for text in doc.text))

    def test_extra_private_fields_never_serialized(self):
        data = catalog()
        data["raw_text"] = "TOP_PRIVATE_BODY"
        data["receipts"] = {"body":"PRIVATE_RECEIPT"}
        card = data["cards"][0]
        card.update(raw_text="CARD_PRIVATE_BODY", receipt_contents="CARD_PRIVATE_RECEIPT")
        card["dates"]["raw"] = "DATE_PRIVATE_BODY"
        card["agency_hints"].append({"raw":"LIST_PRIVATE_BODY"})
        card["snapshot_declarations"]["raw"] = {"body":"DECL_PRIVATE_BODY"}
        data["summary"]["raw"] = "SUMMARY_PRIVATE_BODY"
        output = render_board(data)
        for secret in ["TOP_PRIVATE_BODY","PRIVATE_RECEIPT","CARD_PRIVATE_BODY","CARD_PRIVATE_RECEIPT","DATE_PRIVATE_BODY","LIST_PRIVATE_BODY","DECL_PRIVATE_BODY","SUMMARY_PRIVATE_BODY"]:
            self.assertNotIn(secret, output)

    def test_source_paths_not_clickable(self):
        data = catalog()
        data["cards"][0]["source_paths"] = ["https://invalid.example/private","file:///private/document","javascript:alert(1)"]
        doc = Document(render_board(data))
        self.assertFalse(any("href" in attrs or "src" in attrs for _, attrs in doc.tags))
        self.assertIn("javascript:alert(1)", doc.text)

    def test_unknown_dates_warnings_score(self):
        text = " ".join(Document(render_board(catalog())).text)
        for expected in ["Document date","Unknown","First seen (not document date)","Provisional value score","Private metadata","does not authenticate","Cataloged is not analyzed","Unverified historical declaration","1,234 bytes"]:
            self.assertIn(expected, text)

    def test_counters_are_numeric_only(self):
        data = catalog()
        data["summary"] = {"good":2,"ratio":0.5,"bool":True,"neg":-1,"inf":float("inf"),"nan":float("nan"),"html<img>":3,"nested":{"pending":4}}
        output = render_board(data)
        self.assertIn("<dt>good</dt><dd>2</dd>", output)
        self.assertIn("<dt>nested / pending</dt><dd>4</dd>", output)
        for key in ["bool","neg","inf","nan"]:
            self.assertNotIn(f"<dt>{key}</dt>", output)

    def test_pure_deterministic_nonmutating(self):
        data = catalog(2); original = copy.deepcopy(data)
        with patch("builtins.open", side_effect=AssertionError("I/O forbidden")):
            self.assertEqual(render_board(data), render_board(data))
        self.assertEqual(data, original)

    def test_progressive_controls_without_catalog_script(self):
        output = render_board(catalog())
        ids = {attrs.get("id") for _, attrs in Document(output).tags}
        self.assertTrue({"search","agency","type","stage","analysis","previous","next","reset","result","page-label"} <= ids)
        self.assertIn("const size=50", output)
        self.assertIn("matching.slice(page*size", output)
        self.assertIn("option.textContent=value", output)
        self.assertNotIn("innerHTML", output)
        self.assertIn("all cards are shown", output)
        self.assertNotIn("application/json", output)

    def test_csp_pins_exact_inline_content(self):
        output = render_board(catalog())
        doc = Document(output)
        policy = next(attrs["content"] for tag, attrs in doc.tags if tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy")
        self.assertIn("default-src 'none'", policy)
        self.assertNotIn("unsafe-inline", policy)
        for tag, directive in [("script","script-src"),("style","style-src")]:
            content = re.search(f"<{tag}>(.*?)</{tag}>", output, re.S).group(1)
            digest = base64.b64encode(hashlib.sha256(content.encode()).digest()).decode()
            self.assertIn(f"{directive} 'sha256-{digest}'", policy)


if __name__ == "__main__":
    unittest.main()
