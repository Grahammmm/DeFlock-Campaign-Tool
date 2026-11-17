"""Regressions for portable agency draft exports using synthetic inputs."""
import hashlib
import json
from pathlib import Path

from campaign_tool.records import agency_draft_export as ade
from tests.records.test_agency_draft_export import MD, entry, mapping
from tests.records.test_mail_drafts import Base


class ExportRegressionTests(Base):
    def export(self, drafts, markdown=MD):
        md = self.root / "draft.md"
        md.write_bytes(markdown.encode("utf-8"))
        source = self.root / "mapping.json"
        source.write_text(json.dumps(mapping(*drafts)), encoding="utf-8")
        return ade.export(str(md), str(source), str(self.project), str(self.root / "out"))

    def test_slash_key_refused_and_following_draft_exported(self):
        result = self.export([entry("valid/key"), entry("ordinary")])
        self.assertEqual(result["refused"], [{"draft_key": "valid/key", "code": "draft_key_filename_invalid"}])
        self.assertEqual([Path(path).name for path in result["files"]], ["ordinary.json"])
        self.assertEqual(json.loads(Path(result["files"][0]).read_text())["draft_key"], "ordinary")

    def test_crlf_subject_body_and_raw_digest(self):
        markdown = MD.replace("\n", "\r\n")
        result = self.export([entry()], markdown)
        self.assertTrue(result["ok"], result)
        doc = json.loads(Path(result["files"][0]).read_text())
        self.assertEqual(doc["envelope"]["subject"], "Records follow-up")
        self.assertEqual(doc["body"], "Dear Records Custodian,\n\n1. Please supply the records. Café.\n2. [EDIT ME: date]\n")
        self.assertEqual(doc["source_attachment_sha256"], hashlib.sha256(markdown.encode()).hexdigest())
