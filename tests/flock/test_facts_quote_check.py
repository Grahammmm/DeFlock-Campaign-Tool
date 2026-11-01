"""flock_facts quote-check: each record fact's verbatim_quote is on its cited page of a page-text export (NB-DF-02)."""
import csv
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.flock.test_facts import ROOT, TOOL, snapshot

_spec = importlib.util.spec_from_file_location("flock_facts_quote_check_under_test", TOOL)
facts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(facts)

DOC, OTHER = hashlib.sha256(b"synthetic quote doc").hexdigest(), hashlib.sha256(b"synthetic other doc").hexdigest()
PLANTED = "Zorblax"  # distinctive word in every quote and page; it must never reach the output
PAGE1 = "Intro line.\nThe Zorblax license plate\nreader \u201cretains\u201d images \u2013 30 days.\ffooter"
PAGE2 = "Nothing about Zorblax cameras here."
COUNTS = "found not-found no-document ambiguous-export no-page page-hash-mismatch unreadable-fact skipped-kind bad-row".split()


def fact(quote, page="1", sha=DOC, kind="record", value="Acme", fact_type="vendor", agency="Alpha", status="checked"):
    row = dict(zip(facts.V1, [agency, "REQ-Q-1", fact_type, value, sha, page, quote]))
    row.update(kind=kind, status=status, effective_from="", effective_to="", added_by="quote-tester", added_at="2026-10-31")
    row["fact_id"] = facts.fact_id(row)
    return [row[k] for k in facts.V2]


def fid(row):
    return row[facts.V2.index("fact_id")]


def line(slug, total, **counts):
    return "quote-check | %s | facts=%d " % (slug, total) + " ".join(
        "%s=%d" % (k, counts.get(k.replace("-", "_"), 0)) for k in COUNTS)


class FactsQuoteCheckTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.store, self.export = self.dir / "store", self.dir / "export"
        self.export.mkdir()

    def agency(self, slug, rows, head=facts.V2, store=None):
        path = (store or self.store) / "agencies" / slug / "FACTS.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            csv.writer(handle, lineterminator="\r\n").writerows([head, *rows])
        return path

    def document(self, pages, sha=DOC, sig="0123456789abcdef", record=None):
        folder = self.export / sha / sig
        folder.mkdir(parents=True)
        rows = []
        for number, text in pages.items():
            data = text.encode("utf-8")
            (folder / ("page-%04d.txt" % number)).write_bytes(data)
            rows.append({"page": number, "status": "native_text", "text_file": "page-%04d.txt" % number,
                         "chars": len(text), "text_sha256": hashlib.sha256(data).hexdigest()})
        rec = {"schema": "ocr-stage-v1", "source_sha256": sha, "review_state": "not_reviewed", "pages": rows}
        (folder / "result.json").write_text(json.dumps(rec) if record is None else record, encoding="utf-8")
        return folder

    def run_tool(self, *args, text=None, store=None):
        before = snapshot(self.dir)
        cmd = [sys.executable, "-B", str(TOOL), "quote-check", "--store", str(store or self.store),
               "--text", str(text or self.export), *args]
        done = subprocess.run(cmd, capture_output=True, text=True, check=False)
        self.assertEqual(snapshot(self.dir), before, "quote-check changed the store or the export")
        for leak in (PLANTED, PLANTED.casefold(), "retains", "license plate", "Nothing about", str(self.export), "page-0001"):
            self.assertNotIn(leak, done.stdout + done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        return done.returncode, done.stdout.splitlines(), done.stderr.strip()

    def test_found_on_its_page_and_page_matters(self):
        self.document({1: PAGE1, 2: PAGE2})
        on_page, wrong_page = fact("The Zorblax license plate reader"), fact("The Zorblax license plate reader", page="2")
        self.agency("alpha", [on_page, wrong_page])
        self.assertEqual(self.run_tool(), (3, [line("alpha", 2, found=1, not_found=1),
                                               "alpha | %s | not-found | page 2" % fid(wrong_page)], ""))
        self.agency("alpha", [on_page])
        self.assertEqual(self.run_tool(), (0, [line("alpha", 1, found=1)], ""))

    def test_wrapped_lines_curly_quotes_dashes_case_and_nbsp_fold(self):
        self.document({1: PAGE1})
        quotes = ['the zorblax LICENSE plate reader "retains" images - 30 days.', "plate\u00a0 reader \u201dretains\u201c",
                  "images \u2014 30 days.\nfooter", "\uff3aorblax license"]  # fullwidth Z folds by NFKC
        self.agency("alpha", [fact(q, fact_type="t%d" % n) for n, q in enumerate(quotes)])
        self.assertEqual(self.run_tool(), (0, [line("alpha", 4, found=4)], ""))
        near = fact("The Zorblax licence plate reader")  # one letter off: no fuzzy match
        self.agency("alpha", [near, fact("images 30 days", fact_type="dash-dropped")])
        code, out, _ = self.run_tool()
        self.assertEqual((code, out[0]), (3, line("alpha", 2, not_found=2)))

    def test_missing_document_signature_or_page(self):
        self.document({1: PAGE1})
        self.document({1: PAGE1}, sha=OTHER)
        self.document({1: PAGE1}, sha=OTHER, sig="0123456789abcdef"[::-1])  # reversed: no hex literal for the secret scan
        empty = hashlib.sha256(b"empty sha folder").hexdigest()
        (self.export / empty).mkdir()
        rows = [fact("Zorblax", sha=hashlib.sha256(b"absent").hexdigest()), fact("Zorblax", sha=OTHER),
                fact("Zorblax", page="9"), fact("Zorblax", sha=empty)]
        self.agency("alpha", rows)
        self.assertEqual(self.run_tool(), (0, [line("alpha", 4, no_document=2, ambiguous_export=1, no_page=1),
                                               "alpha | %s | no-document | page 1" % fid(rows[0]),
                                               "alpha | %s | ambiguous-export | page 1" % fid(rows[1]),
                                               "alpha | %s | no-page | page 9" % fid(rows[2]),
                                               "alpha | %s | no-document | page 1" % fid(rows[3])], ""))

    def test_symlinks_in_the_export_are_never_followed(self):
        real = self.document({1: PAGE1}, sha=OTHER)
        linked = self.dir / "linked"
        linked.mkdir()
        os.symlink(real, linked / "0123456789abcdef")
        os.symlink(linked, self.export / DOC)
        self.agency("alpha", [fact("Zorblax")])
        self.assertEqual(self.run_tool()[1][0], line("alpha", 1, no_document=1))
        os.unlink(self.export / DOC)
        (self.export / DOC).mkdir()
        os.symlink(real, self.export / DOC / "0123456789abcdef")
        self.assertEqual(self.run_tool()[1][0], line("alpha", 1, no_document=1))
        os.unlink(self.export / DOC / "0123456789abcdef")
        folder = self.document({}, record="{}")
        os.symlink(real / "page-0001.txt", folder / "page-0001.txt")
        self.assertEqual(self.run_tool()[1][0], line("alpha", 1, no_page=1))

    def test_page_edited_after_export_or_unverifiable_is_a_hash_mismatch(self):
        folder = self.document({1: PAGE1})
        (folder / "page-0001.txt").write_text(PAGE1 + " edited", encoding="utf-8")
        row = fact("Zorblax")
        self.agency("alpha", [row])
        self.assertEqual(self.run_tool(), (3, [line("alpha", 1, page_hash_mismatch=1),
                                               "alpha | %s | page-hash-mismatch | page 1" % fid(row)], ""))
        good = {"schema": "ocr-stage-v1", "source_sha256": DOC,
                "pages": [{"page": 1, "text_sha256": hashlib.sha256((PAGE1 + " edited").encode()).hexdigest()}]}
        for record in ("not json", "[]", json.dumps(dict(good, schema="other")), json.dumps(dict(good, source_sha256=OTHER)),
                       json.dumps(dict(good, pages=[{"page": "1", "text_sha256": good["pages"][0]["text_sha256"]}]))):
            (folder / "result.json").write_text(record, encoding="utf-8")
            self.assertEqual(self.run_tool()[0:2], (3, [line("alpha", 1, page_hash_mismatch=1),
                                                        "alpha | %s | page-hash-mismatch | page 1" % fid(row)]), record)
        (folder / "result.json").unlink()
        self.assertEqual(self.run_tool()[0], 3)
        (folder / "result.json").write_text(json.dumps(good), encoding="utf-8")
        self.assertEqual(self.run_tool(), (0, [line("alpha", 1, found=1)], ""))

    def test_unreadable_is_not_a_failure_and_other_kinds_are_skipped(self):
        self.document({1: PAGE1})
        rows = [fact("", value="UNREADABLE"), fact("Zorblax policy text", kind="policy"), fact("Zorblax law", kind="law"),
                fact("Zorblax")]
        self.agency("alpha", rows)
        self.assertEqual(self.run_tool(), (0, [line("alpha", 4, found=1, unreadable_fact=1, skipped_kind=2),
                                               "alpha | %s | unreadable-fact | page 1" % fid(rows[0]),
                                               "alpha | %s | skipped-kind | page 1" % fid(rows[1]),
                                               "alpha | %s | skipped-kind | page 1" % fid(rows[2])], ""))

    def test_bad_rows_are_reported_by_id_or_row_never_text(self):
        self.document({1: PAGE1})
        bad = fact("Zorblax", status="pending")
        self.agency("alpha", [fact("Zorblax"), bad, ["Alpha", "Zorblax shifted", "x"], fact("Zorblax", agency="Beta")])
        code, out, err = self.run_tool()
        self.assertEqual((code, out[1:], err), (1, ["alpha | %s | bad-row | page 1" % fid(bad), "alpha | row 4 | bad-row | page -",
                                                    "alpha | %s | bad-row | page 1" % fid(fact("Zorblax", agency="Beta"))], ""))
        self.assertEqual(out[0], line("alpha", 4, found=1, bad_row=3))
        self.agency("alpha", [], head=facts.V1)
        self.assertEqual(self.run_tool(), (1, [], "file 1: row 1: header is not an accepted header"))

    def test_text_inside_repository_or_store_is_refused_before_reading(self):
        self.agency("alpha", [["broken"]], head=["not", "a", "header"])
        inside = self.store / "export-copy"
        inside.mkdir()
        os.symlink(self.store, self.dir / "store-link")
        for text in (ROOT, ROOT / "tests" / "flock", self.store, inside, self.dir / "store-link"):
            code, out, err = self.run_tool(text=text)
            self.assertEqual((code, out), (2, []), text)
            self.assertIn("--text must be outside the repository and the store", err)
        self.assertEqual(self.run_tool(text=ROOT, store=self.dir / "absent")[0], 2)
        self.assertEqual(self.run_tool(text=self.dir / "absent"), (1, [], "text: is not a folder"))

    def test_agency_filter_and_unknown_slug(self):
        self.document({1: PAGE1})
        self.agency("alpha", [fact("Zorblax")])
        beta = fact("absent words", agency="Beta")
        self.agency("beta", [beta])
        self.assertEqual(self.run_tool(), (3, [line("alpha", 1, found=1), line("beta", 1, not_found=1),
                                               "beta | %s | not-found | page 1" % fid(beta)], ""))
        self.assertEqual(self.run_tool("--agency", "Alpha"), (0, [line("alpha", 1, found=1)], ""))
        self.assertEqual(self.run_tool("--agency", "gamma"), (1, [], "agency gamma: no FACTS.csv"))
        os.symlink(self.store / "agencies" / "alpha", self.store / "agencies" / "zeta")
        self.assertEqual(self.run_tool(), (1, [], "agencies/zeta: is a symlink; refusing"))

    def test_usage_errors_exit_2_and_help_lists_the_verb(self):
        self.agency("alpha", [fact("Zorblax")])
        store, text = str(self.store), str(self.export)
        for argv in (["quote-check", "--store", store], ["quote-check", "--text", text], ["quote-check", "x", "--store", store, "--text", text],
                     ["quote-check", "--store", store, "--text", text, "--list", "disputed"],
                     ["quote-check", "--store", store, "--text", text, "--agency", "!!"],
                     ["quote-check", "--store", store, "--text", text, "--check", store],
                     ["status", "--store", store, "--text", text], ["--check", store, "--text", text],
                     ["import", "x.csv", "--store", store, "--added-by", "me", "--text", text]):
            done = subprocess.run([sys.executable, "-B", str(TOOL), *argv], capture_output=True, text=True, check=False)
            self.assertEqual((done.returncode, done.stdout), (2, ""), argv)
        done = subprocess.run([sys.executable, "-B", str(TOOL), "--help"], capture_output=True, text=True, check=False)
        self.assertIn("quote-check --store DIR --text EXPORT [--agency SLUG]", " ".join(done.stdout.split()))


if __name__ == "__main__":
    unittest.main()
