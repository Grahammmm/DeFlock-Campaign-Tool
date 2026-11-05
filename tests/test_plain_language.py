"""DF0022-04: `finding check-plain DRAFT|ID --directory D [--json]`, the advisory plain-language check. Synthetic data only.

Run from the repository root: python3 -B -m unittest tests.test_plain_language -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import findings, plain_language, registry  # noqa: E402
from campaign_tool.plain_language import check_plain  # noqa: E402

EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
KEYS = ["rule", "field", "paragraph", "sentence", "word", "start", "end", "word_count", "limit", "abbreviation"]
FIRST, LAST = "Plain-language suggestions (advisory only; not a publication gate).", "suggestion(s). Nothing was changed."


def sentence(count, word="alpha"):
    return " ".join([word] * count) + "."


def finding(**changes):
    base = {"id": "syn-plain-1", "title": "Synthetic title", "summary": "Short summary.", "body_md": "Short body.",
            "slug": "synthetic-title", "state": "published", "sources": [], "corrections": []}
    base.update(changes)
    return base


def rules(warnings):
    return [(row["rule"], row["field"]) for row in warnings]


class Case(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-plain-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(os.path.realpath(temporary.name))
        self.root = self.base / "campaign"
        (self.root / "content" / "findings").mkdir(parents=True)
        self.write(self.root / "campaign.json", {"schema_version": 1, "name": "Synthetic", "county": "Synthetic", "state": "CA"})
        self.published = self.root / "content" / "findings"

    def write(self, path, document):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(document if isinstance(document, bytes) else
                         (document if isinstance(document, str) else json.dumps(document, ensure_ascii=False)).encode("utf-8"))
        return path

    def draft(self, name="draft.json", **changes):
        return self.write(self.base / name, finding(state="draft", **changes))

    def snapshot(self):
        tree = {}
        for folder, dirs, files in os.walk(self.base):
            for name in dirs + files:
                path = os.path.join(folder, name)
                info = os.lstat(path)
                data = b"" if os.path.isdir(path) and not os.path.islink(path) else (
                    os.readlink(path).encode() if os.path.islink(path) else Path(path).read_bytes())
                tree[os.path.relpath(path, self.base)] = (info.st_mode, info.st_mtime_ns, data)
        return tree

    def cli(self, *arguments, returncode, home=None, directory=True):
        before = self.snapshot()
        command = [sys.executable, "-B", "-m", "campaign_tool", "finding", "check-plain", *map(str, arguments)]
        if directory:
            command += ["--directory", str(self.root)]
        env = dict(os.environ, HOME=str(home)) if home else None
        result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(result.returncode, returncode, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.snapshot(), before, "check-plain must not write, touch or create anything")
        if returncode == 1:
            self.assertEqual(result.stdout, "", "a refusal prints nothing on stdout")
            self.assertTrue(result.stderr.startswith("Stopped: "), result.stderr)
        return result

    def refused(self, target, needle):
        result = self.cli(target, returncode=1)
        self.assertIn(needle, result.stderr)
        return result


class RuleTests(unittest.TestCase):
    def test_threshold_boundaries_and_overlapping_rules(self):
        edge = finding(title=sentence(30), summary=sentence(30) + " " + sentence(30), body_md=" ".join([sentence(30)] * 5))
        self.assertEqual(check_plain(edge), [], "exactly 30 / 60 / 150 words must not warn")
        self.assertEqual(check_plain(finding(body_md="\n\n".join([sentence(30)] * 6))), [], "blank lines split paragraphs")
        over = finding(title=sentence(31), summary=sentence(30) + " " + sentence(31),
                       body_md=" ".join([sentence(30)] * 5) + " " + sentence(1))
        # sorted by field, then start offset: the summary's rule order only breaks ties
        self.assertEqual(rules(check_plain(over)), [("long_sentence", "title"), ("long_summary", "summary"),
                                                    ("long_sentence", "summary"), ("long_paragraph", "body_md")])
        # one 151-word sentence is a long sentence and a long paragraph at once: both rows, sentence rule first
        both = check_plain(finding(body_md=sentence(151)))
        self.assertEqual(rules(both), [("long_sentence", "body_md"), ("long_paragraph", "body_md")])
        self.assertEqual([row["word_count"] for row in both], [151, 151])
        self.assertEqual([row["limit"] for row in both], [30, 150])
        self.assertEqual(rules(check_plain(finding(summary=sentence(61)))),
                         [("long_sentence", "summary"), ("long_summary", "summary")])
        summary = [row for row in check_plain(finding(summary=sentence(30) + " " + sentence(31))) if row["rule"] == "long_summary"]
        self.assertEqual((len(summary), summary[0]["word_count"], summary[0]["limit"]), (1, 61, 60))

    def test_positions_unicode_markdown_and_line_endings(self):
        first = "Short sentence here."
        body = f"{first}\r\n\r\n{sentence(31)}"
        row = check_plain(finding(body_md=body))[0]
        start = len(first) + 4
        self.assertEqual(row, {"rule": "long_sentence", "field": "body_md", "paragraph": 2, "sentence": 2, "word": 4,
                               "start": start, "end": len(body) - 1, "word_count": 31, "limit": 30, "abbreviation": None})
        self.assertEqual(list(row), KEYS)
        # Offsets count Unicode code points (an emoji is one), ends are exclusive, and the sentence with no final
        # punctuation, wordless fragments and a punctuation run still number in order.
        text = "Ünï 😀 ... Wait?! " + " ".join(["wörd"] * 31)
        row = check_plain(finding(body_md=text))[0]
        self.assertEqual((row["sentence"], row["word"], row["start"], row["end"]), (3, 3, 17, len(text)))
        # Underscores split words; apostrophes, hyphens and digits do not.
        self.assertEqual(rules(check_plain(finding(body_md=" ".join(["a_b"] * 15) + " c"))), [("long_sentence", "body_md")])
        self.assertEqual(check_plain(finding(body_md=" ".join(["don't", "well-known", "42x", "re-use"] * 7))), [])
        # raw Markdown link destinations and code words count as supplied; a single newline is only whitespace
        link = "[link text](https://example.com/page)"
        self.assertEqual(check_plain(finding(body_md=sentence(24).rstrip(".") + "\n" + link)), [])
        self.assertEqual(rules(check_plain(finding(body_md=sentence(25).rstrip(".") + "\n" + link))), [("long_sentence", "body_md")])
        self.assertEqual(check_plain(finding(body_md="")), [])
        self.assertEqual(check_plain(finding(body_md="... !!!\n\n???\n\n")), [])
        # counters reset for each field; sentence numbers continue across paragraphs of one field
        warn = check_plain(finding(summary=sentence(31), body_md="One.\n\n" + sentence(31)))
        self.assertEqual([(r["field"], r["paragraph"], r["sentence"], r["word"]) for r in warn if r["rule"] == "long_sentence"],
                         [("summary", 1, 1, 1), ("body_md", 2, 2, 2)])

    def test_expansions_are_ordered_not_retroactive(self):
        def found(**fields):
            return [(r["field"], r["start"], r["abbreviation"]) for r in check_plain(finding(**fields))
                    if r["rule"] == "unexplained_abbreviation"]
        self.assertEqual(found(body_md="Automated license plate readers (ALPR) help. ALPR again."), [])
        self.assertEqual(found(body_md="ALPR (automated license plate readers) help. ALPR again."), [])
        self.assertEqual(found(body_md="Automated license plate readers ( ALPR ) help."), [])
        self.assertEqual(found(body_md="ALPR ( automated  license plate readers ) help."), [])
        # an earlier use warns, every earlier use warns, and the later definition never erases them
        text = "ALPR first. ALPR second. Automated license plate readers (ALPR) help. ALPR last."
        self.assertEqual(found(body_md=text), [("body_md", 0, "ALPR"), ("body_md", 12, "ALPR")])
        # title, then summary, then body order
        self.assertEqual(found(title="ALPR report", summary="Automated license plate readers (ALPR) help.", body_md="ALPR."),
                         [("title", 0, "ALPR")])
        self.assertEqual(found(title="Automated license plate readers (ALPR)", summary="ALPR.", body_md="ALPR."), [])
        # an expansion never spans two fields
        self.assertEqual(found(title="Automated license plate", summary="readers (ALPR) help."), [("summary", 9, "ALPR")])
        # mismatched initials, skipped stopwords, wrong parenthesis shapes
        self.assertEqual(found(body_md="Automated license plate readers (ALRP)."), [("body_md", 33, "ALRP")])
        self.assertEqual(found(body_md="Department of Motion Vehicles (DMV)."), [("body_md", 31, "DMV")])
        self.assertEqual(found(body_md="ALPR (automated license plate) here."), [("body_md", 0, "ALPR")])
        self.assertEqual(found(body_md="ALPR (automated license plate readers extra)."), [("body_md", 0, "ALPR")])
        self.assertEqual(found(body_md="Automated license plate readers, (ALPR)."), [("body_md", 34, "ALPR")])
        self.assertEqual(found(body_md="ALPR; automated license plate readers."), [("body_md", 0, "ALPR")])
        # only 2-6 ASCII capitals as a whole word are in scope
        self.assertEqual(found(body_md="Alpr A.L.P.R. A ABCDEFG ALPR2 ALPR-ready ALPR's ÉCOLE a1B"), [])
        self.assertEqual([a for _, _, a in found(body_md="AB and ABCDEF")], ["AB", "ABCDEF"])

    def test_text_in_quotes_and_source_titles_is_skipped(self):
        quoted = 'He said "' + sentence(40) + '" and left.'
        self.assertEqual(check_plain(finding(body_md=quoted)), [])
        self.assertEqual(check_plain(finding(body_md="He said “" + sentence(40) + "” and left.")), [])
        self.assertEqual(check_plain(finding(body_md='The policy says "ALPR data is shared with HSI." Fine.')), [])
        # skipped words still count in the position number of a later word
        row = check_plain(finding(body_md='"one two three" ' + sentence(31)))[0]
        self.assertEqual((row["rule"], row["word"], row["word_count"]), ("long_sentence", 4, 31))
        # an unmatched quote skips nothing
        self.assertEqual(rules(check_plain(finding(body_md='He said "' + sentence(40)))), [("long_sentence", "body_md")])
        title = "Synthetic ALPR Policy of the Sheriff"
        with_source = finding(body_md=f"See {title} for the details.", sources=[{"sha256": "a" * 64, "title": title}])
        self.assertEqual(check_plain(with_source), [])
        self.assertEqual([r["abbreviation"] for r in check_plain(finding(body_md=f"See {title} for the details."))], ["ALPR"])
        sourced_long = finding(body_md=sentence(40).rstrip(".") + " " + title + ".", sources=[{"title": title}])
        self.assertEqual([r["word_count"] for r in check_plain(sourced_long)], [40])
        self.assertEqual(check_plain(finding(sources="not a list", body_md="Fine.")), [])

    def test_glossary_alias_policy(self):
        def glossary(*terms):
            return {"terms": [{"term": t, "aliases": a, "definition_md": "SECRET DEFINITION"} for t, a in terms]}
        text = finding(body_md="ALPR cameras and HSI staff.")
        names = lambda g: [r["abbreviation"] for r in check_plain(text, g) if r["rule"] == "unexplained_abbreviation"]
        self.assertEqual(names(None), ["ALPR", "HSI"])
        self.assertEqual(names({"terms": [], "notice_md": ""}), ["ALPR", "HSI"])
        self.assertEqual(names(glossary(("Automated license plate reader", ["ALPR"]))), ["HSI"])  # first use is exempt
        self.assertEqual(names(glossary(("Automated license plate reader", ["alpr", "license plate reader"]))), ["ALPR", "HSI"])
        self.assertEqual(names(glossary(("ALPR", []))), ["ALPR", "HSI"])  # the canonical term alone grants nothing
        self.assertEqual(names(glossary(("Plate reader", ["ALPRXYZ", "Alpr", "A1"]))), ["ALPR", "HSI"])
        self.assertNotIn("SECRET DEFINITION", json.dumps(check_plain(text, glossary(("x", ["ALPR"])))))


class CommandTests(Case):
    def put_glossary(self, document, raw=False):
        return self.write(self.root / "content" / "glossary.json", document if raw else
                          {"schema_version": 1, "terms": document})

    def test_glossary_file_validation_and_alias_through_the_command(self):
        self.write(self.published / "x.json", finding(body_md="ALPR cameras."))
        self.cli("syn-plain-1", returncode=3)
        self.put_glossary([{"term": "Automated license plate reader", "aliases": ["ALPR"], "definition_md": "A camera."}])
        result = self.cli("syn-plain-1", returncode=0)
        self.assertEqual(result.stdout.splitlines(), [FIRST, "0 " + LAST])
        self.assertNotIn("A camera.", result.stdout)
        self.put_glossary([])
        self.cli("syn-plain-1", returncode=3)
        # a present but malformed glossary refuses with its own diagnostic; nothing is guessed
        self.put_glossary("{not json", raw=True)
        self.refused("syn-plain-1", "glossary.json")
        self.put_glossary([{"term": "A", "aliases": ["X"], "definition_md": "d"}, {"term": "a", "aliases": [], "definition_md": "d"}])
        self.refused("syn-plain-1", "glossary.json")
        (self.root / "content" / "glossary.json").unlink()
        real = self.base / "real-glossary.json"
        self.write(real, {"schema_version": 1, "terms": []})
        os.symlink(real, self.root / "content" / "glossary.json")
        self.refused("syn-plain-1", "glossary.json")
        (self.root / "content" / "glossary.json").unlink()
        os.symlink(self.base / "absent.json", self.root / "content" / "glossary.json")
        self.refused("syn-plain-1", "glossary.json")
        (self.root / "content" / "glossary.json").unlink()
        self.put_glossary("x" * (2 * 1024 * 1024), raw=True)
        self.refused("syn-plain-1", "glossary.json")

    def test_path_id_resolution_and_fail_closed_reads(self):
        self.write(self.published / "free-form-name.json", finding(body_md=sentence(31)))
        by_id = self.cli("syn-plain-1", returncode=3).stdout
        self.assertEqual(self.cli(self.published / "free-form-name.json", returncode=3).stdout, by_id)
        self.assertEqual(self.cli(self.draft(body_md=sentence(31)), returncode=3).stdout, by_id)
        self.refused("syn-plain-9", "no published finding with id 'syn-plain-9' in content/findings/")
        self.refused("bad id!", "check-plain: expected a draft path or finding ID")
        self.refused("x" * 65, "check-plain: expected a draft path or finding ID")
        self.refused("missing.json", "missing.json")  # a .json suffix is a path, never an ID: no fallback
        self.refused("./syn-plain-1", "syn-plain-1")  # a leading dot is a path too
        # a private draft is never found by ID, and neither is an unpublished file in content/findings/
        self.draft("private/drafts/syn-private.json", id="syn-private")
        self.refused("syn-private", "no published finding with id 'syn-private'")
        self.write(self.published / "unpublished.json", finding(id="syn-unpub", state="draft"))
        self.refused("syn-unpub", "no published finding with id 'syn-unpub'")
        self.write(self.published / "twin.json", finding(body_md="Twin."))
        self.refused("syn-plain-1", "id 'syn-plain-1' is published more than once")
        (self.published / "twin.json").unlink()
        # every unreadable candidate refuses; it is never skipped in favour of a valid one
        for name, content in (("bad.json", "{oops"), ("keys.json", json.dumps(finding(id="other", nope=1))),
                              ("utf8.json", b"\xff\xfe"), ("big.json", b" " * (findings.MAX_DRAFT_BYTES + 1)),
                              ("list.json", "[]"), ("noid.json", json.dumps({"title": "x"}))):
            path = self.write(self.published / name, content)
            self.refused("syn-plain-1", name)
            path.unlink()
        (self.published / "dir.json").mkdir()
        self.refused("syn-plain-1", "dir.json")
        (self.published / "dir.json").rmdir()
        os.symlink(self.published / "free-form-name.json", self.published / "link.json")
        self.refused("syn-plain-1", "symlink")
        (self.published / "link.json").unlink()
        os.symlink(self.base / "nowhere.json", self.published / "dangling.json")
        self.refused("syn-plain-1", "dangling.json")
        (self.published / "dangling.json").unlink()
        self.refused(self.base / "campaign" / "content", "content")  # a folder is not a draft
        shutil.rmtree(self.root / "content")
        self.refused("syn-plain-1", "no published finding with id 'syn-plain-1'")
        self.write(self.base / "real" / "findings" / "a.json", finding())
        os.symlink(self.base / "real", self.root / "content")
        self.refused("syn-plain-1", "content")
        (self.root / "content").unlink()
        (self.root / "content").write_text("x", encoding="utf-8")
        self.refused("syn-plain-1", "content")
        (self.root / "content").unlink()
        (self.root / "content").mkdir()
        os.symlink(self.base / "real" / "findings", self.root / "content" / "findings")
        self.refused("syn-plain-1", "findings")

    def test_unfinished_draft_is_checkable(self):
        edit = "[EDIT ME: say what the record shows]"
        draft = self.draft(title=edit, summary=edit, body_md=edit)
        result = self.cli(draft, returncode=3)  # no receipts, evidence, review or legal gate is consulted
        # EDIT and ME are all-capital words like any other: there is no stop-list that quietly exempts them
        self.assertEqual([line.split(": ")[1].split()[0:2] for line in result.stdout.splitlines()[1:-1]],
                         [["unexplained_abbreviation", name] for _ in range(3) for name in ("EDIT", "ME")])
        self.assertEqual(result.stdout.splitlines()[-1], "6 " + LAST)
        self.assertFalse((self.root / "private").exists())
        self.assertEqual(self.cli(self.draft("empty.json", title="", summary="", body_md=""), returncode=0).stdout.count("\n"), 2)
        no_body = self.base / "nobody.json"
        self.write(no_body, {"id": "syn-nobody", "title": "T", "summary": "S"})
        self.cli(no_body, returncode=0)
        for field, value in (("title", 5), ("title", None), ("summary", None), ("summary", ["x"]), ("body_md", None), ("body_md", 3)):
            path = self.write(self.base / f"bad-{field}.json", dict(finding(), **{field: value}))
            self.refused(path, f"check-plain: {field} must be a string")
        for field in ("title", "summary"):
            document = finding()
            del document[field]
            self.refused(self.write(self.base / f"no-{field}.json", document), f"check-plain: {field} must be a string")

    def test_cli_json_text_help_and_exit_contract(self):
        self.write(self.published / "a.json", finding(title="ALPR notice", summary=sentence(31), body_md="Fine."))
        result = self.cli("syn-plain-1", "--json", returncode=3)
        self.assertTrue(result.stdout.endswith("}\n") and result.stdout.count("\n") == 1, result.stdout)
        report = json.loads(result.stdout)
        self.assertEqual(list(report), ["advisory", "finding_id", "warnings"])
        self.assertEqual((report["advisory"], report["finding_id"]), (True, "syn-plain-1"))
        self.assertEqual([r["rule"] for r in report["warnings"]], ["unexplained_abbreviation", "long_sentence"])
        self.assertTrue(all(list(row) == KEYS for row in report["warnings"]))
        self.assertEqual(report["warnings"][0], {"rule": "unexplained_abbreviation", "field": "title", "paragraph": 1,
                                                 "sentence": 1, "word": 1, "start": 0, "end": 4, "word_count": None,
                                                 "limit": None, "abbreviation": "ALPR"})
        lines = self.cli("syn-plain-1", returncode=3).stdout.splitlines()
        self.assertEqual(lines, [FIRST,
                                 "title paragraph 1, sentence 1, word 1: unexplained_abbreviation ALPR (not spelled out earlier)",
                                 "summary paragraph 1, sentence 1, word 1: long_sentence (31 words; limit 30)",
                                 "2 " + LAST])
        clean = self.write(self.base / "clean.json", finding())
        self.assertEqual(self.cli(clean, returncode=0).stdout.splitlines(), [FIRST, "0 " + LAST])
        self.assertEqual(json.loads(self.cli(clean, "--json", returncode=0).stdout)["warnings"], [])
        for bad in (["--fix"], ["--strict"], ["--out", "x"], ["--threshold", "9"]):
            self.assertEqual(self.cli(clean, *bad, returncode=2).stdout, "")
        self.cli(returncode=2)  # no target
        self.cli(clean, clean, returncode=2)
        self.cli(clean, returncode=2, directory=False)  # --directory is required
        help_text = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "finding", "check-plain", "--help"],
                                   cwd=REPO_ROOT, capture_output=True, text=True, timeout=60).stdout
        self.assertIn("DRAFT|ID", help_text)
        self.assertNotIn("--out", help_text)
        # `~` expands, and a relative path is relative to the working directory
        self.write(self.base / "home" / "tilde.json", finding(summary=sentence(31)))
        self.cli("~/tilde.json", returncode=3, home=self.base / "home")

    def test_named_campaign_confinement(self):
        other = self.base / "other"
        self.write(other / "campaign.json", {"schema_version": 1, "name": "Other", "county": "Synthetic", "state": "CA"})
        self.write(other / "draft.json", finding(summary=sentence(31)))
        register = self.base / "config" / "campaigns.json"
        env = dict(os.environ, **{registry.ENV: str(register)})
        for name, folder in (("A", self.root), ("B", other)):
            subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "campaigns", "add", name, "--directory", str(folder)],
                           cwd=REPO_ROOT, check=True, capture_output=True, env=env, timeout=60)
        self.write(self.published / "a.json", finding(summary=sentence(31)))
        run = lambda *arguments: subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "finding", "check-plain", *arguments],
                                                cwd=REPO_ROOT, capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(run("syn-plain-1", "--campaign", "A").returncode, 3)
        refused = run(str(other / "draft.json"), "--campaign", "A")
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertEqual(refused.stdout, "")
        self.assertNotIn("Traceback", refused.stderr)

    def test_no_writes_or_gate_coupling(self):
        self.write(self.published / "a.json", finding(summary=sentence(31)))
        draft = self.draft("private/drafts/d.json", summary=sentence(31))
        before = findings.reviewed_hash(json.loads(draft.read_text(encoding="utf-8")))
        self.cli("syn-plain-1", returncode=3)
        self.cli(draft, returncode=3)
        self.cli("nope", returncode=1)
        self.assertEqual(findings.reviewed_hash(json.loads(draft.read_text(encoding="utf-8"))), before)
        self.assertFalse((self.root / "private").exists())
        # only the checker's own command and the preview advisory import it: no build, publish, review or hash code
        users = sorted(p.name for p in (REPO_ROOT / "campaign_tool").glob("*.py")
                       if p.name != "plain_language.py" and "plain_language" in p.read_text(encoding="utf-8"))
        self.assertEqual(users, ["cli.py", "content_plain.py"])
        cli_text = (REPO_ROOT / "campaign_tool" / "cli.py").read_text(encoding="utf-8")
        self.assertEqual(cli_text.count("plain_language"), 2)
        source = (REPO_ROOT / "campaign_tool" / "plain_language.py").read_text(encoding="utf-8")
        self.assertNotIn("open(", source)


class PreviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-plain-preview-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.draft = self.root / "private" / "drafts" / "fnd_00000000000000d1.json"
        self.draft.parent.mkdir(parents=True)

    def preview(self, **changes):
        document = {"id": "fnd_00000000000000d1", "title": "Cedar draft policy", "slug": "cedar-draft-preview",
                    "summary": "Synthetic summary.", "body_md": "Synthetic body.", "classification": "documented_fact",
                    "confidence": "verified", "event_date": "2026-01-16", "agency_id": "ca-cedar-police",
                    "sources": [{"sha256": "cafe" * 16, "locator": "page:1"}], "published_at": None, "corrections": [],
                    "state": "draft", "author": "synthetic-author", "limitations": ["Synthetic limits."],
                    "counterevidence": ["None found."], **changes}
        self.draft.write_text(json.dumps(document), encoding="utf-8")
        out = self.root / "private" / "preview"
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "finding", "preview", str(self.draft),
                               "--directory", str(self.root), "--out", str(out)], cwd=REPO_ROOT, capture_output=True,
                              text=True, timeout=60)

    def test_preview_prints_the_same_rows_and_stays_exit_zero(self):
        clean = self.preview()
        self.assertEqual(clean.returncode, 0, clean.stderr)
        self.assertNotIn("Plain language", clean.stdout)  # no rows: the output is what it was before this check
        result = self.preview(summary=sentence(31), body_md="ALPR cameras.")
        self.assertEqual(result.returncode, 0, result.stderr)
        advisory = result.stdout.splitlines()[-3:]
        self.assertEqual(advisory[0], "Plain language (advisory)")
        check = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "finding", "check-plain", str(self.draft),
                                "--directory", str(self.root)], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(check.returncode, 3)
        self.assertEqual(advisory[1:], check.stdout.splitlines()[1:-1])
        self.assertIn("Private preview only; public/ was not changed and nothing was published.", result.stdout)

    def test_preview_never_fails_because_of_the_advisory(self):
        (self.root / "content" / "glossary.json").write_text("{bad", encoding="utf-8")
        with mock.patch.object(plain_language, "_glossary", side_effect=ValueError("synthetic")):
            self.assertEqual(plain_language.preview_lines(self.root, self.draft), [])
        self.assertEqual(plain_language.preview_lines(self.root, self.root / "missing.json"), [])


if __name__ == "__main__":
    unittest.main()
