"""DF2007-06: the console takes the human finding review (``records panel --analysis-root ROOT``, ``/findings/review``).

Synthetic only: the real analysis fixture (export, sync, DAG to DONE, real fact review) from the DF2007-05 tests, a throwaway
SSH key trusted in-process, and the pinned-export + decision-journal console of DFGL-16B. The console only journals; the apply
side is tests.records.test_finding_review_apply.
"""
import csv
import html
import http.client
import io
import json
import os
import re
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from campaign_tool.records import admin_panel as ap
from campaign_tool.records import console_findings, console_journal as cj, finding_review as fr, finding_review_apply as fa
from tests.records import test_analysis_packet_outbox as to, test_analysis_tick as tt
from tests.records.test_console_apply import Apply
from tests.records.test_console_export import tree_state
from tests.records.test_console_model import RUN
from tests.records.test_console_phone_journey import WIDTH, Phone, find_browser, submit_of
from tests.records.test_console_snapshot import Panel

CLAIM = "The sample search log has no search-purpose field."


class FindingConsole(Apply):
    """A pinned export + decision journal console over a real analysis root with one finding waiting for a human (N1)."""

    def setUp(self):
        super().setUp()
        self.o = to.PacketOutboxTests("test_empty_received_at_holds_the_finding_as_date_unavailable")
        self.o.start()
        self.addCleanup(self.o.doCleanups)
        self.ar, self.slug = self.o.root, tt.SLUG
        os.chmod(self.ar / ".analysis", 0o700)
        self.o.review("N1", approve=False)  # the facts are checked; no human reviewed the finding
        config = json.loads(self.o.t.config().read_text())
        self.findings = Path(config["compare"]["f3"]["store"]) / "agencies" / self.slug / "runs" / config["review"]["run_date"] / "FINDINGS.csv"
        self.journal = self.j / cj.FILE

    def up_findings(self, **kw):
        self.make_export()
        self.up(analysis_root=str(self.ar), **kw)

    def frow(self, finding="N1"):  # the finding row of the analysis root
        (row,) = [r for r in fr.list_findings(self.ar, agency=self.slug) if r["finding_id"] == finding]
        return row

    def forms(self):
        """{path: {field: value}} of the forms on the page, as a browser submits them (textareas are filled by the caller)."""
        found = {}
        for action, body in re.findall(r'<form method="post" action="(/findings/review/[a-z]+)">(.*?)</form>', self.get("/findings/review"), re.S):
            found[action] = {n: html.unescape(v) for n, v in re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)">', body)}
        return found

    def post(self, action, **fields):
        form = {k: v for k, v in self.forms()["/findings/review/" + action].items() if k != "csrf_token"}
        form.update(fields)
        return self.decide("/findings/review/" + action, **{k: v for k, v in form.items() if v is not None})

    def sign(self, text=None):
        return self.o.signer.sign((text or self.frow()["payload"]).encode("utf-8"))

    def edit_claim(self, claim):
        with open(self.findings, newline="") as handle:
            reader = csv.DictReader(handle)
            names, rows = list(reader.fieldnames or []), list(reader)
        with open(self.findings, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=names, lineterminator="\n")
            writer.writeheader()
            writer.writerows(dict(r, claim=claim) if r["finding_id"] == "N1" else r for r in rows)

    def tick_args(self, *more):
        return ("--from-journal", str(self.journal), *more)


class FindingPage(FindingConsole):
    def test_the_page_lists_the_waiting_finding_with_facts_f4_and_the_exact_payload(self):
        self.up_findings()
        row = self.frow()
        page = self.get("/findings/review")
        text = html.unescape(page)
        for want in (self.slug, "N1", CLAIM, row["topic"], self.o.digest[:12], "page 2", "confirm", "1 finding waiting", "not a human review"):
            self.assertIn(want, text)
        self.assertEqual(html.unescape(re.search(r"<pre>(.*?)</pre>", page, re.S).group(1)), row["payload"])  # the signing string, once escaped
        self.assertEqual(sorted(self.forms()), ["/findings/review/approve", "/findings/review/reject"])
        self.assertEqual(self.forms()["/findings/review/approve"]["finding_sha256"], row["finding_sha256"])
        self.assertIn('href="/findings/review"', self.get("/"))  # the secondary navigation and the connected home note
        self.assertIn("connected (CON-FIND-1)", self.get("/"))

    def test_without_an_analysis_root_the_page_says_configuration_is_needed_and_posts_are_refused(self):
        self.make_export()
        self.up()
        page = self.get("/findings/review")
        self.assertIn("needs configuration", page)
        self.assertNotIn("<form", page)
        self.assertIn("configuration required", self.get("/"))
        self.assertIn("CON-FIND-1", self.get("/"))
        before = tree_state(self.ar)
        self.assertEqual(self.decide("/findings/review/reject", agency=self.slug, finding_id="N1", finding_sha256="a" * 64, reason="x")[0], 503)
        self.assertEqual((self.lines(), tree_state(self.ar)), ([], before))

    def test_an_unreadable_analysis_view_is_said_never_shown_as_an_empty_list(self):
        self.up_findings()
        config = self.ar / "agencies" / self.slug / "review-config.json"
        good = config.read_bytes()
        config.write_text("{broken")
        page = self.get("/findings/review")
        self.assertIn("finding_inputs_unavailable", page)
        self.assertNotIn("No finding is waiting", page)
        config.write_bytes(good)
        journal = self.ar / ".analysis" / "finding-reviews.jsonl"  # a corrupt review journal defers; it is never read as empty
        journal.write_text("not json\n")
        os.chmod(journal, 0o600)
        page = self.get("/findings/review")
        self.assertIn("deferred", page)
        self.assertNotIn("<form", page)  # a deferred finding is no longer FINDING_UNREVIEWED: nothing to decide

    def test_unsafe_roots_are_refused_before_the_server_starts(self):
        self.make_export()
        link = self.tmp / "analysis-link"
        link.symlink_to(self.ar)
        for bad in (str(link), "relative/root", str(self.tmp / "missing"), str(self.root), str(self.j), str(self.root / "inside")):
            with self.assertRaises(ap.PanelError) as caught:
                ap.build_server(export_root=str(self.root), run_id="auto", actor="operator", system=False, decisions_dir=str(self.j),
                                analysis_root=bad)
            self.assertEqual(caught.exception.code, "analysis_root_invalid", bad)

    def test_get_with_a_query_is_refused(self):
        self.up_findings()
        self.get("/findings/review?x=1", 400)


class FindingPost(FindingConsole):
    def test_a_signed_approve_is_journaled_only_and_never_written_to_the_analysis_root(self):
        self.up_findings()
        row, before = self.frow(), tree_state(self.ar)
        signature = self.sign()
        status, location, _ = self.post("approve", signature=signature)
        self.assertEqual((status, location), (303, "/findings/review"))
        (entry,) = self.lines()
        self.assertEqual((entry["kind"], entry["action"], entry["target_id"], entry["revision_seen"]),
                         ("finding_review", "finding_approve", self.slug + ":N1", row["finding_sha256"]))
        self.assertEqual(json.loads(entry["value"]), {"analysis_root_sha256": fa.root_binding(self.ar), "signature": signature})
        self.assertEqual(entry["value"], cj._canon(json.loads(entry["value"])))  # compact sorted JSON
        self.assertEqual(cj.parse(self.journal.read_bytes())[0]["entry_id"], entry["entry_id"])
        self.assertEqual(tree_state(self.ar), before)  # the review journal, receipts and packets are untouched
        page = self.get("/findings/review")
        self.assertIn("applied at the next analysis tick", page)
        self.assertNotIn('action="/findings/review/approve"', page)
        self.assertEqual(self.post_again("approve")[0], 409)  # a pending decision for the same finding is refused
        self.assertEqual(len(self.lines()), 1)

    def post_again(self, action):
        return self.decide("/findings/review/" + action, agency=self.slug, finding_id="N1", finding_sha256=self.frow()["finding_sha256"],
                           **({"signature": "x"} if action == "approve" else {"reason": "x"}))

    def test_a_reject_is_journaled_with_its_private_reason(self):
        self.up_findings()
        self.assertEqual(self.post("reject", reason="Overstates what the log shows.")[0], 303)
        (entry,) = self.lines()
        self.assertEqual((entry["action"], json.loads(entry["value"])["reason"]), ("finding_reject", "Overstates what the log shows."))
        self.assertNotIn("Overstates", self.get("/findings/review"))  # the reason is private; the page shows only the pending note

    def test_refusals_journal_and_write_nothing(self):
        self.up_findings()
        sha, good = self.frow()["finding_sha256"], {"agency": self.slug, "finding_id": "N1"}
        before = tree_state(self.ar)
        port = self.server.server_port
        cases = [("approve", {**good, "finding_sha256": sha}, 400),  # missing signature
                 ("approve", {**good, "finding_sha256": sha, "signature": "s", "extra": "1"}, 400),
                 ("approve", {**good, "finding_sha256": "zz", "signature": "s"}, 400),
                 ("approve", {**good, "finding_sha256": sha, "signature": "   "}, 400),
                 ("approve", {**good, "finding_sha256": sha, "signature": "s" * 9000}, 400),
                 ("reject", {**good, "finding_sha256": sha, "reason": "r" * 501}, 400),
                 ("reject", {**good, "finding_sha256": sha, "reason": "two\nlines"}, 400),
                 ("reject", {"agency": "Bad Slug", "finding_id": "N1", "finding_sha256": sha, "reason": "r"}, 400),
                 ("reject", {**good, "finding_sha256": "f" * 64, "reason": "r"}, 409),  # a changed finding
                 ("reject", {**good, "finding_id": "N404", "finding_sha256": sha, "reason": "r"}, 409)]
        for action, fields, code in cases:
            self.assertEqual(self.decide("/findings/review/" + action, **fields)[0], code, (action, fields))
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("POST", "/findings/review/reject", urllib.parse.urlencode({"csrf_token": "wrong", **good, "finding_sha256": sha, "reason": "r"}),
                     {"Origin": "http://127.0.0.1:%d" % port, "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(conn.getresponse().status, 403)
        conn.close()
        self.assertEqual((self.lines(), tree_state(self.ar)), ([], before))

    def test_a_finding_changed_after_the_page_is_refused_as_stale(self):
        self.up_findings()
        form = self.forms()["/findings/review/reject"]  # what the person saw
        self.edit_claim("A different claim.")
        fields = {k: v for k, v in form.items() if k != "csrf_token"}
        status, _, page = self.decide("/findings/review/reject", reason="no", **fields)
        self.assertEqual(status, 409)
        self.assertEqual(self.lines(), [])

    def test_a_read_only_console_refuses_the_post(self):
        self.make_export()
        Panel.up(self, analysis_root=str(self.ar))  # a pinned export without a journal
        page = self.get("/findings/review")
        self.assertNotIn("<form", page)
        self.assertIn("Read-only", page)
        before = tree_state(self.ar)
        self.assertEqual(self.decide("/findings/review/reject", agency=self.slug, finding_id="N1",
                                     finding_sha256=self.frow()["finding_sha256"], reason="x")[0], 503)
        self.assertEqual(tree_state(self.ar), before)
        self.assertFalse(self.journal.exists())


class FindingSnapshot(FindingConsole):
    def render(self, out, root=None):
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = ap.main(["--project", str(self.live), "--run-id", RUN, "--no-system", "--analysis-root", str(root or self.ar),
                            "--render", str(out)])
        return code, err.getvalue()

    def test_the_snapshot_lists_the_finding_and_exact_text_without_any_form(self):
        out, before = self.tmp / "snap.html", tree_state(self.ar)
        self.assertEqual(self.render(out), (0, ""))
        text = out.read_text()
        self.assertEqual(html.unescape(re.search(r'id="finding-review".*?<pre>(.*?)</pre>', text, re.S).group(1)), self.frow()["payload"])
        for want in (CLAIM, self.o.digest[:12], "1 finding waiting"):
            self.assertIn(want, html.unescape(text))
        for banned in ("<form", "<textarea", "csrf", "/findings/review/approve", "/findings/review/reject"):
            self.assertNotIn(banned, text)
        self.assertEqual(tree_state(self.ar), before)

    def test_the_snapshot_never_lands_inside_the_analysis_root(self):
        inside = self.ar / "snap.html"
        before = tree_state(self.ar)
        code, err = self.render(inside)
        self.assertEqual((code, "output_collides_with_source" in err), (1, True))
        self.assertEqual(tree_state(self.ar), before)


@unittest.skipUnless(find_browser(), "no Chrome or Chromium on PATH (set DEFLOCK_TEST_CHROME)")
class FindingPhone(FindingConsole):
    def test_the_page_fits_390_px_with_a_long_claim_and_44_px_actions(self):
        self.edit_claim("Unbroken" + "x" * 200 + " " + CLAIM * 3)
        self.up_findings()
        phone = Phone(find_browser())
        self.addCleanup(phone.close)
        phone.open("http://127.0.0.1:%d/findings/review" % self.server.server_port)
        width = phone.js("({view: innerWidth, page: document.documentElement.scrollWidth, meta: !!document.querySelector('meta[name=viewport]')})")
        self.assertEqual(width["view"], WIDTH)
        self.assertTrue(width["meta"])
        self.assertLessEqual(width["page"], WIDTH, "the page is wider than the phone")
        self.assertEqual(phone.js("document.querySelector('pre').textContent"), self.frow()["payload"])  # the DOM text is the signing string
        for label, button in (("sig", "/findings/review/approve"), ("why", "/findings/review/reject")):
            box = phone.js("(() => {const b = %s.getBoundingClientRect(); return {w: b.width, h: b.height};})()" % submit_of(button))
            self.assertGreaterEqual(box["h"], 44, label)
        area = phone.js("(() => {const t = document.querySelector('textarea[name=signature]'); return {w: t.getBoundingClientRect().width, "
                        "label: !!document.querySelector('label[for=' + t.id + ']')};})()")
        self.assertTrue(area["label"])
        self.assertLessEqual(area["w"], WIDTH)


if __name__ == "__main__":
    unittest.main()
