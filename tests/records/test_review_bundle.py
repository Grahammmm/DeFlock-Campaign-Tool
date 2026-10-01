"""Synthetic WP8 fixtures only; no model calls, corpus or ledger promotions."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from campaign_tool.records import review_bundle as bundle
from campaign_tool.records.gates import validate_findings as gate


class ReviewBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wp8-synthetic-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        self.output = self.root / "private-bundles"
        self.output.mkdir(mode=0o700)
        self.contexts = {name: object() for name in ("author", "factual", "legal", "privacy", "owner")}
        sessions = {context: name for name, context in self.contexts.items()}
        principals = {}
        for name in self.contexts:
            roles = [name]
            if name == "author":
                roles.append("factual")
            if name == "factual":
                roles.append("privacy")
            principals[name] = {"identity": "synthetic-" + name, "provider": "synthetic-provider",
                                "model": "synthetic-model", "roles": roles}
        self.authority = bundle.Authority(profile_id="synthetic-profile", principals=principals,
            authenticate=lambda context: sessions[context], signing_key=b"synthetic-fixture-key-not-a-secret" * 2)
        bindings = {kind: [] for kind in bundle.KINDS}
        for kind in ("originals", "units", "digests", "sources"):
            bindings[kind] = [self.artifact(kind, ("synthetic " + kind).encode())]
        original, unit = bindings["originals"][0], bindings["units"][0]
        self.proposal = {
            "proposal_id": "synthetic-proposal", "revision": 1, "tier": "A",
            "agency": "Synthetic agency", "classification": "CONFIRMED_DOCUMENTARY_FACT",
            "next_action": "Await independent review and owner decision", "event_date": None,
            "primary_evidence": [{"path": original["path"], "sha256": original["sha256"],
                "locator": "line:1", "observation": "Synthetic bounded observation", "coverage": "selected"}],
            "rules": [], "counterevidence": [], "missing_evidence": [], "exceptions_checked": False,
            "bindings": bindings,
            "coverage": [{"original_sha256": original["sha256"], "unit_sha256": unit["sha256"],
                          "locator": "line:1", "scope": "selected"}], "supersedes": None}
        self.public = bundle.encoded({"title": "Synthetic title", "claim": "Synthetic bounded observation",
                                     "limitations": ["Selected synthetic evidence only"]})

    def artifact(self, name, raw):
        path = self.root / (name + ".txt")
        path.write_bytes(raw)
        path.chmod(0o600)
        return {"id": name, "path": str(path), "sha256": bundle.sha(raw), "bytes": len(raw)}

    def create(self, proposal=None, public=None):
        return bundle.create_bundle(self.output, public if public is not None else self.public,
            proposal or self.proposal, authority=self.authority, auth_context=self.contexts["author"])["bundle_id"]

    def target(self, identity):
        return bundle.review_target(self.output, identity, authority=self.authority)

    def payload(self, identity, role="factual", **changes):
        payload = dict(self.target(identity), role=role, verdict="pass", rationale="Synthetic source comparison",
                       reviewed_at="2026-01-01T00:00:00Z", reviewed_primary=True)
        payload.update(changes)
        return payload

    def review(self, identity, role, actor=None, **changes):
        return bundle.record_review(self.output, identity, self.payload(identity, role, **changes),
            authority=self.authority, auth_context=self.contexts[actor or role])

    def assess(self, identity):
        return bundle.assess_bundle(self.output, identity, authority=self.authority)

    def complete(self, identity, tier="A"):
        self.review(identity, "factual")
        self.review(identity, "privacy")
        if tier == "B":
            self.review(identity, "legal")

    def owner_payload(self, identity, **changes):
        report = self.assess(identity)
        value = {key: report[key] for key in ("bundle_id", "finding_digest", "public_content_sha256", "review_set_sha256")}
        value.update(decision="approve", rationale="Synthetic owner decision", decided_at="2026-01-02T00:00:00Z")
        value.update(changes)
        return value

    def owner(self, identity, payload=None):
        return bundle.record_owner_decision(self.output, identity, payload or self.owner_payload(identity),
            authority=self.authority, auth_context=self.contexts["owner"])

    def legal_proposal(self):
        proposal = copy.deepcopy(self.proposal)
        source = self.artifact("rule", b"Synthetic rule text")
        proposal.update(tier="B", classification="CONFIRMED_RULE_CONFLICT",
                        event_date="2026-01-01", exceptions_checked=True)
        proposal["bindings"]["rules"] = [source]
        proposal["rules"] = [{"rule_id": "synthetic-rule", "clause": "section:1",
            "source_url": "https://official.example.invalid/synthetic",
            "source_sha256": source["sha256"], "applicability": "yes", "version_verified": True,
            "effective_from": "2025-01-01", "effective_to": None}]
        return proposal

    def test_defaults_pending_owner_and_no_publication(self):
        identity = self.create()
        report = self.assess(identity)
        self.assertFalse(report["review_complete"])
        self.assertEqual(report["owner_approval"], "awaiting_owner")
        self.assertFalse(report["publication_ready"])
        self.assertEqual(report["stage_promotions"], 0)

    def test_tier_a_uses_portable_gate_factual_and_privacy(self):
        identity = self.create()
        self.complete(identity)
        with mock.patch.object(gate, "gate", wraps=gate.gate) as dependency:
            report = self.assess(identity)
        self.assertTrue(report["review_complete"])
        self.assertEqual(dependency.call_args.kwargs, {"check_files": True, "tier": "A"})
        self.assertFalse(report["production_review_complete"])
        self.assertFalse(report["handoff_ready"])

    def test_tier_b_needs_legal_and_exact_rule_bytes(self):
        identity = self.create(self.legal_proposal())
        self.complete(identity)
        self.assertFalse(self.assess(identity)["review_complete"])
        self.review(identity, "legal")
        self.assertTrue(self.assess(identity)["review_complete"])

    def test_tier_a_rejects_legal_classification(self):
        proposal = self.legal_proposal()
        proposal["tier"] = "A"
        with self.assertRaisesRegex(bundle.BundleError, "tier_a"):
            self.create(proposal)

    def test_portable_default_still_requires_legal(self):
        identity = self.create()
        directory, body, public = bundle._load(self.output, identity, self.authority)
        finding = bundle._finding(directory, body, public)
        reviews = []
        for role in ("factual", "privacy"):
            reviews.append({**self.payload(identity, role), "finding_id": finding["finding_id"],
                            "reviewer_agent": "synthetic-" + role})
        report = gate.gate(finding, reviews, check_files=True)
        self.assertFalse(report["ready"])
        self.assertIn("missing independent roles: legal", report["blockers"])

    def test_self_review_rejected(self):
        identity = self.create()
        with self.assertRaisesRegex(bundle.BundleError, "self_review"):
            self.review(identity, "factual", actor="author")

    def test_one_identity_for_two_roles_is_insufficient(self):
        identity = self.create()
        self.review(identity, "factual")
        self.review(identity, "privacy", actor="factual")
        self.assertIn("at least two distinct independent reviewers required", self.assess(identity)["blockers"])

    def test_identity_and_provider_cannot_come_from_receipt(self):
        identity = self.create()
        for field in ("reviewer_agent", "provider", "model", "owner", "authenticate"):
            with self.subTest(field=field), self.assertRaisesRegex(bundle.BundleError, "review_field_allowlist"):
                self.review(identity, "factual", **{field: "invented"})

    def test_session_must_be_authenticated_not_principal_label(self):
        identity = self.create()
        with self.assertRaisesRegex(bundle.BundleError, "authentication_failed"):
            bundle.record_review(self.output, identity, self.payload(identity),
                                 authority=self.authority, auth_context="factual")

    def test_authenticated_role_mismatch_rejected(self):
        identity = self.create()
        with self.assertRaisesRegex(bundle.BundleError, "role_not_allowed"):
            self.review(identity, "legal", actor="privacy")

    def test_case_aliases_in_installed_registry_rejected(self):
        values = {"a": {"identity": "Same", "provider": "synthetic", "model": "synthetic", "roles": ["author"]},
                  "b": {"identity": "SAME", "provider": "synthetic", "model": "synthetic", "roles": ["privacy"]}}
        with self.assertRaisesRegex(bundle.BundleError, "duplicate_or_invalid_identity"):
            bundle.Authority(profile_id="test", principals=values, authenticate=lambda _: "a",
                             signing_key=b"synthetic-key" * 4)

    def test_exact_coverage_and_locators_required(self):
        identity = self.create()
        for field, value in (("coverage", []), ("checked_locators", []), ("checked_public_artifacts", [])):
            with self.subTest(field=field), self.assertRaises(bundle.BundleError):
                self.review(identity, "factual", **{field: value})

    def test_coverage_cannot_silently_omit_a_bound_unit(self):
        proposal = copy.deepcopy(self.proposal)
        proposal["bindings"]["units"].append(self.artifact("second-unit", b"Another synthetic unit"))
        identity = self.create(proposal)
        self.complete(identity)
        self.assertIn("incomplete_units_coverage", self.assess(identity)["blockers"])

    def test_gaps_remain_blocked_even_with_passes(self):
        proposal = copy.deepcopy(self.proposal)
        proposal["missing_evidence"] = ["Missing synthetic context"]
        identity = self.create(proposal)
        self.complete(identity)
        self.assertIn("unresolved_evidence_gaps", self.assess(identity)["blockers"])
        with self.assertRaisesRegex(bundle.BundleError, "reviews_not_complete"):
            self.owner(identity)

    def test_missing_digest_binding_stays_blocked(self):
        proposal = copy.deepcopy(self.proposal)
        proposal["bindings"]["digests"] = []
        identity = self.create(proposal)
        self.complete(identity)
        self.assertIn("missing_digests_bindings", self.assess(identity)["blockers"])

    def test_changed_content_invalidates_receipts(self):
        old = self.create()
        payload = self.payload(old)
        changed = json.loads(self.public)
        changed["claim"] = "Changed synthetic observation"
        new = self.create(public=bundle.encoded(changed))
        self.assertNotEqual(old, new)
        with self.assertRaisesRegex(bundle.BundleError, "stale_review_binding"):
            bundle.record_review(self.output, new, payload, authority=self.authority,
                                 auth_context=self.contexts["factual"])

    def test_changed_source_bytes_block_replay_assessment(self):
        identity = self.create()
        self.complete(identity)
        Path(self.proposal["bindings"]["originals"][0]["path"]).write_bytes(b"changed")
        self.assertIn("unavailable_or_changed_originals_binding", self.assess(identity)["blockers"])

    def test_changed_unit_digest_and_source_bytes_block(self):
        identity = self.create()
        self.complete(identity)
        for kind in ("units", "digests", "sources"):
            path = Path(self.proposal["bindings"][kind][0]["path"])
            before = path.read_bytes()
            path.write_bytes(b"changed")
            self.assertIn("unavailable_or_changed_" + kind + "_binding", self.assess(identity)["blockers"])
            path.write_bytes(before)

    def test_changed_rule_bytes_block(self):
        proposal = self.legal_proposal()
        identity = self.create(proposal)
        self.complete(identity, "B")
        Path(proposal["bindings"]["rules"][0]["path"]).write_bytes(b"changed synthetic rule")
        self.assertIn("unavailable_or_changed_rules_binding", self.assess(identity)["blockers"])

    def test_changed_rule_metadata_invalidates_review(self):
        proposal = self.legal_proposal()
        old = self.create(proposal)
        receipt = self.payload(old)
        proposal["rules"][0]["effective_from"] = "2024-01-01"
        new = self.create(proposal)
        with self.assertRaisesRegex(bundle.BundleError, "stale_review_binding"):
            bundle.record_review(self.output, new, receipt, authority=self.authority,
                                 auth_context=self.contexts["factual"])

    def test_counterevidence_bytes_are_bound(self):
        proposal = copy.deepcopy(self.proposal)
        item = self.artifact("counterevidence", b"Synthetic contrary observation")
        proposal["bindings"]["counterevidence"] = [item]
        proposal["counterevidence"] = [{"sha256": item["sha256"], "locator": "line:1",
                                      "observation": "Synthetic counterexample retained"}]
        identity = self.create(proposal)
        self.complete(identity)
        self.assertTrue(self.assess(identity)["review_complete"])
        Path(item["path"]).write_bytes(b"changed")
        self.assertIn("unavailable_or_changed_counterevidence_binding", self.assess(identity)["blockers"])

    def test_mixed_private_fields_not_public_content(self):
        for field in ("primary_evidence", "private_manifest", "original_path", "token", "author_agent"):
            value = json.loads(self.public)
            value[field] = "synthetic-private-value"
            with self.subTest(field=field), self.assertRaisesRegex(bundle.BundleError, "public_field_allowlist"):
                self.create(public=bundle.encoded(value))

    def test_public_container_cannot_smuggle_nested_private_fields(self):
        value = json.loads(self.public)
        value["claim"] = {"private": "synthetic"}
        with self.assertRaises(bundle.BundleError):
            self.create(public=bundle.encoded(value))

    def test_public_and_private_bytes_are_separate(self):
        identity = self.create()
        directory = self.output / identity
        self.assertEqual((directory / "public_content.json").read_bytes(), self.public)
        self.assertNotIn(str(self.root).encode(), self.public)
        self.assertIn(str(self.root).encode(), (directory / "manifest.json").read_bytes())
        for name in ("manifest.json", "public_content.json"):
            self.assertEqual((directory / name).stat().st_mode & 0o777, 0o600)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)

    def test_bundle_replay_is_immutable(self):
        identity = self.create()
        result = bundle.create_bundle(self.output, self.public, self.proposal,
                                     authority=self.authority, auth_context=self.contexts["author"])
        self.assertTrue(result["reused"])
        self.assertEqual(identity, result["bundle_id"])

    def test_tampered_public_content_fails_closed(self):
        identity = self.create()
        (self.output / identity / "public_content.json").write_bytes(b"{}")
        with self.assertRaisesRegex(bundle.BundleError, "public_content_changed"):
            self.assess(identity)

    def test_tampered_manifest_fails_closed(self):
        identity = self.create()
        (self.output / identity / "manifest.json").write_bytes(b"{}")
        with self.assertRaisesRegex(bundle.BundleError, "bundle_manifest_changed"):
            self.assess(identity)

    def test_signed_review_cannot_be_forged_in_private_files(self):
        identity = self.create()
        self.review(identity, "factual")
        directory = self.output / identity / "reviews"
        path = next(directory.glob("*.json"))
        envelope = json.loads(path.read_bytes())
        envelope["body"]["actor"]["identity"] = "invented-reviewer"
        raw = bundle.encoded(envelope)
        forged = directory / (bundle.sha(raw) + ".json")
        forged.write_bytes(raw)
        forged.chmod(0o600)
        with self.assertRaisesRegex(bundle.BundleError, "authentication_signature"):
            self.assess(identity)

    def test_review_replay_does_not_duplicate(self):
        identity = self.create()
        first, second = self.review(identity, "factual"), self.review(identity, "factual")
        self.assertEqual(first["receipt_id"], second["receipt_id"])
        self.assertTrue(second["reused"])
        self.assertEqual(self.assess(identity)["review_receipts"], 1)

    def test_challenge_blocks_even_with_other_passes(self):
        identity = self.create()
        self.complete(identity)
        self.review(identity, "factual", verdict="challenge")
        self.assertIn("unresolved reviewer challenge or block", self.assess(identity)["blockers"])

    def test_owner_requires_authenticated_role(self):
        identity = self.create()
        self.complete(identity)
        with self.assertRaisesRegex(bundle.BundleError, "role_not_allowed"):
            bundle.record_owner_decision(self.output, identity, self.owner_payload(identity),
                                         authority=self.authority, auth_context=self.contexts["author"])

    def test_owner_approval_never_publishes_synthetic(self):
        identity = self.create()
        self.complete(identity)
        first, second = self.owner(identity), self.owner(identity)
        self.assertEqual(first["receipt_id"], second["receipt_id"])
        report = self.assess(identity)
        self.assertEqual(report["owner_approval"], "approved")
        self.assertFalse(report["publication_ready"])
        self.assertFalse(report["production_review_complete"])
        self.assertFalse(report["handoff_ready"])

    def test_owner_approval_does_not_follow_content_hash_change(self):
        old = self.create()
        self.complete(old)
        approval = self.owner_payload(old)
        self.owner(old, approval)
        changed = json.loads(self.public)
        changed["title"] = "Different synthetic title"
        new = self.create(public=bundle.encoded(changed))
        with self.assertRaisesRegex(bundle.BundleError, "stale_owner_binding"):
            self.owner(new, approval)
        self.assertEqual(self.assess(new)["owner_approval"], "awaiting_owner")

    def test_owner_approval_bound_to_exact_review_set(self):
        identity = self.create()
        self.complete(identity)
        self.owner(identity)
        self.review(identity, "factual", rationale="Additional synthetic review")
        self.assertIn("owner_approval_review_set_changed", self.assess(identity)["blockers"])
        self.assertFalse(self.assess(identity)["handoff_ready"])

    def test_owner_rejection_blocks(self):
        identity = self.create()
        self.complete(identity)
        self.owner(identity, self.owner_payload(identity, decision="reject"))
        self.assertIn("owner_rejected", self.assess(identity)["blockers"])

    def test_invalid_receipt_timestamp_rejected(self):
        identity = self.create()
        with self.assertRaisesRegex(bundle.BundleError, "review_evidence"):
            self.review(identity, "factual", reviewed_at="2026-01-01")

    def test_symlink_and_repository_output_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.output, target_is_directory=True)
        with self.assertRaisesRegex(bundle.BundleError, "symlink"):
            bundle.create_bundle(alias, self.public, self.proposal, authority=self.authority,
                                 auth_context=self.contexts["author"])
        (self.output / ".git").write_text("synthetic")
        with self.assertRaisesRegex(bundle.BundleError, "inside_repository"):
            self.create()

    def test_read_rejects_symlink_swapped_after_check(self):
        real = self.root / "real.json"
        real.write_bytes(b"{}")
        real.chmod(0o600)
        target = self.root / "swapped.json"
        target.write_bytes(b"{}")
        target.chmod(0o600)
        checked = bundle._private

        def check_then_swap(path, directory=False):
            result = checked(path, directory)
            target.unlink()
            target.symlink_to(real)
            return result

        with mock.patch.object(bundle, "_private", check_then_swap):
            with self.assertRaisesRegex(bundle.BundleError, "symlink_path"):
                bundle._read(target)
        self.assertEqual(bundle._read(real), b"{}")

    def test_world_readable_root_rejected(self):
        self.output.chmod(0o755)
        with self.assertRaisesRegex(bundle.BundleError, "owner_only"):
            self.create()

    def test_interrupted_bundle_write_exposes_no_bundle(self):
        real = bundle._new
        def interrupt(path, raw):
            if path.name == "public_content.json":
                raise RuntimeError("synthetic interruption")
            return real(path, raw)
        with mock.patch.object(bundle, "_new", side_effect=interrupt):
            with self.assertRaises(RuntimeError):
                self.create()
        self.assertEqual([p.name for p in self.output.iterdir()], ["review-bundle.lock"])
        self.assertTrue(self.create())

    def test_interrupted_review_write_does_not_commit_receipt(self):
        identity = self.create()
        with mock.patch.object(bundle, "_new", side_effect=RuntimeError("synthetic interruption")):
            with self.assertRaises(RuntimeError):
                self.review(identity, "factual")
        self.assertEqual(self.assess(identity)["review_receipts"], 0)
        self.assertFalse(self.review(identity, "factual")["reused"])

    def test_public_byte_bound(self):
        with self.assertRaisesRegex(bundle.BundleError, "json_byte_bound"):
            self.create(public=b" " * (bundle.MAX_PUBLIC + 1))

    def test_duplicate_json_keys_rejected(self):
        with self.assertRaises(bundle.BundleError):
            self.create(public=b'{"title":"one","title":"two","claim":"synthetic","limitations":[]}')

    def test_existing_gate_failure_is_not_bypassed(self):
        identity = self.create()
        self.complete(identity)
        with mock.patch.object(gate, "gate", return_value={"ready": False,
                "finding_digest": self.target(identity)["finding_digest"], "blockers": ["synthetic dependency blocker"]}):
            self.assertIn("synthetic dependency blocker", self.assess(identity)["blockers"])


if __name__ == "__main__":
    unittest.main()
