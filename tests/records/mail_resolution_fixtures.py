"""Synthetic fixtures for T4B slice A; fictional bytes, no mailbox, no network.

Builds a legacy-imported ledger with unresolved originals, a fictional intake output
(CAS blob + content-addressed verification receipt) and a fictional
`verified_mail_export` occurrence. Nothing here is a real record.
"""
import contextlib
import hashlib
import json
import os

from campaign_tool.records.intake.mail_resolution_policy import ApprovedMailResolutionPolicy
from campaign_tool.records.ledger import store
from campaign_tool.records.ledger.occurrence_projection import project_occurrences
from tests.records import test_ledger_occurrences as projection_fixtures


def sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def policy_bytes(originals, held=(), *, marker="fictional approval"):
    return json.dumps({"schema": "approved-mail-resolution-policy-v1",
                       "inventory_sha256": sha("fictional inventory"), "approval_sha256": sha(marker),
                       "originals": originals, "held": sorted(held)},
                      sort_keys=True, separators=(",", ":")).encode()


def build_policy(originals, held=(), **kwargs):
    raw = policy_bytes(originals, held, **kwargs)
    return ApprovedMailResolutionPolicy(raw, expected_sha256=sha(raw))


class MailResolutionCase:
    """Mixin: self.eml is a legacy `eml` original, self.att a legacy `txt` original."""

    def setUpFixture(self):
        self.helper = projection_fixtures.OccurrenceProjectionTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.eml_label = "synthetic message"
        self.eml = self.helper.mail(self.eml_label)
        self.att = self.helper.sha  # base fixture: bytes b"synthetic original", format txt
        self.att_label = "synthetic original"
        self.import_id = self.helper.capture()
        project_occurrences(self.helper.database, self.import_id)
        self.database = self.helper.database
        self.base = self.database.parent.resolve()
        self.output = self.base / "intake-out"
        for name in ("", "blobs", "verification-receipts"):
            (self.output / name).mkdir(mode=0o700, exist_ok=True)
        self.receipt = self.write_receipt(b'{"synthetic":"verification receipt"}')
        self.blob(self.eml, self.eml_label.encode())
        self.blob(self.att, self.att_label.encode())

    def blob(self, subject, raw):
        path = self.output / "blobs" / subject
        path.write_bytes(raw)
        path.chmod(0o400)

    def write_receipt(self, raw):
        digest = sha(raw)
        path = self.output / "verification-receipts" / digest
        path.write_bytes(raw)
        path.chmod(0o400)
        return digest

    def storage(self, subject):
        return str(self.output / "blobs" / subject)

    def rows(self, sql, args=()):
        with store.ledger(self.database, readonly=True) as con:
            return [tuple(row) for row in con.execute(sql, args)]

    def legacy_binding(self, subject):
        for payload_sha, identity in self.rows("SELECT payload_sha256,identity_json FROM legacy_rows "
                                               "WHERE source_table='docs'"):
            if json.loads(identity) == {"sha": subject}:
                return payload_sha
        raise AssertionError("fixture has no legacy docs row")

    def occurrence(self, oid, subject, kind, *, receipt=None, uid=7, mime_path=None):
        receipt = receipt or self.receipt
        source = {"account": "synthetic-account", "folder": "INBOX", "uidvalidity": 1, "uid": uid,
                  "mime_path": mime_path}
        evidence = {"receipt_sha256": receipt, "cas_sha256": subject,
                    "verification_receipt_path": str(self.output / "verification-receipts" / receipt)}
        with store.ledger(self.database) as con:
            con.execute("INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?)",
                        (oid, subject, kind, json.dumps(source, sort_keys=True), None, None,
                         "verified_mail_export", json.dumps(evidence, sort_keys=True)))
            con.commit()
        return oid

    def entry(self, subject, oid, *, receipt=None, dependents=(), size=None):
        return {"bytes": size if size is not None else len(self.eml_label if subject == self.eml else self.att_label),
                "role": "original", "scope": "in_scope",
                "verification_receipt_sha256": receipt or self.receipt, "binding_occurrence_id": oid,
                "dependent_receipts": sorted(dependents), "legacy_binding_sha256": self.legacy_binding(subject)}

    def kwargs(self, subject, oid, policy):
        return dict(subject=subject, policy=policy, verification_receipt_sha256=self.receipt,
                    occurrence_id=oid, storage_path=self.storage(subject))

    @contextlib.contextmanager
    def writer(self, *, commit=True):
        with store.ledger(self.database) as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                yield con
                con.commit() if commit else con.rollback()
            except BaseException:
                con.rollback()
                raise

    def snapshot(self):
        with store.ledger(self.database, readonly=True) as con:
            return list(con.iterdump())
