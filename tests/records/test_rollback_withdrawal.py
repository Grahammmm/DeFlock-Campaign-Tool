"""M7: historical approvals never authorize re-staging obsolete content."""
import json
import unittest
from tests.records import test_publish_authority as fixtures
from campaign_tool.records import publish
from campaign_tool.records.ledger import store


class WithdrawalTests(unittest.TestCase):
    def setUp(self):
        self.h = fixtures.PublicationFixture('runTest')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def test_correction_rollback_only_withdraws_retained_history_stays_immutable(self):
        first, second = self.h.two_releases()
        historical = self.h.staging / 'releases' / first['version'] / (self.h.proposal + '.md')
        original = historical.read_bytes()
        result = self.h.rollback()
        self.assertTrue(result['withdrawn'])
        self.assertIsNone(result['restored'])
        self.assertFalse((self.h.staging / 'content' / (self.h.proposal + '.md')).exists())
        self.assertEqual(historical.read_bytes(), original)
        manifest = json.loads((self.h.staging / 'releases' / result['version'] / 'manifest.json').read_bytes())
        self.assertIsNone(manifest['content_sha256'])
        # Reinstatement uses the still-current approved correction, never v1.
        again = self.h.stage()
        self.assertEqual(again['content_sha256'], second['content_sha256'])

    def test_revoked_current_approval_and_privacy_cannot_restore_old_text(self):
        self.h.two_releases()
        with store.ledger(self.h.root.ledger) as con:
            con.execute("UPDATE proposals SET owner_approval='none',approved_content_sha256=NULL")
            con.execute("UPDATE stage_state SET status='pending' WHERE stage='privacy'")
            con.commit()
        result = self.h.rollback()
        self.assertTrue(result['withdrawn'])
        self.assertIsNone(result['restored'])
        self.assertFalse((self.h.staging / 'content' / (self.h.proposal + '.md')).exists())
        with self.assertRaisesRegex(publish.PublishError, 'owner_approval_required'):
            self.h.stage()
