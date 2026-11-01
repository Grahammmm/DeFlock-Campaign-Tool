"""flock_requests.py summary PATH --inventory CSV: documents per request (posted/withheld), unmatched rows named (NB-DF-03)."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "flock_requests.py"
FIXTURE = ROOT / "tests" / "fixtures" / "flock" / "requests-sample.csv"
HEADER = FIXTURE.read_text(encoding="utf-8").splitlines()[0]
INV_HEADER = "source_id,request,doc_id,title,ext,sha256,analysis_only"
TAIL = " | not a legal status; the canonical ledger wins"
SECRET_TITLE = "Zanzibar-Withheld-Memo-7731"
SECRET_DOC = "88771234"
SHA = {k: k * 64 for k in "abcdef"}
# main's `summary` output on the fixture (golden): --inventory absent must stay byte-identical
GOLDEN = (
    "request status | Example State Office | open=0 determined=0 produced=0 closed=1 withdrawn=0 | receipt_unknown=0"
    " | oldest_open_sent -\n"
    "request status | Example Town Records Office | open=1 determined=0 produced=0 closed=0 withdrawn=0 | receipt_unknown=1"
    " | oldest_open_sent 2026-01-03\n"
    "request status | Sample City Police Department | open=1 determined=1 produced=0 closed=0 withdrawn=0"
    " | receipt_unknown=0 | oldest_open_sent 2026-01-02\n"
    "request status | Sample County Sheriff | open=1 determined=0 produced=0 closed=0 withdrawn=0 | receipt_unknown=0"
    " | oldest_open_sent 2026-01-02\n"
    "request status | agencies=4 requests=5 open=3 receipt_unknown=1" + TAIL + "\n")


def run(*args):
    return subprocess.run([sys.executable, "-B", str(TOOL), *args], capture_output=True, text=True, check=False)


def req(rid, agency, status="open"):
    return ",".join((rid, agency, "2026-01-02", "", "", "2026-01-05" if status == "determined" else "", "", status, ""))


def doc(request, doc_id, sha, analysis_only="no", title="Synthetic doc.pdf"):
    return ",".join((f"synthetic#{request}#{doc_id}", request, doc_id, title, "pdf", sha, analysis_only))


class RequestsInventoryTests(unittest.TestCase):
    def tmp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        return Path(t.name)

    def write(self, path, *lines):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def requests(self, *rows):
        return self.write(self.tmp() / "REQUESTS.csv", HEADER, *rows)

    def inv(self, *rows, header=INV_HEADER):
        return self.write(self.tmp() / "INVENTORY.csv", header, *rows)

    def ok(self, path, inv):
        done = run("summary", str(path), "--inventory", str(inv))
        self.assertEqual((done.returncode, done.stderr), (0, ""), done.stderr)
        for word in ("late", "overdue", "violat"):
            self.assertNotIn(word, done.stdout.lower())
        return done.stdout.splitlines()

    def refused(self, inv, message):
        done = run("summary", str(self.requests(req("R-1", "Alpha Police"))), "--inventory", str(inv))
        self.assertEqual((done.returncode, done.stdout, done.stderr), (1, "", f"summary: {message}\n"))

    def test_two_requests_three_documents_duplicate_sha_counted_once(self):
        path = self.requests(req("R-2", "Alpha Police", "determined"), req("R-1", "Alpha Police"), req("R-3", "Alpha Police"))
        lines = self.ok(path, self.inv(doc("R-1", "1", SHA["a"]), doc("R-1", "2", SHA["a"]),  # same sha, one document
                                       doc(" R-1 ", "3", SHA["b"], "yes"), doc("R-2", "4", SHA["c"])))
        self.assertEqual(lines, [
            "request status | Alpha Police | open=2 determined=1 produced=0 closed=0 withdrawn=0 | receipt_unknown=2"
            " | oldest_open_sent 2026-01-02",
            "request docs | Alpha Police | R-1 | status=open | posted=1 withheld=1",
            "request docs | Alpha Police | R-2 | status=determined | posted=1 withheld=0",
            "request docs | Alpha Police | R-3 | status=open | posted=0 withheld=0",
            "request status | agencies=1 requests=3 open=2 receipt_unknown=2 documents=3 posted=2 withheld=1 unknown_rows=0"
            + TAIL])

    def test_sha_with_any_non_no_row_is_withheld_never_posted(self):
        path = self.requests(req("R-1", "Alpha Police"))
        for second in ("yes", "No", " no", ""):  # only an exact "no" posts, like flock_facts coverage
            for order in ((doc("R-1", "1", SHA["a"]), doc("R-1", "2", SHA["a"], second)),
                          (doc("R-1", "2", SHA["a"], second), doc("R-1", "1", SHA["a"]))):
                lines = self.ok(path, self.inv(*order))
                self.assertEqual(lines[1], "request docs | Alpha Police | R-1 | status=open | posted=0 withheld=1", second)

    def test_unknown_and_missing_request_rows_named_exit_zero(self):
        path = self.requests(req("R-1", "Alpha Police"))
        lines = self.ok(path, self.inv(doc("R-1", "1", SHA["a"]), "", doc("R-9", "2", SHA["b"]), doc(" ", "3", SHA["c"]),
                                       doc("r-1", "4", SHA["d"])))
        self.assertEqual(lines[1:], [
            "request docs | Alpha Police | R-1 | status=open | posted=1 withheld=0",
            "request docs | inventory row 4 | request R-9 | unknown request",
            "request docs | inventory row 5 | request missing",
            "request docs | inventory row 6 | request r-1 | unknown request",  # ids match exactly, case included
            "request status | agencies=1 requests=1 open=1 receipt_unknown=1 documents=1 posted=1 withheld=0 unknown_rows=3"
            + TAIL])

    def test_same_request_id_in_two_agency_folders_is_ambiguous_not_counted(self):
        root = self.tmp()
        self.write(root / "agencies" / "alpha-police" / "REQUESTS.csv", HEADER, req("R-1", "Alpha Police"))
        self.write(root / "agencies" / "beta-sheriff" / "REQUESTS.csv", HEADER, req("R-1", "Beta Sheriff"),
                   req("R-2", "Beta Sheriff"))
        lines = self.ok(root, self.inv(doc("R-1", "1", SHA["a"]), doc("R-2", "2", SHA["b"], "yes")))
        self.assertEqual([x for x in lines if x.startswith("request docs")], [
            "request docs | Alpha Police | R-1 | status=open | posted=0 withheld=0",
            "request docs | Beta Sheriff | R-1 | status=open | posted=0 withheld=0",
            "request docs | Beta Sheriff | R-2 | status=open | posted=0 withheld=1",
            "request docs | inventory row 2 | request R-1 | ambiguous request"])
        self.assertTrue(lines[-1].endswith(" documents=1 posted=0 withheld=1 unknown_rows=1" + TAIL), lines[-1])

    def test_titles_doc_ids_shas_and_source_ids_never_printed(self):
        path = self.requests(req("R-1", "Alpha Police"))
        out = "\n".join(self.ok(path, self.inv(doc("R-1", SECRET_DOC, SHA["e"], "yes", SECRET_TITLE),
                                               doc("R-1", "5", SHA["f"], "no", SECRET_TITLE + "-posted"),
                                               doc("R-404", SECRET_DOC + "9", SHA["a"], "yes", SECRET_TITLE + "-unknown"))))
        for planted in (SECRET_TITLE, SECRET_DOC, "synthetic#", "Synthetic doc", *SHA.values(), "eeeeeeee", "aaaaaaaa"):
            self.assertNotIn(planted, out)
        self.assertIn("request docs | inventory row 4 | request R-404 | unknown request", out)

    def test_bad_inventory_refused_with_empty_stdout(self):
        self.refused(self.inv(doc("R-1", "1", SHA["a"]), header="source_id,request,doc_id,title,ext,sha256,analysis"),
                     "inventory header lacks analysis_only")
        self.refused(self.inv(header="title,ext"), "inventory header lacks request, doc_id, sha256, analysis_only")
        self.refused(self.write(self.tmp() / "INVENTORY.csv", ""), "inventory header lacks request, doc_id, sha256, analysis_only")
        self.refused(self.inv(doc("R-1", "1", SHA["a"]), doc("R-1", "2", SHA["b"]) + ",extra"),
                     "inventory row 3: columns have the wrong count")
        self.refused(self.inv(doc("R-1", "1", SHA["a"]), "R-1,2"), "inventory row 3: columns have the wrong count")
        self.refused(self.tmp() / "missing.csv", "inventory unreadable (FileNotFoundError)")
        self.refused(self.tmp(), "inventory unreadable (IsADirectoryError)")
        bad = self.tmp() / "INVENTORY.csv"
        bad.write_bytes(INV_HEADER.encode() + b"\n\xff\xfe,R-1\n")
        self.refused(bad, "inventory unreadable (UnicodeDecodeError)")

    def test_requests_errors_still_win_and_print_nothing(self):
        done = run("summary", str(self.requests(req("R-1", "Alpha Police", "pending"))), "--inventory",
                   str(self.inv(doc("R-1", "1", SHA["a"]))))
        self.assertEqual((done.returncode, done.stdout), (1, ""))
        self.assertIn("row 2: status must be one of", done.stderr)

    def test_without_inventory_byte_identical_and_header_only_inventory(self):
        done = run("summary", str(FIXTURE))
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, GOLDEN, ""))
        lines = self.ok(FIXTURE, self.inv())
        plain = [x for x in lines if not x.startswith("request docs | ")]
        self.assertEqual(plain[:-1], GOLDEN.splitlines()[:-1])
        self.assertEqual(plain[-1], GOLDEN.splitlines()[-1].replace(
            TAIL, " documents=0 posted=0 withheld=0 unknown_rows=0" + TAIL))
        self.assertEqual(len(lines) - len(plain), 5)  # one zero-count line per fixture request

    def test_inventory_flag_only_with_summary_in_exact_form(self):
        inv = str(self.inv(doc("R-1", "1", SHA["a"])))
        for args in (["due", str(FIXTURE), "--inventory", inv], ["--check", str(FIXTURE), "--inventory", inv],
                     ["summary", str(FIXTURE), "--inventory"], ["summary", "--inventory", inv, str(FIXTURE)],
                     ["summary", str(FIXTURE), "--inventory", inv, "--inventory", inv],
                     ["summary", str(FIXTURE), "--as-of", "2026-01-02"]):
            done = run(*args)
            self.assertEqual((done.returncode, done.stdout), (2, ""), args)
            self.assertTrue(done.stderr.startswith("usage: flock_requests.py"), args)
        done = run("--help")
        self.assertIn("summary PATH [--inventory CSV]", done.stdout)
        self.assertEqual(done.stdout.count("\n"), 1)


if __name__ == "__main__":
    unittest.main()
