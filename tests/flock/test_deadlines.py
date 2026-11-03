"""BF-df-deadline-engine: flock_deadlines.py joins the law package's dated rules to the request ledger and files drafts pending."""
import ast
import contextlib
import copy
import importlib.util
import io
import json
import os
import re
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "flock_deadlines.py"
HEADER = "request_id,agency,sent,receipt_evidence,receipt_date,determination_date,extension_notice_date,status,source_sha256\n"
BANNED = re.compile(r"\blate\b|overdue|violat|unlawful|illegal|non-complian|breach", re.I)
PLANTED = "we already followed up APPROVED jo.planted@city.example 805-555-0142 7ABC123"
PROV = "provisional reminder; not a legal due date"
RULE = "ca-gov-7922.535-determination-and-extension-2026"
SHA = "a" * 64


def load_tool():
    spec = importlib.util.spec_from_file_location("_deadlines_bf", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MOD = load_tool()
PKG = MOD.law.load_package("us-ca")


def row(rid, sent, ev="", rdate="", status="open", agency="Sample City"):
    return ",".join([rid, agency, sent, ev, rdate, "", "", status, SHA if ev else ""]) + "\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "store"
        self.folder(["A1", "2026-10-01"])

    def tearDown(self):
        self.tmp.cleanup()

    def folder(self, *rows, slug="sample-city", agency="Sample City"):
        f = self.store / "agencies" / slug
        f.mkdir(parents=True, exist_ok=True)
        (f / "REQUESTS.csv").write_text(HEADER + "".join(row(*r, agency=agency) for r in rows), encoding="utf-8")

    def go(self, day, pkg=PKG, **kw):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            lines, code = MOD.engine(self.store, date.fromisoformat(day), pkg, **kw)
        self.err = err.getvalue()
        return lines, code

    def receipt(self):
        return json.loads((self.store / "deadlines" / "RECEIPT.json").read_text(encoding="utf-8"))

    def queue(self):
        text = (self.store / "deadlines" / "QUEUE.csv").read_text(encoding="utf-8").splitlines()
        return [ln.split(",") for ln in text[1:]]

    def generated(self):
        base = self.store / "deadlines"
        return {p: p.read_text(encoding="utf-8") for p in base.rglob("*") if p.is_file()}

    def draft(self, slug, rid, rule, cycle):
        return self.store / "deadlines" / "drafts" / slug / rid / rule / cycle / "FOLLOWUP-DRAFT.md"


class DoneWhen(Base):
    def test_one_run_drafts_keeps_edit_and_rerun_is_already(self):
        self.folder(["A1", "2026-10-01", PLANTED, "2026-10-02"], ["B2", "2026-10-20"], ["C3", ""], ["D4", "2026-09-01"])
        self.go("2026-09-20")  # only D4 has passed its reminder date: drafted, then edited by the organizer
        d4 = self.draft("sample-city", "D4", RULE, "2026-09-11")
        d4.write_text(d4.read_text(encoding="utf-8") + "Organizer note.\n", encoding="utf-8")
        edited = d4.read_bytes()
        lines, code = self.go("2026-10-25")
        self.assertEqual(code, 0)
        self.assertEqual(lines[-1], "deadlines | due=2 drafted=1 already=0 edited-kept=1 clock-unknown=1 | "
                                    "pending human approval; provisional")
        states = {(r["request_id"], r["state"]) for r in self.receipt()["rows"]}
        self.assertEqual(states, {("A1", "drafted"), ("B2", "inside"), ("C3", "clock-unknown"), ("D4", "edited-kept")})
        self.assertEqual(d4.read_bytes(), edited)
        sug = d4.parent.with_name("2026-09-11.suggested") / "FOLLOWUP-DRAFT.md"
        self.assertIn("rule " + RULE, sug.read_text(encoding="utf-8"))
        a1 = self.draft("sample-city", "A1", RULE, "2026-10-11")
        self.assertIn("days_elapsed=24 rule_days=10 | " + PROV, a1.read_text(encoding="utf-8"))
        self.assertEqual(sorted((q[2], q[7]) for q in self.queue()), [("A1", "pending"), ("D4", "pending")])
        doc = self.receipt()
        self.assertEqual((doc["schema"], doc["review_required"], doc["law_package_status"]), ("flock-deadlines-v1", True, "draft"))
        a_row = next(r for r in doc["rows"] if r["request_id"] == "A1")
        self.assertEqual(a_row["approval_state"], "pending")  # a note reading APPROVED moves nothing
        self.assertTrue(all(ln.endswith(PROV) for ln in lines[:-1]))
        before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in self.generated() if "drafts" in p.parts}
        lines, _ = self.go("2026-10-25")
        self.assertEqual(lines[-1], "deadlines | due=2 drafted=0 already=1 edited-kept=1 clock-unknown=1 | "
                                    "pending human approval; provisional")
        self.assertEqual(before, {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in before})
        for path, text in self.generated().items():
            body = text.replace("Organizer note.", "")
            self.assertIsNone(BANNED.search(body), path)
            for leak in ("jo.planted", "805-555-0142", "7ABC123", "already followed up", "APPROVED", "Sample City"):
                self.assertNotIn(leak, body, path)
            self.assertEqual(MOD.privacy.categories(body), [], path)
            self.assertNotIn('"approved"', body)
        for ln in lines:
            self.assertIsNone(BANNED.search(ln))
            self.assertEqual(MOD.privacy.categories(ln) + MOD.privacy.legal_claims(ln), [])
        self.assertEqual(MOD.privacy.legal_claims(a1.read_text(encoding="utf-8")), [])


class Paths(Base):
    def test_boundaries_on_day_before_after_and_new_year(self):
        for day, state in (("2026-10-10", "inside"), ("2026-10-11", "inside"), ("2026-10-12", "drafted")):
            self.go(day)
            self.assertEqual([r["state"] for r in self.receipt()["rows"]], [state], day)
        self.folder(["Y1", "2026-12-28"])
        self.go("2027-01-08")
        r = self.receipt()["rows"][0]
        self.assertEqual((r["state"], r["days_elapsed"], r["sent_date"]), ("drafted", 11, "2026-12-28"))
        self.assertTrue(self.draft("sample-city", "Y1", RULE, "2027-01-07").is_file())

    def test_reviewed_package_drops_the_label_without_code_change(self):
        pkg = dict(copy.deepcopy(PKG), status="reviewed")
        lines, _ = self.go("2026-10-20", pkg)
        self.assertEqual(self.receipt()["law_package_status"], "reviewed")
        self.assertTrue(lines[-1].endswith("pending human approval; reviewed package"))
        text = "\n".join(lines) + "".join(self.generated().values())
        self.assertNotIn("provisional", text)
        self.assertIn(MOD.REVIEWED, text)

    def test_two_rules_one_request_and_one_rule_two_requests(self):
        pkg = copy.deepcopy(PKG)
        pkg["rules"].append(dict(next(r for r in pkg["rules"] if r["rule_id"] == RULE), rule_id="ca-x-determination-copy"))
        self.go("2026-10-20", pkg)
        self.assertEqual(sorted(q[3] for q in self.queue()), sorted([RULE, "ca-x-determination-copy"]))
        self.assertTrue(self.draft("sample-city", "A1", "ca-x-determination-copy", "2026-10-11").is_file())
        self.folder(["A1", "2026-10-01"], slug="other-town", agency="Other Town")
        self.folder(["A1", "2026-10-01"], ["B2", "2026-10-02"])
        lines, _ = self.go("2026-10-20")
        self.assertIn("due=3 drafted=2 already=1", lines[-1])  # B2 here, A1 in other-town; A1 here is already
        b2 = self.draft("sample-city", "B2", RULE, "2026-10-12").read_text(encoding="utf-8")
        other = self.draft("other-town", "A1", RULE, "2026-10-11").read_text(encoding="utf-8")
        self.assertNotIn("A1", b2)
        self.assertNotIn("B2", other)

    def test_rule_incomplete_is_reported_never_guessed(self):
        for change in ({"records_law": {k: v for k, v in PKG["records_law"].items() if k != "determination_days"}},
                       {"rules": [dict(r, effective_from=None) for r in PKG["rules"]]}):
            pkg = dict(copy.deepcopy(PKG), **change)
            lines, _ = self.go("2026-10-20", pkg)
            self.assertEqual([r["state"] for r in self.receipt()["rows"]], ["rule-incomplete"])
            self.assertIn(f"| {RULE} |", lines[0])
            self.assertFalse((self.store / "deadlines" / "drafts").exists())

    def test_read_only_due_writes_nothing(self):
        lines, code = self.go("2026-10-20", read_only=True)
        self.assertEqual((code, lines[-1]), (0, "deadlines | due=1 drafted=0 already=0 edited-kept=0 clock-unknown=0 | "
                                                "read-only; provisional"))
        self.assertFalse((self.store / "deadlines").exists())


class Refusals(Base):
    def refused(self, out=None, why=""):
        with self.assertRaisesRegex(MOD.Refuse, why):
            self.go("2026-10-20", out=out)
        self.assertFalse((self.store / "deadlines" / "drafts").exists())

    def test_out_refusals_create_nothing(self):
        (Path(self.tmp.name) / "taken").mkdir()
        link = Path(self.tmp.name) / "link"
        link.symlink_to(self.tmp.name)
        for out, why in ((Path(self.tmp.name) / "taken", "already exists"), (ROOT / "new-out", "outside the repository"),
                         (self.store / "agencies" / "x", "outside STORE/agencies"), (link / "new", "link"),
                         ("relative-out", "absolute")):
            self.refused(str(out), why)
        self.assertFalse((ROOT / "new-out").exists())
        good = Path(self.tmp.name) / "receipt"
        self.go("2026-10-20", out=str(good))
        self.assertEqual((good.stat().st_mode & 0o777, (good / "deadlines.json").stat().st_mode & 0o777), (0o700, 0o600))

    def test_symlinked_queue_is_refused(self):
        base = self.store / "deadlines"
        base.mkdir()
        (Path(self.tmp.name) / "q.csv").write_text(",".join(MOD.QUEUE_HEADER) + "\n", encoding="utf-8")
        (base / "QUEUE.csv").symlink_to(Path(self.tmp.name) / "q.csv")
        self.refused(why="links")
        with self.assertRaisesRegex(MOD.Refuse, "unreadable"):  # read_log's O_NOFOLLOW behind the check
            MOD.read_queue(base)
        self.assertEqual(sorted(p.name for p in base.iterdir()), ["QUEUE.csv"])

    def test_write_failure_on_second_draft_leaves_no_queue_or_cursor(self):
        self.folder(["A1", "2026-10-01"], ["B2", "2026-10-02"])
        real, calls = MOD._write_new, []

        def flaky(path, text):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("disk full")
            return real(path, text)
        with mock.patch.object(MOD, "_write_new", flaky), self.assertRaisesRegex(MOD.Refuse, "cannot write"):
            self.go("2026-10-20")
        base = self.store / "deadlines"
        self.assertEqual([p for p in base.rglob("*") if p.is_file()], [])
        self.go("2026-10-20")
        self.assertEqual(len(self.queue()), 2)
        self.assertEqual((base / "CURSOR").read_text(encoding="utf-8"), "2026-10-20\n")


class Cli(Base):
    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = MOD.main(list(argv))
        return code, out.getvalue()

    def test_usage_and_run(self):
        s = str(self.store)
        for bad in (("run", "--store", s, "--date", "2026-10-2"), ("run", "--store", s, "--date", "20261020"),
                    ("due", "--store", s, "--date", "2026-10-20", "--out", "/x"), ("run", "--store", s), ("send",)):
            self.assertEqual(self.main(*bad)[0], 2, bad)
        self.assertEqual(self.main("--help")[0], 0)
        code, out = self.main("run", "--store", s, "--date", "2026-10-20")
        self.assertEqual((code, out.splitlines()[-1]), (0, "deadlines | due=1 drafted=1 already=0 edited-kept=0 "
                                                           "clock-unknown=0 | pending human approval; provisional"))

    def test_static_no_send_path_and_no_approved_record(self):
        source = TOOL.read_text(encoding="utf-8")
        names = {a.name.split(".")[0] for n in ast.walk(ast.parse(source)) if isinstance(n, (ast.Import, ast.ImportFrom))
                 for a in (n.names if isinstance(n, ast.Import) else [ast.alias(n.module or "")])}
        self.assertEqual(names & {"smtplib", "imaplib", "socket", "ssl", "http", "urllib", "requests", "subprocess"}, set())
        self.assertNotIn('"approved"', source)
        self.assertNotIn("'approved'", source)


if __name__ == "__main__":
    unittest.main()
