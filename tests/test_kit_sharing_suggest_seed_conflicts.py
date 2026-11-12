"""Conflicting duplicate seed ids with control characters refuse with the fixed diagnostic. Synthetic; nothing is fetched."""
import copy
import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import discovery, kit_sharing_suggest  # noqa: E402
from tests import test_kit_sharing_suggest as base  # noqa: E402

MESSAGE = "a receiving agency identifier contains a control character"
CONTROLS = ("\x00", "\t", "\n", "\r", "\x1b", "\x7f", "\x85", "\u2028", "\u2029")


def conflict_seed(suffix="", dimension="name", first_conflicts=False):
    """Harbor id (plus suffix) repeated in both counties; one definition differs in name or kind."""
    seed = copy.deepcopy(base.SEED)
    harbor = [c["agencies"][-1] for c in seed["counties"]]
    for entry in harbor:
        entry["agency_id"] += suffix
    changed = harbor[0 if first_conflicts else 1]
    if dimension == "name":
        changed["name"] = "Harbor Police Dept"
    else:
        changed["kind"] = "sheriff"
    return seed


class SeedConflictTests(unittest.TestCase):
    def setUp(self):
        self.fixture = base.SuggestTests("test_exact_matches_counts_status_and_notices")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.base = self.fixture.root, self.fixture.base

    def refuses(self, seed, message=MESSAGE):
        before = copy.deepcopy(seed)
        for call in (kit_sharing_suggest._seed_index,
                     lambda s: kit_sharing_suggest.build_report(self.root, seed=s),
                     lambda s: kit_sharing_suggest.suggest_from_sharing(self.root, seed=s)):
            with self.assertRaises(ValueError) as caught:
                call(seed)
            self.assertEqual(str(caught.exception), message)
        self.assertEqual(seed, before)

    def test_conflicting_duplicate_ids_obey_existing_control_policy(self):
        for char in CONTROLS:
            for dimension in ("name", "kind"):
                for first in (False, True):
                    with self.subTest(char=repr(char), dimension=dimension, first=first):
                        self.refuses(conflict_seed(char, dimension, first))

    def test_conflict_position_and_order_do_not_leak_or_select(self):
        seed = conflict_seed("\n")
        extra = copy.deepcopy(seed["counties"][0])
        extra["county_fips"] = "06903"
        extra["agencies"] = [copy.deepcopy(seed["counties"][1]["agencies"][-1])]
        extra["agencies"][0]["name"] = "Third Harbor"
        seed["counties"].append(extra)  # late third occurrence conflicts too
        self.refuses(seed)
        seed["counties"].reverse()
        self.refuses(seed)

    def test_real_loader_lf_conflicts_refuse_through_api_and_cli(self):
        for dimension in ("name", "kind"):
            with self.subTest(dimension):
                seed = conflict_seed("\n", dimension)
                directory = self.fixture.seed_directory("Twin Police")
                full = json.loads((directory / "us-ca.json").read_text())
                for county, source in zip(full["counties"], seed["counties"]):
                    for agency, wanted in zip(county["agencies"][-1:], source["agencies"][-1:]):
                        agency.update(agency_id=wanted["agency_id"], name=wanted["name"], kind=wanted["kind"])
                (directory / "us-ca.json").write_text(json.dumps(full), encoding="utf-8")
                loaded = discovery.load_seed("us-ca", base=directory)
                self.assertEqual([c["agencies"][-1]["agency_id"] for c in loaded["counties"]], ["ca-harbor\n"] * 2)
                self.refuses(loaded)
                before = base.tree(self.base)
                code, out, err = self.fixture.run_real_loader_cli(directory)
                self.assertEqual((code, out, err), (1, "", "Stopped: " + MESSAGE + "\n"))
                self.assertEqual(before, base.tree(self.base))

    def test_ordinary_inconsistent_ids_keep_exact_old_message(self):
        for dimension in ("name", "kind"):
            with self.subTest(dimension), self.assertRaises(ValueError) as caught:
                kit_sharing_suggest._seed_index(conflict_seed("", dimension))
            self.assertEqual(str(caught.exception), "seed defines agency_id ca-harbor inconsistently")
        code, out, err = self.fixture.run_cli("--from-sharing", "--directory", self.root, seed=conflict_seed())
        self.assertEqual((code, out, err), (1, "", "Stopped: seed defines agency_id ca-harbor inconsistently\n"))

    def test_identical_duplicates_and_irrelevant_ids_preserve_prior_scope(self):
        seed = copy.deepcopy(base.SEED)
        seed["counties"][1]["agencies"].append(base.seed_agency("ca-unrelated\n", "Unrelated Police"))
        report = kit_sharing_suggest.build_report(self.root, seed=seed)  # no blanket validator
        self.assertEqual(len([s for s in report["suggestions"] if s["agency_id"] == "ca-harbor"]), 1)
        identical = conflict_seed("\n")
        identical["counties"][1]["agencies"][-1] = copy.deepcopy(identical["counties"][0]["agencies"][-1])
        self.assertEqual(len(kit_sharing_suggest._seed_index(identical)), 6)
        with self.assertRaises(ValueError) as caught:  # still the existing renderer refusal
            kit_sharing_suggest.render(kit_sharing_suggest.build_report(self.root, seed=identical))
        self.assertEqual(str(caught.exception), MESSAGE)

    def test_conflict_refuses_with_no_sharing_lists_and_stays_read_only(self):
        self.fixture.senders = [("ca-sender-one", None)]
        self.fixture.write_content()
        seed = conflict_seed("\n")
        before = base.tree(self.base)
        code, out, err = self.fixture.run_cli("--from-sharing", "--directory", self.root, seed=seed)
        self.assertEqual((code, out, err), (1, "", "Stopped: " + MESSAGE + "\n"))
        self.assertEqual(before, base.tree(self.base))

    def test_healthy_report_is_unchanged(self):
        text = kit_sharing_suggest.suggest_from_sharing(self.root, seed=base.SEED)
        self.assertEqual(text, kit_sharing_suggest.suggest_from_sharing(self.root, seed=copy.deepcopy(base.SEED)))
        self.assertIn("[ca-alpha-pd] - selected", text)
        self.assertEqual(text.rstrip("\n").split("\n")[-1], base.TRAILER)


if __name__ == "__main__":
    unittest.main()
