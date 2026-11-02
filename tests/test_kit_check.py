"""kit-check names each selected agency's unfilled placeholders and unverified recipient; kit-sent refuses them."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit, kit_check  # noqa: E402

FRESH = ("[official records custodian - verify on the agency's website]", "[explicit approved cap, e.g. $50]",
         "[date range, e.g. 2023-01-01 through today]", "[requester-approved contact details]")
TEMPLATE = ("[official records custodian]", "[date range]", "[narrow date range]", "[explicit approved cap]",
            "[requester-approved contact details]")


def ready_drafts(root, agency_ids=None):
    """Fixture: fill every bracket placeholder in kit/requests/*.md and verify those agencies (synthetic contact)."""
    for path in sorted((Path(root) / "kit" / "requests").glob("*.md")):
        if agency_ids is None or path.stem in agency_ids:
            path.write_text(kit_check.PLACEHOLDER.sub("synthetic value", path.read_text(encoding="utf-8")),
                            encoding="utf-8")
            kit.verify_agency(root, path.stem, source_url="https://example.org/records", checked_on="2026-01-02",
                              records_email="records@example.org")


def run(*arguments):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def tree(root):
    return {str(p.relative_to(root)): (p.is_symlink(), p.lstat().st_mtime_ns, p.lstat().st_size)
            for p in Path(root).rglob("*")}


class KitCheckTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-check-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        code, _, err = run("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                           "--state", "CA")
        self.assertEqual(code, 0, err)
        code, _, err = run("kit", "--directory", self.root)
        self.assertEqual(code, 0, err)
        self.requests = self.root / "kit" / "requests"
        self.ids = [p.stem for p in sorted(self.requests.glob("*.md"))]
        self.assertGreaterEqual(len(self.ids), 3)
        self.a, self.b = self.ids[:2]
        self.sent = self.root / "kit" / "sent.json"

    def check(self, expect):
        code, out, err = run("kit-check", "--directory", self.root, "--json")
        self.assertEqual(code, expect, err)
        return {row["agency_id"]: row for row in json.loads(out)["agencies"]}, json.loads(out)

    def text(self, expect):
        code, out, err = run("kit-check", "--directory", self.root)
        self.assertEqual(code, expect, err)
        self.assertIn("Read-only. Nothing was sent.", err)
        return out.splitlines()

    def test_fresh_kit_lists_every_placeholder_and_unverified_recipient(self):
        rows, report = self.check(3)
        self.assertEqual(sorted(rows), self.ids)
        self.assertEqual(report["problems"], len(self.ids))
        for agency_id, row in rows.items():
            lines = (self.requests / f"{agency_id}.md").read_text(encoding="utf-8").split("\n")
            found = {hit["placeholder"] for hit in row["placeholders"]}
            self.assertTrue(set(FRESH) <= found, found)
            for hit in row["placeholders"]:
                self.assertIn(hit["placeholder"], lines[hit["line"] - 1])
            self.assertEqual((row["draft"], row["ok"], row["recipient_verified"], row["fee_cap_missing"],
                              row["date_range_missing"]), (f"kit/requests/{agency_id}.md", False, False, True, True))
        lines = self.text(3)
        self.assertEqual(lines[-1], f"{len(self.ids)} of {len(self.ids)} drafts need attention")
        for agency_id in self.ids:
            for label in ("recipient not verified", "fee cap missing", "date range missing"):
                self.assertIn(f"{agency_id}: {label}", lines)
            self.assertTrue(any(line.startswith(f"{agency_id}: line ") and FRESH[1] in line for line in lines))

    def test_template_fallback_names_its_five_placeholders_with_line_numbers(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-") as empty, contextlib.redirect_stdout(io.StringIO()):
            kit.build_kit(self.root, law_base=Path(empty))
        rows, _ = self.check(3)
        row = rows[self.a]
        lines = (self.requests / f"{self.a}.md").read_text(encoding="utf-8").split("\n")
        for placeholder in TEMPLATE:
            [number] = [i for i, line in enumerate(lines, 1) if placeholder in line]
            self.assertIn({"line": number, "placeholder": placeholder}, row["placeholders"])
        self.assertTrue(row["fee_cap_missing"] and row["date_range_missing"])

    def test_filled_and_verified_is_ok_unverified_is_named(self):
        ready_drafts(self.root)
        rows, report = self.check(0)
        self.assertEqual(report["problems"], 0)
        self.assertTrue(all(r["ok"] and not r["placeholders"] and not r["fee_cap_missing"]
                            and not r["date_range_missing"] for r in rows.values()))
        lines = self.text(0)
        self.assertEqual(sorted(lines[:-1]), [f"{agency_id}: OK" for agency_id in self.ids])
        self.assertEqual(lines[-1], f"0 of {len(self.ids)} drafts need attention")
        agencies = self.root / "kit" / "agencies.json"
        document = json.loads(agencies.read_text())
        for entry in document:
            if entry["agency_id"] == self.b:
                entry["verified"] = "true"  # only the JSON boolean true counts
        agencies.write_text(json.dumps(document))
        rows, report = self.check(3)
        self.assertEqual((report["problems"], rows[self.b]["ok"], rows[self.b]["placeholders"]), (1, False, []))
        lines = self.text(3)
        self.assertIn(f"{self.b}: recipient not verified", lines)
        self.assertIn(f"{self.a}: OK", lines)
        self.assertEqual(lines[-1], f"1 of {len(self.ids)} drafts need attention")

    def test_markdown_link_is_not_a_placeholder_but_reworded_brackets_are(self):
        ready_drafts(self.root)
        draft = self.requests / f"{self.a}.md"
        draft.write_text(draft.read_text() + "\nPortal: [portal](https://example.org/x)\n")
        self.assertTrue(self.check(0)[0][self.a]["ok"])
        for extra, cap, dates in (("[TODO fee CAP]", True, False), ("[Date Range here]", False, True),
                                  ("[portal] (https://example.org/x)", False, False)):
            with self.subTest(extra=extra):
                draft.write_text(draft.read_text() + extra + "\n")
                row = self.check(3)[0][self.a]
                self.assertEqual(row["placeholders"][-1]["placeholder"], extra.split(" (")[0])
                self.assertEqual((row["fee_cap_missing"], row["date_range_missing"]), (cap, dates))
                draft.write_text(draft.read_text().replace(extra + "\n", ""))

    def test_missing_or_symlinked_draft_named_without_crash(self):
        ready_drafts(self.root)
        (self.requests / f"{self.a}.md").unlink()
        target = self.tmp / "elsewhere.md"
        target.write_text("clean text\n")
        (self.requests / f"{self.b}.md").unlink()
        (self.requests / f"{self.b}.md").symlink_to(target)
        rows, report = self.check(3)
        self.assertEqual(report["problems"], 2)
        for agency_id in (self.a, self.b):
            self.assertEqual({k: rows[agency_id][k] for k in ("draft", "ok", "error")},
                             {"draft": None, "ok": False, "error": "draft missing or not a regular file"})
        missing = {f"{agency_id}: draft missing or not a regular file" for agency_id in (self.a, self.b)}
        self.assertEqual(missing - set(self.text(3)), set())

    def test_only_selected_law_enforcement_agencies_and_caps(self):
        agencies = self.root / "kit" / "agencies.json"
        document = json.loads(agencies.read_text())
        for entry in document:
            if entry["agency_id"] == self.a:
                entry["selected"] = False
        agencies.write_text(json.dumps(document))
        rows, _ = self.check(3)
        self.assertEqual(sorted(rows), sorted(set(self.ids) - {self.a}))
        draft = self.requests / f"{self.b}.md"
        draft.write_text("[bad\x1b[31m]\n" + "".join(f"[gap {i}]\n" for i in range(60)))
        row = self.check(3)[0][self.b]
        self.assertEqual((len(row["placeholders"]), row["placeholder_count"]), (50, 61))
        lines = self.text(3)
        self.assertIn(f"{self.b}: 11 more placeholder(s) not shown", lines)
        self.assertIn(f"{self.b}: line 1 [bad?[31m]", lines)
        self.assertFalse(any("\x1b" in line for line in lines))
        agencies.unlink()
        code, _, err = run("kit-check", "--directory", self.root)
        self.assertEqual(code, 1)
        self.assertIn("run kit first", err)

    def test_kit_check_is_read_only(self):
        (self.requests / f"{self.a}.md").unlink()
        before = tree(self.root)
        self.text(3)
        self.check(3)
        self.assertEqual(tree(self.root), before)

    def test_kit_sent_refuses_a_draft_that_fails_and_records_a_clean_one(self):
        def send(agency, *extra):
            return run("kit-sent", "--directory", self.root, "--agency", agency, "--date", "2026-03-02",
                       "--channel", "email", *extra)
        code, _, err = send(self.a)
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("Stopped: "), err)
        self.assertIn(f"kit/requests/{self.a}.md still has", err)
        self.assertIn("run kit-check", err)
        self.assertFalse(os.path.lexists(self.sent))
        ready_drafts(self.root, {self.a})
        code, out, err = send(self.a)
        self.assertEqual(code, 0, err)
        self.assertIn("Recorded only. Nothing was sent.", out)
        before = self.sent.read_bytes()
        draft = self.requests / f"{self.b}.md"
        draft.write_text(kit_check.PLACEHOLDER.sub("synthetic value", draft.read_text()))
        code, _, err = send(self.b)  # filled but recipient not verified
        self.assertEqual(code, 1)
        self.assertIn(f"kit/requests/{self.b}.md still has 0 unfilled placeholder(s) or an unverified recipient", err)
        self.assertEqual(self.sent.read_bytes(), before)
        draft_a = self.requests / f"{self.a}.md"
        draft_a.write_text(draft_a.read_text() + "[explicit approved cap]\n")
        code, _, err = send(self.a, "--replace")  # a correction is checked too
        self.assertEqual(code, 1)
        self.assertIn("still has 1 unfilled placeholder(s)", err)
        self.assertEqual(self.sent.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
