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

    def test_preview_advisory_matches_title_aware_checker(self):
        sources = [{"sha256": "cafe" * 16, "locator": "page:1", "title": "ABC"}]
        result = self.preview(sources=sources, body_md="ABC " * 400 + "XYZ.")
        self.assertEqual(result.returncode, 0, result.stderr)
        advisory = result.stdout.splitlines()[-2:]
        self.assertEqual(advisory[0], "Plain language (advisory)")
        check = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "finding", "check-plain", str(self.draft),
                                "--directory", str(self.root)], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(check.returncode, 3)
        self.assertEqual(len(check.stdout.splitlines()[1:-1]), 1)
        self.assertIn("unexplained_abbreviation XYZ", check.stdout)
        self.assertEqual(advisory[1:], check.stdout.splitlines()[1:-1])
        self.assertFalse((self.root / "public").exists())

    def test_preview_never_fails_because_of_the_advisory(self):
        (self.root / "content" / "glossary.json").write_text("{bad", encoding="utf-8")
        with mock.patch.object(plain_language, "_glossary", side_effect=ValueError("synthetic")):
            self.assertEqual(plain_language.preview_lines(self.root, self.draft), [])
        self.assertEqual(plain_language.preview_lines(self.root, self.root / "missing.json"), [])


# Frozen from original main before the RF2205-4 optimization; never generated at test runtime.
BOUNDARY_TEXT = '"skip lead" ... Visible?! “skip inside” Source ABC end. Source ABC\n\n“fully skipped”\n\n!!! ??? "lead again" Next. final "tail"'
BOUNDARY_TOKENS = [
    {'field': 'body_md', 'paragraph': 1, 'word': 1, 'start': 1, 'end': 5, 'text': 'skip', 'skipped': True, 'sentence': None, 'sentence_key': (1, 0)},
    {'field': 'body_md', 'paragraph': 1, 'word': 2, 'start': 6, 'end': 10, 'text': 'lead', 'skipped': True, 'sentence': None, 'sentence_key': (1, 0)},
    {'field': 'body_md', 'paragraph': 1, 'word': 3, 'start': 16, 'end': 23, 'text': 'Visible', 'skipped': False, 'sentence': 1, 'sentence_key': (1, 1)},
    {'field': 'body_md', 'paragraph': 1, 'word': 4, 'start': 27, 'end': 31, 'text': 'skip', 'skipped': True, 'sentence': None, 'sentence_key': (1, 2)},
    {'field': 'body_md', 'paragraph': 1, 'word': 5, 'start': 32, 'end': 38, 'text': 'inside', 'skipped': True, 'sentence': None, 'sentence_key': (1, 2)},
    {'field': 'body_md', 'paragraph': 1, 'word': 6, 'start': 40, 'end': 46, 'text': 'Source', 'skipped': True, 'sentence': None, 'sentence_key': (1, 2)},
    {'field': 'body_md', 'paragraph': 1, 'word': 7, 'start': 47, 'end': 50, 'text': 'ABC', 'skipped': True, 'sentence': None, 'sentence_key': (1, 2)},
    {'field': 'body_md', 'paragraph': 1, 'word': 8, 'start': 51, 'end': 54, 'text': 'end', 'skipped': False, 'sentence': 2, 'sentence_key': (1, 2)},
    {'field': 'body_md', 'paragraph': 1, 'word': 9, 'start': 56, 'end': 62, 'text': 'Source', 'skipped': True, 'sentence': None, 'sentence_key': (1, 3)},
    {'field': 'body_md', 'paragraph': 1, 'word': 10, 'start': 63, 'end': 66, 'text': 'ABC', 'skipped': True, 'sentence': None, 'sentence_key': (1, 3)},
    {'field': 'body_md', 'paragraph': 2, 'word': 11, 'start': 69, 'end': 74, 'text': 'fully', 'skipped': True, 'sentence': None, 'sentence_key': (2, 0)},
    {'field': 'body_md', 'paragraph': 2, 'word': 12, 'start': 75, 'end': 82, 'text': 'skipped', 'skipped': True, 'sentence': None, 'sentence_key': (2, 0)},
    {'field': 'body_md', 'paragraph': 3, 'word': 13, 'start': 94, 'end': 98, 'text': 'lead', 'skipped': True, 'sentence': None, 'sentence_key': (3, 2)},
    {'field': 'body_md', 'paragraph': 3, 'word': 14, 'start': 99, 'end': 104, 'text': 'again', 'skipped': True, 'sentence': None, 'sentence_key': (3, 2)},
    {'field': 'body_md', 'paragraph': 3, 'word': 15, 'start': 106, 'end': 110, 'text': 'Next', 'skipped': False, 'sentence': 3, 'sentence_key': (3, 2)},
    {'field': 'body_md', 'paragraph': 3, 'word': 16, 'start': 112, 'end': 117, 'text': 'final', 'skipped': False, 'sentence': 4, 'sentence_key': (3, 3)},
    {'field': 'body_md', 'paragraph': 3, 'word': 17, 'start': 119, 'end': 123, 'text': 'tail', 'skipped': True, 'sentence': None, 'sentence_key': (3, 3)},
]
FROZEN_CORPUS = [
    ([('title', 'ALPR first.'), ('summary', 'Automated license plate readers (ALPR).'), ('body_md', 'ALPR and HSI.')], (), (), [
        {'rule': 'unexplained_abbreviation', 'field': 'title', 'paragraph': 1, 'sentence': 1, 'word': 1, 'start': 0, 'end': 4, 'word_count': None, 'limit': None, 'abbreviation': 'ALPR'},
        {'rule': 'unexplained_abbreviation', 'field': 'body_md', 'paragraph': 1, 'sentence': 1, 'word': 3, 'start': 9, 'end': 12, 'word_count': None, 'limit': None, 'abbreviation': 'HSI'},
    ]),
    ([('title', 'ALPR (automated license plate readers)'), ('summary', 'ALPR.'), ('body_md', 'ALPR XYZ.')], (), ('XYZ',), [
    ]),
    ([('title', '... !!!'), ('body_md', 'Ünï 😀 ABC.\r\n\r\n[link](https://example.test/a_b-c) don’t well-known “QQQ” DEF')], (), (), [
        {'rule': 'unexplained_abbreviation', 'field': 'body_md', 'paragraph': 1, 'sentence': 1, 'word': 2, 'start': 6, 'end': 9, 'word_count': None, 'limit': None, 'abbreviation': 'ABC'},
        {'rule': 'unexplained_abbreviation', 'field': 'body_md', 'paragraph': 2, 'sentence': 2, 'word': 12, 'start': 72, 'end': 75, 'word_count': None, 'limit': None, 'abbreviation': 'DEF'},
    ]),
    ([('summary', 'alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha.'), ('body_md', 'beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta')], (), (), [
        {'rule': 'long_sentence', 'field': 'summary', 'paragraph': 1, 'sentence': 1, 'word': 1, 'start': 0, 'end': 365, 'word_count': 61, 'limit': 30, 'abbreviation': None},
        {'rule': 'long_summary', 'field': 'summary', 'paragraph': 1, 'sentence': 1, 'word': 1, 'start': 0, 'end': 365, 'word_count': 61, 'limit': 60, 'abbreviation': None},
        {'rule': 'long_sentence', 'field': 'body_md', 'paragraph': 1, 'sentence': 1, 'word': 1, 'start': 0, 'end': 754, 'word_count': 151, 'limit': 30, 'abbreviation': None},
        {'rule': 'long_paragraph', 'field': 'body_md', 'paragraph': 1, 'sentence': 1, 'word': 1, 'start': 0, 'end': 754, 'word_count': 151, 'limit': 150, 'abbreviation': None},
    ]),
    ([('body_md', '"skip lead" ... Visible?! “skip inside” Source ABC end. Source ABC\n\n“fully skipped”\n\n!!! ??? "lead again" Next. final "tail"')], ('Source ABC',), (), [
    ]),
    ([('body_md', '"lead" alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha alpha "inside" beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta beta "tail"')], (), (), [
        {'rule': 'long_sentence', 'field': 'body_md', 'paragraph': 1, 'sentence': 1, 'word': 2, 'start': 7, 'end': 806, 'word_count': 152, 'limit': 30, 'abbreviation': None},
        {'rule': 'long_paragraph', 'field': 'body_md', 'paragraph': 1, 'sentence': 1, 'word': 2, 'start': 7, 'end': 806, 'word_count': 152, 'limit': 150, 'abbreviation': None},
    ]),
    ([('title', 'Automated license plate'), ('summary', 'readers (ALPR).'), ('body_md', 'ABC (another basic concept). ABC XYZ.')], (), (), [
        {'rule': 'unexplained_abbreviation', 'field': 'summary', 'paragraph': 1, 'sentence': 1, 'word': 2, 'start': 9, 'end': 13, 'word_count': None, 'limit': None, 'abbreviation': 'ALPR'},
        {'rule': 'unexplained_abbreviation', 'field': 'body_md', 'paragraph': 1, 'sentence': 2, 'word': 6, 'start': 33, 'end': 36, 'word_count': None, 'limit': None, 'abbreviation': 'XYZ'},
    ]),
    ([('body_md', '___ 😀\n\n???\n\n')], (), (), [
    ]),
]


class ScalingTests(unittest.TestCase):
    def test_sentence_boundary_comparisons_are_linear(self):
        real = plain_language._SENTENCE_END
        for size in (256, 512):
            with self.subTest(size=size):
                comparisons = []

                class Offset(int):
                    def __le__(self, other):
                        comparisons.append(1)
                        return super().__le__(other)

                    def __lt__(self, other):
                        comparisons.append(1)
                        return super().__lt__(other)

                    def __add__(self, other):
                        return Offset(int(self) + other)

                class End:
                    def __init__(self, match):
                        self.match = match

                    def end(self):
                        return Offset(self.match.end())

                class Pattern:
                    def finditer(self, text):
                        return (End(match) for match in real.finditer(text))

                with mock.patch.object(plain_language, '_SENTENCE_END', Pattern()):
                    tokens = plain_language._tokens('body_md', 'word. ' * size, ())
                self.assertEqual(len(tokens), size)
                self.assertEqual((tokens[0]['sentence'], tokens[0]['word']), (1, 1))
                self.assertEqual((tokens[-1]['sentence'], tokens[-1]['word']), (size, size))
                self.assertLessEqual(len(comparisons), 4 * size)

    def test_sentence_and_paragraph_grouping_reads_are_linear(self):
        real = plain_language._tokens
        for size in (256, 512):
            for separator in (' ', '\n\n'):
                with self.subTest(size=size, separator=separator):
                    reads = []

                    class Token(dict):
                        def __getitem__(self, key):
                            if key in ('sentence_key', 'paragraph'):
                                reads.append(1)
                            return super().__getitem__(key)

                    def counted(*args):
                        return [Token(token) for token in real(*args)]

                    with mock.patch.object(plain_language, '_tokens', side_effect=counted):
                        rows = plain_language.check_fields([('body_md', ('word.' + separator) * size)])
                    expected = [] if separator != ' ' else [{
                        'rule': 'long_paragraph', 'field': 'body_md', 'paragraph': 1, 'sentence': 1,
                        'word': 1, 'start': 0, 'end': 6 * size - 2, 'word_count': size,
                        'limit': 150, 'abbreviation': None}]
                    self.assertEqual(rows, expected)
                    self.assertLessEqual(len(reads), 32 * size)

    def test_boundary_cursor_preserves_skipped_and_wordless_fragments(self):
        tokens = plain_language._tokens('body_md', BOUNDARY_TEXT, ('Source ABC',))
        self.assertEqual(tokens, BOUNDARY_TOKENS)
        self.assertTrue(all(list(a) == list(b) for a, b in zip(tokens, BOUNDARY_TOKENS)))

    def test_optimized_engine_matches_frozen_mixed_corpus(self):
        for items, titles, aliases, expected in FROZEN_CORPUS:
            with self.subTest(items=items):
                rows = plain_language.check_fields(items, titles, aliases, summary_rule=True)
                self.assertEqual(rows, expected)
                self.assertTrue(all(list(row) == KEYS for row in rows))


class LargeCommandTests(Case):
    def test_large_short_sentence_cli_report(self):
        draft = self.draft(id='syn-scale', title='Synthetic', summary='Short.', body_md='word. ' * 16000)
        result = self.cli(draft, '--json', returncode=3)
        self.assertEqual(result.stderr, '')
        self.assertEqual(result.stdout.count('\n'), 1)
        self.assertEqual(json.loads(result.stdout), {'advisory': True, 'finding_id': 'syn-scale', 'warnings': [{
            'rule': 'long_paragraph', 'field': 'body_md', 'paragraph': 1, 'sentence': 1, 'word': 1,
            'start': 0, 'end': 95998, 'word_count': 16000, 'limit': 150, 'abbreviation': None}]})
        clean = self.draft('clean-scale.json', title='', summary='', body_md='')
        self.assertEqual(json.loads(self.cli(clean, '--json', returncode=0).stdout),
                         {'advisory': True, 'finding_id': 'syn-plain-1', 'warnings': []})


class CountedText(str):
    """Test-only str that keeps its type through slicing and addition and totals the characters those return."""
    work = 0

    def __getitem__(self, key):
        part = CountedText(super().__getitem__(key))
        if isinstance(key, slice):
            type(self).work += len(part)
        return part

    def __add__(self, other):
        joined = CountedText(str(self) + str(other))
        type(self).work += len(joined)
        return joined

    def __radd__(self, other):
        joined = CountedText(str(other) + str(self))
        type(self).work += len(joined)
        return joined

    def replace(self, old, new, count=-1):
        result = CountedText(super().replace(old, new, count))
        type(self).work += len(result)
        return result


# Frozen from original main (per-quote slice-and-concatenate masking); never generated at test runtime.
MASK_CASES = [
    ('A “b\r\n\tc\u00a0d\u2028e” "f😀g\u0301h" x', (), 'A   \r\n\t \xa0 \u2028           x'),
    ('"" “” "a""b" “c”“d” end', (), '                    end'),
    ('plain text no quotes', (), 'plain text no quotes'),
    ('one " unmatched and “ another', (), 'one " unmatched and “ another'),
    ('say "it’s “nested” ok" then “a "b" c” done', (), 'say                    then           done'),
    ('"a" “b” "c" “d” tail', (), '                tail'),
    ('See "Synthetic ALPR" Policy of Sheriff and Synthetic ALPR Policy. "Whole Title" Whole Title',
     ('Synthetic ALPR Policy', 'Whole Title', 'ALPR Policy', 'Title'),
     'See                  Policy of Sheriff and                      .                          '),
    ('aaa bbb aaa bbb aaa', ('aaa bbb', 'bbb aaa', 'aaa'), '                   '),
]
MASK_WORDS = [  # (word number, start, end, skipped) frozen from original main
    ('A “b\r\n\tc\u00a0d\u2028e” "f😀g\u0301h" x',
     [(1, 0, 1, False), (2, 3, 4, True), (3, 7, 8, True), (4, 9, 10, True), (5, 11, 12, True),
      (6, 15, 16, True), (7, 17, 18, True), (8, 19, 20, True), (9, 22, 23, False)]),
    ('say "it’s “nested” ok" then “a "b" c” done',
     [(1, 0, 3, False), (2, 5, 9, True), (3, 11, 17, True), (4, 19, 21, True), (5, 23, 27, False),
      (6, 29, 30, True), (7, 32, 33, True), (8, 35, 36, True), (9, 38, 42, False)]),
    ('one " unmatched and “ another',
     [(1, 0, 3, False), (2, 6, 15, False), (3, 16, 19, False), (4, 22, 29, False)]),
]


class QuoteMaskTests(unittest.TestCase):
    def test_quote_mask_reconstruction_work_is_linear(self):
        for repeats in (256, 512, 1024):
            with self.subTest(repeats=repeats):
                source = 'word "ABC def" ' * repeats
                CountedText.work = 0
                masked = plain_language._skipped_text(CountedText(source), ())
                self.assertEqual(masked, 'word           ' * repeats)
                self.assertEqual(len(masked), len(source))
                self.assertLessEqual(CountedText.work, 8 * len(source))
                self.assertEqual(plain_language._skipped_text(source, ()), 'word           ' * repeats)

    def test_quote_mask_preserves_all_whitespace_and_codepoint_offsets(self):
        text, titles, expected = MASK_CASES[0]
        masked = plain_language._skipped_text(text, titles)
        self.assertEqual(masked, expected)
        self.assertEqual(len(masked), len(text))
        # every whitespace character (CR, LF, tab, NBSP, U+2028) stays at its original offset; only A and x remain
        self.assertTrue(all(masked[i] == c for i, c in enumerate(text) if c.isspace()))
        self.assertEqual([(i, c) for i, c in enumerate(masked) if not c.isspace()], [(0, 'A'), (len(text) - 1, 'x')])
        text, titles, expected = MASK_CASES[1]
        self.assertEqual(plain_language._skipped_text(text, titles), expected)

    def test_mixed_and_unmatched_quotes_keep_old_match_selection(self):
        for text, titles, expected in MASK_CASES[2:6]:
            with self.subTest(text=text):
                self.assertEqual(plain_language._skipped_text(text, titles), expected)
        for text, words in MASK_WORDS:
            with self.subTest(words=text):
                tokens = plain_language._tokens('body_md', text, ())
                self.assertEqual([(t['word'], t['start'], t['end'], t['skipped']) for t in tokens], words)

    def test_source_title_phase_still_runs_after_quote_masking(self):
        for text, titles, expected in MASK_CASES[6:]:
            with self.subTest(text=text):
                self.assertEqual(plain_language._skipped_text(text, titles), expected)
        tokens = plain_language._tokens('body_md', MASK_CASES[7][0], MASK_CASES[7][1])
        self.assertEqual([(t['word'], t['start'], t['end'], t['skipped']) for t in tokens],
                         [(1, 0, 3, True), (2, 4, 7, True), (3, 8, 11, True), (4, 12, 15, True), (5, 16, 19, True)])


class DenseQuoteCommandTests(Case):
    def test_dense_quote_finding_cli_report_is_complete_and_read_only(self):
        body = 'word "ABC def" ' * 56000
        draft = self.draft(id='syn-quote-scale', title='Synthetic', summary='Short.', body_md=body)
        self.assertLess(draft.stat().st_size, findings.MAX_DRAFT_BYTES)
        result = self.cli(draft, '--json', returncode=3)
        self.assertEqual(result.stderr, '')
        self.assertEqual(result.stdout.count('\n'), 1)
        base = {'field': 'body_md', 'paragraph': 1, 'sentence': 1, 'word': 1, 'start': 0, 'end': 839989,
                'word_count': 56000, 'abbreviation': None}
        report = json.loads(result.stdout)
        self.assertEqual(report, {'advisory': True, 'finding_id': 'syn-quote-scale', 'warnings': [
            {'rule': 'long_sentence', **base, 'limit': 30}, {'rule': 'long_paragraph', **base, 'limit': 150}]})
        self.assertTrue(all(list(row) == KEYS for row in report['warnings']))
        clean = self.draft('quoted-only.json', id='syn-quoted-only', title='', summary='',
                           body_md='"' + 'alpha ' * 4000 + 'ABC"')
        self.assertEqual(json.loads(self.cli(clean, '--json', returncode=0).stdout),
                         {'advisory': True, 'finding_id': 'syn-quoted-only', 'warnings': []})


class QuoteSubstitution:
    """Test-only stand-in for ``_QUOTED``: runs the real substitution and returns its actual result as CountedText."""

    def __init__(self, real):
        self.real = real

    def sub(self, replacement, text):
        return CountedText(self.real.sub(replacement, text))


class SourceTitleMaskTests(unittest.TestCase):
    def counted_mask(self, source, titles):
        blanks = []
        real_blank = plain_language._blank

        def counting_blank(text):
            blanks.append(text)
            return real_blank(text)

        CountedText.work = 0
        with mock.patch.object(plain_language, "_QUOTED", QuoteSubstitution(plain_language._QUOTED)), \
                mock.patch.object(plain_language, "_blank", counting_blank):
            masked = plain_language._skipped_text(source, titles)
        return masked, CountedText.work, blanks

    def test_source_title_reconstruction_work_is_linear_per_title(self):
        for repeats in (256, 512, 1024):
            with self.subTest(repeats=repeats):
                source = "ABC " * repeats
                masked, work, blanks = self.counted_mask(source, ("ABC",))
                self.assertEqual(masked, " " * len(source))
                self.assertLessEqual(work, 8 * len(source))
                self.assertEqual(blanks, ["ABC"])  # the replacement is built once, not per occurrence
                self.assertEqual(plain_language._skipped_text(source, ("ABC",)), " " * len(source))
        # zero-match and all-whitespace titles are identity replacements with the same bounded work
        source = "ABC " * 1024 + "\t\u00a0 " * 100
        for title in ("ZZZ", " \t\u00a0"):
            with self.subTest(title=title):
                masked, work, blanks = self.counted_mask(source, (title,))
                self.assertEqual(masked, source)
                self.assertLessEqual(work, 8 * len(source))
                self.assertLessEqual(len(blanks), 1)

    def test_literal_title_replacement_preserves_order_and_overlaps(self):
        mask = plain_language._skipped_text
        self.assertEqual(mask("ababa", ("aba", "ba")), "     ")
        self.assertEqual(mask("abc", ("ab", "bc")), "  c")
        self.assertEqual(mask("abc", ("bc", "ab")), "a  ")
        # longest first: the longer title is considered before the shorter title's mask exists
        self.assertEqual(mask("abXYcd", ("XY", "ab  cd")), "ab  cd")
        self.assertEqual(mask("aaaaa", ("aa",)), "    a")  # self-overlapping: left-to-right, nonoverlapping
        self.assertEqual(mask("aaaaa", ("aa", "aa", "aaa")), "     ")
        self.assertEqual(mask("x ABC abc y", ("ABC",)), "x     abc y")  # case-sensitive
        self.assertEqual(mask("a\\1 $1 [x] (.*) a\\1", ("a\\1", "$1", "[x]", "(.*)")), " " * 19)
        self.assertEqual(mask("a.b axb", (".",)), "a b axb")  # a title is a literal, not a pattern
        self.assertEqual(mask("keep ABC", ()), "keep ABC")

    def test_title_masks_preserve_unicode_whitespace_and_quote_precedence(self):
        mask = plain_language._skipped_text
        text = "x ABC\u00a0ABC\r\nABC\tABC\U0001F600 ABC\u0301 y"
        expected = "x " + "   \u00a0   \r\n   \t   \U0001F600    \u0301 y"
        masked = mask(text, ("ABC",))
        self.assertEqual(masked, expected)
        self.assertEqual(len(masked), len(text))
        self.assertTrue(all(masked[i] == c for i, c in enumerate(text) if c.isspace()))
        self.assertEqual(mask("a\u2028b\u00a0c", ("\u2028", "\u00a0")), "a\u2028b\u00a0c")
        self.assertEqual(mask('"ABC" ABC XYZ.', ("ABC",)), " " * 10 + "XYZ.")
        self.assertEqual(mask("\u201cA ABC\u201d ABC", ("ABC",)), " " * 11)
        self.assertEqual(mask('ABC "ABC', ("ABC",)), '    "   ')  # an unmatched quote skips nothing
        # a title that only exists across an earlier quote mask is not found (masks are visible to later titles)
        self.assertEqual(mask('ab "cd" ef', ('b "c',)), "ab      ef")

    def test_skipped_title_words_retain_original_positions(self):
        text = "ABC ABC one two. ABC\n\nABC ABC\n\nXYZ end."
        tokens = plain_language._tokens("body_md", text, ("ABC",))
        self.assertEqual([(t["paragraph"], t["word"], t["start"], t["end"], t["skipped"], t["sentence"]) for t in tokens],
                         [(1, 1, 0, 3, True, None), (1, 2, 4, 7, True, None), (1, 3, 8, 11, False, 1),
                          (1, 4, 12, 15, False, 1), (1, 5, 17, 20, True, None), (2, 6, 22, 25, True, None),
                          (2, 7, 26, 29, True, None), (3, 8, 31, 34, False, 2), (3, 9, 35, 38, False, 2)])
        rows = check_plain(finding(title="", summary="", body_md=text, sources=[{"title": "ABC"}]))
        self.assertEqual(rows, [{"rule": "unexplained_abbreviation", "field": "body_md", "paragraph": 3, "sentence": 2,
                                 "word": 8, "start": 31, "end": 34, "word_count": None, "limit": None,
                                 "abbreviation": "XYZ"}])
        dense = finding(title="", summary="", body_md="ABC " * 500)
        self.assertIn(("long_paragraph", "body_md"), rules(check_plain(dense)))
        self.assertEqual(check_plain({**dense, "sources": [{"title": "ABC"}]}), [])

    def test_no_title_and_malformed_source_compatibility(self):
        body = 'He said "ABC def" and ' + sentence(31) + " ABC ok."
        base = check_plain(finding(body_md=body))
        self.assertEqual(rules(base), [("long_sentence", "body_md"), ("unexplained_abbreviation", "body_md")])
        self.assertEqual(base, check_plain(finding(body_md=body, sources=[{"sha256": "a" * 64}, {"title": ""}, "x"])))


class DenseSourceTitleCommandTests(Case):
    def test_dense_source_title_finding_cli_is_complete_and_read_only(self):
        body = "ABC " * 200000 + "XYZ."
        draft = self.draft(id="syn-title-scale", title="", summary="", body_md=body, sources=[{"title": "ABC"}])
        self.assertLess(draft.stat().st_size, findings.MAX_DRAFT_BYTES)
        row = {"rule": "unexplained_abbreviation", "field": "body_md", "paragraph": 1, "sentence": 1, "word": 200001,
               "start": 800000, "end": 800003, "word_count": None, "limit": None, "abbreviation": "XYZ"}
        result = self.cli(draft, "--json", returncode=3)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.count("\n"), 1)
        report = json.loads(result.stdout)
        self.assertEqual(report, {"advisory": True, "finding_id": "syn-title-scale", "warnings": [row]})
        self.assertEqual(list(report["warnings"][0]), KEYS)
        text = self.cli(draft, returncode=3)
        self.assertEqual(text.stderr, "")
        self.assertEqual(text.stdout.splitlines()[0], FIRST)
        self.assertEqual(text.stdout.splitlines()[1:-1], [plain_language.row_text(row)])
        self.assertTrue(text.stdout.splitlines()[-1].endswith(LAST))
        clean = self.draft("skipped.json", id="syn-title-clean", title="", summary="", body_md="ABC " * 200000,
                           sources=[{"title": "ABC"}])
        self.assertEqual(json.loads(self.cli(clean, "--json", returncode=0).stdout),
                         {"advisory": True, "finding_id": "syn-title-clean", "warnings": []})


if __name__ == "__main__":
    unittest.main()
