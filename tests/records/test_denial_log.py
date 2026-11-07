"""Synthetic refusal-log acceptance; all source trees remain unchanged."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records import cli, denial_log as dl, followup_state as fs


def fixtures():
    asks = [{"ask_id": f"ask-{n}", "text": "Synthetic policy " * 20, "rating": rating,
             "evidence": [{"item_id": f"i{n}", "page": n, "doc_sha256": "a" * 64,
                           "quote": "Policy withheld under Government Code section 9999."}]}
            for n, rating in enumerate(("withheld", "withheld", "exemption_cited", "answered", "no_records_stated"), 1)]
    req = {"agency_key": "alpha-pd", "request_id": "r1", "asks": asks}
    state = {"agency_key": "alpha-pd", "request_id": "r1", "item_needs": {"withheld": [1, 2, 3]},
             "evidence": {"events": [{"type": "denial", "date": "2026-11-04", "ids": {"item_id": "i1"}}]}}
    return {"run_id": "run1", "requests": [state]}, {"schema": "records-response-items-v1", "run_id": "run1",
            "requests": [req], "sources": [{"item_id": "i1", "received_at": "2026-11-05T01:00:00Z", "original_sha256": "b" * 64}]}


def queue(status="pending", **extra):
    return {"id": "q1", "kind": "appeal-denial", "agency_id": "alpha-pd", "request_id": "r1",
            "evidence": {"item_id": "i1"}, "status": status, "revision": 0, **extra}


class EntryTests(unittest.TestCase):
    def test_three_refusals_quotes_citations_dates_stable_ids_and_purity(self):
        states, doc = fixtures()
        before = copy.deepcopy((states, doc))
        rows = dl.entries(states, doc, [])
        self.assertEqual([r["kind"] for r in rows], ["withheld", "withheld", "exemption_cited"])
        self.assertEqual(rows[0]["date"], "2026-11-04")
        self.assertEqual(rows[0]["agency_words"], {"quote": doc["requests"][0]["asks"][0]["evidence"][0]["quote"], "page": 1, "doc_sha256": "b" * 64})
        self.assertEqual(rows[1]["agency_words"]["doc_sha256"], "a" * 64)
        self.assertEqual(rows[0]["agency_citation"], {"text": [rows[0]["agency_words"]["quote"]], "label": dl.CITATION})
        self.assertEqual(len(rows[0]["item_text"]), 160)
        self.assertEqual((states, doc), before)
        states["run_id"] = doc["run_id"] = "run2"
        self.assertEqual([r["id"] for r in rows], [r["id"] for r in dl.entries(states, doc, [])])
        doc["requests"][0]["asks"][0]["evidence"][0]["quote"] = "x" * 500
        self.assertEqual(len(dl.entries(states, doc, [])[0]["agency_words"]["quote"]), 400)

    def test_whole_denial_once_and_missing_quote(self):
        states, doc = fixtures()
        doc["requests"][0]["asks"] = []
        states["requests"][0]["evidence"]["events"] *= 2
        rows = dl.entries(states, doc, [queue()])
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["kind"], rows[0]["item_no"], rows[0]["reply"]), ("denied_whole", None, "drafted"))
        self.assertIsNone(rows[0]["agency_words"])
        self.assertEqual(rows[0]["agency_words_note"], "no quote extracted")
        states["requests"][0]["evidence"]["events"] = []
        self.assertEqual(dl.entries(states, doc, []), [])

    def test_reply_states_no_kind_only_coverage_or_ambiguous_sources(self):
        states, doc = fixtures()
        for status, expected in (("pending", "drafted"), ("approved", "approved"), ("rejected", "rejected"), ("sent", "sent"), ("cancelled", "not_drafted"), ("expired", "not_drafted")):
            self.assertEqual(dl.entries(states, doc, [queue(status)])[0]["reply"], expected)
        for q in (queue(evidence={}), queue(agency_id="beta-pd"), queue(request_id="other"), queue(kind="fee-reply")):
            self.assertEqual(dl.entries(states, doc, [q])[0]["reply"], "not_drafted")
        self.assertEqual(dl.entries(states, doc, [queue(), queue(id="q2")])[0]["reply"], "not_drafted")
        doc["requests"][0]["asks"][0]["evidence"].append({"item_id": "other", "quote": "Other", "page": 2})
        self.assertEqual(dl.entries(states, doc, [queue()])[0]["reply"], "not_drafted")

    def test_duplicate_and_stale_inputs_refused(self):
        states, doc = fixtures()
        with self.assertRaises(ValueError):
            dl.entries(states, doc, [queue(), queue()])
        doc["requests"][0]["asks"].append(doc["requests"][0]["asks"][0])
        with self.assertRaises(ValueError):
            dl.entries(states, doc, [])
        doc["run_id"] = "stale"
        with self.assertRaises(ValueError):
            dl.entries(states, doc, [])


class LoadTests(unittest.TestCase):
    def test_real_load_cli_golden_unavailable_and_preservation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            out = root / "run-outputs/run1/outcomes"
            out.mkdir(parents=True)
            _, doc = fixtures()
            req = doc["requests"][0]
            (out / "response-items.json").write_text(json.dumps(doc))
            timeline = {"schema": "records-timeline-v1", "run_id": "run1", "as_of": "2026-11-06", "requests": [
                {"agency_key": "alpha-pd", "request_id": "r1", "events": [{"type": "denial", "date": "2026-11-04", "source": {"item_id": "i1"}}]}]}
            (out / "timeline.json").write_text(json.dumps(timeline))
            (out / "received-items.json").write_text(json.dumps({"schema": "records-received-item-v1", "run_id": "run1", "items": doc["sources"]}))
            before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            expected = {"state": "available", "schema": "records-denial-log-v1", "run_id": "run1", "visibility": "private",
                        "review_required": True, "label": dl.LABEL, "entries": dl.entries(fs.classify_run(root, "run1"), doc, [])}
            stream = io.StringIO()
            with patch("sys.argv", ["records", "denials", "--project", temp, "--run-id", "run1", "--json"]), contextlib.redirect_stdout(stream):
                self.assertEqual(cli.main(), 0)
            self.assertEqual(json.loads(stream.getvalue()), expected)
            self.assertEqual(before, {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()})
            (out / "response-items.json").unlink()
            self.assertEqual(dl.load(root, "run1"), {"state": "unavailable", "code": "denial_log_source_missing"})
            self.assertFalse((root / "approvals").exists())
            self.assertEqual(dl.load(root, "../run1")["state"], "unavailable")
            doc["run_id"] = "stale"
            (out / "response-items.json").write_text(json.dumps(doc))
            self.assertEqual(dl.load(root, "run1")["state"], "unavailable")

    def test_sent_requires_current_revision_receipt(self):
        states, doc = fixtures()
        for revision, expected in ((0, "sent"), (1, "approved")):
            with patch.object(fs, "classify_run", return_value=states), patch.object(fs.received_items, "_safe_dir", return_value=Path("/synthetic")), patch.object(fs, "_read", side_effect=[doc, {"schema": "records-received-item-v1", "run_id": "run1", "items": []}]), patch.object(dl.approval_queue, "load_queue", return_value=[queue("approved")]), patch.object(dl.mail_send, "results", return_value=[{"queue_id": "q1", "revision": revision, "state": "sent"}]):
                self.assertEqual(dl.load("/synthetic", "run1")["entries"][0]["reply"], expected)
