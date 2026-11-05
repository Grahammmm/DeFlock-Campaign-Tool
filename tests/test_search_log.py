"""kit-search-log: private streamed counts of a locally saved ALPR search log. Synthetic fixtures only; nothing is fetched."""
import contextlib
import hashlib
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import registry, search_log  # noqa: E402
from campaign_tool.cli import main  # noqa: E402

AID = "ca-synthetic-police"
WARNING = "A count is a lead, not evidence"
SENTINELS = ("SENTUSER", "CASE-SENT", "7ABC123", "SENTPURPOSE")
HEADER = "Timestamp,User Name,Case Number,Plate,Search Purpose,Organization,State"
ROWS = [  # hand-counted below
    '2026-01-05 10:00:00,SENTUSER1,CASE-SENT1,7ABC123,"Robbery case\r\nSENTPURPOSE1",Alpha PD,CA',
    "2026-01-20T09:00:00Z,SENTUSER2,CASE-SENT2,7ABC123,investigation,Alpha PD,CA",
    "02/03/2026 1:15:00 PM,SENTUSER3,CASE-SENT3,7ABC123,ICE hold SENTPURPOSE3,Beta SO,NV",
    "2026-02-10,SENTUSER4,CASE-SENT4,7ABC123,,Alpha PD,CA",
    "2026-02-11 08:00,SENTUSER5,CASE-SENT5,7ABC123,N/A,Alpha PD,",
    "2026-02-12 08:00,SENTUSER6,CASE-SENT6,7ABC123,office visit,,CA",
    ",,,,,,",
    "",
    "yesterday,SENTUSER7,CASE-SENT7,7ABC123,theft,Alpha PD,CA",
    "2026-03-01,SENTUSER8",
]


def log_bytes(rows=ROWS, header=HEADER, bom=True, newline="\r\n"):
    return (("\ufeff" if bom else "") + newline.join([header, *rows]) + newline).encode("utf-8")


def tree(*roots):
    snap = {}
    for root in roots:
        for path in sorted(Path(root).rglob("*")):
            snap[str(path)] = path.read_bytes() if path.is_file() else None
    return snap


class SearchLogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-search-log-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(os.path.realpath(temporary.name))
        self.root, self.files = self.base / "campaign", self.base / "files"
        (self.root / "kit").mkdir(parents=True)
        self.files.mkdir()
        (self.root / "campaign.json").write_text(json.dumps(
            {"schema_version": 1, "name": "Synthetic", "county": "Synthetic", "state": "CA"}), encoding="utf-8")
        (self.root / "kit" / "agencies.json").write_text(json.dumps([
            {"agency_id": AID, "name": "Synthetic Police Department", "kind": "police", "selected": False},
            {"agency_id": "ca-other-sheriff", "name": "Other Sheriff", "kind": "sheriff"}]), encoding="utf-8")
        patch = mock.patch.dict(os.environ, {registry.ENV: str(self.base / "config" / "campaigns.json")})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(registry.release)

    def csv(self, data=None, name="log.csv"):
        path = self.files / name
        path.write_bytes(log_bytes() if data is None else data)
        return path

    def run_cli(self, *args, directory=True):
        argv = ["campaign_tool", *(() if args[0] == "campaigns" else ("kit-search-log",)), *map(str, args)]
        if directory:
            argv += ["--directory", str(self.root)]
        out, err, code = io.StringIO(), io.StringIO(), 0
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                main()
            except SystemExit as exc:
                code = exc.code or 0
            finally:
                registry.release()
        return code, out.getvalue(), err.getvalue()

    def report(self, path, *extra):
        code, out, err = self.run_cli("--csv", path, "--agency", AID, "--json", *extra)
        self.assertEqual((code, err), (0, ""), out)
        return json.loads(out)

    def refused(self, *args, words=(), directory=True):
        code, out, err = self.run_cli(*args, directory=directory)
        self.assertEqual((code, out), (1, ""), err)
        self.assertTrue(err.startswith("Stopped: "), err)
        for word in words:
            self.assertIn(word, err)
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, err)
        return err

    def test_aliases_overrides_and_json_contract(self):
        path = self.csv()
        data = log_bytes()
        summary = self.report(path)
        self.assertEqual(summary["schema_version"], 1)
        self.assertEqual(list(summary), ["schema_version", "agency_id", "agency_name", "input", "period", "rows", "months",
                                         "organizations", "purposes"])
        self.assertEqual((summary["agency_id"], summary["agency_name"]), (AID, "Synthetic Police Department"))  # unselected is fine
        self.assertEqual(summary["input"], {"sha256": hashlib.sha256(data).hexdigest(), "categories_sha256": None, "columns": {
            "time": "Timestamp", "reason": "Search Purpose", "org": "Organization", "state": "State"}})
        self.assertEqual(summary["period"], {"first_date": "2026-01-05", "last_date": "2026-02-12"})  # as stated, no zone shift
        self.assertEqual(summary["rows"], {"warning": WARNING, "read": 10, "counted": 6,
                                           "skipped": {"blank_row": 2, "column_count": 1, "invalid_time": 1}})
        self.assertEqual(summary["months"], {"warning": WARNING, "rows": [{"month": "2026-01", "count": 2},
                                                                          {"month": "2026-02", "count": 4}]})
        self.assertEqual(summary["organizations"], {"warning": WARNING, "unattributed": 1, "rows": [
            {"name": "Alpha PD", "state": None, "count": 1}, {"name": "Alpha PD", "state": "CA", "count": 3},
            {"name": "Beta SO", "state": "NV", "count": 1}]})
        self.assertEqual(summary["purposes"], {"warning": WARNING, "counts": {
            "blank_or_na": 2, "immigration_related": 1, "vague": 1, "other": 2}})
        rows = summary["rows"]
        self.assertEqual(rows["read"], rows["counted"] + sum(rows["skipped"].values()))
        self.assertEqual(sum(r["count"] for r in summary["months"]["rows"]), rows["counted"])
        self.assertEqual(sum(summary["purposes"]["counts"].values()), rows["counted"])
        self.assertEqual(sum(r["count"] for r in summary["organizations"]["rows"]) + summary["organizations"]["unattributed"],
                         rows["counted"])
        text_code, text, _ = self.run_cli("--csv", path, "--agency", AID)
        self.assertEqual(text_code, 0)
        self.assertEqual(text.count(WARNING), 4)  # one per section
        for line in ("rows read: 10", "2026-02: 4", "Alpha PD [CA]: 3", "immigration_related: 1"):
            self.assertIn(line, text)
        # explicit overrides with unusual headers; an override is an exact match
        odd = self.csv(b"When,Why,Who,Where,Name\n2026-05-01,x,Gamma PD,TX,SENTUSER\n", "odd.csv")
        columns = ("--time-col", "When", "--reason-col", "Why", "--org-col", "Who", "--state-col", "Where")
        self.assertEqual(self.report(odd, *columns)["organizations"]["rows"], [{"name": "Gamma PD", "state": "TX", "count": 1}])
        self.refused("--csv", odd, "--agency", AID, *columns[:6], "--state-col", "where", words=("--state-col",))
        # custom categories replace the defaults completely; their hash is the raw file hash
        cats = self.files / "cats.json"
        cats.write_text(json.dumps({"blank_or_na": ["unknown"], "vague": ["inv"], "immigration_related": ["roundup"]}))
        custom = self.report(self.csv(log_bytes(['2026-01-01,u,c,p,Roundup tonight,A,CA', '2026-01-02,u,c,p,ICE,A,CA',
                                                 '2026-01-03,u,c,p,unknown,A,CA', '2026-01-04,u,c,p,Investigation,A,CA',
                                                 '2026-01-05,u,c,p,inv,A,CA', '2026-01-06,u,c,p,,A,CA'], bom=False), "c.csv"),
                             "--categories", cats)
        self.assertEqual(custom["purposes"]["counts"], {"blank_or_na": 2, "immigration_related": 1, "vague": 1, "other": 2})
        self.assertEqual(custom["input"]["categories_sha256"], hashlib.sha256(cats.read_bytes()).hexdigest())

    def test_missing_ambiguous_duplicate_headers(self):
        cases = {
            "missing time": (b"When,Purpose\n", (), ("--time-col",)),
            "missing reason": (b"Time,SENTHEADER\n", (), ("--reason-col",)),
            "explicit absent": (b"Time,Purpose\n", ("--org-col", "SENTHEADER"), ("--org-col",)),
            "conflicting time aliases": (b"Time,Date,Purpose\n", (), ("--time-col",)),
            "conflicting org aliases": (b"Time,Purpose,Organization,Agency\n", (), ("--org-col",)),
            "conflicting state aliases": (b"Time,Purpose,State,Agency State\n", (), ("--state-col",)),
            "duplicate normalized": (b"Time,Purpose,SENT HEADER,sent_header\n", (), ("same",)),
            "empty header": (b"Time,Purpose,\n", (), ("empty",)),
            "role reuse": (b"Time,Purpose\n", ("--reason-col", "Time"), ("two roles",)),
            "no header": (b"", (), ("no header",)),
        }
        for label, (data, extra, words) in cases.items():
            with self.subTest(label):
                err = self.refused("--csv", self.csv(data), "--agency", AID, *extra, words=words)
                self.assertNotIn("SENTHEADER", err)
                self.assertNotIn("sent_header", err)
                self.assertNotIn("SENT HEADER", err)
        # aliases are case, space and underscore blind
        self.assertEqual(self.report(self.csv(b"SEARCH_TIME,  Stated   Purpose ,Searching_Organization\n2026-01-01,x,Z\n"))[
            "input"]["columns"], {"time": "SEARCH_TIME", "reason": "  Stated   Purpose ", "org": "Searching_Organization", "state": None})
        self.assertEqual(self.report(self.csv(b"Time,Purpose\n2026-01-01,x\n"))["organizations"], {
            "warning": WARNING, "unattributed": 1, "rows": []})  # org column unavailable: unattributed, not zero searches
        self.assertIn("not available", self.run_cli("--csv", self.csv(b"Time,Purpose\n2026-01-01,x\n"), "--agency", AID)[1])
        self.refused("--csv", self.csv(b"Time,Purpose\n"), "--agency", AID, "--time-col", "T\x07", words=("control",))

    def test_skipped_rows_and_empty_period(self):
        rows = ["", " , ", "2026-13-01,a", "2026-02-30,a", "02/03/2026 25:00:00,a", "13/02/2026 10:00:00,a", "2026-01-01 10:00 PM,a",
                "2026-01-01T10:00:00+25:00,a", "2026-01-01,a,extra", "01/31/2026 12:00:00 am,a", "2026-01-02T01:02:03.456-07:00,"]
        summary = self.report(self.csv(("Time,Purpose\n" + "\n".join(rows) + "\n").encode()))
        self.assertEqual(summary["rows"]["skipped"], {"blank_row": 2, "column_count": 1, "invalid_time": 6})
        self.assertEqual((summary["rows"]["read"], summary["rows"]["counted"]), (11, 2))  # blank purpose is a counted search
        self.assertEqual([r["month"] for r in summary["months"]["rows"]], ["2026-01"])
        for rows_, label in (([], "header only"), (["bad,a", ",,"], "all skipped")):
            with self.subTest(label):
                empty = self.report(self.csv(("Time,Purpose\n" + "\n".join(rows_) + "\n").encode()))
                self.assertEqual(empty["period"], {"first_date": None, "last_date": None})
                self.assertEqual((empty["months"]["rows"], empty["rows"]["counted"]), ([], 0))
                self.assertEqual(sum(empty["purposes"]["counts"].values()), 0)

    def test_purpose_word_boundaries_and_precedence(self):
        purposes = {"N/A": "blank_or_na", "  none ": "blank_or_na", "Not  Applicable": "blank_or_na", "NULL": "blank_or_na",
                    "ICE": "immigration_related", "ice hold": "immigration_related", "Border   PATROL sweep": "immigration_related",
                    "ICE/CBP": "immigration_related", "office": "other", "Police Services": "other", "price": "other",
                    "Inv": "vague", "INVESTIGATION": "vague", "inv 22-114": "other", "investigation of theft": "other",
                    "deportation": "immigration_related", "immigrants": "other", "homeland security task": "immigration_related"}
        data = "Time,Purpose\n" + "".join('2026-01-01,"%s"\n' % p for p in purposes)
        counts = self.report(self.csv(data.encode()))["purposes"]["counts"]
        expected = {k: sum(1 for v in purposes.values() if v == k) for k in counts}
        self.assertEqual(counts, expected)
        self.assertEqual(sum(counts.values()), len(purposes))

    def test_privacy_no_writes_no_network(self):
        path = self.csv()
        before = tree(self.base)

        def boom(*a, **k):
            raise AssertionError("network or store used")
        with mock.patch.object(socket.socket, "connect", boom), mock.patch.object(sqlite3, "connect", boom), \
                mock.patch("campaign_tool.cli.database", boom, create=True):
            outputs = [self.run_cli("--csv", path, "--agency", AID), self.run_cli("--csv", path, "--agency", AID, "--json"),
                       self.run_cli("--csv", path, "--agency", "ca-nope"), self.run_cli("--csv", path, "--agency", AID, "--time-col", "SENTUSER9")]
        self.assertEqual([o[0] for o in outputs], [0, 0, 1, 1])
        for _, out, err in outputs:
            for sentinel in SENTINELS + (str(path),):
                self.assertNotIn(sentinel, out + err)
        self.assertEqual(tree(self.base), before)  # no new path, no changed byte; no private/ or ledger created
        self.assertFalse((self.root / "private").exists())
        # a real process: nothing else on stdout/stderr
        done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "kit-search-log", "--directory", str(self.root), "--csv",
                               str(path), "--agency", AID, "--json"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertEqual(done.stdout, json.dumps(json.loads(done.stdout), ensure_ascii=True, indent=2) + "\n")
        self.assertEqual(tree(self.base), before)
        # emitted metadata that trips the shared privacy lint or holds a control character refuses, naming only place and kind
        for label, row, flags in (
                ("email org", "2026-01-01,x,jdoe@example.org,CA", ()), ("plate state", "2026-01-01,x,Alpha PD,7ABC123", ()),
                ("phone org", "2026-01-01,x,(805) 555-1234,CA", ()), ("control org", '2026-01-01,x,"Alpha\x07PD",CA', ()),
                ("multiline org", '2026-01-01,x,"Alpha\nPD",CA', ())):
            with self.subTest(label):
                err = self.refused("--csv", self.csv(("Time,Purpose,Org,State\n" + row + "\n").encode()), "--agency", AID, "--org-col", "Org",
                                   "--state-col", "State", words=("row 1",))
                for leak in ("jdoe", "555", "7ABC123", "Alpha"):
                    self.assertNotIn(leak, err)
        err = self.refused("--csv", self.csv(b"Time,Purpose,jdoe@example.org\n"), "--agency", AID, "--org-col", "jdoe@example.org",
                           words=("privacy lint",))
        self.assertNotIn("jdoe", err)

    def test_limits_streaming_and_mutation(self):
        with mock.patch.object(search_log, "MAX_BYTES", 200):
            exact = b"Time,Purpose\n" + b"2026-01-01,x\n" * 11 + b"2026-01-02,y\n"
            exact += b"\n" * (200 - len(exact))
            self.assertEqual(len(exact), 200)
            self.assertEqual(self.report(self.csv(exact, "exact.csv"))["rows"]["counted"], 12)
            self.refused("--csv", self.csv(exact + b"\n", "plus.csv"), "--agency", AID, words=("larger than",))
            grow = self.csv(exact, "grow.csv")
            real = search_log.csv.reader

            def grower(*a, **k):
                with open(grow, "ab") as handle:
                    handle.write(b"2026-01-03,z\n")
                return real(*a, **k)
            with mock.patch.object(search_log.csv, "reader", grower):
                self.refused("--csv", grow, "--agency", AID, words=("grew past",))
        big = self.files / "huge.csv"
        with open(big, "wb") as handle:
            handle.truncate(100 * 1024 * 1024 + 1)  # sparse: refused from its size before any byte is read
        self.refused("--csv", big, "--agency", AID, words=("100 MiB",))
        # a valid large file is streamed in bounded reads and hashed whole, BOM included
        large = log_bytes(["2026-03-%02d,u,c,p,Robbery,Alpha PD,CA" % (i % 28 + 1) for i in range(12000)])
        calls = []
        real_read = search_log._Reader.readinto

        def counting(self, buffer):
            calls.append(len(buffer))
            return real_read(self, buffer)
        with mock.patch.object(search_log._Reader, "readinto", counting):
            summary = self.report(self.csv(large, "large.csv"))
        self.assertEqual(summary["rows"]["counted"], 12000)
        self.assertEqual(summary["input"]["sha256"], hashlib.sha256(large).hexdigest())
        self.assertGreater(len(calls), 5)
        self.assertLessEqual(max(calls), 64 * 1024)
        # observed mutation while reading
        target = self.csv(large[:2000] + b"\n", "mut.csv")
        real = search_log.csv.reader

        def mutate(*a, **k):
            os.utime(target, ns=(1, 1))
            return real(*a, **k)
        with mock.patch.object(search_log.csv, "reader", mutate):
            self.refused("--csv", target, "--agency", AID, words=("changed while",))
        # file kinds, encodings, syntax
        os.mkfifo(self.files / "pipe.csv")
        os.symlink(self.csv(None, "real.csv"), self.files / "link.csv")
        for name in ("pipe.csv", "link.csv", ".", "missing.csv"):
            with self.subTest(name):
                err = self.refused("--csv", self.files / name, "--agency", AID, words=("csv",))
                self.assertNotIn(str(self.files), err)
        self.refused("--csv", self.csv(b"Time,Purpose\n2026-01-01,\xff\xfe\n", "bad.csv"), "--agency", AID, words=("UTF-8",))
        self.refused("--csv", self.csv(b'Time,Purpose\n2026-01-01,"SENTPURPOSE"x\n', "quote.csv"), "--agency", AID, words=("line 2",))
        self.refused("--csv", self.csv(b'Time,Purpose\n2026-01-01,"SENTPURPOSE\n', "open.csv"), "--agency", AID, words=("malformed",))
        # custom category files
        good = {"blank_or_na": [], "vague": [], "immigration_related": []}
        bad = {"duplicate key": ('{"vague": [], "vague": [], "blank_or_na": [], "immigration_related": []}', "duplicate"),
               "missing key": (json.dumps({"vague": []}), "exactly the keys"),
               "extra key": (json.dumps({**good, "other": []}), "exactly the keys"),
               "not a list": (json.dumps({**good, "vague": "inv"}), "vague"),
               "101 terms": (json.dumps({**good, "vague": [str(i) for i in range(101)]}), "vague"),
               "repeated term": (json.dumps({**good, "vague": ["a", "a"]}), "vague"),
               "long term": (json.dumps({**good, "blank_or_na": ["x" * 81]}), "blank_or_na"),
               "control char": (json.dumps({**good, "immigration_related": ["a\u0007b"]}), "immigration_related"),
               "empty immigration term": (json.dumps({**good, "immigration_related": [""]}), "immigration_related"),
               "not json": ("{SENTPURPOSE", "valid UTF-8 JSON"), "too big": (json.dumps({**good, "vague": ["x" * 70] * 100}) + " " * 70000, "64 KiB")}
        for label, (text, word) in bad.items():
            with self.subTest(label):
                cats = self.files / "bad.json"
                cats.write_text(text)
                err = self.refused("--csv", self.csv(), "--agency", AID, "--categories", cats, words=(word,))
                self.assertNotIn("SENTPURPOSE", err)
        os.symlink(self.files / "bad.json", self.files / "cats-link.json")
        self.refused("--csv", self.csv(), "--agency", AID, "--categories", self.files / "cats-link.json", words=("symlink",))

    def test_long_fields_and_deeply_nested_categories(self):
        big = "x" * (300 * 1024)  # above csv's default 128 KiB field limit, far below 100 MiB
        before = search_log.csv.field_size_limit()
        row = f"2026-01-05 10:00:00,U,C,P,{big},Alpha PD,CA"  # long purpose; the long cells are only counted
        summary = self.report(self.csv(log_bytes(rows=[row, row.replace("Alpha", big)]), "long.csv"))
        self.assertEqual(summary["rows"]["read"], 2)
        self.assertEqual(search_log.csv.field_size_limit(), before)  # restored
        nested = self.files / "nested.json"
        nested.write_text("[" * 60000)
        err = self.refused("--csv", self.csv(), "--agency", AID, "--categories", nested, words=("valid UTF-8 JSON",))
        self.assertNotIn("Traceback", err)

    def test_known_agency_and_campaign_scope(self):
        path = self.csv()
        self.refused("--csv", path, "--agency", "ca-nope", words=("--agency",))
        self.refused("--csv", path, "--agency", AID + "\n", words=("--agency",))
        self.refused("--csv", path, "--agency", "Synthetic Police Department", words=("--agency",))  # an id, never a name
        (self.root / "kit" / "agencies.json").rename(self.root / "kit" / "moved.json")
        self.refused("--csv", path, "--agency", AID, words=("run kit first",))  # before the CSV is opened
        self.refused("--csv", self.files / "missing.csv", "--agency", AID, words=("run kit first",))
        (self.root / "kit" / "moved.json").rename(self.root / "kit" / "agencies.json")
        for missing in (("--agency", AID), ("--csv", path)):
            self.assertEqual(self.run_cli(*missing)[0], 2)
        self.assertEqual(self.run_cli("--csv", path, "--agency", AID, directory=False)[0], 2)  # --directory or --campaign required
        # --campaign A works; the log may not live inside another registered campaign
        other = self.base / "other"
        other.mkdir()
        (other / "campaign.json").write_text((self.root / "campaign.json").read_text())
        for name, directory in (("A", self.root), ("B", other)):
            self.assertEqual(self.run_cli("campaigns", "add", name, "--directory", directory, directory=False)[0], 0)
        (other / "inside.csv").write_bytes(log_bytes())
        code, out, err = self.run_cli("--csv", path, "--agency", AID, "--json", "--campaign", "A", directory=False)
        self.assertEqual((code, json.loads(out)["agency_id"]), (0, AID), err)
        self.assertEqual(self.run_cli("--csv", path, "--agency", AID, "--campaign", "A", "--directory", self.root, directory=False)[0], 2)
        code, out, err = self.run_cli("--csv", other / "inside.csv", "--agency", AID, "--campaign", "A", directory=False)
        self.assertEqual((code, out), (1, ""), err)
        # the guide is linked, documents every alias and its JSON example matches the executable schema
        docs = (REPO_ROOT / "docs" / "SEARCH-LOG.md").read_text(encoding="utf-8")
        self.assertIn("](SEARCH-LOG.md)", (REPO_ROOT / "docs" / "AGENCY-DISCOVERY.md").read_text(encoding="utf-8"))
        for aliases in search_log.ALIASES.values():
            for alias in aliases:
                self.assertIn(alias, docs)
        example = json.loads(docs.split("```json\n", 1)[1].split("```", 1)[0])
        actual = self.report(self.csv(b"Time,Purpose\n"))

        def shape(value):
            return {k: shape(v) for k, v in value.items()} if isinstance(value, dict) else type(value).__name__
        self.assertEqual(list(example), list(actual))
        self.assertEqual({k: v for k, v in shape(example).items() if k not in ("period", "input", "months", "organizations")},
                         {k: v for k, v in shape(actual).items() if k not in ("period", "input", "months", "organizations")})
        self.assertEqual(example["rows"]["warning"], WARNING)
        self.assertEqual(sorted(example["input"]["columns"]), sorted(actual["input"]["columns"]))


if __name__ == "__main__":
    unittest.main()
