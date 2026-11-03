"""flock_requests.py summary PATH [--inventory CSV] --json: one flock-request-summary-v1 document; text unchanged (RF0620-11)."""
import json
import tempfile
import unittest
from pathlib import Path

from tests.flock.test_requests_inventory import GOLDEN, INV_HEADER, SHA, doc
from tests.flock.test_requests_summary import FIXTURE, HEADER, TAIL, counts, row, run

ORDER = ("open", "determined", "produced", "closed", "withdrawn")
NOTE = "not a legal status; the canonical ledger wins"


class RequestsSummaryJsonTests(unittest.TestCase):
    def tmp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        return Path(t.name)

    def write(self, name, *lines):
        path = self.tmp() / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def doc(self, *args):
        done = run("summary", *args)
        self.assertEqual((done.returncode, done.stderr), (0, ""), done.stderr)
        data = json.loads(done.stdout)
        self.assertEqual(done.stdout, json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n")  # one document, nothing else
        self.assertEqual((data["schema"], data["review_required"], data["note"]), ("flock-request-summary-v1", True, NOTE))
        return data, done.stdout

    def text(self, *args):
        done = run("summary", *args)
        self.assertEqual((done.returncode, done.stderr), (0, ""), done.stderr)
        return done.stdout.splitlines()

    def test_two_agencies_counts_match_text_lines(self):
        path = self.write("REQUESTS.csv", HEADER, row("2", "Beta Sheriff", "2026-02-03", "produced"),
                          row("1", "Alpha Police", "2026-03-01", receipt=True), row("3", "Alpha Police", "2026-01-09"),
                          row("4", "Alpha Police", "2025-12-01", "determined", receipt=True),
                          row("5", "Beta Sheriff", "2026-01-04", "withdrawn"), row("6", "Beta Sheriff", "2026-01-05", "closed"))
        data, _ = self.doc(str(path), "--json")
        lines = self.text(str(path))
        self.assertEqual([a["slug"] for a in data["agencies"]], ["alpha-police", "beta-sheriff"])
        for a, line in zip(data["agencies"], lines):
            label, nums, unk, oldest = line.split(" | ")[1:]
            self.assertEqual(a["label"], label)
            self.assertEqual(a["counts"], {k: int(v) for k, v in (x.split("=") for x in nums.split())})
            self.assertEqual(f"receipt_unknown={a['receipt_unknown']}", unk)
            self.assertEqual(oldest, f"oldest_open_sent {a['oldest_open_sent'] or '-'}")
            self.assertNotIn("requests", a)
        self.assertEqual((data["agencies"][0]["receipt_unknown"], data["agencies"][0]["oldest_open_sent"]), (1, "2026-01-09"))
        self.assertEqual(data["totals"], {"agencies": 2, "requests": 6, "open": 2, "receipt_unknown": 1})
        self.assertEqual(lines[-1], "request status | agencies=2 requests=6 open=2 receipt_unknown=1" + TAIL)

    def test_label_with_separator_and_non_ascii_exact_and_text_unchanged(self):
        label = "Züri Polizei | Büro Nord"
        path = self.write("REQUESTS.csv", HEADER, row("1", label, "2026-01-02"))
        data, out = self.doc(str(path), "--json")
        self.assertEqual((data["agencies"][0]["label"], data["agencies"][0]["slug"]), (label, "z-ri-polizei-b-ro-nord"))
        self.assertIn(label, out)  # ensure_ascii=False: no \\u escapes
        self.assertEqual(self.text(str(path)), [
            f"request status | {label} | {counts(open=1)} | receipt_unknown=1 | oldest_open_sent 2026-01-02",
            "request status | agencies=1 requests=1 open=1 receipt_unknown=1" + TAIL])

    def test_no_open_row_oldest_null_text_dash(self):
        path = self.write("REQUESTS.csv", HEADER, row("1", "Gamma Office", "2026-01-02", "determined", receipt=True),
                          row("2", "Gamma Office", "2026-01-03", "closed"))
        data, _ = self.doc(str(path), "--json")
        self.assertIsNone(data["agencies"][0]["oldest_open_sent"])
        self.assertEqual(data["agencies"][0]["counts"], dict.fromkeys(ORDER, 0) | {"determined": 1, "closed": 1})
        self.assertTrue(self.text(str(path))[0].endswith(" | oldest_open_sent -"))

    def test_fixture_text_byte_identical_and_json_matches(self):
        done = run("summary", str(FIXTURE))
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, GOLDEN, ""))
        data, _ = self.doc(str(FIXTURE), "--json")
        self.assertEqual(data["totals"], {"agencies": 4, "requests": 5, "open": 3, "receipt_unknown": 1})
        self.assertEqual([a["oldest_open_sent"] for a in data["agencies"]], [None, "2026-01-03", "2026-01-02", "2026-01-02"])

    def test_inventory_per_request_counts_unknown_row_counted_not_copied(self):
        path = self.write("REQUESTS.csv", HEADER, row("R-2", "Alpha Police", "2026-01-02", "determined"),
                          row("R-1", "Alpha Police", "2026-01-03"), row("R-3", "Beta Sheriff", "2026-01-04"))
        inv = self.write("INVENTORY.csv", INV_HEADER, doc("R-1", "1", SHA["a"]), doc("R-1", "2", SHA["b"], "yes"),
                         doc("R-2", "3", SHA["c"]), doc("ZQ-UNKNOWN-4471", "4", SHA["d"]))
        data, out = self.doc(str(path), "--inventory", str(inv), "--json")
        self.assertEqual(data["agencies"][0]["requests"], [
            {"request_id": "R-1", "status": "open", "posted": 1, "withheld": 1},
            {"request_id": "R-2", "status": "determined", "posted": 1, "withheld": 0}])
        self.assertEqual(data["agencies"][1]["requests"], [{"request_id": "R-3", "status": "open", "posted": 0, "withheld": 0}])
        self.assertEqual(data["totals"], {"agencies": 2, "requests": 3, "open": 2, "receipt_unknown": 2, "documents": 3,
                                          "posted": 2, "withheld": 1, "unknown_rows": 1})
        for hidden in ("ZQ-UNKNOWN-4471", "unknown request", "inventory row", *SHA.values(), "Synthetic doc", "synthetic#"):
            self.assertNotIn(hidden, out)
        text = self.text(str(path), "--inventory", str(inv))
        self.assertIn("request docs | inventory row 5 | request ZQ-UNKNOWN-4471 | unknown request", text)  # text keeps it
        self.assertTrue(text[-1].endswith(" documents=3 posted=2 withheld=1 unknown_rows=1" + TAIL), text[-1])

    def test_flag_order_identical_and_repeats_refused(self):
        inv = str(self.write("INVENTORY.csv", INV_HEADER, doc("R-1", "1", SHA["a"])))
        _, a = self.doc(str(FIXTURE), "--json", "--inventory", inv)
        _, b = self.doc(str(FIXTURE), "--inventory", inv, "--json")
        self.assertEqual(a, b)
        for args in (["--json", "--json"], ["--json", "--inventory", inv, "--json"], ["--inventory", inv, "--inventory", inv],
                     ["--inventory", "--json"], ["--json", "--inventory"], ["--json", "extra"], ["--JSON"],
                     ["--json", "--today", "2026-01-02"]):
            done = run("summary", str(FIXTURE), *args)
            self.assertEqual((done.returncode, done.stdout), (2, ""), args)
            self.assertTrue(done.stderr.startswith("usage: flock_requests.py"), args)
        for args in (["due", str(FIXTURE), "--json"], ["--check", str(FIXTURE), "--json"], ["summary", "--json", str(FIXTURE)]):
            self.assertEqual(run(*args).returncode, 2, args)
        self.assertIn("summary PATH [--inventory CSV] [--as-of YYYY-MM-DD] [--json]", run("--help").stdout)

    def test_header_only_store_empty_document(self):
        path = self.write("REQUESTS.csv", HEADER)
        data, _ = self.doc(str(path), "--json")
        self.assertEqual((data["agencies"], data["totals"]), ([], {"agencies": 0, "requests": 0, "open": 0, "receipt_unknown": 0}))
        data, _ = self.doc(str(path), "--json", "--inventory", str(self.write("INVENTORY.csv", INV_HEADER)))
        self.assertEqual(data["totals"], {"agencies": 0, "requests": 0, "open": 0, "receipt_unknown": 0, "documents": 0,
                                          "posted": 0, "withheld": 0, "unknown_rows": 0})

    def test_load_error_and_inventory_refusal_print_no_json(self):
        bad = self.write("REQUESTS.csv", HEADER, row("1", "Alpha Police", "2026-01-02", "pending"))
        inv = self.write("INVENTORY.csv", INV_HEADER, doc("1", "1", SHA["a"]))
        for args in ([str(bad), "--json"], [str(bad), "--inventory", str(inv), "--json"]):
            done = run("summary", *args)
            self.assertEqual((done.returncode, done.stdout), (1, ""), args)
            self.assertIn("row 2: status must be one of", done.stderr)
        for inv_path, why in ((self.write("INVENTORY.csv", "title,ext"), "inventory header lacks request, doc_id, sha256, "
                               "analysis_only"), (self.tmp() / "missing.csv", "inventory unreadable (FileNotFoundError)")):
            done = run("summary", str(FIXTURE), "--json", "--inventory", str(inv_path))
            self.assertEqual((done.returncode, done.stdout, done.stderr), (1, "", f"summary: {why}\n"))


if __name__ == "__main__":
    unittest.main()
