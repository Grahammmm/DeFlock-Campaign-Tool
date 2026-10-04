"""Synthetic actual intake -> durable bytes/items -> receipt -> checkpoint tests."""
import base64
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from campaign_tool.records.intake import eml_export, imap_intake, mail_delta, mail_wire
from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend
from campaign_tool.records.run import Pipeline
from tests.records import native_msg_fixtures as native
from tests.records import test_imap_intake as harness


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def leaf(body=b'Synthetic body\r\n'):
    return (b'From: fixture@x.invalid\r\nSubject: original\r\n\tcontinued folding\r\n'
            b'Content-Type: text/plain; charset=utf-8\r\n\r\n' + body)


def binary(raw, name, content_type=b'application/octet-stream'):
    return (b'Content-Type: ' + content_type + b'\r\nContent-Disposition: attachment; filename="' +
            name + b'"\r\nContent-Transfer-Encoding: base64\r\n\r\n' + base64.b64encode(raw))


def rfc(raw, name=None):
    header = b'Content-Type: message/rfc822\r\n'
    if name:
        header += b'Content-Disposition: attachment; filename="' + name + b'"\r\n'
    return header + b'\r\n' + raw


def mixed(parts, boundary=b'synthetic-boundary'):
    return (b'From: fixture@x.invalid\r\nSubject: outer\r\nContent-Type: multipart/mixed; boundary="' +
            boundary + b'"\r\n\r\n' +
            b''.join(b'--' + boundary + b'\r\n' + part + b'\r\n' for part in parts) +
            b'--' + boundary + b'--\r\n')


def chain():
    data = b'Exact synthetic attachment\x00\xff'
    child = mixed([leaf(), binary(data, b'fixture.bin')], b'inner-boundary')
    scalar = mixed([leaf(), rfc(child)], b'scalar-boundary')
    outer = mixed([leaf(), binary(scalar, b'nested.eml')])
    return outer, scalar, child, data


class ActualWireIntakeTests(unittest.TestCase):
    def setUp(self):
        self.h = harness.IMAPIntakeTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def intake(self, raw, uid=1):
        server = harness.FakeIMAP({'INBOX': {'uidvalidity': 7, 'messages': {uid: raw}},
                                  'Agencies': {'uidvalidity': 3, 'messages': {}}})
        result = Pipeline(self.h.root, account='synthetic-account').ingest_mailbox(
            self.h.config_path, client_factory=lambda config: server)
        self.assertTrue(server.logged_out)
        return result

    def stored(self, raw):
        rows = self.h.ledger('SELECT bytes,storage_path FROM originals WHERE sha256=?', (digest(raw),))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], len(raw))
        self.assertEqual(Path(rows[0][1]).read_bytes(), raw)

    def success(self, result):
        self.assertEqual(result['failures'], 0, result)
        self.assertEqual(result['preserved'], 1, result)
        self.assertEqual(self.h.checkpoints()['INBOX'], (7, 1))

    def test_scalar_eml_recurses_exact_wire_and_immediate_parents(self):
        raw, scalar, child, data = chain()
        self.success(self.intake(raw))
        for value in (raw, scalar, child, data):
            self.stored(value)
        rows = self.h.ledger('SELECT id,original_sha256,parent_occurrence_id FROM occurrences')
        by_sha = {sha: (oid, parent) for oid, sha, parent in rows}
        for parent, value in ((raw, scalar), (scalar, child), (child, data)):
            self.assertEqual(by_sha[digest(value)][1], by_sha[digest(parent)][0])
        self.assertEqual(self.h.ledger('SELECT count(*) FROM originals')[0][0], 4)
        self.assertEqual(self.h.ledger("SELECT count(*) FROM stage_state WHERE stage='preserve' AND status='done'")[0][0], 4)

    def test_named_and_unnamed_duplicates_are_distinct_occurrences(self):
        child = leaf(b'Exact CRLF\r\n')
        raw = mixed([leaf(), rfc(child), rfc(child, b'named.eml')])
        self.success(self.intake(raw))
        self.stored(child)
        self.assertEqual(self.h.ledger('SELECT count(*) FROM originals')[0][0], 2)
        rows = self.h.ledger('SELECT id,parent_occurrence_id,source_ref FROM occurrences WHERE original_sha256=?', (digest(child),))
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0][0], rows[1][0])
        self.assertEqual(rows[0][1], rows[1][1])
        self.assertNotEqual(json.loads(rows[0][2])['mime_path'], json.loads(rows[1][2])['mime_path'])

    def test_receipt_write_failure_holds_checkpoint_and_retry(self):
        raw, _, _, _ = chain()
        original = eml_export._write_private
        def fail_receipt(path, data):
            if Path(path).name == 'receipt.json':
                raise OSError('synthetic durability failure')
            return original(path, data)
        with patch.object(eml_export, '_write_private', side_effect=fail_receipt):
            result = self.intake(raw)
        self.assertEqual(result['failures'], 1)
        self.assertEqual(self.h.checkpoints()['INBOX'], (7, 0))
        self.assertTrue(any(p.read_bytes() == raw for p in (self.h.root / 'mail').rglob('message.eml')))
        self.success(self.intake(raw))

    def test_verification_receipt_failure_holds_checkpoint(self):
        raw, _, _, _ = chain()
        with patch.object(CanonicalMailBackend, '_capture_verification', side_effect=OSError('synthetic failure')):
            result = self.intake(raw)
        self.assertEqual(result['failures'], 1)
        self.assertEqual(self.h.checkpoints()['INBOX'], (7, 0))
        self.success(self.intake(raw))

    def test_checkpoint_failure_not_reported_as_success_and_replays(self):
        raw, _, _, _ = chain()
        save = imap_intake.IMAPIntake._save
        def fail_checkpoint(intake, con, folder, validity, highest, success):
            if folder == 'INBOX' and highest == 1 and success:
                raise OSError('synthetic checkpoint failure')
            return save(intake, con, folder, validity, highest, success)
        with patch.object(imap_intake.IMAPIntake, '_save', new=fail_checkpoint):
            result = self.intake(raw)
        self.assertEqual(result['preserved'], 0)
        self.assertEqual(result['failures'], 1)
        self.assertEqual(self.h.checkpoints()['INBOX'], (7, 0))
        self.stored(raw)
        count = self.h.ledger('SELECT count(*) FROM originals')[0][0]
        self.success(self.intake(raw))
        self.assertEqual(self.h.ledger('SELECT count(*) FROM originals')[0][0], count)

    def test_incomplete_inventory_cannot_advance_checkpoint(self):
        raw, _, _, _ = chain()
        export = eml_export.export_message
        def omit(*args, **kwargs):
            path = export(*args, **kwargs)
            value = json.loads(Path(path).read_bytes())
            value['attachments'].pop()
            Path(path).write_bytes(json.dumps(value).encode())
            return path
        with patch.object(eml_export, 'export_message', side_effect=omit):
            result = self.intake(raw)
        self.assertEqual(result['failures'], 1)
        self.assertEqual(self.h.checkpoints()['INBOX'], (7, 0))

    def test_corrupt_nested_message_holds_before_complete_receipt(self):
        raw = mixed([leaf(), binary(b'not a message', b'bad.eml')])
        result = self.intake(raw)
        self.assertEqual(result['failures'], 1)
        self.assertEqual(self.h.checkpoints()['INBOX'], (7, 0))

    def test_shared_counts_bytes_depth_fail_closed(self):
        raw, _, _, _ = chain()
        for limits in ({'max_captures': 1}, {'max_parts': 2}, {'max_depth': 1}, {'max_total_bytes': len(raw)}):
            with self.subTest(limits=limits), self.assertRaises(mail_wire.MailWireError):
                mail_wire.prepare(raw, 'eml', **limits)

    @unittest.skipUnless(native.available(), 'optional native MSG parser unavailable')
    def test_outer_msg_is_one_original_and_embedded_item_is_derived(self):
        raw = native.message(embedded=True)
        inbox = self.h.base / 'inbox'
        inbox.mkdir(mode=0o700)
        (inbox / 'synthetic.msg').write_bytes(raw)
        result = Pipeline(self.h.root).ingest_inbox(inbox)
        self.assertEqual(result['failures'], [], result)
        self.assertEqual(len(result['preserved']), 1, result)
        self.stored(raw)
        self.assertEqual(self.h.ledger('SELECT count(*) FROM originals')[0][0], 1)
        projection = json.loads(self.h.ledger('SELECT * FROM mail_messages')[0][7])
        self.assertEqual(projection['schema'], 'verified-msg-properties-v1')
        self.assertEqual(projection['properties']['headers']['subject'], 'Synthetic outer')
        self.assertEqual(projection['source_original_sha256'], digest(raw))
        self.assertFalse(projection['provider_attested'])

        rows = self.h.ledger("SELECT original_sha256,parent_occurrence_id,evidence FROM occurrences WHERE kind='attachment' AND json_extract(evidence,'$.native_item.schema')='native-msg-item-v1'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], digest(raw))
        item = json.loads(rows[0][2])['native_item']
        self.assertFalse(item['received_standalone_original'])
        self.assertEqual(item['representation'], 'derived_embedded_msg_item')
        self.assertTrue(item['ole_storage_locator'])
        files = list(self.h.root.rglob(item['stream_manifest_sha256']))
        self.assertTrue(files)
        manifest = next(p.read_bytes() for p in files if p.is_file())
        self.assertEqual(digest(manifest), item['stream_manifest_sha256'])
        self.assertEqual(self.h.ledger('SELECT count(*) FROM originals WHERE sha256=?', (item['stream_manifest_sha256'],))[0][0], 0)

    @unittest.skipUnless(native.available(), 'optional native MSG parser unavailable')
    def test_attached_msg_native_item_and_nested_eml_checkpoint(self):
        _, scalar, child, data = chain()
        msg = native.message(embedded=True, attachment=scalar)
        raw = mixed([leaf(), binary(msg, b'outer.msg', b'application/vnd.ms-outlook')])
        self.success(self.intake(raw))
        for value in (raw, msg, scalar, child, data):
            self.stored(value)
        self.assertEqual(self.h.ledger('SELECT count(*) FROM originals')[0][0], 5)
        items = self.h.ledger("SELECT id,original_sha256,parent_occurrence_id FROM occurrences WHERE kind='attachment' AND json_extract(evidence,'$.native_item.schema')='native-msg-item-v1'")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0][1], digest(msg))
        scalar_parent = self.h.ledger('SELECT parent_occurrence_id FROM occurrences WHERE original_sha256=?', (digest(scalar),))[0][0]
        self.assertEqual(scalar_parent, items[0][0])

    @unittest.skipUnless(native.available(), 'optional native MSG parser unavailable')
    def test_native_manifest_tamper_holds_checkpoint(self):
        msg = native.message(embedded=True)
        raw = mixed([leaf(), binary(msg, b'outer.msg')])
        export = eml_export.export_message
        reached = []
        def tamper(*args, **kwargs):
            path = export(*args, **kwargs)
            value = json.loads(Path(path).read_bytes())
            item = value['native_items'][0]
            target = path.parent / item['manifest_path']
            if not target.exists():
                target = self.h.root / 'mail' / item['manifest_path']
            self.assertEqual(digest(target.read_bytes()), item['stream_manifest_sha256'])
            target.write_bytes(b'synthetic manifest corruption')
            reached.append(True)
            return path
        with patch.object(eml_export, 'export_message', side_effect=tamper):
            result = self.intake(raw)
        self.assertEqual(reached, [True], 'fixture must reach artifact tampering')
        self.assertEqual(result['failures'], 1)
        self.assertEqual(self.h.checkpoints()['INBOX'], (7, 0))


if __name__ == '__main__':
    unittest.main()
