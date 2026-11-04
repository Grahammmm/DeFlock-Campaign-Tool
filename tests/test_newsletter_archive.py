"""DF1952-09: sent drafts are kept privately and exported to a local public archive by explicit command. Synthetic; nothing is sent."""
import contextlib
import hashlib
import io
import json
import os
import shutil
import smtplib
import socket
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool.content import ContentError, load_content  # noqa: E402
from campaign_tool.content_check import content_problems, problem_record  # noqa: E402
from campaign_tool.newsletter import archive as archive_module  # noqa: E402
from campaign_tool.newsletter.__main__ import main  # noqa: E402
from campaign_tool.newsletter.archive import issue_paths, publish_issue  # noqa: E402
from campaign_tool.newsletter.draft import build_draft  # noqa: E402
from campaign_tool.newsletter.sent import last_sent  # noqa: E402
from campaign_tool.site import build_site  # noqa: E402
from tests.test_site_pages import CLEAN_PNG  # noqa: E402

EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
BASE = "https://cedar-records.example.invalid"
DAY = "2026-03-05"
NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)


def tree(folder):
    folder = Path(folder)
    return {p.relative_to(folder).as_posix(): (p.read_bytes() if p.is_file() and not p.is_symlink() else None)
            for p in sorted(folder.rglob("*"))} if folder.exists() else {}


def public_issue(day, title="Synthetic issue", body="A **bold** line.\n"):
    return {"schema_version": 1, "sent_on": day, "title": title, "body_md": body}


def manifest(consent_footer=None, **extra):
    campaign = {"name": "Synthetic", "base_url": "https://synthetic.example.invalid", "county_name": "Synthetic"}
    if consent_footer:
        campaign["consent_footer"] = consent_footer
    return {"schema_version": 1, "campaign": campaign, "since": None, **extra,
            "findings": [{"title": "Synthetic finding", "summary": "The agency violated the law.",
                          "classification": "documented_fact", "confidence": "verified", "path": "/findings/a.html",
                          "published_at": "2026-03-01T00:00:00+00:00", "corrected": False}],
            "meetings": [{"body": "Synthetic Council", "starts_at": "2099-05-01T18:00:00-07:00", "agenda_item": "Item",
                          "agenda_url": "https://synthetic.example.invalid/agenda", "relevance": None}],
            "decisions": [{"body": "Synthetic Council", "on": "2026-02-01", "result": "approved", "vote": "3-2",
                           "source_url": "https://synthetic.example.invalid/minutes"}],
            "requests": [{"agency": "Synthetic Agency", "topic": "Policy", "asked_on": "2026-02-01", "updated_on": None,
                          "state": "open", "sentence": "Asked on 2026-02-01.", "path": "/agencies/a.html"}],
            "records": [{"title": "Synthetic record", "agency": "Synthetic Agency", "received_at": "2026-02-02",
                         "prefix": "abcdef012345", "public_url": None, "path": "/sources.html#x"}],
            "map": {"records": [{"recorded_on": "2026-02-03", "osm_base": "2026-02-02", "source": "x", "total": 10,
                                 "by_city": {"Synthetic City": 10}, "added": 1, "removed": 0}],
                    "label": "Unincorporated", "path": "/cameras.html"}}


class ArchiveBase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-newsletter-archive-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.kit, self.content = self.root / "kit", self.root / "content"
        self.copies, self.sent = self.kit / "newsletter-issues", self.kit / "newsletter-sent.json"
        self.draft, self.public = self.kit / "newsletter-draft.json", self.content / "newsletters"

    def run_main(self, *extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(socket, "socket", side_effect=AssertionError("network call")) as net, \
                mock.patch.object(smtplib, "SMTP", side_effect=AssertionError("email")), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = main(["--directory", str(self.root), *extra])
            except SystemExit as exc:
                code = exc.code
        self.assertFalse(net.called, "no network call")
        return code, stdout.getvalue(), stderr.getvalue()

    def ok(self, *extra):
        code, out, err = self.run_main(*extra)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def refused(self, message, *extra):
        code, out, err = self.run_main(*extra)
        self.assertEqual((code, out), (1, ""), err)
        self.assertIn("Stopped: ", err)
        self.assertIn(message, err)
        self.assertNotIn("Traceback", err)

    def usage(self, *extra):
        code, out, _ = self.run_main(*extra)
        self.assertEqual((code, out), (2, ""))

    def journal(self, *entries):
        self.kit.mkdir(exist_ok=True)
        self.sent.write_text(json.dumps({"schema_version": 1, "issues": [
            {"sent_on": day, "draft_sha256": digest, "subject": "S", "recorded_at": "x"} for day, digest in entries]}))

    def plant(self, day, data):
        """A kept copy with ``data`` and a journal entry that names its hash."""
        self.copies.mkdir(parents=True, exist_ok=True)
        (self.copies / (day + ".json")).write_bytes(data)
        self.journal((day, hashlib.sha256(data).hexdigest()))

    def archived(self, day=DAY, *extra):
        """From-content draft, recorded and published on ``day``."""
        self.ok("--from-content", *extra)
        self.ok("sent", "--date", day)
        return self.ok("archive", "--date", day, "--publish")


class KeptIssueTests(ArchiveBase):
    def test_sent_keeps_exact_bytes_private(self):
        self.ok("--from-content")
        first = self.draft.read_bytes()
        out = self.ok("sent", "--date", DAY)
        self.assertEqual((set(out), out["sent"]), ({"recorded", "sent"}, False))
        copy = self.copies / (DAY + ".json")
        self.assertEqual(copy.read_bytes(), first)
        self.assertEqual(hashlib.sha256(copy.read_bytes()).hexdigest(), out["recorded"]["draft_sha256"])
        self.assertEqual(json.loads(self.sent.read_text())["issues"], [out["recorded"]])
        self.assertEqual((stat.S_IMODE(copy.stat().st_mode), stat.S_IMODE(self.copies.stat().st_mode)), (0o600, 0o700))
        self.assertFalse(self.public.exists(), "recording publishes nothing")
        self.ok("--from-content", "--since", "2026-01-01")  # a later draft replaces the current one, not the kept copy
        second = self.draft.read_bytes()
        self.assertNotEqual(first, second)
        self.assertEqual(copy.read_bytes(), first)
        out = self.ok("--record-sent", "2026-03-06")  # the older spelling writes the same kept copy
        self.assertEqual((self.copies / "2026-03-06.json").read_bytes(), second)
        self.assertEqual(out["recorded"]["draft_sha256"], hashlib.sha256(second).hexdigest())
        self.assertEqual(sorted(p.name for p in self.copies.iterdir()), [DAY + ".json", "2026-03-06.json"])
        self.refused("this draft was already recorded as sent on 2026-03-06", "sent", "--date", "2026-03-09")
        self.ok("--from-content", "--since", "2026-02-01")
        before = tree(self.kit)
        self.refused("an issue was already recorded as sent on 2026-03-06", "sent", "--date", "2026-03-06")
        self.refused("sent date 2026-03-05 is before the last recorded send", "sent", "--date", "2026-03-05")
        self.refused("is after today", "sent", "--date", "2999-01-01")
        self.assertEqual(tree(self.kit), before)

    def test_copy_and_journal_failures(self):
        self.ok("--from-content")
        outside, sentinel = self.tmp / "outside", self.tmp / "outside" / "sentinel"
        outside.mkdir()
        sentinel.write_text("keep")

        before = tree(self.kit)
        os.symlink(outside, self.copies)  # a linked folder is refused, nothing is written through it
        self.refused("kit/newsletter-issues must be a real folder", "sent", "--date", DAY)
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["sentinel"])
        self.copies.unlink()
        self.copies.write_text("a file")  # a non-folder in its place
        self.refused("kit/newsletter-issues must be a real folder", "sent", "--date", DAY)
        self.copies.unlink()
        self.copies.mkdir()
        os.symlink(sentinel, self.copies / (DAY + ".json"))  # a linked target is never opened or replaced
        self.refused(f"kit/newsletter-issues/{DAY}.json already exists", "sent", "--date", DAY)
        self.assertEqual(sentinel.read_text(), "keep")
        (self.copies / (DAY + ".json")).unlink()
        (self.copies / (DAY + ".json")).write_text("orphan")  # crash-orphan: never overwritten or adopted
        self.refused("with no matching record", "sent", "--date", DAY)
        self.assertEqual((self.copies / (DAY + ".json")).read_text(), "orphan")
        self.assertFalse(self.sent.exists())
        (self.copies / (DAY + ".json")).unlink()
        self.copies.rmdir()
        self.assertEqual(tree(self.kit), before)
        # injected failures leave no copy, no staging and the previous history
        for patched, effect in (("os.replace", OSError("injected")), ("os.fsync", OSError("injected")),
                                ("os.fsync", [None, OSError("injected")])):
            with self.subTest(patched=patched, effect=effect), mock.patch("campaign_tool.newsletter.sent." + patched,
                                                                          side_effect=effect):
                self.refused("injected", "sent", "--date", DAY)
            self.assertEqual(tree(self.kit), before)
        self.ok("sent", "--date", DAY)
        self.ok("--from-content", "--since", "2026-01-01")
        before = tree(self.kit)
        with mock.patch("campaign_tool.newsletter.sent.os.replace", side_effect=OSError("injected")):
            self.refused("injected", "sent", "--date", "2026-03-06")
        self.assertEqual(tree(self.kit), before)  # earlier copy, journal and folder untouched
        self.draft.write_bytes(b" " * (4 * 1024 * 1024 + 1))
        before = tree(self.kit)
        self.refused("kit/newsletter-draft.json is larger than 4194304 bytes", "sent", "--date", "2026-03-07")
        self.draft.unlink()
        self.draft.mkdir()
        self.refused("kit/newsletter-draft.json is not a regular file", "sent", "--date", "2026-03-07")
        self.draft.rmdir()
        self.assertEqual({k: v for k, v in tree(self.kit).items() if k != "newsletter-draft.json"},
                         {k: v for k, v in before.items() if k != "newsletter-draft.json"})
        self.assertEqual(sentinel.read_text(), "keep")

    def test_archive_requires_explicit_publish_and_hash(self):
        self.ok("--from-content")
        self.refused("no sent record for 2026-03-09", "archive", "--date", "2026-03-09")
        out_hash = self.ok("sent", "--date", DAY)["recorded"]["draft_sha256"]
        copy, target = self.copies / (DAY + ".json"), self.public / (DAY + ".json")
        before = tree(self.root)
        out = self.ok("archive", "--date", DAY)  # eligibility only
        self.assertEqual((out["published"], out["sent"], out["target"]), (False, False, f"content/newsletters/{DAY}.json"))
        self.assertEqual(tree(self.root), before)
        out = self.ok("archive", "--date", DAY, "--publish")
        self.assertEqual((out["published"], out["sent"]), (True, False))
        published = json.loads(target.read_text())
        self.assertEqual(list(published), ["schema_version", "sent_on", "title", "body_md"])
        self.assertEqual(published["sent_on"], DAY)
        self.assertNotIn(hashlib.sha256(copy.read_bytes()).hexdigest(), target.read_text())
        self.assertEqual(hashlib.sha256(copy.read_bytes()).hexdigest(), out_hash)  # the kept copy is not touched
        self.assertEqual(content_problems(self.root), [])
        before = tree(self.root)
        self.refused(f"content/newsletters/{DAY}.json already exists", "archive", "--date", DAY, "--publish")
        self.refused(f"content/newsletters/{DAY}.json already exists", "archive", "--date", DAY)
        self.assertEqual(tree(self.root), before)
        # the kept copy is the only source: changed bytes, a missing copy and a missing record all refuse
        target.unlink()
        self.public.rmdir()
        good = copy.read_bytes()
        copy.write_bytes(good + b" ")
        before = tree(self.root)
        self.refused("does not match the recorded draft_sha256", "archive", "--date", DAY, "--publish")
        copy.write_bytes(good)
        copy.unlink()
        self.refused("no kept copy", "archive", "--date", DAY, "--publish")
        self.assertEqual(tree(self.root), {k: v for k, v in before.items() if not k.startswith("kit/newsletter-issues/")})
        bad = {"not json": b"{not json", "duplicate keys": b'{"subject": "a", "subject": "b"}', "not an object": b"[]",
               "bad projection": json.dumps({"subject": "S", "public_archive": {"title": "t"}}).encode(),
               "oversize": b" " * (4 * 1024 * 1024 + 1)}
        for name, data in bad.items():
            with self.subTest(name):
                self.plant(DAY, data)
                code, out, err = self.run_main("archive", "--date", DAY, "--publish")
                self.assertEqual((code, out), (1, ""), err)
                self.assertIn("Stopped: ", err)
                self.assertFalse(self.public.exists())
        self.journal((DAY, hashlib.sha256(good).hexdigest()), (DAY, "ab" * 32))  # legacy duplicate-day history
        copy.write_bytes(good)
        self.assertEqual(last_sent(self.root), DAY)
        self.refused("recorded more than once", "archive", "--date", DAY, "--publish")
        self.assertFalse(self.public.exists())
        with self.assertRaises(ValueError):
            publish_issue(self.root, "2026-02-30")
        for flags in (("--publish",), ("--date", DAY), ("sent",), ("archive",), ("sent", "--date", DAY, "--publish"),
                      ("archive", "--date", "2026-02-30"), ("archive", "--date", DAY, "--from-content"),
                      ("sent", "--date", DAY, "--since", DAY), ("archive", "--date", DAY, "--site", "x"),
                      ("archive", "--date", DAY, "--record-sent", DAY), ("archive", "--date", DAY, "--manifest", "m.json")):
            with self.subTest(flags=flags):
                self.usage(*flags)

    def test_never_uses_the_current_draft_or_content_as_a_fallback(self):
        self.ok()  # template draft: kept as recorded, but it carries no public projection
        template = self.draft.read_bytes()
        self.ok("sent", "--date", DAY)
        self.assertEqual((self.copies / (DAY + ".json")).read_bytes(), template)
        self.ok("--from-content")  # the current draft now has one, but it is not the recorded issue
        self.assertIn("public_archive", json.loads(self.draft.read_text()))
        self.refused("no public archive projection", "archive", "--date", DAY, "--publish")
        self.assertFalse(self.public.exists())
        manifest_file = self.tmp / "m.json"
        manifest_file.write_text(json.dumps(manifest()))
        self.ok("--manifest", str(manifest_file))
        self.ok("sent", "--date", "2026-03-06")
        self.refused("no public archive projection", "archive", "--date", "2026-03-06")


class ProjectionTests(ArchiveBase):
    def test_projection_excludes_private_canaries(self):
        footer = "CANARY-FOOTER sender Canary Sender. Unsubscribe at any time."
        draft = build_draft(manifest(footer, private_notes="CANARY-NOTE", sender="CANARY-SENDER"), NOW, archive_public=True)
        public = draft["public_archive"]
        self.assertEqual((set(public), public["schema_version"], public["title"]), ({"schema_version", "title", "body_md"}, 1, draft["subject"]))
        self.assertTrue(draft["review_flags"], "the legal-claim note travels with the private draft only")
        blob = json.dumps(public)
        for canary in ("CANARY", "nsubscribe", "{{", "TEMPLATE", "review-required", "excluded", "consent"):
            self.assertNotIn(canary, blob)
        for note in draft["review_flags"]:
            self.assertNotIn(note, blob)
        self.assertTrue(draft["text"].startswith(public["body_md"]))  # same Markdown, footer comes after it
        self.assertIn("CANARY-FOOTER", draft["text"])
        # every dependency section, in its existing order, the map caveat and the closing sentence are frozen in
        body = public["body_md"]
        order = ["## New findings", "## Records requests", "## New in the records library", "## Camera map", "## Decisions",
                 "## Upcoming meetings"]
        self.assertEqual([body.index(h) for h in order], sorted(body.index(h) for h in order))
        self.assertIn("community-mapped points (ownership unconfirmed)", body)
        self.assertIn("record abcdef012345", body)
        self.assertTrue(body.rstrip().endswith("This is not legal advice."))
        template = build_draft(manifest(), NOW, archive_public=True)  # the engine's template footer is excluded too
        self.assertEqual(template["consent_footer"], "template")
        self.assertNotIn("TEMPLATE", json.dumps(template["public_archive"]))
        self.assertNotIn("signed up", template["public_archive"]["body_md"])

    def test_private_fields_and_other_issues_never_reach_public_files(self):
        self.ok("--from-content")
        document = json.loads(self.draft.read_text())
        document.update({"private_notes": "CANARY-NOTE", "sender_email": "CANARY-SENDER", "review_flags": ["CANARY-FLAG"]})
        self.draft.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
        self.ok("sent", "--date", DAY)
        self.ok("archive", "--date", DAY, "--publish")
        self.ok("--from-content", "--since", "2026-01-01")
        self.ok("sent", "--date", "2026-03-06")  # recorded, never exported
        published = json.loads((self.public / (DAY + ".json")).read_text())
        self.assertNotIn("CANARY", json.dumps(published))
        self.assertEqual(sorted(p.name for p in self.public.iterdir()), [DAY + ".json"])
        build_site(self.root)
        pages = "".join(p.read_text() for p in (self.root / "public").rglob("newsletters*.html")) \
            + "".join(p.read_text() for p in (self.root / "public" / "newsletters").rglob("*.html"))
        self.assertIn(published["title"], pages)
        for text in ("CANARY", "nsubscribe", "{{", "TEMPLATE", "2026-03-06"):
            self.assertNotIn(text, pages)
        # later content edits do not change what was frozen; a repeated export refuses and leaves it as it was
        frozen = (self.public / (DAY + ".json")).read_bytes()
        finding = next((self.content / "findings").glob("*.json"))
        edited = json.loads(finding.read_text())
        edited["title"] = "Edited later"
        finding.write_text(json.dumps(edited))
        self.refused("already exists", "archive", "--date", DAY, "--publish")
        self.assertEqual((self.public / (DAY + ".json")).read_bytes(), frozen)

    def test_default_callers_and_legacy_identity(self):
        plain = build_draft(manifest(), NOW)
        self.assertNotIn("public_archive", plain)
        opted = build_draft(manifest(), NOW, archive_public=True)
        self.assertEqual({k: v for k, v in opted.items() if k != "public_archive"}, plain)  # HTML and text unchanged
        self.assertEqual((opted["html"], opted["text"]), (plain["html"], plain["text"]))
        with self.assertRaises(TypeError):
            build_draft(manifest(), NOW, True)
        manifest_file = self.tmp / "m.json"
        manifest_file.write_text(json.dumps(manifest()))
        keys = {"subject", "counts", "excluded", "written", "sent", "source", "since_from"}
        out = self.ok("--manifest", str(manifest_file))
        self.assertEqual(set(out), keys)
        self.assertNotIn("public_archive", self.draft.read_text())
        self.assertEqual(set(self.ok()), keys)
        self.assertNotIn("public_archive", self.draft.read_text())
        out = self.ok("--from-content")  # only this opt-in gains the projection, and only inside the hashed file
        self.assertEqual(set(out), keys)
        self.assertEqual(json.loads(self.draft.read_text())["public_archive"]["title"], out["subject"])
        self.assertNotIn("public_archive", out)
        self.ok("--manifest", str(manifest_file))  # a legacy (manifest) draft is kept exactly and archive refuses honestly
        legacy = self.draft.read_bytes()
        self.ok("sent", "--date", DAY)
        self.assertEqual((self.copies / (DAY + ".json")).read_bytes(), legacy)
        self.refused("no public archive projection", "archive", "--date", DAY, "--publish")


class PublicLoaderTests(ArchiveBase):
    def issue(self, name, data):
        self.public.mkdir(parents=True, exist_ok=True)
        path = self.public / name
        path.write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode())
        return path

    def test_public_loader_checker_and_json_paths(self):
        day = "2026-03-05"
        good = public_issue(day)
        long_title, too_big = "t" * 201, "x" * (256 * 1024 + 1)
        cases = {
            f"{day}.json": [
                ("unknown key", dict(good, extra=1)), ("missing key", {k: v for k, v in good.items() if k != "title"}),
                ("version 2", dict(good, schema_version=2)), ("version true", dict(good, schema_version=True)),
                ("not an object", []), ("date mismatch", dict(good, sent_on="2026-03-06")),
                ("bad date", dict(good, sent_on="2026-02-30")), ("blank title", dict(good, title="  ")),
                ("long title", dict(good, title=long_title)), ("multiline title", dict(good, title="a\nb")),
                ("blank body", dict(good, body_md=" \n")), ("big body", dict(good, body_md=too_big)),
                ("raw html", dict(good, body_md="a <b>x</b>")), ("email", dict(good, body_md="write a@b.example")),
                ("phone", dict(good, body_md="call 555-123-4567")), ("hash run", dict(good, body_md="a" * 64)),
                ("unsubscribe", dict(good, body_md="Unsubscribe here")), ("placeholder", dict(good, body_md="{{ x }}")),
                ("template", dict(good, body_md="[TEMPLATE CONSENT FOOTER: x]")),
                ("script link", dict(good, body_md="[x](javascript:alert(1))")), ("http link", dict(good, body_md="[x](http://a.example)")),
                ("data link", dict(good, body_md="[x](data:text/html;base64,AA)")), ("relative link", dict(good, body_md="[x](/a.html)")),
                ("control", dict(good, body_md="a\x07b")), ("carriage return", dict(good, body_md="a\r\nb")),
                ("duplicate keys", b'{"schema_version": 1, "schema_version": 1, "sent_on": "%s", "title": "t", "body_md": "b"}' % day.encode()),
                ("not utf-8", b"\xff\xfe"), ("not json", b"{"), ("file too big", json.dumps(dict(good, body_md="y " * 300000)).encode()),
            ],
            "2026-3-5.json": [("short date name", good)], "notes.json": [("not a date name", good)],
            "2026-02-30.json": [("impossible date name", dict(good, sent_on="2026-02-30"))],
        }
        for name, items in cases.items():
            for label, data in items:
                with self.subTest(name=name, case=label):
                    path = self.issue(name, data)
                    problems = [p for p in content_problems(self.root) if p.startswith("newsletters")]
                    self.assertEqual(len(problems), 1, problems)
                    self.assertTrue(problems[0].startswith(f"newsletters/{name}: "), problems)
                    self.assertEqual(problem_record(problems[0])["file"], f"newsletters/{name}")
                    with self.assertRaisesRegex(ContentError, f"^newsletters/{name}: "):
                        load_content(self.root)
                    self.assertNotIn("CANARY", problems[0])
                    path.unlink()
        self.assertEqual(content_problems(self.root), [])
        self.issue(f"{day}.json", dict(good, body_md="[ok](https://example.org/a) plain text, 2026-03-05, record abcdef012345.\n"))
        self.assertEqual(content_problems(self.root), [])  # build and check accept the same file
        self.assertEqual([i["sent_on"] for i in load_content(self.root)["newsletters"]], [day])
        # every bad issue is named at once; other extensions are ignored
        self.issue("2026-03-07.json", dict(good, sent_on="2026-03-07", title=""))
        self.issue("2026-03-08.json", b"{")
        (self.public / "README.txt").write_text("not an issue")
        problems = [p for p in content_problems(self.root) if p.startswith("newsletters")]
        self.assertEqual([p.split(":")[0] for p in problems], ["newsletters/2026-03-07.json", "newsletters/2026-03-08.json"])
        # entries that are not regular files and a linked or oversized folder
        (self.public / "2026-03-07.json").unlink()
        (self.public / "2026-03-08.json").unlink()
        (self.public / "2026-03-09.json").mkdir()
        os.symlink(self.tmp / "nowhere", self.public / "2026-03-10.json")
        problems = [p for p in content_problems(self.root) if p.startswith("newsletters")]
        self.assertEqual([p.split(":")[0] for p in problems], ["newsletters/2026-03-09.json", "newsletters/2026-03-10.json"])
        (self.public / "2026-03-09.json").rmdir()
        (self.public / "2026-03-10.json").unlink()
        moved = self.tmp / "moved"
        self.public.rename(moved)
        os.symlink(moved, self.public)
        self.assertEqual([p for p in content_problems(self.root) if p.startswith("newsletters")],
                         ["newsletters: the folder must not be a symlink"])
        with self.assertRaisesRegex(ContentError, "newsletters: the folder must not be a symlink"):
            load_content(self.root)
        self.public.unlink()
        moved.rename(self.public)
        with mock.patch.object(archive_module, "MAX_PUBLIC_ISSUES", 1):
            self.issue("2026-03-11.json", public_issue("2026-03-11"))
            with self.assertRaises(ValueError):
                issue_paths(self.content)
            self.assertEqual([p for p in content_problems(self.root) if p.startswith("newsletters")],
                             ["newsletters: at most 1 issue files"])

    def test_refusal_leaves_the_previous_public_site_untouched(self):
        self.issue(f"{DAY}.json", public_issue(DAY))
        build_site(self.root)
        built = tree(self.root / "public")
        self.issue("2026-03-06.json", public_issue("2026-03-06", body="<script>x</script>"))
        with self.assertRaisesRegex(ContentError, "newsletters/2026-03-06.json: body_md: raw HTML"):
            build_site(self.root)
        self.assertEqual(tree(self.root / "public"), built)
        self.assertEqual(sorted(p.name for p in self.root.glob(".public.*")), [])


class SiteTests(ArchiveBase):
    def read(self, name):
        return (self.root / "public" / name).read_text(encoding="utf-8")

    def issues(self):
        for day, title in (("2026-03-05", "First issue"), ("2026-03-10", "Second issue")):
            self.public.mkdir(parents=True, exist_ok=True)
            (self.public / (day + ".json")).write_text(json.dumps(public_issue(day, title, f"Body of {title}.\n")))

    def test_pages_sitemap_reservations_and_rebuild(self):
        self.issues()
        written = {p.relative_to(self.root / "public").as_posix() for p in build_site(self.root)}
        self.assertTrue({"newsletters.html", "newsletters/2026-03-05.html", "newsletters/2026-03-10.html"} <= written)
        index = self.read("newsletters.html")
        self.assertLess(index.index("2026-03-10"), index.index("2026-03-05"))  # newest first
        self.assertIn('<a href="/newsletters/2026-03-10.html">Second issue</a>', index)
        page = self.read("newsletters/2026-03-05.html")
        for text in ("First issue", "Body of First issue.", 'datetime="2026-03-05"', 'href="/newsletters.html"',
                     f'<link rel="canonical" href="{BASE}/newsletters/2026-03-05.html">'):
            self.assertIn(text, page)
        for text in ("nsubscribe", "consent", "{{", "<form"):
            self.assertNotIn(text, page + index)
        self.assertIn('<a href="/newsletters.html">Newsletter archive</a>', self.read("index.html"))
        urls = [u for u in self.read("sitemap.xml").split("<loc>") if "newsletters" in u]
        self.assertEqual([u.split("</loc>")[0] for u in urls],
                         [f"{BASE}/newsletters.html", f"{BASE}/newsletters/2026-03-10.html", f"{BASE}/newsletters/2026-03-05.html"])
        # a removed public issue disappears on rebuild
        (self.public / "2026-03-10.json").unlink()
        build_site(self.root)
        self.assertFalse((self.root / "public" / "newsletters" / "2026-03-10.html").exists())
        self.assertNotIn("2026-03-10", self.read("newsletters.html") + self.read("sitemap.xml"))
        # no base URL: root-relative links, no sitemap
        site_path = self.content / "site.json"
        site = json.loads(site_path.read_text())
        site["base_url"] = None
        site_path.write_text(json.dumps(site))
        build_site(self.root)
        self.assertFalse((self.root / "public" / "sitemap.xml").exists())
        self.assertIn('href="/newsletters/2026-03-05.html"', self.read("newsletters.html"))
        self.assertNotIn("canonical", self.read("newsletters/2026-03-05.html"))

    def test_no_issues_means_unchanged_output(self):
        build_site(self.root)
        base = tree(self.root / "public")
        self.assertFalse({"newsletters.html", "newsletters"} & {p.split("/")[0] for p in base})
        self.assertNotIn("newsletters", self.read("index.html") + self.read("sitemap.xml"))
        self.public.mkdir()
        build_site(self.root)  # an empty folder is the same as none
        self.assertEqual(tree(self.root / "public"), base)
        self.public.rmdir()
        self.assertEqual(tree(self.root / "public"), base)

    def test_slug_and_asset_collisions_refuse_before_writes(self):
        build_site(self.root)
        built = tree(self.root / "public")
        pages = self.content / "pages"
        pages.mkdir(exist_ok=True)
        (pages / "newsletters.json").write_text(json.dumps({"title": "T", "body_md": "x"}))
        for issues in (False, True):
            if issues:
                self.issues()
            with self.assertRaisesRegex(ContentError, "pages/newsletters.json: slug newsletters is reserved"):
                build_site(self.root)
        (pages / "newsletters.json").unlink()
        self.assertEqual(content_problems(self.root), [])
        finding = next((self.content / "findings").glob("*.json"))
        original = finding.read_text()
        finding.write_text(json.dumps(dict(json.loads(original), slug="newsletters")))
        with self.assertRaisesRegex(ContentError, "slug: 'newsletters' is reserved"):
            build_site(self.root)
        finding.write_text(original)
        assets = self.content / "public-assets"
        for relative in ("newsletters/logo.png", "newsletters/2026-03-05/x.png"):
            (assets / relative).parent.mkdir(parents=True, exist_ok=True)
            (assets / relative).write_bytes(CLEAN_PNG)
            with self.assertRaisesRegex(ContentError, "collides with a generated path"):
                build_site(self.root)
            shutil.rmtree(assets / "newsletters")
        shutil.rmtree(self.public, ignore_errors=True)
        self.assertEqual(tree(self.root / "public"), built)  # every refusal came before anything was written


class BuildIsolationTests(ArchiveBase):
    def test_no_private_reads_or_network_during_build(self):
        self.ok("--from-content")
        self.ok("sent", "--date", DAY)
        self.ok("archive", "--date", DAY, "--publish")
        shutil.rmtree(self.kit)  # the build needs only public inputs: no kit, no kept copies, no private notes
        with mock.patch.object(socket, "socket", side_effect=AssertionError("network call")), \
                mock.patch.object(smtplib, "SMTP", side_effect=AssertionError("email")), \
                mock.patch.object(archive_module, "publish_issue", side_effect=AssertionError("private read")), \
                mock.patch.object(archive_module, "read_kept_copy", side_effect=AssertionError("private read")):
            build_site(self.root)
        self.assertTrue((self.root / "public" / "newsletters" / f"{DAY}.html").is_file())
        self.assertFalse(self.kit.exists())
        # hostile Markdown is refused (not rendered) or inert text
        issue = self.public / (DAY + ".json")
        document = json.loads(issue.read_text())
        for body in ("<img src=x onerror=alert(1)>", "[x](javascript:alert(1))", "![x](/y.png)"):
            issue.write_text(json.dumps(dict(document, body_md=body)))
            with self.assertRaises(ContentError):
                build_site(self.root)
        issue.write_text(json.dumps(dict(document, body_md="**bold** &amp; \"quoted\" text\n")))
        build_site(self.root)
        page = (self.root / "public" / "newsletters" / f"{DAY}.html").read_text()
        self.assertIn("<strong>bold</strong> &amp;amp; &quot;quoted&quot; text", page)


if __name__ == "__main__":
    unittest.main()
