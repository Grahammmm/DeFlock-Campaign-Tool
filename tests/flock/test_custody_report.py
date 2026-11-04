"""BF-df-custody-report: tools/custody_report.py writes one private custody page per published finding from recorded fields."""
import ast
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "custody_report.py"
sys.path.insert(0, str(ROOT))
from campaign_tool.findings import reviewed_hash  # noqa: E402

spec = importlib.util.spec_from_file_location("_custody_test", TOOL)
MOD = importlib.util.module_from_spec(spec)
spec.loader.exec_module(MOD)

# Synthetic planted values: none of them may reach a generated file.
NAME, EMAIL, PHONE, PLATE = "Marisol Quintanilla-Verhoeven", "mq.records@example.org", "(805) 555-0142", "7QXR482"
QUOTE = "the department keeps every scanned plate for ninety days"
PLANTED = (NAME, "Quintanilla", EMAIL, PHONE, PLATE, QUOTE)
BANNED = re.compile(r"violat|unlawful|illegal|non-?complian|breach", re.I)
ESTABLISH = ("The SHA-256 hashes on this page establish byte identity, not authenticity.",
             "They show only that the campaign has held these exact bytes since the recorded time.",
             "This page does not say that any document is genuine, complete or correctly understood.",
             "It is not a claim that any agency broke a law.")


def finding(fid, body="Synthetic fixture body. " + QUOTE, summary=None):
    return {"id": fid, "title": "Finding %s about %s" % (fid, NAME), "slug": fid.replace("_", "-"),
            "summary": summary or "Synthetic summary; contact %s or %s, plate %s." % (EMAIL, PHONE, PLATE),
            "body_md": body, "classification": "documented_fact", "confidence": "verified", "event_date": "2026-01-15",
            "agency_id": "ca-cedar-police", "sources": [{"sha256": sha(fid), "locator": "page:1-2", "title": QUOTE}],
            "published_at": "2026-02-01T18:00:00Z", "updated_at": None, "corrections": [], "state": "published",
            "author": "author-a", "limitations": ["Synthetic: \"%s\"" % QUOTE], "counterevidence": []}


def data(fid):
    return ("synthetic bytes for %s" % fid).encode()


def sha(fid):
    return hashlib.sha256(data(fid)).hexdigest()


def approvals(digest, extra=()):
    base = [("reviewer-b", "factual"), ("reviewer-c", "legal"), ("reviewer-b", "privacy")]
    return [{"content_sha256": digest, "role": role, "decision": "approve", "reviewer": who,
             "rationale": "Checked against %s; nothing violated. %s" % (EMAIL, QUOTE),
             "reviewed_at": "2026-01-30T10:00:00Z"} for who, role in base] + list(extra)


class Store:
    """A synthetic campaign store: content/, private/ledger.sqlite, private/objects, reviews and the publication log."""

    def __init__(self, root):
        self.root = root
        for part in ("content/findings", "private/objects", "private/reviews"):
            (root / part).mkdir(parents=True)
        db = sqlite3.connect(root / "private" / "ledger.sqlite")
        db.executescript("CREATE TABLE objects (sha256 TEXT PRIMARY KEY, byte_count INTEGER NOT NULL, stored_at TEXT NOT NULL);"
                         "CREATE TABLE receipts (receipt_id TEXT PRIMARY KEY, object_sha256 TEXT NOT NULL, source_id TEXT NOT NULL,"
                         " received_at TEXT NOT NULL, original_name TEXT NOT NULL);")
        db.commit()
        db.close()
        self.library, self.log = [], []

    def sql(self, statement, values):
        db = sqlite3.connect(self.root / "private" / "ledger.sqlite")
        with db:
            db.execute(statement, values)
        db.close()

    def add(self, doc, *, ledger=True, receipt=True, stored=None, reviews=None, logged=None, note="hash verified"):
        fid, digest = doc["id"], sha(doc["id"])
        (self.root / "content" / "findings" / (doc["slug"] + ".json")).write_text(json.dumps(doc), encoding="utf-8")
        (self.root / "private" / "objects" / digest).write_bytes(data(fid) if stored is None else stored)
        if ledger:
            self.sql("INSERT INTO objects VALUES (?, ?, ?)", (digest, len(data(fid)), "2026-01-20T16:31:00Z"))
        if receipt:
            self.sql("INSERT INTO receipts VALUES (?, ?, ?, ?, ?)",
                     ("rcpt-" + fid, digest, "req-2026-" + fid[-1], "2026-01-20T16:31:00Z", NAME + " letter.pdf"))
        self.library.append({"sha256": digest, "title": "Letter from " + NAME, "agency_id": "ca-cedar-police",
                             "received_at": "2026-01-20T16:30:00+00:00", "public_url": None, "fragment": "cedar", "note": note})
        (self.root / "content" / "sources.json").write_text(json.dumps(self.library), encoding="utf-8")
        current = reviewed_hash(doc)
        receipts = approvals(current) if reviews is None else reviews
        (self.root / "private" / "reviews" / (fid + ".json")).write_text(json.dumps(receipts), encoding="utf-8")
        self.log.append({"at": "2026-02-01T18:00:00Z", "action": "publish", "id": fid, "slug": doc["slug"],
                         "content_sha256": current if logged is None else logged, "approved_by": "G"})
        (self.root / "private" / "publication-log.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in self.log), encoding="utf-8")
        return current


def tree_hash(root):
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        digest.update(str(path.relative_to(root)).encode() + b"\0" + (path.read_bytes() if path.is_file() else b"dir"))
    return digest.hexdigest()


def run(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = MOD.main(list(argv))
    return code, out.getvalue()


class CustodyReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.store = Store(self.base / "store")
        self.outs = self.base / "outs"
        self.outs.mkdir()
        self.n = 0

    def tearDown(self):
        self.tmp.cleanup()

    def four(self):
        self.store.add(finding("fnd_a"))
        self.store.add(finding("fnd_b"), stored=b"bytes altered after the ledger recorded them")
        self.store.add(finding("fnd_c"), receipt=False)
        older = reviewed_hash(finding("fnd_d", body="An earlier version of the body."))
        self.store.add(finding("fnd_d", body="Reviewed and approved by G. " + QUOTE), reviews=approvals(older))

    def build(self, fid, expect=None):
        self.n += 1
        out = self.outs / ("o%d" % self.n)
        code, printed = run("build", "--store", str(self.store.root), "--finding", fid, "--out", str(out))
        if expect is not None:
            self.assertEqual(code, expect, printed)
        doc = json.loads((out / "custody.json").read_text(encoding="utf-8"))
        return doc, (out / "CUSTODY.md").read_text(encoding="utf-8"), out

    def sections_drawn(self, doc, page):
        self.assertEqual(doc["schema"], "custody-report-v1")
        self.assertIs(doc["review_required"], True)
        self.assertEqual(sorted(doc["sections"]), ["establishes", "finding", "receipt_chain", "review", "sources"])
        for heading in ("## 1. The finding", "## 2. Its sources", "## 3. Receipt chain", "## 4. Review",
                        "## 5. What this does and does not establish"):
            self.assertIn(heading, page)

    # 1. the four chains
    def test_complete_chain(self):
        self.four()
        doc, page, out = self.build("fnd_a", expect=0)
        self.sections_drawn(doc, page)
        self.assertEqual(doc["verdict"], {"state": "chain complete", "first_missing": None, "missing": []})
        self.assertEqual(doc["sections"]["sources"][0]["rehash"], "matches")
        chain = doc["sections"]["receipt_chain"][0]
        self.assertEqual(chain["received"], {"state": "receipt", "received_at": "2026-01-20T16:30:00+00:00",
                                             "agency": "ca-cedar-police"})
        self.assertEqual(chain["preservation"]["receipts"],
                         [{"receipt_id": "rcpt-fnd_a", "request_id": "req-2026-a", "preserved_at": "2026-01-20T16:31:00Z"}])
        self.assertEqual(chain["contact_role"], "not-recorded")
        review = doc["sections"]["review"]
        self.assertEqual((review["counted_reviews"], review["owed"]), (3, []))
        self.assertEqual(sorted(r["checks"][0] for r in review["receipts"]), ["factual review", "legal review", "privacy review"])
        self.assertEqual({r["status"] for r in review["receipts"]}, {"current"})
        self.assertIn("**Verdict:** chain complete", page)
        self.assertEqual(oct(out.stat().st_mode & 0o777), "0o700")
        for name in ("CUSTODY.md", "custody.json"):
            self.assertEqual(oct((out / name).stat().st_mode & 0o777), "0o600")

    def test_source_no_longer_rehashes(self):
        self.four()
        doc, page, _ = self.build("fnd_b", expect=1)
        self.sections_drawn(doc, page)
        self.assertEqual(doc["verdict"]["first_missing"], "source S1: re-hash differs")
        self.assertEqual(doc["sections"]["sources"][0]["problem"], "size_mismatch")
        self.assertIn("**Verdict:** chain incomplete — source S1: re-hash differs", page)

    def test_missing_preservation_receipt(self):
        self.four()
        doc, page, _ = self.build("fnd_c", expect=1)
        self.sections_drawn(doc, page)
        self.assertEqual(doc["verdict"]["missing"], ["receipt chain S1: preservation no-receipt"])
        self.assertIn("- S1 preservation: no-receipt", page)

    def test_stale_review_never_counted(self):
        self.four()
        doc, page, _ = self.build("fnd_d", expect=1)
        self.sections_drawn(doc, page)
        self.assertEqual(doc["verdict"]["missing"], ["review: only a stale review (content changed after review)"])
        review = doc["sections"]["review"]
        self.assertEqual(review["counted_reviews"], 0)
        self.assertEqual({r["status"] for r in review["receipts"]}, {"stale review (content changed after review)"})
        self.assertIn("missing_factual_review", review["owed"])

    # 2. drifted
    def test_drifted_never_complete(self):
        self.store.add(finding("fnd_e"), logged="0" * 64)
        doc, _, _ = self.build("fnd_e", expect=1)
        self.assertEqual(doc["sections"]["finding"]["drifted"], "yes")
        self.assertEqual(doc["verdict"]["missing"], ["finding: drifted (content changed after its publication record)"])

    def test_no_publication_record_is_unknown_not_complete(self):
        self.store.add(finding("fnd_e"))
        (self.store.root / "private" / "publication-log.jsonl").write_text("", encoding="utf-8")
        doc, _, _ = self.build("fnd_e", expect=1)
        self.assertEqual(doc["sections"]["finding"]["drifted"], "unknown")
        self.assertEqual(doc["verdict"]["first_missing"], "finding: no publication record")

    def test_correction_entry_rebinds_publication_record(self):
        old, new = finding("fnd_e", body="Before the correction."), finding("fnd_e")
        self.store.add(new, logged=reviewed_hash(old))
        doc, _, _ = self.build("fnd_e", expect=1)
        self.assertEqual(doc["sections"]["finding"]["drifted"], "yes")
        self.store.log.append({"at": "2026-02-03T09:00:00Z", "action": "correct", "id": "fnd_e", "slug": "fnd-e",
                               "content_sha256": reviewed_hash(new), "text_changed": True})
        (self.store.root / "private" / "publication-log.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in self.store.log), encoding="utf-8")
        doc, _, _ = self.build("fnd_e", expect=0)
        self.assertEqual(doc["sections"]["finding"]["drifted"], "no")
        self.assertEqual(doc["sections"]["finding"]["publication_record"], "correct at 2026-02-03T09:00:00Z")

    # 3. stale plus current
    def test_stale_and_current_counted_once(self):
        doc_f = finding("fnd_f")
        stale = dict(approvals("1" * 64)[0], reviewer="reviewer-d")
        self.store.add(doc_f, reviews=approvals(reviewed_hash(doc_f), extra=[stale]))
        doc, page, _ = self.build("fnd_f", expect=0)
        review = doc["sections"]["review"]
        self.assertEqual(review["counted_reviews"], 3)
        self.assertEqual([r["reviewer"] for r in review["receipts"] if r["status"] != "current"], ["reviewer-d"])
        self.assertIn("- stale review (content changed after review): reviewer-d", page)

    def test_current_rejection_is_not_a_review(self):
        doc_f = finding("fnd_f")
        digest = reviewed_hash(doc_f)
        reject = dict(approvals(digest)[0], decision="reject", reviewer="reviewer-d")
        self.store.add(doc_f, reviews=approvals(digest, extra=[reject]))
        doc, _, _ = self.build("fnd_f", expect=1)
        self.assertEqual(doc["sections"]["review"]["counted_reviews"], 3)
        self.assertEqual(doc["verdict"]["first_missing"], "review: owed unresolved_challenge")

    def test_no_review_file(self):
        self.store.add(finding("fnd_f"), reviews=[])
        doc, _, _ = self.build("fnd_f", expect=1)
        self.assertEqual(doc["verdict"]["first_missing"], "review: no current approving review receipt")

    # 4. not-in-ledger and not-checked
    def test_not_in_ledger_and_not_checked_are_distinct(self):
        self.store.add(finding("fnd_g"), ledger=False, receipt=False)
        doc, _, _ = self.build("fnd_g", expect=1)
        self.assertEqual(doc["sections"]["sources"][0]["rehash"], "not-in-ledger")
        self.assertEqual(doc["verdict"]["first_missing"], "source S1: re-hash not-in-ledger")
        (self.store.root / "private" / "ledger.sqlite").unlink()
        doc, _, _ = self.build("fnd_g", expect=1)
        self.assertEqual(doc["sections"]["sources"][0]["rehash"], "not-checked")
        self.assertEqual(doc["verdict"]["first_missing"], "source S1: re-hash not-checked")
        self.assertIn("ledger: private/ledger.sqlite missing; no source was re-hashed", doc["source_errors"])

    def test_missing_library_entry_names_received_step(self):
        self.store.add(finding("fnd_g"))
        (self.store.root / "content" / "sources.json").write_text("[]", encoding="utf-8")
        doc, _, _ = self.build("fnd_g", expect=1)
        self.assertEqual(doc["verdict"]["missing"], ["receipt chain S1: received no-receipt"])

    def test_no_sources_is_not_complete(self):
        doc_h = dict(finding("fnd_h"), sources=[])
        self.store.add(doc_h)
        doc, page, _ = self.build("fnd_h", expect=1)
        self.assertEqual(doc["verdict"]["first_missing"], "sources: none recorded")
        self.assertIn("- none recorded: not established", page)

    # 5. prose bypass
    def test_prose_changes_no_state(self):
        self.store.add(finding("fnd_i", body="Reviewed and approved by G; hash verified."), ledger=False, reviews=[],
                       note="hash verified and approved")
        doc, _, _ = self.build("fnd_i", expect=1)
        self.assertEqual(doc["sections"]["sources"][0]["rehash"], "not-in-ledger")
        self.assertEqual(doc["sections"]["review"]["counted_reviews"], 0)
        self.assertIn("review: no current approving review receipt", doc["verdict"]["missing"])

    # 6. and 7. privacy, wording
    def test_eight_files_carry_no_personal_value_quote_or_violation_word(self):
        self.four()
        outs = [self.build(fid)[2] for fid in ("fnd_a", "fnd_b", "fnd_c", "fnd_d")]
        files = [p for out in outs for p in sorted(out.iterdir())]
        self.assertEqual(len(files), 8)
        for path in files:
            text = path.read_text(encoding="utf-8")
            for value in PLANTED:
                self.assertNotIn(value, text, path.name)
            self.assertIsNone(BANNED.search(text), path.name)
            for match in re.finditer("authentic", text):
                line = text[text.rfind("\n", 0, match.start()) + 1:text.find("\n", match.start())]
                self.assertIn("not authenticity", line)
        page = (outs[0] / "CUSTODY.md").read_text(encoding="utf-8")
        for sentence in ESTABLISH:
            self.assertIn(sentence, page)
        scan = subprocess.run([sys.executable, "-B", str(ROOT / "tools" / "flock_privacy_scan.py"), *map(str, outs)],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(scan.returncode, 0, scan.stdout + scan.stderr)
        self.assertIn("findings=0", scan.stdout)

    def test_personal_value_in_an_id_field_is_withheld_or_refused(self):
        doc_j = finding("fnd_j")
        self.store.add(doc_j, reviews=approvals(reviewed_hash(doc_j), extra=[dict(approvals("2" * 64)[0], reviewer=EMAIL)]))
        doc, page, _ = self.build("fnd_j", expect=0)
        self.assertNotIn(EMAIL, page)
        self.assertIn("withheld", [r["reviewer"] for r in doc["sections"]["review"]["receipts"]])
        self.store.add(dict(finding("fnd_k"), id="7QXR482"))
        code, printed = run("build", "--store", str(self.store.root), "--finding", "7QXR482", "--out", str(self.outs / "plate"))
        self.assertEqual((code, printed.strip()), (2, "custody refused: generated_text_failed_privacy_check"))
        self.assertFalse(os.path.lexists(self.outs / "plate"))

    # 8. positive control: a stray copy fails the public scan, by name
    def test_stray_copy_fails_public_tree_scan(self):
        repo = self.base / "public"
        (repo / "docs").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=60)
        (repo / "docs" / "ok.md").write_text("fine\n", encoding="utf-8")
        cmd = [sys.executable, "-B", str(ROOT / "tools" / "check_public_tree.py"), "--root", str(repo)]
        clean = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
        for name in ("CUSTODY.md", "custody.json"):
            (repo / "docs" / name).write_text("{}\n", encoding="utf-8")
        dirty = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        self.assertEqual(dirty.returncode, 1)
        found = json.loads(dirty.stdout)["findings"]
        self.assertEqual(sorted(found), [["docs/CUSTODY.md", 0, "private_path"], ["docs/custody.json", 0, "private_path"]])

    # 9. OUT refusals
    def test_out_refusals_create_nothing(self):
        self.store.add(finding("fnd_a"))
        (self.store.root / "agencies").mkdir()
        (self.outs / "taken").mkdir()
        real = self.base / "real"
        real.mkdir()
        (self.base / "link").symlink_to(real, target_is_directory=True)
        cases = {str(self.outs / "taken"): "out_exists", str(ROOT / "custody-out-test"): "out_location_refused",
                 str(self.store.root / "agencies" / "x"): "out_location_refused",
                 str(self.store.root / "x"): "out_location_refused",
                 str(self.base / "link" / "x"): "out_parent_unavailable",
                 str(self.outs / "no" / "x"): "out_parent_unavailable",
                 "relative/x": "absolute_normalized_out_required",
                 str(self.outs) + "/a/../b": "absolute_normalized_out_required"}
        before = tree_hash(self.base)
        for out, reason in cases.items():
            with self.subTest(out=out):
                code, printed = run("build", "--store", str(self.store.root), "--finding", "fnd_a", "--out", out)
                self.assertEqual((code, printed.strip()), (2, "custody refused: " + reason))
        self.assertEqual(tree_hash(self.base), before)
        self.assertFalse(os.path.lexists(ROOT / "custody-out-test"))
        self.assertEqual(sorted(p.name for p in real.iterdir()), [])

    def test_usage_and_unknown_finding(self):
        self.store.add(finding("fnd_a"))
        for argv in (("build", "--store", str(self.store.root), "--finding", "nope", "--out", str(self.outs / "u")),
                     ("build", "--store", str(self.store.root), "--out", str(self.outs / "u")),
                     ("build", "--store", "relative", "--finding", "fnd_a", "--out", str(self.outs / "u")),
                     ("status", "--store", str(self.store.root), "--finding", "fnd_a"), ("delete",)):
            with self.subTest(argv=argv):
                self.assertEqual(run(*argv)[0], 2)
        self.assertFalse(os.path.lexists(self.outs / "u"))
        dup = dict(finding("fnd_a"), slug="fnd-a-copy")
        (self.store.root / "content" / "findings" / "fnd-a-copy.json").write_text(json.dumps(dup), encoding="utf-8")
        code, printed = run("build", "--store", str(self.store.root), "--finding", "fnd_a", "--out", str(self.outs / "u"))
        self.assertEqual((code, printed.strip()), (2, "custody refused: finding_not_published_once"))

    def test_status_lists_every_published_finding(self):
        self.four()
        draft = dict(finding("fnd_z"), state="draft", slug="fnd-z")
        (self.store.root / "content" / "findings" / "fnd-z.json").write_text(json.dumps(draft), encoding="utf-8")
        code, printed = run("status", "--store", str(self.store.root))
        self.assertEqual(code, 1)
        self.assertEqual(printed.splitlines(), [
            "custody | fnd_a | chain complete",
            "custody | fnd_b | chain incomplete — source S1: re-hash differs",
            "custody | fnd_c | chain incomplete — receipt chain S1: preservation no-receipt",
            "custody | fnd_d | chain incomplete — review: only a stale review (content changed after review)",
            "custody | published=4 complete=1 | private, review required"])

    # 10. store byte-identical
    def test_store_byte_identical_after_every_run(self):
        self.four()
        before = tree_hash(self.store.root)
        for fid in ("fnd_a", "fnd_b", "fnd_c", "fnd_d"):
            self.build(fid)
        run("status", "--store", str(self.store.root))
        self.assertEqual(tree_hash(self.store.root), before)

    def test_checkpointed_wal_ledger_gains_no_sidecar(self):
        self.four()
        ledger = self.store.root / "private" / "ledger.sqlite"
        db = sqlite3.connect(ledger)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.close()
        self.assertEqual(ledger.read_bytes()[18:20], b"\2\2")
        self.assertEqual(sorted(p.name for p in ledger.parent.glob("ledger.sqlite*")), ["ledger.sqlite"])
        before = tree_hash(self.store.root)
        doc, _, _ = self.build("fnd_a", expect=0)
        self.assertEqual(doc["sections"]["sources"][0]["rehash"], "matches")
        run("status", "--store", str(self.store.root))
        self.assertEqual(sorted(p.name for p in ledger.parent.glob("ledger.sqlite*")), ["ledger.sqlite"])
        self.assertEqual(tree_hash(self.store.root), before)

    def test_live_sidecar_is_a_named_error_not_an_open(self):
        self.store.add(finding("fnd_a"))
        wal = self.store.root / "private" / "ledger.sqlite-wal"
        wal.write_bytes(b"")
        before = tree_hash(self.store.root)
        doc, _, _ = self.build("fnd_a", expect=1)
        self.assertEqual(doc["source_errors"], ["ledger: re-hash check could not run"])
        self.assertEqual(doc["sections"]["sources"][0]["rehash"], "not-checked")
        self.assertFalse(os.path.lexists(str(wal)[:-4] + "-shm"))
        self.assertEqual(tree_hash(self.store.root), before)

    def test_recorded_request_id_shown_unless_personal(self):
        doc_m = finding("fnd_m")
        self.store.add(doc_m, receipt=False)
        long_id = "PRA 2026/0117 " + "x" * 80
        for n, source in enumerate(("PRA 2026/0117 cedar police", long_id, "reply from " + EMAIL, "a\nb")):
            self.store.sql("INSERT INTO receipts VALUES (?, ?, ?, ?, ?)",
                           ("rcpt-m%d" % n, sha("fnd_m"), source, "2026-01-20T16:31:00Z", "x.pdf"))
        doc, page, _ = self.build("fnd_m", expect=0)
        shown = [r["request_id"] for r in doc["sections"]["receipt_chain"][0]["preservation"]["receipts"]]
        self.assertEqual(shown, ["PRA 2026/0117 cedar police", long_id, "withheld", "withheld"])
        self.assertIn("answering PRA 2026/0117 cedar police", page)
        self.assertNotIn(EMAIL, page)

    # 11. static
    def test_static_no_network_subprocess_store_write_or_site_build(self):
        source = TOOL.read_text(encoding="utf-8")
        names = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                names |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module or "")
        for banned in ("socket", "urllib", "http", "requests", "subprocess", "smtplib", "ftplib"):
            self.assertFalse(any(n == banned or n.startswith(banned + ".") for n in names), banned)
        self.assertFalse(any("site" in n or "build" in n for n in names), names)
        self.assertNotIn("subprocess", source)
        self.assertEqual(source.count("os.open("), 1)
        self.assertEqual(source.count(".mkdir("), 1)
        self.assertNotRegex(source, r"write_text|write_bytes|\.unlink\(|\.rename\(|os\.replace|shutil")

    def test_help(self):
        result = subprocess.run([sys.executable, "-B", str(TOOL), "--help"], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0)
        self.assertIn("build --store STORE --finding ID --out NEWDIR", result.stdout)


if __name__ == "__main__":
    unittest.main()
