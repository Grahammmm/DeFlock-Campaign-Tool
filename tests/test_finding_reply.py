"""DF1103-08: agency right of reply on findings (`finding reply-request`, `finding reply`). Synthetic data only;
nothing is sent and no socket is opened.

Run from the repository root: python3 -B -m unittest tests.test_finding_reply -v
"""
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import findings, law  # noqa: E402
from campaign_tool.cli import read_config  # noqa: E402
from campaign_tool.content import ContentError, load_content  # noqa: E402
from campaign_tool.site import PUBLIC_FINDING_FIELDS, PUBLIC_RESPONSE_FIELDS, Site, build_site  # noqa: E402
from tests import test_findings_publish as publish_tests  # noqa: E402  (module import: not re-collected here)

AGENCY, SLUG = "ca-synthetic", "synthetic-title"
D0 = date.today() - timedelta(days=10)  # the request is prepared ten days ago ...
BY, ASKED, ANSWERED = D0 + timedelta(days=5), D0 + timedelta(days=1), D0 + timedelta(days=3)  # ... deadline D0+5
TEXT = 'We <b>disagree</b> & note "x".\n\tSecond line  '
SCHEMA = json.loads((REPO_ROOT / "schemas" / "findings-open-data.schema.json").read_text(encoding="utf-8"))
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
EXAMPLE_SLUG = "cedar-policy-posted"
_NO_SOCKET = mock.patch.object(socket, "socket", side_effect=AssertionError("no network in these tests"))
ABSENT = object()


def hexes(n):
    return ("0123456789abcdef" * 8)[:n]


class ReplyBase(unittest.TestCase):
    # Reuse the DF2237-02 synthetic campaign helpers without re-running its tests (no TestCase bound here).
    _p = vars(publish_tests.FindingPublishTests)
    setUp_campaign, cli, register, write_draft, receipt, log_lines = (
        _p["setUp"], _p["cli"], _p["register"], _p["write_draft"], _p["receipt"], _p["log_lines"])

    def setUp(self):
        _NO_SOCKET.start()
        self.addCleanup(_NO_SOCKET.stop)
        self.setUp_campaign()
        self.record = publish_tests.RECORDS[2]
        path = self.base / "record-reply.txt"
        path.write_bytes(self.record)
        self.cli("ingest", "--directory", self.root, "--file", path, "--source-id", "synthetic-source")
        self.register(*publish_tests.RECORDS)
        (self.content / "agencies.json").write_text(json.dumps([{"agency_id": AGENCY, "name": "Synthetic Agency"}]))
        self.drafts = self.root / "private" / "drafts"
        self.drafts.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.draft = self.drafts / "syn-pub-1.json"
        self.write_draft(publish_tests.draft(agency_id=AGENCY, body_md="PRIVATE-BODY-TEXT"))
        self.reply_md = self.root / "kit" / "replies" / "syn-pub-1.md"
        self.rprefix = publish_tests.sha(self.record)[:12]

    def request(self, by=BY, today=D0, draft=None):
        return findings.reply_request(self.root, draft or self.draft, str(by), today=today)

    def reply(self, status="received", on=ANSWERED, asked=ASKED, text=TEXT, record=None, draft=None, **more):
        return findings.record_reply(self.root, draft or self.draft, status, str(on), str(asked), text=text,
                                     record=record, **more)

    def stored(self):
        return json.loads(self.draft.read_text(encoding="utf-8"))

    def approve(self):
        for role, who in (("factual", "Rev One"), ("legal", "Rev Two"), ("privacy", "Rev One")):
            findings.add_receipt(self.root, self.stored(), role, "approve", who, "Checked synthetic pages.")

    def status(self):
        return findings.review_status(self.root, self.stored())

    def tree(self):
        """Every file under the campaign except the draft itself and the lock file: to prove nothing else changed."""
        return {str(p.relative_to(self.root)): p.read_bytes() for p in sorted(self.root.rglob("*"))
                if p.is_file() and p != self.draft and p.name != ".publish.lock"}

    def refused(self, needle, call, *args, **kwargs):
        before_draft, before = self.draft.read_bytes(), self.tree()
        with self.assertRaises(ValueError) as caught:
            call(*args, **kwargs)
        self.assertIn(needle, str(caught.exception))
        self.assertEqual(self.draft.read_bytes(), before_draft)
        self.assertEqual(self.tree(), before)
        return str(caught.exception)


class RequestTests(ReplyBase):
    def test_request_private_exclusive_and_never_asked(self):
        result = self.request()
        self.assertEqual((result["id"], result["status"]), ("syn-pub-1", "pending"))
        for folder in (self.root / "kit", self.root / "kit" / "replies"):
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.reply_md.stat().st_mode), 0o600)
        text = self.reply_md.read_text(encoding="utf-8")
        for needle in ("Synthetic Agency", "Synthetic title", "Synthetic summary.", str(BY), "Draft only. Nothing was sent.",
                       publish_tests.sha(publish_tests.RECORDS[0])[:12], publish_tests.sha(publish_tests.RECORDS[1])[:12]):
            self.assertIn(needle, text)
        self.assertNotRegex(text, r"[0-9a-f]{40}")  # no full hash
        for secret in ("PRIVATE-BODY-TEXT", "@", "Ada", "http"):  # no body, address, author or link
            self.assertNotIn(secret, text)
        stored = self.stored()
        self.assertEqual(stored["agency_response"], {"status": "pending", "prepared_on": str(D0), "reply_by": str(BY)})
        self.assertNotIn("asked_on", stored["agency_response"])  # preparing is not asking
        self.assertEqual({k: v for k, v in stored.items() if k != "agency_response"}, publish_tests.draft(
            agency_id=AGENCY, body_md="PRIVATE-BODY-TEXT"))
        self.assertEqual(stat.S_IMODE(self.draft.stat().st_mode), 0o600)
        original = self.reply_md.read_bytes()
        self.refused("reply request already exists; keep the original draft", self.request, by=BY + timedelta(days=9))
        self.assertEqual(self.reply_md.read_bytes(), original)
        self.assertFalse((self.root / "private" / "reviews").exists())  # no receipts written
        self.assertFalse((self.content / "findings" / f"{SLUG}.json").exists())  # nothing published

    def test_missing_agency_request_and_bad_paths(self):
        self.refused("prepare a reply request first", self.reply)
        # draft state, agency and finishing the draft
        for change, needle in (({"state": "published"}, "state must be draft or ready"),
                               ({"state": "withdrawn"}, "state must be draft or ready"),
                               ({"agency_id": "ca-unknown"}, "not one of the campaign's agencies"),
                               ({"agency_id": None}, "not one of the campaign's agencies"),
                               ({"summary": "[EDIT ME: one or two plain sentences]"}, "must be finished text"),
                               ({"title": ""}, "must be finished text"),
                               ({"summary": "See " + hexes(64)}, "must be finished text"),
                               ({"sources": [{"locator": "p1", "sha256": "abc"}]}, "64-character lowercase sha256")):
            with self.subTest(change=change):
                good = self.draft.read_text(encoding="utf-8")
                self.write_draft(dict(publish_tests.draft(agency_id=AGENCY), **change))
                self.refused(needle, self.request)
                self.assertFalse(self.reply_md.exists())
                self.draft.write_text(good, encoding="utf-8")
        self.draft.write_text("{not json", encoding="utf-8")  # malformed draft
        self.refused("not valid JSON", self.request)
        self.write_draft(publish_tests.draft(agency_id=AGENCY, unexpected_key=1))
        self.refused("unknown key", self.request)
        self.write_draft(publish_tests.draft(agency_id=AGENCY))
        # paths: outside private/drafts, symlinked file, symlinked parent, symlinked or nonregular kit folders
        outside = self.base / "outside.json"
        outside.write_text(self.draft.read_text(encoding="utf-8"), encoding="utf-8")
        self.refused("inside this campaign's private/drafts folder", self.request, draft=outside)
        self.refused("inside this campaign's private/drafts folder", self.request, draft=self.drafts)
        self.refused("inside this campaign's private/drafts folder", self.request,
                     draft=self.drafts / ".." / ".." / "outside.json")
        link = self.drafts / "link.json"
        os.symlink(outside, link)
        self.refused("symlink", self.request, draft=link)
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        shutil.copy(outside, elsewhere / "d.json")
        os.symlink(elsewhere, self.drafts / "sub")
        self.refused("symlink", self.request, draft=self.drafts / "sub" / "d.json")
        self.refused("symlink", self.reply, draft=self.drafts / "sub" / "d.json")
        os.symlink(elsewhere, self.root / "kit")
        self.refused("must be a real directory", self.request)
        os.unlink(self.root / "kit")
        (self.root / "kit").write_text("a file", encoding="utf-8")
        self.refused("must be a real directory", self.request)
        (self.root / "kit").unlink()
        (self.root / "kit" / "replies").mkdir(parents=True)
        victim = self.base / "victim.md"
        victim.write_text("keep", encoding="utf-8")
        os.symlink(victim, self.reply_md)
        self.refused("symlink", self.request)
        self.assertEqual(victim.read_text(encoding="utf-8"), "keep")
        self.reply_md.unlink()
        # dates: padded, real, not in the past
        for bad in ("2099-1-5", "2099-02-30", "20991231", "", str(date.today() - timedelta(days=1)), "tomorrow"):
            with self.subTest(by=bad):
                self.refused("--by: invalid date or date order", self.request, by=bad, today=None)
        self.request(by=date.today(), today=None)  # a deadline of today is allowed
        self.assertTrue(self.reply_md.exists())

    def test_injected_write_failures_leave_a_marked_orphan_or_nothing(self):
        with mock.patch.object(findings, "_write_private", side_effect=OSError("disk full")):
            with self.assertRaises(ValueError) as caught:
                self.request()
        self.assertEqual(str(caught.exception), "reply request template saved but draft update failed; "
                                                "remove or retain the template manually")
        self.assertTrue(self.reply_md.exists())  # a private orphan template, never claimed atomic
        self.assertNotIn("agency_response", self.stored())
        self.assertEqual(stat.S_IMODE(self.reply_md.stat().st_mode), 0o600)
        self.refused("reply request already exists", self.request)  # the orphan must be removed by hand
        self.reply_md.unlink()
        with mock.patch.object(findings, "_create_text", side_effect=ValueError("cannot create")):
            self.refused("cannot create", self.request)
        self.request()
        with mock.patch.object(findings, "_write_private", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.reply()
        self.assertEqual(self.stored()["agency_response"]["status"], "pending")
        self.assertEqual([p.name for p in self.drafts.iterdir()], ["syn-pub-1.json"])  # no temp file left


class ReplyTests(ReplyBase):
    def setUp(self):
        super().setUp()
        self.request()

    def test_reply_all_statuses_and_boundaries(self):
        shape = {"prepared_on": str(D0), "reply_by": str(BY), "asked_on": str(ASKED), "on": str(ANSWERED)}
        self.assertEqual(self.reply(), {"id": "syn-pub-1", "status": "received"})
        self.assertEqual(self.stored()["agency_response"], dict(shape, status="received", text=TEXT))  # verbatim
        for text in ("x", "y" * 2000, "line\none\ttab\n"):
            self.reply(text=text)
            self.assertEqual(self.stored()["agency_response"]["text"], text)
        bad_text = ["y" * 2001, "", "   \n\t ", "a\x00b", "a\rb", "a\x1bb", "a\x85b", "a\u200bb", "a\u202eb", "a\ud800b",
                    "a" + hexes(40), "x" * 50 + hexes(64)]
        for text in bad_text:
            with self.subTest(text=text[:12]):
                message = self.refused("agency_response.text", self.reply, text=text)
                self.assertNotIn(text.strip()[:6] + "z", message)
                self.assertNotIn(hexes(40), message)
        self.refused("agency_response.text: required", self.reply, text=None)
        self.reply("declined", text=None)
        self.assertEqual(self.stored()["agency_response"], dict(shape, status="declined"))
        self.reply("declined", text="No comment <at> this time.")
        self.assertEqual(self.stored()["agency_response"]["text"], "No comment <at> this time.")
        self.refused("agency_response.text", self.reply, "declined", text="z" * 2001)
        # silence: refused before the deadline, accepted on it and after, never with text or a record
        self.refused("no_response requires --on on or after the reply deadline", self.reply, "no_response",
                     on=BY - timedelta(days=1), text=None)
        self.refused("agency_response.text: not allowed", self.reply, "no_response", on=BY, text="late")
        self.refused("--record is only for --status received", self.reply, "no_response", on=BY, text=None, record="a" * 12)
        self.refused("--record is only for --status received", self.reply, "declined", record="a" * 12)
        self.reply("no_response", on=BY, text=None)  # equal to the deadline is accepted
        self.assertEqual(self.stored()["agency_response"], dict(shape, status="no_response", on=str(BY)))
        self.reply("received", text="Late answer.")  # replacing: nothing of the earlier reply survives
        self.reply("no_response", on=BY + timedelta(days=1), text=None)
        self.assertEqual(sorted(self.stored()["agency_response"]), sorted(shape) + ["status"])
        self.refused("--status must be one of", self.reply, "pending", text=None)
        # dates: real, padded, not future, prepared <= asked <= on, asked <= deadline
        today = date.today()
        for key, value, flag in (("on", today + timedelta(days=1), "--on"), ("on", "2026-1-5", "--on"),
                                 ("on", "2026-02-30", "--on"), ("on", "", "--on"),
                                 ("on", D0 - timedelta(days=1), "--asked-on"), ("on", ASKED - timedelta(days=1), "--asked-on"),
                                 ("asked", D0 - timedelta(days=1), "--asked-on"), ("asked", BY + timedelta(days=1), "--asked-on"),
                                 ("asked", ANSWERED + timedelta(days=1), "--asked-on"), ("asked", "20260101", "--asked-on"),
                                 ("asked", "2026-13-01", "--asked-on")):
            with self.subTest(key=key, value=str(value)):
                self.refused(f"{flag}: invalid date or date order", self.reply, **{key: value})
        self.reply(asked=D0, on=D0)  # prepared_on == asked_on == on
        self.reply(asked=BY, on=BY)  # asked_on == deadline
        self.assertEqual(self.stored()["agency_response"]["asked_on"], str(BY))

    def test_record_resolution_and_safe_render(self):
        prefix = self.rprefix
        self.reply(record=prefix)
        stored = self.stored()
        sha = publish_tests.sha(self.record)
        self.assertEqual(stored["agency_response"]["record_sha256"], sha)
        self.assertEqual(stored["sources"][-1], {"sha256": sha, "title": "Synthetic record",
                                                 "locator": f"Agency response received {ANSWERED}"})
        self.assertEqual(len(stored["sources"]), 3)
        # an already cited record keeps its own citation and locator
        first = publish_tests.sha(publish_tests.RECORDS[0])
        self.reply(record=first[:20])
        stored = self.stored()
        self.assertEqual((stored["agency_response"]["record_sha256"], len(stored["sources"])), (first, 3))
        self.assertEqual(stored["sources"][0]["locator"], "page:1")
        before = self.draft.read_bytes()
        # absent / ambiguous / unregistered / malformed / not stored
        for record, needle in (("f" * 12, "matches no content/sources.json entry"), ("abc", "12-64 lowercase hex"),
                               (prefix.upper(), "12-64 lowercase hex"), ("zz" * 6, "12-64 lowercase hex"), ("", "12-64")):
            with self.subTest(record=record):
                self.refused(needle, self.reply, record=record)
        self.assertEqual(self.draft.read_bytes(), before)
        twin, loose = "ab" * 6 + "1" * 52, "ab" * 6 + "2" * 52
        entries = json.loads((self.content / "sources.json").read_text(encoding="utf-8"))
        template = entries[0]
        (self.content / "sources.json").write_text(json.dumps(entries + [dict(template, sha256=twin),
                                                                         dict(template, sha256=loose)]), encoding="utf-8")
        self.refused("is ambiguous (2 entries match)", self.reply, record="ab" * 6)
        self.refused("not_ingested", self.reply, record=twin)  # registered but never ingested: no evidence
        objects = self.root / "private" / "objects"
        stored_object = next(p for p in objects.rglob("*") if p.is_file() and sha in p.name or sha[2:] in p.name
                             ) if objects.exists() else None
        self.assertIsNotNone(stored_object)
        moved = self.base / "moved-object"
        shutil.move(stored_object, moved)
        self.refused("object_missing", self.reply, record=prefix)
        shutil.move(moved, stored_object)
        self.reply(record=prefix)  # restored: accepted again
        self.assertNotRegex(self.stored()["agency_response"]["text"], "<script")

    def test_every_reply_mutation_stales_approvals(self):
        self.reply(record=self.rprefix)
        self.approve()
        approved = self.draft.read_text(encoding="utf-8")
        before = self.status()
        self.assertEqual((before["review_blockers"], before["receipts_for_this_content"], before["stale_receipts"]), ([], 3, 0))
        digest = before["content_sha256"]
        payload = findings.review_payload(self.root, self.stored(), "factual", "approve", "Rev One", "Checked synthetic pages.")
        self.assertIn(digest.encode(), payload)

        def drop(document):
            del document["agency_response"]

        def set_key(key, value):
            return lambda document: document["agency_response"].__setitem__(key, value)

        def drop_key(key):
            return lambda document: document["agency_response"].pop(key)

        changes = {"text": set_key("text", TEXT + "!"), "on": set_key("on", str(ANSWERED + timedelta(days=1))),
                   "asked_on": set_key("asked_on", str(D0)), "reply_by": set_key("reply_by", str(BY + timedelta(days=1))),
                   "prepared_on": set_key("prepared_on", str(D0 - timedelta(days=1))), "status": set_key("status", "declined"),
                   "record_sha256": set_key("record_sha256", publish_tests.sha(publish_tests.RECORDS[0])),
                   "record removed": drop_key("record_sha256"), "text removed": drop_key("text"), "object removed": drop}
        seen = {digest}
        for name, change in changes.items():
            with self.subTest(change=name):
                document = json.loads(approved)
                change(document)
                self.draft.write_text(json.dumps(document), encoding="utf-8")
                after = self.status()
                self.assertNotIn(after["content_sha256"], seen)
                seen.add(after["content_sha256"])
                self.assertEqual((after["receipts_for_this_content"], after["stale_receipts"]), (0, 3))
                self.assertTrue({"missing_factual_review", "missing_legal_review", "missing_privacy_review"}
                                <= set(after["review_blockers"]), after["review_blockers"])
                changed = findings.review_payload(self.root, document, "factual", "approve", "Rev One", "Checked synthetic pages.")
                self.assertNotEqual(changed, payload)  # a signature over the old payload cannot cover this content
                self.assertIn(after["content_sha256"].encode(), changed)
        self.draft.write_text(approved, encoding="utf-8")  # the approved text is approved again, nothing else changed
        self.assertEqual(self.status()["review_blockers"], [])
        self.assertEqual(len(json.loads((self.root / "private" / "reviews" / "syn-pub-1.json").read_text())), 3)

    def test_adding_a_reply_after_approval_blocks_publish_and_correct(self):
        # publishing without asking stays allowed
        other = self.drafts / "syn-pub-2.json"
        self.write_draft(publish_tests.draft(agency_id=AGENCY, slug="no-ask", id="syn-pub-2", title="No ask title"))
        self.draft.replace(other)
        self.draft = other
        self.approve()
        findings.publish(self.root, self.draft, "Pat Organizer")
        self.assertNotIn("agency_response", json.loads((self.content / "findings" / "no-ask.json").read_text()))
        # a prepared request after approval makes the approval stale; so does a later reply
        for step in (lambda: self.request(today=D0), lambda: self.reply("received", record=self.rprefix)):
            step()
            with self.assertRaises(ValueError) as caught:
                findings.correct(self.root, "syn-pub-2", str(date.today()), "Added the agency reply.", self.draft,
                                 approved_by="Pat Organizer")
            self.assertIn("review blockers", str(caught.exception))
            self.assertEqual(self.status()["receipts_for_this_content"], 0)
        self.approve()
        findings.correct(self.root, "syn-pub-2", str(date.today()), "Added the agency reply.", self.draft,
                         approved_by="Pat Organizer")
        published = json.loads((self.content / "findings" / "no-ask.json").read_text())
        self.assertEqual(published["agency_response"], self.stored()["agency_response"])
        # a new draft with a reply is refused by publish until it is reviewed
        late = self.drafts / "syn-pub-3.json"
        self.write_draft(publish_tests.draft(agency_id=AGENCY, slug="late", id="syn-pub-3", title="Late title"))
        self.draft.replace(late)
        self.draft = late
        self.approve()
        self.request(today=D0)
        self.reply("received", record=self.rprefix)
        with self.assertRaises(ValueError) as caught:
            findings.publish(self.root, self.draft, "Pat Organizer")
        self.assertIn("review blockers", str(caught.exception))
        self.assertFalse((self.content / "findings" / "late.json").exists())
        self.approve()
        findings.publish(self.root, self.draft, "Pat Organizer")
        self.cli("build", "--directory", self.root)
        page = (self.root / "public" / "findings" / "late.html").read_text(encoding="utf-8")
        section = re.search(r'<section class="agency-response".*?</section>', page, re.S).group(0)
        self.assertIn("<h3>Agency response</h3>", section)
        self.assertIn("&lt;b&gt;disagree&lt;/b&gt; &amp; note &quot;x&quot;.<br>\t", section)  # escaped plain text
        self.assertNotIn("<b>", section)
        self.assertIn(f"<code>{self.rprefix}</code>", section)
        self.assertIn(f"We asked on {ASKED}", section)
        self.assertIn(f"The agency replied on {ANSWERED}", section)

    def test_cli_commands_print_no_reply_text_and_misuse_exits_2(self):
        draft = self.drafts / "syn-pub-9.json"
        self.write_draft(publish_tests.draft(agency_id=AGENCY, id="syn-pub-9", slug="cli-one", title="Cli one"))
        self.draft.replace(draft)
        today = date.today()
        base = ("finding", "{}", draft, "--directory", self.root)
        made = self.cli(*[a if a != "{}" else "reply-request" for a in base], "--by", today + timedelta(days=30))
        self.assertEqual(json.loads(made.stdout.splitlines()[0])["status"], "pending")
        self.assertIn("Recorded only. Nothing was sent.", made.stdout)
        self.assertEqual(stat.S_IMODE((self.root / "kit" / "replies" / "syn-pub-9.md").stat().st_mode), 0o600)
        again = self.cli(*[a if a != "{}" else "reply-request" for a in base], "--by", today + timedelta(days=30), returncode=1)
        self.assertIn("reply request already exists; keep the original draft", again.stderr)
        reply = [a if a != "{}" else "reply" for a in base]
        done = self.cli(*reply, "--status", "received", "--on", today, "--asked-on", today, "--text", TEXT,
                        "--record", self.rprefix)
        self.assertEqual(json.loads(done.stdout.splitlines()[0]), {"id": "syn-pub-9", "status": "received"})
        self.assertIn("Recorded only. Nothing was sent.", done.stdout)
        self.assertNotIn("disagree", done.stdout + done.stderr)
        self.assertEqual(json.loads(draft.read_text())["agency_response"]["text"], TEXT)
        early = self.cli(*reply, "--status", "no_response", "--on", today, "--asked-on", today, returncode=1)
        self.assertIn("no_response requires --on on or after the reply deadline", early.stderr)
        for arguments in (reply + ["--status", "received", "--on", today],  # --asked-on is required
                          reply + ["--status", "maybe", "--on", today, "--asked-on", today],
                          ["finding", "reply-request", draft, "--directory", self.root]):  # --by is required
            result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, arguments)],
                                    cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("error:", result.stderr)
        bad = self.cli(*reply, "--status", "received", "--on", today, "--asked-on", today, "--text", "   ", returncode=1)
        self.assertIn("agency_response.text", bad.stderr)


class PublicTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-finding-reply-site-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public, self.path = self.root / "public", self.root / "content" / "findings" / f"{EXAMPLE_SLUG}.json"
        self.sha = json.loads(self.path.read_text(encoding="utf-8"))["sources"][0]["sha256"]

    def set_reply(self, reply):
        document = json.loads(self.path.read_text(encoding="utf-8"))
        document.pop("agency_response", None)
        if reply is not ABSENT:
            document["agency_response"] = reply
        self.path.write_text(json.dumps(document), encoding="utf-8")

    def build(self):
        build_site(self.root)
        return (self.public / "data" / "findings.json").read_text(encoding="utf-8"), \
            (self.public / "findings" / f"{EXAMPLE_SLUG}.html").read_text(encoding="utf-8")

    def row(self):
        return json.loads(self.build()[0])["findings"][0]

    def reply(self, status="received", **more):
        return dict({"status": status, "prepared_on": "2026-02-02", "reply_by": "2026-02-20", "asked_on": "2026-02-03",
                     "on": "2026-02-10"}, **more)

    def test_optional_public_schema_and_privacy(self):
        data, page = self.build()  # legacy: no reply at all
        legacy_row = json.loads(data)["findings"][0]
        self.assertEqual(set(legacy_row), set(PUBLIC_FINDING_FIELDS) - {"agency_response"})
        self.assertNotIn("Agency response", page)
        law.validate(json.loads(data), SCHEMA)
        # pending is prepared, not asked: the public file and page stay exactly as they were
        pending = {"status": "pending", "prepared_on": "2026-02-02", "reply_by": "2026-02-20"}
        self.set_reply(pending)
        self.assertEqual(self.build(), (data, page))
        # received: exact nested allowlist, no author / prepared_on / full hash / private path
        self.set_reply(self.reply(text=TEXT, record_sha256=self.sha))
        data, page = self.build()
        row = json.loads(data)["findings"][0]
        self.assertEqual(row["agency_response"], {"status": "received", "asked_on": "2026-02-03", "reply_by": "2026-02-20",
                                                  "on": "2026-02-10", "text": TEXT, "record_sha256_prefix": self.sha[:12]})
        self.assertEqual(set(row["agency_response"]), set(PUBLIC_RESPONSE_FIELDS))
        self.assertEqual({k: v for k, v in row.items() if k != "agency_response"}, {k: v for k, v in legacy_row.items()})
        self.assertNotRegex(data, r"[0-9a-fA-F]{40,}")
        for private in ("prepared_on", "synthetic-author", "private/", "record_sha256\""):
            self.assertNotIn(private, data)
        law.validate(json.loads(data), SCHEMA)
        self.assertEqual(self.build(), (data, page))  # repeat builds are stable
        section = re.search(r'<section class="agency-response".*?</section>', page, re.S).group(0)
        self.assertIn("The agency replied on 2026-02-10. We asked on 2026-02-03.", section)
        self.assertIn(f"<code>{self.sha[:12]}</code>", section)
        self.assertIn("&lt;b&gt;disagree&lt;/b&gt; &amp; note &quot;x&quot;.<br>", section)
        self.assertNotIn("<b>", section)
        # declined without text; no_response is the single line with the deadline
        self.set_reply(self.reply("declined"))
        row, page = self.row(), self.build()[1]
        self.assertEqual(set(row["agency_response"]), {"status", "asked_on", "reply_by", "on"})
        self.assertIn("The agency declined to comment on 2026-02-10.", page)
        self.set_reply(self.reply("no_response", on="2026-02-20"))
        row, page = self.row(), self.build()[1]
        self.assertEqual(set(row["agency_response"]), {"status", "asked_on", "reply_by", "on"})
        self.assertIn("<p>asked on 2026-02-03; no response by 2026-02-20</p>", page)
        self.assertNotIn("The agency replied", page)
        law.validate(json.loads(self.build()[0]), SCHEMA)
        for broken in ({"status": "pending"}, {"status": "received", "asked_on": "x", "reply_by": "x", "on": "x"}):
            with self.assertRaises(ValueError):
                law.validate({**json.loads(self.build()[0]), "findings": [dict(row, agency_response=broken)]}, SCHEMA)

    def test_injected_extra_key_and_full_hash_are_refused_at_build(self):
        self.set_reply(self.reply(text="Short answer."))
        model = load_content(self.root)
        site = Site(self.root, read_config(self.root), model)
        site.findings_json()
        original_public = Site._public_finding
        for change, needle in ((lambda r: r.update(prepared_on="2026-02-02"), "non-public key prepared_on"),
                               (lambda r: r.update(record_sha256=self.sha), "non-public key record_sha256"),
                               (lambda r: r.update(text="see " + hexes(64)), "full hash")):
            with self.subTest(needle=needle):
                def injected(self_, f, _change=change):
                    value = original_public(self_, f)
                    _change(value["agency_response"])
                    return value
                with mock.patch.object(Site, "_public_finding", new=injected):
                    with self.assertRaisesRegex(ContentError, needle):
                        site.findings_json()

    def test_stored_shape_is_validated_by_the_loader(self):
        cases = [(None, "agency_response: must be an object"), ([], "must be an object"),
                 ({}, "agency_response.status"), (self.reply("maybe"), "agency_response.status"),
                 (dict(self.reply(), zz_secret_key=1), "agency_response: unknown field"),
                 (dict(self.reply(text="x"), nested={"a": 1}), "unknown field"),
                 ({"status": "pending", "prepared_on": "2026-02-02", "reply_by": "2026-02-20", "asked_on": "2026-02-03"},
                  "agency_response.asked_on: not allowed for status pending"),
                 (self.reply(text="x", record_sha256=self.sha[:12]), "agency_response.record_sha256: must be 64"),
                 (self.reply(text="x", record_sha256="e" * 64), "record_sha256: must be one of the finding's cited sources"),
                 (self.reply(text=None), "agency_response.text"), (self.reply(text=5), "agency_response.text"),
                 (self.reply(), "agency_response.text: required for status received"),
                 (self.reply("declined", text="x", record_sha256=self.sha), "record_sha256: not allowed for status declined"),
                 (self.reply("no_response", text="x", on="2026-02-20"), "agency_response.text: not allowed"),
                 (self.reply("no_response", on="2026-02-19"), "agency_response.on: no_response needs"),
                 (self.reply(on="2026-02-02", text="x"), "agency_response.on"),
                 (self.reply(asked_on="2026-02-01", text="x"), "agency_response.asked_on"),
                 (self.reply(asked_on="2026-02-21", text="x"), "agency_response.asked_on"),
                 (self.reply(reply_by="2026-02-01", text="x"), "agency_response.reply_by"),
                 (self.reply(on=str(date.today() + timedelta(days=1)), text="x"), "agency_response.on"),
                 (self.reply(prepared_on="2026-2-2", text="x"), "agency_response.prepared_on"),
                 (self.reply(text="x" * 2001), "agency_response.text")]
        for reply, needle in cases:
            with self.subTest(needle=needle, reply=str(reply)[:40]):
                self.set_reply(reply)
                with self.assertRaises(ContentError) as caught:
                    load_content(self.root)
                self.assertIn(needle, str(caught.exception))
                self.assertNotIn("zz_secret_key", str(caught.exception))
                self.assertNotIn("x" * 20, str(caught.exception))
        self.set_reply(self.reply(text="ok"))
        self.assertEqual(load_content(self.root)["findings"][0]["agency_response"]["text"], "ok")


if __name__ == "__main__":
    unittest.main()
