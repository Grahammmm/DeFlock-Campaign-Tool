"""DF1103-14: opt-in quarterly correction-request counts on corrections.html.

Synthetic content only (a temp copy of the fictional example); injected build day; no network.
Run from the repository root: python3 -m unittest tests.test_site_correction_counts
"""
import io
import json
import os
import shutil
import socket
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import correction_requests as cr
from campaign_tool.content import ContentError, load_site
from campaign_tool.content_check import content_problems
from campaign_tool.site import build_site, preview_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 10, 20)
REFUSAL = "private/correction-requests.json: cannot publish correction request counts; check the private log"
SENTINEL = "ZZ-SENTINEL"
LABELS = ("heading", "lead", "caption", "col_quarter", "col_received", "col_accepted", "col_declined", "col_open", "empty")


def request(number, received, state="open", decided=None, **extra):
    row = {"id": number, "finding_id": f"{SENTINEL}-finding-{number}", "received_on": received,
           "summary": f"{SENTINEL}-summary-{number}", "state": state}
    if state != "open":
        row.update(decided_on=decided or received, note=f"{SENTINEL}-note-{number}")
    row.update(extra)
    return row


class CorrectionCountsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-correction-counts-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.site_json = self.root / "content" / "site.json"
        self.public = self.root / "public"
        self.log = self.root / "private" / "correction-requests.json"

    def setting(self, value="absent"):
        document = json.loads(self.site_json.read_text(encoding="utf-8"))
        document.pop("corrections", None)
        if value != "absent":
            document["corrections"] = value
        self.site_json.write_text(json.dumps(document), encoding="utf-8")

    def enable(self):
        self.setting({"show_request_counts": True})

    def write_log(self, requests, version=1):
        self.log.parent.mkdir(exist_ok=True)
        self.log.write_text(json.dumps({"schema_version": version, "requests": requests}), encoding="utf-8")

    def tree(self):
        return {str(p.relative_to(self.public)): p.read_bytes() for p in sorted(self.public.rglob("*")) if p.is_file()}

    def build(self):
        build_site(self.root, today=TODAY)
        return self.tree()

    def section(self, files=None):
        page = (files or self.tree())["corrections.html"].decode()
        start = page.index('<section id="request-counts">')
        return page[start:page.index("</section>", start)]

    def rows(self):
        return cr.quarterly_counts(self.root, TODAY)

    def test_flag_schema_and_content_check(self):
        for value in ("absent", {}, {"show_request_counts": False}, {"show_request_counts": True}):
            self.setting(value)
            self.build()
        for bad, field in (({"show_request_counts": 1}, "site.json.corrections.show_request_counts"),
                           ({"show_request_counts": 0}, "site.json.corrections.show_request_counts"),
                           ({"show_request_counts": "yes"}, "site.json.corrections.show_request_counts"),
                           ({"show_request_counts": None}, "site.json.corrections.show_request_counts"),
                           ({"show_request_counts": True, SENTINEL: 1}, "site.json.corrections"),
                           (None, "site.json.corrections"), ([], "site.json.corrections")):
            self.setting(bad)
            with self.assertRaises(ContentError) as caught:
                load_site(json.loads(self.site_json.read_text(encoding="utf-8")))
            self.assertIn(field, str(caught.exception))
            self.assertNotIn("yes", str(caught.exception))
            with self.assertRaises(ContentError):
                build_site(self.root, today=TODAY)
            self.assertTrue(any(field in problem for problem in content_problems(self.root)), field)
        # content-check stays content-only: it never opens the private log, even with the flag true
        self.enable()
        self.write_log([request(1, "2026-01-05")])
        real_open = os.open
        seen = []
        with mock.patch("os.open", side_effect=lambda path, *a, **k: seen.append(str(path)) or real_open(path, *a, **k)):
            self.assertEqual(content_problems(self.root), [])
        self.assertFalse([p for p in seen if "correction-requests" in p])

    def test_default_never_touches_private(self):
        broken = {"corrupt": lambda: self.log.write_bytes(b"{" + SENTINEL.encode()),
                  "oversized": lambda: self.log.write_bytes(b" " * (4 * 1024 * 1024 + 2)),
                  "directory": lambda: self.log.mkdir(),
                  "dangling": lambda: os.symlink(self.root / "nowhere", self.log),
                  "valid": lambda: self.write_log([request(1, "2026-01-05")]),
                  "unreadable": lambda: (self.write_log([request(1, "2026-01-05")]), self.log.chmod(0)),
                  "parent link": lambda: (shutil.rmtree(self.root / "private", ignore_errors=True),
                                          os.symlink(self.root / "elsewhere", self.root / "private"))}
        self.build()
        baseline = self.tree()
        for setting in ("absent", {}, {"show_request_counts": False}):
            self.setting(setting)
            baseline = self.build()
            for name, make in broken.items():
                with self.subTest(setting=setting, log=name):
                    private = self.root / "private"
                    if private.is_symlink():
                        private.unlink()
                    elif private.is_dir():
                        shutil.rmtree(private, ignore_errors=True) if name != "valid" else None
                    private.mkdir(exist_ok=True) if name != "parent link" else None
                    if self.log.is_symlink() or self.log.is_file():
                        self.log.chmod(0o600) if self.log.is_file() else None
                        self.log.unlink()
                    make()
                    opened = []
                    real = {"io.open": io.open, "os.open": os.open}

                    def guard(kind):
                        def inner(path, *a, **k):
                            if "correction-requests" in str(path):
                                opened.append(str(path))
                            return real[kind](path, *a, **k)
                        return inner
                    with mock.patch("builtins.open", side_effect=guard("io.open")), \
                            mock.patch("io.open", side_effect=guard("io.open")), \
                            mock.patch("os.open", side_effect=guard("os.open")), \
                            mock.patch.object(cr, "quarterly_counts", side_effect=AssertionError("called")):
                        build_site(self.root, today=TODAY)
                        preview_site(self.root, today=TODAY)
                    self.assertEqual(opened, [])
                    self.assertEqual(self.tree(), baseline)
                    if self.log.exists() and not self.log.is_dir():
                        self.log.chmod(0o600)

    def test_quarter_cohorts_and_current_disposition(self):
        self.write_log([
            request(1, "2026-03-31"), request(2, "2026-04-01", "accepted", "2026-10-02"),
            request(3, "2026-06-30", "declined", "2026-06-30"), request(4, "2026-07-01"),
            request(5, "2026-09-30", "accepted", "2026-10-01"), request(6, "2025-10-01", "declined", "2025-12-31"),
            request(7, "2025-12-31"), request(8, "2026-01-01"), request(9, "2024-02-29", "declined", "2026-02-01")])
        self.assertEqual(self.rows(), [
            {"quarter": "2026-Q3", "received": 2, "accepted": 1, "declined": 0, "still_open": 1},
            {"quarter": "2026-Q2", "received": 2, "accepted": 1, "declined": 1, "still_open": 0},
            {"quarter": "2026-Q1", "received": 2, "accepted": 0, "declined": 0, "still_open": 2},
            {"quarter": "2025-Q4", "received": 2, "accepted": 0, "declined": 1, "still_open": 1},
            {"quarter": "2024-Q1", "received": 1, "accepted": 0, "declined": 1, "still_open": 0}])
        for row in self.rows():
            self.assertEqual(row["received"], row["accepted"] + row["declined"] + row["still_open"])
        self.assertEqual(set(self.rows()[0]), {"quarter", "received", "accepted", "declined", "still_open"})

    def test_historical_finding_not_resolved(self):
        self.write_log([request(1, "2026-02-02")])
        with mock.patch("campaign_tool.correction_requests.load_content", side_effect=AssertionError("resolved")):
            self.assertEqual(self.rows()[0]["received"], 1)
        self.enable()
        self.assertNotIn(SENTINEL, self.build()["corrections.html"].decode())

    def test_enabled_empty_and_missing_storage(self):
        self.enable()
        for prepare in (lambda: shutil.rmtree(self.root / "private", ignore_errors=True),
                        lambda: (self.root / "private").mkdir(exist_ok=True),
                        lambda: self.write_log([])):
            prepare()
            files = self.build()
            self.assertIn("No correction requests have been logged yet.", self.section(files))
            self.assertNotIn("<table", self.section(files))
        shutil.rmtree(self.root / "private", ignore_errors=True)
        self.assertEqual(self.rows(), [])
        self.assertFalse((self.root / "private").exists())

    def test_whole_document_refusals(self):
        good = request(1, "2026-02-02")
        cases = [b"not json", b"\xff\xfe", b" " * (4 * 1024 * 1024 + 1), b"[]", b'{"schema_version": 1}',
                 json.dumps({"schema_version": True, "requests": []}).encode(),
                 json.dumps({"schema_version": 2, "requests": []}).encode(),
                 json.dumps({"schema_version": 1, "requests": {}}).encode(),
                 json.dumps({"schema_version": 1, "requests": [], SENTINEL: 1}).encode()]
        bad_rows = [[1], [dict(good, **{SENTINEL: 1})], [{k: v for k, v in good.items() if k != "summary"}],
                    [dict(good, id=True)], [dict(good, id=0)], [dict(good, id="1")], [dict(good, state="pending")],
                    [dict(good, summary=3)], [dict(good, received_on="2026-2-2")], [dict(good, received_on="2026-02-30")],
                    [dict(good, received_on="2026-10-21")], [dict(good, decided_on="2026-02-03")],
                    [{k: v for k, v in request(1, "2026-02-02", "declined").items() if k != "note"}],
                    [request(1, "2026-02-02", "declined", "2026-02-01")],
                    [request(1, "2026-02-02", "declined", "2026-10-21")],
                    [request(1, "2026-02-02"), request(1, "2026-02-03")], [request(2, "2026-02-02"), request(1, "2026-02-03")]]
        cases += [json.dumps({"schema_version": 1, "requests": rows}).encode() for rows in bad_rows]
        self.enable()
        good_files = self.build()
        for data in cases:
            with self.subTest(data=data[:40]):
                self.log.parent.mkdir(exist_ok=True)
                self.log.write_bytes(data)
                with self.assertRaises(ContentError) as caught:
                    build_site(self.root, today=TODAY)
                self.assertEqual(str(caught.exception), REFUSAL)
                self.assertIsNone(caught.exception.__cause__)
                with self.assertRaises(ContentError):
                    preview_site(self.root, today=TODAY)
                self.assertEqual(self.tree(), good_files)  # the previous public bytes survive

    def test_symlink_fifo_permission_refusals(self):
        self.enable()
        private = self.root / "private"
        outside = Path(tempfile.mkdtemp(prefix="synthetic-outside-"))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        (outside / "correction-requests.json").write_text(json.dumps({"schema_version": 1, "requests": []}), encoding="utf-8")
        private.mkdir()
        os.symlink(outside / "correction-requests.json", self.log)  # file link
        self.refuse_all()
        self.log.unlink()
        os.symlink(outside / "gone.json", self.log)  # dangling link
        self.refuse_all()
        self.log.unlink()
        self.log.mkdir()  # directory
        self.refuse_all()
        self.log.rmdir()
        os.mkfifo(self.log)  # FIFO: refused, never blocks
        self.refuse_all()
        self.log.unlink()
        private.rmdir()
        os.symlink(outside, private)  # parent link
        self.refuse_all()
        private.unlink()
        private.mkdir()
        self.write_log([request(1, "2026-01-05")])
        with mock.patch("campaign_tool.correction_requests._read_json", side_effect=PermissionError(13, SENTINEL, str(outside))):
            self.refuse_all()

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory permissions")
    def test_unsearchable_private_directory_is_refused_not_empty(self):
        self.enable()
        self.write_log([request(1, "2026-01-05")])
        private = self.root / "private"
        private.chmod(0o600)
        self.addCleanup(private.chmod, 0o700)
        self.refuse_all()

    def refuse_all(self):
        with self.assertRaises(ContentError) as caught:
            build_site(self.root, today=TODAY)
        text = str(caught.exception)
        self.assertEqual(text, REFUSAL)
        self.assertNotIn(SENTINEL, text)
        self.assertNotIn("outside", text)
        self.assertIsNone(caught.exception.__cause__)
        self.assertFalse(self.public.exists())

    def test_private_values_never_escape(self):
        self.enable()
        self.write_log([request(1, "2026-02-02", "accepted", "2026-04-05"), request(2, "2026-02-11")])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            first = self.build()
            preview_site(self.root, today=TODAY)
        for private in (SENTINEL, "2026-02-02", "2026-04-05", "2026-02-11", "correction-requests", "private/"):
            self.assertFalse([name for name, data in first.items() if private.encode() in data], private)
            self.assertNotIn(private, out.getvalue() + err.getvalue())
        section = self.section(first)
        self.assertEqual(section.count("data-"), section.count('data-label="'))  # no other data attribute
        self.assertNotIn("<!--", section)
        self.write_log([request(7, "2026-02-20", "accepted", "2026-09-01"), request(9, "2026-03-30")])
        again = self.build()
        self.assertEqual(again, first)  # same cohorts and states, different private strings, ids and dates

    def test_labels_accessibility_and_handover(self):
        self.build()
        off = self.tree()
        self.enable()
        self.write_log([request(1, "2026-02-02", "declined", "2026-02-03"), request(2, "2026-05-02")])
        self.log.chmod(0o600)
        mode, before_bytes = stat.S_IMODE(self.log.stat().st_mode), self.log.read_bytes()
        on = self.build()
        self.assertEqual(self.log.read_bytes(), before_bytes)
        self.assertEqual(set(on), set(off))
        self.assertEqual([name for name in on if on[name] != off[name]], ["corrections.html"])
        page = on["corrections.html"].decode()
        self.assertIn('id="request"', page)  # the policy anchor stays
        self.assertIn("Correction requests we received", self.section(on))
        self.assertIn("Grouped by quarter received; decisions and still-open counts reflect the current log, "
                      "not quarter-end totals.", self.section(on))
        self.assertIn('<th scope="row" data-label="Quarter">2026-Q2</th><td data-label="Received">1</td>', self.section(on))
        self.assertIn('<th scope="col">Still open</th>', self.section(on))
        self.assertIn('/requests.html', on["index.html"].decode())  # DF1103-13 footer slot intact
        self.assertEqual(self.build(), on)  # stable rebuild
        self.assertEqual(stat.S_IMODE(self.log.stat().st_mode), mode)
        # every label is overridable and escaped
        labels = {f"correction_counts.{name}": f'X&"{name}' for name in LABELS}
        (self.root / "content" / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": labels}), encoding="utf-8")
        relabelled = self.section(self.build())
        for name in LABELS:
            if name != "empty":
                self.assertIn(f"X&amp;&quot;{name}", relabelled, name)
        self.write_log([])
        self.assertIn("X&amp;", self.section(self.build()))
        (self.root / "content" / "labels.json").unlink()
        # network is never needed
        with mock.patch.object(socket, "socket", side_effect=AssertionError("network")):
            self.build()

    def test_duplicate_log_refuses_counts_and_default_build_never_reads_it(self):
        text = ('{"schema_version": 1, "requests": [{"id": 1, "finding_id": "%s-f", "received_on": "2026-01-05", '
                '"summary": "%s-s", "state": "declined", "state": "open"}]}' % (SENTINEL, SENTINEL))
        variants = [text, text.replace('"state": "declined", "state": "open"', '"state": "open", "st\\u0061te": "open"'),
                    '{"schema_version": 1, "schema_version": 1, "requests": []}']
        self.log.parent.mkdir(exist_ok=True)
        self.enable()
        for variant in variants:
            with self.subTest(variant=variant[:60]):
                self.log.write_text(variant, encoding="utf-8")
                before = self.log.read_bytes()
                with self.assertRaises(ContentError) as caught:
                    cr.quarterly_counts(self.root, TODAY)
                self.assertEqual(str(caught.exception), REFUSAL)
                self.assertIsNone(caught.exception.__cause__)
                self.assertNotIn(SENTINEL, str(caught.exception))
                with self.assertRaises(ContentError) as built:
                    build_site(self.root, today=TODAY)
                self.assertEqual(str(built.exception), REFUSAL)
                self.assertFalse(self.public.exists())
                self.assertEqual(self.log.read_bytes(), before)
        # counts disabled: the build never opens the duplicate-bearing log
        self.log.write_text(text, encoding="utf-8")
        for setting in ("absent", {}, {"show_request_counts": False}):
            self.setting(setting)
            opened = []
            real = {"io.open": io.open, "os.open": os.open}

            def guard(kind):
                def inner(path, *a, **k):
                    if "correction-requests" in str(path):
                        opened.append(str(path))
                    return real[kind](path, *a, **k)
                return inner
            with mock.patch("io.open", guard("io.open")), mock.patch("os.open", guard("os.open")):
                self.build()
            self.assertEqual(opened, [])

    def test_duplicate_free_log_keeps_counts(self):
        self.enable()
        self.write_log([request(1, "2026-01-05", "declined", "2026-01-06"), request(2, "2026-02-02")])
        self.assertEqual(self.rows(), [{"quarter": "2026-Q1", "received": 2, "accepted": 0, "declined": 1, "still_open": 1}])
        self.build()


if __name__ == "__main__":
    unittest.main()
