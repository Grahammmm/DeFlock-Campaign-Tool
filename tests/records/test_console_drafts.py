"""GET /drafts: the read-only draft history over the existing queue reader (synthetic rows only)."""
import re
import threading
import unittest
from html import unescape

from campaign_tool.records import admin_panel as ap, approval_queue as aq, console_page_drafts as drafts, draft_templates
from campaign_tool.records.console_home import PLAIN_KINDS
from tests.records.test_admin_panel import AS_OF, Http
from tests.records.test_console_model import RUN
from tests.records.test_console_snapshot import Panel, RUN3, RUN4

PRIVATE = "SECRET-BODY actor-x reason-y signature-z"


class Source:
    """Stands in for the queue reader of either mode: queue_rows() returns rows or raises."""
    snapshot = False

    def __init__(self, rows=None, error=None):
        self.rows, self.error = rows, error

    def queue_rows(self):
        if self.error:
            raise self.error
        return self.rows


class Handler:
    def __init__(self, path, source):
        self.path, self.options, self.sent = path, {"project": "unused", "source": source}, None

    def _send(self, status, body):
        self.sent = (status, body)

    def _fail(self, status, *_):
        self.sent = (status, "")


def get(source, path="/drafts"):
    handler = Handler(path, source)
    assert drafts.serve(handler) is True and handler.sent is not None
    return handler.sent


def synthetic():
    """One row per state and one per queue kind (plus an unmapped one); distinct change times, one in summer (PDT)."""
    kinds = sorted(drafts.KINDS) + ["unmapped-kind"]
    rows = []
    for n, kind in enumerate(kinds):
        rows.append({"id": "%064x" % n, "item_id": "i%d" % n, "kind": kind, "status": drafts.STATES[n % 5],
                     "agency_id": ("alpha-pd", "beta-sheriff")[n % 2], "request_id": "req-%d" % n, "revision": n,
                     "created_at": "2026-01-01T00:00:%02dZ" % n, "updated_at": "2026-01-02T10:00:%02dZ" % n,
                     "body": PRIVATE, "evidence": {"why": PRIVATE}, "actor": PRIVATE, "reason": PRIVATE,
                     "approval_evidence": {"signature_sha256": PRIVATE}, "pending_apply": PRIVATE, "edit_pending": True})
    rows[0]["updated_at"], rows[0]["created_at"] = "2026-07-01T19:30:00Z", "2026-06-30T07:05:00Z"
    return rows


def table(page):
    return [[unescape(re.sub(r"<[^>]+>", "", c)) for c in re.findall(r"<td>(.*?)</td>", row)]
            for row in re.findall(r"<tr>(.*?)</tr>", page.split("<tbody>")[-1])]


class DraftHistory(unittest.TestCase):
    def test_drafts_history_includes_terminal_rows_without_private_fields(self):
        # Contract: every row the reader returns is listed, newest change first, metadata only. A pending-only
        # cut, a guessed "sent" label, a raw kind code or a leaked body/actor/signature fails here.
        rows = synthetic()
        status, page = get(Source(rows))
        self.assertEqual(status, 200)
        listed = table(page)
        self.assertEqual(len(listed), len(rows))
        self.assertEqual({r[0] for r in listed}, set(drafts.STATES))
        self.assertEqual(listed[0][:8], ["pending", PLAIN_KINDS.get(rows[0]["kind"], rows[0]["kind"]), "alpha-pd", "req-0",
                                         "2026-06-30 00:05 PDT", "2026-07-01 12:30 PDT", "0", "Review"])
        self.assertEqual([r[5] for r in listed[1:]], sorted((r[5] for r in listed[1:]), reverse=True))  # newest change first
        self.assertEqual(listed[1][5][-3:], "PST")
        by_request = {r[3]: r for r in listed}
        for row in rows:
            self.assertEqual(by_request[row["request_id"]][1], PLAIN_KINDS.get(row["kind"], row["kind"]))
            self.assertIn('href="/review?id=%s"' % row["id"], page)
        self.assertIn("All %d: " % len(rows), page)
        for state in drafts.STATES:
            self.assertIn(">%s %d</a>" % (state, sum(r["status"] == state for r in rows)), page)
        for text in (PRIVATE, "SECRET", "<form", "<button", "/approvals/", "/send"):
            self.assertNotIn(text, page)
        self.assertIn("/review?id=a%2Fb%20c", get(Source([{**rows[0], "id": "a/b c"}]))[1])  # the id is encoded
        status, page = get(Source([{**rows[0], "updated_at": "bad"}, {**rows[1], "id": "0" * 63 + "9"}]))
        self.assertEqual([r[5] for r in table(page)], ["2026-01-02 02:00 PST", "unknown"])  # unknown time sorts last
        # Contract: a reader-accepted timestamp outside Pacific range (year 0001) shows "unknown", never a 500.
        status, page = get(Source([{**rows[0], "created_at": "0001-01-01T00:00:00Z", "updated_at": "0001-01-01T00:00:00Z"}]))
        self.assertEqual((status, table(page)[0][4:6]), (200, ["unknown", "unknown"]))

    def test_filters_intersect_and_unknown_or_duplicate_values_are_refused(self):
        # Contract: one each of state/agency/kind, intersecting; anything else is a 400, not an empty list.
        rows = synthetic()
        source = Source(rows)
        want = [r for r in rows if r["status"] == "approved" and r["agency_id"] == "beta-sheriff"]
        self.assertTrue(want)
        page = get(source, "/drafts?state=approved&agency=beta-sheriff")[1]
        self.assertEqual({r[3] for r in table(page)}, {r["request_id"] for r in want})
        kind = want[0]["kind"]
        self.assertEqual([r[3] for r in table(get(source, "/drafts?kind=" + kind)[1])], [want[0]["request_id"]])
        self.assertEqual(len(table(get(source, "/drafts?state=sent")[1])), sum(r["status"] == "sent" for r in rows))
        self.assertIn("No drafts match this filter.", get(source, "/drafts?state=pending&kind=" + kind)[1])
        self.assertIn("All %d" % len(rows), get(source, "/drafts?state=pending&kind=" + kind)[1])  # counts ignore filters
        for bad in ("state=done", "state=", "agency=nobody", "kind=unknown-kind", "state=sent&state=sent", "agency=alpha-pd&agency=alpha-pd",
                    "kind=" + kind + "&kind=" + kind, "page=2", "STATE=sent"):
            with self.subTest(bad=bad):
                self.assertEqual(get(source, "/drafts?" + bad)[0], 400)
        blank = Source([{**rows[0], "agency_id": "", "kind": ""}])  # blank filters are refused even if a row's field is blank
        for bad in ("agency=", "kind="):
            self.assertEqual(get(blank, "/drafts?" + bad)[0], 400)

    def test_unavailable_queue_is_said_never_shown_as_no_drafts(self):
        # Contract: a reader or shape fault shows the existing unavailable sentence; only a good empty fold says none.
        for source, code in ((Source(error=aq.ApprovalQueueError("DRAFTQ")), "queue_invalid"),
                             (Source(error=aq.ApprovalQueueError("console_model_missing")), "console_model_missing"),
                             (Source(error=OSError("x")), "queue_io"), (Source({"rows": []}), "queue_invalid"),
                             (Source([dict(synthetic()[0], revision="0")]), "queue_invalid"), (Source(["row"]), "queue_invalid")):
            with self.subTest(code=code, source=vars(source)):
                status, page = get(source)
                self.assertEqual(status, 200)
                self.assertIn("Drafts: unavailable.", page)
                self.assertIn(drafts.esc(drafts.console_layout.reason(code)), page)
                for text in ("No drafts", "<table", "All "):
                    self.assertNotIn(text, page)
        self.assertEqual(get(Source(error=aq.ApprovalQueueError("DRAFTQ")), "/drafts?agency=x")[0], 200)
        self.assertEqual(get(Source(error=OSError("x")), "/drafts?state=bogus")[0], 400)
        self.assertIn("No drafts in the approval queue.", get(Source([]))[1])


class DraftHistoryServed(Panel):
    """The same queue through a real pinned export and through the live reader."""

    def setUp(self):
        super().setUp()
        for n, kind in enumerate(("first-request", "appeal-denial", "fee-reply", "next-social-post"), start=3):
            aq.add_draft(self.live, item_id="%064x" % n, kind=kind, agency_id="beta-sheriff", request_id="req-%d" % n,
                         body="Please %d." % n, evidence={"source": "test"}, source_run_id=RUN)
        rejected = next(r for r in aq.load_queue(self.live) if r["kind"] == "appeal-denial")
        aq.reject(self.live, rejected["id"], "operator-1", "no")

    def live_get(self, path):
        server = ap.build_server(self.live, RUN, actor="operator-1", as_of=AS_OF, system=False)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), thread.join(5), server.server_close()))
        return Http(server.server_address[1]).send("GET", path, headers={"Content-Length": None, "Content-Type": None})

    def test_drafts_export_and_live_match_and_unavailable_is_not_empty(self):
        # Contract: export mode and live mode show the same rows from the same queue (no hosted live fallback);
        # a missing export queue is unavailable, not an empty list.
        self.make_export()
        self.up()
        for query in ("", "?state=pending", "?kind=fee-reply", "?agency=caltrans&state=approved"):
            status, _, live = self.live_get("/drafts" + query)
            self.assertEqual(status, 200)
            exported = self.get("/drafts" + query)
            cut = lambda page: re.search(r'<section id="drafts">.*?</section>', page, re.S)[0]
            self.assertEqual(cut(exported), cut(live), query)
        rows = table(self.get("/drafts"))
        self.assertEqual(sorted(r[0] for r in rows), sorted(r["status"] for r in aq.load_queue(self.live)))
        self.assertEqual({r[0] for r in rows}, {"pending", "approved", "rejected"})
        self.make_export(RUN3, self.drop_queue)
        page = self.get("/drafts")
        self.assertIn("The export has no copy of this data (console_model_missing)", page)
        self.assertNotIn("No drafts", page)
        self.get("/drafts?state=bogus", 400)
        self.make_export(RUN4, self.foreign_queue)
        self.assertIn("console_model_mismatch", self.get("/drafts"))


if __name__ == "__main__":
    unittest.main()
