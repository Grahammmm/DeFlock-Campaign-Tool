"""Verified portal and post routes in request drafts: only the To header changes; nothing is sent."""
import contextlib
import copy
import hashlib
import io
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit, kit_check, kit_copy  # noqa: E402

FIXTURE = json.loads((REPO_ROOT / "tests" / "fixtures" / "kit-recipient-prechange-v1.json").read_text("utf-8"))
PACKAGE, AGENCIES, OPTIONS, EXPECTED = (FIXTURE[k] for k in ("package", "agencies", "options", "expected"))
PLACEHOLDER = "[official records custodian - verify on the agency's website]"
TEMPLATE_PLACEHOLDER = "To: [official records custodian]"
POST_TO = "To: by post to the verified mailing address (kit-copy --for mail)"
PORTAL_TO = "To: records portal https://records.example.invalid/portal (nextrequest)"
UNCHANGED = ("email_portal_mail", "verified_email", "unverified_email", "unverified_portal_mail", "records_url_only",
             "no_route", "verified_string_true", "verified_one", "verified_absent_portal")
TODAY = date(2026, 3, 4)
SETTINGS = {"schema_version": 1, "requester": ["Pat Example", "1 Test Way", "Exampleville, CA 90001"],
            "fee_cap_usd": 50, "date_from": "2025-01-01"}
ADDRESS = "Records Unit|123 Example Street|Exampleville, CA 90000"


def combined(agency):
    return kit.render_combined_request(PACKAGE, agency, **OPTIONS)


def template(agency):
    return kit.render_template_request(agency, **OPTIONS)


def swap(text, old, new):
    assert text.count(old) == 1, old
    return text.replace(old, new)


def from_subject(text):
    return text[text.index("Subject:"):]


def run(*arguments):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class RendererTests(unittest.TestCase):
    def test_prechange_bytes_for_unaffected_agencies(self):
        for key in UNCHANGED:
            with self.subTest(key):
                self.assertEqual(combined(AGENCIES[key]), EXPECTED[key]["combined"])
                self.assertEqual(template(AGENCIES[key]), EXPECTED[key]["template"])
        self.assertIn(PLACEHOLDER, EXPECTED["unverified_portal_mail"]["combined"])

    def test_verified_portal_header_in_both_renderers(self):
        for key, route in (("portal_new", PORTAL_TO), ("portal_and_mail_new", PORTAL_TO),
                           ("portal_unknown_new", "To: records portal https://records.example.invalid/p?a=1&b=é (unknown)")):
            with self.subTest(key):
                old = EXPECTED[key]
                self.assertEqual(combined(AGENCIES[key]), swap(old["combined"], "To: " + PLACEHOLDER, route))
                self.assertEqual(template(AGENCIES[key]), swap(old["template"], TEMPLATE_PLACEHOLDER, route))
                self.assertEqual(from_subject(combined(AGENCIES[key])), from_subject(old["combined"]))
                self.assertEqual(from_subject(template(AGENCIES[key])), from_subject(old["template"]))

    def test_malformed_portal_never_becomes_a_route(self):
        valid = AGENCIES["portal_new"]
        for label, portal in (("http", {"vendor": "govqa", "url": "http://records.example.invalid/p"}),
                              ("vendor", {"vendor": "madeup", "url": "https://records.example.invalid/p"}),
                              ("markup", {"vendor": "govqa", "url": "https://records.example.invalid/<b>"}),
                              ("bracket", {"vendor": "govqa", "url": "https://records.example.invalid/[x]"}),
                              ("control", {"vendor": "govqa", "url": "https://records.example.invalid/p\u0007"}),
                              ("newline", {"vendor": "govqa", "url": "https://records.example.invalid/p\nTo: x"}),
                              ("empty", {"vendor": "govqa", "url": ""}), ("string", "https://records.example.invalid/p"),
                              ("nourl", {"vendor": "govqa"})):
            with self.subTest(label):
                agency = {**valid, "portal": portal}
                self.assertEqual(combined(agency), EXPECTED["no_route"]["combined"])
                self.assertEqual(template(agency), EXPECTED["no_route"]["template"])
        # A malformed portal does not block a valid verified post route.
        agency = {**AGENCIES["mail_new"], "portal": {"vendor": "govqa", "url": "http://x.example.invalid/"}}
        self.assertEqual(combined(agency), combined(AGENCIES["mail_new"]))

    def test_verified_mail_requires_postal_proof(self):
        old = EXPECTED["mail_new"]
        self.assertEqual(combined(AGENCIES["mail_new"]), swap(old["combined"], "To: " + PLACEHOLDER, POST_TO))
        self.assertEqual(template(AGENCIES["mail_new"]), swap(old["template"], TEMPLATE_PLACEHOLDER, POST_TO))
        self.assertEqual(from_subject(template(AGENCIES["mail_new"])), from_subject(old["template"]))
        self.assertNotIn("Example Way", combined(AGENCIES["mail_new"]))
        good = AGENCIES["mail_new"]
        bad = {"no_proof": {k: v for k, v in good.items() if k != "mailing_verification"},
               "no_address": {k: v for k, v in good.items() if k != "mailing_address"},
               "unverified": {**good, "verified": False},
               "future": {**good, "mailing_verification": {**good["mailing_verification"], "checked_on": "2999-01-01"}},
               "impossible": {**good, "mailing_verification": {**good["mailing_verification"], "checked_on": "2026-02-31"}},
               "no_source": {**good, "mailing_verification": {"checked_on": "2026-01-15"}},
               "http_source": {**good, "mailing_verification": {"source_url": "http://a.example.invalid/",
                                                                 "checked_on": "2026-01-15"}},
               "one_line": {**good, "mailing_address": ["Only one"]},
               "markup": {**good, "mailing_address": ["Records <b>", "1 Way"]}}
        for label, agency in bad.items():
            with self.subTest(label):
                self.assertIn(PLACEHOLDER, combined(agency))
                self.assertIn(TEMPLATE_PLACEHOLDER, template(agency))
        records_url = {**AGENCIES["records_url_only"], "records_email": None}
        self.assertEqual(combined(records_url), EXPECTED["records_url_only"]["combined"])

    def test_template_changes_only_exact_header_before_subject(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-tpl-") as tmp:
            body_only = Path(tmp) / "body.md"
            body_only.write_text("# Draft\n\nTo: someone\nSubject: S [agency]\n\nTo: [official records custodian]\n",
                                 encoding="utf-8")
            no_header = Path(tmp) / "nohdr.md"
            no_header.write_text("# Draft\n\nSubject: S\n\nSee [official records custodian] and the rest.\n",
                                 encoding="utf-8")
            agency = AGENCIES["portal_new"]
            text = kit.render_template_request(agency, template_path=body_only, **OPTIONS)
            self.assertIn("To: someone\n", text)
            self.assertIn("\nTo: [official records custodian]\n", text)
            self.assertNotIn("records portal", text)
            plain = kit.render_template_request(agency, template_path=no_header, **OPTIONS)
            self.assertNotIn("records portal", plain)
            self.assertIn("See [official records custodian] and", plain)
        email = template(AGENCIES["email_portal_mail"])
        self.assertEqual(email, EXPECTED["email_portal_mail"]["template"])


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        for target in ("urllib.request.urlopen", "socket.socket", "smtplib.SMTP"):
            patcher = mock.patch(target, side_effect=AssertionError("network used"))
            patcher.start()
            self.addCleanup(patcher.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-routes-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        code, _, err = run("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                           "--state", "CA")
        self.assertEqual(code, 0, err)
        self.kit = self.root / "kit"
        self.kit.mkdir(exist_ok=True)
        (self.kit / "request-settings.json").write_text(json.dumps(SETTINGS), encoding="utf-8")
        self.build()
        self.ids = sorted(p.stem for p in (self.kit / "requests").glob("*.md"))
        self.a, self.b = self.ids[0], self.ids[1]

    def build(self, **arguments):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            kit.build_kit(self.root, **arguments)
        return out.getvalue()

    def report(self, agency_id):
        return next(r for r in kit_check.check(self.root)["agencies"] if r["agency_id"] == agency_id)

    def verify(self, agency_id=None, **fields):
        kit.verify_agency(self.root, agency_id or self.a, source_url="https://police.example/records",
                          checked_on="2026-01-02", today=TODAY, **fields)

    def draft(self, agency_id=None):
        return (self.kit / "requests" / f"{agency_id or self.a}.md").read_text(encoding="utf-8")

    def test_kit_check_ready_portal_and_mail_but_other_slots_still_fail(self):
        self.assertFalse(self.report(self.a)["ok"])
        self.verify(self.a, portal="https://records.example.invalid/portal")
        self.verify(self.b, mailing_address=ADDRESS)
        self.build()
        self.assertIn("To: records portal https://records.example.invalid/portal (unknown)", self.draft(self.a))
        self.assertIn(POST_TO, self.draft(self.b))
        for agency_id in (self.a, self.b):
            self.assertEqual(self.report(agency_id)["placeholders"], [], agency_id)
            self.assertTrue(self.report(agency_id)["ok"], agency_id)
        # records_url alone and unverified agencies are still reported.
        other = self.ids[2]
        self.assertFalse(self.report(other)["ok"])
        self.verify(other, records_url="https://records.example.invalid/requests")
        self.build()
        self.assertFalse(self.report(other)["ok"])
        self.assertIn(PLACEHOLDER, self.draft(other))
        # Each remaining slot individually still fails the check.
        for key in ("fee_cap_usd", "date_from", "requester"):
            settings = {k: v for k, v in SETTINGS.items() if k != key}
            (self.kit / "request-settings.json").write_text(json.dumps(settings), encoding="utf-8")
            self.build()
            self.assertFalse(self.report(self.a)["ok"], key)
        (self.kit / "request-settings.json").write_text(json.dumps(SETTINGS), encoding="utf-8")
        self.build()
        self.assertTrue(self.report(self.a)["ok"])

    def test_no_law_package_fallback_branch_passes_check(self):
        self.verify(self.a, portal="https://records.example.invalid/portal")
        with tempfile.TemporaryDirectory(prefix="synthetic-empty-") as empty:
            self.build(law_base=Path(empty))
        self.assertNotIn("Statutory context", self.draft())
        self.assertIn("To: records portal https://records.example.invalid/portal (unknown)", self.draft())
        words = [hit["placeholder"] for hit in self.report(self.a)["placeholders"]]
        self.assertFalse([w for w in words if "custodian" in w], words)

    def test_copy_subject_onward_all_targets_and_no_send(self):
        self.verify(self.a, portal="https://records.example.invalid/portal", mailing_address=ADDRESS)
        self.build()
        after = self.draft()
        route = "To: records portal https://records.example.invalid/portal (unknown)"
        before = swap(after, route, "To: synthetic value")
        self.assertEqual(from_subject(after), from_subject(before))
        self.assertEqual(kit_copy.sendable(after), kit_copy.sendable(before))
        portal = kit_copy.copy(self.root, self.a, "portal", today=TODAY)
        part = kit_copy.sendable(before)
        self.assertEqual((self.kit / "outgoing" / f"{self.a}.txt").read_bytes(),
                         (part["subject"] + "\n\n" + part["body"] + "\n").encode())
        self.assertEqual(portal["subject"], part["subject"])
        mailed = kit_copy.copy(self.root, self.a, "mail", today=TODAY)
        letter = (self.kit / "outgoing" / f"{self.a}.letter.txt").read_text(encoding="utf-8")
        self.assertEqual(mailed["subject"], part["subject"])
        self.assertIn(part["body"].splitlines()[0], letter)
        self.assertNotIn("records portal", letter)
        self.assertFalse((self.kit / "sent.json").exists())
        self.assertEqual(sorted(p.name for p in (self.kit / "outgoing").iterdir()),
                         sorted([f"{self.a}.txt", f"{self.a}.letter.html", f"{self.a}.letter.txt"]))

    def test_email_without_verified_email_still_refuses(self):
        self.verify(self.a, portal="https://records.example.invalid/portal")
        self.build()
        with self.assertRaises(ValueError):
            kit_copy.copy(self.root, self.a, "email", today=TODAY)

    def test_edited_draft_kept_suggestion_and_hashes_stable(self):
        path = self.kit / "requests" / f"{self.a}.md"
        original = path.read_bytes()
        path.write_bytes(original + b"Fee cap: 50\n")
        edited = path.read_bytes()
        self.verify(self.a, portal="https://records.example.invalid/portal")
        for _ in range(2):
            out = self.build()
            self.assertEqual(out, f"kept edited draft requests/{self.a}.md; fresh draft in requests.suggested/{self.a}.md\n")
            self.assertEqual(path.read_bytes(), edited)
            suggestion = (self.kit / "requests.suggested" / f"{self.a}.md").read_text(encoding="utf-8")
            self.assertIn("To: records portal https://records.example.invalid/portal (unknown)", suggestion)
            record = json.loads((self.kit / "drafts-written.json").read_text())["drafts"]
            self.assertEqual(record[f"{self.a}.md"], hashlib.sha256(original).hexdigest())

    def test_unedited_draft_refreshes_with_new_hash_and_is_stable(self):
        path = self.kit / "requests" / f"{self.a}.md"
        old_hash = json.loads((self.kit / "drafts-written.json").read_text())["drafts"][f"{self.a}.md"]
        self.verify(self.a, mailing_address=ADDRESS)
        self.assertEqual(self.build(), "")
        first = path.read_bytes()
        self.assertIn(POST_TO.encode(), first)
        new_hash = json.loads((self.kit / "drafts-written.json").read_text())["drafts"][f"{self.a}.md"]
        self.assertEqual(new_hash, hashlib.sha256(first).hexdigest())
        self.assertNotEqual(new_hash, old_hash)
        self.assertEqual(self.build(), "")
        self.assertEqual(path.read_bytes(), first)
        self.assertFalse((self.kit / "requests.suggested" / f"{self.a}.md").exists())


if __name__ == "__main__":
    unittest.main()
