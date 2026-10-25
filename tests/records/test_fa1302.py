"""FA1302 synthetic full-module regressions. Import has no application side effects."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests.fixtures.fa1302.boundary import Boundary, REPO, WORKER, child_env, checked_output

PREFLIGHT = '''            if (not isinstance(source.get("account"), str)
                    or not isinstance(source.get("folder"), str)):
                continue
'''


def malformed_sources():
    values = [None, 7, True, "scalar", [], ["account"], {}, {"account": "synthetic-adopt-account"}]
    for field in ("account", "folder"):
        for bad in ([], {}, True, None, 9):
            source = {"account": "synthetic-adopt-account", "folder": "Adoption", "uidvalidity": 900, "uid": 11}
            source[field] = bad
            values.append(source)
    return values


class Fixture(unittest.TestCase):
    def setUp(self):
        # The helper/CI must supply an inherited external temp root; no box paths.
        parent = checked_output(os.environ["TMPDIR"])
        if isinstance(self, (GateControl, Isolation)):
            # Control/child outputs are retained evidence, not fixture cleanup.
            self.base = Path(tempfile.mkdtemp(prefix="fa1302-controls-", dir=parent))
        else:
            self.tmp = tempfile.TemporaryDirectory(prefix="fa1302-", dir=parent)
            self.addCleanup(self.tmp.cleanup)
            self.base = Path(self.tmp.name)
        self.boundary = self.enterContext(Boundary(self.base).enter())
        from campaign_tool.records import mail_adoption
        from campaign_tool.records.run import Pipeline
        from tests.records.test_mail_adoption_regression import synthetic_receipt, _write_private
        from tests.records.test_mail_adoption_recovery import tree
        self.module, self.pipeline = mail_adoption, Pipeline
        self.producer, self.write_private, self.tree = synthetic_receipt, _write_private, tree
        self.number = 0

    def make(self, *, string_uids=False, attachments=0):
        self.number += 1
        base = self.base / ("fixture-%d" % self.number)
        base.mkdir(mode=0o700)
        root = self.pipeline(base / "processing").root.path
        export = base / "export"
        export.mkdir(mode=0o700)
        for uid in (11, 12):
            path, declaration = self.producer(export, uid=uid, attachments=attachments)
            if string_uids:
                declaration.update(uid=str(uid), uidvalidity="900")
                self.write_private(path, (json.dumps(declaration, sort_keys=True) + "\n").encode())
        config = base / "config.json"
        self.write_private(config, json.dumps({"schema": self.module.CONFIG_SCHEMA, "source": "mail-history",
                                               "adoption": "enabled", "disable_marker_sha256": None}).encode())
        fixture = {"base": base, "root": root, "export": export, "config": config,
                   "ledger": root / "ledger.sqlite"}
        report = self.adopt(fixture, apply=True)
        self.assertEqual((report["adopted_messages"], report["failures"]), (2, {}), report)
        self.assertFalse(self.module.blocking(report), report)
        return fixture

    def adopt(self, f, **kw):
        return self.module.adopt(f["root"], f["export"], config_path=f["config"], **kw)

    def classify(self, f):
        items, gaps = self.module.discover(f["export"], f["root"] / "intake")
        self.assertEqual(gaps, {})
        result, runs = self.module.classify_receipts(f["ledger"], items, f["root"] / "intake" / "blobs",
                                                    mail_root=f["export"])
        self.assertEqual(runs, [])
        self.assertEqual(set(result), {("synthetic-adopt-account", "Adoption", "900", str(uid)) for uid in (11, 12)})
        return result

    def mutate(self, f, source, *, expected=False, reverse=False):
        con = sqlite3.connect(f["ledger"])
        con.row_factory = sqlite3.Row
        try:
            rows = [dict(row) for row in con.execute("SELECT * FROM occurrences ORDER BY rowid")]
            mail = next(row for row in rows if row["id"] == self.module.message_id(
                ("synthetic-adopt-account", "Adoption", "900", "11")))
            raw = json.dumps(source)
            if expected:
                con.execute("UPDATE occurrences SET source_ref=? WHERE id=?", (raw, mail["id"]))
            else:
                other = dict(mail, id=hashlib.sha256(b"synthetic unrelated occurrence").hexdigest(), source_ref=raw)
                # Reverse table insertion order, not an inert mock/SQL seam.
                ordered = rows + [other] if reverse else [other] + rows
                con.execute("DELETE FROM occurrences")
                for row in ordered:
                    con.execute("INSERT INTO occurrences(%s) VALUES(%s)" %
                                (",".join(row), ",".join("?" * len(row))), tuple(row.values()))
            con.commit()
        finally:
            con.close()

    def intact(self, f, *, report=None):
        before = self.tree(f["base"])
        states = self.classify(f)
        self.assertEqual(set(states.values()), {("complete", None)})
        actual = self.adopt(f)
        self.assertEqual((actual["already_adopted"], actual["identity_conflicts"], actual["pending_messages"]), (2, {}, 0))
        self.assertFalse(self.module.blocking(actual))
        if report is not None:
            self.assertEqual(actual, report)
        self.assertEqual(self.tree(f["base"]), before)
        self.assertEqual(self.boundary.events, [])


class Behavior(Fixture):
    def test_valid_enrolled_control(self):
        self.intact(self.make())

    def test_list_account_peer_no_crash(self):
        f = self.make()
        report = self.adopt(f)
        self.mutate(f, {"account": [], "folder": "Adoption", "uidvalidity": 900, "uid": 11})
        self.intact(f, report=report)

    def test_unrelated_malformed_before_and_after_valid_peers(self):
        for reverse in (False, True):
            for value in malformed_sources():
                with self.subTest(reverse=reverse, source=value):
                    f = self.make()
                    report = self.adopt(f)
                    self.mutate(f, value, reverse=reverse)
                    self.intact(f, report=report)

    def test_expected_mail_id_is_blocking_not_complete(self):
        for value in malformed_sources():
            with self.subTest(source=value):
                f = self.make()
                self.mutate(f, value, expected=True)
                before = self.tree(f["base"])
                states = self.classify(f)
                wanted = ("synthetic-adopt-account", "Adoption", "900", "11")
                peer = (*wanted[:3], "12")
                self.assertEqual(states, {wanted: ("identity_occurrence_mismatch", None), peer: ("complete", None)})
                report = self.adopt(f)
                self.assertEqual((report["already_adopted"], report["identity_conflicts"], report["pending_messages"]),
                                 (1, {"identity_occurrence_mismatch": 1}, 0))
                self.assertTrue(self.module.blocking(report))
                self.assertEqual(self.tree(f["base"]), before)

    def test_integer_string_receipt_uids_and_reversed_order(self):
        for strings in (False, True):
            for reverse in (False, True):
                with self.subTest(strings=strings, reverse=reverse):
                    f = self.make(string_uids=strings)
                    report = self.adopt(f)
                    source = {"account": "unrelated", "folder": "Adoption", "uidvalidity": "900" if strings else 900,
                              "uid": "11" if strings else 11}
                    self.mutate(f, source, reverse=reverse)
                    self.intact(f, report=report)

    def test_occurrence_numeric_strings_retain_exact_binding_refusal(self):
        # Grouping normalizes numeric strings, but exact canonical source bytes do
        # not: preserve the existing identity_locator_changed backstop.
        f = self.make()
        self.mutate(f, {"account": "synthetic-adopt-account", "folder": "Adoption", "uidvalidity": "900",
                        "uid": "11", "mime_path": None}, expected=True)
        before = self.tree(f["base"])
        states = self.classify(f)
        self.assertEqual(states[("synthetic-adopt-account", "Adoption", "900", "11")],
                         ("identity_locator_changed", None))
        self.assertEqual(self.adopt(f)["identity_conflicts"], {"identity_locator_changed": 1})
        self.assertEqual(self.tree(f["base"]), before)

    def test_malformed_attachment_retains_attachment_set_backstop(self):
        f = self.make(attachments=1)
        con = sqlite3.connect(f["ledger"])
        try:
            con.execute("UPDATE occurrences SET source_ref=? WHERE kind='attachment' AND "
                        "json_extract(source_ref,'$.uid')=11", (json.dumps({"account": []}),))
            con.commit()
        finally:
            con.close()
        before = self.tree(f["base"])
        states = self.classify(f)
        self.assertEqual(states[("synthetic-adopt-account", "Adoption", "900", "11")],
                         ("identity_attachment_set_changed", None))
        self.assertEqual(self.adopt(f)["identity_conflicts"], {"identity_attachment_set_changed": 1})
        self.assertEqual(self.tree(f["base"]), before)


class GateControl(Fixture):
    def test_only_preflight_removed_exposes_exact_peer_error(self):
        path = Path(self.module.__file__)
        original = path.read_text()
        self.assertEqual(original.count(PREFLIGHT), 1, "negative control requires the exact frozen preflight")
        broken_path = self.base / "broken_mail_adoption.py"
        broken_path.write_text(original.replace(PREFLIGHT, "", 1))
        spec = importlib.util.spec_from_file_location("campaign_tool.records._fa1302_broken", broken_path)
        if spec is None or spec.loader is None:
            self.fail("negative_control_loader_missing")
        broken = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(broken)
        outcomes = {}
        for label, function, selector in (
            ("valid-broken-control", broken._occurrence_groups, "test_valid_enrolled_control"),
            ("broken-peer", broken._occurrence_groups, "test_list_account_peer_no_crash"),
            ("restored-peer", self.module._occurrence_groups, "test_list_account_peer_no_crash")):
            stream = io.StringIO()
            with patch.object(self.module, "_occurrence_groups", function):
                result = unittest.TextTestRunner(stream=stream, verbosity=2).run(Behavior(selector))
            text = stream.getvalue()
            outcomes[label] = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
                               "skips": len(result.skipped), "output": text}
            # Preserve actual outputs outside temporary fixture cleanup.
            root = self.base
            (root / (label + ".txt")).write_text(text)
            if label == "broken-peer":
                self.assertEqual((result.testsRun, len(result.errors), len(result.failures), len(result.skipped)), (1, 1, 0, 0))
                self.assertEqual(result.errors[0][0].id(), "tests.records.test_fa1302.Behavior.test_list_account_peer_no_crash")
                self.assertIn("TypeError: unhashable type: 'list'", result.errors[0][1])
                self.assertIn("if key in wanted:", result.errors[0][1])
            else:
                self.assertEqual((result.testsRun, len(result.errors), len(result.failures), len(result.skipped)), (1, 0, 0, 0))
        self.assertEqual(path.read_text(), original)
        root = self.base
        (root / "controls.json").write_text(json.dumps(outcomes, indent=2) + "\n")


class Isolation(Fixture):
    def test_fresh_file_child_uses_candidate_and_inherited_roots(self):
        output = self.base / "child"
        argv = [sys.executable, "-B", str(WORKER), "probe", str(output)]
        result = subprocess.run(argv,
                                cwd=REPO, env=child_env(output), capture_output=True, text=True, timeout=120)
        (self.base / "child-result.json").write_text(json.dumps({"argv": argv, "cwd": str(REPO),
            "exit": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
            "stdout_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
            "stderr_sha256": hashlib.sha256(result.stderr.encode()).hexdigest()}, indent=2) + "\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        probe = json.loads((output / "probe.json").read_text())
        self.assertEqual(Path(probe["module"]), REPO / "campaign_tool/records/mail_adoption.py")
        self.assertEqual(probe["temp"], str(output / "tmp"))
        self.assertTrue(probe["bytecode_disabled"])
        self.assertEqual(probe["events"], [])

    def test_output_symlink_ancestry_refused_before_writes(self):
        target = self.base / "target"
        target.mkdir()
        alias = self.base / "alias"
        alias.symlink_to(target, target_is_directory=True)
        for path in (alias, alias / "new" / "leaf"):
            with self.assertRaisesRegex(ValueError, "symlink_output_ancestry"):
                checked_output(path)
        self.assertEqual(list(target.iterdir()), [])

    def test_scoped_boundary_restores_peer_process_state(self):
        previous = (os.environ["TMPDIR"], tempfile.tempdir, sys.dont_write_bytecode,
                    __import__("socket").create_connection, sys.getprofile())
        nested = self.base / "nested"
        guard = Boundary(nested)
        with guard.enter():
            from campaign_tool.records import mail_adoption
            self.assertEqual(Path(mail_adoption.__file__), REPO / "campaign_tool/records/mail_adoption.py")
            self.assertEqual(tempfile.gettempdir(), str(nested / "tmp"))
            self.assertTrue(sys.dont_write_bytecode)
        self.assertEqual((os.environ["TMPDIR"], tempfile.tempdir, sys.dont_write_bytecode,
                          __import__("socket").create_connection, sys.getprofile()), previous)
        self.assertEqual(guard.events, [])
        self.assertEqual(self.boundary.events, [])
