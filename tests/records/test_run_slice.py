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

    def test_single_command_reaches_privacy_and_replays_cleanly(self):
        report = Pipeline(self.root).run(self.inbox)
        self.assertEqual(report["originals"], 2)  # the email and its attachment
        for stage, counts in report["counts"].items():
            self.assertEqual(counts["done"], 2, stage + " not done for both originals: " + json.dumps(counts))
        self.assertEqual(report["end_to_end_complete"], 2)
        self.assertEqual(report["proposals_awaiting_owner"], 2)
        self.assertEqual(report["intake"]["failures"], [])
        receipts = self.ledger("SELECT count(*) FROM receipts")[0][0]
        self.assertEqual(receipts, 14)
        self.assertEqual(self.ledger("SELECT count(*) FROM occurrences")[0][0], 2)
        attempts = dict(self.ledger("SELECT outcome,count(*) FROM stage_attempts GROUP BY outcome"))
        self.assertEqual(attempts, {"accepted": 14})
        public = sorted((self.root / "proposals/public").glob("*.md"))
        self.assertEqual(len(public), 2)
        for path in public:
            text = path.read_text()
            self.assert_private_data_absent(text, path.name)
            self.assertNotRegex(text, r"[0-9a-f]{64}")
            self.assertIn("## Limitations", text)
        policy_proposal = next(p for p in public if "Usage and Privacy Policy" in p.read_text() or "retained for 90 days" in p.read_text())
        self.assertIn("needs attorney review", policy_proposal.read_text())
        self.assertIn("leginfo.legislature.ca.gov", policy_proposal.read_text())
        # The digest itself (private) was redacted before anything model-facing could see it.
        payloads = [bytes(row[0]) for row in self.ledger(
            "SELECT a.payload FROM stage_content c JOIN stage_artifacts a ON a.sha256=c.content_sha256 WHERE c.stage='review'")]
        digests = [json.loads(p)["digest"] for p in payloads]
        self.assertTrue(any(d["redaction_count"] >= 3 for d in digests), [d["redaction_counts"] for d in digests])
        for digest in digests:
            self.assert_private_data_absent(json.dumps(digest["statements"]), "digest statements")
            for conclusion in digest["conclusions"]:
                self.assertEqual(conclusion["confidence"], "needs_attorney_review")
        # Replay: the same message again, in a fresh process-independent pipeline object.
        again = Pipeline(self.root).run(self.inbox)
        self.assertEqual(again["intake"]["failures"], [])
        self.assertEqual(again["originals"], 2)
        self.assertEqual(again["end_to_end_complete"], 2)
        self.assertEqual(self.ledger("SELECT count(*) FROM receipts")[0][0], receipts)
        self.assertEqual(self.ledger("SELECT count(*) FROM occurrences")[0][0], 2)
        self.assertEqual(self.ledger("SELECT count(*) FROM proposals")[0][0], 2)
        snapshot = status(self.root)
        self.assertEqual(snapshot["end_to_end_complete"], 2)
        self.assertEqual(snapshot["blocked"], [])
        self.assertEqual({p["owner_approval"] for p in snapshot["proposals"]}, {"none"})

    def test_cli_entry_point_runs_the_slice(self):
        env = {**os.environ, "PYTHONPATH": str(REPO)}
        env.pop("MODEL_BASE_URL", None)
        env.pop("CHALLENGE_MODEL_BASE_URL", None)
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "run", "--root", str(self.root),
                                 "--inbox", str(self.inbox)], cwd=REPO, env=env, capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertIn("complete through privacy: 2", result.stdout)
        self.assertIn("proposals awaiting owner: 2", result.stdout)
        shown = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "status", "--root", str(self.root)],
                               cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(shown.returncode, 0, shown.stderr[-2000:])
        self.assertEqual(json.loads(shown.stdout)["originals"], 2)

    def test_model_and_second_model_challenge_never_see_unredacted_text(self):
        narrative = {
            "scope": "Fictional ALPR policy", "actors": ["Example agency"],
            "dates": [{"date": "2025-03-01", "locator": {"line": 4, "unit": 3}, "note": "effective date"}],
            "statements": [{"text": "retained for 90 days and then purged automatically.", "locator": {"line": 13, "unit": 12}}],
            "omissions": [{"text": "No public hearing mentioned.", "locator": {"scope": "document"}}],
            "counterevidence": [],
            "conclusions": [{"text": "The policy states a 90-day retention period.", "confidence": "likely",
                             "sources": [{"locator": {"line": 13, "unit": 12}, "rule_id": None}]}],
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
        self.assertEqual(report["end_to_end_complete"], 2)
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
        reviewers = {row[0] for row in self.ledger("SELECT reviewer_id FROM receipts WHERE stage='review'")}
        self.assertEqual(reviewers, {"challenge:model:local-challenger"})
        rows = self.ledger("SELECT a.payload FROM stage_content c JOIN stage_artifacts a ON a.sha256=c.content_sha256 WHERE c.stage='review'")
        self.assertTrue(all(json.loads(bytes(r[0]))["digest"]["model_id"] == "local-primary" for r in rows))
        # Legal rows keep the attorney label and carry the model's own conclusion as an observation.
        compares = [json.loads(bytes(r[0])) for r in self.ledger(
            "SELECT a.payload FROM stage_content c JOIN stage_artifacts a ON a.sha256=c.content_sha256 WHERE c.stage='compare'")]
        rows_with_rules = [row for content in compares for row in content.get("comparisons", [])]
        self.assertTrue(rows_with_rules)
        self.assertTrue(all(row["confidence"] == "needs_attorney_review" for row in rows_with_rules))
        self.assertTrue(all("model_observations" in row for row in rows_with_rules))

    def test_same_model_challenge_requires_fresh_context_declaration(self):
        config = ModelConfig(base_url="http://127.0.0.1:1/v1", model_id="same")
        with self.assertRaisesRegex(ValueError, "fresh_context"):
            challenge.ChallengeConfig(config, primary_model_id="same")
        declared = challenge.ChallengeConfig(config, primary_model_id="same", fresh_context=True)
        self.assertIn("fresh-context", declared.reviewer_id)

    def test_disputed_review_blocks_the_stage_instead_of_passing(self):
        Pipeline(self.root).run(self.inbox)
        subject = self.ledger("SELECT original_sha256 FROM stage_state WHERE stage='review' AND status='done'")[0][0]
        pipeline = Pipeline(self.root)  # a fresh run, as a later scheduled run would be
        stage = pipeline._open_stage_run()
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
