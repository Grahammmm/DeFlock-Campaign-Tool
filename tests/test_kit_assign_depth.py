"""RF0415-1: overdeep request-owner JSON is a fixed unreadable-store refusal, synthetic only.

The real-file cases use genuine nesting below the byte cap and the unmodified stdlib decoder; they need an
interpreter whose decoder actually refuses this depth (verified on CPython 3.12) and skip otherwise.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import kit_assign
from tests import test_kit_assign as existing
from tests.test_kit_assign_bounds import snapshot

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPTH = 100_000
ARRAY = "[" * DEPTH + "0" + "]" * DEPTH
OBJECT = '{"x":' * DEPTH + "0" + "}" * DEPTH
ERROR = "kit/assignments.json unreadable: JSON nesting is too deep"
STOPPED = f"Stopped: {ERROR}\n"
SENTINEL = "PRIVATE-DEPTH-SENTINEL"


def decoder_refuses_depth():
    try:
        json.loads(ARRAY)
    except RecursionError:
        return True
    return False


def document(kind, key):
    """Raw store text with one deeply nested value in the named place."""
    if kind == "owner":
        return '{"schema_version":1,"assignments":{%s:%s}}' % (json.dumps(key), ARRAY)
    if kind == "extra":
        return '{"schema_version":1,"assignments":{},"%s":%s}' % (SENTINEL, OBJECT)
    if kind == "assignments":
        return '{"schema_version":1,"assignments":%s}' % ARRAY
    return ARRAY  # "top"


@unittest.skipUnless(decoder_refuses_depth(), "this interpreter's JSON decoder accepts the chosen nesting")
class KitAssignDepthTests(unittest.TestCase):
    setUp = existing.KitAssignTests.setUp
    ok = existing.KitAssignTests.ok
    put_sent = existing.KitAssignTests.put_sent
    write_store = existing.KitAssignTests.write_store

    def deep(self, kind="owner"):
        text = document(kind, self.a)
        self.assertLess(len(text.encode()), kit_assign.MAX_BYTES)
        self.store.write_text(text, encoding="utf-8")
        return text

    def refused(self, *arguments):
        before = snapshot(self.root)
        with mock.patch("os.replace", side_effect=AssertionError("replacement attempted")), \
                mock.patch("tempfile.mkstemp", side_effect=AssertionError("temporary file attempted")):
            code, out, err = existing.run(arguments[0], "--directory", self.root, *arguments[1:])
        self.assertEqual((code, out, err), (1, "", STOPPED))
        self.assertNotIn("Traceback", err)
        self.assertNotIn(SENTINEL, err)
        self.assertEqual(snapshot(self.root), before)

    def test_real_decoder_depth_failure_is_named_unreadable(self):
        for kind in ("owner", "extra", "assignments", "top"):
            with self.subTest(kind=kind):
                text = self.deep(kind)
                with self.assertRaises(ValueError) as caught:
                    kit_assign.read_assignments(self.root)
                self.assertIs(type(caught.exception), ValueError)
                self.assertEqual(str(caught.exception), ERROR)
                self.assertIsNone(caught.exception.__cause__)
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertEqual(self.store.read_text(encoding="utf-8"), text)
        # Ordinary shape errors keep their own diagnostics when decoding succeeds.
        self.store.write_text('{"schema_version":1,"assignments":{"%s":[]}}' % self.a, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "^kit/assignments.json unreadable: an assignment is malformed$"):
            kit_assign.read_assignments(self.root)

    def test_depth_translation_is_local_and_private(self):
        self.write_store({self.a: "Zoë"})
        secret = RecursionError(f"{SENTINEL} {self.root}")
        with mock.patch.object(kit_assign.json, "loads", side_effect=secret):
            with self.assertRaises(ValueError) as caught:
                kit_assign.read_assignments(self.root)
        self.assertEqual(str(caught.exception), ERROR)
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)
        for unrelated in (AssertionError(SENTINEL), KeyError(SENTINEL), ZeroDivisionError(SENTINEL)):
            with self.subTest(exception=type(unrelated).__name__):
                with mock.patch.object(kit_assign.json, "loads", side_effect=unrelated):
                    with self.assertRaises(type(unrelated)):
                        kit_assign.read_assignments(self.root)
        # A recursion failure outside the decode block is not reclassified.
        with mock.patch.object(kit_assign, "_entry_ok", side_effect=RecursionError(SENTINEL)):
            with self.assertRaises(RecursionError):
                kit_assign.read_assignments(self.root)

    def test_set_clear_and_noop_depth_refusal_never_write(self):
        self.put_sent({self.a: existing.imported("2026-03-02"), f"{self.a}#press": existing.imported("2026-03-03"),
                       self.b: existing.imported("2026-03-04", [{"on": "2026-03-06", "kind": "closed"}])})
        self.deep()
        for agency, action in ((self.a, ("--to", "Replacement")), (self.a, ("--clear",)),
                               (self.a, ("--to", "Zoë")), (self.b, ("--clear",)),
                               (self.b, ("--to", "Alex", "--request", "main"))):
            with self.subTest(agency=agency, action=action):
                self.refused("kit-assign", "--agency", agency, *action)
        self.refused("kit-assign", "--agency", self.a, "--request", "press", "--to", "Sam")

    def test_status_due_coverage_depth_refusal_has_no_partial_output(self):
        self.put_sent({self.a: existing.imported("2026-03-02")})
        self.deep()
        for arguments in (("kit-status",), ("kit-status", "--owner", "Zoë"),
                          ("kit-due", *existing.ASOF), ("kit-due", *existing.ASOF, "--owner", "Zoë"),
                          ("kit-coverage",), ("kit-coverage", "--csv")):
            with self.subTest(arguments=arguments):
                self.refused(*arguments)
        fresh = self.tmp / "report.csv"
        self.refused("kit-status", "--csv", fresh)
        self.assertFalse(fresh.exists())
        existing_csv = self.tmp / "existing.csv"
        existing_csv.write_bytes(b"existing csv sentinel\n")
        code, out, err = existing.run("kit-status", "--directory", self.root, "--csv", existing_csv)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertNotIn("Traceback", err)
        self.assertEqual(existing_csv.read_bytes(), b"existing csv sentinel\n")

    def test_real_cli_subprocess_has_no_traceback(self):
        self.put_sent({self.a: existing.imported("2026-03-02")})
        text = self.deep()
        done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "kit-status", "--directory",
                               str(self.root)], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (1, "", STOPPED))
        self.assertNotIn("RecursionError", done.stderr)
        self.assertEqual(self.store.read_text(encoding="utf-8"), text)

    def test_today_depth_error_is_one_source_item_not_a_crash(self):
        self.put_sent({self.a: existing.imported("2026-03-02")})
        agencies_path = self.kit / "agencies.json"
        agencies = json.loads(agencies_path.read_bytes())
        for agency in agencies:
            if agency["agency_id"] == self.d:
                agency.update(selected=True, verified=True, records_email="records@agency.synthetic.example",
                              verification={"source_url": "https://agency.synthetic.example/records",
                                            "fields": ["records_email"], "checked_on": "2025-01-01"})
        agencies_path.write_text(json.dumps(agencies), encoding="utf-8")
        self.deep()
        before = snapshot(self.root)
        for extra in ((), ("--owner", "Zoë")):
            with self.subTest(extra=extra):
                code, out, err = existing.run("today", "--directory", self.root, *existing.ASOF, "--json", *extra)
                self.assertEqual((code, err), (0, ""))
                items = json.loads(out)
                self.assertFalse(any(i["kind"].startswith("request_") for i in items))
                bad = [i for i in items if i["kind"] == "unreadable"]
                self.assertEqual(len(bad), 1)
                self.assertIn(ERROR, bad[0]["what"])
                self.assertIn("kit/assignments.json", bad[0]["what"])
                self.assertIn("stale_contact", {i["kind"] for i in items})
                self.assertEqual(snapshot(self.root), before)
        code, out, err = existing.run("today", "--directory", self.root, *existing.ASOF)
        self.assertEqual((code, err), (0, ""))
        self.assertIn(ERROR, out)
        self.assertNotIn("Traceback", out)


class KitAssignDepthControlTests(unittest.TestCase):
    setUp = existing.KitAssignTests.setUp
    ok = existing.KitAssignTests.ok
    put_sent = existing.KitAssignTests.put_sent
    write_store = existing.KitAssignTests.write_store

    def test_missing_valid_and_shallow_contracts_unchanged(self):
        self.assertEqual(kit_assign.read_assignments(self.root), {})
        self.write_store({self.a: "Zoë", f"{self.b}#press": "Team", "ca-gone-pd": "Orphan"})
        self.assertEqual(kit_assign.read_assignments(self.root),
                         {self.a: "Zoë", f"{self.b}#press": "Team", "ca-gone-pd": "Orphan"})
        for raw, diagnostic in ((b"{", "not JSON, or a key is repeated"),
                                (b'{"schema_version":1,"schema_version":1,"assignments":{}}',
                                 "not JSON, or a key is repeated"),
                                (b'{"schema_version":true,"assignments":{}}', "expected schema_version"),
                                (b'{"schema_version":1,"assignments":{"%s":[[[[0]]]]}}' % self.a.encode(),
                                 "an assignment is malformed")):
            with self.subTest(raw=raw):
                self.store.write_bytes(raw)
                with self.assertRaisesRegex(ValueError, "unreadable: " + diagnostic):
                    kit_assign.read_assignments(self.root)
                self.assertEqual(self.store.read_bytes(), raw)

    def test_valid_owner_operations_and_noops_unchanged(self):
        self.put_sent({self.a: existing.imported("2026-03-02")})
        out = self.ok("kit-assign", "--directory", self.root, "--agency", self.a, "--to", "Alex")
        self.assertEqual(json.loads(out.removesuffix(existing.DONE)),
                         {"agency_id": self.a, "request": "main", "owner": "Alex", "changed": True})
        before = snapshot(self.root)
        out = self.ok("kit-assign", "--directory", self.root, "--agency", self.a, "--to", "Alex")
        self.assertFalse(json.loads(out.removesuffix(existing.DONE))["changed"])
        self.assertEqual(snapshot(self.root), before)
        self.ok("kit-assign", "--directory", self.root, "--agency", self.a, "--clear")
        self.assertEqual(json.loads(self.store.read_text(encoding="utf-8")),
                         {"schema_version": 1, "assignments": {}})


if __name__ == "__main__":
    unittest.main()
