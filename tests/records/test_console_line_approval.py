"""DF2007-10 (CON-LINE-APPROVE-1): the console shows each open status line's exact payload and takes G's pasted signature."""
import html
import http.client
import json
import threading
import urllib.parse
from contextlib import closing
from unittest import mock

from campaign_tool import approval
from campaign_tool.records import admin_panel as ap
from campaign_tool.records import console_apply, console_journal
from campaign_tool.records import public_updates as pu
from tests.records.test_console_apply import Apply, Interrupted
from tests.records.test_console_export import tree_state
from tests.records.test_console_model import RUN, LiveBase
from tests.records.test_console_oversight import candidate
from tests.records.test_console_snapshot import Panel
from tests.records.test_public_update_line_approval import cands

T0 = 1_793_000_000


def revision_of(project, lid):
    with pu._Locked(project) as lock:
        return pu.line_revision(pu.fold(lock.events())[0][lid])


class LiveConsole(LiveBase):
    def setUp(self):
        super().setUp()
        pu.init(self.live)
        (self.line, self.other) = [x["id"] for x in cands(n=2)]
        pu.propose(self.live, cands(n=2), T0)
        self.server = ap.build_server(self.live, RUN, actor="tester", system=False)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, origin=True):
        headers = {"Host": "127.0.0.1:%d" % self.port}
        if body is not None:
            headers.update({"Content-Type": "application/x-www-form-urlencoded", "Content-Length": str(len(body))})
        if origin:
            headers["Origin"] = "http://127.0.0.1:%d" % self.port
        with closing(http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)) as c:
            c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for k, v in headers.items():
                c.putheader(k, v)
            c.endheaders(body.encode() if body else None)
            r = c.getresponse()
            return r.status, r.read().decode()

    def approve_post(self, **fields):
        fields = {"csrf_token": self.server.csrf_token, "id": self.line, "revision": revision_of(self.live, self.line),
                  "signature": self.signer.sign(pu.line_payload(self.live, self.line)), **fields}
        fields = {k: v for k, v in fields.items() if v is not None}
        return self.request("POST", "/public-updates/approve-line", urllib.parse.urlencode(fields))

    def approvals(self):
        with pu._Locked(self.live) as lock:
            return [e for e in lock.events() if e["event"] == "line_approval"]

    def test_the_page_shows_the_exact_payload_and_the_form_and_the_post_only_records(self):
        status, page = self.request("GET", "/public-updates")
        self.assertEqual(status, 200)
        payload = pu.line_payload(self.live, self.line).decode()
        self.assertIn(payload, html.unescape(page))
        self.assertIn('action="/public-updates/approve-line"', page)
        self.assertIn(revision_of(self.live, self.line), page)
        before = tree_state(self.live)
        self.assertEqual(self.approve_post()[0], 303)
        self.assertNotEqual(tree_state(self.live), before)
        self.assertEqual(len(self.approvals()), 1)
        self.assertEqual(pu.status(self.live)["lines"], {"proposed": 2})  # recorded, not posted, not advanced
        self.assertEqual(pu.switch_state(self.live), "OFF")
        _, page = self.request("GET", "/public-updates")
        self.assertIn("Signed by owner@example.invalid for this exact text", page)
        self.assertNotIn("BEGIN SSH SIGNATURE", page)  # the raw signature never reaches a page
        self.assertEqual(pu.advance(self.live, T0 + 5)["posted"], [self.line])  # only the next advancement posts

    def test_refusals_change_nothing(self):
        stale = revision_of(self.live, self.line)
        cases = (
            (403, self.approve_post(signature="I approve")),
            (403, self.approve_post(signature="  ")),
            (403, self.approve_post(signature="x" * (approval.MAX_SIGNATURE + 1))),
            (403, self.approve_post(signature=self.signer.sign(pu.line_payload(self.live, self.other)))),  # another line's
            (403, self.approve_post(csrf_token="wrong")),
            (409, self.approve_post(revision="0" * 64)),
            (409, self.approve_post(id="0" * 16)),
            (400, self.approve_post(id="zz")),
            (400, self.approve_post(revision="short")),
            (400, self.approve_post(signature=None)),
            (400, self.approve_post(extra="1")),
        )
        for code, (status, _) in cases:
            self.assertEqual(status, code)
        fields = urllib.parse.urlencode({"csrf_token": self.server.csrf_token, "id": self.line, "revision": stale, "signature": "x"})
        self.assertEqual(self.request("POST", "/public-updates/approve-line", fields, origin=False)[0], 403)
        self.assertEqual(self.approvals(), [])
        pu.retract(self.live, self.line, "g")  # a veto first: the line is no longer open
        self.assertEqual(self.approve_post(revision=stale)[0], 409)
        pu.kill(self.live, "g")
        self.assertEqual(self.approve_post(id=self.other, signature=None)[0], 400)

    def test_an_unavailable_trust_anchor_is_an_error_page_not_an_approval(self):
        with mock.patch.object(approval, "verify", side_effect=approval.TrustUnavailable("anchor")):
            self.assertEqual(self.approve_post()[0], 503)
        self.assertEqual(self.approvals(), [])

    def test_the_route_cannot_enable_or_advance(self):
        self.assertEqual(self.approve_post()[0], 303)
        self.assertEqual(self.request("POST", "/public-updates/advance", "csrf_token=x")[0], 404)
        self.assertEqual(self.request("GET", "/public-updates")[0], 200)
        self.assertEqual(pu.switch_state(self.live), "OFF")
        self.assertEqual(self.events_of("post"), [])

    def events_of(self, kind):
        with pu._Locked(self.live) as lock:
            return [e for e in lock.events() if e["event"] == kind]


class JournalLine(Apply):
    """Journal mode: the console journals the pasted signature; decisions-apply verifies and records it."""
    lose_outcomes = Interrupted.lose_outcomes
    interrupted_run = Interrupted.interrupted_run
    assert_replays_to_applied = Interrupted.assert_replays_to_applied

    def setUp(self):
        super().setUp()
        pu.init(self.live)
        pu.propose(self.live, cands(n=2), T0)
        (self.line, self.other) = [x["id"] for x in cands(n=2)]
        self.make_export()
        self.model = json.loads((self.root / "current" / "console-model" / "public-updates.json").read_text())
        self.revision = next(r["revision"] for r in self.model["lines"] if r["id"] == self.line)
        self.payload = self.model["line_payloads"][self.line]

    def post_form(self, **fields):
        fields = {"id": self.line, "revision": self.revision, "signature": self.signer.sign(self.payload.encode()), **fields}
        return self.decide("/public-updates/approve-line", **fields)

    def live_events(self, kind="line_approval"):
        return [e for e in console_apply._events(pu, self.live) if e["event"] == kind]

    def test_the_export_carries_the_payload_and_never_a_signature(self):
        self.assertEqual(self.payload.encode(), pu.line_payload(self.live, self.line))
        self.assertEqual(self.revision, revision_of(self.live, self.line))
        self.assertEqual(sorted(self.model["line_payloads"]), sorted([self.line, self.other]))
        self.assertNotIn("signature", json.dumps(self.model).lower().replace("line_payloads", ""))
        self.up()
        page = html.unescape(self.get("/public-updates"))
        self.assertIn(self.payload, page)
        self.assertIn("approve-line", page)

    def test_a_read_only_export_shows_the_payload_but_no_form(self):
        Panel.up(self)  # no decisions dir: a read-only export
        page = html.unescape(self.get("/public-updates"))
        self.assertIn(self.payload, page)
        self.assertNotIn("approve-line", page)

    def test_the_form_only_journals_and_the_apply_step_verifies_once(self):
        self.up()
        live_before = tree_state(self.live)
        status, location, _ = self.post_form()
        self.assertEqual((status, location), (303, "/public-updates"))
        self.assertEqual(tree_state(self.live), live_before)  # nothing written to the live project
        (entry,) = self.lines()
        self.assertEqual((entry["action"], entry["target_id"], entry["revision_seen"], entry["route"]),
                         (console_journal.LINE_APPROVAL_ACTION, self.line, self.revision, "/public-updates/approve-line"))
        self.assertIn("recorded; pending apply (not approved until applied and verified)", self.get("/public-updates"))
        self.assertEqual(self.post_form()[0], 409)  # a duplicate form while one is waiting
        self.assertEqual(self.post_form(revision="0" * 64)[0], 409)
        self.assertEqual(self.post_form(id=self.other, revision="0" * 64)[0], 409)
        self.assertEqual(len(self.lines()), 1)
        self.assertEqual(self.apply(), ["APPLIED"])
        self.assertEqual(len(self.live_events()), 1)
        self.assertEqual(self.live_events("post") + self.live_events("hold") + self.live_events("switch"), [])
        self.assertEqual(self.apply(), ["ALREADY"])
        self.assertEqual(pu.advance(self.live, T0 + 3)["posted"], [self.line])

    def test_a_forged_signature_and_a_changed_line_are_refused_at_apply(self):
        self.up()
        self.assertEqual(self.post_form(signature="I approve")[0], 303)  # the console verifies nothing ...
        self.assertEqual(self.apply(), ["SIGNATURE_REFUSED"])  # ... the apply step refuses it, finally
        self.assertEqual(self.live_events(), [])
        self.assertEqual(self.apply(), ["ALREADY"])
        self.assertEqual(self.post_form(id=self.other, revision=next(r["revision"] for r in self.model["lines"] if r["id"] == self.other),
                                        signature=self.signer.sign(self.payload.encode()))[0], 303)
        self.assertEqual(self.apply()[-1], "SIGNATURE_REFUSED")  # a signature over another line's payload
        self.assertEqual(self.live_events(), [])

    def test_a_line_changed_or_vetoed_after_the_page_is_stale_or_refused_at_apply(self):
        self.up()
        self.post_form()
        pu.retract(self.live, self.line, "g")  # a veto lands first: it always wins
        self.assertEqual(self.apply(), ["INVALID_TRANSITION"])
        self.assertEqual(self.live_events(), [])
        self.post_form(id=self.other, revision=next(r["revision"] for r in self.model["lines"] if r["id"] == self.other),
                       signature=self.signer.sign(pu.line_payload(self.live, self.other)))
        raw = (self.live / pu.JOURNAL).read_text()
        (self.live / pu.JOURNAL).write_text(raw.replace("Example PD", "Changed PD"))
        self.assertEqual(self.apply()[-1], "STALE_REVISION")

    def test_trust_unavailable_defers_and_a_retry_applies_exactly_once(self):
        self.up()
        self.post_form()
        with mock.patch.object(approval, "verify", side_effect=approval.TrustUnavailable("anchor")):
            self.assertEqual(self.apply()[0], "DEFERRED")
        self.assertEqual(console_apply.receipts(self.live), {})
        self.assertEqual(self.live_events(), [])
        self.assertEqual(self.apply(), ["APPLIED"])
        self.assertEqual(len(self.live_events()), 1)

    def test_an_apply_whose_receipt_failed_replays_to_applied_without_a_second_approval(self):
        self.up()
        self.post_form()
        (entry,) = self.lines()
        self.interrupted_run()
        self.assertEqual(len(self.live_events()), 1)
        self.assert_replays_to_applied(entry["entry_id"])
        self.assertEqual(len(self.live_events()), 1)
        self.assertEqual(self.live_events("post"), [])
