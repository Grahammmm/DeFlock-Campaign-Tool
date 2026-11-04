"""DF0022-02: examples/faq.json, the draft FAQ starter that a fresh `content init` seeds as editable [EDIT ME] copy.

Everything runs on temporary synthetic campaigns; sockets are blocked and nothing is fetched or sent.
"""
import copy
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import content_init, law
from campaign_tool.content import ContentError, load_faq, read_faq
from campaign_tool.content_check import content_problems
from campaign_tool.legal_claims import faq_notes, legal_claims
from campaign_tool.markdown_lite import lint
from campaign_tool.site import build_site

REPO = Path(__file__).resolve().parents[1]
STARTER = REPO / "examples" / "faq.json"
MIRROR = REPO / "campaign_tool" / "_resources" / "examples" / "faq.json"
TODAY = date(2026, 10, 1)
MARKER = "[EDIT ME: verify for this campaign] "
CONTACT = "[EDIT ME: add a verified public correction contact]"
RULE = "ca-gov-7922.535-determination-and-extension-2026"
CAVEAT = ("The California law package is a draft, not independently reviewed; check the current official source and "
          "get qualified advice before relying on it.")
GENERIC = ("[EDIT ME: verify for this campaign] Check the applicable law package and official guidance for response timing. "
           "A reminder is not a legal determination or a promise that records will be produced by that date.")
QUESTIONS = [
    "What is an ALPR?", "What is this campaign asking for?", "Why ask for these records?",
    "What is a public-records request?", "How long does an agency have to answer?",
    "Does a missing record prove a violation?", "Why do we not publish plate reads?",
    "Where can I see the records behind a finding?", "How are findings reviewed?", "What do the finding labels mean?",
    "How are findings corrected?", "How can I ask for a correction?", "How can I help?",
    "Is this campaign anti-police?", "Does this website give legal advice?"]
DEADLINE_AT = QUESTIONS.index("How long does an agency have to answer?")
# Wording a neutral starter must not use: lateness, entitlement, guarantees, automatic outcomes, violation wording.
OVERCLAIM = re.compile(r"\b(?:late|overdue|entitled|entitlement|guarantee[ds]?|automatically|violat\w*|illegal|unlawful\w*|"
                       r"non-?complian\w*|breach\w*)\b", re.I)
NO_SOCKETS = mock.patch("socket.socket", side_effect=AssertionError("network access attempted"))


def starter():
    return json.loads(STARTER.read_text(encoding="utf-8"))


def sentences(text):
    return [part for part in re.split(r"(?<=[.!?])\s+", text.strip()) if part]


def cli(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, args)], cwd=REPO,
                          capture_output=True, text=True, timeout=120)


class FaqCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-faq-starter-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        NO_SOCKETS.start()
        self.addCleanup(NO_SOCKETS.stop)

    def campaign(self, name="campaign", state="CA"):
        root = self.tmp / name
        result = cli("init", "--directory", root, "--name", "Synthetic Cedar Watch", "--county", "Cedar", "--state", state)
        self.assertEqual(result.returncode, 0, result.stderr)
        return root

    def initialized(self, name="campaign", state="CA"):
        root = self.campaign(name, state)
        content_init.init_content(root)
        return root

    def refused(self, document, root):
        problems = []
        self.assertIsNone(load_faq(document, root, [], problems))
        with self.assertRaises(ContentError):
            load_faq(document, root, [])
        return problems


class StarterShapeTests(FaqCase):
    def test_fifteen_questions_and_editable_plain_answers(self):
        document = starter()
        self.assertEqual(set(document), {"schema_version", "entries"})  # no envelope, notice or metadata key
        entries = document["entries"]
        self.assertEqual([entry["question"] for entry in entries], QUESTIONS)
        self.assertEqual(len(set(QUESTIONS)), 15)
        for entry in entries:
            with self.subTest(question=entry["question"]):
                self.assertLessEqual(set(entry), {"question", "answer_md", "rule_id"})  # no finding_ids anywhere
                self.assertLessEqual(len(entry["question"]), 160)
                answer = entry["answer_md"]
                self.assertTrue(answer.startswith(MARKER), answer)  # visibly unfinished organizer copy
                self.assertLessEqual(len(answer), 1500)
                self.assertTrue(1 <= len(sentences(answer[len(MARKER):])) <= 4, answer)
                self.assertEqual(lint(answer), [])  # only markdown_lite constructs, nothing the build degrades
                self.assertNotRegex(answer, OVERCLAIM)
                self.assertNotIn("mailto:", answer)
                self.assertFalse(re.search(r"https?://", answer) and entry["question"] != QUESTIONS[DEADLINE_AT], answer)
        self.assertEqual(sum(entry["answer_md"].count("[EDIT ME: ") for entry in entries), 16)
        correction = entries[QUESTIONS.index("How can I ask for a correction?")]["answer_md"]
        self.assertIn(CONTACT, correction)
        self.assertEqual(sum(CONTACT in entry["answer_md"] for entry in entries), 1)
        # the real loader accepts the file as shipped, in order, with no finding references
        root = self.campaign()
        loaded = load_faq(document, root, [])
        self.assertEqual([entry["question"] for entry in loaded], QUESTIONS)
        self.assertTrue(all(entry["finding_ids"] == [] for entry in loaded))
        # draft organizer choices are said to be draft choices, not universal guarantees or political positions
        for question in ("What is this campaign asking for?", "Why do we not publish plate reads?", "Is this campaign anti-police?"):
            self.assertRegex(entries[QUESTIONS.index(question)]["answer_md"], r"draft|Replace this answer")
        # help and plate-read answers never solicit sightings, personal data, tips or request flooding
        helping = entries[QUESTIONS.index("How can I help?")]["answer_md"]
        self.assertIn("Please do not send plate sightings, tips, accusations or personal data", helping)

    def test_loader_refuses_a_broken_copy_of_the_starter(self):
        root = self.campaign()
        for label, edit, pattern in (
                ("blank answer", lambda d: d["entries"][3].update(answer_md="  "), r"entries\[3\]\.answer_md"),
                ("repeated question", lambda d: d["entries"][1].update(question=QUESTIONS[0]),
                 r"entries\[1\]\.question: duplicates entries\[0\]"),
                ("notice key", lambda d: d.update(notice_md="x"), r"unknown field"),
                ("placeholder finding id", lambda d: d["entries"][0].update(finding_ids=["fnd_x"]),
                 r"entries\[0\]\.finding_ids\[0\]: not a published finding id")):
            with self.subTest(label):
                document = starter()
                edit(document)
                self.assertTrue(any(re.search(pattern, problem) for problem in self.refused(document, root)), label)


class StatutoryAnswerTests(FaqCase):
    def test_statutory_answer_has_exact_rule_and_draft_caveat(self):
        entries = starter()["entries"]
        cited = [entry for entry in entries if "rule_id" in entry]
        self.assertEqual([entry["question"] for entry in cited], [QUESTIONS[DEADLINE_AT]])  # the only statutory answer
        entry = cited[0]
        self.assertEqual(entry["rule_id"], RULE)
        package = law.load_package("us-ca")
        self.assertEqual((package["status"], package["reviewed_by"]), ("draft", []))  # the caveat stays true
        rule = next(item for item in package["rules"] if item["rule_id"] == RULE)
        urls = [source["url"] for source in rule["sources"]]
        links = re.findall(r"\]\((https://[^)]+)\)", entry["answer_md"])
        self.assertEqual(len(links), 1)
        self.assertIn(links[0], urls)  # the one link is an official source of that very rule
        answer = entry["answer_md"]
        self.assertIn(CAVEAT, answer)
        self.assertLessEqual(len(sentences(answer[len(MARKER):])), 4)
        for fact in ("within 10 days of receiving", "at most 14 days", "written notice", "agency head or designee",
                     "A determination is not the same as producing the records",
                     "actual date of receipt, holidays and notice requirements need human checking"):
            self.assertIn(fact, answer)
        for other in entries:  # no other answer states the law or names a statute
            if other is not entry:
                self.assertNotRegex(other["answer_md"], r"§|Gov\. Code|Civ\. Code|statute|\b\d+ days\b", other["question"])
        self.assertNotRegex(answer, r"\b(?:due|deadline)\b.*\b20\d\d-\d\d-\d\d\b")  # no due-date arithmetic

    def test_unknown_or_misapplied_rule_is_refused(self):
        document = starter()
        document["entries"][DEADLINE_AT]["rule_id"] = "ca-gov-7922.535-no-such-rule"
        problems = self.refused(document, self.campaign())
        self.assertTrue(any("entries[4].rule_id: unknown law-package rule id" in problem for problem in problems), problems)
        # the California rule is not usable for a campaign whose state has no law package
        problems = self.refused(starter(), self.campaign("oregon", "OR"))
        self.assertTrue(any("entries[4].rule_id: no usable campaign law package" in problem for problem in problems), problems)


class LegalGuardTests(FaqCase):
    def test_legal_guard_sees_every_answer(self):
        entries = starter()["entries"]
        self.assertEqual(faq_notes(entries), [])
        for state in ("CA", "OR"):
            with self.subTest(state=state):
                root = self.initialized(state.lower(), state)
                self.assertEqual(faq_notes(read_faq(root, [])), [])
        # a positive synthetic assertion in ANY answer is flagged at its position, by the unstubbed guard
        for index in (0, 4, 14):
            with self.subTest(index=index):
                changed = copy.deepcopy(entries)
                changed[index]["answer_md"] += " The synthetic agency violated the law."
                notes = faq_notes(changed)
                self.assertEqual(len(notes), 1, notes)
                self.assertIn(f"faq.json.entries[{index}].answer_md", notes[0])
                self.assertIn("review-required", notes[0])
        self.assertIn("violation-verb", legal_claims("The synthetic agency violated the law."))


class FreshInitTests(FaqCase):
    def test_fresh_init_then_content_check_and_build(self):
        root = self.campaign()
        report = content_init.init_content(root)
        content = root / "content"
        self.assertEqual(report["written"], ["content/faq.json", "content/findings/", "content/meetings/",
                                             "content/site.json", "content/sources.json"])
        texts = [(content / name).read_text(encoding="utf-8") for name in ("site.json", "faq.json")]
        self.assertEqual(report["placeholders"], sum(text.count("[EDIT ME: ") for text in texts))
        self.assertEqual(report["placeholders"], 4 + 16)
        self.assertEqual(json.loads(texts[1]), starter())  # California keeps the starter exactly, rule_id included
        self.assertEqual(content_problems(root), [])
        self.assertEqual(faq_notes(read_faq(root, [])), [])
        build_site(root, today=TODAY)
        page = (root / "public" / "faq.html").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r'<h2 id="q-(\d+)">', page), [str(number) for number in range(1, 16)])
        positions = [page.index(f'id="q-{number}"') for number in range(1, 16)]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(page.count("[EDIT ME: verify for this campaign]"), 15)  # markers stay visible
        self.assertIn(CONTACT, page)
        self.assertIn(CAVEAT, page)
        self.assertIn(f"<code>{RULE}</code>", page)
        self.assertIn("Law package status: draft, not legally reviewed", page)
        self.assertIn("No findings have been published yet", (root / "public" / "findings" / "index.html").read_text(encoding="utf-8"))

    def test_other_state_variant_has_no_california_rule(self):
        root = self.campaign("oregon", "OR")
        report = content_init.init_content(root)
        document = json.loads((root / "content" / "faq.json").read_text(encoding="utf-8"))
        self.assertEqual([entry["question"] for entry in document["entries"]], QUESTIONS)
        self.assertNotIn("rule_id", json.dumps(document))
        self.assertEqual(document["entries"][DEADLINE_AT]["answer_md"], GENERIC)
        expected = copy.deepcopy(starter())
        expected["entries"][DEADLINE_AT] = {"question": QUESTIONS[DEADLINE_AT], "answer_md": GENERIC}
        self.assertEqual(document, expected)  # the other fourteen answers are the shipped starter
        self.assertEqual(report["placeholders"], 4 + 16)
        self.assertEqual(content_problems(root), [])
        build_site(root, today=TODAY)
        page = (root / "public" / "faq.html").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r'<h2 id="q-(\d+)">', page), [str(number) for number in range(1, 16)])
        self.assertNotIn("7922.535", page)
        self.assertNotIn("Law package status", page)
        self.assertNotIn("California", page)

    def test_existing_content_and_midwrite_failure_are_unchanged(self):
        refused = "content/ already exists; nothing was written"

        def tree(root):
            return sorted((str(path.relative_to(root)), path.is_symlink(), path.read_bytes() if path.is_file() and not path.is_symlink() else None)
                          for path in root.rglob("*"))

        def make_dir(path):
            path.mkdir()
            (path / "site.json").write_text('{"organizer": "kept"}\n', encoding="utf-8")

        for label, make in (("directory", make_dir), ("file", lambda path: path.write_text("kept\n", encoding="utf-8")),
                            ("dangling symlink", lambda path: path.symlink_to(self.tmp / "nowhere"))):
            with self.subTest(label):
                root = self.campaign("existing-" + label.split()[0])
                make(root / "content")
                before = tree(root)
                with self.assertRaisesRegex(ValueError, refused):
                    content_init.init_content(root)
                self.assertEqual(tree(root), before)
                self.assertEqual(list(root.glob(".content.new-*")), [])
        root = self.campaign("failures")
        before = tree(root)
        real_write = content_init._write
        # a content/ that appears after faq.json is written is never replaced
        with mock.patch.object(content_init, "_write", side_effect=lambda path, text: (
                real_write(path, text), path.name == "faq.json" and (root / "content").mkdir())):
            with self.assertRaisesRegex(ValueError, refused):
                content_init.init_content(root)
        self.assertEqual(list((root / "content").iterdir()), [])
        self.assertEqual(list(root.glob(".content.new-*")), [])
        (root / "content").rmdir()
        self.assertEqual(tree(root), before)
        # the FAQ write fails; the FAQ is refused by the loader; the starter itself is unreadable
        for label, patches in (
                ("write", mock.patch.object(content_init, "_write", side_effect=lambda path, text: (
                    (_ for _ in ()).throw(OSError("injected FAQ write failure")) if path.name == "faq.json" else real_write(path, text)))),
                ("validation", mock.patch.object(content_init, "load_faq", return_value=None)),
                ("starter", mock.patch.object(content_init, "FAQ_STARTER", self.tmp / "missing.json"))):
            with self.subTest(label), patches:
                with self.assertRaises((OSError, RuntimeError)):
                    content_init.init_content(root)
            self.assertFalse(os.path.lexists(root / "content"))
            self.assertEqual(list(root.glob(".content.new-*")), [])
            self.assertEqual(tree(root), before)

    def test_modes_stay_private(self):
        root = self.initialized()
        content = root / "content"
        for path in [content, *content.rglob("*")]:
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600, path)


class InstalledResourceTests(FaqCase):
    def test_installed_resource_and_from_allowlist(self):
        self.assertEqual(MIRROR.read_bytes(), STARTER.read_bytes())
        self.assertEqual(content_init.FAQ_STARTER, MIRROR)  # the runtime reads the packaged copy, never examples/
        real_read_text = Path.read_text

        def no_checkout_examples(path, *args, **kwargs):
            if (REPO / "examples") in Path(path).parents:
                raise AssertionError("runtime read the checkout examples directory")
            return real_read_text(path, *args, **kwargs)

        cfg = {"schema_version": 1, "name": "Synthetic Cedar Watch", "county": "Cedar", "state": "CA"}
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        previous = Path.cwd()
        os.chdir(elsewhere)
        try:
            with mock.patch.object(Path, "read_text", no_checkout_examples):
                document = content_init.faq_document(cfg)
        finally:
            os.chdir(previous)
        self.assertEqual(document, starter())
        document["entries"][0]["question"] = "changed"  # a caller's edits never reach the next init
        self.assertEqual(content_init.faq_document(cfg), starter())
        # --from copies only public branding: a source FAQ is skipped and the destination gets no starter
        old, new = self.campaign("old"), self.campaign("new", "OR")
        (old / "content").mkdir()
        (old / "content" / "site.json").write_text(json.dumps(
            {"schema_version": 1, "tagline": "A tagline", "mission_md": "A mission", "about_md": "About"}), encoding="utf-8")
        (old / "content" / "faq.json").write_bytes(STARTER.read_bytes())
        sources = [path for path in [old, *old.rglob("*")]]
        before = {path: (path.lstat().st_mtime_ns, path.read_bytes() if path.is_file() else None) for path in sources}
        report = content_init.init_content(new, source_root=old)
        self.assertIn("faq.json", report["skipped"])
        self.assertNotIn("content/faq.json", report["copied"])
        self.assertFalse((new / "content" / "faq.json").exists())
        self.assertEqual({path: (path.lstat().st_mtime_ns, path.read_bytes() if path.is_file() else None) for path in sources}, before)


if __name__ == "__main__":
    unittest.main()
