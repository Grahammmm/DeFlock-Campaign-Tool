"""kit-timing: a kit/agencies.json whose JSON nesting exhausts the decoder is an unreadable name file (fixed refusal).

Synthetic, offline. The deep inputs are real regular files built iteratively; no recursion limit is changed."""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit, kit_timing  # noqa: E402
from tests import test_kit_timing as base  # noqa: E402
from tests.test_kit_status_csv import run  # noqa: E402

A, B = base.A, base.B
ERROR = "kit/agencies.json unreadable: not a JSON list of agencies with names"
DEPTH = 100_000
CANARY = "NAME-FILE-CANARY-9931"


def deep_text(kind="array", depth=DEPTH, inner="0"):
    if kind == "array":
        return "[" * depth + inner + "]" * depth
    return '{"k":' * depth + inner + "}" * depth


def agencies_text(nested, *, selected=True, before=(), after=()):
    """A plausible agency list; ``nested`` is raw JSON text for one ignored field."""
    rows = [json.dumps(r) for r in before]
    rows.append('{"agency_id": "%s", "name": "Alpha", "selected": %s, "extra": %s}'
                % (A, "true" if selected else "false", nested))
    rows += [json.dumps(r) for r in after]
    return "[" + ", ".join(rows) + "]"


class Fixture(base.Base):
    def write_names(self, text):
        (self.root / "kit" / "agencies.json").write_text(text)

    def history(self):
        self.sent((A, "main", base.entry(A, "2026-03-01", [base.step("2026-03-02", "acknowledged")])))

    def snapshot(self):
        return {str(p.relative_to(self.tmp)): (p.lstat().st_mode, p.lstat().st_mtime_ns,
                                               p.read_bytes() if p.is_file() else None)
                for p in sorted(self.tmp.rglob("*"))}

    def cli_subprocess(self, *extra):
        done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "kit-timing", "--directory", str(self.root),
                               *map(str, extra)], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
        return done.returncode, done.stdout, done.stderr


class DeepNameFileTests(Fixture):
    def test_real_deep_agency_metadata_uses_existing_name_refusal(self):
        self.history()
        for kind in ("array", "object"):
            self.write_names(agencies_text(deep_text(kind)))
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError) as caught:
                    self.report()
                self.assertEqual(str(caught.exception), ERROR)
                self.assertIsNone(caught.exception.__cause__)
                self.assertTrue(caught.exception.__suppress_context__)
        self.write_names(agencies_text(deep_text("array", 50)))  # decodable nesting stays admitted
        self.assertEqual([r["name"] for r in self.report()["requests"]], ["Alpha"])

    def test_deep_name_file_cli_plain_and_csv_stop_without_traceback(self):
        self.history()
        self.write_names(agencies_text(deep_text()))
        target = self.private / "t.csv"
        before = self.snapshot()
        extras = ((), ("--csv", target), ("--as-of", "2026-03-01", "--csv", target))
        expected = (1, "", "Stopped: " + ERROR + "\n")
        with mock.patch.object(kit_timing.kit_track, "read_sent", side_effect=AssertionError("history read")) as hist, \
                mock.patch.object(kit_timing, "format_report", side_effect=AssertionError("table built")) as fmt, \
                mock.patch.object(kit_timing, "check_csv", side_effect=AssertionError("target checked")) as chk, \
                mock.patch.object(kit_timing, "write_csv", side_effect=AssertionError("write_csv used")) as wr, \
                mock.patch("campaign_tool.kit_export.write_rows", side_effect=AssertionError("write_rows used")) as rows:
            for extra in extras:
                with self.subTest(in_process=[str(e) for e in extra]):
                    self.assertEqual(run("kit-timing", "--directory", self.root, *extra), expected)
            for spy in (hist, fmt, chk, wr, rows):
                spy.assert_not_called()
        for extra in extras:
            with self.subTest(subprocess=[str(e) for e in extra]):
                code, out, err = self.cli_subprocess(*extra)
                self.assertEqual((code, out, err), expected)
                self.assertNotIn("Traceback", err)
        self.assertFalse(target.exists())
        self.assertEqual(self.snapshot(), before)

    def test_unprocessable_names_are_not_optional_or_request_filtered(self):
        deep = deep_text()
        good = [{"agency_id": B, "name": "Beta"}]
        cases = {
            "missing-sent": (lambda: None, agencies_text(deep), None),
            "empty-sent": (self.sent, agencies_text(deep), None),
            "history": (self.history, agencies_text(deep), None),
            "early-cutoff": (self.history, agencies_text(deep), "2026-02-28"),
            "deselected": (self.history, agencies_text(deep, selected=False), None),
            "unreferenced": (self.history, json.dumps(good)[:-1] + ', {"agency_id": "ca-other", "name": "O", "x": '
                             + deep + "}]", None),
            "later-bad-row": (self.history, agencies_text("0", before=good)[:-1] + ', {"agency_id": "ca-z", "name": "Z", '
                              '"x": ' + deep + "}]", None),
        }
        for label, (setup, text, as_of) in cases.items():
            with self.subTest(label):
                (self.root / "kit" / "sent.json").unlink(missing_ok=True)
                setup()
                self.write_names(text)
                with self.assertRaises(ValueError) as caught:
                    self.report(as_of=as_of)
                self.assertEqual(str(caught.exception), ERROR)

    def test_name_parser_refusal_is_value_free_and_read_only(self):
        self.history()
        self.write_names(agencies_text(deep_text(inner='"%s"' % CANARY)))
        before = self.snapshot()
        for _ in range(2):
            with self.assertRaises(ValueError) as caught:
                self.report()
            self.assertEqual(str(caught.exception), ERROR)
            self.assertNotIn(CANARY, str(caught.exception))
            code, out, err = self.cli_subprocess()
            self.assertEqual((code, out, err), (1, "", "Stopped: " + ERROR + "\n"))
            self.assertNotIn(CANARY, err)
            self.assertNotIn(str(self.tmp), err)
        self.assertEqual(self.snapshot(), before)


class BoundaryTests(Fixture):
    def test_exception_translation_stays_at_name_load_boundary(self):
        self.history()
        self.names(**{A: "Alpha"})
        for raised in (RecursionError("depth"), ValueError("bad"), OSError("io")):
            with self.subTest(raised=type(raised).__name__), \
                    mock.patch.object(kit, "_load_existing", side_effect=raised):
                with self.assertRaises(ValueError) as caught:
                    self.report()
                self.assertEqual(str(caught.exception), ERROR)
                self.assertTrue(caught.exception.__suppress_context__)
        for raised in (RuntimeError("other"), MemoryError()):
            with self.subTest(raised=type(raised).__name__), \
                    mock.patch.object(kit, "_load_existing", side_effect=raised):
                with self.assertRaises(type(raised)):
                    self.report()
        with mock.patch.object(kit_timing.kit_track, "read_sent", side_effect=RecursionError("later")):
            with self.assertRaises(RecursionError):  # after name admission: not a names error
                self.report()

    def test_existing_precedence_and_specific_refusals_survive(self):
        self.write_names(agencies_text(deep_text()))
        for as_of, message in (("bad-date", "--as-of must be a real"), ("2099-01-01", "must not be in the future")):
            with self.subTest(as_of=as_of), self.assertRaisesRegex(ValueError, message):
                self.report(as_of=as_of)
        duplicate = [{"agency_id": A, "name": "Alpha"}] * 2
        self.write_names(json.dumps(duplicate))
        with self.assertRaisesRegex(ValueError, "duplicate agency_id"):
            self.report()
        self.names(**{A: "Alpha"})
        self.sent((A, "main", base.entry(A, "2026-03-01", [base.step("2026-03-20", "closed")])))
        with self.assertRaisesRegex(ValueError, "^" + base.MALFORMED + "$"):
            self.report(as_of="2026-02-28")
        path = self.root / "kit" / "agencies.json"
        path.unlink()
        path.mkdir()
        with self.assertRaisesRegex(ValueError, "not a regular file"):
            self.report()
        path.rmdir()
        path.symlink_to(self.private / "elsewhere.json")
        with self.assertRaisesRegex(ValueError, "not a regular file"):
            self.report()

    def test_admitted_nested_metadata_preserves_report_and_text(self):
        self.sent((A, "main", base.entry(A, "2026-03-01", [base.step("2026-03-02", "acknowledged")])),
                  (A, "named", base.entry(A, "2026-03-01", [base.step("2026-03-01", "produced")], "named")),
                  (B, "main", base.entry(B, "2026-03-01")))
        rows = [{"agency_id": B, "name": "Málaga, Police — East", "selected": "legacy", "kind": 7},
                {"agency_id": A, "name": "Málaga, Police — East", "selected": False}]
        self.write_names(json.dumps(rows))
        plain = self.report()
        plain_text = kit_timing.format_report(plain)
        nested = [dict(r, extra=json.loads(deep_text("array", 200)), more=json.loads(deep_text("object", 200)))
                  for r in rows]
        self.write_names(json.dumps(nested))
        before = self.snapshot()
        self.assertEqual(self.report(), plain)
        self.assertEqual(kit_timing.format_report(self.report()), plain_text)
        self.assertEqual(self.snapshot(), before)
        code, _, err = run("kit-timing", "--directory", self.root, "--as-of", "2026-04-30")
        self.assertEqual((code, err), (0, "Read-only. Nothing was sent.\n"))


if __name__ == "__main__":
    unittest.main()
