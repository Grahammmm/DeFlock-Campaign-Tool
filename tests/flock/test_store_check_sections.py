"""RF0720-9: flock_store_check --section NAME (repeatable) runs only the chosen checks, in SECTIONS order."""
import importlib.util
import json
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("_store_check_base", Path(__file__).with_name("test_store_check.py"))
base = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(base)  # the store builder, by import (tests/flock has no __init__.py)
JSPEC = importlib.util.spec_from_file_location("_store_check_json", Path(__file__).with_name("test_store_check_json.py"))
jbase = importlib.util.module_from_spec(JSPEC)
JSPEC.loader.exec_module(jbase)  # GOLDEN: main's text for the clean store
check, AG, write, fact, snapshot = base.check, base.AG, base.write, base.fact, base.snapshot
TODAY, NOTE = jbase.TODAY, "machine validity only; review-required"


class StoreCheckSectionTests(unittest.TestCase):
    setUp, run_check, section, backup = (base.StoreCheckTests.setUp, base.StoreCheckTests.run_check,
                                         base.StoreCheckTests.section, base.StoreCheckTests.backup)

    def names(self, out):
        return list(dict.fromkeys(ln.split(" | ")[0] for ln in out.splitlines()[:-1]))

    def break_facts_and_requests(self):
        agency = self.store / "agencies" / AG
        write(agency / "FACTS.csv", ",".join(check.facts_tool.V2), fact(), fact(value="other", page="0"))
        write(agency / "REQUESTS.csv", base.REQ_HEAD, base.REQ_ROW, "SAMPLE-2,Sample City Police Department,2026-01-02")

    def test_one_section_only_its_lines_and_errors(self):
        self.break_facts_and_requests()
        code, out, err = self.run_check("--today", TODAY, "--section", "requests")
        self.assertEqual((code, err), (1, ""))
        self.assertEqual(out, f"requests | {AG}: row 3: columns expected 9, found 3\nrequests | errors=1\n"
                              f"store check | sections=1 of 8 (requests) errors=1 notes=0 | {NOTE}\n")

    def test_order_is_sections_order_not_flag_order(self):
        for flags in (("contacts", "requests"), ("requests", "contacts")):
            code, out, _ = self.run_check("--today", TODAY, *[a for f in flags for a in ("--section", f)])
            self.assertEqual((code, self.names(out)), (0, ["requests", "contacts"]), flags)
            self.assertEqual(out.splitlines()[-1],
                             f"store check | sections=2 of 8 (requests,contacts) errors=0 notes=0 | {NOTE}")

    def test_unselected_errors_do_not_count(self):
        write(self.store / "agencies" / AG / "REQUESTS.csv", base.REQ_HEAD, base.REQ_ROW, "SAMPLE-2,x,2026-01-02")
        self.assertEqual(self.run_check("--section", "requests")[0], 1)
        code, out, _ = self.run_check("--section", "facts")
        self.assertEqual((code, self.names(out)), (0, ["facts"]))
        self.assertNotIn("requests", out)

    def test_unknown_or_repeated_section_is_usage_error(self):
        for args in (["--section", "nope"], ["--section", "requests", "--section", "requests"],
                     ["--section", "Requests"], ["--section", ""]):
            code, out, err = self.run_check(*args)
            self.assertEqual((code, out), (2, ""), args)
        self.assertIn("--section NAME given twice", self.run_check("--section", "slugs", "--section", "slugs")[2])

    def test_backups_need_backup_section(self):
        made = self.backup()
        for extra in (["--backups", self.bdir, "--max-backup-age-days", 7], ["--backups", self.bdir],
                      ["--max-backup-age-days", 7]):
            code, out, err = self.run_check("--section", "requests", *extra)
            self.assertEqual((code, out), (2, ""), extra)
        self.assertIn("--backups needs --section backup",
                      self.run_check("--section", "requests", "--backups", self.bdir, "--max-backup-age-days", 7)[2])
        today = made.isoformat()
        code, out, _ = self.run_check("--section", "backup", "--backups", self.bdir, "--max-backup-age-days", 7,
                                      "--today", today)
        self.assertEqual((code, self.names(out)), (0, ["backup"]), out)
        self.assertIn("backup | newest is 0 days old (max 7) ok", out)
        code, out, _ = self.run_check("--section", "backup")  # without --backups: not checked, as today
        self.assertEqual((code, self.section(out, "backup")), (0, ["not checked", "errors=0"]))
        self.assertEqual(self.run_check("--section", "backup", "--backups", self.bdir)[0], 2)  # still go together

    def test_all_eight_named_same_lines_as_no_flag(self):
        self.break_facts_and_requests()
        plain = self.run_check("--today", TODAY)
        flags = [a for s in reversed(check.SECTIONS) for a in ("--section", s)]
        code, out, _ = self.run_check("--today", TODAY, *flags)
        self.assertEqual((code, out.splitlines()[:-1]), (plain[0], plain[1].splitlines()[:-1]))
        self.assertEqual(out.splitlines()[-1], f"store check | sections=8 of 8 ({','.join(check.SECTIONS)}) "
                                               f"errors=2 notes=0 | {NOTE}")

    def test_no_flag_text_and_json_unchanged(self):
        self.assertEqual(self.run_check("--today", TODAY), (0, jbase.GOLDEN, ""))
        code, raw, _ = self.run_check("--today", TODAY, "--json")
        doc = json.loads(raw)
        self.assertNotIn("selected", doc)
        self.assertEqual(sorted(doc), ["note", "review_required", "schema", "sections", "today", "totals"])
        self.assertEqual(doc["totals"], {"sections": 8, "errors": 0, "notes": 0})
        self.assertEqual([s["name"] for s in doc["sections"]], list(check.SECTIONS))
        self.assertEqual(raw, json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=True) + "\n")

    def test_json_lists_run_sections_and_selected(self):
        self.break_facts_and_requests()
        code, raw, _ = self.run_check("--today", TODAY, "--json", "--section", "slugs", "--section", "requests")
        doc = json.loads(raw)
        self.assertEqual(code, 1)
        self.assertEqual([s["name"] for s in doc["sections"]], ["requests", "slugs"])
        self.assertEqual(doc["selected"], ["requests", "slugs"])
        self.assertEqual(doc["totals"], {"sections": 2, "errors": 1, "notes": 0})
        self.assertEqual(raw, json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=True) + "\n")

    def test_json_out_copy_has_selected(self):
        target = self.tmp / "out.json"
        code, out, _ = self.run_check("--today", TODAY, "--json-out", target, "--section", "facts")
        self.assertEqual((code, self.names(out)), (0, ["facts"]))
        doc = json.loads(target.read_text())
        self.assertEqual((doc["selected"], [s["name"] for s in doc["sections"]]), (["facts"], ["facts"]))

    def test_publish_section_linked_run_refused_read_only(self):
        outside = self.tmp / "outside-run"
        outside.mkdir()
        runs = self.store / "agencies" / AG / "runs"
        runs.mkdir()
        (runs / "2026-10-31").symlink_to(outside, target_is_directory=True)
        before = snapshot(self.store)
        code, out, _ = self.run_check("--today", TODAY, "--section", "publish")
        self.assertEqual((code, self.names(out)), (1, ["publish"]), out)
        self.assertTrue(any("link-refused" in ln for ln in self.section(out, "publish")), out)
        self.assertTrue(out.endswith(f"store check | sections=1 of 8 (publish) errors=1 notes=0 | {NOTE}\n"), out)
        self.assertEqual(snapshot(self.store), before)


if __name__ == "__main__":
    unittest.main()
