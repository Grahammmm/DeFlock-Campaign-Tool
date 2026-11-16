"""RF0452-9: flock_store_check never follows a linked agencies/regions folder or linked agency folder before dispatch."""
import builtins
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location("_store_check_links", Path(__file__).parents[2] / "tools" / "flock_store_check.py")
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)
BAD = "invalid-header\n"


def run(store, *argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = check.main([str(store), "--today", "2026-01-01", *argv])
    return code, out.getvalue()


class LinkBoundaryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.store, self.outside = root / "store", root / "outside"
        self.store.mkdir()
        self.outside.mkdir()
        for name in ("FACTS.csv", "POLICY.csv", "REQUESTS.csv", "ROSTER.csv", "WATCHLIST.csv"):
            (self.outside / name).write_text(BAD)

    def link_agency(self, name="linked-pd"):
        (self.store / "agencies").mkdir(exist_ok=True)
        os.symlink(self.outside, self.store / "agencies" / name)

    def opened_outside(self, *argv):
        seen, real = [], builtins.open

        def spy(path, *a, **k):
            if str(path).startswith(str(self.outside)):
                seen.append(str(path))
            return real(path, *a, **k)

        with mock.patch.object(builtins, "open", spy), mock.patch.object(os, "open", wraps=os.open) as osopen:
            code, out = run(self.store, *argv)
        seen += [c.args[0] for c in osopen.call_args_list if str(c.args[0]).startswith(str(self.outside))]
        return code, out, seen

    def test_linked_agency_blocks_facts_and_requests_without_opening(self):
        self.link_agency()
        for section in ("facts", "requests"):
            with self.subTest(section=section):
                code, out, seen = self.opened_outside("--section", section)
                self.assertEqual(code, 1)
                self.assertIn(f"{section} | agencies/linked-pd is a link; not followed\n{section} | errors=1\n", out)
                self.assertNotIn("header", out)
                self.assertEqual(seen, [])

    def test_checkers_not_invoked_when_blocked(self):
        self.link_agency()
        with mock.patch.object(check.facts_tool, "check_path") as facts, \
                mock.patch.object(check.requests_tool, "load") as load:
            code, _ = run(self.store, "--section", "facts", "--section", "requests")
        self.assertEqual(code, 1)
        facts.assert_not_called()
        load.assert_not_called()

    def test_dangling_sibling_and_sorted_capped_with_full_total(self):
        (self.store / "agencies" / "real-pd").mkdir(parents=True)
        for i in range(25):
            os.symlink(self.store / "gone", self.store / "agencies" / f"l{i:02d}")
        os.symlink(self.store / "gone", self.store / "agencies" / "bad name")
        code, out = run(self.store, "--section", "facts", "--json")
        sec = json.loads(out)["sections"][0]
        self.assertEqual((code, sec["errors"]), (1, 26))
        self.assertEqual(sec["lines"][0], "agencies/? is a link; not followed")
        self.assertEqual(sec["lines"][1], "agencies/l00 is a link; not followed")
        self.assertEqual(sec["lines"][20], "… and 6 more")
        self.assertNotIn(str(self.store), out)

    def test_linked_roots_block_affected_sections_only(self):
        os.symlink(self.outside, self.store / "agencies")
        os.symlink(self.store / "gone", self.store / "regions")
        code, out = run(self.store, "--json")
        doc = json.loads(out)
        by = {s["name"]: s for s in doc["sections"]}
        self.assertEqual(code, 1)
        for name in ("facts", "law-policy", "requests", "contacts", "publish"):
            self.assertEqual(by[name]["lines"], ["agencies is a link; not followed"], name)
        self.assertEqual(by["watchlists"]["lines"], ["regions is a link; not followed"])
        self.assertEqual(by["slugs"]["lines"], ["agencies is a link; not followed", "regions is a link; not followed"])
        self.assertEqual(by["backup"]["errors"], 0)
        self.assertEqual(doc["totals"]["errors"], sum(s["errors"] for s in doc["sections"]))
        self.assertEqual(doc["totals"]["errors"], 8)

    def test_linked_agencies_root_slugs_text_and_selected(self):
        os.symlink(self.outside, self.store / "agencies")
        code, out, seen = self.opened_outside("--section", "slugs", "--json")
        doc = json.loads(out)
        self.assertEqual((code, doc["selected"], seen), (1, ["slugs"], []))
        self.assertEqual(doc["sections"][0]["errors"], 1)

    def test_unrelated_sections_still_run_and_backup_skips_collections(self):
        self.link_agency()
        code, out = run(self.store, "--section", "watchlists", "--section", "slugs")
        self.assertEqual(code, 1)
        self.assertIn("watchlists | none\nwatchlists | errors=0\n", out)
        with mock.patch.object(Path, "iterdir", side_effect=AssertionError("enumerated")):
            self.assertEqual(run(self.store, "--section", "backup")[0], 0)

    def test_slugs_only_never_calls_facts_or_requests(self):
        with mock.patch.object(check.facts_tool, "check_path") as facts, \
                mock.patch.object(check.requests_tool, "load") as load:
            self.assertEqual(run(self.store, "--section", "slugs")[0], 0)
        facts.assert_not_called()
        load.assert_not_called()

    def test_clean_empty_store_unchanged_and_law_policy_link_kept(self):
        code, out = run(self.store, "--section", "watchlists")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[-1], "store check | sections=1 of 8 (watchlists) errors=0 notes=0 | " + check.NOTE)
        self.link_agency()
        code, out = run(self.store, "--section", "law-policy")
        self.assertEqual(code, 1)
        self.assertIn("linked-pd: POLICY.csv is a link", out)

    def test_link_not_mutated(self):
        self.link_agency()
        run(self.store, "--section", "facts")
        self.assertTrue((self.store / "agencies" / "linked-pd").is_symlink())
        self.assertEqual((self.outside / "FACTS.csv").read_text(), BAD)


if __name__ == "__main__":
    unittest.main()
