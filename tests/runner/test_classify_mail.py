"""classify_mail: synthetic .eml fixtures, attachment splitting, rules, model refinement."""
import io
import json
import unittest
from email.message import EmailMessage
from pathlib import Path

from campaign_tool.digest.model import ModelConfig
from runner.client import FakeWorkspace
from runner.handlers import classify_mail
from tests.runner.helpers import REPO, context, job, tempdir, tiny_pdf, tiny_xlsx

FIXTURES = REPO / "examples" / "synthetic-mail"


def production_eml():
    m = EmailMessage()
    m["From"] = "records@example.invalid"
    m["To"] = "requests@campaign.example.invalid"
    m["Subject"] = "Re: Request #2026-0042 responsive records"
    m["Date"] = "Wed, 30 Sep 2026 12:00:00 +0000"
    m["Message-ID"] = "<prod-0042@example.invalid>"
    m.set_content("FICTIONAL TRAINING MATERIAL. Attached are the responsive records for your request. Contact Officer Casey Fictional at 805-555-0100.")
    m.add_attachment(tiny_pdf(), maintype="application", subtype="pdf", filename="policy.pdf")
    m.add_attachment(tiny_xlsx(), maintype="application", subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename="search-log.xlsx")
    return m.as_bytes()


class ClassifyMailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempdir()

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, raw, **overrides):
        ws = FakeWorkspace()
        put = ws.put_original(raw, "message/rfc822")
        ws.correspondence.append({"correspondence_id": "cor_0000000000000001", "provider_message_id": "x", "raw_sha256": put["sha256"]})
        ctx = context(self.tmp.name, job("classify_mail", {"correspondence_id": "cor_0000000000000001", "raw_sha256": put["sha256"]}), ws, **overrides)
        return ws, classify_mail.run(ctx)

    def test_fixture_classifications(self):
        expected = {"acknowledgement": ("acknowledgement", "medium"), "extension": ("extension", "high"),
                    "fee_estimate": ("fee_estimate", "high"), "denial": ("denial", "high"), "unrelated": ("unrelated", "medium")}
        for name, (label, confidence) in expected.items():
            raw = (FIXTURES / f"{name}.eml.txt").read_bytes()
            ws, result = self.load(raw)
            self.assertEqual(result.status, "done", name)
            self.assertEqual(result.outputs["classification"], label, name)
            self.assertEqual(result.outputs["classification_confidence"], confidence, name)
            self.assertEqual(result.outputs["attachments"], [])
            self.assertEqual(result.outputs["followups"], [])
            self.assertLessEqual(len(result.outputs["summary"]), 300)
            self.assertNotIn("2026-0042", result.outputs["summary"])
            self.assertEqual(result.outputs["correspondence_update"]["classification"], label)
            self.assertEqual(ws.receipts, {})

    def test_production_splits_attachments_and_enqueues_extract(self):
        ws, result = self.load(production_eml())
        self.assertEqual(result.status, "done")
        self.assertEqual(result.outputs["classification"], "production")
        self.assertEqual(result.outputs["provider_message_id"], "prod-0042@example.invalid")
        atts = result.outputs["attachments"]
        self.assertEqual([a["original_name"] for a in atts], ["policy.pdf", "search-log.xlsx"])
        self.assertEqual(len(ws.originals), 3)  # raw + 2 attachments
        self.assertEqual(len(ws.receipts), 2)
        sources = sorted(r["source_id"] for r in ws.receipts.values())
        self.assertEqual(sources, ["prod-0042@example.invalid#1", "prod-0042@example.invalid#2"])
        followups = result.outputs["followups"]
        self.assertEqual([f["kind"] for f in followups], ["extract", "extract"])
        self.assertEqual(followups[0]["inputs"]["sha256"], atts[0]["sha256"])
        self.assertEqual(followups[0]["inputs"]["correspondence_id"], "cor_0000000000000001")
        self.assertEqual(len(set(f["idempotency_key"] for f in followups)), 2)
        summary = result.outputs["summary"]
        self.assertNotIn("Casey", summary)
        self.assertNotIn("805", summary)
        self.assertIn("2 attachment(s)", summary)
        # the stored attachment bytes are the decoded originals
        self.assertTrue(ws.originals[atts[0]["sha256"]].startswith(b"%PDF-"))

    def test_missing_message_id_uses_hash_source(self):
        m = EmailMessage()
        m["From"] = "records@example.invalid"
        m["Subject"] = "records"
        m.set_content("Attached.")
        m.add_attachment(b"plain bytes", maintype="application", subtype="octet-stream", filename="x.bin")
        ws, result = self.load(m.as_bytes())
        raw_sha = [s for s, mt in ws.media_types.items() if mt == "message/rfc822"][0]
        self.assertEqual(result.outputs["provider_message_id"], "sha256:" + raw_sha)
        self.assertEqual(result.outputs["classification"], "production")

    def test_rules_directly(self):
        self.assertEqual(classify_mail.classify("Re: request", "Records are partially produced; remaining records on a rolling basis", 1)[0], "partial_production")
        self.assertEqual(classify_mail.classify("Re: request", "Please clarify which records you seek; the request is overly broad", 0)[0], "clarification")
        self.assertEqual(classify_mail.classify("hello", "nothing relevant here", 0), ("unclassified", "low", []))

    def test_model_refinement_uses_redacted_text_and_survives_bad_json(self):
        seen = {}

        class Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def opener(request, timeout=0):
            body = json.loads(request.data.decode())
            seen["user"] = body["messages"][1]["content"]
            return Resp(json.dumps({"choices": [{"message": {"content": json.dumps({"classification": "production", "confidence": "high", "summary": "Records attached."})}}]}).encode())

        import campaign_tool.digest.model as model_mod
        original = model_mod.urllib.request.urlopen
        model_mod.urllib.request.urlopen = opener
        try:
            ws, result = self.load(production_eml(), model_base_url="http://127.0.0.1:9/v1")
        finally:
            model_mod.urllib.request.urlopen = original
        self.assertNotIn("Casey", seen["user"])
        self.assertIn("[NAME-1]", seen["user"])
        self.assertIn("[PHONE-1]", seen["user"])
        self.assertIn("Records attached.", result.outputs["summary"])

        def bad(request, timeout=0):
            return Resp(b"not json at all")
        model_mod.urllib.request.urlopen = bad
        try:
            ws, result = self.load(production_eml(), model_base_url="http://127.0.0.1:9/v1")
        finally:
            model_mod.urllib.request.urlopen = original
        self.assertEqual(result.status, "done")
        self.assertEqual(result.outputs["classification"], "production")

    def test_strict_local_refuses_remote_model(self):
        ws, result = self.load(production_eml(), model_base_url="https://api.example.invalid/v1", privacy_tier="strict_local")
        # refinement is skipped (ModelError logged), rules still classify
        self.assertEqual(result.status, "done")
        self.assertEqual(result.outputs["classification"], "production")

    def test_missing_inputs_fail(self):
        ctx = context(self.tmp.name, job("classify_mail", {}))
        self.assertEqual(classify_mail.run(ctx).status, "failed")


if __name__ == "__main__":
    unittest.main()
