"""GET /timeline: one filtered, paged timeline across agencies, on a synthetic timeline (three invented agencies)."""
import http.client
import io
import json
import re
import tempfile
import threading
import unittest
import urllib.parse
from contextlib import closing
from pathlib import Path

from campaign_tool.records import admin_panel as ap, console_export, console_layout as layout
from campaign_tool.records.outcomes import timeline as tl
from tests.records.test_console_export import unlock

RUN = "run_1"
ALPHA, BETA, GAMMA = "alpha-pd", "beta-sheriff", "tracker:gamma / coroner"
ROW = re.compile(r"<tr><td>.*?</tr>")


def event(kind, day, summary=None, source="manual", **extra):
    return {"type": kind, "date": day, "summary": summary or kind + " sentence", "source": {"kind": source}, **extra}


def request(key, rid, events, marks=(), name=None):
    return {"agency_key": key, "agency": name or key.upper(), "request_id": rid, "ledger_request_id": "L-" + rid, "stage": "sent",
            "closed": False, "clocks": {}, "extension_chain": {}, "issues": [], "overdue_marks": list(marks),
            "next_action": {"owner": "agency", "action": "wait"}, "events": events}


def write(project, requests):
    path = project / "run-outputs" / RUN / "outcomes" / "timeline.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": tl.SCHEMA, "run_id": RUN, "as_of": "2026-11-05", "requests": requests}))


def mixed():
    """Three agencies, every event kind once, plus a tie, an aware timestamp either side of a Pacific month end,
    a register extension (COMPUTED), a promised date (INFERRED) and an undated extension notice."""
    kinds = list(tl.EVENT_TYPES)
    alpha = [event(k, "2026-10-%02d" % (i + 1)) for i, k in enumerate(kinds[:9])]
    alpha += [event("call", "2026-11-01T05:00:00Z", "late Oct 31 in Pacific"), event("sent", "2026-11-02T03:30:00Z", "evening Nov 1 in Pacific"),
              event("promised date", "2026-11-20", source="register", new_dates=["2026-11-20"])]
    beta = [event(k, "2026-09-%02d" % (i + 1)) for i, k in enumerate(kinds[9:])]
    beta += [event("extension notice", None, "extension recorded (no notice date held)", source="register"),
             event("extension notice", "2026-11-23", "extension limit", source="register")]
    gamma = [event("call", "2026-11-10", "same day"), event("call", "2026-11-10", "same day again")]
    marks = [{"kind": "determination", "label": tl.OVERDUE_LABEL, "date": "2026-10-20", "basis": "recorded_receipt"}]
    return [request(ALPHA, "R-1", alpha, marks), request(BETA, "R-2", beta), request(GAMMA, "R-3", gamma, name="Gamma Coroner")]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)

    def serve(self, server):
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        port = server.server_address[1]

        def get(path):
            with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=5)) as conn:
                conn.putrequest("GET", path, skip_host=True)
                conn.putheader("Host", "127.0.0.1:%d" % port)
                conn.endheaders()
                response = conn.getresponse()
                return response.status, response.read().decode()
        return get

    def live(self):
        return self.serve(ap.build_server(self.project, RUN, actor="op", system=False))

    def rows(self, get, path, status=200):
        code, body = get(path)
        self.assertEqual(code, status, path)
        return ROW.findall(body), body


class TimelinePage(Base):
    def test_timeline_combines_agencies_filters_then_pages(self):
        # Contract: one newest-first list over all agencies; each filter narrows it; unknown input is 400; the
        # filter applies before paging at 100. Fails if filters run after paging, or a UTC day shifts a Pacific one.
        write(self.project, mixed())
        get = self.live()
        rows, body = self.rows(get, "/timeline")
        self.assertEqual(len(rows), sum(len(r["events"]) for r in mixed()))
        for kind in tl.EVENT_TYPES:
            self.assertIn("<td>%s</td>" % kind, "".join(rows))
        dates = [re.match(r"<tr><td>([^<]*)</td>", r)[1] for r in rows]
        self.assertEqual(dates, sorted(dates[:-1], reverse=True) + ["undated"])  # newest first, undated last
        self.assertIn("2026-11-01</td>", "".join(rows))  # 2026-11-02T03:30Z is the evening of 11-01 in Pacific
        self.assertIn("2026-10-31</td><td>%s" % '<a href="/agency?agency=alpha-pd">ALPHA-PD</a>', "".join(rows))
        same = [r for r in rows if "same day" in r]  # a tie keeps input order
        self.assertEqual([("same day</td>" in r) for r in same], [True, False])
        self.assertIn('href="/agency?agency=tracker%3Agamma%20%2F%20coroner"', body)  # encoded key, admitted agency only
        # Labels: recorded, stated and computed dates are told apart; the disclaimer survives.
        self.assertIn("<td>INFERRED</td>", next(r for r in rows if "promised date" in r and "2026-11-20" in r))
        self.assertIn("<td>COMPUTED</td>", next(r for r in rows if "extension limit" in r))
        self.assertIn("<td>UNKNOWN</td>", next(r for r in rows if "undated" in r))
        self.assertIn("<td>DATED</td>", next(r for r in rows if "same day</td>" in r))
        self.assertIn("arithmetic on our records, not a legal finding", body)
        self.assertIn("OVERDUE (COMPUTED, basis DATED): " + tl.OVERDUE_LABEL, body)
        self.assertEqual(re.findall(r'aria-current="page">([^<]*)<', body), ["Timeline"])
        self.assertIn('<a href="/timeline" aria-current="page">Timeline</a>', body)
        # Each filter on its own, then together.
        self.assertEqual(len(self.rows(get, "/timeline?agency=" + urllib.parse.quote(GAMMA, safe=""))[0]), 2)
        self.assertEqual(len(self.rows(get, "/timeline?kind=call")[0]), 4)
        self.assertEqual(len(self.rows(get, "/timeline?month=2026-10")[0]), 9 + 1)  # the Pacific Oct 31 call counts as October
        rows, body = self.rows(get, "/timeline?month=2026-11&agency=alpha-pd&kind=sent")
        self.assertEqual(len(rows), 1)
        self.assertIn("evening Nov 1 in Pacific", rows[0])
        rows, body = self.rows(get, "/timeline?month=2025-01")
        self.assertEqual((rows, "No events match these filters." in body), ([], True))  # a real empty match, not unavailable
        self.assertNotIn("Timeline unavailable", body)

    def test_timeline_refuses_unknown_duplicate_and_extra_filters(self):
        write(self.project, mixed())
        get = self.live()
        for query in ("agency=nobody", "agency=a%0Ab", "month=2026-13", "month=2026-00", "month=2026-1", "month=November",
                      "kind=bogus", "kind=Call", "page=0", "page=-1", "page=abc", "page=", "page=2", "page=999999999",
                      "agency=alpha-pd&agency=beta-sheriff", "kind=call&kind=sent", "page=1&page=1", "filter=overdue",
                      "agency=alpha-pd&extra=1", "agency=alpha-pd&&kind=call", "agency"):
            with self.subTest(query=query):
                self.assertEqual(get("/timeline?" + query)[0], 400)
        self.assertEqual(self.rows(get, "/timeline?agency=&month=&kind=")[0].__len__(), len(self.rows(get, "/timeline")[0]))  # blank form fields

    def test_timeline_pages_at_100_after_filtering_and_keeps_filters_in_links(self):
        busy = [event("call", "2026-10-%02d" % (1 + i % 28), "busy %03d" % i) for i in range(100)] + [event("call", "2026-11-02", "busy last")]
        write(self.project, [request(ALPHA, "R-1", busy), request(BETA, "R-2", [event("sent", "2026-10-05", "quiet %d" % i) for i in range(30)])])
        get = self.live()
        self.assertEqual([len(self.rows(get, "/timeline" + q)[0]) for q in ("", "?page=2")], [100, 31])
        rows, body = self.rows(get, "/timeline?agency=alpha-pd")
        self.assertEqual((len(rows), len(self.rows(get, "/timeline?agency=alpha-pd&page=2")[0])), (100, 1))  # filter first, then page
        self.assertIn('href="/timeline?agency=alpha-pd&amp;page=2">Next page', body)
        self.assertNotIn("Previous page", body)
        rows, body = self.rows(get, "/timeline?agency=alpha-pd&kind=call&page=2")
        self.assertIn('href="/timeline?agency=alpha-pd&amp;kind=call">Previous page', body)
        self.assertNotIn("Next page", body)
        rows, body = self.rows(get, "/timeline?agency=alpha-pd&month=2026-10")  # exactly 100: one page, no Next
        self.assertEqual(len(rows), 100)
        self.assertNotIn("Next page", body)
        self.assertEqual(get("/timeline?agency=alpha-pd&month=2026-10&page=2")[0], 400)
        self.assertEqual(get("/timeline?kind=sent&page=2")[0], 400)  # 30 rows: no second page

    def test_timeline_preserves_live_export_rows_and_escapes_summary(self):
        # Contract: the page projects the pinned run only (live and export agree) and shows record text as text;
        # an unreadable timeline says so instead of showing an empty list.
        hostile = '<script>alert(1)</script> "q" & <img src=x onerror=1>'
        write(self.project, [request(ALPHA, 'R-"1"<b>', [event("call", "2026-11-03", hostile), event("sent", "2026-11-01")],
                                     name="<i>Alpha</i>"),
                             request(BETA, "R-2", [event("denial", "2026-11-02", "refused")])])
        live_rows, live_body = self.rows(self.live(), "/timeline")
        for raw in ("<script>", "<img", "<i>Alpha", "<b>"):
            self.assertNotIn(raw, live_body)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; &quot;q&quot; &amp; &lt;img src=x onerror=1&gt;", live_body)
        root = Path(self.tmp.name) / "root"
        root.mkdir(mode=0o700)
        self.addCleanup(unlock, root)
        dest = Path(self.tmp.name) / "dest"
        dest.mkdir(mode=0o700)
        console_export.build(str(self.project), RUN, str(dest))
        buf = io.BytesIO()
        console_export.write_tar(str(dest), buf)
        buf.seek(0)
        console_export.install(str(root), buf)
        export_rows, export_body = self.rows(self.serve(ap.build_server(export_root=str(root), run_id="auto", actor="op", system=False)),
                                             "/timeline")
        self.assertEqual(export_rows, live_rows)
        self.assertEqual(len(live_rows), 3)
        self.assertIn("export ", export_body)
        self.assertNotIn("<form method=\"post\"", export_body)
        # Unreadable timeline: named unavailable, no table, never a clean empty list.
        for case in ("missing", "stale", "malformed"):
            with self.subTest(case=case):
                write(self.project, [])
                path = self.project / "run-outputs" / RUN / "outcomes" / "timeline.json"
                if case == "missing":
                    path.unlink()
                else:
                    doc = json.loads(path.read_text())
                    doc.update({"run_id": "other"} if case == "stale" else {"requests": [{"agency_key": 1}]})
                    path.write_text(json.dumps(doc))
                rows, body = self.rows(self.live(), "/timeline")
                self.assertEqual(rows, [])
                self.assertIn("Timeline unavailable", body)
                self.assertNotIn("No events", body)
                self.assertNotIn("<table", body)
        self.assertEqual(self.live()("/timeline?kind=bogus")[0], 400)  # still validated while unavailable

    def test_nav_timeline_link_points_at_the_page_and_home_keeps_its_section(self):
        self.assertIn(("Timeline", "/timeline"), layout.LINKS)
        write(self.project, mixed())
        status, body = self.live()("/")
        self.assertEqual(status, 200)
        self.assertIn('<section id="timeline">', body)
        self.assertIn('<a href="/timeline">Timeline</a>', body)
