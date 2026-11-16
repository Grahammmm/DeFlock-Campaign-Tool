"""DF2007-08: the console lists redactions waiting for sign-off and journals a pasted signature (``records panel --flock-store``).

Synthetic store and values only. The console verifies nothing: the verifier is patched in the test process for the replay step
(``flock_redact.py signoff-apply``), which is where a signature is checked.
"""
import contextlib
import hashlib
import html
import http.client
import io
import json
import os
import re
import urllib.parse
from pathlib import Path
from unittest import mock

from campaign_tool import approval
from campaign_tool.records import admin_panel as ap
from campaign_tool.records import console_journal as cj
from tests.flock.test_redact import SIGNATURE, VALUES, redact, scan
from tests.records.test_console_apply import Apply
from tests.records.test_console_export import tree_state
from tests.records.test_console_journal import OpenSpy

SLUG = "synthetic"


class Redactions(Apply):
    def setUp(self):
        super().setUp()
        self.store = self.tmp / "flock-store"
        (self.store / "originals").mkdir(parents=True, mode=0o700)
        self.store.chmod(0o700)
        self.source = self.store / "originals" / "original.txt"
        self.raw = ("\n".join(VALUES) + "\n").encode()
        self.source.write_bytes(self.raw)
        self.source.chmod(0o600)
        self.sha = redact.digest(self.raw)
        receipt = self.store / "scan.json"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(scan.main([str(self.source), "--json-out", str(receipt)]), 3)
        self.cli("propose", "--scan-receipt", receipt)
        self.folder = redact.record.record_dir(self.store, SLUG, self.sha)
        self.proposal = (self.folder / "PROPOSAL.json").read_bytes()
        self.digest = redact.digest(self.proposal)
        self.verify = mock.patch.object(approval, "verify", return_value="synthetic-reviewer").start()
        self.addCleanup(mock.patch.stopall)
        self.make_export()

    def cli(self, command, *extra, expected=0):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = redact.main([command, str(self.store), SLUG, self.sha, str(self.source), *map(str, extra)])
        self.assertEqual((code, [v for v in VALUES if v in out.getvalue()]), (expected, []), out.getvalue())
        return out.getvalue()

    def up_store(self, **kw):
        self.up(flock_store=str(self.store), **kw)

    def form(self):
        page = self.get("/redactions")
        (body,) = re.findall(r'<form method="post" action="/redactions/signoff">(.*?)</form>', page, re.S)
        return {n: html.unescape(v) for n, v in re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)">', body)}

    def post(self, **fields):
        form = {k: v for k, v in self.form().items() if k != "csrf_token"}
        form.update(fields)
        return self.decide("/redactions/signoff", **{k: v for k, v in form.items() if v is not None})

    def replay(self, expected=0):
        return self.cli("signoff-apply", "--decisions", self.j, expected=expected)


class ListingPage(Redactions):
    def test_pending_proposal_exact_payload(self):
        self.up_store()
        page = self.get("/redactions")
        text = html.unescape(page)
        for want in (SLUG, self.sha[:12], self.sha, "1 proposal waiting", "email", "phone", "plate", "not exhaustive"):
            self.assertIn(want, text)
        (pre,) = re.findall(r"<pre>(.*?)</pre>", page, re.S)
        self.assertEqual(html.unescape(pre).encode(), redact.payload(self.proposal, self.sha))  # byte for byte what payload prints
        form = self.form()
        self.assertTrue(form.pop("csrf_token"))
        self.assertEqual(form, {"slug": SLUG, "sha": self.sha, "proposal_sha256": self.digest})
        for value in VALUES:
            self.assertNotIn(value, page)
        self.assertEqual(self.cli("payload").encode(), redact.payload(self.proposal, self.sha))

    def test_a_not_required_proposal_is_listed_and_a_signed_one_is_not(self):
        self.up_store()
        self.assertIn("1 proposal waiting", self.get("/redactions"))
        self.cli("signoff", "--signature", self.write_signature())
        page = self.get("/redactions")
        self.assertIn("No redaction proposal is waiting", page)
        self.assertNotIn("<form", page)

    def write_signature(self):
        path = self.tmp / "signature"
        path.write_text(SIGNATURE)
        path.chmod(0o600)
        return path

    def test_a_dangling_signoff_link_disables_the_action(self):
        self.up_store()
        os.symlink(self.folder / "nowhere", self.folder / "SIGNOFF.json")
        self.assertNotIn("<form", self.get("/redactions"))

    def test_no_flag_opens_nothing_and_an_empty_store_is_honest(self):
        self.up()
        page = self.get("/redactions")
        self.assertIn("needs configuration", page)
        self.assertNotIn("<form", page)

    def test_an_empty_store_is_honest(self):
        empty = self.tmp / "empty-store"
        empty.mkdir(mode=0o700)
        self.up(flock_store=str(empty))
        self.assertIn("No redaction proposal is waiting", self.get("/redactions"))
        self.assertIn("0 proposals waiting", self.get("/redactions"))

    def test_an_unreadable_proposal_is_said_and_has_no_form(self):
        self.up_store()
        self.folder.joinpath("PROPOSAL.json").write_bytes(self.proposal + b" ")
        page = self.get("/redactions")
        self.assertIn("could not be read safely", page)
        self.assertNotIn("<form", page)

    def test_a_missing_store_after_start_is_unavailable_not_empty(self):
        self.up_store()
        os.rename(self.store, self.tmp / "moved-store")
        page = self.get("/redactions")
        self.assertNotIn("No redaction proposal is waiting", page)
        self.assertIn("flock_store_unavailable", page)

    def test_the_page_is_read_only_without_a_journal(self):
        from tests.records.test_console_snapshot import Panel
        Panel.up(self, flock_store=str(self.store))
        page = self.get("/redactions")
        self.assertIn("Read-only", page)
        self.assertNotIn("<form", page)
        self.assertEqual(self.decide_plain()[0], 503)

    def decide_plain(self):
        port = self.server.server_port
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            connection.request("POST", "/redactions/signoff", urllib.parse.urlencode({"csrf_token": self.server.csrf_token}),
                               {"Origin": "http://127.0.0.1:%d" % port, "Content-Type": "application/x-www-form-urlencoded"})
            return connection.getresponse().status, None
        finally:
            connection.close()

    def test_unsafe_stores_are_refused_before_the_server_starts(self):
        link = self.tmp / "store-link"
        link.symlink_to(self.store)
        loose = self.tmp / "loose"
        loose.mkdir(mode=0o755)
        loose.chmod(0o755)
        for bad in (str(link), "relative/store", str(self.tmp / "missing"), str(loose), str(self.root), str(self.j),
                    str(self.root / "inside"), str(Path(ap.__file__).resolve().parents[2])):
            with self.assertRaises(ap.PanelError) as caught:
                ap.build_server(export_root=str(self.root), run_id="auto", actor="operator", system=False,
                                decisions_dir=str(self.j), flock_store=bad)
            self.assertEqual(caught.exception.code, "flock_store_invalid", bad)

    def test_get_with_a_query_is_refused(self):
        self.up_store()
        self.get("/redactions?x=1", 400)


class SignoffForm(Redactions):
    entries = Redactions.lines

    def test_console_get_post_never_mutate_store(self):
        self.up_store()
        before, spy = tree_state(self.store), OpenSpy(self.store)
        with mock.patch.object(os, "open", spy), mock.patch.object(os, "mkdir") as mkdir, mock.patch.object(os, "chmod") as chmod, \
                mock.patch.object(os, "link") as link, mock.patch.object(os, "rename") as rename:
            self.get("/redactions")
            status, location, _ = self.post(signature=SIGNATURE)
            self.get("/redactions")
        self.assertEqual((status, location), (303, "/redactions"))
        self.assertEqual((spy.writes, mkdir.called, chmod.called, link.called, rename.called), ([], False, False, False, False))
        self.assertEqual(tree_state(self.store), before)  # paths, bytes, modes and times
        self.assertFalse((self.folder / "SIGNOFF.json").exists())
        self.assertFalse((self.folder / "ELIGIBILITY.json").exists())
        self.assertFalse(self.source.with_name(self.sha + ".redacted.txt").exists())

    def test_post_journals_the_signature_and_the_page_says_awaiting_apply(self):
        self.up_store()
        self.assertEqual(self.post(signature=SIGNATURE.replace("\n", "\r\n"))[0], 303)  # a browser submits CRLF
        (entry,) = self.entries()
        self.assertEqual((entry["action"], entry["route"], entry["target_id"], entry["revision_seen"], entry["actor"]),
                         ("redaction_signoff", "/redactions/signoff", f"{SLUG}:{self.sha}", self.digest, "operator"))
        self.assertNotIn("kind", entry)
        self.assertEqual(json.loads(entry["value"]), {"store_id": redact.store_id(self.store), "signature": SIGNATURE})
        self.assertEqual(entry["value"], cj._canon(json.loads(entry["value"])))
        self.assertEqual(entry["source_project_id"], self.pid)
        page = self.get("/redactions")
        self.assertIn("awaiting signoff-apply", page)
        self.assertIn("still unsigned", page)
        self.assertNotIn("<form", page)
        self.assertEqual(self.repeat()[0], 409)  # a pending sign-off for the same proposal is refused
        self.assertEqual(len(self.entries()), 1)
        self.assertNotIn("pending apply: redaction_signoff", self.get("/dashboard"))  # not another page's pending note

    def repeat(self):
        return self.decide("/redactions/signoff", slug=SLUG, sha=self.sha, proposal_sha256=self.digest, signature=SIGNATURE)

    def test_the_generic_replayer_skips_it_and_signoff_apply_signs_once(self):
        self.up_store()
        self.post(signature=SIGNATURE)
        journal_before, live_before = self.journal_bytes(), tree_state(self.live)
        (line,) = self.apply()
        self.assertTrue(line.endswith("SIGNOFF_APPLY_REQUIRED"), line)
        self.assertEqual((self.journal_bytes(), tree_state(self.live)), (journal_before, live_before))  # no generic receipt
        self.assertIn("APPLIED", self.replay())
        self.assertIn("ALREADY", self.replay())
        self.assertEqual(json.loads((self.folder / "SIGNOFF.json").read_bytes())["signed_by"], "synthetic-reviewer")
        self.assertEqual(sum(1 for line in (self.j / redact.RECEIPTS).read_text().splitlines()), 1)
        self.assertIn("No redaction proposal is waiting", self.get("/redactions"))
        self.assertFalse((self.folder / "ELIGIBILITY.json").exists())  # the console builds no derivative; apply stays manual

    def test_a_forged_signature_is_journaled_and_refused_only_at_replay(self):
        self.up_store()
        self.post(signature=SIGNATURE)
        self.verify.side_effect = approval.ApprovalError("synthetic forged")
        before = tree_state(self.store)
        self.assertIn("SIGNATURE_REFUSED", self.replay(expected=1))
        self.assertEqual(tree_state(self.store), before)
        page = self.get("/redactions")
        self.assertIn("SIGNATURE_REFUSED", page)
        self.assertIn("<form", page)  # a refused entry is final; the person may submit another

    def test_a_proposal_changed_after_listing_is_refused_as_stale(self):
        self.up_store()
        form = {k: v for k, v in self.form().items() if k != "csrf_token"}
        value = json.loads(self.proposal)
        value["created_at"] = "2020-01-01T00:00:00Z"
        self.folder.joinpath("PROPOSAL.json").write_bytes(redact.encode(value))
        self.assertEqual(self.decide("/redactions/signoff", signature=SIGNATURE, **form)[0], 409)
        self.assertEqual(self.entries(), [])

    def test_a_proposal_changed_after_journaling_is_stale_at_replay(self):
        self.up_store()
        self.post(signature=SIGNATURE)
        value = json.loads(self.proposal)
        value["created_at"] = "2020-01-01T00:00:00Z"
        self.folder.joinpath("PROPOSAL.json").write_bytes(redact.encode(value))
        self.assertIn("STALE_PROPOSAL", self.replay(expected=1))
        self.assertFalse((self.folder / "SIGNOFF.json").exists())

    def test_a_signoff_that_appeared_after_listing_is_refused(self):
        self.up_store()
        form = {k: v for k, v in self.form().items() if k != "csrf_token"}
        self.cli("signoff", "--signature", self.write_sig())
        self.assertEqual(self.decide("/redactions/signoff", signature=SIGNATURE, **form)[0], 409)
        self.assertEqual(self.entries(), [])

    def write_sig(self):
        path = self.tmp / "signature"
        path.write_text(SIGNATURE)
        path.chmod(0o600)
        return path

    def test_form_security_and_bad_fields_refuse_before_append(self):
        self.up_store()
        good = {k: v for k, v in self.form().items() if k != "csrf_token"}
        port = self.server.server_port
        cases = [({**good}, 400),  # missing signature
                 ({**good, "signature": SIGNATURE, "extra": "1"}, 400),
                 ({**good, "signature": "   "}, 400),
                 ({**good, "signature": "not armored"}, 400),
                 ({**good, "signature": SIGNATURE + "x" * 9000}, 400),
                 ({**good, "signature": SIGNATURE, "sha": "zz"}, 400),
                 ({**good, "signature": SIGNATURE, "slug": "Bad Slug"}, 400),
                 ({**good, "signature": SIGNATURE, "slug": "../x"}, 400),
                 ({**good, "signature": SIGNATURE, "proposal_sha256": "f" * 64}, 409),
                 ({**good, "signature": SIGNATURE, "sha": "e" * 64}, 409)]
        for fields, code in cases:
            self.assertEqual(self.decide("/redactions/signoff", **fields)[0], code, fields.keys())
        for headers, status in (({"Origin": "http://127.0.0.1:%d" % port, "Content-Type": "text/plain"}, 415),
                                ({"Origin": "http://evil.example", "Content-Type": "application/x-www-form-urlencoded"}, 403)):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("POST", "/redactions/signoff", urllib.parse.urlencode({"csrf_token": self.server.csrf_token, **good, "signature": SIGNATURE}), headers)
            self.assertEqual(conn.getresponse().status, status)
            conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)  # a wrong CSRF token
        conn.request("POST", "/redactions/signoff", urllib.parse.urlencode({"csrf_token": "wrong", **good, "signature": SIGNATURE}),
                     {"Origin": "http://127.0.0.1:%d" % port, "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(conn.getresponse().status, 403)
        conn.close()
        self.assertEqual(self.entries(), [])
