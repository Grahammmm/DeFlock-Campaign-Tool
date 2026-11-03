"""RF0510-10: tools/flock_backup.py list FILE [--manifest-sha256 HEX] [--json] shows what a verified backup holds per
agency, regions and LAW.csv as file and byte counts, never a file name below the slug or any content (synthetic stores)."""
import contextlib
import hashlib
import importlib.util
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("flock_backup", ROOT / "tools" / "flock_backup.py")
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)
FULL = {"agencies/a-one/FACTS.csv": b"agency,fact\na-one,SECRET-A1\n", "agencies/a-one/POLICY.csv": b"p-a1\n",
        "agencies/a-one/sub/MEMO.txt": b"memo-a1-body\n", "agencies/b-two/FACTS.csv": b"b-two-content\n",
        "regions/r.json": b'{"r": "REGION-BODY"}\n', "regions/r/WATCHLIST.csv": b"watch\n", "LAW.csv": b"rule,LAW-BODY\n"}
CREATED = r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\+00:00|Z)$"


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = backup.main([str(a) for a in argv])
    return code, out.getvalue(), err.getvalue()


def size(files, prefix):
    return sum(len(data) for name, data in files.items() if name.startswith(prefix))


class BackupListTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.outputs = []

    def make(self, files, name="store"):
        store, out = self.tmp / name, self.tmp / (name + ".tar")
        for rel, data in files.items():
            (store / rel).parent.mkdir(parents=True, exist_ok=True)
            (store / rel).write_bytes(data)
        code, text, err = run("create", store, "--out", out)
        self.assertEqual((code, err), (0, ""))
        return out, text.split("manifest_sha256=")[1].strip()

    def list(self, *argv, code=0):
        got = run("list", *argv)
        self.outputs.append((got[1] + got[2], argv))
        self.assertEqual(got[0], code, got)
        return got

    def hand_built(self, manifest_extra):
        files = {"agencies/a-one/FACTS.csv": b"hand-made\n"}
        entries = {n: {"sha256": hashlib.sha256(d).hexdigest(), "bytes": len(d)} for n, d in files.items()}
        manifest = {"schema_version": 1, "kind": "flock-store", "files": entries,
                    "counts": {"files": 1, "bytes": len(b"hand-made\n")}, **manifest_extra}
        raw = json.dumps(manifest).encode()
        path = self.tmp / ("hand%d.tar" % len(list(self.tmp.glob("hand*.tar"))))
        with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as tar:
            backup._add(tar, "manifest.json", len(raw), io.BytesIO(raw))
            for name, data in files.items():
                backup._add(tar, name, len(data), io.BytesIO(data))
        return path

    def tearDown(self):  # hygiene for every case: no name below the slug, no file content, no odd raw folder name
        for text, argv in self.outputs:
            for banned in ("FACTS.csv", "POLICY.csv", "MEMO", "sub", "r.json", "WATCHLIST", "Odd_Name", "loose.csv",
                           "SECRET", "memo-a1", "b-two-content", "REGION-BODY", "LAW-BODY", "hand-made", "watch\n"):
                self.assertNotIn(banned, text, argv)

    def test_text_and_json_counts_per_agency_regions_law(self):
        out, digest = self.make(FULL)
        code, text, err = self.list(out)
        total = sum(len(d) for d in FULL.values())
        self.assertEqual(err, "")
        lines = text.splitlines()
        self.assertRegex(lines[0], r"^backup \| created_at=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00 files=7 bytes=%d$" % total)
        self.assertEqual(lines[1:], [
            "backup | agency | a-one | files=3 bytes=%d" % size(FULL, "agencies/a-one/"),
            "backup | agency | b-two | files=1 bytes=%d" % size(FULL, "agencies/b-two/"),
            "backup | regions | files=2 bytes=%d" % size(FULL, "regions/"),
            "backup | LAW.csv | files=1 bytes=%d" % len(FULL["LAW.csv"])])
        self.assertNotIn("| ? |", text)
        code, text, err = self.list(out, "--json")
        report = json.loads(text)
        manifest, verified_digest, problems = backup.verify(out)
        self.assertEqual((err, problems, verified_digest), ("", [], digest))
        self.assertEqual(report["totals"], {k: manifest["counts"][k] for k in ("files", "bytes")})
        self.assertRegex(report["created_at"], CREATED)
        self.assertEqual(report, {
            "schema": "flock-backup-list-v1", "review_required": True, "manifest_sha256": digest,
            "created_at": report["created_at"], "totals": {"files": 7, "bytes": total},
            "agencies": [{"slug": "a-one", "files": 3, "bytes": size(FULL, "agencies/a-one/")},
                         {"slug": "b-two", "files": 1, "bytes": size(FULL, "agencies/b-two/")}],
            "regions": {"files": 2, "bytes": size(FULL, "regions/")}, "law_csv": True})
        self.assertEqual(text, json.dumps(report, indent=2, sort_keys=True) + "\n")

    def test_no_regions_and_no_law_csv(self):
        files = {"agencies/a-one/FACTS.csv": b"SECRET\n"}
        out, _ = self.make(files)
        _, text, _ = self.list(out)
        self.assertEqual(text.splitlines()[1:], ["backup | agency | a-one | files=1 bytes=7",
                                                 "backup | regions | files=0 bytes=0", "backup | LAW.csv | files=0 bytes=0"])
        report = json.loads(self.list(out, "--json")[1])
        self.assertEqual((report["regions"], report["law_csv"], report["totals"]), ({"files": 0, "bytes": 0}, False,
                                                                                    {"files": 1, "bytes": 7}))

    def test_tampered_member_refused_nothing_listed(self):
        out, digest = self.make(FULL)
        with tarfile.open(out) as tar:
            offset = tar.getmember("LAW.csv").offset_data
        data = bytearray(out.read_bytes())
        data[offset] ^= 1
        out.write_bytes(bytes(data))
        for extra in ((), ("--json",), ("--manifest-sha256", digest)):
            self.assertEqual(self.list(out, *extra, code=1), (1, "", "LAW.csv: hash_mismatch\n"))

    def test_manifest_digest_wrong_refused_right_accepted(self):
        out, digest = self.make(FULL)
        wrong = "f" * 64 if digest != "f" * 64 else "e" * 64
        for extra in ((), ("--json",)):
            self.assertEqual(self.list(out, "--manifest-sha256", wrong, *extra, code=1),
                             (1, "", "manifest.json: manifest_digest_mismatch\n"))
        self.assertEqual(self.list(out, "--manifest-sha256", "0" * 63, code=1)[1], "")
        code, text, err = self.list(out, "--manifest-sha256", " %s\n" % digest.upper(), "--json")
        self.assertEqual((code, err, json.loads(text)["manifest_sha256"]), (0, "", digest))

    def test_missing_or_malformed_created_at_is_null(self):
        for extra in ({}, {"created_at": "yesterday"}, {"created_at": "2026-11-02T05:25:00+01:00"},
                      {"created_at": "2026-11-02T05:25:00Z\n"}, {"created_at": 1793650442}):
            path = self.hand_built(extra)
            _, text, err = self.list(path)
            self.assertEqual((text.splitlines()[0], err), ("backup | created_at=- files=1 bytes=10", ""))
            self.assertIsNone(json.loads(self.list(path, "--json")[1])["created_at"], extra)
        report = json.loads(self.list(self.hand_built({"created_at": "2026-11-02T05:25:00Z"}), "--json")[1])
        self.assertEqual(report["created_at"], "2026-11-02T05:25:00Z")

    def test_odd_folder_names_counted_under_question_mark_only(self):
        files = dict(FULL, **{"agencies/Odd_Name/FACTS.csv": b"SECRET-odd\n", "agencies/-lead/POLICY.csv": b"SECRET-l\n",
                              "agencies/loose.csv": b"SECRET-loose\n", "agencies/0-zero/FACTS.csv": b"z\n"})
        out, _ = self.make(files)
        _, text, _ = self.list(out)
        odd = len(b"SECRET-odd\n") + len(b"SECRET-l\n") + len(b"SECRET-loose\n")
        self.assertEqual([line for line in text.splitlines() if "| agency |" in line], [
            "backup | agency | 0-zero | files=1 bytes=2", "backup | agency | a-one | files=3 bytes=%d"
            % size(FULL, "agencies/a-one/"), "backup | agency | b-two | files=1 bytes=%d" % size(FULL, "agencies/b-two/"),
            "backup | agency | ? | files=3 bytes=%d" % odd])
        self.assertEqual(text.splitlines()[-1], "backup | LAW.csv | files=1 bytes=%d" % len(FULL["LAW.csv"]))
        report = json.loads(self.list(out, "--json")[1])
        self.assertEqual([a["slug"] for a in report["agencies"]], ["0-zero", "a-one", "b-two", "?"])
        self.assertNotIn("-lead", text)

    def test_not_a_tar_or_missing_file_refused(self):
        not_tar = self.tmp / "not.tar"
        not_tar.write_bytes(b"not a tar at all\n" * 64)
        for path in (not_tar, self.tmp / "absent.tar"):
            for extra in ((), ("--json",)):
                self.assertEqual(self.list(path, *extra, code=1), (1, "", "archive is not a readable uncompressed tar\n"))

    def test_list_writes_nothing(self):
        out, _ = self.make(FULL)
        before = sorted((p.as_posix(), p.stat().st_mtime_ns) for p in self.tmp.rglob("*"))
        self.list(out)
        self.list(out, "--json")
        self.assertEqual(sorted((p.as_posix(), p.stat().st_mtime_ns) for p in self.tmp.rglob("*")), before)


if __name__ == "__main__":
    unittest.main()
