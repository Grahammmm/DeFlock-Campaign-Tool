"""DFGL-21: an anonymous STAGE_INTERNAL_ERROR now names the exception class and function only."""
import contextlib
import io
import json
import unittest
from unittest import mock

from campaign_tool.records import ocr_stage, run_orchestrator, run_stages

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


if __name__ == "__main__":
    unittest.main()
