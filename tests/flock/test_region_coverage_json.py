"""flock_region coverage --json | --json-out NEWFILE: the flock-region-coverage-v1 document (RF0420-10)."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from tests.flock import test_region_coverage as base  # its fixtures; its tests run in their own module
from tests.flock.test_region_coverage import CITY, ROOT, TOOL, run, summary

REFUSED = "coverage refused: json-out "


def load_tool():
    spec = importlib.util.spec_from_file_location("_region_coverage_json", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def dump(doc):
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


class RegionCoverageJsonTest(unittest.TestCase):
    setUp, coverage, edit_config, add_seed = (base.RegionCoverageTest.setUp, base.RegionCoverageTest.coverage,
                                              base.RegionCoverageTest.edit_config, base.RegionCoverageTest.add_seed)

    def json_run(self):
        code, out, err = self.coverage("--json")
        self.assertNotIn("Traceback", out + err)
        return code, out, json.loads(out)

    def doc(self, listed, unlisted, other=None, hand=0, seed=4):
        return {"schema": "flock-region-coverage-v1", "review_required": True, "region": "t", "jurisdiction": "us-ca",
                "county_fips": "06999", "seed_agencies": seed, "listed": listed, "unlisted": unlisted,
                "other_unlisted": {"chp": 1, "city_council": 1} if other is None else other, "hand_added": hand,
                "complete": not unlisted}

    def test_one_unlisted_police(self):  # 1
        self.edit_config(lambda a: a.pop(1))
        code, out, doc = self.json_run()
        self.assertEqual(code, 3)
        self.assertEqual(doc, self.doc(1, [{"agency_id": CITY, "kind": "police", "name": "Sample City Police"}]))
        self.assertEqual(out, dump(doc))  # stdout is the document only, in its fixed form
        text = self.coverage()
        self.assertEqual(text[0], 3)
        self.assertEqual(text[1].splitlines()[-1], summary(doc["listed"], len(doc["unlisted"]), hand=doc["hand_added"],
                                                           seed=doc["seed_agencies"]))

    def test_all_listed(self):  # 2
        code, out, doc = self.json_run()
        self.assertEqual((code, doc), (0, self.doc(2, [])))
        self.assertIs(doc["complete"], True)

    def test_other_kinds_and_hand_added(self):
        self.add_seed(("ca-sample-da", "district_attorney", "Sample DA"), ("ca-sample-board", "county_board", "Board"))
        self.edit_config(lambda a: a.append({
            "slug": "campus-police", "watch_agency": "Campus Police", "seed_agency_id": None,
            "seed_note": "campus department; not in the agency seed", "in_scope": "no", "applicability": "",
            "official_domain": "", "site_slug": None, "watch": []}))
        code, out, doc = self.json_run()
        other = {"chp": 1, "city_council": 1, "county_board": 1, "district_attorney": 1}
        self.assertEqual((code, doc), (0, self.doc(2, [], other, hand=1, seed=6)))
        self.assertEqual(list(doc["other_unlisted"]), sorted(other))

    def test_name_cleaned_and_cut_as_text(self):  # 3
        long = "Long " + "x" * 195
        self.add_seed(("ca-bell-police", "police", "Bell\x07 Police\nunlisted ca-fake police Fake\u202e"),
                      ("ca-long-police", "police", long))
        code, _, doc = self.json_run()
        self.assertEqual(code, 3)
        names = [u["name"] for u in doc["unlisted"]]
        self.assertEqual(names, ["Bell Policeunlisted ca-fake police Fake", long[:120]])
        text = self.coverage()[1].splitlines()
        self.assertEqual(text[:2], ["unlisted %s police %s" % (u["agency_id"], u["name"]) for u in doc["unlisted"]])

    def test_config_error_keeps_text(self):  # 4
        self.edit_config(lambda a: a[1].update(seed_agency_id="ca-nowhere-police"))
        target = self.tmp / "cov.json"
        text = self.coverage()
        for extra in (("--json",), ("--json-out", str(target))):
            with self.subTest(extra=extra):
                got = self.coverage(*extra)
                self.assertEqual(got, text)
                self.assertEqual(got[0], 1)
                with self.assertRaises(ValueError):
                    json.loads(got[1])
                self.assertFalse(os.path.lexists(target))
        self.cfg_path.write_text("[", encoding="utf-8")
        self.assertEqual(self.coverage("--json"), (1, "config: cannot read (JSONDecodeError)\n", ""))

    def test_json_out_refusals(self):  # 5
        (self.tmp / "taken.json").write_text("keep", encoding="utf-8")
        (self.tmp / "dangling").symlink_to(self.tmp / "nowhere.json")
        (self.tmp / "real").mkdir()
        (self.tmp / "linked").symlink_to(self.tmp / "real")
        inside = ROOT / "tests" / "rf0420-10-never-written.json"
        cases = (("cov.json", "must be an absolute path"), (str(self.tmp / "taken.json"), "already exists"),
                 (str(self.tmp / "dangling"), "already exists"), (str(self.tmp / "linked" / "c.json"), "parent is a symlink"),
                 (str(self.tmp / "missing" / "c.json"), "parent is not a folder"), (str(inside), "inside repository"))
        self.cfg_path.unlink()  # the target is refused before the config is read
        for value, why in cases:
            with self.subTest(value=value):
                self.assertEqual(self.coverage("--json-out", value), (1, REFUSED + why + "\n", ""))
        self.assertEqual((self.tmp / "taken.json").read_text(encoding="utf-8"), "keep")
        self.assertFalse(os.path.lexists(self.tmp / "nowhere.json") or os.path.lexists(inside))
        self.assertEqual(list((self.tmp / "real").iterdir()), [])

    def test_json_out_writes_new_private_file(self):  # 6
        for n, change in enumerate((lambda a: None, lambda a: a.pop(1))):
            with self.subTest(case=n):
                self.edit_config(change)
                target = self.tmp / ("cov%d.json" % n)
                text, data = self.coverage(), self.coverage("--json")
                got = self.coverage("--json-out", str(target))
                self.assertEqual(got, text)
                self.assertEqual(got[0], data[0])
                self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)
                self.assertEqual(target.read_bytes(), data[1].encode("utf-8"))
                self.assertEqual(self.coverage("--json-out", str(target))[:2], (1, REFUSED + "already exists\n"))

    def test_usage_errors_exit_2(self):  # 7
        cfg, seed, target = str(self.cfg_path), str(self.seed_dir), str(self.tmp / "x.json")
        for args in (("coverage", cfg, "--json", "--json-out", target), ("--check", cfg, "--json"),
                     ("init", cfg, "--store", str(self.tmp / "s"), "--json"), ("--check", cfg, "--json-out", target),
                     ("init", cfg, "--store", str(self.tmp / "s"), "--json-out", target),
                     ("new", "--county-fips", "06999", "--region", "t", "--out", str(self.tmp / "n.json"), "--json")):
            with self.subTest(args=args):
                self.assertEqual(run(*args, "--seed-dir", seed)[0], 2)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["region.json", "seed"])

    def in_process(self, target, patch):  # main() in this process with one patch applied to a fresh copy of the tool
        tool, out = load_tool(), io.StringIO()
        with patch(tool), contextlib.redirect_stdout(out):
            code = tool.main(["coverage", str(self.cfg_path), "--seed-dir", str(self.seed_dir), "--json-out", str(target)])
        return code, out.getvalue()

    def test_write_failure_leaves_no_file(self):  # 8
        text = self.coverage()[1]
        for n, effect in enumerate((OSError("disk full"), [5, OSError("disk full")])):  # nothing written; a partial write
            with self.subTest(case=n):
                target = self.tmp / ("cov%d.json" % n)
                code, out = self.in_process(target, lambda tool: mock.patch.object(os, "write", side_effect=effect))
                self.assertEqual((code, out), (1, text + "io: OSError\n"))
                self.assertFalse(os.path.lexists(target))

    def test_target_created_after_check_is_not_overwritten(self):
        target = self.tmp / "cov.json"

        def racing(real):
            def result(cfg, seed_dir):
                target.write_text("other", encoding="utf-8")
                return real(cfg, seed_dir)
            return result
        code, out = self.in_process(target, lambda tool: mock.patch.object(tool, "coverage_result",
                                                                           racing(tool.coverage_result)))
        self.assertEqual((code, out.splitlines()[-1]), (1, "io: FileExistsError"))
        self.assertEqual(target.read_text(encoding="utf-8"), "other")


if __name__ == "__main__":
    unittest.main()
