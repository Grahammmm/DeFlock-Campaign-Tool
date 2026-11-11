"""Sharing-lead output refuses control-bearing rendered agency identifiers. Synthetic fixtures; nothing is fetched."""
import contextlib
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import discovery, kit_sharing_suggest, registry  # noqa: E402
from tests import test_kit_sharing_suggest as base  # noqa: E402

MESSAGE = "a receiving agency identifier contains a control character"
CONTROLS = ("\x00", "\t", "\n", "\r", "\x1b", "\x7f", "\x85", "\u2028", "\u2029")


class IdentifierTests(unittest.TestCase):
    def setUp(self):
        # Reuse the fixture builders without importing a discoverable TestCase.
        self.fixture = base.SuggestTests("test_exact_matches_counts_status_and_notices")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.base = self.fixture.root, self.fixture.base

    def report(self):
        return kit_sharing_suggest.build_report(self.root, seed=base.SEED)

    def refuses(self, report):
        before = copy.deepcopy(report)
        with self.assertRaises(ValueError) as caught:
            kit_sharing_suggest.render(report)
        self.assertEqual(str(caught.exception), MESSAGE)
        self.assertEqual(report, before)

    def test_rendered_identifier_locations_obey_existing_control_policy(self):
        for char in CONTROLS:
            with self.subTest(location="suggestion", char=repr(char)):
                report = self.report()
                report["suggestions"][0]["agency_id"] += char
                self.refuses(report)
            with self.subTest(location="candidate", char=repr(char)):
                report = self.report()
                report["ambiguous"][0]["candidates"][1]["agency_id"] += char
                self.refuses(report)

    def test_late_identifier_refuses_entire_report(self):
        row = self.report()["ambiguous"][0]
        for position in (0, -1):
            report = self.report()
            report["suggestions"][position]["agency_id"] += "\n"
            self.refuses(report)
        for position in (0, 1):
            report = self.report()
            report["ambiguous"][0]["candidates"][position]["agency_id"] += "\n"
            self.refuses(report)
        bad = {**row, "candidates": [{"agency_id": "ca-x\n", "name": "Twin Police"}]}
        report = self.report()
        report["ambiguous"] = [row, bad]
        self.refuses(report)
        report["ambiguous"] = [bad, row]
        self.refuses(report)

    def test_existing_name_refusals_are_not_regressed(self):
        name = "a receiving agency name contains a control character"
        report = self.report()
        report["suggestions"][0]["name"] = "Bad\tName"
        report["suggestions"][0]["agency_id"] += "\n"  # name check keeps precedence
        with self.assertRaises(ValueError) as caught:
            kit_sharing_suggest.render(report)
        self.assertEqual(str(caught.exception), name)
        report = self.report()
        report["ambiguous"][0]["candidates"][1]["name"] = "Twin\nPolice"
        with self.assertRaises(ValueError) as caught:
            kit_sharing_suggest.render(report)
        self.assertEqual(str(caught.exception), name)

    def test_valid_report_bytes_and_identity_contracts_remain_unchanged(self):
        report = self.report()
        text = kit_sharing_suggest.render(report)
        self.assertEqual(text, kit_sharing_suggest.render(self.report()))
        self.assertIn("[ca-alpha-pd] - selected", text)
        self.assertIn("candidates: ca-twin-a (Twin Police); ca-twin-b (twin  police.)", text)
        self.assertEqual(text.rstrip("\n").split("\n")[-1], base.TRAILER)

    def lf_seed_directory(self, **replacements):
        directory = self.fixture.seed_directory("Twin Police")
        seed = json.loads((directory / "us-ca.json").read_text())
        for agency in seed["counties"][0]["agencies"]:
            if agency["agency_id"] in replacements:
                agency["agency_id"] += "\n"
        (directory / "us-ca.json").write_text(json.dumps(seed), encoding="utf-8")
        return directory

    def test_real_loader_trailing_lf_identifier_refuses_unique_and_ambiguous_cli(self):
        for target, label in (("ca-alpha-pd", "suggestion"), ("ca-twin-b", "candidate")):
            with self.subTest(label):
                directory = self.lf_seed_directory(**{target: True})
                seed = discovery.load_seed("us-ca", base=directory)  # real schema loader accepts it
                ids = [a["agency_id"] for c in seed["counties"] for a in c["agencies"]]
                self.assertIn(target + "\n", ids)
                report = kit_sharing_suggest.build_report(self.root, seed=seed)
                found = [s["agency_id"] for s in report["suggestions"]]
                found += [c["agency_id"] for a in report["ambiguous"] for c in a["candidates"]]
                self.assertIn(target + "\n", found)
                before = base.tree(self.base)
                code, out, err = self.fixture.run_real_loader_cli(directory)
                self.assertEqual((code, out, err), (1, "", "Stopped: " + MESSAGE + "\n"))
                with self.assertRaises(ValueError) as caught:
                    kit_sharing_suggest.suggest_from_sharing(self.root, seed=seed)
                self.assertEqual(str(caught.exception), MESSAGE)
                self.assertEqual(before, base.tree(self.base))

    def test_identifier_is_not_repaired_into_selected_kit_entry(self):
        report = kit_sharing_suggest.build_report(self.root, seed=base.SEED)
        seed = copy.deepcopy(base.SEED)
        seed["counties"][0]["agencies"][0]["agency_id"] = "ca-alpha-pd\n"
        report = kit_sharing_suggest.build_report(self.root, seed=seed)
        entry = next(s for s in report["suggestions"] if s["agency_id"] == "ca-alpha-pd\n")
        self.assertEqual(entry["status"], "not in kit")
        self.assertFalse(any(s["agency_id"] == "ca-alpha-pd" for s in report["suggestions"]))
        with self.assertRaises(ValueError):
            kit_sharing_suggest.render(report)

    def test_identifier_refusal_is_private_atomic_and_read_only(self):
        directory = self.lf_seed_directory(**{"ca-alpha-pd": True})
        before = base.tree(self.base)

        def fail(*_a, **_k):
            raise AssertionError("forbidden call")
        with contextlib.ExitStack() as stack:
            for target in ("urllib.request.urlopen", "socket.socket.connect", "subprocess.run",
                           "campaign_tool.kit._write_json", "campaign_tool.kit_agencies._save"):
                stack.enter_context(mock.patch(target, side_effect=fail))
            code, out, err = self.fixture.run_real_loader_cli(directory)
        self.assertEqual((code, out), (1, ""))
        self.assertNotIn("ca-alpha", err)
        self.assertNotIn("Alpha", err)
        self.assertEqual(before, base.tree(self.base))
        self.assertFalse((self.root / "private").exists())

    def test_unrendered_identifiers_do_not_expand_refusal_scope(self):
        seed = copy.deepcopy(base.SEED)
        seed["counties"][0]["agencies"].append(base.seed_agency("ca-unlisted\n", "Unlisted Police"))
        report = kit_sharing_suggest.build_report(self.root, seed=seed)
        self.assertNotIn("ca-unlisted", kit_sharing_suggest.render(report))
        self.fixture.senders = []
        self.fixture.write_content()
        report = kit_sharing_suggest.build_report(self.root, seed=seed)
        self.assertIn("No recorded sharing lists.", kit_sharing_suggest.render(report))


if __name__ == "__main__":
    unittest.main()
