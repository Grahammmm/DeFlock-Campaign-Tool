"""DF0022-16: docs/OUTREACH-GUIDE.md. Order and boundaries, local links and anchors, every documented command against the
real parsers, and the guide's own commands run on a disposable copy of the fictional campaign with outbound connections
blocked. Synthetic data only; nothing is sent, posted or published.

Run from the repository root: python3 -B -m unittest tests.test_outreach_guide_doc -v
"""
import json
import re
import shlex
import shutil
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from tests.test_findings_guide_doc import (DocumentError, check_links, command_lines, fences,  # noqa: E402
                                           invoke, parse_only, read, sections, tree)

GUIDE, README = REPO_ROOT / "docs" / "OUTREACH-GUIDE.md", REPO_ROOT / "README.md"
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
FINDING_ID, SLUG = "fnd_0000000000000001", "cedar-policy-posted"
WARNING = "These offline commands post, send and publish nothing. A draft is not approval to use it."
ORDER = ["1. Read the finding first", "2. Social posts, one draft per platform", "3. A thread for one finding",
         "4. Posts for meetings", "5. Press release and letter to the editor", "6. The flyer",
         "7. The newsletter draft", "8. The newsletter approval card (described, not run)",
         "9. Record coverage you received", "10. What the tool never does"]


def blocked(*args, **kwargs):
    raise AssertionError("no network in these tests")


def commands():
    """Every ``python3 -m ...`` command in the guide's sh fences, as argv lists with the line it starts on."""
    found = []
    for kind, body, number in fences(read(GUIDE)):
        if kind == "sh":
            for line in command_lines(body):
                argv = shlex.split(line)
                if argv[:2] == ["python3", "-m"]:
                    found.append((argv[2], argv[3:], number))
                elif argv[0] != "cp":
                    raise DocumentError(f"line {number}: unexpected command: {line}")
    return found


class OutreachGuideTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-outreach-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.mapping = {"CAMPAIGN": self.root, "FINDING_ID": FINDING_ID, "PUBLISHED_ON": "2026-11-01"}

    def argv(self, tokens):
        return [re.sub(r"\$([A-Z_]+)", lambda m: str(self.mapping[m.group(1)]), t) for t in tokens]

    def run_command(self, module, tokens):
        with mock.patch.object(socket.socket, "connect", blocked), mock.patch.object(socket, "getaddrinfo", blocked):
            return invoke(module, self.argv(tokens))

    def run_guide(self, skip=("content",)):
        results = {}
        for module, tokens, number in commands():
            if tokens[0] in skip:
                continue
            code, out, err = self.run_command(module, tokens)
            results[" ".join(tokens[:2])] = (code, out, err)
            self.assertEqual(code, 0, f"line {number}: {tokens}: {err}{out}")
        return results

    def finding_path(self):
        return self.root / "content" / "findings" / f"{SLUG}.json"

    def edit_finding(self, **changes):
        document = json.loads(self.finding_path().read_text(encoding="utf-8"))
        document.update(changes)
        self.finding_path().write_text(json.dumps(document), encoding="utf-8")

    def test_task_order_and_channel_boundaries(self):
        text = read(GUIDE)
        self.assertTrue(text.startswith("# Outreach volunteer guide"))
        self.assertIn(WARNING, text)
        headings = list(sections(text))
        positions = [next(i for i, h in enumerate(headings) if h == name) for name in ORDER]
        self.assertEqual(positions, sorted(positions))
        for needed in ("Worked example (synthetic)", "What you may say", "Before anything leaves your hands"):
            self.assertIn(needed, headings)
        may = sections(text)["What you may say"]
        for word in ("citations", "limitations", "correction", "withdrawn", "raw originals"):
            self.assertIn(word, may)
        approval = sections(text)["8. The newsletter approval card (described, not run)"]
        self.assertIn("There is no command here that creates a card", approval)
        self.assertEqual(len(re.findall(r"```", approval)), 0)
        self.assertIn("No coverage exists for this example", text)
        tokens = {t for c in commands() for t in c[1]}
        for forbidden in ("--record-sent", "--publish", "sent", "archive", "build"):
            self.assertNotIn(forbidden, tokens)
        self.assertEqual([c[0] for c in commands()].count("campaign_tool.newsletter"), 1)

    def test_readme_and_all_doc_fragments_resolve(self):
        readme = read(README)
        self.assertEqual(readme.count("docs/OUTREACH-GUIDE.md"), 1)
        self.assertIn("docs/FINDINGS-GUIDE.md", readme)
        self.assertEqual(check_links(README, readme), [])
        self.assertEqual(check_links(GUIDE, read(GUIDE)), [])
        text = read(GUIDE)
        self.assertEqual(check_links(GUIDE, text + "\n[x](PUBLICATION.md#no-such-heading)\n"),
                         [f"OUTREACH-GUIDE.md:{text.count(chr(10)) + 2}: PUBLICATION.md#no-such-heading: no such heading"])
        self.assertEqual(len(check_links(GUIDE, text + "\n[x](MISSING.md)\n")), 1)

    def test_every_command_and_nested_flag_parses(self):
        seen, list_modes = set(), set()
        for module, tokens, number in commands():
            namespace, path = parse_only(module, self.argv(tokens))
            seen.add((module, path))
            if path == ("supporters", "list"):
                list_modes.add(namespace.as_json)
            if path == ("press", "letter"):
                self.assertEqual((namespace.words, namespace.outlet), (250, "Example News"))
            if path == ("social",):
                self.assertTrue(namespace.thread == ("--thread" in tokens))
        self.assertEqual(list_modes, {False, True})
        for needed in (("campaign_tool", ("social",)), ("campaign_tool", ("press", "draft")),
                       ("campaign_tool", ("press", "letter")), ("campaign_tool", ("flyer",)),
                       ("campaign_tool", ("content", "coverage", "add")),
                       ("campaign_tool", ("content", "coverage", "remove")),
                       ("campaign_tool", ("supporters", "list")),
                       ("campaign_tool", ("content-check",)), ("campaign_tool.newsletter", ())):
            self.assertIn(needed, seen)
        with self.assertRaises(DocumentError):
            parse_only("campaign_tool", ["social", "--directory", "x", "--thraed"])
        with self.assertRaises(DocumentError):
            parse_only("campaign_tool", ["press", "letter", "--directory", "x", "--words", "many"])

    def test_worked_published_example_runs_offline(self):
        before = {k: v for k, v in tree(self.root).items() if k.startswith(("content", "public"))}
        results = self.run_guide()
        self.assertTrue(results["social --directory"][1].rstrip().endswith("Nothing was posted."))
        for name in ("x", "bluesky", "mastodon"):
            self.assertIn("Corrected 2026-02-03", (self.root / "kit/social" / name / f"{SLUG}.md").read_text())
        thread = (self.root / "kit/social/threads/bluesky" / f"{SLUG}.md").read_text()
        self.assertIn("(1/3)", thread)
        self.assertIn("Limitations:", thread)
        self.assertIn("Correction 2026-02-03", thread)
        self.assertIn('"sent": false', results["press draft"][1])
        self.assertIn("Nothing was sent.", results["press letter"][1])
        letter = next((self.root / "kit/press/letters").glob("*.md")).read_text()
        self.assertIn("[YOUR NAME]", letter)
        self.assertIn("To the editor of Example News", letter)
        for name in ("kit/flyer/flyer.html", "kit/flyer/flyer.txt", "kit/newsletter-draft.html",
                     "kit/newsletter-draft.txt", "kit/newsletter-draft.json", "kit/press"):
            self.assertTrue((self.root / name).exists(), name)
        after = {k: v for k, v in tree(self.root).items() if k.startswith(("content", "public"))}
        self.assertEqual(before, after)
        self.assertFalse((self.root / "public").exists())
        self.assertFalse((self.root / "content" / "coverage.json").exists())

    def test_coverage_example_is_local_record_only(self):
        content_before = {k: v for k, v in tree(self.root).items() if k.startswith("content")}
        add, remove = [c for c in commands() if c[1][0] == "content"]
        code, out, err = self.run_command(add[0], add[1])
        self.assertEqual(code, 0, err)
        self.assertIn("Nothing was built, published or sent.", out)
        coverage = json.loads((self.root / "content" / "coverage.json").read_text())
        self.assertEqual(coverage["entries"][0]["headline"], "Published headline")
        self.assertEqual(coverage["entries"][0]["findings"], [FINDING_ID])
        code, out, err = self.run_command(add[0], add[1])
        self.assertNotEqual(code, 0)
        self.assertIn("duplicate URL", out + err)
        self.assertEqual(self.run_command(remove[0], remove[1])[0], 0)
        self.assertEqual(json.loads((self.root / "content" / "coverage.json").read_text())["entries"], [])
        self.assertFalse((self.root / "public").exists())
        changed = {k for k, v in tree(self.root).items() if k.startswith("content") and content_before.get(k) != v}
        self.assertEqual(changed, {"content/coverage.json"})
        self.assertIn("separately authorised", read(GUIDE))

    def test_supporter_inventory_commands_are_private_and_read_only(self):
        from campaign_tool.supporters import add_supporter, set_cosign_consent
        listing = [c for c in commands() if c[1][:2] == ["supporters", "list"]]
        self.assertEqual([("--json" in c[1]) for c in listing], [False, True])
        self.assertFalse({"consent", "remove", "add"} & {c[1][1] for c in commands() if c[1][0] == "supporters"})
        before = tree(self.root)
        self.assertEqual([self.run_command(m, t)[:2] for m, t, _ in listing], [(0, "none recorded\n"), (0, "[]\n")])
        self.assertEqual(tree(self.root), before)  # no kit/ or private store is created
        add_supporter(self.root, name="Cedar Valley Tenants", kind="community", organization=True,
                      confirmed_on="2026-09-01", how="letter")
        set_cosign_consent(self.root, "cedar-valley-tenants", cosign=["any"], confirmed_on="2026-09-02", how="form")
        stored = (self.root / "kit" / "supporters-consent.json").read_text()
        before = tree(self.root)
        (code, text, err), (_, data, _) = [self.run_command(m, t) for m, t, _ in listing]
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(text, 'cedar-valley-tenants | community | "Cedar Valley Tenants" | joined 2026-09-01 | '
                               "consent present | confirmed 2026-09-01 | co-sign any | co-sign confirmed 2026-09-02 | "
                               "staged none\n")
        self.assertEqual(json.loads(data)[0]["cosign"], "any")
        for private in (json.loads(stored)["supporters"]["cedar-valley-tenants"]["public_sha256"], "letter", "form"):
            self.assertNotIn(private, text + data)
        self.assertEqual(tree(self.root), before)
        section = sections(read(GUIDE))["Supporting organizations and co-signers (private, read-only)"]
        for promise in ("not co-sign approval", "`content-check`", "private record without public entry",
                        "SITE-CONTENT.md#supporting-organizations"):
            self.assertIn(promise, section)

    def test_refusal_and_edit_guidance_is_honest(self):
        social = ["social", "--directory", "$CAMPAIGN", "--platform", "bluesky"]
        thread = ["social", "--directory", "$CAMPAIGN", "--thread", "--finding", "$FINDING_ID", "--platform", "bluesky"]
        self.assertEqual(self.run_command("campaign_tool", social)[0], 0)
        # unknown finding: refused, nothing written
        code, _, err = self.run_command("campaign_tool", thread[:-3] + ["fnd_9999999999999999"])
        self.assertNotEqual(code, 0)
        self.assertFalse((self.root / "kit/social/threads").exists())
        # an edited draft is kept and the new text goes to the suggestions folder, never overwritten
        draft = self.root / "kit/social/bluesky" / f"{SLUG}.md"
        draft.write_text(draft.read_text() + "\nMy own words.\n", encoding="utf-8")
        self.edit_finding(title="Cedar Police produced a policy (fictional, retitled)")
        self.assertEqual(self.run_command("campaign_tool", social)[0], 0)
        self.assertIn("My own words.", draft.read_text())
        self.assertTrue((self.root / "kit/social/bluesky.suggested" / f"{SLUG}.md").exists())
        # a thread that cannot fit stops the whole run before anything is written
        self.edit_finding(limitations=[f"Limitation {i} " + "x" * 40 for i in range(12)])
        code, _, err = self.run_command("campaign_tool", thread)
        self.assertNotEqual(code, 0)
        self.assertFalse((self.root / "kit/social/threads").exists())
        # an existing flyer is refused, not overwritten
        flyer = ["flyer", "--directory", "$CAMPAIGN", "--max-findings", "3"]
        self.assertEqual(self.run_command("campaign_tool", flyer)[0], 0)
        (self.root / "kit/flyer/flyer.txt").write_text("my edit", encoding="utf-8")
        code, _, err = self.run_command("campaign_tool", flyer)
        self.assertNotEqual(code, 0)
        self.assertEqual((self.root / "kit/flyer/flyer.txt").read_text(), "my edit")
        # no verified upcoming meeting: a valid, empty result
        code, out, _ = self.run_command("campaign_tool", ["social", "--directory", "$CAMPAIGN", "--meetings",
                                                          "--within-days", "14", "--platform", "bluesky"])
        self.assertEqual(code, 0)
        self.assertEqual(list((self.root / "kit/social/meetings/bluesky").glob("*.md")), [])
        # the release needs a base_url
        site = self.root / "content" / "site.json"
        document = json.loads(site.read_text())
        del document["base_url"]
        site.write_text(json.dumps(document), encoding="utf-8")
        code, _, err = self.run_command("campaign_tool", ["press", "draft", "--directory", "$CAMPAIGN",
                                                          "--finding", "$FINDING_ID"])
        self.assertNotEqual(code, 0)
        self.assertFalse((self.root / "kit/press").exists())
        text = read(GUIDE)
        for promise in ("`--replace` overwrites your edits", "nothing is written", "never deleted", ".suggested",
                        "An empty result is normal"):
            self.assertIn(promise, text)


if __name__ == "__main__":
    unittest.main()
