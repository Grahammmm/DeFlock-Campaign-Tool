"""DF0022-14: the read-only private personal-data register (``personal-data --directory D [--json]``).

Offline and synthetic: invented names, aliases, contacts and dates; any socket use fails the test.
Run from the repository root: python3 -m unittest tests.test_personal_data
"""
import ast
import contextlib
import errno
import io
import json
import os
import re
import stat
import sys
import tempfile
import unittest
import warnings
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, findings, personal_data, supporters  # noqa: E402
from tests.test_kit_check import ready_drafts  # noqa: E402

NOTICE = "Deletion is the organizer's decision. Nothing was changed."
ROW_KEYS = {"kind", "path", "purpose", "status", "record_count", "oldest_record_date", "newest_record_date"}
# The nine kinds, written out independently of the implementation's catalog.
KINDS = {
    "requester": ("kit/request-settings.json", "Requester contact block"),
    "reviewer_roster": ("private/reviewers.json", "Reviewer names and roles"),
    "review_assignments": ("private/review-assignments/*.json", "Finding review assignments"),
    "comments_sent": ("kit/comments-sent.json", "Manually submitted comment log"),
    "correction_requests": ("private/correction-requests.json", "Correction request summaries"),
    "contact_log": ("kit/sent.json", "Organizer contact notes"),
    "volunteer_assignments": ("kit/assignments.json", "Private request owners"),
    "supporters_consent": ("kit/supporters-consent.json", "Supporting organization consent"),
    "speaker_plans": ("kit/meetings/speakers/*.json", "Private speaker coverage"),
}
SENTINELS = ("SENTINEL-NAME-Q7", "sentinel-q7@example.invalid", "555-0147-Q7", "9 Q7 Synthetic Road", "SENTINEL-FREE-TEXT-Q7")
SHA = "c" * 64
STAMP = "2026-03-01T12:00:00+00:00"


def run(*arguments):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def put(root, rel, content, mode=0o600):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else json.dumps(content).encode("utf-8"))
    os.chmod(path, mode)
    return path


def tree(root):
    """Every path with kind, mode, mtime and bytes: any write, touch, chmod, new file or link shows."""
    return {str(p.relative_to(root)): (stat.S_IFMT(p.lstat().st_mode), p.lstat().st_mode, p.lstat().st_mtime_ns,
                                       p.read_bytes() if p.is_file() and not p.is_symlink() else None)
            for p in sorted(Path(root).rglob("*"))}


def valid_stores(root, text="x"):
    """Hand-written stores in the documented formats of the nine kinds; ``text`` is placed in every free-text field."""
    put(root, "kit/request-settings.json", {"schema_version": 1, "requester": [text, text]})
    put(root, "private/reviewers.json", {"schema_version": 1, "reviewers": [
        {"name": text, "roles": ["legal"], "added_on": "2026-01-05", "interest_note": text}]})
    put(root, "private/review-assignments/a.json", {"schema_version": 1, "assignments": {
        "legal": {"reviewer": text, "on": "2026-02-10", "recorded_at": STAMP}}})
    put(root, "kit/comments-sent.json", {"schema_version": 1, "comments": {
        "m1": {"on": "2026-02-01", "channel": "email", "recorded_at": STAMP, "letter_sha256": SHA, "letter_bytes": 9}}})
    put(root, "private/correction-requests.json", {"schema_version": 1, "requests": [
        {"id": 1, "finding_id": "f1", "received_on": "2026-02-02", "summary": text, "state": "open"}]})
    put(root, "kit/sent.json", {"schema_version": 1, "requests": {"agency-a": {
        "sent_on": "2026-02-01", "channel": "email", "recorded_at": STAMP, "steps": [],
        "contacts": [{"on": "2026-02-03", "channel": "phone", "role": text, "note": text, "recorded_at": STAMP}]}}})
    put(root, "kit/assignments.json", {"schema_version": 1, "assignments": {"agency-a": text}})
    put(root, "kit/supporters-consent.json", {"schema_version": 1, "supporters": {"org-a": {
        "organization": True, "confirmed_on": "2026-02-04", "how": "email", "public_sha256": SHA,
        "correspondence": SHA}}})
    put(root, "kit/meetings/speakers/m1.json", {"schema_version": 1, "meeting_id": "m1", "kit_sha256": SHA,
                                                 "assignments": [{"label": text, "point": "ask:1"}]})


class Base(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket", "urllib.request.urlopen"):
            patcher = mock.patch(target, side_effect=AssertionError("network used"))
            patcher.start()
            self.addCleanup(patcher.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-personal-data-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        self.root.mkdir()

    def register(self, root=None):
        return personal_data.personal_data_register(root or self.root)

    def rows(self, report=None):
        return {(row["kind"], row["path"]): row for row in (report or self.register())["files"]}

    def row(self, kind, report=None):
        found = [row for row in (report or self.register())["files"] if row["kind"] == kind]
        self.assertEqual(len(found), 1, kind)
        return found[0]


class RegisterTests(Base):
    def test_all_nine_writer_kinds_and_dates(self):
        root = self.root
        self.assertEqual(run("init", "--directory", root, "--name", "Synthetic", "--county", "San Luis Obispo",
                             "--state", "CA")[0], 0)
        self.assertEqual(run("kit", "--directory", root)[0], 0)
        ready_drafts(root)
        ids = [p.stem for p in sorted((root / "kit" / "requests").glob("*.md"))]
        for agency, day in ((ids[0], "2026-03-02"), (ids[1], "2026-03-03"), (ids[2], "2026-03-04")):
            self.assertEqual(run("kit-sent", "--directory", root, "--agency", agency, "--date", day,
                                 "--channel", "email")[0], 0)
        for agency, day in ((ids[0], "2026-03-09"), (ids[0], "2026-03-05"), (ids[1], "2026-03-07")):  # out of order
            self.assertEqual(run("kit-contact", "--directory", root, "--agency", agency, "--on", day, "--channel", "phone",
                                 "--with", "Records Clerk", "--note", "Asked about status")[0], 0)
        self.assertEqual(run("kit-assign", "--directory", root, "--agency", ids[0], "--to", "vol-one")[0], 0)
        self.assertEqual(run("kit-assign", "--directory", root, "--agency", ids[1], "--to", "vol-two")[0], 0)
        (root / "content").mkdir(exist_ok=True)
        findings.add_reviewer(root, "Synthetic Reviewer One", ["legal"], today=date(2026, 2, 20))
        findings.add_reviewer(root, "Synthetic Reviewer Two", ["factual"], today=date(2026, 1, 10))
        findings.add_reviewer(root, "Synthetic Reviewer Three", ["privacy"], today=date(2026, 2, 1))
        supporters.add_supporter(root, name="Synthetic Civic Group", kind="community", organization=True,
                                 confirmed_on="2026-02-01", how="email")
        supporters.add_supporter(root, name="Synthetic Faith Group", kind="faith", organization=True,
                                 confirmed_on="2026-01-15", how="letter")
        # Stores whose writers need a published finding or a staged meeting are written in their documented format.
        put(root, "kit/request-settings.json", {"schema_version": 1, "requester": ["Synthetic Requester"],
                                                "fee_cap_usd": 50})
        put(root, "private/review-assignments/f-b.json", {"schema_version": 1, "assignments": {
            "legal": {"reviewer": "Synthetic Reviewer One", "on": "2026-02-10", "recorded_at": STAMP},
            "factual": {"reviewer": "Synthetic Reviewer Two", "on": "2026-01-05", "recorded_at": STAMP}}})
        put(root, "private/review-assignments/f-a.json", {"schema_version": 1, "assignments": {
            "privacy": {"reviewer": "Synthetic Reviewer Three", "on": "2026-03-01", "recorded_at": STAMP}}})
        put(root, "kit/comments-sent.json", {"schema_version": 1, "comments": {
            "m2": {"on": "2026-03-08", "channel": "portal", "recorded_at": STAMP},
            "m1": {"on": "2026-02-08", "channel": "email", "recorded_at": STAMP, "letter_sha256": SHA, "letter_bytes": 3}}})
        put(root, "private/correction-requests.json", {"schema_version": 1, "requests": [
            {"id": 1, "finding_id": "f1", "received_on": "2026-03-06", "summary": "s", "state": "declined",
             "decided_on": "2026-03-30", "note": "n"},
            {"id": 2, "finding_id": "f1", "received_on": "2026-03-02", "summary": "s", "state": "open"}]})
        put(root, "kit/meetings/speakers/m1.json", {"schema_version": 1, "meeting_id": "m1", "kit_sha256": SHA,
                                                     "assignments": [{"label": "vol-one", "point": "ask:1"},
                                                                     {"label": "vol-one", "point": "ask:2"},
                                                                     {"label": "vol-two", "point": "ask:1"}]})
        put(root, "kit/meetings/speakers/m2.json", {"schema_version": 1, "meeting_id": "m2", "kit_sha256": SHA,
                                                     "assignments": []})
        report = self.register()
        self.assertEqual(report["schema_version"], 1)
        got = {(row["kind"], row["path"]): (row["status"], row["record_count"], row["oldest_record_date"],
                                            row["newest_record_date"]) for row in report["files"]
               if row["kind"] != "unknown"}
        self.assertEqual(got, {
            ("requester", "kit/request-settings.json"): ("present", 1, None, None),
            ("reviewer_roster", "private/reviewers.json"): ("present", 3, "2026-01-10", "2026-02-20"),
            ("review_assignments", "private/review-assignments/f-a.json"): ("present", 1, "2026-03-01", "2026-03-01"),
            ("review_assignments", "private/review-assignments/f-b.json"): ("present", 2, "2026-01-05", "2026-02-10"),
            ("comments_sent", "kit/comments-sent.json"): ("present", 2, "2026-02-08", "2026-03-08"),
            ("correction_requests", "private/correction-requests.json"): ("present", 2, "2026-03-02", "2026-03-06"),
            ("contact_log", "kit/sent.json"): ("present", 3, "2026-03-05", "2026-03-09"),
            ("volunteer_assignments", "kit/assignments.json"): ("present", 2, None, None),
            ("supporters_consent", "kit/supporters-consent.json"): ("present", 2, "2026-01-15", "2026-02-01"),
            ("speaker_plans", "kit/meetings/speakers/m1.json"): ("present", 3, None, None),
            ("speaker_plans", "kit/meetings/speakers/m2.json"): ("present", 0, None, None),
        })
        for row in report["files"]:
            self.assertEqual(set(row), ROW_KEYS)
            if row["kind"] != "unknown":
                self.assertEqual(row["purpose"], KINDS[row["kind"]][1])
        self.assertEqual([(r["kind"], r["path"]) for r in report["files"]],
                         sorted((r["kind"], r["path"]) for r in report["files"]))

    def test_undated_absent_empty_and_orphans(self):
        report = self.register()  # no kit/ and no private/ at all
        self.assertEqual({(r["kind"], r["path"]) for r in report["files"]}, {(k, v[0]) for k, v in KINDS.items()})
        for row in report["files"]:
            self.assertEqual((row["status"], row["record_count"], row["oldest_record_date"], row["newest_record_date"]),
                             ("absent", 0, None, None))
        self.assertEqual(list(self.root.iterdir()), [], "a missing kit/ or private/ is reported, never created")
        put(self.root, "kit/request-settings.json", {"schema_version": 1, "fee_cap_usd": 5})
        put(self.root, "kit/sent.json", {"schema_version": 1, "requests": {  # a legacy entry has no contacts at all
            "agency-a": {"sent_on": "2026-02-01", "channel": "email", "recorded_at": STAMP, "steps": []}}})
        put(self.root, "kit/assignments.json", {"schema_version": 1, "assignments": {"orphan-agency": "vol-one"}})
        put(self.root, "kit/supporters-consent.json", {"schema_version": 1, "supporters": {"orphan-org": {
            "organization": True, "confirmed_on": "2026-02-04", "how": "email", "public_sha256": SHA, "correspondence": None}}})
        put(self.root, "kit/meetings/speakers/gone.json", {"schema_version": 1, "meeting_id": "gone", "kit_sha256": SHA,
                                                            "assignments": [{"label": "vol-one", "point": "ask:1"}]})
        put(self.root, "private/reviewers.json", {"schema_version": 1, "reviewers": []})
        os.utime(self.root / "kit" / "assignments.json", (1, 1))
        report = self.register()
        self.assertEqual((self.row("requester", report)["status"], self.row("requester", report)["record_count"]),
                         ("present", 0))
        self.assertEqual(self.row("contact_log", report)["record_count"], 0)
        self.assertEqual(self.row("reviewer_roster", report)["record_count"], 0)
        for kind, count in (("volunteer_assignments", 1), ("supporters_consent", 1), ("speaker_plans", 1)):
            self.assertEqual(self.row(kind, report)["record_count"], count)
        self.assertEqual(self.row("volunteer_assignments", report)["oldest_record_date"], None)
        self.assertEqual(self.row("correction_requests", report)["status"], "absent")

    def test_unknown_files_are_never_opened(self):
        valid_stores(self.root)
        names = ["kit/blob.bin", "private/ledger.sqlite", "private/objects/ab/cd.eml", "kit/.hidden", "private/.lock",
                 "kit/requests/agency.md", "private/signers/allowed_signers", "kit/meetings/speakers/.tmp.json",
                 "kit/meetings/notes.txt", "private/review-assignments/draft.txt", "kit/line\nbreak.bin", "kit/caf\u00e9.bin"]
        for name in names:
            put(self.root, name, b"\x00\x01SENTINEL-NAME-Q7 binary")
        opened = []
        real_open = os.open

        def watch(path, flags, *args, **kwargs):
            opened.append(os.fspath(path))
            return real_open(path, flags, *args, **kwargs)
        with mock.patch.object(os, "open", side_effect=watch):
            report = self.register()
        unknown = [r for r in report["files"] if r["kind"] == "unknown"]
        self.assertEqual([r["path"] for r in unknown], sorted(names))
        for row in unknown:
            self.assertEqual((row["status"], row["purpose"], row["record_count"], row["oldest_record_date"],
                              row["newest_record_date"]), ("unknown_kind", "Unknown kind; review locally", None, None, None))
        for name in opened:
            self.assertFalse(name.endswith((".bin", ".sqlite", ".eml", ".md", ".txt", ".hidden", ".lock", ".tmp.json"))
                             or name == "allowed_signers", name)
        code, out, err = run("personal-data", "--directory", self.root)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("kit/line\\u000abreak.bin", out)
        self.assertIn("kit/caf\\u00e9.bin", out)
        self.assertEqual(len(out.splitlines()), len(report["files"]) + 2)
        code, out, err = run("personal-data", "--directory", self.root, "--json")
        self.assertIn("kit/line\\nbreak.bin", out)
        self.assertIn(next(r for r in json.loads(out)["files"] if "break" in r["path"])["path"], ["kit/line\nbreak.bin"])

    def test_malformed_inputs_have_no_partial_or_value_output(self):
        good = {"schema_version": 1}
        cases = {
            "requester": [("kit/request-settings.json", {"schema_version": 2, "requester": ["x"]}),
                          ("kit/request-settings.json", {"schema_version": 1, "requester": "x"}),
                          ("kit/request-settings.json", b'{"schema_version":1,"requester":["' + b"x" * 70000 + b'"]}')],
            "reviewer_roster": [("private/reviewers.json", {"schema_version": True, "reviewers": []}),
                                ("private/reviewers.json", {**good, "reviewers": [
                                    {"name": "n", "roles": ["legal"], "added_on": "2026-02-30"}]}),
                                ("private/reviewers.json", {**good, "reviewers": {}})],
            "review_assignments": [("private/review-assignments/a.json", {**good, "assignments": {
                "legal": {"reviewer": "r", "on": "2026-1-5", "recorded_at": STAMP}}})],
            "comments_sent": [("kit/comments-sent.json", b"\xff\xfe not utf-8"),
                              ("kit/comments-sent.json", {**good, "comments": {"m": {"on": "2026-01-01\n", "channel": "email",
                                                                                    "recorded_at": STAMP}}})],
            "correction_requests": [("private/correction-requests.json", b'{"schema_version":1,"requests":[],"requests":[]}'),
                                    ("private/correction-requests.json", {**good, "requests": [
                                        {"id": True, "finding_id": "f", "received_on": "2026-01-01", "summary": "s",
                                         "state": "open"}]})],
            "contact_log": [("kit/sent.json", {**good, "requests": {"a": {"contacts": [{"on": "soon"}]}}}),
                            ("kit/sent.json", {**good, "requests": {"a": {"contacts": "none"}}}),
                            ("kit/sent.json", b'{"schema_version":1,"requests":{},"x":NaN}')],
            "volunteer_assignments": [("kit/assignments.json", {**good, "assignments": {"a": 5}}),
                                      ("kit/assignments.json", {**good, "assignments": {}, "extra": 1})],
            "supporters_consent": [
                ("kit/supporters-consent.json", {**good, "supporters": {"o": {"confirmed_on": None}}}),
                ("kit/supporters-consent.json", {**good, "supporters": {"o": {"confirmed_on": "2026-02-04"}}}),
                ("kit/supporters-consent.json", {**good, "supporters": {"o": {
                    "organization": False, "confirmed_on": "2026-02-04", "how": "email", "public_sha256": SHA,
                    "correspondence": None}}}),
                ("kit/supporters-consent.json", {**good, "supporters": {"o": {
                    "organization": True, "confirmed_on": "2026-02-04", "how": 5, "public_sha256": SHA,
                    "correspondence": None}}}),
                ("kit/supporters-consent.json", {**good, "supporters": {"o": {
                    "organization": True, "confirmed_on": "2026-02-04", "how": "email", "public_sha256": "nope",
                    "correspondence": None}}}),
                ("kit/supporters-consent.json", {**good, "supporters": {"o": {
                    "organization": True, "confirmed_on": "2026-02-04", "how": "email", "public_sha256": SHA,
                    "correspondence": None, "cosign": "any"}}}),
            ],
            "speaker_plans": [("kit/meetings/speakers/m.json", {**good, "meeting_id": "m", "kit_sha256": SHA,
                                                                   "assignments": [{"label": "a"}]}),
                             *[("kit/meetings/speakers/m.json", {**good, "meeting_id": "m", "kit_sha256": SHA,
                                                                  "assignments": [item]})
                               for item in ({"label": 5, "point": "ask:1"}, {"label": "a", "point": None},
                                            {"label": "a", "point": ["x"]}, "a")],
                             ("kit/meetings/speakers/m.json", {**good, "meeting_id": 7, "kit_sha256": SHA,
                                                                "assignments": []}),
                             ("kit/meetings/speakers/m.json", {**good, "meeting_id": "m", "kit_sha256": "zz",
                                                                "assignments": []})],
        }
        for kind, variants in cases.items():
            for rel, content in variants:
                with self.subTest(kind=kind, rel=rel, content=str(content)[:60]):
                    root = self.tmp / f"case-{kind}-{abs(hash(str(content)))}"
                    root.mkdir()
                    put(root, rel, content)
                    put(root, "kit/assignments.json" if kind != "volunteer_assignments" else "kit/comments-sent.json",
                        {"schema_version": 1, ("assignments" if kind != "volunteer_assignments" else "comments"): {}})
                    code, out, err = run("personal-data", "--directory", root, "--json")
                    report = json.loads(out)
                    bad = [r for r in report["files"] if r["kind"] == kind and r["status"] == "unreadable"]
                    self.assertEqual(len(bad), 1)
                    self.assertEqual((bad[0]["record_count"], bad[0]["oldest_record_date"], bad[0]["newest_record_date"]),
                                     (None, None, None))
                    other = "volunteer_assignments" if kind != "volunteer_assignments" else "comments_sent"
                    self.assertEqual(self.row(other, report)["status"], "present")  # the safe rows still appear
                    self.assertEqual((code, err), (1, ""))
                    self.assertEqual(report["notice"], NOTICE)
        if os.geteuid() != 0:
            put(self.root, "kit/assignments.json", {"schema_version": 1, "assignments": {}}, mode=0)
            code, out, err = run("personal-data", "--directory", self.root, "--json")
            self.assertEqual((code, err), (1, ""))
            self.assertEqual(self.row("volunteer_assignments", json.loads(out))["status"], "unreadable")

    def test_safe_paths_and_bounded_enumeration(self):
        outside = self.tmp / "outside"
        put(outside, "kit/sent.json", {"schema_version": 1, "requests": {}})
        put(outside, "secret-dir/SENTINEL-NAME-Q7.txt", b"x")
        link_root = self.tmp / "link-root"
        link_root.symlink_to(self.root)
        (self.tmp / "real-parent").mkdir()
        (self.tmp / "real-parent" / "campaign").mkdir()
        (self.tmp / "ancestor").symlink_to(self.tmp / "real-parent")
        for target in (link_root, self.tmp / "ancestor" / "campaign"):
            self.assertEqual(run("personal-data", "--directory", target),
                             (1, "", "Stopped: personal-data requires real campaign directories\n"))
        (self.root / "kit").symlink_to(outside / "kit")
        self.assertEqual(run("personal-data", "--directory", self.root, "--json"),
                         (1, "", "Stopped: personal-data requires real campaign directories\n"))
        (self.root / "kit").unlink()
        (self.root / "private").symlink_to(outside)
        self.assertEqual(run("personal-data", "--directory", self.root)[0], 1)
        (self.root / "private").unlink()
        self.assertEqual(run("personal-data", "--directory", self.tmp / "missing"),
                         (1, "", "Stopped: personal-data directory does not exist\n"))
        # Links, hard links, FIFOs, directories and dangling links inside the folders.
        kit = self.root / "kit"
        put(self.root, "kit/assignments.json", {"schema_version": 1, "assignments": {}})
        os.link(kit / "assignments.json", kit / "copy.json")
        (kit / "sent.json").symlink_to(outside / "kit" / "sent.json")
        os.mkfifo(kit / "comments-sent.json")
        os.mkfifo(kit / "pipe.bin")
        (kit / "supporters-consent.json").mkdir()
        (kit / "dangling").symlink_to(self.tmp / "nowhere")
        (kit / "linkdir").symlink_to(outside / "secret-dir")
        put(self.root, "private/reviewers.json", {"schema_version": 1, "reviewers": []})
        report = self.register()
        by = {(r["kind"], r["path"]): r["status"] for r in report["files"]}
        self.assertEqual(by[("volunteer_assignments", "kit/assignments.json")], "unreadable")  # two links
        self.assertEqual(by[("contact_log", "kit/sent.json")], "unreadable")  # a symlink to a readable store
        self.assertEqual(by[("comments_sent", "kit/comments-sent.json")], "unreadable")  # a FIFO, never blocked on
        self.assertEqual(by[("supporters_consent", "kit/supporters-consent.json")], "unreadable")  # a directory
        self.assertEqual(by[("reviewer_roster", "private/reviewers.json")], "present")
        for name in ("copy.json", "pipe.bin", "dangling", "linkdir"):
            self.assertEqual(by[("unknown", "kit/" + name)], "unknown_kind")
        self.assertNotIn("SENTINEL-NAME-Q7", json.dumps(report))  # an unknown directory link is not traversed
        # A store swapped for a link between listing and reading is not followed.
        real_open, swapped = os.open, []

        def swap(path, flags, *args, **kwargs):
            if path == "reviewers.json" and "dir_fd" in kwargs and not swapped:
                os.unlink(path, dir_fd=kwargs["dir_fd"])
                os.symlink(str(outside / "kit" / "sent.json"), path, dir_fd=kwargs["dir_fd"])
                swapped.append(path)
            return real_open(path, flags, *args, **kwargs)
        with mock.patch.object(os, "open", side_effect=swap):
            self.assertEqual(self.by_status("reviewer_roster"), "unreadable")
        self.assertEqual(swapped, ["reviewers.json"])
        # An enumeration error and the entry cap refuse the whole report: no stdout, one fixed message.
        incomplete = "Stopped: personal-data inventory incomplete; review private folders locally\n"
        with mock.patch.object(os, "scandir", side_effect=PermissionError(errno.EACCES, "denied")):
            self.assertEqual(run("personal-data", "--directory", self.root), (1, "", incomplete))
        with mock.patch.object(personal_data, "MAX_ENTRIES", 3):
            self.assertEqual(run("personal-data", "--directory", self.root, "--json"), (1, "", incomplete))

    def by_status(self, kind):
        return self.row(kind)["status"]

    def test_json_contract_text_notice_and_cli(self):
        valid_stores(self.root)
        put(self.root, "kit/zz.bin", b"x")
        code, out, err = run("personal-data", "--directory", self.root, "--json")
        self.assertEqual((code, err, out.count("\n"), out.endswith("\n")), (0, "", 1, True))
        report = json.loads(out)
        self.assertEqual(set(report), {"schema_version", "files", "notice"})
        self.assertEqual((report["schema_version"], report["notice"]), (1, NOTICE))
        self.assertTrue(all(set(row) == ROW_KEYS for row in report["files"]))
        self.assertTrue(out.isascii())
        code, text, err = run("personal-data", "--directory", self.root)
        self.assertEqual((code, err), (0, ""))
        lines = text.splitlines()
        self.assertEqual(lines[0].split("\t"), ["kind", "path", "purpose", "status", "record_count",
                                                "oldest_record_date", "newest_record_date"])
        self.assertEqual(lines[-1], NOTICE)
        self.assertEqual(text.count(NOTICE), 1)
        self.assertEqual(len(lines), len(report["files"]) + 2)
        for line, row in zip(lines[1:-1], report["files"]):
            self.assertEqual(line.split("\t"), ["-" if row[key] is None else str(row[key]) for key in (
                "kind", "path", "purpose", "status", "record_count", "oldest_record_date", "newest_record_date")])
        self.assertNotRegex(text.replace(NOTICE, ""), r"(?i)delet")  # no other wording about deleting
        put(self.root, "kit/sent.json", b"not json")
        self.assertEqual(run("personal-data", "--directory", self.root)[0], 1)
        for bad in ((), ("--directory",), ("--directory", self.root, "--delete"), ("--directory", self.root, "extra"),
                    ("--directory", self.root, "--json", "--fix"), ("--directory", self.root, "--out", "x")):
            with self.subTest(bad=bad):
                self.assertEqual(run("personal-data", *bad)[0], 2)

    def test_no_values_or_mutations(self):
        valid_stores(self.root, text=" ".join(SENTINELS))
        put(self.root, "kit/" + SENTINELS[0] + ".txt", b"free text SENTINEL-FREE-TEXT-Q7")
        os.utime(self.root / "kit" / "sent.json", (1_000_000_000, 1_000_000_000))
        before = tree(self.root)
        for extra in ((), ("--json",)):
            code, out, err = run("personal-data", "--directory", self.root, *extra)
            self.assertEqual((code, err), (0, ""))
            for sentinel in (*SENTINELS, SHA, str(self.root)):
                self.assertNotIn(sentinel, out.replace(SENTINELS[0] + ".txt", ""))
            self.assertEqual(before, tree(self.root))
        missing = self.tmp / "never-made"
        self.assertEqual(run("personal-data", "--directory", missing)[0], 1)
        self.assertFalse(missing.exists())
        self.assertEqual(run("personal-data", "--directory", self.root, "--json")[1].count(str(self.root)), 0)


# --- the writer inventory --------------------------------------------------------------------------------------

_WRITE_FLAGS = {"O_WRONLY", "O_RDWR", "O_CREAT", "O_APPEND", "O_TRUNC"}


def _write_kind(call):
    f = call.func
    name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
    base = f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else ""
    if name in ("write_text", "write_bytes", "mkstemp", "NamedTemporaryFile") or base == "shutil":
        return name
    if base == "os" and name in ("replace", "rename", "link", "symlink"):
        return "os." + name
    if re.match(r"_?(write|atomic)", name):
        return "helper"
    if name == "open":
        modes = [a.value for a in call.args[1:2] + [k.value for k in call.keywords if k.arg == "mode"]
                 if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        flags = {n.attr for a in call.args[1:2] + [k.value for k in call.keywords if k.arg == "flags"]
                 for n in ast.walk(a) if isinstance(n, ast.Attribute)}
        if any(set(m) & set("wax+") for m in modes) or flags & _WRITE_FLAGS:
            return "open-write"
    return None


def detect_writers(package):
    """``{module: {function identity, ...}}`` for every function in campaign_tool that calls a write-capable operation."""
    found = {}

    def visit(node, qual, module):
        for child in ast.iter_child_nodes(node):
            inner = qual + [child.name] if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else qual
            if isinstance(child, ast.Call) and qual and _write_kind(child):
                found.setdefault(module, set()).add(".".join(qual))
            visit(child, inner, module)
    for path in sorted(Path(package).rglob("*.py")):
        if "tests" in path.parts:
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)  # an old regex string in the source is not this test's concern
            parsed = ast.parse(path.read_text(encoding="utf-8"))
        visit(parsed, [], ".".join(path.relative_to(package).with_suffix("").parts))
    return found


# Writers of the nine kinds, by function identity, each tied to its catalog kind.
CATALOG_WRITERS = {
    "findings.add_reviewer": "reviewer_roster", "findings.assign": "review_assignments",
    "meeting_comments.record": "comments_sent",
    "correction_requests.add": "correction_requests", "correction_requests.decide": "correction_requests",
    "kit_track.record_sent": "contact_log", "kit_track.record_step": "contact_log",
    "kit_track.record_contact": "contact_log", "kit_undo.undo_step": "contact_log",
    "kit_import_sent.import_sent": "contact_log", "kit_import_steps._write": "contact_log",
    "kit_import_steps.import_steps": "contact_log",
    "kit_assign.assign": "volunteer_assignments",
    "supporters._install_pair": "supporters_consent", "supporters._install_private": "supporters_consent",
    "supporters._put": "supporters_consent", "supporters._temporary": "supporters_consent",
    "meeting_speakers._write": "speaker_plans", "meeting_speakers.update_plan": "speaker_plans",
}
# The one kind with no writer: the organizer types it. The literal must stay in kit.py, where it is read.
ORGANIZER_AUTHORED = {"requester": ("kit", "request-settings.json")}
# Where each kind's file name or folder literal lives, so a renamed path fails here even if the writer is unchanged.
PATH_LITERALS = {
    "requester": ("kit", "request-settings.json"), "reviewer_roster": ("findings", "reviewers.json"),
    "review_assignments": ("findings", "review-assignments"), "comments_sent": ("meeting_comments", "comments-sent.json"),
    "correction_requests": ("correction_requests", "correction-requests.json"), "contact_log": ("kit_track", "sent.json"),
    "volunteer_assignments": ("kit_assign", "assignments.json"),
    "supporters_consent": ("supporters", "supporters-consent.json"), "speaker_plans": ("meeting_speakers", "speakers"),
}
# Writers in the same modules that are NOT one of the nine kinds; they are still listed as unknown at runtime.
_FINDING = "finding drafts, receipts, logs and locks: private, no catalog store, listed unknown at runtime"
FUNCTION_UNKNOWN = {
    **{f"findings.{name}": _FINDING for name in (
        "_append", "_correct_locked", "_create_text", "_ensure_campaign_id", "_locked", "_open_log", "_publish_locked",
        "_update_locked", "_verifies", "_withdraw_locked", "_write_draft", "_write_logged", "add_receipt",
        "bind_receipts", "new_draft", "new_gap_draft", "record_reply", "reply_request")},
    "findings._write_private": "shared private-file helper; each catalog caller is pinned above",
    **{f"kit.{name}": "generated drafts and the agency list; request-settings.json has no writer"
       for name in ("_write_json", "_write_readme", "_write_text", "build_kit", "verify_agencies_csv", "verify_agency")},
    "correction_requests.reply": "unsent reply draft: ids, dates and the recorded note, no requester contact; listed unknown at runtime",
    **{f"kit_track.{name}": "unsent draft text, no personal-data store" for name in (
        "denial_reply", "followup", "format_coverage")},
}
FUNCTION_LEVEL_MODULES = {"findings", "kit", "kit_track", "kit_assign", "kit_undo", "kit_import_sent", "kit_import_steps",
                          "meeting_comments", "correction_requests", "supporters", "meeting_speakers"}
_MODULES = {
    "records pipeline: ledger, objects, review queues, runner state; listed unknown at runtime": """
        records.agency_reconciliation records.bounded_resume records.catalog records.catalog_links
        records.catalog_stage records.contained_resume records.container_host records.export_clearance
        records.extract.ocr records.extract.vision records.extraction_ledger records.extraction_routes
        records.gates.batch_report records.gates.collect_reviews records.gates.reconcile_coverage
        records.gates.safe_output records.gates.validate_findings records.intake.document_drop
        records.intake.eml_export records.intake.folder records.intake.local_capture records.intake.mail_delta
        records.intake.mail_wire records.intake.repair_intake records.intake.scope_quarantine
        records.ledger.reconcile records.ledger.store records.ledger_catalog records.ledger_catalog_acceptance
        records.legacy_adoption records.mail_adoption records.mail_poll records.mail_resolution_census
        records.native_extract records.ocr_closure records.ocr_review records.ocr_stage
        records.outcomes.comparison records.outcomes.factsheets records.outcomes.reading records.outcomes.register
        records.outcomes.textstate records.portal.download records.portal.engine records.portal.ledger_adapter
        records.portal.store records.publish records.queue_runtime records.recovery.backup
        records.recovery.backup_v2 records.recovery.publication records.review_bundle records.review_retirement
        records.run records.run_orchestrator records.run_stages records.runner.canonical_mail records.runner.core
        records.runner.export_proof records.runtime records.schedule records.systemd_control records.unattended
        records.understanding.cli records.understanding.digest records.worker_lifecycle""",
    "backup archives and keys": "backup backup_crypt",
    "generated drafts, exports and public content, no catalog store": """
        agency_brief content_init content_sharing coverage flyer kit_calendar kit_copy kit_export kit_fees kit_preserve kit_timing
        map_survey meeting_outcome meeting_pack meeting_stage meetings newsletter.__main__ newsletter.archive
        newsletter.draft newsletter.sent outbox outbox_batch press recent search_log site site_check social social_threads""",
    "other local files with no catalog store": """
        agency_actions agency_brief agency_cards approval cli finding_packet image_meta import_existing kit_agencies kit_asked
        finding_search_log kit_fee_reply kit_preserve kit_repeat search_log law law_packet map_controller registry setup_cmd shell sources_cmd""",
}
MODULE_UNKNOWN = {module: reason for reason, names in _MODULES.items() for module in names.split()}

# Every write-capable function of a module with no catalog store, by identity; a new or renamed one fails until it is listed here.
MODULE_FUNCTIONS = {
    "map_survey": ("_install_new", "_write_checks", "render_csv", "survey_import"),
    'agency_brief': ('_write_file', 'build_brief',),  # private kit/briefs html+txt from published content; kept unless --replace
    'agency_actions': ('_install',),
    'agency_brief': ('_write_file', 'build_brief',),
    'agency_cards': ('main',),
    'approval': ('verify',),
    'backup': (
        '_RestoreTarget.write_file', '_publish_archive', '_restore_tar', '_verify_tar', 'export',
        'export.write_tar', 'export_hosted', 'export_into'
    ),
    'backup_crypt': ('decrypted', 'encrypt', 'gpg_path',),
    'cli': (
        '_meetings_multi', '_store_bytes', 'backup_drill_cmd', 'finding_preview', 'initialize', 'main', 'meetings_cmd',
        'readiness', 'weight_budget'
    ),
    'content_init': ('_rename_noreplace', '_write', 'faq_document', 'init_content', 'init_from',),
    'content_sharing': ('import_sharing',),
    'coverage': ('_write', 'add_coverage', 'remove_coverage',),
    'finding_packet': ('_copy_hash', '_create', 'build_packet',),
    'flyer': ('build',),
    'image_meta': ('strip_file',),
    'import_existing': ('_write_receipt', 'run',),
    'kit_agencies': ('_save',),
    'kit_asked': ('run_cli',),
    'kit_calendar': ('calendar',),
    'kit_copy': ('_mail', '_write', 'copy',),
    'kit_export': ('status_csv', 'write_rows',),
    'kit_fees': ('write_csv',),
    'agency_brief': ('_write_file', 'build_brief',),
    'kit_fee_reply': ('_write', 'fee_reply',),
    'kit_preserve': ('preserve_request',),
    'search_log': ('run_cli',),
    'finding_search_log': ('draft_from_summary',),  # private finding draft via findings._write_draft; never published
    'kit_repeat': ('_replace_store', 'repeat_request',),
    'kit_preserve': ('preserve_request',),  # private kit/preservation draft, O_EXCL, 0600, no store
    'kit_timing': ('write_csv',),
    'law': (
        '_apply_sheet_output', '_review_sheet_output', '_write_private', 'main', 'write_review_sheet',
        'write_sheet'
    ),
    'law_packet': ('build',),
    'map_controller': ('main',),
    "map_survey": ('_install_new', '_write_checks', 'render_csv', 'survey_import',),
    'meeting_outcome': ('record_comment_counts', 'record_direction', 'record_minutes', 'record_outcome',),
    'meeting_pack': ('_commit_pack', '_discard', '_write_member', 'build',),
    'meeting_stage': ('stage',),
    'meetings': ('write_matters', 'write_meetings',),
    'newsletter.__main__': ('main',),
    'newsletter.archive': ('publish_issue',),
    'newsletter.draft': ('write_draft',),
    'newsletter.sent': ('record_sent',),
    'outbox': ('main',),
    'outbox_batch': ('_write', 'export',),
    'press': ('build_letter', 'build_press',),
    'recent': ('_write_new', 'run_cli',),
    'records.agency_reconciliation': ('run',),
    'records.bounded_resume': ('_worker',),
    'records.catalog': ('capture_file', 'import_catalog',),
    'records.catalog_links': ('_lock', '_write_new', 'run',),
    'records.catalog_stage': ('CatalogAdapter.preserve_card',),
    'records.contained_resume': ('run_contained',),
    'records.container_host': (
        'Journal._change', 'Journal.allocate', 'PrivateDir.write', 'admit', 'build_board', 'entry',
        'postrun_entry'
    ),
    'records.export_clearance': ('clearance',),
    'records.extract.ocr': (
        '_hold', '_index', '_link_new', '_page_lock', '_prune_unrecorded', '_publish_blocked_disposition',
        '_reconcile_page', '_write_new', 'extract_image_only_pages'
    ),
    'records.extract.vision': ('run_fallback',),
    'records.extraction_ledger': ('store_blob',),
    'records.extraction_routes': ('_child', '_run_image_ocr', '_run_page_ocr', '_write', 'extract',),
    'records.gates.batch_report': ('main',),
    'records.gates.collect_reviews': ('main',),
    'records.gates.reconcile_coverage': ('main',),
    'records.gates.safe_output': ('write_private',),
    'records.gates.validate_findings': ('main',),
    'records.intake.document_drop': ('capture',),
    'records.intake.eml_export': ('_ensure_private', '_write_private', 'export_message',),
    'records.intake.folder': (
        'atomic', 'extract', 'inventory', 'inventory.record', 'report', 'seal_blob', 'worker', 'worker.child',
        'worker.emit'
    ),
    'records.intake.local_capture': ('_store', 'capture',),
    'records.intake.mail_delta': ('import_receipt', 'stage_source',),
    'records.intake.mail_wire': ('store',),
    'records.intake.repair_intake': ('run',),
    'records.intake.scope_quarantine': ('quarantine',),
    'records.ledger.reconcile': ('main',),
    'records.ledger.store': ('capture_database', 'initialize',),
    'records.ledger_catalog': ('_write_new', 'export',),
    'records.ledger_catalog_acceptance': ('_connect', '_locked',),
    'records.legacy_adoption': ('apply_batch', 'main', 'write_proposal',),
    'records.mail_adoption': ('_snapshot_db',),
    'records.mail_poll': ('_write_once', 'poll',),
    'records.mail_resolution_census': ('inventory',),
    'records.native_extract': ('_write_private', 'run', 'write_awaiting_ocr', 'write_private_list',),
    'records.ocr_closure': ('load_cas_page', 'load_cas_vision',),
    'records.ocr_review': ('export', 'mark',),
    'records.ocr_stage': (
        'OcrStage._publish', 'OcrStage.locked', 'OcrStage.process', '_copy_verified', '_publish_noreplace',
        '_write_private', 'main', 'transcribe_png_main'
    ),
    'records.outcomes.comparison': ('write_run_comparison',),
    'records.outcomes.factsheets': ('write_run_factsheets',),
    'records.outcomes.reading': ('main',),
    'records.outcomes.register': ('write_for_run',),
    'records.outcomes.textstate': ('write_run_textstate',),
    'records.portal.download': ('processing_lock',),
    'records.portal.engine': ('_download',),
    'records.portal.ledger_adapter': ('WP1LedgerAdapter._copy_cas',),
    'records.portal.store': ('Queue.__init__', 'Queue.lock', 'Queue.promote',),
    'records.publish': ('_apply', '_approve_locked', '_recover_proposals', '_restore', '_write', 'reopen',),
    'records.queue_runtime': (
        'QueueRuntime.__init__', 'QueueRuntime._lock', '_prepare_child', '_publish', '_write_new'
    ),
    'records.recovery.backup': ('backup', 'copy_exact', 'restore', 'snapshot_ledger', 'write_new',),
    'records.recovery.backup_v2': ('_snapshot_quiescent', 'backup_v2', 'restore_v2',),
    'records.recovery.publication': (
        'PublicationOutbox.__init__', 'PublicationOutbox.execute', 'PublicationOutbox.lock',
        'atomic_public_artifact'
    ),
    'records.review_bundle': ('_locked', '_new',),
    'records.review_retirement': ('arm', 'finish', 'start',),
    'records.run': (
        'Pipeline._record_proposal', 'Pipeline._run_locked', 'Pipeline._run_locked_unchecked',
        'Pipeline.stage_catalog', 'root_lock'
    ),
    'records.run_orchestrator': (
        'atomic_write_json', 'cmd_abort', 'cmd_notify_fallback', 'execute', 'record_understanding_alerts',
        'write_notify_alert'
    ),
    'records.run_stages': ('backup_adoption_dbs', 'stage_outputs',),
    'records.runner.canonical_mail': ('CanonicalMailBackend._capture_verification',),
    'records.runner.core': ('journal', 'run', 'writer_lock',),
    'records.runner.export_proof': ('ExporterWrapper.prepare', 'export_lock',),
    'records.runtime': ('check',),
    'records.schedule': ('render',),
    'records.systemd_control': ('_begin_locked', '_fence', '_prepare', 'stop_post',),
    'records.unattended': ('UnattendedPipeline._save_advance_resume', '_private_bytes',),
    'records.understanding.cli': ('_write', 'run',),
    'records.understanding.digest': ('commit_digest',),
    'records.worker_lifecycle': ('_Directory.write', 'supervise',),
    'registry': ('_store', '_transaction',),
    'search_log': ('run_cli',),  # prints the report to stdout only; the CSV is read, nothing is written
    'setup_cmd': ('ask', 'ask.prompt',),
    'shell': ('main',),
    'site': ('_copy', '_csv_text', '_write', 'build_site', 'build_starter',),
    'site_check': ('write_weights',),
    'social': ('_write_drafts', 'build_meeting_social', 'build_outcome_social', 'build_social',),
    'social_threads': ('build_thread_social',),
    'sources_cmd': ('_batch_write', '_rewrite', '_write_atomic', 'add_source',),
}


def inventory_problems(detected, module_functions=None):
    """What is not pinned: a writer function the constants above do not classify by identity, a module with no
    classification reason, or a pin that is stale."""
    module_functions = MODULE_FUNCTIONS if module_functions is None else module_functions
    problems = []
    for module, functions in sorted(detected.items()):
        if module not in FUNCTION_LEVEL_MODULES and module not in MODULE_UNKNOWN:
            problems.append(f"unclassified writer module {module}")
        for function in sorted(functions):
            name = f"{module}.{function}"
            if name in CATALOG_WRITERS or name in FUNCTION_UNKNOWN or function in module_functions.get(module, ()):
                continue
            problems.append(f"unclassified writer {name}")
    pinned = {f"{m}.{f}" for m, fs in detected.items() for f in fs}
    listed = {f"{m}.{f}" for m, fs in module_functions.items() for f in fs}
    problems += [f"stale pin {name}" for name in sorted((set(CATALOG_WRITERS) | set(FUNCTION_UNKNOWN) | listed) - pinned)]
    problems += [f"stale module pin {m}" for m in sorted(set(MODULE_UNKNOWN) - set(detected))]
    problems += [f"module pinned twice {m}" for m in sorted(set(module_functions) & FUNCTION_LEVEL_MODULES)]
    return problems


class WriterInventoryTests(unittest.TestCase):
    package = REPO_ROOT / "campaign_tool"

    def test_writer_inventory_cannot_silently_grow(self):
        detected = detect_writers(self.package)
        self.assertEqual(inventory_problems(detected), [])
        # Every pinned writer maps to a real kind, every kind has a writer or is organizer-authored, none is both.
        self.assertEqual(set(CATALOG_WRITERS.values()) | set(ORGANIZER_AUTHORED), set(KINDS))
        self.assertEqual(set(CATALOG_WRITERS.values()) & set(ORGANIZER_AUTHORED), set())
        # The register catalog is exactly the pinned nine kinds, paths and purposes.
        self.assertEqual({k: (path, purpose) for k, path, purpose, _a, _c in personal_data.CATALOG}, KINDS)
        # Each kind's path literal still exists in the module that owns it.
        for kind, (module, literal) in PATH_LITERALS.items():
            tree_ = ast.parse((self.package / (module + ".py")).read_text(encoding="utf-8"))
            literals = {n.value for n in ast.walk(tree_) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
            self.assertTrue(any(literal in value for value in literals), f"{kind}: {literal!r} not in {module}.py")
        # Synthetic additions fail the same check (it is not a registry-only comparison).
        self.assertEqual(inventory_problems({**detected, "kit_brand_new": {"save"}}),
                         ["unclassified writer module kit_brand_new",
                          "unclassified writer kit_brand_new.save"])
        self.assertEqual(inventory_problems({**detected, "kit_track": detected["kit_track"] | {"record_note"}}),
                         ["unclassified writer kit_track.record_note"])
        # A new or renamed writer inside an already classified module (here site, a module-level unknown) fails too.
        self.assertIn("site", MODULE_FUNCTIONS)
        grown = {**detected, "site": detected["site"] | {"brand_new_save"}}
        self.assertEqual(inventory_problems(grown), ["unclassified writer site.brand_new_save"])
        some = sorted(detected["site"])[0]
        moved = {**detected, "site": (detected["site"] - {some}) | {some + "_renamed"}}
        self.assertEqual(inventory_problems(moved),
                         [f"unclassified writer site.{some}_renamed", f"stale pin site.{some}"])
        self.assertEqual(inventory_problems(detected, {**MODULE_FUNCTIONS, "kit": ("x",)}),
                         ["stale pin kit.x", "module pinned twice kit"])
        renamed = {**detected, "kit_assign": {"assign_owner"}}
        self.assertEqual(inventory_problems(renamed),
                         ["unclassified writer kit_assign.assign_owner", "stale pin kit_assign.assign"])

    def test_detector_sees_a_new_writer_in_source(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-writers-") as tmp:
            (Path(tmp) / "newmod.py").write_text(
                "import os\n\ndef save(p):\n    with open(p, 'w') as f:\n        f.write('x')\n\n"
                "def keep(p):\n    os.replace(p, p + '.old')\n\ndef read(p):\n    return open(p).read()\n", encoding="utf-8")
            self.assertEqual(detect_writers(tmp), {"newmod": {"save", "keep"}})


if __name__ == "__main__":
    unittest.main()
