"""DF1622-12: docs/TROUBLESHOOTING.md quotes each refusal's opening exactly as campaign_tool/ prints it.

The guide is the only list: entries are parsed from it, never from a second list here. Every opening must start the
static message of a raise in the source file the entry names (found with the ast module, never by running code), so a
reworded message, or old text kept only in a comment, docstring or unrelated string, fails. Fix commands are parsed by
the real argument parser and never run.
"""
import argparse
import ast
import contextlib
import io
import re
import shlex
import sys
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import cli

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "TROUBLESHOOTING.md"
GROUPS = ["kit", "kit-verify-agency", "kit-check", "kit-sent", "kit-response", "sources add", "finding review",
          "finding publish", "build", "content-check", "meetings", "meetings-stage", "backup", "restore"]
FIELDS = ("Message opening", "Source", "Meaning", "Next")
SOURCE = re.compile(r"\[(campaign_tool/[a-z_]+\.py)\]\(\.\./\1\)")
FIELD = re.compile(r"(Message opening|Source|Meaning|Next):[ \t]*(.*)")
COMMAND = re.compile(r"`(python3 -m campaign_tool [^`]+)`")
KIT_CHECK = "campaign_tool/kit_check.py"
README_BLOCK = """```sh
git clone https://github.com/Grahammmm/DeFlock-Campaign-Tool.git
cd DeFlock-Campaign-Tool
python3 -m campaign_tool init --directory ../my-campaign --county "Your County" --state CA --name "Your Campaign"
python3 -m campaign_tool kit --directory ../my-campaign
python3 -m campaign_tool doctor --directory ../my-campaign
python3 -m campaign_tool doctor --directory ../my-campaign --checklist  # [ok]/[todo] rows; each todo names its next step and who takes it
python3 -m campaign_tool build --directory ../my-campaign
python3 -m http.server 8080 --bind 127.0.0.1 --directory ../my-campaign/public
```"""


class GuideError(Exception):
    pass


# --- guide parsing --------------------------------------------------------------------------------------------------

def parse_guide(text):
    """Entries (dicts with group, title, line, opening, source, meaning, next) of the guide; GuideError on a malformed one."""
    entries, group, current = [], None, None
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith("## "):
            group, current = line[3:].strip(), None
        elif line.startswith("### "):
            if group not in GROUPS:
                raise GuideError(f"line {number}: entry outside a command group: {line}")
            current = {"group": group, "title": line[4:].strip(), "line": number, "fields": {}}
            entries.append(current)
        else:
            found = FIELD.match(line)
            if not found:
                continue
            if current is None:
                raise GuideError(f"line {number}: {found.group(1)} outside an entry")
            if found.group(1) in current["fields"]:
                raise GuideError(f"line {number}: {found.group(1)} given twice in '{current['title']}'")
            current["fields"][found.group(1)] = (found.group(2).strip(), number)
    result = []
    for entry in entries:
        fields = entry["fields"]
        missing = [name for name in FIELDS if not fields.get(name, ("", 0))[0]]
        if missing:
            raise GuideError(f"line {entry['line']}: '{entry['title']}' lacks {', '.join(missing)}")
        span = re.fullmatch(r"(`+)(.+?)\1", fields["Message opening"][0])
        if not span or span.group(2).startswith("`") or span.group(2).endswith("`"):
            raise GuideError(f"line {fields['Message opening'][1]}: opening is not exactly one code span")
        opening = span.group(2)
        if len(opening) < 12 or re.search(r"\.\.\.|…|<[^>]*>|\{[^}]*\}", opening) or opening != opening.strip():
            raise GuideError(f"line {fields['Message opening'][1]}: opening is a placeholder or too short: {opening!r}")
        source = SOURCE.fullmatch(fields["Source"][0])
        if not source:
            raise GuideError(f"line {fields['Source'][1]}: Source must be [campaign_tool/x.py](../campaign_tool/x.py)")
        result.append({"group": entry["group"], "title": entry["title"], "line": entry["line"], "opening": opening,
                       "source": source.group(1), "meaning": fields["Meaning"][0], "next": fields["Next"][0],
                       "next_line": fields["Next"][1]})
    if text.count("\nMessage opening:") + text.startswith("Message opening:") != len(result):
        raise GuideError("a 'Message opening:' marker was not parsed as an entry")
    return result


def check_inventory(entries):
    if not 30 <= len(entries) <= 50:
        raise GuideError(f"{len(entries)} entries; expected 30 to 50")
    openings = [e["opening"] for e in entries]
    if len(set(openings)) != len(openings):
        raise GuideError("duplicate opening")
    missing = [g for g in GROUPS if g not in {e["group"] for e in entries}]
    if missing:
        raise GuideError("no entry for " + ", ".join(missing))


# --- source matching (ast only, nothing is executed) -------------------------------------------------------------

def lead(node):
    """(static text at the start of a message expression, whether the whole expression is static)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, True
    if isinstance(node, ast.JoinedStr):
        text = ""
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                text += part.value
            else:
                return text, False
        return text, True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, whole = lead(node.left)
        if not whole:
            return left, False
        right, whole = lead(node.right)
        return left + right, whole
    return "", False


def raise_leads(tree):
    return [lead(node.exc.args[0])[0] for node in ast.walk(tree)
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and node.exc.args]


def _name_row(node):
    """True for the f-string shape f"{name}: <rest>"; returns rest or None."""
    if (isinstance(node, ast.JoinedStr) and len(node.values) == 2 and isinstance(node.values[0], ast.FormattedValue)
            and isinstance(node.values[0].value, ast.Name) and node.values[0].value.id == "name"
            and isinstance(node.values[1], ast.Constant) and isinstance(node.values[1].value, str)):
        return node.values[1].value
    return None


def kit_check_body(opening, tree):
    """kit-check prints rows, not raises: the module constant MISSING (shown as the row's error), or the fixed
    label text of format_text rows `<agency_id>: <label>`."""
    for node in tree.body:
        if (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "MISSING" for t in node.targets)
                and isinstance(node.value, ast.Constant) and node.value.value == opening):
            return any(isinstance(n, ast.Name) and n.id == "MISSING" and isinstance(n.ctx, ast.Load)
                       for n in ast.walk(tree))
    func = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "format_text"), None)
    if func is None:
        return False
    rows = [n for n in ast.walk(func)]
    if any((_name_row(n) or "").startswith(": " + opening) for n in rows):
        return True
    label_row = any(isinstance(n, ast.JoinedStr) and len(n.values) == 3 and isinstance(n.values[2], ast.FormattedValue)
                    and isinstance(n.values[2].value, ast.Name) and n.values[2].value.id == "label" for n in rows)
    return label_row and any(isinstance(n, ast.Tuple) and len(n.elts) == 2 and isinstance(n.elts[1], ast.Constant)
                             and n.elts[1].value == opening for n in rows)


def opening_in_source(opening, source_text, relative):
    tree = ast.parse(source_text)
    if relative == KIT_CHECK:
        return kit_check_body(opening, tree)
    return any(text.startswith(opening) for text in raise_leads(tree))


def read_source(relative):
    if not SOURCE.fullmatch(f"[{relative}](../{relative})"):
        raise GuideError(f"source outside campaign_tool/: {relative}")
    path = (ROOT / relative).resolve()
    if path.parent != (ROOT / "campaign_tool").resolve() or not path.is_file():
        raise GuideError(f"no such source: {relative}")
    return path.read_text(encoding="utf-8")


# --- parser-only command check ----------------------------------------------------------------------------------

def root_parser():
    captured = {}

    def capture(self, args=None, namespace=None):
        captured["parser"] = self
        raise SystemExit(0)

    with mock.patch.object(sys, "argv", ["campaign_tool"]), mock.patch.object(argparse.ArgumentParser, "parse_args", capture):
        with contextlib.suppress(SystemExit):
            cli.main()
    return captured["parser"]


def parse_only(command, parser):
    argv = shlex.split(command)
    if argv[:3] != ["python3", "-m", "campaign_tool"]:
        raise GuideError(f"not a campaign_tool command: {command}")
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        try:
            parser.parse_args(argv[3:])
        except SystemExit as exc:
            if exc.code not in (0, None):
                lines = err.getvalue().strip().splitlines()
                raise GuideError(f"{command}: {lines[-1] if lines else 'refused'}") from None


def check_commands(entries, parser):
    for entry in entries:
        commands = COMMAND.findall(entry["next"])
        if not commands:
            raise GuideError(f"line {entry['next_line']}: '{entry['title']}' has no complete command in Next")
        for command in commands:
            try:
                parse_only(command, parser)
            except GuideError as exc:
                raise GuideError(f"line {entry['next_line']} ({entry['title']}): {exc}") from None


def slug(heading):
    return re.sub(r"[^a-z0-9 -]", "", heading.lower()).strip().replace(" ", "-")


def check_links(text, base, own_name):
    for target in re.findall(r"\]\(([^)\s]+)\)", text):
        if re.match(r"[a-z]+:", target):
            continue
        path, _, anchor = target.partition("#")
        resolved = (base / path).resolve() if path else (base / own_name).resolve()
        if not resolved.is_file():
            raise GuideError(f"broken link: {target}")
        if anchor and resolved.suffix == ".md":
            headings = {slug(m.group(1)) for m in re.finditer(r"^#+ (.+)$", resolved.read_text(encoding="utf-8"), re.M)}
            if anchor not in headings:
                raise GuideError(f"broken anchor: {target}")


# --- tests ----------------------------------------------------------------------------------------------------------

class TroubleshootingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = GUIDE.read_text(encoding="utf-8")
        cls.entries = parse_guide(cls.text)
        cls.parser = root_parser()

    def test_entries_cover_all_commands_and_bound(self):
        check_inventory(self.entries)
        self.assertEqual([g for g in GROUPS if g in {e["group"] for e in self.entries}], GROUPS)
        for entry in self.entries:
            self.assertGreater(len(entry["meaning"]), 30, entry["title"])
        for label, bad in {
                "empty inventory": "",
                "duplicate": self.text.replace("### No county or city matches the location", self.text[
                    self.text.index("### The agency seed file is missing"):self.text.index("### No county or city matches the location")]
                    + "### No county or city matches the location", 1),
                "malformed span": self.text.replace("`No agency seed at`", "No agency seed at", 1),
                "unparsed marker": self.text + "\nMessage opening: `stray marker outside entries`\n"}.items():
            with self.assertRaises(GuideError, msg=label):
                check_inventory(parse_guide(bad))

    def test_each_opening_is_verbatim_in_its_emitter(self):
        for entry in self.entries:
            with self.subTest(entry["title"]):
                self.assertTrue(opening_in_source(entry["opening"], read_source(entry["source"]), entry["source"]),
                                f"line {entry['line']}: {entry['opening']!r} not at the start of a message in {entry['source']}")
        nested = [e for e in self.entries if "`" in e["opening"]]
        self.assertEqual([e["opening"] for e in nested], ["kit/meetings.json not found; run `meetings` first"])
        for source in ("../campaign_tool/cli.py", "tests/test_troubleshooting_doc.py", "campaign_tool/nothing_here.py",
                       "docs/TROUBLESHOOTING.md"):
            with self.assertRaises(GuideError):
                read_source(source)

    def test_reworded_opening_fails_even_if_old_text_remains_elsewhere(self):
        for entry in self.entries:
            changed = entry["opening"][:-1] + ("X" if entry["opening"][-1] != "X" else "Y")
            self.assertFalse(opening_in_source(changed, read_source(entry["source"]), entry["source"]), entry["title"])
        old = "No agency seed at"
        reworded = ('"""No agency seed at docstring."""\n# No agency seed at comment\nOTHER = "No agency seed at"\n'
                    'def f(x):\n    raise ValueError(f"Missing agency seed at {x}")\n')
        self.assertFalse(opening_in_source(old, reworded, "campaign_tool/discovery.py"))
        self.assertTrue(opening_in_source(old, reworded.replace("Missing", "No"), "campaign_tool/discovery.py"))
        self.assertFalse(opening_in_source(old, 'def f(x):\n    raise ValueError("Error: No agency seed at")\n', "campaign_tool/discovery.py"))
        self.assertFalse(opening_in_source(old, 'def f(x):\n    raise ValueError(f"{x} No agency seed at")\n', "campaign_tool/discovery.py"))
        self.assertFalse(opening_in_source(old, "x = 1\n", "campaign_tool/discovery.py"))

    def test_static_prefix_extraction(self):
        def leads(code):
            return raise_leads(ast.parse(code))
        self.assertEqual(leads('raise ValueError("plain message")'), ["plain message"])
        self.assertEqual(leads('raise ValueError("adjacent " "literals here")'), ["adjacent literals here"])
        self.assertEqual(leads('raise ValueError(f"lead text {x!r} tail")'), ["lead text "])
        self.assertEqual(leads('raise ValueError("static " + "then " + y)'), ["static then "])
        self.assertEqual(leads('raise ValueError(x + "later")'), [""])
        self.assertEqual(leads('raise ValueError(f"{x} first")'), [""])
        self.assertEqual(leads('raise ValueError(f"a {x}" "b")'), ["a "])
        self.assertEqual(leads("raise ValueError()"), [])
        # the four kit-check labels are checked as report text, and a wrong label fails
        real = read_source(KIT_CHECK)
        for opening in ("draft missing or not a regular file", "fee cap missing", "date range missing", "recipient not verified"):
            self.assertTrue(opening_in_source(opening, real, KIT_CHECK), opening)
            self.assertFalse(opening_in_source(opening + "!", real, KIT_CHECK), opening)
        for opening, old, new in (("fee cap missing", '"fee cap missing"', '"fee cap absent"'),
                                  ("date range missing", '"date range missing"', '"date range absent"'),
                                  ("recipient not verified", "{name}: recipient not verified", "{name}: recipient unverified"),
                                  ("draft missing or not a regular file", 'MISSING = "draft missing or not a regular file"',
                                   'MISSING = "draft is missing"')):
            self.assertIn(old, real)
            self.assertFalse(opening_in_source(opening, real.replace(old, new), KIT_CHECK), opening)

    def test_next_commands_parse_without_running(self):
        check_commands(self.entries, self.parser)
        groups_with_commands = {e["group"] for e in self.entries if COMMAND.search(e["next"])}
        self.assertEqual(groups_with_commands, set(GROUPS))
        base = {"title": "x", "next_line": 7}
        for label, command in {
                "nonexistent flag": "python3 -m campaign_tool kit-check --directory ../c --bogus-flag",
                "missing required": "python3 -m campaign_tool kit-sent --directory ../c --agency a",
                "wrong nested command": "python3 -m campaign_tool finding revue ../c/d.json --directory ../c",
                "portal vendor option": "python3 -m campaign_tool kit-verify-agency --portal-vendor x"}.items():
            with self.assertRaises(GuideError, msg=label) as caught:
                check_commands([dict(base, next=f"Run `{command}`.")], self.parser)
            self.assertIn("line 7", str(caught.exception))
        with self.assertRaises(GuideError):
            check_commands([dict(base, next="No command here.")], self.parser)

    def test_guide_links_disclaimer_and_readme_block(self):
        self.assertIn("**Not legal advice.**", self.text)
        check_links(self.text, GUIDE.parent, "TROUBLESHOOTING.md")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        install = (ROOT / "docs" / "INSTALLATION.md").read_text(encoding="utf-8")
        self.assertIn("](docs/TROUBLESHOOTING.md)", readme)
        self.assertIn("](TROUBLESHOOTING.md)", install)
        check_links(readme, ROOT, "README.md")
        check_links(install, ROOT / "docs", "INSTALLATION.md")
        start = readme.index("## Start locally")
        block = readme.index("```sh", start)
        self.assertEqual(readme[block:readme.index("```", block + 5) + 3], README_BLOCK)
        self.assertLess(readme.index("](docs/TROUBLESHOOTING.md)"), readme.index("## Preserve a document"))
        self.assertGreater(readme.index("](docs/TROUBLESHOOTING.md)"), block)
        for guide in ("REQUESTS-GUIDE.md", "FINDINGS-GUIDE.md", "REVIEW-GUIDE.md", "OUTREACH-GUIDE.md", "MEETINGS-GUIDE.md"):
            self.assertIn("docs/" + guide, readme)
        with self.assertRaises(GuideError):
            check_links("[x](NO-SUCH-FILE.md)", GUIDE.parent, "x.md")
        with self.assertRaises(GuideError):
            check_links("[x](BACKUP.md#no-such-heading)", GUIDE.parent, "x.md")
        with self.assertRaises(GuideError):
            parse_guide(self.text.replace("## backup\n", "## not a group\n", 1))


if __name__ == "__main__":
    unittest.main()
