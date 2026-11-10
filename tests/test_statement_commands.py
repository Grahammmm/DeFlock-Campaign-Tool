"""DF1109-13: statement add|remove|list maintain content/statements.json without hand-editing.

Temp copies of the fictional example, synthetic names and .example hosts only; nothing is fetched or built.
"""
import json
import os
import shutil
import socket
import stat
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from campaign_tool import statements as st
from campaign_tool.content import ContentError
from campaign_tool.content_statements import read_statements

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 10, 20)
COUNCIL, POLICE = "ca-cedar-city-council", "ca-cedar-police"
SRC = "https://cedar.example/minutes"
PAYLOAD = "Unmistakable Payload"


def person(name, checked_on="2026-09-01"):
    return {"name": name, "role": "Council member", "source_url": "https://cedar.example/council", "checked_on": checked_on}


def row(quote="We will review the contract.", said_on="2026-10-01", agency=COUNCIL, official="Ash Example", **extra):
    return {"agency_id": agency, "official": official, "said_on": said_on, "quote": quote, "context": "meeting",
            "source_url": SRC, "checked_on": "2026-10-10", **extra}


class StatementCommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-statement-cmd-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content = self.root / "content"
        self.file = self.content / "statements.json"
        self.roster([{"agency_id": COUNCIL, "officials": [person("Ash Example")]},
                     {"agency_id": POLICE, "officials": [person("Birch Example")]}])

    def roster(self, bodies):
        (self.content / "officials.json").write_text(json.dumps({"schema_version": 1, "bodies": bodies}), encoding="utf-8")

    def quote(self, text, name="quote.txt"):
        path = self.tmp / name
        path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
        return path

    def add(self, quote="We will review the contract.", said_on="2026-10-01", agency=COUNCIL, official="Ash Example",
            today=TODAY, **extra):
        values = {"context": "meeting", "source_url": SRC, "checked_on": "2026-10-10", **extra}
        path = quote if isinstance(quote, Path) else self.quote(quote)
        return st.add_statement(self.root, agency_id=agency, official=official, said_on=said_on, quote_file=path,
                                today=today, **values)

    def store(self, rows):
        self.file.write_text(json.dumps(rows), encoding="utf-8")

    def stored(self):
        return json.loads(self.file.read_text(encoding="utf-8"))

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): (p.read_bytes() if p.is_file() else None,
                                                      stat.S_IMODE(os.lstat(p).st_mode))
                for p in sorted(self.root.rglob("*"))}

    def temps(self):
        return sorted(p.name for p in self.content.iterdir() if p.name.startswith(".statements-"))

    def refused(self, call, needle, **kwargs):
        before = self.snapshot()
        with self.assertRaises(ContentError) as caught:
            call(**kwargs)
        message = str(caught.exception)
        self.assertIsInstance(caught.exception, st.StatementError)
        self.assertIn(needle, message)
        for secret in (PAYLOAD, "jane.doe", "555-0142", "7ABC123", SRC):
            self.assertNotIn(secret, message)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.temps(), [])
        return message

    def test_first_add_requires_rostered_official_and_context(self):
        (self.content / "officials.json").unlink()
        self.refused(self.add, "officials.json: does not exist")
        self.roster([{"agency_id": COUNCIL, "officials": [person("Ash Example")]}])
        self.refused(self.add, "--agency-id: no officials listed", agency=POLICE, official="Ash Example")
        for name in ("ash example", "Ash  Example", " Ash Example"):
            self.refused(self.add, "--official: not listed", official=name)
        self.refused(self.add, "--context: choose one of", context="tweet")
        for checked, ok in ((TODAY - timedelta(days=366), False), (TODAY + timedelta(days=1), False),
                            (TODAY - timedelta(days=365), True)):
            self.roster([{"agency_id": COUNCIL, "officials": [person("Ash Example", checked.isoformat())]}])
            if ok:
                self.assertEqual(self.add(), {"action": "added", "index": 1})
            else:
                self.assertFalse(self.file.exists())
                self.assertIn("officials.json.bodies[0].officials[0].checked_on", self.refused(self.add, "officials.json"))
        self.assertEqual(stat.S_IMODE(os.lstat(self.file).st_mode), 0o644)
        self.assertEqual(self.stored(), [row()])
        self.assertEqual(self.file.read_bytes(), (json.dumps([row()], indent=2, ensure_ascii=False) + "\n").encode())
        self.assertEqual(self.add("Second.", recording_time="1:02:45", context_note="Campaign note."),
                         {"action": "added", "index": 2})
        self.assertEqual(self.stored()[1], row("Second.", recording_time="1:02:45", context_note="Campaign note."))

    def test_quote_file_round_trips_exactly(self):
        exact = ["  Two  spaces,  kept.  ", "Cafe\u0301 decomposed", "Caf\u00e9 composed", "x" * 400, "<b>not markup</b>"]
        for number, text in enumerate(exact, 1):
            self.assertEqual(self.add(text)["index"], number)
        self.assertEqual([r["quote"] for r in self.stored()], exact)
        self.assertEqual([r["quote"] for r in read_statements(self.root, None, TODAY)], exact)
        for text in (PAYLOAD + "\n", PAYLOAD + "\r\n", PAYLOAD + "\r", "line one\nline two " + PAYLOAD):
            self.refused(self.add, "--quote-file: must be plain text without control characters", quote=self.quote(text))
        self.refused(self.add, "--quote-file: must be 1 to 400", quote=self.quote(""))
        self.refused(self.add, "--quote-file: must be 1 to 400", quote=self.quote("   "))
        self.refused(self.add, "--quote-file: must be 1 to 400", quote=self.quote(PAYLOAD + "y" * 401))
        self.refused(self.add, "--quote-file: not valid UTF-8", quote=self.quote(PAYLOAD.encode() + b"\xff\xfe"))
        self.refused(self.add, "--quote-file: file too large", quote=self.quote(PAYLOAD * 300))
        (self.tmp / "link.txt").symlink_to(self.quote(PAYLOAD))
        self.refused(self.add, "--quote-file: is a symbolic link", quote=self.tmp / "link.txt")
        os.mkfifo(self.tmp / "fifo")
        self.refused(self.add, "--quote-file: must be a regular file", quote=self.tmp / "fifo")
        self.refused(self.add, "--quote-file: must be a regular file", quote=self.tmp)
        self.refused(self.add, "--quote-file: cannot be read", quote=self.tmp / "missing.txt")

    def test_whole_store_and_privacy_refusal_preserve_bytes(self):
        bad_stores = {b"\xff" + PAYLOAD.encode(): "not valid UTF-8 JSON", PAYLOAD.encode(): "not valid UTF-8 JSON",
                      b"null": "must be a list", b"{}": "must be a list",
                      json.dumps([row(PAYLOAD, extra=PAYLOAD)]).encode(): "statements.json[0]: unknown field",
                      json.dumps([row(PAYLOAD, said_on="2026-12-01")]).encode(): "statements.json[0].said_on",
                      json.dumps([row(PAYLOAD), row(PAYLOAD, said_on="2026-10-11")]).encode(): "statements.json[1].said_on",
                      json.dumps([row("jane.doe@example.invalid")]).encode(): "privacy lint: statements.json[0].quote",
                      json.dumps([row(PAYLOAD, context_note=None)]).encode(): "must be omitted rather than null",
                      b'[{"agency_id": "a", "agency_id": "b"}]': "repeats a key"}
        for data, needle in bad_stores.items():
            with self.subTest(needle=needle):
                self.file.write_bytes(data)
                self.refused(self.add, needle)
                self.refused(st.remove_statement, needle, root=self.root, index=1, today=TODAY)
                self.refused(st.list_statements, needle, root=self.root, today=TODAY)
        self.file.unlink()
        self.add()
        privacy = (({"quote": "Write jane.doe@example.invalid " + PAYLOAD}, "privacy lint: --quote-file: email"),
                   ({"quote": "Call 805-555-0142 " + PAYLOAD}, "privacy lint: --quote-file: phone"),
                   ({"context_note": "Plate 7ABC123 seen"}, "privacy lint: --context-note: plate"),
                   ({"said_on": "2026-10-21", "checked_on": "2026-10-21"}, "--said-on: is in the future"),
                   ({"said_on": "2026-10-11"}, "--said-on: must not be after checked_on"),
                   ({"checked_on": "2026-02-30"}, "--checked-on: must be a real YYYY-MM-DD date"),
                   ({"source_url": "http://cedar.example/" + PAYLOAD}, "--source-url: must be an https:// URL"),
                   ({"recording_time": "62:00"}, "--recording-time: use H:MM:SS"),
                   ({"context_note": PAYLOAD + " <b>"}, "--context-note: must be 1 to 200"))
        for values, needle in privacy:
            with self.subTest(needle=needle):
                self.refused(self.add, needle, **values)
        self.file.write_bytes(b" " * (2 * 1024 * 1024 + 1))
        self.refused(self.add, "file too large")

    def test_duplicate_equality_and_limits(self):
        self.add()
        self.refused(self.add, "already stored at index 1")
        reordered = dict(reversed(list(row("Hand written.").items())))
        self.store(self.stored() + [reordered])
        self.refused(self.add, "already stored at index 2", quote="Hand written.")
        self.assertEqual(self.add(context_note="Different note.")["index"], 3)
        self.assertEqual(self.add(recording_time="0:00:01")["index"], 4)
        self.assertEqual(self.add(said_on="2026-09-30")["index"], 5)
        self.store([row(f"Quote {n}.") for n in range(199)])
        self.assertEqual(self.add("Row two hundred.")["index"], 200)
        self.refused(self.add, "statements.json: must be a list of at most 200 statements", quote="Row 201.")
        self.store([row(f"Quote {n}.") for n in range(201)])
        self.refused(st.list_statements, "at most 200", root=self.root, today=TODAY)
        with mock.patch.object(st, "MAX_BYTES", 10):
            self.store([])
            self.refused(self.add, "would be larger than")

    def test_list_newest_first_retains_stored_removal_indices(self):
        self.add("First, older.", said_on="2026-01-05")
        self.add("Second, newest.", said_on="2026-03-01")
        self.add("Third, same day as first.", said_on="2026-01-05", agency=POLICE, official="Birch Example")
        self.add("Fourth, same day too.", said_on="2026-01-05")
        listed = st.list_statements(self.root, today=TODAY)
        self.assertEqual([(r["index"], r["quote"][:5]) for r in listed],
                         [(2, "Secon"), (1, "First"), (3, "Third"), (4, "Fourt")])
        self.assertEqual(listed[0], {"index": 2, **row("Second, newest.", said_on="2026-03-01")})
        self.assertEqual(json.loads(st.format_statements(listed)), listed)
        for bad in ("0", "-1", "+1", "1.0", " 1", "01", "\u0661", "", True, 0, -2, 1.0, None):
            with self.subTest(index=bad):
                self.refused(st.remove_statement, "--index: must be a whole number", root=self.root, index=bad,
                             today=TODAY)
        for bad in ("5", 99, "9" * 40):
            self.refused(st.remove_statement, "--index: out of range", root=self.root, index=bad, today=TODAY)
        self.assertEqual(st.remove_statement(self.root, index="3", today=TODAY), {"action": "removed", "index": 3})
        self.assertEqual([r["quote"][:5] for r in self.stored()], ["First", "Secon", "Fourt"])
        for _ in range(3):
            st.remove_statement(self.root, index=1, today=TODAY)
        self.assertEqual(self.file.read_bytes(), b"[]\n")
        self.assertEqual(st.list_statements(self.root, today=TODAY), [])
        self.assertEqual(st.format_statements([]), "[]\n")
        self.refused(st.remove_statement, "out of range", root=self.root, index=1, today=TODAY)

    def test_atomic_failures_and_target_arrival(self):
        self.add()
        os.chmod(self.file, 0o640)
        real_fsync = os.fsync
        for target in ("fsync", "replace"):
            with self.subTest(target=target), mock.patch.object(os, target, side_effect=OSError("synthetic")):
                self.assertIn("write failed; the previous file was kept", self.refused(self.add, "", quote="Another."))
        self.assertEqual(stat.S_IMODE(os.lstat(self.file).st_mode), 0o640)

        def compete(fd):  # another writer lands between staging and the rename
            real_fsync(fd)
            self.file.write_bytes(b"[]\n")
        with mock.patch.object(os, "fsync", side_effect=compete):
            with self.assertRaises(st.StatementError) as caught:
                self.add("Another.")
        self.assertIn("changed while it was being edited", str(caught.exception))
        self.assertEqual(self.file.read_bytes(), b"[]\n")
        self.assertEqual(self.temps(), [])
        self.file.unlink()

        def arrive(fd):
            real_fsync(fd)
            if not self.file.exists():
                self.file.write_bytes(b"[]\n")
        with mock.patch.object(os, "fsync", side_effect=arrive), self.assertRaises(st.StatementError):
            self.add()
        self.assertEqual((self.file.read_bytes(), self.temps()), (b"[]\n", []))
        self.file.unlink()
        (self.tmp / "elsewhere.json").write_text("[]", encoding="utf-8")
        self.file.symlink_to(self.tmp / "elsewhere.json")
        self.refused(self.add, "is a symbolic link")
        self.refused(st.list_statements, "is a symbolic link", root=self.root, today=TODAY)
        self.file.unlink()
        officials = self.content / "officials.json"
        officials.rename(self.tmp / "officials.json")
        officials.symlink_to(self.tmp / "officials.json")
        self.refused(self.add, "is a symbolic link")
        officials.unlink()
        self.content.rename(self.tmp / "real-content")
        self.content.symlink_to(self.tmp / "real-content")
        self.refused(self.add, "content/ must be a real folder")
        self.assertFalse((self.tmp / "real-content" / "statements.json").exists())

    def test_absence_and_read_only_commands_change_nothing(self):
        before = self.snapshot()
        with mock.patch.object(socket, "socket", side_effect=AssertionError("no network")), \
                mock.patch("campaign_tool.site.build_site", side_effect=AssertionError("no build")):
            self.assertEqual(st.list_statements(self.root, today=TODAY), [])
            self.refused(self.add, "--source-url", source_url="ftp://cedar.example/x")
            self.refused(st.remove_statement, "does not exist", root=self.root, index=1, today=TODAY)
            self.assertEqual(self.snapshot(), before)
            self.add()
            after_add = self.snapshot()
            self.assertEqual(set(after_add) - set(before), {"content/statements.json"})
            self.assertEqual({k: v for k, v in after_add.items() if k != "content/statements.json"}, before)
            st.list_statements(self.root, today=TODAY)
            self.assertEqual(self.snapshot(), after_add)
        self.assertFalse((self.root / "public").exists())


if __name__ == "__main__":
    unittest.main()
