"""RF1801-2: ``social --check --text`` escapes terminal-active draft names; API and JSON identities are unchanged
(synthetic fixtures only)."""
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from campaign_tool.social import build_social, check_social
from campaign_tool.social_check import format_text

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
SLUG = "cedar-policy-posted"
FOOTER = "Read-only. Nothing was posted.\n"

LEGACY_STEM = "legacy-\x1b[2J\n0 problem(s).\rspoof"
LEGACY_SHOWN = "legacy-\\x1b[2J\\x0a0 problem(s).\\x0dspoof"
OSC_STEM = "variant-\x1b]0;SYNTHETIC\x07\nRead-only. Nothing was posted."
OSC_SHOWN = "variant-\\x1b]0;SYNTHETIC\\x07\\x0aRead-only. Nothing was posted."
SUGGESTED_STEM = "sugg-\x07\r\x1b[31mred"
SUGGESTED_SHOWN = "sugg-\\x07\\x0d\\x1b[31mred"


def terminal_active(text):
    """Characters other than the report's own LF that a terminal could act on."""
    return [c for c in text if c != "\n" and not c.isprintable()]


class SafetyCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-social-text-safety-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(os.path.realpath(temporary.name)) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.social = self.root / "kit" / "social"

    def stale_file(self, folder, stem):
        directory = self.social / folder if folder else self.social
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{stem}.md").write_text("synthetic stale draft", encoding="utf-8")

    def cli(self, *extra):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "social", "--directory", str(self.root),
                               "--check", *extra], cwd=REPO_ROOT, capture_output=True, timeout=120)

    def tree(self):
        return {str(p): (p.read_bytes() if p.is_file() else b"", p.stat().st_mode, p.stat().st_mtime_ns)
                for p in sorted(self.root.rglob("*"))}


class RealFiles(SafetyCase):
    def test_real_legacy_stale_filename_cannot_clear_screen_or_forge_summary(self):
        build_social(self.root)
        self.stale_file("", LEGACY_STEM)
        report = check_social(self.root)
        self.assertEqual(report["stale"], [LEGACY_STEM])  # the API keeps the exact name
        self.assertEqual(report["problems"], 1)
        text = format_text(report)
        self.assertEqual(terminal_active(text), [])
        self.assertEqual(text, "Checked 1 published finding(s).\n"
                               f"kit/social:\n  {SLUG}: ok\n  stale {LEGACY_SHOWN}\n"
                               "1 problem(s).\n" + FOOTER)
        self.assertEqual([ln for ln in text.splitlines() if ln.startswith("Read-only")], [FOOTER.strip()])

    def test_real_variant_and_suggested_stale_names_cannot_emit_osc_or_bel(self):
        build_social(self.root, ["x"])
        (self.social / "x.suggested").mkdir()
        (self.social / "x.suggested" / f"{SLUG}.md").write_text("not current", encoding="utf-8")
        self.stale_file("x", OSC_STEM)
        self.stale_file("x.suggested", SUGGESTED_STEM)
        report = check_social(self.root)
        x, suggested = report["folders"]["kit/social/x"], report["folders"]["kit/social/x.suggested"]
        self.assertEqual(x["stale"], [OSC_STEM])
        self.assertEqual(suggested["stale"], [SUGGESTED_STEM])
        self.assertEqual(x["problems"], 1)
        self.assertEqual(suggested["problems"], 2)  # the outdated draft and the one stale file, once each
        text = format_text(report)
        self.assertEqual(terminal_active(text), [])
        self.assertIn(f"  stale {OSC_SHOWN}: ", text)
        self.assertIn(f"  stale {SUGGESTED_SHOWN}: ", text)
        self.assertIn(f"  {SLUG}: ok\n", text)  # the safe sibling stays listed
        self.assertEqual(text.count(f"stale {OSC_SHOWN}"), 1)
        self.assertEqual(text.splitlines()[-1], FOOTER.strip())
        self.assertEqual([ln for ln in text.splitlines() if ln == FOOTER.strip()], [FOOTER.strip()])
        self.assertEqual(text.count("\nkit/social/x:\n"), 1)

    def test_undecodable_filename_is_shown_as_its_surrogate_escape(self):
        build_social(self.root)
        raw = os.fsencode(self.social) + b"/bad-\xff.md"
        with open(raw, "wb") as handle:
            handle.write(b"synthetic")
        report = check_social(self.root)
        self.assertEqual(report["stale"], ["bad-\udcff"])
        self.assertIn("  stale bad-\\udcff\n", format_text(report))  # shown escaped, never as a raw surrogate

    def test_real_cli_text_safe_json_exact_and_exits_preserved(self):
        build_social(self.root)
        clean = self.cli("--text")
        self.assertEqual((clean.returncode, clean.stderr), (0, b""))
        self.assertEqual(self.cli().returncode, 0)
        self.stale_file("", LEGACY_STEM)
        build_social(self.root, ["x"])
        self.stale_file("x", OSC_STEM)
        text = self.cli("--text")
        self.assertEqual(text.returncode, 3)
        self.assertEqual(text.stderr, b"")
        shown = text.stdout.decode("utf-8")
        self.assertEqual(terminal_active(shown), [])
        self.assertIn(f"  stale {LEGACY_SHOWN}\n", shown)
        self.assertIn(f"  stale {OSC_SHOWN}: ", shown)
        self.assertEqual(shown.splitlines()[-2:], ["2 problem(s).", FOOTER.strip()])
        raw = self.cli()
        self.assertEqual(raw.returncode, 3)
        decoded = json.loads(raw.stdout.decode("utf-8"))
        self.assertEqual(decoded, json.loads(json.dumps(check_social(self.root), ensure_ascii=False)))
        self.assertEqual(decoded["stale"], [LEGACY_STEM])
        self.assertEqual(decoded["folders"]["kit/social/x"]["stale"], [OSC_STEM])

    def test_formatter_and_checker_do_not_mutate_reports_or_campaign(self):
        build_social(self.root)
        build_social(self.root, ["x"])
        self.stale_file("", LEGACY_STEM)
        self.stale_file("x", OSC_STEM)
        before = self.tree()
        report = check_social(self.root)
        original = copy.deepcopy(report)
        first = format_text(report)
        self.assertEqual(format_text(report), first)
        self.assertEqual(report, original)
        self.assertEqual(self.cli("--text").returncode, 3)
        self.assertEqual(self.cli().returncode, 3)
        self.assertEqual(self.tree(), before)


class Formatter(unittest.TestCase):
    @staticmethod
    def report(**changes):
        base = {"findings": 2, "drafts": [], "stale": [], "problems": 0, "posted": False}
        base.update(changes)
        return base

    def test_every_dynamic_formatter_fragment_is_escaped_once(self):
        report = self.report(
            drafts=[{"slug": "s\x1b1", "state": "ed\x07ited", "problems": ["p\r1", "p\\2"], "note": "n\n1"}],
            stale=["st\x1b"], problems=9,
            folders={"kit/social/h\x1b[2J": {
                "drafts": [{"slug": "f\ts", "state": "ok", "problems": [], "note": ""}],
                "stale": ["fs\x00", "plain"], "stale_reasons": {"fs\x00": ["x\x9b", "r\x07a", "r\nb"]},
                "folder_problems": ["linked\x1b folder", "back\\slash"], "problems": 3}})
        self.assertEqual(format_text(report),
                         "Checked 2 published finding(s).\n"
                         "kit/social:\n"
                         "  s\\x1b1: ed\\x07ited (p\\x0d1, p\\\\2) - n\\x0a1\n"
                         "  stale st\\x1b\n"
                         "kit/social/h\\x1b[2J:\n"
                         "  folder: linked\\x1b folder\n"
                         "  folder: back\\\\slash\n"
                         "  f\\x09s: ok\n"
                         "  stale fs\\x00: x\\x9b; r\\x07a; r\\x0ab\n"
                         "  stale plain\n"
                         "9 problem(s).\n" + FOOTER)

    def test_c0_del_c1_bidi_and_unicode_separators_have_literal_escapes(self):
        pieces = {chr(n): f"\\x{n:02x}" for n in [*range(0x20), 0x7F, *range(0x80, 0xA0)]}
        pieces.update({"\u2028": "\\u2028", "\u2029": "\\u2029", "\u202e": "\\u202e", "\u2066": "\\u2066",
                       "\udcff": "\\udcff", "\U000e0001": "\\U000e0001"})
        for char, shown in pieces.items():
            with self.subTest(char=shown):
                text = format_text(self.report(stale=[f"a{char}b"], problems=1))
                self.assertEqual(text, f"Checked 2 published finding(s).\nkit/social:\n  stale a{shown}b\n1 problem(s).\n" + FOOTER)
                self.assertEqual(terminal_active(text), [])

    def test_literal_backslash_sequences_remain_distinguishable(self):
        real_lf, real_esc = "a\nb", "a\x1bb"
        literal_lf, literal_esc = "a\\x0ab", "a\\x1bb"
        shown = [format_text(self.report(stale=[s], problems=1)).split("\n")[2] for s in (real_lf, real_esc, literal_lf, literal_esc)]
        self.assertEqual(shown, ["  stale a\\x0ab", "  stale a\\x1bb", "  stale a\\\\x0ab", "  stale a\\\\x1bb"])
        self.assertEqual(len(set(shown)), 4)

    def test_printable_unicode_and_ordinary_report_bytes_are_unchanged(self):
        report = self.report(
            drafts=[{"slug": SLUG, "state": "ok", "problems": [], "note": ""},
                    {"slug": "café-日本語-🚀", "state": "edited", "problems": ["too long", "bad link"], "note": "ünïcode note"}],
            stale=["résumé 日本"], problems=4,
            folders={"kit/social/x": {"drafts": [{"slug": SLUG, "state": "outdated", "problems": ["outdated"]}],
                                      "stale": ["old 日本"], "stale_reasons": {"old 日本": ["source unpublished"]},
                                      "folder_problems": ["not a folder"], "problems": 2}})
        self.assertEqual(format_text(report),
                         "Checked 2 published finding(s).\n"
                         "kit/social:\n"
                         f"  {SLUG}: ok\n"
                         "  café-日本語-🚀: edited (too long, bad link) - ünïcode note\n"
                         "  stale résumé 日本\n"
                         "kit/social/x:\n"
                         "  folder: not a folder\n"
                         f"  {SLUG}: outdated (outdated)\n"
                         "  stale old 日本: source unpublished\n"
                         "4 problem(s).\n" + FOOTER)
        self.assertEqual(format_text(self.report()),
                         "Checked 2 published finding(s).\nkit/social:\n0 problem(s).\n" + FOOTER)

    def test_repeated_control_input_is_one_linear_pass(self):
        stem = "\x1b[2J" * 5000
        text = format_text(self.report(stale=[stem], problems=1))
        self.assertEqual(text.split("\n")[2], "  stale " + "\\x1b[2J" * 5000)
        self.assertEqual(terminal_active(text), [])


if __name__ == "__main__":
    unittest.main()
