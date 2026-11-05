"""kit-agency suggest --from-sharing: receiving agencies as private request leads. Synthetic fixtures; nothing is fetched."""
import contextlib
import copy
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import discovery, registry  # noqa: E402
from campaign_tool.cli import main  # noqa: E402

NOTICE = "Receiving data is a reason to ask, not evidence of misuse"
TRAILER = "Private, read-only; nothing was selected, written, fetched or sent."
SOURCE = "https://transparency.flocksafety.com/synthetic"


def seed_agency(agency_id, name, kind="police"):
    return {"agency_id": agency_id, "name": name, "kind": kind, "jurisdiction_name": "Synthetic", "place_fips": None}


SEED = {"schema_version": 1, "jurisdiction": "us-ca", "counties": [
    {"county_fips": "06901", "name": "Alpha", "seat": "A", "agencies": [
        seed_agency("ca-alpha-pd", "Alpha Police Department"),
        seed_agency("ca-alpha-so", "Alpha County Sheriff's Office", "sheriff"),
        seed_agency("ca-twin-a", "Twin Police"), seed_agency("ca-twin-b", "twin  police."),
        seed_agency("ca-harbor", "Harbor Police")]},
    {"county_fips": "06902", "name": "Beta", "seat": "B", "agencies": [
        seed_agency("ca-beta-pd", "Beta Police Department"),
        seed_agency("ca-harbor", "Harbor Police")]}]}

SENDER_1 = [
    {"name": "Alpha Police Department", "state": "CA", "kind": "local"},
    {"name": "beta  police department.", "state": "CA", "kind": "local"},
    {"name": "Beta Police Department", "state": "CA", "kind": "unknown"},
    {"name": "Twin Police", "state": "CA", "kind": "local"},
    {"name": "Nowhere Constabulary", "state": "CA", "kind": "local"},
    {"name": "Alpha Police Department.", "state": "NV", "kind": "local"},
    {"name": "Federal Bureau", "state": "US", "kind": "federal"},
    {"name": "Fed CA", "state": "CA", "kind": "federal"},
    {"name": "Acme Private", "state": "CA", "kind": "private"},
    {"name": "Acme Private NV", "state": "NV", "kind": "private"}]
SENDER_2 = [
    {"name": "ALPHA POLICE DEPARTMENT", "state": "CA", "kind": "local"},
    {"name": "Alpha Police Department.", "state": "CA", "kind": "local"},
    {"name": "Harbor Police", "state": "CA", "kind": "local"},
    {"name": "Alpha Sheriff", "state": "CA", "kind": "local"},
    {"name": "Caf\u00e9 PD", "state": "CA", "kind": "local"}]


def tree(root):
    return {str(p): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode), p.stat().st_mtime_ns) if p.is_file() else None
            for p in sorted(Path(root).rglob("*"))}


class SuggestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-suggest-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(os.path.realpath(temporary.name))
        self.root = self.base / "campaign"
        (self.root / "kit").mkdir(parents=True)
        (self.root / "content").mkdir()
        self.write_json("campaign.json", {"schema_version": 1, "name": "Synthetic", "county": "Alpha", "state": "CA"})
        self.kit = [{"agency_id": "ca-alpha-pd", "name": "Alpha PD", "kind": "police", "selected": True},
                    {"agency_id": "ca-beta-pd", "name": "Beta PD", "kind": "police", "selected": False},
                    {"agency_id": "ca-alpha-so", "name": "Alpha SO", "kind": "sheriff"},
                    {"agency_id": "ca-organizer-homonym", "name": "Twin Police", "kind": "police", "selected": True}]
        self.write_json("kit/agencies.json", self.kit)
        self.senders = [("ca-sender-one", SENDER_1), ("ca-sender-two", SENDER_2)]
        self.write_content()
        patch = mock.patch.dict(os.environ, {registry.ENV: str(self.base / "config" / "campaigns.json")})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(registry.release)

    def write_json(self, name, value):
        (self.root / name).write_text(json.dumps(value), encoding="utf-8")

    def write_content(self):
        entries = []
        for agency_id, recipients in self.senders:
            entry = {"agency_id": agency_id, "name": agency_id, "kind": "police"}
            if recipients is not None:
                entry["profile"] = {"sharing": {"checked_on": "2026-02-01", "source_url": SOURCE, "recipients": recipients}}
            entries.append(entry)
        self.write_json("content/agencies.json", entries)

    def run_cli(self, *args, seed=SEED):
        argv = ["campaign_tool", "kit-agency", "suggest", *map(str, args)]
        out, err, code = io.StringIO(), io.StringIO(), 0
        with mock.patch.object(sys, "argv", argv), mock.patch.object(discovery, "load_seed", return_value=seed), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                main()
            except SystemExit as exc:
                code = exc.code or 0
            finally:
                registry.release()
        return code, out.getvalue(), err.getvalue()

    def suggest(self):
        code, out, err = self.run_cli("--from-sharing", "--directory", self.root)
        self.assertEqual((code, err), (0, ""), out)
        return out

    def refused(self, *words):
        before = tree(self.base)
        code, out, err = self.run_cli("--from-sharing", "--directory", self.root)
        self.assertEqual((code, out), (1, ""), err)
        for word in words:
            self.assertIn(word, err)
        self.assertEqual(before, tree(self.base))

    def section(self, out, title):
        lines = out.split("\n")
        start = next(i for i, line in enumerate(lines) if line.startswith(title))
        end = next((i for i in range(start + 1, len(lines)) if lines[i] == ""), len(lines))
        return lines[start + 1:end]

    def test_exact_matches_counts_status_and_notices(self):
        out = self.suggest()
        self.assertIn(NOTICE + ".", out)
        self.assertEqual(out.rstrip("\n").split("\n")[-1], TRAILER)
        leads = self.section(out, "Suggested agencies")
        # Alpha PD: sender one (3 spellings incl. Alpha Police Department.) and two -> 2 senders; selected by exact ID
        self.assertEqual(len(leads), 3, leads)
        self.assertIn("Alpha Police Department [ca-alpha-pd] - selected - listed by 2 of the campaign's agencies", leads[0])
        self.assertIn("ALPHA POLICE DEPARTMENT; Alpha Police Department; Alpha Police Department.", leads[0])
        # Beta: two identities (local, unknown kind) collapse to ONE suggestion, one sender; deselected
        self.assertIn("Beta Police Department [ca-beta-pd] - not selected - listed by 1 of", leads[1])
        self.assertIn("Beta Police Department; beta  police department.", leads[1])
        self.assertEqual(sum("[ca-beta-pd]" in line for line in leads), 1)
        self.assertIn("Unique suggested seed agencies: 3", out)

    def test_default_true_and_not_in_kit_and_homonym(self):
        self.senders[1] = ("ca-sender-two", SENDER_2 + [{"name": "Alpha County Sheriff's Office", "state": "CA",
                                                         "kind": "state"}])
        self.write_content()
        out = self.suggest()
        self.assertIn("[ca-alpha-so] - selected", out)  # omitted selected follows the true default
        self.assertIn("Twin Police - CA, local - listed by 1; candidates: ca-twin-a (Twin Police); ca-twin-b (twin  police.)",
                      out)
        self.assertNotIn("ca-organizer-homonym", out)  # a same-named organizer entry is not the seed entry

    def test_not_in_kit_is_distinct_from_not_selected(self):
        self.write_json("kit/agencies.json", [e for e in self.kit if e["agency_id"] != "ca-beta-pd"])
        out = self.suggest()
        self.assertIn("[ca-beta-pd] - not in kit", out)
        self.assertIn("kit-agency select does not add it", out)
        self.assertNotIn("[ca-beta-pd] - not selected", out)

    def test_ambiguous_and_unmatched_listed_as_written_with_no_recommendation(self):
        out = self.suggest()
        ambiguous = self.section(out, "Ambiguous names")
        # sender one's Twin Police and sender two's Harbor Police (ca-harbor repeated in two counties is ONE agency)
        self.assertEqual(len(ambiguous), 1, ambiguous)
        self.assertIn("candidates: ca-twin-a (Twin Police); ca-twin-b (twin  police.)", ambiguous[0])
        self.assertIn("[ca-harbor] - not in kit - listed by 1", out)
        unmatched = self.section(out, "Unmatched names")
        self.assertEqual(sorted(unmatched), sorted([
            "  Alpha Sheriff - CA, local - listed by 1", "  Café PD - CA, local - listed by 1",
            "  Nowhere Constabulary - CA, local - listed by 1"]))
        self.assertIn("none is recommended", out)

    def test_exclusions_are_counted_not_named(self):
        out = self.suggest()
        self.assertIn("federal 2, private 2, out-of-state 1", out)  # US federal + CA federal; CA/NV private; NV local
        for name in ("Federal Bureau", "Fed CA", "Acme Private", "NV"):
            self.assertNotIn(name, out)

    def test_normalization_has_no_fuzzy_matching(self):
        self.senders = [("ca-sender-one", [
            {"name": "Alpha Police Department..", "state": "CA", "kind": "local"},
            {"name": "Alpha Police Dept", "state": "CA", "kind": "local"},
            {"name": "Alpha  Police\u00a0Department", "state": "CA", "kind": "local"},
            {"name": "Alpha Pol\u0069ce Department", "state": "CA", "kind": "local"},
            {"name": "Alpha-Police Department", "state": "CA", "kind": "local"}])]
        self.write_content()
        out = self.suggest()
        self.assertEqual(self.section(out, "Suggested agencies"), ["  Alpha Police Department [ca-alpha-pd] - selected - "
                                                                    "listed by 1 of the campaign's agencies; written as: "
                                                                    "Alpha  Police\u00a0Department; Alpha Pol\u0069ce Department"])
        unmatched = "\n".join(self.section(out, "Unmatched names"))
        for written in ("Alpha Police Department..", "Alpha Police Dept", "Alpha-Police Department"):
            self.assertIn(written, unmatched)

    def test_sender_duplicates_count_once_and_senders_independent_of_selection(self):
        self.senders = [("ca-sender-one", [{"name": "Alpha Police Department", "state": "CA", "kind": "local"},
                                           {"name": "Alpha Police Department.", "state": "CA", "kind": "local"}])]
        self.write_content()
        self.assertIn("listed by 1 of the campaign's agencies", self.suggest())

    def test_inconsistent_duplicate_seed_id_refuses(self):
        seed = copy.deepcopy(SEED)
        seed["counties"][1]["agencies"][1]["name"] = "Harbor Police Dept"
        before = tree(self.base)
        code, out, err = self.run_cli("--from-sharing", "--directory", self.root, seed=seed)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("ca-harbor inconsistently", err)
        self.assertEqual(before, tree(self.base))

    def test_missing_lists_is_an_empty_success(self):
        for senders in ([("ca-sender-one", None)], []):
            self.senders = senders
            self.write_content()
            out = self.suggest()
            self.assertIn("No recorded sharing lists.", out)
            self.assertIn("Unique suggested seed agencies: 0", out)
            self.assertIn("federal 0, private 0, out-of-state 0", out)
            self.assertIn(NOTICE, out)
        (self.root / "content" / "agencies.json").unlink()
        self.assertIn("No recorded sharing lists.", self.suggest())
        self.assertFalse((self.root / "private").exists())

    def test_malformed_input_refuses_without_output_or_writes(self):
        self.senders = [("ca-sender-one", SENDER_1[:1] + [{"name": "Bad", "state": "ZZ", "kind": "local"}])]
        self.write_content()
        self.refused("recipients[1].state")
        (self.root / "content" / "agencies.json").write_text("not json", encoding="utf-8")
        self.refused("agencies.json")

    def test_kit_problems_refuse(self):
        (self.root / "kit" / "agencies.json").unlink()
        self.refused("kit/agencies.json is missing")
        real = self.base / "real.json"
        real.write_text(json.dumps(self.kit), encoding="utf-8")
        (self.root / "kit" / "agencies.json").symlink_to(real)
        self.refused("kit/agencies.json is missing")
        (self.root / "kit" / "agencies.json").unlink()
        self.write_json("kit/agencies.json", self.kit + [self.kit[0]])
        self.refused("duplicate agency_id ca-alpha-pd")
        self.write_json("kit/agencies.json", [{**self.kit[0], "selected": "yes"}])
        self.refused("selected must be true or false")

    def test_invalid_config_and_seed_refuse(self):
        self.write_json("campaign.json", {"schema_version": 1, "name": "S", "county": "Alpha", "state": "California"})
        self.refused("two-letter state")
        self.write_json("campaign.json", {"schema_version": 1, "name": "S", "county": "Alpha", "state": "CA"})
        with mock.patch.object(discovery, "load_seed", side_effect=ValueError("No agency seed at nowhere")):
            before = tree(self.base)
            with mock.patch.object(sys, "argv", ["campaign_tool", "kit-agency", "suggest", "--from-sharing",
                                                 "--directory", str(self.root)]), \
                    contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as caught:
                main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("No agency seed", err.getvalue())
        self.assertEqual(before, tree(self.base))

    def test_control_characters_in_a_name_refuse(self):
        from campaign_tool import kit_sharing_suggest
        report = kit_sharing_suggest.build_report(self.root, seed=SEED)
        report["unmatched"][0]["names"] = ["Bad\x1b[31mName"]
        with self.assertRaises(ValueError):
            kit_sharing_suggest.render(report)

    def test_parser_rejects_other_modes_and_writes_nothing(self):
        before = tree(self.base)
        for extra in (["--id", "ca-alpha-pd"], ["--name", "X"], ["--csv", "x.csv"], ["--check"], ["--replace"],
                      ["--online"], ["--apply"], ["--json"], ["--out", "x"]):
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()):
                code, out, _err = self.run_cli("--from-sharing", "--directory", self.root, *extra)
                self.assertEqual((code, out), (2, ""))
        code, out, _err = self.run_cli("--directory", self.root)  # --from-sharing is required
        self.assertEqual((code, out), (2, ""))
        code, out, _err = self.run_cli("--from-sharing")  # a directory (or --campaign) is required
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(before, tree(self.base))

    def test_read_only_repeatable_and_legacy_commands_unchanged(self):
        before = tree(self.base)

        def fail(*_a, **_k):
            raise AssertionError("forbidden call")
        with contextlib.ExitStack() as stack:
            for target in ("urllib.request.urlopen", "socket.socket.connect", "subprocess.run", "os.mkdir", "os.rename",
                           "os.replace", "os.chmod", "campaign_tool.kit._write_json", "campaign_tool.kit_agencies._save",
                           "campaign_tool.kit_agencies.set_selected", "campaign_tool.kit_agencies.add_agency",
                           "campaign_tool.kit_agencies.import_csv", "campaign_tool.kit.build_kit"):
                stack.enter_context(mock.patch(target, side_effect=fail))
            first, second = self.suggest(), self.suggest()
        self.assertEqual(first, second)
        self.assertEqual(before, tree(self.base))
        # the legacy select command still writes and still says Nothing was sent.
        argv = ["campaign_tool", "kit-agency", "select", "--directory", str(self.root), "--id", "ca-beta-pd"]
        out = io.StringIO()
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out):
            main()
        registry.release()
        self.assertIn("Nothing was sent.", out.getvalue())
        self.assertTrue(json.loads((self.root / "kit" / "agencies.json").read_text())[1]["selected"])


if __name__ == "__main__":
    unittest.main()
