"""Structure checks for docs/ORDINANCE-CHECKLIST.md, offline.

These tests certify document shape, link syntax and a pinned set of official code hosts the
author checked by hand. They do not certify that any ordinance says what a row claims, that a
link is live, or that a provision is still in force.
"""
import datetime
import posixpath
import re
import socket
import unittest
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parents[1]
DOC = REPO / "docs" / "ORDINANCE-CHECKLIST.md"
HEADING = "## Elements and enacted examples"
COLUMNS = ["Element", "Question", "Example", "Citation", "Official code page", "Checked"]
# Hosts the author read by hand: Berkeley's city code host, Davis's eCode360 code, and the
# American Legal Publishing code library that sf.gov names as San Francisco's official publisher.
OFFICIAL_HOSTS = {"berkeley.municipal.codes", "ecode360.com", "codelibrary.amlegal.com"}
DISCLAIMERS = ("draft reading aid", "not legal advice", "not a law package")


def table_rows(text):
    match = re.search(r"^" + re.escape(HEADING) + r"\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    if match is None:
        return None
    lines = [ln.strip() for ln in match.group(1).splitlines() if ln.strip().startswith("|")]
    return [[c.strip() for c in ln.strip("|").split("|")] for ln in lines[2:]]


def check_document(text):
    problems = []
    opening = text[:1500].lower()
    for phrase in DISCLAIMERS:
        if phrase not in opening:
            problems.append("opening lacks the phrase: " + phrase)
    rows = table_rows(text)
    if rows is None:
        return problems + ["no '%s' table" % HEADING]
    if not 8 <= len(rows) <= 14:
        problems.append("table has %d rows; need 8 to 14" % len(rows))
    seen = set()
    for row in rows:
        name = row[0] if row else "(empty row)"
        if len(row) != len(COLUMNS):
            problems.append("row '%s': %d cells, need %d" % (name, len(row), len(COLUMNS)))
            continue
        element, question, example, citation, page, checked = row
        if not element or element.lower() in seen:
            problems.append("row '%s': element is empty or duplicated" % name)
        seen.add(element.lower())
        if not question.endswith("?") or len(question) < 15:
            problems.append("row '%s': question must be a sentence ending in ?" % name)
        if len(example) < 40:
            problems.append("row '%s': attributed enacted example is missing" % name)
        if not re.search(r"\d+\.\d+|\b\d+[A-Z]?\.\d+", citation):
            problems.append("row '%s': citation has no section locator" % name)
        links = re.findall(r"\]\(([^)\s]*)\)", page)
        if len(links) != 1:
            problems.append("row '%s': needs exactly one code-page link" % name)
        else:
            parsed = urlparse(links[0])
            if parsed.scheme != "https":
                problems.append("row '%s': code-page link must be https" % name)
            elif parsed.hostname not in OFFICIAL_HOSTS:
                problems.append("row '%s': host %r is not a pinned official code host" % (name, parsed.hostname))
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", checked):
                raise ValueError(checked)
            datetime.date.fromisoformat(checked)
        except ValueError:
            problems.append("row '%s': checked date %r is not a real YYYY-MM-DD date" % (name, checked))
    return problems


def local_link_problems(text, doc="docs/ORDINANCE-CHECKLIST.md"):
    problems = []
    for target in re.findall(r"\]\(([^)\s]+)\)", text):
        if re.match(r"[a-z]+:", target):
            continue
        path, _, fragment = target.partition("#")
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(doc), path))
        file = REPO / resolved
        if not file.is_file():
            problems.append("link does not resolve: " + target)
        elif fragment:
            slugs = {re.sub(r"[^a-z0-9 -]", "", h.lower()).strip().replace(" ", "-")
                     for h in re.findall(r"^#+\s+(.+)$", file.read_text(encoding="utf-8"), re.M)}
            if fragment not in slugs:
                problems.append("fragment does not resolve: " + target)
    return problems


class OrdinanceChecklistTests(unittest.TestCase):
    def setUp(self):
        def refuse(*args, **kwargs):
            raise AssertionError("network access attempted")
        for owner, name in ((socket.socket, "connect"), (urllib.request, "urlopen")):
            original = getattr(owner, name)
            setattr(owner, name, refuse)
            self.addCleanup(setattr, owner, name, original)
        self.text = DOC.read_text(encoding="utf-8")

    def test_actual_document_passes(self):
        self.assertEqual(check_document(self.text), [])
        self.assertEqual(local_link_problems(self.text), [])
        self.assertIn("(LAW-PACKAGES.md", self.text)
        self.assertIn("(REVIEW-GUIDE.md)", self.text)

    def mutated(self, old, new, count=1):
        self.assertIn(old, self.text)
        return check_document(self.text.replace(old, new, count))

    def test_row_without_link_or_locator_fails_naming_the_row(self):
        link = "[berkeley.municipal.codes](https://berkeley.municipal.codes/BMC/2.99.030)"
        problems = self.mutated(link, "berkeley.municipal.codes")
        self.assertEqual(problems, ["row 'Approval before acquisition': needs exactly one code-page link"])
        problems = self.mutated("Berkeley Municipal Code (BMC) 2.99.030(1)", "Berkeley Municipal Code")
        self.assertEqual(problems, ["row 'Approval before acquisition': citation has no section locator"])

    def test_http_empty_and_unpinned_hosts_fail(self):
        base = "https://berkeley.municipal.codes/BMC/2.99.030"
        self.assertEqual(len(self.mutated(base, base.replace("https", "http"))), 1)
        self.assertEqual(len(self.mutated("(" + base + ")", "()")), 1)
        news = self.mutated(base, "https://www.example-news.com/story")
        self.assertEqual(len(news), 1)
        self.assertIn("not a pinned official code host", news[0])

    def test_bad_dates_fail(self):
        self.assertEqual(len(self.mutated("2026-11-04", "2026-02-30")), 1)
        self.assertEqual(len(self.mutated("2026-11-04", "11/04/2026")), 1)

    def test_missing_disclaimers_fail(self):
        for phrase in DISCLAIMERS:
            problems = check_document(self.text.replace(phrase, "text", 1).replace(phrase.capitalize(), "Text", 1))
            self.assertEqual(problems, ["opening lacks the phrase: " + phrase], phrase)

    def test_row_count_must_be_8_to_14_and_distinct(self):
        rows = [ln for ln in self.text.splitlines() if ln.startswith("| ") and "---" not in ln][1:]
        self.assertTrue(8 <= len(rows) <= 14)
        padded = self.text.replace(rows[0], rows[0] + "\n" + rows[0])
        self.assertIn("row 'Approval before acquisition': element is empty or duplicated", check_document(padded))
        short = self.text
        for row in rows[: len(rows) - 7]:
            short = short.replace(row + "\n", "")
        self.assertEqual(check_document(short), ["table has 7 rows; need 8 to 14"])
        many = self.text
        for index in range(15 - len(rows)):
            many = many.replace(rows[0], rows[0] + "\n" + rows[0].replace("Approval", "Extra%d" % index))
        self.assertEqual(check_document(many), ["table has 15 rows; need 8 to 14"])

    def test_question_must_end_with_question_mark(self):
        problems = self.mutated("or signing an outside agreement?", "or signing an outside agreement.")
        self.assertEqual(problems, ["row 'Approval before acquisition': question must be a sentence ending in ?"])

    def test_broken_local_link_fails(self):
        bad = self.text + "\n[x](NO-SUCH-GUIDE.md) [y](LAW-PACKAGES.md#no-such-heading)\n"
        self.assertEqual(len(local_link_problems(bad)), 2)


if __name__ == "__main__":
    unittest.main()
