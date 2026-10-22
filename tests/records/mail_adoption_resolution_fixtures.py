"""Synthetic fixtures for T4B slice 3 (adopt-mail resolution plan, census, acceptance).

Fictional EML and attachment bytes, a fictional legacy import, fictional policy files
written under a temporary records root. No mailbox, no network, no real record.
"""
import hashlib
import json
import os
from pathlib import Path
import types

from campaign_tool.records import mail_adoption
from campaign_tool.records.intake import eml_export, mail_delta
from campaign_tool.records.ledger import store
from tests.records.mail_resolution_fixtures import policy_bytes, sha
from tests.records.test_canonical_mail_resolution import ACCOUNT, BackendResolutionCase, FOLDER, VALIDITY
from tests.records.test_mail_adoption import write_private
from tests.records.test_mail_adoption_coded_gaps import rfc822_receipt
from tests.records.test_mail_adoption_regression import _blob


def pdf(label):
    return ("%%PDF-1.4\nSynthetic resolution fixture %s\n%%%%EOF\n" % label).encode()


class ResolutionAdoptionCase(BackendResolutionCase):
    """A records root + frozen export + legacy-imported ledger + pinned-policy config."""

    def setUp(self):
        super().setUp()
        self.cfg = self.base / "adoption.json"
        self.write_config(None)

    # -- receipts --------------------------------------------------------------
    def mk(self, uid, labels=()):
        path, receipt, digest = self.custom_receipt(uid, [pdf(label) for label in labels])
        return types.SimpleNamespace(path=path, receipt=receipt, digest=digest, uid=uid, eml=receipt["original_eml"],
                                     atts=receipt["attachments"])

    def mk_rfc822(self, uid, labels=()):
        """A receipt the legacy MIME check refuses (``unsupported_rfc822_part``), with attachments."""
        path, receipt = rfc822_receipt(self.export, uid=uid)
        if labels:
            for index, label in enumerate(labels, start=2):
                item = _blob(self.export, pdf(label))
                item.update(part=index, filename="part-%d.pdf" % index, original_filename="part-%d.pdf" % index,
                            content_type="application/pdf")
                receipt["attachments"].append(item)
            os.unlink(path)
            eml_export._ensure_private(path, (json.dumps(receipt, sort_keys=True) + "\n").encode())
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return types.SimpleNamespace(path=path, receipt=receipt, digest=digest, uid=uid, eml=receipt["original_eml"],
                                     atts=receipt["attachments"])

    def declare_legacy(self, *originals, eml=()):
        """Declare originals (an ``{sha, bytes}`` mapping each) and receipt EMLs as unresolved legacy rows."""
        for r in eml:
            self.declare(r.eml["bytes"], r.eml["sha256"], "eml", r.uid)
        for item in originals:
            self.declare(item["bytes"], item["sha256"], "pdf")

    def occ(self, r, digest):
        """Sorted occurrence ids the backend will write for `digest` in receipt `r`."""
        items, _ = mail_adoption.discover(self.export, self.root / "intake")
        item = next(i for i in items if i["receipt_sha256"] == r.digest)
        return mail_adoption.receipt_occurrences(item, self.export, self.root / "intake")[digest]

    def entry_for(self, item, binding, oid, dependents=()):
        return self.entry(item["sha256"], item["bytes"], binding.digest, oid, [d.digest for d in dependents])

    # -- policy and config ------------------------------------------------------
    def write_policy(self, originals, held=()):
        raw = policy_bytes(originals, held)
        digest = sha(raw)
        folder = self.root / "resolution-policies"
        folder.mkdir(mode=0o700, exist_ok=True)
        path = folder / digest
        if not path.exists():
            write_private(path, raw)
        return digest

    def write_config(self, policy_sha, *, v2=True):
        config = {"schema": mail_adoption.CONFIG_SCHEMA_V2 if v2 else mail_adoption.CONFIG_SCHEMA,
                  "source": "mail-history", "adoption": "enabled", "disable_marker_sha256": None}
        if v2:
            config["legacy_resolution_policy_sha256"] = policy_sha
        if self.cfg.exists():
            self.cfg.unlink()
        write_private(self.cfg, json.dumps(config).encode())

    def pin(self, originals, held=()):
        digest = self.write_policy(originals, held)
        self.write_config(digest)
        return digest

    def adopt(self, apply=False, **kw):
        return mail_adoption.adopt(self.root, self.export, config_path=self.cfg, apply=apply, **kw)

    # -- observation ------------------------------------------------------------
    def table(self, name):
        return self.rows("SELECT * FROM %s ORDER BY 1,2" % name)

    def state(self):
        """Path, mode, size, mtime and hash of everything under the root (ledger, catalog, CAS, sidecars)."""
        from tests.records.test_mail_adoption_recovery import tree
        return tree(self.root)

    def resolution_rows(self):
        return self.rows("SELECT * FROM original_resolutions ORDER BY original_sha256") \
            if self.rows("SELECT 1 FROM sqlite_master WHERE name='original_resolutions'") else []

    def invariants(self, report):
        self.assertEqual(report["new_messages"], report["eligible_new_messages"] + report["resolvable_messages"]
                         + report["coded_gap_messages"])
        self.assertEqual(report["receipts"], report["already_adopted"] + report["pending_messages"]
                         + report["coded_gap_messages"] + sum(report["identity_conflicts"].values()))
        self.assertEqual(report["policy_originals"], report["policy_originals_resolved"]
                         + report["resolvable_originals"] + report["policy_originals_blocked"])
        self.assertEqual(report["resolvable_messages"], report["resolvable_binding_messages"]
                         + report["resolvable_dependent_messages"])
