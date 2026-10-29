"""FA1403 synthetic regressions: ambiguous serialized canonical occurrence evidence.

The adoption audit (``identity_conflict`` via ``classify_receipts``) decodes the
stored ``verified_mail_export`` occurrence evidence. A plain JSON decode keeps
the LAST of duplicate object members, so raw evidence holding a wrong then a
correct ``cas_sha256`` (or any other member) used to compare equal and the
identity was counted as adopted. Every duplicate member, at any depth and in
either value order (even with equal values), must instead classify the identity
as one ``identity_evidence_unreadable`` conflict in dry-run and apply, with no
enrollment, CAS or ledger mutation, while a clean peer identity stays complete.

Each case enrolls genuinely through the real command and backend first, then
rewrites exactly one stored evidence text. Everything is synthetic: generated
receipts/bytes in a fresh temporary root, no private inputs, no CLI child, no
network.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from campaign_tool.records import mail_adoption
from campaign_tool.records.intake import eml_export
from campaign_tool.records.run import Pipeline
from tests.records import native_msg_fixtures as native
from tests.records.test_mail_adoption_regression import _write_private, synthetic_receipt
from tests.records.test_mail_wire_intake import binary, chain, leaf, mixed

UNREADABLE = "identity_evidence_unreadable"
ACCOUNT, MAILBOX, VALIDITY = "synthetic-adopt-account", "Adoption", 900
ORDERS = ("wrong_then_correct", "correct_then_wrong", "equal_values")


def flip_hex(value):
    """Same-length different lowercase hex digest (computed, never a literal)."""
    return value[:-1] + ("0" if value[-1] != "0" else "1")


WRONG = {
    "cas_sha256": flip_hex,
    "receipt_sha256": flip_hex,
    "bytes": lambda value: value + 1,
    "export_receipt_path": lambda value: value + ".other",
    "verification_receipt_path": lambda value: str(Path(value).with_name(flip_hex(Path(value).name))),
    "native_item": lambda value: dict(value, representation="synthetic_drift"),
}


class Raw(str):
    """Pre-serialized JSON text inserted verbatim."""


def encode(value):
    return str(value) if isinstance(value, Raw) else json.dumps(value, sort_keys=True, separators=(",", ":"))


def raw_object(pairs):
    """Serialize (key, value) pairs in order, duplicates preserved."""
    return Raw("{" + ",".join(json.dumps(key) + ":" + encode(value) for key, value in pairs) + "}")


def duplicated(mapping, key, order, wrong=None):
    """``mapping`` serialized with ``key`` present twice in the requested value order."""
    correct = mapping[key]
    bad = (wrong or WRONG[key])(correct)
    values = {"wrong_then_correct": (bad, correct), "correct_then_wrong": (correct, bad),
              "equal_values": (correct, correct)}[order]
    pairs = []
    for name in sorted(mapping):
        pairs.extend((name, value) for value in (values if name == key else (mapping[name],)))
    return raw_object(pairs)


def tree(base):
    """Every path under ``base`` with mode, size, mtime_ns and content hash."""
    result = {}
    for parent, dirs, files in os.walk(base):
        for name in dirs + files:
            path = Path(parent) / name
            info = path.lstat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            result[str(path.relative_to(base))] = (info.st_mode, info.st_size, info.st_mtime_ns, digest)
    return result


def ledger_dump(ledger):
    """Complete logical ledger content, read without sidecar side effects."""
    con = sqlite3.connect(Path(ledger).as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        return list(con.iterdump())
    finally:
        con.close()


class _Enrolled(unittest.TestCase):
    """Two identities (uid 11 under test, uid 12 the clean peer), genuinely adopted."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.root = Pipeline(self.base / "processing").root.path
        self.ledger = self.root / "ledger.sqlite"
        self.export = self.base / "mail-history"
        self.export.mkdir(mode=0o700)
        self.export_uid11()
        synthetic_receipt(self.export, uid=12, attachments=2)
        self.cfg = self.base / "config.json"
        _write_private(self.cfg, json.dumps({"schema": mail_adoption.CONFIG_SCHEMA, "source": "mail-history",
                                             "adoption": "enabled", "disable_marker_sha256": None}).encode())
        first = self.adopt(apply=True)
        self.assertEqual((first["adopted_messages"], first["failures"]), (2, {}), first)
        self.assert_clean()

    def export_uid11(self):
        synthetic_receipt(self.export, uid=11, attachments=1)

    def adopt(self, **kw):
        return mail_adoption.adopt(self.root, self.export, config_path=self.cfg, **kw)

    # -- ledger access ---------------------------------------------------------
    def rows(self, uid=11, kind=None):
        con = sqlite3.connect(self.ledger.as_uri() + "?mode=ro&immutable=1", uri=True)
        con.row_factory = sqlite3.Row
        try:
            found = [dict(row) for row in con.execute("SELECT id,kind,source_ref,evidence FROM occurrences "
                                                      "ORDER BY kind,id")
                     if json.loads(row["source_ref"])["uid"] == uid and kind in (None, row["kind"])]
        finally:
            con.close()
        self.assertTrue(found, "fixture occurrence missing")
        return found

    def set_evidence(self, oid, text):
        con = sqlite3.connect(self.ledger)
        try:
            self.assertEqual(con.execute("UPDATE occurrences SET evidence=? WHERE id=?", (text, oid)).rowcount, 1)
            con.commit()
        finally:
            con.close()
        self.assertFalse(Path(str(self.ledger) + "-wal").exists())

    def rewrite(self, row, text):
        """Store ``text`` as ``row``'s evidence; restore the original bytes afterwards."""
        self.set_evidence(row["id"], text)
        return lambda: self.set_evidence(row["id"], row["evidence"])

    # -- assertions ------------------------------------------------------------
    def snapshot(self):
        return ledger_dump(self.ledger), tree(self.base)

    def assert_clean(self):
        report = self.adopt()
        self.assertEqual((report["already_adopted"], report["identity_conflicts"], report["pending_messages"]),
                         (2, {}, 0), report)
        self.assertFalse(mail_adoption.blocking(report))

    def assert_unreadable_and_inert(self, repeats=1):
        """Dry-run and apply: one unreadable conflict, peer complete, nothing written."""
        before = self.snapshot()
        for _ in range(repeats):
            for apply in (False, True):
                report = self.adopt(apply=apply)
                self.assertEqual(report["identity_conflicts"], {UNREADABLE: 1}, report)
                self.assertEqual((report["already_adopted"], report["new_messages"], report["incomplete_messages"],
                                  report["pending_messages"]), (1, 0, 0, 0), report)
                self.assertEqual((report["adopted_messages"], report["resumed_messages"],
                                  report["adopted_attachments"], report["failures"]), (0, 0, 0, {}), report)
                self.assertTrue(mail_adoption.blocking(report))
                if apply:
                    self.assertNotIn("run_id", report)
                    self.assertEqual(report["after"]["identity_conflicts"], {UNREADABLE: 1})
                    self.assertEqual(report["after"]["already_adopted"], 1)
                # No enrollment, CAS, verification-receipt, export or ledger change.
                self.assertEqual(self.snapshot(), before)

    def check_duplicate(self, row, text, key, repeats=1):
        self.assertEqual(text.count(json.dumps(key) + ":"), 2, "fixture must hold a raw duplicate member")
        restore = self.rewrite(row, text)
        try:
            self.assert_unreadable_and_inert(repeats)
        finally:
            restore()
        self.assert_clean()

    def check_member(self, key, kind="attachment"):
        row = self.rows(kind=kind)[0]
        evidence = json.loads(row["evidence"])
        for order in ORDERS:
            with self.subTest(key=key, order=order, kind=kind):
                self.check_duplicate(row, duplicated(evidence, key, order), key)


class StrictEvidenceDecoderTests(unittest.TestCase):
    """The private strict decoder itself (A2): duplicates refused at every depth."""

    def test_duplicate_members_rejected_at_every_depth(self):
        cases = {
            "top": '{"a":1,"b":2,"a":1}',
            "top_wrong_then_correct": '{"cas_sha256":"x","cas_sha256":"y"}',
            "depth2": '{"native_item":{"part":"1","part":"2"}}',
            "depth3": '{"n":{"m":{"k":1,"k":1}}}',
            "object_in_array": '{"list":[1,{"k":true,"k":false}]}',
            "nested_arrays": '[[{"k":null,"k":null}]]',
            "escaped_key": '{"cas' + "\\u005f" + 'sha256":"x","cas_sha256":"x"}',
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                self.assertIsNotNone(json.loads(text))   # the plain decoder silently accepts it
                with self.assertRaises(ValueError):
                    mail_adoption._strict_evidence(text)

    def test_valid_evidence_and_repeated_array_values_decode_unchanged(self):
        for text in ('{"a":[1,1,"x","x",{"k":1},{"k":1}],"b":{"k":[[],[]]}}',
                     '{"ole_storage_locator":["s","s"],"part":"1.2"}', '{}', '[]', '{"k":{"k":{"k":1}}}'):
            with self.subTest(text=text):
                self.assertEqual(mail_adoption._strict_evidence(text), json.loads(text))

    def test_excessive_nesting_is_a_value_error(self):
        text = '{"k":' * 200000 + "1" + "}" * 200000
        with self.assertRaises(ValueError):
            mail_adoption._strict_evidence(text)


class LegacyEvidenceDuplicateTests(_Enrolled):
    """A1/A3 on legacy receipts: duplicate members of every evidence field."""

    def test_duplicate_cas_sha256_is_unreadable(self):
        self.check_member("cas_sha256")

    def test_duplicate_receipt_sha256_is_unreadable(self):
        self.check_member("receipt_sha256")

    def test_duplicate_bytes_is_unreadable(self):
        self.check_member("bytes")

    def test_duplicate_verification_receipt_path_is_unreadable(self):
        self.check_member("verification_receipt_path")

    def test_duplicate_export_receipt_path_is_unreadable(self):
        self.check_member("export_receipt_path")

    def test_duplicate_member_on_mail_occurrence_is_unreadable(self):
        self.check_member("cas_sha256", kind="mail")

    def test_escaped_duplicate_member_name_is_unreadable(self):
        row = self.rows(kind="attachment")[0]
        evidence = json.loads(row["evidence"])
        pairs = [("cas" + "\\u005f" + "sha256", flip_hex(evidence["cas_sha256"]))]
        text = Raw("{" + ",".join(['"%s":%s' % (pairs[0][0], encode(pairs[0][1]))]
                                  + [json.dumps(k) + ":" + encode(v) for k, v in sorted(evidence.items())]) + "}")
        self.assertEqual(json.loads(text), evidence)   # last-wins decode looks clean
        restore = self.rewrite(row, text)
        try:
            self.assert_unreadable_and_inert()
        finally:
            restore()
        self.assert_clean()

    def test_repeated_classification_keeps_clean_peer_complete_and_inputs_unchanged(self):
        row = self.rows(kind="attachment")[0]
        text = duplicated(json.loads(row["evidence"]), "cas_sha256", "wrong_then_correct")
        self.check_duplicate(row, text, "cas_sha256", repeats=3)

    def test_undecodable_and_overdeep_evidence_are_unreadable_without_exception(self):
        row = self.rows(kind="attachment")[0]
        for name, text in (("truncated", row["evidence"][:-1]),
                           ("overdeep", '{"k":' * 200000 + "1" + "}" * 200000)):
            with self.subTest(case=name):
                restore = self.rewrite(row, text)
                try:
                    self.assert_unreadable_and_inert()
                finally:
                    restore()
                self.assert_clean()

    def test_clean_evidence_stays_complete_without_mutation(self):
        before = self.snapshot()
        for apply in (False, True):
            report = self.adopt(apply=apply)
            self.assertEqual((report["already_adopted"], report["identity_conflicts"], report["pending_messages"]),
                             (2, {}, 0))
            self.assertFalse(mail_adoption.blocking(report))
        self.assertEqual(self.snapshot(), before)

    def test_single_wrong_cas_keeps_evidence_mismatch_code(self):
        row = self.rows(kind="attachment")[0]
        evidence = json.loads(row["evidence"])
        evidence["cas_sha256"] = flip_hex(evidence["cas_sha256"])
        restore = self.rewrite(row, json.dumps(evidence, sort_keys=True, separators=(",", ":")))
        try:
            for apply in (False, True):
                report = self.adopt(apply=apply)
                self.assertEqual(report["identity_conflicts"], {"identity_evidence_mismatch": 1})
                self.assertEqual(report["already_adopted"], 1)
        finally:
            restore()
        self.assert_clean()


class NativeItemEvidenceDuplicateTests(_Enrolled):
    """A1/A2 on an exact-wire receipt with a native MSG item (uid 11)."""

    def export_uid11(self):
        _, scalar, _, _ = chain()
        msg = native.message(embedded=True, attachment=scalar)
        raw = mixed([leaf(), binary(msg, b"outer.msg", b"application/vnd.ms-outlook")])
        path = eml_export.export_message(raw, mail_root=self.export, account=ACCOUNT, mailbox=MAILBOX,
                                         uidvalidity=VALIDITY, uid=11)
        target = self.export / "receipts" / "wire-11.json"
        eml_export._ensure_private(target, Path(path).read_bytes())

    def native_row(self):
        found = [row for row in self.rows(kind="attachment") if "native_item" in json.loads(row["evidence"])]
        self.assertEqual(len(found), 1)
        return found[0]

    def test_duplicate_native_item_member_is_unreadable(self):
        row = self.native_row()
        evidence = json.loads(row["evidence"])
        for order in ORDERS:
            with self.subTest(order=order):
                self.check_duplicate(row, duplicated(evidence, "native_item", order), "native_item")

    def test_duplicate_member_inside_native_item_is_unreadable(self):
        row = self.native_row()
        evidence = json.loads(row["evidence"])
        for order in ORDERS:
            with self.subTest(order=order):
                inner = duplicated(evidence["native_item"], "representation", order,
                                   wrong=lambda value: "synthetic_drift")
                text = raw_object(sorted(dict(evidence, native_item=inner).items()))
                self.check_duplicate(row, text, "representation")

    def test_native_item_single_drift_keeps_evidence_mismatch_code(self):
        row = self.native_row()
        evidence = json.loads(row["evidence"])
        evidence["native_item"]["representation"] = "synthetic_drift"
        restore = self.rewrite(row, json.dumps(evidence, sort_keys=True, separators=(",", ":")))
        try:
            report = self.adopt()
            self.assertEqual(report["identity_conflicts"], {"identity_evidence_mismatch": 1})
        finally:
            restore()
        self.assert_clean()

    def test_native_receipt_with_array_values_stays_complete(self):
        item = json.loads(self.native_row()["evidence"])["native_item"]
        self.assertIsInstance(item["ole_storage_locator"], list)
        self.assert_clean()


del _Enrolled

if __name__ == "__main__":
    unittest.main()
