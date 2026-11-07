"""DF0022-11: data/sources.zip holds only this build's own public source copies, deterministically (synthetic files, no network)."""
import contextlib
import csv
import hashlib
import io
import json
import os
import re
import shutil
import socket
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from campaign_tool import cli, site as site_module, source_bundle
from campaign_tool.content import ContentError
from campaign_tool.site import build_site, preview_site
from campaign_tool.site_data_catalogue import verify_data_package
from campaign_tool.source_bundle import bundle_bytes, bundle_selection, check_source_bundle
from tests.test_doc_assets import pdf
from tests.test_site_build import EXAMPLE

BASE = "https://cedar-records.example.invalid"
ES_LABELS = Path(__file__).resolve().parents[1] / "examples" / "labels" / "es.json"
NOTE_EMPTY, NOTE_OVERSIZE = source_bundle.NOTE_EMPTY, source_bundle.NOTE_OVERSIZE


def entry(number, url, title="Doc", agency="ca-cedar-police", received="2026-02-01T10:00:00+00:00", fragment="cedar-police-records"):
    return {"sha256": f"{number:02x}" * 32, "title": title, "agency_id": agency, "received_at": received,
            "public_url": url, "fragment": fragment, "pages": 1, "note": "private note"}


def run_build(root, check=False):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        cli.build(root, check=check)
    assert err.getvalue() == "", err.getvalue()
    return "".join(line + "\n" for line in out.getvalue().splitlines() if line.startswith("sources.zip"))  # the skip line; stderr must stay empty


class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-source-bundle-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public, self.content = self.root / "public", self.root / "content"
        self.add_sources(entry(1, BASE + "/docs/a.pdf#page=2", "Alpha"), entry(2, BASE + "/docs/b.pdf", "Beta"))
        self.asset("docs/a.pdf", pdf(b"<< /Length 0 >>"))
        self.asset("docs/b.pdf", pdf())
        patcher = mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network used"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def asset(self, name, data):
        path = self.content / "public-assets" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def add_sources(self, *new, replace=False):
        path = self.content / "sources.json"
        rows = [] if replace else json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps(rows + list(new)), encoding="utf-8")

    def zip_bytes(self):
        return (self.public / "data" / "sources.zip").read_bytes()

    def members(self, data=None):
        with zipfile.ZipFile(io.BytesIO(data if data is not None else self.zip_bytes())) as archive:
            return {info.filename: archive.read(info) for info in archive.infolist()}

    def manifest(self, data=None):
        return list(csv.reader(io.StringIO(self.members(data)["manifest.csv"].decode("utf-8"), newline="")))


class MembershipTests(Fixture):
    def test_only_current_asset_copy_membership(self):
        assets = {"docs/a.pdf", "docs/b.pdf", "style.css"}
        urls = {
            "ok": BASE + "/docs/a.pdf#frag", "external": "https://example.invalid/docs/a.pdf",
            "sibling": "https://cedar-records.example.invalid.evil.invalid/docs/a.pdf", "port": BASE + ":8443/docs/a.pdf",
            "http-like prefix": "https://cedar-records.example.invalid/x/docs/a.pdf", "query": BASE + "/docs/a.pdf?x=1",
            "encoded dot": BASE + "/docs/%2e%2e/docs/a.pdf", "encoded slash": BASE + "/docs%2fa.pdf",
            "traversal": BASE + "/docs/../docs/a.pdf", "dot": BASE + "/docs/./a.pdf", "empty": BASE + "/docs//a.pdf",
            "generated": BASE + "/data/sources.json", "unreferenced": BASE + "/docs/missing.pdf", "root": BASE + "/",
            "credentials": "https://" + "user" + ":" + "pw" + "@cedar-records.example.invalid/docs/a.pdf", "null": None,
        }
        rows = [entry(i, url, name) for i, (name, url) in enumerate(urls.items(), 1)]
        chosen = bundle_selection(rows, BASE, assets)
        self.assertEqual([(row["title"], path) for row, path in chosen], [("ok", "docs/a.pdf")])
        self.assertEqual(bundle_selection(rows, BASE + "/", assets)[0][1], "docs/a.pdf")  # trailing slash on base_url
        self.assertEqual(bundle_selection(rows, None, assets), [])  # no base_url: nothing is local
        prefixed = bundle_selection([entry(1, "https://h.invalid/site/docs/a.pdf")], "https://h.invalid/site", assets)
        self.assertEqual([path for _, path in prefixed], ["docs/a.pdf"])
        self.assertEqual(bundle_selection([entry(1, "https://h.invalid/sitemap/docs/a.pdf")], "https://h.invalid/site", assets), [])

    def test_build_bundles_only_copied_files(self):
        self.add_sources(entry(3, "https://example.invalid/docs/c.pdf", "External"),
                         entry(4, BASE + "/data/sources.json", "Generated"), entry(5, BASE + "/docs/ghost.pdf", "Ghost"))
        self.asset("docs/orphan.pdf", pdf())  # copied to public/ but cited by no library entry
        build_site(self.root)
        self.assertEqual(sorted(self.members()), ["files/docs/a.pdf", "files/docs/b.pdf", "manifest.csv"])
        self.assertEqual(self.members()["files/docs/a.pdf"], (self.public / "docs" / "a.pdf").read_bytes())


class ManifestTests(Fixture):
    def test_manifest_public_fields_and_safe_csv(self):
        rows = [(entry(1, None, "=SUM(1,2)", agency="unknown-agency", received="2026-01-01T00:00:00+00:00"), "docs/a.pdf"),
                (entry(2, None, 'Quote " comma, line\nbreak é', received="2026-01-02T00:00:00+00:00"), "docs/a.pdf"),  # same copy twice
                (entry(3, None, "@cmd", received="2026-01-03T00:00:00+00:00"), "docs/b.pdf")]
        for prefix in "+-\t\r":
            rows.append((entry(len(rows) + 1, None, prefix + "x"), "docs/b.pdf"))
        data = bundle_bytes(rows, {"docs/a.pdf": b"redacted copy", "docs/b.pdf": b"bb"}, agencies_by_id={"ca-cedar-police": {"name": "Cedar, Police"}})
        text = self.members(data)["manifest.csv"].decode("utf-8")
        self.assertFalse(text.startswith("\ufeff"))
        self.assertIn("\r\n", text)
        table = list(csv.reader(io.StringIO(text, newline="")))
        self.assertEqual(table[0], ["sha256", "title", "agency", "received_at", "public_path", "bytes"])
        self.assertEqual(len(table), 1 + len(rows))
        by_title = {row[1]: row for row in table[1:]}
        self.assertEqual(by_title["'=SUM(1,2)"], ["01" * 6, "'=SUM(1,2)", "unknown-agency", "2026-01-01T00:00:00+00:00", "/docs/a.pdf", "13"])
        self.assertEqual(by_title['Quote " comma, line\nbreak é'][2:], ["Cedar, Police", "2026-01-02T00:00:00+00:00", "/docs/a.pdf", "13"])
        self.assertEqual(by_title["'@cmd"][5], "2")
        for prefix in "+-\t\r":
            self.assertIn("'" + prefix + "x", by_title)
        self.assertEqual([row[4] for row in table[1:]], sorted(row[4] for row in table[1:]))
        self.assertNotIn("private note", text)
        self.assertNotIn("cedar-police-records", text)  # fragment
        self.assertNotIn("01" * 32, text)  # never the full hash
        self.assertEqual(sorted(self.members(data)), ["files/docs/a.pdf", "files/docs/b.pdf", "manifest.csv"])  # one copy per file


class ArchiveTests(Fixture):
    def test_archive_reproducible_metadata_and_order(self):
        rows = [(entry(2, None, "Beta"), "docs/b.pdf"), (entry(1, None, "Alpha"), "docs/a.pdf")]
        payloads = {"docs/b.pdf": b"BBB", "docs/a.pdf": b"AAAA"}
        first = bundle_bytes(rows, payloads)
        self.assertEqual(bundle_bytes(rows[::-1], dict(reversed(list(payloads.items())))), first)
        with zipfile.ZipFile(io.BytesIO(first)) as archive:
            infos = archive.infolist()
            self.assertEqual([i.filename for i in infos], ["files/docs/a.pdf", "files/docs/b.pdf", "manifest.csv"])
            for info in infos:
                self.assertEqual((info.compress_type, info.date_time, info.external_attr, info.create_system),
                                 (zipfile.ZIP_STORED, (1980, 1, 1, 0, 0, 0), 0o100644 << 16, 3))
                self.assertEqual((info.extra, info.comment, info.flag_bits, info.file_size, info.compress_size),
                                 (b"", b"", 0, info.file_size, info.file_size))
            self.assertEqual(archive.comment, b"")
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.read("files/docs/a.pdf"), b"AAAA")
        # two real builds, with file times and modes changed between them, write the same bytes
        build_site(self.root)
        before = self.zip_bytes()
        for path in (self.content / "public-assets").rglob("*.pdf"):
            os.utime(path, (1_000_000_000, 1_000_000_000))
            path.chmod(0o600)
        self.add_sources(*json.loads((self.content / "sources.json").read_text(encoding="utf-8"))[::-1], replace=True)
        build_site(self.root)
        self.assertEqual(self.zip_bytes(), before)

    def test_exact_final_size_limit_and_skip(self):
        self.assertEqual(source_bundle.MAX_BYTES, 200 * 1024 * 1024)
        build_site(self.root)
        size = len(self.zip_bytes())
        members = self.members()
        measured = 22 + sum(76 + 2 * len(name) + len(data) for name, data in members.items())  # 30+n local, 46+n central
        self.assertEqual(measured, size)
        rows = [(entry(1, None), "docs/a.pdf")]
        payload = {"docs/a.pdf": b"x" * 100}
        exact = len(bundle_bytes(rows, payload))
        self.assertEqual(len(bundle_bytes(rows, payload, max_bytes=exact)), exact)  # equal to the cap is accepted
        self.assertIsNone(bundle_bytes(rows, payload, max_bytes=exact - 1))  # one byte over is skipped
        with mock.patch.object(source_bundle, "MAX_BYTES", size - 1):
            self.assertEqual(run_build(self.root).count(NOTE_OVERSIZE), 1)
        self.assertFalse((self.public / "data" / "sources.zip").exists())  # yesterday's archive is gone
        self.assertNotIn("sources.zip", (self.public / "sources.html").read_text(encoding="utf-8"))
        self.assertNotIn("sources.zip", (self.public / "data.html").read_text(encoding="utf-8"))
        self.assertNotIn("sources.zip", (self.public / "data" / "datapackage.json").read_text(encoding="utf-8"))
        with mock.patch.object(source_bundle, "MAX_BYTES", size):
            self.assertEqual(run_build(self.root), "")
        self.assertEqual(len(self.zip_bytes()), size)
        self.add_sources(entry(9, "https://example.invalid/x.pdf"), replace=True)  # nothing is local any more
        self.assertEqual(run_build(self.root).count(NOTE_EMPTY), 1)
        self.assertFalse((self.public / "data" / "sources.zip").exists())
        self.assertEqual(verify_data_package(self.public), [])


class StagingTests(Fixture):
    def snapshot(self):
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}

    def test_staging_failure_and_preview_are_safe(self):
        build_site(self.root)
        before = self.snapshot()
        (self.content / "public-assets" / "docs" / "a.pdf").write_bytes(pdf(b"<< /Length 1 >>"))  # a changed source
        report = preview_site(self.root)
        self.assertEqual(self.snapshot(), before)  # preview writes nothing
        self.assertIn("data/sources.zip", [c["path"] for c in report["changed"]])
        self.add_sources(entry(9, "https://example.invalid/x.pdf"), replace=True)
        report = preview_site(self.root)
        self.assertIn("data/sources.zip", [r["path"] for r in report["removed"]])
        self.assertEqual(self.snapshot(), before)
        # a copy that changes after planning is refused and the old tree stays
        self.add_sources(entry(1, BASE + "/docs/a.pdf", "Alpha"), replace=True)
        real = site_module._copy

        def racing(source, target, written, limit):
            real(source, target, written, limit)
            if target.name == "a.pdf":
                target.write_bytes(target.read_bytes() + b"\n%raced")
        with mock.patch.object(site_module, "_copy", racing), self.assertRaisesRegex(
                ContentError, "sources.zip: public copy changed during build"):
            build_site(self.root)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".public.")], [])
        # a vanished file between planning and reading is the same refusal
        with mock.patch.object(source_bundle, "read_copy", side_effect=ContentError(source_bundle.CHANGED)), \
                self.assertRaisesRegex(ContentError, "public copy changed during build"):
            build_site(self.root)
        self.assertEqual(self.snapshot(), before)


class CheckTests(Fixture):
    def test_check_refuses_stale_or_tampered_bundle_before_write(self):
        self.assertEqual(run_build(self.root, check=True), "")  # no public/: the first build is allowed
        self.assertEqual(run_build(self.root, check=True), "")  # and is stable
        archive = self.public / "data" / "sources.zip"
        good = archive.read_bytes()

        def refused(message="differs from rebuilt public-source bundle|data catalogue check failed"):  # either gate may speak first
            snapshot = {p: p.read_bytes() for p in self.public.rglob("*") if p.is_file() and not p.is_symlink()}
            with self.assertRaisesRegex(ContentError, message):
                run_build(self.root, check=True)
            self.assertEqual({p: p.read_bytes() for p in self.public.rglob("*") if p.is_file() and not p.is_symlink()}, snapshot)  # no repair

        archive.write_bytes(good[:-1])
        refused()
        archive.write_bytes(good + b"\0")
        refused()
        archive.write_bytes(good.replace(b"AAAA", b"BBBB") if b"AAAA" in good else bytes([good[0] ^ 1]) + good[1:])
        refused()
        archive.unlink()
        refused()
        archive.symlink_to(self.root / "elsewhere.zip")
        refused()
        archive.unlink()
        archive.write_bytes(good)
        self.assertEqual(run_build(self.root, check=True), "")
        (self.content / "public-assets" / "docs" / "a.pdf").write_bytes(pdf(b"<< /Length 2 >>"))  # a changed input
        refused()
        run_build(self.root)  # an ordinary build repairs it
        self.assertEqual(run_build(self.root, check=True), "")
        self.add_sources(entry(9, "https://example.invalid/x.pdf"), replace=True)  # old archive now expected absent
        refused()
        self.assertTrue(archive.exists())
        self.assertEqual(run_build(self.root).count(NOTE_EMPTY), 1)  # the ordinary build drops the stale archive
        self.assertFalse(archive.exists())
        self.assertEqual(run_build(self.root, check=True).count(NOTE_EMPTY), 1)

    def test_check_source_bundle_function(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-check-") as folder:
            public = Path(folder) / "public"
            check_source_bundle(public, b"x")  # no public/: first build
            public.mkdir()
            check_source_bundle(public, None)
            with self.assertRaisesRegex(ContentError, "differs from rebuilt"):
                check_source_bundle(public, b"x")
            (public / "data").mkdir()
            (public / "data" / "sources.zip").write_bytes(b"x")
            check_source_bundle(public, b"x")
            with self.assertRaisesRegex(ContentError, "differs from rebuilt"):
                check_source_bundle(public, None)


class PublicOutputTests(Fixture):
    def test_bundle_labels_catalogue_and_privacy(self):
        self.add_sources(entry(3, "https://example.invalid/docs/a.pdf", "External"))
        (self.content / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": {
            "sources.bundle": "Get every copy", "sources.bundle_scope": "Scope & \"more\""}}), encoding="utf-8")
        self.content.joinpath("site.json").write_text(
            json.dumps({**json.loads(self.content.joinpath("site.json").read_text(encoding="utf-8")), "license": {"id": "CC-BY-4.0"}}),
            encoding="utf-8")
        build_site(self.root)
        data = self.zip_bytes()
        digest, size = hashlib.sha256(data).hexdigest(), len(data)
        page = (self.public / "sources.html").read_text(encoding="utf-8")
        self.assertIn('<a href="/data/sources.zip" download>Get every copy</a>', page)
        self.assertIn(f"{size} bytes, SHA-256 {digest}", page)
        self.assertIn("Scope &amp; &quot;more&quot;", page)
        self.assertEqual(len(re.findall(r"[0-9a-f]{64}", digest)), 1)
        self.assertNotIn("External", self.members()["manifest.csv"].decode("utf-8"))
        rows = re.findall(r'<tr data-path="data/sources.zip" data-bytes="(\d+)" data-sha256="([0-9a-f]{64})">', (self.public / "data.html").read_text(encoding="utf-8"))
        self.assertEqual(rows, [(str(size), digest)])
        resource = next(r for r in json.loads((self.public / "data" / "datapackage.json").read_text(encoding="utf-8"))["resources"]
                        if r["name"] == "sources-zip")
        self.assertEqual((resource["path"], resource["mediatype"], resource["bytes"], resource["hash"], resource["schema"], resource["licenses"]),
                         ("sources.zip", "application/zip", size, "sha256:" + digest, None, []))  # never the campaign CC licence
        self.assertEqual(verify_data_package(self.public), [])
        for name in ("data/sources.json", "data/sources.csv", "data/findings.json"):  # the archive digest is not in the source JSON
            self.assertNotIn(digest, (self.public / name).read_text(encoding="utf-8"))
        # the catalogue gate still refuses a tampered descriptor
        descriptor = self.public / "data" / "datapackage.json"
        document = json.loads(descriptor.read_text(encoding="utf-8"))
        for item in document["resources"]:
            if item["name"] == "sources-zip":
                item["licenses"] = [{"name": "CC-BY-4.0", "path": "https://creativecommons.org/licenses/by/4.0/"}]
        descriptor.write_text(json.dumps(document), encoding="utf-8")
        self.assertTrue(verify_data_package(self.public))

    def test_spanish_labels_and_no_link_without_the_file(self):
        shutil.copy(ES_LABELS, self.content / "labels.json")
        build_site(self.root)
        page = (self.public / "sources.html").read_text(encoding="utf-8")
        self.assertIn("Descargar todas las copias públicas de las fuentes (ZIP)", page)
        self.assertIn("Solo se incluyen las copias públicas alojadas por esta compilación", page)
        self.add_sources(entry(9, "https://example.invalid/x.pdf"), replace=True)
        build_site(self.root)
        for name in ("sources.html", "data.html", "data/datapackage.json", "sitemap.xml"):
            self.assertNotIn("sources.zip", (self.public / name).read_text(encoding="utf-8"), name)


if __name__ == "__main__":
    unittest.main()
