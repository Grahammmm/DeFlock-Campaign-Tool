"""DF2207-15: private speaker coverage plan for a staged meeting. Synthetic only, no network.

Run from the repository root: python3 -B -m unittest tests.test_meeting_speakers -v
"""
import contextlib
import hashlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from campaign_tool import cli, meeting_speakers as ms  # noqa: E402

NOW = datetime(2026, 12, 5, 12, 0, tzinfo=timezone.utc)
START = "2026-12-10T18:00:00-08:00"
KIT = ("## Public comment: Synthetic Council, 2026-12-10\n\n## Two-minute comment\nHello.\n\n## Two asks\n"
       "- Pause the contract\n- Pause the contract\n\n## How to take part\nCheck.\n\n## Sources\n"
       "- **Finding one** /findings/one.html\n- Source record, p1, sha256 abc\n")
PART = {"in_person": True, "remote": None, "written_comment": None, "speaker_minutes": 3,
        "source_url": "https://agenda.synthetic.example/rules", "verified_by": "Pat Organizer", "verified_at": "2026-12-01"}


def run(*arguments):
    out, err, code = io.StringIO(), io.StringIO(), 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-speakers-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.root = self.tmp / "campaign"
        (self.root / "content" / "meetings").mkdir(parents=True)
        self.stage()

    def stage(self, ident="m-a", kit=KIT, **extra):
        document = {"id": ident, "body": "Synthetic Council", "starts_at": START, "comment_kit_md": kit,
                    "cited_findings": ["f_one"], **extra}
        path = self.root / "content" / "meetings" / f"{ident}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def add(self, label, point, ident="m-a", now=NOW):
        return ms.update_plan(self.root, ident, add=label, point=point, now=now)

    def store(self, ident="m-a"):
        return self.root / "kit" / "meetings" / "speakers" / f"{ident}.json"

    def refused(self, message, call, *args, **kwargs):
        with self.assertRaises(ms.MeetingSpeakersError) as caught:
            call(self.root, *args, **kwargs)
        self.assertEqual(str(caught.exception), message)

    def tree(self):
        return {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(self.root.rglob("*")) if p.is_file()}


class CliTests(Base):
    def args(self, *more):
        return ("meetings-speakers", "--directory", self.root, "--id", "m-a", *more)

    def test_cli_actions_and_label_contract(self):
        code, out, _ = run("meetings-speakers", "--help")
        self.assertEqual(code, 0)
        self.assertIn("--point", out)
        for bad in ((), ("--list", "--remove", "Ann"), ("--add", "Ann"), ("--list", "--point", "ask:1"),
                    ("--remove", "Ann", "--point", "ask:1")):
            self.assertEqual(run(*self.args(*bad))[0], 2, bad)
        future = "2099-01-01T10:00:00-08:00"
        self.stage(starts_at=future)
        code, out, err = run(*self.args("--add", "  Ann   Lee ", "--point", "ask:1"))
        self.assertEqual((code, json.loads(out), err.strip()),
                         (0, {"id": "m-a", "changed": True, "assignments": 1}, "Recorded only. Nothing was sent."))
        self.assertEqual(run(*self.args("--add", "A" * 40, "--point", "finding:f_one"))[0], 0)
        self.assertEqual(run(*self.args("--add", "B", "--point", "ask:2"))[0], 0)
        for label in ("A" * 41, "  ", "ann@example.org", "<b>x</b>", "call 555-123-4567", "x\u0007y",
                      "\uff11\uff12\uff13\uff14\uff15\uff16\uff17"):
            code, out, err = run(*self.args("--add", label, "--point", "ask:1"))
            self.assertEqual(code, 1, label)
            self.assertTrue(err.startswith("Stopped: --add"))
            self.assertNotIn(label.strip() or "\0", err)
        code, out, _ = run(*self.args("--list"))
        listing = json.loads(out)
        self.assertEqual(listing["multiple_points"], [])
        self.assertEqual(listing["speaker_minutes"], None)
        self.assertEqual(run(*self.args("--remove", "Nobody"))[1].strip(), '{"id": "m-a", "changed": false, "assignments": 3}')
        self.assertEqual(json.loads(run(*self.args("--remove", " Ann  Lee"))[1])["assignments"], 2)

    def test_cli_refuses_root_and_ancestor_symlinks(self):
        self.stage(starts_at="2099-01-01T10:00:00-08:00")
        real = self.tmp / "realroot"
        self.root.rename(real)
        self.root.symlink_to(real)
        for action in (("--add", "Ann", "--point", "ask:1"), ("--list",), ("--remove", "Ann")):
            code, out, err = run(*self.args(*action))
            self.assertEqual((code, err.strip()), (1, "Stopped: " + ms.SYMLINK), action)
        self.assertFalse((real / "kit").exists())
        self.root.unlink()
        parent = self.tmp / "parent"
        parent.mkdir()
        real.rename(parent / "campaign")
        link = self.tmp / "linkparent"
        link.symlink_to(parent)
        code, out, err = run("meetings-speakers", "--directory", link / "campaign", "--id", "m-a",
                             "--add", "Ann", "--point", "ask:1")
        self.assertEqual((code, err.strip()), (1, "Stopped: " + ms.SYMLINK))
        self.assertFalse((parent / "campaign" / "kit").exists())

    def test_cli_exact_domain_refusals(self):
        self.stage(starts_at="2000-01-01T10:00:00-08:00")
        code, out, err = run(*self.args("--list"))
        self.assertEqual((code, err.strip()), (1, "Stopped: " + ms.PAST))
        code, out, err = run("meetings-speakers", "--directory", self.root, "--id", "nope", "--list")
        self.assertEqual((code, err.strip()), (1, "Stopped: " + ms.NOT_STAGED))


class PlanTests(Base):
    def test_staged_points_use_snapshot(self):
        points = ms.meeting_points(self.root, "m-a", now=NOW)
        self.assertEqual([p["point"] for p in points], ["finding:f_one", "ask:1", "ask:2"])
        self.assertEqual([p["text"] for p in points], ["f_one", "Pause the contract", "Pause the contract"])
        one = KIT.replace("## Two asks\n- Pause the contract\n- Pause the contract", "## One ask\n- Only ask")
        self.stage("m-b", one, cited_findings=["f_a", "f_b", "f_c"])
        self.assertEqual([p["point"] for p in ms.meeting_points(self.root, "m-b", now=NOW)],
                         ["finding:f_a", "finding:f_b", "finding:f_c", "ask:1"])
        zero = one.replace("- **Finding one** /findings/one.html\n", "- No published findings yet.\n")
        path = self.stage("m-c", zero)
        document = json.loads(path.read_text())
        del document["cited_findings"]
        path.write_text(json.dumps(document))
        self.assertEqual([p["point"] for p in ms.meeting_points(self.root, "m-c", now=NOW)], ["ask:1"])
        three = KIT.replace("## Two asks\n- Pause the contract\n- Pause the contract",
                            "## Three asks\n\n- A\n- B\n- C")
        self.stage("m-d", three)
        self.assertEqual(len(ms.meeting_points(self.root, "m-d", now=NOW)), 4)
        legacy = self.stage("m-e")  # cites findings in Sources but never stored which
        document = json.loads(legacy.read_text())
        del document["cited_findings"]
        legacy.write_text(json.dumps(document))
        self.refused(ms.UNAVAILABLE, ms.meeting_points, "m-e", now=NOW)
        for name, bad in (("m-f", KIT.replace("## Two asks", "## Two asks\n- Pause the contract\n## Two asks")),
                          ("m-g", KIT.replace("## Two asks", "## Three asks")),
                          ("m-h", KIT.replace("## Two asks", "## Elsewhere")),
                          ("m-i", KIT.replace("- Pause the contract\n- Pause", "Pause the contract\n- Pause")),
                          ("m-j", KIT.replace("- Pause the contract\n- Pause the contract", "- Only one"))):
            self.stage(name, bad)
            self.refused(ms.UNAVAILABLE, ms.meeting_points, name, now=NOW)

    def test_pair_add_remove_and_coverage(self):
        before = self.tree()
        listing = ms.list_plan(self.root, "m-a", now=NOW)
        self.assertEqual(listing["uncovered"], ["finding:f_one", "ask:1", "ask:2"])
        self.assertEqual(self.tree(), before)
        self.assertFalse((self.root / "kit").exists())
        self.assertTrue(self.add("Zed", "ask:2")["changed"])
        self.assertTrue(self.add("Ann", "ask:1")["changed"])
        self.assertTrue(self.add("Bo", "ask:1")["changed"])
        self.assertTrue(self.add("Ann", "finding:f_one")["changed"])
        self.assertFalse(self.add("Ann", "ask:1")["changed"])
        listing = ms.list_plan(self.root, "m-a", now=NOW)
        self.assertEqual([p["speakers"] for p in listing["points"]], [["Ann"], ["Ann", "Bo"], ["Zed"]])
        self.assertEqual(listing["uncovered"], [])
        self.assertEqual(listing["multiple_points"], [{"label": "Ann", "points": ["finding:f_one", "ask:1"]}])
        stored = json.loads(self.store().read_text())
        self.assertEqual(list(stored), ["schema_version", "meeting_id", "kit_sha256", "assignments"])
        self.assertEqual(stored["kit_sha256"], hashlib.sha256(KIT.encode()).hexdigest())
        self.assertEqual([(a["label"], a["point"]) for a in stored["assignments"]],
                         [("Ann", "ask:1"), ("Ann", "finding:f_one"), ("Bo", "ask:1"), ("Zed", "ask:2")])
        self.assertEqual(ms.update_plan(self.root, "m-a", remove="Ann", now=NOW), {"id": "m-a", "changed": True, "assignments": 2})
        self.assertFalse(ms.update_plan(self.root, "m-a", remove="Ann", now=NOW)["changed"])
        self.assertEqual(ms.list_plan(self.root, "m-a", now=NOW)["uncovered"], ["finding:f_one"])
        for point in ("ask:3", "ask:01", "finding:nope", "finding:", "ask:1 ", "slug"):
            self.refused(ms.UNKNOWN_POINT, ms.update_plan, "m-a", add="Q", point=point, now=NOW)

    def test_pair_limit(self):
        self.stage(cited_findings=[f"f{n}" for n in range(3)])
        for n in range(MAX := ms.MAX_PAIRS):
            self.add(f"v{n:03d}", "ask:1")
        self.assertEqual(len(json.loads(self.store().read_text())["assignments"]), MAX)
        self.refused(f"speaker plan is full ({MAX} pairs)", ms.update_plan, "m-a", add="extra", point="ask:2", now=NOW)
        self.assertFalse(self.add("v000", "ask:1")["changed"])  # an exact repeat is still a no-op at the cap

    def test_verified_limit_and_time_gates(self):
        self.assertIsNone(ms.list_plan(self.root, "m-a", now=NOW)["speaker_minutes"])
        for minutes in (1, 10):
            self.stage(participation={**PART, "speaker_minutes": minutes})
            self.assertEqual(ms.list_plan(self.root, "m-a", now=NOW)["speaker_minutes"], minutes)
        self.stage(participation={**PART, "speaker_minutes": None})
        self.assertIsNone(ms.list_plan(self.root, "m-a", now=NOW)["speaker_minutes"])
        self.stage(participation={**PART, "speaker_minutes": 11})
        with self.assertRaises(ValueError):
            ms.list_plan(self.root, "m-a", now=NOW)
        self.stage()
        start = datetime.fromisoformat(START)
        for now in (start, start + timedelta(hours=1), start + timedelta(days=1)):
            for call in (lambda: ms.list_plan(self.root, "m-a", now=now), lambda: self.add("A", "ask:1", now=now),
                         lambda: ms.update_plan(self.root, "m-a", remove="A", now=now)):
                with self.assertRaises(ms.MeetingSpeakersError) as caught:
                    call()
                self.assertEqual(str(caught.exception), ms.PAST)
        # the instant, not the date string: 2026-12-11 02:00 UTC is 2026-12-10 18:00 -08:00 exactly
        self.assertEqual(ms.list_plan(self.root, "m-a", now=datetime(2026, 12, 11, 1, 59, tzinfo=timezone.utc))["id"], "m-a")
        self.refused(ms.PAST, ms.list_plan, "m-a", now=datetime(2026, 12, 11, 2, 0, tzinfo=timezone.utc))
        self.assertEqual(ms.list_plan(self.root, "m-a", now=datetime(2026, 12, 10, 3, 0, tzinfo=timezone.utc))["id"], "m-a")
        with self.assertRaises(ms.MeetingSpeakersError):
            ms.list_plan(self.root, "m-a", now=datetime(2026, 12, 5, 12, 0))
        self.stage(status="cancelled")
        self.refused(ms.CANCELLED, ms.list_plan, "m-a", now=NOW)
        self.refused(ms.CANCELLED, ms.update_plan, "m-a", remove="A", now=NOW)
        self.stage()
        imported = self.root / "kit" / "meetings.json"
        imported.parent.mkdir()
        imported.write_text(json.dumps({"schema_version": 1, "meetings": [{"id": "m-a", "status": "cancelled"}]}))
        self.refused(ms.CANCELLED, ms.list_plan, "m-a", now=NOW)
        imported.write_text(json.dumps({"schema_version": 1, "meetings": [{"id": "other", "status": "cancelled"}]}))
        self.assertEqual(ms.list_plan(self.root, "m-a", now=NOW)["id"], "m-a")
        for ident in ("../m-a", "M-A", "m/a", ".", "..", ""):
            self.refused(ms.NOT_STAGED, ms.list_plan, ident, now=NOW)
        self.refused(ms.NOT_STAGED, ms.list_plan, "m-zzz", now=NOW)
        path = self.stage()
        document = json.loads(path.read_text())
        document["id"] = "m-other"
        path.write_text(json.dumps(document))
        with self.assertRaises(ms.MeetingSpeakersError):
            ms.list_plan(self.root, "m-a", now=NOW)

    def test_add_refuses_unknown_existing_reference_in_current_store(self):
        self.add("Ann", "ask:1")
        document = json.loads(self.store().read_text())
        document["assignments"].append({"label": "Bo", "point": "ask:3"})
        self.store().write_text(json.dumps(document), encoding="utf-8")
        before = self.store().read_bytes()
        self.refused(ms.STALE, ms.list_plan, "m-a", now=NOW)
        self.refused(ms.STALE, ms.update_plan, "m-a", add="Cy", point="ask:2", now=NOW)
        self.refused(ms.STALE, ms.update_plan, "m-a", add="Ann", point="ask:1", now=NOW)
        self.assertEqual(self.store().read_bytes(), before)
        self.assertEqual(ms.update_plan(self.root, "m-a", remove="Bo", now=NOW)["assignments"], 1)

    def test_stale_snapshot_requires_explicit_cleanup(self):
        self.add("Ann", "ask:1")
        self.add("Bo", "ask:2")
        old = json.loads(self.store().read_text())["kit_sha256"]
        self.stage(kit=KIT.replace("- Pause the contract\n- Pause the contract", "- New first\n- Pause the contract"))
        self.refused(ms.STALE, ms.list_plan, "m-a", now=NOW)
        self.refused(ms.STALE, ms.update_plan, "m-a", add="Cy", point="ask:1", now=NOW)
        self.assertEqual(ms.update_plan(self.root, "m-a", remove="Ann", now=NOW)["assignments"], 1)
        self.assertEqual(json.loads(self.store().read_text())["kit_sha256"], old)  # still the old binding
        self.refused(ms.STALE, ms.list_plan, "m-a", now=NOW)
        self.assertEqual(ms.update_plan(self.root, "m-a", remove="Bo", now=NOW)["assignments"], 0)
        self.assertEqual(ms.list_plan(self.root, "m-a", now=NOW)["uncovered"], ["finding:f_one", "ask:1", "ask:2"])
        self.add("Cy", "ask:1")
        self.assertNotEqual(json.loads(self.store().read_text())["kit_sha256"], old)
        # an out-of-range reference left behind by a re-stage with fewer asks is still removable
        self.add("Dee", "ask:2")
        self.stage(kit=KIT.replace("## Two asks\n- Pause the contract\n- Pause the contract", "## One ask\n- Pause"))
        self.refused(ms.STALE, ms.list_plan, "m-a", now=NOW)
        self.assertEqual(ms.update_plan(self.root, "m-a", remove="Dee", now=NOW)["assignments"], 1)


class PathTests(Base):
    def test_private_atomic_paths_and_bad_store(self):
        os.umask(0o000)
        self.addCleanup(os.umask, 0o077)
        self.add("Ann", "ask:1")
        self.assertEqual(stat.S_IMODE(self.store().stat().st_mode), 0o600)
        for folder in ("meetings", "speakers"):
            self.assertEqual(stat.S_IMODE((self.root / "kit" / ("meetings/speakers" if folder == "speakers" else folder)).stat().st_mode), 0o700)
        before = self.store().read_bytes(), self.store().stat().st_mtime_ns
        self.add("Ann", "ask:1")
        ms.update_plan(self.root, "m-a", remove="Nobody", now=NOW)
        ms.list_plan(self.root, "m-a", now=NOW)
        self.assertEqual((self.store().read_bytes(), self.store().stat().st_mtime_ns), before)
        with mock.patch("os.replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.add("Bo", "ask:2")
        self.assertEqual(self.store().read_bytes(), before[0])
        self.assertEqual([p.name for p in self.store().parent.iterdir()], ["m-a.json"])
        good = json.loads(before[0])
        digest = good["kit_sha256"]
        bad_documents = [
            b"\xff\xfe", b"[]", b"{", json.dumps({**good, "extra": 1}).encode(),
            json.dumps({**good, "schema_version": True}).encode(), json.dumps({**good, "meeting_id": "m-b"}).encode(),
            json.dumps({**good, "kit_sha256": digest.upper()}).encode(), json.dumps({**good, "assignments": "x"}).encode(),
            json.dumps({**good, "assignments": [{"label": "Ann", "point": "ask:9"}]}).encode(),
            json.dumps({**good, "assignments": [{"label": " Ann", "point": "ask:1"}]}).encode(),
            json.dumps({**good, "assignments": [{"label": "a@b.c", "point": "ask:1"}]}).encode(),
            json.dumps({**good, "assignments": [{"label": "Ann", "point": "ask:1", "x": 1}]}).encode(),
            json.dumps({**good, "assignments": [{"label": "Ann", "point": "ask:1"}] * 2}).encode(),
            b'{"schema_version": 1, "schema_version": 1, "meeting_id": "m-a", "kit_sha256": "%s", "assignments": []}' % digest.encode(),
            b" " * (1 << 20 + 1) + b"{}",
        ]
        for data in bad_documents:
            self.store().write_bytes(data)
            self.refused(ms.BAD_STORE, ms.list_plan, "m-a", now=NOW)
            self.refused(ms.BAD_STORE, ms.update_plan, "m-a", add="Q", point="ask:1", now=NOW)
        self.store().unlink()
        self.store().mkdir()
        self.refused(ms.BAD_STORE, ms.list_plan, "m-a", now=NOW)
        self.store().rmdir()
        os.mkfifo(self.store())
        self.refused(ms.BAD_STORE, ms.list_plan, "m-a", now=NOW)
        self.store().unlink()
        self.store().write_bytes(before[0])
        os.link(self.store(), self.tmp / "other-link")
        self.refused(ms.BAD_STORE, ms.list_plan, "m-a", now=NOW)
        self.refused(ms.BAD_STORE, ms.update_plan, "m-a", add="Q", point="ask:1", now=NOW)

    def test_symlinks_refused_everywhere(self):
        self.add("Ann", "ask:1")
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "m-a.json").write_text("{}")
        for rel in ("kit/meetings/speakers/m-a.json", "kit/meetings/speakers", "kit/meetings", "kit"):
            link = self.root / rel
            moved = link.with_name(link.name + "-real")
            link.rename(moved)
            link.symlink_to(moved if moved.is_dir() else outside / "m-a.json")
            for call in (lambda: ms.list_plan(self.root, "m-a", now=NOW), lambda: self.add("Ann", "ask:1"),
                         lambda: ms.update_plan(self.root, "m-a", remove="Ann", now=NOW)):
                with self.assertRaises(ms.MeetingSpeakersError, msg=rel) as caught:
                    call()
                self.assertEqual(str(caught.exception), ms.SYMLINK, rel)
            link.unlink()
            moved.rename(link)
        dangling = self.root / "kit" / "meetings.json"
        dangling.symlink_to(self.tmp / "missing")
        self.refused(ms.SYMLINK, ms.list_plan, "m-a", now=NOW)
        dangling.unlink()
        for rel in ("content/meetings/m-a.json", "content/meetings", "content"):
            link = self.root / rel
            moved = link.with_name(link.name + "-real")
            link.rename(moved)
            link.symlink_to(moved)
            self.refused(ms.SYMLINK, ms.list_plan, "m-a", now=NOW)
            link.unlink()
            moved.rename(link)
        real = self.tmp / "realroot"
        self.root.rename(real)
        self.root.symlink_to(real)
        self.refused(ms.SYMLINK, ms.list_plan, "m-a", now=NOW)
        self.assertEqual(json.loads((outside / "m-a.json").read_text()), {})

    def test_no_public_or_other_private_leak(self):
        for name in ("assignments.json", "comments-sent.json"):
            (self.root / "kit").mkdir(exist_ok=True)
            (self.root / "kit" / name).write_text('{"keep": 1}')
        (self.root / "public").mkdir()
        before = self.tree()
        self.add("Ann Aardvark", "ask:1")
        after = self.tree()
        changed = {k for k in after if before.get(k) != after[k]}
        self.assertEqual(changed, {"kit/meetings/speakers/m-a.json"})
        self.assertFalse(any("Aardvark" in p.read_text(errors="ignore") for p in self.root.rglob("*")
                             if p.is_file() and "speakers" not in p.parts))
        self.assertEqual(list((self.root / "public").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
