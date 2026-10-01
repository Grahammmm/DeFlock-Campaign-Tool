"""Synthetic composition with actual WP8 dependency; never calls a site/network.

The integration runner must overlay the pinned WP8 package AND gates/test paths.
REQUIRE_WP8_INTEGRATION=1 makes an absent dependency fail instead of skip.
"""
import copy
import json
import os
from pathlib import Path
import socket
import unittest
from unittest.mock import patch
from campaign_tool.records.recovery import publication as p
try:
    from campaign_tool.records import review_bundle as wp8
    from campaign_tool.records.gates import validate_findings as gate
    from tests.records import test_review_bundle as fixtures
except ImportError:
    if os.environ.get("REQUIRE_WP8_INTEGRATION") == "1":
        raise
    wp8 = None


@unittest.skipIf(wp8 is None, "Actual WP8 dependency not installed; composition not established")
class PublicationReviewBridgeTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.ReviewBundleTests()
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.f=self.fixture
        for name in ("socket","getaddrinfo"):
            guard=patch.object(socket,name,side_effect=AssertionError("network forbidden"))
            guard.start();self.addCleanup(guard.stop)
        self.bundle_id=self.f.create();self.f.complete(self.bundle_id)
        self.owner_id=self.f.owner(self.bundle_id)["receipt_id"]
        self.installed_id="synthetic-bridge"
        self.review_gate=p.configure_wp8_review_gate(self.installed_id,root=self.f.output,authority=self.f.authority)
        self.addCleanup(p._INSTALLED_REVIEW_VERIFIERS.pop,self.installed_id)
        self.ref=self.review_gate.reference(self.bundle_id,owner_receipt_id=self.owner_id,owner="synthetic-owner")
        self.authority=p.SyntheticApprovalAuthority()
        self.withdrawal=p.SyntheticRollbackAuthority(owner=self.authority.owner)
        self.runtime=p.testing_runtime(self.authority,review_verifier=self.review_gate,rollback_authority=self.withdrawal)
        self.box=p.PublicationOutbox(self.f.root/"outbox");self.addCleanup(self.box.close)

    def stage(self,*,content=None,ref=None,name="synthetic-proposal",supersedes=None):
        content=self.f.public if content is None else content
        ref=self.ref if ref is None else ref
        return self.box.stage(self.runtime,proposal_id=name,content=content,review_bundle=ref,
            approval=self.authority.approve(name,content,ref,supersedes=supersedes),supersedes=supersedes)

    def execute(self,action="prepare"):
        return self.box.execute(self.runtime,"synthetic-proposal",action)

    def withdraw(self,name="synthetic-proposal"):
        target=self.box.rollback_target(self.runtime,name)
        return self.box.execute(self.runtime,name,"rollback",
                                rollback_authorization=self.withdrawal.approve(target))

    def test_exact_json_review_owner_prepare_deploy_and_rollback(self):
        with patch.object(gate,"gate",wraps=gate.gate) as dependency:
            self.assertEqual(self.stage()["state"],"staged")
            prepared=self.execute();deployed=self.execute("deploy");rolled=self.withdraw()
        self.assertTrue(dependency.called)
        self.assertEqual(dependency.call_args.kwargs,{"check_files":True,"tier":"A"})
        self.assertEqual((self.box.site/"synthetic-proposal.json").read_bytes(),self.f.public)
        self.assertFalse((self.box.site/"synthetic-proposal.md").exists())
        self.assertFalse(prepared["production"]);self.assertFalse(deployed["production"])
        self.assertTrue(rolled["test_only"]);self.assertIsNone(self.runtime.adapter.active_version)
        self.assertFalse(self.f.assess(self.bundle_id)["handoff_ready"])

    def test_new_challenge_blocks_previously_staged(self):
        self.stage();self.f.review(self.bundle_id,"factual",verdict="challenge")
        self.assertEqual(self.execute()["state"],"blocked")
        self.assertEqual(self.runtime.adapter.calls,[])

    def test_new_challenge_blocks_prepared_deploy_and_completed_replay(self):
        self.stage();self.execute();self.f.review(self.bundle_id,"privacy",verdict="challenge")
        self.assertEqual(self.execute("deploy")["state"],"blocked")
        self.assertEqual(self.execute()["state"],"blocked")
        self.assertEqual(len(self.runtime.adapter.calls),1)

    def changed_binding_blocks(self,kind):
        self.stage()
        Path(self.f.proposal["bindings"][kind][0]["path"]).write_bytes(b"synthetic changed evidence")
        self.assertEqual(self.execute()["state"],"blocked")
        self.assertEqual(self.runtime.adapter.calls,[])

    def test_changed_original_blocks(self):self.changed_binding_blocks("originals")
    def test_changed_unit_blocks(self):self.changed_binding_blocks("units")
    def test_changed_digest_blocks(self):self.changed_binding_blocks("digests")
    def test_changed_source_blocks(self):self.changed_binding_blocks("sources")

    def test_added_pass_makes_owner_review_set_stale(self):
        self.stage();self.f.review(self.bundle_id,"factual",rationale="Additional synthetic pass")
        self.assertEqual(self.execute()["state"],"blocked")
        self.assertEqual(self.runtime.adapter.calls,[])

    def test_owner_rejection_blocks_previously_staged(self):
        self.stage()
        self.f.owner(self.bundle_id,self.f.owner_payload(self.bundle_id,decision="reject"))
        self.assertEqual(self.execute()["state"],"blocked")
        self.assertEqual(self.runtime.adapter.calls,[])

    def test_owner_signature_cannot_approve_different_rendered_bytes(self):
        self.assertEqual(self.stage(content=b"# Rendered but not reviewed\n")["state"],"blocked")
        self.assertEqual(self.runtime.adapter.calls,[])

    def test_descriptor_cannot_supply_private_root_or_callback(self):
        reference=json.loads(self.ref);reference["root"]=str(self.f.output)
        self.assertEqual(self.stage(ref=wp8.encoded(reference))["state"],"blocked")
        self.assertEqual(self.runtime.adapter.calls,[])

    def test_owner_receipt_must_be_exact_and_authenticated(self):
        reference=json.loads(self.ref);reference["owner_receipt_id"]=wp8.sha(b"not a receipt")
        self.assertEqual(self.stage(ref=wp8.encoded(reference))["state"],"blocked")
        self.assertEqual(self.runtime.adapter.calls,[])

    def test_wp9_owner_signature_does_not_substitute_for_wp8_owner(self):
        self.authority=p.SyntheticApprovalAuthority(owner="other-owner")
        self.withdrawal=p.SyntheticRollbackAuthority(owner=self.authority.owner)
        self.runtime=p.testing_runtime(self.authority,review_verifier=self.review_gate,rollback_authority=self.withdrawal)
        self.assertEqual(self.stage()["blocked_reason"],"wp8_owner_identity_mismatch")
        self.assertEqual(self.runtime.adapter.calls,[])

    def test_synthetic_wp8_cannot_be_used_for_production_assessment(self):
        self.stage()
        job=dict(self.box.db.execute("SELECT * FROM publication_jobs").fetchone())
        with self.assertRaisesRegex(p.ReviewGateError,"synthetic_wp8_not_production"):
            with self.review_gate.assess(job,owner="synthetic-owner",test_only=False):
                self.fail("Synthetic authority reached production")

    def test_review_append_cannot_race_adapter_under_wp8_lock(self):
        self.stage();original=self.runtime.adapter.perform
        def attempting_append(*args,**kwargs):
            with self.assertRaises((wp8.BundleError,BlockingIOError)):
                self.f.review(self.bundle_id,"factual",verdict="challenge")
            return original(*args,**kwargs)
        with patch.object(self.runtime.adapter,"perform",side_effect=attempting_append):
            self.execute()
        self.f.review(self.bundle_id,"factual",verdict="challenge")
        self.assertEqual(self.execute("deploy")["state"],"blocked")
        self.assertEqual(len(self.runtime.adapter.calls),1)

    def test_changed_installed_policy_blocks(self):
        reference=json.loads(self.ref);reference["authority_policy_sha256"]=wp8.sha(b"different policy")
        self.assertEqual(self.stage(ref=wp8.encoded(reference))["blocked_reason"],"wp8_authority_policy_changed")

    def test_supersession_maps_exact_prior_bundle_to_prior_proposal(self):
        self.stage();self.execute();self.execute("deploy")
        proposal=copy.deepcopy(self.f.proposal)
        proposal.update(proposal_id="synthetic-correction",revision=2,supersedes=self.bundle_id)
        new_public=wp8.encoded({"title":"Synthetic correction","claim":"Corrected synthetic statement","limitations":["Synthetic only"]})
        new_id=self.f.create(proposal,new_public);self.f.complete(new_id)
        owner_id=self.f.owner(new_id)["receipt_id"]
        reference=self.review_gate.reference(new_id,owner_receipt_id=owner_id,owner="synthetic-owner")
        self.assertEqual(self.stage(content=new_public,ref=reference,name="synthetic-correction",
            supersedes="synthetic-proposal")["state"],"staged")
        self.box.execute(self.runtime,"synthetic-correction","prepare")
        self.assertEqual((self.box.site/"synthetic-correction.json").read_bytes(),new_public)
        self.assertEqual((self.box.site/"synthetic-proposal.json").read_bytes(),self.f.public)

    def test_review_hold_preserves_prepared_phase(self):
        self.stage();self.execute();self.f.review(self.bundle_id,"factual",verdict="challenge")
        result=self.execute("deploy")
        self.assertEqual(result["state"],"blocked")
        row=self.box.status()[0]
        self.assertEqual(row["state"],"prepared")
        self.assertTrue(row["blocked_reason"])

    def test_challenged_deployed_content_has_separate_authorized_withdrawal(self):
        self.stage();self.execute();prior=self.execute("deploy")
        proposal=copy.deepcopy(self.f.proposal)
        proposal.update(proposal_id="synthetic-disputed",revision=2,supersedes=self.bundle_id)
        public=wp8.encoded({"title":"Synthetic newer update","claim":"Later disputed synthetic statement","limitations":["Synthetic only"]})
        identity=self.f.create(proposal,public);self.f.complete(identity)
        owner_id=self.f.owner(identity)["receipt_id"]
        reference=self.review_gate.reference(identity,owner_receipt_id=owner_id,owner="synthetic-owner")
        self.assertEqual(self.stage(content=public,ref=reference,name="synthetic-disputed",supersedes="synthetic-proposal")["state"],"staged")
        self.box.execute(self.runtime,"synthetic-disputed","prepare")
        deployed=self.box.execute(self.runtime,"synthetic-disputed","deploy")
        before=[tuple(row) for row in self.box.db.execute("SELECT * FROM publication_events ORDER BY sequence")]
        self.f.review(identity,"factual",verdict="challenge")
        replay=self.box.execute(self.runtime,"synthetic-disputed","deploy")
        self.assertEqual(replay["state"],"blocked")
        row=next(row for row in self.box.status() if row["proposal_id"]=="synthetic-disputed")
        self.assertEqual(row["state"],"deployed");self.assertTrue(row["blocked_reason"])
        no_approval=self.box.execute(self.runtime,"synthetic-disputed","rollback")
        self.assertEqual(no_approval["reason"],"explicit_rollback_authorization_required")
        self.assertEqual(self.runtime.adapter.active_version,deployed["deployed_version"])
        receipt=self.withdraw("synthetic-disputed")
        self.assertEqual(receipt["rollback_ref"],deployed["deployed_version"])
        self.assertEqual(receipt["restored_version"],prior["deployed_version"])
        self.assertEqual(receipt["restored_content_sha256"],wp8.sha(self.f.public))
        self.assertEqual(self.runtime.adapter.active_version,prior["deployed_version"])
        self.assertFalse(self.f.assess(identity)["review_complete"])
        self.assertFalse(receipt["production"])
        after=[tuple(row) for row in self.box.db.execute("SELECT * FROM publication_events ORDER BY sequence")]
        self.assertEqual(after[:len(before)],before)
        self.assertEqual((self.box.site/"synthetic-disputed.json").read_bytes(),public)
        self.assertEqual(next(row for row in self.box.status() if row["proposal_id"]=="synthetic-disputed")["state"],"rolled_back")
