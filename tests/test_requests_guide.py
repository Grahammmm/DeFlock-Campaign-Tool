"""DF1103-15: docs/REQUESTS-GUIDE.md. Every command and option the guide shows against the real parsers, local links and
anchors, and the guide's own fenced commands run in order in a temporary synthetic campaign with outbound connections
blocked. Nothing is sent, printed, paid, uploaded or published.

Run from the repository root: python3 -B -m unittest tests.test_requests_guide -v

Conventions the guide keeps and these tests read: ```sh blocks run in document order (a trailing "# exit N" is the exit
code that line ends with, default 0; "cp A B" is the one non-campaign_tool line allowed); ```bash blocks are alternatives
checked against the parser and never run; "text|json|csv save PATH" fences are files the reader saves;
"text replace-line-6 PATH" replaces line 6 of PATH; $ACK_PREFIX and $RECORDS_PREFIX are the 12-character prefixes the
guide tells the reader to copy from `status --list --source-id`, resolved here at run time from the ingest just done.
"""
import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import smtplib
import socket
import stat
import sys
import tempfile
import time
import unittest
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli  # noqa: E402

GUIDE, README, LAUNCH = REPO_ROOT / "docs" / "REQUESTS-GUIDE.md", REPO_ROOT / "README.md", REPO_ROOT / "LAUNCH.md"
NO_NETWORK = AssertionError("no network in these tests")
FENCE = re.compile(r"^```([^\n]*)\n(.*?)^```$", re.S | re.M)
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
AGENCY = "ca-example-police"
EXIT_NOTE = re.compile(r"\s+#\s*exit\s+(\d+)\s*$")


class DocumentError(AssertionError):
    pass


# --- markdown helpers ---------------------------------------------------------------------------------------------

def read(path):
    return Path(path).read_text(encoding="utf-8")


def slug(heading):
    heading = re.sub(r"[`*_]", "", heading.strip().lower())
    return re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", heading))


def anchors(text):
    """GitHub-style heading anchors outside code fences; a repeated heading gets -1, -2."""
    seen, found = {}, set()
    for line in FENCE.sub("", text).splitlines():
        match = re.match(r"#{1,6}\s+(.*)", line)
        if match:
            base = slug(match.group(1))
            found.add(base if base not in seen else f"{base}-{seen[base]}")
            seen[base] = seen.get(base, 0) + 1
    return found


def check_links(source, text):
    """Problems for every local link in ``text`` as if it were the file ``source``: file, then fragment."""
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
    parts = re.split(r"^## (.*)$", text, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2]))


def fences(text):
    """(info string, body, first line number) of every fenced block, in document order."""
    return [(m.group(1).strip(), m.group(2), text.count("\n", 0, m.start()) + 1) for m in FENCE.finditer(text)]


def command_lines(block):
    return [line.strip() for line in re.sub(r"\\\n\s*", " ", block).splitlines() if line.strip()]


# --- the real parsers -----------------------------------------------------------------------------------------------

def root_parser():
    captured = {}
    original = argparse.ArgumentParser.parse_args

    def capture(self, args=None, namespace=None):
        captured["parser"] = self
        raise SystemExit(0)

    with mock.patch.object(sys, "argv", ["campaign_tool"]), mock.patch.object(argparse.ArgumentParser, "parse_args", capture):
        cli.main()
    assert original is not None
    return captured["parser"]


def children(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    return {}


def option_strings(parser):
    found = set()
    for action in parser._actions:
        found.update(action.option_strings)
    for child in children(parser).values():
        found |= option_strings(child)
    return found


def parse_only(argv):
    """Parse ``argv`` (after ``python3 -m campaign_tool``) with the real parser; DocumentError when it refuses."""
    parser, err = root_parser(), io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        try:
            parser.parse_args(list(argv))
        except SystemExit as exc:
            if exc.code not in (0, None):
                lines = err.getvalue().strip().splitlines()
                raise DocumentError(lines[-1] if lines else "refused") from None


def validate_commands(text, root=None):
    """Every command the text shows: fenced sh/bash lines (and the one allowed ``cp``) and inline code spans, each
    checked against the real parsers; DocumentError naming the guide line otherwise. Returns the leaf commands."""
    root = root or root_parser()
    top = children(root)
    leaves = set()
    for info, body, number in fences(text):
        if info.split(" ")[0] not in ("sh", "bash"):
            continue
        for line in command_lines(body):
            line = EXIT_NOTE.sub("", line)
            argv = shlex.split(line)
            if info == "sh" and argv[:1] == ["cp"] and len(argv) == 3:
                continue
            if argv[:3] != ["python3", "-m", "campaign_tool"]:
                raise DocumentError(f"line {number}: not a campaign_tool command: {line}")
            args = [re.sub(r"\$[A-Z_]+", "0123456789ab", token) for token in argv[3:]]
            try:
                parse_only(args)
            except DocumentError as exc:
                raise DocumentError(f"line {number}: {line}: {exc}") from None
            leaves.add(args[0])
    prose = FENCE.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    for number, line in enumerate(prose.splitlines(), 1):
        for span in re.findall(r"`([^`]+)`", line):
            tokens = span.split()
            if span.startswith("python3 -m campaign_tool"):
                try:
                    parse_only([re.sub(r"\$[A-Z_]+", "0123456789ab", t) for t in tokens[3:]])
                except DocumentError as exc:
                    raise DocumentError(f"line {number}: {span}: {exc}") from None
                continue
            if tokens and tokens[0] in top or (tokens and tokens[0] == "kit" and "--scope" in tokens):
                allowed = option_strings(top[tokens[0]]) | {"-h", "--help"}
                for token in tokens[1:]:
                    if token.startswith("--") and token not in allowed:
                        raise DocumentError(f"line {number}: {span}: {tokens[0]} has no option {token}")
                if tokens[0] == "kit" and len(tokens) > 1 and not tokens[1].startswith("-"):
                    raise DocumentError(f"line {number}: {span}: kit has no command {tokens[1]}")
            elif len(tokens) == 1 and tokens[0].startswith("--") and tokens[0] not in option_strings(root):
                raise DocumentError(f"line {number}: {span}: no command has the option {tokens[0]}")
    return leaves


def invoke(arguments):
    """Call the real ``main``: (exit code, stdout text, stderr text)."""
    out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", newline="", write_through=True)
    err, code = io.StringIO(), 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            with mock.patch.object(sys, "argv", ["campaign_tool", *arguments]):
                result = cli.main()
            code = result if isinstance(result, int) else 0
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return code, out.buffer.getvalue().decode("utf-8"), err.getvalue()


def settle_clock():
    now = datetime.now(timezone.utc)
    if now.hour == 23 and now.minute == 59 and now.second >= 55:
        time.sleep(7)


class Walk:
    """The guide's own fenced commands run in document order against a synthetic campaign in temporary storage.
    ``advance(stop)`` stops after the first command for which ``stop(arguments)`` is true."""

    def __init__(self, case, guide_text=None):
        settle_clock()
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-requests-guide-")
        case.addCleanup(temporary.cleanup)
        self.case, self.base = case, Path(temporary.name).resolve()
        self.root, self.files = self.base / "my-campaign", self.base / "practice-files"
        self.files.mkdir()
        self.prefixes, self.history = {}, []
        self.pending = self.units(guide_text if guide_text is not None else read(GUIDE))

    def path(self, text):
        return text.replace("../my-campaign", str(self.root)).replace("../practice-files", str(self.files))

    def units(self, text):
        for info, body, number in fences(text):
            words = info.split(" ")
            if words[0] == "sh":
                for line in command_lines(body):
                    yield "command", line, number
            elif len(words) == 3 and words[0] in ("text", "json", "csv") and words[1] == "save":
                yield "save", (words[2], body), number
            elif len(words) == 3 and words[:2] == ["text", "replace-line-6"]:
                yield "replace", (words[2], body), number

    def advance(self, stop=None):
        for kind, value, number in self.pending:
            if kind == "save":
                target = Path(self.path(value[0]))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(value[1], encoding="utf-8")
            elif kind == "replace":
                target = Path(self.path(value[0]))
                lines = target.read_text(encoding="utf-8").split("\n")
                lines[5] = value[1].rstrip("\n")
                target.write_text("\n".join(lines), encoding="utf-8")
            else:
                done = self.execute(value, number)
                if done and stop is not None and stop(done[3]):
                    return done[:3]
        if stop is not None:
            raise DocumentError("the guide has no such command")
        return None

    def execute(self, line, number):
        expected = EXIT_NOTE.search(line)
        line = EXIT_NOTE.sub("", line)
        argv = shlex.split(self.path(line))
        if argv[0] == "cp":
            shutil.copyfile(argv[1], argv[2])
            return None
        arguments = [re.sub(r"\$([A-Z_]+)", lambda m: self.prefixes[m.group(1)], t) for t in argv[3:]]
        code, out, err = invoke(arguments)
        self.case.assertEqual(code, int(expected.group(1)) if expected else 0,
                              f"guide line {number}: {' '.join(arguments)}\n{out}\n{err}")
        self.history.append((arguments, code, out, err))
        if arguments[:1] == ["status"] and "--source-id" in arguments:
            name = {"synthetic-ack-letter": "ACK_PREFIX", "synthetic-records": "RECORDS_PREFIX"}[arguments[-1]]
            self.prefixes[name] = json.loads(out)["receipts"][0]["sha256_prefix"]
        return code, out, err, arguments

    def sent(self):
        return json.loads(read(self.root / "kit" / "sent.json"))

    def entry(self, key):
        data = self.sent()
        return (data.get("requests") or data)[key] if key in (data.get("requests") or data) else None

    def run(self, *arguments):
        return invoke([str(a) for a in arguments])

    def refused_unchanged(self, *arguments, files=("kit/sent.json",)):
        before = {f: (self.root / f).read_bytes() for f in files}
        code, out, err = self.run(*arguments)
        self.case.assertNotEqual(code, 0, out)
        self.case.assertEqual({f: (self.root / f).read_bytes() for f in files}, before)
        return err


def match(*words):
    return lambda arguments: arguments[:len(words)] == list(words)


class GuideTests(unittest.TestCase):
    def setUp(self):
        umask = os.umask(0o077)
        self.addCleanup(os.umask, umask)
        for patch in (mock.patch.object(socket, "socket", side_effect=NO_NETWORK),
                      mock.patch.object(socket, "create_connection", side_effect=NO_NETWORK),
                      mock.patch.object(socket, "getaddrinfo", side_effect=NO_NETWORK),
                      mock.patch.object(urllib.request, "urlopen", side_effect=NO_NETWORK),
                      mock.patch.object(smtplib.SMTP, "__init__", side_effect=NO_NETWORK)):
            patch.start()
            self.addCleanup(patch.stop)

    # --- commands and options are real -----------------------------------------------------------------------------
    def test_every_documented_command_and_option_exists(self):
        text = read(GUIDE)
        leaves = validate_commands(text)
        for needed in ("init", "kit", "kit-agency", "kit-verify-agency", "kit-check", "kit-copy", "kit-sent",
                       "kit-assign", "kit-response", "kit-contact", "kit-response-undo", "kit-due", "today",
                       "kit-followup", "kit-denial-reply", "kit-timing", "kit-coverage", "kit-status",
                       "kit-sent-import", "kit-response-import", "import-existing", "ingest", "status"):
            self.assertIn(needed, leaves, needed)
        # every response kind the guide lists is a real --kind choice
        kinds = re.search(r"`--kind` is (.*?); a date may not", text, re.S).group(1)
        listed = set(re.findall(r"`([a-z_]+)`", kinds))
        action = next(a for a in children(root_parser())["kit-response"]._actions if a.dest == "kind")
        self.assertEqual(listed, set(action.choices))
        # negative controls: each broken mention fails and names the guide line
        for label, bad in {
                "misspelled command": "```sh\npython3 -m campaign_tool kit-copi --directory D --agency A --for mail\n```\n",
                "unsupported flag": "```sh\npython3 -m campaign_tool kit-copy --directory D --agency A --for mail --print\n```\n",
                "invalid --for choice": "```sh\npython3 -m campaign_tool kit-copy --directory D --agency A --for fax\n```\n",
                "kit due (the command is kit-due)": "```sh\npython3 -m campaign_tool kit due --directory D\n```\n",
                "an inline invented option": "Use `kit-sent --resend` here.\n",
                "an inline invented command": "Use `python3 -m campaign_tool kit-contact-undo --directory D`.\n",
                "not a campaign_tool command": "```sh\nrm -rf x\n```\n"}.items():
            with self.subTest(label), self.assertRaisesRegex(DocumentError, r"^(line \d+|.*)"):
                validate_commands("intro\n" + bad)
        with self.assertRaisesRegex(DocumentError, r"^line 2: "):
            validate_commands("intro\n```sh\npython3 -m campaign_tool kit-copi --directory D\n```\n")

    # --- order, links, anchors -------------------------------------------------------------------------------------
    def test_all_required_tasks_and_reference_anchors(self):
        text = read(GUIDE)
        self.assertEqual(check_links(GUIDE, text), [])
        headings = re.findall(r"^## (\d+)\. (.*)$", text, re.M)
        self.assertEqual([int(n) for n, _ in headings], list(range(1, 20)))
        keywords = ["open a private campaign", "choose agencies", "verify the official contact", "requester, scope",
                    "draft and edit", "check", "copy for email, portal or post", "send it by hand", "record it as sent",
                    "assign a teammate", "record what the agency did", "record your own contacts", "undo a mistake",
                    "due dates", "follow-up", "reply to a denial", "how long it took", "coverage", "import requests"]
        for (_, title), keyword in zip(headings, keywords):
            self.assertIn(keyword, title.lower())
        positions = [text.index(f"## {n}. ") for n, _ in headings]
        self.assertEqual(positions, sorted(positions))
        self.assertLess(text.index("## Practice setup"), positions[0])
        D, T = "AGENCY-DISCOVERY.md#", "AGENCY-TIMELINE.md#"
        required = {1: [D + "what-the-kit-writes"], 2: [D + "add-many-agencies-from-a-spreadsheet"],
                    3: [D + "what-organizers-must-verify-before-sending", D + "recording-a-checked-contact"],
                    4: [D + "request-settings"], 6: [D + "recording-a-hand-sent-request"],
                    9: [D + "several-requests-to-one-agency"], 10: [T + "who-owns-a-request-kit-assign"],
                    11: [T + "stated-production-date-kit-requests", T + "agency-reference-number-kit-requests"],
                    12: [T + "your-own-contact-with-the-records-office-kit-contact"],
                    13: [T + "taking-back-the-latest-step-kit-response-undo"],
                    14: [T + "private-calendar-kit-calendar"], 16: [T + "withheld-basis-kit-requests"],
                    17: [T + "response-times-kit-timing"], 18: [D + "track-requests-in-a-spreadsheet"],
                    19: [T + "requests-sent-before-you-used-the-tool-kit-sent-import",
                         T + "a-transcribed-timeline-in-one-write-kit-response-import"]}
        parts = sections(text)
        for number, title in headings:
            body = parts[f"{number}. {title}"]
            for target in required.get(int(number), []):
                self.assertIn(f"]({target})", body, f"task {number}")
        # the reference sections stay where they were: linked, not moved
        for name in ("AGENCY-DISCOVERY.md", "AGENCY-TIMELINE.md"):
            self.assertTrue((REPO_ROOT / "docs" / name).is_file())
        # incoming links resolve, and controls fail: missing file, missing fragment, duplicate heading slugs
        self.assertEqual(check_links(README, read(README)), [])
        self.assertEqual(check_links(LAUNCH, read(LAUNCH)), [])
        for label, mutated in {
                "missing file": text.replace("(AGENCY-TIMELINE.md#", "(AGENCY-TIMELINES.md#", 1),
                "missing fragment": text.replace("#request-settings", "#no-such-heading", 1),
                "a file-only link to a missing file": text + "\n[x](NO-SUCH-GUIDE.md)\n"}.items():
            with self.subTest(label):
                problems = check_links(GUIDE, mutated)
                self.assertEqual(len(problems), 1)
                self.assertRegex(problems[0], r"^REQUESTS-GUIDE\.md:\d+: ")
        self.assertEqual(anchors("# A\n## Same\n## Same\n"), {"a", "same", "same-1"})
        self.assertEqual(len(check_links(README, read(README).replace("(docs/REQUESTS-GUIDE.md)", "(docs/REQUEST-GUIDE.md)"))), 1)

    # --- the worked example -----------------------------------------------------------------------------------------
    def test_worked_example_offline(self):
        walk = Walk(self)
        walk.advance(match("kit-verify-agency"))
        agencies = json.loads(read(walk.root / "kit" / "agencies.json"))
        mine = next(a for a in agencies if a["agency_id"] == AGENCY)
        self.assertTrue(mine["verified"])
        self.assertEqual(mine["mailing_verification"]["checked_on"], "2026-10-01")
        for agency in agencies:  # the practice county's suggestions are not selected
            self.assertEqual(agency["selected"], agency["agency_id"] == AGENCY or agency["kind"] not in ("sheriff", "chp"))
        walk.advance(match("kit-check"))  # exit 3 while the To: line is a placeholder
        walk.advance(match("kit-check"))  # exit 0 after the guide's edit
        walk.advance(match("kit-copy"))
        outgoing = walk.root / "kit" / "outgoing"
        self.assertEqual(sorted(p.name for p in outgoing.iterdir()), [f"{AGENCY}.letter.html", f"{AGENCY}.letter.txt"])
        for path in outgoing.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertFalse((walk.root / "kit" / "sent.json").exists())  # copying records nothing
        walk.advance(match("kit-sent"))
        main = walk.entry(AGENCY)
        self.assertEqual((main["sent_on"], main["channel"], main["steps"]), ("2026-10-05", "mail", []))
        walk.advance(match("kit-assign"))
        self.assertEqual(json.loads(read(walk.root / "kit" / "assignments.json"))["assignments"], {AGENCY: "Team Blue"})
        ack = hashlib.sha256((walk.files / "ack-letter.txt").read_bytes()).hexdigest()
        records = hashlib.sha256((walk.files / "records.txt").read_bytes()).hexdigest()
        walk.advance(match("kit-response"))
        self.assertTrue(ack.startswith(walk.prefixes["ACK_PREFIX"]))
        self.assertTrue(records.startswith(walk.prefixes["RECORDS_PREFIX"]))
        step = walk.entry(AGENCY)["steps"][0]
        self.assertEqual((step["kind"], step["correspondence"]), ("acknowledged", [ack]))
        self.assertFalse(step.get("records"))  # correspondence is not production
        walk.advance(match("kit-response"))
        walk.advance(match("kit-response"))
        walk.advance(match("kit-contact"))
        main = walk.entry(AGENCY)
        self.assertEqual([s["kind"] for s in main["steps"]], ["acknowledged", "determination", "produced"])
        self.assertEqual(len(main["contacts"]), 1)  # a contact is not a step
        walk.advance(match("kit-response"))  # closed, recorded by mistake
        self.assertEqual(walk.entry(AGENCY)["steps"][-1]["kind"], "closed")
        walk.advance(match("kit-response-undo"))
        main = walk.entry(AGENCY)
        self.assertEqual([s["kind"] for s in main["steps"]], ["acknowledged", "determination", "produced"])
        self.assertEqual(len(main["contacts"]), 1)
        archive = json.loads(read(walk.root / "kit" / "sent-undone.json"))["undos"]
        self.assertEqual(len(archive), 1)  # archived, not deleted
        self.assertEqual(stat.S_IMODE((walk.root / "kit" / "sent-undone.json").stat().st_mode), 0o600)
        self.assertIn("closed", json.dumps(archive[0]))
        code, out, _ = walk.advance(match("kit-due"))
        due = json.loads(out)
        self.assertEqual((code, due["past_due"]), (3, 1))
        self.assertEqual([r["request"] for r in due["open"]], ["narrowed"])
        self.assertEqual([r["request"] for r in due["answered"]], ["main"])
        walk.advance()
        narrowed = walk.entry(f"{AGENCY}#narrowed")
        self.assertEqual(narrowed["steps"][0]["records"], [records])
        names = [args[0] for args, *_ in walk.history]
        timing = next(out for args, _, out, _ in walk.history if args[0] == "kit-timing")
        self.assertIn("calendar days; provisional; not a legal deadline", timing)
        coverage = next(out for args, _, out, _ in walk.history if args[0] == "kit-coverage")
        self.assertIn("with_records 1", coverage)
        self.assertNotIn("kit-copy --for email", " ".join(names))
        self.assertFalse((walk.root / "public").exists())

    # --- follow-up and denial branches ------------------------------------------------------------------------------
    def test_followup_and_denial_branches(self):
        walk = Walk(self)
        walk.advance(match("kit-due"))
        before = (walk.root / "kit" / "sent.json").read_bytes()
        walk.advance(match("kit-followup"))
        draft = walk.root / "kit" / "followups" / f"{AGENCY}--narrowed-2026-10-30.md"
        self.assertTrue(draft.is_file())
        self.assertEqual(stat.S_IMODE(draft.stat().st_mode), 0o600)
        self.assertEqual((walk.root / "kit" / "sent.json").read_bytes(), before)  # nothing sent, nothing recorded
        # the answered main request is not eligible; nothing is written
        err = walk.refused_unchanged("kit-followup", "--directory", walk.root, "--agency", AGENCY, "--as-of", "2026-10-30")
        self.assertIn("was answered (determination on 2026-10-22); no follow-up drafted", err)
        self.assertEqual([p.name for p in draft.parent.iterdir()], [draft.name])
        # an existing draft is kept unless --replace
        kept = draft.read_bytes()
        walk.run("kit-followup", "--directory", walk.root, "--agency", AGENCY, "--request", "narrowed", "--as-of", "2026-10-30")
        self.assertEqual(draft.read_bytes(), kept)
        # a request name the agency never had is refused
        self.assertIn("no sent request ca-example-police/nosuch", walk.refused_unchanged(
            "kit-followup", "--directory", walk.root, "--agency", AGENCY, "--request", "nosuch", "--as-of", "2026-10-30"))
        walk.advance(match("kit-response"))  # the narrowed answer
        self.assertEqual([s["kind"] for s in walk.entry(AGENCY)["steps"]],
                         ["acknowledged", "determination", "produced"])  # named requests stay isolated
        # denial: no denial yet, an unknown rule, then the guide's own command
        err = walk.refused_unchanged("kit-denial-reply", "--directory", walk.root, "--agency", AGENCY)
        self.assertIn("no recorded denial", err)
        walk.advance(match("kit-response"))  # denied, with the agency's own stated basis
        self.assertEqual(walk.entry(AGENCY)["steps"][-1]["withheld_basis"], ["Exemption cited by the agency (SYNTHETIC)"])
        before = (walk.root / "kit" / "sent.json").read_bytes()
        folder = walk.root / "kit" / "followups"
        count = len(list(folder.iterdir()))
        err = walk.refused_unchanged("kit-denial-reply", "--directory", walk.root, "--agency", AGENCY, "--rule", "no-such-rule")
        self.assertEqual(len(list(folder.iterdir())), count)
        self.assertTrue(err.startswith("Stopped: "), err)
        walk.advance(match("kit-denial-reply"))
        reply = folder / f"{AGENCY}-denial-2026-10-31.md"
        self.assertEqual(stat.S_IMODE(reply.stat().st_mode), 0o600)
        body = read(reply)
        for expected in ("ca-civ-1798.90.52-access-records", "[EDIT ME: what you asked for]", "[name]"):
            self.assertIn(expected, body)
        self.assertEqual((walk.root / "kit" / "sent.json").read_bytes(), before)  # drafts only
        # latest-only undo keeps earlier steps, evidence and the other request
        walk.run("kit-response-undo", "--directory", walk.root, "--agency", AGENCY, "--reason", "Denied recorded twice")
        self.assertEqual([s["kind"] for s in walk.entry(AGENCY)["steps"]], ["acknowledged", "determination", "produced"])
        self.assertEqual(len(walk.entry(f"{AGENCY}#narrowed")["steps"]), 1)
        self.assertEqual(len(json.loads(read(walk.root / "kit" / "sent-undone.json"))["undos"]), 2)
        # a contact naming a person or a phone number is refused
        err = walk.refused_unchanged("kit-contact", "--directory", walk.root, "--agency", AGENCY, "--on", "2026-10-14",
                                     "--channel", "phone", "--with", "Jane Doe 555-123-4567", "--note", "x")
        self.assertIn("--with is a job title, not a person's name", err)

    # --- import -----------------------------------------------------------------------------------------------------
    def test_import_preview_apply_and_bad_row(self):
        walk = Walk(self)
        walk.advance(lambda a: a[:2] == ["kit-agency", "add"] and "ca-example-sheriff" in a)
        walk.advance(match("kit-sent-import"))  # the preview: nothing is written
        sent = (walk.root / "kit" / "sent.json").read_bytes()
        self.assertNotIn(b"ca-example-sheriff", sent)
        csv = walk.files / "sent-before.csv"
        bad = walk.files / "sent-bad.csv"
        bad.write_text(read(csv).replace("2026-03-09", "2026-13-09"), encoding="utf-8")
        code, out, err = walk.run("kit-sent-import", "--directory", walk.root, "--csv", bad, "--apply")
        self.assertEqual(code, 2)
        self.assertIn("line 3: sent_on: must be a real YYYY-MM-DD date", err)
        self.assertIn("kit/sent.json was not changed", err)
        self.assertEqual((walk.root / "kit" / "sent.json").read_bytes(), sent)  # bad row: whole file refused
        walk.advance(match("kit-sent-import"))  # --apply
        imported = walk.entry("ca-example-sheriff")
        self.assertTrue(imported["imported"])
        self.assertEqual((imported["sent_on"], imported["channel"], imported["reference"]), ("2026-03-02", "email", "R0123-030226"))
        self.assertNotIn("draft_sha256", imported)  # no draft is implied
        self.assertFalse(imported.get("correspondence"))  # no letter was ingested, so none is linked
        self.assertTrue(walk.entry("ca-example-sheriff#contract-2026")["imported"])
        # applying the same file again is refused and changes nothing
        again = (walk.root / "kit" / "sent.json").read_bytes()
        self.assertIn("already recorded", walk.refused_unchanged(
            "kit-sent-import", "--directory", walk.root, "--csv", csv, "--apply"))
        self.assertEqual((walk.root / "kit" / "sent.json").read_bytes(), again)
        # imported requests take no kit-sent replace and no generated draft
        self.assertFalse((walk.root / "kit" / "requests" / "ca-example-sheriff.md").exists())
        # the response import: a bad row refuses the file; the guide's file is previewed, then applied
        timeline = walk.files / "timeline.csv"
        broken = walk.files / "timeline-bad.csv"
        broken.write_text(read(timeline) + "2026-03-01,determination,,,,\n", encoding="utf-8")
        self.assertIn("line 4", walk.refused_unchanged(
            "kit-response-import", "--directory", walk.root, "--agency", "ca-example-sheriff", "--csv", broken, "--apply"))
        walk.advance(match("kit-response-import"))
        self.assertEqual((walk.root / "kit" / "sent.json").read_bytes(), again)
        walk.advance(match("kit-response-import"))
        self.assertEqual([s["kind"] for s in walk.entry("ca-example-sheriff")["steps"]], ["acknowledged", "determination"])
        # folder preservation is a different command and records no request
        self.assertNotIn("import-existing", " ".join(" ".join(a) for a, *_ in walk.history))

    # --- identifiers, network, wording ------------------------------------------------------------------------------
    def test_no_real_identifiers_or_network(self):
        text = read(GUIDE)
        with self.assertRaises(AssertionError):  # negative control: the network block is active
            socket.create_connection(("police.example", 443))
        with self.assertRaises(AssertionError):
            urllib.request.urlopen("https://police.example/records")
        body = text
        for address in re.findall(r"[\w.+-]+@([\w.-]+)", body):
            self.assertTrue(address.endswith(".example"), address)
        for host in re.findall(r"https?://([\w.-]+)", body):
            self.assertTrue(host.endswith(".example"), host)
        self.assertNotRegex(body, r"\b\d{3}[-. ]\d{3}[-. ]\d{4}\b")
        self.assertIn("SYNTHETIC - NOT A REAL ADDRESS", body)
        # hashes are resolved at run time, never pasted (the one 40-hex value is the commit the guide was checked against)
        hexes = re.findall(r"\b[0-9a-f]{12,64}\b", body)
        self.assertEqual([h for h in hexes if len(h) != 40], [])
        self.assertEqual(len(re.findall(r"^Checked against main [0-9a-f]{40}\.$", text, re.M)), 1)
        self.assertTrue(re.search(r"\b[0-9a-f]{12}\b", "hash 0123456789ab here"))  # the detector sees a pasted prefix
        required = ("not legal advice", "Nothing in this guide", "provisional", "not legal deadlines",
                    "Demo: assume you sent this by hand; the tool sends nothing.", "calendar days",
                    "undo archives, it does not delete", "private", "never a person's name")
        lowered = text.lower()
        for phrase in required:
            self.assertIn(phrase.lower(), lowered, phrase)
        forbidden = re.compile(r"\b(unlawful\w*|illegal\w*|violat\w+|misconduct|guarantee\w*|broke the law|will respond|"
                               r"must respond|you are entitled|you should sue|file a lawsuit)\b", re.I)
        self.assertIsNone(forbidden.search(text), forbidden.search(text))
        self.assertIsNotNone(forbidden.search("The agency unlawfully delayed this"))  # seeded control
        # running the whole guide leaves only private campaign files: no public site, no content, no outbound artifact
        walk = Walk(self)
        walk.advance()
        top = {p.name for p in walk.root.iterdir()}
        self.assertFalse(top & {"public", "content", "dist"}, top)
        self.assertFalse(list(walk.root.rglob("*.eml")))
        for name in ("sent.json", "sent-undone.json", "assignments.json"):
            self.assertEqual(stat.S_IMODE((walk.root / "kit" / name).stat().st_mode), 0o600, name)
        used = {args[0] for args, *_ in walk.history}
        self.assertTrue(used.isdisjoint({"build", "finding", "social", "press", "supporters", "backup", "restore",
                                         "meetings", "meetings-comment-sent", "law"}), used)
        refused = walk.refused_unchanged("kit-copy", "--directory", walk.root, "--agency", AGENCY, "--for", "email",
                                         files=("kit/sent.json",))
        self.assertIn("no verified records_email", refused)

    # --- entry documents --------------------------------------------------------------------------------------------
    def test_entry_docs_keep_local_smoke_contract(self):
        readme, launch = read(README), read(LAUNCH)
        self.assertEqual(readme.count("](docs/REQUESTS-GUIDE.md"), 1)
        self.assertEqual(launch.count("](docs/REQUESTS-GUIDE.md"), 1)
        start = re.search(r"^## Start locally\s*$(.*?)(?=^## |\Z)", readme, re.M | re.S).group(1)
        self.assertIn("](docs/REQUESTS-GUIDE.md", start)
        first = re.search(r"```sh\n(.*?)```", start, re.S).group(1)
        self.assertNotIn("REQUESTS-GUIDE", first)
        self.assertNotRegex(first, r"kit-(sent|response|contact|assign)")
        lines = [line for line in first.splitlines() if line.strip()]
        self.assertTrue(lines[0].startswith("git clone ") and lines[-1].startswith("python3 -m http.server "))
        daily = re.search(r"^## Try it in ten minutes.*?$(.*?)(?=^## |\Z)", launch, re.M | re.S).group(1)
        self.assertIn("](docs/REQUESTS-GUIDE.md", daily)
        self.assertIn("8 rules verified against leginfo text", launch)  # the law-count table is untouched
        self.assertEqual(check_links(README, readme), [])
        self.assertEqual(check_links(LAUNCH, launch), [])
        # seeded controls: a broken incoming link is caught, and a link moved out of its section is
        self.assertEqual(len(check_links(LAUNCH, launch.replace("(docs/REQUESTS-GUIDE.md", "(docs/REQUESTS-GUIDES.md"))), 1)
        self.assertNotIn("](docs/REQUESTS-GUIDE.md", readme.replace(start, ""))


if __name__ == "__main__":
    unittest.main()
