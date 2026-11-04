"""`sources verify-copy` (DF2037-05, RF0200-2): record whether a locally downloaded public copy is the same file as
the record, for the one public URL (library fragment) chosen when a record has several.

Synthetic campaign and bytes only; nothing is fetched. Hashes are built from bytes at test time (no hash literals).
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from campaign_tool import law, sources_cmd
from campaign_tool.content import ContentError
from campaign_tool.content_check import content_problems
from campaign_tool.site import build_site
from campaign_tool.sources_cmd import edit_source, verify_copy

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SCHEMA = json.loads((REPO_ROOT / "schemas" / "sources-open-data.schema.json").read_text(encoding="utf-8"))
RECORD = b"synthetic DF2037-05 record bytes; not a real document\n"
A = hashlib.sha256(RECORD).hexdigest()
B = hashlib.sha256(b"synthetic DF2037-05 private record\n").hexdigest()
X = hashlib.sha256(b"synthetic DF2037-05 x").hexdigest()
Y = X[:12] + hashlib.sha256(b"synthetic DF2037-05 y").hexdigest()[12:]  # a different record, same 12-char prefix
TODAY = date(2026, 3, 1)
SLUG = "cedar-policy-posted"
SAME = "same file as this record number"


def entry(sha, fragment, url="https://example.invalid/fixtures/policy", **extra):
    return {"sha256": sha, "title": "Synthetic record", "agency_id": "ca-cedar-police",
            "received_at": "2026-01-20T16:30:00+00:00", "public_url": url, "fragment": fragment, **extra}


def cli(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "sources", "verify-copy", *args], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=120)


class VerifyCopyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-verify-copy-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.sources = self.root / "content" / "sources.json"
        self.write_library([entry(A, "cedar-police-records"), entry(A, "cedar-second-home"),
                            entry(B, "cedar-private", url=None), entry(X, "cedar-x"), entry(Y, "cedar-y")])
        finding = self.root / "content" / "findings" / f"{SLUG}.json"
        data = json.loads(finding.read_text(encoding="utf-8"))
        data["sources"][0]["sha256"] = A
        finding.write_text(json.dumps(data), encoding="utf-8")
        self.copy = self.download(RECORD)

    def write_library(self, document):
        self.sources.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    def library(self):
        return json.loads(self.sources.read_text(encoding="utf-8"))

    def download(self, data, name="downloaded.pdf"):
        path = self.tmp / name
        path.write_bytes(data)
        return path

    def page(self, name):
        build_site(self.root)
        return (self.root / "public" / name).read_text(encoding="utf-8")

    def assert_refused(self, message, prefix=A, file=None, checked_on="2026-02-01", reason=None, today=TODAY):
        before = self.sources.read_bytes()
        with self.assertRaisesRegex(ValueError, message):
            verify_copy(self.root, prefix, str(file or self.copy), checked_on, reason, today=today)
        self.assertEqual(self.sources.read_bytes(), before)

    def test_matching_copy_is_recorded_on_every_entry_and_shown_without_network(self):
        with mock.patch("socket.socket", side_effect=OSError("network is not allowed in this test")):
            result = verify_copy(self.root, A[:12], str(self.copy), "2026-02-01", today=TODAY)
            sources_html, finding_html = self.page("sources.html"), self.page(f"findings/{SLUG}.html")
        check = {"matches": True, "checked_on": "2026-02-01"}
        self.assertEqual(result, {"sha256_prefix": A[:12], **check})
        self.assertEqual([e.get("public_copy") for e in self.library()], [check, check, None, None, None])
        status = f'Public copy checked <time datetime="2026-02-01">2026-02-01</time>: {SAME}'
        self.assertEqual(sources_html.count(status), 2)
        self.assertIn(status, finding_html)
        for how in ("How to check a copy", "<code>sha256sum FILE</code>", "<code>shasum -a 256 FILE</code>",
                    "<code>Get-FileHash FILE</code>"):
            self.assertEqual(sources_html.count(how), 1, how)
        doc = json.loads((self.root / "public" / "data" / "sources.json").read_text(encoding="utf-8"))
        law.validate(doc, SCHEMA)
        self.assertEqual([row["public_copy"] for row in doc["sources"]], [check, check, None, None, None])

    def test_differing_copy_with_a_reason_is_stored_and_shown(self):
        wording = {"redacted": "differs (redacted copy)", "converted": "differs (converted copy)",
                   "unknown": "differs (reason unknown)"}
        for reason, text in wording.items():
            with self.subTest(reason=reason):
                data = f"synthetic {reason} public copy\n".encode()
                result = verify_copy(self.root, A, str(self.download(data)), "2026-02-01", reason, today=TODAY)
                digest = hashlib.sha256(data).hexdigest()
                self.assertEqual(result, {"sha256_prefix": A[:12], "matches": False, "checked_on": "2026-02-01",
                                          "reason": reason, "copy_sha256_prefix": digest[:12]})
                self.assertEqual(self.library()[0]["public_copy"],
                                 {"matches": False, "checked_on": "2026-02-01", "reason": reason})
                html = self.page("sources.html")
                self.assertIn(text, html)
                self.assertNotIn(SAME, html)
                self.assertIn(text, self.page(f"findings/{SLUG}.html"))
                public = (self.root / "public" / "data" / "sources.json").read_text(encoding="utf-8")
                law.validate(json.loads(public), SCHEMA)
                self.assertNotIn(digest, public)

    def test_cli_refuses_a_mismatch_without_reason_and_a_match_with_one(self):
        root, before = str(self.root), self.sources.read_bytes()
        other = self.download(b"synthetic different public copy\n", "other.pdf")
        for args, message in (((str(other),), "Stopped: copy differs from the record; pass --differs-because"),
                              ((str(self.copy), "--differs-because", "redacted"),
                               "Stopped: copy matches; --differs-because is only for a differing copy")):
            with self.subTest(message=message):
                result = cli("--directory", root, "--sha256", A[:16], "--checked-on", "2026-02-01", "--file", *args)
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertIn(message, result.stderr)
                self.assertEqual(self.sources.read_bytes(), before)
        result = cli("--directory", root, "--sha256", A[:16], "--checked-on", "2026-02-01", "--file", str(self.copy))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"sha256_prefix": A[:12], "matches": True, "checked_on": "2026-02-01"})

    def test_bad_prefixes_are_refused_by_name(self):
        for prefix, message in ((A[:11], "--sha256: give 12 to 64 lowercase hex"),
                                ("g" * 12, "--sha256: give 12 to 64 lowercase hex"),
                                (A[:12].upper(), "--sha256: give 12 to 64 lowercase hex"),
                                (A + "0", "--sha256: give 12 to 64 lowercase hex"),
                                (hashlib.sha256(b"synthetic unlisted").hexdigest()[:12], "no library entry starts with"),
                                (X[:12], "prefix matches 2 entries; give more characters")):
            with self.subTest(prefix=prefix):
                self.assert_refused(message, prefix=prefix)

    def test_no_public_url_and_out_of_range_dates_are_refused(self):
        self.assert_refused("entry has no public_url; nothing to compare", prefix=B[:12])
        for checked_on, message in ((TODAY + timedelta(days=1)).isoformat(), "must not be in the future"), \
                ("2026-01-19", r"must not be before the record was received \(2026-01-20\)"), \
                ("2026-02-30", "a real YYYY-MM-DD date"), ("2026-2-01", "a real YYYY-MM-DD date"):
            with self.subTest(checked_on=checked_on):
                self.assert_refused(message, checked_on=checked_on)
        self.assert_refused("--differs-because: one of", reason="photocopied")
        verify_copy(self.root, A[:12], str(self.copy), "2026-01-20", today=TODAY)  # the received day itself is fine

    def test_symlink_directory_missing_and_oversized_files_are_refused(self):
        link = self.tmp / "link.pdf"
        os.symlink(self.copy, link)
        folder = self.tmp / "folder"
        folder.mkdir()
        for file, message in ((link, "is a symlink"), (folder, "is not a regular file|cannot be read"),
                              (self.tmp / "missing.pdf", "does not exist")):
            with self.subTest(file=file.name):
                self.assert_refused("--file: .*(" + message + ")", file=file)
        with mock.patch.object(sources_cmd, "COPY_MAX", len(RECORD) - 1), \
                mock.patch.object(sources_cmd.hashlib, "sha256", side_effect=AssertionError("hashed")):
            self.assert_refused("--file: larger than", file=self.copy)

    def test_editing_the_public_url_drops_the_check_and_the_status(self):
        verify_copy(self.root, A[:12], str(self.copy), "2026-02-01", today=TODAY)
        kept = edit_source(self.root, A[:12], "cedar-police-records", title="Synthetic retitled record")
        self.assertNotIn("public_copy_removed", kept)
        self.assertIn("public_copy", self.library()[0])
        self.assertEqual(edit_source(self.root, A[:12], "cedar-police-records", public_url="https://example.invalid/new"),
                         {"edited": A[:12], "fragment": "cedar-police-records", "changed": ["public_url"],
                          "public_copy_removed": True})
        self.assertEqual(edit_source(self.root, A[:12], "cedar-second-home", clear_public_url=True)["public_copy_removed"],
                         True)
        self.assertEqual([e.get("public_copy") for e in self.library()], [None] * 5)
        html = self.page("sources.html")
        for absent in ("Public copy checked", "How to check a copy"):
            self.assertNotIn(absent, html)
            self.assertNotIn(absent, self.page(f"findings/{SLUG}.html"))

    def test_hand_edited_public_copy_is_refused_by_build_and_content_check(self):
        bad = {"reason on a match": {"matches": True, "checked_on": "2026-02-01", "reason": "redacted"},
               "unknown reason": {"matches": False, "checked_on": "2026-02-01", "reason": "photocopied"},
               "missing reason": {"matches": False, "checked_on": "2026-02-01"},
               "string matches": {"matches": "yes", "checked_on": "2026-02-01"},
               "bad date": {"matches": True, "checked_on": "2026-02-30"},
               "extra key": {"matches": True, "checked_on": "2026-02-01", "copy_sha256": "x"},
               "not an object": "same"}
        for name, value in bad.items():
            with self.subTest(name=name):
                self.write_library([entry(A, "cedar-police-records", public_copy=value)])
                with self.assertRaisesRegex(ContentError, r"sources\.json\[0\]\.public_copy"):
                    build_site(self.root)
                self.assertTrue(any("sources.json[0].public_copy" in line for line in content_problems(self.root)))
        self.write_library([entry(A, "cedar-police-records", url=None,
                                  public_copy={"matches": True, "checked_on": "2026-02-01"})])
        with self.assertRaisesRegex(ContentError, r"sources\.json\[0\]\.public_copy: needs a public_url"):
            build_site(self.root)


U1 = "https://example.invalid/fixtures/policy"
U2 = "https://example.invalid/fixtures/redacted-policy"
DIFFERING = {"matches": False, "checked_on": "2026-01-25", "reason": "redacted"}
REFUSAL_MULTIPLE = "record has multiple public URLs; add --fragment to choose the copy"


def tree(root):
    """Every file under ``root`` as {relative path: bytes}, for byte-identical before/after comparisons."""
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(Path(root).rglob("*")) if path.is_file()}


class UrlScopedVerifyCopyTests(unittest.TestCase):
    """RF0200-2: one downloaded copy only attests to the entries sharing its exact public_url string."""
    setUp = VerifyCopyTests.setUp
    write_library = VerifyCopyTests.write_library
    library = VerifyCopyTests.library
    download = VerifyCopyTests.download
    page = VerifyCopyTests.page

    def refused(self, message, prefix=A, file=None, fragment=None, checked_on="2026-02-01", reason=None, today=TODAY):
        """verify_copy must refuse with ``message`` before hashing or writing anything."""
        before = tree(self.root)
        with mock.patch.object(sources_cmd, "_hash_copy", side_effect=AssertionError("hashed")), \
                mock.patch.object(sources_cmd, "_rewrite", side_effect=AssertionError("written")):
            with self.assertRaisesRegex(ValueError, message):
                verify_copy(self.root, prefix, str(file or self.copy), checked_on, reason, today=today, fragment=fragment)
        self.assertEqual(tree(self.root), before)

    def test_multiple_urls_require_fragment_before_hashing(self):
        self.write_library([entry(A, "cedar-original", url=U1), entry(A, "cedar-redacted", url=U2, public_copy=DIFFERING)])
        before = tree(self.root)
        self.refused(REFUSAL_MULTIPLE)
        result = cli("--directory", str(self.root), "--sha256", A[:16], "--checked-on", "2026-02-01", "--file",
                     str(self.copy))
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("Stopped: " + REFUSAL_MULTIPLE, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(tree(self.root), before)
        self.assertEqual(self.library()[1]["public_copy"], DIFFERING)

    def test_fragment_updates_only_exact_url_group(self):
        library = [entry(A, "cedar-one", url=U1), entry(A, "cedar-two", url=U1),
                   entry(A, "cedar-redacted", url=U2, note="keep me", public_copy=DIFFERING), entry(X, "cedar-x", url=U1)]
        self.write_library(library)
        shape = [list(item) for item in self.library()]
        result = verify_copy(self.root, A[:12], str(self.copy), "2026-02-01", today=TODAY, fragment="cedar-two")
        check = {"matches": True, "checked_on": "2026-02-01"}
        self.assertEqual(result, {"sha256_prefix": A[:12], **check})
        after = self.library()
        self.assertEqual([e.get("public_copy") for e in after], [check, check, DIFFERING, None])
        self.assertEqual(after[2], library[2])  # the other URL's entry is byte-for-value identical
        self.assertEqual(list(after[2]), shape[2])  # including key order and optional fields
        self.assertEqual([e["fragment"] for e in after], [e["fragment"] for e in library])
        self.assertNotIn("public_copy", after[3])  # another record sharing the URL is untouched

    def test_selected_group_includes_entries_of_other_fragments_and_agencies(self):
        second = entry(A, "cedar-sheriff", url=U1)
        second["agency_id"] = "ca-cedar-county-sheriff"
        self.write_library([entry(A, "cedar-one", url=U1), second, entry(A, "cedar-redacted", url=U2)])
        verify_copy(self.root, A[:12], str(self.copy), "2026-02-01", today=TODAY, fragment="cedar-one")
        self.assertEqual([e.get("public_copy") is not None for e in self.library()], [True, True, False])

    def test_selected_redacted_copy_never_changes_matching_alias(self):
        self.write_library([entry(A, "cedar-original", url=U1), entry(A, "cedar-redacted", url=U2)])
        match = {"matches": True, "checked_on": "2026-02-01"}
        verify_copy(self.root, A[:12], str(self.copy), "2026-02-01", today=TODAY, fragment="cedar-original")
        redacted = self.download(b"synthetic redacted public copy\n", "redacted.pdf")
        result = verify_copy(self.root, A[:12], str(redacted), "2026-02-02", "redacted", today=TODAY,
                             fragment="cedar-redacted")
        self.assertEqual(result["reason"], "redacted")
        first = {"matches": False, "checked_on": "2026-02-02", "reason": "redacted"}
        self.assertEqual([e["public_copy"] for e in self.library()], [match, first])
        # opposite direction: a match replaces the earlier differing check, no reason key survives
        verify_copy(self.root, A[:12], str(self.copy), "2026-02-03", today=TODAY, fragment="cedar-redacted")
        self.assertEqual([e["public_copy"] for e in self.library()],
                         [match, {"matches": True, "checked_on": "2026-02-03"}])
        # and a differing recheck of the first URL replaces its match without touching the second
        verify_copy(self.root, A[:12], str(redacted), "2026-02-04", "converted", today=TODAY, fragment="cedar-original")
        self.assertEqual([e["public_copy"] for e in self.library()],
                         [{"matches": False, "checked_on": "2026-02-04", "reason": "converted"},
                          {"matches": True, "checked_on": "2026-02-03"}])

    def test_url_comparison_does_not_normalize(self):
        variants = [U1, U1 + "/", U1 + "?page=1", U1 + "#top", U1.replace("policy", "Policy")]
        self.write_library([entry(A, f"cedar-v{number}", url=url) for number, url in enumerate(variants)])
        self.refused(REFUSAL_MULTIPLE)
        for number in range(len(variants)):
            with self.subTest(variant=variants[number]):
                verify_copy(self.root, A[:12], str(self.copy), "2026-02-0%d" % (number + 1), today=TODAY,
                            fragment=f"cedar-v{number}")
                checks = [e.get("public_copy") for e in self.library()]
                self.assertEqual([c["checked_on"] for c in checks if c], ["2026-02-0%d" % (n + 1)
                                                                          for n in range(number + 1)])
                self.assertEqual([c is not None for c in checks], [n <= number for n in range(len(variants))])

    def test_fragment_does_not_disambiguate_record_prefix(self):
        self.refused("prefix matches 2 entries; give more characters", prefix=X[:12], fragment="cedar-x")
        self.write_library([entry(A, "cedar-one", url=U1), entry(A, "cedar-null", url=None), entry(B, "cedar-b")])
        for fragment, message in (("cedar-missing", "no library entry for this record in the selected fragment"),
                                  ("cedar-b", "no library entry for this record in the selected fragment"),
                                  ("Cedar-One", "no library entry for this record in the selected fragment"),
                                  ("cedar", "no library entry for this record in the selected fragment"),
                                  ("cedar-one ", "no library entry for this record in the selected fragment"),
                                  ("", "--fragment: give a non-empty library fragment"),
                                  (7, "--fragment: give a non-empty library fragment"),
                                  (["cedar-one"], "--fragment: give a non-empty library fragment"),
                                  ("cedar-null", "selected entry has no public_url; nothing to compare")):
            with self.subTest(fragment=fragment):
                self.refused(message, fragment=fragment)
        for prefix in (A[:11], A[:12].upper()):  # a fragment cannot rescue a bad prefix
            self.refused("--sha256: give 12 to 64 lowercase hex", prefix=prefix, fragment="cedar-one")
        result = cli("--directory", str(self.root), "--sha256", A[:12], "--checked-on", "2026-02-01", "--file",
                     str(self.copy), "--fragment", "")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("Stopped: --fragment: give a non-empty library fragment", result.stderr)

    def test_only_selected_alias_dates_bound_check(self):
        later = entry(A, "cedar-later", url=U2, received_at="2026-02-20T09:00:00+00:00")
        self.write_library([entry(A, "cedar-one", url=U1), later])
        verify_copy(self.root, A[:12], str(self.copy), "2026-02-01", today=TODAY, fragment="cedar-one")  # unselected later receipt
        self.assertEqual(self.library()[0]["public_copy"], {"matches": True, "checked_on": "2026-02-01"})
        later_same_url = entry(A, "cedar-later", url=U1, received_at="2026-02-10T09:00:00+00:00")
        self.write_library([entry(A, "cedar-one", url=U1), later_same_url])
        self.refused(r"must not be before the record was received \(2026-02-10\)", fragment="cedar-one")
        self.refused(r"must not be before the record was received \(2026-02-10\)", fragment="cedar-later")
        verify_copy(self.root, A[:12], str(self.copy), "2026-02-10", today=TODAY, fragment="cedar-one")  # the received day
        self.assertEqual([e["public_copy"]["checked_on"] for e in self.library()], ["2026-02-10"] * 2)
        self.write_library([entry(A, "cedar-one", url=U1), later])
        for checked_on, message in (((TODAY + timedelta(days=1)).isoformat(), "must not be in the future"),
                                    ("2026-02-30", "a real YYYY-MM-DD date"),
                                    ("2026-01-19", r"must not be before the record was received \(2026-01-20\)")):
            with self.subTest(checked_on=checked_on):
                self.refused(message, fragment="cedar-one", checked_on=checked_on)

    def test_selected_group_keeps_file_safety_and_atomicity(self):
        self.write_library([entry(A, "cedar-original", url=U1), entry(A, "cedar-redacted", url=U2, public_copy=DIFFERING)])
        link, folder, fifo = self.tmp / "link.pdf", self.tmp / "folder", self.tmp / "fifo.pdf"
        os.symlink(self.copy, link)
        folder.mkdir()
        os.mkfifo(fifo)
        before = tree(self.root)
        for file, message in ((link, "is a symlink"), (folder, "is not a regular file|cannot be read"),
                              (fifo, "is not a regular file|cannot be read"), (self.tmp / "missing.pdf", "does not exist")):
            with self.subTest(file=file.name):
                with self.assertRaisesRegex(ValueError, "--file: .*(" + message + ")"):
                    verify_copy(self.root, A[:12], str(file), "2026-02-01", today=TODAY, fragment="cedar-redacted")
                self.assertEqual(tree(self.root), before)
        with mock.patch.object(sources_cmd, "COPY_MAX", len(RECORD) - 1), \
                mock.patch.object(sources_cmd.hashlib, "sha256", side_effect=AssertionError("hashed")):
            with self.assertRaisesRegex(ValueError, "--file: larger than"):
                verify_copy(self.root, A[:12], str(self.copy), "2026-02-01", today=TODAY, fragment="cedar-redacted")
        other = self.download(b"synthetic different public copy\n", "other.pdf")
        with self.assertRaisesRegex(ValueError, "copy differs from the record; pass --differs-because"):
            verify_copy(self.root, A[:12], str(other), "2026-02-01", today=TODAY, fragment="cedar-redacted")
        with self.assertRaisesRegex(ValueError, "copy matches; --differs-because is only for a differing copy"):
            verify_copy(self.root, A[:12], str(self.copy), "2026-02-01", "redacted", today=TODAY, fragment="cedar-redacted")
        self.assertEqual(tree(self.root), before)
        self.sources.chmod(0o640)
        original = self.sources.read_bytes()
        with mock.patch.object(sources_cmd.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                verify_copy(self.root, A[:12], str(self.copy), "2026-02-01", today=TODAY, fragment="cedar-redacted")
        self.assertEqual(self.sources.read_bytes(), original)
        self.assertEqual(self.sources.stat().st_mode & 0o777, 0o640)
        self.assertEqual(list(self.sources.parent.glob(".sources-*")), [])

    def test_url_scoped_cli_and_public_output_without_network(self):
        self.write_library([entry(A, "cedar-original", url=U1), entry(A, "cedar-redacted", url=U2)])
        redacted = self.download(b"synthetic redacted public copy\n", "redacted.pdf")
        digest = hashlib.sha256(redacted.read_bytes()).hexdigest()
        helped = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "sources", "verify-copy", "--help"],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(helped.returncode, 0, helped.stderr)
        self.assertIn("--fragment", helped.stdout)
        base = ("--directory", str(self.root), "--sha256", A[:16], "--checked-on", "2026-02-01")
        refusal = cli(*base, "--file", str(self.copy), "--fragment", "cedar-missing")
        self.assertEqual(refusal.returncode, 3, refusal.stderr)
        self.assertIn("Stopped: no library entry for this record in the selected fragment", refusal.stderr)
        first = cli(*base, "--file", str(self.copy), "--fragment", "cedar-original")
        second = cli(*base, "--file", str(redacted), "--fragment", "cedar-redacted", "--differs-because", "redacted")
        self.assertEqual((first.returncode, second.returncode), (0, 0), first.stderr + second.stderr)
        self.assertEqual(json.loads(first.stdout), {"sha256_prefix": A[:12], "matches": True, "checked_on": "2026-02-01"})
        self.assertEqual(json.loads(second.stdout), {"sha256_prefix": A[:12], "matches": False, "checked_on": "2026-02-01",
                                                     "reason": "redacted", "copy_sha256_prefix": digest[:12]})
        for report in (first.stdout, second.stdout, refusal.stderr):
            for private in (str(self.tmp), "redacted.pdf", "downloaded.pdf", U1, U2, "cedar-original", "cedar-redacted",
                            A, digest):
                self.assertNotIn(private, report)
        with mock.patch("socket.socket", side_effect=OSError("network is not allowed in this test")):
            html = self.page("sources.html")
        self.assertEqual(html.count(f'Public copy checked <time datetime="2026-02-01">2026-02-01</time>: {SAME}'), 1)
        self.assertEqual(html.count('Public copy checked <time datetime="2026-02-01">2026-02-01</time>: '
                                    "differs (redacted copy)"), 1)
        public = (self.root / "public" / "data" / "sources.json").read_text(encoding="utf-8")
        doc = json.loads(public)
        law.validate(doc, SCHEMA)
        self.assertEqual([row["public_copy"] for row in doc["sources"]],
                         [{"matches": True, "checked_on": "2026-02-01"},
                          {"matches": False, "checked_on": "2026-02-01", "reason": "redacted"}])
        for text in (html, public):
            self.assertNotIn(str(self.tmp), text)
            self.assertNotIn("redacted.pdf", text)
            self.assertNotIn(digest, text)


if __name__ == "__main__":
    unittest.main()
