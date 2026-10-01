"""Offline combined-runner acceptance contract against the explicit WP1 overlay."""
import json
import unittest
from unittest.mock import patch
from campaign_tool.records.portal import fetch_queue
from campaign_tool.records.portal.runner_bridge import PortalRunnerBridge,INTERFACE_VERSION
from campaign_tool.records.portal.policy import PortalError
from tests import test_records_portal_wp1 as fixtures
from tests.test_records_portal import approval,FakeTransport,response,NOW,HOST,URL,SOURCE


class RunnerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.WP1PortalTests.setUpClass()
        cls.store=fixtures.WP1PortalTests.store

    setUp=fixtures.WP1PortalTests.setUp
    tearDown=fixtures.WP1PortalTests.tearDown
    count=fixtures.WP1PortalTests.count

    def bridge(self):
        def forbidden(*args,**kwargs):
            raise AssertionError('network/policy callback must not be used by local outbox')
        return PortalRunnerBridge(queue=self.queue,ledger=self.adapter,
            approval_loader=forbidden,egress=forbidden,transport=forbidden)

    def preserve_sidecar(self):
        fetch_queue(self.queue,apply=True,approval_loader=approval,egress=lambda u:True,
            transport=FakeTransport(response()),wall=lambda:NOW.timestamp())

    def test_local_outbox_drains_without_network_and_is_idempotent(self):
        self.preserve_sidecar()
        with patch('socket.socket',side_effect=AssertionError('network forbidden')),patch('socket.getaddrinfo',side_effect=AssertionError('DNS forbidden')):
            first=self.bridge().drain_outbox(apply=True)
            replay=self.bridge().drain_outbox(apply=True)
        self.assertEqual(first['interface_version'],INTERFACE_VERSION)
        self.assertEqual(first['portal']['ledger_delivered'],1)
        self.assertEqual(first['portal']['ledger_pending'],0)
        self.assertEqual(replay['portal']['ledger_delivered'],0)
        self.assertEqual(self.count('originals'),1)
        self.assertEqual(self.count('occurrences'),1)
        self.assertEqual(self.count('stage_state'),7)
        self.assertEqual(len(first['pending_card_inputs']),1)
        self.assertTrue(all(v['pending']==1 and v['done']==0 for v in self.store.counts(self.database)['stages'].values()))
        self.assertNotIn('token=',json.dumps(first))

    def test_dry_outbox_does_not_enroll_or_invoke_callbacks(self):
        self.preserve_sidecar()
        result=self.bridge().drain_outbox()
        self.assertEqual(result['portal']['ledger_pending'],1)
        self.assertEqual(result['portal']['ledger_delivered'],0)
        self.assertEqual(result['pending_card_inputs'],[])
        self.assertEqual(self.count('originals'),0)

    def test_ledger_commit_before_ack_recovers_offline(self):
        self.preserve_sidecar()
        def crash(stage):
            if stage=='after_canonical_commit':raise RuntimeError('synthetic interruption')
        self.adapter.fault=crash
        failed=self.bridge().drain_outbox(apply=True)
        self.assertEqual(failed['portal']['ledger_error'],'ledger_delivery_pending')
        self.assertEqual(failed['portal']['ledger_pending'],1)
        self.adapter.fault=None
        completed=self.bridge().drain_outbox(apply=True)
        self.assertEqual(completed['portal']['ledger_pending'],0)
        self.assertEqual(self.count('occurrences'),1)
        self.assertEqual(self.count('runs'),1)
        self.assertEqual(self.count('stage_events'),7)

    def test_partial_outbox_failure_counts_prior_acknowledgments(self):
        self.preserve_sidecar()
        self.queue.inventory(HOST,'26-002','43',URL.replace('42','43'),SOURCE)
        self.preserve_sidecar()
        commits=[0]
        def fail_second(stage):
            if stage=='after_canonical_commit':
                commits[0]+=1
                if commits[0]==2:raise RuntimeError('synthetic later failure')
        self.adapter.fault=fail_second
        result=self.bridge().drain_outbox(apply=True)
        self.assertEqual(result['portal']['ledger_delivered'],1)
        self.assertEqual(result['portal']['ledger_pending'],1)
        self.assertEqual(self.count('occurrences'),2)
        self.adapter.fault=None
        result=self.bridge().drain_outbox(apply=True)
        self.assertEqual(result['portal']['ledger_delivered'],1)
        self.assertEqual(result['portal']['ledger_pending'],0)
        self.assertEqual(self.count('occurrences'),2)

    def test_acceptance_boundary_and_limits(self):
        self.assertEqual(INTERFACE_VERSION,'portal-runner-v1')
        for limit in (0,True,1001):
            with self.assertRaises(ValueError):self.bridge().drain_outbox(limit=limit)
        result=self.bridge().run()
        self.assertEqual(set(result),{'interface_version','portal','pending_card_inputs','stage_promotions','canonical_integration_verified','pipeline_complete'})
        self.assertEqual(result['stage_promotions'],0)
        self.assertFalse(result['canonical_integration_verified'])
        self.assertFalse(result['pipeline_complete'])
        with self.assertRaisesRegex(PortalError,'trusted_portal_stage_adapter_unconfigured'):
            self.bridge().validate_and_promote({},'synthetic-run')


if __name__=='__main__':unittest.main()
