"""DF1507-11: `today --directory D [--as-of YYYY-MM-DD] [--json]`, one ranked read-only to-do list. Synthetic only.

Run from the repository root: python3 -B -m unittest tests.test_today -v
"""
import contextlib
import io
import json
import shlex
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, findings, today  # noqa: E402
from tests.test_findings_publish import draft  # noqa: E402
from tests.test_findings_status import snapshot  # noqa: E402
from tests.test_kit_check import ready_drafts  # noqa: E402

AS_OF = date(2026, 3, 15)
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign" / "content"
KIT_LINK = "https://synthetic.example/findings/cedar-policy-posted.html"
ITEM_KEYS = {"rank", "kind", "what", "due", "command"}


def run(*arguments):
    out, err, code = io.StringIO(), io.StringIO(), 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def meeting(ident, starts, deadline, status="scheduled", kit=""):
    written = {"how": "email the synthetic clerk", "deadline": deadline}
    return {"id": ident, "body": f"Synthetic Council {ident}", "starts_at": starts, "status": status,
            "agenda_url": f"https://agenda.synthetic.example/{ident}", "comment_kit_md": kit,
            "participation": {"in_person": True, "remote": None, "written_comment": written,
                              "source_url": "https://agenda.synthetic.example/rules", "verified_by": "Pat Organizer",
                              "verified_at": "2026-03-10"}}


class TodayTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-today-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.root = self.tmp / "campaign"
        self.ok("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo", "--state", "CA")

    def ok(self, *arguments):
        code, out, err = run(*arguments)
        self.assertEqual(code, 0, err)
        return out

    def build_tree(self):
        """One synthetic campaign with every item kind plus the near misses that must not be listed."""
        self.ok("kit", "--directory", self.root)
        ready_drafts(self.root)  # kit-sent refuses unfilled placeholders and unverified recipients (DF1507-07)
        self.a, self.b, self.c = (p.stem for p in sorted((self.root / "kit" / "requests").glob("*.md"))[:3])
        ready_drafts(self.root, (self.a, self.c))  # kit-sent refuses unfilled placeholders and unverified recipients (DF1507-07)
        for agency, on in ((self.c, "2026-03-02"), (self.a, "2026-03-10")):  # due 2026-03-12 (past) / 2026-03-20
            self.ok("kit-sent", "--directory", self.root, "--agency", agency, "--date", on, "--channel", "email")
        path = self.root / "kit" / "agencies.json"
        agencies = json.loads(path.read_text(encoding="utf-8"))
        for entry in agencies:
            if entry["agency_id"] in (self.a, self.b):  # a: checked 438 days ago (stale); b: 14 days ago (fresh)
                entry.update(selected=True, verified=True, records_email="records@agency.synthetic.example",
                             verification={"source_url": "https://agency.synthetic.example/records", "fields":
                                           ["records_email"], "checked_on": "2025-01-01" if entry["agency_id"] ==
                                           self.a else "2026-03-01"})
        path.write_text(json.dumps(agencies), encoding="utf-8")
        content = self.root / "content"
        (content / "findings").mkdir(parents=True)
        (content / "meetings").mkdir()
        shutil.copyfile(EXAMPLE / "findings" / "cedar-policy-posted.json", content / "findings" / "cedar-policy-posted.json")
        for document in (meeting("m-week", "2026-03-23T18:00:00-07:00", "2026-03-22T12:00:00-07:00", kit=KIT_LINK),
                         meeting("m-eight", "2026-03-24T18:00:00-07:00", "2026-03-23T12:00:00-07:00"),
                         meeting("m-past", "2026-03-16T18:00:00-07:00", "2026-03-14T12:00:00-07:00"),
                         meeting("m-off", "2026-03-19T18:00:00-07:00", "2026-03-18T12:00:00-07:00", "cancelled")):
            (content / "meetings" / f"{document['id']}.json").write_text(json.dumps(document), encoding="utf-8")
        drafts = self.root / "private" / "drafts"
        drafts.mkdir(mode=0o700)
        for name, changes, roles in (("td-ready", {}, ("factual", "legal", "privacy")),
                                     ("td-block", {"summary": ""}, ("factual",)), ("td-new", {}, ())):
            document = draft(id=name, slug=name, **changes)
            (drafts / f"{name}.json").write_text(json.dumps(document), encoding="utf-8")
            for role, reviewer in zip(roles, ("Rev One", "Rev Two", "Rev One")):
                findings.add_receipt(self.root, document, role, "approve", reviewer, "Checked synthetic pages.")

    def kinds(self, items):
        return [item["kind"] for item in items]

    def test_every_kind_ranked_with_its_command(self):
        self.build_tree()
        items = today.todo(self.root, AS_OF)
        self.assertEqual(self.kinds(items), ["comment_deadline", "request_past_due", "finding_ready", "finding_blocked",
                                             "stale_comment_kit", "stale_contact"], items)
        self.assertEqual([item["rank"] for item in items], list(range(1, len(items) + 1)))
        self.assertTrue(all(set(item) == ITEM_KEYS for item in items), items)
        deadline, past, ready, blocked, kit_item, contact = items
        # 7 days out listed; 8 days out, already past and cancelled are not.
        self.assertIn("(m-week)", deadline["what"])
        self.assertEqual(deadline["due"], "2026-03-22T12:00:00-07:00")
        self.assertEqual(deadline["command"],
                         "submit written comment: email the synthetic clerk (agenda: https://agenda.synthetic.example/m-week)")
        # Past due only; the upcoming request is not listed.
        self.assertIn(f"({self.c})", past["what"])
        self.assertEqual((past["due"], past["command"]), ("2026-03-12", f"kit-status --directory {self.root}"))
        self.assertNotIn(self.a, json.dumps([past]))
        drafts = self.root / "private" / "drafts"
        self.assertEqual(ready["command"],
                         f"finding publish {drafts / 'td-ready.json'} --directory {self.root} --approved-by NAME")
        self.assertEqual(blocked["command"], f"finding review-status {drafts / 'td-block.json'} --directory {self.root}")
        self.assertIn("missing_summary", blocked["what"])
        self.assertEqual(kit_item["command"], f"meetings-stage --directory {self.root} --id m-week --replace")
        self.assertIn("cedar-policy-posted (corrected 2026-02-03)", kit_item["what"])
        self.assertTrue(contact["command"].startswith(f"kit-verify-agency --directory {self.root} --agency-id {self.a} "))
        self.assertEqual(contact["due"], "2025-01-01")
        self.assertNotIn(self.b, json.dumps(items))  # checked 14 days ago: fresh

    def test_finding_commands_name_the_file_read_not_its_id(self):
        """A draft whose filename differs from its id gets its own path; a decoy named <id>.json is not used."""
        self.build_tree()
        drafts = self.root / "private" / "drafts"
        notes, blocked_notes = drafts / "notes.json", drafts / "blocked notes.json"
        (drafts / "td-ready.json").rename(notes)
        (drafts / "td-block.json").rename(blocked_notes)
        (drafts / "td-ready.json").write_text(json.dumps(draft(id="decoy", slug="decoy")), encoding="utf-8")
        items = {i["kind"]: i for i in today.todo(self.root, AS_OF) if i["kind"].startswith("finding")}
        self.assertEqual(set(items), {"finding_ready", "finding_blocked"})
        ready, blocked = items["finding_ready"], items["finding_blocked"]
        self.assertEqual(ready["command"],
                         f"finding publish {notes} --directory {self.root} --approved-by NAME")
        self.assertEqual(blocked["command"],
                         f"finding review-status {shlex.quote(str(blocked_notes))} --directory {self.root}")
        self.assertNotIn("DRAFT", json.dumps(list(items.values())))
        status = json.loads(self.ok(*shlex.split(blocked["command"])))  # the listed command runs as printed
        self.assertIn("missing_summary", json.dumps(status))  # td-block's own blocker: the renamed file was read
        argv = shlex.split(ready["command"])  # the publish command parses to the renamed file
        self.assertEqual(argv[:3], ["finding", "publish", str(notes)])
        self.assertEqual(findings.load_draft(Path(argv[2]))["id"], "td-ready")

    def test_window_edges_and_due_today(self):
        self.build_tree()
        later = today.todo(self.root, date(2026, 3, 20))  # a is due that day; m-week/m-eight inside the window
        self.assertEqual([i["kind"] for i in later if i["kind"].startswith("request")],
                         ["request_past_due", "request_due_today"])
        self.assertEqual([i["what"].split("(")[-1] for i in later if i["kind"] == "comment_deadline"],
                         ["m-week)", "m-eight)"])
        on_day = today.todo(self.root, date(2026, 3, 14))  # deadline on the as-of day: listed; m-week now 8 days out
        self.assertEqual([i["what"].split("(")[-1] for i in on_day if i["kind"] == "comment_deadline"], ["m-past)"])

    def test_corrupt_sent_json_is_one_fix_item_and_hides_nothing_else(self):
        self.build_tree()
        (self.root / "kit" / "sent.json").write_text("{not json", encoding="utf-8")
        (self.root / "content" / "meetings" / "zz-broken.json").write_text("[1, 2", encoding="utf-8")
        (self.root / "private" / "drafts" / "zz-bad.json").write_text("not json", encoding="utf-8")
        items = today.todo(self.root, AS_OF)
        self.assertEqual(self.kinds(items), ["comment_deadline", "finding_ready", "finding_blocked", "stale_comment_kit",
                                             "stale_contact"] + ["unreadable"] * 3, items)
        bad_draft, broken, sent = [item for item in items if item["kind"] == "unreadable"]  # sorted by file label
        self.assertTrue(bad_draft["what"].startswith(f"fix {self.root / 'private' / 'drafts' / 'zz-bad.json'}: "))
        self.assertTrue(broken["what"].startswith("fix content/meetings/zz-broken.json: "), broken)
        self.assertEqual(sent["what"], "fix kit/sent.json: kit/sent.json unreadable: not JSON")
        self.assertEqual(sent["command"], "open kit/sent.json and fix it")
        for item in (bad_draft, broken, sent):
            self.assertLessEqual(len(item["what"].split(": ", 1)[1]), today.ERROR_CHARS)

    def test_empty_campaign_nothing_due(self):
        self.assertEqual(run("today", "--directory", self.root, "--as-of", "2026-03-15"),
                         (0, "Nothing due on 2026-03-15.\n", ""))
        self.assertEqual(run("today", "--directory", self.root, "--as-of", "2026-03-15", "--json"), (0, "[]\n", ""))

    def test_bad_as_of_and_missing_campaign_stop(self):
        for bad in ("2026-02-30", "2026-3-1", "soon"):
            code, out, err = run("today", "--directory", self.root, "--as-of", bad)
            self.assertEqual((code, out), (1, ""))
            self.assertTrue(err.startswith("Stopped: --as-of must be a real YYYY-MM-DD date"), err)
        code, _, err = run("today", "--directory", self.tmp / "nowhere", "--as-of", "2026-03-15")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("Stopped: "), err)

    def test_read_only_and_exact_json_and_text(self):
        self.build_tree()
        before = snapshot(self.root)
        code, out, err = run("today", "--directory", self.root, "--as-of", "2026-03-15", "--json")
        self.assertEqual((code, err), (0, ""))
        items = json.loads(out)
        self.assertEqual(items, today.todo(self.root, AS_OF))
        self.assertEqual([item["rank"] for item in items], list(range(1, 7)))
        text = self.ok("today", "--directory", self.root, "--as-of", "2026-03-15").splitlines()
        self.assertEqual(text, [f"{i['rank']}. [{i['kind']}] {i['what']} — {i['command']}" for i in items])
        self.assertEqual(snapshot(self.root), before)


if __name__ == "__main__":
    unittest.main()
