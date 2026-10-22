"""M1: actual secure reads and injected I/O failures stay message-local."""
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from campaign_tool.records import run
from tests.records.test_run_slice import synthetic_email


class InboxFailureIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.inbox = self.base / 'inbox'
        self.inbox.mkdir()
        (self.inbox / 'z-valid.eml').write_bytes(synthetic_email())
        self.pipeline = run.Pipeline(self.base / 'root')

    def assert_survivor(self, report, failed):
        self.assertEqual(report['messages'], len(failed) + 1)
        self.assertEqual([r['file'] for r in report['preserved']], ['z-valid.eml'])
        self.assertEqual([r['file'] for r in report['failures']], failed)
        self.assertEqual(report['replayed'], [])
        self.assertTrue(all(f['detail'] == 'fetch_or_preserve_failed' for f in report['failures']))

    def test_leaf_and_dangling_symlinks_do_not_hide_or_abort_following_message(self):
        target = self.base / 'target'
        target.write_bytes(b'SYNTHETIC MUST NOT BE READ')
        (self.inbox / 'a-link.eml').symlink_to(target)
        (self.inbox / 'b-missing.MSG').symlink_to(self.base / 'absent')
        report = run.Pipeline(self.base / 'root').ingest_inbox(self.inbox)
        self.assert_survivor(report, ['a-link.eml', 'b-missing.MSG'])
        self.assertEqual(target.read_bytes(), b'SYNTHETIC MUST NOT BE READ')
        # Positive control: a second ordinary scan replays the valid message, never the links.
        again = run.Pipeline(self.base / 'root').ingest_inbox(self.inbox)
        self.assertEqual([r['file'] for r in again['replayed']], ['z-valid.eml'])
        self.assertEqual(again['preserved'], [])
        self.assertEqual([r['file'] for r in again['failures']], ['a-link.eml', 'b-missing.MSG'])

    def test_open_and_read_faults_are_message_local(self):
        original = run.intake_folder.secure_open
        (self.inbox / 'a-open.eml').write_bytes(b'synthetic')
        (self.inbox / 'b-read.eml').write_bytes(b'synthetic')

        @contextmanager
        def faulty(path):
            path = Path(path)
            if path.name == 'a-open.eml':
                raise PermissionError('synthetic open failure')
            if path.name == 'b-read.eml':
                stream = mock.Mock()
                stream.read.side_effect = OSError('synthetic read failure')
                yield stream
            else:
                with original(path) as stream:
                    yield stream

        with mock.patch.object(run.intake_folder, 'secure_open', faulty):
            report = run.Pipeline(self.base / 'root').ingest_inbox(self.inbox)
        self.assert_survivor(report, ['a-open.eml', 'b-read.eml'])
        self.assertEqual([r['error'] for r in report['failures']], ['PermissionError', 'OSError'])

    def test_symlinked_inbox_ancestor_is_never_followed_for_reads(self):
        alias = self.base / 'alias'
        alias.symlink_to(self.inbox, target_is_directory=True)
        report = self.pipeline.ingest_inbox(alias)
        self.assertEqual(report['preserved'], [])
        self.assertEqual([r['file'] for r in report['failures']], ['z-valid.eml'])
        self.assert_survivor(run.Pipeline(self.base / 'root').ingest_inbox(self.inbox), [])


if __name__ == '__main__':
    unittest.main()
