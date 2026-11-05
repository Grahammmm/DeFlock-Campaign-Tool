"""Offline structure checks for docs/RECORDS-PRIMER.md against the us-ca law package request scopes."""
import json
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from tests.test_review_guide_doc import check_links  # noqa: E402

PRIMER = REPO_ROOT / "docs" / "RECORDS-PRIMER.md"
README = REPO_ROOT / "README.md"
DISCOVERY = REPO_ROOT / "docs" / "AGENCY-DISCOVERY.md"
PACKAGE = REPO_ROOT / "jurisdictions" / "us-ca" / "package.json"
H3S = ["What these records are", "Where to ask", "What a production may look like", "What to check",
       "Where practice varies"]
FENCE_OPEN = re.compile(r"^\s*(`{3,}|~{3,})")


def scopes():
    return json.loads(PACKAGE.read_text(encoding="utf-8"))["request_scopes"]


def outside_fences(text):
    """Lines of ``text`` (CRLF tolerated) that are not inside a backtick or tilde fence."""
    lines, fence = [], None
    for line in text.replace("\r\n", "\n").split("\n"):
        match = FENCE_OPEN.match(line)
        if fence is None and match:
            fence = match.group(1)[0] * len(match.group(1))
        elif fence is not None and re.match(r"^\s*" + re.escape(fence[0]) + "{%d,}\\s*$" % len(fence), line):
            fence = None
        elif fence is None:
            lines.append(line)
    return lines


def parse_sections(text):
    """(scope_id, title, body) per H2 outside fences, in order; a malformed H2 raises ValueError."""
    found, current = [], None
    for line in outside_fences(text):
        if line.startswith("## "):
            head = line[3:]
            if " — " not in head:
                raise ValueError(f"malformed H2 (no ' — '): {line!r}")
            ident, title = head.split(" — ", 1)
            current = [ident.strip(), title.strip(), []]
            found.append(current)
        elif current is not None:
            current[2].append(line)
    return [(i, t, "\n".join(b)) for i, t, b in found]


def scope_problems(text, package_scopes):
    try:
        found = parse_sections(text)
    except ValueError as error:
        return [str(error)]
    problems, expected = [], [(s["scope_id"], s["title"]) for s in package_scopes]
    ids = [i for i, _, _ in found]
    for scope_id, title in expected:
        count = ids.count(scope_id)
        if count != 1:
            problems.append(f"{scope_id}: expected exactly one section, found {count}")
        elif (scope_id, title) not in [(i, t) for i, t, _ in found]:
            problems.append(f"{scope_id}: title does not match the package ({title!r})")
    known = {scope_id for scope_id, _ in expected}
    problems += [f"{i}: unknown scope section" for i in ids if i not in known]
    if not problems and ids != [i for i, _ in expected]:
        problems.append("sections are not in package order")
    return problems


def detail_problems(text, package_scopes):
    problems = []
    by_id = {i: body for i, _, body in parse_sections(text)}
    for scope in package_scopes:
        body = by_id.get(scope["scope_id"])
        if body is None:
            problems.append(f"{scope['scope_id']}: no section")
            continue
        parts = re.split(r"^### (.*)$", body, flags=re.M)
        headings, bodies = parts[1::2], parts[2::2]
        if headings != H3S:
            problems.append(f"{scope['scope_id']}: subsections are {headings}")
            continue
        problems += [f"{scope['scope_id']}: empty '{h}'" for h, b in zip(headings, bodies) if not b.strip()]
        cited = [r for r in scope["rule_ids"] if f"`{r}`](../jurisdictions/us-ca/package.json)" in body]
        if not cited:
            problems.append(f"{scope['scope_id']}: no associated rule cited with the package link")
    return problems


def caveat_problems(text):
    lowered = text.lower()
    return [m for m in ("this is not legal advice", "status: draft", "not been independently legally reviewed")
            if m not in lowered]


def read(path):
    return Path(path).read_text(encoding="utf-8")


class RecordsPrimerTests(unittest.TestCase):
    def test_source_scopes_have_exactly_one_matching_section(self):
        package_scopes = scopes()
        self.assertTrue(package_scopes)
        found = parse_sections(read(PRIMER))
        self.assertEqual([(s["scope_id"], s["title"]) for s in package_scopes], [(i, t) for i, t, _ in found])
        self.assertEqual([], scope_problems(read(PRIMER), package_scopes))

    def test_scope_validator_rejects_missing_duplicate_unknown_and_stale_titles(self):
        package_scopes = scopes()
        text = read(PRIMER)
        sections = [f"## {i} — {t}\n" for i, t, _ in parse_sections(text)]
        first, second = sections[0], sections[1]
        cases = {
            "missing": text.replace(first, ""),
            "duplicate": text + "\n" + first,
            "unknown": text.replace(first, "## bogus — Something else\n"),
            "stale title": text.replace(first, first.replace(" — ", " — Old ")),
            "malformed": text.replace(first, "## agreements ALPR vendor agreements\n"),
            "prose only": "The scopes are " + ", ".join(s["scope_id"] for s in package_scopes) + ".\n",
            "empty": "",
        }
        wanted = {"missing": "agreements", "duplicate": "agreements", "unknown": "bogus",
                  "stale title": "agreements", "malformed": "malformed H2", "prose only": "agreements"}
        for name, mutated in cases.items():
            with self.subTest(name):
                problems = scope_problems(mutated, package_scopes)
                self.assertTrue(problems, name)
                if name in wanted:
                    self.assertTrue(any(wanted[name] in p for p in problems), problems)
        self.assertNotEqual(first, second)

    def test_scope_expectations_follow_package_changes(self):
        package_scopes = scopes() + [{"scope_id": "synthetic", "title": "Synthetic scope", "items": [], "rule_ids": []}]
        text = read(PRIMER)
        self.assertTrue(any("synthetic" in p for p in scope_problems(text, package_scopes)))
        self.assertEqual([], scope_problems(text + "\n## synthetic — Synthetic scope\n", package_scopes))

    def test_fenced_headings_do_not_count(self):
        package_scopes = scopes()
        text = read(PRIMER)
        self.assertEqual([], scope_problems(text.replace("\n", "\r\n"), package_scopes))
        for fence in ("```", "~~~", "````"):
            with self.subTest(fence):
                extra = f"\n{fence}\n## fake — Not a scope\n{fence}\n"
                self.assertEqual([], scope_problems(text + extra, package_scopes))
                heading = f"## {package_scopes[0]['scope_id']} — {package_scopes[0]['title']}\n"
                hidden = text.replace(heading, f"{fence}\n{heading}{fence}\n", 1)
                self.assertTrue(scope_problems(hidden, package_scopes))

    def test_each_scope_has_substantive_checks_variation_and_package_rule(self):
        package_scopes = scopes()
        text = read(PRIMER)
        self.assertEqual([], detail_problems(text, package_scopes))
        first = package_scopes[0]
        blank = re.sub(r"(### What to check\n).*?(?=### Where practice varies)", r"\1\n", text, count=1, flags=re.S)
        self.assertTrue(any("empty 'What to check'" in p for p in detail_problems(blank, package_scopes)))
        for rule in first["rule_ids"]:
            text = text.replace(f"`{rule}`](../jurisdictions/us-ca/package.json)", f"`{rule}`](x)")
        uncited = detail_problems(text, package_scopes)
        self.assertTrue(any(first["scope_id"] in p and "no associated rule" in p for p in uncited), uncited)
        renamed = read(PRIMER).replace("### Where to ask", "### Where", 1)
        self.assertTrue(detail_problems(renamed, package_scopes))

    def test_disclaimer_and_draft_status_caveat(self):
        text = read(PRIMER)
        self.assertEqual([], caveat_problems(text))
        self.assertIn("**This is not legal advice.**", text)
        self.assertTrue(caveat_problems(text.replace("This is not legal advice", "This is advice")))
        self.assertTrue(caveat_problems(text.replace("status: draft", "status: reviewed")))
        self.assertEqual("draft", json.loads(PACKAGE.read_text(encoding="utf-8"))["status"])

    def test_primer_and_entry_point_links_resolve(self):
        for path in (PRIMER, README, DISCOVERY):
            with self.subTest(path.name):
                # inline code spans such as `[text](url)` are prose examples, not links
                self.assertEqual([], check_links(path, re.sub(r"`[^`\n]*`", "", read(path))))
        self.assertEqual(1, read(README).count("(docs/RECORDS-PRIMER.md)"))
        self.assertEqual(1, read(DISCOVERY).count("(RECORDS-PRIMER.md)"))
        self.assertIn("(AGENCY-DISCOVERY.md)", read(PRIMER))
        self.assertTrue(check_links(PRIMER, "[x](NO-SUCH-FILE.md)"))
        self.assertTrue(check_links(PRIMER, "[x](AGENCY-DISCOVERY.md#no-such-heading)"))


if __name__ == "__main__":
    unittest.main()
