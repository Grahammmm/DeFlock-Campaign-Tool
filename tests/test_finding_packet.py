"""DF2037-13: `finding packet DRAFT --directory D --out DIR`. Synthetic example campaign and synthetic records only.

Run from the repository root: python3 -B -m unittest tests.test_finding_packet -v
"""
import ast
import ctypes
import errno
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import finding_packet, findings  # noqa: E402
from campaign_tool.content import load_finding_preview  # noqa: E402

EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
RECORDS = [b"synthetic packet record %d\n" % index for index in range(2)]
BANNER = '<p class="draft-banner" role="note">Draft - not published</p>'
NOINDEX = '<meta name="robots" content="noindex">'
FILES = ["MANIFEST.sha256", "README.txt", "draft.json", "preview.html", "receipts.json", "records",
         "review-status.json", "reviews.json"]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def draft(**changes):
    base = {"id": "fnd_00000000000000p1", "title": "Cedar packet draft", "slug": "cedar-packet-draft",
            "summary": "Synthetic summary.", "body_md": "Synthetic body.", "classification": "documented_fact",
            "confidence": "verified", "event_date": "2026-01-16", "agency_id": "ca-cedar-police",
            "sources": [{"sha256": sha(RECORDS[0]), "locator": "page:1"}, {"sha256": sha(RECORDS[0]), "locator": "page:2"},
                        {"sha256": sha(RECORDS[1]), "locator": "page:1"}],
            "published_at": None, "corrections": [], "state": "draft", "author": "synthetic-author",
            "limitations": ["Synthetic limits."], "counterevidence": ["None found."]}
    base.update(changes)
    return base


def trees(root):
    """public/, content/ and private/ as {relative path: (mode, bytes)} (directories included)."""
    return {p.relative_to(root).as_posix(): (os.lstat(p).st_mode, p.read_bytes() if p.is_file() else None)
            for name in ("public", "content", "private") for p in sorted((root / name).rglob("*"))}


class FindingPacketTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-finding-packet-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        (self.root / "public").mkdir()
        for data, source_ids in ((RECORDS[0], ("synthetic-a", "synthetic-b")), (RECORDS[1], ("synthetic-a",))):
            path = self.base / ("record-%s.txt" % sha(data)[:8])
            path.write_bytes(data)
            for source_id in source_ids:
                self.cli("ingest", "--directory", self.root, "--file", path, "--source-id", source_id)
        library = self.root / "content" / "sources.json"
        entries = json.loads(library.read_text(encoding="utf-8"))
        entries += [dict(entries[0], sha256=sha(data), title=f"Synthetic packet record {i}") for i, data in enumerate(RECORDS)]
        library.write_text(json.dumps(entries), encoding="utf-8")
        self.objects = self.root / "private" / "objects"
        self.ledger = self.root / "private" / "ledger.sqlite"
        self.draft = self.base / "draft.json"
        self.draft.write_text(json.dumps(draft(), indent=1), encoding="utf-8")
        self.out = self.base / "packet-x"

    def cli(self, *arguments, returncode=0):
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, arguments)],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, returncode, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        if returncode:
            self.assertTrue(result.stderr.startswith("Stopped: "), result.stderr)
        return result

    def packet(self, out=None, returncode=0):
        return self.cli("finding", "packet", self.draft, "--directory", self.root, "--out", out or self.out,
                        returncode=returncode)

    def refused(self, needle, out=None):
        """In-process refusal: the message names ``needle``; nothing at DIR, no temp folder, stores unchanged."""
        out = Path(out or self.out)
        before, siblings = trees(self.root), sorted(os.listdir(out.parent)) if out.parent.is_dir() else None
        with self.assertRaises(ValueError) as caught:
            finding_packet.build_packet(self.root, self.draft, out)
        self.assertIn(needle, str(caught.exception))
        self.assertEqual(trees(self.root), before)
        if siblings is not None:
            self.assertEqual(sorted(os.listdir(out.parent)), siblings)
        return str(caught.exception)

    def read(self, name):
        return json.loads((self.out / name).read_text(encoding="utf-8"))

    def test_packet_has_records_receipts_reviews_manifest_and_private_modes(self):
        findings.add_receipt(self.root, draft(), "factual", "approve", "Rev One", "Checked synthetic pages.")
        before = trees(self.root)
        result = self.packet()
        self.assertEqual(json.loads(result.stdout.splitlines()[0]), {"written": str(self.out), "files": 9, "records": 2})
        self.assertIn("nothing was zipped, uploaded or sent", result.stdout)
        self.assertEqual(trees(self.root), before)
        self.assertEqual(sorted(os.listdir(self.out)), FILES)
        self.assertEqual(sorted(os.listdir(self.out / "records")), sorted(sha(d) for d in RECORDS))
        for data in RECORDS:
            self.assertEqual((self.out / "records" / sha(data)).read_bytes(), (self.objects / sha(data)).read_bytes())
        self.assertEqual((self.out / "draft.json").read_bytes(), self.draft.read_bytes())
        html = (self.out / "preview.html").read_text(encoding="utf-8")
        self.assertEqual((html.count(BANNER), html.count(NOINDEX)), (1, 1))
        self.assertIn("Cedar packet draft", html)
        db = sqlite3.connect(self.ledger.as_uri() + "?mode=ro", uri=True)
        self.addCleanup(db.close)
        expected = {d: [{"receipt_id": r, "source_id": s, "received_at": t, "original_name": n} for r, s, t, n in db.execute(
            "SELECT receipt_id, source_id, received_at, original_name FROM receipts WHERE object_sha256 = ? "
            "ORDER BY received_at, receipt_id", (d,))] for d in map(sha, RECORDS)}
        self.assertEqual(self.read("receipts.json"), expected)
        self.assertEqual({r["source_id"] for r in expected[sha(RECORDS[0])]}, {"synthetic-a", "synthetic-b"})
        self.assertEqual(len(expected[sha(RECORDS[1])]), 1)
        self.assertEqual(self.read("reviews.json"), json.loads((self.root / "private" / "reviews" /
                                                                f"{draft()['id']}.json").read_text()))
        self.assertEqual(self.read("review-status.json"), findings.review_status(self.root, draft()))
        self.assertEqual(self.read("review-status.json")["receipts_for_this_content"], 1)
        self.assertIn("sha256sum -c MANIFEST.sha256", (self.out / "README.txt").read_text(encoding="utf-8"))
        lines = (self.out / "MANIFEST.sha256").read_text(encoding="ascii").splitlines()
        listed = [line.split("  ", 1)[1] for line in lines]
        files = sorted(p.relative_to(self.out).as_posix() for p in self.out.rglob("*") if p.is_file())
        self.assertEqual(listed, sorted(listed))
        self.assertEqual(listed, [f for f in files if f != "MANIFEST.sha256"])
        for line in lines:  # sha256sum -c
            digest, name = line.split("  ", 1)
            self.assertEqual(sha((self.out / name).read_bytes()), digest, name)
        for path in [self.out, *self.out.rglob("*")]:
            self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o700 if path.is_dir() else 0o600, path)
        self.assertEqual(sorted(os.listdir(self.base)), ["campaign", "draft.json", "packet-x"] +
                         sorted(f"record-{sha(d)[:8]}.txt" for d in RECORDS))

    def test_existing_out_even_empty_or_a_second_run_refused(self):
        self.out.mkdir()
        self.assertIn(f"{self.out} exists; choose a new folder", self.packet(returncode=2).stderr)
        self.assertEqual(os.listdir(self.out), [])
        self.out.rmdir()
        self.packet()
        manifest = (self.out / "MANIFEST.sha256").read_bytes()
        self.refused("exists; choose a new folder")
        self.assertEqual((self.out / "MANIFEST.sha256").read_bytes(), manifest)

    def test_out_inside_public_content_private_or_via_symlinked_parent_refused(self):
        for name in ("public", "content", "private"):
            self.refused(f"--out is inside {name}/", self.root / name / "packet-x")
            (self.root / name / "sub").mkdir()
            self.refused(f"--out is inside {name}/", self.root / name / "sub" / ".." / "packet-x")
        (self.base / "to-root").symlink_to(self.root)
        self.refused("--out is inside content/", self.base / "to-root" / "content" / "packet-x")
        for target in (self.root / "private", self.base):
            link = self.base / f"link-{target.name}"
            link.symlink_to(target)
            self.refused("must be a real folder, not a symlink", link / "packet-x")
            self.assertFalse((target / "packet-x").exists())
        self.refused("must be a real folder", self.base / "missing" / "packet-x")
        self.assertFalse(os.path.lexists(self.out))

    def test_missing_or_altered_object_refused_naming_prefix(self):
        stored = self.objects / sha(RECORDS[1])
        data = stored.read_bytes()
        stored.unlink()
        self.assertIn(f"{sha(RECORDS[1])[:12]} object_missing", self.packet(returncode=2).stderr)
        self.assertFalse(os.path.lexists(self.out))
        stored.write_bytes(data + b"altered")
        self.refused(f"{sha(RECORDS[1])[:12]} hash_mismatch")
        stored.unlink()
        stored.symlink_to(self.base / "draft.json")
        self.refused(f"{sha(RECORDS[1])[:12]} object_missing")

    def test_object_changed_while_copying_leaves_no_folder_or_temp(self):
        real = finding_packet._copy_hash
        with mock.patch.object(finding_packet, "_copy_hash", lambda source, sink=None, **kw: real(source, sink, **kw)
                               if sink is None else ("0" * 64, real(source, sink, **kw)[1])):
            self.refused("changed while copying")
        self.assertFalse(os.path.lexists(self.out))

    def grown_objects(self, extra, limit):
        """Patch the object opener: opens 0-1 are preflight, 2-3 the copy pass; ``extra`` {open index: appended bytes}.
        Handles report a 1-byte fstat (a stale size) while reading real bytes. Returns (reads, sink_writes)."""
        tiny = self.base / "tiny"
        tiny.write_bytes(b"x")
        fd = os.open(tiny, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        reads, writes, opens = [], [], []
        real = self.__dict__.setdefault("real_open", finding_packet._open_object)  # first, unpatched opener
        real_create = self.__dict__.setdefault("real_create", finding_packet._create)
        test = self

        class Handle:
            def __init__(self, data):
                self.data, self.pos = data, 0

            def fileno(self):
                return fd

            def read(self, n):
                test.assertGreater(n, 0)
                reads.append((len(opens) - 1, n))
                chunk = self.data[self.pos:self.pos + n]
                self.pos += len(chunk)
                return chunk

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def opener(objects, sha_):
            with real(objects, sha_) as handle:
                data = handle.read()
            opens.append(sha_)
            return Handle(data + extra.get(len(opens) - 1, b""))


        def create(path, data=None):
            handle = real_create(path, data)
            if data is None and Path(path).parent.name == "records":
                inner = handle.write
                handle.write = lambda chunk: (writes.append(len(chunk)), inner(chunk))[1]
            return handle
        patches = (mock.patch.object(finding_packet, "_open_object", opener),
                   mock.patch.object(finding_packet, "_create", create),
                   mock.patch.object(finding_packet, "MAX_RECORD_BYTES", limit))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return reads, writes

    def test_growth_after_real_budget_fstat_refuses_before_staging(self):
        limit = len(RECORDS[0]) + len(RECORDS[1])
        reads, writes = self.grown_objects({0: b"G" * limit}, limit)  # fits by stat, grows past the budget when read
        self.refused("packet too large")
        self.assertEqual((writes, [n for _, n in reads]), ([], [limit + 1]))
        self.assertFalse(os.path.lexists(self.out))

    def test_copy_growth_stops_before_budget_and_cleans_owned_staging(self):
        limit = len(RECORDS[0]) + len(RECORDS[1])
        sentinel = self.base / "sibling.txt"
        sentinel.write_bytes(b"keep")
        reads, writes = self.grown_objects({2: b"G" * 50}, limit)  # first copy stream grows after preflight passed
        self.refused("packet too large")
        self.assertTrue(all(n <= limit + 1 for _, n in reads), reads)
        self.assertEqual(writes, [])  # the crossing chunk is never written
        self.assertEqual(sentinel.read_bytes(), b"keep")

    def test_aggregate_limit_counts_actual_bytes_across_distinct_records(self):
        limit = len(RECORDS[0]) + len(RECORDS[1])
        for index, writes_expected in ((1, []), (3, None)):  # later record grows: preflight, then copy pass
            with self.subTest(open_index=index):
                reads, writes = self.grown_objects({index: b"G"}, limit)
                self.refused("packet too large")
                self.assertFalse(os.path.lexists(self.out))
                if writes_expected is None:  # first record fully copied, the second's crossing read writes nothing
                    self.assertEqual(len(writes), 1)
                self.assertEqual(reads[-1][1], len(RECORDS[1]) + 1)  # remaining is the later record's size, plus one

    def test_exact_limit_eof_and_one_byte_overflow_both_passes(self):
        limit = len(RECORDS[0]) + len(RECORDS[1])
        reads, writes = self.grown_objects({}, limit)  # exact fit succeeds in both passes
        self.assertEqual(finding_packet.build_packet(self.root, self.draft, self.out)["records"], 2)
        self.assertEqual(sum(writes), limit)
        self.assertTrue(all(n <= limit + 1 for _, n in reads))

    def reader(self, data, step=None):
        sizes, state = [], {"pos": 0}

        class Reader:
            def read(self, n):
                assert n > 0
                sizes.append(n)
                chunk = data[state["pos"]:state["pos"] + min(n, step or n)]
                state["pos"] += len(chunk)
                return chunk
        return Reader(), sizes

    def test_copy_hash_boundaries_short_reads_and_overflow_chunk_not_written(self):
        data = b"abcdefgh"
        for limit, ok in ((8, True), (9, True), (7, False), (0, False)):
            sink = []
            source, sizes = self.reader(data, step=3)  # short reads are not EOF
            call = lambda: finding_packet._copy_hash(source, mock.Mock(write=sink.append), limit=limit)  # noqa: E731
            if ok:
                self.assertEqual(call(), (sha(data), 8))
                self.assertEqual(b"".join(sink), data)
                self.assertEqual(sizes[0], min(finding_packet.CHUNK, limit + 1))
            else:
                with self.assertRaises(ValueError):
                    call()
                self.assertLessEqual(sum(map(len, sink)), limit)
        source, sizes = self.reader(b"")
        self.assertEqual(finding_packet._copy_hash(source, limit=0), (sha(b""), 0))
        self.assertEqual(sizes, [1])  # zero remaining still does a one-byte EOF check
        source, sizes = self.reader(b"xyz")
        sink = []
        with self.assertRaises(ValueError):
            finding_packet._copy_hash(source, mock.Mock(write=sink.append), limit=2)
        self.assertEqual((sink, sizes), ([], [3]))  # whole crossing chunk refused before any sink write

    def test_records_over_500_mib_refused(self):
        with mock.patch.object(finding_packet, "MAX_RECORD_BYTES", len(RECORDS[0]) + len(RECORDS[1]) - 1):
            self.refused("packet too large")
        self.assertFalse(os.path.lexists(self.out))

    def test_draft_without_receipts_is_written_with_its_blockers(self):
        self.packet()
        status = self.read("review-status.json")
        self.assertEqual(status, findings.review_status(self.root, draft()))
        self.assertTrue(status["review_blockers"])
        self.assertEqual(status["receipts_for_this_content"], 0)
        self.assertEqual(self.read("reviews.json"), [])

    def test_preview_refusal_stops_with_that_refusal(self):
        document = draft(classification="not-a-class")
        self.draft.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            load_finding_preview(document, str(self.draft))
        self.assertIn(str(caught.exception), self.packet(returncode=2).stderr)
        self.assertFalse(os.path.lexists(self.out))
        self.refused(str(caught.exception))

    def test_ledger_with_wal_or_journal_sidecar_refused_and_ledger_unchanged(self):
        ledger = self.ledger.read_bytes()
        for suffix in ("wal", "journal"):
            sidecar = Path(f"{self.ledger}-{suffix}")
            sidecar.write_bytes(b"")
            self.assertIn("journal or WAL file beside it", self.packet(returncode=2).stderr)
            self.refused("journal or WAL file beside it")
            sidecar.unlink()
        self.assertEqual(self.ledger.read_bytes(), ledger)
        self.assertFalse(os.path.lexists(self.out))

    def test_checkpointed_wal_ledger_without_sidecars_read_from_snapshot_private_tree_unchanged(self):
        db = sqlite3.connect(self.ledger)
        self.assertEqual(db.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        db.close()  # last connection checkpoints and removes -wal/-shm; the header still says WAL
        self.assertEqual(self.ledger.read_bytes()[18:20], b"\2\2")
        self.assertEqual(sorted(p.name for p in self.ledger.parent.glob("ledger.sqlite*")), ["ledger.sqlite"])
        before = trees(self.root)
        finding_packet.build_packet(self.root, self.draft, self.out)
        self.assertEqual(sorted(p.name for p in self.ledger.parent.glob("ledger.sqlite*")), ["ledger.sqlite"])
        self.assertEqual(trees(self.root), before)  # no -wal/-shm created in private/, no byte changed
        self.assertEqual(self.read("receipts.json")[sha(RECORDS[1])][0]["source_id"], "synthetic-a")
        shutil.rmtree(self.out)
        before = trees(self.root)
        self.packet()  # the CLI path too
        self.assertEqual(trees(self.root), before)

    def test_packet_oversize_refuses_before_staging(self):
        from campaign_tool import ledger_list
        size = self.ledger.stat().st_size
        with mock.patch.object(ledger_list, "MAX_LEDGER_BYTES", size - 1):
            self.refused(ledger_list.LEDGER_SIZE_ERROR)  # no target, no staging folder, stores and inputs unchanged
        self.assertFalse(os.path.lexists(self.out))
        with open(self.ledger, "r+b") as handle:
            handle.truncate(ledger_list.MAX_LEDGER_BYTES + 1)  # sparse: the production limit plus one byte
        stamp, siblings = self.ledger.stat().st_mtime_ns, sorted(os.listdir(self.base))
        self.assertIn(ledger_list.LEDGER_SIZE_ERROR, self.packet(returncode=2).stderr)
        self.assertEqual((self.ledger.stat().st_size, self.ledger.stat().st_mtime_ns, sorted(os.listdir(self.base))),
                         (ledger_list.MAX_LEDGER_BYTES + 1, stamp, siblings))
        self.assertFalse(os.path.lexists(self.out))

    def arriving(self, make):
        """Seam: ``make(target)`` runs after staging is complete, just before the real native no-replace rename."""
        real = finding_packet._noreplace_rename()
        self.assertIsNotNone(real)
        calls = []

        def seam():
            def rename(source, target):
                calls.append((source, target, sorted(os.listdir(source))))
                make(Path(target))
                return real(source, target)
            return rename
        return mock.patch.object(finding_packet, "_noreplace_rename", seam), calls

    def failing(self, code):
        calls = []

        def seam():
            def rename(source, target):
                calls.append((source, target))
                ctypes.set_errno(code)
                return -1
            return rename
        return mock.patch.object(finding_packet, "_noreplace_rename", seam), calls

    def test_arriving_empty_destination_is_preserved_at_native_install(self):
        arrived = []

        def make(target):
            target.mkdir(mode=0o750)
            os.chmod(target, 0o750)
            arrived.append(os.lstat(target))
        patch, calls = self.arriving(make)
        before = trees(self.root)
        with patch:
            with self.assertRaises(ValueError) as caught:
                finding_packet.build_packet(self.root, self.draft, self.out)
        self.assertIn(f"{self.out} exists; choose a new folder", str(caught.exception))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2], FILES)  # staging was complete when the install was attempted
        now = os.lstat(self.out)
        self.assertEqual((now.st_dev, now.st_ino, stat.S_IMODE(now.st_mode)),
                         (arrived[0].st_dev, arrived[0].st_ino, 0o750))
        self.assertEqual(os.listdir(self.out), [])
        self.assertEqual(sorted(os.listdir(self.base))[:2], ["campaign", "draft.json"])
        self.assertFalse([n for n in os.listdir(self.base) if n.startswith(".packet-x")])
        self.assertEqual(trees(self.root), before)

    def test_arriving_destination_types_never_replaced_or_followed(self):
        (self.base / "referent").mkdir()
        kinds = {
            "dir": lambda t: t.mkdir(),
            "nonempty": lambda t: (t.mkdir(), (t / "sentinel").write_bytes(b"keep")),
            "file": lambda t: t.write_bytes(b"keep"),
            "link": lambda t: t.symlink_to(self.base / "referent"),
            "dangling": lambda t: t.symlink_to(self.base / "nowhere"),
        }
        for name, make in kinds.items():
            with self.subTest(name):
                patch, calls = self.arriving(make)
                with patch, self.assertRaises(ValueError) as caught:
                    finding_packet.build_packet(self.root, self.draft, self.out)
                self.assertIn("exists; choose a new folder", str(caught.exception))
                self.assertEqual(len(calls), 1)
                mode = os.lstat(self.out).st_mode
                if name in ("link", "dangling"):
                    self.assertTrue(stat.S_ISLNK(mode))
                    self.assertEqual(os.listdir(self.base / "referent"), [])
                    self.assertFalse(os.path.lexists(self.base / "nowhere"))
                elif name == "file":
                    self.assertEqual(self.out.read_bytes(), b"keep")
                elif name == "nonempty":
                    self.assertEqual(os.listdir(self.out), ["sentinel"])
                else:
                    self.assertEqual(os.listdir(self.out), [])
                self.assertFalse([n for n in os.listdir(self.base) if n.startswith(".packet-x")])
                if os.path.isdir(self.out) and not os.path.islink(self.out):
                    shutil.rmtree(self.out)
                else:
                    os.unlink(self.out)
        self.out.mkdir()  # initially existing: the early diagnostic still wins, no native call
        self.refused("exists; choose a new folder")

    def test_absent_destination_atomic_install_retains_exact_packet(self):
        seen = []
        patch, calls = self.arriving(lambda t: seen.append(os.path.lexists(t)))
        before = trees(self.root)
        with patch:
            result = finding_packet.build_packet(self.root, self.draft, self.out)
        self.assertEqual(seen, [False])
        self.assertEqual(calls[0][2], FILES)
        self.assertEqual(result, {"written": str(self.out), "files": 9, "records": 2})
        self.assertEqual(sorted(os.listdir(self.out)), FILES)
        self.assertEqual((self.out / "draft.json").read_bytes(), self.draft.read_bytes())
        for line in (self.out / "MANIFEST.sha256").read_text(encoding="ascii").splitlines():
            digest, name = line.split("  ", 1)
            self.assertEqual(sha((self.out / name).read_bytes()), digest, name)
        for path in [self.out, *self.out.rglob("*")]:
            self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o700 if path.is_dir() else 0o600, path)
        self.assertEqual(sorted(os.listdir(self.base)), ["campaign", "draft.json", "packet-x"] +
                         sorted(f"record-{sha(d)[:8]}.txt" for d in RECORDS))
        self.assertEqual(trees(self.root), before)

    def test_no_replace_capability_unavailable_refuses_before_staging(self):
        with mock.patch.object(finding_packet, "_noreplace_rename", lambda: None), \
                mock.patch.object(os, "rename") as plain, mock.patch.object(os, "replace") as replace, \
                mock.patch.object(os, "mkdir") as mkdir, mock.patch.object(tempfile, "mkdtemp") as staging:
            self.refused(finding_packet._UNSUPPORTED)
        for fake in (plain, replace, mkdir, staging):
            fake.assert_not_called()
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertIsNone(finding_packet._noreplace_rename())
        self.assertFalse(os.path.lexists(self.out))

    def test_native_unsupported_filesystem_refuses_and_cleans_only_staging(self):
        (self.base / "sibling").write_bytes(b"keep")
        for code in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            with self.subTest(code=code):
                patch, calls = self.failing(code)
                with patch, mock.patch.object(os, "rename") as plain:
                    self.refused(finding_packet._UNSUPPORTED)
                plain.assert_not_called()
                self.assertEqual(len(calls), 1)
        self.assertEqual((self.base / "sibling").read_bytes(), b"keep")
        self.assertFalse(os.path.lexists(self.out))
        patch, _ = self.failing(errno.EINVAL)
        with patch:
            result = self.cli_in_process(2)
        self.assertEqual(result, ("", f"Stopped: {finding_packet._UNSUPPORTED}\n"))

    def test_native_errors_are_not_retried_as_replacing_rename(self):
        for code in (errno.EEXIST, errno.ENOTEMPTY):
            with self.subTest(code=code):
                patch, calls = self.failing(code)
                with patch:
                    self.refused("exists; choose a new folder")
                self.assertEqual(len(calls), 1)
        for code in (errno.EACCES, errno.EXDEV, errno.EIO):
            with self.subTest(code=code):
                patch, calls = self.failing(code)
                before, siblings = trees(self.root), sorted(os.listdir(self.base))
                with patch, mock.patch.object(os, "rename") as plain:
                    with self.assertRaises(OSError) as caught:
                        finding_packet.build_packet(self.root, self.draft, self.out)
                self.assertEqual(caught.exception.errno, code)
                plain.assert_not_called()
                self.assertEqual(len(calls), 1)
                self.assertEqual(Path(calls[0][1]), self.out)
                self.assertTrue(Path(calls[0][0]).name.startswith(".packet-x."))
                self.assertEqual((trees(self.root), sorted(os.listdir(self.base))), (before, siblings))

    def test_real_native_primitive_flags_and_conflict(self):
        rename = finding_packet._noreplace_rename()
        self.assertIsNotNone(rename)
        source, target = self.base / "src", self.base / "dst"
        source.mkdir()
        (source / "f").write_bytes(b"x")
        target.mkdir()
        self.assertEqual(rename(source, target), -1)
        self.assertEqual(ctypes.get_errno(), errno.EEXIST)
        self.assertEqual((os.listdir(source), os.listdir(target)), (["f"], []))
        target.rmdir()
        self.assertEqual(rename(source, target), 0)
        self.assertEqual(os.listdir(target), ["f"])

    def cli_in_process(self, returncode):
        """Real cli.main dispatch; returns (stdout, stderr)."""
        import contextlib
        import io
        from campaign_tool import cli
        out, err = io.StringIO(), io.StringIO()
        argv = ["campaign_tool", "finding", "packet", str(self.draft), "--directory", str(self.root), "--out", str(self.out)]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as code:
                cli.main()
        self.assertEqual(code.exception.code, returncode)
        return out.getvalue(), err.getvalue()

    def test_cli_conflict_and_support_refusal_emit_no_success(self):
        patch, _ = self.arriving(lambda t: t.mkdir())
        with patch:
            stdout, stderr = self.cli_in_process(2)
        self.assertEqual((stdout, stderr), ("", f"Stopped: {self.out} exists; choose a new folder\n"))
        self.assertEqual(os.listdir(self.out), [])
        with mock.patch.object(finding_packet, "_noreplace_rename", lambda: None):
            self.out.rmdir()
            stdout, stderr = self.cli_in_process(2)
        self.assertEqual((stdout, stderr), ("", f"Stopped: {finding_packet._UNSUPPORTED}\n"))
        self.assertFalse(os.path.lexists(self.out))

    def test_no_network_imports(self):
        tree = ast.parse((REPO_ROOT / "campaign_tool" / "finding_packet.py").read_text(encoding="utf-8"))
        names = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        names |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        self.assertFalse(names & {"socket", "ssl", "http", "urllib", "requests", "smtplib", "ftplib", "zipfile", "tarfile"})


if __name__ == "__main__":
    unittest.main()
