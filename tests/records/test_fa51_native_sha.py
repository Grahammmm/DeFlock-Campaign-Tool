"""FA51: native_items must declare an accepted source_original_sha256 digest.

Synthetic, local and offline. Every receipt is produced by the actual native
export path (tests.records.native_msg_fixtures + eml_export.export_message)
under a fresh temporary root; no received mail, records or ledgers are read.
Receipt content is never printed; failures report only coded reasons.

A1  producer positive and empty native_items through parse, load_receipt and
    mail_adoption.discover.
A2  malformed native digests become mail_delta.Rejected("invalid_sha256").
A3  discover counts the malformed receipt as a coded gap, keeps processing a
    valid peer and leaves both receipt files byte-identical.
A4  in-memory control: with only the native digest gate disabled, the
    malformed value is accepted and the discovery projection raises TypeError.
"""
import collections
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from campaign_tool.records import mail_adoption
from campaign_tool.records.intake import eml_export, mail_delta, mail_wire, native_receipt
from tests.records import native_msg_fixtures as native

NATIVE = native.available()
MISSING = object()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def malformed_values(valid):
    """(label, value) pairs; MISSING removes the key entirely."""
    return [
        ("missing", MISSING), ("null", None), ("list", [valid]), ("empty_list", []),
        ("object", {"sha256": valid}), ("bool_true", True), ("bool_false", False),
        ("int_zero", 0), ("int", 123), ("float", 1.5), ("empty", ""),
        ("uppercase", valid.upper()), ("short", valid[:63]), ("long", valid + "0"),
        ("nonhex", "g" + valid[1:]), ("padded", " " + valid[1:]), ("newline", valid + "\n"),
        ("prefixed", "sha256:" + valid),
    ]


def omit_native_digest_validation(item):
    """A4 baseline double: the native gate without its digest validation."""
    return item.get("source_original_sha256")


def write_private(path, raw):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)


class Fixture:
    """Fresh per-test root holding one actual producer-exported receipt."""

    def __init__(self, *, embedded=True):
        self.root = Path(tempfile.mkdtemp(prefix="fa51-")).resolve()
        self.root.chmod(0o700)
        self.mail = self.root / "mail"
        self.raw = native.message(embedded=embedded)
        self.path = eml_export.export_message(
            self.raw, mail_root=self.mail, account="synthetic", mailbox="INBOX",
            uidvalidity=1, uid=1, original_format="msg")
        self.bytes = Path(self.path).read_bytes()
        self.receipt = json.loads(self.bytes)

    def variant(self, value, index=0):
        receipt = copy.deepcopy(self.receipt)
        entry = receipt["native_items"][index]
        if value is MISSING:
            del entry["source_original_sha256"]
        else:
            entry["source_original_sha256"] = value
        return receipt

    def receipts_dir(self):
        directory = self.mail / "receipts"
        directory.mkdir(mode=0o700, exist_ok=True)
        return directory

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


@unittest.skipUnless(NATIVE, "optional native MSG parser unavailable")
class ProducerPositiveTests(unittest.TestCase):
    """A1: the actual producer receipt keeps its native digests and identities."""

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)

    def test_a1_producer_receipt_parses_with_exact_native_digest(self):
        receipt = self.fx.receipt
        self.assertEqual(receipt["schema"], mail_wire.SCHEMA)
        natives = receipt["native_items"]
        self.assertGreaterEqual(len(natives), 1)
        self.assertEqual({entry["source_original_sha256"] for entry in natives}, {sha(self.fx.raw)})
        parsed, identity, items = native_receipt.parse(copy.deepcopy(receipt))
        self.assertEqual(identity, ("synthetic", "INBOX", "1", "1"))
        self.assertEqual(parsed["native_items"], natives)
        # Derived native items stay out of the original/attachment item list.
        self.assertEqual(items[0]["part"], "0")
        self.assertEqual(items[0]["sha"], sha(self.fx.raw))
        self.assertEqual(items[0]["kind"], "msg")
        self.assertEqual(len(items), 1 + len(receipt["attachments"]))
        self.assertTrue({entry["part"] for entry in natives}.isdisjoint(item["part"] for item in items))

    def test_a1_load_receipt_and_discover_keep_native_digest_set(self):
        parsed, identity, items = mail_delta.load_receipt(self.fx.path)
        self.assertEqual(identity, ("synthetic", "INBOX", "1", "1"))
        self.assertEqual(parsed["native_items"], self.fx.receipt["native_items"])
        write_private(self.fx.receipts_dir() / "valid.json", self.fx.bytes)
        found, gaps = mail_adoption.discover(self.fx.mail, self.fx.root / "intake")
        self.assertEqual(gaps, collections.Counter())
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["identity"], identity)
        self.assertEqual(found[0]["native_shas"], frozenset({sha(self.fx.raw)}))
        self.assertEqual(found[0]["receipt_sha256"], sha(self.fx.bytes))
        self.assertEqual([item["part"] for item in found[0]["items"]], [item["part"] for item in items])

    def test_a1_empty_native_items_remain_supported(self):
        plain = Fixture(embedded=False)
        self.addCleanup(plain.close)
        self.assertEqual(plain.receipt["schema"], mail_wire.SCHEMA)
        self.assertEqual(plain.receipt["native_items"], [])
        _, identity, items = mail_delta.load_receipt(plain.path)
        self.assertEqual(identity, ("synthetic", "INBOX", "1", "1"))
        self.assertEqual(items[0]["sha"], sha(plain.raw))
        write_private(plain.receipts_dir() / "plain.json", plain.bytes)
        found, gaps = mail_adoption.discover(plain.mail, plain.root / "intake")
        self.assertEqual((len(found), gaps), (1, collections.Counter()))
        self.assertEqual(found[0]["native_shas"], frozenset())

    def test_a1_native_part_uniqueness_is_preserved(self):
        receipt = copy.deepcopy(self.fx.receipt)
        receipt["native_items"].append(copy.deepcopy(receipt["native_items"][0]))
        with self.assertRaises(mail_delta.Rejected) as caught:
            native_receipt.parse(receipt)
        self.assertEqual(str(caught.exception), "duplicate_or_invalid_part")


@unittest.skipUnless(NATIVE, "optional native MSG parser unavailable")
class MalformedDigestTests(unittest.TestCase):
    """A2: every malformed native digest is the exact coded rejection."""

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        self.valid = sha(self.fx.raw)

    def test_a2_parse_rejects_each_malformed_native_digest(self):
        for label, value in malformed_values(self.valid):
            for index in (0, len(self.fx.receipt["native_items"]) - 1):
                with self.subTest(value=label, index=index):
                    with self.assertRaises(mail_delta.Rejected) as caught:
                        native_receipt.parse(self.fx.variant(value, index))
                    self.assertEqual(str(caught.exception), "invalid_sha256")

    def test_a2_load_receipt_rejects_each_malformed_native_digest(self):
        directory = self.fx.root / "variants"
        directory.mkdir(mode=0o700)
        for number, (label, value) in enumerate(malformed_values(self.valid)):
            with self.subTest(value=label):
                path = directory / f"variant-{number}.json"
                write_private(path, json.dumps(self.fx.variant(value), sort_keys=True).encode())
                with self.assertRaises(mail_delta.Rejected) as caught:
                    mail_delta.load_receipt(path)
                self.assertEqual(str(caught.exception), "invalid_sha256")

    def test_a2_valid_digest_and_unrelated_fields_still_parse(self):
        # Holding every other field constant, the exact valid digest is accepted.
        parsed, _, _ = native_receipt.parse(self.fx.variant(self.valid))
        self.assertEqual(parsed["native_items"][0]["source_original_sha256"], self.valid)


@unittest.skipUnless(NATIVE, "optional native MSG parser unavailable")
class DiscoveryReplayTests(unittest.TestCase):
    """A3: actual load_receipt + discover over one malformed and one valid peer."""

    def run_discovery(self, value):
        fx = Fixture()  # fresh synthetic mail root per declaration
        self.addCleanup(fx.close)
        directory = fx.receipts_dir()
        bad = directory / "a-malformed.json"
        good = directory / "b-valid.json"
        bad_raw = json.dumps(fx.variant(value), sort_keys=True).encode()
        write_private(bad, bad_raw)
        write_private(good, fx.bytes)
        found, gaps = mail_adoption.discover(fx.mail, fx.root / "intake")
        return fx, found, gaps, (bad, bad_raw), (good, fx.bytes)

    def test_a3_discover_counts_gap_and_keeps_valid_peer(self):
        valid = sha(native.message(embedded=True))
        for label, value in (("list", [valid]), ("object", {"sha256": valid}), ("null", None),
                             ("missing", MISSING), ("uppercase", valid.upper())):
            with self.subTest(value=label):
                fx, found, gaps, (bad, bad_raw), (good, good_raw) = self.run_discovery(value)
                self.assertEqual(sha(fx.raw), valid)
                self.assertEqual(gaps, collections.Counter({"invalid_sha256": 1}))
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0]["path"].name, "b-valid.json")
                self.assertEqual(found[0]["identity"], ("synthetic", "INBOX", "1", "1"))
                self.assertEqual(found[0]["native_shas"], frozenset({valid}))
                self.assertEqual(sorted(path.name for path in bad.parent.iterdir()),
                                 ["a-malformed.json", "b-valid.json"])
                self.assertEqual(bad.read_bytes(), bad_raw)
                self.assertEqual(good.read_bytes(), good_raw)


@unittest.skipUnless(NATIVE, "optional native MSG parser unavailable")
class NegativeControlTests(unittest.TestCase):
    """A4: disabling only the native digest gate reproduces the defect."""

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        self.valid = sha(self.fx.raw)

    def test_a4_control_accepts_malformed_and_consumer_raises_type_error(self):
        with mock.patch.object(native_receipt, "_native_source_digest", omit_native_digest_validation):
            parsed, _, _ = native_receipt.parse(self.fx.variant([self.valid]))
            self.assertEqual(parsed["native_items"][0]["source_original_sha256"], [self.valid])
            directory = self.fx.receipts_dir()
            write_private(directory / "a-malformed.json",
                          json.dumps(self.fx.variant([self.valid]), sort_keys=True).encode())
            write_private(directory / "b-valid.json", self.fx.bytes)
            with self.assertRaises(TypeError):
                mail_adoption.discover(self.fx.mail, self.fx.root / "intake")
            accepted, _, _ = native_receipt.parse(self.fx.variant(self.valid))
            self.assertEqual(accepted["native_items"][0]["source_original_sha256"], self.valid)

    def test_a4_candidate_gate_is_the_deciding_check(self):
        with self.assertRaises(mail_delta.Rejected) as caught:
            native_receipt.parse(self.fx.variant([self.valid]))
        self.assertEqual(str(caught.exception), "invalid_sha256")


if __name__ == "__main__":
    unittest.main()
