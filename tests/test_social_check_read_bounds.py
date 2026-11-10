"""RF0725-3: the additional-folder social checker reads each draft (1 MiB) and private sidecar (2 MiB) through a
descriptor-verified, byte-limited reader (synthetic only). The legacy top-level ``social.check_social`` reader is out of scope.

Run from the repository root: python3 -B -m unittest tests.test_social_check_read_bounds -q
"""
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from campaign_tool import social as S
from campaign_tool import social_check as SC
from campaign_tool import today
from campaign_tool.social import build_meeting_social, build_social
from tests import test_social_check_folders as F

CANARY = "PRIVATE-CANARY-9f3c"
CAP = 4000  # small patched cap for the matrix; the real caps are exercised separately


def pad_json(path, size, ending=b" "):
    """Make the JSON file at ``path`` exactly ``size`` bytes by appending legal trailing whitespace."""
    data = Path(path).read_bytes().rstrip()
    assert len(data) <= size
    Path(path).write_bytes(data + ending * (size - len(data)))


def pad_draft(path, size):
    """Valid edit of the draft whose byte size is exactly ``size`` (multibyte padding, so bytes != code points)."""
    data = Path(path).read_bytes()
    room = size - len(data) - 1
    Path(path).write_bytes(data + b"\n" + "\u00e9".encode() * (room // 2) + b"x" * (room % 2))
    assert Path(path).stat().st_size == size


class Recorder:
    """Records every os.read request/return and every os.open/os.close on this module's descriptors."""

    def __init__(self, test, chunk=None, after_fstat=None):
        self.requests, self.returned, self.opened, self.closed = [], [], [], []
        self.paths, self.read_paths, self.fd_path = [], set(), {}
        self.chunk, self.after_fstat = chunk, after_fstat
        real_read, real_open, real_close, real_fstat = os.read, os.open, os.close, os.fstat

        def read(fd, n):
            self.requests.append(n)
            self.read_paths.add(self.fd_path.get(fd))
            data = real_read(fd, n if self.chunk is None else min(n, self.chunk))
            self.returned.append(len(data))
            return data

        def opener(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            name = os.path.basename(str(path))  # only this module's drafts and sidecars; the legacy root reader is out of scope
            if (name.endswith(".md") or name in (".written.json", ".check.json")) and os.path.basename(os.path.dirname(str(path))) != "social":
                self.opened.append(fd)
                self.fd_path[fd] = str(path)
            return fd

        def closer(fd):
            if fd in self.fd_path:
                self.closed.append(fd)
                del self.fd_path[fd]
            return real_close(fd)

        def fstat(fd):
            info = real_fstat(fd)
            if self.after_fstat and fd in self.fd_path and self.fd_path[fd].endswith(self.after_fstat[0]):
                hook, self.after_fstat = self.after_fstat[1], None
                hook()
            return info

        for name, fake in (("read", read), ("open", opener), ("close", closer), ("fstat", fstat)):
            patcher = mock.patch.object(SC.os, name, fake)
            patcher.start()
            test.addCleanup(patcher.stop)

    @property
    def total(self):
        return sum(self.returned)


class ReadBoundsCase(F.FolderCheckCase):
    def meetings_built(self, platforms=None):
        self.meeting()
        build_social(self.root)
        build_meeting_social(self.root, platforms, now=F.NOW)

    def block(self, rel):
        return self.folder(rel)

    def cap(self, draft=CAP, state=CAP):
        for name, value in (("MAX_DRAFT_BYTES", draft), ("MAX_STATE_BYTES", state)):
            patcher = mock.patch.object(SC, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class DraftLimit(ReadBoundsCase):
    def test_markdown_limit_is_in_bytes_for_primary_and_suggested(self):
        self.cap(draft=CAP)
        build_social(self.root, ["x"])
        primary = self.social / "x" / f"{F.SLUG}.md"
        suggested_dir = self.social / "x.suggested"
        suggested_dir.mkdir()
        suggested = suggested_dir / f"{F.SLUG}.md"
        suggested.write_bytes(primary.read_bytes())
        expect = {"x": ("edited", None), "x.suggested": ("outdated", ["outdated"])}
        for path, rel in ((primary, "x"), (suggested, "x.suggested")):
            for size in (CAP - 1, CAP):
                pad_draft(path, size)
                row = self.folder(rel)["drafts"][0]
                self.assertEqual((row["slug"], row["state"]), (F.SLUG, expect[rel][0]), (rel, size))
            pad_draft(path, CAP + 1)  # one byte over: refused, never truncated into edited/outdated
            row = self.folder(rel)["drafts"][0]
            self.assertEqual((row["state"], row["problems"]), ("not_regular", ["not_regular"]), rel)

    def test_real_one_mib_boundary(self):
        build_social(self.root, ["x"])
        draft = self.social / "x" / f"{F.SLUG}.md"
        pad_draft(draft, SC.MAX_DRAFT_BYTES)
        self.assertEqual(SC.MAX_DRAFT_BYTES, 1024 * 1024)
        self.assertEqual(self.states("x"), {F.SLUG: ("edited", ["over_cap"])})
        pad_draft(draft, SC.MAX_DRAFT_BYTES + 1)
        self.assertEqual(self.states("x"), {F.SLUG: ("not_regular", ["not_regular"])})
        self.assertEqual(self.folder("x")["problems"], 1)

    def test_oversized_draft_keeps_stale_first_and_counts_once(self):
        self.cap()
        self.meetings_built()
        draft = self.social / "meetings" / "m-one.md"
        pad_draft(draft, CAP + 1)
        self.assertEqual(self.states("meetings"), {"m-one": ("not_regular", ["not_regular"])})
        self.meeting(facts=None)  # participation no longer verified: stale, not an ordinary problem
        block = self.folder("meetings")
        self.assertEqual((block["drafts"], block["stale"], block["problems"]), ([], ["m-one"], 1))


class StateLimit(ReadBoundsCase):
    def test_padded_check_json_at_and_over_cap(self):
        self.cap()
        self.meetings_built()
        sidecar = self.social / "meetings" / ".check.json"
        pad_json(sidecar, CAP)
        block = self.folder("meetings")
        self.assertEqual((block["problems"], "folder_problems" in block), (0, False))
        pad_json(sidecar, CAP + 1)
        block = self.folder("meetings")
        self.assertEqual(block["folder_problems"], [".check.json is not a usable regular file; its contents are ignored"])
        self.assertEqual((block["problems"], [(e["slug"], e["state"]) for e in block["drafts"]]), (1, [("m-one", "ok")]))

    def test_padded_written_json_at_and_over_cap(self):
        self.cap()
        build_social(self.root, ["x"])
        sidecar = self.social / "x" / ".written.json"
        pad_json(sidecar, CAP, b"\n")
        self.assertEqual(self.folder("x")["problems"], 0)
        pad_json(sidecar, CAP + 1, b"\n")
        block = self.folder("x")
        self.assertEqual(block["folder_problems"], [".written.json is not a usable regular file; its contents are ignored"])
        self.assertEqual(block["problems"], 1)  # a clean draft does not hide the sidecar problem

    def test_real_two_mib_boundary(self):
        self.meetings_built()
        written = self.social / "meetings" / ".written.json"
        sidecar = self.social / "meetings" / ".check.json"
        pad_json(sidecar, SC.MAX_STATE_BYTES)
        pad_json(written, SC.MAX_STATE_BYTES)
        self.assertEqual(SC.MAX_STATE_BYTES, 2 * 1024 * 1024)
        self.assertEqual(self.folder("meetings")["problems"], 0)
        pad_json(sidecar, SC.MAX_STATE_BYTES + 1)
        pad_json(written, SC.MAX_STATE_BYTES + 1)
        block = self.folder("meetings")
        self.assertEqual(len(block["folder_problems"]), 2)
        self.assertEqual(block["problems"], 2)

    def test_oversized_sidecar_cannot_supply_reschedule_baseline_or_binding(self):
        self.cap()
        self.meetings_built()
        self.meeting(starts="2026-11-15T10:00:00-08:00")
        self.assertIn("rescheduled", self.folder("meetings")["stale_reasons"]["m-one"][0])  # usable sidecar: stale
        pad_json(self.social / "meetings" / ".check.json", CAP + 1)
        block = self.folder("meetings")
        self.assertEqual(block["stale"], [])  # the ignored baseline cannot make it stale
        row = block["drafts"][0]
        self.assertEqual((row["state"], row["problems"]), ("outdated", ["outdated"]))
        self.assertIn("cannot be checked; rebuild with social --meetings", row["note"])
        self.assertEqual(block["problems"], 2)

    def test_oversized_written_json_cannot_grant_outdated_or_binding(self):
        self.cap()
        build_social(self.root, ["x"])
        self.change_finding(title="A changed synthetic title")
        self.assertEqual(self.states("x"), {F.SLUG: ("outdated", ["outdated"])})  # baseline matches: outdated
        pad_json(self.social / "x" / ".written.json", CAP + 1)
        self.assertEqual(self.states("x"), {F.SLUG: ("edited", [])})  # no baseline: an unknown edit

    def test_unusable_state_shapes_stay_bad_and_absent_stays_absent(self):
        self.meetings_built()
        sidecar = self.social / "meetings" / ".check.json"
        good = sidecar.read_bytes()
        bad = {"empty": b"", "bad utf-8": b"\xff\xfe{", "malformed": b"{", "deep": b"[" * 200000,
               "wrong type": b"[]", "unknown schema": json_bytes({"schema_version": 2, "drafts": {}}),
               "wrong kind": good.replace(b'"meetings"', b'"threads"', 1),
               "bad window": good.replace(b'"within_days": 14', b'"within_days": 99', 1)}
        for label, data in bad.items():
            sidecar.write_bytes(data)
            block = self.folder("meetings")
            self.assertEqual(len(block.get("folder_problems", [])), 1, label)
        sidecar.write_bytes(good)
        self.assertNotIn("folder_problems", self.folder("meetings"))
        sidecar.unlink()
        self.assertNotIn("folder_problems", self.folder("meetings"))  # absent is not a problem

    def test_valid_custom_window_still_binds_a_suggested_draft_and_primary_baseline(self):
        self.meeting(starts="2026-11-30T09:00:00-08:00")  # 20 days out: only a 30-day window generates it
        build_social(self.root)
        build_meeting_social(self.root, within_days=30, now=F.NOW)
        self.assertEqual(self.states("meetings"), {"m-one": ("ok", [])})
        draft = self.social / "meetings" / "m-one.md"
        draft.write_text(draft.read_text(encoding="utf-8") + " (organizer note)", encoding="utf-8")
        self.meeting(starts="2026-11-30T09:00:00-08:00", agenda_item="Item 8: ALPR")
        build_meeting_social(self.root, within_days=30, now=F.NOW)  # edit kept; fresh draft goes to meetings.suggested
        self.assertEqual(self.states("meetings.suggested"), {"m-one": ("ok", [])})
        sidecar = self.social / "meetings.suggested" / ".check.json"
        self.assertEqual(json.loads(sidecar.read_text(encoding="utf-8"))["drafts"]["m-one"]["within_days"], 30)


def json_bytes(document):
    return json.dumps(document).encode("utf-8")


class ActualReadBound(ReadBoundsCase):
    def draft_path(self, size):
        path = Path(tempfile.mkdtemp(prefix="synthetic-bound-", dir=self.root)) / "d.md"
        path.write_bytes(b"a" * size)
        return path

    def test_every_request_is_explicit_positive_and_total_is_limit_plus_one_at_most(self):
        exact, over = self.draft_path(CAP), self.draft_path(CAP + 1)
        rec = Recorder(self, chunk=7)  # repeated short reads: a short read is never EOF
        self.assertEqual(SC._read_bounded(exact, CAP), b"a" * CAP)
        self.assertEqual(rec.total, CAP)
        self.assertEqual(SC._read_bounded(over, CAP), None)
        self.assertTrue(all(0 < n <= CAP + 1 for n in rec.requests))
        self.assertEqual(rec.opened, rec.closed)

    def test_preexisting_oversize_reads_zero_body_bytes(self):
        over = self.draft_path(CAP + 1)
        rec = Recorder(self)
        self.assertIsNone(SC._read_bounded(over, CAP))
        self.assertEqual((rec.requests, len(rec.opened), rec.opened == rec.closed), ([], 1, True))

    def test_growth_after_real_fstat_stops_at_limit_plus_one(self):
        for extra in (1, 10 * CAP):
            path = self.draft_path(CAP)
            rec = Recorder(self)

            def grow(path=path, extra=extra):
                with open(path, "ab") as handle:
                    handle.write(b"b" * extra)

            rec.after_fstat = ("d.md", grow)
            self.assertIsNone(SC._read_bounded(path, CAP), extra)
            self.assertEqual(rec.total, CAP + 1, extra)
            self.assertTrue(all(0 < n <= CAP + 1 for n in rec.requests), rec.requests)
            self.assertEqual(rec.opened, rec.closed)

    def test_growth_of_a_sidecar_and_a_draft_is_refused_before_trust(self):
        self.cap()
        self.meetings_built()
        sidecar = self.social / "meetings" / ".check.json"
        pad_json(sidecar, CAP)
        rec = Recorder(self)

        def grow():
            with open(sidecar, "ab") as handle:
                handle.write(b" " * 5 * CAP)

        rec.after_fstat = (".check.json", grow)
        block = self.folder("meetings")
        self.assertEqual(len(block["folder_problems"]), 1)
        self.assertTrue(all(0 < n <= CAP + 1 for n in rec.requests))
        self.assertEqual(rec.opened, rec.closed)

    def test_growth_of_a_draft_during_a_check_is_refused_before_trust(self):
        self.cap()
        build_social(self.root, ["x"])
        draft = self.social / "x" / f"{F.SLUG}.md"
        pad_draft(draft, CAP)
        rec = Recorder(self)

        def grow():
            with open(draft, "ab") as handle:
                handle.write(b"y" * 5 * CAP)

        rec.after_fstat = (".md", grow)
        self.assertEqual(self.states("x"), {F.SLUG: ("not_regular", ["not_regular"])})
        self.assertTrue(all(0 < n <= CAP + 1 for n in rec.requests))
        self.assertEqual(rec.opened, rec.closed)


class RosterAndSeams(ReadBoundsCase):
    def check_direct(self):
        findings, base_url = S._published(self.root)
        with mock.patch.object(S, "_read_regular", side_effect=AssertionError("unbounded reader")), \
                mock.patch.object(S, "_read_state", side_effect=AssertionError("unbounded reader")):
            return SC.check_folders(self.root, findings, base_url, F.NOW)

    def test_every_variant_folder_uses_the_bounded_reader_and_oversize_is_refused(self):
        self.cap()
        self.meetings_built()
        build_meeting_social(self.root, ["x"], now=F.NOW)
        build_social(self.root, ["x"])
        for rel in ("meetings", "meetings/x"):  # keep an edit in each so the rebuild writes suggestions
            draft = self.social / rel / "m-one.md"
            draft.write_text(draft.read_text(encoding="utf-8") + " note", encoding="utf-8")
        self.meeting(starts="2026-11-15T10:00:00-08:00")
        build_meeting_social(self.root, now=F.NOW)
        build_meeting_social(self.root, ["x"], now=F.NOW)
        rec = Recorder(self)
        before = self.check_direct()
        for rel in ("kit/social/x", "kit/social/meetings", "kit/social/meetings.suggested", "kit/social/meetings/x.suggested"):
            self.assertIn(rel, before)
        self.assertTrue(rec.requests and all(0 < n <= CAP + 1 for n in rec.requests))
        self.assertEqual(rec.opened, rec.closed)
        rec.requests.clear()
        for rel in ("x/" + F.SLUG, "meetings.suggested/m-one", "meetings/x.suggested/m-one"):
            pad_draft(self.social / (rel + ".md"), CAP + 1)
        after = self.check_direct()
        for rel, stem in (("kit/social/x", F.SLUG), ("kit/social/meetings.suggested", "m-one"),
                          ("kit/social/meetings/x.suggested", "m-one")):
            row = [r for r in after[rel]["drafts"] if r["slug"] == stem][0]
            self.assertEqual((row["state"], row["problems"]), ("not_regular", ["not_regular"]), rel)
        self.assertTrue(all(0 < n <= CAP + 1 for n in rec.requests))

    def test_suggested_draft_is_read_once_per_folder_check_and_never_across_checks(self):
        self.meetings_built()
        draft = self.social / "meetings" / "m-one.md"
        draft.write_text(draft.read_text(encoding="utf-8") + " note", encoding="utf-8")
        self.meeting(starts="2026-11-15T10:00:00-08:00")
        build_meeting_social(self.root, now=F.NOW)
        target = (self.social / "meetings.suggested" / "m-one.md").resolve()
        reads = []
        real = SC._read_bounded
        with mock.patch.object(SC, "_read_bounded", lambda p, limit: (reads.append(Path(p).resolve()), real(p, limit))[1]):
            self.check()
            self.assertEqual(reads.count(target), 1)
            self.check()
            self.assertEqual(reads.count(target), 2)  # nothing cached across separate checks


class SpecialFilesAndSurfaces(ReadBoundsCase):
    def test_links_special_files_and_missing_are_fixed_refusals_without_reading_targets(self):
        build_social(self.root, ["x"])
        folder = self.social / "x"
        draft = folder / f"{F.SLUG}.md"
        target = self.root / "outside.md"
        target.write_text(CANARY, encoding="utf-8")
        draft.unlink()
        rec = Recorder(self)
        for make in (lambda: os.symlink(target, draft), lambda: os.symlink(self.root / "dangling", draft),
                     lambda: os.mkfifo(draft), lambda: draft.mkdir()):
            if draft.is_dir() and not draft.is_symlink():
                draft.rmdir()
            elif os.path.lexists(draft):
                draft.unlink()
            make()
            self.assertEqual(self.states("x"), {F.SLUG: ("not_regular", ["not_regular"])})
        self.assertNotIn(str(target), rec.read_paths)
        self.assertNotIn(str(draft), rec.read_paths)
        self.assertEqual(rec.opened, rec.closed)
        draft.rmdir()
        self.assertEqual(self.states("x"), {F.SLUG: ("missing", ["missing"])})
        (folder / ".written.json").unlink()
        os.mkfifo(folder / ".written.json")  # a special sidecar is a fixed bad, and does not block
        self.assertEqual(self.folder("x")["folder_problems"], [".written.json is not a usable regular file; its contents are ignored"])

    def test_open_and_read_failures_are_fixed_and_safe_siblings_still_checked(self):
        build_social(self.root, ["x", "mastodon"])
        for failing in ("open", "read"):
            with mock.patch.object(SC.os, failing, side_effect=OSError(CANARY)):
                report = self.check()
            text = json.dumps(report)
            self.assertNotIn(CANARY, text)
            self.assertNotIn(str(self.root), text)
        self.assertEqual(self.states("mastodon"), {F.SLUG: ("ok", [])})

    def test_api_surface_an_oversized_draft_and_sidecar(self):
        self.cap()
        self.meetings_built()
        build_social(self.root, ["x"])
        pad_draft(self.social / "x" / f"{F.SLUG}.md", CAP + 1)
        pad_json(self.social / "meetings" / ".check.json", CAP + 1)
        report = self.check()
        self.assertEqual(report["folders"]["kit/social/x"]["drafts"][0]["state"], "not_regular")
        self.assertEqual(report["folders"]["kit/social/meetings"]["problems"], 1)
        self.assertEqual(report["problems"], 2)
        self.assertFalse(report["posted"])

    def test_cli_and_today_surface_real_size_oversized_draft_and_sidecar(self):
        self.meetings_built()
        build_social(self.root, ["x"])
        pad_draft(self.social / "x" / f"{F.SLUG}.md", SC.MAX_DRAFT_BYTES + 1)
        pad_json(self.social / "meetings" / ".check.json", SC.MAX_STATE_BYTES + 1)
        before = self.tree()
        text_report = self.cli("--check", "--text")
        json_report = self.cli("--check")
        for result in (text_report, json_report):
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertNotIn(str(self.root), result.stdout)
        data = json.loads(json_report.stdout)
        self.assertEqual((data["problems"], data["posted"]), (2, False))
        self.assertIn(f"{F.SLUG}: not_regular (not_regular)", text_report.stdout)
        self.assertIn("folder: .check.json is not a usable regular file; its contents are ignored", text_report.stdout)
        self.assertEqual(self.tree(), before)
        items = today._social(self.root, datetime(2026, 11, 10).date(), "--directory x")
        self.assertEqual([i["kind"] for i in items], ["unreadable"])
        self.assertIn("kit/social/x", json.dumps(items))
        self.assertIn("kit/social/meetings", json.dumps(items))

    def test_ordinary_admitted_report_unchanged_and_checks_are_read_only_and_private(self):
        self.meetings_built()
        build_social(self.root)
        before = self.tree()
        modes = {str(p): stat.S_IMODE(p.stat().st_mode) for p in self.root.rglob("*")}
        with mock.patch("socket.socket.connect", side_effect=AssertionError("network used")), \
                mock.patch("subprocess.run", side_effect=AssertionError("subprocess used")):
            report = self.check()
        self.assertEqual(report["problems"], 0)
        self.assertEqual(report["folders"]["kit/social/meetings"], {"drafts": [{"slug": "m-one", "state": "ok", "problems": []}],
                                                                    "stale": [], "stale_reasons": {}, "problems": 0})
        self.assertEqual(self.tree(), before)
        self.assertEqual({str(p): stat.S_IMODE(p.stat().st_mode) for p in self.root.rglob("*")}, modes)


if __name__ == "__main__":
    unittest.main()
