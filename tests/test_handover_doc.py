"""DF0022-15: docs/HANDOVER.md. Task order and safety wording, local links and anchors, every documented command against
the real parsers, the backup-exclusion caveat against the real member selection, and no secrets. Synthetic data only;
nothing is sent, published or deleted.

Run from the repository root: python3 -B -m unittest tests.test_handover_doc -v
"""
import re
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import backup  # noqa: E402
from tests.test_findings_guide_doc import (DocumentError, check_links, command_lines, fences,  # noqa: E402
                                           invoke, parse_only, read, sections)

GUIDE, README = REPO_ROOT / "docs" / "HANDOVER.md", REPO_ROOT / "README.md"
SAFETY = "This checklist sends, publishes and deletes nothing. Stop if either organizer cannot account for a private file."
WARNING = "A passing backup-drill does not prove the whole campaign was transferred."
HOLD = "Handover incomplete: private files outside the backup still need a verified custody transfer."
ORDER = ["1. What the campaign directory holds, and what never leaves the machine",
         "2. Pause edits and make a fresh backup",
         "3. " + WARNING.rstrip("."),
         "4. Check the archive and restore it on the incoming machine",
         "5. Reconcile excluded files, then register the campaign on this machine",
         "6. Open records requests", "7. Staged meetings and written-comment deadlines",
         "8. Findings in review, their assignments and the reviewer roster",
         "9. The personal-data register", "10. Accounts, by role, and sign-off"]
ROLES = ["Domain and DNS owner", "Hosting owner", "Mailbox owner", "Newsletter owner", "Backup custodian",
         "Review coordinator"]
MAPPING = {"D": "/tmp/synthetic/demo", "ARCHIVE": "/tmp/synthetic/backup.tar", "DIGEST": "0" * 64,
           "NEW_EMPTY_DIR": "/tmp/synthetic/restored", "DRAFT": "drafts/synthetic.json", "KEY.pub": "organizer.asc",
           "OUT.json": "/tmp/synthetic/receipt.json"}


def commands():
    """Every ``python3 -m ...`` command in the guide's sh fences as (module, argv tail, line)."""
    found = []
    for kind, body, number in fences(read(GUIDE)):
        if kind == "sh":
            for line in command_lines(body):
                argv = shlex.split(line)
                if argv[:3] != ["python3", "-m", "campaign_tool"]:
                    raise DocumentError(f"line {number}: unexpected command: {line}")
                found.append(("campaign_tool", [MAPPING.get(t, t) for t in argv[3:]], number))
    return found


class HandoverGuideTests(unittest.TestCase):
    def test_task_order_and_safety_boundaries(self):
        text = read(GUIDE)
        self.assertTrue(text.startswith("# Hand over a campaign"))
        self.assertIn(SAFETY, text)
        headings = list(sections(text))
        self.assertEqual(headings, ORDER)
        body = sections(text)
        self.assertIn(HOLD, body[ORDER[2]])
        self.assertIn("**" + WARNING + "**", body[ORDER[2]])
        self.assertIn("never overwrite or delete", body[ORDER[3]])
        self.assertIn("not the permanent restore", body[ORDER[3]])
        self.assertIn("never deleted automatically", body[ORDER[8]])
        for role in ROLES:
            self.assertIn("| " + role + " |", body[ORDER[9]])
        self.assertIn("never a login, credential, key, account id", body[ORDER[9]])
        # the sign-off checklist is left unfilled
        self.assertEqual(len(re.findall(r"^- \[ \] ", text, re.M)), 5)
        self.assertNotIn("- [x]", text.lower())
        # the verbs it must never teach
        tokens = {t for c in commands() for t in c[1]}
        for forbidden in ("--publish", "--record-sent", "send", "sent", "build", "campaigns remove", "remove",
                          "--replace", "push"):
            self.assertNotIn(forbidden, tokens)
        for kind, body_text, number in fences(text):
            self.assertEqual(kind, "sh", f"line {number}: unexpected fence")
            for word in ("git add", "scp", "rsync", "curl", "wget", "gh ", "rm "):
                self.assertNotIn(word, body_text)

    def test_links_and_section_fragments_resolve(self):
        readme, text = read(README), read(GUIDE)
        self.assertEqual(readme.count("docs/HANDOVER.md"), 1)
        # the link sits outside the first Start locally shell block, which stays untouched
        start = readme.index("## Start locally")
        block_end = readme.index("```\n", readme.index("```sh", start) + 5)
        self.assertGreater(readme.index("docs/HANDOVER.md"), block_end)
        self.assertEqual(check_links(README, readme), [])
        self.assertEqual(check_links(GUIDE, text), [])
        for target in ("FORK-AND-RUN.md#2-keep-the-campaign-directory-outside-the-repository", "DATA-HANDLING.md",
                       "BACKUP.md#restore-drill", "MULTI-CAMPAIGN.md#--campaign-name", "STATUS.md",
                       "PUBLICATION.md#reviewer-roster-role-assignment-not-authentication",
                       "MEETINGS.md#recording-a-comment-you-submitted", "../README.md#accounts-and-costs"):
            self.assertIn("(" + target + ")", text)
        line = text.count("\n") + 2
        self.assertEqual(check_links(GUIDE, text + "\n[x](BACKUP.md#no-such-heading)\n"),
                         [f"HANDOVER.md:{line}: BACKUP.md#no-such-heading: no such heading"])
        self.assertEqual(len(check_links(GUIDE, text + "\n[x](MISSING.md)\n")), 1)

    def test_documented_commands_parse(self):
        seen = set()
        for module, tokens, number in commands():
            namespace, path = parse_only(module, tokens)
            seen.add(path)
        for needed in (("backup",), ("verify",), ("backup-drill",), ("restore",), ("campaigns", "list"),
                       ("campaigns", "add"), ("kit-status",), ("kit-due",), ("today",), ("finding", "status"),
                       ("finding", "review-status"), ("finding", "reviewers", "list"), ("personal-data",),
                       ("doctor",)):
            self.assertIn(needed, seen)
        with self.assertRaises(DocumentError):
            parse_only("campaign_tool", ["kit-statuss", "--directory", "x"])
        with self.assertRaises(DocumentError):
            parse_only("campaign_tool", ["backup-drill", "--file", "x", "--expect-manifest-sha256-typo", "y"])
        # the command the dependency card added answers --help
        code, out, _ = invoke("campaign_tool", ["personal-data", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("--json", out)

    def test_backup_exclusions_are_not_full_restore_claims(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-handover-") as tmp:
            root = Path(tmp)
            files = ["campaign.json", "content/site.json", "kit/notes.md", "public/index.html",
                     "private/drafts/d.json", "private/reviews/r.json", "private/review-assignments/a.json",
                     "private/reviewers.json", "private/ledger.sqlite"]
            for name in files:
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text("synthetic", encoding="utf-8")
            archived = {name for name, _ in backup._members(root)}
        for name in ("private/drafts/d.json", "private/reviews/r.json", "private/review-assignments/a.json",
                     "private/reviewers.json", "public/index.html"):
            self.assertNotIn(name, archived)
        for name in ("campaign.json", "kit/notes.md", "private/ledger.sqlite"):
            self.assertIn(name, archived)
        text = sections(read(GUIDE))[ORDER[2]]
        for excluded in ("private/drafts/", "private/reviews/", "private/review-assignments/",
                         "private/reviewers.json", "campaign register"):
            self.assertIn(excluded, text)
        self.assertNotRegex(text.lower(), r"roster (is|was|has been) (backed up|restored)")
        self.assertNotRegex(read(GUIDE).lower(), r"complete (backup|archive) (is|was) (made|written)")

    def test_no_secrets_and_private_outputs(self):
        text = read(GUIDE)
        self.assertNotRegex(text, r"[0-9a-f]{32,}")
        self.assertNotRegex(text, r"-----BEGIN")
        self.assertNotRegex(text, r"(?i)\b(api[_ -]?key|secret|token|passphrase|password)\s*[:=]")
        self.assertNotRegex(text, r"[\w.+-]+@(?!example\.)[\w-]+\.[\w.]+")
        self.assertNotRegex(text, r"https?://")
        # no output of an inventory command is embedded: the only fences are commands
        self.assertEqual({k for k, _, _ in fences(text)}, {"sh"})
        # the sign-off table rows for the roles carry no filled-in cell
        for line in text.splitlines():
            if any(line.startswith("| " + role + " |") for role in ROLES):
                self.assertEqual(line.count("|"), 4)
                self.assertEqual(line.split("|")[2].strip(), "")
                self.assertEqual(line.split("|")[3].strip(), "")


if __name__ == "__main__":
    unittest.main()
