"""Synthetic occurrence receipts: byte identity is not provider identity."""
import hashlib
import json
import os
import unittest
from unittest import mock

from campaign_tool.records import mail_poll
from campaign_tool.records.intake import mail_delta
from tests.records import test_mail_poll as fixtures


def no_mid(raw):
    return b"\n".join(line for line in raw.split(b"\n") if not line.startswith(b"Message-ID:"))


class ReceiptIdentityTest(unittest.TestCase):
    setUp = fixtures.MailPollTest.setUp
    tearDown = fixtures.MailPollTest.tearDown
    write_config = fixtures.MailPollTest.write_config
    run_poll = fixtures.MailPollTest.run_poll
    state = fixtures.MailPollTest.state
    receipts = fixtures.MailPollTest.receipts
    assert_line = fixtures.MailPollTest.assert_line

    def paths(self, raw, uid=2, validity=7):
        sha = hashlib.sha256(raw).hexdigest()
        pid = mail_poll.classify(raw, validity, uid)[0]
        root = self.store / "mail-poll" / "receipts"
        return root / (sha + ".json"), root / (sha + "-" + pid + ".json")

    def assert_handoffs(self, raw, identities):
        sha = hashlib.sha256(raw).hexdigest()
        receipts = [r for r in self.receipts() if r["sha256"] == sha]
        self.assertEqual({(r["uidvalidity"], r["uid"]) for r in receipts}, set(identities))
        self.assertEqual(len(receipts), len(identities))
        self.assertEqual(len({r["provider_id_sha256"] for r in receipts}), len(identities))
        root = self.store / "mail-poll" / "mail"
        for receipt in receipts:
            _, identity, _ = mail_delta.load_receipt(root / receipt["intake_receipt"])
            self.assertEqual(identity[-2:], (str(receipt["uidvalidity"]), str(receipt["uid"])))
        originals = list(root.rglob("message.eml"))
        self.assertEqual([p.read_bytes() for p in originals].count(raw), 1)

    def assert_replay_immutable(self, box):
        root = self.store / "mail-poll"
        before = fixtures.tree(root / "mail"), fixtures.tree(root / "receipts"), fixtures.tree(root / "quarantine")
        calls = len([c for c in box.calls if c[0] == "FETCH"])
        self.assertEqual(self.run_poll(box)[0], 0)
        self.assertEqual(before, (fixtures.tree(root / "mail"), fixtures.tree(root / "receipts"),
                                  fixtures.tree(root / "quarantine")))
        self.assertEqual(calls, len([c for c in box.calls if c[0] == "FETCH"]))

    def test_identical_no_mid_different_uids_in_one_pass(self):
        raw = no_mid(fixtures.message(1))
        box = fixtures.Box({1: raw, 2: raw, 3: fixtures.message(3)})
        code, line, err = self.run_poll(box)
        self.assertEqual(code, 0, err)
        self.assert_line(line, new=3)
        self.assertEqual(self.state()["cursor"], 3)
        self.assert_handoffs(raw, [(7, 1), (7, 2)])
        self.assertTrue(all(p.exists() for p in self.paths(raw)))
        self.assert_replay_immutable(box)

    def test_identical_no_mid_in_later_pass(self):
        raw = no_mid(fixtures.message(1))
        box = fixtures.Box({1: raw})
        self.assertEqual(self.run_poll(box)[0], 0)
        primary = fixtures.tree(self.store / "mail-poll" / "receipts")
        box.messages.update({2: raw, 3: fixtures.message(3)})
        code, line, err = self.run_poll(box)
        self.assertEqual(code, 0, err)
        self.assert_line(line, new=2, already=1)
        self.assertEqual(self.state()["cursor"], 3)
        self.assert_handoffs(raw, [(7, 1), (7, 2)])
        after = fixtures.tree(self.store / "mail-poll" / "receipts")
        self.assertTrue(all(after[k] == v for k, v in primary.items()))
        self.assert_replay_immutable(box)

    def test_no_mid_uidvalidity_reset_keeps_occurrence_handoffs(self):
        raw = no_mid(fixtures.message(1))
        box = fixtures.Box({1: raw})
        self.assertEqual(self.run_poll(box)[0], 0)
        first = self.paths(raw)[0]
        before = first.read_bytes(), first.stat().st_mtime_ns
        box.validity, box.messages = 8, {10: raw, 11: fixtures.message(3)}
        code, line, err = self.run_poll(box)
        self.assertEqual(code, 0, err)
        self.assert_line(line, new=2)
        self.assertEqual((self.state()["uidvalidity"], self.state()["cursor"]), (8, 11))
        self.assert_handoffs(raw, [(7, 1), (8, 10)])
        self.assertEqual(before, (first.read_bytes(), first.stat().st_mtime_ns))
        self.assert_replay_immutable(box)

    def test_crash_after_primary_or_alternate_receipt_reuses_exact_paths(self):
        raw = no_mid(fixtures.message(1))
        for count in (1, 2):
            with self.subTest(receipts=count):
                self.store = self.root / ("crash-%d" % count)
                box = fixtures.Box({u: raw for u in range(1, count + 1)})
                with mock.patch.object(mail_poll.folder, "atomic", side_effect=OSError("synthetic state failure")):
                    self.assertEqual(self.run_poll(box)[0], 1)
                self.assertFalse((self.store / "mail-poll" / "state.json").exists())
                before = fixtures.tree(self.store / "mail-poll")
                self.assertEqual(len(self.receipts()), count)
                code, _, err = self.run_poll(box)
                self.assertEqual(code, 0, err)
                after = fixtures.tree(self.store / "mail-poll")
                self.assertTrue(all(after[k] == v for k, v in before.items()))
                self.assert_handoffs(raw, [(7, u) for u in range(1, count + 1)])
                self.assertEqual(self.state()["cursor"], count)
                self.assert_replay_immutable(box)

    def test_receipt_corruption_is_not_a_new_occurrence(self):
        raw = no_mid(fixtures.message(1))
        cases = ("invalid-json", "not-object", "schema", "sha256", "provider-invalid", "provider-same",
                 "uid-bool", "validity-zero", "same-identity", "alternate-conflict", "symlink", "oversize", "fifo")
        for case in cases:
            with self.subTest(case=case):
                self.store = self.root / case
                box = fixtures.Box({1: raw})
                self.assertEqual(self.run_poll(box)[0], 0)
                primary, alternate = self.paths(raw)
                data = json.loads(primary.read_bytes())
                target = primary
                if case == "invalid-json":
                    primary.write_bytes(b"{")
                elif case == "not-object":
                    primary.write_text("[]")
                elif case in ("schema", "sha256", "provider-invalid", "provider-same", "uid-bool", "validity-zero", "same-identity"):
                    field, value = {"schema": ("schema", "invalid"), "sha256": ("sha256", "0" * 64),
                                    "provider-invalid": ("provider_id_sha256", "../invalid"),
                                    "provider-same": ("provider_id_sha256", mail_poll.classify(raw, 7, 2)[0]),
                                    "uid-bool": ("uid", True), "validity-zero": ("uidvalidity", 0),
                                    "same-identity": ("review", "changed")}[case]
                    data[field] = value
                    primary.write_text(json.dumps(data))
                elif case == "alternate-conflict":
                    alternate.write_bytes(b"planted conflict")
                    alternate.chmod(0o600)
                    target = alternate
                elif case == "symlink":
                    target = self.root / "symlink-target"
                    target.write_bytes(primary.read_bytes())
                    primary.unlink()
                    primary.symlink_to(target)
                elif case == "fifo":
                    primary.unlink()
                    os.mkfifo(primary, 0o600)
                else:
                    primary.write_bytes(b" " * (64 * 1024 + 1))
                before_state = (self.store / "mail-poll" / "state.json").read_bytes()
                before = None if case == "fifo" else (target.read_bytes(), target.stat().st_mtime_ns)
                # Same-identity corruption must refuse on crash replay, not route around it.
                if case == "same-identity":
                    state_path = self.store / "mail-poll" / "state.json"
                    state_path.unlink()
                else:
                    box.messages.update({2: raw, 3: fixtures.message(3)})
                code, line, err = self.run_poll(box)
                self.assertEqual((code, line), (1, ""))
                self.assertIn("refused: receipt_conflict", err)
                if case == "same-identity":
                    self.assertFalse(state_path.exists())
                else:
                    self.assertEqual((self.store / "mail-poll" / "state.json").read_bytes(), before_state)
                    self.assertNotIn(("FETCH", "3", "(BODY.PEEK[])"), box.calls)
                if before is not None:
                    self.assertEqual(before, (target.read_bytes(), target.stat().st_mtime_ns))
                if case != "alternate-conflict":
                    self.assertFalse(alternate.exists())

    def test_later_fetch_failure_preserves_receipts_and_cursor(self):
        raw = no_mid(fixtures.message(1))
        box = fixtures.Box({1: raw})
        self.assertEqual(self.run_poll(box)[0], 0)
        before_state = (self.store / "mail-poll" / "state.json").read_bytes()
        box.messages.update({2: raw, 3: fixtures.message(3)})
        fetch = mail_poll._fetch
        def fail(client, uid):
            if uid == 3:
                raise OSError("synthetic fetch failure")
            return fetch(client, uid)
        with mock.patch.object(mail_poll, "_fetch", side_effect=fail):
            self.assertEqual(self.run_poll(box)[0], 1)
        self.assertEqual((self.store / "mail-poll" / "state.json").read_bytes(), before_state)
        before = fixtures.tree(self.store / "mail-poll" / "receipts")
        self.assert_handoffs(raw, [(7, 1), (7, 2)])
        self.assertEqual(self.run_poll(box)[0], 0)
        after = fixtures.tree(self.store / "mail-poll" / "receipts")
        self.assertTrue(all(after[k] == v for k, v in before.items()))
        self.assertEqual(self.state()["cursor"], 3)

    def test_mid_dedup_conflict_notice_and_unsafe_twins(self):
        regular = fixtures.message(1)
        notice = no_mid(fixtures.notice(2))
        unsafe = no_mid(fixtures.message(4, attachments=(("../unsafe.txt", b"synthetic"),)))
        box = fixtures.Box({1: regular, 2: regular, 3: fixtures.message(1, extra=" amended"),
                            4: notice, 5: notice, 6: unsafe, 7: unsafe})
        code, line, err = self.run_poll(box)
        self.assertEqual(code, 0, err)
        self.assert_line(line, new=3, already=1, conflict=1, notices=2, skipped=2)
        receipts = self.receipts()
        self.assertEqual(len(receipts), 5)
        held = [r for r in receipts if r["held"]]
        self.assertEqual(len(held), 2)
        self.assertEqual(len({r["held"] for r in held}), 1)
        self.assertTrue(all(r["intake_receipt"] is None for r in held))
        self.assertEqual((self.store / "mail-poll" / held[0]["held"]).read_bytes(), unsafe)
        self.assert_handoffs(notice, [(7, 4), (7, 5)])
        for path in (self.store / "mail-poll" / "receipts").glob("*"):
            for value in (fixtures.ADDRESS, fixtures.SUBJECT, fixtures.SYNTHETIC_CREDENTIAL, "unsafe.txt"):
                self.assertNotIn(value, path.read_text())
        self.assertEqual(self.state()["cursor"], 7)
        self.assert_replay_immutable(box)

    def test_dry_run_of_no_mid_twins_writes_nothing(self):
        raw = no_mid(fixtures.message(1))
        box = fixtures.Box({1: raw, 2: raw, 3: fixtures.message(3)})
        mailbox = box.snapshot()
        self.assertEqual(self.run_poll(box, "--dry-run")[0], 0)
        self.assertIsNone(fixtures.tree(self.store))
        self.assertEqual(mailbox, box.snapshot())
        box.messages = {1: raw}
        self.assertEqual(self.run_poll(box)[0], 0)
        box.messages.update({2: raw, 3: fixtures.message(3)})
        before = fixtures.tree(self.store), box.snapshot()
        self.assertEqual(self.run_poll(box, "--dry-run")[0], 0)
        self.assertEqual(before, (fixtures.tree(self.store), box.snapshot()))


if __name__ == "__main__":
    unittest.main()
