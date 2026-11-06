"""Manual-event form acceptance: synthetic project, real loopback HTTP and journal."""
import http.client
import json
import os
import tempfile
import threading
import unittest
import urllib.parse
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from campaign_tool.records import admin_panel as ap
from campaign_tool.records import console_board as cb
from campaign_tool.records import console_manual_events as cm
from campaign_tool.records.outcomes import timeline as tl

RUN = "run_1"


class Inputs(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.fields = {}
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("type") == "hidden":
            self.fields[attrs["name"]] = attrs["value"]


def setup(project, agency="agency-one", request="REF-1"):
    run = project / "run-outputs" / RUN
    run.mkdir(parents=True)
    register = {"as_of": "2026-11-05", "requests": [
        {"agency": agency, "agency_key": agency, "request_id": request,
         "ledger_request_id": request, "deadline": {}, "status": "open", "issues": []}]}
    (run / "CURRENT-REQUEST-REGISTER.json").write_text(json.dumps(register))
    tl.write_run_timeline(project, RUN)


class ManualEvents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        setup(self.project)
        self.server = ap.build_server(self.project, RUN, actor="Operator <one>", system=False)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.origin = "http://127.0.0.1:%d" % self.server.server_port
        self.journal = self.project / "manual-events.jsonl"

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method, path, fields=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            body = urllib.parse.urlencode(fields) if fields is not None else None
            headers = {"Origin": self.origin, "Content-Type": "application/x-www-form-urlencoded", **(headers or {})}
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            return response.status, response.read().decode(), dict(response.getheaders())
        finally:
            connection.close()

    def form(self):
        status, page, _ = self.request("GET", "/manual-event?agency=agency-one&request=REF-1")
        self.assertEqual(status, 200)
        fields = Inputs(page).fields
        return fields | {"type": "portal check", "date": "2026-11-05", "summary": "Portal checked <private>", "corrects": ""}

    def test_three_types_append_actor_and_immediate_timeline_no_rewrite(self):
        previous = b""
        for kind in cm.TYPES:
            fields = self.form() | {"type": kind}
            status, _, headers = self.request("POST", "/manual-event", fields)
            self.assertEqual(status, 303)
            self.assertEqual(headers["Location"], "/agency?agency=agency-one")
            raw = self.journal.read_bytes()
            self.assertTrue(raw.startswith(previous))
            previous = raw
        events = tl.read_manual_events(self.journal)
        self.assertEqual([e["type"] for e in events], list(cm.TYPES))
        self.assertTrue(all(e["actor"] == "Operator <one>" for e in events))
        status, page, _ = self.request("GET", "/agency?agency=agency-one")
        self.assertEqual(status, 200)
        self.assertIn("Portal checked &lt;private&gt;", page)
        self.assertIn("by Operator &lt;one&gt;", page)
        self.assertIn("hand note", page)
        notes = [e for e in cb.load(self.project, RUN)["timeline"]["doc"]["requests"][0]["events"] if e["type"] == "hand note"]
        self.assertIsNone(cb._last_contact({"events": notes}, date(2026, 11, 5)))
        self.assertEqual(self.journal.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.project / "approvals" / "queue.jsonl").exists())
        self.assertFalse((self.project / "processing-v1").exists())

    def test_cancel_and_unknown_target_write_nothing(self):
        fields = self.form()
        self.assertIn("ticket", fields)
        self.assertFalse(self.journal.exists())  # Cancel is navigation, not a mutation.
        self.request("GET", "/agency?agency=agency-one")
        self.assertFalse(self.journal.exists())
        self.assertEqual(self.request("GET", "/manual-event?agency=other&request=REF-1")[0], 404)
        self.assertFalse(self.journal.exists())

    def test_duplicate_retry_is_idempotent_and_changed_retry_conflicts(self):
        fields = self.form()
        self.assertEqual(self.request("POST", "/manual-event", fields)[0], 303)
        original = self.journal.read_bytes()
        self.assertEqual(self.request("POST", "/manual-event", fields)[0], 303)
        self.assertEqual(self.journal.read_bytes(), original)
        self.assertEqual(self.request("POST", "/manual-event", fields | {"summary": "changed"})[0], 409)
        self.assertEqual(self.journal.read_bytes(), original)

    def test_corrections_append_and_preserve_original(self):
        first = self.form()
        self.assertEqual(self.request("POST", "/manual-event", first)[0], 303)
        original = self.journal.read_bytes()
        correction = self.form() | {"corrects": first["id"], "summary": "Corrected note"}
        self.assertEqual(self.request("POST", "/manual-event", correction)[0], 303)
        self.assertTrue(self.journal.read_bytes().startswith(original))
        page = cb.render_agency(cb.load(self.project, RUN), "agency-one")
        self.assertIn("superseded by a correction", page)
        self.assertIn("Corrected note", page)
        self.assertEqual(self.request("POST", "/manual-event", self.form() | {"corrects": "absent"})[0], 409)

    def test_expired_stale_and_tampered_forms_refuse_without_append(self):
        fields = self.form()
        with patch.object(cm.time, "time", return_value=cm.time.time() + cm.TTL + 2):
            self.assertEqual(self.request("POST", "/manual-event", fields)[0], 410)
        self.assertEqual(self.request("POST", "/manual-event", fields | {"ticket": fields["ticket"] + "x"})[0], 403)
        self.assertEqual(self.request("POST", "/manual-event", fields | {"ticket": fields["ticket"] + "é"})[0], 403)
        payload = fields["ticket"].rpartition(".")[0]
        forged = payload + "." + cm._sign(payload, fields["csrf_token"])
        self.assertEqual(self.request("POST", "/manual-event", fields | {"ticket": forged})[0], 403)
        register = self.project / "run-outputs" / RUN / "CURRENT-REQUEST-REGISTER.json"
        register.write_text(register.read_text() + "\n")
        self.assertEqual(self.request("POST", "/manual-event", fields)[0], 409)
        self.assertFalse(self.journal.exists())

    def test_invalid_fields_and_existing_http_guards(self):
        fields = self.form()
        cases = [({"csrf_token": "wrong"}, 403), ({"actor": "spoof"}, 400),
                 ({"date": "2026-02-30"}, 400), ({"date": "2026-11-05extra"}, 400),
                 ({"type": "sent"}, 400), ({"summary": " "}, 400),
                 ({"summary": "x" * 2001}, 400), ({"summary": "line\nbreak"}, 400)]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                self.assertEqual(self.request("POST", "/manual-event", fields | changes)[0], expected)
        for headers in ({"Origin": "http://evil.test"}, {"Host": "evil.test"}):
            self.assertEqual(self.request("POST", "/manual-event", fields, headers)[0], 403)
        self.assertEqual(self.request("DELETE", "/manual-event")[0], 405)
        self.assertFalse(self.journal.exists())

    def test_unsafe_or_corrupt_journal_refuses_and_preserves_bytes(self):
        fields = self.form()
        for raw in (b'{"partial":', b'{}\n'):
            self.journal.write_bytes(raw)
            os.chmod(self.journal, 0o600)
            self.assertEqual(self.request("POST", "/manual-event", fields)[0], 503)
            self.assertEqual(self.journal.read_bytes(), raw)
        self.journal.unlink()
        target = self.project / "untouched"
        target.write_bytes(b"unchanged")
        self.journal.symlink_to(target)
        self.assertEqual(self.request("POST", "/manual-event", fields)[0], 503)
        self.assertEqual(target.read_bytes(), b"unchanged")

    def test_refresh_failure_retry_never_duplicates_durable_event(self):
        fields = self.form()
        with patch.object(tl, "write_run_timeline", side_effect=OSError("unavailable")):
            self.assertEqual(self.request("POST", "/manual-event", fields)[0], 503)
        self.assertEqual(len(tl.read_manual_events(self.journal)), 1)
        self.assertEqual(self.request("POST", "/manual-event", fields)[0], 303)
        self.assertEqual(len(tl.read_manual_events(self.journal)), 1)


if __name__ == "__main__":
    unittest.main()
