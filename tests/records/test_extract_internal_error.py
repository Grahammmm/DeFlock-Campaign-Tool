"""DFGL-21: an anonymous STAGE_INTERNAL_ERROR now names the exception class and function only."""
import builtins
import contextlib
import io
import json
import sys
import unittest
from unittest import mock

from campaign_tool.records import native_extract, ocr_stage, run, run_orchestrator, run_stages

SCHEMA = run_orchestrator.load_stage_result_schema()
PRIVATE = "PRIVATE-VALUE-1"


def run_main(main, argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(argv)
    return code, out.getvalue()


class InternalErrorNamesClassAndFunction(unittest.TestCase):
    def assertNamed(self, code, raw, function):
        self.assertEqual(code, 1)
        self.assertNotIn(PRIVATE, raw)
        line = json.loads(raw)
        self.assertEqual(line["error_code"], "STAGE_INTERNAL_ERROR")
        self.assertEqual(line["internal"], {"type": "KeyError", "where": function})
        self.assertIsNotNone(run_orchestrator.parse_stage_result(raw.encode(), SCHEMA), "schema-valid")

    def test_run_stages_extract_names_the_innermost_function(self):
        with mock.patch.object(run_stages, "paths", side_effect=KeyError(PRIVATE)):
            code, raw = run_main(run_stages.main, ["extract", "--project", "/p", "--timeout", "60",
                                                   "--run-id", "r"])
        self.assertNamed(code, raw, "campaign_tool.records.run_stages.stage_extract")

    def test_run_stages_handler_outside_the_package_names_main(self):
        with mock.patch.dict(run_stages.HANDLERS, {"extract": mock.Mock(side_effect=KeyError(PRIVATE))}):
            code, raw = run_main(run_stages.main, ["extract", "--project", "/p", "--timeout", "60",
                                                   "--run-id", "r"])
        self.assertNamed(code, raw, "campaign_tool.records.run_stages.main")

    def test_ocr_stage_names_the_innermost_function(self):
        with mock.patch.object(ocr_stage, "build_stage", side_effect=KeyError(PRIVATE)):
            code, raw = run_main(ocr_stage.main, ["--queue", "/q", "--source-root", "/s", "--out", "/o"])
        self.assertNamed(code, raw, "campaign_tool.records.ocr_stage.run")

    def test_orchestrator_keeps_internal_on_the_stage_entry(self):
        line = run_stages.result("fail", {"failed": 1}, "STAGE_INTERNAL_ERROR",
                                 internal={"type": "KeyError", "where": "campaign_tool.records.x.f"})
        parsed = run_orchestrator.parse_stage_result(json.dumps(line).encode(), SCHEMA)
        self.assertEqual(parsed["internal"], line["internal"])

    def test_message_free_fields_reject_free_text(self):
        line = run_stages.result("fail", {"failed": 1}, "STAGE_INTERNAL_ERROR",
                                 internal={"type": "KeyError", "where": f"/p/{PRIVATE} x"})
        self.assertIsNone(run_orchestrator.parse_stage_result(json.dumps(line).encode(), SCHEMA))


class ExtractFailingConditionsAreCoded(unittest.TestCase):
    """H1 (a lazy import fails under the runtime) and H2 (the OCR probe raises) end in a coded outcome."""

    ARGV = ["extract", "--project", "/p", "--timeout", "60", "--run-id", "r"]

    def deny_import(self, module, member):
        real = builtins.__import__

        def guarded(name, globals=None, locals=None, fromlist=(), level=0):
            if level and name == module and member in (fromlist or ()) \
                    and sys._getframe(1).f_code.co_name == "stage_extract":
                raise ImportError(PRIVATE)
            return real(name, globals, locals, fromlist, level)

        return mock.patch.object(builtins, "__import__", guarded)

    def assertCoded(self, argv, code_name):
        code, raw = run_main(run_stages.main, argv)
        line = json.loads(raw)
        self.assertEqual(code, 1)
        self.assertNotIn(PRIVATE, raw)
        self.assertEqual(line["error_code"], code_name)
        self.assertNotEqual(line["error_code"], "STAGE_INTERNAL_ERROR")
        self.assertEqual(line["internal"]["where"], "campaign_tool.records.run_stages.stage_extract")
        self.assertIsNotNone(run_orchestrator.parse_stage_result(raw.encode(), SCHEMA), "schema-valid")

    def test_h1_native_extract_import_failure_is_coded(self):
        with self.deny_import("", "native_extract"):
            self.assertCoded(self.ARGV, "EXTRACT_IMPORT_FAILED")

    def test_h1_vision_import_failure_is_coded(self):
        with mock.patch.object(run, "local_ocr_tools", return_value=({"tesseract": {}}, "sig")), \
                self.deny_import("extract", "vision"):
            self.assertCoded([*self.ARGV, "--ocr", "--ocr-vision-config", "/c"], "EXTRACT_IMPORT_FAILED")

    def test_h1_ocr_review_import_failure_is_coded(self):
        with mock.patch.object(native_extract, "run", return_value={}), \
                mock.patch.object(run_stages, "extract_result", return_value=(run_stages.result("ok", {}), 0)), \
                self.deny_import("ocr_review", "pending"):
            self.assertCoded(self.ARGV, "EXTRACT_IMPORT_FAILED")

    def test_h2_ocr_probe_raising_is_coded_not_internal(self):
        with mock.patch.object(run, "local_ocr_tools", side_effect=OSError(PRIVATE)):
            self.assertCoded([*self.ARGV, "--ocr"], "EXTRACT_OCR_UNAVAILABLE")

    def test_other_exceptions_still_report_the_internal_error_code(self):
        with mock.patch.object(run, "local_ocr_tools", return_value=({"tesseract": {}}, "sig")), \
                mock.patch.object(run_stages, "paths", side_effect=KeyError(PRIVATE)):
            code, raw = run_main(run_stages.main, [*self.ARGV, "--ocr"])
        self.assertEqual(json.loads(raw)["error_code"], "STAGE_INTERNAL_ERROR")


if __name__ == "__main__":
    unittest.main()
