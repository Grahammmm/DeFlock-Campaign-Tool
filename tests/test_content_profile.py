"""DF1109B-09: ``content agency-profile list|recheck``. Synthetic agencies in temporary storage; nothing is fetched.

Run from the repository root: python3 -B -m unittest tests.test_content_profile -v
"""
import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, content, content_profile  # noqa: E402
from campaign_tool.content import load_agencies  # noqa: E402

TODAY = date.today()
DAY = TODAY.isoformat()


class _FrozenDate(date):
    """MAIN-RED: pin the product's clock to TODAY so a UTC-midnight rollover mid-run cannot move "today"."""

    @classmethod
    def today(cls):
        return TODAY


_CLOCK_PATCHES = []


def setUpModule():
    for module in (content, content_profile):
        patch = mock.patch.object(module, "date", _FrozenDate)
        patch.start()
        _CLOCK_PATCHES.append(patch)


def tearDownModule():
    while _CLOCK_PATCHES:
        _CLOCK_PATCHES.pop().stop()


A, B = "ca-alpha-pd", "ca-beta-so"
PORTAL = "https://transparency.flocksafety.com/alpha-pd"
POLICY_IDS = ("authorized_purposes", "authorized_users_training", "security_monitoring", "sharing_restrictions",
              "custodian_title", "accuracy_correction", "retention_destruction")
CONTRACT_IDS = ("term_renewal", "price_changes", "data_ownership", "retention", "sharing_network", "vendor_data_use",
                "audit_search_logs", "termination_data_exit")


def ago(days):
    return (TODAY - timedelta(days=days)).isoformat()


def agencies():
    alpha = {
        "agency_id": A, "slug": "alpha", "name": "Alpha Police Department (fictional)", "kind": "police",
        "verified": True, "flock_transparency_slug": "alpha-pd",
        "verification": {"source_url": "https://example.invalid/alpha/contact", "checked_on": ago(400), "fields": []},
        "profile": {
            "status": "Records requested",
            "alpr_policy": {"url": "https://example.invalid/alpha/policy.pdf", "source_url": "https://example.invalid/alpha",
                            "checked_on": ago(40)},
            "policy_check": {"checked_on": ago(30), "policy_url": "https://example.invalid/alpha/policy.pdf",
                             "elements": {key: {"status": "unclear"} for key in POLICY_IDS}},
            "contract": {"vendor": "Synthetic vendor", "start": "2025-01-01", "end": "2027-12-31",
                         "source_url": "https://example.invalid/alpha/agreement.pdf", "checked_on": ago(50)},
            "contract_check": {"checked_on": ago(45), "contract_url": "https://example.invalid/alpha/agreement.pdf",
                               "elements": {key: {"status": "not_found"} for key in CONTRACT_IDS}},
            "pre_implementation_meeting": {"status": "not_found_yet", "checked_on": ago(20)},
            "sharing": {"checked_on": ago(10), "source_url": PORTAL,
                        "recipients": [{"name": "Neighbour PD (fictional)", "state": "CA", "kind": "local"}]},
            "funding": [{"kind": "grant", "name": "Synthetic Program", "as_stated_in": "Award letter",
                         "source_url": "https://example.invalid/alpha/award", "checked_on": ago(60)},
                        {"kind": "general_fund", "name": "General fund", "as_stated_in": "Budget",
                         "source_url": "https://example.invalid/alpha/budget", "checked_on": ago(5)}],
            "transparency": [{"checked_on": ago(3), "source_url": PORTAL, "cameras": 12},
                             {"checked_on": ago(30), "source_url": PORTAL, "cameras": 10},
                             {"checked_on": ago(90), "source_url": PORTAL, "cameras": 8}],
            "reports": [{"title": "Annual report", "kind": "annual_report", "period_start": "2025-01-01",
                         "period_end": "2025-12-31", "source_url": "https://example.invalid/alpha/report",
                         "checked_on": DAY}]}}
    beta = {"agency_id": B, "name": "Beta Sheriff (fictional)", "kind": "sheriff",
            "profile": {"funding": [{"kind": "other", "name": "Ünïcode fund", "as_stated_in": "Minutes",
                                     "source_url": "https://example.invalid/beta/minutes", "checked_on": ago(30)}]}}
    gamma = {"agency_id": "ca-gamma-pd", "name": "Gamma (deselected)", "selected": False,
             "profile": {"alpr_policy": {"url": "https://example.invalid/g.pdf", "source_url": "https://example.invalid/g",
                                         "checked_on": ago(999)}}}
    return [alpha, beta, gamma]


EXPECTED = [  # (agency, field, index, days old), oldest first; ties by agency then field then index
    (A, "transparency", 3, 90), (A, "funding", 1, 60), (A, "contract", None, 50), (A, "contract_check", None, 45),
    (A, "alpr_policy", None, 40), (A, "policy_check", None, 30), (A, "transparency", 2, 30), (B, "funding", 1, 30),
    (A, "pre_implementation_meeting", None, 20), (A, "sharing", None, 10), (A, "funding", 2, 5),
    (A, "transparency", 1, 3), (A, "reports", 1, 0)]


def run(*arguments):
    out, err, code = io.StringIO(), io.StringIO(), 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return code, out.getvalue(), err.getvalue()


class ContentProfileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-agency-profile-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "campaign"
        (self.root / "content").mkdir(parents=True)
        self.target = self.root / "content" / "agencies.json"
        self.write(agencies())

    def write(self, document, compact=False):
        self.target.write_text(json.dumps(document, ensure_ascii=False, indent=None if compact else 2) + "\n", encoding="utf-8")

    def stored(self):
        return json.loads(self.target.read_text(encoding="utf-8"))

    def snapshot(self):
        return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in (self.root / "content").iterdir()}

    def recheck(self, *extra):
        return run("content", "agency-profile", "recheck", "--directory", self.root, *extra)

    def assert_refused(self, code, out, err, needle, status=1):
        self.assertEqual(code, status, err)
        self.assertEqual(out, "")
        self.assertIn(needle, err)

    # --- list ---------------------------------------------------------------------------------------------------
    def test_list_all_nine_families_oldest_first_original_indexes(self):
        rows = content_profile.list_profile_checks(self.root)
        self.assertEqual([(r["agency_id"], r["field"], r["index"], r["age_days"]) for r in rows], EXPECTED)
        self.assertEqual({tuple(r) for r in rows}, {("agency_id", "field", "index", "checked_on", "age_days")})
        self.assertTrue(all(r["checked_on"] == ago(r["age_days"]) for r in rows))
        code, out, err = run("content", "agency-profile", "list", "--directory", self.root)
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(len(lines), len(EXPECTED))
        self.assertEqual(lines[0], f"{A} | transparency[3] | checked_on={ago(90)} | age_days=90")
        self.assertEqual(lines[4], f"{A} | alpr_policy | checked_on={ago(40)} | age_days=40")
        code, out, _ = run("content", "agency-profile", "list", "--directory", self.root, "--agency", B, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), [{"agency_id": B, "field": "funding", "index": 1, "checked_on": ago(30),
                                            "age_days": 30}])
        self.assertTrue(out.endswith("]\n"))
        fixed = date(2024, 3, 1)  # an injected day: a leap day stored the day before is one day old
        document = agencies()[1:2]
        document[0]["profile"]["funding"][0]["checked_on"] = "2024-02-29"
        self.write(document)
        self.assertEqual([r["age_days"] for r in content_profile.list_profile_checks(self.root, today=fixed)], [1])

    def test_list_filter_missing_empty_and_broken_no_write(self):
        before = self.snapshot()
        for agency in ("ca-gamma-pd", "ca-nobody-pd"):  # deselected and unknown ids refuse instead of looking empty
            self.assert_refused(*run("content", "agency-profile", "list", "--directory", self.root, "--agency", agency),
                                "not a selected agency")
        self.assertEqual(self.snapshot(), before)
        self.write([{"agency_id": B, "name": "Beta Sheriff (fictional)"}])
        self.assertEqual(run("content", "agency-profile", "list", "--directory", self.root)[1], "No recorded profile checks.\n")
        self.assertEqual(run("content", "agency-profile", "list", "--directory", self.root, "--json")[1], "[]\n")
        self.target.unlink()
        self.assertEqual(run("content", "agency-profile", "list", "--directory", self.root), (0, "No recorded profile checks.\n", ""))
        (self.root / "content").rmdir()
        self.assertEqual(content_profile.list_profile_checks(self.root), [])
        self.assertFalse((self.root / "content").exists())  # nothing is created
        (self.root / "content").mkdir()
        broken = {"not json": "{", "not a list": "{}", "future date": None, "bad date": None}
        for name, text in broken.items():
            with self.subTest(name=name):
                if text is None:
                    document = agencies()
                    document[1]["profile"]["funding"][0]["checked_on"] = (ago(-1) if name == "future date" else "2026-02-30")
                    text = json.dumps(document)
                self.target.write_text(text, encoding="utf-8")
                before = self.snapshot()
                self.assert_refused(*run("content", "agency-profile", "list", "--directory", self.root), "Stopped:")
                self.assertEqual(self.snapshot(), before)
        self.target.unlink()
        other = self.base / "other"
        (other / "content").mkdir(parents=True)
        (other / "content" / "agencies.json").write_text(json.dumps(agencies()), encoding="utf-8")
        self.target.symlink_to(other / "content" / "agencies.json")
        self.assert_refused(*run("content", "agency-profile", "list", "--directory", self.root), "agencies.json")
        self.target.unlink()
        (self.root / "content").rmdir()
        (self.root / "content").symlink_to(other / "content")
        self.assert_refused(*run("content", "agency-profile", "list", "--directory", self.root), "not a link")

    # --- recheck ------------------------------------------------------------------------------------------------
    def test_recheck_only_checked_on_and_real_loader_after_write(self):
        cases = [("policy_check", None), ("alpr_policy", None), ("contract_check", None), ("contract", None),
                 ("pre_implementation_meeting", None), ("sharing", None), ("funding", 2), ("transparency", 1),
                 ("reports", 1)]
        for field, index in cases:  # dependent readings first, so every document date stays legal
            with self.subTest(field=field):
                before = self.stored()
                old = before[0]["profile"][field] if index is None else before[0]["profile"][field][index - 1]
                args = ["--agency", A, "--field", field, "--checked-on", DAY] + ([] if index is None else ["--index", index])
                code, out, err = self.recheck(*args)
                self.assertEqual(code, 0, err)
                where = field if index is None else f"{field}[{index}]"
                if old["checked_on"] == DAY:
                    self.assertEqual(out, f"Already checked on {DAY}; nothing changed.\n{content_profile.NOTICE}\n")
                    continue
                self.assertEqual(out, f"Rechecked {A} {where}: {old['checked_on']} -> {DAY}.\n{content_profile.NOTICE}\n")
                expected = copy.deepcopy(before)
                (expected[0]["profile"][field] if index is None else expected[0]["profile"][field][index - 1])["checked_on"] = DAY
                self.assertEqual(self.stored(), expected)  # exactly one value changed; order and every other field kept
                load_agencies(self.stored(), "content/agencies.json")
        self.assertEqual(sorted(p.name for p in (self.root / "content").iterdir()), ["agencies.json"])
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
        before = self.snapshot()  # an equal date is a no-op: same bytes, same mtime
        self.assertEqual(self.recheck("--agency", B, "--field", "funding", "--index", "1", "--checked-on", ago(30))[0], 0)
        self.assertEqual(self.snapshot(), before)
        self.assertIn("Ünïcode fund", self.target.read_text(encoding="utf-8"))

    def test_recheck_dates_indexes_and_mode_refusals(self):
        before = self.snapshot()
        refusals = [
            (["--agency", A, "--field", "alpr_policy", "--checked-on", ago(-1)], "must not be after today"),
            (["--agency", A, "--field", "alpr_policy", "--checked-on", ago(41)], f"before stored checked_on {ago(40)}"),
            (["--agency", A, "--field", "alpr_policy", "--checked-on", "2026-2-03"], "YYYY-MM-DD"),
            (["--agency", A, "--field", "alpr_policy", "--checked-on", "2025-02-30"], "--checked-on"),
            (["--agency", A, "--field", "funding", "--checked-on", DAY], "--index: required for funding, 1 to 2"),
            (["--agency", A, "--field", "funding", "--index", "0", "--checked-on", DAY], "entries 1 to 2"),
            (["--agency", A, "--field", "funding", "--index", "-1", "--checked-on", DAY], "entries 1 to 2"),
            (["--agency", A, "--field", "funding", "--index", "3", "--checked-on", DAY], "entries 1 to 2"),
            (["--agency", A, "--field", "funding", "--index", "01", "--checked-on", DAY], "entries 1 to 2"),
            (["--agency", A, "--field", "sharing", "--index", "1", "--checked-on", DAY], "single record"),
            (["--agency", B, "--field", "reports", "--index", "1", "--checked-on", DAY], "no recorded reports"),
            (["--agency", B, "--field", "contract", "--checked-on", DAY], "no recorded contract"),
            (["--agency", "ca-nobody-pd", "--field", "contract", "--checked-on", DAY], "not found"),
            (["--agency", "ca-gamma-pd", "--field", "alpr_policy", "--checked-on", DAY], "is not selected")]
        for args, needle in refusals:
            with self.subTest(args=args):
                self.assert_refused(*self.recheck(*args), needle)
                self.assertEqual(self.snapshot(), before)
        for args in (["--agency", A, "--field", "vendor", "--checked-on", DAY], ["--agency", A, "--field", "contract"],
                     ["--agency", A, "--checked-on", DAY], ["--agency", A, "--field", "contract", "--checked-on", DAY, "--json"]):
            with self.subTest(args=args):
                self.assertEqual(self.recheck(*args)[0], 2)  # argparse: unknown field, missing or foreign flags
        self.target.unlink()
        self.assert_refused(*self.recheck("--agency", A, "--field", "contract", "--checked-on", DAY), "not found")
        self.assertFalse(self.target.exists())

    def test_policy_and_contract_recheck_dependent_first(self):
        before = self.snapshot()
        code, out, err = self.recheck("--agency", A, "--field", "alpr_policy", "--checked-on", DAY)
        self.assert_refused(code, out, err, "profile.policy_check.checked_on: must not be before profile.alpr_policy.checked_on")
        self.assertIn(f"Recheck policy_check first with --checked-on {DAY} (or later), then retry alpr_policy.", err)
        code, out, err = self.recheck("--agency", A, "--field", "contract", "--checked-on", DAY)
        self.assertIn(f"Recheck contract_check first with --checked-on {DAY} (or later), then retry contract.", err)
        self.assertEqual(self.snapshot(), before)
        for field in ("policy_check", "alpr_policy", "contract_check", "contract"):
            self.assertEqual(self.recheck("--agency", A, "--field", field, "--checked-on", DAY)[0], 0, field)
            load_agencies(self.stored(), "content/agencies.json")
        profile = self.stored()[0]["profile"]
        self.assertEqual(profile["policy_check"]["elements"], agencies()[0]["profile"]["policy_check"]["elements"])
        self.assertEqual(profile["alpr_policy"]["url"], "https://example.invalid/alpha/policy.pdf")

    def test_transparency_keeps_strict_order_and_distinct_dates(self):
        before = self.snapshot()
        for day in (ago(3), ago(2)):  # equal to or newer than the snapshot above it
            self.assert_refused(*self.recheck("--agency", A, "--field", "transparency", "--index", "2", "--checked-on", day),
                                f"transparency[2] must stay older than transparency[1] ({ago(3)})")
            self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.recheck("--agency", A, "--field", "transparency", "--index", "3", "--checked-on", ago(31))[0], 0)
        self.assertEqual([s["checked_on"] for s in self.stored()[0]["profile"]["transparency"]], [ago(3), ago(30), ago(31)])

    def test_whole_file_error_and_encoded_size_refuse_before_temp(self):
        document = agencies()
        document[1]["profile"]["funding"][0]["kind"] = "lottery"  # an untouched agency the build would refuse
        self.write(document)
        before = self.snapshot()
        self.assert_refused(*self.recheck("--agency", A, "--field", "sharing", "--checked-on", DAY), "profile.funding[0].kind")
        self.assertEqual(self.snapshot(), before)
        self.write(agencies(), compact=True)
        prospective = copy.deepcopy(agencies())
        prospective[0]["profile"]["sharing"]["checked_on"] = DAY
        size = len((json.dumps(prospective, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
        self.assertLess(self.target.stat().st_size, size)  # the canonical form is larger than what is stored
        before = self.snapshot()
        with mock.patch.object(content_profile, "MAX_JSON_BYTES", size - 1), \
                mock.patch.object(content_profile.os, "open", wraps=os.open) as opened:
            self.assert_refused(*self.recheck("--agency", A, "--field", "sharing", "--checked-on", DAY), "would exceed")
        self.assertFalse(any(call.args[1] & os.O_CREAT for call in opened.call_args_list if len(call.args) > 1))
        self.assertEqual(self.snapshot(), before)
        with mock.patch.object(content_profile, "MAX_JSON_BYTES", size):
            self.assertEqual(self.recheck("--agency", A, "--field", "sharing", "--checked-on", DAY)[0], 0)
        self.assertEqual(self.target.stat().st_size, size)

    def test_guarded_install_directory_target_swaps_and_concurrent_edit(self):
        other = self.base / "other"
        (other / "content").mkdir(parents=True)
        (other / "content" / "agencies.json").write_text(json.dumps(agencies()), encoding="utf-8")
        foreign = (other / "content" / "agencies.json").read_bytes()
        concurrent = json.dumps(agencies()[:2]).encode("utf-8")

        def edit_in_place():  # an organizer saves the file while the command runs
            with open(self.target, "r+b") as handle:
                handle.write(concurrent)
                handle.truncate()

        def swap_directory():
            os.rename(self.root / "content", self.base / "moved")
            os.symlink(other / "content", self.root / "content")

        for name, swap in (("edit", edit_in_place), ("directory", swap_directory)):
            with self.subTest(swap=name), mock.patch.object(content_profile, "_before_install", side_effect=swap):
                self.assert_refused(*self.recheck("--agency", A, "--field", "sharing", "--checked-on", DAY),
                                    "nothing was written")
            self.assertEqual((other / "content" / "agencies.json").read_bytes(), foreign)
        self.assertEqual((self.base / "moved" / "agencies.json").read_bytes(), concurrent)  # the edit is never overwritten
        self.assertEqual(sorted(p.name for p in (self.base / "moved").iterdir()), ["agencies.json"])  # temp cleaned up
        self.assertEqual(sorted(p.name for p in (other / "content").iterdir()), ["agencies.json"])
        (self.root / "content").unlink()
        os.rename(self.base / "moved", self.root / "content")
        self.write(agencies())
        before = self.snapshot()
        with mock.patch.object(content_profile.os, "replace", side_effect=OSError(5, "Input/output error")):
            self.assertEqual(self.recheck("--agency", A, "--field", "sharing", "--checked-on", DAY)[0], 1)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
