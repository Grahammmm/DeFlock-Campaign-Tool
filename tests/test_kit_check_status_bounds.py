"""kit-check law-status extraction visits each physical line once (no overlapping continuation rescans)."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit_check  # noqa: E402

NORMAL, FALLBACK = "Law package status:", "NO REVIEWED LAW PACKAGE:"
FAMILIES = ((NORMAL, "normal"), (FALLBACK, "fallback"))


class CountingLines(list):
    """The real split lines; counts every element handed out by iteration, indexing or slicing."""
    count = 0

    def __iter__(self):
        for item in list.__iter__(self):
            self.count += 1
            yield item

    def __getitem__(self, key):
        result = list.__getitem__(self, key)
        self.count += len(result) if isinstance(key, slice) else 1
        return result


def status_of(data):
    return kit_check._summary(data)["status"]


def counted_summary(data):
    """(summary, element accesses) with the real ``_lines`` forwarded through a counting list."""
    seen = []
    real = kit_check._lines

    def wrapper(raw):
        lines = CountingLines(real(raw))
        seen.append(lines)
        return lines

    with mock.patch.object(kit_check, "_lines", wrapper):
        summary = kit_check._summary(data)
    return summary, sum(lines.count for lines in seen), len(seen[0])


def repeated(marker, count, tail=""):
    return (marker + "\n") * count + tail


def run(*arguments):
    out, err, code = io.StringIO(), io.StringIO(), 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def snapshot(root):
    return {str(p.relative_to(root)): (p.is_symlink(), p.lstat().st_mode, p.lstat().st_mtime_ns,
                                       None if p.is_symlink() or p.is_dir() else p.read_bytes())
            for p in sorted(Path(root).rglob("*"))}


class WorkBoundTests(unittest.TestCase):
    def check_family(self, marker):
        for count in (128, 256, 512):
            for tail in ("", "\n"):
                text = repeated(marker, count, tail)
                expected = [marker] * count
                summary, accessed, total = counted_summary(text.encode())
                self.assertEqual(summary["status"], expected, (count, tail))
                self.assertLessEqual(accessed, 40 * total + 40, (count, tail, accessed, total))
                self.assertEqual(status_of(text.encode()), expected)  # plain built-in data, no instrumentation

    def test_repeated_normal_status_lines_have_linear_work_and_storage(self):
        self.check_family(NORMAL)

    def test_repeated_fallback_status_lines_have_linear_work_and_storage(self):
        self.check_family(FALLBACK)
        text = (FALLBACK + " a.\n") * 300
        self.assertEqual(status_of(text.encode()), [FALLBACK + " a."] * 300)  # a period never ends fallback

    def test_large_admitted_fixture_is_linear(self):
        for marker in (NORMAL, FALLBACK):
            data = repeated(marker, 4096).encode()
            self.assertLess(len(data), kit_check.MAX_DRAFT_BYTES)
            summary, accessed, total = counted_summary(data)
            self.assertEqual(len(summary["status"]), 4096)
            self.assertLessEqual(accessed, 40 * total + 40)


class ExtractionLiteralTests(unittest.TestCase):
    def test_status_boundaries_and_disjoint_blocks_are_exact(self):
        cases = [
            (b"", []),
            (b"no marker\n## Heading\n", []),
            (NORMAL.encode(), [NORMAL]),
            (b"Law package status: done.\nnext", ["Law package status: done."]),
            (b"Law package status: done.  \nnext", ["Law package status: done.  "]),
            (b"Intro. Law package status: wrapped\nmore text.\nafter", ["Intro. Law package status: wrapped", "more text."]),
            (b"Law package status: a\n   \nlater", ["Law package status: a"]),
            (b"Law package status: a\nb\nc", ["Law package status: a", "b", "c"]),
            (b"Law package status: a.\n\nx\nNO REVIEWED LAW PACKAGE: n\nm.\n\nLaw package status: b\n\n\n",
             ["Law package status: a.", "NO REVIEWED LAW PACKAGE: n", "m.", "Law package status: b"]),
            (b"Law package status: a\r\nb.\r\n\r\nz", ["Law package status: a", "b."]),
            (b"Law package status: a\r\nb\r\n\r\nc\r\n", ["Law package status: a", "b"]),
            (b"NO REVIEWED LAW PACKAGE: x.\ny.\n\nz.", ["NO REVIEWED LAW PACKAGE: x.", "y."]),
        ]
        for data, expected in cases:
            self.assertEqual(status_of(data), expected, data)

    def test_nested_markers_are_consumed_once_without_text_deduplication(self):
        cases = [
            ("Law package status: a\nLaw package status: b\nc.\nLaw package status: d.\n",
             ["Law package status: a", "Law package status: b", "c.", "Law package status: d."]),
            ("Law package status: a\nNO REVIEWED LAW PACKAGE: b\nc.\ntail\n",
             ["Law package status: a", "NO REVIEWED LAW PACKAGE: b", "c."]),
            ("NO REVIEWED LAW PACKAGE: a.\nLaw package status: b.\nc.\n\nz",
             ["NO REVIEWED LAW PACKAGE: a.", "Law package status: b.", "c."]),
            ("NO REVIEWED LAW PACKAGE: a\nNO REVIEWED LAW PACKAGE: b\n\n",
             ["NO REVIEWED LAW PACKAGE: a", "NO REVIEWED LAW PACKAGE: b"]),
            ("Law package status: a.\n\nLaw package status: a.\n", ["Law package status: a."] * 2),
            ("NO REVIEWED LAW PACKAGE: Law package status: x.\ny\n", ["NO REVIEWED LAW PACKAGE: Law package status: x."]),
            ("NO REVIEWED LAW PACKAGE: Law package status: x\ny\n\nz", ["NO REVIEWED LAW PACKAGE: Law package status: x", "y"]),
            ("Law package status: caf\u00e9\n\u00f1 ok.\nafter", ["Law package status: caf\u00e9", "\u00f1 ok."]),
        ]
        for text, expected in cases:
            self.assertEqual(status_of(text.encode()), expected, text)
        self.assertEqual(status_of(b"Law package status: a\xff\nb.\nc"), ["Law package status: a\ufffd", "b."])

    def test_generated_status_and_other_summary_fields_preserve_literals(self):
        text = ("# Draft records request: Synthetic PD\n\nDRAFT ONLY. Nothing has been sent. Verify the recipient\n"
                "source and review. Law package status: reviewed.\n\nSubject: Records, 2023-01-01 through today\n\n"
                "## Policies\n\nThe date range for this request is 2023-01-01 through today.\n\n"
                "withholding. Please advise\nbefore incurring fees beyond $50.\n\n"
                "Law package status: pending\n## Inside heading\nPlease advise inside\nRules the scopes relate to:\n"
                "- r1: Cite A (review: ok)\n\n"
                "## Statutory context (for the requester, not for sending)\n\nRules the scopes relate to:\n"
                "- r2: Cite B (review: ok)\n- not a citation\n\nlast\n")
        self.assertEqual(kit_check._summary(text.encode()), {
            "scopes": ["## Policies", "## Inside heading"],
            "date": ["The date range for this request is 2023-01-01 through today."],
            "fee": ["withholding. Please advise", "before incurring fees beyond $50.", "Please advise inside"],
            "status": ["source and review. Law package status: reviewed.", "Law package status: pending",
                       "## Inside heading", "Please advise inside", "Rules the scopes relate to:",
                       "- r1: Cite A (review: ok)"],
            "citations": ["- r1: Cite A (review: ok)"]})
        fallback = ("NO REVIEWED LAW PACKAGE: none found\nnothing cited.\nthird line\n\nSubject: Records\n")
        self.assertEqual(status_of(fallback.encode()),
                         ["NO REVIEWED LAW PACKAGE: none found", "nothing cited.", "third line"])


class CliTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="synthetic-status-campaign-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "campaign"
        (self.root / "kit" / "requests").mkdir(parents=True)
        (self.root / "kit" / "requests.suggested").mkdir()
        self.set_agencies(["ca-a-police", "ca-b-police"])

    def set_agencies(self, ids):
        entries = [{"agency_id": i, "name": i, "kind": "police", "selected": True, "verified": True} for i in ids]
        (self.root / "kit" / "agencies.json").write_text(json.dumps(entries), encoding="utf-8")

    def write(self, agency_id, text, suggested=False):
        folder = self.root / "kit" / ("requests.suggested" if suggested else "requests")
        (folder / (agency_id + ".md")).write_text(text, encoding="utf-8")

    def test_repeated_status_real_cli_remains_complete_and_read_only(self):
        for marker, family in FAMILIES:
            primary = repeated(marker, 512, "\n")
            self.write("ca-a-police", primary)
            self.write("ca-a-police", primary + "Unstructured edit\n", suggested=True)
            self.write("ca-b-police", "# Clean\n")
            self.write("ca-b-police", "# Clean\n", suggested=True)
            before = snapshot(self.root)
            sizes = []
            real = kit_check._summary

            def recording(data):
                summary = real(data)
                sizes.append(len(summary["status"]))
                return summary

            with mock.patch.object(kit_check, "_summary", recording), \
                    mock.patch("subprocess.run", side_effect=AssertionError("no diff process")), \
                    mock.patch("subprocess.Popen", side_effect=AssertionError("no diff process")), \
                    mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used")):
                code, out, err = run("kit-check", "--directory", self.root, "--json")
                text_code, text_out, text_err = run("kit-check", "--directory", self.root)
            note = "Read-only. Nothing was sent.\n"
            self.assertEqual((code, text_code, err, text_err), (3, 3, note, note), family)
            self.assertEqual(sizes, [512] * 4, family)
            report = json.loads(out)
            self.assertEqual(report["problems"], 1)
            row = report["agencies"][0]
            self.assertEqual(row["ok"], False)
            self.assertEqual({k: v for k, v in row["suggested_differs"].items()}, {
                "path": "kit/requests.suggested/ca-a-police.md", "scopes_added": [], "scopes_removed": [],
                "date_range_differs": False, "fee_cap_differs": False, "law_package_status_differs": False,
                "rule_citations_added": [], "rule_citations_removed": [],
                "compare_command": "diff -u -- kit/requests/ca-a-police.md kit/requests.suggested/ca-a-police.md"})
            self.assertTrue(report["agencies"][1]["ok"])
            self.assertIn("ca-b-police: OK\n", text_out)
            self.assertIn("law package status line differs: no", text_out)
            self.assertTrue(text_out.endswith("1 of 2 drafts need attention\n"))
            self.assertNotIn("Unstructured edit", out + text_out)
            self.assertEqual(snapshot(self.root), before)
            # changing one consumed status line sets only the status flag; the agency is still counted once
            lines = primary.split("\n")
            lines[100] = marker + " changed"
            self.write("ca-a-police", "\n".join(lines), suggested=True)
            code, out, _ = run("kit-check", "--directory", self.root, "--json")
            report = json.loads(out)
            flags = {k: v for k, v in report["agencies"][0]["suggested_differs"].items() if v is True}
            self.assertEqual((code, report["problems"], flags), (3, 1, {"law_package_status_differs": True}), family)

    def test_identical_and_absent_suggestion_bypass_summary(self):
        malformed = repeated(NORMAL, 512, "\n")
        self.write("ca-a-police", malformed)
        self.write("ca-a-police", malformed, suggested=True)
        self.write("ca-b-police", malformed)
        with mock.patch.object(kit_check, "_summary", side_effect=AssertionError("summary used")):
            report = kit_check.check(self.root)
            self.assertEqual(report["problems"], 0)
            for row in report["agencies"]:
                self.assertNotIn("suggested_differs", row)
            entry = {"agency_id": "ca-a-police", "verified": True}
            with mock.patch.object(kit_check, "_read_suggestion", side_effect=AssertionError("suggestion read")):
                self.assertTrue(kit_check.agency_report(self.root, entry)["ok"])

    def test_existing_unsafe_and_byte_bounds_still_apply_before_summary(self):
        self.write("ca-a-police", repeated(NORMAL, 4, "\n"))
        suggestion = self.root / "kit" / "requests.suggested" / "ca-a-police.md"
        with mock.patch.object(kit_check, "_summary", side_effect=AssertionError("summary used")):
            suggestion.write_bytes(b"x" * (kit_check.MAX_DRAFT_BYTES + 1))
            row = kit_check.check(self.root)["agencies"][0]
            self.assertEqual(row["suggested_differs"]["error"], "suggestion exceeds 1 MiB")
            suggestion.unlink()
            suggestion.symlink_to(self.root / "kit" / "requests" / "ca-a-police.md")
            row = kit_check.check(self.root)["agencies"][0]
            self.assertEqual(row["suggested_differs"]["error"], kit_check.NOT_REGULAR)
            self.assertFalse(row["ok"])


if __name__ == "__main__":
    unittest.main()
