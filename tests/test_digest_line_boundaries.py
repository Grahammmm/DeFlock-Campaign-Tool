"""RF1915-1: digest detectors split page/row text on every ``str.splitlines`` boundary.

Synthetic text only; the model transport is an in-memory opener. CR-only and
Unicode-separated page text must get the same line-local detection and
provenance as LF text, while original strings, units with an existing ``line``
locator and legacy LF/CRLF/plain output stay as they were.
"""
from copy import deepcopy
import hashlib
import io
import json
import unittest

from campaign_tool import law
from campaign_tool.digest.build import build_digest
from campaign_tool.digest.detectors import (
    _lines, detect_audit_log, detect_policy_elements, detect_public_hearing,
    detect_retention, detect_sharing, run_detectors)
from campaign_tool.digest.model import ModelConfig, ModelError
from campaign_tool.digest.model_input import ModelInputError, ModelSources

# Exactly Python's str.splitlines() terminators.
SEPARATORS = {"LF": "\n", "CRLF": "\r\n", "CR": "\r", "VT": "\x0b", "FF": "\x0c", "FS": "\x1c",
              "GS": "\x1d", "RS": "\x1e", "NEL": "\x85", "LS": "\u2028", "PS": "\u2029"}
NON_LF = {name: sep for name, sep in SEPARATORS.items() if name not in ("LF", "CRLF")}

PAGE = {"page": 7}
LOCATOR = {"page": 7, "member": "synthetic.pdf", "nested": {"members": ["a"], "deeper": {"path": "x"}}}


def unit(text, locator=None):
    return {"locator": deepcopy(PAGE if locator is None else locator), "text": text}


def digest_json(hits):
    return json.dumps(hits, sort_keys=True)


class LineSplitTests(unittest.TestCase):
    def test_every_splitlines_boundary_numbers_source_lines(self):
        for name, sep in SEPARATORS.items():
            with self.subTest(sep=name):
                text = f"one{sep}two{sep}three"
                self.assertEqual(list(_lines(unit(text))),
                                 [({"page": 7, "line": 1}, "one"), ({"page": 7, "line": 2}, "two"),
                                  ({"page": 7, "line": 3}, "three")])

    def test_mixed_crlf_blank_and_terminal_boundaries(self):
        mixed = "A\r\nB\rC\nD\x85E\u2028F\u2029G\x0bH\x0cI\x1cJ\x1dK\x1eL"
        self.assertEqual([(l["line"], t) for l, t in _lines(unit(mixed))],
                         list(enumerate("ABCDEFGHIJKL", 1)))  # CRLF counts once
        blanks = "\r\r x \u2028\x85"  # leading/interior blanks count; no phantom terminal line
        self.assertEqual([(l["line"], t) for l, t in _lines(unit(blanks))],
                         [(1, ""), (2, ""), (3, " x "), (4, "")])
        for sep in SEPARATORS.values():  # one terminated line is line 1, like legacy LF
            self.assertEqual(list(_lines(unit(f"abc{sep}"))), [({"page": 7, "line": 1}, "abc")])
        self.assertEqual(list(_lines(unit("\r"))), [({"page": 7, "line": 1}, "")])
        # Separator-free singleton and empty text keep the legacy locator and text.
        self.assertEqual(list(_lines(unit("abc"))), [({"page": 7}, "abc")])
        self.assertEqual(list(_lines(unit(""))), [({"page": 7}, "")])
        self.assertEqual(list(_lines({"text": None})), [({}, "")])

    def test_existing_line_locator_remains_authoritative(self):
        for sep in SEPARATORS.values():
            text = f"Data retained for 90 days.{sep}Board meets every 30 days."
            self.assertEqual(list(_lines({"locator": {"line": 17, "page": 2}, "text": text})),
                             [({"line": 17, "page": 2}, text)])
        text = "Intro\rData retained for 90 days.\rAudit performed."
        sources = ModelSources([{"locator": {"line": 17}, "text": text}])
        hits = detect_retention([{"locator": {"line": 17}, "text": text}], law.load_package("us-ca"))
        self.assertEqual([h["locator"] for h in hits], [{"line": 17}])
        sources.primary_payload(hits, [])
        with self.assertRaises(ModelInputError):
            sources.restore_output(model_output({"unit": 0, "line": 2}))


class DetectorBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.package = law.load_package("us-ca")

    def test_retention_context_cannot_cross_any_splitlines_boundary(self):
        for name, sep in SEPARATORS.items():
            with self.subTest(sep=name):
                unrelated = unit(f"Retention policy applies.{sep}The board meets every 90 days.")
                self.assertEqual(detect_retention([unrelated], self.package), [])
                genuine = unit(f"Intro.{sep}Data is retained for 90 days.{sep}Audit performed.")
                hits = detect_retention([genuine], self.package)
                self.assertEqual(len(hits), 1)
                self.assertEqual(hits[0]["locator"], {"page": 7, "line": 2})
                self.assertEqual(hits[0]["excerpt"], "Data is retained for 90 days.")
                self.assertEqual(hits[0]["detail"]["stated_days"], 90)

    def test_all_families_use_exact_non_lf_line_locators(self):
        lines = ["Intro.", "Data is retained for 30 days.", "Records shared with the FBI.",
                 "Immigration requests are refused.", "Authorized users must be trained.",
                 "An audit log is reviewed.", "Public hearing on this agenda item."]
        doc = {"scope": "document"}
        expected = [  # (detector, locator line or document, excerpt, element), detector order
            ("retention_period", 2, lines[1], None),
            ("out_of_state_or_federal_sharing", 3, lines[2], None),
            ("immigration_enforcement_mention", 4, lines[3], None),
            ("policy_element_present", 2, lines[1], "retention"),
            ("policy_element_present", 3, lines[2], "sharing"),
            ("policy_element_present", 5, lines[4], "authorized_users"),
            ("policy_element_present", 5, lines[4], "training"),
            ("policy_element_present", 6, lines[5], "monitoring"),
            ("policy_element_absent", doc, "", "authorized_purposes"),
            ("policy_element_absent", doc, "", "security"),
            ("policy_element_absent", doc, "", "accuracy"),
            ("policy_element_absent", doc, "", "posting"),
            ("policy_element_absent", doc, "", "policy_present"),
            ("audit_log_mention", 6, lines[5], None),
            ("public_hearing_mention", 7, lines[6], None),
        ]
        lf_output = None
        for name, sep in SEPARATORS.items():
            with self.subTest(sep=name):
                hits = run_detectors([unit(sep.join(lines))], self.package)
                got = [(h["detector"], h["locator"] if h["locator"] == doc else h["locator"]["line"],
                        h["excerpt"], h["detail"].get("element")) for h in hits]
                self.assertEqual(got, expected)
                self.assertTrue(all(h["locator"] == doc or h["locator"]["page"] == 7 for h in hits))
                if name == "LF":
                    lf_output = digest_json(hits)
                else:  # same serialized detector output as the long-standing LF behaviour
                    self.assertEqual(digest_json(hits), lf_output)

    def test_multiword_matches_do_not_bridge_logical_lines(self):
        cases = ((detect_sharing, "out of{s}state"), (detect_sharing, "national{s}lookup"),
                 (detect_audit_log, "access{s}log"),
                 (detect_public_hearing, "public{s}hearing"),
                 (detect_policy_elements, "authorized{s}users"))
        for name, sep in SEPARATORS.items():
            for detector, template in cases:
                with self.subTest(sep=name, detector=detector.__name__):
                    hits = detector([unit(template.format(s=sep))], self.package)
                    if detector is detect_sharing:
                        self.assertEqual(hits, [])
                    elif detector is detect_policy_elements:
                        absent = {h["detail"]["element"] for h in hits if h["kind"] == "absence"}
                        self.assertIn("authorized_users", absent)
                        self.assertEqual([h for h in hits if h["kind"] != "absence"], [])
                    else:
                        self.assertEqual([h["kind"] for h in hits], ["absence"])
                        self.assertEqual(hits[0]["locator"], {"scope": "document"})

    def test_lf_crlf_and_plain_legacy_outputs_are_byte_identical(self):
        # Hashes of run_detectors output captured from main 21ffc26a before the repair.
        for label, units in legacy_cases().items():
            with self.subTest(case=label):
                digest = hashlib.sha256(digest_json(run_detectors(units, self.package)).encode()).hexdigest()
                self.assertEqual(digest, LEGACY_SHA256[label])

    def test_original_text_and_nested_locator_metadata_are_unchanged(self):
        units = [unit("Intro\rData retained for 90 days.\u2028Audit performed.", LOCATOR),
                 unit("FBI\x85Other\x85Records", {"sheet": "S", "row": [1, {"k": "v"}]})]
        before = deepcopy(units)
        hits = run_detectors(units, self.package)
        self.assertEqual(units, before)
        self.assertTrue(any(h["locator"] == {**LOCATOR, "line": 2} for h in hits))
        sharing = [h["locator"] for h in hits if h["detector"] == "out_of_state_or_federal_sharing"]
        self.assertEqual(sharing, [{"sheet": "S", "row": [1, {"k": "v"}], "line": 1}])  # independent numbering
        self.assertEqual(units, before)


def model_output(locator):
    return {"scope": "Synthetic source", "actors": [],
            "dates": [],
            "statements": [{"text": "Data retained for 90 days.", "locator": deepcopy(locator)}],
            "omissions": [], "counterevidence": [],
            "conclusions": [{"text": "Retention is described.", "confidence": "needs_attorney_review",
                             "sources": [{"locator": deepcopy(locator), "rule_id": None}]}]}


class OpaqueProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.package = law.load_package("us-ca")
        self.text = "Intro\rData retained for 90 days.\rAudit performed."

    def units(self):
        return [{"locator": deepcopy(LOCATOR), "text": self.text}]

    def run_build(self, locator):
        captured = []

        def opener(request, **kwargs):
            captured.append(json.loads(request.data))
            content = json.dumps(model_output(locator))
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": content}}]}).encode())

        original = self.units()
        before = deepcopy(original)
        digest = build_digest("a" * 64, original, self.package, "us-ca", "draft", "redacted_cloud",
                              model_config=ModelConfig("https://model.example.invalid/v1"), opener=opener)
        self.assertEqual(original, before)
        return digest, captured

    def test_non_lf_model_hit_restores_exact_source_line(self):
        for name, sep in NON_LF.items():
            with self.subTest(sep=name):
                self.text = f"Intro{sep}Data retained for 90 days.{sep}Audit performed."
                digest, captured = self.run_build({"unit": 0, "line": 2})
                payload = json.loads(captured[0]["messages"][1]["content"])
                retention = [h for h in payload["detector_hits"] if h["detector"] == "retention_period"]
                self.assertEqual([h["locator"] for h in retention], [{"unit": 0, "line": 2}])
                self.assertNotIn("synthetic.pdf", json.dumps(captured))
                self.assertEqual(digest["statements"][0]["locator"], {**LOCATOR, "line": 2})
                self.assertEqual(digest["conclusions"][0]["sources"][0]["locator"], {**LOCATOR, "line": 2})
                restored = digest["statements"][0]["locator"]
                restored["nested"]["members"].append("changed")  # detached from the caller's unit
                self.assertEqual(LOCATOR["nested"]["members"], ["a"])

    def test_invalid_line_references_still_fail_closed(self):
        for bad in ({"unit": 0, "line": 0}, {"unit": 0, "line": 4}, {"unit": 0, "line": -1},
                    {"unit": 0, "line": True}, {"unit": 1, "line": 2}):
            with self.subTest(ref=bad), self.assertRaises(ModelError):
                self.run_build(bad)

    def test_truncated_source_leaves_only_issued_hit_line_citable(self):
        units = self.units()
        hits = detect_retention(units, self.package)
        self.assertEqual([h["locator"] for h in hits], [{**LOCATOR, "line": 2}])
        sources = ModelSources(units)
        self.assertTrue(sources.primary_payload(hits, [], max_chars=0)["truncated"])
        restored = sources.restore_output(model_output({"unit": 0, "line": 2}))
        self.assertEqual(restored["statements"][0]["locator"], {**LOCATOR, "line": 2})
        for refused in ({"unit": 0}, {"unit": 0, "line": 1}, {"unit": 0, "line": 3}, {"unit": 0, "line": 0}):
            with self.subTest(ref=refused), self.assertRaises(ModelInputError):
                sources.restore_output(model_output(refused))


def legacy_cases():
    lf = "Intro\nData retained for 90 days.\nFBI sharing; ICE request.\nAudit log kept 6 months.\nPublic hearing held."
    return {
        "lf": [unit(lf)],
        "crlf": [unit(lf.replace("\n", "\r\n"), LOCATOR)],
        "plain": [unit("Data retained for 90 days; the FBI audit log: public hearing, authorized users.")],
        "existing_line": [{"locator": {"line": 17}, "text": "Policy: data retained for 1 year\nAudit performed"},
                          {"locator": {"line": 3, "page": 2}, "text": "Training provided."}],
        "empty": [unit(""), {"text": None}],
    }


LEGACY_SHA256 = {
    "lf": "6df5444413e0e3b4f8b2f1f3529d0be14ab399f214a2bb3ee529d140a3b314f4",
    "crlf": "c77472fe5c0b78b1056644c3b01b0c6929f63c2c15b3f335289c31e075210173",
    "plain": "6a2ee9532ee8b725d102c0091f2dc9e6c98901a269766772bc11128b6f88756f",
    "existing_line": "1f8853ae8b431f699d680b9e04890883e6b5d221edde6d43289527c47865d9b6",
    "empty": "8588241dd42c89c88e8576f3bc6740b4ac0689ea0f1a69d559c9021bf7e356ba",
}
