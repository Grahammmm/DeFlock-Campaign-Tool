"""DF0022-03: organizer-recorded press coverage, content/coverage.json -> /news.html, "Reported in" lines, the CLI.

Builds temporary copies of the fictional example with synthetic outlets on reserved .test hosts; sockets are blocked, nothing is
fetched. The no-coverage render is compared byte for byte with the digest table of tests/test_site_faq.py (the example has no
faq.json, press or coverage), so an unrelated page change cannot hide here.
"""
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from campaign_tool import coverage as coverage_module
from campaign_tool.content import COVERAGE_KINDS, RESERVED_PAGE_SLUGS, ContentError, coverage_entries, load_content, load_coverage
from campaign_tool.content_check import content_problems, problem_record
from campaign_tool.coverage import CoverageError, add_coverage, remove_coverage
from campaign_tool.labels import DEFAULTS, PLACEHOLDER
from campaign_tool.site import GENERATED_TOP_LEVEL, build_site
from campaign_tool.site_check import check_accessibility, check_links
from tests.test_site_faq import BASELINE
from tests.test_site_packet import strip_packet_counterevidence

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
LD_JSON = re.compile(rb'  <script type="application/ld\+json">[^<]*</script>\n')  # DF1407-08: inert data, absent from the pre-feature pins
BUILD_DAY = date(2026, 10, 1)  # the day BASELINE was rendered on
NO_SOCKETS = mock.patch("socket.socket", side_effect=AssertionError("network access attempted"))
FINDING_ID, FINDING_SLUG = "fnd_0000000000000001", "cedar-policy-posted"
NEWS_KEYS = ["news.title", "news.description", "news.empty", "news.findings", "news.kind.news", "news.kind.opinion",
             "news.kind.broadcast", "news.kind.other", "finding.reported_in", "footer.news", "press.news"]
FOOTER = '<p><a href="/news.html">In the news</a></p>'
PRESS = {"contact_role": "Media contact", "contact_email": "media@example.invalid"}
LOC = re.compile(r"<loc>([^<]+)</loc>")


def row(number=1, **extra):
    return {"outlet": "Example News", "headline": f"Published headline {number}",
            "url": f"https://news.example.test/story-{number}", "published_on": "2026-09-01", **extra}


def document(*rows, **extra):
    return {"schema_version": 1, "entries": list(rows), **extra}


def snapshot(root):
    """Every file under ``root`` except the build output: bytes, mode and mtime (folders: mode only; symlinks not followed)."""
    found = {}
    for path in sorted(Path(root).rglob("*")):
        relative = path.relative_to(root).as_posix()
        if relative == "public" or relative.startswith("public/"):
            continue
        info = os.lstat(path)
        found[relative] = ((None, info.st_mode, None) if path.is_dir() or path.is_symlink()
                           else (path.read_bytes(), info.st_mode, info.st_mtime_ns))
    return found


class NewsCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-news-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content, self.public = self.root / "content", self.root / "public"
        self.path = self.content / "coverage.json"
        NO_SOCKETS.start()
        self.addCleanup(NO_SOCKETS.stop)

    def put(self, value, name="coverage.json"):
        path = self.content / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode("utf-8"))
        return path

    def add_finding(self, number, **changes):
        base = json.loads((self.content / "findings" / f"{FINDING_SLUG}.json").read_text(encoding="utf-8"))
        finding = dict(base, id=changes.pop("id", f"fnd_000000000000000{number}"), slug=f"zq-{number}",
                       title=f"Zq title {number}", summary=f"Zq summary {number}", body_md="", **changes)
        self.put(finding, f"findings/zq-{number}.json")
        return finding

    def site_json(self, **changes):
        """Set site.json keys; a value of None removes the key."""
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

    def cli(self, *args, directory=None):
        flags = [] if directory is False else ["--directory", str(directory or self.root)]
        return subprocess.run([sys.executable, "-m", "campaign_tool", "content", "coverage", *args, *flags],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)

    def refused(self, value, pattern):
        """load_content refuses with ``pattern``; content-check lists build's own line; the file is named in the JSON record."""
        self.put(value)
        with self.assertRaisesRegex(ContentError, pattern) as caught:
            load_content(self.root, BUILD_DAY)
        problems = content_problems(self.root)
        self.assertIn(str(caught.exception), problems, "content-check does not name the build's own refusal")
        record = problem_record(str(caught.exception))
        self.assertTrue(record["file"].startswith("coverage.json"), record)
        return str(caught.exception)


class ShapeTests(NewsCase):
    def test_coverage_shape_boundaries_and_check_parity(self):
        envelope = {
            "null": (None, r"coverage\.json: expected an object"),
            "list": ([], r"coverage\.json: expected an object"),
            "string": ("x", r"coverage\.json: expected an object"),
            "missing entries": ({"schema_version": 1}, r"coverage\.json: must have exactly the fields"),
            "unknown field": ({"schema_version": 1, "entries": [], "extra": 1}, r"coverage\.json: must have exactly the fields"),
            "bool version": (document(schema_version=True), r"coverage\.json\.schema_version: must be 1"),
            "float version": ({"schema_version": 1.0, "entries": []}, r"coverage\.json\.schema_version: must be 1"),
            "version 2": ({"schema_version": 2, "entries": []}, r"coverage\.json\.schema_version: must be 1"),
            "null entries": ({"schema_version": 1, "entries": None}, r"coverage\.json\.entries: must be a list of at most 1000"),
            "object entries": ({"schema_version": 1, "entries": {}}, r"coverage\.json\.entries: must be a list of at most 1000"),
            "1001 entries": (document(*(row(n) for n in range(1001))), r"coverage\.json\.entries: must be a list of at most 1000"),
            "row not object": (document([]), r"entries\[0\]: expected an object"),
            "unknown row field": (document(row(1, quote="text")), r"entries\[0\]: unknown field\(s\) \['quote'\]"),
            "missing row field": (document({k: v for k, v in row(1).items() if k != "url"}), r"entries\[0\]: missing field\(s\) \['url'\]"),
        }
        for name, (value, pattern) in envelope.items():
            with self.subTest(name):
                self.refused(value, pattern)
        self.assertEqual(len(coverage_entries(document(*(row(n) for n in range(1000))))), 1000)  # the 1000 endpoint is accepted
        self.assertEqual(coverage_entries(document()), [])

        for field, limit in {"outlet": 80, "headline": 200}.items():
            self.assertEqual(coverage_entries(document(row(1, **{field: "x" * limit})))[0][field], "x" * limit)
            for name, value in (("empty", ""), ("blank", "   "), ("tab only", "\t"), ("too long", "x" * (limit + 1)),
                                ("angle open", "a<b"), ("angle close", "a>b"), ("newline", "a\nb"), ("carriage return", "a\rb"),
                                ("null byte", "a\x00b"), ("c0 control", "a\x1fb"), ("delete", "a\x7fb"), ("line separator", "a\u2028b"),
                                ("paragraph separator", "a\u2029b"), ("surrogate", "a\ud800b"), ("number", 7), ("null", None),
                                ("list", ["x"])):
                with self.subTest(field=field, case=name):
                    self.refused(document(row(1, **{field: value})),
                                 rf"entries\[0\]\.{field}: must be non-blank plain single-line text")
        exact = '  Ünïcode — “Quoted” & Co.  '  # kept exactly: no trimming, rewriting, Markdown or HTML handling
        kept = coverage_entries(document(row(1, outlet=exact, headline="*Bold* [link](x) &amp; **raw**")))[0]
        self.assertEqual((kept["outlet"], kept["headline"]), (exact, "*Bold* [link](x) &amp; **raw**"))

        future = (date.today() + timedelta(days=1)).isoformat()
        for name, value in (("short month", "2026-9-01"), ("slash", "2026/09/01"), ("compact", "20260901"), ("not a day", "2026-02-30"),
                            ("not leap", "2025-02-29"), ("month 13", "2026-13-01"), ("full-width digits", "２０２６-09-01"),
                            ("datetime", "2026-09-01T00:00:00"), ("number", 20260901), ("null", None)):
            with self.subTest(published_on=name):
                self.refused(document(row(1, published_on=value)),
                             r"entries\[0\]\.published_on: must be a real date written YYYY-MM-DD")
        for day in ("2024-02-29", date.today().isoformat(), "2000-01-01"):  # leap day, today and an old date are accepted
            self.assertEqual(coverage_entries(document(row(1, published_on=day)))[0]["published_on"], day)
        self.refused(document(row(1), row(2, published_on=future)),
                     r"coverage\.json\.entries\[1\]\.published_on: must not be after today")
        with self.assertRaisesRegex(ContentError, r"entries\[0\]\.published_on: must not be after today"):
            coverage_entries(document(row(1, published_on="2026-10-02")), today=date(2026, 10, 1))
        self.assertEqual(coverage_entries(document(row(1, published_on="2026-10-01")), today=date(2026, 10, 1))[0]["kind"], "news")

        self.assertEqual(coverage_entries(document(row(1)))[0]["findings"], [])  # defaults
        for kind in COVERAGE_KINDS:
            self.assertEqual(coverage_entries(document(row(1, kind=kind)))[0]["kind"], kind)
        for name, value in (("capital", "News"), ("unknown", "blog"), ("empty", ""), ("null", None), ("list", ["news"])):
            with self.subTest(kind=name):
                self.refused(document(row(1, kind=value)), r"entries\[0\]\.kind: must be one of news, opinion, broadcast, other")

        files = {
            "invalid UTF-8": (b"\xff\xfe{}", r"coverage\.json: not valid UTF-8 JSON"),
            "invalid JSON": (b"{", r"coverage\.json: not valid UTF-8 JSON"),
            "empty file": (b"", r"coverage\.json: not valid UTF-8 JSON"),
            "too large": (b'{"schema_version":1,"entries":[],"pad":"' + b"x" * (2 * 1024 * 1024) + b'"}', r"coverage\.json: file too large"),
            "nested too deeply": (b"[" * 100000, r"coverage\.json: not valid UTF-8 JSON"),
        }
        for name, (data, pattern) in files.items():
            with self.subTest(file=name):
                self.refused(data, pattern)
        (self.tmp / "real.json").write_text(json.dumps(document()), encoding="utf-8")
        for name in ("symlink", "dangling symlink", "folder"):
            with self.subTest(file=name):
                self.path.unlink()
                if name == "symlink":
                    os.symlink(self.tmp / "real.json", self.path)
                elif name == "dangling symlink":
                    os.symlink(self.tmp / "missing.json", self.path)
                else:
                    self.path.mkdir()
                with self.assertRaisesRegex(ContentError, r"coverage\.json: (symlinks are not read|must be a regular file)") as caught:
                    load_content(self.root, BUILD_DAY)
                self.assertIn(str(caught.exception), content_problems(self.root))
                if name == "folder":
                    self.path.rmdir()
                else:
                    self.path.unlink()
                self.put(b"{}")  # keep a file for the next round's unlink
        self.path.unlink()
        self.assertEqual(content_problems(self.root), [])  # absent is not a problem

    def test_present_but_empty_is_kept_and_absent_has_no_key(self):
        self.assertNotIn("coverage", load_content(self.root, BUILD_DAY))
        self.put(document())
        self.assertEqual(load_content(self.root, BUILD_DAY)["coverage"], [])
        self.assertEqual(content_problems(self.root), [])

    def test_exact_urls_and_published_ids(self):
        base = "https://news.example.test/story"
        for first, second in ((base, base), (base + "?utm=1", base + "?utm=1"), (base + "#a", base + "#a")):
            with self.subTest(url=first):
                self.refused(document(row(1, url=first), row(2, url=second)), r"coverage\.json\.entries\[1\]\.url: duplicate URL")
        accepted = [base, base + "?utm=1", base + "#a", base + "/", "https://NEWS.EXAMPLE.TEST/STORY", base + "?utm=2"]
        self.assertEqual(len(coverage_entries(document(*(row(n, url=url) for n, url in enumerate(accepted))))), len(accepted))
        # Construct synthetic userinfo at runtime; keep the unsafe URL rejection covered.
        credentials_url = "https://{}:{}@news.example.test/a".format("user", "pw")
        for name, url in (("http", "http://news.example.test/a"), ("credentials", credentials_url),
                          ("bad host", "https://localhost/a"), ("no host", "https:///a"), ("space", "https://news.example.test/a b"),
                          ("javascript", "javascript:alert(1)"), ("scheme-less", "news.example.test/a"), ("null", None), ("number", 5),
                          ("angle", "https://news.example.test/<a>"), ("quote", 'https://news.example.test/"a"')):
            with self.subTest(url=name):
                self.refused(document(row(1, url=url)), r"entries\[0\]\.url: (must be an https:// URL|invalid URL host)")

        self.add_finding(2)
        self.add_finding(3)
        self.add_finding(4)
        self.add_finding(5, id="Fnd_Mixed-Case_5")  # existing ids allow letters, digits, underscore and hyphen
        ids = [FINDING_ID, "fnd_0000000000000002", "fnd_0000000000000003", "fnd_0000000000000004", "Fnd_Mixed-Case_5"]
        message = "must name at most 3 unique published finding IDs"
        for refs in ([], [ids[0]], ids[:3], [ids[4]], [ids[4], ids[1]]):
            self.put(document(row(1, findings=refs)))
            self.assertEqual(load_content(self.root, BUILD_DAY)["coverage"][0]["findings"], refs)
            self.assertEqual(content_problems(self.root), [])
        for name, refs in (("four", ids[:4]), ("repeat", [ids[0], ids[0]]), ("unknown", ["fnd_9999999999999999"]),
                           ("slug", [FINDING_SLUG]), ("case differs", [ids[4].lower()]), ("prefix", [ids[0][:10]]), ("empty id", [""]),
                           ("number", [1]), ("null", [None]), ("not a list", ids[0]), ("object", {}), ("space", [ids[0] + " "]),
                           ("slash", ["a/b"]), ("too long", ["x" * 65])):
            with self.subTest(findings=name):
                self.refused(document(row(1, findings=refs)), r"entries\[0\]\.findings: " + message)
        # drafts are never read: an id found only in a private draft is just unknown, and the refusal repeats no id
        (self.root / "private" / "drafts").mkdir(parents=True)
        draft = dict(json.loads((self.content / "findings" / f"{FINDING_SLUG}.json").read_text()), id="fnd_draft_00000001", state="draft")
        (self.root / "private" / "drafts" / "d.json").write_text(json.dumps(draft), encoding="utf-8")
        shown = self.refused(document(row(1, findings=["fnd_draft_00000001"])), r"entries\[0\]\.findings: " + message)
        self.assertNotIn("fnd_draft", shown)
        # withdrawn: the finding file leaves content/findings, so a stored reference is refused until the entry is repaired
        self.put(document(row(1, findings=["fnd_0000000000000004"])))
        self.assertEqual(content_problems(self.root), [])
        (self.content / "findings" / "zq-4.json").unlink()
        self.refused(document(row(1, findings=["fnd_0000000000000004"])), r"entries\[0\]\.findings: " + message)
        # no chronology rule: same-day and earlier-than-the-finding articles are accepted
        self.put(document(row(1, published_on="2026-02-01", findings=[FINDING_ID]),
                          row(2, published_on="2020-01-01", findings=[FINDING_ID])))
        self.assertEqual(content_problems(self.root), [])
        # a None list skips only the membership check
        self.assertEqual(load_coverage(document(row(1, findings=["fnd_unknown_0001"])), None)[0]["findings"], ["fnd_unknown_0001"])


class CliTests(NewsCase):
    def args(self, number=1, published_on="2026-09-01", more=()):
        return ["add", "--outlet", "Example News", "--headline", f"Published headline {number}", "--url",
                f"https://news.example.test/story-{number}", "--published-on", published_on, *more]

    def test_cli_add_remove_atomic_and_recovery(self):
        self.add_finding(2)
        before = snapshot(self.root)
        done = self.cli(*self.args(1))
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        first, second = done.stdout.splitlines()
        self.assertEqual(json.loads(first), {"action": "added", "url": "https://news.example.test/story-1"})
        self.assertEqual(second, "Nothing was built, published or sent.")
        self.assertEqual(self.path.read_bytes(), (json.dumps(document(row(1, findings=[], kind="news")), indent=2) + "\n").encode())
        after = snapshot(self.root)
        self.assertEqual({k for k in after if after[k] != before.get(k)}, {"content/coverage.json"})
        self.assertFalse(self.public.exists(), "coverage add must not build")
        self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), "0o644")

        added = self.cli(*self.args(2, more=["--finding", FINDING_ID, "--finding", "fnd_0000000000000002", "--kind", "opinion"]))
        self.assertEqual(added.returncode, 0, added.stderr)
        entries = json.loads(self.path.read_text())["entries"]
        self.assertEqual([e["url"] for e in entries], ["https://news.example.test/story-1", "https://news.example.test/story-2"])
        self.assertEqual((entries[1]["findings"], entries[1]["kind"]), ([FINDING_ID, "fnd_0000000000000002"], "opinion"))
        stored = self.path.read_bytes()

        def refused(args, pattern):
            result = self.cli(*args)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertRegex(result.stderr, pattern)
            self.assertEqual(self.path.read_bytes(), stored, "a refused request changed coverage.json")

        refused(self.args(1), r"Stopped: coverage\.json\.entries\[2\]\.url: duplicate URL")  # no upsert, even with other text
        refused(["add", "--outlet", "Other", "--headline", "H", "--url", "https://news.example.test/story-1", "--published-on", "2026-09-02"],
                r"duplicate URL")
        refused(self.args(3, more=["--finding", "fnd_0000000000000009"]), r"entries\[2\]\.findings: must name at most 3 unique published")
        refused(self.args(3, more=["--finding", FINDING_ID, "--finding", FINDING_ID]), r"entries\[2\]\.findings")
        refused(self.args(3, more=["--finding", FINDING_ID] * 4), r"entries\[2\]\.findings")
        refused(self.args(3, published_on=(date.today() + timedelta(days=1)).isoformat()), r"published_on: must not be after today")
        refused(["add", "--outlet", "x" * 81, "--headline", "H", "--url", "https://news.example.test/z", "--published-on", "2026-09-01"],
                r"entries\[2\]\.outlet")
        refused(["add", "--outlet", "O", "--headline", "a<b", "--url", "https://news.example.test/z", "--published-on", "2026-09-01"],
                r"entries\[2\]\.headline")
        refused(["add", "--outlet", "O", "--headline", "H", "--url", "http://news.example.test/z", "--published-on", "2026-09-01"],
                r"entries\[2\]\.url: must be an https")
        refused(["remove", "--url", "https://news.example.test/unknown"], r"Stopped: coverage\.json: URL not found")
        refused(["remove", "--url", "https://news.example.test/story-1/"], r"URL not found")  # exact text only

        for argv in (["remove", "--url", "https://news.example.test/story-1", "--outlet", "X"],  # add-only flags on remove
                     ["remove", "--url", "https://news.example.test/story-1", "--finding", "x"],
                     ["add", "--outlet", "O", "--headline", "H", "--url", "https://news.example.test/z"],  # missing flag
                     ["remove"], ["add", "--outlet", "O"], [], [*self.args(3), "--kind", "blog"]):
            with self.subTest(argv=argv):
                result = self.cli(*argv)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(self.path.read_bytes(), stored)
        self.assertEqual(self.cli(*self.args(3), directory=False).returncode, 2)  # --directory is required

        removed = self.cli("remove", "--url", "https://news.example.test/story-1")
        self.assertEqual((removed.returncode, removed.stderr), (0, ""))
        lines = removed.stdout.splitlines()
        self.assertEqual(json.loads(lines[0]), {"action": "removed", "url": "https://news.example.test/story-1"})
        self.assertEqual(lines[1], "Nothing was built, published or sent.")
        self.assertEqual([e["url"] for e in json.loads(self.path.read_text())["entries"]], ["https://news.example.test/story-2"])
        last = self.cli("remove", "--url", "https://news.example.test/story-2")
        self.assertEqual(last.returncode, 0, last.stderr)
        self.assertEqual(self.path.read_text(), '{\n  "schema_version": 1,\n  "entries": []\n}\n')  # last removal keeps a valid file
        self.assertEqual(content_problems(self.root), [])
        self.assertIn("news.html", self.build())  # the page stays enabled with its empty message
        self.assertEqual(self.cli("remove", "--url", "https://news.example.test/story-2").returncode, 2)

    def test_missing_content_malformed_store_symlinks_and_failed_replace(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        for directory in (empty, self.tmp / "absent"):
            result = self.cli(*self.args(1), directory=directory)
            self.assertEqual(result.returncode, 2)
            self.assertRegex(result.stderr, r"content/ does not exist")
            self.assertEqual(sorted(p.name for p in empty.iterdir()), [], "the command must not scaffold")
        self.assertEqual(self.cli("remove", "--url", "https://news.example.test/a", directory=empty).returncode, 2)
        (empty / "content").mkdir()
        self.assertRegex(self.cli(*self.args(1), directory=empty).stderr, r"content/site\.json is required")
        (empty / "content" / "site.json").write_text('{"schema_version": 1}', encoding="utf-8")
        self.assertEqual(self.cli(*self.args(1), directory=empty).returncode, 2)  # an invalid site.json is refused too
        self.assertFalse((empty / "content" / "coverage.json").exists())

        absent = self.cli("remove", "--url", "https://news.example.test/a")  # no store at all
        self.assertEqual((absent.returncode, absent.stdout), (2, ""))
        self.assertRegex(absent.stderr, r"Stopped: coverage\.json: URL not found")
        self.assertFalse(self.path.exists())

        for name, data in (("malformed JSON", b"{not json"), ("wrong shape", b"[]"), ("unknown field", json.dumps(document(row(1, x=1))).encode()),
                           ("future date", json.dumps(document(row(1, published_on="2999-01-01"))).encode())):
            with self.subTest(name):
                self.path.write_bytes(data)
                for argv in (self.args(7), ["remove", "--url", "https://news.example.test/story-1"]):
                    result = self.cli(*argv)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(self.path.read_bytes(), data, "a malformed store was overwritten")
                self.assertEqual([p for p in self.content.iterdir() if p.name.startswith(".coverage")], [])

        self.path.unlink()
        (self.tmp / "target.json").write_text(json.dumps(document()), encoding="utf-8")
        os.symlink(self.tmp / "target.json", self.path)
        result = self.cli(*self.args(1))
        self.assertEqual(result.returncode, 2)
        self.assertRegex(result.stderr, r"symlinks are not read")
        self.assertEqual((self.tmp / "target.json").read_text(), json.dumps(document()))
        self.assertTrue(self.path.is_symlink())
        self.path.unlink()

        moved = self.tmp / "moved-content"
        self.content.rename(moved)
        os.symlink(moved, self.content)  # a symlinked content/ is refused, nothing is written through it
        result = self.cli(*self.args(1))
        self.assertEqual(result.returncode, 2)
        self.assertFalse((moved / "coverage.json").exists())
        self.content.unlink()
        moved.rename(self.content)

        # a failed replace keeps the old bytes and leaves no temporary file behind
        add_coverage(self.root, outlet="O", headline="H1", url="https://news.example.test/one", published_on="2026-09-01")
        old, listing = self.path.read_bytes(), sorted(p.name for p in self.content.iterdir())
        with mock.patch.object(coverage_module.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(CoverageError, r"could not be written; the previous file was kept"):
                add_coverage(self.root, outlet="O", headline="H2", url="https://news.example.test/two", published_on="2026-09-01")
            with self.assertRaises(CoverageError):
                remove_coverage(self.root, url="https://news.example.test/one")
        self.assertEqual(self.path.read_bytes(), old)
        self.assertEqual(sorted(p.name for p in self.content.iterdir()), listing)
        with mock.patch.object(coverage_module.os, "fsync", side_effect=OSError("io")):
            with self.assertRaises(CoverageError):
                add_coverage(self.root, outlet="O", headline="H2", url="https://news.example.test/two", published_on="2026-09-01")
        self.assertEqual((self.path.read_bytes(), sorted(p.name for p in self.content.iterdir())), (old, listing))
        with mock.patch.object(coverage_module, "MAX_JSON_BYTES", 100):  # an oversize result is refused before any write
            with self.assertRaisesRegex(CoverageError, r"would exceed"):
                add_coverage(self.root, outlet="O", headline="H2", url="https://news.example.test/two", published_on="2026-09-01")
        self.assertEqual(self.path.read_bytes(), old)
        self.assertEqual(remove_coverage(self.root, url="https://news.example.test/one"),
                         {"action": "removed", "url": "https://news.example.test/one"})

    def test_removing_an_entry_whose_finding_was_withdrawn_and_stale_references_elsewhere(self):
        self.add_finding(2)
        self.add_finding(3)
        stale_a, stale_b = "https://news.example.test/a", "https://news.example.test/b"
        add_coverage(self.root, outlet="O", headline="A", url=stale_a, published_on="2026-09-01", finding_ids=["fnd_0000000000000002"])
        add_coverage(self.root, outlet="O", headline="B", url=stale_b, published_on="2026-09-02", finding_ids=["fnd_0000000000000003"])
        add_coverage(self.root, outlet="O", headline="C", url="https://news.example.test/c", published_on="2026-09-03")
        (self.content / "findings" / "zq-2.json").unlink()  # withdrawn
        stored = self.path.read_bytes()
        with self.assertRaisesRegex(ContentError, r"entries\[0\]\.findings"):  # the build is refused until it is repaired
            load_content(self.root, BUILD_DAY)
        # an unrelated add is refused while a stale reference exists, naming it; nothing changes
        with self.assertRaisesRegex(CoverageError, r"entries\[0\]\.findings: must name at most 3 unique published finding IDs"):
            add_coverage(self.root, outlet="O", headline="D", url="https://news.example.test/d", published_on="2026-09-04")
        self.assertEqual(self.path.read_bytes(), stored)
        (self.content / "findings" / "zq-3.json").unlink()
        with self.assertRaisesRegex(CoverageError, r"entries\[\d\]\.findings"):  # removing C leaves two stale references
            remove_coverage(self.root, url="https://news.example.test/c")
        self.assertEqual(self.path.read_bytes(), stored)
        # with B's finding back, A can be removed although its own finding is gone; the repair never drops B or C
        self.add_finding(3)
        self.assertEqual(remove_coverage(self.root, url=stale_a)["action"], "removed")
        self.assertEqual([e["url"] for e in json.loads(self.path.read_text())["entries"]], [stale_b, "https://news.example.test/c"])
        self.assertEqual(content_problems(self.root), [])

    def test_init_and_strip_metadata_are_unchanged(self):
        fresh = self.tmp / "fresh"
        started = subprocess.run([sys.executable, "-m", "campaign_tool", "init", "--directory", str(fresh), "--name", "Synthetic Cedar Watch",
                                  "--county", "Cedar", "--state", "CA"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(started.returncode, 0, started.stderr)
        result = subprocess.run([sys.executable, "-m", "campaign_tool", "content", "init", "--directory", str(fresh)],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((fresh / "content" / "coverage.json").exists(), "content init must not enable coverage")
        self.assertEqual(content_problems(fresh), [])
        listed = subprocess.run([sys.executable, "-m", "campaign_tool", "content", "--help"], cwd=REPO_ROOT,
                                capture_output=True, text=True, timeout=120)
        self.assertIn("coverage", listed.stdout)
        self.assertIn("strip-metadata", listed.stdout)


class PageTests(NewsCase):
    def test_news_order_escaping_backlinks_and_press_matrix(self):
        self.add_finding(2)
        self.add_finding(3)
        headline = 'Fish & Chips: "quoted" \'single\' *plain* [text](x) &amp; end'
        self.put(document(
            row(1, published_on="2026-09-01", url="https://news.example.test/b", headline="Tie second", kind="opinion"),
            row(2, published_on="2026-09-01", url="https://news.example.test/a?x=1&y=2", headline=headline, kind="broadcast",
                findings=[FINDING_ID, "fnd_0000000000000003"], outlet="Fish & Co. News"),
            row(3, published_on="2026-09-20", url="https://news.example.test/newest", headline="Newest", kind="other",
                findings=["fnd_0000000000000002"]),
            row(4, published_on="2025-12-31", url="https://news.example.test/oldest", headline="Oldest", findings=[FINDING_ID])))
        stored = self.path.read_bytes()
        files = self.build()
        self.assertEqual(self.path.read_bytes(), stored)  # the build never rewrites the input
        page = files["news.html"].decode()
        self.assertEqual(re.findall(r'<a href="(https://news\.example\.test/[^"]+)" rel="noopener noreferrer">', page),
                         ["https://news.example.test/newest", "https://news.example.test/a?x=1&amp;y=2",
                          "https://news.example.test/b", "https://news.example.test/oldest"])  # newest first, equal date by URL
        self.assertEqual(len(re.findall(r"<h1", page)), 1)
        self.assertIn("<h2>In the news</h2>", page)
        self.assertIn(f'<span class="news-outlet">{html.escape("Fish & Co. News")}</span>', page)
        self.assertIn(f'rel="noopener noreferrer">{html.escape(headline)}</a>', page)
        self.assertNotIn("Fish & Chips", page)
        self.assertNotIn("<em>", page)  # headlines are literal text, never Markdown
        self.assertIn('<time datetime="2026-09-20">2026-09-20</time>', page)
        for kind in ("News", "Opinion", "Broadcast", "Other"):
            self.assertIn(f'<span class="news-kind">{kind}</span>', page)
        self.assertIn('<a href="/findings/zq-2.html">Zq title 2</a>', page)
        cited = re.search(r"Fish &amp; Co\. News.*?</li></ul></div></li>", page, re.S).group(0)
        self.assertLess(cited.index(f'href="/findings/{FINDING_SLUG}.html"'), cited.index('href="/findings/zq-3.html"'))  # as stored
        self.assertEqual(page.count(FOOTER), 1)
        for forbidden in ("<script", "<img", "<iframe", "<blockquote", "<video", "<embed", "<object"):
            self.assertNotIn(forbidden, page)
        for text in ("Published headline 1", "story-1"):
            self.assertNotIn(text, page)  # only what the entries say
        self.assertEqual(check_links(self.public), [])
        self.assertEqual(check_accessibility(self.public), [])

        def reported(slug):
            return re.findall(r'<p class="reported-in">.*?</p>', files[f"findings/{slug}.html"].decode())
        self.assertEqual(reported(FINDING_SLUG), [
            '<p class="reported-in">Reported in: <a href="https://news.example.test/a?x=1&amp;y=2" rel="noopener noreferrer">'
            'Fish &amp; Co. News</a> (<time datetime="2026-09-01">2026-09-01</time>)</p>',
            '<p class="reported-in">Reported in: <a href="https://news.example.test/oldest" rel="noopener noreferrer">'
            'Example News</a> (<time datetime="2025-12-31">2025-12-31</time>)</p>'])
        self.assertEqual(len(reported("zq-2")), 1)
        self.assertEqual(len(reported("zq-3")), 1)
        self.assertNotIn("Newest", files[f"findings/{FINDING_SLUG}.html"].decode())  # only entries that name this finding
        pages_with_backlinks = {"news.html", f"findings/{FINDING_SLUG}.html", "findings/zq-2.html", "findings/zq-3.html"}
        for name, data in files.items():
            if name not in pages_with_backlinks:
                self.assertNotIn(b"Reported in", data, name)
                self.assertNotIn(b"news.example.test", data, name)  # not in feeds, the search index, JSON or other pages

        # press x coverage matrix: press.html exists only with site.json.press; its link only with coverage
        link = '<a href="/news.html">Press coverage of the campaign</a>'
        for press in (False, True):
            for coverage in (None, document(), document(row(1))):
                with self.subTest(press=press, coverage=None if coverage is None else len(coverage["entries"])):
                    self.site_json(press=PRESS if press else None)
                    self.path.unlink(missing_ok=True)
                    if coverage is not None:
                        self.put(coverage)
                    built = self.build()
                    self.assertEqual("press.html" in built, press)
                    self.assertEqual("news.html" in built, coverage is not None)
                    if press:
                        self.assertEqual(built["press.html"].decode().count(link), 0 if coverage is None else 1)
                    html_pages = [n for n in built if n.endswith(".html") and n != "news.html"]
                    linked = [n for n in html_pages if FOOTER.encode() in built[n]]
                    self.assertEqual(linked, [] if coverage is None else html_pages)
        self.put(document())
        empty = self.build()["news.html"].decode()
        self.assertIn('<p class="notice">No press coverage has been recorded yet.</p>', empty)
        self.assertNotIn('class="news-list"', empty)

    def without_catalogue(self, files):
        """DF2207-12 adds exactly two outputs, one footer paragraph and one sitemap URL; remove only those, keep every pinned digest.
        DF1109-08: the packet's default counterevidence block is asserted and removed the same way."""
        data_link = b'<p><a href="/data.html">Open data</a></p>'
        data_url = b'<url><loc>https://cedar-records.example.invalid/data.html</loc></url>'
        legacy = {}
        for name, data in files.items():
            if name in ("data.html", "data/datapackage.json", "data/cameras.csv"):  # DF0237-12 adds the camera CSV
                continue
            if name.endswith(".html"):
                assert data.count(data_link) == 1, name
                data = data.replace(data_link, b"", 1)
            if name == "sitemap.xml":
                assert data.count(data_url) == 1
                data = data.replace(data_url, b"", 1)
            legacy[name] = strip_packet_counterevidence(self, name, data)
        return legacy

    def test_optional_bytes_toggle_reservations_and_public_contracts(self):
        absent = self.build()
        self.assertEqual(self.digests(self.without_catalogue(absent)), BASELINE)  # no coverage file: every output is the pre-change render
        self.assertTrue(all(b"news.html" not in data for data in absent.values()))

        self.put(document(row(1, findings=[FINDING_ID]), row(2)))
        enabled = self.build()
        self.assertEqual(set(enabled) - set(absent), {"news.html"})
        self.assertEqual(set(absent) - set(enabled), set())
        reported = re.compile(r'<p class="reported-in">.*?</p>')
        for name, data in self.without_catalogue(enabled).items():
            if name == "news.html" or name == "sitemap.xml":
                continue
            if name.endswith(".html"):
                text = data.decode()
                self.assertEqual(text.count(FOOTER), 1, name)
                stripped = reported.sub("", text).replace(FOOTER, "", 1)  # then it is the untouched render
                self.assertEqual(hashlib.sha256(stripped.encode()).hexdigest(), BASELINE[name], name)
            else:
                self.assertEqual(hashlib.sha256(data).hexdigest(), BASELINE[name], name)  # feeds, JSON, CSV, scripts, hashes
        new_loc = "https://cedar-records.example.invalid/news.html"
        locs = LOC.findall(enabled["sitemap.xml"].decode())
        self.assertEqual(locs.count(new_loc), 1)
        self.assertEqual([loc for loc in locs if loc != new_loc], LOC.findall(absent["sitemap.xml"].decode()))
        nav = re.search(r"<nav.*?</nav>", enabled["index.html"].decode(), re.S).group(0)
        self.assertNotIn("news.html", nav)  # no top-nav entry

        self.path.unlink()  # disabling removes the page and every link on the next build
        self.assertEqual(self.build(), absent)

        self.put(document(row(1)))  # without base_url there is no sitemap, but the footer link and page stay
        self.site_json(base_url=None)
        no_base = self.build()
        self.assertNotIn("sitemap.xml", no_base)
        self.assertIn("news.html", no_base)
        self.assertEqual(no_base["index.html"].decode().count(FOOTER), 1)

        before = self.tree()  # a failed build keeps the previous public/ bytes
        self.put(document(row(1, findings=["fnd_9999999999999999"])))
        with self.assertRaisesRegex(ContentError, r"coverage\.json\.entries\[0\]\.findings"):
            build_site(self.root, today=BUILD_DAY)
        self.assertEqual(self.tree(), before)

    def test_reservations_hold_with_and_without_the_file(self):
        self.assertIn("news", RESERVED_PAGE_SLUGS)
        self.assertIn("news.html", GENERATED_TOP_LEVEL)
        for with_file in (False, True):
            with self.subTest(with_file=with_file):
                self.path.unlink(missing_ok=True)
                if with_file:
                    self.put(document())
                self.put({"title": "Our news", "description": "d", "body_md": "text"}, "pages/news.json")
                with self.assertRaisesRegex(ContentError, r"news"):
                    load_content(self.root, BUILD_DAY)
                self.assertTrue(any("news" in line for line in content_problems(self.root)))
                (self.content / "pages" / "news.json").unlink()
                self.put({"schema_version": 1, "redirects": [{"from": "/news.html", "to": "/about.html"}]}, "redirects.json")
                with self.assertRaisesRegex(ContentError, r"redirects\[0\]: from /news\.html is a page this build writes"):
                    self.build()
                (self.content / "redirects.json").unlink()
        shutil.rmtree(self.content / "pages")
        self.assertEqual(content_problems(self.root), [])


class LabelTests(NewsCase):
    def test_every_news_label_overridden_and_spanish_complete(self):
        for key in NEWS_KEYS:
            self.assertIn(key, DEFAULTS)
        self.assertEqual(DEFAULTS["finding.reported_in"], "Reported in: {outlet} ({date})")
        self.assertEqual(DEFAULTS["news.title"], "In the news")
        markers = {key: f"X{key}X" for key in NEWS_KEYS}
        markers["finding.reported_in"] = "Xreported {outlet} {date}X"
        self.put({"schema_version": 1, "labels": markers}, "labels.json")
        self.site_json(press=PRESS)
        self.put(document(row(1, kind="news", findings=[FINDING_ID]), row(2, kind="opinion"), row(3, kind="broadcast"),
                          row(4, kind="other")))
        files = self.build()
        news, finding, press = (files[name].decode() for name in ("news.html", f"findings/{FINDING_SLUG}.html", "press.html"))
        for key in ("news.title", "news.findings", "news.kind.news", "news.kind.opinion", "news.kind.broadcast", "news.kind.other"):
            self.assertIn(f"X{key}X", news, key)
        self.assertIn("Xnews.descriptionX", news)  # the meta description
        self.assertRegex(finding, r"Xreported <a href=\"https://news\.example\.test/story-1\" [^>]*>Example News</a> "
                                  r"<time datetime=\"2026-09-01\">2026-09-01</time>X")
        self.assertIn("Xpress.newsX", press)
        self.assertEqual([text.count("Xfooter.newsX") for text in (news, finding, press)], [1, 1, 1])
        for fixed in ("In the news", "Reported in", "Press coverage of the campaign", "Broadcast"):
            for name, text in (("news", news), ("finding", finding), ("press", press)):
                self.assertNotIn(fixed, text, (fixed, name))

        self.put(document(row(1, findings=[FINDING_ID])))  # the finding list label shows beside a cited finding
        self.assertIn("Xnews.findingsX", self.build()["news.html"].decode())
        self.put(document())
        markers["news.empty"] = "Xnews.emptyX"
        empty = self.build()["news.html"].decode()
        self.assertIn("Xnews.emptyX", empty)
        self.assertNotIn("No press coverage", empty)
        self.assertEqual(check_links(self.public), [])
        self.assertEqual(check_accessibility(self.public), [])

        before = snapshot(self.root)  # content-check is read-only
        self.assertEqual(content_problems(self.root), [])
        self.assertEqual(snapshot(self.root), before)

        draft = json.loads((REPO_ROOT / "examples" / "labels" / "es.json").read_text(encoding="utf-8"))["labels"]
        for key in NEWS_KEYS:
            with self.subTest(key):
                self.assertIn(key, draft)
                self.assertNotEqual(draft[key], DEFAULTS[key])
                self.assertEqual(set(PLACEHOLDER.findall(draft[key])), set(PLACEHOLDER.findall(DEFAULTS[key])))
        self.put({"schema_version": 1, "labels": draft}, "labels.json")
        self.put(document(row(1, findings=[FINDING_ID], kind="broadcast")))
        spanish = self.build()
        self.assertIn("En las noticias", spanish["news.html"].decode())
        self.assertIn("Transmisión", spanish["news.html"].decode())
        self.assertIn("Reportado en: <a ", spanish[f"findings/{FINDING_SLUG}.html"].decode())
        self.assertEqual(spanish["index.html"].decode().count('<a href="/news.html">En las noticias</a>'), 1)


if __name__ == "__main__":
    unittest.main()
