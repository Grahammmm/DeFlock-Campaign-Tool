"""DF2037-06: `ingest --zip ARCHIVE --source-id ID` stores the archive and each regular member by hash.

Members are read in memory, never extracted to disk; each gets a receipt `<archive>/<member path>` and a
ledger `contains` row. Every archive is built here from synthetic bytes. Run from the repository root:
python3 -B -m unittest tests.test_ingest_zip -v
"""

import bz2
import hashlib
import io
import json
import lzma
import os
from pathlib import Path
import resource
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
import zlib

REPO_ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 50 * 1024 * 1024


def deflate(data):
    packer = zlib.compressobj(6, zlib.DEFLATED, -zlib.MAX_WBITS)
    return packer.compress(data) + packer.flush()


def zip_lzma(data):
    """A zip LZMA (method 14) member body: version, properties size 5, lc/lp/pb byte, dictionary, raw stream."""
    packer = lzma.LZMACompressor(lzma.FORMAT_RAW, filters=[{"id": lzma.FILTER_LZMA1, "dict_size": 1 << 20,
                                                            "lc": 3, "lp": 0, "pb": 2}])
    return struct.pack("<BBHBI", 9, 20, 5, (2 * 5 + 0) * 9 + 3, 1 << 20) + packer.compress(data) + packer.flush()


def build_zip(entries):
    """Zip bytes from (name, data, options); options may set method, flags, mode, raw, size and crc
    so headers can say what a hostile archive would say. `central_version` sets the central extraction
    version. Local-only options (local_name, local_flags,
    local_method, local_crc, local_csize, local_usize, extra, local_nlen, local_elen) change only the local
    header; `encoding` is the name codec."""
    body, central = b"", b""
    for name, data, opt in entries:
        encoded = name.encode(opt.get("encoding", "utf-8"))
        method = opt.get("method", 8)
        flags = opt.get("flags", 0x800)
        raw = opt.get("raw", deflate(data) if method == 8 else data)
        crc, usize = opt.get("crc", zlib.crc32(data)), opt.get("size", len(data))
        common = struct.pack("<HHHHHIII", opt.get("central_version", 20), flags, method, 0, 0x21, crc, len(raw), usize)
        local_name, extra = opt.get("local_name", encoded), opt.get("extra", b"")
        local = struct.pack("<HHHHHIII", 20, opt.get("local_flags", flags), opt.get("local_method", method), 0, 0x21,
                            opt.get("local_crc", crc), opt.get("local_csize", len(raw)), opt.get("local_usize", usize))
        offset = len(body)
        body += (b"PK\x03\x04" + local + struct.pack("<HH", opt.get("local_nlen", len(local_name)),
                                                     opt.get("local_elen", len(extra))) + local_name + extra + raw)
        central += (b"PK\x01\x02" + struct.pack("<H", 0x0314) + common
                    + struct.pack("<HHHHHII", len(encoded), 0, 0, 0, 0, opt.get("mode", 0o100644) << 16,
                                  offset)
                    + encoded)
    end = b"PK\x05\x06" + struct.pack("<HHHHIIH", 0, 0, len(entries), len(entries), len(central), len(body), 0)
    return body + central + end


def limit_memory():
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))


class IngestZipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-ingest-zip-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root, self.work, self.inbox = self.base / "campaign", self.base / "work", self.base / "inbox"
        self.work.mkdir()
        self.inbox.mkdir()
        self.cli("init", "--name", "Synthetic Campaign", "--county", "Synthetic County", "--state", "ca")

    def cli(self, command, *arguments, returncode=0, preexec_fn=None):
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO_ROOT)}
        result = subprocess.run(
            [sys.executable, "-B", "-m", "campaign_tool", command, "--directory", str(self.root), *map(str, arguments)],
            cwd=self.work, env=env, capture_output=True, text=True, timeout=300, preexec_fn=preexec_fn)
        self.assertEqual(result.returncode, returncode, f"stdout={result.stdout!r}; stderr={result.stderr!r}")
        self.assertNotIn("Traceback", result.stderr)
        return result

    def ingest(self, archive, source_id="synthetic-1", dry=False, **kwargs):
        result = self.cli("ingest", "--zip", archive, "--source-id", source_id, *(["--dry-run"] if dry else []),
                          **kwargs)
        if dry:
            body, last = result.stdout.rstrip("\n").rsplit("\n", 1)
            self.assertEqual(last, "Dry run: nothing was written.")
            return json.loads(body)
        return json.loads(result.stdout)

    def archive(self, entries, name="a.zip"):
        path = self.inbox / name
        path.write_bytes(build_zip(entries))
        return path

    def files(self):
        return {str(p.relative_to(self.base)): p.read_bytes() for p in self.base.rglob("*") if p.is_file()}

    def rows(self, query):
        db = sqlite3.connect(self.root / "private" / "ledger.sqlite")
        try:
            return db.execute(query).fetchall()
        finally:
            db.close()

    def skipped(self, report):
        return {item["path"]: item["code"] for item in report["skipped"]}

    def test_two_members_three_objects_receipts_two_contains_rows_and_nothing_else_written(self):
        x, y = b"%PDF-1.4 synthetic x\n", b"synthetic y\n"
        path = self.archive([("x.pdf", x, {}), ("sub/y.txt", y, {"method": 0})])
        before = self.files()
        report = self.ingest(path)
        whole = hashlib.sha256(path.read_bytes()).hexdigest()
        sx, sy = hashlib.sha256(x).hexdigest(), hashlib.sha256(y).hexdigest()
        self.assertEqual(report["archive"]["sha256"], whole)
        self.assertEqual(report["archive"]["outcome"], "stored")
        self.assertEqual(report["members"], [{"path": "x.pdf", "sha256": sx, "outcome": "stored"},
                                             {"path": "sub/y.txt", "sha256": sy, "outcome": "stored"}])
        self.assertEqual((report["skipped"], report["stored"], report["present"]), ([], 3, 0))
        created = {k: v for k, v in self.files().items() if k not in before}
        objects = "campaign/private/objects/"
        self.assertEqual(set(created), {"campaign/private/ledger.sqlite", objects + whole, objects + sx, objects + sy})
        self.assertEqual((created[objects + sx], created[objects + sy]), (x, y))
        self.assertEqual(sorted(r[0] for r in self.rows("SELECT original_name FROM receipts")),
                         ["a.zip", "a.zip/sub/y.txt", "a.zip/x.pdf"])
        self.assertEqual(sorted(self.rows("SELECT * FROM contains")), sorted([(whole, sx, "x.pdf"), (whole, sy, "sub/y.txt")]))
        self.cli("status", "--check")

    def test_reingest_same_source_all_present_and_new_source_adds_receipts_only(self):
        path = self.archive([("x.pdf", b"x", {}), ("sub/y.txt", b"y", {})])
        self.ingest(path)
        counts = "SELECT (SELECT count(*) FROM objects), (SELECT count(*) FROM receipts), (SELECT count(*) FROM contains)"
        self.assertEqual(self.rows(counts), [(3, 3, 2)])
        again = self.ingest(path)
        self.assertEqual([again["archive"]["outcome"]] + [m["outcome"] for m in again["members"]], ["present"] * 3)
        self.assertEqual((again["stored"], again["present"]), (0, 3))
        self.assertEqual(self.rows(counts), [(3, 3, 2)])
        other = self.ingest(path, source_id="synthetic-2")
        self.assertEqual((other["stored"], other["present"]), (0, 3))
        self.assertEqual(self.rows(counts), [(3, 6, 2)])
        self.assertEqual(self.rows("SELECT count(*) FROM receipts WHERE source_id = 'synthetic-2'"), [(3,)])

    def test_traversal_absolute_drive_nul_and_long_names_skipped_unsafe_nothing_outside_objects(self):
        names = ["../evil.txt", "/etc/x", "C:\\x", "a\\..\\..\\b", "./dot.txt", "a//b.txt", "nul\0.txt", "n" * 250]
        path = self.archive([(n, b"evil " + n.encode(), {}) for n in names] + [("ok.txt", b"ok", {})])
        before = self.files()
        report = self.ingest(path)
        self.assertEqual(sorted(self.skipped(report).values()), ["unsafe_name"] * len(names))
        self.assertEqual([m["path"] for m in report["members"]], ["ok.txt"])
        created = set(self.files()) - set(before)
        self.assertEqual({c for c in created if not c.startswith("campaign/private/objects/")},
                         {"campaign/private/ledger.sqlite"})
        self.assertEqual(len(created), 3)
        self.assertFalse((self.base / "evil.txt").exists() or (self.work / "evil.txt").exists())

    def test_encrypted_symlink_directory_and_duplicate_name_each_skipped_by_code(self):
        first = b"first x\n"
        path = self.archive([("sub/", b"", {"mode": 0o040755}), ("locked.pdf", b"secret", {"flags": 0x801}),
                             ("link", b"/etc/passwd", {"mode": 0o120777}), ("x.pdf", first, {}),
                             ("x.pdf", b"second x\n", {})])
        report = self.ingest(path)
        self.assertEqual(report["skipped"], [{"path": "sub/", "code": "directory"},
                                             {"path": "locked.pdf", "code": "encrypted"},
                                             {"path": "link", "code": "symlink"},
                                             {"path": "x.pdf", "code": "duplicate_name"}])
        self.assertEqual(report["members"], [{"path": "x.pdf", "sha256": hashlib.sha256(first).hexdigest(),
                                              "outcome": "stored"}])
        self.assertEqual(self.rows("SELECT count(*) FROM objects"), [(2,)])

    def test_lying_header_capped_read_bombs_and_corrupt_members_skipped_archive_still_stored(self):
        packer, raw = zlib.compressobj(1, zlib.DEFLATED, -zlib.MAX_WBITS), b""
        chunk = bytes(1024 * 1024)
        for _ in range(1024):  # 1 GiB of zeros inflated from about 1 MiB; the header claims 10 bytes
            raw += packer.compress(chunk)
        raw += packer.flush()
        small = deflate(b"z" * 200_000)
        path = self.archive([
            ("liar.bin", b"", {"raw": raw, "size": 10, "crc": 0}),
            ("bomb.bin", bytes(1_000_000), {}),
            ("quiet-bomb.bin", b"", {"raw": small, "size": 10, "crc": 0}),
            ("bad-crc.txt", b"synthetic", {"crc": 1}),
            ("short.txt", b"synthetic", {"size": 4}),
            ("ok.txt", b"ok", {})])
        report = self.ingest(path, preexec_fn=limit_memory)  # 512 MiB address space: the 1 GiB is never held
        self.assertEqual(self.skipped(report), {"liar.bin": "too_large", "bomb.bin": "ratio",
                                                "quiet-bomb.bin": "ratio", "bad-crc.txt": "unreadable",
                                                "short.txt": "unreadable"})
        self.assertEqual(report["archive"]["outcome"], "stored")
        self.assertEqual([m["path"] for m in report["members"]], ["ok.txt"])
        self.assertTrue((self.root / "private" / "objects" / report["archive"]["sha256"]).is_file())

    def test_header_over_50_mib_skipped_too_large_without_reading(self):
        path = self.archive([("huge.bin", b"tiny", {"size": MAX_BYTES + 1}), ("ok.txt", b"ok", {})])
        report = self.ingest(path, preexec_fn=limit_memory)
        self.assertEqual(self.skipped(report), {"huge.bin": "too_large"})
        self.assertEqual([m["path"] for m in report["members"]], ["ok.txt"])

    def test_bzip2_and_lzma_measured_on_the_real_stream_not_the_header(self):
        text = b"synthetic record line\n" * 50
        prefix = text[:10]  # a forged header: size 10 and the CRC of exactly those first 10 bytes
        forged = {"size": 10, "crc": zlib.crc32(prefix)}
        path = self.archive([
            ("ok-bz.txt", text, {"method": 12, "raw": bz2.compress(text)}),
            ("ok-xz.txt", text, {"method": 14, "raw": zip_lzma(text), "flags": 0x802}),
            ("short-bz.txt", b"", {"method": 12, "raw": bz2.compress(text), **forged}),
            ("short-xz.txt", b"", {"method": 14, "raw": zip_lzma(text), "flags": 0x802, **forged}),
            ("bomb-bz.bin", b"", {"method": 12, "raw": bz2.compress(bytes(2_000_000)), "size": 10,
                                  "crc": zlib.crc32(bytes(10))}),
            ("cut-xz.txt", text, {"method": 14, "raw": zip_lzma(text)[:-8], "flags": 0x802})])
        report = self.ingest(path)
        self.assertEqual(self.skipped(report), {"short-bz.txt": "unreadable", "short-xz.txt": "unreadable",
                                                "bomb-bz.bin": "ratio", "cut-xz.txt": "unreadable"})
        digest = hashlib.sha256(text).hexdigest()
        self.assertEqual(report["members"], [{"path": "ok-bz.txt", "sha256": digest, "outcome": "stored"},
                                             {"path": "ok-xz.txt", "sha256": digest, "outcome": "present"}])
        self.assertEqual((self.root / "private" / "objects" / digest).read_bytes(), text)
        self.assertFalse((self.root / "private" / "objects" / hashlib.sha256(prefix).hexdigest()).exists())

    def test_2001_entries_refused_before_any_write_and_non_zip_stopped(self):
        many = self.archive([(f"f{i}.txt", b"%d" % i, {"method": 0}) for i in range(2001)])
        before = self.files()
        result = self.cli("ingest", "--zip", many, "--source-id", "synthetic-1", returncode=1)
        self.assertIn("Stopped: a.zip: more than 2000 entries; nothing was imported", result.stderr)
        self.assertEqual(self.files(), before)
        exactly = self.archive([(f"f{i}.txt", b"%d" % i, {"method": 0}) for i in range(2000)], name="ok.zip")
        self.assertEqual(len(self.ingest(exactly, dry=True)["members"]), 2000)  # the boundary itself is accepted
        fake = self.inbox / "fake.zip"
        fake.write_bytes(b"%PDF-1.4 not a zip\n")
        before = self.files()
        for dry in ([], ["--dry-run"]):
            result = self.cli("ingest", "--zip", fake, "--source-id", "synthetic-1", *dry, returncode=1)
            self.assertIn("Stopped: not a zip archive", result.stderr)
        link = self.inbox / "link.zip"
        link.symlink_to(exactly)
        result = self.cli("ingest", "--zip", link, "--source-id", "synthetic-1", returncode=1)
        self.assertIn("Stopped: --zip must be a regular, non-symlink file", result.stderr)
        self.assertEqual({k for k in self.files() if k != "inbox/link.zip"}, set(before))

    def test_nested_archive_stored_as_member_never_opened(self):
        inner = build_zip([("inner-secret.txt", b"inner bytes", {})])
        path = self.archive([("inner.zip", inner, {}), ("letter.txt", b"letter", {})])
        result = self.cli("ingest", "--zip", path, "--source-id", "synthetic-1")
        report = json.loads(result.stdout)
        self.assertEqual([m["path"] for m in report["members"]], ["inner.zip", "letter.txt"])
        self.assertNotIn("inner-secret", result.stdout)
        digest = hashlib.sha256(inner).hexdigest()
        self.assertEqual((self.root / "private" / "objects" / digest).read_bytes(), inner)
        self.assertEqual(self.rows("SELECT count(*) FROM objects"), [(3,)])
        self.assertEqual(self.rows("SELECT count(*) FROM contains WHERE container_sha256 = '%s'" % digest), [(0,)])

    def test_dry_run_fresh_campaign_and_old_ledger_without_contains_write_nothing(self):
        x = b"synthetic x\n"
        path = self.archive([("x.pdf", x, {}), ("y.txt", b"y", {}), ("../evil", b"e", {})])
        # Model a legacy campaign without private state, including the init identity.
        (self.root / "private" / "campaign-id.json").unlink()
        (self.root / "private").rmdir()
        before = self.files()
        report = self.ingest(path, dry=True)
        self.assertEqual(self.files(), before)
        self.assertFalse((self.root / "private" / "ledger.sqlite").exists())
        self.assertEqual((report["would_store"], report["present"], report["dry_run"]), (3, 0, True))
        self.assertEqual(self.skipped(report), {"../evil": "unsafe_name"})
        (self.root / "private").mkdir(mode=0o700)
        old = sqlite3.connect(self.root / "private" / "ledger.sqlite")
        old.executescript("CREATE TABLE objects (sha256 TEXT PRIMARY KEY, byte_count INTEGER NOT NULL, "
                          "stored_at TEXT NOT NULL); CREATE TABLE receipts (receipt_id TEXT PRIMARY KEY, "
                          "object_sha256 TEXT NOT NULL, source_id TEXT NOT NULL, received_at TEXT NOT NULL, "
                          "original_name TEXT NOT NULL);")
        old.execute("INSERT INTO objects VALUES (?, ?, 'synthetic')", (hashlib.sha256(x).hexdigest(), len(x)))
        old.commit()
        old.close()
        before = self.files()
        report = self.ingest(path, dry=True)
        self.assertEqual(self.files(), before, "dry run changed the old ledger or wrote a file")
        self.assertEqual([m["outcome"] for m in report["members"]], ["present", "would_store"])
        self.assertEqual((report["archive"]["outcome"], report["would_store"], report["present"]), ("would_store", 2, 1))
        self.assertEqual(self.rows("SELECT name FROM sqlite_master WHERE name = 'contains'"), [])

    def unreadable(self, report):
        return {p for p, code in self.skipped(report).items() if code == "unreadable"}

    def test_local_identity_mismatch_skips_only_member(self):
        bad = {"local-name.txt": {"local_name": b"other.txt"},
               "local-case.txt": {"local_name": b"LOCAL-CASE.TXT"},
               "local-utf8.txt": {"local_name": b"local-utf8.tx\xff"},
               "local-method.txt": {"local_method": 0},
               "local-flags.txt": {"local_flags": 0x802},
               "local-encrypted.txt": {"local_flags": 0x801}}
        entries = [(n, b"synthetic " + n.encode(), o) for n, o in bad.items()] + [("ok.txt", b"ok", {})]
        path = self.archive(entries)
        report = self.ingest(path)
        self.assertEqual(self.skipped(report), {n: "unreadable" for n in bad})
        self.assertEqual([m["path"] for m in report["members"]], ["ok.txt"])
        self.assertEqual(self.rows("SELECT count(*) FROM objects"), [(2,)])
        self.assertEqual(self.rows("SELECT count(*) FROM contains"), [(1,)])
        self.assertEqual(report["archive"]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual((self.root / "private" / "objects" / report["archive"]["sha256"]).read_bytes(), path.read_bytes())

    def test_local_crc_sizes_and_zip64_extra(self):
        text = b"synthetic record\n" * 20
        def zip64(*values):
            body = b"".join(struct.pack("<Q", v) for v in values)
            return struct.pack("<HH", 1, len(body)) + body
        sentinel = {"local_usize": 0xFFFFFFFF, "local_csize": 0xFFFFFFFF}
        packed = len(deflate(text))
        bad = {"crc.txt": {"local_crc": 5}, "usize.txt": {"local_usize": 7}, "csize.txt": {"local_csize": 7},
               "no-extra.txt": dict(sentinel),
               "short-extra.txt": {**sentinel, "extra": zip64(len(text))},
               "wrong-extra.txt": {**sentinel, "extra": zip64(len(text) + 1, packed)},
               "cut-tlv.txt": {"extra": b"\x01\x00\x10"},
               "overrun-tlv.txt": {"extra": struct.pack("<HH", 0x9999, 40) + b"x"},
               "elen-past-end.txt": {"local_elen": 60000}}
        good = {"plain.txt": {}, "zip64.txt": {**sentinel, "extra": zip64(len(text), packed)},
                "other-tlv.txt": {"extra": struct.pack("<HH", 0x9999, 2) + b"xx"}}
        entries = [(n, text, o) for n, o in {**bad, **good}.items()]
        report = self.ingest(self.archive(entries))
        self.assertEqual(self.skipped(report), {n: "unreadable" for n in bad})
        digest = hashlib.sha256(text).hexdigest()
        self.assertEqual([(m["path"], m["sha256"]) for m in report["members"]], [(n, digest) for n in good])
        self.assertEqual((self.root / "private" / "objects" / digest).read_bytes(), text)

    def test_force_zip64_small_members_all_methods(self):
        text = b"synthetic forced zip64 line\n" * 30
        names = {zipfile.ZIP_STORED: "s.txt", zipfile.ZIP_DEFLATED: "d.txt", zipfile.ZIP_BZIP2: "b.txt",
                 zipfile.ZIP_LZMA: "l.txt"}
        buffer = io.BytesIO()
        for method, name in names.items():
            with zipfile.ZipFile(buffer, "a", compression=method) as archive:
                with archive.open(name, "w", force_zip64=True) as member:
                    member.write(text)
        path = self.inbox / "z64.zip"
        path.write_bytes(buffer.getvalue())
        report = self.ingest(path)
        self.assertEqual((report["skipped"], [m["path"] for m in report["members"]]), ([], list(names.values())))
        self.assertEqual({m["sha256"] for m in report["members"]}, {hashlib.sha256(text).hexdigest()})

    def test_streaming_descriptor_and_unicode_compatibility(self):
        class Unseekable:
            def __init__(self):
                self.parts = []
            def write(self, data):
                self.parts.append(bytes(data))
                return len(data)
            def flush(self):
                pass
        text = b"synthetic streamed\n" * 10
        stream = Unseekable()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("streamed.txt", text)
            archive.writestr("caf\u00e9.txt", text)
        data = b"".join(stream.parts)
        self.assertEqual(struct.unpack_from("<H", data, 6)[0] & 0x8, 0x8)
        path = self.inbox / "stream.zip"
        path.write_bytes(data)
        report = self.ingest(path)
        self.assertEqual(([m["path"] for m in report["members"]], report["skipped"]), (["streamed.txt", "caf\u00e9.txt"], []))
        legacy = self.archive([("caf\u00e9-cp437.txt", b"cp437 body", {"flags": 0, "encoding": "cp437"}),
                               ("utf8-\u00e9.txt", b"utf8 body", {}),
                               ("placeholder.txt", b"ph", {"flags": 0x808, "local_crc": 0, "local_csize": 0, "local_usize": 0}),
                               ("true-values.txt", b"tv", {"flags": 0x808}),
                               ("wrong-crc.txt", b"wc", {"flags": 0x808, "local_crc": 99}),
                               ("wrong-size.txt", b"ws", {"flags": 0x808, "local_usize": 99})], name="legacy.zip")
        report = self.ingest(legacy, source_id="synthetic-2")
        self.assertEqual(self.skipped(report), {"wrong-crc.txt": "unreadable", "wrong-size.txt": "unreadable"})
        self.assertEqual([m["path"] for m in report["members"]],
                         ["caf\u00e9-cp437.txt", "utf8-\u00e9.txt", "placeholder.txt", "true-values.txt"])
        prefixed = self.inbox / "prefixed.zip"
        prefixed.write_bytes(b"#!/bin/sh\n" * 7 + build_zip([("p.txt", b"prefixed", {}), ("q.txt", b"q", {"method": 0})]))
        report = self.ingest(prefixed, source_id="synthetic-3")
        self.assertEqual(([m["path"] for m in report["members"]], report["skipped"]), (["p.txt", "q.txt"], []))

    def test_member_spans_do_not_cross_record_boundaries(self):
        path = self.archive([("cross-next.txt", b"x" * 40, {"local_elen": 300}),
                             ("ok.txt", b"ok body", {}),
                             ("alias-1.txt", b"one", {}),
                             ("alias-2.txt", b"two", {}),
                             ("far.txt", b"far", {}),
                             ("cross-central.txt", b"tail", {"local_nlen": 40, "local_elen": 4000})])
        data = bytearray(path.read_bytes())
        # alias-2 points at alias-1's local header: find that header by its name
        one = data.index(b"alias-1.txt") - 30
        central_two = data.index(b"alias-2.txt", data.index(b"PK\x01\x02")) - 46 + 42
        struct.pack_into("<I", data, central_two, one)
        central_far = data.index(b"far.txt", data.index(b"PK\x01\x02")) - 46 + 42
        struct.pack_into("<I", data, central_far, 10_000_000)
        path.write_bytes(bytes(data))
        report = self.ingest(path)
        self.assertEqual(self.skipped(report), {n: "unreadable" for n in
                         ("cross-next.txt", "alias-1.txt", "alias-2.txt", "far.txt", "cross-central.txt")})
        self.assertEqual([m["path"] for m in report["members"]], ["ok.txt"])
        self.assertEqual(self.rows("SELECT count(*) FROM objects"), [(2,)])

    def test_header_refusal_dry_run_and_replay(self):
        path = self.archive([("bad.txt", b"bad", {"local_name": b"evil.txt"}), ("../x", b"x", {}),
                             ("ok.txt", b"ok", {})])
        before = self.files()
        dry = self.ingest(path, dry=True)
        self.assertEqual(self.files(), before)
        self.assertEqual(self.skipped(dry), {"bad.txt": "unreadable", "../x": "unsafe_name"})
        real = self.ingest(path)
        self.assertEqual(self.skipped(real), self.skipped(dry))
        self.assertEqual([m["path"] for m in real["members"]], [m["path"] for m in dry["members"]])
        self.assertEqual(set(real), {"archive", "members", "skipped", "stored", "present"})
        again = self.ingest(path)
        self.assertEqual((again["stored"], again["present"], again["skipped"]), (0, 2, real["skipped"]))
        self.assertEqual(self.rows("SELECT count(*) FROM contains"), [(1,)])
        self.assertEqual(self.rows("SELECT count(*) FROM receipts"), [(2,)])
        self.assertEqual(real["archive"]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.cli("status", "--check")

    def test_unsupported_central_extract_version_refuses_without_traceback(self):
        import campaign_tool.ingest_zip as module
        for version in (64, 99, 255):
            data = build_zip([("a.txt", b"a", {"central_version": version})])
            with self.assertRaises(ValueError) as caught:
                module._open_archive(data, "a.zip")
            self.assertEqual(str(caught.exception), "not a zip archive")
            self.assertTrue(caught.exception.__suppress_context__)
            path = self.inbox / f"v{version}.zip"
            path.write_bytes(data)
            before = self.files()
            for dry in ([], ["--dry-run"]):
                result = self.cli("ingest", "--zip", path, "--source-id", "synthetic-1", *dry, returncode=1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr.strip(), "Stopped: not a zip archive")
            self.assertEqual(self.files(), before)
            self.assertFalse((self.root / "private" / "ledger.sqlite").exists())

    def test_unsupported_later_entry_refuses_entire_archive_before_ledger(self):
        path = self.archive([("good.txt", b"good", {}), ("later.txt", b"later", {"central_version": 99})])
        before = self.files()
        for dry in ([], ["--dry-run"]):
            result = self.cli("ingest", "--zip", path, "--source-id", "synthetic-1", *dry, returncode=1)
            self.assertIn("Stopped: not a zip archive", result.stderr)
        self.assertEqual(self.files(), before)
        self.assertFalse((self.root / "private" / "ledger.sqlite").exists())
        self.ingest(self.archive([("ok.txt", b"ok", {})], name="ok.zip"))
        populated = self.files()
        result = self.cli("ingest", "--zip", path, "--source-id", "synthetic-1", returncode=1)
        self.assertIn("Stopped: not a zip archive", result.stderr)
        self.assertEqual(self.files(), populated)

    def test_constructor_refusal_never_echoes_archive_metadata(self):
        marker = "MARKER-\u00e9\u4e2d"
        raw = build_zip([(f"{marker}.txt", b"x", {"central_version": 99})])
        raw = raw[:-2] + struct.pack("<H", 12) + b"COMMENT-SECR"
        path = self.inbox / "meta.zip"
        path.write_bytes(raw)
        result = self.cli("ingest", "--zip", path, "--source-id", "synthetic-1", returncode=1)
        for text in (result.stdout, result.stderr):
            for leak in ("MARKER", "COMMENT", "9.9", "version", "NotImplemented"):
                self.assertNotIn(leak, text)

    def test_supported_directory_versions_keep_preview_apply_replay(self):
        for version in (10, 20, 45, zipfile.MAX_EXTRACT_VERSION):
            path = self.archive([("m.txt", b"member %d" % version, {"central_version": version})], name=f"v{version}.zip")
            dry = self.ingest(path, source_id=f"s{version}", dry=True)
            real = self.ingest(path, source_id=f"s{version}")
            self.assertEqual([m["path"] for m in real["members"]], ["m.txt"])
            self.assertEqual(real["members"][0]["sha256"], hashlib.sha256(b"member %d" % version).hexdigest())
            self.assertEqual(real["archive"]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(dry["archive"]["sha256"], real["archive"]["sha256"])
            again = self.ingest(path, source_id=f"s{version}")
            self.assertEqual((again["stored"], again["skipped"]), (0, []))

    def test_unsupported_method_remains_member_unreadable(self):
        path = self.archive([("good.txt", b"good", {}), ("odd.bin", b"odd", {"method": 99, "raw": b"odd"})])
        dry = self.ingest(path, dry=True)
        real = self.ingest(path)
        self.assertEqual(self.skipped(dry), {"odd.bin": "unreadable"})
        self.assertEqual(self.skipped(real), self.skipped(dry))
        self.assertEqual([m["path"] for m in real["members"]], ["good.txt"])
        self.assertEqual(real["archive"]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

    def compressed(self, method, data):
        return {8: deflate, 12: bz2.compress, 14: zip_lzma}[method](data)

    def member_bytes(self, entry):
        """(archive bytes, ZipInfo, payload start, header-checked result) for one synthetic member."""
        from campaign_tool import ingest_zip
        data = build_zip([entry])
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            info = archive.infolist()[0]
            start = ingest_zip._payload_start(data, info, archive.start_dir)
        return ingest_zip, data, info, start

    def unused_entry(self, method, name, text, tail):
        raw = self.compressed(method, text) + tail
        return (name, text, {"method": method, "raw": raw, **({"flags": 0x802} if method == 14 else {})})

    def tails(self, method):
        return {"zero": b"\x00", "marker": b"UNCONSUMED-MARKER", "second": self.compressed(method, b"second stream\n")}

    def test_compressed_member_rejects_every_unused_suffix(self):
        text = b"synthetic record line\n" * 20
        for method in (8, 12, 14):
            clean_module, data, info, start = self.member_bytes(self.unused_entry(method, "m.txt", text, b""))
            self.assertEqual(clean_module._member_bytes(data, info, start), (text, None))
            for label, tail in self.tails(method).items():
                with self.subTest(method=method, tail=label):
                    module, data, info, start = self.member_bytes(self.unused_entry(method, "m.txt", text, tail))
                    self.assertIsNotNone(start, "local header preflight must still accept the member")
                    self.assertEqual(module._member_bytes(data, info, start), (None, "unreadable"))

    def test_empty_compressed_stream_with_suffix_is_unreadable(self):
        for method in (8, 12, 14):
            module, data, info, start = self.member_bytes(self.unused_entry(method, "e.txt", b"", b""))
            self.assertEqual(module._member_bytes(data, info, start), (b"", None))
            for label, tail in self.tails(method).items():
                with self.subTest(method=method, tail=label):
                    module, data, info, start = self.member_bytes(self.unused_entry(method, "e.txt", b"", tail))
                    self.assertEqual(module._member_bytes(data, info, start), (None, "unreadable"))

    def mixed_unused_archive(self):
        text = b"shared decoded prefix\n"
        entries = [self.unused_entry(8, "bad8.txt", text, b"UNCONSUMED-MARKER"),
                   self.unused_entry(12, "bad12.txt", b"bz2 body\n", b"\x00"),
                   self.unused_entry(14, "bad14.txt", b"lzma body\n", self.compressed(14, b"more")),
                   ("peer-shared.txt", text, {}),
                   ("peer-bz.txt", b"healthy bz\n", {"method": 12, "raw": bz2.compress(b"healthy bz\n")}),
                   ("peer-xz.txt", b"healthy xz\n", {"method": 14, "raw": zip_lzma(b"healthy xz\n"), "flags": 0x802})]
        return text, self.archive(entries, name="mixed.zip")

    def test_mixed_archive_unused_payload_has_no_derived_provenance(self):
        text, path = self.mixed_unused_archive()
        report = self.ingest(path)
        bad = {"bad8.txt", "bad12.txt", "bad14.txt"}
        self.assertEqual(self.skipped(report), {name: "unreadable" for name in bad})
        self.assertEqual([m["path"] for m in report["members"]], ["peer-shared.txt", "peer-bz.txt", "peer-xz.txt"])
        whole = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual((self.root / "private" / "objects" / whole).read_bytes(), path.read_bytes())
        names = [r[0] for r in self.rows("SELECT original_name FROM receipts")]
        self.assertEqual(sorted(names), sorted(["mixed.zip"] + [f"mixed.zip/{n}" for n in
                                                                ("peer-shared.txt", "peer-bz.txt", "peer-xz.txt")]))
        self.assertEqual(self.rows("SELECT count(*) FROM contains"), [(3,)])
        # the shared decoded bytes exist through the healthy peer only
        self.assertEqual((self.root / "private" / "objects" / hashlib.sha256(text).hexdigest()).read_bytes(), text)

    def test_unused_payload_preview_apply_replay_decisions_agree(self):
        _text, path = self.mixed_unused_archive()
        for populated in (False, True):
            if populated:
                self.ingest(self.archive([("other.txt", b"other", {})], name="other.zip"), source_id="other")
            before = self.files()
            dry = self.ingest(path, dry=True)
            self.assertEqual(self.files(), before, "preview wrote something")
        real = self.ingest(path)
        self.assertEqual(self.skipped(real), self.skipped(dry))
        self.assertEqual([m["path"] for m in real["members"]], [m["path"] for m in dry["members"]])
        self.assertEqual(set(real), {"archive", "members", "skipped", "stored", "present"})
        again = self.ingest(path)
        self.assertEqual((again["stored"], again["skipped"]), (0, real["skipped"]))
        self.assertEqual([m["outcome"] for m in again["members"]], ["present"] * 3)
        self.assertEqual(self.rows("SELECT count(*) FROM contains"), [(4,)])
        self.assertEqual(self.rows("SELECT count(*) FROM receipts WHERE original_name LIKE 'mixed.zip/bad%'"), [(0,)])
        self.cli("status", "--check")

    def test_payload_end_is_not_physical_record_end(self):
        text = b"streamed payload\n" * 5
        for method in (8, 12, 14):
            with self.subTest(method=method):
                # bytes after an accurately bounded compressed slice (before the next record) are not decoder input
                module, data, info, start = self.member_bytes(self.unused_entry(method, "s.txt", text, b""))
                padded = bytearray(data)
                at = start + info.compress_size
                padded[at:at] = b"OUTSIDE-SLICE"
                self.assertEqual(module._member_bytes(bytes(padded), info, start), (text, None))
        # real stdlib streaming writer and forced ZIP64 members stay accepted
        for method in (zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA):
            with self.subTest(streaming=method):
                class Sink(io.RawIOBase):
                    def __init__(self):
                        self.chunks = []
                    def writable(self):
                        return True
                    def write(self, b):
                        self.chunks.append(bytes(b))
                        return len(b)
                sink = Sink()
                with zipfile.ZipFile(sink, "w", method) as writer:
                    with writer.open("s.txt", "w", force_zip64=True) as member:
                        member.write(text)
                path = self.inbox / f"stream{method}.zip"
                path.write_bytes(b"".join(sink.chunks))
                report = self.ingest(path, source_id=f"s{method}")
                self.assertEqual((report["skipped"], [m["path"] for m in report["members"]]), ([], ["s.txt"]))

    def test_stored_content_and_single_stream_vectors_remain_exact(self):
        marker = b"UNCONSUMED-MARKER"
        module, data, info, start = self.member_bytes(("m.bin", marker, {"method": 0}))
        self.assertEqual(module._member_bytes(data, info, start), (marker, None))
        for text in (b"a", b"ab", b"x" * 3, bytes(range(256))):
            for method in (8, 12, 14):
                module, data, info, start = self.member_bytes(self.unused_entry(method, "v.bin", text, b""))
                self.assertEqual(module._member_bytes(data, info, start), (text, None))

    def test_bomb_and_prior_skip_precedence_is_unchanged(self):
        from unittest import mock
        text = b"q" * 5000
        for method in (8, 12, 14):
            with self.subTest(method=method):
                module, data, info, start = self.member_bytes(
                    self.unused_entry(method, "c.txt", text, b"UNCONSUMED-MARKER"))
                with mock.patch.object(module, "MAX_BYTES", 100):
                    self.assertEqual(module._member_bytes(data, info, start), (None, "too_large"))

    def test_integrity_document_is_linked_and_states_its_limits(self):
        document = REPO_ROOT / "docs" / "ZIP-INGEST-INTEGRITY.md"
        self.assertIn("docs/ZIP-INGEST-INTEGRITY.md", (REPO_ROOT / "campaign_tool" / "ingest_zip.py").read_text())
        text = document.read_text()
        for phrase in ("structural", "descriptor", "ZIP64", "original archive"):
            self.assertIn(phrase, text)

    BAD_MESSAGE = "Stopped: Archive filename cannot be represented as UTF-8; nothing was imported\n"

    def raw_archive(self, raw_name, entries=None, parent=None, content=None):
        directory = os.fsencode(str(parent or self.inbox))
        path = os.fsdecode(directory + b"/" + raw_name)
        if parent is not None:
            os.makedirs(os.fsdecode(directory), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(content if content is not None else build_zip(entries if entries is not None else [("good.txt", b"synthetic\n", {})]))
        return path

    def test_raw_zip_basename_refuses_before_preview_and_store(self):
        for raw in (b"synthetic-\xff.zip", b"synthetic-\xe4\xb8.zip"):
            path = self.raw_archive(raw)
            for dry in (True, False):
                before = self.files()
                result = self.cli("ingest", "--zip", path, "--source-id", "s", *(["--dry-run"] if dry else []),
                                  returncode=1)
                self.assertEqual((result.stdout, result.stderr), ("", self.BAD_MESSAGE))
                self.assertEqual(self.files(), before)
            os.unlink(path)

    def test_zip_filename_refusal_precedence_and_empty_archives(self):
        for content in (b"not a zip at all", build_zip([]), build_zip([("a", b"x", {"central_version": 99})]),
                        build_zip([("../u", b"x", {})])):
            path = self.raw_archive(b"bad-\xff.zip", content=content)
            result = self.cli("ingest", "--zip", path, "--source-id", "s", returncode=1)
            self.assertEqual((result.stdout, result.stderr), ("", self.BAD_MESSAGE))
            os.unlink(path)
        good = self.inbox / "good.zip"
        good.write_bytes(b"not a zip at all")
        result = self.cli("ingest", "--zip", good, "--source-id", "s", returncode=1)
        self.assertIn("not a zip archive", result.stderr)

    def test_bad_name_retry_preserves_existing_digest_and_manual_rename_works(self):
        entries = [("good.txt", b"synthetic\n", {})]
        ok = self.archive(entries, "ok.zip")
        self.ingest(ok)
        bad = self.raw_archive(b"bad-\xff.zip", entries)
        before = self.files()
        for _ in range(2):
            self.cli("ingest", "--zip", bad, "--source-id", "synthetic-1", returncode=1)
        self.assertEqual({k: v for k, v in self.files().items()}, before)
        renamed = self.inbox / "ordinary.zip"
        os.rename(bad, renamed)
        report = self.ingest(renamed, source_id="synthetic-2")
        self.assertEqual([m["path"] for m in report["members"]], ["good.txt"])
        self.assertIn("ordinary.zip", [r[0] for r in self.rows("SELECT original_name FROM receipts")])

    def test_zip_unicode_leaf_and_raw_ancestors_remain_supported(self):
        for index, leaf in enumerate(("caf\u00e9.zip", "cafe\u0301.zip", "\u4e2d\u6587.zip", "\U0001f600.zip")):
            path = self.raw_archive(leaf.encode("utf-8"), parent=self.inbox / f"d{index}",
                                    entries=[("m.txt", f"synthetic {index}\n".encode(), {})])
            report = self.ingest(path, source_id=f"u{index}")
            self.assertEqual(report["archive"]["outcome"], "stored")
            self.assertIn(leaf, [r[0] for r in self.rows("SELECT original_name FROM receipts")])
        nested = os.fsdecode(os.fsencode(str(self.inbox)) + b"/raw-\xff")
        path = self.raw_archive(b"ok.zip", parent=nested, entries=[("m.txt", b"synthetic anc\n", {})])
        report = self.ingest(path, source_id="anc")
        self.assertEqual(report["archive"]["outcome"], "stored")
        self.assertIn("ok.zip", [r[0] for r in self.rows("SELECT original_name FROM receipts")])

    def test_integrity_document_names_container_admission(self):
        text = (REPO_ROOT / "docs" / "ZIP-INGEST-INTEGRITY.md").read_text()
        for phrase in ("cannot be represented as UTF-8", "whole-container refusal", "rename the file yourself",
                       "container-level admission"):
            self.assertIn(phrase, text)

    def test_integrity_document_names_full_payload_consumption(self):
        text = (REPO_ROOT / "docs" / "ZIP-INGEST-INTEGRITY.md").read_text()
        for phrase in ("single stream", "unused", "declared compressed payload", "unreadable", "not a migration"):
            self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
