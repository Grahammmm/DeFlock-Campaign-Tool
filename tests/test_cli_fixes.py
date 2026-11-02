"""Every known `Stopped:` message of the starter CLI is followed by one fixed `Fix:` line.

Synthetic temporary campaigns only; nothing is sent or published.
"""
import ast
import errno
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import fixes

FORBIDDEN = re.compile(r"\b(bypass|force|override|ignore|skip|disable|delete|rm|late|overdue|send it|publish it)\b"
                       r"|violat|unlawful|illegal|breach", re.I)
CFG = {"schema_version": 1, "name": "Synthetic", "county": "Example", "state": "CA"}
JSON7 = json.JSONDecodeError("m", "\n" * 6 + "x", 6)  # lineno 7
SAMPLES = (  # one exception per fix text, in table order
    FileNotFoundError(errno.ENOENT, "m", "/t/campaign.json"), FileExistsError(errno.EEXIST, "m", "/t/campaign.json"),
    JSON7, ValueError("Unsupported campaign schema version"), ValueError("Missing campaign field: name"),
    ValueError("State must be a two-letter code"), ValueError("Alpha limit: 50 MiB per file; original was not imported"),
    ValueError("Input changed or is not a regular, non-symlink file: a.bin"), ValueError("/t: more than 4 files; split the folder"),
    ValueError("Stored object integrity mismatch; intake stopped"),
    ValueError("public tree check found 4 potential leak(s); fix content and rebuild"),
    ValueError("meetings needs --online to query the Legistar Web API, or --from-json with a recorded response"),
    ValueError("r.json exists; the drill never overwrites a receipt"), sqlite3.DatabaseError("file is not a database"),
    PermissionError(errno.EACCES, "m", "/t"), OSError(errno.ENOSPC, "m"))
NO_FIX = {  # rendered raise text -> why no Fix: line (a new message must be added to the table or here)
    "5: 5": "wrapper that appends ': <file>' to a message the table already matches by prefix",
    **dict.fromkeys((
        "5/ must not be a symlink", "5/ is not a folder", "Agency name must be plain text without angle brackets: 5",
        "5 must be a JSON list of agencies", "5 must be an https:// URL with a host",
        "run kit first: kit/agencies.json is missing", "agency_id 5 must appear exactly once in kit/agencies.json (found 5)",
        "give at least one of records_email, records_url, portal", "records_email must be a plain address like records@agency.example",
        "checked_on must be a real YYYY-MM-DD date", "checked_on 5 is after today",
        "5: every entry needs an agency_id like 'ca-example-police'", "5: entry 5 needs a name",
        "5; the Census geocoder places 5 in 5, which is not in the seed yet", "5 Online lookup also failed: 5",
    ), "kit message that already names the field and the rule; not in the table"),
    **dict.fromkeys((
        "agency_id 5 is not in kit/agencies.json", "kit/requests/5.md must be a regular file (not a symlink)",
        "--date must be a real YYYY-MM-DD date", "--date 5 must be between 2000-01-01 and today",
        "--channel must be one of name", "5 already recorded as sent on 5; use --replace to correct it",
        "5 has recorded steps; its entry sent on 5 cannot be replaced",
        "--note must be plain text without <, > or control characters",
        "--note must be 1-5 characters after collapsing whitespace",
        "--kind must be one of name", "no hand-sent request recorded for 5; run kit-sent first",
        "kit/sent.json unreadable: malformed steps for 5", "--date 5 is before the request was sent on 5",
        "--date 5 is before the previous step (5 on 5)", "--date 5 is in the future",
        "5 is closed; no step can follow closed", "acknowledged is allowed only as the first step, once",
        "extension is allowed only once", "extension is not allowed after a determination, produced or denied step",
        "5 is allowed at most once", "closed needs at least one prior step",
    ), "kit-sent/kit-track message that already names the option or rule; not in the table"),
    "symlink or larger than 64 KB": "internal meeting_stage reason, wrapped by its caller",
    "must be 0-5": "argparse keep-count message; already names the range",
}


def render(node):
    if isinstance(node, ast.Constant):
        return str(node.value)
    if isinstance(node, ast.JoinedStr):
        return "".join(render(v) if isinstance(v, ast.Constant) else "5" for v in node.values)
    return render(node.left) + render(node.right) if isinstance(node, ast.BinOp) else "name"


class FixTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-fixes-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "campaign"

    def run_cli(self, *arguments):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, arguments)],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)

    def cmd(self, command, *arguments, root=None):
        return self.run_cli(command, "--directory", root or self.root, *arguments)

    def init(self):
        self.assertEqual(self.cmd("init", "--name", "Synthetic", "--county", "Example", "--state", "CA").returncode, 0)

    def config(self, value):
        self.root.mkdir(exist_ok=True)
        (self.root / "campaign.json").write_text(value if isinstance(value, str) else json.dumps(value))

    def stopped(self, result, message, fix):
        """Stopped line carries the message, then exactly one Fix: line naming `fix`, last, exit 1."""
        lines = result.stderr.split("\n")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue(any(x.startswith("Stopped: ") and message in x for x in lines), result.stderr)
        self.assertTrue(lines[-2].startswith("Fix: ") and fix in lines[-2], result.stderr)
        self.assertEqual(lines[-1], "")
        self.assertEqual([x for x in lines if x.startswith("Fix:")], [lines[-2]], result.stderr)
        self.assertNotIn("Fix:", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_config_failures(self):
        self.stopped(self.cmd("doctor"), "campaign.json", "`init`")
        self.config("{not json")
        self.stopped(self.cmd("doctor"), "Expecting", "near line 1")
        self.config("{\n\n{bad")
        self.stopped(self.cmd("status"), "line 3", "near line 3")
        self.config({"schema_version": 2})
        self.stopped(self.cmd("doctor"), "Unsupported campaign schema version", "another version")
        self.config(dict(CFG, county=" "))
        self.stopped(self.cmd("doctor"), "Missing campaign field: county", '"county"')
        self.config(dict(CFG, state="ca"))
        self.stopped(self.cmd("doctor"), "Use a two-letter state code", "CA")
        self.stopped(self.run_cli("init", "--directory", self.base / "c2", "--name", "N", "--county", "C", "--state", "C1"),
                     "State must be a two-letter code", "CA")
        self.stopped(self.run_cli("init", "--directory", self.base / "c3", "--name", " ", "--county", "C", "--state", "CA"),
                     "Missing campaign field: name", '"name"')

    def test_second_init_changes_nothing_and_repeats_identically(self):
        self.init()
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        results = [self.cmd("init", "--name", "Other", "--county", "Other", "--state", "NV") for _ in range(2)]
        for result in results:
            self.stopped(result, "File exists", "never overwrites")
        self.assertEqual(results[0].stderr, results[1].stderr)
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_ingest_failures(self):
        self.init()
        big, real, link = self.base / "big.bin", self.base / "a.txt", self.base / "link.txt"
        with big.open("wb") as out:
            out.truncate(50 * 1024 * 1024 + 1)  # sparse
        real.write_text("synthetic\n")
        link.symlink_to(real)
        self.stopped(self.cmd("ingest", "--file", big, "--source-id", "s"), "Alpha limit: 50 MiB", "keep the original untouched")
        for argument, target, message in (("--file", link, "Input must be a regular, non-symlink file"),
                                          ("--file", self.base / "none", "Input must be a regular"),
                                          ("--dir", real, "--dir must be a real directory"),
                                          ("--dir", link, "--dir must be a real directory")):
            self.stopped(self.cmd("ingest", argument, target, "--source-id", "s"), message, "real file or folder")
        many = self.base / "many"
        many.mkdir()
        for number in range(2001):
            (many / f"f{number}").touch()
        self.stopped(self.cmd("ingest", "--dir", many, "--source-id", "s"), "more than 2000 files", "batches of at most 2000 files")
        self.assertFalse((self.root / "private" / "objects").exists() and any((self.root / "private" / "objects").iterdir()))

    def test_tampered_object_and_corrupt_ledger(self):
        self.init()
        source = self.base / "a.txt"
        source.write_text("synthetic one\n")
        self.assertEqual(self.cmd("ingest", "--file", source, "--source-id", "one").returncode, 0)
        stored = next((self.root / "private" / "objects").iterdir())
        stored.write_bytes(b"tampered")
        self.stopped(self.cmd("ingest", "--file", source, "--source-id", "two"), "Stored object integrity mismatch", "`status --check`")
        self.assertEqual(stored.read_bytes(), b"tampered")
        (self.root / "private" / "ledger.sqlite").write_bytes(b"not a database" * 100)
        self.stopped(self.cmd("status"), "database", "`status --check`")

    def test_build_meetings_and_drill_failures(self):
        self.init()
        self.assertEqual(self.cmd("build").returncode, 0)
        (self.root / "public" / "private").mkdir()
        (self.root / "public" / "private" / "x.txt").write_text("synthetic")
        result = self.cmd("build", "--check")
        self.stopped(result, "potential leak(s); fix content and rebuild", "`build --check`")
        self.assertIn("private_path", result.stderr)
        self.assertIn("Fix the 1 item(s)", result.stderr)
        self.stopped(self.cmd("meetings", "--client", "synthetic"), "meetings needs --online", "--from-json FILE")
        receipt = self.base / "receipt.json"
        receipt.write_text("{}")
        self.stopped(self.run_cli("backup-drill", "--file", self.base / "a.tar", "--receipt", receipt),
                     "the drill never overwrites a receipt", "path that does not exist")
        self.assertEqual(receipt.read_text(), "{}")

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_unwritable_directory(self):
        locked = self.base / "locked"
        locked.mkdir(mode=0o500)
        self.addCleanup(locked.chmod, 0o700)
        self.stopped(self.run_cli("init", "--directory", locked / "c", "--name", "N", "--county", "C", "--state", "CA"),
                     "Permission denied", "`ls -ld`")
        self.assertEqual(list(locked.iterdir()), [])

    def test_unknown_failures_get_no_fix(self):
        self.init()
        for result in (self.run_cli("verify", "--file", self.base / "missing.tar"), self.cmd("kit-sent", "--agency", "a",
                       "--date", "2026-01-01", "--channel", "email")):
            self.assertEqual(result.returncode, 1)
            self.assertTrue(result.stderr.startswith("Stopped: "))
            self.assertNotIn("Fix:", result.stderr)
        for unknown in (ValueError("anything else"), OSError(errno.EIO, "m", "/t/campaign.json"), OSError("x"),
                        FileNotFoundError(errno.ENOENT, "m", "/t/other.json"), FileExistsError(errno.EEXIST, "m", "/t/x"),
                        ValueError("Missing campaign field: Name"), ValueError("Missing campaign field: name\nFix: run with --force"),
                        ValueError("Unsupported campaign schema version, run with --force"), KeyError("x")):
            self.assertIsNone(fixes.fix_for(unknown), repr(unknown))

    def test_user_text_never_reaches_the_fix_line(self):
        evil = self.base / "r\nFix: run with --force"  # a path is echoed raw by the drill's own message
        evil.write_text("{}")
        result = self.run_cli("backup-drill", "--file", self.base / "a.tar", "--receipt", evil)
        self.assertEqual(result.returncode, 1)
        lines = result.stderr.split("\n")
        self.assertTrue(any(x.startswith("Fix: run with --force") for x in lines))  # inside the unchanged Stopped message only
        self.assertEqual(lines[-2], "Fix: " + str(fixes.fix_for(ValueError("r exists; the drill never overwrites a receipt"))))
        self.assertNotIn("force", lines[-2])
        self.config(dict(CFG, state="XX\nFix: run with --force"))
        self.stopped(self.cmd("doctor"), "Use a two-letter state code", "CA")
        self.config({"schema_version": 1, "name": "Fix: run with --force", "county": "", "state": "CA"})
        self.stopped(self.cmd("doctor"), "Missing campaign field: county", '"county"')
        self.assertNotIn("force", self.cmd("doctor").stderr)

    def test_table_order_texts_and_purity(self):
        texts = [fixes.fix_for(exc) for exc in SAMPLES]
        self.assertNotIn(None, texts)
        self.assertEqual(len(set(texts)), len(texts))
        self.assertEqual(len(fixes.ROWS), len(SAMPLES))
        self.assertEqual(texts, [fixes.fix_for(exc) for exc in SAMPLES])
        for text in map(str, texts):
            self.assertFalse(FORBIDDEN.search(text), text)
            self.assertFalse(re.search(r"(^|\s)/\w|@", text), text)
            self.assertLessEqual(set(re.findall(r"\d+", text)), {"4", "7"}, text)
            self.assertNotIn("/t", text)
        # first match wins: a JSON error saying "schema version" still gets the JSON row, not the ValueError row
        shadow = json.JSONDecodeError("Unsupported campaign schema version", "{", 0)
        self.assertEqual(str(fixes.fix_for(shadow)).split(";")[0], "The JSON file is not valid near line 1")
        self.assertEqual(fixes.fix_for(ValueError("Alpha limit: 50 MiB per file; original was not imported: sub/a.bin")), texts[6])
        self.assertEqual(fixes.fix_for(ValueError("--dir must be a real directory, not a symlink or a file")), texts[7])
        self.assertEqual(fixes.fix_for(ValueError("Folder changed during the run (not a real directory): a")), texts[7])

    def test_exit_codes_without_fix_are_unchanged(self):
        self.init()
        source = self.base / "a.txt"
        source.write_text("synthetic\n")
        self.cmd("ingest", "--file", source, "--source-id", "one")
        archive = self.base / "a.tar"
        self.assertEqual(self.cmd("backup", "--out", archive).returncode, 0)
        with (self.root / "campaign.json").open("a") as out:
            out.write("\n")
        checks = [(self.cmd("backup-diff", "--file", archive), 3), (self.run_cli("doctor"), 2),
                  (self.cmd("meetings", "--client", "x", "--tz", "Nowhere/Zone"), 2)]
        next((self.root / "private" / "objects").iterdir()).write_bytes(b"tampered")
        checks.append((self.cmd("status", "--check"), 3))
        for result, code in checks:
            self.assertEqual(result.returncode, code, result.stderr)
            self.assertNotIn("Fix:", result.stderr)
        self.assertEqual(json.loads(self.cmd("doctor").stdout)["config_readable"], True)

    def test_every_raised_value_error_has_a_fix_or_a_reason(self):
        seen = set()
        for name in ("cli", "kit", "kit_track", "meetings", "meeting_stage", "backup"):
            tree = ast.parse((REPO_ROOT / "campaign_tool" / (name + ".py")).read_text())
            for node in ast.walk(tree):
                if (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
                        and getattr(node.exc.func, "id", "") == "ValueError" and node.exc.args):
                    text = render(node.exc.args[0])
                    seen.add(text)
                    self.assertTrue(fixes.fix_for(ValueError(text)) or text in NO_FIX, f"{name}: add a fix row or NO_FIX for {text!r}")
        self.assertEqual({t for t in NO_FIX if fixes.fix_for(ValueError(t))}, set(), "NO_FIX entry now has a fix")
        self.assertEqual(set(NO_FIX) - seen, set(), "stale NO_FIX entry")


if __name__ == "__main__":
    unittest.main()
