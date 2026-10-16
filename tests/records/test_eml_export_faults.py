"""Synthetic export interruption and immutable publication regressions."""
from email.message import EmailMessage
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records.intake import eml_export as export, mail_delta


class ExportFaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # macOS /var is a symlink, which secure_open intentionally rejects.
        self.root = Path(self.temp.name).resolve()
        message = EmailMessage()
        message['From'] = 'synthetic@example.invalid'
        message['To'] = 'recipient@example.invalid'
        message.set_content('Synthetic body')
        message.add_attachment(b'synthetic attachment', maintype='application',
                               subtype='octet-stream', filename='synthetic.bin')
        self.raw = message.as_bytes()

    def run_export(self, uid=1):
        return export.export_message(self.raw, mail_root=self.root,
                                     account='synthetic', mailbox='INBOX',
                                     uidvalidity=1, uid=uid)

    def snapshot(self):
        return {p: (p.read_bytes(), p.stat().st_ino)
                for p in self.root.rglob('*') if p.is_file()
                and not p.name.startswith('.export-')}

    def test_partial_write_replay_at_each_object(self):
        original = os.fdopen
        for object_index in range(3):
            with self.subTest(object=object_index):
                self.root = Path(self.temp.name).resolve() / str(object_index)
                calls = 0

                def interrupted(fd, *args, **kwargs):
                    nonlocal calls
                    stream = original(fd, *args, **kwargs)
                    if not args or args[0] != 'wb':
                        return stream
                    index = calls
                    calls += 1
                    if index == object_index:
                        real_write = stream.write

                        def write(data):
                            real_write(data[:10])
                            stream.flush()
                            raise OSError('synthetic partial write')
                        stream.write = write
                    return stream

                with patch.object(export.os, 'fdopen', interrupted):
                    with self.assertRaises(OSError):
                        self.run_export()
                accepted = self.snapshot()
                receipt = self.run_export()
                mail_delta.load_receipt(receipt)
                self.assertEqual(receipt, self.run_export())
                for path, value in accepted.items():
                    self.assertEqual(value, self.snapshot()[path])

    def test_fsync_and_publication_failures_recover(self):
        for operation, count in [('fsync', 6), ('link', 3)]:
            original = getattr(os, operation)
            for boundary in range(count):
                with self.subTest(operation=operation, boundary=boundary):
                    self.root = Path(self.temp.name).resolve() / f'{operation}-{boundary}'
                    calls = 0

                    def fail_once(*args, **kwargs):
                        nonlocal calls
                        index = calls
                        calls += 1
                        if index == boundary:
                            raise OSError('synthetic publication failure')
                        return original(*args, **kwargs)

                    with patch.object(export.os, operation, fail_once):
                        with self.assertRaises(OSError):
                            self.run_export()
                    accepted = self.snapshot()
                    mail_delta.load_receipt(self.run_export())
                    for path, value in accepted.items():
                        self.assertEqual(value, self.snapshot()[path])
                    # A later occurrence can advance after the failed UID replays.
                    mail_delta.load_receipt(self.run_export(uid=2))

    def test_flush_failure_and_cleanup_after_publish_recover(self):
        original_fdopen = os.fdopen

        def bad_flush(fd, *args, **kwargs):
            stream = original_fdopen(fd, *args, **kwargs)
            if args and args[0] == 'wb':
                def fail():
                    raise OSError('synthetic flush failure')
                stream.flush = fail
            return stream

        with patch.object(export.os, 'fdopen', bad_flush):
            with self.assertRaises(OSError):
                self.run_export()
        self.assertEqual({}, self.snapshot())
        original_unlink = os.unlink
        failed = False

        def bad_cleanup(path, *args, **kwargs):
            nonlocal failed
            if not failed and Path(path).name.startswith('.export-'):
                failed = True
                raise OSError('synthetic cleanup failure')
            return original_unlink(path, *args, **kwargs)

        with patch.object(export.os, 'unlink', bad_cleanup):
            with self.assertRaises(OSError):
                self.run_export()
        accepted = self.snapshot()
        mail_delta.load_receipt(self.run_export())
        for path, value in accepted.items():
            self.assertEqual(value, self.snapshot()[path])

    def test_existing_file_never_replaced(self):
        target = self.root / 'accepted'
        target.write_bytes(b'accepted evidence')
        before = (target.read_bytes(), target.stat().st_ino)
        with self.assertRaises(FileExistsError):
            export._write_private(target, b'different bytes')
        self.assertEqual(before, (target.read_bytes(), target.stat().st_ino))
        private = self.root / 'new'
        export._write_private(private, b'complete')
        self.assertEqual(0o600, private.stat().st_mode & 0o777)

    def test_orphan_attempt_ignored(self):
        receipt = self.run_export()
        (receipt.parent / '.export-abandoned').write_bytes(b'partial')
        before = self.snapshot()
        self.assertEqual(receipt, self.run_export())
        self.assertEqual(before, self.snapshot())
