"""RF2330-8: flock_store_check --json / --json-out NEWFILE give one flock-store-check-v1 document with the text's lines."""
import contextlib
import importlib.util
import io
import json
import os
import stat
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("_store_check_base", Path(__file__).with_name("test_store_check.py"))
base = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(base)  # the store builder, by import (tests/flock has no __init__.py)
check, AG, write, fact, snapshot = base.check, base.AG, base.write, base.fact, base.snapshot
TODAY = "2026-10-31"
GOLDEN = ("facts | errors=0\nlaw-policy | errors=0\nrequests | errors=0\ncontacts | errors=0\nwatchlists | errors=0\n"
          "publish | runs=0 no_export=0 invalid=0 drifted=0 pending=0 partly_approved=0 approved=0 | review-required; "
          "G decides any site use\npublish | errors=0\nslugs | errors=0\nbackup | not checked\nbackup | errors=0\n"
          "store check | sections=8 errors=0 notes=0 | machine validity only; review-required\n")  # main f57087a


class StoreCheckJsonTests(unittest.TestCase):
    setUp, run_check, section = base.StoreCheckTests.setUp, base.StoreCheckTests.run_check, base.StoreCheckTests.section

    def run_json(self, *extra):
        code, out, err = self.run_check("--today", TODAY, "--json", *extra)
        return code, json.loads(out), out, err

    def assert_same_lines(self, doc, text):
        self.assertEqual([s["name"] for s in doc["sections"]], list(check.SECTIONS))
        for s in doc["sections"]:
            self.assertEqual(s["lines"] + [f"errors={s['errors']}"], self.section(text, s["name"]), s["name"])

    def test_clean_store_json_and_text_unchanged(self):
        code, out, err = self.run_check("--today", TODAY)
        self.assertEqual((code, out, err), (0, GOLDEN, ""))
        code, doc, raw, err = self.run_json()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual({k: v for k, v in doc.items() if k != "sections"},
                         {"schema": "flock-store-check-v1", "review_required": True, "today": TODAY,
                          "totals": {"sections": 8, "errors": 0, "notes": 0},
                          "note": "machine validity only; review-required"})
        self.assertEqual([(s["errors"], s["notes"]) for s in doc["sections"]], [(0, 0)] * 8)
        self.assert_same_lines(doc, out)
        self.assertEqual(raw, json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=True) + "\n")

    def test_bad_fact_and_ragged_request_same_lines_as_text(self):
        agency = self.store / "agencies" / AG
        write(agency / "FACTS.csv", ",".join(check.facts_tool.V2), fact(), fact(value="other", page="0"))
        write(agency / "REQUESTS.csv", base.REQ_HEAD, base.REQ_ROW, "SAMPLE-2,Sample City Police Department,2026-01-02")
        code, doc, _, _ = self.run_json()
        self.assertEqual(code, 1)
        self.assertEqual({s["name"]: s["errors"] for s in doc["sections"] if s["errors"]}, {"facts": 1, "requests": 1})
        self.assertEqual(doc["totals"], {"sections": 8, "errors": 2, "notes": 0})
        self.assert_same_lines(doc, self.run_check("--today", TODAY)[1])

    def test_notes_counted_per_section(self):
        (self.store / "agencies" / "extra-pd").mkdir()
        _, doc, _, _ = self.run_json()
        slugs = doc["sections"][check.SECTIONS.index("slugs")]
        self.assertEqual((slugs["errors"], slugs["notes"], slugs["lines"]),
                         (0, 1, ["note: agency folder extra-pd is in no ROSTER"]))
        self.assertEqual(doc["totals"]["notes"], 1)

    def test_twenty_five_bad_facts_capped(self):
        bad = [fact(value=f"v{i}", page="0") for i in range(25)]
        write(self.store / "agencies" / AG / "FACTS.csv", ",".join(check.facts_tool.V2), *bad)
        code, doc, raw, _ = self.run_json()
        facts = doc["sections"][0]
        self.assertEqual((code, facts["errors"], len(facts["lines"]), facts["lines"][-1]), (1, 25, 21, "… and 5 more"))
        self.assertIn("\\u2026 and 5 more", raw)  # ensure_ascii
        self.assertTrue(raw.isascii())

    def test_raising_checker_reported_by_class(self):
        with mock.patch.object(check.law_policy, "load_all", side_effect=OSError("/secret/path")):
            code, doc, raw, _ = self.run_json()
        self.assertEqual(code, 1)
        self.assertEqual(doc["sections"][1], {"name": "law-policy", "errors": 1, "notes": 0, "lines": ["failed: OSError"]})
        self.assertEqual([s["errors"] for s in doc["sections"][2:]], [0] * 6)
        self.assertNotIn("/secret/path", raw)

    def test_json_out_refused_before_any_section(self):
        real, link = self.tmp / "real", self.tmp / "link"
        real.mkdir()
        link.symlink_to(real, target_is_directory=True)
        (self.tmp / "exists.json").write_text("{}")
        cases = [("out.json", "not an absolute path"), (self.tmp / "exists.json", "already exists"),
                 (link / "out.json", "parent is a symlink"), (self.tmp / "nope" / "out.json", "parent is not a folder"),
                 (self.store / "out.json", "inside STORE"), (self.store / "agencies" / "out.json", "inside STORE"),
                 (f"{self.tmp}/real/../store/out.json", "inside STORE"),
                 (self.bdir / "out.json", "inside --backups"), (ROOT / "rf2330-8-out.json", "inside the repository"),
                 (ROOT / "tools" / "rf2330-8-out.json", "inside the repository")]
        before = (snapshot(self.store), snapshot(self.bdir), snapshot(real))
        for target, reason in cases:
            with mock.patch.object(check.facts_tool, "check_path") as facts:
                code, out, err = self.run_check("--backups", self.bdir, "--max-backup-age-days", 7, "--json-out", target)
            self.assertEqual((code, out, err), (2, "", f"store check: json-out refused ({reason})\n"), target)
            facts.assert_not_called()
            self.assertFalse(os.path.lexists(str(target)) and reason != "already exists", target)
        self.assertEqual((snapshot(self.store), snapshot(self.bdir), snapshot(real)), before)
        self.assertEqual((self.tmp / "exists.json").read_text(), "{}")

    def test_json_out_writes_private_copy_text_unchanged(self):
        extra = ("--backups", self.bdir, "--max-backup-age-days", 30, "--today", self.backup().isoformat())
        before = (snapshot(self.store), snapshot(self.bdir))
        _, text, _ = self.run_check(*extra)
        _, as_json, _ = self.run_check(*extra, "--json")
        target = self.tmp / "check.json"
        code, out, err = self.run_check(*extra, "--json-out", target)
        self.assertEqual((code, out, err), (0, text, ""))
        self.assertEqual(target.read_bytes(), as_json.encode("ascii"))
        self.assertEqual(stat.S_IMODE(os.lstat(target).st_mode), 0o600)
        self.assertEqual((snapshot(self.store), snapshot(self.bdir)), before)
        self.assertEqual(self.run_check(*extra, "--json-out", target)[0], 2)  # never overwritten
        self.assertEqual(target.read_bytes(), as_json.encode("ascii"))

    def test_json_out_write_failure_leaves_no_file(self):
        real_fdopen, target = os.fdopen, self.tmp / "fail.json"

        def broken(fd, mode):
            real_fdopen(fd, mode).close()
            raise OSError(28, "No space left", "/secret/path")
        with mock.patch.object(check.os, "fdopen", side_effect=broken):
            code, out, err = self.run_check("--json-out", target)
        self.assertEqual((code, err), (2, "store check: json-out write failed (OSError)\n"))
        self.assertIn("store check | sections=8", out)
        self.assertFalse(os.path.lexists(target))

    def test_json_and_json_out_together_refused_and_no_values_shown(self):
        target = self.tmp / "both.json"
        self.assertEqual(self.run_check("--json", "--json-out", target)[0], 2)
        self.assertFalse(os.path.lexists(target))
        agency = self.store / "agencies" / AG
        write(agency / "FACTS.csv", ",".join(check.facts_tool.V2), fact(), fact(page="0"))
        write(agency / "CONTACT.csv", "agency,role,email,source_url,source_sha256,checked_at,added_by",
              f"Sample City Police Department,press,{base.LOCAL}@city.example,https://www.city.example/r,{base.HEX},"
              "2026-10-30,t")
        code, _, out, err = self.run_json()
        code2, _, _ = self.run_check("--json-out", target)
        self.assertEqual((code, code2), (1, 1))
        self.assertIn(f"{AG}: row 2: role is invalid", out)
        for sentinel in (base.SECRET_VALUE, base.SECRET_QUOTE, base.LOCAL):
            self.assertNotIn(sentinel, out + err + target.read_text())

    backup = base.StoreCheckTests.backup


if __name__ == "__main__":
    unittest.main()
