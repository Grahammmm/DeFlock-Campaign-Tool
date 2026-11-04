"""Acceptance checks for the public threat model, including failing controls."""
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs/THREAT-MODEL.md"
HEADINGS = (
    "What this project is worth attacking for",
    "Adversary categories",
    "What the project deliberately does not defend against",
    "What a challenge actually looks like",
    "What this means for the trust items",
)
TRUST = {
    "df-pii-everywhere", "df-legal-claim-guard", "df-custody-report",
    "df-data-handling", "df-publish-gate", "df-secrets-ci", "df-threat-model",
}
ITEMS = TRUST | {"df-law-review"}
HASH_SENTENCE = "A hash establishes byte identity, not authenticity."


def section(text, title):
    match = re.search(r"(?ms)^## " + re.escape(title) + r"\n(.*?)(?=^## |\Z)", text)
    return match.group(1) if match else ""


def validate(text):
    problems = []
    if len(text.splitlines()) > 150:
        problems.append("line cap")
    for heading in HEADINGS:
        if not re.search(r"(?m)^## " + re.escape(heading) + r"$", text):
            problems.append("missing section: " + heading)
    rows = [line for line in section(text, HEADINGS[1]).splitlines()
            if line.startswith("| ") and not line.startswith(("| Category", "| ---"))]
    if len(rows) != 6:
        problems.append("six threat rows required")
    for row in rows:
        paths = re.findall(r"`([^`]+\.[a-zA-Z0-9]+)`", row)
        for path in paths:
            if not (ROOT / path).is_file():
                problems.append("missing path: " + path)
        gaps = re.findall(r"GAP\s+`(df-[a-z-]+)`", row)
        if "GAP" in row and (not gaps or any(gap not in ITEMS for gap in gaps)):
            problems.append("gap without sheet item")
        if not paths and not gaps:
            problems.append("row without file or gap")
    ranking = section(text, HEADINGS[4])
    if "(operator's reading, not a re-decision)" not in ranking:
        problems.append("ranking marker")
    if set(re.findall(r"\bdf-[a-z-]+", ranking)) != TRUST:
        problems.append("ranking items")
    if text.count(HASH_SENTENCE) != 1:
        problems.append("hash sentence count")
    if re.search(r"violat|unlawful|illegal|non-?complian|breach", text, re.I):
        problems.append("legal assertion language")
    data = json.loads((ROOT / "data/agencies/us-ca.json").read_text())
    names = [a["name"] for c in data["counties"] for a in c["agencies"]]
    if any(name.casefold() in text.casefold() for name in names):
        problems.append("named agency")
    # Mask repository paths and section headings before checking for names and locations.
    prose = re.sub(r"`[^`]*`|(?m:^#+ .*?$)", "", text)
    if re.search(r"\b[A-Z][a-z]+\s+[A-Z][a-z]+\b", prose):
        problems.append("possible personal name")
    if re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b|(?<![\w/])/(?:[\w.-]+/)+[\w.-]+", prose):
        problems.append("address or absolute path")
    if re.search(r"\b[a-z0-9-]+\.(?:com|org|net|gov|edu|local)\b", prose, re.I):
        problems.append("hostname")
    return problems


class ThreatModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = DOC.read_text(encoding="utf-8")

    def test_document(self):
        self.assertEqual([], validate(self.text))

    def test_overlong_document_fails(self):
        self.assertIn("line cap", validate(self.text + "\nextra\n" * 151))

    def test_each_missing_heading_is_named(self):
        for heading in HEADINGS:
            with self.subTest(heading=heading):
                self.assertIn("missing section: " + heading,
                              validate(self.text.replace("## " + heading, "## Removed")))

    def test_missing_and_nonexistent_row_citations_fail(self):
        text = self.text.replace("`docs/BACKUP.md` describes backup posture; GAP `df-data-handling`",
                                 "no citation")
        self.assertIn("row without file or gap", validate(text))
        text = self.text.replace("`docs/BACKUP.md`", "`docs/DOES-NOT-EXIST.md`")
        self.assertIn("missing path: docs/DOES-NOT-EXIST.md", validate(text))
        self.assertIn("gap without sheet item", validate(self.text.replace("GAP `df-secrets-ci`", "GAP")))

    def test_names_and_assertive_legal_words_fail(self):
        data = json.loads((ROOT / "data/agencies/us-ca.json").read_text())
        agency = data["counties"][0]["agencies"][0]["name"]
        self.assertIn("named agency", validate(self.text + "\n" + agency + " acted."))
        self.assertIn("possible personal name", validate(self.text + "\nJane Doe acted."))
        for word in ("violated", "unlawful", "illegal", "non-compliant", "breach"):
            with self.subTest(word=word):
                self.assertIn("legal assertion language", validate(self.text + "\n" + word))

    def test_sensitive_locations_fail(self):
        for value in ("192.0.2.10", "/home/example/private"):
            self.assertIn("address or absolute path", validate(self.text + "\n" + value))
        self.assertIn("hostname", validate(self.text + "\nexample.local"))

    def test_ranking_and_hash_controls(self):
        self.assertIn("ranking marker", validate(self.text.replace("(operator's reading, not a re-decision)", "")))
        self.assertIn("ranking items", validate(self.text + "\n`df-invented`"))
        self.assertIn("hash sentence count", validate(self.text + "\n" + HASH_SENTENCE))
        self.assertIn("hash sentence count", validate(self.text.replace(HASH_SENTENCE, "")))

    def test_privacy_scanner(self):
        result = subprocess.run([sys.executable, "-B", "tools/flock_privacy_scan.py",
                                 "docs/THREAT-MODEL.md"], cwd=ROOT,
                                capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("findings=0", result.stdout)

    def test_required_coverage(self):
        threats = section(self.text, HEADINGS[1]).lower()
        for phrase in ("discredit", "private contents", "lawful demand", "false or harmful",
                       "lost machine", "supply chain or credentials"):
            self.assertIn(phrase, threats)
        excluded = section(self.text, HEADINGS[2]).lower()
        for phrase in ("host access", "lawful compelled disclosure", "organizer who chooses"):
            self.assertIn(phrase, excluded)


if __name__ == "__main__":
    unittest.main()
