"""Console calendar (CON-CAL-1) on synthetic run outputs: invented agencies, example.test hosts."""
import copy
import json
import os
import re
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from campaign_tool.records import console_calendar as cc
from campaign_tool.records import followup_clock as fc
from campaign_tool.records.outcomes import timeline as tl

RUN = "run_1"
TODAY = date(2026, 11, 5)
NOW = datetime(2026, 11, 5, 18, 30, 5, tzinfo=timezone.utc)
SENTINEL = "SENTINEL-BODY-TEXT"
LONG = "Beta Sheriff and Coroner Département of Invented Services for Calendar Folding"


def clock(**kw):
    return {"state": "running", "basis": "recorded_receipt", "base_due": "2026-11-09", "extended_due": "2026-11-23",
            "due": "2026-11-09", "recorded_due": None, "determination_at": None, "extension_recorded": False, **kw}


def row(agency, request, deadline=None, name=None, promised=None, **kw):
    return {"agency": name or agency.upper(), "agency_key": agency, "request_id": request, "ledger_request_id": "L-" + request,
            "deadline": deadline or clock(), "promised": promised or {"state": "none", "date": None}, "next_action": "wait",
            "status": "open", "issues": [], "production_at": None, "asked": "x", "request_date": "2026-10-01",
            "sources": [], **kw}


def put(path, doc, raw=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw if raw is not None else json.dumps(doc))
    os.chmod(path, 0o600)


def register(rows):
    return {"schema": "records-request-register-v1", "as_of": TODAY.isoformat(), "requests": rows, "limits": []}


def settings_doc(**agencies):
    return {"schema": fc.SETTINGS_SCHEMA, "agencies": {k: {"autonomy": v} for k, v in agencies.items()}}


def build(project, rows, manual=(), links=(), drop=()):
    out = project / "run-outputs" / RUN
    docs = {"outcomes/timeline.json": tl.build_timeline(register(rows), {"items": []}, [], [], list(manual), run_id=RUN),
            "outcomes/release-links.json": {"schema": "records-release-links-v1", "run_id": RUN, "links": list(links)},
            fc.REGISTER_NAME: register(rows)}
    for name, doc in docs.items():
        if name not in drop:
            put(out / name, doc)
    put(project / fc.SETTINGS_FILE, settings_doc(**{"alpha-pd": "draft", "beta-so": "off"}))


def link(day, basis="stated_date", inferred=False, host="files.example.test"):
    expiry = {"date": day, "basis": basis, "year_inferred": inferred} if day else {}
    return {"link_id": "L" * 64, "host": host, "platform": "x", "url_redacted": "https://files.example.test/" + SENTINEL,
            "agency_ids": ["alpha-pd"], "sources": [], "expiry": expiry, "expiry_state": "stated" if day else "none"}


def manual(event, request, kind, day, new, agency="alpha-pd"):
    return {"event_id": event, "agency_id": agency, "request_id": request, "type": kind, "date": day, "actor": "G",
            "summary": SENTINEL, "new_date": new}


def stub(reg, rows=(), settings=None):
    return {"run_id": RUN, "now": NOW, "timeline": {"state": "ok", "doc": {"requests": []}},
            "links": {"state": "ok", "doc": {"links": []}}, "public": {"state": "ok", "lines": []},
            "packets": {"state": "ok", "rows": []}, "queue": {"state": "ok", "rows": list(rows)},
            "followup": {"state": "ok", "register": reg, "settings": settings or fc.load_settings(Path("/nonexistent-x"))}}


def snapshot(root):
    return {str(p): p.read_bytes() for p in sorted(Path(root).rglob("*")) if p.is_file() and not p.name.endswith(".lock")}


class Calendar(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.project = Path(tmp.name)

    def model(self, rows, **kw):
        build(self.project, rows, **kw)
        return cc.load(str(self.project), RUN, NOW)

    def test_each_kind_with_basis_source_and_month_day(self):
        rows = [row("alpha-pd", "R-1"),
                row("beta-so", "R-2", clock(basis="submission_proxy", base_due="2026-11-12", extended_due="2026-11-26",
                                            due="2026-11-12"), name=LONG),
                row("beta-so", "R-3", clock(state="unknown", basis="unknown", base_due=None, extended_due=None, due=None)),
                row("alpha-pd", "R-4", clock(state="past_due_reminder", due="2026-10-20", base_due="2026-10-20",
                                             extended_due="2026-11-03"))]
        notices = [manual("m1", "R-1", "promised date", "2026-11-01", "2026-11-20"),
                   manual("m2", "R-2", "extension notice", "2026-11-02", "2026-11-18", "beta-so"),
                   manual("m3", "R-2", "extension notice", "2026-11-03", "2026-11-25", "beta-so")]
        model = self.model(rows, manual=notices, links=[link("2026-11-17"), link("2026-11-27", inferred=True), link(None)])
        model["public"] = {"state": "ok", "lines": [{"state": "holding", "hold_until": int(datetime(2026, 11, 8, 9, tzinfo=timezone.utc).timestamp()),
                                                     "agency_key": "alpha-pd"}]}
        model["packets"] = {"state": "ok", "rows": [{"kind": "documents", "status": "proposed", "agency_key": "alpha-pd",
                                                      "request_id": "R-1", "expires_at": "2026-11-15T10:00:00Z"}]}
        before = snapshot(self.project)
        with mock.patch.object(fc, "queue_followups", side_effect=AssertionError("must not queue")):
            items, notes = cc.build(model, TODAY)
            html = cc.render(model, TODAY, date(2026, 11, 1))
        self.assertEqual(snapshot(self.project), before)
        got = {(e["kind"], e["date"].isoformat(), e["request"]): e for e in items}
        want = {("statutory_due", "2026-11-09", "R-1"): "DATED", ("extension_limit", "2026-11-23", "R-1"): "DATED",
                ("statutory_due", "2026-11-12", "R-2"): "INFERRED", ("expected_window", "2026-11-20", "R-1"): "DATED",
                ("expected_window", "2026-11-18", "R-2"): "DATED", ("expected_window", "2026-11-25", "R-2"): "DATED",
                ("overdue", "2026-10-20", "R-4"): "DATED", ("link_expiry", "2026-11-17", ""): "DATED",
                ("link_expiry", "2026-11-27", ""): "INFERRED", ("publication_pending", "2026-11-08", ""): "DATED",
                ("publication_pending", "2026-11-15", "R-1"): "INFERRED", ("followup_draft_due", "2026-11-10", "R-1"): "INFERRED"}
        for key, basis in want.items():
            self.assertIn(key, got)
            self.assertEqual(got[key]["basis"], basis, key)
            self.assertTrue(got[key]["source"], key)
        self.assertFalse([k for k in got if k[2] == "R-3"], "unknown clock yields no entry")
        self.assertIn("no clock: 1 open requests", " ".join(notes))
        self.assertIn("no stated expiry for 1 release links", " ".join(notes))
        self.assertIn("follow-up drafting is off for this agency", " ".join(notes))
        self.assertRegex(html, r"<td>17<br>Link expiry")
        self.assertRegex(html, r"<td>8<br>Publication step")
        self.assertIn("draft only, sends after your approval", html)
        self.assertLess(html.index("OVERDUE"), html.index("2026-11-09 &mdash;"))
        self.model_ics(model)

    def model_ics(self, model):
        items = cc.entries(model, TODAY)
        text = cc.to_ics(items, NOW)
        self.assertTrue(text.endswith("\r\n") and "\n" not in text.replace("\r\n", ""))
        physical = text.split("\r\n")[:-1]
        self.assertTrue(all(len(p.encode()) <= 75 for p in physical))
        lines = []
        for p in physical:
            lines.append(lines.pop() + p[1:] if p.startswith(" ") else p)
        self.assertEqual([lines.count(x) for x in ("BEGIN:VCALENDAR", "VERSION:2.0")], [1, 1])
        self.assertEqual(sum(1 for x in lines if x.startswith("PRODID:")), 1)
        events = [[] for x in lines if x == "BEGIN:VEVENT"]
        self.assertEqual(len(events), len(items))
        idx = -1
        for x in lines:
            idx += x == "BEGIN:VEVENT"
            if idx >= 0 and ":" in x and x not in ("BEGIN:VEVENT", "END:VEVENT", "END:VCALENDAR"):
                events[idx].append(x)
        for ev in events:
            names = [x.split(":", 1)[0] for x in ev]
            self.assertEqual(sorted(names), sorted(["UID", "DTSTAMP", "DTSTART;VALUE=DATE", "SUMMARY", "DESCRIPTION"]))
            value = {x.split(":", 1)[0]: x.split(":", 1)[1] for x in ev}
            self.assertRegex(value["UID"], r"^[0-9a-f]{32}@calendar\.deflock\.invalid$")
            self.assertEqual(value["DTSTAMP"], "20261105T183005Z")
            self.assertRegex(value["DTSTART;VALUE=DATE"], r"^\d{8}$")
            for field in ("SUMMARY", "DESCRIPTION"):
                self.assertNotRegex(value[field], r"@|http|[0-9a-f]{32}")
                self.assertNotIn(SENTINEL, value[field])
        self.assertEqual(len({e[0] for e in events}), len(events), "UIDs are unique")
        self.assertNotRegex(text, r"http|[0-9a-f]{64}")
        self.assertNotIn(SENTINEL, text)
        self.assertEqual(text.count("@"), len(items))
        self.assertIn("é", text.replace("\r\n ", ""))

    def test_followup_projection_matches_planner(self):
        d = lambda n: (TODAY + timedelta(days=n)).isoformat()
        e = row("alpha-pd", "E", clock(due=d(5)))
        e2 = row("alpha-pd", "E2", clock(state="determined", determination_at="2026-11-01", due=d(5)),
                 promised={"state": "upcoming", "date": d(7)})
        c = {**copy.deepcopy(e), "request_id": "C", "status": "closed"}
        dd = row("alpha-pd", "D", clock(state="determined", determination_at="2026-10-30", due="2026-10-20"))
        i = {**copy.deepcopy(e), "request_id": "I", "issues": ["determination_date_inconsistent"]}
        o = {**copy.deepcopy(e), "request_id": "O", "agency_key": "beta-so", "agency": "BETA-SO"}
        n = row("alpha-pd", "N", clock(state="past_due_reminder", due="2026-10-20"))
        reg = register([e, e2, c, dd, i, o, n])
        settings = {"invalid": False, "default": {"autonomy": "off", "notice_after_days": 10, "silence_after_days": 0},
                    "agencies": {"alpha-pd": {"autonomy": "draft", "notice_after_days": 10, "silence_after_days": 0}}}
        approved = {"id": "q1", "item_id": fc.item_id("alpha-pd", "N", "determination", "2026-10-20"), "kind": "reminder-no-response",
                    "agency_id": "alpha-pd", "request_id": "N", "status": "approved", "updated_at": d(-3) + "T12:00:00Z"}
        items, notes = cc.build(stub(reg, [approved], settings), TODAY)
        got = sorted((x["request"], x["date"].isoformat(), x["label"].split(" ")[0]) for x in items if x["kind"] == "followup_draft_due")
        probe = copy.deepcopy(e)
        probe["deadline"]["state"], probe["promised"] = "past_due_reminder", {"state": "none", "date": None}
        direct = fc.plan({**reg, "requests": [probe]}, settings, [], as_of=d(6))
        self.assertEqual([c["kind"] for c in direct], ["reminder-no-response"])
        probe2 = copy.deepcopy(e2)
        probe2["promised"]["state"], probe2["deadline"]["state"] = "passed", "running"
        direct2 = fc.plan({**reg, "requests": [probe2]}, settings, [], as_of=d(8))
        self.assertEqual([c["kind"] for c in direct2], ["reminder-promised-date"])
        self.assertEqual(got, [("E", d(6), "reminder-no-response"), ("E2", d(8), "reminder-promised-date"),
                               ("N", d(7), fc.NOTICE)])
        text = " ".join(notes)
        self.assertIn("no follow-up projected for 3 requests", text)
        self.assertIn("follow-up drafting is off for this agency: BETA-SO", text)
        self.assertNotRegex(json.dumps([x["label"] for x in items], default=str), r"\bsent\b|goes out")

    def test_pending_draft_is_awaiting_approval(self):
        e = row("alpha-pd", "E", clock(state="past_due_reminder", due="2026-10-20"))
        ident = fc.item_id("alpha-pd", "E", "determination", "2026-10-20")
        queue = [{"id": "q", "item_id": ident, "kind": "reminder-no-response", "agency_id": "alpha-pd", "request_id": "E",
                  "status": "pending", "updated_at": "2026-11-05T00:00:00Z"}]
        settings = {"invalid": False, "default": {"autonomy": "draft", "notice_after_days": 10, "silence_after_days": 0}, "agencies": {}}
        items = cc.entries(stub(register([e]), queue, settings), TODAY)
        self.assertEqual([(x["date"], "awaiting your approval (Home)" in x["label"]) for x in items], [(TODAY, True)])

    def test_unavailable_sources_are_named_and_others_listed(self):
        rows = [row("alpha-pd", "R-1")]
        model = self.model(rows, links=[link("2026-11-17")], drop=("outcomes/timeline.json", "outcomes/release-links.json"))
        items, notes = cc.build(model, TODAY)
        text = " ".join(notes)
        self.assertIn("timeline: unavailable", text)
        self.assertIn("release links: unavailable", text)
        self.assertEqual([x["kind"] for x in items], ["followup_draft_due"])
        model = self.model(rows, links=[link("2026-11-17")])
        put(self.project / "run-outputs" / RUN / fc.REGISTER_NAME, None, raw="{broken")
        items, notes = cc.build(cc.load(str(self.project), RUN, NOW), TODAY)
        self.assertIn("follow-up projection: unavailable (register_unreadable)", notes)
        self.assertIn("link_expiry", [x["kind"] for x in items])
        model = self.model(rows)
        put(self.project / fc.SETTINGS_FILE, None, raw="{broken")
        self.assertIn("follow-up projection: unavailable (settings_invalid)", cc.build(cc.load(str(self.project), RUN, NOW), TODAY)[1])
        # packets: an absent journal is empty, a corrupt one is unavailable
        self.assertEqual(model["packets"], {"state": "ok", "rows": []})
        self.assertFalse([n for n in cc.build(model, TODAY)[1] if n.startswith("public packets")])
        (self.project / "public-packets.jsonl").write_text("not json\n")
        bad = cc.load(str(self.project), RUN, NOW)
        self.assertEqual(bad["packets"]["state"], "unavailable")
        self.assertTrue([n for n in cc.build(bad, TODAY)[1] if n.startswith("public packets: unavailable")])

    def test_month_parameter_and_cli_ics(self):
        self.assertEqual(cc._month("2026-12", TODAY), date(2026, 12, 1))
        for bad in ("2026-13", "26-1", "x", "2026-1"):
            self.assertIsNone(cc._month(bad, TODAY))
        self.model([row("alpha-pd", "R-1")])
        out = self.project / "cal.ics"
        with mock.patch("sys.argv", ["records calendar", "--project", str(self.project), "--run-id", RUN, "--ics", str(out)]):
            self.assertEqual(cc.main(), 0)
        self.assertTrue(out.read_bytes().startswith(b"BEGIN:VCALENDAR\r\n"))
        self.assertEqual(oct(out.stat().st_mode & 0o777), "0o600")


    def test_malformed_register_rows_degrade_only_followup(self):
        rows = [row("alpha-pd", "R-1")]
        model = self.model(rows, links=[link("2026-11-17")])
        for bad in ("junk", {"agency_key": "alpha-pd"}, {**rows[0], "deadline": "oops"}, {**rows[0], "request_id": 7}):
            put(self.project / "run-outputs" / RUN / fc.REGISTER_NAME, register(rows + [bad]))
            items, notes = cc.build(cc.load(str(self.project), RUN, NOW), TODAY)
            self.assertIn("follow-up projection: unavailable (register_invalid)", notes)
            self.assertIn("link_expiry", [x["kind"] for x in items])
        # a row that passes the shape check but still breaks the planner must not crash or leak partial entries
        broken = {**rows[0], "deadline": {"state": "running", "due": "9999-12-31"}}
        put(self.project / "run-outputs" / RUN / fc.REGISTER_NAME, register([rows[0], broken]))
        items, notes = cc.build(cc.load(str(self.project), RUN, NOW), TODAY)
        self.assertIn("link_expiry", [x["kind"] for x in items])

    def test_ics_has_no_paths_and_folds_to_75_octets(self):
        e = {"date": TODAY, "kind": "overdue", "agency_key": "k", "request": "R-1 /home/u/records/x.pdf C:\\data\\a.txt ~/p",
             "agency": "City/County PD ./rel/file.csv private/report.parquet private.docs/report.csv", "label": "l", "basis": "DATED",
             "source": "see /var/lib/deflock/run/out.json and ../up/x.tsv data/report.markdown archive.v2/notes.txt " + "é" * 120}
        text = cc.to_ics([e], NOW)
        for frag in ("/home", "/var", "C:", "x.pdf", "out.json", "report.parquet", "report.markdown", "../", "~/", "file.csv", "private.docs", "archive.v2", "notes.txt"):
            self.assertNotIn(frag, text)
        self.assertIn("City/County PD", text.replace("\r\n ", ""))
        for line in text.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75, line)
        self.assertEqual(cc._fold("a" * 74 + "é" * 40).split("\r\n ")[-1].encode().__len__() <= 74, True)
        for n in range(70, 240):
            for ch in ("a", "é", "€"):
                for ln in cc._fold("X" + ch * n).split("\r\n"):
                    self.assertLessEqual(len(ln.encode("utf-8")), 75)

    def engine_row(self, status="pending", **kw):
        return {"id": "engine-1", "item_id": "route:synthetic", "kind": "followup-playbook",
                "agency_id": "alpha-pd", "request_id": "R-1", "status": status, "revision": 2,
                "source_run_id": "route_run", "body": SENTINEL,
                "evidence": {"pattern": "partial", "planned_send_date": "2026-11-12",
                             "private_body": SENTINEL, "url": "https://example.test/" + SENTINEL}, **kw}

    def test_engine_planned_dates_labels_privacy_and_queue_preserved(self):
        from campaign_tool.records import approval_queue as aq
        self.model([])
        for status, day in (("pending", "2026-11-12"), ("approved", "2026-11-13"), ("rejected", "2026-11-14")):
            q = self.engine_row(status)
            aq.add_draft(self.project, **{k: q[k] for k in
                         ("kind", "agency_id", "request_id", "body", "source_run_id")},
                         item_id="route:" + status, evidence={**q["evidence"], "planned_send_date": day})
        model = cc.load(str(self.project), RUN, NOW)
        for q in model["queue"]["rows"]:
            q["status"] = q["item_id"].split(":")[1]
        before = snapshot(self.project)
        items = cc.entries(model, TODAY)
        self.assertEqual([(e["date"].isoformat(), e["label"]) for e in items], [
            ("2026-11-12", "partial follow-up: awaiting your approval (Home)"),
            ("2026-11-13", "partial follow-up: approved; sending is yours (send not tracked)")])
        self.assertTrue(all(e["basis"] == "INFERRED" and
                            e["source"] == "follow-up engine route (run route_run)" for e in items))
        html, ics = cc.render(model, TODAY, TODAY.replace(day=1)), cc.to_ics(items, NOW)
        self.assertIn("awaiting your approval (Home)", html)
        self.assertIn("approved; sending is yours", html)
        self.assertEqual(ics.count("BEGIN:VEVENT"), 2)
        self.assertIn("DTSTART;VALUE=DATE:20261112", ics)
        self.assertIn("DTSTART;VALUE=DATE:20261113", ics)
        for text in (html, ics):
            self.assertNotIn(SENTINEL, text)
            self.assertNotIn("https://example.test/", text)
        self.assertEqual(snapshot(self.project), before)

    def test_engine_exclusions_duplicates_stale_revisions_and_delivery_states(self):
        q = self.engine_row("approved")
        model = stub(register([]), [q, copy.deepcopy(q)])
        for revision, state, count in ((2, "sent", 0), (1, "sent", 1), (2, "failed", 1),
                                       (2, "unknown", 1), (2, "attempt", 1)):
            with self.subTest(revision=revision, state=state):
                model["sends"] = {"state": "ok", "configured": True,
                                  "rows": [{"queue_id": q["id"], "revision": revision, "state": state}]}
                items = cc.entries(model, TODAY)
                self.assertEqual(len(items), count)
                if items:
                    self.assertIn("approved; sending is yours", items[0]["label"])
                    self.assertNotIn("send not tracked", items[0]["label"])
                self.assertEqual(cc.to_ics(items, NOW).count("BEGIN:VEVENT"), count)
        for status in ("rejected", "cancelled", "expired"):
            self.assertEqual(cc.entries(stub(register([]), [self.engine_row(status)]), TODAY), [])
        for day in (None, "bad", "2026-02-30", "2026-11-04", "2027-11-12"):
            bad = self.engine_row(evidence={"pattern": "partial", "planned_send_date": day})
            self.assertEqual(cc.entries(stub(register([]), [bad]), TODAY), [])
        self.assertEqual(cc.entries(stub(register([]), [self.engine_row(kind="first-request")]), TODAY), [])

    def test_engine_send_source_loading_and_fail_closed(self):
        self.model([])
        with mock.patch.object(cc.mail_send, "results", return_value=[]) as results:
            cc.load(str(self.project), RUN, NOW)
            results.assert_not_called()
            model = cc.load(str(self.project), RUN, NOW, send_config="not-read")
            results.assert_called_once_with(str(self.project), strict=True)
        with mock.patch.object(cc.mail_send, "results", side_effect=cc.mail_send.MailSendError("invalid")):
            model = cc.load(str(self.project), RUN, NOW, send_config="not-read")
        model["queue"]["rows"] = [self.engine_row("approved"), self.engine_row(request_id="R-2")]
        items, notes = cc.build(model, TODAY)
        self.assertEqual([e["request"] for e in items], ["R-2"])
        self.assertIn("follow-up planned sends: unavailable (sends_unreadable)", notes)
        model["queue"] = {"state": "unavailable"}
        self.assertEqual(cc.entries(model, TODAY), [])

    def test_boundary_months_are_rejected(self):
        for bad in ("0000-01", "0001-01", "9999-12", "2201-01", "1899-12"):
            self.assertIsNone(cc._month(bad, TODAY), bad)
        for good in ("1900-01", "2200-12"):
            first = cc._month(good, TODAY)
            self.assertTrue(cc.render(stub(register([])), TODAY, first))


    def meeting(self, ident="legistar-alpha-1", **kw):
        return {"id": ident, "body": "Alpha Council Département", "agenda_item": SENTINEL,
                "starts_at": "2026-11-12T18:00:00-08:00", "time_known": True,
                "source": "legistar", "status": "scheduled", "matched_terms": ["flock"],
                "agenda_url": "https://agenda.example.test/" + SENTINEL, **kw}

    def meetings_model(self, scan, manual=()):
        self.model([])
        campaign = self.project / "campaign"
        put(campaign / "kit/meetings.json", {"schema_version": 1, "generated": "2026-11-05T12:00:00Z",
                                           "meetings": scan})
        put(campaign / "kit/meetings-manual.json", {"schema_version": 1, "meetings": list(manual)})
        return campaign, cc.load(str(self.project), RUN, NOW, meetings_dir=campaign)

    def test_meetings_scheduled_cancelled_manual_staged_and_ics(self):
        scan = [self.meeting(), self.meeting("legistar-alpha-2", status="cancelled", time_known=False)]
        manual = [self.meeting("manual-alpha", source="manual", starts_at="2026-11-13T00:00:00-08:00")]
        campaign, _ = self.meetings_model(scan, manual)
        put(campaign / "content/meetings/legistar-alpha-1.json", {"synthetic": True})
        before = snapshot(self.project)
        model = cc.load(str(self.project), RUN, NOW, meetings_dir=campaign)
        items, notes = cc.build(model, TODAY)
        self.assertEqual(len(items), 3)
        self.assertEqual(notes, [])
        self.assertTrue(all(e["kind"] == "meeting" and e["basis"] == "DATED" for e in items))
        html = cc.render(model, TODAY, TODAY.replace(day=1))
        self.assertIn("keyword lead, verify on the official agenda", html)
        self.assertIn("comment kit staged", items[0]["label"])
        self.assertNotIn("comment kit staged", items[1]["label"])
        self.assertIn("cancelled", items[1]["label"])
        self.assertIn("time unknown", items[1]["label"])
        self.assertIn("legistar agenda via kit/meetings.json (generated 2026-11-05)", html)
        self.assertIn("manual agenda via kit/meetings-manual.json (generated unknown)", html)
        self.model_ics(model)
        ics = cc.to_ics(items, NOW).replace("\r\n ", "")
        self.assertEqual(ics.count("SUMMARY:Meeting: Alpha Council Département (DATED)"), 3)
        for private in ("agenda.example.test", "legistar-alpha", "manual-alpha", SENTINEL):
            self.assertNotIn(private, ics)
        self.assertEqual(snapshot(self.project), before)

    def test_meetings_undated_duplicate_past_expired_and_stale(self):
        first = self.meeting(stale=True)
        scan = [first, copy.deepcopy(first), self.meeting("undated", starts_at=None),
                self.meeting("bad-date", starts_at="2026-02-30"),
                self.meeting("past", starts_at="2026-11-04"), self.meeting("expired", status="expired"),
                self.meeting("unscanned", not_scanned=True)]
        _, model = self.meetings_model(scan)
        items, notes = cc.build(model, TODAY)
        self.assertEqual(len(items), 1)
        self.assertIn("stale source", items[0]["label"])
        self.assertIn("undated meetings skipped: 2 (meetings.json)", notes)
        self.assertEqual(cc.to_ics(items, NOW).count("BEGIN:VEVENT"), 1)
        self.assertEqual(len(cc.entries(model, TODAY, since=date(2026, 11, 1))), 2)

    def test_meetings_absent_unreadable_bounded_and_independent(self):
        self.model([])
        self.assertEqual(cc._load_meetings(None), ([], []))
        campaign = self.project / "absent"
        self.assertEqual(cc._load_meetings(campaign), ([], []))
        campaign, _ = self.meetings_model([], [self.meeting("manual-alpha", source="manual")])
        path = campaign / "kit/meetings.json"
        for raw in ("{broken", "[]", json.dumps({"schema_version": 1, "meetings": [self.meeting("manual-alpha"), None]}),
                    " " * (4 * 1024 * 1024 + 1)):
            with self.subTest(raw_size=len(raw)):
                put(path, None, raw=raw)
                model = cc.load(str(self.project), RUN, NOW, meetings_dir=campaign)
                items, notes = cc.build(model, TODAY)
                self.assertEqual(len(items), 1)
                self.assertIn("unavailable (meetings): meetings.json", notes)
        path.unlink()
        path.symlink_to(campaign / "kit/meetings-manual.json")
        self.assertIn("unavailable (meetings): meetings.json", cc._load_meetings(campaign)[1])
        path.unlink()
        os.mkfifo(path)
        self.assertIn("unavailable (meetings): meetings.json", cc._load_meetings(campaign)[1])

    def test_meetings_panel_option_and_route(self):
        from campaign_tool.records import admin_panel
        from io import BytesIO
        campaign, _ = self.meetings_model([self.meeting()])
        with admin_panel.build_server(str(self.project), RUN, actor="operator", system=False,
                                      meetings_dir=campaign) as server:
            self.assertEqual(server.options["meetings_dir"], campaign)
            handler = mock.Mock(path="/calendar", server=server, command="GET", wfile=BytesIO())
            self.assertTrue(cc.serve(handler))
            self.assertIn("keyword lead", handler._send.call_args.args[1])
            handler.path = "/calendar.ics"
            self.assertTrue(cc.serve(handler))
            self.assertNotIn(SENTINEL.encode(), handler.wfile.getvalue())
        with mock.patch.object(admin_panel, "build_server", side_effect=OSError) as build_server:
            self.assertEqual(admin_panel.main(["--project", str(self.project), "--run-id", RUN,
                                              "--actor", "operator", "--meetings-dir", str(campaign)]), 1)
            self.assertEqual(build_server.call_args.kwargs["meetings_dir"], str(campaign))


if __name__ == "__main__":
    unittest.main()
