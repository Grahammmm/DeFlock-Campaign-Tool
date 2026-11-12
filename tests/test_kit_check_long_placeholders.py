"""A long unfilled [placeholder] still blocks kit-check readiness, kit-sent recording and kit-copy; only display is clipped."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit, kit_check  # noqa: E402
from tests import test_kit_check as fixtures  # noqa: E402  (module alias: do not recollect its TestCase)

run, ready_drafts = fixtures.run, fixtures.ready_drafts
MAX = kit_check.MAX_DRAFT_BYTES


def span(length, fill="x", tail=""):
    """A bracket placeholder whose interior is exactly ``length`` characters (``tail`` ends it)."""
    return "[" + (fill * length)[:length - len(tail)] + tail + "]"


def snapshot(root):
    return {str(p.relative_to(root)): (p.is_symlink(), p.lstat().st_mode, p.lstat().st_mtime_ns,
                                       None if p.is_dir() else p.read_bytes()) for p in Path(root).rglob("*")}


class CountingText(str):
    """A str that counts the characters its ``find`` calls inspect (the scanner's only text access)."""
    work = 0

    def find(self, sub, start=0, end=None):
        end = len(self) if end is None else min(end, len(self))
        found = super().find(sub, start, end)
        type(self).work += (found + len(sub) if found >= 0 else end) - start
        return found


class LongPlaceholderTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-long-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        code, _, err = run("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                           "--state", "CA")
        self.assertEqual(code, 0, err)
        code, _, err = run("kit", "--directory", self.root)
        self.assertEqual(code, 0, err)
        self.requests = self.root / "kit" / "requests"
        self.ids = [p.stem for p in sorted(self.requests.glob("*.md"))]
        self.a, self.b = self.ids[:2]
        self.sent = self.root / "kit" / "sent.json"
        ready_drafts(self.root)
        self.draft = self.requests / f"{self.a}.md"
        self.clean = self.draft.read_text(encoding="utf-8")

    def put(self, extra, base=None, agency=None):
        path = self.requests / f"{agency or self.a}.md"
        path.write_text((self.clean if base is None else base) + extra, encoding="utf-8")
        return path

    def report(self):
        return kit_check.agency_report(self.root, {"agency_id": self.a, "verified": True})

    def cli(self, expect):
        code, out, err = run("kit-check", "--directory", self.root, "--json")
        self.assertEqual(code, expect, err)
        code, text, err = run("kit-check", "--directory", self.root)
        self.assertEqual(code, expect, err)
        self.assertIn("Read-only. Nothing was sent.", err)
        return {row["agency_id"]: row for row in json.loads(out)["agencies"]}, text.splitlines()

    def test_interior_159_160_161_and_large_spans_never_disappear(self):
        for length in (159, 160, 161, 162, 5000, 60000):
            with self.subTest(length=length):
                text = span(length)
                self.assertEqual(kit_check.draft_problems("ok\n" + text),
                                 [{"line": 2, "placeholder": text[:160]}])
                self.assertEqual(len(kit_check.draft_problems(text)[0]["placeholder"]), 160)
                self.put("\n" + text + "\n")
                row = self.report()
                self.assertEqual((row["ok"], row["placeholder_count"], len(row["placeholders"])), (False, 1, 1))
                self.assertEqual(row["placeholders"][0]["line"], self.clean.count("\n") + 2)
                self.assertLessEqual(len(row["placeholders"][0]["placeholder"]), 160)
                rows, lines = self.cli(3)
                self.assertEqual(rows[self.a]["placeholder_count"], 1)
                self.assertEqual(lines.count(f"{self.a}: line {row['placeholders'][0]['line']} " + text[:160]), 1)
        # multibyte interior: found while the draft is under the byte cap, refused as too large above it
        self.put("\n" + span(300000, "é") + "\n")
        row = self.report()
        self.assertEqual((row["ok"], row["placeholder_count"]), (False, 1))
        self.put("\n" + span(600000, "é") + "\n")
        self.assertEqual(self.report()["error"], "draft exceeds 1 MiB")

    def test_full_span_fee_and_date_flags_survive_display_clipping(self):
        cases = ((span(170, "y", " CAP"), True, False), (span(170, "y", " Date Range"), False, True),
                 (span(170, "y", " cap date RANGE"), True, True), (span(170, "y", " note"), False, False))
        for text, cap, dates in cases:
            with self.subTest(text=text[-16:]):
                self.put("\n" + text + "\n")
                row = self.report()
                self.assertEqual((row["fee_cap_missing"], row["date_range_missing"]), (cap, dates))
                self.assertEqual(row["placeholders"][-1]["placeholder"], text[:160])
                self.assertEqual(self.cli(3)[0][self.a]["fee_cap_missing"], cap)
        self.put("\nfee cap and date range outside\n" + span(200, "y") + "\n")  # keywords outside matches do not count
        row = self.report()
        self.assertEqual((row["placeholder_count"], row["fee_cap_missing"], row["date_range_missing"]), (1, False, False))

    def test_empty_unclosed_nested_adjacent_and_link_grammar_is_preserved(self):
        long_link, long_text = "[" + "z" * 300 + "](https://example.org/x)", "[" + "z" * 300 + "]"
        cases = (("[]", []), ("[[[[", []), ("]]] [x]", ["[x]"]), ("[a [b] c", ["[a [b]"]), ("[[]x]", ["[[]"]),
                 ("[a][b]", ["[a]", "[b]"]), ("[a](u)", []), ("[a](u) [b]", ["[b]"]), ("[a] (u)", ["[a]"]),
                 ("[x[y](u)]", []), ("[a\nb]", []), ("[][x]", ["[x]"]), ("[a] and [b", ["[a]"]),
                 (long_link, []), (long_link + " [c]", ["[c]"]), (long_text + " (u)", [long_text])
                 )
        for text, expected in cases:
            with self.subTest(text=text[:20]):
                got = [hit["placeholder"] for hit in kit_check.draft_problems(text)]
                self.assertEqual(got, [e[:160] for e in expected])
                if len(text) < 100:  # the substitution-compatible regex agrees on short input
                    self.assertEqual(got, [m for line in text.split("\n") for m in kit_check.PLACEHOLDER.findall(line)])
        self.assertEqual(kit_check.draft_problems("a\n\n[b] [c]\n[d"),
                         [{"line": 3, "placeholder": "[b]"}, {"line": 3, "placeholder": "[c]"}])
        self.assertEqual(kit_check.draft_problems("a\r\n[b]\r\n"), [{"line": 2, "placeholder": "[b]"}])
        self.put("\r\n[" + "q" * 400 + "]\r\n[r](http://e.org)\r\n", base=self.clean.replace("\n", "\r\n"))
        row = self.report()
        self.assertEqual([hit["line"] for hit in row["placeholders"]], [self.clean.count("\n") + 2])
        self.assertEqual(kit_check.PLACEHOLDER.sub("v", "[a] [" + "z" * 400 + "](u) [b]"), "v [" + "z" * 400 + "](u) v")

    def test_more_than_fifty_long_hits_keep_total_and_terminal_safety(self):
        lines = []
        for i in range(61):
            tail = " cap" if i == 60 else " date range" if i == 59 else ""
            interior = (f"gap {i}" + ("\x1b[31m" if i == 0 else "") + "." * 170)[:170] + tail
            lines.append("[" + interior + "]")
        self.put("\n" + "\n".join(lines) + "\n")
        row = self.report()
        self.assertEqual((len(row["placeholders"]), row["placeholder_count"]), (50, 61))
        self.assertEqual((row["fee_cap_missing"], row["date_range_missing"], row["ok"]), (True, True, False))
        self.assertTrue(all(len(hit["placeholder"]) <= 160 for hit in row["placeholders"]))
        self.assertEqual(set(row), {"agency_id", "recipient_verified", "draft", "placeholders", "placeholder_count",
                                    "fee_cap_missing", "date_range_missing", "ok"})
        rows, text = self.cli(3)
        self.assertIn(f"{self.a}: 11 more placeholder(s) not shown", text)
        self.assertFalse(any("\x1b" in line for line in text))
        self.assertTrue(any(line.endswith("gap 0?[31m" + "." * 149) for line in text))
        self.assertEqual(rows[self.a]["placeholder_count"], 61)

    def test_unclosed_and_link_dense_inputs_have_linear_scan_work(self):
        builders = {"unclosed": lambda n: "[" * n, "unclosed lines": lambda n: "[x\n" * (n // 3),
                    "links": lambda n: "[a](u)" * (n // 6), "hits": lambda n: "[ab]" * (n // 4),
                    "long hits": lambda n: ("[" + "w" * 996 + "]\n") * (n // 1000)}
        for name, build in builders.items():
            works = []
            for size in (24000, 48000):
                text = build(size)
                CountingText.work = 0
                got = kit_check.draft_problems(CountingText(text))
                works.append(CountingText.work)
                self.assertLessEqual(CountingText.work, 3 * len(text) + 16, (name, size))
                self.assertEqual(got, kit_check.draft_problems(str(text)), name)
            self.assertLessEqual(works[1], 2.2 * works[0] + 16, (name, works))
        for name, build in builders.items():  # actual files at the byte cap are checked, not skipped
            with self.subTest(name=name):
                self.put("\n" + build(MAX // 2))
                row = self.report()
                self.assertEqual(row["ok"], name not in ("hits", "long hits"), row.get("error"))
                self.assertLessEqual(len(row["placeholders"]), 50)

    def test_main_and_named_sent_requests_refuse_before_receipt_changes(self):
        def send(*extra):
            return run("kit-sent", "--directory", self.root, "--agency", self.a, "--date", "2026-03-02",
                       "--channel", "email", *extra)
        long_text = "\n" + span(161) + "\n"
        self.put(long_text)
        code, out, err = send()
        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"kit/requests/{self.a}.md still has 1 unfilled placeholder(s) or an unverified recipient", err)
        self.assertFalse(os.path.lexists(self.sent))
        named = self.requests / f"{self.a}--second.md"
        named.write_text(self.clean + long_text, encoding="utf-8")
        code, _, err = send("--request", "second")
        self.assertEqual(code, 1)
        self.assertIn(f"kit/requests/{self.a}--second.md still has 1 unfilled placeholder(s)", err)
        self.assertFalse(os.path.lexists(self.sent))
        self.put("")  # clean control records only
        code, out, err = send()
        self.assertEqual(code, 0, err)
        self.assertIn("Recorded only. Nothing was sent.", out)
        before, info = self.sent.read_bytes(), self.sent.stat()
        before_all = snapshot(self.root)
        self.put(long_text)
        for extra, name in (("--replace", self.a), ("--request", "second")):
            code, _, err = send(*([extra] if extra == "--replace" else [extra, name]))
            self.assertEqual(code, 1, err)
            self.assertIn("unfilled placeholder(s)", err)
        self.assertEqual((self.sent.read_bytes(), self.sent.stat().st_mode, self.sent.stat().st_mtime_ns),
                         (before, info.st_mode, info.st_mtime_ns))
        self.assertEqual({k: v for k, v in snapshot(self.root).items() if "requests/" not in k},
                         {k: v for k, v in before_all.items() if "requests/" not in k})
        self.assertEqual(kit_check.draft_problems(named.read_text(encoding="utf-8"))[0]["placeholder"], span(161)[:160])

    def test_copy_gates_reject_long_sendable_and_whole_mail_placeholders(self):
        out = self.root / "kit" / "outgoing"

        def copy(target, expect):
            code, text, err = run("kit-copy", "--directory", self.root, "--agency", self.a, "--for", target, "--json")
            self.assertEqual(code, expect, err)
            return err
        self.put("\n")
        body = self.clean.replace("Rolling production", span(200, "p") + " Rolling")
        self.assertNotEqual(body, self.clean)
        for target in ("portal", "email"):
            self.put("", base=body)
            before = snapshot(self.root)
            err = copy(target, 3)
            self.assertIn("1 unfilled placeholder(s) in the sendable part: " + span(200, "p")[:160], err)
            self.assertEqual(snapshot(self.root), before)
            self.assertFalse(out.exists())
        context = self.clean.replace("## Statutory context (for the requester, not for sending)\n",
                                     "## Statutory context (for the requester, not for sending)\n\n" + span(300, "c"))
        self.assertNotEqual(context, self.clean)
        self.put("", base=context)
        before = snapshot(self.root)
        err = copy("mail", 3)
        self.assertIn(f"kit-check fails for {self.a} (whole draft; unfilled: " + span(300, "c")[:160], err)
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse(out.exists())
        copy("portal", 0)  # an excluded section does not stop the sendable copy; the clean control still writes
        self.assertEqual(sorted(p.name for p in out.iterdir()), [f"{self.a}.txt"])
        self.assertFalse(self.sent.exists())

    def test_reader_cap_suggestions_and_clean_agencies_are_unchanged(self):
        padding = "x" * 99 + "\n"
        self.put("", base=padding * (MAX // 100) + "x" * (MAX % 100))
        self.assertEqual(len(self.draft.read_bytes()), MAX)
        self.assertEqual((self.report()["ok"], self.report()["placeholder_count"]), (True, 0))
        self.draft.write_bytes(self.draft.read_bytes() + b"x")
        self.assertEqual(self.report()["error"], "draft exceeds 1 MiB")
        self.put("")
        suggested = self.root / "kit" / "requests.suggested"
        suggested.mkdir(exist_ok=True)
        (suggested / f"{self.a}.md").write_text(self.clean + "## Extra scope\n", encoding="utf-8")
        self.assertTrue(self.report()["ok"] and "suggested_differs" not in self.report())  # default is suggestion-blind
        self.put(span(250) + "\n", base=self.clean + "\n")
        (suggested / f"{self.a}.md").unlink()  # a missing suggestion changes nothing
        before = snapshot(self.root)
        rows, text = self.cli(3)
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual((rows[self.a]["ok"], rows[self.a]["placeholder_count"]), (False, 1))
        self.assertTrue(rows[self.b]["ok"] and f"{self.b}: OK" in text)
        self.assertIn("1 of", text[-1])
        agencies = self.root / "kit" / "agencies.json"
        document = json.loads(agencies.read_text())
        for entry in document:
            if entry["agency_id"] == self.b:
                entry["verified"] = False
        agencies.write_text(json.dumps(document))
        rows, _ = self.cli(3)
        self.assertEqual((rows[self.b]["ok"], rows[self.b]["placeholder_count"], rows[self.b]["recipient_verified"]),
                         (False, 0, False))


if __name__ == "__main__":
    unittest.main()
