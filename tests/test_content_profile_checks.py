"""DF1109B-18: ``content agency-profile policy-check|contract-check``. Synthetic agencies in temporary storage; nothing is
fetched, built or sent. The expected checklist ids are written out independently of the parser.

Run from the repository root: python3 -B -m unittest tests.test_content_profile_checks -v
"""
import contextlib
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
from campaign_tool import cli, content_profile  # noqa: E402
from campaign_tool.content import load_agencies  # noqa: E402

TODAY = date.today()
A, B = "ca-alpha-pd", "ca-beta-so"
URL_A, URL_B = "https://example.invalid/alpha/policy-a.pdf", "https://example.invalid/alpha/policy-b.pdf"
DEAL_A, DEAL_B = "https://example.invalid/alpha/agreement-a.pdf", "https://example.invalid/alpha/agreement-b.pdf"
POLICY_IDS = ("authorized_purposes", "authorized_users_training", "security_monitoring", "sharing_restrictions",
              "custodian_title", "accuracy_correction", "retention_destruction")
CONTRACT_IDS = ("term_renewal", "price_changes", "data_ownership", "retention", "sharing_network", "vendor_data_use",
                "audit_search_logs", "termination_data_exit")
NOTICE = "This is the organizer's reading, not a legal review.\nRecorded only. Nothing was fetched, built, published or sent.\n"
NO_NETWORK = AssertionError("no network in these tests")


def ago(days):
    return (TODAY - timedelta(days=days)).isoformat()


def agencies():
    """Alpha has a policy and a contract (no readings yet); beta has no documents; gamma is deselected."""
    alpha = {"agency_id": A, "slug": "alpha", "name": "Alpha Police Department (fictional)", "kind": "police",
             "profile": {"status": "Records requested",
                         "alpr_policy": {"url": URL_A, "source_url": "https://example.invalid/alpha", "checked_on": ago(40)},
                         "contract": {"vendor": "Synthetic vendor", "start": "2025-01-01", "end": "2027-12-31",
                                      "source_url": DEAL_A, "checked_on": ago(50)}}}
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


def elements(ids, status="addressed"):
    return [f"--element={key}={status}" for key in ids]


class ReadingTests(unittest.TestCase):
    def setUp(self):
        for patch in (mock.patch.object(socket, "socket", side_effect=NO_NETWORK),
                      mock.patch.object(socket, "create_connection", side_effect=NO_NETWORK),
                      mock.patch.object(urllib.request, "urlopen", side_effect=NO_NETWORK)):
            patch.start()
            self.addCleanup(patch.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-profile-checks-")
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
        code, out, err = run("content-check", "--directory", self.root)  # the bare synthetic campaign has no site.json
        self.assertNotIn("agencies.json", out + err)

    def policy_check(self, *extra, ids=POLICY_IDS, agency=A, checked_on=None):
        return run("content", "agency-profile", "policy-check", "--directory", self.root, "--agency", agency,
                   "--checked-on", checked_on or ago(30), *(elements(ids) if ids else ()), *extra)

    def contract_check(self, *extra, ids=CONTRACT_IDS, agency=A, checked_on=None):
        return run("content", "agency-profile", "contract-check", "--directory", self.root, "--agency", agency,
                   "--checked-on", checked_on or ago(45), *(elements(ids) if ids else ()), *extra)

    def policy(self, *extra, url=URL_B, checked_on=None):
        return run("content", "agency-profile", "policy", "--directory", self.root, "--agency", A, "--url", url,
                   "--source-url", "https://example.invalid/alpha/policies", "--checked-on", checked_on or ago(10), *extra)

    def contract(self, *extra, url=DEAL_B, checked_on=None):
        return run("content", "agency-profile", "contract", "--directory", self.root, "--agency", A, "--vendor",
                   "Synthetic vendor", "--start", "2026-01-01", "--end", "2028-12-31", "--source-url", url,
                   "--checked-on", checked_on or ago(10), *extra)

    def assert_refused(self, result, needle, status=1):
        code, out, err = result
        self.assertEqual(code, status, err)
        self.assertEqual(out, "")
        self.assertIn(needle, err)

    def assert_unchanged(self, before, result, needle, status=1):
        self.assert_refused(result, needle, status)
        self.assertEqual(self.tree(), before)

    # --- recording --------------------------------------------------------------------------------------------------
    def test_policy_and_contract_all_ids_written_in_checklist_order(self):
        ids = list(reversed(POLICY_IDS))
        args = [f"--element={key}={'addressed:1' if n == 0 else 'not_found' if n == 1 else 'unclear'}" for n, key in enumerate(ids)]
        code, out, err = run("content", "agency-profile", "policy-check", "--directory", self.root, "--agency", A,
                             "--checked-on", ago(30), *args)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, f"Recorded profile.policy_check for {A} (checked_on {ago(30)}; 7 elements).\n{NOTICE}")
        reading = self.profile()["policy_check"]
        self.assertEqual(list(reading), ["checked_on", "policy_url", "elements"])
        self.assertEqual(reading["policy_url"], URL_A)
        self.assertEqual(list(reading["elements"]), list(POLICY_IDS))
        self.assertEqual(reading["elements"]["retention_destruction"], {"status": "addressed", "page": 1})
        self.assertEqual(reading["elements"]["accuracy_correction"], {"status": "not_found"})
        self.assertEqual(reading["elements"]["authorized_purposes"], {"status": "unclear"})
        self.loads()
        args = [f"--element={key}={status}" for key, status in zip(reversed(CONTRACT_IDS), ("addressed:4", "not_found:2", "unclear:9999") * 3)]
        self.assertEqual(self.contract_check("--note", "retention=Stated on page 2.", ids=())[0], 1)  # no empty reading
        code, out, err = run("content", "agency-profile", "contract-check", "--directory", self.root, "--agency", A,
                             "--checked-on", ago(45), *args, "--note", "term_renewal=A=B: kept whole.")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, f"Recorded profile.contract_check for {A} (checked_on {ago(45)}; 8 elements).\n{NOTICE}")
        reading = self.profile()["contract_check"]
        self.assertEqual((reading["contract_url"], list(reading["elements"])), (DEAL_A, list(CONTRACT_IDS)))
        self.assertEqual(reading["elements"]["termination_data_exit"], {"status": "addressed", "page": 4})
        self.assertEqual(reading["elements"]["term_renewal"], {"status": "not_found", "page": 2, "note": "A=B: kept whole."})
        self.assertEqual(reading["elements"]["data_ownership"], {"status": "unclear", "page": 9999})
        self.assertEqual(self.profile()["alpr_policy"], agencies()[0]["profile"]["alpr_policy"])
        self.assertEqual(self.stored()[1:], agencies()[1:])
        self.loads()

    def test_missing_unknown_duplicate_ids_all_named_before_write(self):
        before = self.tree()
        self.assert_unchanged(before, self.policy_check(ids=()), "Missing element ids: 'authorized_purposes', "
                              "'authorized_users_training', 'security_monitoring', 'sharing_restrictions', 'custodian_title', "
                              "'accuracy_correction', 'retention_destruction'; unknown element ids: (none); duplicate element ids: (none)")
        self.assert_unchanged(before, self.policy_check(ids=POLICY_IDS[1:]), "Missing element ids: 'authorized_purposes'; unknown")
        self.assert_unchanged(before, self.policy_check("--element", "authorized_purposes=unclear"),
                              "duplicate element ids: 'authorized_purposes'")  # equal values still count twice
        self.assert_unchanged(before, self.policy_check("--element", "authorized_purposes=not_found"),
                              "duplicate element ids: 'authorized_purposes'")
        self.assert_unchanged(before, self.policy_check("--element", "zebra=unclear", "--element", "Alpha=unclear",
                                                        ids=POLICY_IDS[2:]),
                              "Missing element ids: 'authorized_purposes', 'authorized_users_training'; unknown element ids: "
                              "'Alpha', 'zebra'; duplicate element ids: (none)")
        result = self.policy_check("--element", "zebra=unclear", "--element", "zebra=unclear", "--element", "security_monitoring=unclear",
                                   ids=POLICY_IDS[2:])
        self.assert_unchanged(before, result, "unknown element ids: 'zebra'; duplicate element ids: 'security_monitoring'")
        self.assert_unchanged(before, self.contract_check(ids=CONTRACT_IDS[:-1]), "Missing element ids: 'termination_data_exit'")
        self.assert_unchanged(before, self.contract_check("--element", "retention=addressed", "--element", "audit_log=unclear"),
                              "unknown element ids: 'audit_log'; duplicate element ids: 'retention'")

    def test_element_token_and_page_boundaries(self):
        before = self.tree()
        rest = POLICY_IDS[1:]
        for token in ("authorized_purposes", "authorized_purposes=", "authorized_purposes=addressed:", "authorized_purposes=addressed:1:2",
                      "=addressed", "authorized_purposes==addressed"):
            with self.subTest(token=token):
                self.assert_unchanged(before, self.policy_check("--element", token, ids=rest), "--element")
        for token in ("authorized_purposes=Addressed", "authorized_purposes= addressed", "authorized_purposes=addressed ",
                      "authorized_purposes=found", "Authorized_purposes=addressed", " authorized_purposes=addressed"):
            with self.subTest(token=token):
                self.assert_unchanged(before, self.policy_check("--element", token, ids=rest), "--element")
        for page in ("0", "10000", "01", "+1", "-1", "1.0", "1e2", "١", "１", " 1", "1 ", "x"):
            with self.subTest(page=page):
                self.assert_unchanged(before, self.policy_check("--element", f"authorized_purposes=addressed:{page}", ids=rest),
                                      "authorized_purposes: page must be a whole number from 1 to 9999")
        for page in ("1", "9999"):
            code, _, err = self.policy_check("--element", f"authorized_purposes=addressed:{page}", "--replace", ids=rest)
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(self.profile()["policy_check"]["elements"]["authorized_purposes"], {"status": "addressed", "page": int(page)})
        self.assertEqual(self.policy_check("--element", "authorized_purposes=addressed", "--replace", ids=rest)[0], 0)
        self.assertEqual(self.profile()["policy_check"]["elements"]["authorized_purposes"], {"status": "addressed"})
        for status in ("not_found", "unclear"):
            self.assert_unchanged(self.tree(), self.policy_check("--element", f"authorized_purposes={status}:3", "--replace", ids=rest),
                                  "authorized_purposes: page is only allowed when status is addressed")
        for status in ("addressed", "not_found", "unclear"):  # the contract keeps a page with any status
            code, _, err = self.contract_check("--element", f"term_renewal={status}:7", "--replace", ids=CONTRACT_IDS[1:])
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(self.profile()["contract_check"]["elements"]["term_renewal"], {"status": status, "page": 7})

    def test_contract_notes_split_once_and_are_not_echoed(self):
        self.assertEqual(self.contract_check("--note", "retention=a=b:c d")[0], 0)
        self.assertEqual(self.profile()["contract_check"]["elements"]["retention"]["note"], "a=b:c d")
        self.assertEqual(self.contract_check("--note", "retention=" + "x" * 200, "--replace")[0], 0)
        before = self.tree()
        sentinel = "SENTINEL-NOTE-TEXT"
        cases = [("--note", "retention"), ("--note", "nonesuch=" + sentinel), ("--note", "=" + sentinel),
                 ("--note", "retention=" + "x" * 201), ("--note", "retention="), ("--note", "retention=   "),
                 ("--note", f"retention=<b>{sentinel}</b>"), ("--note", f"retention=a\u2028{sentinel}"),
                 ("--note", f"retention=a\u200b{sentinel}"), ("--note", f"retention=a\n{sentinel}")]
        for case in cases:
            with self.subTest(case=case[1][:30]):
                result = self.contract_check(*case, "--replace")
                self.assert_unchanged(before, result, "--note")
                self.assertNotIn(sentinel, result[2])
        result = self.contract_check("--note", "retention=one", "--note", "retention=two", "--replace")
        self.assert_unchanged(before, result, "--note retention: given more than once")
        for text, category in (("write jane@example.org", "email"), ("call 555-123-4567", "phone"), ("plate 7ABC123", "plate")):
            result = self.contract_check("--note", f"retention={text}", "--replace")
            self.assert_unchanged(before, result, f"privacy lint: --note retention: {category}")
            self.assertNotIn(text.split()[-1], result[2])
        self.assert_unchanged(before, self.policy_check("--note", "authorized_purposes=text"), "unrecognized arguments", status=2)

    # --- the stored document -----------------------------------------------------------------------------------------
    def test_document_required_and_url_override_refused(self):
        before = self.tree()
        self.assert_unchanged(before, self.policy_check(agency=B), f"profile.policy_check: record profile.alpr_policy with "
                              "content agency-profile policy first")
        self.assert_unchanged(before, self.contract_check(agency=B), "record profile.contract with content agency-profile contract first")
        document = agencies()
        del document[0]["profile"]["contract"]
        self.write(document)
        before = self.tree()
        self.assert_unchanged(before, self.contract_check(), "record profile.contract with content agency-profile contract first")
        self.assertEqual(self.policy_check()[0], 0)  # the other kind's document never substitutes, nor blocks
        document[0]["profile"]["alpr_policy"] = None
        document[0]["profile"].pop("policy_check", None)
        self.write(document)
        before = self.tree()
        self.assert_unchanged(before, self.policy_check(), "record profile.alpr_policy with content agency-profile policy first")
        for flag in ("--policy-url", "--contract-url"):
            self.assert_unchanged(before, self.policy_check(flag, URL_B), "unrecognized arguments", status=2)
        self.assert_refused(self.policy_check(agency="ca-gamma-pd"), "--agency ca-gamma-pd: is not selected")
        self.assert_refused(self.policy_check(agency="ca-nobody"), "--agency ca-nobody: not found")
        document[0]["profile"]["alpr_policy"] = agencies()[0]["profile"]["alpr_policy"]
        document.append(dict(document[0]))
        self.write(document)
        self.assert_refused(self.policy_check(), f"--agency {A}: not found")  # a duplicated id is ambiguous

    def test_reading_dates_and_full_replace(self):
        before = self.tree()
        for bad in ((TODAY + timedelta(days=1)).isoformat(), "2026-1-02", "2026-02-30", "yesterday"):
            with self.subTest(date=bad):
                self.assertEqual(self.policy_check(checked_on=bad)[0], 1)
                self.assertEqual(self.tree(), before)
        self.assert_unchanged(before, self.policy_check(checked_on=ago(41)),
                              f"--checked-on: must not be before profile.alpr_policy.checked_on {ago(40)}")
        self.assertEqual(self.policy_check(checked_on=ago(40))[0], 0)  # equal to the document's own day
        before = self.tree()
        self.assert_unchanged(before, self.policy_check(checked_on=ago(40)), "profile.policy_check: ca-alpha-pd already has one; pass --replace")
        ids = [i for i in CONTRACT_IDS if i != "price_changes"]
        args = elements(ids) + ["--element", "price_changes=addressed:3", "--note", "retention=old note"]
        self.assertEqual(run("content", "agency-profile", "contract-check", "--directory", self.root, "--agency", A,
                             "--checked-on", ago(45), *args)[0], 0)
        document = self.stored()
        before = self.tree()
        self.assert_unchanged(before, self.contract_check(), "profile.contract_check: ca-alpha-pd already has one; pass --replace")
        code, out, err = self.contract_check("--replace", checked_on=ago(44))  # no page, no note this time
        self.assertEqual((code, err), (0, ""))
        stored = self.profile()["contract_check"]
        self.assertEqual(stored["checked_on"], ago(44))
        self.assertTrue(all(entry == {"status": "addressed"} for entry in stored["elements"].values()))
        self.assertEqual(self.profile()["contract"], document[0]["profile"]["contract"])
        self.assertEqual(self.stored()[1:], document[1:])

    # --- the DF1109B-17 acceptance sequence, command only --------------------------------------------------------------
    def test_policy_a_to_b_command_only(self):
        self.assertEqual(self.policy_check()[0], 0)
        self.loads()
        before = self.tree()
        self.assert_unchanged(before, self.policy("--replace"), "changes while policy_check exists; pass --replace --drop-check")
        code, out, err = self.policy("--replace", "--drop-check", checked_on=ago(20))
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"Dropped profile.policy_check (checked_on {ago(30)}); record a new reading with content agency-profile policy-check.", out)
        self.assertNotIn("policy_check", self.profile())
        self.loads()
        self.assert_refused(self.policy_check(checked_on=ago(21)), f"must not be before profile.alpr_policy.checked_on {ago(20)}")
        code, out, err = self.policy_check(checked_on=ago(20))  # no --replace after the drop
        self.assertEqual((code, err), (0, ""))
        reading = self.profile()["policy_check"]
        self.assertEqual((reading["policy_url"], reading["checked_on"]), (URL_B, ago(20)))  # never rebound to A
        self.assertNotIn(URL_A, self.target.read_text(encoding="utf-8"))
        self.loads()

    def test_contract_a_to_b_command_only(self):
        self.assertEqual(self.contract_check()[0], 0)
        self.loads()
        before = self.tree()
        self.assert_unchanged(before, self.contract("--replace"), "changes while contract_check exists; pass --replace --drop-check")
        code, out, err = self.contract("--replace", "--drop-check", checked_on=ago(20))
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"Dropped profile.contract_check (checked_on {ago(45)}); record a new reading with content agency-profile contract-check.", out)
        self.assertNotIn("contract_check", self.profile())
        self.loads()
        self.assert_refused(self.contract_check(checked_on=ago(21)), f"must not be before profile.contract.checked_on {ago(20)}")
        code, out, err = self.contract_check("--note", "retention=Not in this agreement.", checked_on=ago(19))
        self.assertEqual((code, err), (0, ""))
        reading = self.profile()["contract_check"]
        self.assertEqual((reading["contract_url"], reading["checked_on"]), (DEAL_B, ago(19)))
        self.assertNotIn(DEAL_A, self.target.read_text(encoding="utf-8"))
        self.loads()

    def test_same_url_date_recovery_both_routes(self):
        for check, document, doc_url in ((self.policy_check, self.policy, URL_A), (self.contract_check, self.contract, DEAL_A)):
            self.assertEqual(check()[0], 0)
            self.assert_refused(document("--replace", url=doc_url, checked_on=ago(20)), "pass --drop-check or redo")
            # route 1: replace the reading first at a date not before the intended new document date, then the document
            self.assertEqual(check("--replace", checked_on=ago(20))[0], 0)
            code, out, err = document("--replace", url=doc_url, checked_on=ago(20))
            self.assertEqual((code, err), (0, ""))
            self.assertNotIn("Dropped", out)
            self.loads()
            # route 2: the document with --drop-check, then a fresh reading without --replace
            code, out, err = document("--replace", "--drop-check", url=doc_url, checked_on=ago(10))
            self.assertEqual((code, err), (0, ""))
            self.assertIn("Dropped profile.", out)
            self.loads()
            self.assertEqual(check(checked_on=ago(10))[0], 0)
            self.loads()

    # --- failure paths -----------------------------------------------------------------------------------------------
    def test_broken_store_checklist_and_atomic_failures(self):
        self.assert_refused(self.policy_check(agency="ca-nobody"), "--agency ca-nobody: not found")
        for data in (b"", b"\xff\xfe", b"{not json", b"{}\n"):
            with self.subTest(data=data):
                self.target.write_bytes(data)
                before = self.tree()
                self.assert_unchanged(before, self.policy_check(), "agencies.json")
        document = agencies()
        document[1]["kind"] = 7  # an unrelated agency the build would refuse blocks the write
        self.write(document)
        before = self.tree()
        self.assert_unchanged(before, self.policy_check(), "nothing was written")
        document = agencies()
        document[1]["name"] = "Beta Sheriff 555-123-4567"
        self.write(document)
        before = self.tree()
        self.assert_unchanged(before, self.policy_check(), "privacy lint: agencies[1].name: phone")
        self.write(agencies())
        self.target.unlink()
        self.assert_refused(self.policy_check(), "agencies.json")
        self.assertEqual(list((self.root / "content").iterdir()), [])
        self.write(agencies())
        before = self.tree()
        with mock.patch.object(content_profile, "load_contract_checklist", side_effect=content_profile.ContentError("broken")):
            self.assert_unchanged(before, self.contract_check(), "the contract checklist could not be loaded")
        with mock.patch.object(content_profile.law, "load_checklist", side_effect=ValueError("broken")):
            self.assert_unchanged(before, self.policy_check(), "the policy checklist could not be loaded")
        for name in ("replace", "fsync"):
            with self.subTest(fail=name), mock.patch.object(content_profile.os, name, side_effect=OSError("injected")):
                code, out, err = self.policy_check()
                self.assertNotEqual(code, 0)
                self.assertEqual(out, "")
                self.assertEqual(self.tree(), before)  # no partial write and no temporary file left

        def swap():
            self.target.write_text(self.target.read_text(encoding="utf-8"), encoding="utf-8")
        with mock.patch.object(content_profile, "_before_install", side_effect=swap):
            self.assert_refused(self.policy_check(), "changed while the write was running")
        self.assertNotIn("policy_check", self.profile())
        self.assertEqual(sorted(p.name for p in (self.root / "content").iterdir()), ["agencies.json"])
        other = self.base / "other"
        (other / "content").mkdir(parents=True)
        (other / "content" / "agencies.json").write_text(json.dumps(agencies()), encoding="utf-8")
        linked = self.base / "linked"
        linked.mkdir()
        os.symlink(other / "content", linked / "content")
        result = run("content", "agency-profile", "policy-check", "--directory", linked, "--agency", A, "--checked-on", ago(30),
                     *elements(POLICY_IDS))
        self.assert_refused(result, "content/")
        self.assertEqual(json.loads((other / "content" / "agencies.json").read_text(encoding="utf-8")), agencies())

    def test_readings_do_not_change_list_or_publish(self):
        self.assertEqual(self.policy_check()[0], 0)
        code, out, _ = run("content", "agency-profile", "list", "--directory", self.root, "--agency", A)
        self.assertEqual(code, 0)
        self.assertEqual(out, f"{A} | contract | checked_on={ago(50)} | age_days=50\n{A} | alpr_policy | checked_on={ago(40)} | age_days=40\n"
                              f"{A} | policy_check | checked_on={ago(30)} | age_days=30\n")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["content"])  # no ledger, build or public tree
        self.assertEqual(sorted(p.name for p in (self.root / "content").iterdir()), ["agencies.json"])


if __name__ == "__main__":
    unittest.main()
