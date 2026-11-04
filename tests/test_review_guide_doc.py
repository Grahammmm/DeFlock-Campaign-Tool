"""DF1952-15: docs/REVIEW-GUIDE.md. Local links and anchors, every documented command against the real CLI parser, and the
synthetic worked example run in temporary campaign storage with outbound connections blocked. Synthetic data only; nothing
is sent, published or built.

Run from the repository root: python3 -B -m unittest tests.test_review_guide_doc -v
"""
import argparse
import contextlib
import hashlib
import io
import json
import re
import shlex
import shutil
import socket
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, content, findings  # noqa: E402

GUIDE, README, PUBLICATION = (REPO_ROOT / "docs" / "REVIEW-GUIDE.md", REPO_ROOT / "README.md",
                              REPO_ROOT / "docs" / "PUBLICATION.md")
RECORD = b"Synthetic policy text. Retention: 30 days.\n"
SENTINEL = "PRIVATE-SENTINEL-7731"
NO_NETWORK = AssertionError("no network in these tests")
FENCE = re.compile(r"^```(\w*)\n(.*?)^```$", re.S | re.M)
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
PREFIX = ["python3", "-m", "campaign_tool"]


def draft_document(**changes):
    base = {"id": "syn-guide-1", "title": "Synthetic policy page posted", "slug": "synthetic-policy-page",
            "summary": "The synthetic policy page lists a retention period of 30 days.",
            "body_md": "Body text for the synthetic example.\n", "classification": "documented_fact",
            "confidence": "high", "event_date": "2026-01-02", "agency_id": "ex-synthetic-agency",
            "sources": [{"locator": "p. 1", "sha256": hashlib.sha256(RECORD).hexdigest()}],
            "published_at": None, "corrections": [], "state": "draft", "author": "Author Z",
            "limitations": ["Synthetic limits."], "counterevidence": ["None found."]}
    base.update(changes)
    return base


# --- markdown helpers -------------------------------------------------------------------------------------------

def read(path):
    return Path(path).read_text(encoding="utf-8")


def slug(heading):
    heading = re.sub(r"[`*_]", "", heading.strip().lower())
    return re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", heading))


def anchors(text):
    """GitHub-style heading anchors of a Markdown text (outside code fences), duplicates suffixed -1, -2."""
    seen, found = {}, set()
    for line in FENCE.sub("", text).splitlines():
        match = re.match(r"#{1,6}\s+(.*)", line)
        if match:
            base = slug(match.group(1))
            found.add(base if base not in seen else f"{base}-{seen[base]}")
            seen[base] = seen.get(base, 0) + 1
    return found


def check_links(source, text):
    """Problems for every local link in ``text`` (as if it were the file ``source``): missing file or fragment."""
    problems = []
    for target in LINK.findall(FENCE.sub("", text)):
        if re.match(r"[a-z][a-z0-9+.-]*:", target):
            continue  # external: never fetched here
        name, _, fragment = target.partition("#")
        path = (Path(source).parent / name).resolve() if name else Path(source).resolve()
        if not path.exists():
            problems.append(f"{target}: no such file")
        elif fragment and (path.suffix != ".md" or fragment not in anchors(read(path))):
            problems.append(f"{target}: no such heading")
    return problems


def section(text, heading):
    parts = re.split(r"^## ", text, flags=re.M)
    matches = [p for p in parts if p.startswith(heading + "\n")]
    if len(matches) != 1:
        raise DocumentError(f"expected exactly one '## {heading}' section, found {len(matches)}")
    return matches[0]


# --- command helpers --------------------------------------------------------------------------------------------

class DocumentError(AssertionError):
    pass


def command_lines(block):
    """Logical lines of a sh block (backslash continuations joined)."""
    return [line.strip() for line in re.sub(r"\\\n\s*", " ", block).splitlines() if line.strip()]


def documented(text):
    """Every command in the text's sh fences and inline code spans, as argv lists. A line that is not a campaign_tool
    command is an error rather than silently skipped."""
    commands = []
    for kind, body in FENCE.findall(text):
        if kind == "sh":
            for line in command_lines(body):
                argv = shlex.split(line)
                if argv[:3] != PREFIX:
                    raise DocumentError(f"not a campaign_tool command: {line}")
                commands.append(argv)
    for span in re.findall(r"`(python3 -m campaign_tool[^`]*)`", FENCE.sub("", text)):
        commands.append(shlex.split(span))
    return commands


class Parsed(Exception):
    pass


def substitute(argv, mapping):
    def one(token):
        return re.sub(r"\$([A-Z_]+)", lambda m: str(mapping[m.group(1)]), token)
    return [one(token) for token in argv[3:]]


def parse_only(arguments):
    """The real CLI parse of ``arguments`` (nothing runs); the Namespace, or DocumentError if the parser refuses."""
    original = argparse.ArgumentParser.parse_args

    def capture(self, args=None, namespace=None):
        raise Parsed(original(self, args, namespace))

    err = io.StringIO()
    with mock.patch.object(sys, "argv", ["campaign_tool", *arguments]), \
            mock.patch.object(argparse.ArgumentParser, "parse_args", capture), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except Parsed as parsed:
            return parsed.args[0]
        except SystemExit:
            raise DocumentError(err.getvalue().strip().splitlines()[-1] if err.getvalue() else "refused") from None
    raise DocumentError("parser returned without parsing")


def run(arguments):
    """Run the real CLI in process: (exit code, stdout text, stderr text). stdout has a .buffer like the real one."""
    out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", newline="", write_through=True)
    err, code = io.StringIO(), 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return code, out.buffer.getvalue().decode("utf-8"), err.getvalue()


def tree(root):
    root = Path(root)
    return {str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None) for p in sorted(root.rglob("*"))}


class Sandbox:
    """A synthetic campaign in temporary storage plus the guide's variables."""

    def __init__(self, case, roster=True):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-review-guide-")
        case.addCleanup(temporary.cleanup)
        self.case, self.base = case, Path(temporary.name).resolve()
        self.root = self.base / "campaign"
        self.draft = self.root / "private" / "drafts" / "syn-guide-1.json"
        self.before = self.base / "draft-before.json"
        self.mapping = {"CAMPAIGN": self.root, "DRAFT": self.draft, "DRAFT_BEFORE": self.before,
                        "PREVIEW_DIR": self.base / "preview", "PACKET_DIR": self.base / "packet",
                        "SIGNATURE": self.base / "payload.sig", "FINDING_ID": "syn-guide-1"}
        self.ok("init", "--directory", self.root, "--name", "Synthetic", "--county", "Synthetic County", "--state", "ca")
        self.write(draft_document())
        shutil.copy(self.draft, self.before)
        self.blocks = [[substitute(argv, self.mapping) for argv in documented_block] for documented_block in
                       worked_blocks()]

    def write(self, document):
        self.draft.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.draft.write_text(json.dumps(document), encoding="utf-8")

    def ok(self, *arguments):
        code, out, err = run(arguments)
        self.case.assertEqual(code, 0, err)
        return out

    def refused(self, *arguments):
        before = tree(self.root)
        code, out, err = run(arguments)
        self.case.assertNotEqual(code, 0, out)
        self.case.assertTrue(err.startswith("Stopped: "), err)
        self.case.assertEqual(tree(self.root), before)
        return err

    def play(self, index):
        """Run worked-example block ``index`` as written in the guide; diff must exit 3, everything else 0."""
        results = []
        for arguments in self.blocks[index]:
            code, out, err = run(arguments)
            self.case.assertEqual(code, 3 if arguments[:2] == ["finding", "diff"] else 0, err or out)
            results.append(out)
        return results

    def status(self):
        return json.loads(self.ok("finding", "review-status", self.draft, "--directory", self.root))

    def review(self, role, decision, reviewer, rationale="Checked synthetic p. 1."):
        return self.ok("finding", "review", self.draft, "--directory", self.root, "--role", role, "--decision",
                       decision, "--reviewer", reviewer, "--rationale", rationale)

    def roster(self):
        self.ok("finding", "reviewers", "add", "--directory", self.root, "--name", "Reviewer A", "--role", "factual")
        self.ok("finding", "reviewers", "add", "--directory", self.root, "--name", "Reviewer B", "--role", "legal",
                "--role", "privacy")

    def public_state(self):
        return {name: data for name, data in tree(self.root).items()
                if name.split("/")[0] in ("public", "content", "kit")}


def worked_blocks():
    text = section(read(GUIDE), "Worked example (synthetic)")
    blocks = [[shlex.split(line) for line in command_lines(body)] for kind, body in FENCE.findall(text) if kind == "sh"]
    for block in blocks:
        for argv in block:
            if argv[:3] != PREFIX:
                raise DocumentError(f"not a campaign_tool command: {argv}")
    return blocks


class GuideTests(unittest.TestCase):
    def setUp(self):
        patches = [mock.patch.object(socket, "socket", side_effect=NO_NETWORK),
                   mock.patch.object(socket, "create_connection", side_effect=NO_NETWORK)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_role_sections_and_decision_rationales(self):
        text = read(GUIDE)
        lower = text.lower()
        for phrase in ("nothing here is legal advice", "does not change the review status",
                       "must not be the draft's `author`", "confidential", "a role is not an identity",
                       "there is no \"abstain\" decision", "unresolved_challenge", "never"):
            self.assertIn(phrase, lower)
        for heading, concepts in {
                "Factual reviewer": ("does not show it never happened", "locator", "counterevidence", "infers"),
                "Legal reviewer": ("event date", "rule_version", "`likely`", "exceptions", "qualified independent reviewer",
                                   "does not make it so"),
                "Privacy reviewer": ("indirect", "file metadata", "separate redaction sign-off",
                                     "never ask anyone to edit an original", "never the sensitive detail")}.items():
            body = section(text, heading)
            for label in ("**Read.**", "**Check.**", "**Decision.**", "**Rationale.**"):
                self.assertIn(label, body, f"{heading}: {label}")
            decision = body.split("**Decision.**")[1].split("**Rationale.**")[0]
            self.assertIn("Approve", decision)
            self.assertTrue("changes_requested" in decision, heading)
            self.assertIn("reject", decision.lower())
            for concept in concepts:
                self.assertIn(concept, body.lower(), f"{heading}: {concept}")
        choosing = section(text, "Choosing a decision and writing it down")
        for decision in findings.DECISIONS:
            self.assertIn(decision, choosing)
        self.assertIn("role + claim or field + safe locator or rule citation", choosing)
        self.assertEqual(len(re.findall(r"^## (Factual|Legal|Privacy) reviewer$", text, re.M)), 3)

    def test_local_links_and_anchors_resolve(self):
        text = read(GUIDE)
        self.assertEqual(check_links(GUIDE, text), [])
        self.assertEqual(check_links(PUBLICATION, read(PUBLICATION)), [])
        self.assertEqual(check_links(README, read(README)), [])
        self.assertIn("(docs/REVIEW-GUIDE.md)", read(README))
        self.assertIn("(REVIEW-GUIDE.md)", read(PUBLICATION))
        targets = {link.partition("#")[0] for link in LINK.findall(text)}
        self.assertTrue({"PUBLICATION.md", "LAW-PACKAGES.md"} <= targets)
        self.assertIn("effective-date-discipline", anchors(read(REPO_ROOT / "docs" / "LAW-PACKAGES.md")))
        for label, mutated in {
                "missing file": text.replace("(DATA-HANDLING.md)", "(NO-SUCH-FILE.md)"),
                "missing fragment": text.replace("#agency-right-of-reply", "#no-such-heading", 1),
                "link to a missing guide": text + "\n[x](NO-SUCH-GUIDE.md)\n"}.items():
            with self.subTest(label):
                self.assertEqual(len(check_links(GUIDE, mutated)), 1)
        self.assertEqual(len(check_links(README, read(README).replace("(docs/REVIEW-GUIDE.md)", "(docs/REVIEW-GUIDES.md)"))), 1)

    def test_every_documented_command_is_real(self):
        mapping = Sandbox(self).mapping
        commands = documented(read(GUIDE))
        seen = set()
        for argv in commands:
            args = parse_only(substitute(argv, mapping))
            self.assertEqual(args.command, "finding", argv)
            seen.add((args.finding_command, getattr(args, "reviewers_command", None)))
            if args.finding_command == "diff":  # the one cross-check cli.main makes after parsing
                self.assertEqual(len(args.inputs), 1 if args.from_draft else 2, argv)
            if args.finding_command == "review":
                self.assertTrue(args.signature or "--signature" not in argv)
        self.assertEqual(seen, {("review-payload", None), ("review", None), ("review-status", None), ("diff", None),
                                ("assign", None), ("preview", None), ("packet", None), ("reviewers", "add"),
                                ("reviewers", "list")})
        forms = {(a[4], "--replace" in a, "--from" in a) for a in commands if a[4] in ("assign", "diff")}
        self.assertEqual(forms, {("assign", False, False), ("assign", True, False), ("diff", False, False),
                                 ("diff", False, True)})
        sample = [a for a in commands if a[4:5] == ["review-payload"]][0]
        for label, bad in {
                "unknown child command": [*PREFIX, "finding", "frobnicate", *sample[5:]],
                "unknown reviewers child": [*PREFIX, "finding", "reviewers", "remove", "--directory", "$CAMPAIGN"],
                "misspelled option": [t.replace("--reviewer", "--revewer") for t in sample],
                "missing required option": [t for i, t in enumerate(sample) if t != "--role" and sample[i - 1] != "--role"],
                "unknown top-level command": [*PREFIX, "frobnicate"]}.items():
            with self.subTest(label), self.assertRaises(DocumentError):
                parse_only(substitute(bad, mapping))
        with self.assertRaises(DocumentError):
            documented("```sh\nssh-keygen -Y sign -f KEY payload\n```\n")
        with self.assertRaises(DocumentError):
            documented("```sh\npython3 -m campaign_tool finding review-status X --directory D\nrm -rf x\n```\n")

    def test_worked_example_private_assignment_and_role_receipts(self):
        box = Sandbox(self)
        baseline = box.public_state()
        box.play(0)
        listed = json.loads(box.ok("finding", "reviewers", "list", "--directory", box.root))
        self.assertEqual(sorted(r["name"] for r in listed["reviewers"]), ["Reviewer A", "Reviewer B"])
        for who, role in (("Author Z", "factual"), ("Reviewer B", "factual"), ("Reviewer Q", "factual"),
                          ("Reviewer A", "legal")):
            with self.subTest(who=who, role=role):
                box.refused("finding", "review", box.draft, "--directory", box.root, "--role", role, "--decision",
                            "approve", "--reviewer", who, "--rationale", "Synthetic.")
                box.refused("finding", "assign", box.draft, "--directory", box.root, "--role", role, "--reviewer",
                            who, "--on", "2026-03-01")
        assigned = box.play(1)
        report = box.status()
        self.assertTrue({"missing_factual_review", "missing_legal_review", "missing_privacy_review",
                         "need_two_independent_reviewers"} <= set(report["review_blockers"]))
        self.assertEqual(report["receipts_for_this_content"], 0)
        self.assertEqual({r: s["state"] for r, s in report["assignments"].items()}, {r: "assigned" for r in findings.ROLES})
        self.assertEqual(report["assignments"]["privacy"]["reviewer"], "Reviewer B")
        self.assertNotIn("Reviewer A", json.dumps({k: v for k, v in report.items() if k != "assignments"}))
        self.assertTrue(assigned[-1].lstrip().startswith("{"))
        box.play(2)
        report = box.status()
        self.assertEqual(report["review_blockers"], [])
        self.assertEqual({r: s["state"] for r, s in report["assignments"].items()},
                         {r: "receipt_recorded" for r in findings.ROLES})
        self.assertEqual((report["receipts_for_this_content"], report["stale_receipts"]), (3, 0))
        self.assertEqual(box.public_state(), baseline)  # no publish, build or send artefacts
        self.assertFalse((box.root / "private" / "publication-log.jsonl").exists())

    def test_payload_is_read_only_and_bound_to_exact_review(self):
        box = Sandbox(self)
        (box.root / "private" / "campaign-id.json").unlink()
        payload = ("finding", "review-payload", box.draft, "--directory", box.root, "--role", "factual", "--decision",
                   "approve", "--reviewer", "Reviewer A", "--rationale", "Checked p. 1.")
        before = tree(box.root)
        code, out, err = run(payload)
        self.assertNotEqual(code, 0)
        self.assertTrue(err.startswith("Stopped: "), err)
        self.assertEqual((tree(box.root), out), (before, ""))
        box.roster()  # the documented prerequisite: reviewers add creates the campaign id
        self.assertRegex(findings.campaign_id(box.root), r"^cmp_[0-9a-f]{32}$")
        before = tree(box.root)
        code, first, err = run(payload)
        self.assertEqual(code, 0, err)
        self.assertEqual(tree(box.root), before)
        self.assertFalse((box.root / "private" / "reviews").exists())
        self.assertIn(findings.campaign_id(box.root), first)
        self.assertEqual(run(payload)[1], first)
        self.assertNotEqual(run(payload[:-1] + ("Checked p. 2.",))[1], first)
        box.write(draft_document(summary="A different synthetic summary."))
        self.assertNotEqual(run(payload)[1], first)
        self.assertFalse((box.root / "private" / "reviews").exists())
        self.assertEqual(box.status()["receipts_for_this_content"], 0)

    def test_revision_stales_receipts_and_diff_exit_codes(self):
        box = Sandbox(self)
        box.play(0), box.play(1), box.play(2)
        code, out, _ = run(("finding", "diff", box.before, box.draft, "--directory", box.root))
        self.assertEqual((code, out.strip().splitlines()[-1]), (0, "no differences"))
        box.write(draft_document(summary="The synthetic policy page lists a retention period of 60 days."))
        results = box.play(3)
        self.assertIn("summary", results[0])
        self.assertTrue(results[0].strip().endswith("1 field(s) differ"))
        report = box.status()
        self.assertEqual((report["receipts_for_this_content"], report["stale_receipts"]), (0, 3))
        self.assertTrue({"missing_factual_review", "missing_legal_review", "missing_privacy_review",
                         "need_two_independent_reviewers"} <= set(report["review_blockers"]))
        # fresh reviews of the new text work; the old receipts stay as stale history
        box.review("factual", "approve", "Reviewer A")
        self.assertEqual(box.status()["stale_receipts"], 3)
        self.assertEqual(len(json.loads((box.root / "private" / "reviews" / "syn-guide-1.json").read_text())), 4)
        # fields outside the diff's allowlist still change the reviewed hash
        for label, change in {"agency_response": {"agency_response": {"status": "pending", "prepared_on": "2026-02-20",
                                                                         "reply_by": "2026-03-06"}},
                              "legal basis": {"rule_version": "2026-01-01", "duty": "Synthetic duty.",
                                              "exceptions": "None.", "rule_ids": ["syn-rule"]}}.items():
            with self.subTest(label):
                other = Sandbox(self)
                other.play(0), other.play(1), other.play(2)
                self.assertEqual(other.status()["review_blockers"], [])
                other.write(draft_document(**change))
                code, out, _ = run(("finding", "diff", other.before, other.draft, "--directory", other.root))
                self.assertEqual((code, out.strip().splitlines()[-1]), (0, "no differences"))
                report = other.status()
                self.assertEqual((report["receipts_for_this_content"], report["stale_receipts"]), (0, 3))
                self.assertIn("missing_legal_review", report["review_blockers"])
        self.assertIn("An empty diff does not mean the reviewed content is unchanged", read(GUIDE))

    def test_challenge_is_not_cleared_by_approval(self):
        for decision in ("reject", "changes_requested"):
            with self.subTest(decision=decision):
                box = Sandbox(self)
                box.roster()
                box.review("factual", "approve", "Reviewer A")
                box.review("legal", decision, "Reviewer B", "Legal: needs a citation.")
                box.review("privacy", "approve", "Reviewer B")
                self.assertIn("unresolved_challenge", box.status()["review_blockers"])
                box.review("legal", "approve", "Reviewer B", "Legal: now fine.")
                report = box.status()
                self.assertIn("unresolved_challenge", report["review_blockers"])
                self.assertEqual(report["receipts_for_this_content"], 4)
                box.write(draft_document(summary="Revised synthetic summary."))  # only a revision starts clean
                report = box.status()
                self.assertEqual((report["receipts_for_this_content"], report["stale_receipts"]), (0, 4))
        text = read(GUIDE).lower()
        self.assertIn("stays `unresolved_challenge` even if someone else later approves", text)
        self.assertIn("do not look for a way to remove or overwrite a receipt", text)
        for argv in documented(read(GUIDE)):
            self.assertNotIn("delete", " ".join(argv))

    def test_reply_and_privacy_boundaries(self):
        with self.assertRaises(AssertionError):  # negative control: the no-network guard is really active
            socket.socket()
        pending, received = [json.loads(body) for kind, body in FENCE.findall(section(read(GUIDE), "Worked example (synthetic)"))
                             if kind == "json"]
        cited = {draft_document()["sources"][0]["sha256"]}
        today = date(2026, 3, 15)
        for value in (pending, received):
            self.assertEqual(content._agency_response(value, "agency_response", cited, today), dict(sorted(value.items())))
        self.assertEqual((pending["status"], received["status"]), ("pending", "received"))
        self.assertTrue({"asked_on", "on", "text"} <= set(received) and not {"asked_on", "on"} & set(pending))
        box = Sandbox(self)
        box.roster()
        document = draft_document(body_md=f"{SENTINEL} private body.\n", agency_response=pending)
        box.write(document)
        box.review("factual", "approve", "Reviewer A")
        outputs = [box.ok("finding", "review-status", box.draft, "--directory", box.root),
                   run(("finding", "review-payload", box.draft, "--directory", box.root, "--role", "legal",
                        "--decision", "approve", "--reviewer", "Reviewer B", "--rationale", "Legal: ok."))[1]]
        self.assertEqual(box.status()["receipts_for_this_content"], 1)
        box.write(dict(document, agency_response=received))  # a reply arriving later is reviewed content
        report = box.status()
        self.assertEqual((report["receipts_for_this_content"], report["stale_receipts"]), (0, 1))
        outputs.append(box.ok("finding", "review-status", box.draft, "--directory", box.root))
        for out in outputs:
            self.assertNotIn(SENTINEL, out)
            self.assertNotIn("Synthetic reply text", out)
        text = read(GUIDE)
        lower = text.lower()
        for phrase in ("not that anyone asked the agency", "attributed assertion", "not as a verified fact",
                       "read the whole field", "never the sensitive detail itself", "do not forward, post or quote"):
            self.assertIn(phrase, lower)
        self.assertNotIn(SENTINEL, text)
        for label, pattern in {"email": r"[\w.+-]+@[\w-]+\.[\w.]+", "phone": r"\b\d{3}[-. )]+\d{3}[-. ]\d{4}\b",
                               "long hex": r"[0-9a-fA-F]{32,}", "ip": r"\b\d{1,3}(\.\d{1,3}){3}\b"}.items():
            self.assertIsNone(re.search(pattern, text), label)
        for rationale in re.findall(r"--rationale \"([^\"]*)\"", text):
            self.assertLessEqual(len(rationale), 2000)
            self.assertNotIn("\n", rationale)


if __name__ == "__main__":
    unittest.main()
