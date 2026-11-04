"""kit-sent-import: requests the campaign sent before it used the tool, from one checked CSV in one write.
Offline, synthetic: invented agencies, references and letters; no socket may be opened and nothing is sent."""
import contextlib
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit, kit_export, kit_import_sent, kit_track  # noqa: E402
from tests.test_kit_check import ready_drafts  # noqa: E402

HEADER = "agency_id,request,sent_on,channel,reference,letter_record"
TODAY = date(2026, 6, 1)
FULL_HEX = re.compile(r"[0-9a-f]{64}")
RAW = "RAW-VALUE-ZQX"  # a marker no message may echo


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


def snapshot(root):  # every path with its kind, mode and bytes: any write, new file or symlink shows
    return {str(p.relative_to(root)): (p.is_symlink(), p.lstat().st_mode, p.read_bytes() if p.is_file() else None)
            for p in sorted(Path(root).rglob("*"))}


def synthetic(label):
    return hashlib.sha256(label.encode()).hexdigest()


class KitSentImportTests(unittest.TestCase):
    def setUp(self):
        for target, effect in (("socket.socket", AssertionError("socket opened")),
                               ("urllib.request.urlopen", AssertionError("network used")),
                               ("smtplib.SMTP", AssertionError("smtp used"))):
            patcher = mock.patch(target, side_effect=effect)
            patcher.start()
            self.addCleanup(patcher.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-sent-import-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        for command in (("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                         "--state", "CA"), ("kit", "--directory", self.root)):
            self.assertEqual(run(*command)[0], 0)
        self.kit, self.sent = self.root / "kit", self.root / "kit" / "sent.json"
        self.ids = [p.stem for p in sorted((self.kit / "requests").glob("*.md"))]
        self.assertGreaterEqual(len(self.ids), 4)
        self.a, self.b, self.c = self.ids[:3]
        self.letters = {name: synthetic("letter " + name) for name in "xyz"}
        db = cli.database(self.root)
        with db:
            db.executemany("INSERT INTO objects (sha256, byte_count, stored_at) VALUES (?, 1, ?)",
                           [(h, "2026-03-01T00:00:00+00:00") for h in self.letters.values()])
        db.close()

    # helpers
    def csv(self, *rows, name="sent.csv", header=HEADER):
        path = self.tmp / name
        path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
        return path

    def do(self, path, *extra):
        return run("kit-sent-import", "--directory", self.root, "--csv", path, *extra)

    def api(self, path, **kwargs):
        return kit_import_sent.import_sent(self.root, path, today=TODAY, **kwargs)

    def stored(self):
        return json.loads(self.sent.read_text())["requests"]

    def refuses(self, path, *words, absent=(), api=True):
        """The file is refused with exit 2 and `Stopped:`, names each word, echoes no raw value, writes nothing."""
        before = snapshot(self.root)
        if api:
            with self.assertRaises(kit_import_sent.SentImportError) as raised:
                self.api(path, apply=True)
            message = str(raised.exception)
        else:
            code, out, message = self.do(path, "--apply")
            self.assertEqual(code, 2, out + message)
            self.assertTrue(message.startswith("Stopped: "), message)
            self.assertNotIn("Traceback", message)
        for word in words:
            self.assertIn(word, message)
        for word in (RAW, *absent):
            self.assertNotIn(word, message)
        self.assertIsNone(FULL_HEX.search(message))
        self.assertEqual(snapshot(self.root), before)
        return message

    def ordinary_send(self, agency, on="2026-03-02", *extra):
        ready_drafts(self.root, [agency])
        return run("kit-sent", "--directory", self.root, "--agency", agency, "--date", on, "--channel", "email", *extra)

    # 1
    def test_preview_apply_and_origin_schema(self):
        self.assertEqual(self.ordinary_send(self.c)[0], 0)
        normal = self.stored()[self.c]
        path = self.csv(f"{self.a},,2026-03-02,email,R0001-110125,{self.letters['x'][:12]}",
                        f"{self.a},contract-2026,2026-03-09,portal,,",
                        f"{self.b},main,2026-02-27,mail,,",
                        f"{self.b},narrowed,2026-04-01,email,,{self.letters['y'][:20]}")
        before = snapshot(self.root)
        code, out, err = self.do(path)
        self.assertEqual((code, json.loads(out), err), (0, {"checked": 4, "imported": 0, "written": False},
                                                         "Check only. Nothing was written or sent.\n"))
        self.assertEqual(snapshot(self.root), before)
        with mock.patch.object(kit, "_write_json", wraps=kit._write_json) as writer:
            code, out, err = self.do(path, "--apply")
        self.assertEqual((code, json.loads(out), err), (0, {"checked": 4, "imported": 4, "written": True},
                                                         "Recorded only. Nothing was sent.\n"))
        self.assertEqual(writer.call_count, 1)
        self.assertEqual(stat.S_IMODE(self.sent.stat().st_mode), 0o600)
        stored = self.stored()
        self.assertEqual(sorted(stored), sorted([self.a, f"{self.a}#contract-2026", self.b, f"{self.b}#narrowed", self.c]))
        self.assertEqual(stored[self.c], normal)  # an ordinary entry is untouched
        main = stored[self.a]
        self.assertEqual({k: v for k, v in main.items() if k != "recorded_at"},
                         {"imported": True, "sent_on": "2026-03-02", "channel": "email", "steps": [],
                          "reference": "R0001-110125", "correspondence": [self.letters["x"]]})
        self.assertRegex(main["recorded_at"], kit_track.STAMP.pattern)
        self.assertEqual(set(stored[f"{self.a}#contract-2026"]), {"imported", "sent_on", "channel", "recorded_at", "steps"})
        self.assertEqual(stored[f"{self.b}#narrowed"]["correspondence"], [self.letters["y"]])
        for key in (self.a, f"{self.a}#contract-2026", self.b, f"{self.b}#narrowed"):
            self.assertNotIn("draft", stored[key])
            self.assertNotIn("draft_sha256", stored[key])
        self.assertEqual(sorted(kit_track.read_sent(self.root)), sorted(stored))  # the store still validates

    # 2
    def test_csv_boundaries_and_physical_lines(self):
        row = f"{self.a},,2026-03-02,email,,"
        path = self.tmp / "bom.csv"  # BOM, blank rows, a quoted cell with a comma, padded cells
        path.write_bytes(("\ufeff" + HEADER + f'\n\n  {self.a} ,"",2026-03-02, email ,"R-1",\n\n').encode("utf-8"))
        self.assertEqual(self.api(path), {"checked": 1, "imported": 0, "written": False})
        # 1 and 500 rows pass, 0 and 501 are refused
        many = lambda n: self.csv(*[f"{self.a},r{i},2026-03-02,email,," for i in range(n)], name=f"{n}.csv")
        self.assertEqual(self.api(many(500), apply=True)["imported"], 500)
        self.sent.unlink()
        self.refuses(self.csv(name="zero.csv"), "no rows")
        self.refuses(many(501), "501 rows")
        self.refuses(self.csv(row, header="agency_id,request,sent_on,channel,reference"), "header")
        self.refuses(self.csv(row, header="request,agency_id,sent_on,channel,reference,letter_record"), "header")
        self.refuses(self.csv(row, header=HEADER + ",extra"), "header")
        self.refuses(self.csv(row, header=HEADER.replace("channel", "chan")), "header")
        self.refuses(self.csv(row, f"{self.a},extra,2026-03-02,email,,,", name="wide.csv"), "line 3: row: expected 6 cells")
        (self.tmp / "empty.csv").write_bytes(b"")
        self.refuses(self.tmp / "empty.csv", "empty")
        (self.tmp / "quote.csv").write_text(HEADER + f'\n{self.a},"open,2026-03-02,email,,\n', encoding="utf-8")
        self.refuses(self.tmp / "quote.csv", "line 2: malformed CSV")
        (self.tmp / "stray.csv").write_text(HEADER + f'\n{self.a},"ok"x,2026-03-02,email,,\n', encoding="utf-8")
        self.refuses(self.tmp / "stray.csv", "line 2: malformed CSV")
        (self.tmp / "latin.csv").write_bytes(HEADER.encode() + b"\n" + self.a.encode() + b",\xff,2026-03-02,email,,\n")
        self.refuses(self.tmp / "latin.csv", "not UTF-8")
        (self.tmp / "big.csv").write_bytes(HEADER.encode() + b"\n" + b"x" * (512 * 1024))
        self.refuses(self.tmp / "big.csv", "larger than 512 KiB")
        (self.tmp / "target.csv").write_text(HEADER + f"\n{row}\n", encoding="utf-8")
        (self.tmp / "link.csv").symlink_to(self.tmp / "target.csv")
        self.refuses(self.tmp / "link.csv", "symlinks are refused")
        (self.tmp / "folder.csv").mkdir()
        self.refuses(self.tmp / "folder.csv", "regular file")
        os.mkfifo(self.tmp / "pipe.csv")
        self.refuses(self.tmp / "pipe.csv", "regular file")
        self.refuses(self.tmp / "missing.csv", "cannot be opened")
        # physical start lines count blank lines and quoted multi-line cells
        multi = self.tmp / "lines.csv"
        multi.write_text(HEADER + f'\n{row}\n\n{self.a},m1,2026-03-02,email,"bad\n{RAW}\nref",\n'
                         f"{self.a},m2,2026-03-02,fax,,\n", encoding="utf-8")
        message = self.refuses(multi, "2 problem(s)", "line 4: reference", "line 7: channel")
        self.assertNotIn("line 3", message)
        self.refuses(multi, "line 4: reference", api=False)

    # 3
    def test_row_validation_and_atomic_refusal(self):
        stem_clash = f"{self.a}--narrowed"
        agencies = json.loads((self.kit / "agencies.json").read_text())
        agencies += [{"agency_id": stem_clash, "name": "Synthetic stem"}, {"agency_id": self.c, "name": "Dup"}]
        (self.kit / "agencies.json").write_text(json.dumps(agencies))
        bad = [("agency_id", f"Bad_ID{RAW},,2026-03-02,email,,"), ("agency_id", f"ca-unknown-{RAW.lower()},,2026-03-02,email,,"),
               ("agency_id", f"{self.c},,2026-03-02,email,,"),
               ("request", f"{self.a},Bad_{RAW},2026-03-02,email,,"), ("request", f"{self.a},../{RAW},2026-03-02,email,,"),
               ("request", f"{self.a},{'a' * 41},2026-03-02,email,,"), ("request", f"{self.a},narrowed,2026-03-02,email,,"),
               ("sent_on", f"{self.a},d1,2026-02-30,email,,"), ("sent_on", f"{self.a},d2,2026-3-1,email,,"),
               ("sent_on", f"{self.a},d3,2026-03-01T00:00,email,,"), ("sent_on", f"{self.a},d4,1999-12-31,email,,"),
               ("sent_on", f"{self.a},d5,2026-06-02,email,,"), ("sent_on", f"{self.a},d6,２０２６-03-01,email,,"),
               ("sent_on", f"{self.a},d7,,email,,"),
               ("channel", f"{self.a},c1,2026-03-02,fax,,"), ("channel", f"{self.a},c2,2026-03-02,EMAIL,,"),
               ("channel", f"{self.a},c3,2026-03-02,muckrock,,"), ("channel", f"{self.a},c4,2026-03-02,,,"),
               ("reference", f"{self.a},r1,2026-03-02,email,bad {RAW},"), ("reference", f"{self.a},r2,2026-03-02,email,{'R' * 41},"),
               ("letter_record", f"{self.a},l1,2026-03-02,email,,{RAW.lower()}"),
               ("letter_record", f"{self.a},l2,2026-03-02,email,,{synthetic('absent')[:12]}")]
        for field, row in bad:
            with self.subTest(row=row):
                good = f"{self.b},,2026-03-02,email,,"
                message = self.refuses(self.csv(good, row), "1 problem(s)", f"line 3: {field}: ",
                                       absent=(row.split(",")[0],))  # the valid row before it is not written either
                self.assertNotIn("line 2", message)
        self.assertFalse(self.sent.exists())
        # one bad row among many: all or none, late bad row included, every bad line named
        rows = [f"{self.a},p{i},2026-03-02,portal,," for i in range(30)] + [f"{self.a},late,2026-03-02,fax,,"]
        self.refuses(self.csv(*rows), "line 32: channel")
        self.assertFalse(self.sent.exists())

    # 4
    def test_existing_and_infile_duplicates(self):
        self.assertEqual(self.ordinary_send(self.a)[0], 0)
        normal = self.sent.read_bytes()
        message = self.refuses(self.csv(f"{self.a},,2026-03-02,email,,", f"{self.b},,2026-03-02,email,,"),
                               "line 2: request: already recorded")
        self.assertEqual(self.sent.read_bytes(), normal)
        self.assertEqual(self.api(self.csv(f"{self.a},named,2026-03-02,email,,"), apply=True)["imported"], 1)
        # a named key is isolated from main; exact rerun of imported rows refuses, no skip or replace
        again = self.csv(f"{self.a},named,2026-03-02,email,,")
        self.refuses(again, "line 2: request: already recorded")
        self.refuses(self.csv(f"{self.a},named,2026-03-02,email,,", f"{self.b},,2026-03-02,email,,"),
                     "line 2: request: already recorded")
        # within one file: empty request and explicit main collide; the first physical line is named
        self.refuses(self.csv(f"{self.b},,2026-03-02,email,,", f"{self.c},,2026-03-02,email,,",
                              f"{self.b},main,2026-03-03,portal,,"), "line 4: request: same request as line 2")
        self.assertEqual(self.api(self.csv(f"{self.b},,2026-03-02,email,,", f"{self.b},one,2026-03-02,email,,",
                                           f"{self.c},one,2026-03-02,email,,")), {"checked": 3, "imported": 0, "written": False})
        # a refused row never reserves its key: a later identical key is checked on its own
        message = self.refuses(self.csv(f"{self.b},dup,2026-03-02,fax,,", f"{self.b},dup,2026-03-02,email,,"),
                               "1 problem(s)", "line 2: channel")
        self.assertNotIn("line 3", message)
        # more than 20 problems: exact total, 20 shown, the trailer, nothing from the input
        message = self.refuses(self.csv(*[f"{self.b},n{i},2026-03-02,{RAW},{RAW}!,{RAW.lower()}" for i in range(9)]),
                               "27 problem(s)", "and 7 more")
        self.assertEqual(len(re.findall(r"^line \d+:", message, re.M)), 20)

    # 5
    def test_letter_binding_is_correspondence(self):
        twin = self.letters["x"][:12] + "0" * 52
        db = cli.database(self.root)
        with db:
            db.execute("INSERT INTO objects (sha256, byte_count, stored_at) VALUES (?, 1, '2026-03-01T00:00:00+00:00')", (twin,))
        db.close()
        ledger_before = (self.root / "private" / "ledger.sqlite").read_bytes()
        cases = {"missing": (synthetic("absent")[:16], "not ingested"), "ambiguous": (self.letters["x"][:12], "ambiguous"),
                 "nonhex": ("z" * 16, "give at least 12 lowercase hex"), "short": (self.letters["y"][:11], "give at least 12 lowercase hex"),
                 "upper": (self.letters["y"][:16].upper(), "give at least 12 lowercase hex"),
                 "long": (self.letters["y"] + "0", "give at least 12 lowercase hex")}
        for label, (prefix, reason) in cases.items():
            with self.subTest(label):
                self.refuses(self.csv(f"{self.a},,2026-03-02,email,,{prefix}"), "line 2: letter_record: " + reason,
                             absent=(prefix,))
        self.assertEqual(self.api(self.csv(f"{self.a},,2026-03-02,email,,{self.letters['y'][:12]}",
                                           f"{self.a},second,2026-03-03,email,,{self.letters['y'][:30]}",
                                           f"{self.b},,2026-03-03,email,,{self.letters['z']}"), apply=True)["imported"], 3)
        stored = self.stored()
        self.assertEqual(stored[self.a]["correspondence"], [self.letters["y"]])  # one FULL hash, entry level
        self.assertEqual(stored[f"{self.a}#second"]["correspondence"], [self.letters["y"]])  # same original, two requests
        self.assertEqual(stored[self.b]["correspondence"], [self.letters["z"]])
        self.assertEqual((self.root / "private" / "ledger.sqlite").read_bytes(), ledger_before)  # read-only lookup
        code, out, err = run("kit-status", "--directory", self.root)
        self.assertEqual(code, 0, err)
        self.assertNotIn(self.letters["y"], out)
        rows = {(r["agency_id"], r["request"]): r for r in json.loads(out)["requests"]}
        shown = rows[(self.a, "main")]
        self.assertEqual((shown["correspondence"], shown["correspondence_prefixes"], shown["records"]),
                         (1, [self.letters["y"][:12]], 0))  # a letter, never a production record
        self.assertEqual(rows[(self.b, "main")]["correspondence_prefixes"], [self.letters["z"][:12]])

    # 6
    def test_origin_validation_and_normal_gate(self):
        self.assertEqual(self.ordinary_send(self.c)[0], 0)
        normal = self.stored()[self.c]
        good = {"imported": True, "sent_on": "2026-03-02", "channel": "email", "recorded_at": "2026-03-03T10:00:00+00:00",
                "steps": []}
        contact = {"on": "2026-03-04", "channel": "phone", "role": "records clerk", "note": "Asked for status",
                   "recorded_at": "2026-03-04T10:00:00+00:00"}

        def readable(entry, key=self.a):
            self.sent.write_text(json.dumps({"schema_version": 1, "requests": {key: entry}}))
            try:
                kit_track.read_sent(self.root)
                return True
            except ValueError as exc:
                self.assertIn("unreadable", str(exc))
                return False
        self.assertTrue(readable(good))
        self.assertTrue(readable({**good, "reference": "R-1", "correspondence": [synthetic("a")], "contacts": [contact],
                                  "undo_ids": ["a" * 32], "extra_unrelated": {"kept": 1}}))
        self.assertTrue(readable(good, f"{self.a}#named"))
        draft = {"draft": f"requests/{self.a}.md", "draft_sha256": synthetic("d")}
        self.assertTrue(readable({k: v for k, v in good.items() if k != "imported"} | draft))  # an old entry reads as before
        for label, entry in {
                "draft path": {**good, "draft": draft["draft"]}, "draft hash": {**good, "draft_sha256": draft["draft_sha256"]},
                "both": {**good, **draft}, "false": {**good, "imported": False, "draft": None},
                "one": {**good, "imported": 1}, "string": {**good, "imported": "true"}, "null": {**good, "imported": None},
                "no origin": {k: v for k, v in good.items() if k != "imported"},
                "draft only": {k: v for k, v in good.items() if k != "imported"} | {"draft": draft["draft"]},
                "hash only": {k: v for k, v in good.items() if k != "imported"} | {"draft_sha256": draft["draft_sha256"]},
                "false with draft": {**good, "imported": False, **draft},
                "bad reference": {**good, "reference": "no good!"}, "bad corr": {**good, "correspondence": ["abc"]},
                "bad undo": {**good, "undo_ids": ["x"]}, "bad contact": {**good, "contacts": [{**contact, "on": "2026-03-01"}]},
                "contact keys": {**good, "contacts": [{"on": "2026-03-04"}]}, "bad day": {**good, "sent_on": "2026-02-30"},
                "bad channel": {**good, "channel": "fax"}, "bad stamp": {**good, "recorded_at": "yesterday"},
                "steps": {**good, "steps": "none"}}.items():
            with self.subTest(label):
                self.assertFalse(readable(entry))
        self.sent.write_text(json.dumps({"schema_version": 1, "requests": {self.c: normal}}))
        # ordinary kit-sent still needs a checked draft; the imported key can never be replaced by it
        code, out, err = run("kit-sent", "--directory", self.root, "--agency", self.b, "--date", "2026-03-02",
                             "--channel", "email")
        self.assertEqual(code, 1)
        self.assertIn("placeholder", err)
        self.assertEqual(self.api(self.csv(f"{self.a},,2026-03-02,email,,", f"{self.a},named,2026-03-02,email,,"),
                                  apply=True)["imported"], 2)
        ready_drafts(self.root)
        (self.kit / "requests" / f"{self.a}--named.md").write_text("Named draft.\n", encoding="utf-8")
        before = self.sent.read_bytes()
        for extra, request in (((), "main"), (("--replace",), "main"), (("--replace",), "named"), ((), "named")):
            with self.subTest(extra=extra, request=request), mock.patch.object(kit_track, "_draft", side_effect=AssertionError):
                code, out, err = run("kit-sent", "--directory", self.root, "--agency", self.a, "--date", "2026-03-05",
                                     "--channel", "email", "--request", request, *extra)
            self.assertEqual(code, 1, out)
            self.assertIn("imported requests cannot be replaced with kit-sent", err)
            self.assertEqual(self.sent.read_bytes(), before)

    # 7
    def test_readers_without_draft_access(self):
        self.assertEqual(self.ordinary_send(self.c)[0], 0)
        legacy = [r for r in kit_track.status(self.root)["requests"] if r["agency_id"] == self.c][0]
        self.assertIs(legacy["draft_changed_since_sent"], False)
        self.assertNotIn("imported", legacy)
        self.api(self.csv(f"{self.a},,2026-03-02,email,,", f"{self.a},named,2026-03-03,portal,R-9,",
                          f"{self.b},,2026-03-04,mail,,"), apply=True)
        draft_gone = self.kit / "requests" / f"{self.b}.md"
        draft_gone.unlink()
        real_draft = kit_track._draft

        def guarded(root, agency_id, request="main"):  # only the ordinary request may have its draft read
            if agency_id != self.c:
                raise AssertionError("draft read for an imported request")
            return real_draft(root, agency_id, request)
        with mock.patch.object(kit_track, "_draft", side_effect=guarded):
            rows = {(r["agency_id"], r["request"]): r for r in kit_track.status(self.root, today=TODAY)["requests"]
                    if r["agency_id"] != self.c}
            self.assertEqual(len(rows), 3)
            for row in rows.values():
                self.assertIsNone(row["draft_changed_since_sent"])
                self.assertIs(row["imported"], True)
                self.assertIsNotNone(row["determination_due"])  # the provisional date still comes from the sent day
            self.assertEqual(rows[(self.a, "named")]["reference"], "R-9")
            report = kit_track.due(self.root, "2026-06-01", today=TODAY)
            self.assertEqual({(r["agency_id"], r["request"]) for r in report["open"]},
                             {(self.a, "main"), (self.a, "named"), (self.b, "main"), (self.c, "main")})
            self.assertEqual(run("kit-calendar", "--directory", self.root, "--out", self.tmp / "cal.ics")[0], 0)
            self.assertEqual(run("today", "--directory", self.root)[0], 0)
            code, out, err = run("kit-status", "--directory", self.root, "--csv", self.tmp / "tracker.csv")
            self.assertEqual(code, 0, err)
            coverage = {r["agency_id"]: r for r in kit_track.coverage(self.root)["rows"]}
            self.assertIs(coverage[self.b]["drafted"], False)  # honest: this campaign never drafted it here
            self.assertEqual(coverage[self.b]["sent_on"], "2026-03-04")
            for gate in ("main", "named"):  # follow-up keeps its past-due gate and writes no original draft
                code, out, err = run("kit-followup", "--directory", self.root, "--agency", self.a, "--request", gate)
                self.assertEqual(code, 0, err)
        table = list(csv.reader(io.StringIO((self.tmp / "tracker.csv").read_text(encoding="utf-8"))))
        self.assertEqual(tuple(table[0]), kit_export.HEADER)
        column = kit_export.HEADER.index("draft_changed_since_sent")
        cells = {(row[0], row[2]): row[column] for row in table[1:]}
        self.assertEqual(cells[(self.a, "2026-03-02")], "")  # blank for an imported request
        self.assertEqual(cells[(self.c, "2026-03-02")], "no")  # ordinary rows keep their yes/no
        # existing gates: answered, closed and waiting stop a follow-up exactly as for any request
        for kind, on, code_wanted, words in (("clarification_requested", "2026-03-10", 3, "waiting on you"),
                                             ("determination", "2026-03-12", 1, "was answered")):
            step = run("kit-response", "--directory", self.root, "--agency", self.b, "--date", on, "--kind", kind)
            self.assertEqual(step[0], 0, step[2])
            code, out, err = run("kit-followup", "--directory", self.root, "--agency", self.b)
            self.assertEqual(code, code_wanted, err)
            self.assertIn(words, err)
            if kind == "clarification_requested":
                self.assertEqual(run("kit-response", "--directory", self.root, "--agency", self.b, "--date", "2026-03-11",
                                     "--kind", "clarified")[0], 0)

    # 8
    def test_dependency_timeline_contacts_and_undo_preserve_origin(self):
        self.api(self.csv(f"{self.a},,2026-03-02,email,R-7,{self.letters['x'][:12]}",
                          f"{self.a},named,2026-03-02,portal,,"), apply=True)
        origin = {k: v for k, v in self.stored()[self.a].items() if k not in ("steps",)}
        timeline = self.tmp / "timeline.csv"
        timeline.write_text("date,kind,note,amount,expected_on,partial\n2026-03-05,acknowledged,,,,\n"
                            "2026-03-10,extension,Letter received,,,\n", encoding="utf-8")
        args = ("--directory", self.root, "--agency", self.a, "--csv", timeline)
        before = self.sent.read_bytes()
        self.assertEqual(run("kit-response-import", *args)[0], 0)  # preview
        self.assertEqual(self.sent.read_bytes(), before)
        self.assertEqual(run("kit-response-import", *args, "--apply")[0], 0)
        entry = self.stored()[self.a]
        self.assertEqual([s["kind"] for s in entry["steps"]], ["acknowledged", "extension"])
        self.assertEqual({k: v for k, v in entry.items() if k != "steps"}, origin)
        code, out, err = run("kit-contact", "--directory", self.root, "--agency", self.a, "--on", "2026-03-12",
                             "--channel", "phone", "--with", "records clerk", "--note", "Asked for status")
        self.assertEqual(code, 0, err)
        code, out, err = run("kit-response-undo", "--directory", self.root, "--agency", self.a, "--reason", "typo")
        self.assertEqual(code, 0, err)
        entry = self.stored()[self.a]
        self.assertEqual([s["kind"] for s in entry["steps"]], ["acknowledged"])
        self.assertEqual(len(entry["contacts"]), 1)
        self.assertEqual(len(entry["undo_ids"]), 1)
        for key in ("imported", "sent_on", "channel", "recorded_at", "reference", "correspondence"):
            self.assertEqual(entry[key], origin[key])
        self.assertNotIn("draft", entry)
        self.assertNotIn("draft_sha256", entry)
        # the named import on the same agency keeps its own empty timeline and its own contacts
        self.assertEqual(self.stored()[f"{self.a}#named"]["steps"], [])
        self.assertNotIn("contacts", self.stored()[f"{self.a}#named"])
        kit_track.read_sent(self.root)  # and the whole store still validates
        # an import cannot rewrite what response-import and contact built on it
        self.refuses(self.csv(f"{self.a},,2026-03-02,email,R-7,"), "already recorded")

    # 9
    def test_store_faults_serialized_limit_and_writer_failure(self):
        path = self.csv(f"{self.a},,2026-03-02,email,,", f"{self.b},,2026-03-03,email,,")
        self.sent.write_text("{not json")
        self.refuses(path, "kit/sent.json unreadable")
        self.sent.write_text(json.dumps({"schema_version": 1, "requests": {self.a: {"sent_on": "2026-03-02"}}}))
        self.refuses(path, "kit/sent.json unreadable")
        self.sent.write_bytes(b" " * (kit_track.MAX_BYTES + 1))
        self.refuses(path, "larger than 1 MiB")
        self.sent.unlink()
        (self.tmp / "other.json").write_text(json.dumps({"schema_version": 1, "requests": {}}))
        self.sent.symlink_to(self.tmp / "other.json")
        self.refuses(path, "not a regular file")
        self.sent.unlink()
        agencies = self.kit / "agencies.json"
        original = agencies.read_bytes()
        agencies.write_text("{broken")
        self.refuses(path, "kit/agencies.json unreadable")
        agencies.write_text(json.dumps({"agency_id": self.a}))
        self.refuses(path, "kit/agencies.json unreadable")
        agencies.unlink()
        agencies.symlink_to(self.tmp / "other.json")
        self.refuses(path, "kit/agencies.json is missing")
        agencies.unlink()
        self.refuses(path, "kit/agencies.json is missing")
        agencies.write_bytes(original)
        # a missing kit folder, a symlinked kit folder
        moved = self.tmp / "moved-kit"
        self.kit.rename(moved)
        self.refuses(path, "run kit first")
        self.kit.symlink_to(moved)
        self.refuses(path, "run kit first")
        self.kit.unlink()
        moved.rename(self.kit)
        # final serialized size is checked before any write
        with mock.patch.object(kit_track, "MAX_BYTES", 400):
            self.refuses(path, "larger than 1 MiB")
        # a writer that fails is a failure, never a success summary
        with mock.patch.object(kit, "_write_json", side_effect=OSError("disk full")):
            self.refuses(path, "could not be written", absent=("disk full",))
        before = snapshot(self.root)
        with mock.patch("os.replace", side_effect=OSError("replace failed")):
            code, out, err = self.do(path, "--apply")
        self.assertEqual((code, out), (2, ""))
        self.assertTrue(err.startswith("Stopped: "))
        self.assertEqual(snapshot(self.root), before)  # the temp file is gone and nothing replaced
        # a missing sent.json is an empty store: apply creates only that one file
        files = {p for p in self.root.rglob("*")}
        self.assertEqual(self.do(path, "--apply")[0], 0)
        self.assertEqual({p for p in self.root.rglob("*")} - files, {self.sent})
        # an existing store is kept entry for entry
        extra = self.csv(f"{self.c},,2026-03-04,email,,", name="more.csv")
        old = self.stored()
        self.assertEqual(self.do(extra, "--apply")[0], 0)
        self.assertEqual({k: v for k, v in self.stored().items() if k != self.c}, old)

    # 10
    def test_cli_help_privacy_and_offline(self):
        code, out, err = run("kit-sent-import", "--help")
        self.assertEqual(code, 0)
        for word in ("--directory", "--csv", "--apply"):
            self.assertIn(word, out)
        self.assertEqual(run("kit-sent-import", "--directory", self.root)[0], 2)  # argparse misuse
        self.assertEqual(run("kit-sent-import", "--csv", "x.csv")[0], 2)
        private_reference = "SECRET-REF-1"
        path = self.csv(f"{self.a},contract,2026-03-02,email,{private_reference},{self.letters['x'][:14]}")
        for extra in ((), ("--apply",)):
            code, out, err = self.do(path, *extra)
            self.assertEqual(code, 0, err)
            terminal = out + err
            self.assertEqual(set(json.loads(out)), {"checked", "imported", "written"})
            for value in (private_reference, self.letters["x"][:14], self.a, "contract", "2026-03-02", "Traceback"):
                self.assertNotIn(value, terminal)
            self.assertIsNone(FULL_HEX.search(terminal))
        # a refusal exits 2 with one Stopped block and names no input value
        code, out, err = self.do(self.csv(f"{self.a},{RAW},2026-03-02,email,,"), "--apply")
        self.assertEqual((code, out), (2, ""))
        self.assertTrue(err.startswith("Stopped: "))
        self.assertNotIn(RAW, err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(sqlite3.connect(self.root / "private" / "ledger.sqlite").execute(
            "SELECT count(*) FROM objects").fetchone()[0], 3)  # no ingest happened


if __name__ == "__main__":
    unittest.main()
