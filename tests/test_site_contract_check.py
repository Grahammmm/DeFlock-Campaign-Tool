"""DF2207-03: an organizer-verified profile.contract_check records, per term an ALPR vendor agreement may set, whether the
linked contract addresses it; the agency page shows it as an organizer's reading, never as a legal review or a finding.
Offline, synthetic agencies, example.org URLs only.
"""
import copy
import json
import os
import re
import shutil
import tempfile
import unittest
from datetime import date, timedelta
from html import escape, unescape
from pathlib import Path
from unittest import mock

from campaign_tool import content
from campaign_tool.content import ContentError, load_contract_checklist
from campaign_tool.content_check import content_problems
from campaign_tool.legal_claims import legal_claims
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 11, 1)  # fixed site build date
IDS = ["term_renewal", "price_changes", "data_ownership", "retention", "sharing_network", "vendor_data_use",
       "audit_search_logs", "termination_data_exit"]
LABELS = ["Term and automatic renewal", "Price changes", "Data ownership", "Retention",
          "Sharing and national-network access", "Vendor use of data", "Audit and search-log access",
          "Termination and data return or deletion"]
SOURCE = "https://example.org/cedar/contract.pdf?id=7&v=2"
CONTRACT = {"vendor": "Example Vendor Co (fictional)", "start": "2024-07-01", "end": "2027-06-30", "renewal_note": "",
            "source_url": SOURCE, "checked_on": "2026-10-30"}
HEADING = "What the contract says (organizer reading, not a legal review)"
FORBIDDEN = ("compl", "violat", "unlawful", "illegal", "fails to")
LABEL = "profile.contract_check"


def elements(**changes):
    value = {i: {"status": "not_found"} for i in reversed(IDS)}  # written in shuffled (reversed) order
    value.update(changes)
    return value


def check(**changes):
    value = {"checked_on": "2026-10-31", "contract_url": SOURCE, "elements": elements()}
    value.update(changes)
    return value


def refuse_network(*args, **kwargs):
    raise AssertionError("network access attempted")


class ContractCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-contract-check-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        self.path = self.root / "content" / "agencies.json"
        self.base = json.loads(self.path.read_text())
        self.police = next(a for a in self.base if a["agency_id"] == "ca-cedar-police")  # has sources, so an agency page
        self.label = f"content/agencies.json[{self.base.index(self.police)}].{LABEL}"

    def write(self, contract=CONTRACT, contract_check=..., **profile):  # type: ignore[assignment]
        document = copy.deepcopy(self.base)
        entry = next(a for a in document if a["agency_id"] == "ca-cedar-police")
        if contract is not ...:
            entry["profile"]["contract"] = contract
        if contract_check is not ...:
            entry["profile"]["contract_check"] = contract_check
        entry["profile"].update(profile)
        self.path.write_text(json.dumps(document))

    def build(self):
        with mock.patch("urllib.request.urlopen", side_effect=refuse_network) as urlopen:
            build_site(self.root, today=TODAY)
        self.assertEqual(urlopen.call_count, 0)

    def snapshot(self):
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def section(self, page):
        return "<section" + page.split('<section class="contract-check"', 1)[1].split("</section>", 1)[0] + "</section>"

    def refusal(self, contract_check, contract=CONTRACT):
        """Build refuses naming the field; content-check lists that same text."""
        self.write(contract, contract_check)
        with self.assertRaises(ContentError) as caught:
            self.build()
        self.assertIn(self.label, str(caught.exception))
        problems = content_problems(self.root)
        assert problems is not None
        self.assertIn(str(caught.exception), problems)
        return str(caught.exception)

    def test_full_reading_order_pages_notes_both_surfaces(self):
        notes = {"term_renewal": "Auto-renews yearly & needs 60 days' notice (\"evergreen\")", "price_changes": "a" * 200,
                 "data_ownership": "n", "termination_data_exit": "See exhibit B"}
        value = check(elements=elements(
            term_renewal={"status": "addressed", "page": 1, "note": notes["term_renewal"]},
            price_changes={"status": "addressed", "page": 9999, "note": notes["price_changes"]},
            data_ownership={"status": "unclear", "page": 12, "note": notes["data_ownership"]},
            sharing_network={"status": "not_found", "page": 5}, vendor_data_use={"status": "unclear"},
            audit_search_logs={"status": "addressed"},
            termination_data_exit={"status": "not_found", "note": notes["termination_data_exit"]}))
        self.write(contract_check=value)
        self.build()

        def note(key):
            return f' <span class="contract-check-note">— {escape(notes[key])}</span>'
        expected = [
            "Term and automatic renewal: addressed (page 1)" + note("term_renewal"),
            "Price changes: addressed (page 9999)" + note("price_changes"),
            "Data ownership: unclear (page 12)" + note("data_ownership"),
            "Retention: not found in the linked contract",
            "Sharing and national-network access: not found in the linked contract (page 5)",
            "Vendor use of data: unclear",
            "Audit and search-log access: addressed",
            "Termination and data return or deletion: not found in the linked contract" + note("termination_data_exit")]
        tail = ('<p>Not found describes this reading, not whether a term exists elsewhere.</p><p>Checked 2026-10-31 against '
                '<a href="https://example.org/cedar/contract.pdf?id=7&amp;v=2" rel="noopener noreferrer">the linked contract</a>. '
                'Checklist: draft.</p></section>')
        for name, tag in (("agencies.html", "h4"), ("agencies/cedar.html", "h3")):
            page = self.read(name)
            html = self.section(page)
            self.assertEqual(html.count(f"<{tag}>{HEADING}</{tag}>"), 1, name)
            self.assertEqual(re.findall(r"<li>(.*?)</li>", html), expected, name)
            self.assertTrue(html.endswith(tail), html[-300:])
            self.assertIn("&amp;", html)
            self.assertIn("&quot;evergreen&quot;", html)
            self.assertNotIn('"evergreen"', html)
            self.assertEqual(page.count('class="contract-check"'), 1)
        cards = self.read("agencies.html")
        self.assertLess(cards.index('class="contract-term"'), cards.index('class="contract-check"'))  # below the contract line
        self.assertEqual([e["id"] for e in load_contract_checklist()["elements"]], IDS)
        self.assertEqual([e["label"] for e in load_contract_checklist()["elements"]], LABELS)

    def test_missing_and_mismatched_source(self):
        url = "contract_url: must equal profile.contract.source_url"
        for other in (SOURCE + "#page=3", SOURCE + "/", "https://example.org/cedar/other.pdf"):
            with self.subTest(other=other):
                self.assertIn(f"{self.label}.{url}", self.refusal(check(contract_url=other)))
        self.assertIn(f"{self.label}.contract_url: must be an https:// URL",
                      self.refusal(check(contract_url="http://example.org/cedar/contract.pdf?id=7&v=2"),
                                   contract={**CONTRACT, "source_url": "https://example.org/cedar/contract.pdf?id=7&v=2"}))
        self.assertIn(f"{self.label}.contract_url: must be an https:// URL", self.refusal(check(contract_url=None)))
        for contract in (None, ...):  # null contract, and no contract at all
            with self.subTest(contract=contract):
                self.assertIn(f"{self.label}.contract_url: needs a valid profile.contract", self.refusal(check(), contract=contract))
        entry = {k: v for k, v in check().items() if k != "contract_url"}
        self.assertIn(f"{self.label}.contract_url: required", self.refusal(entry))
        self.write({k: v for k, v in CONTRACT.items() if k != "source_url"}, check())  # a bad contract is its own problem
        with self.assertRaises(ContentError) as caught:
            self.build()
        self.assertIn("profile.contract", str(caught.exception))
        problems = "\n".join(content_problems(self.root) or [])
        self.assertIn("profile.contract: needs source_url", problems)
        self.assertIn(f"{self.label}.contract_url: needs a valid profile.contract", problems)
        self.assertNotIn("must equal profile.contract.source_url", problems)  # no comparison against an invalid contract
        # a verified contract source does not need a verified contact: the organizer-checked source is what counts
        self.write(contract_check=check())
        document = json.loads(self.path.read_text())
        next(a for a in document if a["agency_id"] == "ca-cedar-police")["verified"] = False
        self.path.write_text(json.dumps(document))
        self.build()
        self.assertIn(HEADING, self.read("agencies.html"))

    def test_dates_and_shape_parity(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        self.assertIn(f"{self.label}.checked_on: must not be after the build date", self.refusal(check(checked_on=tomorrow)))
        self.assertIn(f"{self.label}.checked_on: must not be before profile.contract.checked_on",
                      self.refusal(check(checked_on="2026-10-29")))
        for bad in ("2026-02-30", "2026-2-3", "20261031", "2026-10-31T00:00:00", 20261031, None):
            with self.subTest(checked_on=bad):
                self.assertIn(f"{self.label}.checked_on", self.refusal(check(checked_on=bad)))
        self.write(contract_check=check(checked_on=CONTRACT["checked_on"]))  # the same day is allowed
        self.build()
        self.write(contract_check=check(checked_on=TODAY.isoformat()))  # the build day is allowed
        self.build()
        cases = (({"elements": []}, f"{self.label}.elements: must be a non-empty object"),
                 ({"elements": {}}, f"{self.label}.elements: must be a non-empty object"),
                 ({"elements": None}, f"{self.label}.elements: must be a non-empty object"),
                 ({"elements": elements(retention=["addressed"])}, f"{self.label}.elements.retention: must be an object"),
                 ({"elements": elements(retention=None)}, f"{self.label}.elements.retention: must be an object"),
                 ({"elements": elements(retention={"status": "compliant"})}, f"{self.label}.elements.retention.status: choose one of"),
                 ({"elements": elements(retention={})}, f"{self.label}.elements.retention.status: choose one of"),
                 ({"elements": elements(retention={"status": "addressed", "score": 1})}, f"{self.label}.elements.retention: unknown field 'score'"),
                 ({"elements": {**elements(), "invented": {"status": "addressed"}}}, f"{self.label}.elements.invented: unknown element"),
                 ({"score": 3}, f"{self.label}: unknown field 'score'"))
        for changes, fragment in cases:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.refusal(check(**changes)))
        missing = {k: v for k, v in elements().items() if k != "price_changes"}
        self.assertIn(f"{self.label}.elements.price_changes: missing", self.refusal(check(elements=missing)))
        self.assertIn(f"{self.label}: must be an object", self.refusal("addressed"))
        self.assertIn(f"{self.label}: must be an object", self.refusal([]))

    def test_page_note_boundaries(self):
        where = f"{self.label}.elements.retention"
        for page in (0, 10000, True, 1.0, "3", None, -1):
            with self.subTest(page=page):
                self.assertIn(f"{where}.page: must be a whole number from 1 to 9999",
                              self.refusal(check(elements=elements(retention={"status": "addressed", "page": page}))))
        for note in ("", "   ", "x" * 201, "<b>bold</b>", "a > b", "line\nbreak", "tab\there", "nul\x00", None, 5, ["note"]):
            with self.subTest(note=note):
                self.assertIn(f"{where}.note: plain text of 1 to 200 characters",
                              self.refusal(check(elements=elements(retention={"status": "unclear", "note": note}))))
        for status in ("not_found", "unclear", "addressed"):  # a page can explain uncertainty, so any status takes one
            self.write(contract_check=check(elements=elements(retention={"status": status, "page": 7})))
            self.build()
        text = "  é" + "x" * 194 + "ü  "  # 200 characters, spaces kept as read
        self.assertEqual(len(text), 200)
        self.write(contract_check=check(elements=elements(retention={"status": "unclear", "note": text})))
        self.build()
        self.assertIn(f"— {escape(text)}</span>", self.read("agencies.html"))
        self.write(contract_check=check(elements=elements(retention={"status": "unclear"})))  # a note may be left out
        self.build()
        self.assertNotIn("contract-check-note", self.read("agencies.html"))
        # several bad elements and agencies are all reported in one pass
        document = copy.deepcopy(self.base)
        selected = []
        for index, entry in enumerate(document):
            if entry["agency_id"] in ("ca-cedar-county-sheriff", "ca-cedar-police"):
                selected.append(index)
                entry["profile"]["contract"] = CONTRACT
                entry["profile"]["contract_check"] = check(elements=elements(
                    retention={"status": "addressed", "page": 0}, price_changes={"status": "nope"},
                    data_ownership={"status": "unclear", "note": "<i>"}))
        self.assertEqual(len(selected), 2)
        self.path.write_text(json.dumps(document))
        with self.assertRaises(ContentError):
            self.build()
        problems = "\n".join(content_problems(self.root) or [])
        for index in selected:
            prefix = f"content/agencies.json[{index}].{LABEL}.elements"
            for fragment in (".retention.page: must be a whole number", ".price_changes.status: choose one of",
                             ".data_ownership.note: plain text of 1 to 200"):
                self.assertIn(prefix + fragment, problems)

    def test_an_unselected_agency_with_a_broken_check_is_skipped(self):
        self.write()
        document = json.loads(self.path.read_text())
        sheriff = next(a for a in document if a["agency_id"] == "ca-cedar-county-sheriff")
        sheriff.update(selected=False)
        sheriff["profile"]["contract_check"] = {"checked_on": "nope"}
        self.path.write_text(json.dumps(document))
        self.build()
        self.assertEqual(content_problems(self.root), [])

    def test_resource_failure_and_installed_path(self):
        self.write(contract_check=check())
        self.build()
        before = self.snapshot()
        resources = self.tmp / "_resources"
        target = resources / "checklists" / "contract-checklist.json"
        target.parent.mkdir(parents=True)
        good = json.loads((REPO_ROOT / "checklists" / "contract-checklist.json").read_text())

        def broken(document=None, raw=None):
            target.write_bytes(raw if raw is not None else json.dumps(document).encode())
            return mock.patch.object(content, "CONTRACT_CHECKLIST_PATH", target)

        def ids(order):
            return [{"id": i, "label": l} for i, l in order]
        pairs = list(zip(IDS, LABELS))
        cases = {
            "empty file": dict(raw=b""), "malformed json": dict(raw=b"{not json"), "invalid utf-8": dict(raw=b"\xff\xfe{}"),
            "not an object": dict(document=[good]), "extra key": dict(document={**good, "rule_id": "x"}),
            "missing key": dict(document={"schema_version": 1, "elements": good["elements"]}),
            "version true": dict(document={**good, "schema_version": True}), "version 2": dict(document={**good, "schema_version": 2}),
            "version string": dict(document={**good, "schema_version": "1"}), "version float": dict(document={**good, "schema_version": 1.0}),
            "status reviewed": dict(document={**good, "status": "reviewed"}),
            "empty elements": dict(document={**good, "elements": []}),
            "duplicate ids": dict(document={**good, "elements": ids(pairs[:7] + [pairs[0]])}),
            "wrong order": dict(document={**good, "elements": ids([pairs[1], pairs[0]] + pairs[2:])}),
            "missing id": dict(document={**good, "elements": ids(pairs[:7])}),
            "unknown id": dict(document={**good, "elements": ids(pairs[:7] + [("other", "Other")])}),
            "element extra key": dict(document={**good, "elements": [{**good["elements"][0], "rule_id": "x"}] + good["elements"][1:]}),
            "blank label": dict(document={**good, "elements": [{"id": IDS[0], "label": "  "}] + good["elements"][1:]}),
            "markup label": dict(document={**good, "elements": [{"id": IDS[0], "label": "<b>x"}] + good["elements"][1:]}),
            "number label": dict(document={**good, "elements": [{"id": IDS[0], "label": 3}] + good["elements"][1:]}),
            "oversized": dict(raw=b" " * (64 * 1024 + 1)),
        }
        for name, case in cases.items():
            with self.subTest(name), broken(**case):
                with self.assertRaisesRegex(ContentError, "contract checklist checklists/contract-checklist.json: "):
                    load_contract_checklist()
                with self.assertRaises(ContentError) as caught:  # a build that uses it is refused, naming the check field
                    self.build()
                self.assertIn("the contract checklist could not be loaded", str(caught.exception))
                self.assertNotIn(str(self.tmp), str(caught.exception))
                lines = content_problems(self.root) or []
                self.assertTrue(any("the contract checklist could not be loaded" in line for line in lines))
                self.assertEqual(self.snapshot(), before, "a refused build must keep the prior public tree")
        with broken(good):  # the unchanged content is accepted from an outside location
            self.assertEqual(load_contract_checklist()["status"], "draft")
        target.unlink()
        with mock.patch.object(content, "CONTRACT_CHECKLIST_PATH", target), self.assertRaisesRegex(ContentError, "missing"):
            load_contract_checklist()
        target.mkdir()  # a directory in place of the file
        with mock.patch.object(content, "CONTRACT_CHECKLIST_PATH", target), self.assertRaisesRegex(ContentError, "not a file"):
            load_contract_checklist()
        target.rmdir()
        real = self.tmp / "real.json"
        real.write_text(json.dumps(good))
        target.symlink_to(real)  # the file is a link
        with mock.patch.object(content, "CONTRACT_CHECKLIST_PATH", target), self.assertRaisesRegex(ContentError, "symbolic link"):
            load_contract_checklist()
        target.unlink()
        shutil.copy(real, target)
        moved = self.tmp / "moved"
        shutil.copytree(resources / "checklists", moved)  # a link in the folder chain
        shutil.rmtree(resources / "checklists")
        (resources / "checklists").symlink_to(moved)
        with mock.patch.object(content, "CONTRACT_CHECKLIST_PATH", target), self.assertRaisesRegex(ContentError, "symbolic link"):
            load_contract_checklist()
        other = self.tmp / "other"
        other.mkdir()
        (other / "checklists").mkdir()
        shutil.copy(real, other / "checklists" / "contract-checklist.json")
        link = self.tmp / "linked"
        link.symlink_to(other)
        with mock.patch.object(content, "CONTRACT_CHECKLIST_PATH", link / "checklists" / "contract-checklist.json"):
            with self.assertRaisesRegex(ContentError, "symbolic link"):  # the _resources folder is a link
                load_contract_checklist()
        with mock.patch.object(content.Path, "read_bytes", side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaisesRegex(ContentError, "unreadable"):  # holds even when the tests run as root
                load_contract_checklist()
        # the packaged resource loads from an unrelated working directory, by the package's own location
        packaged = REPO_ROOT / "campaign_tool" / "_resources" / "checklists" / "contract-checklist.json"
        self.assertEqual(content.CONTRACT_CHECKLIST_PATH, packaged)
        self.assertEqual(packaged.read_bytes(), (REPO_ROOT / "checklists" / "contract-checklist.json").read_bytes())
        old = os.getcwd()
        self.addCleanup(os.chdir, old)
        os.chdir(self.tmp)
        self.assertEqual([e["id"] for e in load_contract_checklist()["elements"]], IDS)
        self.build()  # still builds, and an absent check never reads the resource
        with mock.patch.object(content, "CONTRACT_CHECKLIST_PATH", self.tmp / "absent.json"):
            self.write()
            self.build()

    def test_no_check_byte_identical_and_repeatable(self):
        self.write()  # a contract, but no reading of it
        self.build()
        plain = self.snapshot()
        self.assertFalse([n for n, b in plain.items() if b"contract-check" in b or b"What the contract says" in b])
        self.write(contract_check=None)
        self.build()
        self.assertEqual(self.snapshot(), plain)
        self.write(contract_check=check())
        self.build()
        with_check = self.snapshot()
        changed = {n for n in plain.keys() | with_check.keys() if plain.get(n) != with_check.get(n)}
        self.assertEqual(changed, {"agencies.html", "agencies/cedar.html"})  # no card JS, open data, feed or other agency page
        for name, data in with_check.items():
            if name not in changed:
                self.assertNotIn(b"What the contract says", data, name)
        self.build()
        self.assertEqual(self.snapshot(), with_check)  # repeatable
        self.write(contract_check=None)  # removing the check restores the exact bytes
        self.build()
        self.assertEqual(self.snapshot(), plain)

    def test_neutral_copy_legal_guard(self):
        value = check(elements=elements(
            term_renewal={"status": "addressed", "page": 2, "note": "Five-year term with one renewal option"},
            retention={"status": "unclear", "note": "Section 4 mentions days but the number is unreadable"}))
        self.write(contract_check=value)
        self.build()
        for name in ("agencies.html", "agencies/cedar.html"):
            html = self.section(self.read(name))
            text = unescape(re.sub(r"<[^>]+>", " ", html))
            self.assertEqual(legal_claims(text), [])
            for word in FORBIDDEN:
                self.assertNotIn(word, html.lower())
        # tool-authored labels, status words and footer alone are clean too
        self.write(contract_check=check())
        self.build()
        self.assertEqual(legal_claims(unescape(re.sub(r"<[^>]+>", " ", self.section(self.read("agencies.html"))))), [])
        # the guard stays alert-only: it recognises an accusation typed into a note, and the build neither refuses nor edits it
        accusation = "Example Agency violated the law"
        self.assertTrue(legal_claims(accusation))
        self.write(contract_check=check(elements=elements(retention={"status": "unclear", "note": accusation})))
        self.build()
        self.assertIn(f"— {accusation}</span>", self.read("agencies.html"))
        # invalid input keeps the original content and the prior public tree
        before, original = self.snapshot(), self.path.read_text()
        self.write(contract_check=check(contract_url="https://example.org/other.pdf"))
        broken = self.path.read_text()
        with self.assertRaises(ContentError):
            self.build()
        self.assertEqual(self.path.read_text(), broken)
        self.assertNotEqual(broken, original)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
