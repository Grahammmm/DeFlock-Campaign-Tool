"""Synthetic Home decisions: urgency, metadata-only packets, degradation and HTTP routing."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from campaign_tool.records import approval_queue as aq, console_home as home, public_packets as pp
from campaign_tool.records import public_updates as pu
from tests.records import test_admin_panel as panel
from tests.records.test_console_board import build, put, RUN

NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)


def fixture(project):
    build(project)
    path = project / "run-outputs" / RUN / "outcomes/timeline.json"
    doc = json.loads(path.read_text())
    for request, days in zip(doc["requests"], (30, 1, None, None)):
        request["overdue_marks"] = [] if days is None else [{"kind": "statutory_date_passed", "label": "overdue",
            "date": (NOW - timedelta(days=days)).date().isoformat(), "basis": "recorded_receipt"}]
    put(path, doc)
    for n, (request, kind, hours) in enumerate((("R-1", "first-request", 2), ("R-2", "reply-extension", 50),
                                               ("R-9", "reminder-no-response", 200)), 1):
        with mock.patch.object(aq, "_now", return_value=(NOW - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")):
            aq.add_draft(project, item_id=f"{n:064x}", kind=kind, agency_id="alpha-pd", request_id=request,
                         body="BODY-SENTINEL-1 person@example.invalid", evidence={"why": "Recorded request"}, source_run_id=RUN)
    events = []
    for n, state in enumerate(("proposed", "holding")):
        line = {"id": str(n) * 16, "agency_key": "alpha-pd", "template": "L1", "text": "LINE-TEXT-SENTINEL",
                "event": {"type": "sent", "date": "2026-11-04"}}
        events.append({"schema": pu.SCHEMA, "event": "line", "id": line["id"], "ts": NOW.timestamp() - 3600, "line": line})
        if state == "holding":
            events.append({"schema": pu.SCHEMA, "event": "hold", "id": line["id"], "hold_until": NOW.timestamp() + 21600})
    (project / pu.JOURNAL).write_text("".join(json.dumps(e) + "\n" for e in events))
    packet = {"kind": "summary", "agency_key": "alpha-pd", "request_id": "R-1", "source": "fixture",
              "title": "Synthetic summary", "text": "PACKET-TEXT-SENTINEL-1", "documents": [{"sha256": "a" * 64,
              "date_received": "2026-10-20"}], "citations": []}
    pp.propose(project, packet, now=NOW.timestamp() - 13 * 86400)
    return packet


class Home(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        self.packet = fixture(self.project)

    def model(self, **kwargs):
        return home.load(self.project, RUN, now=NOW, **kwargs)

    def test_urgency_counts_links_metadata_and_privacy(self):
        model = self.model()
        rows = home.decision_rows(model, NOW.date())
        queue = [r for r in rows if r["source"] == "queue"]
        self.assertEqual([r["age_hours"] for r in queue], [2, 50, 200])
        self.assertEqual([r["overdue_days"] for r in queue], [30, 1, None])
        holding = next(r for r in rows if r["kind"] == "holding")
        self.assertLess(rows.index(queue[1]), rows.index(holding))
        self.assertLess(rows.index(holding), rows.index(queue[2]))
        for row in rows:
            self.assertTrue(all(row[k] not in (None, "") for k in ("agency", "what", "why", "age_hours", "link")))
        page = home.render_home(model, "unused")
        for text in ("Requests 1 · Follow-ups 2 · Sends 0", "Status lines 2 · Packets 1", "overdue 30 days since 2026-10-06",
                     "not a legal finding", "CON-SITEPR-1", "CON-REDACT-1", "CON-FIND-1", 'id="standing"', 'id="outstanding"'):
            self.assertIn(text, page)
        for text in ("BODY-SENTINEL-1", "person@example.invalid", "PACKET-TEXT-SENTINEL-1", "LINE-TEXT-SENTINEL", "@", "<form"):
            self.assertNotIn(text, page)

    def test_sends_revision_matching_next_kinds_and_unknown_outcome(self):
        model = self.model(send_config="not-read")
        approved = dict(model["queue"]["rows"][0], status="approved", revision=2)
        model["queue"]["rows"].append(approved | {"id": "approved"})
        for kind in home.NEXT:
            model["queue"]["rows"].append(approved | {"id": kind, "kind": kind, "status": "pending"})
        for state, revision, count in (("sent", 2, 0), ("sent", 1, 1), ("unknown", 2, 1), ("attempt", 2, 1)):
            model["sends"]["rows"] = [{"queue_id": "approved", "revision": revision, "state": state}]
            rows = home.decision_rows(model, NOW.date())
            self.assertEqual(sum(r["kind_group"] == "send" for r in rows), count)
            if state in ("unknown", "attempt"):
                self.assertIn("reconcile", next(r["why"] for r in rows if r["kind_group"] == "send"))
        model["sends"]["configured"] = False
        self.assertFalse(any(r["kind_group"] == "send" for r in home.decision_rows(model, NOW.date())))
        for kind, (group, anchor) in home.NEXT.items():
            row = next(r for r in rows if r["kind"] == kind)
            self.assertEqual((row["kind_group"], row["link"]), (group, "/next-steps#" + anchor))

    def test_pending_exact_keys_readonly_and_legacy_status(self):
        path = self.project / pp.JOURNAL
        before = path.read_bytes()
        legacy = pp.status(self.project, NOW.timestamp())
        rows = pp.pending(self.project, NOW.timestamp())
        self.assertEqual(set(rows[0]), set("id revision status kind agency_key request_id source title proposed_at age_hours expires_at hours_to_expiry approved_at signed_by".split()))
        self.assertEqual((rows[0]["request_id"], rows[0]["source"], rows[0]["age_hours"], rows[0]["hours_to_expiry"]), ("R-1", "fixture", 312, 24))
        self.assertEqual((rows[0]["proposed_at"], rows[0]["expires_at"]), ("2026-10-23T00:00:00Z", "2026-11-06T00:00:00Z"))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(pp.status(self.project, NOW.timestamp()), legacy)
        self.assertEqual(set(legacy["packets"][0]), set("id revision status kind agency_key title".split()))
        self.assertEqual(pp.propose(self.project, self.packet, now=NOW.timestamp())["result"], "duplicate")
        self.assertEqual(len(pp.pending(self.project, NOW.timestamp())), 1)
        self.assertEqual(pp.pending(self.project, NOW.timestamp() + 2 * 86400), [])
        with pp._Locked(self.project) as lock:
            lock.append("approved", NOW.timestamp(), id=rows[0]["id"], revision=0, principal="synthetic", signature="fixture")
        approved = pp.pending(self.project, NOW.timestamp())[0]
        self.assertEqual((approved["status"], approved["signed_by"], approved["approved_at"]),
                         ("approved", "synthetic", "2026-11-05T00:00:00Z"))
        self.assertFalse(any(r["source"] == "packets" for r in home.decision_rows(self.model(), NOW.date())))
        for state in ("rejected", "cancelled", "written"):
            path.write_bytes(before)
            with pp._Locked(self.project) as lock:
                if state == "written":
                    lock.append("approved", NOW.timestamp(), id=rows[0]["id"], revision=0, principal="synthetic", signature="fixture")
                lock.append(state, NOW.timestamp(), id=rows[0]["id"], revision=0, file="fixture", file_sha256="a" * 64)
            self.assertEqual(pp.pending(self.project, NOW.timestamp()), [])
        path.write_bytes(before)
        pp.propose(self.project, self.packet | {"title": "New revision"}, now=NOW.timestamp())
        with pp._Locked(self.project) as lock:
            lock.append("approved", NOW.timestamp(), id=rows[0]["id"], revision=0, principal="stale", signature="fixture")
        self.assertEqual([(r["revision"], r["status"]) for r in pp.pending(self.project, NOW.timestamp())], [(1, "proposed")])

    def test_missing_invalid_and_independent_sources(self):
        path = self.project / pp.JOURNAL
        path.unlink()
        self.assertEqual(pp.pending(self.project), [])
        self.assertEqual(pp.status(self.project), {"switch": "OFF", "packets": []})
        self.assertFalse(path.exists())
        page = home.render_home(self.model(), "")
        self.assertIn("No packets pending", page)
        self.assertNotIn("packets: unavailable", page)
        path.write_bytes(b"not-json\n")
        with self.assertRaisesRegex(pp.PacketError, "journal_invalid"):
            pp.pending(self.project)
        self.assertIn("packets: unavailable (journal_invalid)", home.render_home(self.model(), ""))
        for source in ("timeline", "queue", "public", "packets", "sends"):
            model = self.model()
            model[source] = {"state": "unavailable", "code": "synthetic_bad"}
            page = home.render_home(model, "")
            self.assertIn(source + ": unavailable (synthetic_bad)", page)
            self.assertIn("Needs your decision", page)
        with mock.patch.object(home.mail_send, "results", side_effect=OSError):
            self.assertEqual(self.model(send_config="not-read")["sends"]["state"], "unavailable")
        (self.project / "approvals" / home.mail_send.RESULTS).write_bytes(b"invalid\n")
        self.assertEqual(self.model(send_config="not-read")["sends"]["state"], "unavailable")
        (self.project / pu.JOURNAL).write_bytes(b"invalid\n")
        self.assertEqual(self.model()["public"]["state"], "unavailable")
        (self.project / "approvals" / "queue.jsonl").write_bytes(b"invalid\n")
        self.assertEqual(self.model()["queue"]["state"], "unavailable")
        timeline = self.project / "run-outputs" / RUN / "outcomes/timeline.json"
        doc = json.loads(timeline.read_text())
        doc["run_id"] = "stale"
        put(timeline, doc)
        model = self.model()
        self.assertEqual(model["timeline"]["code"], "run_mismatch")
        self.assertTrue(all(r["overdue_days"] is None for r in home.decision_rows(model, NOW.date())))


class Routes(panel.ServerCase):
    def test_home_dashboard_and_reject_return(self):
        get = lambda path: self.http.send("GET", path, headers={"Content-Length": None, "Content-Type": None})
        status, _, page = get("/")
        self.assertEqual(status, 200)
        self.assertIn("Needs your decision", page)
        self.assertIn(self.draft_row["id"], get("/dashboard")[2])
        with mock.patch.object(panel.ap, "load_dashboard", return_value={"synthetic": True}):
            with mock.patch.object(panel.ap, "render_dashboard", return_value="old dashboard bytes"):
                self.assertEqual(get("/dashboard")[2], "old dashboard bytes")
        status, headers, _ = self.http.post("/approvals/reject", {"csrf_token": self.tok, "id": self.draft_row["id"], "reason": "not needed"})
        self.assertEqual((status, headers["location"]), (303, "/"))
        self.assertIn("Follow-ups 0", get("/")[2])
        (self.project / pp.JOURNAL).write_bytes(b"invalid\n")
        self.assertEqual(get("/")[0], 200)
        self.assertIn("packets: unavailable (journal_invalid)", get("/")[2])
