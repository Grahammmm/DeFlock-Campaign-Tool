"""DF1109B-07: ``content elsewhere add|remove|list`` maintain content/elsewhere.json in stored (citation) order.

Offline and synthetic only: temp copies of examples/fictional-campaign, .test/.invalid sources; nothing is fetched or sent.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import elsewhere_cmd
from campaign_tool.content_check import content_problems
from campaign_tool.content_elsewhere import read_elsewhere
from campaign_tool.elsewhere_cmd import ElsewhereError, add_entry, list_entries, parse_index, remove_entry
from campaign_tool.meeting_stage import stage

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 11, 1)
MID = "legistar-examplecity-3001"
STORED_PROBE = "ZQ-PRIVATE-7c1"


def entry(n, **extra):
    row = {"place": f"Springfield {n}", "state": "OR", "body": "Springfield City Council", "decided_on": "2026-03-10",
           "kind": "paused", "as_stated": f"The council voted to pause contract {n}.",
           "source_url": f"https://records.example.test/minutes/{n}", "checked_on": "2026-09-15"}
    row.update(extra)
    return row


def flags(row):
    return [part for key, value in row.items() for part in ("--" + key.replace("_", "-"), value)]


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-elsewhere-cmd-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content = self.root / "content"
        self.path = self.content / "elsewhere.json"

    def put(self, document):
        self.path.write_text(json.dumps(document), encoding="utf-8")

    def tree(self):
        return {str(p.relative_to(self.root)): (p.read_bytes() if p.is_file() and not p.is_symlink() else None,
                                                 p.lstat().st_mode) for p in sorted(self.root.rglob("*"))}

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "content", "elsewhere", *args],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)

    def meeting(self, name, *snapshots):
        document = json.loads((self.content / "meetings" / "2026-10-cedar-council.json").read_text(encoding="utf-8"))
        document["cited_elsewhere"], document["id"] = list(snapshots), name[:-len(".json")]
        (self.content / "meetings" / name).write_text(json.dumps(document), encoding="utf-8")

    def assert_refused(self, call, pattern):
        before = self.tree()
        with self.assertRaisesRegex(ElsewhereError, pattern) as caught:
            call()
        self.assertEqual(self.tree(), before)
        self.assertNotIn(STORED_PROBE, str(caught.exception))


class CommandTests(Base):
    def test_cli_add_list_stage_uses_stored_not_page_order(self):
        D = str(self.root)
        rows = [entry(1, kind="paused", decided_on="2026-01-02"), entry(2, kind="adopted_ordinance", decided_on="2026-05-01"),
                entry(3, kind="ended_contract", decided_on="2026-02-01", news_url="https://news.example.test/3")]
        for n, row in enumerate(rows, 1):
            done = self.cli("add", "--directory", D, *flags(row))
            self.assertEqual((done.returncode, done.stdout), (0, f"Elsewhere entry added: index {n}.\n"
                             "Content only; nothing was fetched, built, sent or deployed.\n"), done.stderr)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), rows)
        listed = self.cli("list", "--directory", D)
        self.assertEqual((listed.returncode, listed.stdout), (0, "index | place | body | decided_on | kind | checked_on\n"
                         "1 | Springfield 1 | Springfield City Council | 2026-01-02 | paused | 2026-09-15\n"
                         "2 | Springfield 2 | Springfield City Council | 2026-05-01 | adopted_ordinance | 2026-09-15\n"
                         "3 | Springfield 3 | Springfield City Council | 2026-02-01 | ended_contract | 2026-09-15\n"))
        (self.root / "kit").mkdir(exist_ok=True)
        (self.root / "kit" / "meetings.json").write_text(json.dumps({"schema_version": 1, "meetings": [
            {"id": MID, "body": "Example City Council", "starts_at": "2026-11-18T18:00:00-08:00"}]}), encoding="utf-8")
        stage(self.root, MID, elsewhere_indices=[3, 1], today=TODAY)
        staged = json.loads((self.content / "meetings" / f"{MID}.json").read_text(encoding="utf-8"))
        self.assertEqual(staged["cited_elsewhere"], [rows[2], rows[0]])
        self.assertFalse([p for p in content_problems(self.root) if "elsewhere" in p])

    def test_missing_list_and_remove_never_create_files(self):
        before = self.tree()
        listed = self.cli("list", "--directory", str(self.root))
        self.assertEqual((listed.returncode, listed.stdout), (0, "No decisions elsewhere recorded.\n"))
        removed = self.cli("remove", "--directory", str(self.root), "--index", "1")
        self.assertEqual((removed.returncode, removed.stdout), (1, ""))
        self.assertIn("Stopped: content/elsewhere.json: does not exist; there is nothing to remove", removed.stderr)
        self.assertEqual(self.tree(), before)
        bare = Path(self.root.parent / "not-a-campaign")
        bare.mkdir()
        self.assertEqual(self.cli("list", "--directory", str(bare)).returncode, 1)
        self.assertEqual(list(bare.iterdir()), [])

    def test_first_add_and_last_remove_restore_absent_state(self):
        before = self.tree()
        self.assertEqual(add_entry(self.root, **entry(1), today=TODAY)["index"], 1)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o644)
        self.assertTrue(self.path.read_bytes().endswith(b"]\n"))
        done = self.cli("remove", "--directory", str(self.root), "--index", "1")
        self.assertEqual((done.returncode, done.stdout), (0, "Elsewhere entry removed: index 1.\n"
                         "No staged meetings cite the same place, body and date.\n"
                         "content/elsewhere.json removed; no decisions elsewhere remain.\n"
                         "Later stored positions shift down by one; run content elsewhere list before choosing --elsewhere again.\n"
                         "Content only; nothing was fetched, built, sent or deployed.\n"))
        self.assertIsNone(read_elsewhere(self.root, TODAY))
        self.assertEqual(self.tree(), before)

    def test_whole_store_refused_without_repair(self):
        bad = [b"[]", b"null", b"{}", b'"x"', b"\xff\xfe", b"[{", b"[" * 5000 + b"]" * 5000,
               json.dumps([entry(1), entry(1, source_url="https://other.example.test/x", state="WA")]).encode(),
               json.dumps([entry(1), dict(entry(2), **{STORED_PROBE: "x"})]).encode(),
               json.dumps([entry(1), dict(entry(2), place=" ")]).encode(),
               b'[{"place": "a", "place": "b"}]']
        for data in bad:
            with self.subTest(data=data[:40]):
                self.path.write_bytes(data)
                for call in (lambda: add_entry(self.root, **entry(9), today=TODAY),
                             lambda: remove_entry(self.root, 1, today=TODAY), lambda: list_entries(self.root, today=TODAY)):
                    self.assert_refused(call, "elsewhere.json")
                self.assertEqual(self.path.read_bytes(), data)
        listed = self.cli("list", "--directory", str(self.root))
        self.assertEqual((listed.returncode, listed.stdout), (1, ""))

    def test_loader_boundaries_and_duplicate_identity(self):
        self.put([entry(n) for n in range(99)])
        self.assertEqual(add_entry(self.root, **entry(99, news_url="https://news.example.test/a"), today=TODAY)["index"], 100)
        self.assert_refused(lambda: add_entry(self.root, **entry(100), today=TODAY), "1 to 100 entries")
        self.put([entry(1)])
        cases = ((dict(source_url="https://other.example.test/x", state="WA"), r"\[1\]: duplicate of elsewhere.json\[0\]"),
                 (dict(checked_on="2026-11-02"), r"checked_on: 2026-11-02 is in the future"),
                 (dict(decided_on="2026-09-16"), "must not be after checked_on"),
                 (dict(decided_on="2026-02-29"), "real YYYY-MM-DD"), (dict(state="or"), "state"),
                 (dict(source_url="https://u:p@records.example.test/"), "source_url"),
                 (dict(news_url="http://news.example.test/"), "news_url"), (dict(as_stated="x" * 201), "as_stated"))
        for change, pattern in cases:
            with self.subTest(change=change):
                self.assert_refused(lambda: add_entry(self.root, **dict(entry(1), **change), today=TODAY), pattern)
        add_entry(self.root, **entry(1, kind="declined"), today=TODAY)  # a different kind is a different decision
        add_entry(self.root, **entry(1, decided_on="2024-02-29", place="  Springfield 1  ", as_stated="y" * 200), today=TODAY)
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual([row["kind"] for row in stored], ["paused", "declined", "paused"])
        self.assertEqual(stored[2]["place"], "Springfield 1")
        self.assertNotIn("news_url", stored[2])

    def test_privacy_every_field_and_old_row(self):
        self.put([entry(1)])
        for field, value, category in (("place", "Call 555-123-4567", "phone"), ("body", "ann.lee@example.org", "email"),
                                       ("as_stated", "Plate 7ABC123 seen", "plate"),
                                       ("source_url", "https://records.example.test/call/555-123-4567", "phone"),
                                       ("news_url", "https://news.example.test/plate/7ABC123", "plate")):
            with self.subTest(field=field):
                self.assert_refused(lambda: add_entry(self.root, **entry(2, **{field: value}), today=TODAY),
                                    rf"privacy lint: elsewhere.json\[1\].{field}: {category}")
        self.put([entry(1), entry(2, body="Clerk ann.lee@example.org")])
        for call in (lambda: add_entry(self.root, **entry(3), today=TODAY), lambda: remove_entry(self.root, 1, today=TODAY),
                     lambda: list_entries(self.root, today=TODAY)):
            self.assert_refused(call, r"privacy lint: elsewhere.json\[1\].body: email")
        done = self.cli("list", "--directory", str(self.root))
        self.assertEqual(done.returncode, 1)
        self.assertNotIn("ann.lee", done.stdout + done.stderr)

    def test_legal_note_is_advisory_and_indexed(self):
        self.put([entry(1, as_stated="The county violated SB 34 by sharing plate data.")])
        done = add_entry(self.root, **entry(2, as_stated="The city violated SB 34 by sharing plate data."), today=TODAY)
        self.assertEqual(done["index"], 2)
        self.assertTrue(done["notes"])
        self.assertTrue(all("elsewhere.json[1].as_stated" in note and "violated" not in note for note in done["notes"]))
        self.assertEqual(len(json.loads(self.path.read_text(encoding="utf-8"))), 2)


class RemoveTests(Base):
    def setUp(self):
        super().setUp()
        self.rows = [entry(1), entry(2), entry(1, kind="declined", source_url="https://records.example.test/d")]
        self.put(self.rows)

    def test_remove_middle_warns_three_field_matches_without_rewriting_kits(self):
        self.meeting("z-exact.json", self.rows[0])
        self.meeting("a-same-triple.json", entry(2), dict(entry(1), kind="other"))
        self.meeting("m-other.json", entry(2), entry(1, decided_on="2026-03-11"), entry(1, body="Springfield Board"))
        kits = {p.name: p.read_bytes() for p in (self.content / "meetings").iterdir()}
        done = self.cli("remove", "--directory", str(self.root), "--index", "1")
        self.assertEqual((done.returncode, done.stdout), (0, "Elsewhere entry removed: index 1.\n"
                         "Warning: meetings/a-same-triple.json cites the same place, body and date; review and restage with meetings-stage --replace.\n"
                         "Warning: meetings/z-exact.json cites the same place, body and date; review and restage with meetings-stage --replace.\n"
                         "Later stored positions shift down by one; run content elsewhere list before choosing --elsewhere again.\n"
                         "Content only; nothing was fetched, built, sent or deployed.\n"), done.stderr)
        self.assertEqual({p.name: p.read_bytes() for p in (self.content / "meetings").iterdir()}, kits)
        self.assertEqual([row["index"] for row in list_entries(self.root, today=TODAY)], [1, 2])
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), self.rows[1:])
        self.assertTrue([p for p in content_problems(self.root) if p.startswith("meetings/z-exact.json.cited_elsewhere[0]")])
        # an existing unresolved citation never blocks a further removal or a re-add repair
        self.assertEqual(remove_entry(self.root, 2, today=TODAY)["citing"], ["meetings/a-same-triple.json", "meetings/z-exact.json"])
        add_entry(self.root, **self.rows[0], today=TODAY)
        self.assertFalse([p for p in content_problems(self.root) if p.startswith("meetings/z-exact.json")])

    def test_broken_meeting_scan_refuses_before_remove(self):
        meetings = self.content / "meetings"
        for name, make, pattern in (
                ("bad.json", lambda p: p.write_text("{", encoding="utf-8"), "not valid UTF-8 JSON"),
                ("bad.json", lambda p: p.write_text(json.dumps({"body": "x"}), encoding="utf-8"), "meetings/bad.json"),
                ("big.json", lambda p: p.write_bytes(b" " * (2 * 1024 * 1024 + 1)), "file too large"),
                ("link.json", lambda p: p.symlink_to(meetings / "2026-10-cedar-council.json"), "symbolic link"),
                ("dir.json", lambda p: p.mkdir(), "regular file")):
            with self.subTest(name=name, pattern=pattern):
                make(meetings / name)
                self.assert_refused(lambda: remove_entry(self.root, 1, today=TODAY), pattern)
                (meetings / name).rmdir() if (meetings / name).is_dir() and not (meetings / name).is_symlink() \
                    else (meetings / name).unlink()
        shutil.rmtree(meetings)
        (self.content / "meetings").symlink_to(self.root / "kit", target_is_directory=True)
        self.assert_refused(lambda: remove_entry(self.root, 1, today=TODAY), "content/meetings: must be a real folder")
        (self.content / "meetings").unlink()
        self.assertEqual(remove_entry(self.root, 1, today=TODAY)["citing"], [])

    def test_meeting_scan_refusals_repeat_no_stored_text(self):
        meetings = self.content / "meetings"
        base = json.loads((meetings / "2026-10-cedar-council.json").read_text(encoding="utf-8"))
        for label, change in (("snapshot key", lambda d: d.update(cited_elsewhere=[dict(self.rows[0], **{STORED_PROBE: "x"})])),
                              ("quoted value", lambda d: d.update(starts_at=STORED_PROBE))):
            with self.subTest(label):
                document = dict(base, id="probe")
                change(document)
                (meetings / "probe.json").write_text(json.dumps(document), encoding="utf-8")
                self.assert_refused(lambda: remove_entry(self.root, 1, today=TODAY), r"^meetings/probe\.json\b[^']*; ")
                (meetings / "probe.json").unlink()

    def test_index_syntax_and_argparse_refusals(self):
        for token in ("0", "-1", "+1", " 1", "1 ", "1.0", "1e2", "١", "01", "9" * 10, ""):
            with self.subTest(token=token), self.assertRaisesRegex(ElsewhereError, r"^--index: must be a whole number"):
                parse_index(token)
        for bad in (True, 0, -1, "1", 1.0):
            with self.subTest(bad=bad):
                self.assert_refused(lambda: remove_entry(self.root, bad, today=TODAY), "--index: must be a whole number")
        self.assert_refused(lambda: remove_entry(self.root, 4, today=TODAY), r"^--index: choose 1 to 3 \(stored order\)$")
        before, D = self.tree(), str(self.root)
        for args, code in ((("remove", "--directory", D, "--index", "0"), 1), (("remove", "--directory", D, "--index", "4"), 1),
                           (("remove", "--directory", D), 2), (("list", "--directory", D, "--place", "x"), 2),
                           (("remove", "--directory", D, "--index", "1", "--kind", "paused"), 2), (("rename", "--directory", D), 2),
                           (("add", "--directory", D, *flags(entry(5, kind="banned"))), 2),
                           (("add", "--directory", D, *flags(entry(5))[:-2]), 2)):
            with self.subTest(args=args[:1] + args[3:]):
                done = self.cli(*args)
                self.assertEqual((done.returncode, done.stdout), (code, ""), done.stderr)
        self.assertEqual(self.tree(), before)

    def test_path_types_and_target_change_are_fail_closed(self):
        outside = self.root.parent / "outside.json"
        outside.write_text(json.dumps([entry(7)]), encoding="utf-8")
        self.path.unlink()
        for make, pattern in ((lambda: self.path.symlink_to(outside), "symbolic link"),
                              (lambda: self.path.symlink_to(self.root / "missing"), "symbolic link"),
                              (lambda: self.path.mkdir(), "regular file"), (lambda: os.mkfifo(self.path), "regular file"),
                              (lambda: os.link(outside, self.path), "single link")):
            with self.subTest(pattern=pattern):
                make()
                for call in (lambda: add_entry(self.root, **entry(9), today=TODAY), lambda: list_entries(self.root, today=TODAY),
                             lambda: remove_entry(self.root, 1, today=TODAY)):
                    self.assert_refused(call, pattern)
                self.path.rmdir() if self.path.is_dir() and not self.path.is_symlink() else self.path.unlink()
        self.assertEqual(json.loads(outside.read_text(encoding="utf-8")), [entry(7)])
        self.put(self.rows)
        with mock.patch.object(elsewhere_cmd, "_unchanged", side_effect=FileExistsError):
            for call in (lambda: add_entry(self.root, **entry(9), today=TODAY), lambda: remove_entry(self.root, 1, today=TODAY)):
                self.assert_refused(call, "changed while it was being edited; nothing was written")
        elsewhere_dir = self.root.parent / "outside-content"
        elsewhere_dir.mkdir()
        shutil.rmtree(self.content)
        self.content.symlink_to(elsewhere_dir, target_is_directory=True)
        self.assert_refused(lambda: add_entry(self.root, **entry(9), today=TODAY), "content/ must be a real folder")
        self.assertEqual(list(elsewhere_dir.iterdir()), [])

    def test_atomic_failure_preserves_previous_bytes_and_mode(self):
        self.path.chmod(0o600)
        real_open = os.open

        def temp_fails(name, *args, **kwargs):
            if str(name).startswith(".elsewhere-"):
                raise OSError(28, "no space")
            return real_open(name, *args, **kwargs)
        for target, effect in (("open", temp_fails), ("fsync", OSError(5, "x")), ("replace", OSError(5, "x"))):
            with self.subTest(target=target), mock.patch.object(elsewhere_cmd.os, target, side_effect=effect):
                self.assert_refused(lambda: add_entry(self.root, **entry(9), today=TODAY),
                                    "^content/elsewhere.json: write failed; the previous file was kept$")
        self.put([entry(1)])
        self.path.chmod(0o600)
        with mock.patch.object(elsewhere_cmd.os, "unlink", side_effect=OSError(5, "x")):
            self.assert_refused(lambda: remove_entry(self.root, 1, today=TODAY), "could not be removed; the previous file was kept")
        add_entry(self.root, **entry(9), today=TODAY)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_encoded_size_checked_before_temp_creation(self):
        exact = len((json.dumps(self.rows + [entry(9, as_stated="é")], indent=2, ensure_ascii=False) + "\n").encode())
        for cap, ok in ((exact, True), (exact - 1, False)):
            with self.subTest(cap=cap), mock.patch.object(elsewhere_cmd, "MAX_JSON_BYTES", cap), \
                    mock.patch.object(elsewhere_cmd.os, "open", wraps=os.open) as opened:
                if ok:
                    add_entry(self.root, **entry(9, as_stated="é"), today=TODAY)
                else:
                    self.put(self.rows)
                    self.assert_refused(lambda: add_entry(self.root, **entry(9, as_stated="é"), today=TODAY),
                                        "would be larger than")
                    self.assertFalse([c for c in opened.call_args_list if str(c.args[0]).startswith(".elsewhere-")])
        self.put(self.rows)
        self.assert_refused(lambda: add_entry(self.root, **entry(9, place="Spring\udcfffield"), today=TODAY),
                            r"place|cannot be stored as UTF-8")

    def test_no_outbound(self):
        with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network")), \
                mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            add_entry(self.root, **entry(9), today=TODAY)
            list_entries(self.root, today=TODAY)
            remove_entry(self.root, 4, today=TODAY)
        self.assertFalse((self.root / "public").exists())


if __name__ == "__main__":
    unittest.main()
