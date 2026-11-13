"""Synthetic Home decisions: newest first, metadata-only packets, degradation and HTTP routing."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from campaign_tool.records import approval_queue as aq, console_home as home, public_packets as pp
from campaign_tool.records.console_layout import plain, unavailable_note
from campaign_tool.records import public_updates as pu
from tests.records import test_admin_panel as panel
from tests.records.test_console_board import build, put, RUN
from tests.records import test_console_calendar as cal

NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)


def put_received(project):
    """The received-items source the complete fixture reads; Home counts waiting requests only when it is present."""
    put(project / "run-outputs" / RUN / "outcomes/received-items.json",
        {"schema": "records-received-item-v1", "run_id": RUN, "items": []})


def fixture(project):
    build(project)
    put_received(project)
    path = project / "run-outputs" / RUN / "outcomes/timeline.json"
    doc = json.loads(path.read_text())
    for request, days in zip(doc["requests"], (None, 30, None, None)):
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

    def test_newest_counts_links_metadata_and_privacy(self):
        model = self.model()
        rows = home.decision_rows(model, NOW.date())
        queue = [r for r in rows if r["source"] == "queue"]
        self.assertEqual([r["age_hours"] for r in queue], [2, 50, 200])
        self.assertEqual([r["overdue_days"] for r in queue], [None, 30, None])
        holding = next(r for r in rows if r["kind"] == "holding")
        self.assertLess(rows.index(holding), rows.index(queue[1]))
        self.assertLess(rows.index(holding), rows.index(queue[2]))
        for row in rows:
            self.assertTrue(all(row[k] not in (None, "") for k in ("agency", "what", "why", "age_hours", "link")))
        page = home.render_home(model, "unused")
        for text in ("Requests 1 · Follow-ups 2", "Status lines 2 · Packets 1", '<span class="overdue">OVERDUE 30 days</span>',
                     "not a legal finding", "CON-SITEPR-1", "CON-REDACT-1", "CON-FIND-1", 'id="standing"', 'id="outstanding"'):
            self.assertIn(text, page)
        for text in ("BODY-SENTINEL-1", "person@example.invalid", "PACKET-TEXT-SENTINEL-1", "LINE-TEXT-SENTINEL", "@", "<form"):
            self.assertNotIn(text, page)

    def test_layout_labels_and_review_links(self):
        model = self.model()
        rows = home.decision_rows(model, NOW.date())
        page = home.render_home(model, "")
        self.assertIn('<p role="status">6 need your decision · ', page)
        self.assertLess(page.index('6 need your decision'), page.index('<section id="decide">'))
        start, end = page.index('<details id="more">'), page.index('</details>')
        self.assertLess(page.index('<section id="decide">'), start)
        for text in ('id="standing"', 'id="outstanding"', 'CON-SITEPR-1', 'CON-REDACT-1', 'CON-FIND-1'):
            self.assertTrue(start < page.index(text) < end, text)
        self.assertIn('<summary>More detail</summary>', page)
        self.assertTrue(page.index('</table></section>') < page.index('id="week"') < start)
        self.assertTrue(page.index('id="week"') < page.index('id="waiting"') < start)
        self.assertTrue(page.index('id="waiting"') < page.index('id="timeline"') < page.index('id="next"') < start)
        table = page.split('<tbody>', 1)[1].split('</tbody>', 1)[0]
        for row in rows:
            label = "Review" if row["source"] == "queue" else "Open"
            self.assertIn('<a href="%s">%s</a>' % (row["link"], label), table)
        queue = [r for r in rows if r["source"] == "queue"]
        self.assertEqual([r["what"] for r in queue],
                         ["First request", "Reply to an extension", "Reminder: no reply"])
        self.assertLess(table.index(queue[0]["link"]), table.index(queue[1]["link"]))
        self.assertLess(table.index(queue[1]["link"]), table.index(queue[2]["link"]))
        self.assertEqual({r["link"] for r in rows if r["source"] == "packets"}, {"/packets"})
        self.assertEqual({r["what"] for r in rows if r["source"] == "public"}, {"Public status line (L1, 2026-11-04)"})
        self.assertEqual({r["what"] for r in rows if r["source"] == "packets"}, {"Update packet"})

    def test_label_map_unknown_escaping_and_age_ties(self):
        model = self.model()
        template = model["queue"]["rows"][0]
        model["public"]["lines"] = []
        model["packets"]["rows"] = []
        expected = {
            "first-request": "First request", "reminder-no-response": "Reminder: no reply",
            "reminder-promised-date": "Reminder: promised date passed",
            "formal-notice-no-response": "Formal notice: no reply", "appeal-denial": "Reply to a refusal",
            "reply-extension": "Reply to an extension", "fee-reply": "Reply about fees",
            "followup-missing-records": "Ask for missing records", "next-social-post": "Suggested post",
            "next-site-update": "Suggested site update", "next-followup-probe": "Suggested follow-up",
            "<unknown>": "<unknown>",
        }
        model["queue"]["rows"] = [template | {"kind": kind, "id": kind} for kind in reversed(expected)]
        rows = home.decision_rows(model, NOW.date())
        self.assertEqual([r["id"] for r in rows], sorted(expected))
        self.assertEqual({r["kind"]: r["what"] for r in rows}, expected)
        self.assertIn('&lt;unknown&gt;', home.render_home(model, ""))
        self.assertNotIn('<unknown>', home.render_home(model, ""))
        model["queue"]["rows"] += [template | {"id": "undated", "created_at": None}]
        rows = home.decision_rows(model, NOW.date())
        self.assertEqual((rows[-1]["id"], rows[-1]["age_hours"]), ("undated", None))
        for state in ("rejected", "cancelled", "expired"):
            model["queue"]["rows"] = [template | {"status": state}]
            self.assertEqual(home.decision_rows(model, NOW.date()), [])
            page = home.render_home(model, "")
            self.assertIn('0 need your decision', page)
            self.assertNotIn('Requests 0', page)

    def test_decisions_same_hour_are_exact_newest_first(self):
        items = ["a" * 64, "f" * 64]
        if aq.draft_id(items[0], "first-request") > aq.draft_id(items[1], "first-request"):
            items.reverse()  # older draft gets the lexically smaller id, so an id sort would be wrong
        ids = []
        for item, stamp, req in zip(items, ("2026-11-04T23:01:00Z", "2026-11-04T23:59:00Z"), ("R-3", "R-4")):
            with mock.patch.object(aq, "_now", return_value=stamp):
                ids.append(aq.add_draft(self.project, item_id=item, kind="first-request", agency_id="alpha-pd",
                                        request_id=req, body="BODY-SENTINEL-1", evidence={"why": "Recorded request"},
                                        source_run_id=RUN)["id"])
        older, newer = ids
        self.assertLess(older, newer)
        model = self.model()
        rows = [r for r in home.decision_rows(model, NOW.date()) if r["id"] in ids]
        self.assertEqual([r["age_hours"] for r in rows], [0, 0])
        self.assertEqual([r["id"] for r in rows], [newer, older])
        page = home.render_home(model, "")
        self.assertLess(page.index("/review?id=" + newer), page.index("/review?id=" + older))

    def test_decisions_cross_source_exact_time_order(self):
        model = self.model(send_config="not-read")
        stamp = lambda minutes: (NOW - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        template = model["queue"]["rows"][0]
        model["queue"]["rows"] = [
            template | {"id": "q-pending", "created_at": stamp(50)},
            template | {"id": "q-approved", "status": "approved", "revision": 1, "created_at": stamp(10)}]
        model["public"]["lines"] = [dict(model["public"]["lines"][0], id="p-line", proposed_at=NOW.timestamp() - 30 * 60)]
        model["packets"]["rows"] = [dict(model["packets"]["rows"][0], id="p-packet", proposed_at=stamp(40), age_hours=0)]
        rows = home.decision_rows(model, NOW.date())
        self.assertEqual([r["id"] for r in rows], ["q-approved", "p-line", "p-packet", "q-pending"])
        self.assertEqual([r["source"] for r in rows], ["sends", "public", "packets", "queue"])
        self.assertEqual({r["age_hours"] for r in rows}, {0})

    def test_decisions_exact_ties_and_unknown_times(self):
        model = self.model()
        template = model["queue"]["rows"][0]
        line = model["public"]["lines"][0]
        keys = set(home.decision_rows(model, NOW.date())[0])
        model["packets"]["rows"] = []
        same = "2026-11-04T23:00:00Z"
        model["queue"]["rows"] = [
            template | {"id": "b", "created_at": same}, template | {"id": "a", "created_at": same},
            template | {"id": "x-missing", "created_at": None}, template | {"id": "x-bad", "created_at": "not a time"}]
        lines = [dict(line, id=ident, proposed_at=value) for ident, value in (
            ("a", NOW.timestamp() - 3600), ("l-bool", True), ("l-text", same), ("l-nan", float("nan")),
            ("l-inf", float("inf")), ("l-huge", 1e30), ("l-none", None), ("l-zero", 0))]
        expected = [("a", "public"), ("a", "queue"), ("b", "queue"), ("l-zero", "public"), ("l-bool", "public"),
                    ("l-huge", "public"), ("l-inf", "public"), ("l-nan", "public"), ("l-none", "public"),
                    ("l-text", "public"), ("x-bad", "queue"), ("x-missing", "queue")]
        for order in (lines, lines[::-1]):
            model["public"]["lines"] = order
            rows = home.decision_rows(model, NOW.date())
            self.assertEqual([(r["id"], r["source"]) for r in rows], expected)
            self.assertEqual({frozenset(r) for r in rows}, {frozenset(keys)})
            self.assertEqual([r["age_hours"] for r in rows if r["id"].startswith(("l-b", "l-h", "l-i", "l-n", "l-t", "x-"))],
                             [None] * 8)
        home.render_home(model, "")

    def test_decisions_future_times_do_not_collapse_order(self):
        model = self.model()
        stamp = lambda minutes: (NOW + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        template = model["queue"]["rows"][0]
        model["public"]["lines"], model["packets"]["rows"] = [], []
        model["queue"]["rows"] = [template | {"id": ident, "created_at": stamp(m)}
                                  for ident, m in (("past", -10), ("far", 60), ("near", 30))]
        before = json.dumps(model["queue"]["rows"], sort_keys=True)
        rows = home.decision_rows(model, NOW.date())
        self.assertEqual([r["id"] for r in rows], ["far", "near", "past"])
        self.assertEqual([r["age_hours"] for r in rows], [0, 0, 0])
        self.assertEqual(json.dumps(model["queue"]["rows"], sort_keys=True), before)
        self.assertNotIn("BODY-SENTINEL", home.render_home(model, ""))

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
        for kind, (group, _) in home.NEXT.items():
            row = next(r for r in rows if r["kind"] == kind)
            self.assertEqual((row["kind_group"], row["link"]), (group, "/review?id=" + kind))

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
        self.assertIn("update packets: unavailable", home.render_home(self.model(), ""))
        for source in ("timeline", "queue", "public", "packets", "sends"):
            model = self.model()
            model[source] = {"state": "unavailable", "code": "synthetic_bad"}
            page = home.render_home(model, "")
            self.assertIn(plain([source])[0] + ": unavailable", page)
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


class UnknownCounts(unittest.TestCase):
    """DFGL-13: a source that cannot be read is named in a banner and its counts print as unknown, never 0."""
    setUp, model = Home.setUp, Home.model

    def test_complete_fixture_has_no_banner_and_known_counts(self):
        page = home.render_home(self.model(), '')
        self.assertNotIn('Some information could not be loaded', page)
        self.assertNotIn('unknown need your decision', page)
        self.assertRegex(page, r'<p role="status">\d+ need your decision')
        self.assertIn('waiting on agencies (', page)

    def test_missing_received_items_banner_and_no_zero_waiting(self):
        (self.project / "run-outputs" / RUN / "outcomes/received-items.json").unlink()
        page = home.render_home(self.model(), '')
        self.assertIn('<div role="status" class="diag">Some information could not be loaded: received documents. '
                      'Counts that depend on it show as unknown.</div>', page)
        self.assertIn('waiting on agencies: unknown', page)
        self.assertNotRegex(page, r'\b0 waiting')
        self.assertNotIn('No requests waiting on agencies', page)
        self.assertNotRegex(page, r'\d+ waiting on agencies \(')
        # a request list that is empty because the source is missing is "not known", not "none"
        timeline = self.project / "run-outputs" / RUN / "outcomes/timeline.json"
        put(timeline, json.loads(timeline.read_text()) | {"requests": []})
        page = home.render_home(self.model(), '')
        self.assertIn('Not known: received documents not loaded.', page)
        self.assertNotIn('No requests waiting on agencies', page)

    def test_unreadable_decision_source_makes_decision_count_unknown(self):
        for source in ("queue", "public", "packets", "sends"):
            model = self.model()
            model[source] = {"state": "unavailable", "code": "synthetic_bad"}
            page = home.render_home(model, '')
            self.assertIn('unknown need your decision', page, source)
            self.assertNotRegex(page, r'\b0 need your decision')
            self.assertIn('Some information could not be loaded: ', page)


class Week(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.project = Path(tmp.name)

    def model(self):
        cal.put(self.project / cal.fc.SETTINGS_FILE, cal.settings_doc(**{'alpha-pd': 'off'}))
        return home.load(self.project, RUN, now=NOW)

    def test_week_window_basis_links_duplicates_and_empty(self):
        rows = [cal.row("alpha-pd", "R-" + str(day), cal.clock(due=(NOW + timedelta(days=day)).date().isoformat(),
                    base_due=(NOW + timedelta(days=day)).date().isoformat(), extended_due=None)) for day in (-1, 0, 3, 7, 9)]
        cal.build(self.project, rows + [rows[2]])
        model = self.model()
        week = model["calendar"]["rows"]
        self.assertEqual([e["request"] for e in week], ["R-0", "R-3", "R-7"])
        page = home.render_home(model, "")
        section = page.split('<section id="week">')[1].split('</section>')[0]
        self.assertIn('3 due in the next 7 days', page)
        self.assertIn('Sun Nov 08 · Statutory due · ALPHA-PD · R-3 · UNVERIFIED', section)
        self.assertEqual(section.count('/calendar?month=2026-11'), 3)
        for request in ('R--1', 'R-9'):
            self.assertNotIn(request, section)
        cal.build(self.project, [])
        self.assertIn('Nothing due in the next 7 days.', home.render_home(self.model(), ""))

    def test_week_cap_and_order(self):
        cal.build(self.project, [cal.row('alpha-pd', 'R-%02d' % n, cal.clock(due='2026-11-08',
            base_due='2026-11-08', extended_due=None)) for n in range(25)], links=[cal.link('2026-11-08')])
        model = self.model()
        week = model['calendar']['rows']
        self.assertEqual(len(week), 26)
        self.assertEqual([(e['date'], e['kind']) for e in week], sorted((e['date'], e['kind']) for e in week))
        self.assertEqual(week[0]['kind'], 'link_expiry')
        section = home.render_home(model, '').split('<section id="week">')[1].split('</section>')[0]
        self.assertEqual(section.count('<li>'), 20)
        self.assertNotIn('R-24', section)
        self.assertIn('26 due in the next 7 days', home.render_home(model, ''))

    def test_week_failure_is_independent_and_escaped(self):
        for result in (RuntimeError('private detail'), {'state': 'unavailable', 'code': '<bad>'}):
            kwargs = {'side_effect': result} if isinstance(result, Exception) else {'return_value': result}
            with mock.patch.object(home.console_calendar, 'load', **kwargs):
                page = home.render_home(self.model(), '')
            code = 'calendar_unreadable' if isinstance(result, Exception) else '&lt;bad&gt;'
            self.assertIn(unavailable_note('Calendar', code.replace('&lt;', '<').replace('&gt;', '>')).replace('<', '&lt;').replace('>', '&gt;'), page)
            self.assertIn('Needs your decision', page)
            self.assertIn('More detail', page)
            self.assertNotIn('private detail', page)
            self.assertNotIn('Nothing due in the next 7 days.', page)
            self.assertIn('unknown due in the next 7 days', page)
        with mock.patch.object(home.console_calendar, 'entries', side_effect=ValueError('bad')):
            self.assertEqual(self.model()['calendar']['state'], 'unavailable')

    def test_week_meetings_cancelled_stale_and_month_boundary(self):
        cal.build(self.project, [])
        meetings = self.project / 'campaign'
        day = '2026-12-01'
        meeting = {'id': 'm1', 'body': '<Council>', 'agenda_item': '<ALPR>', 'starts_at': day,
                   'status': 'cancelled', 'stale': True}
        cal.put(meetings / 'kit/meetings.json', {'schema_version': 1, 'meetings': [meeting, meeting]})
        model = home.load(self.project, RUN, now=datetime(2026, 11, 28, tzinfo=timezone.utc), meetings_dir=meetings)
        self.assertEqual(len(model['calendar']['rows']), 1)
        page = home.render_home(model, '')
        self.assertIn('/calendar?month=2026-12', page)
        self.assertIn('Meeting · &lt;Council&gt; ·  · DATED', page)
        self.assertIn('(cancelled)', page)
        self.assertIn('(stale source)', page)
        cal.build(self.project, [cal.row('alpha-pd', 'R-stale')])
        path = self.project / 'run-outputs' / RUN / 'outcomes/timeline.json'
        doc = json.loads(path.read_text())
        doc['run_id'] = 'stale'
        cal.put(path, doc)
        self.assertEqual(self.model()['calendar']['rows'], [])


class Waiting(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.project = Path(tmp.name)
        build(self.project)
        put_received(self.project)
        self.path = self.project / 'run-outputs' / RUN / 'outcomes/timeline.json'
        self.doc = json.loads(self.path.read_text())
        self.template = self.doc['requests'][1]

    def request(self, ident, due='2026-11-09', owner='agency', overdue=None, **extra):
        return self.template | {'request_id': ident, 'ledger_request_id': 'L-' + ident,
            'clocks': {'due': due, 'basis': 'submission_proxy'},
            'next_action': {'owner': owner, 'action': 'wait'}, 'closed': False,
            'events': [{'type': 'sent', 'date': '2026-10-01', 'source': {'kind': 'outbox_receipt'}}],
            'overdue_marks': [] if overdue is None else [{'kind': 'statutory_date_passed',
                'label': 'past due', 'date': overdue, 'basis': 'recorded_receipt'}], **extra}

    def page(self, requests):
        put(self.path, self.doc | {'requests': requests})
        model = home.load(self.project, RUN, now=NOW)
        return model, home.render_home(model, '')

    def test_order_dates_basis_counts_and_exclusions(self):
        requests = [self.request('unknown', due=None), self.request('future'),
            self.request('recent', overdue='2026-11-03'),
            self.request('oldest', owner='coordinator', overdue='2026-10-20'),
            self.request('ours', owner='G'), self.request('closed', closed=True, overdue='2026-10-01'),
            self.request('sooner', due='2026-11-06')]
        model, page = self.page(requests)
        self.assertEqual([r['request_id'] for r in model['waiting']['rows']],
                         ['oldest', 'recent', 'sooner', 'future', 'unknown'])
        section = page.split('<section id="waiting">')[1].split('</section>')[0]
        for text in ('OVERDUE 16 days</span> since 2026-10-20 (on our records; not a legal finding)',
                     'class="overdue"', 'OVERDUE 2 days', '2026-10-01 (DATED)',
                     '2026-11-09 (INFERRED; submission_proxy)', 'due in 4 days', 'due date unknown',
                     'href="/agency?agency=alpha-pd"', 'Asked on', 'Due (basis)'):
            self.assertIn(text, section)
        for ident in ('ours', 'closed'):
            self.assertNotIn('<td>' + ident + '</td>', section)
        self.assertIn('· 5 waiting on agencies (2 overdue)', page)

    def test_cap_duplicate_and_empty(self):
        requests = [self.request('R-%02d' % n) for n in range(45)]
        model, page = self.page(requests + [requests[0]])
        self.assertEqual(len(model['waiting']['rows']), 45)
        section = page.split('<section id="waiting">')[1].split('</section>')[0]
        self.assertEqual(section.count('<tr>'), 41)  # header plus 40 requests
        self.assertIn('href="/board?filter=waiting">5 more on the Agencies page</a>', section)
        self.assertIn('45 waiting on agencies (0 overdue)', page)
        self.assertNotIn('<td>R-40</td>', section)
        self.assertIn('No requests waiting on agencies.', self.page([])[1])

    def test_failure_stale_and_unusable_dates(self):
        base = home.co.load(self.project, RUN, now=NOW)
        for failure in (OSError('private detail'), ValueError('private detail')):
            with mock.patch.object(home.co, 'load', return_value=dict(base)), mock.patch.object(home.cb, 'load', side_effect=failure):
                page = home.render_home(home.load(self.project, RUN, now=NOW), '')
            self.assertIn('Waiting on agencies: unavailable. The agencies board could not be read.', page)
            self.assertIn('waiting on agencies: unknown', page)
            self.assertNotIn('private detail', page)
        for mutate, code in (({'run_id': 'stale'}, 'run_mismatch'), ({'requests': None}, 'shape_invalid')):
            put(self.path, self.doc | mutate)
            page = home.render_home(home.load(self.project, RUN, now=NOW), '')
            self.assertIn(unavailable_note('Waiting on agencies', code), page)
            self.assertNotIn('0 waiting on agencies', page)
        row = self.request('<request>', due='bad', agency='<Agency>', agency_key='alpha/<pd>',
            events=[{'type': 'sent', 'date': '2026-09-01', 'superseded': True},
                    {'type': 'draft', 'date': '2026-10-02', 'draft_status': 'cancelled'},
                    {'type': 'draft', 'date': '2026-10-03', 'draft_status': 'expired'}])
        model, page = self.page([row])
        section = page.split('<section id="waiting">')[1].split('</section>')[0]
        self.assertIsNone(model['waiting']['rows'][0]['asked'])
        for text in ('unknown (UNKNOWN)', 'due date unknown', '&lt;Agency&gt;', '&lt;request&gt;',
                     '/agency?agency=alpha%2F%3Cpd%3E'):
            self.assertIn(text, section)
        self.assertNotIn('<Agency>', section)
        self.assertNotIn('2026-09-01', section)


    def test_timeline_latest_three_grouping_labels_and_escaping(self):
        events = [{'type': 'production', 'date': '2026-10-%02d' % n} for n in range(1, 6)]
        events += [{'type': 'denial', 'date': '2026-11-04', 'superseded': True}]
        events += [{'type': 'draft', 'date': '2026-11-03', 'draft_status': s} for s in ('cancelled', 'expired')]
        alpha = self.request('a', events=events)
        beta = self.request('b', agency_key='beta-pd', agency='<Beta>',
                            events=[{'type': 'acknowledged', 'date': '2026-11-01'}])
        empty = self.request('c', agency_key='empty/pd', agency='Empty', events=[], due=None)
        model, page = self.page([alpha, beta, empty, alpha])
        rows = home.timeline_rows(model, NOW.date())
        self.assertEqual([r['key'] for r in rows], ['beta-pd', 'alpha-pd', 'empty/pd'])
        self.assertEqual([e['date'] for e in rows[1]['events']], ['2026-10-05', '2026-10-04', '2026-10-03'])
        section = page.split('<section id="timeline">')[1].split('</section>')[0]
        for text in ('&lt;Beta&gt;', '2026-11-01 (DATED) acknowledged', 'No events recorded.',
                     'Next: unknown (UNKNOWN)', '2026-11-09 Statutory due (UNVERIFIED;',
                     'Gov. Code § 7922.535', '/agency?agency=empty%2Fpd'):
            self.assertIn(text, section)
        self.assertEqual(section.count('Full timeline</a>'), 3)
        for text in ('denial', 'draft', '2026-10-02', '<Beta>'):
            self.assertNotIn(text, section)
        model['deadlines'] = {('alpha-pd', 'a'): {'determination_due': {
            'date': '2026-11-09', 'label': 'COMPUTED', 'package': 'us-ca sha256:' + 'a' * 64}}}
        self.assertIn('Statutory due (COMPUTED;', home.render_progress(model, NOW.date()))
        alpha['events'] += [{'type': 'promised date', 'date': '2026-10-05', 'new_dates': ['2026-11-07']}]
        model, _ = self.page([alpha])
        self.assertIn('Next: 2026-11-07 promised date (DATED)', home.render_progress(model, NOW.date()))
        alpha['closed'] = True
        model, _ = self.page([alpha])
        self.assertIn('Next: unknown (UNKNOWN)', home.render_progress(model, NOW.date()))
        alpha['closed'] = False
        alpha['events'] = [e for e in alpha['events'] if e['type'] != 'promised date']
        alpha['clocks'] = {'due': '2026-11-09', 'basis': 'submission_proxy', 'state': 'determined'}
        model, _ = self.page([alpha])
        self.assertIn('Next: unknown (UNKNOWN)', home.render_progress(model, NOW.date()))

    def test_timeline_and_action_caps(self):
        requests = [self.request('r-%02d' % n, agency_key='agency-%02d' % n,
                    agency='Agency %02d' % n, next_action={'owner': 'coordinator', 'action': 'Review reply'})
                    for n in range(35)]
        model, page = self.page(requests)
        self.assertEqual(page.split('<section id="timeline">')[1].split('</section>')[0].count('Full timeline</a>'), 30)
        self.assertEqual(page.split('<section id="next">')[1].split('</section>')[0].count('<p>'), 10)
        model['queue']['rows'] = [{'id': 'synthetic', 'revision': 1, 'kind': 'next-site-update', 'status': 'pending'}]
        section = home.render_progress(model, NOW.date()).split('<section id="next">')[1]
        self.assertEqual(section.count('<p>'), 10)
        self.assertEqual(section.count('Review reply'), 9)
        self.assertIn('1 suggested drafts waiting', section)

    def test_next_owners_drafts_dedup_status_and_empty(self):
        rows = [self.request('ours', next_action={'owner': 'G', 'action': '<Review reply>'}),
                self.request('theirs', agency_key='beta-pd', agency='Beta', next_action={'owner': 'agency', 'action': 'Respond'}),
                self.request('coordinator', agency_key='gamma-pd', agency='Gamma',
                             next_action={'owner': 'coordinator', 'action': 'Review records'})]
        model, _ = self.page(rows)
        template = {'id': 'synthetic', 'revision': 2, 'kind': 'next-followup-probe', 'status': 'pending'}
        model['queue']['rows'] = [template, template] + [template | {'id': s, 'status': s}
            for s in ('cancelled', 'expired', 'rejected', 'superseded', 'approved')]
        section = home.render_progress(model, NOW.date()).split('<section id="next">')[1]
        for text in ('ALPHA-PD: &lt;Review reply&gt;', 'Gamma: Review records', '/next-steps', '1 suggested drafts waiting'):
            self.assertIn(text, section)
        for text in ('Respond', '<Review reply>', 'No next steps recorded.'):
            self.assertNotIn(text, section)
        model, page = self.page([])
        self.assertIn('No next steps recorded.', page)
        self.assertIn('No agency events recorded.', page)

    def test_progress_unavailable_stale_and_independent_sources(self):
        for patch, code in (({'run_id': 'stale'}, 'run_mismatch'), ({'requests': None}, 'shape_invalid')):
            put(self.path, self.doc | patch)
            page = home.render_home(home.load(self.project, RUN, now=NOW), '')
            section = page.split('<section id="timeline">')[1].split('</section>')[0]
            self.assertEqual(section.count('<p'), 1)
            self.assertIn(unavailable_note('Timeline', code), section)
            self.assertIn('Needs your decision', page)
            self.assertIn('Next actions: unavailable until the request timeline loads.', page)
            self.assertNotIn('No next steps recorded.', page)
        model, _ = self.page([self.request('r')])
        model['queue'] = {'state': 'unavailable', 'code': '<bad>'}
        page = home.render_progress(model, NOW.date())
        self.assertIn('Full timeline</a>', page)
        self.assertIn('Suggested drafts: unavailable. It could not be read; ask the operator to check the file (&lt;bad&gt;).', page)
        self.assertNotIn('No next steps recorded.', page)


class Routes(panel.ServerCase):
    def test_home_dashboard_and_reject_return(self):
        get = lambda path: self.http.send("GET", path, headers={"Content-Length": None, "Content-Type": None})
        status, _, page = get("/")
        self.assertEqual(status, 200)
        self.assertIn("Needs your decision", page)
        link = "/review?id=" + self.draft_row["id"]
        self.assertIn('<a href="%s">Review</a>' % link, page)
        self.assertEqual(get(link)[0], 200)
        self.assertIn(self.draft_row["id"], get("/dashboard")[2])
        with mock.patch.object(panel.ap, "load_dashboard", return_value={"synthetic": True}):
            with mock.patch.object(panel.ap, "render_dashboard", return_value="old dashboard bytes"):
                self.assertEqual(get("/dashboard")[2], "old dashboard bytes")
        status, headers, _ = self.http.post("/approvals/reject", {"csrf_token": self.tok, "id": self.draft_row["id"], "reason": "not needed"})
        self.assertEqual((status, headers["location"]), (303, "/"))
        self.assertIn("0 need your decision", get("/")[2])
        self.assertNotIn("Follow-ups 0", get("/")[2])
        (self.project / pp.JOURNAL).write_bytes(b"invalid\n")
        self.assertEqual(get("/")[0], 200)
        self.assertIn("update packets: unavailable", get("/")[2])


class Reminders(unittest.TestCase):
    setUp, request, page = Waiting.setUp, Waiting.request, Waiting.page

    def test_reminder_column_integration_and_plain_why(self):
        from campaign_tool.records import followup_clock as fc
        from tests.records.test_followup_clock import row, doc
        requests = [self.request(s, due="2026-10-01", overdue="2026-10-01")
                    for s in ("drafted", "approved", "rejected", "off", "wait")]
        register = [row("alpha-pd", s, due="2026-10-01") for s in ("drafted", "approved", "rejected")]
        register += [row("off-pd", "off", due="2026-10-01"), row("wait-pd", "wait", due="2026-11-04")]
        requests[3]["agency_key"], requests[4]["agency_key"] = "off-pd", "wait-pd"
        put(self.project / 'run-outputs' / RUN / fc.REGISTER_NAME, doc(register, "2026-11-05"))
        settings = {"schema": fc.SETTINGS_SCHEMA, "default": {"autonomy": "draft"},
                    "agencies": {"off-pd": {"autonomy": "off"}, "wait-pd": {"silence_after_days": 3}}}
        cal.put(self.project / fc.SETTINGS_FILE, settings)
        ids = {}
        for s in ("drafted", "approved", "rejected"):
            q = aq.add_draft(self.project, item_id=fc.item_id("alpha-pd", s, "determination", "2026-10-01"),
                kind=fc.REMINDERS[0], agency_id="alpha-pd", request_id=s, body="Synthetic reminder",
                evidence={"clock_kind": "determination", "clock_date": "2026-10-01"}, source_run_id=RUN)
            ids[s] = q["id"]
            if s == "rejected":
                aq.reject(self.project, q["id"], "G", "not needed")
        model, initial_page = self.page(requests)
        self.assertIn('Draft ready - <a href="/review?id=' + ids['drafted'], initial_page)
        model['deadlines'] = {('alpha-pd', 'drafted'): {'determination_due': {
            'date': '2026-10-01', 'label': 'COMPUTED', 'package': 'us-ca sha256:' + 'a' * 64}}}
        self.assertIn('No reply since 2026-10-01 (COMPUTED due date;', home.render_home(model, ''))
        model['deadlines'] = {}
        # Synthetic projection of an already-approved queue row; no approval operation occurs here.
        queue = [q | {"status": "approved"} if q["id"] == ids["approved"] else q for q in model["queue"]["rows"]]
        model["waiting"]["reminders"]["rows"] = fc.reminder_status(doc(register), fc.load_settings(self.project), queue, as_of="2026-11-05")
        page = home.render_home(model, '')
        section = page.split('<section id="waiting">')[1].split('</section>')[0]
        for text in ('Reminder</th>', 'Draft ready - <a href="/review?id=' + ids['drafted'] + '">Review</a>',
                     '>Approved<', '>Rejected<', 'Reminders are off for this agency (see Settings)', 'Reminder due 2026-11-08'):
            self.assertIn(text, section)
        self.assertIn('No reply since 2026-10-01 (UNVERIFIED due date; on our records, not a legal finding)', page)
        template = next(q for q in model["queue"]["rows"] if q["id"] == ids["drafted"])
        for kind in (*fc.REMINDERS, fc.NOTICE):
            model["queue"]["rows"] = [template | {"kind": kind, "evidence": {"clock_kind": "promised", "clock_date": "<date>"}}]
            self.assertIn('No reply since &lt;date&gt; (DATED due date;', home.render_home(model, ''))
        before = sorted((str(p), p.read_bytes()) for p in self.project.rglob('*') if p.is_file())
        home.load(self.project, RUN, now=NOW)
        self.assertEqual(sorted((str(p), p.read_bytes()) for p in self.project.rglob('*') if p.is_file()), before)
        with mock.patch.object(fc, 'reminder_status', side_effect=ValueError('private detail')):
            page = home.render_home(home.load(self.project, RUN, now=NOW), '')
        self.assertIn('Reminder status unavailable', page)
        self.assertNotIn('private detail', page)


class RefusalNext(unittest.TestCase):
    setUp, model = Home.setUp, Home.model
    def test_counts_source_link_zero_and_engine_label(self):
        from campaign_tool.records import denial_log
        from tests.records.test_denial_log import fixtures, queue
        states, doc = fixtures()
        entries = denial_log.entries(states, doc, [queue()])
        with mock.patch.object(home.cb.denial_log, "load", return_value={"state": "available", "entries": entries}):
            model = self.model()
        page = home.render_home(model, '')
        section = page.split('<section id="next">')[1].split('</section>')[0]
        self.assertIn('href="/board">2 refusals with no reply drafted</a>', section)
        template = model["queue"]["rows"][0]
        model["queue"]["rows"] = [template | {"kind": "followup-playbook"}]
        self.assertEqual(next(r for r in home.decision_rows(model, NOW.date()) if r["source"] == "queue")["what"], "Reply to a refusal")
        for refusals in ({"state": "available", "entries": []}, {"state": "unavailable", "code": "stale"}):
            model["refusals"] = refusals
            self.assertNotIn('refusals with no reply drafted', home.render_home(model, ''))
