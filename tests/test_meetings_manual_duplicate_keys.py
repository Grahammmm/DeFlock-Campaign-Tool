"""RF0825-3: kit/meetings-manual.json with a repeated JSON member name is refused before any history is lost.

Synthetic only: temp copies of examples/fictional-campaign, no network. Duplicate-bearing inputs are raw JSON text
(a serialized dict cannot hold a repeated member) built from a store the real manual API wrote.
"""
import copy
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from campaign_tool import meetings_manual as mm
from campaign_tool.meeting_stage import MeetingStageError, stage
from campaign_tool.meetings_manual import ManualMeetingError, cancel_manual, read_manual, read_meetings, record_manual
from tests import test_meetings_manual as base  # module alias: its TestCases are not collected here

MID, TODAY, REASON, BASE = base.MID, base.TODAY, base.REASON, base.BASE
OTHER = "manual-zeta-board"  # sorts after MID: entry 0 is MID, entry 1 is OTHER
REFUSAL = "kit/meetings-manual.json: a JSON object repeats a key"
STOPPED = f"Stopped: {REFUSAL}\n"
CANARY = "ZQ-HISTORY-CANARY"


class Raw(str):
    """JSON text used verbatim (a key spelled with escapes, or a value)."""


class Pairs(list):
    """A JSON object as an ordered list of (key, value) pairs, so a key may repeat."""


def to_pairs(value):
    if isinstance(value, dict):
        return Pairs((k, to_pairs(v)) for k, v in value.items())
    if isinstance(value, list):
        return [to_pairs(v) for v in value]
    return value


def render(value):
    if isinstance(value, Raw):
        return str(value)
    if isinstance(value, Pairs):
        return "{" + ", ".join(f"{k if isinstance(k, Raw) else json.dumps(k)}: {render(v)}" for k, v in value) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(render(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def walk(tree, path):
    for step in path:
        tree = next(v for k, v in tree if k == step) if isinstance(tree, Pairs) else tree[step]
    return tree


def with_member(tree, path, key, value, front=False):
    """A copy of ``tree`` with one more (key, value) member in the object at ``path``."""
    tree = copy.deepcopy(tree)
    members = walk(tree, path)
    members.insert(0 if front else len(members), (key, value))
    return tree


class DupBase(base.ManualBase):
    def setUp(self):
        super().setUp()
        guard = mock.patch("socket.socket", side_effect=AssertionError("network access attempted"))
        guard.start()
        self.addCleanup(guard.stop)

    def history_store(self):
        """A real store: MID with a replace and a cancel in its history, plus a second scheduled meeting."""
        self.add()
        self.replace(body="Smallville Water Board 2")
        cancel_manual(self.root, MID, on="2026-11-10", reason="Notice posted", today=TODAY)
        self.add(OTHER, agenda_item="Item 7")
        return to_pairs(self.doc())

    def put(self, tree):
        self.manual.write_bytes((render(tree) + "\n").encode("utf-8"))
        return self.manual.read_bytes()

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): (None if p.is_dir() else p.read_bytes(), p.lstat().st_mode & 0o7777,
                                                       p.lstat().st_mtime_ns)
                for p in sorted(self.root.rglob("*"))}

    def refuses(self, call, error=ValueError):  # ManualMeetingError and MeetingStageError are both ValueErrors
        with self.assertRaises(error) as caught:
            call()
        self.assertEqual(str(caught.exception), REFUSAL)

    def cli(self, *args):
        return base.cli(*args)


class ReaderTests(DupBase):
    def test_manual_duplicate_members_refuse_at_every_object_depth(self):
        tree = self.history_store()
        objects = [(), ("meetings", 0), ("meetings", 0, "history", 0), ("meetings", 0, "history", 1),
                   ("meetings", 0, "history", 0, "before"), ("meetings", 1)]
        checked = 0
        for path in objects:
            keys = [k for k, _ in walk(tree, path)] + ["unknown-member"]
            for key in keys:
                known = key != "unknown-member"
                source = tree if known else with_member(tree, path, key, "same")  # an unknown name first appears once
                for value in (walk(tree, path + (key,)) if known else "same", 5, "other"):
                    for front in (False, True):
                        with self.subTest(path=path, key=key, front=front, value=repr(value)[:20]):
                            raw = self.put(with_member(source, path, key, value, front))
                            self.refuses(lambda: read_manual(self.root))
                            self.refuses(lambda: read_meetings(self.root))
                            self.assertEqual(self.manual.read_bytes(), raw)
                            checked += 1
        self.assertGreater(checked, 150)
        # a repeated name whose first value is malformed and whose last value is valid is still ambiguous
        self.put(with_member(tree, (), "meetings", 5, front=True))
        self.refuses(lambda: read_manual(self.root))
        self.put(tree)
        self.assertEqual([e["id"] for e in read_manual(self.root)], [MID, OTHER])

    def test_decoded_escape_equivalent_keys_collide_but_separate_objects_do_not(self):
        tree = self.history_store()
        for path, spelled, plain in ((("meetings", 0), Raw('"histo\\u0072y"'), "history"),
                                     ((), Raw('"schem\\u0061_version"'), "schema_version"),
                                     (("meetings", 1), Raw('"\\u0069d"'), "id"),
                                     (("meetings", 0, "history", 0), Raw('"\\u006fn"'), "on")):
            for front in (False, True):
                with self.subTest(plain=plain, front=front):
                    raw = self.put(with_member(tree, path, spelled, [] if plain == "history" else 1, front))
                    self.assertIn(str(spelled).encode(), raw)  # the escaped spelling really reached the bytes
                    self.refuses(lambda: read_manual(self.root))
        # equal repeated unknown members and private-looking values refuse too
        self.put(with_member(with_member(tree, (), "zz", CANARY), (), "zz", CANARY))
        self.refuses(lambda: read_manual(self.root))
        # the same member name in different objects is ordinary
        self.put(tree)
        entries = read_manual(self.root)
        self.assertEqual([e["history"][0]["before"]["id"] for e in entries[:1]], [MID])
        # no case folding or Unicode normalization: these are different names, refused by the strict schema instead
        for first, second in (("History", "HISTORY"), ("\u00e9", "e\u0301")):
            self.put(with_member(with_member(tree, ("meetings", 0), first, 1), ("meetings", 0), second, 1))
            with self.assertRaises(ManualMeetingError) as caught:
                read_manual(self.root)
            self.assertIn("is not a manual meeting entry", str(caught.exception))


class LifecycleTests(DupBase):
    def test_unrelated_add_cannot_rewrite_ambiguous_history(self):
        tree = self.history_store()
        self.assertEqual(len(walk(tree, ("meetings", 0, "history"))), 2)  # two records a last-wins read would erase
        good = self.put(tree)
        history = json.loads(good)["meetings"][0]["history"]
        for label, member in (("empty-last", ("history", [])), ("empty-first", ("history", [])),
                              ("equal", ("history", copy.deepcopy(walk(tree, ("meetings", 0, "history"))))),
                              ("escaped", (Raw('"histo\\u0072y"'), []))):
            with self.subTest(label):
                raw = self.put(with_member(tree, ("meetings", 0), *member, front=label == "empty-first"))
                before = self.snapshot()
                with mock.patch.object(mm, "_save") as save:
                    self.refuses(lambda: self.add("manual-third", agenda_item="Item 8"))
                    save.assert_not_called()
                self.assertEqual(self.manual.read_bytes(), raw)
                self.assertEqual(self.snapshot(), before)  # no rewrite, no temp file, no new member
        # the same unrelated add on the duplicate-free store keeps every recorded correction
        self.manual.write_bytes(good)
        self.add("manual-third", agenda_item="Item 8")
        self.assertEqual(self.doc()["meetings"][0]["history"], history)
        self.assertEqual([e["id"] for e in self.doc()["meetings"]], sorted([MID, OTHER, "manual-third"]))

    def test_replace_cancel_and_healthy_target_refuse_whole_ambiguous_store(self):
        tree = self.history_store()
        good = self.put(tree)
        # the repeat sits in MID's nested snapshot; OTHER, the requested meeting, is healthy and still refuses
        raw = self.put(with_member(tree, ("meetings", 0, "history", 0, "before"), "body", "x"))
        before = self.snapshot()
        calls = (lambda: self.replace(on="2026-11-10", reason="Back on"),
                 lambda: self.replace(body="Smallville Water Board 3", reason="again"),
                 lambda: record_manual(self.root, OTHER, **base.BASE, replace=True, on="2026-11-10", reason="r", today=TODAY),
                 lambda: cancel_manual(self.root, OTHER, on="2026-11-10", reason="r", today=TODAY),
                 lambda: cancel_manual(self.root, MID, on="2026-11-10", reason="r", today=TODAY))
        for number, call in enumerate(calls):
            with self.subTest(number=number), mock.patch.object(mm, "_save") as save:
                self.refuses(call)
                save.assert_not_called()
            self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.manual.read_bytes(), raw)
        # restored duplicate-free bytes: replacement, cancellation and restoration keep the dated before-snapshots
        self.manual.write_bytes(good)
        cancel_manual(self.root, OTHER, on="2026-11-10", reason="Notice posted", today=TODAY)
        self.replace(on="2026-11-10", reason="Back on")
        entries = {e["id"]: e for e in self.doc()["meetings"]}
        self.assertEqual([h["action"] for h in entries[MID]["history"]], ["replace", "cancel", "replace"])
        self.assertEqual(entries[MID]["history"][2]["before"]["status"], "cancelled")
        self.assertEqual((entries[MID]["status"], entries[OTHER]["status"]), ("scheduled", "cancelled"))

    def test_stage_create_and_replace_refuse_duplicate_status_or_unrelated_rows(self):
        self.add()
        self.add(OTHER, agenda_item="Item 7")
        good = self.manual.read_bytes()
        stage(self.root, MID)  # a healthy staged target exists for the replace cases
        staged = self.root / "content" / "meetings"
        existing = (staged / f"{MID}.json").read_bytes()
        tree = to_pairs(json.loads(good))
        scenarios = {"status-cancelled-last": with_member(tree, ("meetings", 0), "status", "cancelled"),
                     "status-cancelled-first": with_member(tree, ("meetings", 0), "status", "cancelled", front=True),
                     "unrelated-history": with_member(tree, ("meetings", 1), "history", []),
                     "escaped-status": with_member(tree, ("meetings", 0), Raw('"stat\\u0075s"'), "scheduled")}
        for label, store in scenarios.items():
            raw = self.put(store)
            for target, replace in ((MID, True), (OTHER, False), (OTHER, True)):
                with self.subTest(label=label, target=target, replace=replace):
                    before = self.snapshot()
                    self.refuses(lambda: stage(self.root, target, replace=replace), MeetingStageError)
                    self.assertEqual(self.snapshot(), before)
                    self.assertEqual(self.manual.read_bytes(), raw)
            self.assertEqual((staged / f"{MID}.json").read_bytes(), existing)
            self.assertFalse((staged / f"{OTHER}.json").exists())
            self.assertFalse([p for p in staged.iterdir() if p.name.endswith(".tmp")])
        # duplicate-free: staging keeps its marker, report and private-field exclusion
        self.manual.write_bytes(good)
        report = stage(self.root, OTHER)
        self.assertEqual(report["note"], "Organizer-entered meeting; official agenda checked 2026-11-09.")
        self.assertNotIn("history", (staged / f"{OTHER}.json").read_text(encoding="utf-8"))


class CliTests(DupBase):
    def test_actual_cli_add_replace_cancel_and_stage_refuse_without_values(self):
        d = str(self.root)
        entry = ["--body", "Canary Body ZQ", "--agency-id", BASE["agency_id"], "--starts-at", "2099-01-20T18:30:00-08:00",
                 "--agenda-url", BASE["agenda_url"], "--item-title", "Canary Item ZQ", "--source-url", BASE["source_url"],
                 "--checked-on", "2026-01-01"]
        for meeting in (MID, OTHER):
            made = self.cli("meetings-add", "--directory", d, "--id", meeting, *entry)
            self.assertEqual(made.returncode, 0, made.stderr)
        self.assertEqual(self.cli("meetings-stage", "--directory", d, "--id", OTHER).returncode, 0)
        good = self.manual.read_bytes()
        tree = to_pairs(json.loads(good))
        forms = {"conflict": with_member(tree, ("meetings", 0), "status", "cancelled"),
                 "equal": with_member(tree, ("meetings", 1), "status", "scheduled"),
                 "escaped": with_member(tree, ("meetings", 1), Raw('"histo\\u0072y"'), [])}
        commands = {"add": ["meetings-add", "--id", "manual-third", *entry],
                    "replace": ["meetings-add", "--id", MID, *entry, "--replace", "--on", "2026-02-01", "--reason", REASON],
                    "cancel": ["meetings-add", "--id", MID, "--cancel", "--on", "2026-02-01", "--reason", REASON],
                    "stage-create": ["meetings-stage", "--id", MID],
                    "stage-replace": ["meetings-stage", "--id", OTHER, "--replace"]}
        for form, store in forms.items():
            raw = self.put(store)
            for name, args in commands.items():
                with self.subTest(form=form, command=name):
                    before = self.snapshot()
                    run = self.cli(*args[:1], "--directory", d, *args[1:])
                    self.assertEqual((run.returncode, run.stdout, run.stderr), (1, "", STOPPED))
                    self.assertEqual(self.snapshot(), before)
                    self.assertEqual(self.manual.read_bytes(), raw)
        self.manual.write_bytes(good)  # duplicate-free: the same commands work again
        ok = self.cli("meetings-add", "--directory", d, "--id", MID, "--cancel", "--on", "2026-02-01", "--reason", REASON)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn("Recorded only. Nothing was sent or published.", ok.stdout)


class LegacyAndCompatibilityTests(DupBase):
    def test_legacy_parse_admission_and_semantic_errors_are_unchanged(self):
        self.add()
        good = self.doc()
        entry = good["meetings"][0]
        cases = [(b"\xff\xfe nope", "not UTF-8 JSON"), (b"{not json", "not UTF-8 JSON"),
                 (b'{"a": 1, "a": 2', "not UTF-8 JSON"),  # a syntax failure stops the decoder before any repeat is seen
                 (b"[" * 100000, "not UTF-8 JSON"), (b" " * (4 * 1024 * 1024 + 1), "larger than 4 MiB"),
                 (json.dumps({**good, "schema_version": True}).encode(), "unsupported schema_version"),
                 (json.dumps({**good, "meetings": [{**entry, "status": "gone"}]}).encode(), "invalid field"),
                 (json.dumps({**good, "meetings": [{**entry, "history": "x"}]}).encode(), "history must be a list"),
                 (json.dumps({**good, "meetings": [entry, {**entry}]}).encode(), "duplicate meeting id")]
        for raw, needle in cases:
            with self.subTest(needle=needle, size=len(raw)):
                self.manual.write_bytes(raw)
                with self.assertRaises(ManualMeetingError) as caught:
                    read_manual(self.root)
                self.assertIn(needle, str(caught.exception))
                self.assertNotIn("repeats a key", str(caught.exception))
                self.assertRaises(MeetingStageError, stage, self.root, MID)
        self.manual.unlink()
        self.assertEqual(read_manual(self.root), [])
        outside = self.tmp / "outside.json"
        outside.write_text("{}")
        os.symlink(outside, self.manual)
        with self.assertRaises(ManualMeetingError) as caught:
            read_manual(self.root)
        self.assertIn("must not be a symlink", str(caught.exception))
        # the pure validator on an already decoded object keeps its semantics
        self.assertEqual(len(mm._manual_document(good)), 1)
        self.assertRaises(ManualMeetingError, mm._manual_document, {**good, "extra": 1})

    def test_duplicate_free_lifecycle_and_scan_bytes_are_compatible(self):
        self.manual.parent.mkdir(exist_ok=True)
        self.manual.write_bytes(b'{"meetings": [], "schema_version": 1}')  # a valid empty store, members reordered
        self.assertEqual(read_manual(self.root), [])
        self.manual.unlink()
        scan = [{"id": "legistar-x-1", "body": "B", "starts_at": "2026-10-14T18:00:00-07:00", "agenda_item": "A"}]
        self.write_scan(scan)
        scan_bytes = self.scan.read_bytes()
        self.add(body="Conseil d\u2019\u00e9coute \u2014 Caf\u00e9")
        expected = {"id": MID, "body": "Conseil d\u2019\u00e9coute \u2014 Caf\u00e9", "agency_id": "ca-cedar-police",
                    "starts_at": "2026-11-20T18:30:00-08:00", "agenda_url": "https://example.invalid/agenda",
                    "agenda_item": "Item 5: ALPR renewal", "source_url": "https://example.invalid/page",
                    "checked_on": "2026-11-09", "source": "manual", "time_known": True, "status": "scheduled", "history": []}
        text = json.dumps({"schema_version": 1, "meetings": [expected]}, indent=2, ensure_ascii=False) + "\n"
        self.assertEqual(self.manual.read_bytes(), text.encode("utf-8"))
        self.assertEqual(oct(self.manual.stat().st_mode & 0o777), "0o600")
        self.assertEqual([m["id"] for m in read_meetings(self.root)], ["legistar-x-1", MID])
        # the same members in another order read identically
        entries = to_pairs(json.loads(text))
        entries[1][1][0] = Pairs(reversed(entries[1][1][0]))
        self.put(entries)
        self.assertEqual(read_manual(self.root)[0]["body"], expected["body"])
        self.manual.write_bytes(text.encode("utf-8"))
        self.replace(body="Board 2", on="2026-11-10", reason="Moved")
        cancel_manual(self.root, MID, on="2026-11-10", reason="Notice posted", today=TODAY)
        self.replace(on="2026-11-10", reason="Back on")
        final = self.doc()["meetings"][0]
        self.assertEqual([(h["action"], h["before"]["status"]) for h in final["history"]],
                         [("replace", "scheduled"), ("cancel", "scheduled"), ("replace", "cancelled")])
        self.assertEqual((final["status"], final["body"]), ("scheduled", BASE["body"]))
        self.assertEqual(final["history"][0]["before"], {k: v for k, v in expected.items() if k != "history"})
        self.assertEqual(self.scan.read_bytes(), scan_bytes)


if __name__ == "__main__":
    unittest.main()
