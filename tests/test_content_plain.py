"""DF1622-07: `content check-plain --directory D [--json]`, the advisory plain-language check of site content.

Synthetic data only. Run from the repository root: python3 -B -m unittest tests.test_content_plain -v
"""
import errno
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
        with mock.patch.object(content_plain.os, "open", side_effect=PermissionError("denied /secret")):
            with self.assertRaises(content_plain.ContentError) as caught:
                content_plain.check_content(self.root)
        self.assertEqual(str(caught.exception), "content/pages/ok.json: could not be read")

    def test_no_partial_report_when_a_later_file_is_bad(self):
        self.page("a", body="The ALPR.")
        self.write(self.root / "content" / "glossary.json", b"{")
        self.refused("content/glossary.json: not valid UTF-8 JSON")


CAP = content_plain.MAX_JSON_BYTES
SURFACES = ("page", "faq", "glossary")


class BoundedReadTests(Case):
    def target(self, kind):
        return {"page": self.root / "content" / "pages" / "probe.json",
                "faq": self.root / "content" / "faq.json",
                "glossary": self.root / "content" / "glossary.json"}[kind]

    def document(self, kind):
        body = "Café ALPR. 😀"
        return {"page": {"title": "Café", "body_md": body},
                "faq": {"entries": [{"question": "Café?", "answer_md": body}]},
                "glossary": {"terms": [{"term": "T", "definition_md": body}]}}[kind]

    def put(self, kind, size=None):
        raw = json.dumps(self.document(kind), ensure_ascii=False).encode("utf-8")
        if size is not None:
            self.assertGreaterEqual(size, len(raw))
            raw += b" " * (size - len(raw))
        self.write(self.target(kind), raw)
        return self.target(kind)

    def recording_read(self):
        calls = []
        real = os.read

        def spy(fd, count):
            data = real(fd, count)
            calls.append((count, len(data)))
            return data
        return calls, spy

    def test_static_oversize_page_faq_glossary_refuse_before_payload_read(self):
        for kind in SURFACES:
            with self.subTest(kind=kind):
                self.put(kind, CAP + 1)
                calls, spy = self.recording_read()
                with mock.patch.object(content_plain.os, "read", side_effect=spy), \
                        mock.patch.object(Path, "read_bytes", side_effect=AssertionError("legacy full read")):
                    with self.assertRaises(content_plain.ContentError) as caught:
                        content_plain.check_content(self.root)
                self.assertEqual(calls, [], "an oversized descriptor is refused before any payload read")
                self.assertTrue(str(caught.exception).endswith(": file too large"), str(caught.exception))
                result = self.refused(str(self.target(kind).relative_to(self.root)) + ": file too large")
                self.assertNotIn(str(self.base), result.stderr)
                self.target(kind).unlink()

    def test_exact_cap_and_one_under_accept_utf8_bytes(self):
        for kind in SURFACES:
            self.put(kind)
            control = self.report()
            self.assertEqual(len(control["warnings"]), 1)
            for size in (CAP - 1, CAP):
                with self.subTest(kind=kind, size=size):
                    path = self.put(kind, size)
                    self.assertEqual(path.stat().st_size, size)
                    self.assertLess(len(path.read_text(encoding="utf-8")), size, "multibyte text: bytes, not characters")
                    self.assertEqual(self.report(), control)
            self.target(kind).unlink()

    def test_growth_after_real_fstat_is_capped_and_refused(self):
        for kind in SURFACES:
            with self.subTest(kind=kind):
                path = self.put(kind)
                real_fstat, grown = os.fstat, []

                def fstat(fd):
                    info = real_fstat(fd)
                    if not grown:
                        grown.append(True)
                        with open(path, "ab") as handle:
                            handle.write(b" " * (CAP + 4096))
                    return info  # the pre-growth result

                calls, spy = self.recording_read()
                with mock.patch.object(content_plain.os, "fstat", side_effect=fstat), \
                        mock.patch.object(content_plain.os, "read", side_effect=spy), \
                        mock.patch.object(content_plain.json, "loads", side_effect=AssertionError("parsed")):
                    with self.assertRaises(content_plain.ContentError) as caught:
                        content_plain.check_content(self.root)
                self.assertTrue(str(caught.exception).endswith(": file too large"), str(caught.exception))
                self.assertEqual(sum(got for _, got in calls), CAP + 1)
                served = 0
                for asked, got in calls:
                    self.assertLessEqual(asked, CAP + 1 - served, "each request fits the remaining allowance")
                    served += got
                path.unlink()

    def test_short_reads_and_read_failure_close_descriptor(self):
        self.put("page")
        self.faq(("Q", "Short."))
        real_open, real_close, real_read = os.open, os.close, os.read
        opened, closed = [], []

        def spy_open(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            return fd

        def spy_close(fd):
            closed.append(fd)
            return real_close(fd)

        def short(fd, count):
            return real_read(fd, min(count, 7))

        with mock.patch.object(content_plain.os, "open", side_effect=spy_open), \
                mock.patch.object(content_plain.os, "close", side_effect=spy_close), \
                mock.patch.object(content_plain.os, "read", side_effect=short):
            self.assertEqual(len(content_plain.check_content(self.root)["warnings"]), 1)
        self.assertEqual(len(opened), 2)
        self.assertEqual(sorted(opened), sorted(closed))

        opened.clear()
        closed.clear()
        reads = []

        def failing(fd, count):
            reads.append(count)
            if len(reads) == 2:
                raise OSError(errno.EIO, "injected /secret failure")
            return real_read(fd, min(count, 7))

        with mock.patch.object(content_plain.os, "open", side_effect=spy_open), \
                mock.patch.object(content_plain.os, "close", side_effect=spy_close), \
                mock.patch.object(content_plain.os, "read", side_effect=failing):
            with self.assertRaises(content_plain.ContentError) as caught:
                content_plain.check_content(self.root)
        self.assertEqual(str(caught.exception), "content/pages/probe.json: could not be read")
        self.assertEqual(len(opened), 1, "no second open or fallback full read")
        self.assertEqual(opened, closed)
        self.assertEqual(len(reads), 2)

    def test_leaf_swap_to_link_or_fifo_is_prompt_refusal(self):
        outside = self.write(self.base / "outside.json", {"title": "ABC", "body_md": "XYZ"})
        for how in ("symlink", "fifo", "directory"):
            with self.subTest(how=how):
                path = self.put("page")
                real_open, calls = os.open, []

                def swap(target, *args, **kwargs):
                    os.unlink(target)
                    if how == "symlink":
                        os.symlink(outside, target)
                    elif how == "fifo":
                        os.mkfifo(target)
                    else:
                        os.mkdir(target)
                    return real_open(target, *args, **kwargs)

                reads, spy = self.recording_read()
                with mock.patch.object(content_plain.os, "open", side_effect=swap), \
                        mock.patch.object(content_plain.os, "read", side_effect=spy):
                    with self.assertRaises(content_plain.ContentError) as caught:
                        content_plain.check_content(self.root)
                self.assertEqual(str(caught.exception), "content/pages/probe.json: " + content_plain.NOT_REGULAR)
                self.assertEqual(reads, [], "neither the outside file nor the FIFO was read")
                if how == "directory":
                    path.rmdir()
                else:
                    path.unlink()
        self.assertEqual(outside.read_bytes(), b'{"title": "ABC", "body_md": "XYZ"}')

    def test_later_oversize_source_has_no_partial_text_or_json(self):
        for kind in ("faq", "glossary"):
            with self.subTest(kind=kind):
                self.page("first", body="The ALPR. " + sentence(31))
                self.put(kind, CAP + 1)
                for mode in ((), ("--json",)):
                    result = self.cli(*mode, returncode=1)
                    self.assertEqual(result.stdout, "")
                    self.assertIn(f"{self.target(kind).relative_to(self.root)}: file too large", result.stderr)
                    self.assertNotIn(str(self.base), result.stderr)
                    self.assertNotIn("ALPR", result.stderr)
                self.target(kind).unlink()


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

    def test_advisory_and_isolation_contract_survives_bounded_reader(self):
        self.page("a", body="The ALPR.")
        self.faq(("Q", "Short."))
        self.glossary(("T", "Plain.", None))
        real_open, flags_seen = os.open, []

        def spy(path, flags, *args, **kwargs):
            flags_seen.append(flags)
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(content_plain.os, "open", side_effect=spy):
            self.assertEqual(len(content_plain.check_content(self.root)["warnings"]), 1)
        self.assertEqual(len(flags_seen), 3)
        forbidden = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND | os.O_EXCL
        for flags in flags_seen:
            self.assertEqual(flags & forbidden, 0, "production opens are read-only")
            self.assertEqual(flags & (os.O_NOFOLLOW | os.O_NONBLOCK), os.O_NOFOLLOW | os.O_NONBLOCK)


DEEP = 10000
TOO_DEEP = "{}: JSON nesting too deep"


class NestingTests(Case):
    """RF2345-5: a decoder RecursionError is a fixed named refusal, never a traceback."""

    target, document, put = BoundedReadTests.target, BoundedReadTests.document, BoundedReadTests.put
    LABELS = {"page": "content/pages/probe.json", "faq": "content/faq.json", "glossary": "content/glossary.json"}

    def deep_bytes(self, kind, opener="[", closer="]"):
        """A usable document whose ignored `extra` field nests DEEP containers (built by repetition, not recursion)."""
        nested = (opener if opener == "[" else '{"k":') * DEEP + "0" + closer * DEEP
        base = json.dumps(self.document(kind), ensure_ascii=False)
        raw = (base[:-1] + ', "extra": ' + nested + "}").encode("utf-8")
        self.assertLess(len(raw), CAP)
        return raw

    def test_real_parser_excessive_nesting_refuses_page_faq_glossary(self):
        for kind in SURFACES:
            for opener, closer in (("[", "]"), ("{", "}")):
                with self.subTest(kind=kind, opener=opener):
                    self.write(self.target(kind), self.deep_bytes(kind, opener, closer))
                    with self.assertRaises(content_plain.ContentError) as caught:
                        content_plain.check_content(self.root)
                    self.assertEqual(str(caught.exception), TOO_DEEP.format(self.LABELS[kind]))
                    for mode in ((), ("--json",)):
                        result = self.cli(*mode, returncode=1)
                        self.assertEqual(result.stderr, "Stopped: " + TOO_DEEP.format(self.LABELS[kind]) + "\n")
                    self.target(kind).unlink()

    def test_decoder_recursion_refusal_is_fixed_and_suppresses_cause(self):
        for kind in SURFACES:
            with self.subTest(kind=kind):
                self.put(kind)
                marker = RecursionError("PRIVATE-MARKER /home/secret/path")
                with mock.patch.object(content_plain.json, "loads", side_effect=marker):
                    with self.assertRaises(content_plain.ContentError) as caught:
                        content_plain.check_content(self.root)
                self.assertEqual(str(caught.exception), TOO_DEEP.format(self.LABELS[kind]))
                self.assertIsNone(caught.exception.__cause__)
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertNotIn("PRIVATE-MARKER", str(caught.exception))
                self.target(kind).unlink()

    def test_later_nested_source_emits_no_partial_advisory(self):
        for kind in SURFACES:
            with self.subTest(kind=kind):
                self.page("first", body="The ALPR. " + sentence(31))
                self.write(self.target(kind), self.deep_bytes(kind))
                for mode in ((), ("--json",)):
                    result = self.cli(*mode, returncode=1)
                    self.assertEqual(result.stdout, "")
                    self.assertIn(TOO_DEEP.format(self.LABELS[kind]), result.stderr)
                    self.assertNotIn(str(self.base), result.stderr)
                    self.assertNotIn("ALPR", result.stderr)
                self.target(kind).unlink()

    def test_parser_failure_closes_descriptor_once_and_does_not_reopen(self):
        self.write(self.target("page"), self.deep_bytes("page"))
        real_open, real_close = os.open, os.close
        opened, closed = [], []

        def spy_open(*args, **kwargs):
            opened.append(real_open(*args, **kwargs))
            return opened[-1]

        def spy_close(fd):
            closed.append(fd)
            return real_close(fd)

        with mock.patch.object(content_plain.os, "open", side_effect=spy_open), \
                mock.patch.object(content_plain.os, "close", side_effect=spy_close):
            with self.assertRaises(content_plain.ContentError):
                content_plain.check_content(self.root)
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened, closed)

    def test_oversize_still_refused_before_the_parser(self):
        self.put("page", CAP + 1)
        with mock.patch.object(content_plain.json, "loads", side_effect=AssertionError("parsed")):
            with self.assertRaises(content_plain.ContentError) as caught:
                content_plain.check_content(self.root)
        self.assertEqual(str(caught.exception), "content/pages/probe.json: file too large")

    def test_moderate_nested_metadata_and_shallow_reports_are_unchanged(self):
        self.page("a", body="The ALPR.", meta={"a": [[{"b": [1, 2]}], "]\"["], "c": {}, "d": []})
        self.faq(("Q", "Short."))
        report = self.report()
        self.assertEqual([row["rule"] for row in report["warnings"]], ["unexplained_abbreviation"])
        self.assertEqual(self.cli(returncode=0).stdout.splitlines()[0], FIRST)

    def test_only_decoder_recursion_is_normalized(self):
        self.put("page")
        for error in (RuntimeError("boom"), MemoryError()):
            with mock.patch.object(content_plain.json, "loads", side_effect=error):
                with self.assertRaises(type(error)):
                    content_plain.check_content(self.root)
        with mock.patch.object(content_plain.plain_language, "check_fields", side_effect=RecursionError("engine")):
            with self.assertRaises(RecursionError):
                content_plain.check_content(self.root)
        self.write(self.target("page"), b"{")
        with self.assertRaises(content_plain.ContentError) as caught:
            content_plain.check_content(self.root)
        self.assertIn("not valid UTF-8 JSON", str(caught.exception))


class LargePageTests(Case):
    def test_large_page_uses_shared_engine_without_truncation(self):
        self.page('scale', title='Synthetic', body='word. ' * 16000)
        result = self.cli('--json', returncode=0)
        self.assertEqual(result.stderr, '')
        self.assertEqual(result.stdout.count('\n'), 1)
        self.assertEqual(json.loads(result.stdout), {'advisory': True, 'warnings': [{
            'file': 'content/pages/scale.json', 'entry': 'scale', 'rule': 'long_paragraph',
            'field': 'body_md', 'paragraph': 1, 'sentence': 1, 'word': 1, 'start': 0,
            'end': 95998, 'word_count': 16000, 'limit': 150, 'abbreviation': None}]})


if __name__ == "__main__":
    unittest.main()
