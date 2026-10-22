"""Synthetic tests for the rescued records understanding package.

Every fixture below is invented for these tests: the agency, request numbers,
policy text and hashes do not describe any real record, person or source.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from campaign_tool.records import grounded_evidence, retained_units
from campaign_tool.records.understanding import (
    cli,
    correspondence,
    document,
    evidence,
    policy,
    reconcile,
)

REPO = Path(__file__).resolve().parents[2]
AGENCY = "example-agency"
REQUEST = "R-0001"

PAGE_ONE = (
    "Public records request R-0001\n"
    "Agency: Example Agency\n"
    "Date: 2030-01-15\n"
    "Policy 100.1 requires retention of audit log entries for 30 days."
)
PAGE_TWO = '{"retention_days": 30, "system": "synthetic"}'


def digest(data):
    return hashlib.sha256(data).hexdigest()


ORIGINAL = digest(b"synthetic original document for understanding tests")


def native_receipt():
    return {
        "schema": "format-extraction-v1",
        "original_sha256": ORIGINAL,
        "original_changed": False,
        "status": "complete",
        "parser": "synthetic",
        "parser_version": "0",
        "route": {"format": "pdf"},
        "pages": [{"page_no": 1, "status": "ok"}, {"page_no": 2, "status": "ok"}],
        "units": [
            {"kind": "text", "locator": {"page": 1}, "text": PAGE_ONE, "data": {}},
            {"kind": "text", "locator": {"page": 2}, "text": PAGE_TWO, "data": {}},
        ],
    }


def make_bundle():
    raw = evidence.canonical(native_receipt())
    return evidence.normalize_receipt(native_receipt(), ORIGINAL, digest(raw))


def policy_versions(content_sha256=ORIGINAL, requirements=None):
    return [{
        "version_id": "v1",
        "family_id": "retention",
        "jurisdiction": "example-state",
        "agency_id": "*",
        "regulated_roles": ["operator"],
        "effective_from": "2029-01-01",
        "issued_at": "2028-12-01",
        "retrieved_at": "2031-01-01",
        "supersession_status": "not_superseded_as_of_retrieval",
        "source": {
            "source_id": "synthetic-policy",
            "url": "https://example.invalid/policy",
            "locator": "section 100.1",
            "verification_status": "verified",
            "content_sha256": content_sha256,
        },
        "requirements": requirements if requirements is not None else [],
    }]


class EvidenceTests(unittest.TestCase):
    def test_receipt_normalizes_to_pinned_bundle(self):
        bundle = make_bundle()
        self.assertEqual(len(bundle["units"]), 2)
        self.assertIs(evidence.validate_bundle(bundle), bundle)
        context = bundle["source_context"][ORIGINAL]
        self.assertFalse(context["review_acceptance"])
        self.assertFalse(context["semantic_acceptance"])

    def test_tampered_bundle_is_rejected(self):
        bundle = make_bundle()
        bundle["units"][0]["text"] = "altered"
        with self.assertRaisesRegex(ValueError, "bundle_snapshot_mismatch"):
            evidence.validate_bundle(bundle)

    def test_receipt_binding_mismatch_is_rejected(self):
        receipt = dict(native_receipt(), original_changed=True)
        with self.assertRaisesRegex(ValueError, "native_receipt_binding"):
            evidence.normalize_receipt(receipt, ORIGINAL, "c" * 64)

    def test_duplicate_json_keys_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate_json_key"):
            evidence.decode(b'{"a":1,"a":2}')

    def test_quote_citation_and_claim_round_trip(self):
        bundle = make_bundle()
        unit = next(u for u in bundle["units"] if u["locator"] == {"page": 1})
        cite = evidence.cite_text(unit, "audit log entries for 30 days")
        self.assertEqual(cite["quote"], "audit log entries for 30 days")
        checked = evidence.validate_citation(cite, bundle)
        self.assertEqual(checked["semantic_assessment"], "NOT_ASSESSED")
        self.assertFalse(checked["review_acceptance"])
        made = evidence.claim(bundle, "Retention is 30 days.", [cite])
        self.assertEqual(evidence.validate_claim(made, bundle)["semantic_assessment"], "NOT_ASSESSED")
        with self.assertRaisesRegex(ValueError, "quote_not_in_source"):
            evidence.cite_text(unit, "not present in the synthetic text")


class GroundedEvidenceTests(unittest.TestCase):
    def test_json_pointer_support_is_exact(self):
        bundle = make_bundle()
        unit = next(u for u in bundle["units"] if u["locator"] == {"page": 2})
        support = grounded_evidence.pointer_support(unit, "/retention_days")
        self.assertEqual(support["quote"], "30")
        self.assertEqual(grounded_evidence.validate_support(support, bundle)["structural_support"],
                         "verified_exact_retained_bytes")
        forged = dict(support, quote="31")
        with self.assertRaisesRegex(ValueError, "evidence_token_or_quote_mismatch"):
            grounded_evidence.validate_support(forged, bundle)


class RetainedUnitsTests(unittest.TestCase):
    def test_strict_json_and_path_guards(self):
        with self.assertRaises(retained_units.RetainedUnitError) as caught:
            retained_units._json('{"k":1,"k":2}')
        self.assertEqual(caught.exception.code, "duplicate_json_key")
        with self.assertRaises(retained_units.RetainedUnitError) as caught:
            retained_units._path("relative/path")
        self.assertEqual(caught.exception.code, "path_invalid")
        with self.assertRaises(retained_units.RetainedUnitError) as caught:
            retained_units._outside_git(str(REPO / "campaign_tool"))
        self.assertEqual(caught.exception.code, "path_inside_git")


class DocumentTests(unittest.TestCase):
    def test_analyze_reports_candidates_without_acceptance(self):
        result = document.analyze(make_bundle())
        self.assertEqual(result["schema"], document.SCHEMA)
        self.assertFalse(result["review_acceptance"])
        self.assertEqual(result["semantic_assessment"], "NOT_ASSESSED")
        labels = {c["candidate"] for c in result["classifications"]}
        self.assertIn("policy", labels)
        for item in result["classifications"]:
            evidence.validate_citation(item["citation"], make_bundle())
        self.assertTrue(any("2030-01-15" in json.dumps(d) for d in result["date_mentions"]))
        pages = result["coverage"][0]["pages"]
        self.assertEqual([p["page"] for p in pages], [1, 2])
        self.assertTrue(all(p["visual_review"] == "not_established" for p in pages))

    def test_model_proposal_must_be_cited(self):
        bundle = make_bundle()
        unit = bundle["units"][0]
        cite = evidence.cite_text(unit, "Example Agency")
        ok = document.apply_model(bundle, {"claims": [{"text": "Names an agency.", "citations": [cite]}]})
        self.assertEqual(ok["status"], "structurally_cited_not_semantically_assessed")
        self.assertEqual(len(ok["claims"]), 1)
        uncited = document.apply_model(bundle, {"claims": [{"text": "Uncited.", "citations": []}]})
        self.assertEqual(uncited["status"], "abstained")
        self.assertEqual(uncited["claims"], [])
        self.assertIn("model_claim_uncited", uncited["gaps"])


class CorrespondenceTests(unittest.TestCase):
    def test_tracks_explicit_events_and_never_sends(self):
        events = [
            {"agency_id": AGENCY, "request_number": REQUEST, "event_type": "request",
             "occurred_at": "2030-01-01T10:00:00Z", "items_requested": ["audit logs"]},
            {"agency_id": AGENCY, "request_number": REQUEST,
             "text": "We received your request.", "occurred_at": "2030-01-02T10:00:00Z"},
            {"agency_id": AGENCY, "event_type": "other"},
        ]
        result = correspondence.track(events, as_of="2030-02-01T00:00:00Z")
        self.assertEqual(len(result["requests"]), 1)
        request = result["requests"][0]
        self.assertEqual(len(request["timeline"]), 2)
        self.assertFalse(request["private_followup_draft"]["send"])
        self.assertTrue(request["private_followup_draft"]["requires_human_review"])
        self.assertIn("agency_or_request_number_missing", [h["reason"] for h in result["holds"]])
        self.assertFalse(result["review_acceptance"])

    def test_bundle_mode_requires_citations(self):
        events = [{"agency_id": AGENCY, "request_number": REQUEST, "event_type": "request"}]
        result = correspondence.track(events, as_of="2030-02-01T00:00:00Z", bundle=make_bundle())
        self.assertEqual(result["requests"], [])
        self.assertIn("event_citation_required", [h["reason"] for h in result["holds"]])


class PolicyTests(unittest.TestCase):
    def test_selects_single_in_force_version(self):
        versions = policy_versions(requirements=[{"requirement_id": "r1", "text": "retain 30 days"}])
        self.assertTrue(policy.validate_versions(versions)["valid"])
        out = policy.requirements_for(versions, agency_id=AGENCY, jurisdiction="example-state",
                                      regulated_role="operator", event_at="2030-01-15")
        self.assertEqual(out["status"], "selected")
        self.assertFalse(out["legal_conclusion"])
        self.assertEqual(out["requirements"][0]["version_id"], "v1")

    def test_gaps_are_explicit(self):
        versions = policy_versions()
        early = policy.select_version(versions, agency_id=AGENCY, jurisdiction="example-state",
                                      regulated_role="operator", event_at="2028-06-01")
        self.assertEqual(early["status"], "not_in_force")
        role = policy.select_version(versions, agency_id=AGENCY, jurisdiction="example-state",
                                     regulated_role="vendor", event_at="2030-01-15")
        self.assertEqual(role["status"], "regulated_role_gap")
        overlap = versions + [dict(versions[0], version_id="v2")]
        codes = [i["code"] for i in policy.validate_versions(overlap)["issues"]]
        self.assertIn("overlapping_versions", codes)


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.bundle = make_bundle()
        unit = self.bundle["units"][0]
        req_text = "audit log entries for 30 days"
        self.rule_cite = evidence.cite_text(unit, req_text)
        self.versions = policy_versions(requirements=[{
            "requirement_id": "r1", "text": req_text, "citations": [self.rule_cite],
            "test": {"field": "retention_days", "operator": "at_most", "expected": 30},
        }])
        data_unit = self.bundle["units"][1]
        self.fact_cite = grounded_evidence.pointer_support(data_unit, "/retention_days")
        self.scope = {"agency_id": AGENCY, "jurisdiction": "example-state",
                      "regulated_role": "operator", "event_at": "2030-01-15"}

    def compare(self, value):
        facts = [{"fact_id": "f1", "field": "retention_days", "value": value,
                  "citations": [self.fact_cite]}]
        return reconcile.compare(facts, self.versions, bundle=self.bundle, **self.scope)

    def test_classifications_never_reach_legal_conclusions(self):
        ok = self.compare(30)
        self.assertEqual(ok["findings"][0]["classification"], "no_conflict_observed")
        self.assertEqual(ok["legal_conclusions"], 0)
        self.assertEqual(ok["findings"][0]["legal_assessment"], "NOT_ASSESSED")
        self.assertEqual(self.compare(45)["findings"][0]["reason"], "fact_value_unbound")
        self.assertEqual(self.compare("n/a")["findings"][0]["classification"], "missing_evidence")

    def test_uncited_facts_become_gaps(self):
        facts = [{"fact_id": "f1", "field": "retention_days", "value": 30, "citations": []}]
        out = reconcile.compare(facts, self.versions, bundle=self.bundle, **self.scope)
        self.assertEqual(out["gaps"], [{"fact_id": "f1", "reason": "fact_uncited"}])


class CliTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.root.chmod(0o700)
        self.output = self.root / "out"

    def write_input(self, value):
        path = self.root / "input.json"
        path.write_bytes(evidence.canonical(value))
        path.chmod(0o600)
        return path

    def run_cli(self, *args):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records.understanding", *args],
                              cwd=REPO, env=env, capture_output=True, text=True, timeout=60)

    def test_code_pins_cover_package_and_dependencies(self):
        pins = cli.code_pins()
        self.assertIn("cli.py", pins)
        self.assertIn("records/grounded_evidence.py", pins)
        self.assertIn("records/retained_units.py", pins)

    def test_policy_command_writes_private_immutable_run(self):
        path = self.write_input({"versions": policy_versions()})
        first = self.run_cli("policy", "--input", str(path), "--output", str(self.output))
        self.assertEqual(first.returncode, 0, first.stderr)
        result = json.loads(first.stdout)
        self.assertFalse(result["reused"])
        self.assertTrue(result["summary"]["version_validation_valid"])
        run_dir = Path(result["output"])
        self.assertEqual(sorted(p.name for p in run_dir.iterdir()), ["receipt.json", "result.json"])
        receipt = json.loads((run_dir / "receipt.json").read_text())
        self.assertEqual(receipt["production_database_writes"], 0)
        self.assertEqual(receipt["external_content_transmission"], 0)
        self.assertEqual(os.stat(run_dir / "result.json").st_mode & 0o777, 0o400)
        second = self.run_cli("policy", "--input", str(path), "--output", str(self.output))
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertTrue(json.loads(second.stdout)["reused"])

    def test_document_command_from_native_binding(self):
        receipt_raw = evidence.canonical(native_receipt())
        receipt_path = self.root / "receipt.json"
        receipt_path.write_bytes(receipt_raw)
        receipt_path.chmod(0o600)
        path = self.write_input({"native_binding": {
            "receipt_path": str(receipt_path), "receipt_sha256": digest(receipt_raw),
            "original_sha256": ORIGINAL}})
        done = self.run_cli("document", "--input", str(path), "--output", str(self.output))
        self.assertEqual(done.returncode, 0, done.stderr)
        summary = json.loads(done.stdout)["summary"]
        self.assertEqual(summary["normalized_units"], 2)
        self.assertFalse(summary["review_acceptance"])

    def test_rejects_world_readable_input_and_output_inside_source(self):
        path = self.write_input({"versions": []})
        path.chmod(0o644)
        bad = self.run_cli("policy", "--input", str(path), "--output", str(self.output))
        self.assertNotEqual(bad.returncode, 0)
        with self.assertRaisesRegex(ValueError, "output_symlink_or_git|output_inside_source"):
            cli.private_root(REPO / "build-understanding-output")


if __name__ == "__main__":
    unittest.main()
