"""FA53: an unchanged mail replay needs a preservation declaration per item.

Synthetic-only. Every fixture is generated under a fresh temporary root. The
missing-declaration ledgers are CONSTRUCTED row by row from a legitimate
template import (no row or file is deleted to manufacture them).

Test classes read the importer from ``MODULE`` so the acceptance helper can run
the same assertions against the pinned original module as a negative control.
"""
from email.message import EmailMessage
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import tempfile
import unittest

from campaign_tool.records.intake import folder, mail_delta

ATTACHMENTS = (b"%PDF-1.4\nfa53 synthetic attachment one\n",
               b"%PDF-1.4\nfa53 synthetic attachment two\n")
PRIOR = b"fa53 synthetic prior catalog record"
EXPECTED_REFUSAL = "existing_blob_invalid"
MISSING_CASES = ("original", "one_attachment", "multiple")
HELPER = Path(__file__).resolve().parents[2] / "tools" / "accept_fa53_replay_preservation.py"
# Set by the acceptance helper to a fresh per-invocation evidence directory so
# A3 before/after snapshots are retained; None (direct unittest runs) keeps
# them in memory only.
SNAPSHOT_SINK = None


def retain_snapshot(case_id, record):
    if SNAPSHOT_SINK is None:
        return None
    sink = Path(SNAPSHOT_SINK)
    sink.mkdir(mode=0o700, exist_ok=True)
    path = sink / (case_id + ".json")
    with open(path, "x") as handle:  # a duplicate case ID is an error, never an overwrite
        handle.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return path


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    return sha(Path(path).read_bytes())


class Fixture:
    """One synthetic export root plus helpers to build private intake outputs."""

    def __init__(self, base):
        self.base = Path(os.path.realpath(base))
        self.root = self.base / "export"
        self.root.mkdir(mode=0o700)
        (self.root / "mail").mkdir(mode=0o700)
        message = EmailMessage()
        message["From"] = "fa53@example.invalid"
        message["To"] = "recipient@example.invalid"
        message["Subject"] = "FA53 synthetic fixture"
        message.set_content("FA53 synthetic body")
        for n, data in enumerate(ATTACHMENTS):
            message.add_attachment(data, maintype="application", subtype="pdf",
                                   filename="fa53-%d.pdf" % n)
        self.eml = message.as_bytes()
        (self.root / "mail" / "message.eml").write_bytes(self.eml)
        attachments = []
        for n, data in enumerate(ATTACHMENTS):
            name = "fa53-%d.pdf" % n
            (self.root / "mail" / name).write_bytes(data)
            attachments.append({"part": str(n + 2), "path": "mail/" + name, "bytes": len(data),
                                "sha256": sha(data), "filename": name,
                                "original_filename": name, "content_type": "application/pdf"})
        self.receipt = {
            "account_id": "fa53-synthetic-account", "folder": "INBOX",
            "uidvalidity": 11, "uid": 501, "complete": True, "bytes": len(self.eml),
            "headers": {"subject": "ignored"}, "internaldate": "2026-01-01T00:00:00Z",
            "original_eml": {"path": "mail/message.eml", "bytes": len(self.eml),
                             "sha256": sha(self.eml)},
            "attachments": attachments,
        }
        self.sizes = {sha(self.eml): len(self.eml)}
        self.sizes.update({sha(data): len(data) for data in ATTACHMENTS})

    @property
    def eml_sha(self):
        return sha(self.eml)

    @property
    def attachment_shas(self):
        return [sha(data) for data in ATTACHMENTS]

    def missing(self, case):
        return {"original": [self.eml_sha],
                "one_attachment": [self.attachment_shas[0]],
                "multiple": [self.eml_sha] + self.attachment_shas}[case]

    def write_receipt(self, name, **changes):
        path = self.base / name
        path.write_text(json.dumps(dict(self.receipt, **changes)))
        return path

    def output(self, name):
        """Fresh owner-only intake v3.2 output with a completed baseline run."""
        out = self.base / name
        out.mkdir(mode=0o700)
        (out / "blobs").mkdir(mode=0o700)
        db_path = out / "intake.sqlite"
        db = sqlite3.connect(db_path)
        try:
            db.executescript(folder.SCHEMA)
            db_path.chmod(0o600)
            db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?)",
                       ("baseline", "before", "before", "inventory", "complete", "{}"))
            db.execute("INSERT INTO meta VALUES('inventory_run','baseline')")
            prior = sha(PRIOR)
            blob = out / "blobs" / prior
            blob.write_bytes(PRIOR)
            blob.chmod(0o400)
            db.execute("INSERT INTO docs(sha,bytes,format,stage,first_seen,review_status,version,digest) "
                       "VALUES(?,?,?,?,?,?,?,?)", (prior, len(PRIOR), "txt", "complete", "before",
                                                    "not_reviewed", folder.VERSION, "{}"))
            db.execute("INSERT INTO preservations VALUES(?,?,?,?)",
                       (prior, str(blob), "before", len(PRIOR)))
            db.execute("INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?,?,?)",
                       ("fa53-prior", prior, "prior.txt", str(self.root), None, "null",
                        "original", "{}", "before", "before"))
            db.execute("INSERT INTO seen VALUES(?,?,?)", ("baseline", "fa53-prior", prior))
            db.commit()
        finally:
            db.close()
        return out

    def construct(self, template, name, omit=(), wrong_size=(), skip_blobs=(), corrupt_blobs=()):
        """Build a completed-shaped output by INSERTING the template's rows.

        Preservation declarations for ``omit`` are never inserted; ``wrong_size``
        declarations are inserted one byte too large. Blobs in ``skip_blobs`` are
        never copied and ``corrupt_blobs`` receive different synthetic bytes.
        """
        out = self.base / name
        out.mkdir(mode=0o700)
        (out / "blobs").mkdir(mode=0o700)
        db_path = out / "intake.sqlite"
        source = sqlite3.connect(template / "intake.sqlite")
        target = sqlite3.connect(db_path)
        try:
            target.executescript(folder.SCHEMA)
            db_path.chmod(0o600)
            target.execute("PRAGMA foreign_keys=OFF")
            tables = [row[0] for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            for table in tables:
                rows = source.execute("SELECT * FROM %s" % table).fetchall()
                if table == "preservations":
                    rows = [(digest, str(out / "blobs" / digest), verified,
                             size + 1 if digest in wrong_size else size)
                            for digest, _, verified, size in rows if digest not in omit]
                for row in rows:
                    target.execute("INSERT INTO %s VALUES(%s)" % (table, ",".join("?" * len(row))), row)
            target.commit()
            if target.execute("PRAGMA foreign_key_check").fetchall():
                raise AssertionError("constructed fixture violates foreign keys")
        finally:
            source.close()
            target.close()
        for blob in sorted((template / "blobs").iterdir()):
            if blob.name in skip_blobs:
                continue
            dest = out / "blobs" / blob.name
            dest.write_bytes(b"fa53 corrupt synthetic bytes" if blob.name in corrupt_blobs
                             else blob.read_bytes())
            dest.chmod(0o400)
        lock = out / "writer.lock"
        lock.write_bytes(b"")
        lock.chmod(0o600)
        return out


def snapshot(out, *extra):
    """Every SQL row plus every output/receipt/source file hash and mode."""
    db = sqlite3.connect(out / "intake.sqlite")
    try:
        tables = {name: sorted(json.dumps(row) for row in db.execute("SELECT * FROM %s" % name))
                  for (name,) in db.execute(
                      "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")}
    finally:
        db.close()
    files = {}
    for path in [out] + [Path(p) for p in extra]:
        for item in ([path] if path.is_file() else sorted(path.rglob("*"))):
            mode = item.lstat()
            files[str(item)] = (stat.S_IMODE(mode.st_mode),
                                file_sha(item) if stat.S_ISREG(mode.st_mode) else None)
    return {"tables": tables, "files": files}


def attempt(module, receipt, root, out):
    """Outcome of the actual importer: ("replay"|"imported", result) or ("rejected", code)."""
    try:
        result = module.import_receipt(receipt, root, out)
    except module.Rejected as error:
        return "rejected", str(error)
    return result["status"], result


def missing_declaration_outcomes(module, base):
    """A4 driver: original-vs-candidate outcome for every missing-declaration case."""
    Path(base).mkdir(mode=0o700, exist_ok=True)
    fixture = Fixture(base)
    receipt = fixture.write_receipt("receipt.json")
    template = fixture.output("template")
    first = attempt(module, receipt, fixture.root, template)
    outcomes = {"template_import": first[0]}
    outcomes["fully_declared"] = attempt(
        module, receipt, fixture.root, fixture.construct(template, "declared"))[0:2]
    for case in MISSING_CASES:
        out = fixture.construct(template, "missing-" + case, omit=fixture.missing(case))
        status, detail = attempt(module, receipt, fixture.root, out)
        outcomes[case] = (status, detail if status == "rejected" else detail.get("run_id"))
    return {key: list(value) if isinstance(value, tuple) else value
            for key, value in outcomes.items()}


class _Base(unittest.TestCase):
    MODULE = mail_delta

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="fa53-")
        self.addCleanup(temp.cleanup)
        self.fx = Fixture(temp.name)
        self.receipt = self.fx.write_receipt("receipt.json")

    def run_import(self, out, receipt=None):
        return self.MODULE.import_receipt(receipt or self.receipt, self.fx.root, out)

    def active(self, out):
        db = sqlite3.connect(out / "intake.sqlite")
        try:
            return db.execute("SELECT value FROM meta WHERE key='inventory_run'").fetchone()[0]
        finally:
            db.close()

    def rows(self, out, sql, args=()):
        db = sqlite3.connect(out / "intake.sqlite")
        try:
            return db.execute(sql, args).fetchall()
        finally:
            db.close()

    def template(self):
        out = self.fx.output("template")
        result = self.run_import(out)
        self.assertEqual(result["status"], "imported")
        return out, result

    def assert_refused_unchanged(self, out, code, case_id, missing=()):
        before = snapshot(out, self.receipt, self.fx.root)
        outcome = list(attempt(self.MODULE, self.receipt, self.fx.root, out))
        after = snapshot(out, self.receipt, self.fx.root)
        # JSON round-trip so the retained record compares exactly like the in-memory one.
        before, after = json.loads(json.dumps(before)), json.loads(json.dumps(after))
        retain_snapshot(case_id, {
            "case_id": case_id, "test_id": self.id(), "module": self.MODULE.__name__,
            "module_file": self.MODULE.__file__, "expected_refusal": code, "outcome": outcome,
            "missing_declarations": sorted(missing), "before": before, "after": after,
            "unchanged": before == after})
        self.assertEqual(outcome, ["rejected", code])
        self.assertEqual(after, before)
        # Caller rollback: no transaction or writer lock lingers after refusal.
        db = sqlite3.connect(out / "intake.sqlite", timeout=0)
        try:
            db.execute("BEGIN IMMEDIATE")
            db.rollback()
        finally:
            db.close()


class A1ImportReplayTests(_Base):
    def test_a1_initial_import_declares_every_item_and_exact_replay_keeps_run(self):
        out, result = self.template()
        self.assertEqual((result["added_occurrences"], result["added_edges"]), (3, 2))
        self.assertNotEqual(result["run_id"], "baseline")
        declared = {digest: (path, size) for digest, path, _, size in
                    self.rows(out, "SELECT sha,path,verified_at,bytes FROM preservations")}
        for digest, size in self.fx.sizes.items():
            self.assertEqual(declared[digest], (str(out / "blobs" / digest), size))
            self.assertEqual(folder.verify_blob(digest, out)["bytes"], size)
        before = snapshot(out, self.receipt)
        replay = self.run_import(out)
        self.assertEqual(replay, dict(status="replay", added_occurrences=0, added_edges=0,
                                      run_id=result["run_id"]))
        self.assertEqual(snapshot(out, self.receipt), before)
        self.assertEqual(self.rows(out, "SELECT count(*) FROM runs")[0][0], 2)

    def test_a1_new_occurrence_sharing_content_keeps_identity_and_edges(self):
        out, first = self.template()
        docs = self.rows(out, "SELECT sha,bytes FROM docs ORDER BY sha")
        declared = self.rows(out, "SELECT * FROM preservations ORDER BY sha")
        edges = self.rows(out, "SELECT * FROM edges ORDER BY child")
        second_receipt = self.fx.write_receipt("receipt-65.json", uid=502)
        second = self.run_import(out, second_receipt)
        self.assertEqual((second["status"], second["added_occurrences"], second["added_edges"]),
                         ("imported", 3, 0))
        self.assertNotEqual(second["run_id"], first["run_id"])
        self.assertEqual(self.rows(out, "SELECT sha,bytes FROM docs ORDER BY sha"), docs)
        self.assertEqual(self.rows(out, "SELECT * FROM preservations ORDER BY sha"), declared)
        self.assertEqual(self.rows(out, "SELECT * FROM edges ORDER BY child"), edges)
        self.assertEqual({(parent, child) for parent, _, child, _ in edges},
                         {(self.fx.eml_sha, child) for child in self.fx.attachment_shas})
        self.assertEqual(self.rows(out, "SELECT count(*) FROM seen WHERE run=?",
                                   (second["run_id"],))[0][0], 7)
        self.assertEqual(self.run_import(out, second_receipt)["status"], "replay")
        self.assertEqual(self.run_import(out)["run_id"], second["run_id"])


class A2MissingDeclarationTests(_Base):
    def _missing(self, case):
        template, _ = self.template()
        out = self.fx.construct(template, "missing-" + case, omit=self.fx.missing(case))
        absent = set(self.fx.missing(case))
        present = {row[0] for row in self.rows(out, "SELECT sha FROM preservations")}
        self.assertFalse(absent & present)
        # Every other replay precondition is present and verified.
        for digest, size in self.fx.sizes.items():
            self.assertEqual(folder.verify_blob(digest, out)["bytes"], size)
        self.assertEqual(self.rows(out, "SELECT * FROM occurrences ORDER BY oid"),
                         self.rows(template, "SELECT * FROM occurrences ORDER BY oid"))
        self.assertEqual(self.rows(out, "SELECT * FROM seen ORDER BY run,oid"),
                         self.rows(template, "SELECT * FROM seen ORDER BY run,oid"))
        self.assertEqual(self.rows(out, "SELECT * FROM edges ORDER BY child"),
                         self.rows(template, "SELECT * FROM edges ORDER BY child"))
        active = self.active(out)
        self.assert_refused_unchanged(out, EXPECTED_REFUSAL, "a2-missing-" + case, absent)
        self.assertEqual(self.active(out), active)
        self.assertFalse(absent & {row[0] for row in self.rows(out, "SELECT sha FROM preservations")})

    def test_a2_missing_original_declaration_refused(self):
        self._missing("original")

    def test_a2_missing_one_attachment_declaration_refused(self):
        self._missing("one_attachment")

    def test_a2_missing_multiple_declarations_refused(self):
        self._missing("multiple")

    def test_a2_fully_declared_constructed_fixture_replays(self):
        template, first = self.template()
        out = self.fx.construct(template, "declared")
        before = snapshot(out, self.receipt, self.fx.root)
        result = self.run_import(out)
        self.assertEqual(result, dict(status="replay", added_occurrences=0, added_edges=0,
                                      run_id=first["run_id"]))
        self.assertEqual(snapshot(out, self.receipt, self.fx.root), before)

    def test_a2_wrong_size_declaration_keeps_size_conflict(self):
        template, _ = self.template()
        out = self.fx.construct(template, "wrong-size", wrong_size=[self.fx.attachment_shas[1]])
        self.assert_refused_unchanged(out, "preservation_size_conflict", "a2-wrong-size")


class A3ConsumerBoundaryTests(_Base):
    def test_a3_changed_import_still_declares_missing_preservation(self):
        template, first = self.template()
        missing = self.fx.attachment_shas[0]
        out = self.fx.construct(template, "changed", omit=[missing])
        result = self.run_import(out, self.fx.write_receipt("receipt-changed.json", uid=503))
        self.assertEqual(result["status"], "imported")
        self.assertNotEqual(result["run_id"], first["run_id"])
        self.assertEqual(self.rows(out, "SELECT path,bytes FROM preservations WHERE sha=?", (missing,)),
                         [(str(out / "blobs" / missing), self.fx.sizes[missing])])
        self.assertEqual(self.run_import(out)["status"], "replay")

    def test_a3_missing_and_corrupt_blob_refusals_preserved(self):
        template, _ = self.template()
        target = self.fx.attachment_shas[1]
        absent = self.fx.construct(template, "absent-blob", skip_blobs=[target])
        self.assert_refused_unchanged(absent, EXPECTED_REFUSAL, "a3-absent-blob")
        corrupt = self.fx.construct(template, "corrupt-blob", corrupt_blobs=[self.fx.eml_sha])
        self.assert_refused_unchanged(corrupt, EXPECTED_REFUSAL, "a3-corrupt-blob")

    def test_a3_outcome_driver_matches_frozen_behavior(self):
        outcomes = missing_declaration_outcomes(self.MODULE, Path(self.fx.base) / "driver")
        self.assertEqual(outcomes["template_import"], "imported")
        self.assertEqual(outcomes["fully_declared"][0], "replay")
        for case in MISSING_CASES:
            self.assertEqual(outcomes[case], ["rejected", EXPECTED_REFUSAL], case)


class HelperEvidenceRootTests(unittest.TestCase):
    """A5: the acceptance helper refuses unsafe evidence parents before writing anything."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("fa53_acceptance_helper", HELPER)
        cls.helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.helper)

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="fa53-root-")
        self.addCleanup(temp.cleanup)
        self.base = Path(os.path.realpath(temp.name))
        self.admitted = self.base / "candidate"
        self.repo = self.admitted / "repo"
        self.repo.mkdir(parents=True)
        (self.admitted / "real").mkdir()
        (self.admitted / "plain-file").write_bytes(b"")
        (self.admitted / "link-leaf").symlink_to(self.admitted / "real", target_is_directory=True)
        (self.admitted / "link-outside").symlink_to(self.base, target_is_directory=True)

    def check(self, raw):
        return self.helper.safe_output_parent(str(raw), admitted=self.admitted, repo=self.repo)

    def tree(self):
        # os.walk never follows the deliberately linked directories.
        return sorted((os.path.join(top, name), os.path.islink(os.path.join(top, name)))
                      for top, dirs, files in os.walk(self.base) for name in dirs + files)

    def test_in_root_parents_accepted(self):
        for raw in (self.admitted / "evidence", self.admitted / "real", self.admitted / "real" / "new"):
            self.assertEqual(self.check(raw), (raw, None), raw)

    def test_unsafe_parents_refused_without_writing(self):
        before = self.tree()
        refusals = {
            "evidence": "output_parent_not_absolute",
            self.base / "outside": "output_parent_outside_candidate",
            self.admitted: "output_parent_outside_candidate",
            str(self.admitted) + "/../escape": "output_parent_outside_candidate",
            str(self.admitted) + "-sibling/evidence": "output_parent_outside_candidate",
            self.repo: "output_parent_inside_repo",
            self.repo / "evidence": "output_parent_inside_repo",
            self.admitted / "link-leaf": "output_parent_linked:" + str(self.admitted / "link-leaf"),
            self.admitted / "link-leaf" / "evidence": "output_parent_linked:" + str(self.admitted / "link-leaf"),
            self.admitted / "link-outside" / "evidence":
                "output_parent_linked:" + str(self.admitted / "link-outside"),
            self.admitted / "plain-file": "output_parent_not_directory:" + str(self.admitted / "plain-file"),
            self.admitted / "plain-file" / "evidence":
                "output_parent_not_directory:" + str(self.admitted / "plain-file"),
            self.admitted / "missing" / "evidence": "output_parent_ancestor_missing",
        }
        for raw, problem in refusals.items():
            self.assertEqual(self.check(raw), (None, problem), raw)
        self.assertEqual(self.tree(), before)
        self.assertFalse((self.base / "outside").exists())

    def test_default_root_is_the_directory_containing_the_repository(self):
        helper = self.helper
        self.assertEqual(helper.ADMITTED_ROOT, HELPER.parents[2])
        token = secrets.token_hex(6)
        outside = helper.ADMITTED_ROOT.parent / ("fa53-outside-" + token)
        self.assertEqual(helper.safe_output_parent(str(outside)), (None, "output_parent_outside_candidate"))
        self.assertFalse(outside.exists())
        inside = helper.ADMITTED_ROOT / ("fa53-evidence-probe-" + token)
        self.assertEqual(helper.safe_output_parent(str(inside)), (inside, None))
        self.assertFalse(inside.exists())


del _Base

if __name__ == "__main__":
    unittest.main()
