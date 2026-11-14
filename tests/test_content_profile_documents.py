"""DF1109B-17: ``content agency-profile policy|contract``. Synthetic agencies in temporary storage; nothing is fetched.

Run from the repository root: python3 -B -m unittest tests.test_content_profile_documents -v
"""
import contextlib
import copy
import io
import json
import os
import socket
import sys
import tempfile
import unittest
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, content, content_profile  # noqa: E402
from campaign_tool.content import load_agencies  # noqa: E402

TODAY = date.today()


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
URL_A, URL_B = "https://example.invalid/alpha/policy-a.pdf", "https://example.invalid/alpha/policy-b.pdf"
DEAL_A, DEAL_B = "https://example.invalid/alpha/agreement-a.pdf", "https://example.invalid/alpha/agreement-b.pdf"
POLICY_IDS = ("authorized_purposes", "authorized_users_training", "security_monitoring", "sharing_restrictions",
              "custodian_title", "accuracy_correction", "retention_destruction")
CONTRACT_IDS = ("term_renewal", "price_changes", "data_ownership", "retention", "sharing_network", "vendor_data_use",
                "audit_search_logs", "termination_data_exit")
NOTICE = "Recorded only. Nothing was fetched, built, published or sent.\n"
WAYBACK = "https://web.archive.org/web/20260101120000/https://example.invalid/alpha/policy-a.pdf"
NO_NETWORK = AssertionError("no network in these tests")


def ago(days):
    return (TODAY - timedelta(days=days)).isoformat()


def agencies():
    """Alpha has a policy and a contract, each with a reading of the same URL; beta has no documents yet."""
    alpha = {"agency_id": A, "slug": "alpha", "name": "Alpha Police Department (fictional)", "kind": "police",
             "profile": {
                 "status": "Records requested",
                 "alpr_policy": {"url": URL_A, "source_url": "https://example.invalid/alpha", "checked_on": ago(40)},
                 "policy_check": {"checked_on": ago(30), "policy_url": URL_A,
                                  "elements": {key: {"status": "unclear"} for key in POLICY_IDS}},
                 "contract": {"vendor": "Synthetic vendor", "start": "2025-01-01", "end": "2027-12-31",
                              "renewal_note": "Renews yearly.", "source_url": DEAL_A, "checked_on": ago(50),
                              "cost": {"amount_usd": "100.00", "period": "annual", "as_stated_in": "Agreement A"}},
                 "contract_check": {"checked_on": ago(45), "contract_url": DEAL_A,
                                    "elements": {key: {"status": "not_found"} for key in CONTRACT_IDS}}}}
    beta = {"agency_id": B, "name": "Beta Sheriff (fictional)", "kind": "sheriff", "profile": {"status": "Ünïcode"}}
    gamma = {"agency_id": "ca-gamma-pd", "name": "Gamma (deselected)", "selected": False}
    return [alpha, beta, gamma]


def run(*arguments):
    out, err, code = io.StringIO(), io.StringIO(), 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return code, out.getvalue(), err.getvalue()


class DocumentWriterTests(unittest.TestCase):
    def setUp(self):
        for patch in (mock.patch.object(socket, "socket", side_effect=NO_NETWORK),
                      mock.patch.object(socket, "create_connection", side_effect=NO_NETWORK),
                      mock.patch.object(urllib.request, "urlopen", side_effect=NO_NETWORK)):
            patch.start()
            self.addCleanup(patch.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-profile-documents-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "campaign"
        (self.root / "content").mkdir(parents=True)
        self.target = self.root / "content" / "agencies.json"
        self.write(agencies())

    def write(self, document):
        self.target.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def stored(self):
        return json.loads(self.target.read_text(encoding="utf-8"))

    def profile(self, agency=A):
        return next(a for a in self.stored() if a["agency_id"] == agency)["profile"]

    def tree(self):
        return {p.name: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in (self.root / "content").iterdir()}

    def loads(self):
        load_agencies(self.stored(), "content/agencies.json", TODAY)  # raises if the build would refuse the file

    def policy(self, *extra, agency=A, url=URL_B, checked_on=None):
        return run("content", "agency-profile", "policy", "--directory", self.root, "--agency", agency, "--url", url,
                   "--source-url", "https://example.invalid/alpha/policies", "--checked-on", checked_on or ago(10), *extra)

    def contract(self, *extra, agency=A, url=DEAL_B, checked_on=None, start="2026-01-01", end="2028-12-31"):
        return run("content", "agency-profile", "contract", "--directory", self.root, "--agency", agency, "--vendor",
                   "Synthetic vendor", "--start", start, "--end", end, "--source-url", url,
                   "--checked-on", checked_on or ago(10), *extra)

    def assert_refused(self, result, needle, status=1):
        code, out, err = result
        self.assertEqual(code, status, err)
        self.assertEqual(out, "")
        self.assertIn(needle, err)

    def assert_unchanged(self, before, result, needle, status=1):
        self.assert_refused(result, needle, status)
        self.assertEqual(self.tree(), before)

    # --- create ----------------------------------------------------------------------------------------------------
    def test_create_documents_preserves_unrelated_raw_data(self):
        before = self.stored()
        code, out, err = self.policy(agency=B)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, f"Recorded profile.alpr_policy for {B} (checked_on {ago(10)}).\n{NOTICE}")
        code, out, err = self.contract("--renewal-note", "Renews yearly.", "--cost-usd", "12345.60", "--period", "total",
                                       "--as-stated-in", "Example services agreement", agency=B)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, f"Recorded profile.contract for {B} (checked_on {ago(10)}).\n{NOTICE}")
        after = self.stored()
        self.assertEqual(after[0], before[0])
        self.assertEqual(after[2], before[2])
        self.assertEqual(after[1]["profile"], {
            "status": "Ünïcode",
            "alpr_policy": {"url": URL_B, "source_url": "https://example.invalid/alpha/policies", "checked_on": ago(10)},
            "contract": {"vendor": "Synthetic vendor", "start": "2026-01-01", "end": "2028-12-31",
                         "renewal_note": "Renews yearly.", "source_url": DEAL_B, "checked_on": ago(10),
                         "cost": {"amount_usd": "12345.60", "period": "total",
                                  "as_stated_in": "Example services agreement"}}})
        self.assertIn('"amount_usd": "12345.60"', self.target.read_text(encoding="utf-8"))  # the typed string, no float
        self.assertIn("Ünïcode", self.target.read_text(encoding="utf-8"))
        self.loads()
        self.assertEqual(sorted(p.name for p in (self.root / "content").iterdir()), ["agencies.json"])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["content"])  # no build, ledger or other file

    def test_replace_requires_explicit_flag_and_cost_group_is_all_or_none(self):
        before = self.tree()
        stored = self.profile()["alpr_policy"]
        same = run("content", "agency-profile", "policy", "--directory", self.root, "--agency", A, "--url", URL_A,
                   "--source-url", stored["source_url"], "--checked-on", stored["checked_on"])
        self.assert_unchanged(before, same, f"profile.alpr_policy: {A} already has one; pass --replace")
        self.assert_unchanged(before, self.contract(url=DEAL_A), f"profile.contract: {A} already has one; pass --replace")
        self.assert_unchanged(before, self.policy("--drop-check", url=URL_A), "--drop-check requires --replace")
        for partial in (("--cost-usd", "1"), ("--cost-usd", "1", "--period", "annual"), ("--as-stated-in", "Title")):
            self.assert_unchanged(before, self.contract("--replace", *partial, url=DEAL_A, checked_on=ago(50)),
                                  "--cost-usd, --period and --as-stated-in go together")
        # a full replacement on the same URL drops the unsupplied optional cost and renewal note; the reading stays
        code, out, err = self.contract("--replace", url=DEAL_A, checked_on=ago(50))
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("Dropped", out)
        self.assertEqual(self.profile()["contract"], {"vendor": "Synthetic vendor", "start": "2026-01-01",
                                                      "end": "2028-12-31", "source_url": DEAL_A, "checked_on": ago(50)})
        self.assertEqual(self.profile()["contract_check"], agencies()[0]["profile"]["contract_check"])
        self.loads()

    # --- the acceptance transitions: A -> B with the old reading removed, never rebound ---------------------------
    def test_policy_url_swap_requires_drop_check(self):
        before = self.tree()
        self.assert_unchanged(before, self.policy("--replace"),
                              "profile.alpr_policy.url changes while policy_check exists; pass --replace --drop-check, "
                              "then record a new reading with policy-check")
        code, out, err = self.policy("--replace", "--drop-check")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, f"Recorded profile.alpr_policy for {A} (checked_on {ago(10)}).\nDropped profile.policy_check "
                              f"(checked_on {ago(30)}); record a new reading with content agency-profile policy-check.\n"
                              f"{NOTICE}")
        profile = self.profile()
        self.assertEqual(profile["alpr_policy"]["url"], URL_B)
        self.assertNotIn("policy_check", profile)
        self.assertNotIn(URL_A, self.target.read_text(encoding="utf-8"))  # the old reading was not rebound
        self.assertEqual(profile["contract_check"], agencies()[0]["profile"]["contract_check"])
        self.loads()

    def test_contract_url_swap_requires_drop_check(self):
        before = self.tree()
        self.assert_unchanged(before, self.contract("--replace"),
                              "profile.contract.source_url changes while contract_check exists; pass --replace "
                              "--drop-check, then record a new reading with contract-check")
        code, out, err = self.contract("--replace", "--drop-check")
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"Dropped profile.contract_check (checked_on {ago(45)}); record a new reading with content "
                      "agency-profile contract-check.\n", out)
        profile = self.profile()
        self.assertEqual(profile["contract"]["source_url"], DEAL_B)
        self.assertNotIn("contract_check", profile)
        self.assertNotIn(DEAL_A, self.target.read_text(encoding="utf-8"))
        self.assertIn("policy_check", profile)
        self.loads()

    def test_same_url_reading_date_guard_and_discovery_page_change(self):
        before = self.tree()
        self.assert_unchanged(before, self.policy("--replace", url=URL_A, checked_on=ago(29)),
                              "profile.policy_check.checked_on would be before profile.alpr_policy.checked_on; pass "
                              f"--drop-check or redo policy-check first with --checked-on not before {ago(29)}")
        self.assert_unchanged(before, self.contract("--replace", url=DEAL_A, checked_on=ago(44)),
                              "profile.contract_check.checked_on would be before profile.contract.checked_on; pass "
                              f"--drop-check or redo contract-check first with --checked-on not before {ago(44)}")
        reading = copy.deepcopy(self.profile()["policy_check"])
        code, out, err = self.policy("--replace", url=URL_A, checked_on=ago(30))  # equal date, new discovery page
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("Dropped", out)
        self.assertEqual(self.profile()["policy_check"], reading)
        self.assertEqual(self.profile()["alpr_policy"]["source_url"], "https://example.invalid/alpha/policies")
        self.loads()
        code, out, _ = self.policy("--replace", "--drop-check", url=URL_A, checked_on=ago(29))  # an explicit drop
        self.assertEqual(code, 0)
        self.assertIn(f"Dropped profile.policy_check (checked_on {ago(30)})", out)
        self.assertNotIn("policy_check", self.profile())
        self.loads()
        code, out, _ = self.policy("--replace", "--drop-check", url=URL_A)  # no reading left: a no-op, no claim
        self.assertEqual(code, 0)
        self.assertNotIn("Dropped", out)
        self.loads()

    def test_archive_pair_stays_only_with_same_document(self):
        document = agencies()
        document[0]["profile"]["alpr_policy"].update(archived_url=WAYBACK, archived_on="2026-01-01")
        self.write(document)
        code, out, _ = self.policy("--replace", url=URL_A, checked_on=ago(30))
        self.assertEqual(code, 0)
        self.assertNotIn("archived", out)
        self.assertEqual(self.profile()["alpr_policy"]["archived_url"], WAYBACK)
        code, out, _ = self.policy("--replace", "--drop-check")
        self.assertEqual(code, 0)
        self.assertIn("Removed profile.alpr_policy archived_url and archived_on", out)
        self.assertFalse({"archived_url", "archived_on"} & set(self.profile()["alpr_policy"]))
        self.loads()
        document[0]["profile"]["alpr_policy"].pop("archived_on")  # a half pair cannot be carried forward
        self.write(document)
        before = self.tree()
        self.assert_unchanged(before, self.policy("--replace", url=URL_A, checked_on=ago(30)), "archived_on go together")

    # --- refusals before any output file ---------------------------------------------------------------------------
    def test_loader_and_privacy_refuse_before_output(self):
        before = self.tree()
        cases = [(self.policy("--replace", "--drop-check", checked_on=(TODAY + timedelta(days=1)).isoformat()),
                  "--checked-on: must not be after today"),
                 (self.policy("--replace", "--drop-check", checked_on="2026-1-02"), "--checked-on"),
                 (self.policy("--replace", "--drop-check", url="http://example.invalid/p.pdf"), "alpr_policy.url"),
                 (self.contract("--vendor", "<b>Vendor</b>", agency=B), "contract.vendor"),
                 (self.contract("--vendor", "Call 555-123-4567", agency=B), "profile.contract.vendor: phone"),
                 (self.contract("--renewal-note", "jane@example.org", agency=B), "profile.contract.renewal_note: email"),
                 (self.contract("--cost-usd", "1", "--period", "total", "--as-stated-in", "Plate 7ABC123", agency=B),
                  "profile.contract.cost.as_stated_in: plate")]
        for result, needle in cases:
            with self.subTest(needle=needle):
                self.assert_unchanged(before, result, needle)
        for _, _, err in (case[0] for case in cases[4:]):
            for secret in ("555-123-4567", "jane@example.org", "7ABC123"):
                self.assertNotIn(secret, err)  # the place and the category, never the text
        document = agencies()  # an unrelated agency the build would refuse, then one the privacy gate refuses
        document[1]["kind"] = 7
        self.write(document)
        before = self.tree()
        self.assert_unchanged(before, self.policy("--replace", "--drop-check"), "nothing was written")
        document = agencies()
        document[1]["name"] = "Beta Sheriff 555-123-4567"
        self.write(document)
        before = self.tree()
        self.assert_unchanged(before, self.policy("--replace", "--drop-check"), "privacy lint: agencies[1].name: phone")

    def test_cost_and_term_boundaries(self):
        accepted = ("0", "100000000", "12.5", "0.01")
        for amount in accepted:
            with self.subTest(amount=amount):
                code, _, err = self.contract("--replace", "--drop-check", "--cost-usd", amount, "--period", "one_time",
                                             "--as-stated-in", "Agreement", agency=A)
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(self.profile()["contract"]["cost"]["amount_usd"], amount)
        before = self.tree()
        for amount in ("100000000.01", "1.234", "-1", "+1", "1e3", "1,000", "NaN"):
            with self.subTest(amount=amount):
                self.assert_unchanged(before, self.contract("--replace", "--cost-usd", amount, "--period", "annual",
                                                            "--as-stated-in", "Agreement"), "cost.amount_usd")
        self.assert_unchanged(before, self.contract("--replace", "--cost-usd", "1", "--period", "monthly",
                                                    "--as-stated-in", "Agreement"), "invalid choice", status=2)
        self.assert_unchanged(before, self.contract("--replace", start="2026-02-01", end="2026-01-31"),
                              "contract.end: must not be before start")
        self.assertEqual(self.contract("--replace", start="2026-02-01", end="2026-02-01")[0], 0)
        self.loads()

    def test_missing_empty_broken_and_unselected_store(self):
        self.assert_refused(self.policy(agency="ca-gamma-pd"), "--agency ca-gamma-pd: is not selected")
        self.assert_refused(self.policy(agency="ca-nobody"), "--agency ca-nobody: not found")
        document = agencies()
        document[1]["profile"] = "broken"
        self.write(document)
        self.assert_refused(self.policy(agency=B), "profile: must be an object")
        for data in (b"", b"\xff\xfe", b"{not json", b"{}\n"):
            with self.subTest(data=data):
                self.target.write_bytes(data)
                before = self.tree()
                self.assert_unchanged(before, self.policy(agency=B), "agencies.json")
        self.target.unlink()
        self.assert_refused(self.policy(agency=B), "agencies.json")
        self.assertEqual(list((self.root / "content").iterdir()), [])  # never initialized

    def test_profile_write_boundary_and_failure_rollback(self):
        before = self.tree()
        for name in ("replace", "fsync"):
            with self.subTest(fail=name), mock.patch.object(content_profile.os, name, side_effect=OSError("injected")):
                code, out, err = self.policy("--replace", "--drop-check")
                self.assertNotEqual(code, 0)
                self.assertEqual(out, "")
                self.assertNotIn("Dropped", err)
                self.assertEqual(self.tree(), before)  # no partial removal and no temporary file left

        def swap():
            self.target.write_text(self.target.read_text(encoding="utf-8"), encoding="utf-8")
        with mock.patch.object(content_profile, "_before_install", side_effect=swap):
            self.assert_refused(self.policy("--replace", "--drop-check"), "changed while the write was running")
        self.assertIn("policy_check", self.profile())
        self.assertEqual(sorted(p.name for p in (self.root / "content").iterdir()), ["agencies.json"])
        other = self.base / "other"
        (other / "content").mkdir(parents=True)
        (other / "content" / "agencies.json").write_text(json.dumps(agencies()), encoding="utf-8")
        linked = self.base / "linked"
        (linked).mkdir()
        os.symlink(other / "content", linked / "content")
        result = run("content", "agency-profile", "policy", "--directory", linked, "--agency", B, "--url", URL_B,
                     "--source-url", URL_B, "--checked-on", ago(1))
        self.assert_refused(result, "content/")
        self.assertEqual(json.loads((other / "content" / "agencies.json").read_text(encoding="utf-8")), agencies())


if __name__ == "__main__":
    unittest.main()
