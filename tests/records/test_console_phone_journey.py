"""R4-DF-03: the organizer's decisions driven in a real browser at phone width (390 px) against a synthetic export.

The console is started the way the hosted decision page runs (``records panel --export-root DIR --run-id auto
--decisions-dir J``) over a synthetic export. A headless Chrome, sized and touch-emulated as a phone, opens the home
page, taps through to one item and decides it with real taps and typing: reject (with a reason), edit (the draft
text), tie a received message to a request (pick the request, paste the approval signature) and approve (paste the
approval signature). Each decision must end as exactly one line of the decision journal and as "pending apply" on the
page; the export and the live project must stay untouched. Until now these decisions were only tested by posting forms
straight to the server, so a missing field, a control off screen or a page wider than the phone went unnoticed.

The browser is driven over the Chrome DevTools Protocol with the standard library only (no Selenium or Playwright
package). Chrome is found on PATH (GitHub's ubuntu runners ship google-chrome) or in DEFLOCK_TEST_CHROME. With no
browser the tests are skipped, unless DEFLOCK_REQUIRE_BROWSER=1 makes that a failure.

The approval signature typed here is placeholder text: the journal never verifies a signature (the next records run
does, see console_journal), so nothing here proves a signature check.

KNOWN DEFECT (ORIGIN_DEFECT), found by this test: a real browser's decision POST is refused. See the last test.
"""
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from campaign_tool.records import approval_queue as aq
from campaign_tool.records import console_journal, console_origin
from campaign_tool.records.outcomes import unmatched
from tests.records.test_console_export import tree_state
from tests.records.test_console_journal import Journal
from tests.records.test_console_model import RUN

WIDTH, HEIGHT = 390, 844
BROWSERS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")
SIGNATURE = "-----BEGIN SSH SIGNATURE-----\nU1NIU0lHplaceholderOnlyNotVerifiedHere\n-----END SSH SIGNATURE-----"
TIMEOUT = 30


def find_browser():
    named = os.environ.get("DEFLOCK_TEST_CHROME")
    if named:
        return named if os.access(named, os.X_OK) else None
    return next(filter(None, map(shutil.which, BROWSERS)), None)


class CdpError(Exception):
    pass


class WebSocket:
    """The least of RFC 6455 that the DevTools endpoint needs: one text message out, one text message in."""

    def __init__(self, host, port, path):
        self.sock = socket.create_connection((host, port), timeout=TIMEOUT)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key)).encode())
        self.buffer = b""
        while b"\r\n\r\n" not in self.buffer:
            self.buffer += self._chunk()
        head, _, self.buffer = self.buffer.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise CdpError("websocket refused: %r" % head[:80])

    def _chunk(self):
        data = self.sock.recv(65536)
        if not data:
            raise CdpError("browser closed the connection")
        return data

    def _take(self, count):
        while len(self.buffer) < count:
            self.buffer += self._chunk()
        data, self.buffer = self.buffer[:count], self.buffer[count:]
        return data

    def send(self, text):
        data = text.encode()
        size = len(data)
        head = bytes([0x81])
        if size < 126:
            head += bytes([0x80 | size])
        elif size < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", size)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", size)
        mask = os.urandom(4)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self):
        message = b""
        while True:
            first, second = self._take(2)
            size = second & 0x7F
            if size == 126:
                size = struct.unpack(">H", self._take(2))[0]
            elif size == 127:
                size = struct.unpack(">Q", self._take(8))[0]
            payload = self._take(size)  # a server never masks
            opcode = first & 0x0F
            if opcode == 0x8:
                raise CdpError("browser closed the websocket")
            if opcode == 0x9:  # ping: answer with the same payload
                mask = os.urandom(4)
                self.sock.sendall(bytes([0x8A, 0x80 | len(payload)]) + mask +
                                  bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
                continue
            if opcode in (0x0, 0x1, 0x2):
                message += payload
                if first & 0x80:
                    return message.decode()

    def close(self):
        self.sock.close()


class Phone:
    """One headless Chrome with one tab sized and touch-emulated as a 390 px phone."""

    def __init__(self, binary, *, mend_origin):
        self.nulled = 0  # POSTs the browser sent with "Origin: null" (see ORIGIN_DEFECT)
        self.profile = tempfile.mkdtemp(prefix="deflock-phone-")
        self.process = subprocess.Popen(
            [binary, "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", "--no-first-run",
             "--no-default-browser-check", "--disable-extensions", "--disable-background-networking",
             "--password-store=basic", "--remote-debugging-port=0", "--user-data-dir=" + self.profile, "about:blank"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.ws = None
        self.counter = 0
        self.events = []
        try:
            port = self._debug_port()
            with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % port, timeout=TIMEOUT) as response:
                page = next(t for t in json.load(response) if t["type"] == "page")
            self.ws = WebSocket("127.0.0.1", port, "/devtools/page/" + page["id"])
            self.call("Page.enable")
            self.call("Runtime.enable")
            self.call("Emulation.setDeviceMetricsOverride", width=WIDTH, height=HEIGHT, deviceScaleFactor=3, mobile=True)
            self.call("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=1)
            if mend_origin:
                self.call("Fetch.enable", patterns=[{"urlPattern": "*", "requestStage": "Request"}])
        except BaseException:
            self.close()
            raise

    def _debug_port(self):
        marker = Path(self.profile) / "DevToolsActivePort"
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise CdpError("the browser exited at start (code %s)" % self.process.returncode)
            try:
                lines = marker.read_text().splitlines()
                if len(lines) >= 2:
                    return int(lines[0])
            except (OSError, ValueError):
                pass
            time.sleep(0.05)
        raise CdpError("the browser never opened its debugging port")

    def close(self):
        if self.ws is not None:
            self.ws.close()
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, 15)
            except OSError:
                pass
            try:
                self.process.wait(10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.process.pid, 9)
                except OSError:
                    pass
                self.process.wait(10)
        else:
            self.process.wait()
        shutil.rmtree(self.profile, ignore_errors=True)

    def call(self, method, **params):
        mine = self._next_id()
        self.ws.send(json.dumps({"id": mine, "method": method, "params": params}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == mine:
                if "error" in message:
                    raise CdpError("%s: %s" % (method, message["error"]))
                return message["result"]
            if message.get("method") == "Fetch.requestPaused":
                self._release(message["params"])
            elif "method" in message:
                self.events.append(message["method"])

    def _release(self, paused):
        """Let every request go on. Only a form POST that carries `Origin: null` gets its Origin replaced with the
        page's own origin, which is what the console's Origin check expects (ORIGIN_DEFECT); nothing else changes."""
        request = paused["request"]
        headers = dict(request["headers"])
        if request["method"] == "POST" and headers.get("Origin") == "null":
            self.nulled += 1
            parts = urllib.parse.urlsplit(request["url"])
            headers["Origin"] = "%s://%s" % (parts.scheme, parts.netloc)
        self.ws.send(json.dumps({"id": self._next_id(), "method": "Fetch.continueRequest", "params": {
            "requestId": paused["requestId"], "headers": [{"name": k, "value": v} for k, v in headers.items()]}}))

    def _next_id(self):
        self.counter += 1
        return self.counter

    def js(self, expression):
        result = self.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in result:
            raise CdpError("script failed: %s" % json.dumps(result["exceptionDetails"])[:300])
        return result["result"].get("value")

    def wait_for(self, expression, what):
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            if self.js(expression):
                return
            time.sleep(0.05)
        raise CdpError("timed out waiting for " + what)

    def open(self, url):
        self.call("Page.navigate", url=url)
        self.settled()

    def settled(self):
        """The page now showing is a fresh, completely loaded document (not the one a tap started from)."""
        self.wait_for("document.readyState === 'complete' && !window.__old", "the page to load")

    def tap(self, target, *, settle=False):
        """A real touch at the middle of the control. It must be on screen and hit-testable there: whatever is
        really at that point has to be the control (or inside it), or the tap would land on something else."""
        box = self.js("""(() => {
            const el = (%s);
            if (!el) return null;
            el.scrollIntoView({block: 'center', inline: 'center'});
            const r = el.getBoundingClientRect();
            const x = r.left + r.width / 2, y = r.top + r.height / 2;
            const hit = document.elementFromPoint(x, y);
            return {x: x, y: y, w: r.width, h: r.height, inside: x >= 0 && y >= 0 && x < innerWidth && y < innerHeight,
                    hit: !!hit && (hit === el || el.contains(hit) || hit.contains(el))};
        })()""" % target)
        if box is None:
            raise CdpError("no such control: " + target)
        if not (box["inside"] and box["hit"]):
            raise CdpError("control cannot be tapped at %d px wide: %s %r" % (WIDTH, target, box))
        if settle:
            self.js("window.__old = true")
        point = [{"x": box["x"], "y": box["y"], "id": 1}]
        self.call("Input.dispatchTouchEvent", type="touchStart", touchPoints=point)
        self.call("Input.dispatchTouchEvent", type="touchEnd", touchPoints=[])
        if settle:
            self.settled()
        return box

    def type_into(self, target, text):
        """Tap the field (focus by touch), clear what it holds and type the text as the keyboard would insert it."""
        self.tap(target)
        if not self.js("(%s) === document.activeElement" % target):
            raise CdpError("the tap did not focus the field: " + target)
        self.js("(%s).select()" % target)
        self.call("Input.insertText", text=text)

    def text(self):
        return self.js("document.body.innerText")


def by_label(text):
    """The control a person would find by its visible label (a label's text, whole or leading)."""
    return ("[...document.querySelectorAll('label')].filter(l => l.textContent.trim().startsWith(%s))"
            ".map(l => l.control)[0]" % json.dumps(text))


def radio_choice(value):
    return "document.querySelector('input[type=radio][name=choice][value=' + CSS.escape(%s) + ']')" % json.dumps(value)


def submit_of(action):
    return "document.querySelector('form[action=\"%s\"] button[type=submit]')" % action


def link_to(path):
    return "document.querySelector('a[href=\"%s\"]')" % path


class PhoneJourney(Journal):
    """Synthetic export with three pending drafts and one received message to tie, served in journal mode."""

    mend_origin = True  # ORIGIN_DEFECT: remove this (and Phone._release) once the console accepts a real browser's POST

    def setUp(self):
        super().setUp()
        binary = find_browser()
        if binary is None:
            if os.environ.get("DEFLOCK_REQUIRE_BROWSER") == "1":
                self.fail("DEFLOCK_REQUIRE_BROWSER=1 but no Chrome or Chromium was found")
            self.skipTest("no Chrome or Chromium on PATH (set DEFLOCK_TEST_CHROME)")
        self.reject_id, self.edit_id, self.approve_id = (self.draft(n, "Please extend %d." % n) for n in (11, 12, 13))
        self.tie_id = self.message_link()
        self.make_export()
        self.up()
        self.root_before, self.live_before = tree_state(self.root), tree_state(self.live)
        self.base = "http://127.0.0.1:%d" % self.server.server_port
        # Registered after the server's own cleanup, so it runs first: the browser's idle keep-alive connections
        # must be gone before the server is closed or closing waits for them.
        self.phone = Phone(binary, mend_origin=self.mend_origin)
        self.addCleanup(self.phone.close)
        self.binary = binary

    def draft(self, n, body):
        aq.add_draft(self.live, item_id="%064x" % n, kind="reply-extension", agency_id="caltrans", request_id="req-%d" % n,
                     body=body, evidence={"source": "test"}, source_run_id=RUN)
        return aq.draft_id("%064x" % n, "reply-extension")

    def message_link(self):
        sha = "ab" * 32
        options = [{"request_id": "req-b", "agency_id": "caltrans"}, {"request_id": "req-a", "agency_id": "caltrans"}]
        aq.queue_message_links(self.live, RUN, [unmatched._draft(
            {"message_sha256": sha, "received_date": "2026-11-09", "sender_domain": "caltrans.example",
             "attachment_count": 1, "candidates": options, "agency_id": "caltrans"})])
        return aq.draft_id(sha, "message_link")

    # ---- what every phone step checks -------------------------------------------------------------------------

    def fits(self, where):
        width = self.phone.js("({view: innerWidth, page: document.documentElement.scrollWidth, "
                              "meta: !!document.querySelector('meta[name=viewport]')})")
        self.assertEqual(width["view"], WIDTH, where)
        self.assertTrue(width["meta"], where + ": no viewport meta tag")
        self.assertLessEqual(width["page"], WIDTH, "%s: the page is wider than the phone (%s px)" % (where, width["page"]))

    def start_at(self, item_id):
        """Home, then the one tap a person makes: the item in the 'needs your decision' list."""
        self.phone.open(self.base + "/")
        self.fits("home")
        self.phone.tap(link_to("/review?id=" + item_id), settle=True)
        self.assertEqual(self.phone.js("location.pathname + location.search"), "/review?id=" + item_id)
        self.fits("review " + item_id[:8])

    def entries(self):
        return self.lines()

    def only_new_entry(self, count_before, **expected):
        entries = self.entries()
        self.assertEqual(len(entries), count_before + 1, entries)
        entry = entries[-1]
        for key, value in expected.items():
            self.assertEqual(entry[key], value, key)
        self.assertEqual((entry["schema"], entry["actor"], entry["source_project_id"], entry["export_run_id"]),
                         (console_journal.SCHEMA, "operator", self.pid, RUN))
        return entry

    def nothing_else_changed(self):
        self.assertEqual(tree_state(self.root), self.root_before, "the export changed")
        self.assertEqual(tree_state(self.live), self.live_before, "the live project changed")

    # ---- the four decisions -----------------------------------------------------------------------------------

    def test_reject_with_a_reason(self):
        self.start_at(self.reject_id)
        before = self.entries()
        self.phone.type_into(by_label("Rejection reason"), "   ")  # spaces pass the phone's own check, not the server's
        self.phone.tap(submit_of("/approvals/reject"), settle=True)
        self.assertEqual(self.entries(), before)
        self.assertIn("Say why you are rejecting", self.phone.text())
        self.fits("refused reject")
        self.phone.type_into(by_label("Rejection reason"), "Not our request: wrong agency")
        self.phone.tap(submit_of("/approvals/reject"), settle=True)
        entry = self.only_new_entry(len(before), action="reject", target_id=self.reject_id, route="/approvals/reject",
                                    value="Not our request: wrong agency", revision_seen=0)
        self.assertNotIn("choice", entry)
        self.assertEqual(self.phone.js("location.pathname + location.search"), "/review?id=" + self.reject_id)
        self.assertIn("pending apply: reject", self.phone.text())
        self.fits("after reject")
        self.nothing_else_changed()

    def test_edit_the_draft_text(self):
        self.start_at(self.edit_id)
        before = self.entries()
        self.assertEqual(before, [])
        new_text = "Please extend the deadline by ten days.\nThank you."
        self.phone.type_into(by_label("Draft text"), new_text)
        self.phone.tap(submit_of("/approvals/edit"), settle=True)
        entry = self.only_new_entry(0, action="edit", target_id=self.edit_id, route="/approvals/edit", value=new_text,
                                    revision_seen=0)
        self.assertNotIn("choice", entry)
        self.assertIn("pending apply: edit", self.phone.text())
        self.fits("after edit")
        self.nothing_else_changed()

    def test_tie_a_message_to_a_request(self):
        self.start_at(self.tie_id)
        page = self.phone.text()
        self.assertIn("Tie this message to a request", page)
        before = self.entries()
        self.phone.tap(radio_choice("req-b"))
        self.assertTrue(self.phone.js("(%s).checked" % radio_choice("req-b")), "the tap did not choose the request")
        self.phone.type_into(by_label("Detached SSH signature"), SIGNATURE)
        self.phone.tap(submit_of("/approvals/approve"), settle=True)
        entry = self.only_new_entry(len(before), action="approve", target_id=self.tie_id, route="/approvals/approve",
                                    value=SIGNATURE, revision_seen=0, choice="req-b")
        self.assertEqual(entry["choice"], "req-b")
        self.assertIn("pending apply: approve", self.phone.text())
        self.fits("after tie")
        self.nothing_else_changed()

    def test_approve_with_a_signature(self):
        self.start_at(self.approve_id)
        before = self.entries()
        self.phone.type_into(by_label("Detached SSH signature"), SIGNATURE)
        self.phone.tap(submit_of("/approvals/approve"), settle=True)
        entry = self.only_new_entry(len(before), action="approve", target_id=self.approve_id, route="/approvals/approve",
                                    value=SIGNATURE, revision_seen=0)
        self.assertNotIn("choice", entry)
        self.assertIn("pending apply: approve", self.phone.text())
        self.fits("after approve")
        self.nothing_else_changed()

    def test_known_defect_a_browser_post_carries_origin_null_and_is_refused(self):
        """ORIGIN_DEFECT. Every response carries `Referrer-Policy: no-referrer`; with that policy a browser sends
        `Origin: null` on a same-origin form POST, and the console accepts only its own exact origin. So a decision
        made in a real browser is refused (403, "Refused") and nothing reaches the journal. The four tests above
        replace that one header, in the browser, so the rest of each journey can be tested. When the console is fixed
        (for example `Referrer-Policy: same-origin`) this test fails on purpose: then delete it, `mend_origin` and
        `Phone._release`, and the journeys run on the browser's own request."""
        plain = Phone(self.binary, mend_origin=False)
        self.addCleanup(plain.close)
        seen = []
        real = console_origin.request_ok

        def spy(handler, post):
            if post:
                seen.append(handler.headers.get("Origin"))
            return real(handler, post)

        with mock.patch.object(console_origin, "request_ok", spy):
            plain.open(self.base + "/review?id=" + self.reject_id)
            plain.type_into(by_label("Rejection reason"), "Not our request")
            plain.tap(submit_of("/approvals/reject"), settle=True)
        if self.entries():
            self.fail("the console now accepts a plain browser POST: remove ORIGIN_DEFECT (mend_origin, "
                      "Phone._release and this test)")
        self.assertEqual(seen, ["null"])
        self.assertIn("Refused", plain.text())
        self.nothing_else_changed()


if __name__ == "__main__":
    unittest.main()
