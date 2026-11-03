"""tools/records_search.py: private index, cited hits with no document text, honest coverage, never public."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "records_search.py"
_spec = importlib.util.spec_from_file_location("_records_search_under_test", TOOL)
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)
from campaign_tool.records import public_scan  # noqa: E402

SECRET_PHRASE = "zebra quartz lantern"
EMAIL, PHONE, PLATE, NAME = "jane.roe@example.org", "(805) 555-0142", "7ABC123", "Marguerite Okonkwo-Tal"
PREFIX = "Résumé — "  # multi-byte prefix: offsets are UTF-8 bytes


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def units(*pages):
    return "".join(json.dumps({"kind": "pdf_page", "locator": {"page": n}, "text": t}) + "\n" for n, t in pages)


class Store:
    """Ten synthetic documents: seven with text, one absent and one empty extraction, one unreadable."""

    def __init__(self, base):
        self.root = Path(base) / "store"
        self.docs = {}
        texts = {
            ("slo-county", "doc-01"): units((1, "Intro page"), (2, PREFIX + "the ALPR camera contract")),
            ("slo-county", "doc-02"): units((1, "Retention policy: thirty days")),
            ("slo-county", "doc-03"): units((1, "Audit log for " + SECRET_PHRASE + " and " + EMAIL)),
            ("slo-county", "doc-04"): units((3, "Contact " + PHONE + " plate " + PLATE + " officer " + NAME)),
            ("slo-county", "doc-05"): None,
            ("arroyo-grande", "doc-06"): units((1, "Sharing agreement"), (4, "camera placement map")),
            ("arroyo-grande", "doc-07"): units((1, "Invoice")),
            ("arroyo-grande", "doc-08"): units((1, "Training record")),
            ("arroyo-grande", "doc-09"): units((1, "   ")),
            ("arroyo-grande", "doc-10"): b"\xff\xfe not json",
        }
        for (agency, doc_id), body in texts.items():
            self.add(agency, doc_id, body)

    def add(self, agency, doc_id, body):
        original = sha(agency + doc_id)
        self.docs[(agency, doc_id)] = original
        folder = self.root / "agencies" / agency
        folder.mkdir(parents=True, exist_ok=True)
        rows = ["request,doc_id,sha256,analysis_only"] + [
            "R-%s,%s,%s,no" % (d[-2:], d, s) for (a, d), s in self.docs.items() if a == agency]
        (folder / "INVENTORY.csv").write_text("\n".join(rows) + "\n")
        if body is not None:
            self.write(agency, doc_id, body)

    def write(self, agency, doc_id, body):
        path = self.root / "agencies" / agency / "extracted" / self.docs[(agency, doc_id)] / "units.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body if isinstance(body, bytes) else body.encode())


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = rs.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.store = Store(self.base)
        self.out = self.base / "index"

    def tearDown(self):
        self._tmp.cleanup()

    def index(self, out=None):
        return run("index", "--store", str(self.store.root), "--out", str(out or self.out))

    def query(self, terms):
        return run("query", "--index", str(self.out), terms)

    def rows(self, stdout):
        return [line.split(" | ") for line in stdout.splitlines()[1:] if line.count(" | ") == 5]


class DoneWhenTest(Base):
    def test_index_query_stat_and_coverage(self):
        code, out, err = self.index()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines(), [
            "index: documents=10 changed=10 unchanged=0 removed=0", "no-text: absent=1 empty=1",
            "coverage: indexed=7 no-text=2 unreadable=1; extracted text is unreviewed"])
        self.assertEqual(stat.S_IMODE(os.stat(self.out).st_mode), 0o700)
        self.assertEqual(os.listdir(self.out), [rs.INDEX_NAME])
        self.assertEqual(stat.S_IMODE(os.stat(self.out / rs.INDEX_NAME).st_mode), 0o600)
        code, out, _ = self.query("camera")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], "document_id | page | offset | request | agency | sha256")
        self.assertEqual(self.rows(out), [
            ["doc-06", "4", "0", "R-06", "arroyo-grande", self.store.docs[("arroyo-grande", "doc-06")]],
            ["doc-01", "2", str(len((PREFIX + "the ALPR ").encode())), "R-01", "slo-county",
             self.store.docs[("slo-county", "doc-01")]]])
        self.assertEqual(lines[-3:], ["hits=2", "no-text: absent=1 empty=1",
                                      "coverage: indexed=7 no-text=2 unreadable=1; extracted text is unreviewed"])
        for word in ("camera", "contract", "placement", "ALPR", "Résumé"):
            self.assertNotIn(word, out)
        code, out, _ = run("stat", "--index", str(self.out))
        self.assertEqual((code, out.splitlines()[0]), (0, "index: documents=10 pages=9"))
        self.assertTrue(out.endswith("coverage: indexed=7 no-text=2 unreadable=1; extracted text is unreviewed\n"))

    def test_phrases_and_operators(self):
        self.index()
        self.assertEqual([r[0] for r in self.rows(self.query('"camera placement"')[1])], ["doc-06"])
        self.assertEqual([r[0] for r in self.rows(self.query("invoice OR retention")[1])], ["doc-07", "doc-02"])
        self.assertEqual([r[0] for r in self.rows(self.query("camera contract")[1])], ["doc-01"])
        self.assertEqual([r[0] for r in self.rows(self.query("camera AND invoice")[1])], [])

    def test_zero_hits_caveat_vs_plain(self):
        self.index()
        code, out, _ = self.query("helicopter")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["no hits in indexed text; 2 documents have no extracted text",
                                            "no-text: absent=1 empty=1",
                                            "coverage: indexed=7 no-text=2 unreadable=1; extracted text is unreviewed"])
        self.assertNotIn("no results", out)
        self.store.write("slo-county", "doc-05", units((1, "now extracted")))
        self.store.write("arroyo-grande", "doc-09", units((1, "now extracted too")))
        self.store.write("arroyo-grande", "doc-10", units((1, "repaired")))
        self.index()
        self.assertEqual(self.query("helicopter")[1].splitlines(), [
            "no hits in indexed text", "no-text: absent=0 empty=0",
            "coverage: indexed=10 no-text=0 unreadable=0; extracted text is unreviewed"])

    def test_three_distinct_missing_text_states(self):
        self.index()
        with contextlib.closing(sqlite3.connect(self.out / rs.INDEX_NAME)) as conn:
            got = dict((d, (s, r)) for d, s, r in conn.execute("SELECT doc_id, state, reason FROM docs"))
        self.assertEqual((got["doc-05"], got["doc-09"], got["doc-10"]),
                         (("no-text", "absent"), ("no-text", "empty"), ("unreadable", "not_utf8_json_units")))


class NoTextEverPrintedTest(Base):
    def test_planted_phrase_and_personal_data_never_printed(self):
        outputs = [self.index()]
        for terms in ('"%s"' % SECRET_PHRASE, '"%s"' % EMAIL, "lantern", PLATE, '"Okonkwo-Tal"', "officer"):
            outputs.append(self.query(terms))
            self.assertEqual(len(self.rows(outputs[-1][1])), 1, terms)
        outputs += [run("stat", "--index", str(self.out)), self.query('"zebra'), self.query("zebra.*")]
        folder = self.base / "outputs"
        folder.mkdir()
        for n, (_, out, err) in enumerate(outputs):
            for value in (SECRET_PHRASE, "zebra", "quartz", "lantern", EMAIL, "555-0142", PLATE, NAME, "Okonkwo",
                          "Marguerite", "Audit log", "officer"):
                self.assertNotIn(value, out + err)
            (folder / ("out-%02d.txt" % n)).write_text(out + err)
        scan = subprocess.run([sys.executable, "-B", str(ROOT / "tools" / "flock_privacy_scan.py"), str(folder)],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(scan.returncode, 0, scan.stdout)
        self.assertIn("findings=0", scan.stdout)


class IndexDirRefusalTest(Base):
    def assert_refused(self, out, reason):
        before = sorted(os.listdir(self.base))
        code, stdout, err = self.index(out)
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("refused: " + reason, err)
        self.assertEqual(sorted(os.listdir(self.base)), before)

    def test_inside_repository_refused_nothing_created(self):
        target = ROOT / "records-search-index-test"
        code, _, err = self.index(target)
        self.assertEqual(code, 2)
        self.assertIn("refused: indexdir_inside_repository", err)
        self.assertFalse(os.path.lexists(target))
        other = self.base / "other-repo"
        (other / ".git").mkdir(parents=True)
        self.assert_refused(other / "idx", "indexdir_inside_repository")

    def test_inside_public_output_refused(self):
        (self.base / "public").mkdir()
        self.assert_refused(self.base / "public" / "idx", "indexdir_inside_public_output")
        self.assert_refused(self.base / "site" / "idx", "indexdir_inside_public_output")

    def test_store_published_area_refused(self):
        self.assert_refused(self.store.root / "public" / "idx", "indexdir_inside_public_output")

    def test_parent_symlink_refused(self):
        (self.base / "real").mkdir()
        os.symlink(self.base / "real", self.base / "alias")
        self.assert_refused(self.base / "alias" / "idx", "indexdir_parent_unavailable")
        self.assertEqual(os.listdir(self.base / "real"), [])

    def test_existing_non_empty_or_unprivate_refused(self):
        (self.base / "busy").mkdir(mode=0o700)
        (self.base / "busy" / "notes.txt").write_text("x")
        self.assert_refused(self.base / "busy", "indexdir_not_empty")
        self.assertEqual(os.listdir(self.base / "busy"), ["notes.txt"])
        (self.base / "open").mkdir()
        os.chmod(self.base / "open", 0o755)
        self.assert_refused(self.base / "open", "indexdir_not_private")
        self.assertEqual(os.listdir(self.base / "open"), [])

    def test_relative_or_unnormalized_refused(self):
        self.assert_refused(Path("relative/idx"), "indexdir_must_be_absolute_and_normalized")
        code, _, err = run("index", "--store", str(self.store.root), "--out", str(self.base) + "/x/../idx")
        self.assertEqual(code, 2)
        self.assertFalse(os.path.lexists(self.base / "idx"))

    def test_bad_inventory_refused_nothing_created(self):
        path = self.store.root / "agencies" / "slo-county" / "INVENTORY.csv"
        good = path.read_text()
        path.write_text(good + "R-11,doc-01,%s,no\n" % sha("dup"))
        self.assert_refused(self.out, "inventory_duplicate_document:slo-county:7")
        path.write_text(good + "R-12,../escape,%s,no\n" % sha("bad"))
        self.assert_refused(self.out, "inventory_row_invalid:slo-county:7")

    def test_query_without_index_refused(self):
        code, out, err = self.query("camera")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("refused: index_unavailable", err)
        self.assertFalse(os.path.lexists(self.out))


class PublicScanPositiveControlTest(unittest.TestCase):
    def test_misplaced_index_fails_public_tree_check_and_is_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=60)
            (repo / "README.md").write_text("synthetic\n")
            (repo / "docs").mkdir()
            (repo / "docs" / rs.INDEX_NAME).write_bytes(b"SQLite format 3\x00")
            (repo / "stray.RSIDX").write_bytes(b"x")
            result = subprocess.run([sys.executable, "-B", str(ROOT / "tools" / "check_public_tree.py"),
                                     "--root", str(repo)], capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 1, result.stderr)
            findings = json.loads(result.stdout)["findings"]
            self.assertIn(["docs/" + rs.INDEX_NAME, 0, "private_path"], findings)
            self.assertIn(["stray.RSIDX", 0, "private_path"], findings)
            (repo / "docs" / rs.INDEX_NAME).unlink()
            (repo / "stray.RSIDX").unlink()
            clean = subprocess.run([sys.executable, "-B", str(ROOT / "tools" / "check_public_tree.py"),
                                    "--root", str(repo)], capture_output=True, text=True, timeout=60)
            self.assertEqual(clean.returncode, 0, clean.stdout)

    def test_index_uses_the_public_scan_denial_constants(self):
        self.assertIs(rs.DENIED_SUFFIXES, public_scan.DENIED_SUFFIXES)
        self.assertIn(Path(rs.INDEX_NAME).suffix, public_scan.DENIED_SUFFIXES)
        self.assertIn(rs.INDEX_NAME, public_scan.DENIED_NAMES)
        with mock.patch.object(rs, "DENIED_NAMES", set()), tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(rs.Refused) as caught:
                rs.check_indexdir(Path(tmp).resolve() / "idx")
        self.assertEqual(str(caught.exception), "index_name_not_denied_by_public_scan")


class IncrementalTest(Base):
    def snapshot(self):
        path = self.out / rs.INDEX_NAME
        return path.read_bytes(), os.stat(path).st_mtime_ns

    def rowids(self):
        with contextlib.closing(sqlite3.connect(self.out / rs.INDEX_NAME)) as conn:
            return dict(((d, p), r) for r, d, p in conn.execute("SELECT rowid, doc_id, page FROM pages"))

    def test_unchanged_is_noop(self):
        self.index()
        before = self.snapshot()
        code, out, _ = self.index()
        self.assertEqual((code, out.splitlines()[0]), (0, "index: documents=10 changed=0 unchanged=10 removed=0"))
        self.assertEqual(self.snapshot(), before)

    def test_changed_bytes_replace_only_that_document(self):
        self.index()
        before = self.rowids()
        self.store.write("arroyo-grande", "doc-07", units((1, "Revised invoice with helicopter")))
        code, out, _ = self.index()
        self.assertEqual(out.splitlines()[0], "index: documents=10 changed=1 unchanged=9 removed=0")
        after = self.rowids()
        self.assertEqual({k: v for k, v in after.items() if k[0] != "doc-07"},
                         {k: v for k, v in before.items() if k[0] != "doc-07"})
        self.assertNotEqual(after[("doc-07", 1)], before[("doc-07", 1)])
        self.assertEqual([r[0] for r in self.rows(self.query("helicopter")[1])], ["doc-07"])
        self.assertEqual(self.stat_mode(), (0o700, 0o600, [rs.INDEX_NAME]))

    def test_removed_document_rows_dropped(self):
        self.index()
        path = self.store.root / "agencies" / "arroyo-grande" / "INVENTORY.csv"
        path.write_text("\n".join(line for line in path.read_text().splitlines() if "doc-06" not in line) + "\n")
        code, out, _ = self.index()
        self.assertEqual(out.splitlines()[0], "index: documents=9 changed=0 unchanged=9 removed=1")
        self.assertEqual([r[0] for r in self.rows(self.query("camera")[1])], ["doc-01"])

    def test_crash_mid_index_keeps_previous_index(self):
        self.index()
        before = self.snapshot()
        self.store.write("slo-county", "doc-02", units((1, "helicopter retention")))
        self.store.write("arroyo-grande", "doc-08", units((1, "helicopter training")))
        calls = []

        def crash():
            calls.append(1)
            if len(calls) == 1:
                raise KeyboardInterrupt("injected")
        with mock.patch.object(rs, "_checkpoint", crash), self.assertRaises(KeyboardInterrupt):
            self.index()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(os.listdir(self.out), [rs.INDEX_NAME])
        self.assertEqual(self.rows(self.query("helicopter")[1]), [])
        self.assertEqual(len(self.rows(self.query("camera")[1])), 2)
        self.assertEqual(self.index()[1].splitlines()[0], "index: documents=10 changed=2 unchanged=8 removed=0")

    def stat_mode(self):
        return (stat.S_IMODE(os.stat(self.out).st_mode), stat.S_IMODE(os.stat(self.out / rs.INDEX_NAME).st_mode),
                os.listdir(self.out))


class QueryUsageTest(Base):
    def test_malformed_regex_and_enormous_queries_are_usage_errors(self):
        self.index()
        bad = ['"camera', 'camera"', '""', "camera.*", "^camera$", "[a-z]+", "cam(era|ra)", "AND camera", "camera OR",
               "camera AND OR invoice", "camera; rm -rf x", "$(id)", "`id`", "camera*", "NEAR(camera invoice)", "",
               "   ", "x " * 200, " ".join(["word"] * 17)]
        for terms in bad:
            code, out, err = self.query(terms)
            self.assertEqual((code, out), (2, ""), terms)
            self.assertEqual(err, "records_search: refused: query_invalid\n", terms)

    def test_cli_errors_never_echo_arguments_or_trace(self):
        code, out, err = run("query", "--index", str(self.out), "camera", "--secret-flag")
        self.assertEqual((code, out, err), (2, "", "records_search: refused: usage_invalid\n"))
        result = subprocess.run([sys.executable, "-B", str(TOOL), "query", "--index", str(self.out), "zebra.*"],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("zebra", result.stdout + result.stderr)
        result = subprocess.run([sys.executable, "-B", str(TOOL), "--help"], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0)
        self.assertIn("index --store STORE --out INDEXDIR", result.stdout)


class StaticTest(unittest.TestCase):
    def test_no_network_subprocess_or_copy_out(self):
        source = TOOL.read_text()
        imported = set(re.findall(r"^(?:import|from)\s+([\w., ]+)", source, re.M))
        names = {name.strip().split(".")[0] for line in imported for name in line.split(",")}
        for banned in ("subprocess", "socket", "ssl", "http", "urllib", "requests", "shutil", "ftplib", "smtplib"):
            self.assertNotIn(banned, names)
        for call in ("os.system", "os.popen", "Popen", "copyfile", "eval(", "exec("):
            self.assertNotIn(call, source)
        self.assertIsNone(re.search(r"\b(?:upload|send|post|export)\w*\(", source))


if __name__ == "__main__":
    unittest.main()
