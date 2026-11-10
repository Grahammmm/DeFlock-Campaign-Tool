"""DF1109-12: officials add|recheck|remove|list maintain content/officials.json without hand-editing.

Temp copies of the fictional example, synthetic names and .example hosts only; nothing is fetched or built.
"""
import contextlib
import io
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from campaign_tool import cli
from campaign_tool import officials_cmd as oc
from campaign_tool.content import ContentError, load_content, load_officials
from campaign_tool.meeting_outcome import record_outcome
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 10, 20)
COUNCIL, POLICE = "ca-cedar-city-council", "ca-cedar-police"
MID = "cedar-council-2026-10"
SRC = "https://cedar.example/council"
PAYLOAD = "Unmistakable Payload"
FILE_NAME = oc.FILE


def day(days_ago, today=TODAY):
    return (today - timedelta(days=days_ago)).isoformat()


def person(name, checked_on="2026-09-01", role="Council member"):
    return {"name": name, "role": role, "source_url": SRC, "checked_on": checked_on}


class OfficialsCommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-officials-cmd-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content = self.root / "content"
        self.file = self.content / "officials.json"

    def add(self, name, agency=COUNCIL, today=TODAY, **extra):
        values = {"role": "Council member", "source_url": SRC, "checked_on": "2026-09-01", **extra}
        return oc.add_official(self.root, agency_id=agency, name=name, today=today, **values)

    def write(self, bodies):
        self.file.write_text(json.dumps({"schema_version": 1, "bodies": bodies}), encoding="utf-8")

    def stored(self):
        return json.loads(self.file.read_text(encoding="utf-8"))

    def names(self):
        return [(body["agency_id"], p["name"]) for body in self.stored()["bodies"] for p in body["officials"]]

    def snap(self):
        info = os.lstat(self.file)
        return self.file.read_bytes(), stat.S_IMODE(info.st_mode), info.st_mtime_ns

    def temps(self):
        return sorted(p.name for p in self.content.iterdir() if p.name.startswith(".officials-"))

    def refused(self, call, needle, **kwargs):
        before = self.snap() if self.file.is_file() and not self.file.is_symlink() else None
        with self.assertRaises(ContentError) as caught:
            call(**kwargs)
        self.assertIn(needle, str(caught.exception))
        self.assertNotIn(PAYLOAD, str(caught.exception))
        if before is not None:
            self.assertEqual(self.snap(), before)
        self.assertEqual(self.temps(), [])
        return str(caught.exception)

    def test_first_add_and_body_append_preserve_order(self):
        self.assertIsNone(oc.list_officials(self.root, today=TODAY))
        self.assertFalse(os.path.lexists(self.file))
        self.refused(lambda: oc.recheck_official(self.root, agency_id=COUNCIL, name="Ash Example", checked_on="2026-09-01",
                                                 today=TODAY), "does not exist")
        self.assertFalse(os.path.lexists(self.file))
        self.assertEqual(self.add("Ash Example", profile_url="https://cedar.example/ash"),
                         {"agency_id": COUNCIL, "position": 1, "created": True})
        self.assertEqual(stat.S_IMODE(os.lstat(self.file).st_mode), 0o600)
        self.assertEqual(self.stored()["bodies"][0]["officials"][0], {
            "name": "Ash Example", "role": "Council member", "profile_url": "https://cedar.example/ash",
            "source_url": SRC, "checked_on": "2026-09-01"})
        self.add("Birch Example")
        self.add("Ash Example", agency=POLICE)  # the same name in another body is legal
        self.add("ash example")  # case-sensitive, as --votes matching
        self.assertEqual(self.names(), [(COUNCIL, "Ash Example"), (COUNCIL, "Birch Example"), (COUNCIL, "ash example"),
                                        (POLICE, "Ash Example")])
        self.assertTrue(self.file.read_text(encoding="utf-8").endswith("}\n"))
        self.refused(lambda: self.add("Birch Example"), "already lists an official with this name")
        self.refused(lambda: self.add(" Birch \u00a0 Example "), "already lists")
        self.add("Zo\u00eb Example")
        self.refused(lambda: self.add("Zoe\u0308  Example"), "already lists")
        # a hand-edited legacy duplicate still loads and lists; exact-name targeting refuses rather than guessing
        self.write([{"agency_id": COUNCIL, "officials": [person("Dup Example"), person("Dup Example")]}])
        self.assertEqual(len(load_officials(self.stored(), [{"agency_id": COUNCIL}], TODAY)[COUNCIL]), 2)
        self.assertEqual([row["name"] for row in oc.list_officials(self.root, today=TODAY)["officials"]], ["Dup Example"] * 2)
        for call in (lambda: oc.recheck_official(self.root, agency_id=COUNCIL, name="Dup Example", checked_on="2026-10-01",
                                                 today=TODAY),
                     lambda: oc.remove_official(self.root, agency_id=COUNCIL, name="Dup Example", today=TODAY)):
            self.refused(call, "listed more than once")

    def test_whole_file_validation_precedes_atomic_write(self):
        for data, needle in ((b"{not json", "not valid UTF-8 JSON"), (b"", "not valid UTF-8 JSON"), (b"null", "expected an object"),
                             (b'{"schema_version": 1, "bodies": [], "extra": 1}', "unknown field"),
                             (b'{"schema_version": 1, "schema_version": 1, "bodies": []}', "repeats a key"),
                             (json.dumps({"schema_version": 1, "bodies": [{"agency_id": "ca-nowhere",
                                                                            "officials": [person("A Example")]}]}).encode(),
                              "not a loaded agency"),
                             (b" " * (oc.MAX_JSON_BYTES + 1), "file too large")):
            self.file.write_bytes(data)
            self.refused(lambda: self.add("New Example"), needle)
        self.file.unlink()
        for kwargs, needle in (({"agency": "ca-nowhere"}, "--agency: not a loaded agency"),
                               ({"name": PAYLOAD + " x@y.example"}, "privacy lint: --name: email"),
                               ({"role": PAYLOAD + " 805-555-0142"}, "privacy lint: --role: phone"),
                               ({"name": PAYLOAD + " <b>"}, "must be plain text"),
                               ({"source_url": "http://cedar.example/" + PAYLOAD.replace(" ", "")}, "source_url: must be an https"),
                               ({"profile_url": "ftp://cedar.example/x"}, "profile_url: must be an https"),
                               ({"checked_on": "2026-02-30"}, "--checked-on: must be a real"),
                               ({"checked_on": "2026-10-21"}, "must not be after"),
                               ({"checked_on": "2025-10-19"}, "older than 365 days")):
            name = kwargs.pop("name", "New Example")
            self.refused(lambda: self.add(name, **kwargs), needle)
            self.assertFalse(os.path.lexists(self.file))
        self.add("Ash Example")
        calls = []
        real = oc.load_officials
        with mock.patch.object(oc, "load_officials", side_effect=lambda *a, **k: calls.append((a, k)) or real(*a, **k)):
            self.add("Birch Example", agency=POLICE)
        prospective, strict = calls[-1][0][0], calls[-1][1]
        self.assertEqual(strict, {"require_fresh": True})
        self.assertEqual([b["agency_id"] for b in prospective["bodies"]], [COUNCIL, POLICE])  # the whole file, not one row
        self.assertEqual(self.names(), [(COUNCIL, "Ash Example"), (POLICE, "Birch Example")])

    def test_list_boundary_ages_is_read_only(self):
        self.write([])
        self.assertEqual(oc.list_officials(self.root, today=TODAY)["officials"], [])
        self.write([{"agency_id": COUNCIL, "officials": [person("A Example", day(0)), person("B Example", day(365)),
                                                         person("C Example", day(366))]}])
        before = self.snap()
        report = oc.list_officials(self.root, today=TODAY)
        self.assertEqual([(r["age_days"], r["status"]) for r in report["officials"]], [(0, "current"), (365, "current"),
                                                                                      (366, "stale")])
        self.assertEqual(set(report["officials"][0]), {"agency_id", "name", "role", "profile_url", "source_url",
                                                       "checked_on", "age_days", "status"})
        self.assertEqual(report["stale"], 1)
        self.assertEqual(self.snap(), before)
        leap = date(2028, 2, 29)
        self.write([{"agency_id": COUNCIL, "officials": [person("A Example", "2027-03-01"), person("B Example", "2027-02-28")]}])
        self.assertEqual([r["status"] for r in oc.list_officials(self.root, today=leap)["officials"]], ["current", "stale"])
        for bad, needle in (("2026-10-21", "must not be after"), ("2026-02-30", "checked_on"), ("20261001", "checked_on")):
            self.write([{"agency_id": COUNCIL, "officials": [person("A Example", bad)]}])
            self.refused(lambda: oc.list_officials(self.root, today=TODAY), needle)
        self.assertEqual(self.temps(), [])

    def test_recheck_stale_rows_sequentially_without_relaxing_build(self):
        self.write([{"agency_id": COUNCIL, "officials": [person("A Example", day(400)), person("B Example", day(10)),
                                                         person("C Example", day(366))]}])
        self.refused(lambda: load_content(self.root, today=TODAY), "older than 365 days")
        self.refused(lambda: self.add("D Example"), "older than 365 days")  # add keeps the whole file strict
        recheck = lambda name, on: oc.recheck_official(self.root, agency_id=COUNCIL, name=name, checked_on=on, today=TODAY)  # noqa: E731
        for name, on, needle in (("A Example", day(-1), "must not be after today"), ("B Example", day(20), "never moves back"),
                                 ("A Example", "2026-13-01", "must be a real"), ("A Example", day(366), "older than 365 days")):
            self.refused(lambda: recheck(name, on), needle)
        self.refused(lambda: recheck("Nobody Example", day(1)), "no official with exactly this name")
        result = recheck("A Example", day(1))
        self.assertEqual(result, {"agency_id": COUNCIL, "position": 1, "changed": True, "still_stale": 1})
        self.assertEqual([p["checked_on"] for p in self.stored()["bodies"][0]["officials"]], [day(1), day(10), day(366)])
        with self.assertRaises(ContentError):
            load_content(self.root, today=TODAY)  # the build is still strict until the last repair
        before = self.snap()
        self.assertFalse(recheck("A Example", day(1))["changed"])
        self.assertEqual(self.snap(), before)  # same day: no write, same bytes, mode and mtime
        self.assertEqual(recheck("C Example", day(0))["still_stale"], 0)
        self.assertIsNotNone(load_content(self.root, today=TODAY))

    def test_remove_refuses_votes_and_statements_with_same_body_identity(self):
        for name in ("Ash Example", "Birch Example", "Cedar Example"):
            self.add(name)
        self.add("Ash Example", agency=POLICE)
        self.add("Birch Example", agency=POLICE)
        votes = self.tmp / "votes.csv"
        votes.write_text("name,vote\nAsh Example,yes\n", encoding="utf-8")
        record_outcome(self.root, MID, "approved", "2026-10-14", "https://example.invalid/minutes", "Synthetic Organizer",
                       today=TODAY, votes_file=votes)
        statements = self.content / "statements.json"
        statements.write_text(json.dumps([{"agency_id": COUNCIL, "official": "Birch Example", "said_on": "2026-09-01",
                                           "quote": "We will review it.", "context": "meeting",
                                           "source_url": "https://cedar.example/minutes", "checked_on": "2026-09-02"}]),
                              encoding="utf-8")
        remove = lambda name, agency=COUNCIL: oc.remove_official(self.root, agency_id=agency, name=name, today=TODAY)  # noqa: E731
        self.refused(lambda: remove("Ash Example"), "content/meetings/2026-10-cedar-council.json")
        self.refused(lambda: remove("Birch Example"), "statements.json[0]")
        self.assertEqual(remove("Ash Example", POLICE), {"agency_id": POLICE, "body_dropped": False})  # other body
        self.assertEqual(remove("Birch Example", POLICE), {"agency_id": POLICE, "body_dropped": True})
        outside = self.tmp / "outside.json"
        outside.write_text("[]", encoding="utf-8")
        for setup, needle in ((lambda: statements.write_text("[{", encoding="utf-8"), "statements.json: not valid"),
                              (lambda: (statements.unlink(), statements.symlink_to(outside)), "symlinks are not read"),
                              (lambda: (statements.unlink(), (self.content / "meetings" / "x.json").symlink_to(outside)),
                               "content/meetings/x.json: is a symbolic link"),
                              (lambda: ((self.content / "meetings" / "x.json").unlink(),
                                        (self.content / "meetings" / "y.json").write_text("{", encoding="utf-8")),
                               "content/meetings/y.json: not valid")):
            setup()
            self.refused(lambda: remove("Cedar Example"), needle)
        (self.content / "meetings" / "y.json").unlink()
        self.assertEqual(outside.read_text(encoding="utf-8"), "[]")
        self.assertFalse(remove("Cedar Example")["body_dropped"])
        self.assertEqual(self.names(), [(COUNCIL, "Ash Example"), (COUNCIL, "Birch Example")])
        shutil.rmtree(self.content / "meetings")
        remove("Ash Example")
        self.assertTrue(remove("Birch Example")["body_dropped"])
        self.assertEqual(self.stored(), {"schema_version": 1, "bodies": []})
        self.assertEqual(oc.list_officials(self.root, today=TODAY)["officials"], [])

    def test_links_fifo_parent_swap_and_first_create_arrival_refuse(self):
        marker = self.tmp / "marker.json"
        marker.write_text(json.dumps({"schema_version": 1, "bodies": []}), encoding="utf-8")
        calls = (lambda: oc.list_officials(self.root, today=TODAY), lambda: self.add("Ash Example"),
                 lambda: oc.recheck_official(self.root, agency_id=COUNCIL, name="Ash Example", checked_on=day(1), today=TODAY),
                 lambda: oc.remove_official(self.root, agency_id=COUNCIL, name="Ash Example", today=TODAY))
        for make, needle in ((lambda: self.file.symlink_to(marker), "is a symbolic link"),
                             (lambda: os.mkfifo(self.file), "must be a regular file"),
                             (lambda: self.file.mkdir(), "must be a regular file")):
            make()
            for call in calls:
                self.refused(call, needle)
            self.file.rmdir() if self.file.is_dir() and not self.file.is_symlink() else self.file.unlink()
        self.assertEqual(json.loads(marker.read_text(encoding="utf-8")), {"schema_version": 1, "bodies": []})
        moved = self.tmp / "real-content"
        self.content.rename(moved)
        self.content.symlink_to(moved)
        for call in calls:
            self.refused(call, "content/ must be a real folder")
        self.content.unlink()
        moved.rename(self.content)

        real_fsync = os.fsync
        def arrive(fd):  # a file appears at the target while the first one is staged
            real_fsync(fd)
            if not os.path.lexists(self.file):
                self.file.write_bytes(b"arrived\n")
        with mock.patch.object(oc.os, "fsync", side_effect=arrive):
            self.refused(lambda: self.add("Ash Example"), "changed while it was being edited")
        self.assertEqual(self.file.read_bytes(), b"arrived\n")
        self.file.unlink()
        self.add("Ash Example")
        os.chmod(self.file, 0o640)
        def swap(fd):
            real_fsync(fd)
            if self.file.read_bytes().endswith(b"}\n"):
                self.file.write_bytes(self.file.read_bytes() + b" ")
        with mock.patch.object(oc.os, "fsync", side_effect=swap), self.assertRaises(ContentError) as caught:
            self.add("Birch Example")
        self.assertIn("changed while it was being edited", str(caught.exception))
        self.assertEqual(self.names(), [(COUNCIL, "Ash Example")])  # the other writer's bytes are kept
        self.assertEqual(self.temps(), [])
        for target, needle in (("fsync", "write failed"), ("replace", "write failed")):
            with mock.patch.object(oc.os, target, side_effect=OSError("injected")):
                self.refused(lambda: self.add("Birch Example"), needle)
            self.assertEqual(stat.S_IMODE(os.lstat(self.file).st_mode), 0o640)
        self.add("Birch Example")
        self.assertEqual(stat.S_IMODE(os.lstat(self.file).st_mode), 0o640)  # the existing mode is kept

    def test_refusals_never_echo_the_agency_value(self):
        self.add("Ash Example")
        marker = "ca-" + PAYLOAD.replace(" ", "-").lower()
        for call in (lambda: oc.recheck_official(self.root, agency_id=marker, name="Ash Example", checked_on=day(1),
                                                 today=TODAY),
                     lambda: oc.remove_official(self.root, agency_id=marker, name="Ash Example", today=TODAY)):
            message = self.refused(call, "no official with exactly this name is listed for this --agency")
            self.assertNotIn(marker, message)
        self.write([{"agency_id": COUNCIL, "officials": [person("Dup Example"), person("Dup Example")]}])
        for call in (lambda: oc.recheck_official(self.root, agency_id=COUNCIL, name="Dup Example", checked_on=day(1),
                                                 today=TODAY),
                     lambda: oc.remove_official(self.root, agency_id=COUNCIL, name="Dup Example", today=TODAY)):
            message = self.refused(call, "listed more than once for this --agency")
            self.assertNotIn(COUNCIL, message)

    def test_folder_flush_failure_after_commit_does_not_claim_the_old_file_was_kept(self):
        real_fsync = os.fsync
        for first in (True, False):  # first create (link) and later edit (rename)
            if not first:
                self.add("Ash Example")
            calls = []
            def fail_folder(fd):  # the staged file's fsync succeeds; the folder's, after the commit, fails
                calls.append(fd)
                if len(calls) == 2:
                    raise OSError("injected")
                real_fsync(fd)
            with mock.patch.object(oc.os, "fsync", side_effect=fail_folder), self.assertRaises(ContentError) as caught:
                self.add("Birch Example" if not first else "Ash Example")
            message = str(caught.exception)
            self.assertNotIn("previous file was kept", message)
            self.assertNotIn("nothing was written", message)
            self.assertIn("new roster was written", message)
            self.assertIn("Birch Example" if not first else "Ash Example", [n for _, n in self.names()])  # it did commit
            self.assertEqual(self.temps(), [])
            if first:
                self.file.unlink()

    # --- RF2205-1: writes are bound to the content/ folder the inputs were read from -------------------------------

    FOLDER_REFUSAL = "content/: changed while it was being edited; nothing was written"

    def tree(self, folder):
        """Every entry of a folder as {name: (bytes, mode, mtime_ns)} so a refusal can prove nothing else moved."""
        return {p.name: (p.read_bytes(), stat.S_IMODE(os.lstat(p).st_mode), os.lstat(p).st_mtime_ns)
                for p in Path(folder).iterdir() if p.is_file()}

    def swap_in_real_folder(self, link_roster=False, copy_roster=False):
        """Rename the validated content/ aside and put a new real content/ (with a marker, no site.json) in its place."""
        aside = self.tmp / "content-aside"
        self.content.rename(aside)
        self.content.mkdir()
        (self.content / "marker.txt").write_text("replacement marker\n", encoding="utf-8")
        if link_roster:
            os.link(aside / FILE_NAME, self.file)
        elif copy_roster:
            shutil.copy2(aside / FILE_NAME, self.file)
        return aside

    def restore_folder(self, aside):
        if os.path.islink(self.content):
            self.content.unlink()
        elif self.content.exists():
            shutil.rmtree(self.content)
        aside.rename(self.content)

    def swap_before_install(self, **kwargs):
        """Patch the installer so the swap happens after the real state read and before installation starts."""
        real, box = oc._install, {}

        def swapping(*args, **options):
            box["aside"] = self.swap_in_real_folder(**kwargs)
            box["before_new"], box["before_old"] = self.tree(self.content), self.tree(box["aside"])
            return real(*args, **options)
        return mock.patch.object(oc, "_install", side_effect=swapping), box

    def swap_after_staging(self, action):
        """Patch fsync so ``action`` runs once, right after the staged file was fsynced and before publication."""
        real, box = os.fsync, {"calls": 0}

        def hooked(fd):
            real(fd)
            box["calls"] += 1
            if box["calls"] == 1:
                box["aside"] = action()
        return mock.patch.object(oc.os, "fsync", side_effect=hooked), box

    def mutations(self):
        """The three mutating verbs against a roster holding Ash and Birch (first create is covered separately)."""
        return (("add", lambda: self.add("Cedar Example")),
                ("recheck", lambda: oc.recheck_official(self.root, agency_id=COUNCIL, name="Ash Example",
                                                        checked_on=day(1), today=TODAY)),
                ("remove", lambda: oc.remove_official(self.root, agency_id=COUNCIL, name="Birch Example", today=TODAY)))

    def test_first_add_refuses_real_content_substitution_before_staging(self):
        patch, box = self.swap_before_install()
        with patch, self.assertRaises(ContentError) as caught:
            self.add("Ash Example")
        self.assertEqual(str(caught.exception), self.FOLDER_REFUSAL)
        self.assertNotIn("Ash Example", str(caught.exception))
        self.assertFalse(os.path.lexists(self.file))
        self.assertFalse(os.path.lexists(box["aside"] / FILE_NAME))
        self.assertEqual(self.tree(self.content), box["before_new"])  # marker untouched, no temp created
        self.assertEqual(self.tree(box["aside"]), box["before_old"])

    def test_existing_mutators_refuse_substituted_parent_even_with_same_roster_inode(self):
        self.add("Ash Example")
        self.add("Birch Example")
        for verb, call in self.mutations():
            patch, box = self.swap_before_install(link_roster=True)
            with patch, self.assertRaises(ContentError) as caught:
                call()
            self.assertEqual(str(caught.exception), self.FOLDER_REFUSAL, verb)
            old, new = os.stat(box["aside"] / FILE_NAME), os.stat(self.file)
            self.assertEqual((old.st_dev, old.st_ino), (new.st_dev, new.st_ino), verb)  # file identity alone matches
            self.assertEqual(self.tree(self.content), box["before_new"], verb)
            self.assertEqual(self.tree(box["aside"]), box["before_old"], verb)
            self.assertEqual(self.names(), [(COUNCIL, "Ash Example"), (COUNCIL, "Birch Example")], verb)
            self.restore_folder(box["aside"])

    def test_parent_substitution_during_staging_refuses_and_cleans_original_temp(self):
        for first in (True, False):
            if not first:
                self.add("Ash Example")
            before = self.tree(self.content)
            patch, box = self.swap_after_staging(lambda: self.swap_in_real_folder(copy_roster=not first))
            with patch, self.assertRaises(ContentError) as caught:
                self.add("Ash Example" if first else "Birch Example")
            self.assertEqual(str(caught.exception), self.FOLDER_REFUSAL)
            self.assertEqual(self.tree(box["aside"]), before)  # only this operation's own temp was removed
            self.assertEqual(sorted(p.name for p in self.content.iterdir()),
                             ["marker.txt"] + ([] if first else [FILE_NAME]))
            self.assertEqual((self.content / "marker.txt").read_text(encoding="utf-8"), "replacement marker\n")
            if not first:
                self.assertEqual(self.names(), [(COUNCIL, "Ash Example")])  # the replacement's roster is unchanged
            self.restore_folder(box["aside"])
            if first:
                self.assertFalse(os.path.lexists(self.file))

    def test_parent_missing_or_symlink_at_prepublication_check_refuses(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "marker.txt").write_text("outside marker\n", encoding="utf-8")
        outside_before = self.tree(outside)
        for first in (True, False):
            for variant in ("missing", "symlink"):
                if not first and not self.file.exists():
                    self.add("Ash Example")
                before = self.tree(self.content)

                def action(variant=variant):
                    aside = self.tmp / "content-aside"
                    self.content.rename(aside)
                    if variant == "symlink":
                        self.content.symlink_to(outside)
                    return aside
                patch, box = self.swap_after_staging(action)
                with patch, self.assertRaises(ContentError) as caught:
                    self.add("Ash Example" if first else "Birch Example")
                self.assertEqual(str(caught.exception), self.FOLDER_REFUSAL)
                self.assertEqual(self.tree(box["aside"]), before)  # nothing published to the detached folder
                self.assertEqual(self.tree(outside), outside_before)  # nothing written through the link
                self.assertEqual(sorted(p.name for p in outside.iterdir()), ["marker.txt"])
                self.restore_folder(box["aside"])
                self.assertEqual(self.tree(self.content), before)
            if first:
                self.assertFalse(os.path.lexists(self.file))

    def test_directory_identity_ignores_size_and_mtime_changes(self):
        sibling = b"a sibling that arrived while staging\n"
        patch, _ = self.swap_after_staging(lambda: (self.content / "arrived.txt").write_bytes(sibling))
        with patch:
            self.assertEqual(self.add("Ash Example"), {"agency_id": COUNCIL, "position": 1, "created": True})
        patch, _ = self.swap_after_staging(lambda: (self.content / "arrived-2.txt").write_bytes(sibling))
        with patch:
            self.assertEqual(self.add("Birch Example")["position"], 2)
        self.assertEqual(self.names(), [(COUNCIL, "Ash Example"), (COUNCIL, "Birch Example")])
        self.assertEqual(((self.content / "arrived.txt").read_bytes(), (self.content / "arrived-2.txt").read_bytes()),
                         (sibling, sibling))
        self.assertEqual(self.temps(), [])

    def test_legacy_state_shape_and_statement_roster_consumer_remain_compatible(self):
        agencies, document, loaded = oc._state(self.root, TODAY)
        self.assertEqual((document, loaded), (None, None))
        self.assertTrue(agencies)
        self.add("Ash Example")
        legacy = oc._state(self.root, TODAY)
        self.assertEqual(len(legacy), 3)
        info = os.lstat(self.file)
        self.assertEqual(legacy[2], ((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns), stat.S_IMODE(info.st_mode)))
        folder = os.stat(self.content)
        for state in (oc._state(self.root, TODAY, with_folder_identity=True),
                      oc._existing(self.root, TODAY, with_folder_identity=True)):
            self.assertEqual(len(state), 4)
            self.assertEqual(state[:3], legacy)
            self.assertEqual(state[3], (folder.st_dev, folder.st_ino))
        self.assertEqual(len(oc._existing(self.root, TODAY)), 3)
        os.unlink(self.file)
        self.assertEqual(oc._state(self.root, TODAY, with_folder_identity=True)[1:],
                         (None, None, (folder.st_dev, folder.st_ino)))

    def test_parent_refusals_are_private_and_descriptors_are_closed(self):
        opened = set()
        real_open, real_close = os.open, os.close

        def track_open(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if flags & os.O_DIRECTORY:
                opened.add(fd)
            return fd

        def track_close(fd):
            opened.discard(fd)
            return real_close(fd)
        tracking = (mock.patch.object(oc.os, "open", side_effect=track_open),
                    mock.patch.object(oc.os, "close", side_effect=track_close))
        with tracking[0], tracking[1]:
            self.add("Ash Example")  # success
            self.assertEqual(opened, set())
            patch, box = self.swap_before_install(link_roster=True)
            with patch, self.assertRaises(ContentError):
                self.add("Birch Example")  # mismatch at entry
            self.assertEqual(opened, set())
            self.restore_folder(box["aside"])
            patch, box = self.swap_after_staging(lambda: self.swap_in_real_folder(link_roster=True))
            with patch, self.assertRaises(ContentError):
                self.add("Birch Example")  # mismatch at the final check
            self.assertEqual(opened, set())
            self.restore_folder(box["aside"])

            def vanish():
                aside = self.tmp / "content-aside"
                self.content.rename(aside)
                return aside
            patch, box = self.swap_after_staging(vanish)
            with patch, self.assertRaises(ContentError):
                self.add("Birch Example")  # the reopen at the final check fails
            self.assertEqual(opened, set())
            self.restore_folder(box["aside"])
            before = self.snap()
            oc.list_officials(self.root, today=TODAY)
            self.assertFalse(oc.recheck_official(self.root, agency_id=COUNCIL, name="Ash Example", checked_on="2026-09-01",
                                                 today=TODAY)["changed"])
            self.assertEqual((self.snap(), opened), (before, set()))  # list and same-day recheck install nothing

        args = ["campaign_tool", "officials", "add", "--agency", COUNCIL, "--name", PAYLOAD, "--role", "Mayor",
                "--source-url", SRC, "--checked-on", (date.today() - timedelta(days=3)).isoformat(),
                "--directory", str(self.root)]
        patch, box = self.swap_before_install()
        out, err = io.StringIO(), io.StringIO()
        with patch, mock.patch.object(sys, "argv", args), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            cli.main()
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("changed while it was being edited", err.getvalue())
        for text in (PAYLOAD, "content-aside", str(self.tmp), "Traceback"):
            self.assertNotIn(text, err.getvalue())

    def test_commands_never_fetch_build_or_publish(self):
        build_site(self.root, today=TODAY)
        public = {p: p.read_bytes() for p in (self.root / "public").rglob("*") if p.is_file()}
        with mock.patch.object(socket, "socket", side_effect=AssertionError("no network")), \
                mock.patch("campaign_tool.site.build_site", side_effect=AssertionError("no build")):
            self.add("Ash Example")
            self.add("Birch Example")
            oc.recheck_official(self.root, agency_id=COUNCIL, name="Ash Example", checked_on="2026-10-01", today=TODAY)
            oc.list_officials(self.root, today=TODAY)
            oc.remove_official(self.root, agency_id=COUNCIL, name="Birch Example", today=TODAY)
        self.assertEqual({p: p.read_bytes() for p in (self.root / "public").rglob("*") if p.is_file()}, public)
        votes = self.tmp / "votes.csv"
        votes.write_text("name,vote\nAsh  Example,no\n", encoding="utf-8")
        record_outcome(self.root, MID, "approved", "2026-10-14", "https://example.invalid/minutes", "Synthetic Organizer",
                       today=TODAY, votes_file=votes)
        stored = json.loads((self.content / "meetings" / "2026-10-cedar-council.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["outcome"]["votes"], [{"name": "Ash Example", "vote": "no"}])

    def test_cli_verbs_exit_codes_and_no_payload(self):
        def run(*args):
            return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "officials", *args,
                                   "--directory", str(self.root)], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        listed = run("list")
        self.assertEqual((listed.returncode, listed.stdout), (0, "No officials listed.\n"))
        self.assertFalse(os.path.lexists(self.file))
        recent = (date.today() - timedelta(days=3)).isoformat()
        added = run("add", "--agency", COUNCIL, "--name", "Ash Example", "--role", "Mayor", "--source-url", SRC,
                    "--checked-on", recent)
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertTrue(added.stdout.endswith("Recorded only. Nothing was fetched, built, published or sent.\n"))
        again = run("add", "--agency", COUNCIL, "--name", "Ash Example", "--role", PAYLOAD, "--source-url", SRC,
                    "--checked-on", recent)
        self.assertEqual(again.returncode, 2)
        self.assertIn("Stopped: --name:", again.stderr)
        self.assertNotIn("Traceback", again.stderr)
        self.assertNotIn(PAYLOAD, again.stderr + again.stdout)
        for extra in (("recheck", "--agency", COUNCIL, "--name", "Ash Example", "--checked-on", recent, "--role", "Mayor"),
                      ("add", "--agency", COUNCIL, "--name", "B Example", "--role", "Mayor", "--source", SRC,
                       "--source-url", SRC, "--checked-on", recent),  # no abbreviated flags
                      ("list", "--agency", COUNCIL)):
            self.assertEqual(run(*extra).returncode, 2)
        report = run("list")
        self.assertEqual(json.loads(report.stdout.split("\n}\n")[0] + "\n}")["officials"][0]["status"], "current")
        self.assertIn("0 stale", report.stdout)
        removed = run("remove", "--agency", COUNCIL, "--name", "Ash Example")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertEqual(self.stored(), {"schema_version": 1, "bodies": []})

    # --- RF0145-4: removal scans every meetings/*.json the build reads, dot-prefixed names included ------------------

    HIDDEN = ".retained-meeting.json"

    def hidden_vote(self, voter="Ash Example", today=TODAY, hide=True):
        """Roster Ash+Birch (council), Ash+Birch (police), a real recorded vote for ``voter``; the meeting file is
        renamed to a dot-prefixed name (its explicit id stays inside, so the build still loads it)."""
        recent = (today - timedelta(days=3)).isoformat()
        for name in dict.fromkeys((voter, "Birch Example")):
            self.add(name, today=today, checked_on=recent)
            self.add(name, agency=POLICE, today=today, checked_on=recent)
        votes = self.tmp / "votes.csv"
        votes.write_text(f"name,vote\n{voter},yes\n", encoding="utf-8")
        record_outcome(self.root, MID, "approved", "2026-10-14", "https://example.invalid/minutes", "Synthetic Organizer",
                       today=today, votes_file=votes)
        meetings = self.content / "meetings"
        if hide:
            os.rename(meetings / "2026-10-cedar-council.json", meetings / self.HIDDEN)
        return meetings / (self.HIDDEN if hide else "2026-10-cedar-council.json")

    def meeting_state(self):
        """Names in meetings/ with bytes (regular files) or the file type, so a refusal can prove nothing moved."""
        state = {}
        for entry in sorted((self.content / "meetings").iterdir(), key=lambda p: p.name):
            info = os.lstat(entry)
            state[entry.name] = (entry.read_bytes(), stat.S_IMODE(info.st_mode), info.st_mtime_ns) \
                if stat.S_ISREG(info.st_mode) else stat.S_IFMT(info.st_mode)
        return state

    def remove(self, name, agency=COUNCIL, today=TODAY):
        return oc.remove_official(self.root, agency_id=agency, name=name, today=today)

    def test_hidden_meeting_vote_blocks_removal_and_preserves_loadability(self):
        control = self.hidden_vote(hide=False)
        self.refused(lambda: self.remove("Ash Example"), "content/meetings/2026-10-cedar-council.json")
        os.rename(control, control.with_name(self.HIDDEN))
        self.assertEqual([m["id"] for m in load_content(self.root, today=TODAY)["meetings"]], [MID])  # the build reads it
        roster, meetings = self.snap(), self.meeting_state()
        with mock.patch.object(oc, "_install", side_effect=AssertionError("must not install")):
            message = self.refused(lambda: self.remove("Ash Example"), "content/meetings/" + self.HIDDEN)
        self.assertIn("nothing was written", message)
        self.assertEqual((self.snap(), self.meeting_state()), (roster, meetings))
        self.assertEqual([m["id"] for m in load_content(self.root, today=TODAY)["meetings"]], [MID])  # still loadable
        self.assertEqual(self.names(), [(COUNCIL, "Ash Example"), (COUNCIL, "Birch Example"), (POLICE, "Ash Example"),
                                        (POLICE, "Birch Example")])

    def test_real_cli_hidden_vote_refuses_without_success_or_write(self):
        today = date.today()
        self.hidden_vote(voter=PAYLOAD, today=today)
        roster, meetings = self.snap(), self.meeting_state()
        done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "officials", "remove", "--agency", COUNCIL,
                               "--name", PAYLOAD, "--directory", str(self.root)], cwd=REPO_ROOT, capture_output=True,
                              text=True, timeout=60)
        self.assertEqual((done.returncode, done.stdout), (2, ""))
        self.assertEqual(done.stderr.count("\n"), 1)
        self.assertTrue(done.stderr.startswith("Stopped: --name: still named by a recorded vote in content/meetings/"
                                               + self.HIDDEN), done.stderr)
        for text in (PAYLOAD, str(self.tmp), "Traceback", oc.DONE, "DONE"):
            self.assertNotIn(text, done.stderr + done.stdout)
        self.assertEqual((self.snap(), self.meeting_state(), self.temps()), (roster, meetings, []))

    def test_hidden_json_candidates_fail_closed_before_install(self):
        self.hidden_vote(hide=False)
        meetings = self.content / "meetings"
        valid = (meetings / "2026-10-cedar-council.json").read_bytes()
        outside = self.tmp / "outside.json"
        outside.write_bytes(valid)
        cases = (("malformed", lambda p: p.write_bytes(b"{not json"), "not valid UTF-8 JSON"),
                 ("invalid utf8", lambda p: p.write_bytes(b"\xff\xfe{}"), "not valid UTF-8 JSON"),
                 ("duplicate key", lambda p: p.write_bytes(b'{"id": "a", "id": "b"}'), "repeats a key"),
                 ("invalid shape", lambda p: p.write_bytes(b'{"id": "other-meeting"}'), ".json"),
                 ("oversized", lambda p: p.write_bytes(b" " * (oc.MAX_JSON_BYTES + 1)), "file too large"),
                 ("valid link", lambda p: p.symlink_to(outside), "is a symbolic link"),
                 ("dangling link", lambda p: p.symlink_to(self.tmp / "missing.json"), "is a symbolic link"),
                 ("directory", lambda p: p.mkdir(), "must be a regular file"),
                 ("fifo", lambda p: os.mkfifo(p), "must be a regular file"))

        def timed_out(*_):
            raise AssertionError("a hidden candidate blocked the scan")
        for label, make, needle in cases:
            with self.subTest(label):
                path = meetings / ".bad-candidate.json"
                make(path)
                roster, state = self.snap(), self.meeting_state()
                previous = signal.signal(signal.SIGALRM, timed_out)
                signal.alarm(20)
                try:
                    with mock.patch.object(oc, "_install", side_effect=AssertionError("must not install")):
                        message = self.refused(lambda: self.remove("Birch Example"), needle)
                finally:
                    signal.alarm(0)
                    signal.signal(signal.SIGALRM, previous)
                self.assertIn("meetings/.bad-candidate.json", message)
                self.assertEqual((self.snap(), self.meeting_state()), (roster, state))
                self.assertEqual(outside.read_bytes(), valid)
                path.rmdir() if label == "directory" else path.unlink()
        self.assertEqual(self.remove("Birch Example"), {"agency_id": COUNCIL, "body_dropped": False})  # control

    def test_scan_membership_matches_build_glob_without_following_links(self):
        self.hidden_vote(hide=False)
        meetings = self.content / "meetings"
        valid = json.loads((meetings / "2026-10-cedar-council.json").read_text(encoding="utf-8"))
        for number, name in enumerate(("visible.json", ".hidden.json", "..double.json", ".json", "a.b.json"), 1):
            (meetings / name).write_text(json.dumps({**valid, "id": f"cedar-extra-{number}"}), encoding="utf-8")
        for name in (".meeting.json.tmp", "UPPER.JSON", "notes.txt", ".draft.json~"):
            (meetings / name).write_text("{ not a meeting", encoding="utf-8")
        (meetings / "nested").mkdir()
        (meetings / "nested" / "inner.json").write_text("{ not a meeting", encoding="utf-8")
        expected = sorted(p.name for p in meetings.glob("*.json"))
        for name in (".hidden.json", "..double.json", ".json", "visible.json"):
            self.assertIn(name, expected)
        for name in (".meeting.json.tmp", "UPPER.JSON", "notes.txt", "nested", "inner.json"):
            self.assertNotIn(name, expected)
        self.assertEqual(len(load_content(self.root, today=TODAY)["meetings"]), len(expected))
        with mock.patch.object(oc, "_read", wraps=oc._read) as spy:
            scanned = oc._meetings(self.root, TODAY)
        self.assertEqual([call.args[1] for call in spy.call_args_list], expected)
        self.assertEqual([stem + ".json" for stem, _ in scanned], expected)

    def test_hidden_vote_same_body_identity_and_unrelated_removal(self):
        voter = "Ash Example"
        self.hidden_vote(voter=voter)
        self.add("ash example", today=TODAY, checked_on="2026-10-01")  # exact, case-sensitive name matching
        before = self.meeting_state()
        self.refused(lambda: self.remove(voter), "content/meetings/" + self.HIDDEN)
        self.assertEqual(self.remove(voter, agency=POLICE), {"agency_id": POLICE, "body_dropped": False})
        self.assertEqual(self.remove("ash example"), {"agency_id": COUNCIL, "body_dropped": False})
        self.assertEqual(self.remove("Birch Example"), {"agency_id": COUNCIL, "body_dropped": False})
        self.assertEqual(self.names(), [(COUNCIL, voter), (POLICE, "Birch Example")])
        self.assertEqual(self.meeting_state(), before)  # hidden meeting bytes intact
        self.assertEqual([m["id"] for m in load_content(self.root, today=TODAY)["meetings"]], [MID])

    def test_hidden_candidate_disappears_after_enumeration_refuses(self):
        hidden = self.hidden_vote()
        roster = self.snap()
        real = oc._read

        def vanish(folder_fd, name, label):
            if name == self.HIDDEN:
                hidden.unlink()
            return real(folder_fd, name, label)
        with mock.patch.object(oc, "_read", side_effect=vanish), \
                mock.patch.object(oc, "_install", side_effect=AssertionError("must not install")):
            self.refused(lambda: self.remove("Birch Example"), "content/meetings/" + self.HIDDEN
                         + ": changed while it was being read")
        self.assertEqual(self.snap(), roster)

    def test_non_json_editor_artifacts_and_missing_meetings_remain_compatible(self):
        self.hidden_vote(hide=False)
        meetings = self.content / "meetings"
        for name in (".meeting.json.tmp", ".meeting.json.swp", "notes.txt", "UPPER.JSON"):
            (meetings / name).write_bytes(b"\xff{ not json")
        with mock.patch.object(oc, "_meetings", side_effect=AssertionError("no meeting scan")):
            self.add("Cedar Example", today=TODAY, checked_on="2026-10-01")
            oc.recheck_official(self.root, agency_id=COUNCIL, name="Ash Example", checked_on="2026-10-18", today=TODAY)
            oc.list_officials(self.root, today=TODAY)
        self.assertEqual(self.remove("Cedar Example"), {"agency_id": COUNCIL, "body_dropped": False})
        self.assertEqual(self.remove("Ash Example", agency=POLICE), {"agency_id": POLICE, "body_dropped": False})
        for path in meetings.iterdir():
            path.unlink()
        self.assertEqual(oc._meetings(self.root, TODAY), [])  # empty folder
        self.assertEqual(self.remove("Ash Example"), {"agency_id": COUNCIL, "body_dropped": False})
        meetings.rmdir()
        self.assertEqual(oc._meetings(self.root, TODAY), [])  # absent folder
        self.assertEqual(self.remove("Birch Example"), {"agency_id": COUNCIL, "body_dropped": True})
        self.assertEqual(stat.S_IMODE(os.lstat(self.file).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
