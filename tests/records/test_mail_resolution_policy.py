"""Contract A: synthetic owner artifacts, no real policies or records."""
import copy
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records.ledger import store
from campaign_tool.records.ledger import mail_resolution_policy as policy


def value(subject="a" * 64, size=18, receipt="b" * 64, occurrence="c" * 64, legacy="d" * 64):
    return {"schema": "approved-mail-resolution-policy-v1", "inventory_sha256": "e" * 64,
            "approval_sha256": "f" * 64, "originals": {subject: {
                "bytes": size, "role": "original", "scope": "in_scope",
                "verification_receipt_sha256": receipt, "binding_occurrence_id": occurrence,
                "dependent_receipts": [], "legacy_binding_sha256": legacy}}, "held": []}


def approved(v):
    raw = store.canonical(v).encode()
    return policy.ApprovedMailResolutionPolicy(raw, expected_sha256=hashlib.sha256(raw).hexdigest())


class MailResolutionPolicyTests(unittest.TestCase):
    def test_valid_canonical_hash_pinned_and_copy_isolation(self):
        v = value()
        p = approved(v)
        self.assertEqual(p.sha256, hashlib.sha256(p.raw).hexdigest())
        item = p.admission("a" * 64, 18)
        item["dependent_receipts"].append("0" * 64)
        self.assertEqual(p.admission("a" * 64, 18), v["originals"]["a" * 64])
        for subject, size in (("0" * 64, 18), ("a" * 64, 19), ("a" * 64, True)):
            with self.assertRaisesRegex(ValueError, "resolution_policy_invalid"):
                p.admission(subject, size)

    def test_hash_mismatch(self):
        with self.assertRaisesRegex(ValueError, "resolution_policy_mismatch"):
            policy.ApprovedMailResolutionPolicy(store.canonical(value()).encode(), expected_sha256="0" * 64)

    def test_invalid_schema_and_every_entry_field(self):
        mutations = [lambda v: v.update(extra=1), lambda v: v.pop("held"),
                     lambda v: v.update(schema="other"), lambda v: v.update(inventory_sha256="A" * 64),
                     lambda v: v.update(approval_sha256="bad"), lambda v: v.update(originals=[]),
                     lambda v: v.update(held=["a" * 64]), lambda v: v.update(held=["2" * 64, "1" * 64]),
                     lambda v: v.update(held=["0" * 64] * 2), lambda v: v.update(held=["F" * 64])]
        bad_fields = {"bytes": [True, -1, 1.5, "18"], "role": ["unresolved"], "scope": ["out_of_scope"],
                      "verification_receipt_sha256": ["B" * 64], "binding_occurrence_id": ["mail:123", "", "c" * 63],
                      "legacy_binding_sha256": [None], "dependent_receipts": [["2" * 64, "1" * 64],
                      ["0" * 64] * 2, ["b" * 64], ["G" * 64], "bad"]}
        for field, invalids in bad_fields.items():
            for invalid in invalids:
                mutations.append(lambda v, field=field, invalid=invalid: v["originals"]["a" * 64].update({field: invalid}))
        mutations.extend([lambda v: v["originals"]["a" * 64].pop("role"),
                          lambda v: v["originals"]["a" * 64].update(extra=1),
                          lambda v: v["originals"].update({"A" * 64: v["originals"].pop("a" * 64)})])
        for mutate in mutations:
            v = copy.deepcopy(value()); mutate(v)
            with self.subTest(v=v), self.assertRaisesRegex(ValueError, "resolution_policy_invalid"):
                approved(v)

    def test_duplicate_keys_nonfinite_malformed_and_noncanonical(self):
        raw = store.canonical(value()).encode()
        cases = [raw.replace(b'"bytes":18', b'"bytes":18,"bytes":18'),
                 raw.replace(b'"bytes":18', b'"bytes":NaN'),
                 raw.replace(b'"bytes":18', b'"bytes":Infinity'), b'{} garbage', b'\xff', b' ' + raw]
        for bad in cases:
            with self.subTest(raw=bad), self.assertRaisesRegex(ValueError, "resolution_policy_invalid"):
                policy.ApprovedMailResolutionPolicy(bad, expected_sha256=hashlib.sha256(bad).hexdigest())

    def test_actual_limits(self):
        v = value(); v["held"] = [format(i, "064x") for i in range(10000)]
        with self.assertRaisesRegex(ValueError, "resolution_policy_invalid"):
            approved(v)
        v = value(); v["originals"]["a" * 64]["dependent_receipts"] = [format(i, "064x") for i in range(10001)]
        with self.assertRaisesRegex(ValueError, "resolution_policy_invalid"):
            approved(v)
        raw = b" " * (policy.MAX_POLICY_BYTES + 1)
        with self.assertRaisesRegex(ValueError, "resolution_policy_invalid"):
            policy.ApprovedMailResolutionPolicy(raw, expected_sha256=hashlib.sha256(raw).hexdigest())
        v = value(); v["originals"] = {}; v["held"] = [format(i, "064x") for i in range(10000)]
        self.assertEqual(len(approved(v).raw), len(store.canonical(v).encode()))

    def test_file_loader_owner_regular_permissions_and_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); directory = root / "resolution-policies"; directory.mkdir()
            p = approved(value()); path = directory / p.sha256
            path.write_bytes(p.raw); path.chmod(0o600)
            self.assertEqual(policy.ApprovedMailResolutionPolicy.load(root, expected_sha256=p.sha256).raw, p.raw)
            for mode in (0o640, 0o604):
                path.chmod(mode)
                with self.assertRaisesRegex(ValueError, "resolution_policy_invalid"):
                    policy.ApprovedMailResolutionPolicy.load(root, expected_sha256=p.sha256)
            path.chmod(0o600)
            with patch.object(policy.os, "getuid", return_value=os.getuid() + 1):
                with self.assertRaisesRegex(ValueError, "resolution_policy_invalid"):
                    policy.ApprovedMailResolutionPolicy.load(root, expected_sha256=p.sha256)
            target = root / "valid-policy"
            target.write_bytes(p.raw); target.chmod(0o600)
            path.unlink(); path.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "resolution_policy_invalid"):
                policy.ApprovedMailResolutionPolicy.load(root, expected_sha256=p.sha256)
            path.unlink(); path.mkdir()
            with self.assertRaisesRegex(ValueError, "resolution_policy_invalid"):
                policy.ApprovedMailResolutionPolicy.load(root, expected_sha256=p.sha256)
            path.rmdir(); os.mkfifo(path)
            with self.assertRaisesRegex(ValueError, "resolution_policy_invalid"):
                policy.ApprovedMailResolutionPolicy.load(root, expected_sha256=p.sha256)
            path.unlink(); directory.rmdir()
            target_dir = root / "valid-policies"; target_dir.mkdir()
            (target_dir / p.sha256).write_bytes(p.raw); (target_dir / p.sha256).chmod(0o600)
            directory.symlink_to(target_dir, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "resolution_policy_invalid"):
                policy.ApprovedMailResolutionPolicy.load(root, expected_sha256=p.sha256)
