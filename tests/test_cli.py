"""Newly authored M0 regressions, not recovered historical tests.

All fixtures are synthetic and temporary. Tests exercise the offline starter;
passing structural review checks is not authenticated or substantive review.
Run from the repository root: python3 -B tests/test_cli.py -v
"""

import copy
import hashlib
import html
import json
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import review


class StarterCLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-cli-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "campaign"

    def run_cli(self, command, *arguments, returncode=0, root=None):
        result = subprocess.run(
            [sys.executable, "-B", "-m", "campaign_tool", command,
             "--directory", str(root or self.root), *map(str, arguments)],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(
            result.returncode, returncode,
            f"{command}: stdout={result.stdout!r}; stderr={result.stderr!r}",
        )
        self.assertNotIn("Traceback", result.stderr)
        if returncode == 0:
            self.assertEqual(result.stderr, "")
        return result

    def initialize(self):
        return self.run_cli(
            "init", "--name", "Synthetic Campaign", "--county",
            "Synthetic County", "--state", "ca",
        )

    def config(self):
        return json.loads((self.root / "campaign.json").read_text())

    def write_config(self, config):
        (self.root / "campaign.json").write_text(json.dumps(config))

    def fixture(self, name="synthetic-policy.txt", data=b"Synthetic policy only.\n"):
        path = self.base / name
        path.write_bytes(data)
        return path

    def ingest(self, source, source_id="synthetic-delivery-a"):
        return json.loads(self.run_cli(
            "ingest", "--file", source, "--source-id", source_id,
        ).stdout)

    def counts(self):
        report = json.loads(self.run_cli("status").stdout)
        self.assertEqual(report["substantive_review"], "not_tracked_in_alpha")
        return report["stored_objects"], report["receipt_occurrences"]

    def ledger_rows(self, table):
        # Only fixed internal table names are passed, never fixture content.
        connection = sqlite3.connect(
            (self.root / "private" / "ledger.sqlite").as_uri() + "?mode=ro",
            uri=True,
        )
        try:
            return connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        finally:
            connection.close()

    def test_init_safe_defaults_doctor_and_empty_status(self):
        self.initialize()
        self.assertEqual(self.config(), {
            "schema_version": 1, "name": "Synthetic Campaign",
            "county": "Synthetic County", "state": "CA", "country": "US",
            "jurisdiction_verified": False, "law_package_status": "unreviewed",
            "external_sends": "disabled", "publication": "manual",
            "newsletter": {"mode": "not_configured"},
        })
        private = self.root / "private"
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o700)
        self.assertEqual(self.counts(), (0, 0))
        self.assertFalse((private / "ledger.sqlite").exists())
        report = json.loads(self.run_cli("doctor").stdout)
        self.assertIs(report["config_readable"], True)
        for field in (
            "jurisdiction_verified", "reviewed_law_package",
            "public_deployment_checked", "newsletter_checked",
            "safe_to_send_automatically", "production_ready",
        ):
            self.assertIs(report[field], False, field)
        self.assertTrue(report["next_actions"])

    def test_init_rejects_blank_names_counties_and_invalid_states(self):
        cases = [(field, value) for field in ("name", "county")
                 for value in ("", " ", "\t\n")]
        cases += [("state", value) for value in ("", "C", "CAL", "C1", " CA ")]
        for index, (field, value) in enumerate(cases):
            with self.subTest(field=field, value=value):
                values = {"name": "Synthetic", "county": "Synthetic", "state": "CA"}
                values[field] = value
                root = self.base / f"invalid-{index}"
                result = self.run_cli(
                    "init", "--name", values["name"], "--county", values["county"],
                    "--state", values["state"], root=root, returncode=1,
                )
                expected = ("State must be a two-letter code" if field == "state"
                            else "Missing campaign field: " + field)
                self.assertIn(expected, result.stderr)
                self.assertFalse((root / "campaign.json").exists())
                self.assertFalse((root / "private").exists())

    def test_init_refuses_to_overwrite_existing_campaign(self):
        self.initialize()
        original = (self.root / "campaign.json").read_bytes()
        result = self.run_cli(
            "init", "--name", "Replacement", "--county", "Other Synthetic",
            "--state", "NY", returncode=1,
        )
        self.assertIn("Stopped:", result.stderr)
        self.assertEqual((self.root / "campaign.json").read_bytes(), original)

    def test_config_validation_rejects_malformed_or_missing_fields(self):
        self.initialize()
        original = self.config()
        cases = []
        for field in ("name", "county", "state"):
            missing = dict(original)
            del missing[field]
            cases.append((missing, "Missing campaign field: " + field))
            for value in (None, 7, "", " \t"):
                cases.append(({**original, field: value}, "Missing campaign field: " + field))
        for value in (0, 2, "1", None):
            cases.append(({**original, "schema_version": value}, "Unsupported campaign schema"))
        for value in ("ca", "C1", "CAL"):
            cases.append(({**original, "state": value}, "Use a two-letter state code"))
        for config, expected in cases:
            with self.subTest(config=config):
                self.write_config(config)
                self.assertIn(expected, self.run_cli("doctor", returncode=1).stderr)
        (self.root / "campaign.json").write_text("{not valid json")
        self.assertIn("Stopped:", self.run_cli("doctor", returncode=1).stderr)
        self.assertFalse((self.root / "public").exists())
        self.assertFalse((self.root / "private" / "ledger.sqlite").exists())

    def test_build_escapes_public_fields_and_excludes_nonallowlisted_data(self):
        self.initialize()
        config = self.config()
        name = '<script>alert("synthetic")</script> & campaign'
        county = 'Synthetic <img src=x onerror="alert(1)"> County'
        config.update({
            "name": name, "county": county,
            "internal_note": "SYNTHETIC_CONFIG_NOT_PUBLIC",
            "newsletter": {"mode": "SYNTHETIC_NESTED_NOT_PUBLIC"},
        })
        self.write_config(config)
        source = self.fixture(data=b"SYNTHETIC_ORIGINAL_NOT_PUBLIC\n")
        self.ingest(source, "SYNTHETIC_RECEIPT_NOT_PUBLIC")
        self.run_cli("build")
        public = self.root / "public"
        self.assertEqual({p.name for p in public.iterdir()}, {"index.html", "style.css", "_headers"})
        page = (public / "index.html").read_text()
        self.assertIn(html.escape(name, quote=True), page)
        self.assertIn(html.escape(county, quote=True), page)
        self.assertNotIn("<script", page.lower())
        self.assertNotIn("<img", page.lower())
        self.assertIn("No agency findings are asserted", page)
        for path in public.iterdir():
            exported = path.read_text()
            for marker in ("SYNTHETIC_CONFIG_NOT_PUBLIC", "SYNTHETIC_NESTED_NOT_PUBLIC",
                           "SYNTHETIC_ORIGINAL_NOT_PUBLIC", "SYNTHETIC_RECEIPT_NOT_PUBLIC"):
                self.assertNotIn(marker, exported, path.name)
        headers = (public / "_headers").read_text()
        for directive in ("default-src 'none'", "form-action 'none'", "frame-ancestors 'none'",
                          "X-Content-Type-Options: nosniff", "Referrer-Policy: no-referrer"):
            self.assertIn(directive, headers)

    def test_ingest_preserves_exact_bytes_identity_and_private_permissions(self):
        self.initialize()
        data = b"Synthetic binary fixture\x00\xff\r\n"
        source = self.fixture(data=data)
        result = self.ingest(source)
        digest = hashlib.sha256(data).hexdigest()
        identity = hashlib.sha256(json.dumps(
            ["synthetic-delivery-a", digest], separators=(",", ":"),
        ).encode()).hexdigest()
        self.assertEqual(result, {
            "sha256": digest, "receipt_id": identity, "new_receipt": True,
            "analysis_status": "not_started",
        })
        stored = self.root / "private" / "objects" / digest
        self.assertEqual(stored.read_bytes(), data)
        self.assertEqual(source.read_bytes(), data)
        self.assertEqual(self.counts(), (1, 1))
        objects = self.ledger_rows("objects")
        self.assertEqual(objects[0][:2], (digest, len(data)))
        self.assertTrue(objects[0][2])
        receipt = self.ledger_rows("receipts")[0]
        self.assertEqual(receipt[:3], (identity, digest, "synthetic-delivery-a"))
        self.assertTrue(receipt[3])
        self.assertEqual(receipt[4], source.name)
        for path, mode in ((stored, 0o600), (stored.parent, 0o700),
                           (stored.parent.parent, 0o700),
                           (self.root / "private" / "ledger.sqlite", 0o600)):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode, str(path))
        self.assertEqual({p.name for p in stored.parent.iterdir()}, {digest})

    def test_ingest_duplicate_bytes_preserve_distinct_delivery_occurrences(self):
        self.initialize()
        first = self.ingest(self.fixture("synthetic-first.txt"), "delivery-one")
        second = self.ingest(self.fixture("synthetic-second.txt"), "delivery-two")
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertNotEqual(first["receipt_id"], second["receipt_id"])
        self.assertTrue(first["new_receipt"])
        self.assertTrue(second["new_receipt"])
        self.assertEqual(self.counts(), (1, 2))
        receipts = self.ledger_rows("receipts")
        self.assertEqual({row[2] for row in receipts}, {"delivery-one", "delivery-two"})
        self.assertEqual({row[4] for row in receipts}, {"synthetic-first.txt", "synthetic-second.txt"})

    def test_ingest_same_delivery_is_idempotent_even_with_renamed_input(self):
        self.initialize()
        first = self.ingest(self.fixture("synthetic-original.txt"))
        before = self.ledger_rows("receipts")
        objects_before = self.ledger_rows("objects")
        second = self.ingest(self.fixture("synthetic-renamed.txt"))
        self.assertEqual(second, {**first, "new_receipt": False})
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(self.ledger_rows("receipts"), before)
        self.assertEqual(self.ledger_rows("objects"), objects_before)

    def test_ingest_changed_bytes_under_same_source_preserves_both_versions(self):
        self.initialize()
        source = self.fixture(data=b"Synthetic version one\n")
        first = self.ingest(source)
        source.write_bytes(b"Synthetic version two\n")
        second = self.ingest(source)
        self.assertNotEqual(first["sha256"], second["sha256"])
        self.assertNotEqual(first["receipt_id"], second["receipt_id"])
        self.assertTrue(second["new_receipt"])
        self.assertEqual(self.counts(), (2, 2))
        objects = self.root / "private" / "objects"
        self.assertEqual((objects / first["sha256"]).read_bytes(), b"Synthetic version one\n")
        self.assertEqual((objects / second["sha256"]).read_bytes(), b"Synthetic version two\n")

    def test_ingest_corrupt_stored_object_stops_without_receipt_or_overwrite(self):
        self.initialize()
        source = self.fixture()
        first = self.ingest(source)
        before = self.ledger_rows("receipts")
        objects_before = self.ledger_rows("objects")
        stored = self.root / "private" / "objects" / first["sha256"]
        stored.write_bytes(b"Synthetic deliberate corruption\n")
        result = self.run_cli(
            "ingest", "--file", source, "--source-id", "second-delivery", returncode=1,
        )
        self.assertIn("Stored object integrity mismatch", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(stored.read_bytes(), b"Synthetic deliberate corruption\n")
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(self.ledger_rows("receipts"), before)
        self.assertEqual(self.ledger_rows("objects"), objects_before)

    def test_ingest_rejects_symlinks_missing_files_and_directories(self):
        self.initialize()
        source = self.fixture()
        linked = self.base / "synthetic-link.txt"
        linked.symlink_to(source)
        dangling = self.base / "synthetic-dangling.txt"
        dangling.symlink_to(self.base / "absent-target")
        for path in (linked, dangling, self.base / "missing.txt", self.base):
            with self.subTest(path=path.name):
                result = self.run_cli(
                    "ingest", "--file", path, "--source-id", "synthetic-invalid", returncode=1,
                )
                self.assertIn("Input must be a regular, non-symlink file", result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertFalse((self.root / "private" / "ledger.sqlite").exists())
                self.assertFalse((self.root / "private" / "objects").exists())
        self.assertEqual(source.read_bytes(), b"Synthetic policy only.\n")


class StarterReviewTests(unittest.TestCase):
    def finding(self):
        return {
            "id": "synthetic-finding", "author": "synthetic-author",
            "classification": "documented_fact", "summary": "A synthetic policy was produced.",
            "sources": [{"sha256": hashlib.sha256(b"Synthetic policy only.\n").hexdigest(),
                         "locator": "page:1"}],
            "limitations": ["Synthetic fixture; not a real finding."], "counterevidence": [],
        }

    def receipts(self, finding):
        return [{
            "content_sha256": review.content_hash(finding), "decision": "approve",
            "reviewer": reviewer, "role": role, "rationale": "Synthetic structural check only.",
            "reviewed_at": "2026-01-01T00:00:00+00:00",
        } for role, reviewer in (("factual", "synthetic-reviewer-a"),
                                 ("legal", "synthetic-reviewer-b"),
                                 ("privacy", "synthetic-reviewer-a"))]

    def test_review_accepts_complete_content_bound_structural_receipts(self):
        finding = self.finding()
        receipts = self.receipts(finding)
        self.assertEqual(review.review_blockers(finding, receipts), [])
        reordered = dict(reversed(list(finding.items())))
        reordered["sources"] = [dict(reversed(list(finding["sources"][0].items())))]
        self.assertEqual(review.content_hash(reordered), review.content_hash(finding))
        self.assertEqual(review.review_blockers(reordered, receipts), [])
        for classification in ("documented_fact", "information_gap", "redaction",
                               "agency_assertion", "no_conflict"):
            with self.subTest(classification=classification):
                finding["classification"] = classification
                self.assertEqual(review.review_blockers(finding, self.receipts(finding)), [])

    def test_review_blocks_missing_evidence_fields_and_malformed_source_hashes(self):
        cases = []
        for field in ("id", "author", "summary", "sources", "limitations", "counterevidence"):
            finding = self.finding()
            del finding[field]
            cases.append(("absent-" + field, finding, "missing_" + field))
        for value in ("", " \t", None, 7):
            cases.append((f"summary-{value!r}", {**self.finding(), "summary": value}, "missing_summary"))
        for sources in ([], None):
            cases.append((f"evidence-{sources!r}", {**self.finding(), "sources": sources}, "missing_evidence"))
        for value in ("a" * 63, "a" * 65, "A" * 64, "g" * 64, "../synthetic", 123, ["a" * 64]):
            finding = self.finding()
            finding["sources"][0]["sha256"] = value
            cases.append((f"hash-{value!r}", finding, "invalid_source_hash"))
        for source in ({}, {"locator": "page:1"}, {"sha256": "a" * 64},
                       {"locator": "", "sha256": "a" * 64}, "synthetic-not-an-object"):
            cases.append((f"source-{source!r}", {**self.finding(), "sources": [source]},
                          "source_missing_locator_or_hash"))
        cases.append(("unknown-classification", {**self.finding(), "classification": "synthetic-unknown"},
                      "unknown_classification"))
        for label, finding, expected in cases:
            with self.subTest(case=label):
                # Invalid evidence must block even with receipts bound to that content.
                blockers = review.review_blockers(finding, self.receipts(finding))
                self.assertIn(expected, blockers)
                self.assertEqual(blockers, sorted(set(blockers)))

    def test_review_conflicts_require_event_date_rule_duty_and_exceptions(self):
        for classification in ("apparent_conflict", "confirmed_conflict"):
            complete = {**self.finding(), "classification": classification,
                        "event_date": "2025-12-01", "rule_version": "synthetic-rule-v1",
                        "duty": "Synthetic duty only", "exceptions": "Synthetic exception considered"}
            self.assertEqual(review.review_blockers(complete, self.receipts(complete)), [])
            for field in ("event_date", "rule_version", "duty", "exceptions"):
                for missing in (True, False):
                    with self.subTest(classification=classification, field=field, missing=missing):
                        finding = dict(complete)
                        if missing:
                            del finding[field]
                        else:
                            finding[field] = ""
                        self.assertEqual(review.review_blockers(finding, self.receipts(finding)),
                                         ["missing_" + field])

    def test_review_rejects_stale_incomplete_and_nonindependent_approvals(self):
        finding = self.finding()
        baseline = self.receipts(finding)
        cases = [("no-receipts", [], "need_two_independent_reviewers")]
        for role in ("factual", "legal", "privacy"):
            cases.append(("absent-" + role, [r for r in baseline if r["role"] != role],
                          "missing_" + role + "_review"))
        for field, value in (("reviewer", finding["author"]), ("reviewer", ""),
                             ("rationale", ""), ("reviewed_at", ""), ("decision", "pending"),
                             ("content_sha256", "malformed-hash"), ("role", "unknown-role")):
            changed = copy.deepcopy(baseline)
            changed[0][field] = value
            cases.append((f"invalid-{field}-{value}", changed, "missing_factual_review"))
        for field in ("content_sha256", "decision", "reviewer", "rationale", "reviewed_at", "role"):
            changed = copy.deepcopy(baseline)
            del changed[0][field]
            cases.append(("missing-" + field, changed, "missing_factual_review"))
        same_reviewer = [{**r, "reviewer": "synthetic-reviewer-a"} for r in baseline]
        cases.append(("one-reviewer-many-roles", same_reviewer, "need_two_independent_reviewers"))
        for label, receipts, expected in cases:
            with self.subTest(case=label):
                self.assertIn(expected, review.review_blockers(finding, receipts))
        changed_finding = {**finding, "summary": "Changed synthetic public statement."}
        self.assertNotEqual(review.content_hash(changed_finding), review.content_hash(finding))
        self.assertEqual(review.review_blockers(changed_finding, baseline), [
            "missing_factual_review", "missing_legal_review", "missing_privacy_review",
            "need_two_independent_reviewers",
        ])

    def test_review_current_challenges_block_but_stale_challenges_do_not(self):
        finding = self.finding()
        approvals = self.receipts(finding)
        for decision in ("reject", "changes_requested"):
            with self.subTest(decision=decision):
                challenge = {**approvals[0], "reviewer": "synthetic-challenger", "decision": decision}
                # Repeated approvals must not silently resolve a matching challenge.
                self.assertEqual(review.review_blockers(
                    finding, [challenge, challenge, *approvals, *approvals],
                ), ["unresolved_challenge"])
                stale_finding = {**finding, "summary": "Earlier synthetic wording."}
                stale = {**challenge, "content_sha256": review.content_hash(stale_finding)}
                self.assertEqual(review.review_blockers(finding, [*approvals, stale]), [])


if __name__ == "__main__":
    unittest.main()
