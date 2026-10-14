"""One synthetic CPRA email travels preserve -> extract -> catalog -> detect -> review
(with independent challenge) -> compare -> privacy in a single command.

Runs in the default suite with no environment gating, against the real
installed adapters and one ledger. A fake loopback model server covers the
model path and proves that nothing unredacted crosses the model boundary.
"""
from email.message import EmailMessage
from email.policy import SMTP
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from campaign_tool.digest.model import ModelConfig
from campaign_tool.records import challenge, stage_adapters
from campaign_tool.records.run import Pipeline, status

REPO = Path(__file__).resolve().parents[2]
PLATE, OFFICER, PHONE = "7ABC123", "Deputy Marcus Quill", "805-555-0147"
POLICY = f"""FICTIONAL TRAINING MATERIAL - NOT A REAL AGENCY POLICY

Automated License Plate Reader Usage and Privacy Policy, version demo-2
Effective March 1, 2025.

1. Purpose. ALPR data may be used only for authorized purposes: locating stolen
vehicles and vehicles associated with an active investigation.

2. Authorized users. Access is limited to sworn personnel who have completed
training, for example {OFFICER}, reachable at {PHONE}.

3. Retention. ALPR detections that are not connected to an investigation are
retained for 90 days and then purged automatically. Example detection {PLATE}.

4. Sharing. Detections may be shared with other law enforcement agencies in
California. Out-of-state agencies and federal agencies may request a search
through the vendor's nationwide network; the requesting agency records the reason.

5. Audit. The system keeps an audit log of each query, including the user,
date, time and stated purpose. Supervisors review the audit log quarterly.
"""


def synthetic_email(body="Attached is the ALPR usage and privacy policy responsive to your request.\n",
                    attachment=POLICY.encode(), filename="alpr-policy.txt", message_id="<r42@agency.example.invalid>"):
    message = EmailMessage()
    message["From"] = "records@agency.example.invalid"
    message["To"] = "requests@campaign.example.invalid"
    message["Subject"] = "Response to records request 2026-0042"
    message["Date"] = "Tue, 15 Sep 2026 10:00:00 -0700"
    message["Message-ID"] = message_id
    message.set_content(body)
    if attachment is not None:
        message.add_attachment(attachment, maintype="text", subtype="plain", filename=filename)
    return message.as_bytes(policy=SMTP)


class FakeModelServer:
    def __init__(self, content):
        server = self
        server.content, server.requests = content, []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("content-length", 0))
                body = json.loads(self.rfile.read(length))
                server.requests.append(body)
                content = server.content(body) if callable(server.content) else server.content
                payload = json.dumps({"choices": [{"message": {"content": json.dumps(content)}}]}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class RunSliceTests(unittest.TestCase):
    def setUp(self):
        os.umask(0o077)
        self.tmp = tempfile.TemporaryDirectory(prefix="records-slice-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        os.chmod(self.base, 0o700)
        self.inbox = self.base / "inbox"
        self.inbox.mkdir(mode=0o700)
        (self.inbox / "response.eml").write_bytes(synthetic_email())
        self.root = self.base / "root"

    def ledger(self, sql):
        con = sqlite3.connect(self.root / "ledger.sqlite")
        try:
            return con.execute(sql).fetchall()
        finally:
            con.close()

    def assert_private_data_absent(self, text, where):
        for secret in (PLATE, OFFICER, PHONE, "Quill"):
            self.assertNotIn(secret, text, where + " leaked " + secret)

    def test_single_command_holds_automated_review_and_replays_cleanly(self):
        report = Pipeline(self.root).run(self.inbox)
        self.assertEqual(report["originals"], 2)
        for stage in ("preserve", "extract", "catalog", "detect"):
            self.assertEqual(report["counts"][stage]["done"], 2)
        self.assertEqual(report["counts"]["review"]["blocked"], 2)
        for stage in ("compare", "privacy"):
            self.assertEqual(report["counts"][stage]["done"], 0)
        self.assertEqual(report["end_to_end_complete"], 0)
        self.assertEqual(report["proposals_awaiting_owner"], 0)
        self.assertEqual(report["intake"]["failures"], [])
        self.assertEqual(self.ledger("SELECT count(*) FROM occurrences")[0][0], 2)
        self.assertEqual(self.ledger("SELECT count(*) FROM proposals")[0][0], 0)
        payloads = self.ledger("SELECT a.payload FROM stage_content c JOIN stage_artifacts a "
                               "ON a.sha256=c.content_sha256 WHERE c.stage='review'")
        self.assertEqual(len(payloads), 2)
        for row in payloads:
            content = json.loads(bytes(row[0]))
            progress = content["processing_coverage"]
            self.assertEqual(progress["summary"]["accepted_units"], 0)
            self.assertFalse(progress["model_emission_recorded"])
            self.assertEqual(progress["summary"]["receipt_coverage"]["scope"], "selected")
            record = content["challenge"]
            self.assertFalse(record["independent_review"])
            self.assertFalse(record["primary_evidence_inspected"])
            self.assertFalse(record["blind_first_pass"])
            self.assertIn("independent_factual_review_required", {d["reason"] for d in record["disputes"]})
            self.assert_private_data_absent(json.dumps(content["digest"]["statements"]), "digest statements")
        self.assertEqual(self.ledger("SELECT count(*) FROM receipts WHERE stage='review' AND verdict='pass'")[0][0], 0)
        again = Pipeline(self.root).run(self.inbox)
        self.assertEqual(again["originals"], 2)
        self.assertEqual(again["end_to_end_complete"], 0)
        self.assertEqual(again["proposals_awaiting_owner"], 0)
        self.assertEqual(self.ledger("SELECT count(*) FROM occurrences")[0][0], 2)
        self.assertFalse(status(self.root)["proposals"])

    def test_cli_entry_point_runs_the_slice(self):
        env = {**os.environ, "PYTHONPATH": str(REPO)}
        env.pop("MODEL_BASE_URL", None)
        env.pop("CHALLENGE_MODEL_BASE_URL", None)
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "run", "--root", str(self.root),
                                 "--inbox", str(self.inbox)], cwd=REPO, env=env, capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 3, result.stderr[-2000:])
        self.assertIn("complete through privacy: 0", result.stdout)
        self.assertIn("proposals awaiting owner: 0", result.stdout)
        shown = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "status", "--root", str(self.root)],
                               cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(shown.returncode, 0, shown.stderr[-2000:])
        self.assertEqual(json.loads(shown.stdout)["originals"], 2)

    def test_model_and_second_model_challenge_never_see_unredacted_text(self):
        narrative = {
            "scope": "Fictional ALPR policy", "actors": ["Example agency"],
            "dates": [{"date": "2025-03-01", "locator": {"unit": 3}, "note": "effective date"}],
            "statements": [{"text": "retained for 90 days and then purged automatically.", "locator": {"unit": 12}}],
            "omissions": [{"text": "No public hearing mentioned.", "locator": {"scope": "document"}}],
            "counterevidence": [],
            "conclusions": [{"text": "The policy states a 90-day retention period.", "confidence": "likely",
                             "sources": [{"locator": {"unit": 12}, "rule_id": None}]}],
        }
        minimal = {"scope": "Short transmittal email", "actors": [], "dates": [], "statements": [],
                   "omissions": [], "counterevidence": [],
                   "conclusions": [{"text": "The email only transmits an attachment.", "confidence": "likely",
                                    "sources": [{"locator": {"scope": "document"}, "rule_id": None}]}]}

        def answer(body):
            return narrative if "retained for 90 days" in json.dumps(body) else minimal

        primary = FakeModelServer(answer)
        self.addCleanup(primary.close)
        second = FakeModelServer({"disputes": [], "notes": "no issues"})
        self.addCleanup(second.close)
        model = ModelConfig(base_url=primary.url, model_id="local-primary", privacy_tier="strict_local")
        challenger = challenge.ChallengeConfig(ModelConfig(base_url=second.url, model_id="local-challenger",
                                                           privacy_tier="strict_local"), primary_model_id="local-primary")
        report = Pipeline(self.root, model=model, challenge=challenger).run(self.inbox)
        self.assertEqual(report["end_to_end_complete"], 0)
        self.assertEqual(report["proposals_awaiting_owner"], 0)
        self.assertEqual(report["counts"]["review"]["blocked"], 2)
        self.assertEqual(report["model_id"], "local-primary")
        self.assertEqual(report["challenge_model_id"], "local-challenger")
        self.assertGreaterEqual(len(primary.requests), 1)
        self.assertGreaterEqual(len(second.requests), 1)
        for request in primary.requests + second.requests:
            self.assert_private_data_absent(json.dumps(request), "model request")
        # The challenger receives sources before the digest.
        user_turns = [m["content"] for m in second.requests[0]["messages"] if m["role"] == "user"]
        self.assertTrue(user_turns[0].startswith("SOURCES"))
        self.assertTrue(user_turns[1].startswith("DIGEST"))
        rows = self.ledger("SELECT a.payload FROM stage_content c JOIN stage_artifacts a "
                           "ON a.sha256=c.content_sha256 WHERE c.stage='review'")
        for row in rows:
            content = json.loads(bytes(row[0]))
            self.assertEqual(content["digest"]["model_id"], "local-primary")
            self.assertEqual(content["challenge"]["model_check_verdict"], "pass")
            self.assertEqual(content["challenge"]["reviewer_id"], "challenge:model:local-challenger")
            self.assertFalse(content["challenge"]["independent_review"])
            self.assertIn("independent_factual_review_required",
                          {d["reason"] for d in content["challenge"]["disputes"]})
        self.assertEqual(self.ledger("SELECT count(*) FROM receipts WHERE stage='review' AND verdict='pass'")[0][0], 0)
        self.assertEqual(self.ledger("SELECT count(*) FROM stage_content WHERE stage='compare'")[0][0], 0)

    def test_same_model_challenge_requires_fresh_context_declaration(self):
        config = ModelConfig(base_url="http://127.0.0.1:1/v1", model_id="same")
        with self.assertRaisesRegex(ValueError, "fresh_context"):
            challenge.ChallengeConfig(config, primary_model_id="same")
        declared = challenge.ChallengeConfig(config, primary_model_id="same", fresh_context=True)
        self.assertIn("fresh-context", declared.reviewer_id)

    def test_primary_redaction_policy_is_forwarded_to_challenge_without_lifting_review_hold(self):
        phrase = "Synthetic Private Context"
        (self.inbox / "response.eml").write_bytes(synthetic_email(body=phrase + "\n"))
        pipeline = Pipeline(self.root, redaction_allowlist=["Example agency"], redaction_denylist=[phrase])
        with patch.object(challenge, "challenge_review", wraps=challenge.challenge_review) as checked:
            report = pipeline.run(self.inbox)
        self.assertEqual(checked.call_count, 2)
        for call in checked.call_args_list:
            self.assertEqual(call.kwargs["allowlist"], pipeline.allowlist)
            self.assertEqual(call.kwargs["denylist"], pipeline.denylist)
            self.assertNotIn(phrase, json.dumps(call.args[1]))
        self.assertEqual(report["counts"]["review"]["blocked"], 2)
        self.assertEqual(report["proposals_awaiting_owner"], 0)

    def test_cached_test_review_is_reopened_and_owner_approval_revoked_before_reuse(self):
        from campaign_tool.records import publish
        from campaign_tool.records.unattended import UnattendedPipeline
        from tests.records import preaccepted_publication
        for implementation in (Pipeline, UnattendedPipeline):
            with self.subTest(implementation=implementation.__name__):
                self.root = self.base / ("cached-" + implementation.__name__)
                proposal = preaccepted_publication.policy(Pipeline(self.root))
                publish.approve(self.root, proposal, owner_id="synthetic-owner")
                report = implementation(self.root).run(None)
                self.assertEqual(report["end_to_end_complete"], 0)
                self.assertEqual(report["counts"]["review"]["done"], 0)
                self.assertEqual(self.ledger("SELECT owner_approval,approved_content_sha256 FROM proposals"), [("none", None)])
                with self.assertRaisesRegex(publish.PublishError, "owner_approval_required"):
                    publish.publish(self.root, proposal, staging=self.base / "synthetic-staging")
                reasons = self.ledger("SELECT reason FROM stage_events WHERE stage='review' "
                                      "AND reason LIKE '%cached_automated_or_test_review%'")
                self.assertTrue(reasons)

    def test_disputed_review_blocks_the_stage_instead_of_passing(self):
        Pipeline(self.root).run(self.inbox)
        subject = self.ledger("SELECT original_sha256 FROM stage_state WHERE stage='review' AND status='blocked'")[0][0]
        pipeline = Pipeline(self.root)  # a fresh run, as a later scheduled run would be
        stage = pipeline._open_stage_run()
        self.addCleanup(pipeline._close_stage_run, {"synthetic_dispute": True})
        content = pipeline._content(subject, "review")
        digest = content["digest"]
        digest["statements"].append({"text": "The agency admitted deleting the logs.", "locator": {"line": 999}})
        units = pipeline._units(subject)
        record = challenge.challenge_review(digest, [{"locator": u["locator"], "text": u["text"]} for u in units],
                                            pipeline.package)
        self.assertEqual(record["verdict"], "challenge")
        self.assertTrue(any(d["reason"] == "locator_not_in_sources" for d in record["disputes"]))
        new_content = {**content, "digest": digest, "challenge": record}
        result = stage_adapters.promote(stage["runner"], subject, "review", new_content, author_id="records-digest",
                                        coverage={"denominator": {"kind": "items", "total": len(units)},
                                                  "covered": len(units), "scope": "full_text"},
                                        locators=[u["locator_text"] for u in units], challenges=[("factual", record)])
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["disputed"])
        state = self.ledger(f"SELECT status,reason FROM stage_state WHERE original_sha256='{subject}' AND stage='review'")[0]
        self.assertEqual(state[0], "blocked")
        self.assertIn("challenge_disputed", state[1])
        # Dependents were reopened: compare and privacy are no longer done for this subject.
        dependents = dict(self.ledger(f"SELECT stage,status FROM stage_state WHERE original_sha256='{subject}' AND stage IN ('compare','privacy')"))
        self.assertEqual(set(dependents.values()), {"pending"})

    def test_privacy_challenge_rejects_identifiers_and_private_hashes(self):
        bad = challenge.challenge_privacy("Deputy Marcus Quill queried 7ABC123 at " + "a" * 64)
        self.assertEqual(bad.get("verdict"), "challenge")
        reasons = {d["reason"] for d in bad["disputes"]}
        self.assertIn("identifier_in_public_text", reasons)
        self.assertIn("private_identity_or_placeholder_in_public_text", reasons)
        good = challenge.challenge_privacy("# Policy\n\nThe policy retains detections for 90 days.\n")
        self.assertEqual(good["verdict"], "pass")


if __name__ == "__main__":
    unittest.main()
