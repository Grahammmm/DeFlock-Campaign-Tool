"""DF2007-12: records e2e-proof --with-adoption rehearses the real delta and adoption stages on synthetic data."""
import contextlib
import hashlib
import io
import json
import os
import shutil
import smtplib
import sqlite3
import stat
import tempfile
import unittest
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path
from unittest import mock

from campaign_tool.records import e2e_proof, native_extract, run as records_run
from campaign_tool.records.mail_adoption import DISABLE_MARKER
from tests.records.test_e2e_proof import FIXTURE, HAVE_PARSERS, pdf_email, png_scan, text_pdf
from tests.records.test_e2e_proof_unread import TOOLS
from tests.records.test_run_slice import synthetic_email

NOW = datetime(2026, 11, 15, 12, 0, 0, tzinfo=timezone.utc)
SECOND_RUN = "run_20261115T120001Z_00000002"
SENTINEL_SENDER, SENTINEL_SUBJECT, SENTINEL_FILE = "sentinel-sender@sentinel-agency.example.invalid", \
    "SENTINEL-private-subject", "SENTINEL-private-file.txt"


def make_project(root):
    """A project directory laid out as the replay makes it: owner-only marker with the exact sandbox text."""
    project = Path(root) / "project"
    project.mkdir(mode=0o700, parents=True)
    marker = project / e2e_proof.MARKER
    marker.write_text("e2e-proof sandbox\n", encoding="utf-8")
    marker.chmod(0o600)
    return project


def snapshot(root):
    """Every path under ``root`` with its mode and content digest (or link target): a no-write proof."""
    rows = []
    for path in sorted(Path(root).rglob("*")):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            rows.append((str(path), "link", os.readlink(path)))
        elif stat.S_ISREG(info.st_mode):
            rows.append((str(path), stat.S_IMODE(info.st_mode), hashlib.sha256(path.read_bytes()).hexdigest()))
        else:
            rows.append((str(path), stat.S_IMODE(info.st_mode), "dir"))
    return rows


def count(project, table):
    db = sqlite3.connect("file:%s?mode=ro" % (Path(project) / "processing-v1" / "ledger.sqlite"), uri=True)
    try:
        return db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
    finally:
        db.close()


def stage(report, name):
    return next(s for s in report["stages"] if s["stage"] == name)


class AdoptionBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="e2e-adopt-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.campaign = self.tmp / "campaign"
        (self.campaign / "content").mkdir(parents=True)
        (self.campaign / "content" / "agencies.json").write_text(json.dumps(
            [{"agency_id": "ca-agency", "name": "Agency", "profile": {"status": "Records requested", "summary": "s"}}]))
        self.message = self.tmp / "m.eml"
        self.raw = pdf_email()  # the multipart boundary is random: build once so every use is the same message
        self.message.write_bytes(self.raw)

    def replay(self, label, message=None, **kwargs):
        return e2e_proof.replay(message or self.message, self.tmp / "out", self.campaign, label=label,
                                with_adoption=kwargs.pop("with_adoption", True), **kwargs)

    def sandbox(self, label):
        return self.tmp / "out" / label

    def project(self, label):
        return self.sandbox(label) / "project"


class RehearsalTests(AdoptionBase):
    @unittest.skipUnless(HAVE_PARSERS, "UNSUPPORTED_RUNTIME: pypdf/openpyxl not in this interpreter")
    def test_fictional_reply_rehearses_real_delta_and_adoption(self):
        with mock.patch.object(records_run.Pipeline, "ingest_inbox", side_effect=AssertionError("ingest shortcut")):
            report = self.replay("one")
        self.assertEqual((report["result"], report["manual_steps"], report["unread"]), ("PASS", [], []), report["stages"])
        self.assertIs(report["with_adoption"], True)
        self.assertEqual([s["stage"] for s in report["stages"]], [
            "arrival_trigger", "adoption_setup", "delta", "adoption", "save_mail", "extract", "digest_gates",
            "outputs", "next_steps", "telegram_text", "console"])
        for name in ("delta", "adoption"):
            counts = stage(report, name)["detail"]["counts"]
            self.assertEqual((stage(report, name)["status"], counts["input"], counts["output"], counts["failed"]),
                             ("ok", 1, 1, 0), name)
        self.assertEqual(stage(report, "save_mail")["detail"], {"failures": 0, "messages": 1})
        project = self.project("one")
        self.assertEqual((count(project, "mail_messages"), count(project, "originals"), count(project, "occurrences")),
                         (1, 2, 2))
        db = sqlite3.connect("file:%s?mode=ro" % (project / "processing-v1" / "ledger.sqlite"), uri=True)
        self.addCleanup(db.close)
        originals = {r[0] for r in db.execute("SELECT sha256 FROM originals")}
        self.assertEqual(originals, {hashlib.sha256(self.raw).hexdigest(), hashlib.sha256(text_pdf()).hexdigest()})
        self.assertEqual(stage(report, "extract")["detail"]["counts"]["output"], 2)
        markdown = (self.sandbox("one") / "proof-report.md").read_text()
        self.assertIn("were not rehearsed", markdown)
        self.assertEqual(json.loads((self.sandbox("one") / "proof-report.json").read_text())["with_adoption"], True)

    def test_same_sandbox_rerun_adopts_nothing_new(self):
        report = self.replay("one", message=self.tmp / "m.eml")
        self.assertEqual(stage(report, "adoption")["detail"]["counts"]["output"], 1)
        project = self.project("one")
        before = (count(project, "mail_messages"), count(project, "originals"), count(project, "occurrences"))
        timeline = e2e_proof.Timeline(self.sandbox("one"))
        delta, adoption = e2e_proof._rehearse_adoption(project, SECOND_RUN, project / e2e_proof.ADOPTION_CONFIG, timeline)
        for entry in (delta, adoption):
            counts = entry["detail"]["counts"]
            self.assertEqual((entry["status"], counts["input"], counts["output"], counts["skipped"], counts["failed"]),
                             ("ok", 1, 0, 1, 0), entry["stage"])
        self.assertEqual((count(project, "mail_messages"), count(project, "originals"), count(project, "occurrences")),
                         before)
        files = snapshot(self.sandbox("one") / "project" / "mail-history")
        with self.assertRaises(e2e_proof.ProofError) as caught:  # a finished replay is never reopened or overwritten
            self.replay("one")
        self.assertEqual(caught.exception.code, "OUT_EXISTS")
        self.assertEqual(snapshot(self.sandbox("one") / "project" / "mail-history"), files)

    def test_two_fresh_labels_each_adopt_their_own_message(self):
        reports = [self.replay("a"), self.replay("b")]
        for report in reports:
            self.assertEqual(stage(report, "adoption")["detail"]["counts"]["output"], 1)
        self.assertEqual([count(self.project(x), "mail_messages") for x in "ab"], [1, 1])

    def test_generated_malformed_config_fails_naming_adoption_code(self):
        cases = {"malformed_json": b'{"SENTINEL-config-secret',
                 "unknown_key": json.dumps({"schema": e2e_proof.ADOPTION_CONFIG_SCHEMA, "source": "mail-history",
                                            "adoption": "enabled", "disable_marker_sha256": None,
                                            "account": "SENTINEL-config-secret"}).encode(),
                 "wrong_adoption_value": json.dumps({"schema": e2e_proof.ADOPTION_CONFIG_SCHEMA, "source": "mail-history",
                                                     "adoption": "SENTINEL-config-secret",
                                                     "disable_marker_sha256": None}).encode()}
        for name, body in cases.items():
            with self.subTest(case=name), mock.patch.object(e2e_proof, "_adoption_config_bytes", return_value=body):
                report = self.replay("bad-" + name, message=self.tmp / "m.eml")
                self.assertEqual(report["result"], "FAIL")
                self.assertEqual((stage(report, "delta")["status"], stage(report, "adoption")["status"],
                                  stage(report, "adoption")["code"]), ("ok", "fail", "ADOPTION_REFUSED"))
                self.assertIn({"stage": "adoption", "why": "ADOPTION_REFUSED"}, report["manual_steps"])
                for later in ("save_mail", "extract", "digest_gates", "outputs", "console"):
                    self.assertEqual(stage(report, later)["status"], "not_run", later)
                self.assertEqual(count(self.project("bad-" + name), "mail_messages"), 0)
                for name_ in ("proof-report.json", "proof-report.md"):
                    self.assertNotIn("SENTINEL-config-secret", (self.sandbox("bad-" + name) / name_).read_text())

    def test_config_with_public_mode_is_refused_by_the_real_adapter(self):
        real = e2e_proof._prepare_adoption

        def prepare(project, raw, now):
            result = real(project, raw, now)
            os.chmod(Path(project) / e2e_proof.ADOPTION_CONFIG, 0o644)
            return result
        with mock.patch.object(e2e_proof, "_prepare_adoption", prepare):
            report = self.replay("mode", message=self.tmp / "m.eml")
        self.assertEqual((report["result"], stage(report, "adoption")["code"]), ("FAIL", "ADOPTION_REFUSED"))
        self.assertEqual(stage(report, "extract")["status"], "not_run")

    def test_other_real_adoption_codes_propagate_unchanged(self):
        real = e2e_proof._adapter

        def adapter(name, project, run_id, *extra):
            status, code, detail = real(name, project, run_id, *extra)
            return ("fail", "ADOPTION_BLOCKED", detail) if name == "adoption" else (status, code, detail)
        with mock.patch.object(e2e_proof, "_adapter", adapter):
            report = self.replay("blocked", message=self.tmp / "m.eml")
        self.assertEqual(report["result"], "FAIL")
        self.assertIn({"stage": "adoption", "why": "ADOPTION_BLOCKED"}, report["manual_steps"])
        self.assertEqual(stage(report, "extract")["status"], "not_run")

    def test_message_the_real_classifier_rejects_is_never_reported_as_adopted(self):
        raw = (b"From: a@agency.example.invalid\r\nTo: b@campaign.example.invalid\r\nSubject: x\r\n"
               b"Message-ID: <nested@agency.example.invalid>\r\nMIME-Version: 1.0\r\n"
               b"Content-Type: multipart/mixed; boundary=XX\r\n\r\n--XX\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
               b"--XX\r\nContent-Type: message/rfc822\r\n\r\nSubject: inner\r\n\r\ninner body\r\n--XX--\r\n")
        nested = self.tmp / "nested.eml"
        nested.write_bytes(raw)
        report = self.replay("nested", message=nested)
        # The real adopter sets the message aside as a coded gap (its own warn code); the proof must not call that adopted.
        self.assertEqual((stage(report, "adoption")["status"], stage(report, "adoption")["code"],
                          stage(report, "adoption")["detail"]["counts"]["output"]), ("warn", "ADOPTION_CODED_GAPS", 0))
        self.assertEqual(report["result"], "FAIL")
        self.assertIn({"stage": "save_mail", "why": "SAVE_MAIL_NOT_ADOPTED"}, report["manual_steps"])
        self.assertEqual(stage(report, "extract")["status"], "not_run")
        self.assertEqual(count(self.project("nested"), "mail_messages"), 0)


class SetupSafetyTests(AdoptionBase):
    def test_non_sandbox_refusal_precedes_every_write(self):
        def canary(project):
            (project / "processing-v1").mkdir(parents=True, exist_ok=True)
            (project / "processing-v1" / "ledger.sqlite").write_bytes(b"not touched")

        def missing(root):
            project = Path(root) / "project"
            canary(project)
            return project

        def wrong_text(root):
            project = make_project(root)
            (project / e2e_proof.MARKER).write_text("some other sandbox\n")
            canary(project)
            return project

        def symlink_marker(root):
            project = make_project(root)
            (project / e2e_proof.MARKER).unlink()
            (Path(root) / "elsewhere").write_text("e2e-proof sandbox\n")
            os.chmod(Path(root) / "elsewhere", 0o600)
            (project / e2e_proof.MARKER).symlink_to(Path(root) / "elsewhere")
            canary(project)
            return project

        def public_marker(root):
            project = make_project(root)
            (project / e2e_proof.MARKER).chmod(0o644)
            canary(project)
            return project

        def symlink_root(root):
            real = make_project(Path(root) / "real")
            canary(real)
            link = Path(root) / "linked" / "project"
            link.parent.mkdir()
            link.symlink_to(real)
            return link

        def wrong_name(root):
            project = make_project(root)
            project = project.rename(Path(root) / "other")
            canary(project)
            return project

        for name, build in (("missing", missing), ("wrong_text", wrong_text), ("symlink_marker", symlink_marker),
                            ("public_marker", public_marker), ("symlink_root", symlink_root), ("wrong_name", wrong_name)):
            with self.subTest(case=name):
                root = self.tmp / name
                root.mkdir()
                project = build(root)
                before = snapshot(root)
                with self.assertRaises(e2e_proof.ProofError) as caught:
                    e2e_proof._prepare_adoption(project, self.raw, NOW)
                self.assertEqual(caught.exception.code, "ADOPTION_NOT_SANDBOX")
                self.assertEqual(snapshot(root), before)
                timeline = e2e_proof.Timeline(root)
                delta, adoption = e2e_proof._rehearse_adoption(project, SECOND_RUN, project / e2e_proof.ADOPTION_CONFIG,
                                                               timeline)
                self.assertEqual((delta["status"], delta["code"], adoption["status"]),
                                 ("fail", "ADOPTION_NOT_SANDBOX", "not_run"))
                self.assertEqual(snapshot(root), before)
        with self.subTest(case="wrong_owner"):
            root = self.tmp / "owner"
            root.mkdir()
            project = make_project(root)
            before = snapshot(root)
            with mock.patch.object(e2e_proof.os, "geteuid", return_value=os.geteuid() + 1):
                with self.assertRaises(e2e_proof.ProofError) as caught:
                    e2e_proof._prepare_adoption(project, self.raw, NOW)
            self.assertEqual(caught.exception.code, "ADOPTION_NOT_SANDBOX")
            self.assertEqual(snapshot(root), before)

    def test_linked_output_directory_is_refused_without_writing_through_it(self):
        project = make_project(self.tmp / "linked")
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        (project / "mail-history").symlink_to(elsewhere)
        with self.assertRaises(e2e_proof.ProofError) as caught:
            e2e_proof._prepare_adoption(project, self.raw, NOW)
        self.assertEqual(caught.exception.code, "ADOPTION_SETUP_REFUSED")
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertFalse((project / e2e_proof.ADOPTION_CONFIG).exists())

    def test_setup_never_repairs_gate_or_replaces_existing_generated_data(self):
        project = make_project(self.tmp / "ok")
        first = e2e_proof._prepare_adoption(project, self.raw, NOW)
        self.assertEqual(first[:2], ("ok", None))
        stored = snapshot(project)
        receipts = list((project / "mail-history" / "receipts").iterdir())
        self.assertEqual(len(receipts), 1)
        self.assertEqual(e2e_proof._prepare_adoption(project, self.raw, NOW)[:2], ("ok", None))  # identical: reused
        self.assertEqual(len(list((project / "mail-history" / "receipts").iterdir())), 1)
        self.assertEqual([r for r in snapshot(project) if "ledger.sqlite" not in r[0]],
                         [r for r in stored if "ledger.sqlite" not in r[0]])
        config = project / e2e_proof.ADOPTION_CONFIG
        for name, damage in (("config_bytes", lambda: config.write_text("{}\n")),
                             ("config_mode", lambda: config.chmod(0o644)),
                             ("receipt_bytes", lambda: receipts[0].write_text("{}\n"))):
            with self.subTest(case=name):
                project = make_project(self.tmp / name)
                e2e_proof._prepare_adoption(project, self.raw, NOW)
                receipts = list((project / "mail-history" / "receipts").iterdir())
                config = project / e2e_proof.ADOPTION_CONFIG
                damage()
                before = snapshot(project / "mail-history")
                held = (config.read_bytes(), config.stat().st_mode, receipts[0].read_bytes())
                with self.assertRaises(e2e_proof.ProofError) as caught:
                    e2e_proof._prepare_adoption(project, self.raw, NOW)
                self.assertEqual(caught.exception.code, "ADOPTION_SETUP_REFUSED")
                self.assertEqual((config.read_bytes(), config.stat().st_mode, receipts[0].read_bytes()), held)
                self.assertEqual(snapshot(project / "mail-history"), before)
        with self.subTest(case="disable_marker"):
            project = make_project(self.tmp / "marker")
            (project / "processing-v1").mkdir()
            marker = project / "processing-v1" / DISABLE_MARKER
            marker.write_bytes(b'{"SENTINEL": 1}')
            before = snapshot(project)
            with self.assertRaises(e2e_proof.ProofError) as caught:
                e2e_proof._prepare_adoption(project, self.raw, NOW)
            self.assertEqual(caught.exception.code, "ADOPTION_SETUP_REFUSED")
            self.assertEqual(snapshot(project), before)
            self.assertFalse((project / "mail-history").exists())

    def test_export_identity_is_always_fictional_and_not_header_derived(self):
        seen = set()
        for sender, recipient in (("a@one.example.invalid", "b@two.example.invalid"),
                                  ("someone@real-looking.example", "owner@live-account.example")):
            message = EmailMessage()
            message["From"], message["To"], message["Subject"] = sender, recipient, "s"
            message["Message-ID"] = "<m@one.example.invalid>"
            message.set_content("body\n")
            message.add_attachment(text_pdf(), maintype="application", subtype="pdf", filename="p.pdf")
            raw = message.as_bytes(policy=SMTP)
            with mock.patch.dict(os.environ, {"MAIL_ACCOUNT": "SENTINEL-live-account"}):
                receipt, objects = e2e_proof._adoption_receipt(raw, NOW)
            self.assertEqual((receipt["account_id"], receipt["folder"], receipt["uidvalidity"], receipt["uid"]),
                             ("owner@adoption.example.invalid", "Sandbox.example.invalid/INBOX", 1, 1))
            self.assertEqual(receipt["original_eml"]["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(objects["objects/%s/%s" % (receipt["original_eml"]["sha256"][:2],
                                                       receipt["original_eml"]["sha256"])], raw)
            self.assertEqual([a["sha256"] for a in receipt["attachments"]], [hashlib.sha256(text_pdf()).hexdigest()])
            self.assertEqual(receipt["attachments"][0]["original_filename"], "p.pdf")
            seen.add(json.dumps({k: v for k, v in receipt.items() if k not in ("original_eml", "bytes", "attachments")},
                                sort_keys=True))
        self.assertEqual(len(seen), 1)
        self.assertNotIn("SENTINEL-live-account", seen.pop())
        config = json.loads(e2e_proof._adoption_config_bytes())
        self.assertEqual(config, {"schema": "mail-history-adoption-v1", "source": "mail-history", "adoption": "enabled",
                                  "disable_marker_sha256": None})


class ReportTests(AdoptionBase):
    def test_flag_absent_preserves_default_report_bytes(self):
        paths = ("proof-report.json", "proof-report.md", "telegram-summary.txt", "project/run-report.json")
        out = {}
        for label, kwargs in (("omitted", {}), ("false", {"with_adoption": False})):
            with mock.patch.object(e2e_proof, "_run_id", lambda now: "run_20261115T120000Z_0badc0de"), \
                    mock.patch.object(e2e_proof, "time", type("T", (), {"monotonic": staticmethod(lambda: 1.0)})):
                report = e2e_proof.replay(self.tmp / "m.eml", self.tmp / "out", self.campaign, label=label, now=NOW,
                                          as_of="2026-11-15", **kwargs)
            base = self.sandbox(label)
            out[label] = {p: self.normalise((base / p).read_text(), base) for p in paths}
            self.assertNotIn("with_adoption", report)
            self.assertEqual([s["stage"] for s in report["stages"]], [
                "arrival_trigger", "save_mail", "extract", "digest_gates", "outputs", "next_steps", "telegram_text",
                "console"])
            self.assertFalse((base / "project" / "mail-history").exists())
            self.assertFalse((base / "project" / e2e_proof.ADOPTION_CONFIG).exists())
            self.assertNotIn("adoption", out[label]["proof-report.md"].replace("not_run", ""))
            skipped = json.loads((base / "project" / "run-report.json").read_text())["stages"]
            self.assertEqual([(s["name"], s["status"]) for s in skipped[:4]], [
                ("sync", "skipped"), ("delta", "skipped"), ("adoption", "skipped"), ("portal", "skipped")])
        self.assertEqual(out["omitted"], out["false"])

    @staticmethod
    def normalise(text, base):
        import re
        text = text.replace(str(base), "BASE")
        text = re.sub(r'"(finished_at|seen_at)": "[^"]*"', r'"\1": "T"', text)
        return re.sub(r'"duration_seconds": \d+', '"duration_seconds": 0', text)

    @unittest.skipUnless(HAVE_PARSERS, "UNSUPPORTED_RUNTIME: pypdf/openpyxl not in this interpreter")
    def test_enabled_stage_report_is_honest_and_private(self):
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = SENTINEL_SENDER, "requests@campaign.example.invalid", SENTINEL_SUBJECT
        message["Message-ID"] = "<sentinel@sentinel-agency.example.invalid>"
        message.set_content("The policy is attached.\n")
        message.add_attachment(text_pdf(), maintype="application", subtype="pdf", filename=SENTINEL_FILE.replace("txt", "pdf"))
        sentinel = self.tmp / "sentinel.eml"
        sentinel.write_bytes(message.as_bytes(policy=SMTP))
        trip = AssertionError("network or send entry point used")
        with mock.patch.object(smtplib.SMTP, "__init__", side_effect=trip), \
                mock.patch.object(smtplib.SMTP_SSL, "__init__", side_effect=trip), \
                mock.patch.object(urllib.request, "urlopen", side_effect=trip):
            report = self.replay("private", message=sentinel)
        self.assertEqual((report["result"], report["manual_steps"]), ("PASS", []), report["stages"])
        base = self.sandbox("private")
        shown = {p: (base / p).read_text() for p in ("proof-report.json", "proof-report.md", "telegram-summary.txt",
                                                      "project/run-report.json")}
        for name, text in shown.items():
            for secret in (SENTINEL_SENDER, SENTINEL_SUBJECT, SENTINEL_FILE.replace("txt", "pdf"),
                           e2e_proof.ADOPTION_ACCOUNT, e2e_proof.ADOPTION_FOLDER, "Sandbox.example.invalid",
                           "mail-history-adoption-v1"):
                self.assertNotIn(secret, text, (name, secret))
        stages = json.loads(shown["project/run-report.json"])["stages"]
        names = [s["name"] for s in stages]
        self.assertEqual(names.count("delta"), 1)
        self.assertEqual(names.count("adoption"), 1)
        by_name = {s["name"]: s for s in stages}
        self.assertEqual([(by_name[n]["status"], by_name[n]["error_code"]) for n in ("sync", "portal")],
                         [("skipped", "REPLAY_STARTS_AFTER_MAILBOX_SYNC")] * 2)
        self.assertEqual((by_name["delta"]["status"], by_name["delta"]["counts"]["output"]), ("ok", 1))
        self.assertEqual((by_name["adoption"]["status"], by_name["adoption"]["counts"]["output"]), ("ok", 1))
        self.assertEqual(names[:4], ["sync", "delta", "adoption", "portal"])
        self.assertIn("delta ok 1/1, adoption ok 1/1", shown["telegram-summary.txt"])
        for entry in report["stages"]:
            for output in entry["outputs"]:
                self.assertFalse(output.startswith("/"), output)
        self.assertIs(report["sent"], False)
        self.assertIs(report["published"], False)

    def test_adoption_preserves_parent_unread_fail_policy(self):
        picture = self.tmp / "pic.eml"
        picture.write_bytes(synthetic_email(attachment=png_scan(), filename="scan.png"))
        real = native_extract._importable
        with mock.patch.object(native_extract, "_importable",
                               side_effect=lambda name: False if name == "PIL.Image" else real(name)), \
                mock.patch.object(records_run, "local_ocr_tools", return_value=TOOLS):
            report = self.replay("pic", message=picture, ocr=True)
        png = hashlib.sha256(png_scan()).hexdigest()
        self.assertEqual(report["result"], "FAIL")
        self.assertIn({"stage": "extract", "why": "extract:PARSER_MISSING"}, report["manual_steps"])
        self.assertEqual(report["unread"], [{"sha256_prefix": png[:12], "kind": "png", "outcome": "PARSER_MISSING"}])
        self.assertEqual(stage(report, "adoption")["status"], "ok")

    @unittest.skipUnless(HAVE_PARSERS, "UNSUPPORTED_RUNTIME: pypdf/openpyxl not in this interpreter")
    def test_adoption_keeps_corrupt_pdf_agency_side(self):
        bad = b"%PDF-1.4\nnot a document"
        corrupt = self.tmp / "bad.eml"
        corrupt.write_bytes(synthetic_email(attachment=bad, filename="bad.pdf"))
        report = self.replay("bad", message=corrupt)
        self.assertEqual((report["result"], report["manual_steps"]), ("PASS", []), report["stages"])
        self.assertEqual(report["unread"], [{"sha256_prefix": hashlib.sha256(bad).hexdigest()[:12], "kind": "pdf",
                                             "outcome": "CORRUPT_DOCUMENT"}])


class SeedAndOcrTests(AdoptionBase):
    @unittest.skipUnless(HAVE_PARSERS, "UNSUPPORTED_RUNTIME: pypdf/openpyxl not in this interpreter")
    def test_seed_follows_the_adopted_message_and_joins_each_original(self):
        shutil.rmtree(self.campaign)
        shutil.copytree(FIXTURE / "content", self.campaign / "content")
        seed = self.tmp / "seed.json"
        seed.write_text((FIXTURE / "seed.json").read_text().replace(
            "@POLICY_PDF_SHA256@", hashlib.sha256(text_pdf()).hexdigest()))
        report = self.replay("seeded", seed=seed)
        self.assertEqual((report["result"], report["manual_steps"]), ("PASS", []), report["stages"])
        self.assertEqual([s["stage"] for s in report["stages"]][:7], [
            "arrival_trigger", "adoption_setup", "delta", "adoption", "save_mail", "seed", "extract"])
        project = self.project("seeded")
        self.assertEqual(stage(report, "seed")["detail"]["joins"], count(project, "originals"))
        self.assertEqual(count(project, "joins"), count(project, "originals"))
        self.assertEqual(count(project, "mail_messages"), 1)

    def test_ocr_budgets_are_the_parents_and_a_bad_setup_stops_everything_after_it(self):
        seen = []
        real = e2e_proof._stage

        def stage_(argv):
            seen.append(argv)
            if argv[0] in ("delta", "adoption"):
                return real(argv)
            if argv[0] == "extract":
                project, run_id = Path(argv[argv.index("--project") + 1]), argv[argv.index("--run-id") + 1]
                (project / "processing-v1" / "runs").mkdir(parents=True, exist_ok=True)
                native_extract.write_private_list(project / "processing-v1", run_id, [])
            return 0, {"status": "ok", "counts": {}, "outcomes": []}
        with mock.patch.object(e2e_proof, "_stage", stage_):
            report = self.replay("ocr", message=self.tmp / "m.eml", ocr=True)
        self.assertTrue(report["ocr"])
        self.assertEqual([a[0] for a in seen][:3], ["delta", "adoption", "extract"])
        extract = next(a for a in seen if a[0] == "extract")
        self.assertEqual(extract[extract.index("--timeout") + 1], "600")
        self.assertEqual(extract[extract.index("--ocr-page-timeout") + 1], "120")
        self.assertEqual(extract[extract.index("--document-timeout") + 1], "300")
        self.assertIn("--ocr", extract)
        # a setup that fails leaves seed, extract and every later stage not run, and runs neither adapter
        seen.clear()
        with mock.patch.object(e2e_proof, "_stage", stage_), \
                mock.patch.object(e2e_proof, "_prepare_adoption", side_effect=e2e_proof.ProofError("ADOPTION_SETUP_REFUSED")):
            report = self.replay("nosetup", message=self.tmp / "m.eml", seed=self.tmp / "unused-seed.json")
        self.assertEqual(seen, [])
        self.assertEqual(report["result"], "FAIL")
        self.assertEqual([(s["stage"], s["status"]) for s in report["stages"]][1:], [
            ("adoption_setup", "fail"), ("delta", "not_run"), ("adoption", "not_run"), ("save_mail", "not_run"),
            ("seed", "not_run"), ("extract", "not_run"), ("digest_gates", "not_run"), ("outputs", "not_run"),
            ("next_steps", "not_run"), ("telegram_text", "not_run"), ("console", "not_run")])
        self.assertIn({"stage": "adoption_setup", "why": "ADOPTION_SETUP_REFUSED"}, report["manual_steps"])


class FlagTests(unittest.TestCase):
    def test_flag_reaches_replay_only_when_given(self):
        for argv, expected in (([], False), (["--with-adoption"], True)):
            with mock.patch.object(e2e_proof, "replay", return_value={"result": "PASS"}) as replay, \
                    mock.patch.object(e2e_proof, "pick_message", return_value=Path("m.eml")), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = e2e_proof.main(["--message", "m.eml", "--campaign-root", "c", "--out", "o", *argv])
            self.assertEqual(code, 0)
            self.assertIs(replay.call_args.kwargs["with_adoption"], expected)


if __name__ == "__main__":
    unittest.main()
