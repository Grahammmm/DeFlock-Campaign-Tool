"""DF1109-03: `today` names private press, social and newsletter drafts that went out of date, from each tool's own
read-only check (press check, social --check, newsletter --check). Nothing is generated, posted or sent. Synthetic only.

Run from the repository root: python3 -B -m unittest tests.test_today_outreach -v
"""
import json
import os
import shlex
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import press_check, today  # noqa: E402
from campaign_tool.content import load_content  # noqa: E402
from campaign_tool.press import build_letter, build_press  # noqa: E402
from campaign_tool.social import build_social  # noqa: E402

EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
AS_OF = date(2026, 5, 1)
NOW = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
FIRST, SLUG, MEETING, DAY = "fnd_0000000000000001", "cedar-policy-posted", "cedar-police-2026-06", "2026-03-01"
OUTREACH = ("outreach_stale", "outreach_not_checked", "unreadable")
NOT_CHECKED = "newsletter draft not checked; rebuild with --from-content"
PARTICIPATION = {"in_person": True, "remote": None, "written_comment": {"how": "email the synthetic clerk",
                 "deadline": "2026-05-04T17:00:00-07:00"}, "source_url": "https://agenda.synthetic.example/rules",
                 "verified_by": "Pat Organizer", "verified_at": "2026-04-20"}
# Captured from main (post DF1109-02) before this change for setUp's campaign plus the cedar-comment meeting.
NO_DRAFTS_TEXT = ("1. [comment_deadline] Written comment due 2026-05-04T17:00:00-07:00 for Cedar Council (fictional) "
                  "(cedar-comment) — submit written comment: email the synthetic clerk")
NO_DRAFTS_JSON = ('[\n  {\n    "rank": 1,\n    "kind": "comment_deadline",\n    "what": "Written comment due '
                  '2026-05-04T17:00:00-07:00 for Cedar Council (fictional) (cedar-comment)",\n    "due": '
                  '"2026-05-04T17:00:00-07:00",\n    "command": "submit written comment: email the synthetic clerk"\n'
                  '  }\n]')


def snapshot(root):
    """{relative path: (kind, bytes or link target, mode)} of the whole tree."""
    out = {}
    for path in sorted(Path(root).rglob("*")):
        info = path.lstat()
        body = os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None
        out[path.relative_to(root).as_posix()] = (body, info.st_mode)
    return out


class Outreach(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket.connect", "urllib.request.urlopen"):
            patcher = mock.patch(target, side_effect=AssertionError("network used"))
            patcher.start()
            self.addCleanup(patcher.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-today-outreach-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(os.path.realpath(temporary.name)) / "my campaign"  # a space: commands quote it
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        shutil.rmtree(self.root / "content" / "meetings")
        (self.root / "content" / "meetings").mkdir()
        self.meeting(MEETING, "2026-06-10T18:00:00-07:00")
        self.directory = shlex.quote(str(self.root))

    def meeting(self, ident, starts_at, **extra):
        document = {"id": ident, "body": "Cedar Council (fictional)", "starts_at": starts_at,
                    "agency_id": "ca-cedar-police", **extra}
        (self.root / "content" / "meetings" / f"{ident}.json").write_text(json.dumps(document), encoding="utf-8")

    def finding(self, name=SLUG, **changes):
        path = self.root / "content" / "findings" / f"{SLUG}.json"
        document = {**json.loads(path.read_text(encoding="utf-8")), **changes}
        (self.root / "content" / "findings" / f"{name}.json").write_text(json.dumps(document), encoding="utf-8")
        return document

    def put(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def newsletter(self, provenance=True):
        """kit/newsletter-draft.json listing exactly the current content (a match), or with no provenance."""
        model = load_content(self.root)
        saved = {"schema_version": 1, "built_at": "2026-04-01T00:00:00+00:00", "since": None,
                 "findings": [{"id": f["id"], "slug": f["slug"], "published_at": f["published_at"], "section": "new",
                               "corrections": [{"date": c["date"], "note": c["note"]} for c in f["corrections"]]}
                              for f in model["findings"]],
                 "meetings": [{"id": m["id"], "starts_at": m["starts_at"], "agenda_url": m["agenda_url"]}
                              for m in model["meetings"]]}
        self.put("kit/newsletter-draft.json", json.dumps({"subject": "Synthetic", **({"provenance": saved}
                                                                                      if provenance else {})}))

    def items(self, as_of=AS_OF, **kwargs):
        return [(i["kind"], i["what"], i["command"]) for i in today.todo(self.root, as_of, **kwargs)
                if i["kind"] in OUTREACH]

    def press_item(self, n):
        return ("outreach_stale", f"press: {n} draft check problem(s); inspect before use",
                f"press check --directory {self.directory}")

    def social_item(self, n):
        return ("outreach_stale", f"social: {n} stale or outdated draft(s); inspect before use",
                f"social --directory {self.directory} --check")

    def newsletter_item(self, n):
        return ("outreach_stale", f"newsletter: {n} content difference(s); inspect before use",
                f"python3 -m campaign_tool.newsletter --directory {self.directory} --check")

    def test_one_press_item_counts_checker_problems(self):
        build_press(self.root, [FIRST], DAY)
        build_letter(self.root, [FIRST], DAY, now=NOW)
        self.assertEqual(self.items(), [])  # healthy drafts: nothing
        self.finding(corrections=[*self.finding()["corrections"], {"date": "2026-04-01", "note": "Synthetic fix."}])
        self.meeting(MEETING, "2026-06-10T18:00:00-07:00", status="cancelled")  # the letter: two reasons, one row
        self.put(f"kit/press.suggested/{DAY}-gone.md", "<!-- DRAFT: not sent. Review before any use. findings "
                 "fnd_0000000000000009 -->\n\nbody\n")  # a suggested sibling citing an unpublished finding
        report = press_check.check_press(self.root, now=datetime(2026, 5, 1, tzinfo=timezone.utc))
        self.assertEqual(report["problems"], 3)
        self.assertEqual(self.items(), [self.press_item(3)])

    def test_social_filters_missing_and_counts_all_folders_once(self):
        build_social(self.root)
        build_social(self.root, ["x"])
        self.assertEqual(self.items(), [])
        self.finding(title="A changed synthetic title")  # both written drafts are now outdated
        self.finding("cedar-second", id="fnd_0000000000000002", slug="cedar-second")  # missing twice: not counted
        self.put("kit/social/gone.md", "an old draft for no published finding\n")  # stale
        self.assertEqual(self.items(), [self.social_item(3)])
        os.symlink(self.root / "content", self.root / "kit" / "social" / "mastodon")  # a hazard, not entered
        self.assertEqual(self.items(), [self.social_item(3), ("unreadable", "fix kit/social/: social --check could "
                                        "not check kit/social/mastodon", "open kit/social/ and fix it")])

    def test_newsletter_differences_and_unprovenanced_info(self):
        self.newsletter()
        self.assertEqual(self.items(), [])  # a match adds nothing
        self.meeting(MEETING, "2026-06-10T18:00:00-07:00", status="cancelled")
        self.meeting("cedar-later", "2026-07-01T18:00:00-07:00")  # upcoming but not in the draft
        self.assertEqual(self.items(), [self.newsletter_item(2)])
        self.newsletter(provenance=False)
        self.assertEqual(self.items(), [("outreach_not_checked", NOT_CHECKED,
                                         f"python3 -m campaign_tool.newsletter --directory {self.directory} "
                                         "--from-content")])
        text = today.run_cli(self.root, "2026-05-01")
        self.assertIn(f"[outreach_not_checked] {NOT_CHECKED} — ", text)
        self.assertNotIn("match", text)
        rows = json.loads(today.run_cli(self.root, "2026-05-01", as_json=True))
        self.assertEqual([(r["kind"], r["what"], r["due"]) for r in rows if r["kind"].startswith("outreach")],
                         [("outreach_not_checked", NOT_CHECKED, None)])
        self.assertEqual(set(rows[0]), {"rank", "kind", "what", "due", "command"})

    def test_absent_empty_and_healthy_drafts_are_byte_identical(self):
        self.meeting("cedar-comment", "2026-05-05T18:00:00-07:00", status="scheduled", participation=PARTICIPATION)

        def outputs():
            return tuple(today.run_cli(self.root, "2026-05-01", as_json=j).replace(str(self.root), "<CAMPAIGN>")
                         for j in (False, True))
        self.assertEqual(outputs(), (NO_DRAFTS_TEXT, NO_DRAFTS_JSON))
        for folder in ("kit/press", "kit/social"):
            (self.root / folder).mkdir(parents=True)
        self.assertEqual(outputs(), (NO_DRAFTS_TEXT, NO_DRAFTS_JSON))
        build_press(self.root, [FIRST], DAY)
        build_social(self.root)
        self.newsletter()
        self.assertEqual(outputs(), (NO_DRAFTS_TEXT, NO_DRAFTS_JSON))

    def test_one_failing_check_is_isolated(self):
        build_press(self.root, [FIRST], DAY)
        build_social(self.root)
        self.put(f"kit/press.suggested/{DAY}-gone.md", "<!-- DRAFT: not sent. Review before any use. findings "
                 "fnd_0000000000000009 -->\n\nbody\n")
        self.put("kit/social/gone.md", "stale\n")
        self.newsletter(provenance=False)
        both = [self.press_item(1), ("outreach_not_checked", NOT_CHECKED, f"python3 -m campaign_tool.newsletter "
                                     f"--directory {self.directory} --from-content"), self.social_item(1)]
        self.assertEqual(sorted(self.items()), sorted(both))
        with mock.patch.object(press_check, "check_press", side_effect=OSError("synthetic failure")):
            self.assertEqual(sorted(self.items()), sorted(both[1:] + [
                ("unreadable", "fix kit/press/: synthetic failure", "open kit/press/ and fix it")]))
        self.put("kit/newsletter-draft.json", "{not json")
        self.assertEqual(self.items(), [self.press_item(1), self.social_item(1), (
            "unreadable", "fix kit/newsletter-draft.json: kit/newsletter-draft.json unreadable: not valid JSON with "
            "unique keys", "open kit/newsletter-draft.json and fix it")])
        (self.root / "kit" / "newsletter-draft.json").unlink()
        os.symlink(self.root / "missing.json", self.root / "kit" / "newsletter-draft.json")  # broken link: not absent
        self.assertEqual([i[0] for i in self.items()], ["outreach_stale", "outreach_stale", "unreadable"])
        (self.root / "content" / "site.json").write_text("{", encoding="utf-8")  # social needs content: one item
        kinds = [(i[0], i[1].split(":")[0]) for i in self.items()]
        self.assertEqual(kinds, [("outreach_stale", "press"), ("unreadable", "fix kit/newsletter-draft.json"),
                                 ("unreadable", "fix kit/social/")])

    def test_day_boundary_owner_and_read_only(self):
        self.meeting("cedar-dawn", "2026-05-01T00:00:00+00:00")  # starts exactly at the as-of UTC day start
        self.newsletter()
        self.assertEqual(self.items(), [])  # not yet past at 2026-05-01T00:00Z
        self.assertEqual(self.items(date(2026, 5, 2)), [self.newsletter_item(1)])  # already started
        self.assertEqual(self.items(date(2026, 5, 2)), self.items(date(2026, 5, 2)))
        self.assertEqual(self.items(date(2026, 5, 2), owner="Pat Organizer"), [self.newsletter_item(1)])
        build_press(self.root, [FIRST], DAY)
        build_social(self.root)
        self.put("kit/social/gone.md", "stale\n")
        before = snapshot(self.root)
        refuse = [mock.patch(f"campaign_tool.{name}", side_effect=AssertionError(name)) for name in
                  ("social.build_social", "press.build_press", "press.build_letter", "newsletter.draft.write_draft")]
        for patcher in refuse:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.assertEqual(self.items(date(2026, 5, 2)), [self.newsletter_item(1), self.social_item(1)])
        today.run_cli(self.root, "2026-05-02", as_json=True)
        self.assertEqual(snapshot(self.root), before)

    def test_new_ranks_follow_finding_work_and_keep_the_suffix(self):
        at = today.KINDS.index("finding_blocked")
        self.assertEqual(today.KINDS[at:at + 4], ("finding_blocked", "outreach_stale", "outreach_not_checked",
                                                  "contract_ending"))
        self.assertLess(today.KINDS.index("correction_request_open"), at)
        self.assertEqual(today.KINDS[-5:], ("review_waiting", "stale_body_schedule", "expected_regular_meeting",
                                            "backup_due", "unreadable"))
        self.assertEqual(len(set(today.KINDS)), len(today.KINDS))


if __name__ == "__main__":
    unittest.main()
