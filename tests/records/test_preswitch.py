"""Synthetic inventory, flag and executed-classifier acceptance (DFB-02b)."""
import builtins
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from campaign_tool.records import preswitch as p
from campaign_tool.records import extraction_routes as routes, native_extract as ne
from campaign_tool.records.intake import native_msg

BLOCKED = {"PIL": None, "PIL.Image": None, "extract_msg": None, "olefile": None}


class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.module = "campaign_tool.records.run_stages"
        self.file = self.root / "campaign_tool/records/run_stages.py"
        self.file.parent.mkdir(parents=True)
        self.file.write_text("def stage():\n    import notinstalledpkg\n")

    def entry(self):
        _, functions = p.source_info(self.file)
        return dict(module=self.module, qualname="stage", top="notinstalledpkg",
                    fingerprint=functions["stage"], when_missing="typed refusal",
                    proof="tests.records.test_preswitch.DependencyTests.test_inventory_and_stale")

    def test_unreviewed_site_is_fail(self):
        result = p.deps(self.root, inventory=[])
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["rows"], [dict(module=self.module, line=2, qualname="stage",
                         top="notinstalledpkg", status="UNREVIEWED_MISSING")])

    def test_inventory_and_stale(self):
        entry = self.entry()
        result = p.deps(self.root, inventory=[entry])
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["rows"][0]["status"], "CONTAINED")
        self.file.write_text(self.file.read_text().replace("    import", "     import"))
        result = p.deps(self.root, inventory=[entry])
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual([r["status"] for r in result["rows"]], ["STALE", "UNREVIEWED_MISSING"])
        result = p.deps(self.root, inventory=[entry], own=False)
        self.assertEqual([r["status"] for r in result["rows"]], ["UNREVIEWED_MISSING"])
        self.file.write_text("def removed():\n    pass\n")
        self.assertEqual(p.deps(self.root, inventory=[entry])["rows"][0]["status"], "STALE")

    def test_false_green_shapes_all_fail(self):
        shapes = [
            'def stage():\n    _importable("notinstalledpkg")\n    import notinstalledpkg\n',
            'def stage():\n    if False:\n        _importable("notinstalledpkg")\n    import notinstalledpkg\n',
            'def stage():\n    try:\n        import notinstalledpkg\n    except ImportError:\n        raise\n',
            'def stage():\n    try:\n        import notinstalledpkg\n    except ImportError:\n        pass\n    notinstalledpkg.use()\n',
            'def stage(_importable):\n    if _importable("notinstalledpkg"):\n        import notinstalledpkg\n',
            'from contextlib import suppress\ndef stage():\n    with suppress(ImportError):\n        import notinstalledpkg\n    notinstalledpkg.use()\n',
            'def stage():\n    if False:\n        import notinstalledpkg\n',
        ]
        for source in shapes:
            with self.subTest(source=source):
                self.file.write_text(source)
                result = p.deps(self.root, inventory=[])
                self.assertEqual(result["status"], "FAIL")
                self.assertEqual(result["rows"][0]["status"], "UNREVIEWED_MISSING")

    def test_installed_and_outside_closure(self):
        self.file.write_text("def stage():\n    import installedpkg\n")
        self.file.with_name("outside.py").write_text("import notinstalledpkg\n")
        with mock.patch.object(p.importlib.util, "find_spec", return_value=object()):
            self.assertEqual(p.deps(self.root, inventory=[]), p.check("PASS", []))

    def test_closure_relative_absolute_nested_and_initializers(self):
        self.file.write_text("def stage():\n    from . import child\n")
        self.file.with_name("child.py").write_text("from campaign_tool.records.other import f\n")
        self.file.with_name("other.py").write_text("def f():\n    import notinstalledpkg\n")
        (self.root / "campaign_tool/__init__.py").write_text("import notinstalledparent\n")
        result = p.deps(self.root, inventory=[])
        self.assertEqual({r["top"] for r in result["rows"]}, {"notinstalledpkg", "notinstalledparent"})
        self.file.write_text("pass\n")
        result = p.deps(self.root, ["campaign_tool.records.other"], inventory=[])
        self.assertIn("notinstalledpkg", {r["top"] for r in result["rows"]})

    def test_own_tree_missing_sites_and_proof_ids(self):
        original = p.importlib.util.find_spec
        with mock.patch.object(p.importlib.util, "find_spec", side_effect=lambda top: None if top in BLOCKED else original(top)):
            result = p.deps(p.OWN_ROOT)
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(len(result["rows"]), 6)
        self.assertEqual({r["status"] for r in result["rows"]}, {"CONTAINED"})
        inventory = json.loads(p.INVENTORY.read_text())
        for entry in inventory:
            suite = unittest.defaultTestLoader.loadTestsFromName(entry["proof"])
            self.assertEqual(suite.countTestCases(), 1)
            self.assertNotIn("_FailedTest", str(suite))


class FormTests(unittest.TestCase):
    def test_all_forms_and_missing_pillow(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(sys.modules, BLOCKED):
            result = p.probe_forms(Path(temp))
        self.assertEqual(result["status"], "PASS", result)
        rows = {r["form"]: r for r in result["rows"]}
        self.assertEqual(set(rows), routes.SUPPORTED | routes.IMAGES | set(ne.FORM_PARSER) | {"xls"})
        for form in routes.IMAGES:
            self.assertEqual((rows[form]["ocr_off"], rows[form]["ocr_on"]), ("AWAITING_OCR", "PARSER_MISSING"))

    def test_removed_guard_reports_real_exception_frame(self):
        original = ne._importable
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(sys.modules, BLOCKED), mock.patch.object(
                ne, "_importable", side_effect=lambda name: True if name == ne.IMAGE_PARSER else original(name)):
            result = p.probe_forms(Path(temp))
        self.assertEqual(result["status"], "FAIL")
        for row in result["rows"]:
            if row["form"] in routes.IMAGES:
                self.assertEqual(row["status"], "FORM_PROBE_RAISED")
                self.assertEqual(row["ocr_on_internal"], dict(type="ModuleNotFoundError",
                                 where="campaign_tool.records.native_extract.classify_form"))


class FlagAndOutputTests(unittest.TestCase):
    def test_rendered_plan_and_unknown_flag(self):
        template = p.OWN_ROOT / "deploy/records-runtime/targets/live-container.json.in"
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            plan = temp / "plan.json"
            # Rendering replaces placeholders only; the checker never uses their values.
            import re
            plan.write_text(re.sub(r"@[A-Z_]+@", "synthetic", template.read_text()))
            stages = p.load_plan(plan)
            self.assertEqual(p.stage_flags(p.OWN_ROOT, stages, temp)["status"], "PASS")
            name, module, verbs, flags = stages[0]
            result = p.stage_flags(p.OWN_ROOT, [(name, module, verbs, flags + ["--no-such-flag"])], temp)
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(result["rows"], [dict(stage=name, module=module, flag="--no-such-flag",
                                                  status="STAGE_FLAG_UNKNOWN")])

    def test_help_import_failure_is_sanitized_and_never_runs_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "campaign_tool"
            package.mkdir()
            (package / "__init__.py").write_text("")
            (package / "broken.py").write_text('def fail():\n    import notinstalledpkg\nfail()\n')
            result = p.stage_flags(root, [("extract", "campaign_tool.broken", [], ["--private"])], root)
            self.assertEqual(result["status"], "FAIL")
            row = result["rows"][0]
            self.assertEqual(row["status"], "STAGE_IMPORT_FAILED")
            self.assertEqual(row["internal"], dict(type="ModuleNotFoundError", where="campaign_tool.broken.fail"))
            (package / "safe.py").write_text('import argparse\np=argparse.ArgumentParser()\np.add_argument("--safe")\np.parse_args()\nraise RuntimeError("STAGE_EXECUTED")\n')
            self.assertEqual(p.stage_flags(root, [("extract", "campaign_tool.safe", [], ["--safe"])], root)["status"], "PASS")

    def test_json_schema_privacy_usage_and_foreign_skip(self):
        with mock.patch.dict(sys.modules, BLOCKED), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(p.main(["--json"]), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(set(report), {"schema_version", "checks", "overall"})
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(set(report["checks"]), set("ABC"))
        self.assertEqual(report["checks"]["B"]["status"], "skipped")
        for check in report["checks"].values():
            self.assertEqual(set(check), {"status", "rows"})
        self.assertNotIn(str(p.OWN_ROOT.parent), output.getvalue())
        self.assertNotIn("synthetic", output.getvalue())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "campaign_tool/records/run_stages.py"
            path.parent.mkdir(parents=True)
            path.write_text("pass\n")
            self.assertEqual(p.run(root)["checks"]["C"], p.check("skipped", [{"status": "OTHER_TREE"}]))
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                p.main(["--engine-root", str(root / "absent")])
            self.assertEqual(caught.exception.code, 2)


class ProofTests(unittest.TestCase):
    def test_image_child_missing_dependency(self):
        # Exercise EACH import site under sys.modules blocking, then exercise the
        # existing bounded-child failure translation. Earlier PIL imports must
        # succeed to reach later sites, so block at the named function boundary.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "synthetic.png"
            raw = b"synthetic image"
            source.write_bytes(raw)
            sha = hashlib.sha256(raw).hexdigest()
            originals = {name: getattr(routes, name) for name in ("_image", "image_pdf_wrapper")}
            for site in ("_child", "_image", "image_pdf_wrapper"):
                with self.subTest(site=site):
                    visited = []
                    def block_call(name):
                        def call(*args):
                            visited.append(name)
                            with mock.patch.dict(sys.modules, {"PIL": None, "PIL.Image": None}):
                                return originals[name](*args)
                        return call
                    def child(args, timeout):
                        dest, run = Path(args[8]), Path(args[9])
                        try:
                            if site == "_child":
                                visited.append(site)
                                with mock.patch.dict(sys.modules, {"PIL": None, "PIL.Image": None}):
                                    routes._child(args[5], args[6], args[7], dest, run, True)
                            else:
                                # Earlier imports succeed; only the exact reviewed import fails.
                                from PIL import Image
                                with mock.patch.object(routes, site, side_effect=block_call(site)):
                                    if site == "image_pdf_wrapper":
                                        with mock.patch.object(routes, "_image", return_value=([{}], "pillow", "test")):
                                            routes._child(args[5], args[6], args[7], dest, run, True)
                                    else:
                                        routes._child(args[5], args[6], args[7], dest, run, True)
                        except ModuleNotFoundError:
                            return SimpleNamespace(returncode=1)
                        self.fail("missing dependency did not execute")
                    with mock.patch.object(routes, "run_bounded", side_effect=child):
                        result = routes.extract(str(source), sha, str(root), timeout=10)
                    self.assertEqual(visited, [site])
                    self.assertEqual(result["status"], "blocked")
                    codes = {i["code"] for i in result["issues"]}
                    self.assertEqual(codes, {"decoder_unavailable_or_failed"})
                    self.assertEqual(ne.classify_codes(codes), ne.CORRUPT)

    def test_msg_missing_dependency(self):
        # NativeMSGError is the existing typed refusal, not a stage escape.
        for top in ("extract_msg", "olefile"):
            blocked = {top: None}
            if top == "olefile":
                blocked["extract_msg"] = SimpleNamespace()  # reach the second import itself
            with self.subTest(top=top), mock.patch.dict(sys.modules, blocked):
                with self.assertRaisesRegex(native_msg.NativeMSGError, "^msg_decoder_unavailable$"):
                    native_msg.inventory(native_msg.MAGIC)
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    source = root / "synthetic.msg"
                    source.write_bytes(native_msg.MAGIC)
                    def child(args, timeout):
                        try:
                            routes._child(args[5], args[6], args[7], Path(args[8]), Path(args[9]))
                        except ValueError:
                            return SimpleNamespace(returncode=1)
                        self.fail("missing MSG decoder did not refuse")
                    with mock.patch.object(routes, "run_bounded", side_effect=child):
                        result = routes.extract(str(source), hashlib.sha256(native_msg.MAGIC).hexdigest(), str(root))
                    codes = {issue["code"] for issue in result["issues"]}
                    self.assertEqual(codes, {"decoder_unavailable_or_failed"})
                    self.assertEqual(ne.classify_codes(codes), ne.CORRUPT)
        with mock.patch.dict(sys.modules, {"extract_msg": None}):
            pipeline = SimpleNamespace(_form=lambda *_: "msg", _original=lambda _: {}, ocr_tools=None)
            self.assertEqual(ne.classify_form(pipeline, "probe", missing=[]), ("msg", ne.PARSER_MISSING))


if __name__ == "__main__":
    unittest.main()
