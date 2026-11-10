"""DF1109-12: officials add|recheck|remove|list maintain content/officials.json without hand-editing.

Temp copies of the fictional example, synthetic names and .example hosts only; nothing is fetched or built.
"""
import json
import os
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


if __name__ == "__main__":
    unittest.main()
