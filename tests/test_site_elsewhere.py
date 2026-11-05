"""DF1107-04: organizer-verified decisions elsewhere, content/elsewhere.json -> /elsewhere.html, content-check, legal notes.

Synthetic places on reserved .test hosts; sockets are blocked, nothing is fetched. The no-file render is compared with the
independently pinned digest table of tests/test_site_faq.py (the example has no elsewhere file), not with the candidate renderer.
"""
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool.content import RESERVED_PAGE_SLUGS, STARTER_SLUGS, ContentError, load_content
from campaign_tool.content_check import content_problems, problem_record, run_cli
from campaign_tool.content_elsewhere import KINDS, load_elsewhere, read_elsewhere
from campaign_tool.labels import DEFAULTS
from campaign_tool.legal_claims import elsewhere_notes, site_notes
from campaign_tool.site import GENERATED_TOP_LEVEL, build_site
from campaign_tool.site_check import check_accessibility, check_links
from tests.test_site_faq import BASELINE

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
LD_JSON = re.compile(rb'  <script type="application/ld\+json">[^<]*</script>\n')  # DF1407-08: inert data, absent from the pre-feature pins
BUILD_DAY = date(2026, 10, 1)  # the day BASELINE was rendered on
NO_SOCKETS = mock.patch("socket.socket", side_effect=AssertionError("network access attempted"))
FOOTER = '<p><a href="/elsewhere.html">Decisions elsewhere</a></p>'
NOTE = "Decisions elsewhere are not decisions by this campaign's agencies."
LOC = re.compile(r"<loc>([^<]+)</loc>")
LABEL_KEYS = ["elsewhere.title", "footer.elsewhere", "elsewhere.description", "elsewhere.note", "elsewhere.verified",
              "elsewhere.decided", "elsewhere.source", "elsewhere.news", "elsewhere.checked",
              *(f"elsewhere.kind.{kind}" for kind in KINDS)]
ACCUSE = "The county violated SB 34 by sharing plate data."


def entry(number=1, **extra):
    row = {"place": f"Springfield {number}", "state": "OR", "body": "Springfield City Council", "decided_on": "2026-03-10",
           "kind": "paused", "as_stated": "The council voted to pause the contract.",
           "source_url": f"https://records.example.test/minutes/{number}", "checked_on": "2026-09-15"}
    row.update(extra)
    return {key: value for key, value in row.items() if value is not ...}  # ``key=...`` drops a field


class Case(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-elsewhere-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content, self.public = self.root / "content", self.root / "public"
        self.path = self.content / "elsewhere.json"
        NO_SOCKETS.start()
        self.addCleanup(NO_SOCKETS.stop)

    def put(self, value, name="elsewhere.json"):
        path = self.content / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode("utf-8"))
        return path

    def site_json(self, **changes):
        path = self.content / "site.json"
        site = json.loads(path.read_text(encoding="utf-8"))
        for key, value in changes.items():
            site.pop(key, None) if value is None else site.__setitem__(key, value)
        path.write_text(json.dumps(site), encoding="utf-8")

    def build(self, day=BUILD_DAY):
        build_site(self.root, today=day)
        return self.tree()

    def tree(self):
        return {p.relative_to(self.public).as_posix(): LD_JSON.sub(b"", p.read_bytes()) for p in sorted(self.public.rglob("*")) if p.is_file()}

    def digests(self, files):
        return {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}

    @staticmethod
    def without_catalogue(files):
        """DF2207-12 adds exactly two outputs, one footer paragraph and one sitemap URL; remove only those, keep every pinned digest."""
        data_link = b'<p><a href="/data.html">Open data</a></p>'
        data_url = b'<url><loc>https://cedar-records.example.invalid/data.html</loc></url>'
        legacy = {}
        for name, data in files.items():
            if name in ("data.html", "data/datapackage.json"):
                continue
            if name.endswith(".html"):
                assert data.count(data_link) == 1, name
                data = data.replace(data_link, b"", 1)
            if name == "sitemap.xml":
                assert data.count(data_url) == 1
                data = data.replace(data_url, b"", 1)
            legacy[name] = data
        return legacy

    def refused(self, value, pattern):
        """build refuses with ``pattern``; content-check lists build's own line and names elsewhere.json in the JSON record."""
        self.put(value)
        with self.assertRaisesRegex(ContentError, pattern) as caught:
            load_content(self.root, BUILD_DAY)
        problems = content_problems(self.root)
        self.assertIn(str(caught.exception), problems, "content-check does not name the build's own refusal")
        self.assertEqual(problem_record(str(caught.exception))["file"].split("[")[0].split(":")[0], "elsewhere.json")

    def run_check(self, as_json=False):
        out, err, code = io.StringIO(), io.StringIO(), 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                run_cli(self.root, as_json=as_json)
            except SystemExit as exc:
                code = exc.code
        return out.getvalue(), err.getvalue(), code

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args, "--directory", str(self.root)],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=300)


class RenderTests(Case):
    def test_groups_order_sorting_escaping_and_sourcing(self):
        rows = [
            entry(1, kind="other", decided_on="2026-01-01"),
            entry(2, kind="audit_ordered", decided_on="2026-02-01"),
            entry(3, kind="restricted_sharing", decided_on="2026-02-02"),
            entry(4, kind="adopted_ordinance", decided_on="2026-02-03"),
            entry(5, kind="declined", decided_on="2026-02-04"),
            entry(6, kind="ended_contract", decided_on="2025-12-31", news_url="https://news.example.test/story?id=6"),
            entry(7, kind="paused", decided_on="2026-02-01", place="Zed Falls", as_stated="Paused \"all\" use & renewal; see A&B <not markup>".replace("<not markup>", "(no markup)")),
            entry(8, kind="paused", decided_on="2026-04-01", place="Mid Town"),
            entry(9, kind="paused", decided_on="2026-02-01", place="Alpha Town"),  # same day as Zed Falls: place breaks the tie
        ]
        self.put(rows)
        before = json.loads(self.path.read_text(encoding="utf-8"))
        model = load_content(self.root, BUILD_DAY)
        self.assertEqual(model["elsewhere"], rows)  # stored order and values untouched
        files = self.build()
        self.assertEqual(self.build(), files)  # deterministic
        page = files["elsewhere.html"].decode()
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), before)
        headings = re.findall(r"<h3>([^<]+)</h3>", page)
        self.assertEqual(headings, [DEFAULTS[f"elsewhere.kind.{kind}"] for kind in KINDS])
        paused = page.split("<h3>Paused</h3>")[1].split("<h3>")[0]
        self.assertEqual(re.findall(r"<strong>([^<]+), OR</strong>", paused), ["Mid Town", "Alpha Town", "Zed Falls"])
        self.assertIn("Paused &quot;all&quot; use &amp; renewal; see A&amp;B (no markup)", paused)
        self.assertIn(NOTE, page)
        self.assertIn("<title>Decisions elsewhere | ", page)
        self.assertEqual(page.count('rel="noopener noreferrer">Official source</a>'), len(rows))
        self.assertEqual(page.count('>News coverage</a>'), 1)
        self.assertIn('href="https://news.example.test/story?id=6"', page)
        self.assertIn('href="https://records.example.test/minutes/6"', page)
        self.assertEqual(page.count("Checked <time datetime=\"2026-09-15\">2026-09-15</time>"), len(rows))
        self.assertIn('Decided <time datetime="2026-04-01">2026-04-01</time>', page)
        main = page.split('id="content"')[1].split("</section>")[0]
        self.assertNotRegex(main, r"(?i)\b\d+\s+(cities|places|decisions|jurisdictions)\b|\brank|\bscore|\btotal")
        self.assertEqual(re.findall(r"<h1", main), [])
        self.assertNotIn("<script", main)
        self.assertEqual(check_links(self.public), [])
        self.assertEqual(check_accessibility(self.public), [])

    def test_empty_groups_omitted_and_one_and_hundred_entries_render(self):
        self.put([entry(1, kind="declined")])
        page = self.build()["elsewhere.html"].decode()
        self.assertEqual(re.findall(r"<h3>([^<]+)</h3>", page), ["Declined"])
        self.put([entry(n) for n in range(100)])
        self.assertEqual(self.build()["elsewhere.html"].decode().count('class="elsewhere-item"'), 100)

    def test_absent_is_byte_identical_and_toggle_adds_only_conditional_artifacts(self):
        absent = self.build()
        self.assertEqual(self.digests(self.without_catalogue(absent)), BASELINE)  # independent pins: no file, no change
        self.assertNotIn("elsewhere", load_content(self.root, BUILD_DAY))
        self.assertTrue(all(b"elsewhere" not in data for data in absent.values()))
        self.put([entry(1)])
        enabled = self.build()
        self.assertEqual(set(enabled) - set(absent), {"elsewhere.html"})
        for name in ("data.html", "data/datapackage.json"):  # the catalogue lists no elsewhere data: its bytes stay as without the toggle
            if name == "data/datapackage.json":
                self.assertEqual(enabled[name], absent[name], name)
            else:
                self.assertEqual(enabled[name].decode().replace(FOOTER, "", 1), absent[name].decode(), name)
        for name, data in self.without_catalogue(enabled).items():
            if name in ("elsewhere.html", "sitemap.xml"):
                continue
            if name.endswith(".html"):
                text = data.decode()
                self.assertEqual(text.count(FOOTER), 1, name)
                self.assertEqual(hashlib.sha256(text.replace(FOOTER, "", 1).encode()).hexdigest(), BASELINE[name], name)
            else:
                self.assertEqual(hashlib.sha256(data).hexdigest(), BASELINE[name], name)
        new = "https://cedar-records.example.invalid/elsewhere.html"
        locs = LOC.findall(enabled["sitemap.xml"].decode())
        self.assertEqual(locs.count(new), 1)
        self.assertEqual([loc for loc in locs if loc != new], LOC.findall(absent["sitemap.xml"].decode()))
        self.path.unlink()  # removing the file removes the page, the footer link and the sitemap entry
        self.assertEqual(self.build(), absent)

    def test_no_base_url_keeps_page_and_footer_but_no_sitemap(self):
        self.put([entry(1)])
        self.site_json(base_url=None)
        files = self.build()
        self.assertNotIn("sitemap.xml", files)
        self.assertIn("elsewhere.html", files)
        self.assertEqual(files["index.html"].decode().count(FOOTER), 1)

    def test_invalid_file_leaves_the_last_good_tree(self):
        self.put([entry(1)])
        self.build()
        before = self.tree()
        self.put([entry(1, source_url="http://records.example.test/x")])
        with self.assertRaisesRegex(ContentError, r"elsewhere\.json\[0\]\.source_url: must be an https:// URL"):
            build_site(self.root, today=BUILD_DAY)
        self.assertEqual(self.tree(), before)
        self.assertEqual(list(self.root.glob(".public.*")), [])


class ValidationTests(Case):
    def test_envelope_and_entry_shape(self):
        pattern = r"elsewhere\.json: must be a list of 1 to 100 entries"
        for name, value in (("null", None), ("object", {}), ("string", "x"), ("empty list", []),
                            ("101 entries", [entry(n) for n in range(101)])):
            with self.subTest(name):
                self.refused(value, pattern)
        self.assertEqual(len(load_elsewhere([entry(n) for n in range(100)], today=BUILD_DAY)), 100)
        self.refused([5], r"elsewhere\.json\[0\]: expected an object")
        self.refused([entry(1, score=3)], r"elsewhere\.json\[0\]: unknown field\(s\) \['score'\]")
        self.refused([entry(1, source_url=...)], r"elsewhere\.json\[0\]: missing field\(s\) \['source_url'\]")
        self.refused([entry(1, news_url=...), entry(2, source_url=..., news_url="https://news.example.test/a")],
                     r"elsewhere\.json\[1\]: missing field\(s\) \['source_url'\]")  # a news link never replaces the official source

    def test_text_fields(self):
        for field in ("place", "body", "as_stated"):
            self.assertEqual(load_elsewhere([entry(1, **{field: "x" * 200})], today=BUILD_DAY)[0][field], "x" * 200)
            self.assertEqual(load_elsewhere([entry(1, **{field: "  Padded  "})], today=BUILD_DAY)[0][field], "Padded")
            for name, value in (("empty", ""), ("blank", "   "), ("201 characters", "x" * 201), ("angle open", "a<b"),
                                ("angle close", "a>b"), ("newline", "a\nb"), ("tab", "a\tb"), ("null byte", "a\x00b"),
                                ("line separator", "a\u2028b"), ("bidi control", "a\u202eb"), ("number", 7), ("null", None)):
                with self.subTest(field=field, case=name):
                    self.refused([entry(1, **{field: value})], rf"elsewhere\.json\[0\]\.{field}: must be trimmed plain single-line text")
        kept = 'Ünïcode — “Quoted” & Co. *not bold* [x](y)'  # nothing rewritten, translated or Markdown-handled
        self.assertEqual(load_elsewhere([entry(1, as_stated=kept)], today=BUILD_DAY)[0]["as_stated"], kept)

    def test_state_kind_and_dates(self):
        for name, value in (("lowercase", "or"), ("one letter", "O"), ("three", "ORE"), ("digit", "O1"), ("non-ascii", "ÖR"),
                            ("trailing newline", "OR\n"), ("null", None), ("number", 12)):
            with self.subTest(state=name):
                self.refused([entry(1, state=value)], r"elsewhere\.json\[0\]\.state: must be a two-letter uppercase")
        for name, value in (("capital", "Paused"), ("unknown", "banned"), ("empty", ""), ("null", None), ("list", ["paused"])):
            with self.subTest(kind=name):
                self.refused([entry(1, kind=value)], r"elsewhere\.json\[0\]\.kind: must be one of ended_contract, paused")
        for field in ("decided_on", "checked_on"):
            for name, value in (("short month", "2026-9-01"), ("not a day", "2026-02-30"), ("not leap", "2025-02-29"),
                                ("month 13", "2026-13-01"), ("full-width", "２０２６-09-01"), ("datetime", "2026-09-01T00:00:00"),
                                ("number", 20260901), ("null", None)):
                with self.subTest(field=field, case=name):
                    self.refused([entry(1, **{"decided_on": "2026-01-01", "checked_on": "2026-09-15", field: value})],
                                 rf"elsewhere\.json\[0\]\.{field}: must be a real YYYY-MM-DD date")
        self.assertEqual(load_elsewhere([entry(1, decided_on="2024-02-29", checked_on="2024-02-29")], today=BUILD_DAY)[0]["decided_on"],
                         "2024-02-29")
        self.refused([entry(1, checked_on="2999-01-01")], r"elsewhere\.json\[0\]\.checked_on: 2999-01-01 is in the future")
        self.refused([entry(1, decided_on="2026-09-16", checked_on="2026-09-15")],
                     r"elsewhere\.json\[0\]\.decided_on: must not be after checked_on")
        # the build day is the supplied one: checked_on equal to it passes, one day later is refused
        self.assertEqual(len(load_elsewhere([entry(1, checked_on="2026-10-01")], today=date(2026, 10, 1))), 1)
        with self.assertRaisesRegex(ContentError, r"checked_on: 2026-10-02 is in the future"):
            load_elsewhere([entry(1, checked_on="2026-10-02")], today=date(2026, 10, 1))
        self.put([entry(1, checked_on="2026-10-02")])
        with self.assertRaisesRegex(ContentError, r"checked_on: 2026-10-02 is in the future"):
            read_elsewhere(self.root, date(2026, 10, 1))  # the reader takes the build day, as load_content passes it

    def test_urls(self):
        for field, extra in (("source_url", {}), ("news_url", {})):
            for name, value in (("http", "http://records.example.test/x"), ("ftp", "ftp://records.example.test/x"),
                                ("userinfo", "https://user@records.example.test/x"), ("no host", "https:///x"),
                                ("space", "https://records.example.test/a b"), ("empty", ""), ("null", None), ("number", 4)):
                with self.subTest(field=field, case=name):
                    self.refused([entry(1, **{field: value})], rf"elsewhere\.json\[0\]\.{field}: must be an https:// URL|invalid URL host")
        self.assertNotIn("news_url", load_elsewhere([entry(1)], today=BUILD_DAY)[0])  # omission is allowed
        self.assertEqual(load_elsewhere([entry(1, news_url="https://news.example.test/a")], today=BUILD_DAY)[0]["news_url"],
                         "https://news.example.test/a")

    def test_duplicates_name_both_indices_and_near_misses_are_allowed(self):
        self.refused([entry(1), entry(2), entry(1, source_url="https://other.example.test/x", as_stated="Different text.")],
                     r"elsewhere\.json\[2\]: duplicate of elsewhere\.json\[0\] \(same place, body, decided_on and kind\)")
        self.refused([entry(1), entry(1, place="  Springfield 1  ")], r"elsewhere\.json\[1\]: duplicate of elsewhere\.json\[0\]")
        accepted = load_elsewhere([entry(1), entry(1, kind="declined"), entry(1, decided_on="2026-03-11"),
                                   entry(1, place="springfield 1"), entry(1, body="Springfield City council")], today=BUILD_DAY)
        self.assertEqual(len(accepted), 5)  # literal comparison only: no case folding, no fuzzy match, no agency lookup
        self.assertEqual([row["place"] for row in accepted], ["Springfield 1"] * 3 + ["springfield 1", "Springfield 1"])

    def test_checker_collects_every_independent_problem(self):
        self.put([entry(1, state="x"), entry(2, kind="nope"), entry(3), entry(3)])
        lines = [line for line in content_problems(self.root) if line.startswith("elsewhere.json")]
        self.assertEqual(len(lines), 3)
        self.assertEqual(sorted(re.match(r"elsewhere\.json\[(\d)\]", line).group(1) for line in lines), ["0", "1", "3"])
        out, _, code = self.run_check(as_json=True)
        record = json.loads(out)
        self.assertEqual(code, 3)
        self.assertEqual(record["count"], 3)
        self.assertEqual({row["file"].split("[")[0] for row in record["problems"]}, {"elsewhere.json"})
        collected = []
        self.assertIsNone(read_elsewhere(self.root, BUILD_DAY, collected))
        self.assertEqual(sorted(collected), sorted(lines))

    def test_unreadable_file_kinds_are_refused_before_any_output(self):
        files = {"invalid UTF-8": (b"\xff\xfe[]", r"elsewhere\.json: not valid UTF-8 JSON"),
                 "invalid JSON": (b"[", r"elsewhere\.json: not valid UTF-8 JSON"),
                 "too large": (b'["' + b"x" * (2 * 1024 * 1024) + b'"]', r"elsewhere\.json: file too large"),
                 "nested too deeply": (b"[" * 100000, r"elsewhere\.json: not valid UTF-8 JSON")}
        for name, (data, pattern) in files.items():
            with self.subTest(name):
                self.refused(data, pattern)
        (self.tmp / "real.json").write_text(json.dumps([entry(1)]), encoding="utf-8")
        for name in ("symlink", "dangling symlink", "folder", "fifo"):
            with self.subTest(name):
                self.path.unlink(missing_ok=True)
                if name == "symlink":
                    os.symlink(self.tmp / "real.json", self.path)
                elif name == "dangling symlink":
                    os.symlink(self.tmp / "missing.json", self.path)
                elif name == "folder":
                    self.path.mkdir()
                else:
                    os.mkfifo(self.path)
                with self.assertRaisesRegex(ContentError, r"elsewhere\.json: (symlinks are not read|must be a regular file)"):
                    build_site(self.root, today=BUILD_DAY)
                self.assertFalse(self.public.exists(), "a refused build must write nothing")
                self.assertTrue(any(line.startswith("elsewhere.json: ") for line in content_problems(self.root)))
                if name == "folder":
                    self.path.rmdir()
        self.path.unlink(missing_ok=True)
        self.assertIsNone(read_elsewhere(self.root))  # absent is None, not an empty list


class GuardTests(Case):
    def test_as_stated_is_scanned_alert_only_and_never_echoed(self):
        private = "ZQ-PRIVATE-SENTINEL"
        self.put([entry(1, as_stated=f"{ACCUSE} {private}"),
                  entry(2, as_stated='The article said "the county violated SB 34" in 2024.'),  # quoted: the guard's own boundary
                  entry(3, as_stated="The council voted to end the contract."),
                  entry(4, as_stated="Cedar Police is out of compliance with SB 34.")])
        expected = ["review-required: elsewhere.json[0].as_stated: legal-claim wording (violation-verb)",
                    "review-required: elsewhere.json[3].as_stated: legal-claim wording (non-compliant)"]
        self.assertEqual(elsewhere_notes(load_content(self.root)["elsewhere"]), expected)
        self.assertEqual(site_notes(self.root), expected)
        for as_json in (False, True):
            out, err, code = self.run_check(as_json)
            self.assertEqual((code, err.splitlines()), (0, expected))
            self.assertNotIn(private, out + err)
        record = json.loads(self.run_check(True)[0])
        self.assertEqual((record["count"], record["problems"]), (0, []))  # not an error
        self.assertEqual(sorted(record), ["count", "law_package", "problems", "schema", "truncated"])
        built = self.cli("build")
        self.assertEqual(built.returncode, 0, built.stderr)
        self.assertEqual([line for line in built.stdout.splitlines() if line.startswith("review-required: elsewhere")], expected)
        self.assertNotIn(private, built.stdout + built.stderr)
        checked = self.cli("content-check")
        self.assertEqual((checked.returncode, checked.stderr.splitlines()), (0, expected))
        self.path.unlink()
        self.assertEqual(site_notes(self.root), [])
        self.assertEqual(self.run_check()[1], "")

    def test_invalid_file_prints_no_notes(self):
        self.put([entry(1, as_stated=ACCUSE), entry(1, as_stated=ACCUSE)])  # duplicate: the whole file is refused
        out, err, code = self.run_check(True)
        self.assertEqual((err, code), ("", 3))
        self.assertEqual([n for n in site_notes(self.root) if "elsewhere" in n], [])

    def test_privacy_gate_refuses_contact_values_without_echoing_them(self):
        self.put([entry(1)])
        self.build()
        before = self.tree()
        for field, value in (("as_stated", "Contact jane.roe@example.org about it."), ("place", "Springfield 415-555-0132"),
                             ("body", "Council jane.roe@example.org")):
            with self.subTest(field=field):
                self.put([entry(1, **{field: value})])
                with self.assertRaises(ContentError) as caught:
                    build_site(self.root, today=BUILD_DAY)
                self.assertIn(f"elsewhere[0].{field}", str(caught.exception))
                self.assertNotIn(value.split()[1], str(caught.exception))
                self.assertEqual(self.tree(), before)


class SlugAndLabelTests(Case):
    def test_reserved_with_and_without_the_file(self):
        self.assertIn("elsewhere", STARTER_SLUGS)
        self.assertIn("elsewhere", RESERVED_PAGE_SLUGS)
        self.assertIn("elsewhere.html", GENERATED_TOP_LEVEL)
        for with_file in (False, True):
            with self.subTest(with_file=with_file):
                self.path.unlink(missing_ok=True)
                if with_file:
                    self.put([entry(1)])
                self.put({"title": "Elsewhere", "description": "d", "body_md": "text"}, "pages/elsewhere.json")
                with self.assertRaisesRegex(ContentError, r"elsewhere"):
                    load_content(self.root, BUILD_DAY)
                self.assertTrue(any("elsewhere" in line for line in content_problems(self.root)))
                (self.content / "pages" / "elsewhere.json").unlink()
                self.put({"schema_version": 1, "redirects": [{"from": "/elsewhere.html", "to": "/about.html"}]}, "redirects.json")
                if with_file:
                    with self.assertRaisesRegex(ContentError, r"from /elsewhere\.html is a page this build writes"):
                        self.build()
                (self.content / "redirects.json").unlink()
        shutil.rmtree(self.content / "pages", ignore_errors=True)

    def test_a_finding_cannot_take_the_slug(self):
        finding = json.loads(next((self.content / "findings").glob("*.json")).read_text(encoding="utf-8"))
        self.put(dict(finding, id="fnd_00000000000000ee", slug="elsewhere"), "findings/elsewhere.json")
        with self.assertRaisesRegex(ContentError, r"elsewhere"):
            load_content(self.root, BUILD_DAY)

    def test_every_label_is_replaceable_and_spanish_draft_is_complete(self):
        markers = {key: f"X{key}X" for key in LABEL_KEYS}
        markers["elsewhere.decided"] = "Xdecided {date}X"
        markers["elsewhere.checked"] = "Xchecked {date}X"
        self.put({"schema_version": 1, "labels": markers}, "labels.json")
        self.put([entry(n, kind=kind) for n, kind in enumerate(KINDS, 1)])
        files = self.build()
        page = files["elsewhere.html"].decode()
        for key in LABEL_KEYS:
            if key not in ("elsewhere.news", "elsewhere.decided", "elsewhere.checked", "footer.elsewhere"):
                self.assertIn(f"X{key}X", page, key)
        self.assertIn('Xdecided <time datetime="2026-03-10">2026-03-10</time>X', page)
        self.assertIn('Xchecked <time datetime="2026-09-15">2026-09-15</time>X', page)
        self.assertIn("Xfooter.elsewhereX", files["index.html"].decode())
        for fixed in ("Decisions elsewhere", "Official source", "Paused", "Ended a contract", "Checked "):
            self.assertNotIn(fixed, page, fixed)
        self.put([entry(1, news_url="https://news.example.test/a")])
        self.assertIn("Xelsewhere.newsX", self.build()["elsewhere.html"].decode())
        draft = json.loads((REPO_ROOT / "examples" / "labels" / "es.json").read_text(encoding="utf-8"))["labels"]
        for key in LABEL_KEYS:
            with self.subTest(key):
                self.assertIn(key, DEFAULTS)
                self.assertNotEqual(draft[key], DEFAULTS[key])
                self.assertEqual(sorted(re.findall(r"\{[a-z_]+\}", draft[key])), sorted(re.findall(r"\{[a-z_]+\}", DEFAULTS[key])))


if __name__ == "__main__":
    unittest.main()
