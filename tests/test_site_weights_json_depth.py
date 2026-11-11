"""RF2340-1: recursion-exhausting weight baselines get the named "not valid JSON" refusal, never a traceback.

Synthetic content only; no network.
"""
import contextlib
import io
import json
import unittest
from unittest import mock

from campaign_tool import cli, site_check
from tests.test_site_weights_record import Base

DEPTH = 10000
RECORD = {"schema": "page-weights-v1", "budget_kb": 1,
          "pages": [{"page": "index.html", "html_bytes": 1, "total": 1, "assets": []}]}


def deep_array(depth=DEPTH):
    return "[" * depth + "]" * depth


def bodies():
    head = json.dumps(RECORD)[:-1]
    return {
        "top-array": deep_array(),
        "extension-array": head + ', "x": ' + deep_array() + "}",
        "extension-object": head + ', "x": ' + '{"a":' * DEPTH + "1" + "}" * DEPTH + "}",
    }


class DepthTests(Base):
    def write(self, text):
        path = self.private / "base.json"
        path.write_text(text)
        return path

    def test_real_overdeep_baselines_are_named_value_errors(self):
        for name, text in bodies().items():
            path = self.write(text)
            with self.subTest(name), self.assertRaises(ValueError) as caught:
                site_check.read_weights(path)
            self.assertEqual(str(caught.exception), f"--weights-baseline {path}: not valid JSON")
            self.assertTrue(caught.exception.__suppress_context__)

    def test_direct_budget_refuses_before_table_and_verdict(self):
        self.public.mkdir()
        (self.public / "a.html").write_text("<!doctype html><html><body>a</body></html>")
        path = self.write(deep_array())
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(ValueError):
            cli.weight_budget(self.public, 100000, weights_baseline=path, weights_out=self.private / "out.json")
        self.assertEqual(out.getvalue(), "")
        self.assertFalse((self.private / "out.json").exists())

    def test_actual_build_cli_exits_one_without_traceback_or_output(self):
        out = self.private / "out.json"
        for name, text in bodies().items():
            path = self.write(text)
            with self.subTest(name):
                code, stdout, err = self.build("--weights-baseline", str(path), "--weights-out", str(out))
                self.assertEqual(code, 1)
                self.assertIn(f"--weights-baseline {path}: not valid JSON", err)
                self.assertNotIn("Traceback", err)
                self.assertNotIn("RecursionError", err + stdout)
                self.assertFalse(out.exists())

    def test_injected_recursion_text_is_not_rendered(self):
        path = self.write(json.dumps(RECORD))
        real = json.loads

        def loads(text, *args, **kwargs):
            if text.startswith('{"schema": "page-weights-v1"'):
                raise RecursionError("SYNTHETIC-MARKER")
            return real(text, *args, **kwargs)

        with mock.patch.object(site_check.json, "loads", side_effect=loads):
            with self.assertRaises(ValueError) as caught:
                site_check.read_weights(path)
            self.assertEqual(str(caught.exception), f"--weights-baseline {path}: not valid JSON")
            code, stdout, err = self.build("--weights-baseline", str(path))
        self.assertEqual(code, 1)
        self.assertNotIn("SYNTHETIC-MARKER", err + stdout)

    def test_shallow_and_decodable_inputs_keep_old_contract(self):
        ok = self.write(json.dumps({**RECORD, "x": json.loads("[" * 50 + "]" * 50)}))
        self.assertEqual(site_check.read_weights(ok)["schema"], "page-weights-v1")
        bad = self.write("[[]]")
        with self.assertRaisesRegex(ValueError, "not a page-weights-v1 record"):
            site_check.read_weights(bad)
        bad = self.write("{nope")
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            site_check.read_weights(bad)


if __name__ == "__main__":
    unittest.main()
