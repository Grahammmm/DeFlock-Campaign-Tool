"""DF2207-16: docs/FINDINGS-GUIDE.md. Local links and anchors, every documented command and option against the real
parsers, and the guide's own fenced commands (with its documented manual edits) run in order in temporary campaign
storage with outbound connections blocked. Synthetic data only; nothing is sent, posted, uploaded or deployed.

Run from the repository root: python3 -B -m unittest tests.test_findings_guide_doc -v

Conventions the guide keeps and these tests read: ```sh blocks are the main path, run in document order; ```bash blocks are
alternative or optional forms (checked against the real parser, run by the test that names them); one ```text block is the
synthetic record; ```json blocks are the manual edits (a list is content/agencies.json, an object with "classification"
is the first draft edit, an object with only "summary" is the revision, an object with "rule_ids" is the optional legal
basis placeholder). Every $VARIABLE in a command must have a fixture value below.
"""
import argparse
import contextlib
import hashlib
import importlib
import io
import json
import os
import re
import shlex
import smtplib
import socket
import stat
import sys
import tempfile
import time
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, findings, law  # noqa: E402

GUIDE, README, PUBLICATION = (REPO_ROOT / "docs" / "FINDINGS-GUIDE.md", REPO_ROOT / "README.md",
                              REPO_ROOT / "docs" / "PUBLICATION.md")
NO_NETWORK = AssertionError("no network in these tests")
FENCE = re.compile(r"^```(\w*)\n(.*?)^```$", re.S | re.M)
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
MODULES = {"campaign_tool", "campaign_tool.newsletter", "campaign_tool.law"}
AGENCY = "ex-records-office"


class DocumentError(AssertionError):
    pass


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
    """Problems for every local link in ``text`` (as if it were the file ``source``): the source line, then the cause."""
    problems = []
    blanked = FENCE.sub(lambda m: "\n" * m.group(0).count("\n"), text)  # keeps line numbers
    for number, line in enumerate(blanked.splitlines(), 1):
        for target in LINK.findall(line):
            if re.match(r"[a-z][a-z0-9+.-]*:", target):
                continue  # external: never fetched here
            name, _, fragment = target.partition("#")
            path = (Path(source).parent / name).resolve() if name else Path(source).resolve()
            where = f"{Path(source).name}:{number}: {target}"
            if not path.exists():
                problems.append(f"{where}: no such file")
            elif fragment and (path.suffix != ".md" or fragment not in anchors(read(path))):
                problems.append(f"{where}: no such heading")
    return problems


def sections(text):
    """{heading: body} of the ``## `` sections."""
    parts = re.split(r"^## (.*)$", text, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2]))


def fences(text):
    """(kind, body, first line number) of every fenced block, in document order."""
    return [(m.group(1), m.group(2), text.count("\n", 0, m.start()) + 1) for m in FENCE.finditer(text)]


# --- command helpers --------------------------------------------------------------------------------------------

def command_lines(block):
    """Logical lines of a sh block (backslash continuations joined)."""
    return [line.strip() for line in re.sub(r"\\\n\s*", " ", block).splitlines() if line.strip()]


def documented(text):
    """Every command in the text's sh and bash fences and inline code spans, as (argv, line). A line that is not a
    ``python3 -m campaign_tool...`` command is an error rather than silently skipped."""
    commands = []
    for kind, body, number in fences(text):
        if kind in ("sh", "bash"):
            for line in command_lines(body):
                argv = shlex.split(line)
                if argv[:2] != ["python3", "-m"] or argv[2] not in MODULES:
                    raise DocumentError(f"line {number}: not a campaign_tool command: {line}")
                commands.append((argv, number))
    for span in re.findall(r"`(python3 -m campaign_tool[^`]*)`", FENCE.sub("", text)):
        argv = shlex.split(span)
        if argv[2] not in MODULES:
            raise DocumentError(f"not a campaign_tool command: {span}")
        commands.append((argv, 0))
    return commands


def substitute(arguments, mapping):
    def one(token):
        return re.sub(r"\$([A-Z0-9_]+)", lambda m: str(mapping[m.group(1)]), token)
    return [one(token) for token in arguments]


class Parsed(Exception):
    pass


def entry(name):
    if name == "campaign_tool":
        return cli.main
    return importlib.import_module(name + (".__main__" if name.endswith("newsletter") else "")).main


def invoke(name, arguments):
    """Call the real ``main`` of ``name``; (exit code, stdout text, stderr text)."""
    out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", newline="", write_through=True)
    err, code = io.StringIO(), 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            if name == "campaign_tool":
                with mock.patch.object(sys, "argv", ["campaign_tool", *arguments]):
                    result = cli.main()
            else:
                result = entry(name)(list(arguments))
            code = result if isinstance(result, int) else 0
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return code, out.buffer.getvalue().decode("utf-8"), err.getvalue()


def run(*arguments):
    return invoke("campaign_tool", [str(a) for a in arguments])


def parse_only(name, arguments):
    """The real parse of ``arguments`` (nothing runs): (Namespace, leaf subcommand path); DocumentError if refused."""
    original = argparse.ArgumentParser.parse_args

    def capture(self, args=None, namespace=None):
        raise Parsed(original(self, args, namespace), self)

    err = io.StringIO()
    with mock.patch.object(sys, "argv", ["campaign_tool", *arguments]), \
            mock.patch.object(argparse.ArgumentParser, "parse_args", capture), contextlib.redirect_stderr(err):
        try:
            entry(name)() if name == "campaign_tool" else entry(name)(list(arguments))
        except Parsed as parsed:
            namespace, parser = parsed.args
            return namespace, leaf_path(parser, list(arguments))
        except SystemExit:
            raise DocumentError(err.getvalue().strip().splitlines()[-1] if err.getvalue() else "refused") from None
    raise DocumentError("parser returned without parsing")


def leaf_path(parser, tokens):
    path = []
    while True:
        action = next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)
        if action is None or not tokens or tokens[0] not in action.choices:
            return tuple(path)
        parser = action.choices[tokens[0]]
        path.append(tokens[0])
        tokens = tokens[1:]


def first_json(text):
    """The first JSON document at the start of a command's output (a status line may follow it)."""
    return json.JSONDecoder().raw_decode(text.lstrip())[0]


def tree(root):
    root = Path(root)
    return {str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None) for p in sorted(root.rglob("*"))}


def settle_clock():
    """Avoid running one walk across UTC midnight, where "today" would change between two commands."""
    now = datetime.now(timezone.utc)
    if now.hour == 23 and now.minute == 59 and now.second >= 55:
        time.sleep(7)


def match(*words):
    return lambda arguments: arguments[:len(words)] == list(words)


class Walk:
    """The guide's own fenced commands run in document order against a synthetic campaign in temporary storage, with the
    guide's manual edits applied where the guide puts them. ``advance`` stops after the first command that matches."""

    def __init__(self, case):
        settle_clock()
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-findings-guide-")
        case.addCleanup(temporary.cleanup)
        self.case, self.base = case, Path(temporary.name).resolve()
        self.today = datetime.now(timezone.utc).date()
        self.root = self.base / "campaign"
        self.mapping = {"CAMPAIGN": self.root, "RECORD": self.base / "record.txt", "TODAY": self.today,
                        "REPLY_BY": self.today + timedelta(days=14), "PREVIEW_DIR": self.base / "preview",
                        "PREVIEW_DIR_2": self.base / "preview-2", "PACKET_DIR": self.base / "packet",
                        "PACKET_DIR_2": self.base / "packet-2",
                        "SEARCH_LOG": self.base / "search-log.csv", "SUMMARY": self.base / "summary.json"}
        self.draft = self.revised = self.rereviewed = None
        self.reviews, self.history = [], []
        self.guide = read(GUIDE)
        self.pending = self.units()

    # fixture access to the guide's fragments
    def fragment(self, test):
        found = [json.loads(body) for kind, body, _ in fences(self.guide) if kind == "json" and test(json.loads(body))]
        self.case.assertEqual(len(found), 1)
        return found[0]

    def edit_fragment(self):
        return self.fragment(lambda v: isinstance(v, dict) and "classification" in v)

    def revision_fragment(self):
        return self.fragment(lambda v: isinstance(v, dict) and list(v) == ["summary"])

    def rule_fragment(self):
        return self.fragment(lambda v: isinstance(v, dict) and "rule_ids" in v)

    def units(self):
        for kind, body, number in fences(self.guide):
            if kind == "text":
                yield "record", body, number
            elif kind == "json":
                yield "json", json.loads(body), number
            elif kind == "sh":
                for line in command_lines(body):
                    argv = shlex.split(line)
                    if argv[:3] != ["python3", "-m", "campaign_tool"] and argv[2] != "campaign_tool.newsletter":
                        raise DocumentError(f"line {number}: not a campaign_tool command: {line}")
                    yield "command", argv, number

    def update_draft(self, **changes):
        document = json.loads(self.draft.read_text(encoding="utf-8"))
        document.update(changes)
        self.draft.write_text(json.dumps(document, indent=2), encoding="utf-8")

    def apply(self, value):
        """Apply a documented manual edit to the draft (the guide's replacements, exactly)."""
        document = json.loads(self.draft.read_text(encoding="utf-8"))
        for key, new in value.items():
            if key == "sources":
                for index, part in enumerate(new):
                    document["sources"][index].update(part)
            else:
                document[key] = new
        self.draft.write_text(json.dumps(document, indent=2), encoding="utf-8")

    def fill_site(self):
        """The guide tells the reader to replace content/site.json's [EDIT ME] lines and add a base_url."""
        path = self.root / "content" / "site.json"
        site = json.loads(path.read_text(encoding="utf-8"))
        for key, text in site.items():
            if isinstance(text, str) and "[EDIT ME" in text:
                site[key] = re.sub(r"\[EDIT ME: [^\]]*\]", "Practice text.", text)
        site["base_url"] = "https://campaign.example.invalid"
        path.write_text(json.dumps(site, indent=2), encoding="utf-8")

    def advance(self, stop=None):
        """Run units until the command ``stop(args)`` matches (args after ``python3 -m campaign_tool``); the exit code,
        stdout and stderr of that command. With no ``stop``, run to the end of the guide. A guide without the command is
        a failure, never a skip."""
        last = None
        for kind, value, number in self.pending:
            if kind == "record":
                self.mapping["RECORD"].write_text(value, encoding="utf-8")
            elif kind == "json":
                if isinstance(value, list):
                    (self.root / "content" / "agencies.json").write_text(json.dumps(value), encoding="utf-8")
                    self.fill_site()
                elif "classification" in value or list(value) == ["summary"]:
                    if list(value) == ["summary"]:
                        self.revised = True
                    self.apply(value)
                else:
                    self.case.assertIn("rule_ids", value)  # the optional legal-basis placeholder: never applied
            else:
                last = self.execute(value, number)
                if stop is not None and stop(last[3]):
                    return last[:3]
        if stop is not None:
            raise DocumentError("the guide has no such command")
        return last[:3] if last else None

    def execute(self, argv, number):
        arguments = substitute(argv[3:], self.mapping)
        if arguments[:2] == ["finding", "correct"] and "--from" in arguments and self.revised and not self.rereviewed:
            self.rereviewed = True  # "have both reviewers record fresh receipts (the review lines of step 7 again)"
            self.case.assertTrue(self.reviews)
            for again in self.reviews:
                self.case.assertEqual(invoke(argv[2], again)[0], 0)
        code, out, err = invoke(argv[2], arguments)
        expected = 3 if arguments[:2] == ["finding", "diff"] and self.revised else 0
        self.case.assertEqual(code, expected, f"guide line {number}: {' '.join(arguments)}\n{out}\n{err}")
        self.history.append((arguments, code, out))
        if arguments[:1] == ["status"] and "--list" in arguments:
            self.mapping["PREFIX"] = json.loads(out)["receipts"][0]["sha256_prefix"]
        elif arguments[:2] == ["finding", "new"] and "--gap" not in arguments:
            made = json.loads(out.splitlines()[0])
            self.draft, self.mapping["DRAFT"] = Path(made["written"]), Path(made["written"])
            self.mapping["FINDING_ID"] = made["id"]
        elif arguments[:2] == ["finding", "review"]:
            self.reviews.append(arguments)
        return code, out, err, arguments

    # fixture conveniences
    def ok(self, *arguments):
        code, out, err = run(*arguments)
        self.case.assertEqual(code, 0, err or out)
        return out

    def refused(self, *arguments, watch=("content", "public", "kit")):
        before = {k: v for k, v in tree(self.root).items() if k.split("/")[0] in watch}
        code, out, err = run(*arguments)
        self.case.assertNotEqual(code, 0, out)
        self.case.assertTrue(err.startswith("Stopped: "), err)
        self.case.assertEqual({k: v for k, v in tree(self.root).items() if k.split("/")[0] in watch}, before)
        return err

    def status(self):
        return json.loads(self.ok("finding", "review-status", self.draft, "--directory", self.root))

    def review(self, role, decision, reviewer, rationale="Checked synthetic p. 1."):
        return run("finding", "review", self.draft, "--directory", self.root, "--role", role, "--decision", decision,
                   "--reviewer", reviewer, "--rationale", rationale)

    def slug(self):
        return json.loads(self.draft.read_text(encoding="utf-8"))["slug"]

    def public_state(self):
        return {k: v for k, v in tree(self.root).items() if k.split("/")[0] in ("public", "content")}


class GuideTests(unittest.TestCase):
    def setUp(self):
        umask = os.umask(0o077)
        self.addCleanup(os.umask, umask)
        self.calls = []
        patches = [mock.patch.object(socket, "socket", side_effect=NO_NETWORK),
                   mock.patch.object(socket, "create_connection", side_effect=NO_NETWORK),
                   mock.patch.object(socket, "getaddrinfo", side_effect=NO_NETWORK),
                   mock.patch.object(urllib.request, "urlopen", side_effect=NO_NETWORK),
                   mock.patch.object(smtplib.SMTP, "__init__", side_effect=NO_NETWORK)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    # --- 1 -------------------------------------------------------------------------------------------------------
    def test_task_order_and_reference_links(self):
        text = read(GUIDE)
        self.assertEqual(check_links(GUIDE, text), [])
        self.assertEqual(check_links(README, read(README)), [])
        headings = re.findall(r"^## (\d+)\. (.*)$", text, re.M)
        self.assertEqual([int(n) for n, _ in headings], list(range(1, 12)))
        keywords = ["register the evidence", "start the finding", "write the finding", "check the wording",
                    "outside-review packet", "right of reply", "assign reviewers", "publish locally", "correct",
                    "social, press and newsletter", "withdraw"]
        for (_, title), keyword in zip(headings, keywords):
            self.assertIn(keyword, title.lower())
        order = [text.index(f"## {n}. ") for n, _ in headings]
        self.assertEqual(order, sorted(order))
        self.assertLess(text.index("## Before you start"), text.index("## Practice setup"))
        self.assertLess(text.index("## Practice setup"), order[0])
        required = {1: ["before-publishing-evidence-check"], 2: ["offline-review-receipts-cli",
                    "drafting-a-gap-finding-from-a-request"], 3: ["offline-review-receipts-cli"],
                    4: ["offline-review-receipts-cli", "before-publishing-evidence-check"],
                    5: ["offline-review-receipts-cli"], 6: ["agency-right-of-reply"],
                    7: ["reviewer-roster-role-assignment-not-authentication", "not-done"],
                    8: ["offline-review-receipts-cli", "before-publishing-evidence-check",
                        "publication-gate-one-check-at-every-publishing-path"],
                    9: ["offline-review-receipts-cli", "corrections-and-withdrawal"],
                    10: ["social-drafts-never-posted", "press-drafts", "propose-approve-execute"],
                    11: ["offline-review-receipts-cli", "corrections-and-withdrawal", "not-done"]}
        parts = sections(text)
        for number, title in headings:
            body = parts[f"{number}. {title}"]
            for fragment in required[int(number)]:
                self.assertIn(f"](PUBLICATION.md#{fragment})", body, f"task {number}")
        self.assertIn("(SITE-CONTENT.md#adding-a-source-record)", parts["1. Register the evidence"])
        self.assertIn("(LAW-PACKAGES.md#effective-date-discipline)", parts["4. Check the wording and the law, then preview"])
        self.assertIn("(NEWSLETTER.md#drafts)", parts["10. Draft the social, press and newsletter copies"])
        # the reviewer side is linked, not copied; this guide has no role walkthroughs
        self.assertIn("(REVIEW-GUIDE.md)", text)
        self.assertFalse(re.search(r"^## (Factual|Legal|Privacy) reviewer", text, re.M))
        for phrase in ("local content", "local site", "on the internet", "nothing here is legal advice"):
            self.assertTrue(phrase in text.lower(), phrase)
        self.assertIn("(docs/FINDINGS-GUIDE.md)", read(README))
        for label, mutated in {
                "missing file": text.replace("(REVIEW-GUIDE.md)", "(NO-SUCH-FILE.md)", 1),
                "missing fragment": text.replace("#agency-right-of-reply", "#no-such-heading", 1),
                "link to a missing guide": text + "\n[x](NO-SUCH-GUIDE.md)\n"}.items():
            with self.subTest(label):
                problems = check_links(GUIDE, mutated)
                self.assertEqual(len(problems), 1)
                self.assertRegex(problems[0], r"^FINDINGS-GUIDE\.md:\d+: ")
        broken = read(README).replace("(docs/FINDINGS-GUIDE.md)", "(docs/FINDINGS-GUIDES.md)")
        self.assertEqual(len(check_links(README, broken)), 1)

    # --- 2 -------------------------------------------------------------------------------------------------------
    def test_every_documented_command_and_option_is_real(self):
        text = read(GUIDE)
        mapping = Walk(self).mapping
        mapping.update(PREFIX="0123456789ab", DRAFT="d.json", FINDING_ID="fnd_000000000001")
        commands = documented(text)
        leaves = set()
        for argv, number in commands:
            namespace, leaf = parse_only(argv[2], substitute(argv[3:], mapping))
            leaves.add((argv[2], leaf))
            for forbidden in ("--record-sent", "sent", "--publish", "--online", "--signature"):
                self.assertNotIn(forbidden, argv, f"line {number}")
            if leaf == ("finding", "diff"):
                self.assertEqual(len(namespace.inputs), 1 if namespace.from_draft else 2, argv)
        self.assertEqual(leaves, {
            ("campaign_tool", ("init",)), ("campaign_tool", ("content", "init")), ("campaign_tool", ("ingest",)),
            ("campaign_tool", ("status",)), ("campaign_tool", ("sources", "add")),
            ("campaign_tool", ("sources", "list")), ("campaign_tool", ("finding", "new")),
            ("campaign_tool", ("finding", "review-status")), ("campaign_tool", ("finding", "preview")),
            ("campaign_tool", ("finding", "packet")), ("campaign_tool", ("finding", "reply-request")),
            ("campaign_tool", ("finding", "reply")), ("campaign_tool", ("finding", "reviewers", "add")),
            ("campaign_tool", ("finding", "reviewers", "list")), ("campaign_tool", ("finding", "reviewers", "retire")),
            ("campaign_tool", ("finding", "assign")),
            ("campaign_tool", ("finding", "review")), ("campaign_tool", ("finding", "publish")),
            ("campaign_tool", ("content-check",)), ("campaign_tool", ("evidence",)),
            ("campaign_tool", ("kit-search-log",)), ("campaign_tool", ("build",)),
            ("campaign_tool", ("finding", "diff")), ("campaign_tool", ("finding", "correct")),
            ("campaign_tool", ("social",)), ("campaign_tool", ("press", "draft")),
            ("campaign_tool.newsletter", ()), ("campaign_tool", ("finding", "withdraw")),
            ("campaign_tool.law", ("show",))})
        flags = {token for argv, _ in commands for token in argv if token.startswith("--")}
        self.assertTrue({"--gap", "--request", "--by", "--asked-on", "--from", "--from-content", "--site",
                         "--approved-by"} <= flags)
        for module, leaf in sorted(leaves):  # --help of every documented command exits 0 and prints usage
            code, out, err = invoke(module, [*leaf, "--help"])
            self.assertEqual(code, 0, f"{module} {leaf}: {err}")
            self.assertIn("usage:", out)
        sample = next(argv for argv, _ in commands if argv[3:5] == ["finding", "review"])
        newsletter = next(argv for argv, _ in commands if argv[2] == "campaign_tool.newsletter")
        for label, (module, bad) in {
                "unknown child command": ("campaign_tool", ["finding", "frobnicate"]),
                "an invented law-check": ("campaign_tool", ["finding", "law-check", "d.json", "--directory", "D"]),
                "an invented wording-check": ("campaign_tool", ["finding", "wording-check", "d.json"]),
                "unknown reviewers child": ("campaign_tool", ["finding", "reviewers", "remove", "--directory", "D"]),
                "misspelled option": ("campaign_tool", [t.replace("--reviewer", "--revewer") for t in sample[3:]]),
                "missing required option": ("campaign_tool", [t for i, t in enumerate(sample[3:])
                                                              if t != "--role" and sample[3:][i - 1] != "--role"]),
                "unknown top-level command": ("campaign_tool", ["frobnicate"]),
                "unknown newsletter option": ("campaign_tool.newsletter", [*newsletter[3:], "--mail-it"]),
                "conflicting newsletter modes": ("campaign_tool.newsletter",
                                                 ["--directory", "D", "--from-content", "--record-sent", "2026-01-01"]),
                "unknown law child": ("campaign_tool.law", ["frobnicate"])}.items():
            with self.subTest(label), self.assertRaises(DocumentError):
                parse_only(module, substitute(bad, mapping))
        with self.assertRaises(DocumentError):
            documented("```sh\nssh-keygen -Y sign -f KEY payload\n```\n")
        with self.assertRaises(DocumentError):
            documented("```sh\npython3 -m campaign_tool finding review-status X --directory D\nrm -rf x\n```\n")
        for label, mutated in {
                "a misspelled verb in the guide": text.replace("campaign_tool finding packet", "campaign_tool finding packit", 1),
                "a misspelled flag in the guide": text.replace("--asked-on \"$TODAY\"", "--asked-in \"$TODAY\"", 1),
                "an invented verb in the guide": text.replace("campaign_tool finding preview", "campaign_tool finding law-check", 1),
                "a dropped required flag in the guide": text.replace(" --role factual --decision", " --decision", 1)}.items():
            with self.subTest(label), self.assertRaises(DocumentError):
                for argv, _ in documented(mutated):
                    parse_only(argv[2], substitute(argv[3:], mapping))
        used = set(re.findall(r"\$([A-Z0-9_]+)", "".join(body for kind, body, _ in fences(text) if kind in ("sh", "bash"))))
        self.assertLessEqual(used - {"PREFIX", "DRAFT", "FINDING_ID"}, set(mapping))

    # --- 3 -------------------------------------------------------------------------------------------------------
    def test_worked_example_from_ingested_source_to_local_page(self):
        walk = Walk(self)
        walk.advance(match("ingest"))
        self.assertTrue((walk.root / "private" / "ledger.sqlite").is_file())
        walk.advance(match("sources", "add"))
        library = json.loads(read(walk.root / "content" / "sources.json"))
        self.assertEqual([(e["agency_id"], e["fragment"]) for e in library], [(AGENCY, "example-retention")])
        record = (walk.base / "record.txt").read_bytes()
        digest = hashlib.sha256(record).hexdigest()
        self.assertEqual(library[0]["sha256"], digest)
        self.assertTrue(digest.startswith(walk.mapping["PREFIX"]))
        self.assertNotIn(record.splitlines()[0], (walk.root / "content" / "sources.json").read_bytes())
        walk.advance(match("finding", "new"))
        draft = json.loads(read(walk.draft))
        self.assertEqual((draft["state"], draft["author"], draft["published_at"]), ("draft", "Author Z", None))
        self.assertEqual(stat.S_IMODE(walk.draft.stat().st_mode), 0o600)
        self.assertEqual(walk.draft.parent, walk.root / "private" / "drafts")
        generated = {k: draft[k] for k in ("id", "slug")} | {"sha256": draft["sources"][0]["sha256"]}
        self.assertIn("[EDIT ME", walk.draft.read_text())
        walk.refused("finding", "preview", walk.draft, "--directory", walk.root, "--out", walk.base / "early")
        self.assertFalse((walk.base / "early").exists())
        walk.advance(match("finding", "review-status"))  # the guide's edit has been applied by now
        edited = json.loads(read(walk.draft))
        for key, value in walk.edit_fragment().items():
            if key != "sources":
                self.assertEqual(edited[key], value)
        self.assertEqual(edited["sources"][0]["locator"], "p. 1")
        self.assertEqual({k: edited[k] for k in ("id", "slug")} | {"sha256": edited["sources"][0]["sha256"]}, generated)
        self.assertEqual([b for b in walk.status()["review_blockers"] if b.startswith("edit_marker")], [])
        baseline = walk.public_state()
        walk.advance(match("finding", "preview"))
        preview = walk.mapping["PREVIEW_DIR"]
        page = (preview / "findings" / f"{generated['slug']}.html").read_text(encoding="utf-8")
        self.assertIn("Draft - not published", page)
        self.assertIn("noindex", page)
        self.assertFalse(preview.resolve().is_relative_to(walk.root))
        self.assertEqual(walk.public_state(), baseline)
        self.assertFalse((walk.root / "public").exists())
        walk.advance(match("finding", "packet"))
        packet = walk.mapping["PACKET_DIR"]
        self.assertFalse(packet.resolve().is_relative_to(walk.root))
        self.assertEqual((packet / "records" / digest).read_bytes(), record)  # confidential: holds the original
        self.assertEqual(stat.S_IMODE(packet.stat().st_mode), 0o700)
        self.assertTrue({"draft.json", "MANIFEST.sha256", "reviews.json"} <= {p.name for p in packet.iterdir()})
        self.assertEqual(walk.public_state(), baseline)
        walk.advance(match("finding", "reply"))
        walk.advance(match("finding", "assign"))
        self.assertTrue({"missing_factual_review", "missing_legal_review", "missing_privacy_review",
                         "need_two_independent_reviewers"} <= set(walk.status()["review_blockers"]))
        walk.advance(match("finding", "review"))
        self.assertIn("need_two_independent_reviewers", walk.status()["review_blockers"])
        walk.advance(lambda a: a[:2] == ["finding", "review"] and "privacy" in a)
        report = walk.status()
        self.assertEqual((report["review_blockers"], report["receipts_for_this_content"]), ([], 3))
        self.assertEqual({r: s["state"] for r, s in report["assignments"].items()},
                         {r: "receipt_recorded" for r in findings.ROLES})
        before = walk.public_state()
        self.assertEqual(before, baseline)  # nothing public changed up to the moment of publication
        walk.advance(match("finding", "publish"))
        log = [json.loads(line) for line in (walk.root / "private" / "publication-log.jsonl").read_text().splitlines()]
        self.assertEqual([(e["action"], e["id"], e["approved_by"]) for e in log],
                         [("publish", generated["id"], "Organizer Y")])
        self.assertNotEqual(log[0]["approved_by"], edited["author"])
        published = json.loads(read(walk.root / "content" / "findings" / f"{generated['slug']}.json"))
        self.assertEqual((published["state"], published["id"]), ("published", generated["id"]))
        self.assertFalse((walk.root / "public").exists())  # publish alone builds nothing
        walk.advance(match("build"))
        local = (walk.root / "public" / "findings" / f"{generated['slug']}.html").read_text(encoding="utf-8")
        self.assertIn("p. 1", local)
        self.assertIn(walk.mapping["PREFIX"], local)
        self.assertIn("Agency response", local)
        self.assertNotIn("Draft - not published", local)
        self.assertNotIn(record.splitlines()[0].decode(), local)
        self.assertIn("0 problem(s)", run("content-check", "--directory", walk.root)[1])

    # --- 4 -------------------------------------------------------------------------------------------------------
    def test_markers_wording_and_law_limits(self):
        walk = Walk(self)
        walk.advance(match("finding", "new"))
        self.assertIn("edit_marker:summary", walk.status()["review_blockers"])
        for role, who in (("factual", "Reviewer A"), ("legal", "Reviewer B"), ("privacy", "Reviewer B")):
            walk.ok("finding", "reviewers", "add", "--directory", walk.root, "--name", who, "--role", role)
        for role, who in (("factual", "Reviewer A"), ("legal", "Reviewer B"), ("privacy", "Reviewer B")):
            self.assertEqual(walk.review(role, "approve", who)[0], 0)
        # an untouched skeleton cannot be published, even with receipts, and nothing is written
        err = walk.refused("finding", "publish", walk.draft, "--directory", walk.root, "--approved-by", "Organizer Y")
        self.assertIn("edit_marker", err)
        self.assertEqual(os.listdir(walk.root / "content" / "findings"), [])
        walk.apply(walk.edit_fragment())
        # a legal conclusion in the summary stays a blocker for documented_fact
        wording = {"summary": "The office violated the law by keeping plate images 30 days."}
        walk.apply(wording)
        for who, role in (("Reviewer A", "factual"), ("Reviewer B", "legal"), ("Reviewer B", "privacy")):
            self.assertEqual(walk.review(role, "approve", who)[0], 0)
        report = walk.status()
        self.assertIn("legal_conclusion_wording_in_summary", report["review_blockers"])
        self.assertEqual(json.loads(read(walk.draft))["classification"], "documented_fact")
        err = walk.refused("finding", "publish", walk.draft, "--directory", walk.root, "--approved-by", "Organizer Y")
        self.assertIn("legal_conclusion_wording_in_summary", err)
        self.assertNotIn("violated", err)  # the field name only, never the words
        walk.apply(walk.edit_fragment())
        self.assertNotIn("legal_conclusion_wording_in_summary", walk.status()["review_blockers"])
        # private preview refuses an unknown rule and one not in force on the event date
        package = law.load_package("us-ca")
        in_force = next(r for r in package["rules"]
                        if r["effective_to"] is None and "2012-01-01" < r["effective_from"] <= "2026-02-10")
        status_before = law.package_status("us-ca")
        walk.apply({"rule_ids": ["no-such-rule"]})
        self.assertIn("no-such-rule", walk.refused("finding", "preview", walk.draft, "--directory", walk.root, "--out",
                                                    walk.base / "p-unknown"))
        self.assertFalse((walk.base / "p-unknown").exists())
        walk.apply({"rule_ids": [in_force["rule_id"]], "event_date": "2010-01-01"})
        err = walk.refused("finding", "preview", walk.draft, "--directory", walk.root, "--out", walk.base / "p-early")
        self.assertIn("not in force", err)
        self.assertFalse((walk.base / "p-early").exists())
        fragment = walk.rule_fragment()
        self.assertEqual(sorted(fragment), ["duty", "exceptions", "rule_ids", "rule_version"])
        walk.apply(dict(fragment, rule_ids=[in_force["rule_id"]], event_date="2026-02-10"))
        walk.ok("finding", "preview", walk.draft, "--directory", walk.root, "--out", walk.base / "p-ok")
        html = (walk.base / "p-ok" / "findings" / f"{walk.slug()}.html").read_text(encoding="utf-8")
        self.assertIn("draft, not legally reviewed", html)
        self.assertEqual(law.package_status("us-ca"), status_before)
        self.assertEqual(status_before[0], "draft")  # the shipped package is not promoted by any of this
        # content-check reads published content; it does not look at private drafts
        self.assertEqual(run("content-check", "--directory", walk.root)[0], 0)
        text = read(GUIDE)
        self.assertIn("`content-check` reads published content, not private drafts", text)
        self.assertIn("There is no separate wording-check or law-check command", text)
        self.assertIn("The shipped package is a draft", text)

    # --- 5 -------------------------------------------------------------------------------------------------------
    def test_assignments_and_reply_need_current_review(self):
        walk = Walk(self)
        walk.advance(match("finding", "packet"))
        draft_id = walk.mapping["FINDING_ID"]
        text = lambda: json.loads(read(walk.draft))  # noqa: E731
        # a reply cannot be recorded before a request was prepared
        err = walk.refused("finding", "reply", walk.draft, "--directory", walk.root, "--status", "declined", "--on",
                           walk.today, "--asked-on", walk.today)
        self.assertIn("prepare a reply request first", err)
        walk.advance(match("finding", "reply-request"))
        notice = walk.root / "kit" / "replies" / f"{draft_id}.md"
        body = notice.read_text(encoding="utf-8")
        self.assertIn("Nothing was sent", body)
        self.assertIsNone(re.search(r"[\w.+-]+@[\w-]+\.[\w.]+|[0-9a-f]{40,}", body))
        self.assertEqual(text()["agency_response"], {"status": "pending", "prepared_on": str(walk.today),
                                                       "reply_by": str(walk.mapping["REPLY_BY"])})
        self.assertEqual(sorted(p.name for p in (walk.root / "kit").iterdir()), ["replies"])  # nothing sent or outboxed
        self.assertIn("already exists", walk.refused("finding", "reply-request", walk.draft, "--directory", walk.root,
                                                     "--by", walk.mapping["REPLY_BY"]))
        # silence cannot be recorded before the deadline, nor asking before preparation
        self.assertIn("deadline", walk.refused("finding", "reply", walk.draft, "--directory", walk.root, "--status",
                                               "no_response", "--on", walk.today, "--asked-on", walk.today))
        self.assertIn("--asked-on", walk.refused("finding", "reply", walk.draft, "--directory", walk.root, "--status",
                                                 "declined", "--on", walk.today, "--asked-on",
                                                 walk.today - timedelta(days=1)))
        for role, who in (("factual", "Reviewer A"), ("legal", "Reviewer B"), ("privacy", "Reviewer B")):
            walk.ok("finding", "reviewers", "add", "--directory", walk.root, "--name", who, "--role", role)
            self.assertEqual(walk.review(role, "approve", who)[0], 0)
        self.assertEqual(walk.status()["review_blockers"], [])
        # a pending reply is only prepared, never asked: recording asking and the reply is a separate, later step
        walk.advance(match("finding", "reply"))  # received, attested asking: reviewed content changed
        report = walk.status()
        self.assertEqual((report["receipts_for_this_content"], report["stale_receipts"]), (0, 3))
        self.assertIn("need_two_independent_reviewers", report["review_blockers"])
        self.assertEqual(text()["agency_response"]["status"], "received")
        self.assertEqual(text()["agency_response"]["asked_on"], str(walk.today))
        self.assertNotIn("The office says", json.dumps(report))
        self.assertEqual(os.listdir(walk.root / "content" / "findings"), [])
        # assignments alone clear nothing
        walk.advance(match("finding", "assign"))
        self.assertEqual({r: s["state"] for r, s in walk.status()["assignments"].items()}["factual"], "assigned")
        self.assertTrue({"missing_factual_review", "need_two_independent_reviewers"} <= set(walk.status()["review_blockers"]))
        err = walk.refused("finding", "publish", walk.draft, "--directory", walk.root, "--approved-by", "Organizer Y")
        self.assertIn("missing_", err)
        # a prepared (pending) request is never shown on the page, and nothing in publish waits for the deadline
        other = Walk(self)
        other.advance(match("finding", "reply-request"))
        for role, who in (("factual", "Reviewer A"), ("legal", "Reviewer B"), ("privacy", "Reviewer B")):
            other.ok("finding", "reviewers", "add", "--directory", other.root, "--name", who, "--role", role)
            self.assertEqual(other.review(role, "approve", who)[0], 0)
        self.assertEqual(other.status()["review_blockers"], [])
        other.ok("finding", "publish", other.draft, "--directory", other.root, "--approved-by", "Organizer Y")
        other.ok("build", "--directory", other.root)
        shown = (other.root / "public" / "findings" / f"{other.slug()}.html").read_text(encoding="utf-8")
        self.assertNotIn("Agency response", shown)
        self.assertNotIn("pending", shown.lower())
        text = read(GUIDE)
        self.assertIn("Nothing in `finding publish` waits for the deadline", text)
        self.assertIn("a pending request is not shown on the page", text)

    # --- 6 -------------------------------------------------------------------------------------------------------
    def test_gap_is_an_alternative_not_absence_proof(self):
        walk = Walk(self)
        walk.advance(match("sources", "add"))
        gap = next(argv for kind, body, _ in fences(read(GUIDE)) if kind == "bash"
                   for argv in map(shlex.split, command_lines(body)) if "--gap" in argv)
        correspondence = hashlib.sha256((walk.base / "record.txt").read_bytes()).hexdigest()

        def seed(steps, request="main"):
            key = AGENCY if request == "main" else f"{AGENCY}#{request}"
            entry_ = {"sent_on": "2026-01-05", "channel": "email", "draft": f"requests/{AGENCY}.md",
                      "draft_sha256": "a" * 64, "recorded_at": "2026-01-05T00:00:00+00:00", "steps": steps,
                      "correspondence": [correspondence]}
            (walk.root / "kit").mkdir(exist_ok=True)
            (walk.root / "kit" / "sent.json").write_text(json.dumps({"schema_version": 1, "requests": {key: entry_}}))

        def step(on, kind, **more):
            return {"on": on, "kind": kind, "recorded_at": "2026-03-01T00:00:00+00:00", **more}

        arguments = substitute(gap[3:], walk.mapping)
        for label, steps in {"open": [], "acknowledged only": [step("2026-01-20", "acknowledged")],
                             "produced": [step("2026-02-01", "produced", records=["d4" * 32])],
                             "partly produced then closed": [step("2026-02-01", "produced", partial=True,
                                                                  records=["d4" * 32]), step("2026-02-20", "closed")]}.items():
            with self.subTest(label):
                seed(steps)
                err = walk.refused(*arguments, watch=("content", "public"))
                self.assertFalse((walk.root / "private" / "drafts").exists())
                self.assertRegex(err, "still open|produced records")
        seed([step("2026-01-20", "acknowledged"), step("2026-02-10", "denied", withheld_basis=["Synthetic basis"])])
        code, out, err = run(*arguments)
        self.assertEqual(code, 0, err)
        self.assertNotIn("blocker:", out)  # the correspondence record is in the library
        draft = findings.load_draft(Path(json.loads(out.splitlines()[0])["written"]))
        self.assertEqual((draft["classification"], draft["event_date"], draft["state"], draft["author"]),
                         ("information_gap", "2026-02-10", "draft", "Author Z"))
        self.assertIn("denied on 2026-02-10", draft["body_md"])
        self.assertIn("[EDIT ME", draft["summary"])  # the interpretation stays with the author
        for combined in (["--agency", AGENCY], ["--source", walk.mapping["PREFIX"]]):
            with self.subTest(combined=combined):
                before = tree(walk.root)
                code, out, err = run(*arguments, *combined)
                self.assertEqual(code, 2)  # an argument error, not a draft
                self.assertIn("cannot be combined with --agency or --source", err)
                self.assertEqual(tree(walk.root), before)
        # correspondence missing from the library is a printed blocker and a marker, and so blocks publication
        (walk.root / "content" / "sources.json").write_text("[]")
        for folder in (walk.root / "private" / "drafts").glob("*.json"):
            folder.unlink()
        code, out, err = run(*arguments)
        self.assertEqual(code, 0, err)
        self.assertIn("blocker: " + correspondence[:12] + " is not in the source library", out)
        missing = findings.load_draft(Path(json.loads(out.splitlines()[0])["written"]))
        self.assertTrue(any("add these sources before review" in item for item in missing["limitations"]))
        walk.draft = Path(json.loads(out.splitlines()[0])["written"])
        self.assertIn("edit_marker:limitations", walk.status()["review_blockers"])
        self.assertIn("edit_marker", walk.refused("finding", "publish", walk.draft, "--directory", walk.root,
                                                  "--approved-by", "Organizer Y"))
        text = read(GUIDE)
        self.assertIn("does not show that nothing happened", text)
        self.assertIn("Do not combine `--gap` with `--agency` or `--source`", text)

    # --- 7 -------------------------------------------------------------------------------------------------------
    def test_correction_withdrawal_and_private_drafts(self):
        walk = Walk(self)
        walk.advance(match("build"))  # the first build of the published finding
        slug_, finding_id = walk.slug(), walk.mapping["FINDING_ID"]
        published = walk.root / "content" / "findings" / f"{slug_}.json"
        first = json.loads(read(published))
        self.assertEqual((first["corrections"], first["published_at"] is not None), ([], True))
        code, out, err = walk.advance(match("finding", "diff"))
        self.assertEqual(code, 3)  # differences are a result, not a malfunction
        self.assertIn("summary", out)
        walk.advance(match("finding", "review-status"))
        self.assertEqual(walk.status()["receipts_for_this_content"], 0)  # the revision stales the old receipts
        self.assertEqual(walk.status()["stale_receipts"], 3)
        walk.advance(match("finding", "correct"))
        corrected = json.loads(read(published))
        self.assertEqual((corrected["id"], corrected["slug"], corrected["published_at"]),
                         (finding_id, slug_, first["published_at"]))
        self.assertEqual(corrected["summary"], walk.revision_fragment()["summary"])
        self.assertEqual([c["date"] for c in corrected["corrections"]], [str(walk.today)])
        self.assertEqual(run("finding", "diff", finding_id, "--directory", walk.root, "--from", walk.draft)[0], 0)
        walk.advance(match("build"))
        page = (walk.root / "public" / "findings" / f"{slug_}.html").read_text(encoding="utf-8")
        note = corrected["corrections"][0]["note"]
        self.assertIn(note, page)
        self.assertIn(note, (walk.root / "public" / "corrections.html").read_text(encoding="utf-8"))
        walk.advance(match("social"))
        walk.advance(match("press", "draft"))
        walk.advance(lambda a: "--from-content" in a)
        link = f"https://campaign.example.invalid/findings/{slug_}.html"
        social = (walk.root / "kit" / "social" / f"{slug_}.md").read_text(encoding="utf-8")
        press = next((walk.root / "kit" / "press").glob("*.md")).read_text(encoding="utf-8")
        newsletter = (walk.root / "kit" / "newsletter-draft.txt").read_text(encoding="utf-8")
        for draft in (social, press, newsletter):
            self.assertIn(link, draft)
        self.assertIn(f"Corrected {walk.today}.", social)
        self.assertIn(note, press)
        original_line = (walk.base / "record.txt").read_text().splitlines()[0]
        for draft in (social, press, newsletter, page):
            self.assertNotIn(original_line, draft)
            self.assertNotIn(str(walk.root / "private"), draft)
        # a draft you edited is kept; the fresh text goes to .suggested (note-only correction regenerates)
        (walk.root / "kit" / "social" / f"{slug_}.md").write_text(social + "\nMy own edit.\n", encoding="utf-8")
        note_only = next(argv for kind, body, _ in fences(read(GUIDE)) if kind == "bash"
                         for argv in map(shlex.split, command_lines(body)) if argv[3:5] == ["finding", "correct"])
        self.assertEqual(run(*substitute(note_only[3:], walk.mapping))[0], 0)  # note-only: no --from, no text change
        self.assertEqual(json.loads(read(published))["summary"], corrected["summary"])
        out = run("social", "--directory", walk.root)[1]
        self.assertEqual(first_json(out)["kept_edited"], [slug_])
        self.assertIn("My own edit.", (walk.root / "kit" / "social" / f"{slug_}.md").read_text(encoding="utf-8"))
        self.assertTrue(list((walk.root / "kit").glob("social.suggested/*")))
        walk.advance(match("finding", "withdraw"))
        self.assertEqual(os.listdir(walk.root / "content" / "findings"), [])
        kept = json.loads(read(walk.root / "private" / "withdrawn" / f"{slug_}.json"))
        self.assertEqual((kept["id"], kept["slug"]), (finding_id, slug_))
        self.assertIn("reason", kept["withdrawn"])
        self.assertTrue((walk.root / "kit" / "social" / f"{slug_}.md").exists())  # nothing erased silently
        self.assertTrue(next((walk.root / "kit" / "press").glob("*.md")).exists())
        walk.advance(match("build"))
        self.assertFalse((walk.root / "public" / "findings" / f"{slug_}.html").exists())
        self.assertNotIn(slug_, (walk.root / "public" / "sitemap.xml").read_text(encoding="utf-8"))
        self.assertIn(slug_, first_json(run("social", "--directory", walk.root)[1])["stale"])
        # the id and slug stay taken: the withdrawn draft (its receipts are current) cannot be published again
        self.assertEqual(walk.status()["review_blockers"], [])
        self.assertIn("withdrawn", walk.refused("finding", "publish", walk.draft, "--directory", walk.root,
                                                "--approved-by", "Organizer Y", watch=("content",)).lower())

    def test_search_log_start_replays_with_a_synthetic_producer_summary(self):
        walk = Walk(self)
        walk.advance(match("sources", "add"))
        log = walk.mapping["SEARCH_LOG"]
        log.write_text("Timestamp,Search Purpose,Organization,State\n2026-01-05,theft,Org A,CA\n"
                       "2026-01-06,investigation,Org A,CA\n2026-01-07,,Org B,NV\n", encoding="utf-8")
        digest = hashlib.sha256(log.read_bytes()).hexdigest()
        (walk.root / "kit").mkdir(exist_ok=True)
        (walk.root / "kit" / "agencies.json").write_text(json.dumps(
            [{"agency_id": AGENCY, "name": "Example Records Office (fictional)", "kind": "sheriff"}]), encoding="utf-8")
        walk.ok("ingest", "--directory", walk.root, "--file", log, "--source-id", "example-search-log")
        walk.ok("sources", "add", "--directory", walk.root, "--sha256", digest[:12], "--title", "Search log (invented)",
                "--agency-id", AGENCY, "--received-at", "2026-02-10T17:00:00+00:00", "--fragment", "example-log")
        commands = [argv for kind, body, _ in fences(read(GUIDE)) if kind == "bash"
                    for argv in map(shlex.split, command_lines(body)) if "kit-search-log" in argv or "--search-log" in argv]
        self.assertEqual([argv[3] for argv in commands], ["kit-search-log", "finding"])
        code, out, err = run(*substitute(commands[0][3:], walk.mapping))
        self.assertEqual(code, 0, err)
        walk.mapping["SUMMARY"].write_text(out, encoding="utf-8")
        code, out, err = run(*substitute(commands[1][3:], walk.mapping))
        self.assertEqual(code, 0, err)
        draft = findings.load_draft(Path(json.loads(out.splitlines()[0])["written"]))
        self.assertEqual([s["sha256"] for s in draft["sources"]], [digest])
        self.assertIn("| Org A | CA | 2 |", draft["body_md"])
        self.assertTrue(draft["summary"].startswith("[EDIT ME: "))
        self.assertEqual((draft["author"], draft["state"]), ("Author Z", "draft"))
        self.assertFalse((walk.root / "public").exists() and any((walk.root / "public").rglob("fnd_*")))

    # --- 8 -------------------------------------------------------------------------------------------------------
    def test_challenges_privacy_and_network_guard(self):
        with self.assertRaises(AssertionError):  # negative control: the no-network guard is really active
            socket.socket()
        with self.assertRaises(AssertionError):
            urllib.request.urlopen("https://campaign.example.invalid")
        walk = Walk(self)
        walk.advance(match("finding", "reviewers", "list"))
        # the author cannot review; one person cannot cover every role
        walk.ok("finding", "reviewers", "add", "--directory", walk.root, "--name", "Reviewer B", "--role", "factual",
                "--role", "legal", "--role", "privacy")
        walk.ok("finding", "reviewers", "add", "--directory", walk.root, "--name", "Author Z", "--role", "factual")
        self.assertNotEqual(walk.review("factual", "approve", "Author Z")[0], 0)
        self.assertNotEqual(walk.review("factual", "approve", " author  z ")[0], 0)
        for role in findings.ROLES:
            self.assertEqual(walk.review(role, "approve", "Reviewer B")[0], 0)
        report = walk.status()
        self.assertIn("need_two_independent_reviewers", report["review_blockers"])
        before = walk.public_state()
        self.assertIn("need_two_independent_reviewers", walk.refused(
            "finding", "publish", walk.draft, "--directory", walk.root, "--approved-by", "Organizer Y"))
        self.assertEqual(walk.public_state(), before)  # a refused gate leaves public bytes unchanged
        self.assertNotEqual(run("finding", "publish", walk.draft, "--directory", walk.root, "--approved-by",
                                "Author Z")[0], 0)  # the publisher is not the author either
        self.assertEqual(walk.review("factual", "changes_requested", "Reviewer B", "Needs a second page cited.")[0], 0)
        walk.ok("finding", "reviewers", "add", "--directory", walk.root, "--name", "Reviewer A", "--role", "factual")
        self.assertEqual(walk.review("factual", "approve", "Reviewer A")[0], 0)
        report = walk.status()
        self.assertIn("unresolved_challenge", report["review_blockers"])  # a later approval does not clear it
        self.assertEqual(walk.public_state(), before)
        self.assertEqual(os.listdir(walk.root / "content" / "findings"), [])
        # a packet holds the originals: private folder, refused inside content/, public/ or private/
        for inside in ("public", "content", "private"):
            self.assertNotEqual(run("finding", "packet", walk.draft, "--directory", walk.root, "--out",
                                    walk.root / inside / "packet")[0], 0)
            self.assertFalse((walk.root / inside / "packet").exists())
        packet = walk.base / "packet-private"
        walk.ok("finding", "packet", walk.draft, "--directory", walk.root, "--out", packet)
        record = (walk.base / "record.txt").read_bytes()
        self.assertEqual(stat.S_IMODE(packet.stat().st_mode), 0o700)
        self.assertEqual((packet / "records" / hashlib.sha256(record).hexdigest()).read_bytes(), record)
        # private sentinels stay out of the public tree
        walk.ok("build", "--directory", walk.root)
        for path in (walk.root / "public").rglob("*"):
            if path.is_file():
                self.assertNotIn(record.splitlines()[0], path.read_bytes(), path.name)
        # the guide's documented commands cannot send, post or deploy, and the network guard saw nothing
        text = read(GUIDE)
        for argv, _ in documented(text):
            for forbidden in ("kit-copy", "post_social", "outbox", "deploy", "--record-sent", "kit-sent"):
                self.assertNotIn(forbidden, argv)
        self.assertIn("The tool never zips, uploads or sends it", text)
        self.assertIn("Do not hand it to the agency", text)
        for label, pattern in {"email": r"[\w.+-]+@[\w-]+\.[\w.]+", "phone": r"\b\d{3}[-. )]+\d{3}[-. ]\d{4}\b",
                               "long hex": r"[0-9a-fA-F]{32,}", "ip": r"\b\d{1,3}(\.\d{1,3}){3}\b"}.items():
            self.assertIsNone(re.search(pattern, text), label)
        for rationale in re.findall(r"--rationale \"([^\"]*)\"", text):
            self.assertLessEqual(len(rationale), 2000)

    def test_retire_guide_examples_parse_and_replay_separately(self):
        """DF1109B-14: the optional retirement block of task 7, run after the guide's own roster and assignment
        lines against a synthetic campaign (the main walk never runs it)."""
        guide = read(GUIDE)
        retire = next([shlex.split(line) for line in command_lines(body)] for kind, body, _ in fences(guide)
                      if kind == "bash" and "retire" in body)
        setup = [shlex.split(line) for kind, body, _ in fences(guide) if kind == "sh" and "reviewers add" in body
                 for line in command_lines(body)]
        self.assertEqual([argv[3:6] for argv in setup], [["finding", "reviewers", "add"]] * 2
                         + [["finding", "reviewers", "list"]] + [["finding", "assign", "$DRAFT"]] * 3)
        with tempfile.TemporaryDirectory(prefix="synthetic-findings-guide-retire-") as temporary:
            base = Path(temporary).resolve()
            mapping = {"CAMPAIGN": base / "campaign", "DRAFT": base / "draft.json",  # a day both clocks accept
                       "TODAY": min(datetime.now().date(), datetime.now(timezone.utc).date())}
            self.assertEqual(run("init", "--directory", mapping["CAMPAIGN"], "--name", "Synthetic", "--county",
                                 "Synthetic County", "--state", "ca")[0], 0)
            mapping["DRAFT"].write_text(json.dumps({
                "id": "syn-guide-1", "title": "Synthetic", "slug": "synthetic", "summary": "Synthetic.",
                "classification": "documented_fact", "sources": [{"locator": "p. 1", "sha256": "a" * 64}],
                "state": "draft", "author": "Author Z", "limitations": "None.", "counterevidence": "None."}),
                encoding="utf-8")
            for argv in setup + retire:
                code, out, err = run(*substitute(argv[3:], mapping))
                self.assertEqual(code, 0, f"{argv}: {err}")
            roster = {e["name"]: e for e in findings.list_reviewers(mapping["CAMPAIGN"])["reviewers"]}
            self.assertEqual(roster["Reviewer B"]["roles"], ["legal", "privacy"])  # nothing removed
            self.assertEqual(roster["Reviewer B"]["retired_on"], {"privacy": str(mapping["TODAY"])})  # legal reinstated
            self.assertNotIn("retired_on", roster["Reviewer A"])
            draft = findings.load_draft(mapping["DRAFT"])
            states = findings.assignment_states(mapping["CAMPAIGN"], draft)
            self.assertEqual((states["legal"]["reviewer"], states["privacy"]["state"]),
                             ("Reviewer C", "needs_reassignment"))
            code, _, err = run("finding", "reviewers", "retire", "--directory", mapping["CAMPAIGN"], "--name",
                               "Reviewer A")
            self.assertEqual(code, 2)
            self.assertIn("--on", err)


if __name__ == "__main__":
    unittest.main()
