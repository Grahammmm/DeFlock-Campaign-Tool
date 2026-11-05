"""DF1622-07: `content check-plain --directory D [--json]`, the advisory plain-language check of site content.

Synthetic data only. Run from the repository root: python3 -B -m unittest tests.test_content_plain -v
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import content_plain, plain_language  # noqa: E402
from campaign_tool.plain_language import check_plain  # noqa: E402

ROW_KEYS = ["file", "entry", "rule", "field", "paragraph", "sentence", "word", "start", "end", "word_count", "limit",
            "abbreviation"]
FIRST, LAST = plain_language.HEADING, "suggestion(s). Nothing was changed."


def sentence(count, word="alpha"):
    return " ".join([word] * count) + "."


class Case(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-content-plain-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(os.path.realpath(temporary.name))
        self.root = self.base / "campaign"
        (self.root / "content").mkdir(parents=True)
        self.write(self.root / "campaign.json", {"schema_version": 1, "name": "Synthetic", "county": "Synthetic", "state": "CA"})

    def write(self, path, document):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(document if isinstance(document, bytes) else
                         (document if isinstance(document, str) else json.dumps(document, ensure_ascii=False)).encode("utf-8"))
        return path

    def page(self, slug, title="Short title", body="Short body.", **extra):
        return self.write(self.root / "content" / "pages" / f"{slug}.json", {"title": title, "body_md": body, **extra})

    def faq(self, *pairs):
        return self.write(self.root / "content" / "faq.json", {"schema_version": 1, "entries": [
            {"question": q, "answer_md": a} for q, a in pairs]})

    def glossary(self, *terms):
        return self.write(self.root / "content" / "glossary.json", {"schema_version": 1, "terms": [
            {"term": t, "definition_md": d, **({"aliases": al} if al else {})} for t, d, al in terms]})

    def snapshot(self):
        rows = []
        for path in sorted(self.base.rglob("*")):
            info = path.lstat()
            rows.append((str(path.relative_to(self.base)), info.st_mode, info.st_mtime_ns,
                         path.read_bytes() if path.is_file() and not path.is_symlink() else None))
        return rows

    def cli(self, *arguments, returncode, directory=True, command=("content", "check-plain")):
        before = self.snapshot()
        args = [sys.executable, "-B", "-m", "campaign_tool", *command, *map(str, arguments)]
        if directory:
            args += ["--directory", str(self.root)]
        result = subprocess.run(args, cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, returncode, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.snapshot(), before, "content check-plain must not write, touch or create anything")
        if returncode == 1:
            self.assertEqual(result.stdout, "", "a refusal prints no partial report")
            self.assertTrue(result.stderr.startswith("Stopped: "), result.stderr)
        return result

    def report(self):
        return json.loads(self.cli("--json", returncode=0).stdout)

    def refused(self, fragment):
        result = self.cli(returncode=1)
        self.assertIn(fragment, result.stderr)
        return result


class SurfaceTests(Case):
    def test_all_surfaces_original_field_names_and_source_positions(self):
        self.page("b-page", title=sentence(31), body="Fine.\n\n" + sentence(151))
        self.page("a-page", body="The ALPR system.")
        self.faq(("Why?", "Short."), ("What is LPR?", sentence(31)))
        self.glossary(("Zed", "Plain.", None), ("Alpha", "The XYZ rule.", None))
        report = self.report()
        self.assertEqual(list(report), ["advisory", "warnings"])
        self.assertIs(report["advisory"], True)
        got = [(r["file"], r["entry"], r["rule"], r["field"], r["paragraph"], r["sentence"], r["word"])
               for r in report["warnings"]]
        self.assertEqual(got, [
            ("content/pages/a-page.json", "a-page", "unexplained_abbreviation", "body_md", 1, 1, 2),
            ("content/pages/b-page.json", "b-page", "long_sentence", "title", 1, 1, 1),
            ("content/pages/b-page.json", "b-page", "long_sentence", "body_md", 2, 2, 2),
            ("content/pages/b-page.json", "b-page", "long_paragraph", "body_md", 2, 2, 2),
            ("content/faq.json", "entries[1]", "unexplained_abbreviation", "question", 1, 1, 3),
            ("content/faq.json", "entries[1]", "long_sentence", "answer_md", 1, 1, 1),
            ("content/glossary.json", "terms[1]", "unexplained_abbreviation", "definition_md", 1, 1, 2),
        ])
        for row in report["warnings"]:
            self.assertEqual(list(row), ROW_KEYS)
        self.assertNotIn(str(self.base), json.dumps(report))
        self.assertNotIn("alpha alpha", json.dumps(report))  # no raw prose in rows

    def test_shared_thresholds_and_no_summary_rule(self):
        self.page("edge", title=sentence(30), body=sentence(30) + " " + sentence(31))
        self.faq(("Q", sentence(61)))
        self.glossary(("T", sentence(150), None))
        warnings = self.report()["warnings"]
        self.assertEqual([(r["entry"], r["rule"], r["field"], r["word_count"]) for r in warnings],
                         [("edge", "long_sentence", "body_md", 31), ("entries[0]", "long_sentence", "answer_md", 61),
                          ("terms[0]", "long_sentence", "definition_md", 150)])
        self.assertNotIn("long_summary", {r["rule"] for r in warnings})
        self.glossary(("T", sentence(151), None))
        rows = [(r["rule"], r["word_count"]) for r in self.report()["warnings"] if r["entry"] == "terms[0]"]
        self.assertEqual(rows, [("long_sentence", 151), ("long_paragraph", 151)])
        self.assertEqual(len(self.report()["warnings"]), 4)

    def test_same_rules_as_finding_checker(self):
        body = sentence(31) + " The ALPR (automated license plate reader) and ALPR again. “QUOTED ABC” é😀 XYZ."
        self.page("p", title="T", body=body)
        expected = check_plain({"title": "T", "summary": "", "body_md": body})
        got = [{k: v for k, v in r.items() if k not in ("file", "entry")} for r in self.report()["warnings"]]
        self.assertEqual(got, [r for r in expected if r["field"] != "summary"])

    def test_unicode_crlf_markdown_quotes_and_offsets(self):
        body = ("Emoji 😀 XYZ here.\r\n\r\n[link](https://example.test/a_b-c) and don't \"SKIP ME\" ABC "
                "“ALSO QRS” done.\r\n\r\nUnmatched \"QQQ rest")
        self.page("u", body=body)
        warnings = self.report()["warnings"]
        self.assertEqual([(r["abbreviation"], r["paragraph"], r["word"], r["start"], r["end"]) for r in warnings],
                         [("XYZ", 1, 2, body.index("XYZ"), body.index("XYZ") + 3),
                          ("ABC", 2, 14, body.index(" ABC ") + 1, body.index(" ABC ") + 4)] + [
                             ("QQQ", 3, 19, body.index("QQQ"), body.index("QQQ") + 3)])
        self.assertTrue(all(body[r["start"]:r["end"]] == r["abbreviation"] for r in warnings))

    def test_expansion_scope_and_glossary_aliases(self):
        self.page("one", title="Automated license plate reader (ALPR)", body="The ALPR is on. The FBI is not.")
        self.page("two", body="ALPR again.")
        self.faq(("Reader ALPR?", "Reverse form: ALPR (automated license plate reader)."),
                 ("Later", "ALPR once more."))
        self.glossary(("Camera", "A CCTV camera. Use CCTV freely.", ["CCTV"]), ("Other", "A DMV form.", None))
        got = [(r["file"], r["entry"], r["field"], r["abbreviation"]) for r in self.report()["warnings"]]
        self.assertEqual(got, [
            ("content/pages/one.json", "one", "body_md", "FBI"),
            ("content/pages/two.json", "two", "body_md", "ALPR"),
            ("content/faq.json", "entries[0]", "question", "ALPR"),
            ("content/faq.json", "entries[1]", "answer_md", "ALPR"),
            ("content/glossary.json", "terms[1]", "definition_md", "DMV"),
        ])

    def test_empty_optional_content_and_unfinished_prose(self):
        clean = self.cli(returncode=0).stdout
        self.assertEqual(clean, f"{FIRST}\n0 {LAST}\n")
        self.assertEqual(self.report(), {"advisory": True, "warnings": []})
        (self.root / "content" / "pages").mkdir()
        self.page("blank", title="", body="", description="XYZ " + sentence(40), notice_md="ABC")
        self.write(self.root / "content" / "pages" / "notes.txt", "not json")
        self.write(self.root / "content" / "site.json", {"anything": "XYZ"})
        self.faq(("[EDIT ME: question]", "[EDIT ME: answer]"))
        self.glossary(("XYZ", "[EDIT ME]", ["XYZ"]))
        self.assertEqual([(r["file"], r["entry"], r["abbreviation"]) for r in self.report()["warnings"]],
                         [("content/faq.json", "entries[0]", a) for a in ("EDIT", "ME") * 2]
                         + [("content/glossary.json", "terms[0]", a) for a in ("EDIT", "ME")])

    def test_text_format(self):
        self.page("a", body="The ALPR. " + sentence(31))
        out = self.cli(returncode=0).stdout.splitlines()
        self.assertEqual(out[0], FIRST)
        self.assertEqual(out[1], "content/pages/a.json a: body_md paragraph 1, sentence 1, word 2: "
                                 "unexplained_abbreviation ALPR (not spelled out earlier)")
        self.assertEqual(out[2], "content/pages/a.json a: body_md paragraph 1, sentence 2, word 3: long_sentence (31 words; limit 30)")
        self.assertEqual(out[-1], f"2 {LAST}")


class ReadTests(Case):
    def test_fail_closed_named_reads(self):
        pages = self.root / "content" / "pages"
        self.page("ok", body="XYZ")
        bad = pages / "bad.json"
        for payload, why in ((b"\xff\xfe", "not valid UTF-8 JSON"), (b"{not json", "not valid UTF-8 JSON"),
                             (b"[]", "title must be a string"), (b'{"title": 1, "body_md": ""}', "title must be a string"),
                             (b'{"title": "t"}', "body_md must be a string"),
                             (b'{"title": "t", "body_md": null}', "body_md must be a string"),
                             (b"x" * (2 * 1024 * 1024 + 1), "file too large")):
            with self.subTest(why=why, payload=payload[:8]):
                bad.write_bytes(payload)
                result = self.refused("content/pages/bad.json: " + why)
                self.assertNotIn(str(self.base), result.stderr)
                self.assertNotIn("XYZ", result.stderr)
        bad.unlink()
        for text, why in ((json.dumps({"entries": {}}), "content/faq.json: entries must be a list"),
                          (json.dumps({"entries": [1]}), "content/faq.json entries[0]: must be an object"),
                          (json.dumps({"entries": [{"question": "q"}]}), "entries[0]: answer_md must be a string")):
            self.write(self.root / "content" / "faq.json", text)
            self.refused(why)
        (self.root / "content" / "faq.json").unlink()
        for text, why in ((json.dumps({"terms": [{"definition_md": "d", "aliases": "ABC"}]}), "terms[0]: aliases must be a list of strings"),
                          (json.dumps({"terms": [{"definition_md": "d", "aliases": [1]}]}), "aliases must be a list of strings"),
                          (json.dumps({"terms": [{}]}), "terms[0]: definition_md must be a string"),
                          (json.dumps([]), "content/glossary.json: terms must be a list")):
            self.write(self.root / "content" / "glossary.json", text)
            self.refused(why)

    def test_links_directories_and_fifos_are_refused_not_followed(self):
        pages = self.root / "content" / "pages"
        self.page("ok")
        target = self.write(self.base / "outside.json", {"title": "ABC", "body_md": "x"})
        link = pages / "link.json"
        link.symlink_to(target)
        self.refused("content/pages/link.json: must be a regular file")
        link.unlink()
        link.symlink_to(self.base / "missing.json")
        self.refused("content/pages/link.json: must be a regular file")
        link.unlink()
        (pages / "dir.json").mkdir()
        self.refused("content/pages/dir.json: must be a regular file")
        (pages / "dir.json").rmdir()
        os.mkfifo(pages / "fifo.json")
        self.refused("content/pages/fifo.json: must be a regular file")
        os.unlink(pages / "fifo.json")
        self.write(self.root / "content" / "faq.json", {"entries": []})
        (self.root / "content" / "faq.json").unlink()
        (self.root / "content" / "faq.json").symlink_to(self.base / "missing.json")
        self.refused("content/faq.json: must be a regular file")
        (self.root / "content" / "faq.json").unlink()
        os.mkfifo(self.root / "content" / "glossary.json")
        self.refused("content/glossary.json: must be a regular file")

    def test_symlinked_or_non_folder_ancestors_and_unreadable_files(self):
        elsewhere = self.base / "elsewhere"
        self.write(elsewhere / "a.json", {"title": "t", "body_md": "b"})
        (self.root / "content" / "pages").symlink_to(elsewhere)
        self.refused("content/pages: must be a real folder")
        (self.root / "content" / "pages").unlink()
        self.write(self.root / "content" / "pages", "a file")
        self.refused("content/pages: must be a real folder")
        (self.root / "content" / "pages").unlink()
        moved = self.base / "moved"
        (self.root / "content").rename(moved)
        (self.root / "content").symlink_to(moved)
        self.refused("content: must be a real folder")
        (self.root / "content").unlink()
        moved.rename(self.root / "content")
        self.page("ok")
        with mock.patch.object(content_plain, "_read_json", side_effect=PermissionError("denied /secret")):
            with self.assertRaises(content_plain.ContentError) as caught:
                content_plain.check_content(self.root)
        self.assertEqual(str(caught.exception), "content/pages/ok.json: could not be read")

    def test_no_partial_report_when_a_later_file_is_bad(self):
        self.page("a", body="The ALPR.")
        self.write(self.root / "content" / "glossary.json", b"{")
        self.refused("content/glossary.json: not valid UTF-8 JSON")


class CliTests(Case):
    def test_cli_text_json_exits_and_named_campaign(self):
        self.page("a", body="The ALPR.")
        self.assertEqual(self.report()["warnings"][0]["entry"], "a")
        text = self.cli(returncode=0)
        self.assertIn("1 suggestion(s)", text.stdout)
        self.cli("--bogus", returncode=2)
        self.cli(returncode=2, directory=False)
        self.cli("extra", returncode=2)
        empty = self.base / "not-a-campaign"
        empty.mkdir()
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "content", "check-plain", "--directory", str(empty)],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertTrue(result.stderr.startswith("Stopped: "), result.stderr)

    def test_json_is_one_utf8_line(self):
        self.page("u", title="Café", body="😀 ABC")
        raw = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "content", "check-plain", "--json", "--directory",
                              str(self.root)], cwd=REPO_ROOT, capture_output=True, timeout=60, env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        self.assertEqual(raw.returncode, 0)
        self.assertEqual(raw.stdout.count(b"\n"), 1)
        self.assertEqual(json.loads(raw.stdout.decode("utf-8"))["warnings"][0]["start"], 2)


class IsolationTests(Case):
    def finding_cli(self, *arguments):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "finding", "check-plain", *arguments], cwd=REPO_ROOT,
                              capture_output=True, text=True, timeout=60)

    def test_finding_bytes_and_no_gate_coupling(self):
        draft = self.write(self.base / "draft.json", {"id": "syn-1", "title": "Synthetic", "summary": sentence(61),
                                                      "body_md": "The ALPR.", "slug": "s", "state": "draft", "sources": [],
                                                      "corrections": []})
        text = self.finding_cli(str(draft), "--directory", str(self.root))
        self.assertEqual(text.returncode, 3)
        self.assertEqual(text.stdout, "\n".join([
            FIRST,
            "summary paragraph 1, sentence 1, word 1: long_sentence (61 words; limit 30)",
            "summary paragraph 1, sentence 1, word 1: long_summary (61 words; limit 60)",
            "body_md paragraph 1, sentence 1, word 2: unexplained_abbreviation ALPR (not spelled out earlier)",
            f"3 {LAST}"]) + "\n")
        as_json = self.finding_cli(str(draft), "--directory", str(self.root), "--json")
        self.assertEqual(as_json.returncode, 3)
        report = json.loads(as_json.stdout)
        self.assertEqual(list(report), ["advisory", "finding_id", "warnings"])
        self.assertEqual([r["rule"] for r in report["warnings"]], ["long_sentence", "long_summary", "unexplained_abbreviation"])
        self.assertEqual(list(report["warnings"][0]), ["rule", "field", "paragraph", "sentence", "word", "start", "end",
                                                       "word_count", "limit", "abbreviation"])

    def test_no_network_and_nothing_imports_the_adapter_but_cli(self):
        self.page("a", body="The ALPR.")
        with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network")):
            self.assertEqual(len(content_plain.check_content(self.root)["warnings"]), 1)
        users = sorted(p.name for p in (REPO_ROOT / "campaign_tool").glob("*.py")
                       if p.name != "content_plain.py" and "content_plain" in p.read_text(encoding="utf-8"))
        self.assertEqual(users, ["cli.py"])
        self.assertNotIn("open(", (REPO_ROOT / "campaign_tool" / "content_plain.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
