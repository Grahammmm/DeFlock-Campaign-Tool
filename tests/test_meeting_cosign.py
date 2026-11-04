"""DF2207-14: consent-scoped co-signed written comments (synthetic organizations only; no network).

A listing is never permission to co-sign: the private consent record carries an optional ``cosign`` grant for ``any``
meeting or a list of meeting ids, and ``meetings-stage --cosign`` names only organizations whose grant covers that exact
meeting. Removal or a narrowed grant makes every staged meeting that still lists the id fail ``content-check``.
"""
import contextlib
import io
import json
from html import unescape
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool.content import ContentError, load_content
from campaign_tool.content_check import content_problems
from campaign_tool.meeting_stage import MeetingStageError, stage
from campaign_tool.meetings import COSIGN_HEADING, MeetingsError, comment_kit_md, cosign_block_md
from campaign_tool.site import build_site
from campaign_tool.supporters import (SupporterError, add_supporter, remove_supporter, resolve_cosigners,
                                      set_cosign_consent)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 10, 12)
MID, MID2 = "legistar-examplecity-1001", "legistar-examplecity-1002"
STARTS = {MID: "2026-10-14T18:00:00-07:00", MID2: "2026-10-21T18:00:00-07:00"}
PART = {"in_person": True, "remote": {"available": True, "how": "Zoom link on the posted agenda"},
        "written_comment": {"how": "Email the city clerk", "deadline": "2026-10-14T12:00:00-07:00"},
        "source_url": "https://example.invalid/council/agenda-2026-10-14",
        "verified_by": "Synthetic Organizer", "verified_at": "2026-10-10"}
A, B, C = "Alpha Valley Alliance", "Birch Hollow Coalition", "Cedar Ridge Network"
IDS = {A: "alpha-valley-alliance", B: "birch-hollow-coalition", C: "cedar-ridge-network"}
LISTING_DATE, GRANT_DATE = "2026-09-01", "2026-08-17"  # GRANT_DATE appears only in the private grant
DIGEST = "ab" * 32


def snapshot(root):
    """Every file under ``root`` but the build output: bytes, mode and mtime (links not followed)."""
    found = {}
    for path in sorted(Path(root).rglob("*")):
        relative = path.relative_to(root).as_posix()
        if relative == "public" or relative.startswith("public/"):
            continue
        info = os.lstat(path)
        found[relative] = (None if path.is_dir() or path.is_symlink() else path.read_bytes(), info.st_mode, info.st_mtime_ns)
    return found


def tree(public):
    return {p.relative_to(public).as_posix(): p.read_bytes() for p in sorted(Path(public).rglob("*")) if p.is_file()}


def run(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=120)


def org_name(n):
    """A distinct synthetic organization name with no markup characters."""
    return f"Zed {chr(97 + n % 26)}{chr(97 + n // 26)} Alliance"


def written_section(kit):
    return kit.split("## Written comment\n", 1)[1].split("\n\n## ", 1)[0]


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-meeting-cosign-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public_file = self.root / "content" / "supporters.json"
        self.private_file = self.root / "kit" / "supporters-consent.json"
        (self.root / "kit").mkdir(exist_ok=True)
        (self.root / "kit" / "meetings.json").write_text(json.dumps({"schema_version": 1, "meetings": [
            {"id": mid, "body": "Example City Council", "starts_at": starts, "agenda_item": "Item 7: ALPR",
             "agenda_url": "https://example.invalid/agenda"} for mid, starts in STARTS.items()]}), encoding="utf-8")

    def add(self, name, **fields):
        values = dict(name=name, kind="community", organization=True, confirmed_on=LISTING_DATE, how="email", today=TODAY)
        values.update(fields)
        return add_supporter(self.root, **values)["id"]

    def grant(self, supporter_id, *scope, **fields):
        values = dict(cosign=list(scope) or ["any"], confirmed_on=GRANT_DATE, how="letter", today=TODAY)
        values.update(fields)
        return set_cosign_consent(self.root, supporter_id, **values)

    def ledger(self, *digests):
        (self.root / "private").mkdir(exist_ok=True)
        db = sqlite3.connect(self.root / "private" / "ledger.sqlite")
        db.execute("CREATE TABLE IF NOT EXISTS objects (sha256 TEXT PRIMARY KEY)")
        db.executemany("INSERT OR IGNORE INTO objects VALUES (?)", [(d,) for d in digests])
        db.commit()
        db.close()

    def part_file(self, mid=MID, written=True, name=None):
        value = json.loads(json.dumps(PART))
        value["written_comment"] = ({"how": "Email the city clerk", "deadline": STARTS[mid][:10] + "T12:00:00-07:00"}
                                    if written else None)
        path = self.tmp / (name or f"participation-{mid}.json")
        path.write_text(json.dumps(value), encoding="utf-8")
        return str(path)

    def stage(self, mid=MID, ids=(), written=True, **kwargs):
        part = self.part_file(mid, written)
        return stage(self.root, mid, participation=part, today=TODAY, cosign_ids=list(ids) if ids else None, **kwargs)

    def document(self, mid=MID):
        return json.loads((self.root / "content" / "meetings" / f"{mid}.json").read_text(encoding="utf-8"))

    def edit(self, mid, change):
        path = self.root / "content" / "meetings" / f"{mid}.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        change(document)
        path.write_text(json.dumps(document, indent=2), encoding="utf-8")

    def private(self):
        return json.loads(self.private_file.read_text(encoding="utf-8"))

    def write_private(self, document):
        self.private_file.write_text(json.dumps(document), encoding="utf-8")
        os.chmod(self.private_file, 0o600)

    def meeting_files(self):
        return sorted(p.name for p in (self.root / "content" / "meetings").glob("legistar-*.json"))


class ScopeTests(Base):
    def test_scope_is_explicit_and_exact(self):
        a, b = self.add(A), self.add(B)
        before = snapshot(self.root)
        with self.assertRaisesRegex(SupporterError, f"--cosign {a}: has not consented to co-sign$"):
            self.stage(ids=[a])  # a listing alone never signs
        self.assertEqual(self.meeting_files(), [])
        self.assertEqual(snapshot(self.root), before)
        self.grant(a)  # any: two different meetings
        self.grant(b, MID)  # an exact list
        self.assertEqual(self.stage(MID, [a, b])["cosigners"], [a, b])
        self.assertEqual(self.stage(MID2, [a])["cosigners"], [a])
        with self.assertRaisesRegex(SupporterError, "has not consented to co-sign this meeting"):
            self.stage(MID2, [a, b])
        self.assertEqual(self.document(MID2)["cosigners"], [a])  # the refused attempt wrote nothing
        self.grant(b, "legistar-examplecity-100")  # no prefix matching: 1001 is not covered by ...100
        with self.assertRaisesRegex(SupporterError, "has not consented to co-sign this meeting"):
            self.stage(MID, [b], replace=True)
        with self.assertRaisesRegex(SupporterError, "not listed"):
            resolve_cosigners(self.root, MID, ["no-such-org"], today=TODAY)

    def test_invalid_grants_and_binding_refuse_without_writes(self):
        a = self.add(A)
        self.grant(a)
        good = self.private()
        record = good["supporters"][a]
        many = [f"meeting-{n}" for n in range(101)]
        cases = [("bool", {"cosign": True}), ("null", {"cosign": None}), ("empty", {"cosign": []}),
                 ("upper", {"cosign": "ANY"}), ("other word", {"cosign": "all"}), ("duplicate", {"cosign": ["m-a", "m-a"]}),
                 ("oversize", {"cosign": many}), ("any in list", {"cosign": ["any"]}), ("number", {"cosign": [5]}),
                 ("bad id", {"cosign": ["Not A Slug"]}),
                 ("future date", {"cosign_confirmed_on": "2999-01-01"}), ("bad how", {"cosign_how": "telepathy"}),
                 ("short digest", {"cosign_correspondence": "abcd"}), ("hash mismatch", {"public_sha256": "0" * 64})]
        for label, change in cases:
            with self.subTest(label):
                document = json.loads(json.dumps(good))
                document["supporters"][a].update(change)
                self.write_private(document)
                before = snapshot(self.root)
                with self.assertRaises(ContentError):
                    self.stage(ids=[a])
                self.assertEqual(snapshot(self.root), before)
                self.assertEqual(self.meeting_files(), [])
        for label, keys in (("missing companion", ["cosign_how"]), ("companion without cosign", ["cosign"])):
            with self.subTest(label):
                document = json.loads(json.dumps(good))
                for key in keys:
                    del document["supporters"][a][key]
                self.write_private(document)
                with self.assertRaises(ContentError):
                    self.stage(ids=[a])
        del record  # listing fields stay as they were

    def test_unlisted_or_missing_store_never_signs(self):
        with self.assertRaises(SupporterError):
            self.stage(ids=["alpha-valley-alliance"])  # no supporters at all
        a = self.add(A)
        self.grant(a)
        self.private_file.unlink()
        with self.assertRaises(ContentError):
            self.stage(ids=[a])
        self.assertEqual(self.meeting_files(), [])


class StageTests(Base):
    def setUp(self):
        super().setUp()
        self.ids = [self.add(name) for name in (A, B, C)]
        for supporter_id in self.ids:
            self.grant(supporter_id)

    def test_order_limit_and_letter_only(self):
        a, b, c = self.ids
        report = self.stage(ids=[c, a, b])
        self.assertEqual(report["cosigners"], [c, a, b])
        document = self.document()
        self.assertEqual(document["cosigners"], [c, a, b])
        kit = document["comment_kit_md"]
        self.assertTrue(written_section(kit).endswith("[city]\n\n" + cosign_block_md([C, A, B])))
        self.assertEqual(kit.count(COSIGN_HEADING), 1)
        self.assertEqual([kit.index(n) for n in (C, A, B)], sorted(kit.index(n) for n in (C, A, B)))  # flag order
        for name in (A, B, C):
            self.assertEqual(kit.count(name), 1)  # the letter only: never the spoken, asks or sources sections
        self.assertNotIn("example.invalid/agenda", kit.split("## How to take part")[0].split(COSIGN_HEADING)[1])
        self.assertNotIn(GRANT_DATE, json.dumps(document))
        # duplicate, unknown and over-limit lists are refused, never trimmed or deduplicated
        for ids, needle in (([a, a], "more than once"), ([a, "ghost-org"], "not listed"), ([], "1 to 30")):
            with self.assertRaisesRegex(MeetingStageError if needle != "not listed" else SupporterError, needle):
                stage(self.root, MID, participation=self.part_file(), today=TODAY, cosign_ids=ids, replace=True)
        with self.assertRaisesRegex(MeetingStageError, "1 to 30"):
            self.stage(ids=[f"org-{n}" for n in range(31)], replace=True)
        self.assertEqual(self.document()["cosigners"], [c, a, b])

    def test_thirty_signers_are_kept_in_order(self):
        extra = [self.add(org_name(n)) for n in range(27)]
        for supporter_id in extra:
            self.grant(supporter_id)
        ids = list(reversed(self.ids + extra))
        self.assertEqual(len(ids), 30)
        self.assertEqual(self.stage(ids=ids)["cosigners"], ids)
        self.assertEqual(self.document()["cosigners"], ids)
        self.assertEqual(content_problems(self.root), [])

    def test_special_names_stay_literal(self):
        name = "Parks & Rec [Click](https://evil.example.invalid/x) **Bold** <b>"
        with self.assertRaises(SupporterError):
            self.add(name)  # angle brackets are refused at listing
        name = r"Parks (North) & Rec [Click](https://evil.example.invalid/x) **Bold** _it_ |pipe| \ #!~`"
        supporter_id = self.add(name)
        self.grant(supporter_id)
        self.stage(ids=[supporter_id])
        kit = self.document()["comment_kit_md"]
        self.assertIn(r"- Parks \(North\) & Rec \[Click\]\(https://evil.example.invalid/x\) \*\*Bold\*\* \_it\_ \|pipe\| \\ \#\!\~\`", kit)
        build_site(self.root, today=TODAY)
        page = (self.root / "public" / "meetings" / f"{MID}.html").read_text(encoding="utf-8")
        self.assertNotIn('href="https://evil.example.invalid', page)
        self.assertNotIn("<strong>Bold</strong>", page)
        self.assertIn("Parks", page)
        roster = page.split("<strong>Co-signed by</strong>", 1)[1].split("</ul>", 1)[0]
        items = re.findall(r"<li>(.*?)</li>", roster, re.DOTALL)
        self.assertEqual(len(items), 1)
        self.assertEqual(unescape(re.sub(r"<[^>]*>", "", items[0])), name)

    def test_written_comment_is_required(self):
        a = self.ids[0]
        before = snapshot(self.root)
        with self.assertRaisesRegex(MeetingStageError, "verified written_comment"):
            self.stage(ids=[a], written=False)
        with self.assertRaisesRegex(MeetingStageError, "verified written_comment"):
            stage(self.root, MID, today=TODAY, cosign_ids=[a])  # no --participation: kit text is not a substitute
        self.assertEqual(snapshot(self.root), before)
        meeting = {"body": "Example City Council", "starts_at": STARTS[MID], "agenda_item": "Item 7",
                   "participation": {"in_person": True, "source_url": "https://example.invalid/a", "verified_at": "2026-10-10"}}
        with self.assertRaisesRegex(MeetingsError, "no verified written comment"):
            comment_kit_md(meeting, [], "C", cosign_names=[A])  # direct misuse is loud, not silent omission

    def test_roster_is_not_body_words_and_overcap_refuses(self):
        written = {"how": "Email", "deadline": "2026-10-14T12:00:00-07:00"}

        def kit(words, names=None):
            meeting = {"body": "Board", "starts_at": STARTS[MID], "agenda_item": "word " * words + "end",
                       "participation": {"written_comment": written, "source_url": "https://example.invalid/a",
                                         "verified_at": "2026-10-10"}}
            return comment_kit_md(meeting, [], "C", cosign_names=names)

        def letter_words(text):
            return len(written_section(text).split(COSIGN_HEADING)[0].split())

        room = 400 - letter_words(kit(0))
        names = [org_name(n) for n in range(30)]
        text = kit(room - 20, names)  # letter body ~380 words; with the roster the section is far past 400
        self.assertLessEqual(letter_words(text), 400)
        self.assertGreater(len(written_section(text).split()), 450)
        self.assertEqual(all(n in text for n in names), True)
        self.assertNotIn("over 400 words", text)
        with self.assertRaisesRegex(MeetingsError, "over 400 words"):
            kit(room + 5, names)  # the body falls back to the advisory: nothing to sign
        self.assertIn("over 400 words", kit(room + 5))  # without co-signers the advisory kit is unchanged
        for bad in ([], ["x"] * 31, [""], [5]):
            with self.assertRaisesRegex(MeetingsError, "1 to 30"):
                kit(0, bad)

    def test_custom_asks_and_cosign_compose(self):
        a, b, c = self.ids
        asks = ["Publish the policy.", "Hold a public hearing."]
        plain = self.stage(asks=asks)
        plain_bytes = (self.root / "content" / "meetings" / f"{MID}.json").read_bytes()
        self.assertNotIn("cosigners", plain)
        self.assertNotIn("cosigners", json.loads(plain_bytes))
        report = self.stage(ids=[a, b], asks=asks, replace=True)
        self.assertEqual(report["findings"], plain["findings"])
        document = self.document()
        kit = document["comment_kit_md"]
        self.assertIn("## Two asks", kit)
        self.assertIn("1. Publish the policy.\n2. Hold a public hearing.", written_section(kit))
        self.assertEqual(document["cited_findings"], plain["findings"])
        self.assertEqual(kit.replace("\n\n" + cosign_block_md([A, B]), ""), json.loads(plain_bytes)["comment_kit_md"])
        # --replace resolves only the current flags: omitting --cosign removes the ids and the block, byte for byte
        again = self.stage(asks=asks, replace=True)
        self.assertEqual((self.root / "content" / "meetings" / f"{MID}.json").read_bytes(), plain_bytes)
        self.assertEqual(again, {**plain, "replaced": True})

    def test_cli_flags_and_report(self):
        a, b, c = self.ids
        part = self.part_file()
        result = run("meetings-stage", "--directory", str(self.root), "--id", MID, "--participation", part,
                     "--cosign", b, "--cosign", a)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["cosigners"], [b, a])
        self.assertEqual(self.document()["cosigners"], [b, a])
        for flags in ((a, a), (a, "ghost-org")):
            result = run("meetings-stage", "--directory", str(self.root), "--id", MID, "--participation", part, "--replace",
                         *[x for ident in flags for x in ("--cosign", ident)])
            self.assertEqual(result.returncode, 1)
            self.assertIn("Stopped:", result.stderr)
        self.assertEqual(self.document()["cosigners"], [b, a])


class GrantTests(Base):
    def test_grant_and_revoke_are_private_atomic(self):
        a, b = self.add(A), self.add(B)
        self.ledger(DIGEST)
        public = (self.public_file.read_bytes(), os.stat(self.public_file).st_mtime_ns)
        listing = self.private()["supporters"][a]
        out = self.grant(a, correspondence="ab" * 6)
        self.assertEqual(out, {"id": a, "action": "cosign granted"})
        self.grant(b, MID, MID2)
        self.assertEqual((self.public_file.read_bytes(), os.stat(self.public_file).st_mtime_ns), public)
        self.assertEqual(os.stat(self.private_file).st_mode & 0o777, 0o600)
        records = self.private()["supporters"]
        self.assertEqual(records[a]["cosign"], "any")
        self.assertEqual(records[a]["cosign_correspondence"], DIGEST)
        self.assertEqual(records[b]["cosign"], [MID, MID2])
        self.assertEqual(records[b]["cosign_correspondence"], None)
        for key, value in listing.items():  # the listing confirmation fields are never overwritten
            self.assertEqual(records[a][key], value)
        load_content(self.root, today=TODAY)  # grants alone never change what content loads
        self.assertEqual(set_cosign_consent(self.root, a, clear=True, today=TODAY), {"id": a, "action": "cosign cleared"})
        self.assertEqual(self.private()["supporters"][a], listing)  # the whole optional group is gone
        self.assertEqual((self.public_file.read_bytes(), os.stat(self.public_file).st_mtime_ns), public)

    def test_bad_grants_are_refused_and_change_nothing(self):
        a = self.add(A)
        self.ledger(DIGEST)
        before = snapshot(self.root)
        base = dict(confirmed_on=GRANT_DATE, how="letter", today=TODAY)
        bad = [dict(cosign=["any", MID]), dict(cosign=[]), dict(cosign=["Not A Slug"]), dict(cosign=[MID, MID]),
               dict(cosign=[f"m-{n}" for n in range(101)]),
               dict(cosign=["any"], confirmed_on=None), dict(cosign=["any"], how=None),
               dict(cosign=["any"], confirmed_on="2999-01-01"), dict(cosign=["any"], confirmed_on="2026-02-30"),
               dict(cosign=["any"], how="telepathy"), dict(cosign=["any"], correspondence="cd" * 6),
               dict(cosign=None), dict(cosign=["any"], clear=True), dict(clear=True, how="email")]
        for fields in bad:
            with self.subTest(fields):
                with self.assertRaises(SupporterError):
                    set_cosign_consent(self.root, a, **{**base, **fields})
                self.assertEqual(snapshot(self.root), before)
        with self.assertRaisesRegex(SupporterError, "unknown supporter id"):
            self.grant("ghost-org")
        self.assertEqual(snapshot(self.root), before)

    def test_unsafe_stores_and_failed_replace_leave_the_private_file(self):
        a = self.add(A)
        before = snapshot(self.root)
        with mock.patch("campaign_tool.supporters.os.replace", side_effect=OSError("disk")):
            with self.assertRaisesRegex(SupporterError, "write failed"):
                self.grant(a)
        self.assertEqual([p.name for p in self.private_file.parent.glob(".supporters-*")], [])  # no temporary left
        files = lambda shot: {path: info for path, info in shot.items() if info[0] is not None}  # noqa: E731
        self.assertEqual(files(snapshot(self.root)), files(before))  # a folder's own mtime moves with a temporary file
        outside = self.tmp / "outside.json"
        outside.write_bytes(self.private_file.read_bytes())
        self.private_file.unlink()
        os.symlink(outside, self.private_file)
        with self.assertRaises(SupporterError):
            self.grant(a)
        self.assertNotIn("cosign", outside.read_text(encoding="utf-8"))  # the link target is never written
        self.private_file.unlink()
        shutil.copy(outside, self.private_file)
        os.chmod(self.private_file, 0o644)  # group/other access is refused, never silently tightened
        with self.assertRaises(SupporterError):
            self.grant(a)
        self.assertEqual(os.stat(self.private_file).st_mode & 0o777, 0o644)
        os.chmod(self.private_file, 0o600)
        self.private_file.write_text(self.private_file.read_text().replace('"supporters": {', '"supporters": {"x": 1, "x": 2, ', 1))
        with self.assertRaises(SupporterError):
            self.grant(a)  # a duplicate key makes the store unusable

    def test_cli_consent_prints_only_id_and_action(self):
        a = self.add(A)
        self.ledger(DIGEST)
        result = run("supporters", "consent", "--directory", str(self.root), "--id", a, "--cosign", "any",
                     "--confirmed-on", GRANT_DATE, "--how", "letter", "--correspondence", "ab" * 6)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.splitlines()[0]), {"id": a, "action": "cosign granted"})
        for secret in (DIGEST, GRANT_DATE):
            self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertEqual(self.private()["supporters"][a]["cosign"], "any")
        neither = run("supporters", "consent", "--directory", str(self.root), "--id", a)
        self.assertEqual(neither.returncode, 2)  # one of --cosign / --clear-cosign is required
        both = run("supporters", "consent", "--directory", str(self.root), "--id", a, "--cosign", "any", "--clear-cosign")
        self.assertEqual(both.returncode, 2)
        listed = run("supporters", "consent", "--directory", str(self.root), "--id", a, "--cosign", "any", MID,
                     "--confirmed-on", GRANT_DATE, "--how", "letter")
        self.assertEqual(listed.returncode, 1)
        self.assertIn("Stopped:", listed.stderr)
        cleared = run("supporters", "consent", "--directory", str(self.root), "--id", a, "--clear-cosign")
        self.assertEqual(cleared.returncode, 0, cleared.stderr)
        self.assertNotIn("cosign", self.private()["supporters"][a])


class RemovalAndStaleTests(Base):
    def setUp(self):
        super().setUp()
        self.a, self.b = self.add(A), self.add(B)
        self.grant(self.a)
        self.grant(self.b)
        self.stage(MID, [self.a, self.b])
        self.stage(MID2, [self.a])
        build_site(self.root, today=TODAY)
        self.assertEqual(content_problems(self.root), [])

    def test_removed_supporter_names_every_meeting(self):
        public = tree(self.root / "public")
        remove_supporter(self.root, self.a, today=TODAY)  # removal itself still succeeds
        problems = content_problems(self.root)
        for mid in (MID, MID2):
            self.assertIn(f"meetings/{mid}.json.cosigners: {self.a}: is not listed in content/supporters.json", problems)
        self.assertEqual(len([p for p in problems if "cosigners" in p]), 2)
        with self.assertRaisesRegex(ContentError, "cosigners"):
            load_content(self.root, today=TODAY)
        with self.assertRaises(ContentError):
            build_site(self.root, today=TODAY)
        self.assertEqual(tree(self.root / "public"), public)  # the earlier public snapshot is intact
        cli = run("content-check", "--directory", str(self.root))
        self.assertEqual(cli.returncode, 3)
        self.assertEqual(cli.stdout.count(self.a), 2)
        # repairing one of two stale meetings does not skip the other: the gate stays whole
        before = snapshot(self.root)
        with self.assertRaisesRegex(ContentError, f"meetings/{MID2}.json.cosigners"):
            self.stage(MID, [self.b], replace=True)
        self.assertEqual(snapshot(self.root), before)
        # listing the organization again, with a fresh grant, makes both meetings valid again
        self.assertEqual(self.add(A), self.a)
        self.grant(self.a)
        self.assertEqual(content_problems(self.root), [])

    def test_repair_clears_one_meetings_ids_and_block(self):
        remove_supporter(self.root, self.b, today=TODAY)  # only MID names it
        self.assertEqual([p for p in content_problems(self.root) if "cosigners" in p],
                         [f"meetings/{MID}.json.cosigners: {self.b}: is not listed in content/supporters.json"])
        with self.assertRaises(ContentError):
            self.stage(MID, [self.b], replace=True)  # a removed id cannot be named again
        with self.assertRaises(ContentError):
            self.stage(MID2, [self.a], replace=True)  # the repair path skips only the exact meeting being replaced
        self.stage(MID, [self.a], replace=True)  # resolves only the current flags
        self.assertEqual(self.document()["cosigners"], [self.a])
        self.assertEqual(content_problems(self.root), [])
        self.stage(MID, replace=True)  # no --cosign: old ids and block are gone, never inherited
        self.assertNotIn("cosigners", self.document())
        self.assertNotIn(COSIGN_HEADING, self.document()["comment_kit_md"])
        self.assertEqual(content_problems(self.root), [])
        build_site(self.root, today=TODAY)

    def test_stale_grant_and_block_fail_closed(self):
        tracked = f"meetings/{MID}.json.cosigners"
        self.grant(self.b, MID2)  # narrowed: no longer covers MID
        self.assertEqual([p for p in content_problems(self.root) if tracked in p],
                         [f"{tracked}: {self.b}: has not consented to co-sign this meeting"])
        self.grant(self.b)
        self.assertEqual(content_problems(self.root), [])
        set_cosign_consent(self.root, self.b, clear=True, today=TODAY)  # revoked
        self.assertEqual([p for p in content_problems(self.root) if tracked in p],
                         [f"{tracked}: {self.b}: has not consented to co-sign"])
        self.grant(self.b)
        # an edited public row no longer matches its consent record
        rows = json.loads(self.public_file.read_text(encoding="utf-8"))
        rows["supporters"][1]["name"] = "Birch Hollow Coalition Renamed"
        self.public_file.write_text(json.dumps(rows), encoding="utf-8")
        self.assertTrue(any(tracked in p for p in content_problems(self.root)))
        rows["supporters"][1]["name"] = B
        self.public_file.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        self.assertEqual(content_problems(self.root), [])

    def test_missing_wrong_and_duplicate_blocks_fail(self):
        label = f"meetings/{MID}.json.cosigners"
        original = self.document()["comment_kit_md"]
        block = cosign_block_md([A, B])
        cases = {
            "missing": original.replace("\n\n" + block, ""),
            "wrong order": original.replace(block, cosign_block_md([B, A])),
            "duplicate": original.replace(block, block + "\n\n" + block),
            "outside the letter": original.replace("\n\n" + block, "").replace("## Sources\n", "## Sources\n" + block + "\n"),
            "extra name": original.replace(block, block + "\n- " + C),
        }
        for what, kit in cases.items():
            with self.subTest(what):
                self.assertNotEqual(kit, original)
                self.edit(MID, lambda d: d.update(comment_kit_md=kit))
                self.assertTrue(any(p.startswith(label) and "exactly one current" in p for p in content_problems(self.root)))
                with self.assertRaises(ContentError):
                    load_content(self.root, today=TODAY)
        self.edit(MID, lambda d: d.update(comment_kit_md=original))
        # a copied roster without the metadata, and a lost written comment, are both caught; names are never scraped
        self.edit(MID, lambda d: d.pop("cosigners"))
        self.assertIn(f"meetings/{MID}.json: the comment kit has a co-signed block but the meeting lists no cosigners",
                      content_problems(self.root))
        self.edit(MID, lambda d: d.update(cosigners=[self.a, self.b]))
        self.edit(MID, lambda d: d["participation"].update(written_comment=None))
        self.assertTrue(any("no verified written comment" in p for p in content_problems(self.root)))
        self.edit(MID, lambda d: d.pop("participation"))
        self.assertTrue(any("no verified written comment" in p for p in content_problems(self.root)))

    def test_checks_are_read_only_and_restaging_revalidates(self):
        self.edit(MID, lambda d: d.update(cosigners=[self.a, self.b, "ghost-org"]))
        before = snapshot(self.root)
        os.chmod(self.private_file, 0o600)
        problems = content_problems(self.root)
        self.assertTrue(any("ghost-org" in p for p in problems))
        with self.assertRaises(ContentError):
            load_content(self.root, today=TODAY)
        self.assertEqual(snapshot(self.root), before)

    def test_malformed_private_store_reports_safely(self):
        good = self.private_file.read_bytes()
        self.private_file.write_text('{"schema_version": 1, "supporters": {"x": SECRET-SENTINEL}', encoding="utf-8")
        before = snapshot(self.root)
        text = "\n".join(content_problems(self.root))
        self.assertNotIn("SECRET-SENTINEL", text)
        self.assertIn("kit/supporters-consent.json", text)
        self.assertIn(f"meetings/{MID}.json.cosigners: {self.a}: has no consent record matching the listed row", text)
        self.assertEqual(snapshot(self.root), before)  # nothing mutated or chmodded
        self.private_file.write_bytes(good)

    def test_restaging_a_meeting_revalidates_its_ids(self):
        self.assertEqual(self.stage(MID, [self.b, self.a], replace=True)["cosigners"], [self.b, self.a])
        self.assertEqual(content_problems(self.root), [])
        set_cosign_consent(self.root, self.b, clear=True, today=TODAY)
        with self.assertRaises(ContentError):
            self.stage(MID, [self.b, self.a], replace=True)
        self.assertEqual(self.document()["cosigners"], [self.b, self.a])  # the refused attempt wrote nothing


class OutputTests(Base):
    def test_count_labels_and_private_leak_scan(self):
        a, b = self.add(A), self.add(B)
        self.ledger(DIGEST)
        self.grant(a, correspondence="ab" * 6)
        self.grant(b, MID)
        reports = [json.dumps(self.stage(MID, [a, b])), json.dumps(self.stage(MID2, [a]))]
        build_site(self.root, today=TODAY)
        public = self.root / "public"
        many = (public / "meetings" / f"{MID}.html").read_text(encoding="utf-8")
        one = (public / "meetings" / f"{MID2}.html").read_text(encoding="utf-8")
        self.assertIn('<a href="/supporters.html">Co-signed by 2 organizations</a>', many)
        self.assertIn('<a href="/supporters.html">Co-signed by 1 organization</a>', one)
        self.assertTrue((public / "supporters.html").is_file())  # the link target exists
        other = (public / "meetings.html").read_text(encoding="utf-8")
        self.assertNotIn("Co-signed by 2", other)
        everything = {path: data.decode("utf-8", "replace") for path, data in tree(public).items()}
        for secret in (DIGEST, GRANT_DATE, '"cosign"', "cosign_confirmed_on", "cosign_how"):
            self.assertEqual([p for p, text in everything.items() if secret in text], [], secret)
            self.assertFalse(any(secret in r for r in reports), secret)
        self.assertEqual([p for p, text in everything.items() if p.endswith(".json") and "cosigners" in text], [])
        # the label overrides reach the page with the count placeholder
        (self.root / "content" / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": {
            "meeting.cosigned_one": "ZQ-ONE {count}", "meeting.cosigned_many": "ZQ-MANY {count}"}}), encoding="utf-8")
        build_site(self.root, today=TODAY)
        self.assertIn('<a href="/supporters.html">ZQ-MANY 2</a>', (public / "meetings" / f"{MID}.html").read_text(encoding="utf-8"))
        self.assertIn('<a href="/supporters.html">ZQ-ONE 1</a>', (public / "meetings" / f"{MID2}.html").read_text(encoding="utf-8"))

    def test_meetings_without_cosign_render_no_line(self):
        self.add(A)
        self.stage()
        build_site(self.root, today=TODAY)
        page = (self.root / "public" / "meetings" / f"{MID}.html").read_text(encoding="utf-8")
        self.assertNotIn("Co-signed", page)
        self.assertNotIn("cosigned", page)
        self.assertNotIn("cosigners", self.document())

    def test_only_selected_ids_are_stored(self):
        a = self.add(A)
        self.grant(a)
        self.stage(ids=[a])
        stored = self.document()
        self.assertEqual(sorted(stored), sorted({"id", "body", "starts_at", "agenda_item", "agenda_url", "comment_kit_md",
                                                 "participation", "cited_findings", "cosigners"}))
        self.assertEqual(stored["cosigners"], [a])


if __name__ == "__main__":
    unittest.main()
