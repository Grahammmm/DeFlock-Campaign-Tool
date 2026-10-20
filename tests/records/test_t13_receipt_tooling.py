"""T13 receipt tooling (G D3-A / D4-A, 2026-10-20). Synthetic fixtures only.

* ``SandboxBaselineTests``: ``scripts/t11i_sandbox_baseline.py`` seeds a marked
  sandbox copy from a pinned pre-apply backup and relocates the backup's
  ``verified_mail_export`` identity paths from the TEST root to the sandbox root.
  The relocated sandbox adopts its remaining receipts with no identity conflict.
  A wrong pin, a missing marker, a backup with a non-empty WAL and a foreign-root
  reference it cannot relocate are refused, and nothing is written to TEST.
* ``FailureManifestTests``: ``scripts/extract_failure_manifest.py`` itemizes the
  failed documents of the newest extract run by hashes, ledger ids, uid numbers, a
  UTC date and a fixed sender-domain class, with no address, domain or text.
* ``LegacyReceiptFormTests``: pre-T4 identities bind ``export_receipt_path`` to the
  intake verification copy of the receipt, with no ``objects`` beside it. The
  format of their attachments comes from the receipt entry and the canonical
  original (``eml_export.attachment_forms(heads=)``, ``Pipeline._form``) instead of
  an EXTRACT_ERROR.

No Docker, network, mailbox, TEST copy or live data is used.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from campaign_tool.records import mail_adoption
from campaign_tool.records.intake import eml_export
from campaign_tool.records.run import Pipeline
from tests.records.test_t12_harness_strictness import load_script, make_project, write_private, write_receipt
from tests.runner.helpers import tiny_pdf

baseline = load_script("t11i_sandbox_baseline")
manifest = load_script("extract_failure_manifest")
injector = load_script("t12_fault_inject")


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def adopt(project):
    root = Pipeline(project / "processing-v1").root.path
    return mail_adoption.adopt(root, project / "mail-history", apply=True,
                               config_path=project / ".t4-adoption-config-v1.json")


def query(ledger, sql, *args):
    con = sqlite3.connect(Path(ledger).as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def tree_digest(root):
    digest = hashlib.sha256()
    for path in sorted(Path(root).rglob("*")):
        if path.is_file() and not path.is_symlink():
            digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


class SandboxBaselineTests(unittest.TestCase):
    def setUp(self):
        os.umask(0o077)
        temp = tempfile.TemporaryDirectory(prefix="t13-baseline-")
        self.addCleanup(temp.cleanup)
        work = Path(temp.name).resolve()
        # "TEST": one message adopted (the pre-apply state), backed up, then the rest adopted.
        built = make_project(work / "build")
        self.test = work / "records-project-TEST"
        shutil.copytree(built, self.test, symlinks=True)
        (self.test / injector.SANDBOX_MARKER).unlink()  # TEST itself is never a sandbox
        receipts = sorted((self.test / "mail-history" / "receipts").glob("*.json"))
        self.held = self.test / ".held-receipts"
        self.held.mkdir(mode=0o700)
        for path in receipts[1:]:
            path.rename(self.held / path.name)
        adopt(self.test)
        self.backup = self.test / ".t4-ledger-backup-synthetic"
        self.backup.mkdir(mode=0o700)
        for name, rel in baseline.DATABASES:
            con = sqlite3.connect(self.test / rel)
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.close()
            shutil.copy2(self.test / rel, self.backup / name)
        self.backup_sha = sha256_file(self.backup / "ledger.sqlite")
        for path in self.held.iterdir():
            path.rename(self.test / "mail-history" / "receipts" / path.name)
        adopt(self.test)
        # The sandbox: a copy of the post-apply TEST, marked.
        self.sandbox = work / ".t11i-sandbox-synthetic" / "records-project"
        self.sandbox.parent.mkdir(mode=0o700)
        self.sandbox.mkdir(mode=0o700)
        for item in ("mail-history", "processing-v1", "portal-queue-v1", ".t4-adoption-config-v1.json"):
            source = self.test / item
            (shutil.copytree if source.is_dir() else shutil.copy2)(source, self.sandbox / item)
        write_private(self.sandbox / injector.SANDBOX_MARKER, b'{"sandbox_marker_version":1}\n')
        self.roots = [str(work / "records-project"), str(self.test)]
        self.test_digest = tree_digest(self.test)

    def seed(self, **kw):
        args = dict(project=self.sandbox, backup=self.backup, ledger_sha256=self.backup_sha,
                    from_roots=self.roots)
        args.update(kw)
        return baseline.seed(args["project"], args["backup"], args["ledger_sha256"], args["from_roots"])

    def test_seeds_the_pinned_backup_and_relocates_every_identity_path(self):
        ledger = self.sandbox / "processing-v1" / "ledger.sqlite"
        test_count = query(self.test / "processing-v1" / "ledger.sqlite", "SELECT count(*) FROM mail_messages")
        self.assertEqual(query(ledger, "SELECT count(*) FROM mail_messages"), test_count)  # post-apply copy
        self.assertEqual(test_count, [(3,)])
        result = self.seed()
        self.assertEqual(result["baseline_ledger_sha256"], self.backup_sha)
        self.assertEqual(result["mail_messages"], 1)
        self.assertGreaterEqual(result["occurrences_relocated"], 1)
        self.assertGreaterEqual(result["originals_relocated"], 1)
        self.assertEqual((result["foreign_root_refs_after"], result["integrity"], result["foreign_key_violations"]),
                         (0, "ok", 0))
        self.assertIs(result["immutability_trigger_enforced"], True)
        for (evidence,) in query(ledger, "SELECT evidence FROM occurrences WHERE "
                                         "acquisition_method='verified_mail_export'"):
            for key in baseline.EV_KEYS:
                self.assertTrue(json.loads(evidence)[key].startswith(str(self.sandbox) + "/"), key)
        for (path,) in query(ledger, "SELECT storage_path FROM originals WHERE storage_path IS NOT NULL"):
            self.assertFalse(path.startswith(str(self.test) + "/"))
        self.assertEqual(oct((self.sandbox / "processing-v1" / "ledger.sqlite").stat().st_mode & 0o777), "0o600")
        # The relocated sandbox is the restored baseline: the rest adopts with no identity conflict.
        report = adopt(self.sandbox)
        self.assertEqual(sum(report["identity_conflicts"].values()), 0, report["identity_conflicts"])
        self.assertEqual(query(ledger, "SELECT count(*) FROM mail_messages"),
                         query(self.test / "processing-v1" / "ledger.sqlite", "SELECT count(*) FROM mail_messages"))
        self.assertEqual(tree_digest(self.test), self.test_digest)  # TEST only read

    def test_the_main_entry_prints_one_json_line(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = baseline.main(["--project", str(self.sandbox), "--backup", str(self.backup),
                                  "--ledger-sha256", "0" * 64, *sum((["--from-root", r] for r in self.roots), [])])
        self.assertEqual((code, json.loads(out.getvalue())),
                         (1, {"status": "refused", "code": "baseline_ledger_hash_mismatch"}))

    def assertRefused(self, code, **kw):
        before, test_before = tree_digest(self.sandbox), tree_digest(self.test)
        with self.assertRaises(baseline.Refused) as caught:
            self.seed(**kw)
        self.assertEqual(str(caught.exception), code)
        self.assertEqual(tree_digest(self.test), test_before)  # TEST (and its backup) only read
        return before

    def test_a_different_backup_ledger_is_refused_before_any_write(self):
        before = self.assertRefused("baseline_ledger_hash_mismatch", ledger_sha256="f" * 64)
        self.assertEqual(tree_digest(self.sandbox), before)

    def test_a_project_without_the_sandbox_marker_is_refused(self):
        (self.sandbox / injector.SANDBOX_MARKER).unlink()
        self.assertRefused("sandbox_marker_missing")
        self.assertRefused("sandbox_marker_missing", project=self.test)

    def test_a_backup_with_a_non_empty_wal_is_refused(self):
        (self.backup / "ledger.sqlite-wal").write_bytes(b"pending frames")
        before = self.assertRefused("sidecar_not_empty")
        self.assertEqual(tree_digest(self.sandbox), before)

    def test_a_foreign_root_reference_that_cannot_be_relocated_is_refused(self):
        ledger = self.backup / "ledger.sqlite"
        con = sqlite3.connect(ledger)
        with con:
            row = con.execute("SELECT id, original_sha256 FROM occurrences LIMIT 1").fetchone()
            con.execute("INSERT INTO occurrences(id, original_sha256, kind, source_ref, parent_occurrence_id, "
                        "acquired_at, acquisition_method, evidence) VALUES(?,?,?,?,?,?,?,?)",
                        ("synthetic-local-ref", row[1], "local", "{}", None, None, "captured_legacy_local",
                         json.dumps({"path": str(self.test) + "/elsewhere/file.bin"})))
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
        self.assertRefused("foreign_root_reference_not_relocatable", ledger_sha256=sha256_file(ledger))

    def test_from_roots_must_be_outside_the_sandbox(self):
        self.assertRefused("from_root_invalid", from_roots=[str(self.sandbox)])
        self.assertRefused("from_root_missing", from_roots=[])

    # T13 review (Sol #1): no write may follow a symlink out of the marked sandbox.

    def assertContained(self, code="sandbox_path_not_contained", **kw):
        sandbox_before, backup_before = tree_digest(self.sandbox), tree_digest(self.backup)
        test_ledger = sha256_file(self.test / "processing-v1" / "ledger.sqlite")
        self.assertRefused(code, **kw)  # TEST (target of every link below) unchanged
        self.assertEqual(sha256_file(self.test / "processing-v1" / "ledger.sqlite"), test_ledger)
        self.assertEqual(tree_digest(self.backup), backup_before)
        self.assertEqual(tree_digest(self.sandbox), sandbox_before)

    def link_to_test(self, rel):
        shutil.rmtree(self.sandbox / rel)
        (self.sandbox / rel).symlink_to(self.test / rel, target_is_directory=True)

    def test_a_symlinked_processing_dir_is_refused(self):
        self.link_to_test("processing-v1")
        self.assertContained()

    def test_a_symlinked_intake_dir_is_refused(self):
        self.link_to_test("processing-v1/intake")
        self.assertContained()

    def test_a_symlinked_database_is_refused(self):
        for name, rel in baseline.DATABASES:
            with self.subTest(name):
                target = self.sandbox / rel
                saved = target.with_name(target.name + ".saved")
                target.rename(saved)
                target.symlink_to(self.test / rel)
                try:
                    self.assertContained()
                finally:
                    target.unlink()
                    saved.rename(target)

    def test_a_symlinked_sidecar_is_refused(self):
        wal = self.sandbox / "processing-v1" / "ledger.sqlite-wal"
        if os.path.lexists(wal):
            wal.unlink()
        wal.symlink_to(self.test / "processing-v1" / "ledger.sqlite")
        self.assertContained("sidecar_not_empty")

    def test_a_symlinked_project_is_refused(self):
        link = self.sandbox.parent.parent / "sandbox-link"
        link.symlink_to(self.sandbox, target_is_directory=True)
        self.assertContained(project=link)

    def test_a_project_reached_through_a_symlinked_ancestor_is_refused(self):
        link = self.sandbox.parent.parent / "sandbox-parent-link"
        link.symlink_to(self.sandbox.parent, target_is_directory=True)
        self.assertContained(project=link / self.sandbox.name)

    def test_a_relocation_target_behind_a_symlink_is_refused(self):
        # The blobs the relocated originals point at must resolve inside the sandbox, too.
        self.link_to_test("processing-v1/intake/blobs")
        before = tree_digest(self.test)
        with self.assertRaises(baseline.Refused) as caught:
            self.seed()
        self.assertEqual(str(caught.exception), "original_target_unexpected")
        self.assertEqual(tree_digest(self.test), before)


def make_manifest_project(base, documents, mails, occurrences):
    project = base / "records-project"
    runs = project / "processing-v1" / "runs"
    runs.mkdir(parents=True, mode=0o700)
    con = sqlite3.connect(project / "processing-v1" / "ledger.sqlite")
    con.executescript(
        "CREATE TABLE occurrences(id TEXT PRIMARY KEY, original_sha256 TEXT, kind TEXT, source_ref TEXT,"
        " parent_occurrence_id TEXT, acquired_at TEXT, acquisition_method TEXT, evidence TEXT);"
        "CREATE TABLE mail_messages(id TEXT PRIMARY KEY, account TEXT, folder TEXT, uidvalidity INTEGER,"
        " uid INTEGER, message_id TEXT, eml_sha256 TEXT, headers TEXT, received_at TEXT,"
        " account_provenance_status TEXT);")
    con.executemany("INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?)", occurrences)
    con.executemany("INSERT INTO mail_messages VALUES(?,?,?,?,?,?,?,?,?,?)", mails)
    con.commit()
    con.close()
    old = runs / "extract-run_old.json"
    write_private(old, json.dumps({"documents": [{"original_sha256": "9" * 64, "form": "pdf",
                                                  "outcome": "EXTRACT_ERROR"}]}).encode())
    os.utime(old, (1, 1))
    write_private(runs / "extract-run_new.json", json.dumps({"documents": documents}).encode())
    return project


def headers(sender, date="Tue, 29 Sep 2026 23:30:00 -0700"):
    fields = [["From", sender], ["To", "organizer@campaign.example.invalid"],
              ["Subject", "Synthetic secret subject"]] + ([["Date", date]] if date else [])
    return json.dumps({"schema": "verified-eml-headers-v1", "fields": fields})


class FailureManifestTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="t13-manifest-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        eml, pdf, ok, parent = "a" * 64, "b" * 64, "c" * 64, "d" * 64
        documents = [{"original_sha256": eml, "form": None, "outcome": "CORRUPT_DOCUMENT"},
                     {"original_sha256": pdf, "form": "pdf", "outcome": "EXTRACT_ERROR"},
                     {"original_sha256": ok, "form": "pdf", "outcome": "EXTRACTED"},
                     {"original_sha256": "e" * 64, "form": "pdf", "outcome": "AWAITING_OCR"}]
        mails = [("mail-1", "acct", "Records/Agency", 900, 31, "<m1@portal.example.invalid>", eml,
                  headers("Clerk <clerk@nextrequest.com>"), None, "x"),
                 ("mail-2", "acct", "INBOX", 900, 32, None, parent,
                  headers("person@gmail.com", date=None), "2026-09-30T01:02:03+00:00", "x")]
        evidence = lambda receipt: json.dumps({"receipt_sha256": receipt, "export_receipt_path": "/x"})
        occurrences = [("o-eml", eml, "mail", "{}", None, None, "verified_mail_export", evidence("1" * 64)),
                       ("o-parent", parent, "mail", "{}", None, None, "verified_mail_export", evidence("2" * 64)),
                       ("o-pdf", pdf, "attachment", "{}", "o-parent", None, "verified_mail_export",
                        evidence("2" * 64))]
        self.project = make_manifest_project(self.base, documents, mails, occurrences)

    def test_itemizes_each_failed_document_of_the_newest_run(self):
        result = manifest.itemize(self.project)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["by_code"], {"CORRUPT_DOCUMENT": 1, "EXTRACT_ERROR": 1})
        self.assertEqual(result["documents_listed"], 4)
        self.assertEqual(result["extract_list_sha256"],
                         sha256_file(self.project / "processing-v1" / "runs" / "extract-run_new.json"))
        eml, pdf = result["documents"]
        self.assertEqual((eml["code"], eml["original_sha256"], eml["occurrence_kinds"]),
                         ("CORRUPT_DOCUMENT", "a" * 64, ["mail"]))
        self.assertEqual(eml["mail"], {
            "via": "self", "mail_message_id": "mail-1", "uidvalidity": 900, "uid": 31,
            "folder_sha256": hashlib.sha256(b"Records/Agency").hexdigest(),
            "message_id_sha256": hashlib.sha256(b"<m1@portal.example.invalid>").hexdigest(),
            "receipt_sha256": "1" * 64, "date_utc": "2026-09-30", "received_at_date": None,
            "sender_domain_class": "records_portal"})
        # An attachment is itemized by the mail it came from.
        self.assertEqual((pdf["code"], pdf["form"], pdf["mail"]["via"], pdf["mail"]["uid"]),
                         ("EXTRACT_ERROR", "pdf", "parent_mail", 32))
        self.assertEqual((pdf["mail"]["date_utc"], pdf["mail"]["received_at_date"],
                          pdf["mail"]["message_id_sha256"], pdf["mail"]["sender_domain_class"]),
                         (None, "2026-09-30", None, "webmail"))

    def test_no_address_domain_folder_or_text_leaves_the_ledger(self):
        text = json.dumps(manifest.itemize(self.project))
        for needle in ("clerk", "nextrequest", "gmail", "person", "Records/Agency", "INBOX", "Synthetic",
                       "portal.example", "campaign.example", "acct"):
            self.assertNotIn(needle, text)

    def test_sender_domain_classes_are_fixed(self):
        cases = {"a@x.ca.gov": "government", "a@co.slo.ca.us": "government", "a@city.gov.uk": "government",
                 "a@flocksafety.com": "flock_vendor", "a@mail.flocksafety.com": "flock_vendor",
                 "a@x.govqa.us": "records_portal", "a@yahoo.com": "webmail", "a@example.org": "organization",
                 "a@vendor.com": "commercial", "a@x.invalid": "other", "not an address": "unparseable",
                 "a@localhost": "unparseable", None: "absent"}
        for value, expected in cases.items():
            with self.subTest(value):
                self.assertEqual(manifest.sender_class(value), expected)
                self.assertIn(expected, manifest.SENDER_CLASSES)

    def test_failure_codes_match_the_engine(self):
        from campaign_tool.records import native_extract
        self.assertEqual(set(manifest.FAILED), native_extract.FAILED)

    def test_refuses_without_an_extract_list(self):
        for path in (self.project / "processing-v1" / "runs").iterdir():
            path.unlink()
        with self.assertRaises(manifest.Refused) as caught:
            manifest.itemize(self.project)
        self.assertEqual(str(caught.exception), "extract_list_missing")


class LegacyReceiptFormTests(unittest.TestCase):
    """Pre-T4 identity: export_receipt_path is the intake verification copy (no objects beside it)."""

    def setUp(self):
        os.umask(0o077)
        temp = tempfile.TemporaryDirectory(prefix="t13-forms-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()

    def test_attachment_forms_from_heads_without_objects(self):
        mail = self.base / "mail-history"
        (mail / "receipts").mkdir(parents=True, mode=0o700)
        pdf = tiny_pdf()
        write_receipt(mail, 41, body="Synthetic.\n", attachments=[("letter.pdf", pdf)])
        receipt = next((mail / "receipts").glob("*.json"))
        copy = self.base / "intake" / "verification-receipts" / sha256_file(receipt)
        copy.parent.mkdir(parents=True, mode=0o700)
        shutil.copy2(receipt, copy)
        pdf_sha = hashlib.sha256(pdf).hexdigest()
        self.assertEqual(eml_export.attachment_forms(receipt)[pdf_sha], "pdf")
        with self.assertRaises(OSError):
            eml_export.attachment_forms(copy)  # what EXTRACT_ERROR came from
        self.assertEqual(eml_export.attachment_forms(copy, heads={pdf_sha: pdf}), {pdf_sha: "pdf"})
        self.assertEqual(eml_export.attachment_forms(copy, heads={"0" * 64: pdf}), {})

    def test_pipeline_form_of_a_pre_t4_attachment(self):
        project = make_project(self.base)
        adopt(project)
        pipeline = Pipeline(project / "processing-v1")
        ledger = pipeline.root.ledger
        rows = query(ledger, "SELECT id, original_sha256, evidence FROM occurrences WHERE kind='attachment' "
                             "AND acquisition_method='verified_mail_export'")
        pdf_rows = [row for row in rows if pipeline._form(row[1], pipeline._original(row[1])) == "pdf"]
        self.assertTrue(pdf_rows)
        oid, subject, evidence = pdf_rows[0]
        evidence = json.loads(evidence)
        evidence["export_receipt_path"] = evidence["verification_receipt_path"]
        self.assertFalse((Path(evidence["export_receipt_path"]).parent.parent / "objects").exists())
        con = sqlite3.connect(ledger)
        with con:
            con.execute("UPDATE occurrences SET evidence=? WHERE id=?", (json.dumps(evidence), oid))
        con.close()
        self.assertEqual(pipeline._form(subject, pipeline._original(subject)), "pdf")


if __name__ == "__main__":
    unittest.main()
