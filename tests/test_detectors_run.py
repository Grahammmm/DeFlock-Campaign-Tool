"""F-042 detector runner regressions: synthetic inputs only, no real stores.

Every fixture is built at test time inside a fresh private temporary root:
one synthetic agency, three record types (retention-setting, sharing-list and
policy support, plus the producer's structural header row), and two WP6 rules.
The canonical copy is a fully synthetic v001 ledger in rollback-journal mode.
No records content, services, network, shared checkout or canonical ledger.
"""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from campaign_tool.records.detectors import core, persistence, pipeline
from campaign_tool.records.detectors.core import DETECTORS, canonical, digest
from campaign_tool.records.ledger import store
from campaign_tool.records.ledger.migrations import v001
from tools import detectors_run


def h(value):
    return hashlib.sha256(value.encode()).hexdigest()


AGENCY = "synthetic-agency"
RUN_ID = "synthetic-f042-producer-run-1"
SUBJECT = h("synthetic F-042 csv original")
POLICY = h("synthetic F-042 policy original")
EXPECTED_STATUS = {
    "search-before-training": ("inapplicable", "no_matching_type_in_complete_normalization"),
    "purpose-quality": ("inapplicable", "no_matching_type_in_complete_normalization"),
    "external-sharing": ("evaluated", "supplied_context_evaluated"),
    "retention-over-policy": ("evaluated", "supplied_context_evaluated"),
    "audit-gap": ("inapplicable", "no_matching_type_in_complete_normalization"),
    "cpra-deadline": ("inapplicable", "no_matching_type_in_complete_normalization"),
    "volume-anomaly": ("inapplicable", "no_matching_type_in_complete_normalization"),
}


def observed(value):
    return {"state": "observed", "value": value}


def inventory():
    """Coordinator-supplied source_bindings (the authoritative inventory)."""
    rows = [("unit-csv-0001", SUBJECT, {"row": 1}), ("unit-csv-0002", SUBJECT, {"row": 2}),
            ("unit-csv-0003", SUBJECT, {"row": 3}), ("unit-policy-0001", POLICY, {"page": 1})]
    return [{"unit_id": uid, "original_sha256": original, "locator": locator,
             "artifact_sha256": h("synthetic extracted unit " + uid)} for uid, original, locator in rows]


def producer_output(sources):
    """F-040a-shaped producer output (rows.normalize result keys and record shape)."""
    by_id = {row["unit_id"]: row for row in sources}
    records = {
        "unit-csv-0001": {"original_sha256": SUBJECT, "locator": {"row": 1}, "record_type": "other",
                          "event_id": "unit-csv-0001", "event_at": None, "fields": {}},
        "unit-csv-0002": {"original_sha256": SUBJECT, "locator": {"row": 2}, "record_type": "retention-setting",
                          "event_id": "retention-1", "event_at": "2020-01-20T12:00:00Z",
                          "fields": {"retention_mode": observed("days"), "retention_days": observed(31)}},
        "unit-csv-0003": {"original_sha256": SUBJECT, "locator": {"row": 3}, "record_type": "sharing-list",
                          "event_id": "sharing-1", "event_at": "2020-01-20T12:00:00Z",
                          "fields": {"access_enabled": observed(True), "recipient_level": observed("federal"),
                                     "recipient_jurisdiction": observed("ZZ")}},
        "unit-policy-0001": {"original_sha256": POLICY, "locator": {"page": 1}, "record_type": "policy",
                             "event_id": "unit-policy-0001", "event_at": None, "fields": {}},
    }
    units = [{"record": records[uid], "source": {"unit_id": uid, "artifact_sha256": by_id[uid]["artifact_sha256"]}}
             for uid in sorted(records)]
    return {
        "normalized": {"schema": pipeline.NORMALIZED_SCHEMA, "version": "explicit-csv-text-rows-v1",
                       "complete": True, "source_inventory_sha256": digest(sources), "units": units},
        # The producer echoes the inventory it was given; the runner must
        # never treat this echo as the coordinator's authoritative inventory.
        "source_bindings": sources, "run_id": RUN_ID,
        "mapping_sha256": h("synthetic mapping"),
        "source_receipts": [{"artifact_sha256": SUBJECT, "derived_receipt_sha256": h("synthetic receipt")}],
        "diagnostics": [{"unit_id": uid, "agency_id": AGENCY, "reasons": [], "cells": {}, "status": "mapped"}
                        for uid in sorted(records) if uid.startswith("unit-csv")],
        "events": [], "upstream": [], "review_required": True, "qualified": False,
        "activation_allowed": False, "publication_ready": False, "stage_promotions": 0,
    }


def rules():
    common = {"agency_id": AGENCY, "source_sha256": POLICY, "effective_from": "2020-01-01",
              "effective_to": "2021-01-01", "version": "synthetic-v1",
              "version_verified": True, "applicability": "yes"}
    return {"version": "synthetic-f042-pack-v1", "entries": [
        dict(common, detector="retention-over-policy", rule_id="synthetic-retention",
             parameters={"maximum_days": 30, "unit": "calendar_days"}),
        dict(common, detector="external-sharing", rule_id="synthetic-sharing",
             parameters={"home_jurisdiction": "AA"}),
    ]}


def build_canonical(path, *, originals=(SUBJECT, POLICY), evidence=None, wal=False, checksum=None):
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA journal_mode=DELETE")
        con.executescript(v001.SQL)
        con.execute("INSERT INTO schema_migrations VALUES(?,?)", (v001.VERSION, checksum or store.SCHEMA_HASH))
        con.execute("INSERT INTO ledger_meta VALUES('acceptance','candidate_unreconciled')")
        for original in originals:
            con.execute("INSERT INTO originals VALUES(?,1,'text/plain',NULL,'original','in_scope',NULL,"
                        "'captured',NULL,'{}')", (original,))
        con.execute("INSERT INTO agencies VALUES(?,?,NULL,NULL)", (AGENCY, "Synthetic Agency"))
        proof = evidence or {"source_sha256": SUBJECT, "locator": canonical({"row": 1})}
        con.execute("INSERT INTO joins VALUES('join-1',?,?,NULL,'agency',?,'typed',NULL)",
                    (SUBJECT, AGENCY, json.dumps(proof)))
        con.execute("PRAGMA user_version=1")
        con.commit()
        if wal:
            con.execute("PRAGMA journal_mode=WAL")
    finally:
        con.close()
    os.chmod(path, 0o600)


def sha_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class DetectorsRunTests(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory(prefix="f042-run-")
        self.addCleanup(holder.cleanup)
        self.root = Path(os.path.realpath(holder.name))
        os.chmod(self.root, 0o700)
        self.inputs = self.root / "inputs"
        os.mkdir(self.inputs, 0o700)
        os.chmod(self.inputs, 0o700)
        self.sources = inventory()
        self.values = {"inventory": self.sources, "units": producer_output(self.sources),
                       "rules": rules(), "config": {"as_of": "2020-01-20"}}
        self.canonical_path = self.inputs / "canonical.sqlite"
        build_canonical(self.canonical_path)
        self.counter = 0

    def write(self, name, value, raw=None):
        path = self.inputs / (name + "-" + str(self.counter) + ".json")
        self.counter += 1
        data = raw if raw is not None else (json.dumps(value, sort_keys=True) + "\n").encode()
        path.write_bytes(data)
        return path

    def argv(self, *, omit=(), absent=(), override=None, values=None, canonical_path=None):
        values = values or self.values
        out = self.root / ("out-" + str(self.counter))
        self.counter += 1
        argv = ["--run-id", RUN_ID, "--subject-sha256", SUBJECT, "--out-root", str(out)]
        paths = {}
        for name in ("inventory", "units", "rules", "config"):
            paths[name] = self.write(name, values[name])
        paths["canonical_copy"] = canonical_path or self.canonical_path
        for name, path in paths.items():
            if name in omit:
                continue
            flag = "--" + name.replace("_", "-")
            expected = sha_file(path)
            if name in absent:
                path = self.inputs / ("absent-" + name + ".json")
            argv += [flag, str(path), flag + "-sha256", expected]
        if override:
            for flag, value in override.items():
                index = argv.index(flag)
                argv[index + 1] = value
        return argv, out

    def run_cli(self, argv, canonical_path=None):
        canonical_path = canonical_path or self.canonical_path
        before = sha_file(canonical_path)
        buffer = io.StringIO()
        spy = mock.patch.object(pipeline, "evaluate_structured", wraps=pipeline.evaluate_structured)
        spy.start()
        try:
            # Keep the bound-signature check meaningful under the spy.
            with mock.patch.dict(detectors_run.BOUND_SIGNATURES, {"pipeline.evaluate_structured": (
                    lambda: spy.temp_original, detectors_run.BOUND_SIGNATURES["pipeline.evaluate_structured"][1])}):
                code = detectors_run.main(argv, stdout=buffer)
            calls = pipeline.evaluate_structured.call_count
        finally:
            spy.stop()
        # Canonical-copy bytes are unchanged after success and every failure.
        self.assertEqual(sha_file(canonical_path), before)
        for suffix in detectors_run.SIDECARS:
            self.assertFalse(os.path.lexists(str(canonical_path) + suffix), suffix)
        result = json.loads(buffer.getvalue())
        self.assertEqual(result["exit_code"], code)
        for key, value in detectors_run.FLAGS.items():
            self.assertIs(type(result[key]), type(value))
            self.assertEqual(result[key], value)
        return code, result, calls

    def direct(self, values=None):
        """Independent recomputation through the real adapter, same inputs."""
        values = values or self.values
        with store.ledger(self.canonical_path, readonly=True) as con:
            joins = pipeline.canonical_joins(con, values["inventory"])
        return pipeline.evaluate_structured(
            subject_sha256=SUBJECT, normalized=values["units"]["normalized"],
            source_bindings=values["inventory"], joins=joins, rules=values["rules"], config=values["config"])

    def assert_blocked_everywhere(self, result, reason):
        self.assertEqual(set(result["structured"]), set(DETECTORS))
        for name, entry in result["structured"].items():
            self.assertEqual(entry, pipeline._entry("blocked", reason), name)
        self.assertFalse(result["clean"])

    # Positive path -------------------------------------------------------

    def test_producer_shaped_positive_reaches_real_adapter_and_persists(self):
        argv, out = self.argv()
        code, result, calls = self.run_cli(argv)
        self.assertEqual(code, 0)
        self.assertEqual(calls, 1)
        self.assertEqual(result["outcome"], "adapter_completed")
        self.assertTrue(result["adapter"]["called"])
        self.assertTrue(result["clean"])
        self.assertIsNone(result["joins_error"])
        statuses = {name: (entry["status"], entry["reason"]) for name, entry in result["structured"].items()}
        self.assertEqual(statuses, EXPECTED_STATUS)
        self.assertEqual(result["status_counts"], {"evaluated": 2, "blocked": 0, "inapplicable": 5})
        expected = self.direct()
        self.assertEqual(result["adapter"]["result"], expected)
        self.assertEqual(result["structured"], expected["structured"])
        self.assertEqual(expected["publication_ready"], False)
        self.assertEqual(expected["stage_promotions"], 0)
        retention = result["structured"]["retention-over-policy"]
        self.assertEqual(retention["hit_count"], 1)
        hit = retention["hits"][0]
        self.assertEqual(hit["observed_values_private"], {"setting_days": 31, "policy_days": 30})
        self.assertEqual(hit["applicability"]["effective_to"], "2021-01-01")
        self.assertEqual(hit["applicability"]["rule_source_sha256"], POLICY)
        self.assertEqual(result["structured"]["external-sharing"]["hit_count"], 1)
        # Inventory/hash identities are bound exactly.
        self.assertEqual(result["inputs"]["canonical_copy"], {"sha256": sha_file(self.canonical_path)})
        self.assertEqual(expected["revisions"]["source_bindings_sha256"], digest(self.sources))
        # Existing persistence, inside the exclusive synthetic root only.
        self.assertEqual(result["persistence"]["status"], "persisted")
        for name in DETECTORS:
            receipt = result["persistence"]["detectors"][name]
            self.assertEqual(receipt["status"], "persisted")
            self.assertEqual(receipt["run_id"], RUN_ID + "/" + name)
            self.assertEqual(receipt["manifest_sha256"], expected["structured"][name]["manifest"]["manifest_sha256"])
            self.assertFalse(receipt["replayed"])
            self.assertEqual(receipt["stage_promotions"], 0)
        with sqlite3.connect((out / "detectors.sqlite").as_uri() + "?mode=ro", uri=True) as con:
            count = lambda table: con.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
            self.assertEqual(count("wp6_manifests"), 7)
            self.assertEqual(count("detector_hits"), 2)
            self.assertEqual(count("originals"), 2)
            self.assertEqual({row[0] for row in con.execute("SELECT status FROM runs")}, {"completed"})
            self.assertEqual({row[0] for row in con.execute("SELECT kind FROM runs")}, {"detector-runner"})
        self.assertEqual(oct(os.stat(out).st_mode & 0o777), "0o700")
        stored = json.loads((out / "result.json").read_text())
        self.assertEqual(stored["result_sha256"], result["result_sha256"])
        manifest = json.loads((out / "manifest.json").read_text())
        self.assertEqual(manifest["detector_version"], core.VERSION)
        self.assertEqual(manifest["adapter_version"], pipeline.VERSION)
        self.assertEqual(manifest["persistence_version"], persistence.ADAPTER_VERSION)
        self.assertEqual(manifest["inputs"], result["inputs"])
        self.assertEqual(manifest["run_id"], RUN_ID)

    def test_identical_inputs_give_byte_identical_results(self):
        first_argv, first = self.argv()
        second_argv, second = self.argv()
        self.assertEqual(self.run_cli(first_argv)[0], 0)
        self.assertEqual(self.run_cli(second_argv)[0], 0)
        self.assertEqual((first / "result.json").read_bytes(), (second / "result.json").read_bytes())
        self.assertEqual((first / "manifest.json").read_bytes(), (second / "manifest.json").read_bytes())

    # Missing context ------------------------------------------------------

    def test_missing_inventory_blocks_every_detector_without_derivation(self):
        for mode in ("omit", "absent"):
            with self.subTest(mode=mode):
                argv, out = self.argv(**{mode: ("inventory",)})
                code, result, calls = self.run_cli(argv)
                self.assertEqual(code, 3)
                self.assertEqual(calls, 0)
                self.assertEqual(result["outcome"], "context_missing")
                self.assertEqual(result["reason"], "required_context_unresolved")
                self.assertEqual(result["context_missing"], ["inventory"])
                self.assert_blocked_everywhere(result, "required_context_unresolved")
                self.assertEqual(result["status_counts"], {"evaluated": 0, "blocked": 7, "inapplicable": 0})
                self.assertFalse(result["adapter"]["called"])
                self.assertIsNone(result["inputs"]["inventory"])
                self.assertEqual(result["persistence"]["status"], "not_attempted")
                self.assertFalse((out / "detectors.sqlite").exists())
                self.assertTrue((out / "result.json").exists())

    def test_every_missing_required_input_is_not_a_clean_result(self):
        for name in ("units", "canonical_copy", "rules", "config"):
            with self.subTest(name=name):
                argv, _ = self.argv(omit=(name,))
                code, result, calls = self.run_cli(argv)
                self.assertEqual(code, 3)
                self.assertEqual(calls, 0)
                self.assertEqual(result["context_missing"], [name])
                self.assert_blocked_everywhere(result, "required_context_unresolved")

    # Preserved adapter typed reasons --------------------------------------

    def test_unbound_rule_source_preserves_adapter_reason(self):
        values = copy.deepcopy(self.values)
        values["rules"]["entries"][0]["source_sha256"] = h("synthetic unbound policy")
        argv, _ = self.argv(values=values)
        code, result, calls = self.run_cli(argv)
        self.assertEqual(code, 3)
        self.assertEqual(calls, 1)
        self.assertEqual(result["outcome"], "adapter_completed")
        self.assertEqual(result["reason"], "detector_context_blocked")
        self.assertFalse(result["clean"])
        self.assertEqual(result["structured"]["retention-over-policy"],
                         pipeline._entry("blocked", "rule_source_unbound"))
        self.assertEqual(result["structured"]["external-sharing"]["status"], "evaluated")
        self.assertEqual(result["structured"], self.direct(values)["structured"])
        self.assertEqual(result["persistence"]["detectors"]["retention-over-policy"],
                         {"status": "not_persisted", "reason": "adapter_blocked_before_evaluation"})
        self.assertEqual(result["persistence"]["detectors"]["external-sharing"]["status"], "persisted")

    def test_unresolved_applicability_preserves_adapter_reason(self):
        for key, value in (("version_verified", False), ("applicability", "unknown")):
            with self.subTest(key=key):
                values = copy.deepcopy(self.values)
                values["rules"]["entries"][1][key] = value
                argv, _ = self.argv(values=values)
                code, result, _ = self.run_cli(argv)
                self.assertEqual(code, 3)
                self.assertEqual(result["structured"]["external-sharing"],
                                 pipeline._entry("blocked", "rule_version_or_applicability_unresolved"))
                self.assertEqual(result["structured"]["retention-over-policy"]["status"], "evaluated")
                self.assertEqual(result["structured"], self.direct(values)["structured"])

    def test_incomplete_normalization_and_inventory_drift_stay_blocked(self):
        for mutation, reason in (("incomplete", "normalization_incomplete"),
                                 ("drift", "source_inventory_mismatch"),
                                 ("extra_source", "normalization_inventory_incomplete")):
            with self.subTest(mutation=mutation):
                values = copy.deepcopy(self.values)
                if mutation == "incomplete":
                    values["units"]["normalized"]["complete"] = False
                elif mutation == "drift":
                    values["inventory"][0]["artifact_sha256"] = h("changed coordinator inventory")
                else:
                    extra = dict(values["inventory"][0], unit_id="unit-csv-9999", locator={"row": 9})
                    values["inventory"].append(extra)
                    values["units"]["normalized"]["source_inventory_sha256"] = digest(values["inventory"])
                argv, _ = self.argv(values=values)
                code, result, calls = self.run_cli(argv)
                self.assertEqual(code, 3)
                self.assertEqual(calls, 1)
                self.assert_blocked_everywhere(result, reason)
                self.assertEqual(result["persistence"]["status"], "not_applicable")

    def test_canonical_join_failure_is_recorded_and_blocks(self):
        bad = self.inputs / "bad-join.sqlite"
        build_canonical(bad, evidence={"source_sha256": h("unsupported"), "locator": canonical({"row": 1})})
        argv, _ = self.argv(canonical_path=bad)
        code, result, calls = self.run_cli(argv, canonical_path=bad)
        self.assertEqual(code, 3)
        self.assertEqual(calls, 1)
        self.assertEqual(result["joins_error"], "typed_join_support_missing")
        self.assert_blocked_everywhere(result, "canonical_join_snapshot_missing")

    # Refusals --------------------------------------------------------------

    def test_identity_mismatch_and_missing_expected_hash_refuse(self):
        argv, out = self.argv()
        index = argv.index("--inventory-sha256")
        argv[index + 1] = h("not the inventory bytes")
        code, result, calls = self.run_cli(argv)
        self.assertEqual((code, result["outcome"], result["reason"]),
                         (2, "refused", "input_identity_mismatch:inventory"))
        self.assertEqual(calls, 0)
        self.assertIsNone(result["structured"])
        self.assertFalse(out.exists())
        argv, _ = self.argv()
        index = argv.index("--rules-sha256")
        del argv[index:index + 2]
        code, result, _ = self.run_cli(argv)
        self.assertEqual((code, result["reason"]), (2, "expected_identity_missing:rules"))
        argv, _ = self.argv()
        index = argv.index("--canonical-copy-sha256")
        argv[index + 1] = h("not the canonical copy")
        code, result, _ = self.run_cli(argv)
        self.assertEqual((code, result["reason"]), (2, "input_identity_mismatch:canonical_copy"))
        self.assertTrue(result["canonical_copy"]["unchanged"])

    def test_adapter_signature_drift_refuses_before_reading(self):
        def changed(*, subject_sha256, normalized=None, source_bindings=None, joins=None, rules=None):
            raise AssertionError("must not be called")
        argv, out = self.argv()
        buffer = io.StringIO()
        with mock.patch.object(pipeline, "evaluate_structured", changed):
            code = detectors_run.main(argv, stdout=buffer)
        result = json.loads(buffer.getvalue())
        self.assertEqual((code, result["reason"]), (2, "adapter_signature_mismatch:pipeline.evaluate_structured"))
        self.assertFalse(out.exists())

    def test_unqualified_canonical_copies_refuse_unchanged(self):
        wal = self.inputs / "wal.sqlite"
        build_canonical(wal, wal=True)
        argv, _ = self.argv(canonical_path=wal)
        code, result, calls = self.run_cli(argv, canonical_path=wal)
        self.assertEqual((code, result["reason"], calls), (2, "canonical_copy_wal_unqualified", 0))
        wrong = self.inputs / "wrong-schema.sqlite"
        build_canonical(wrong, checksum=h("other schema"))
        argv, _ = self.argv(canonical_path=wrong)
        code, result, calls = self.run_cli(argv, canonical_path=wrong)
        self.assertEqual((code, result["reason"], calls), (2, "canonical_copy_unqualified", 0))
        self.assertTrue(result["canonical_copy"]["unchanged"])
        notdb = self.inputs / "not-a-database.sqlite"
        notdb.write_bytes(b"synthetic text, not sqlite\n" * 8)
        os.chmod(notdb, 0o600)
        argv, _ = self.argv(canonical_path=notdb)
        code, result, _ = self.run_cli(argv, canonical_path=notdb)
        self.assertEqual((code, result["reason"]), (2, "canonical_copy_not_sqlite"))

    def test_unsafe_paths_and_non_exclusive_out_root_refuse(self):
        argv, out = self.argv()
        os.mkdir(out, 0o700)
        code, result, _ = self.run_cli(argv)
        self.assertEqual((code, result["reason"]), (2, "out_root_not_exclusive"))
        self.assertEqual(list(out.iterdir()), [])
        link = self.inputs / "inventory-link.json"
        target = self.write("inventory", self.sources)
        os.symlink(target, link)
        argv, _ = self.argv(override={"--inventory": str(link)})
        code, result, _ = self.run_cli(argv)
        self.assertEqual((code, result["reason"]), (2, "path_symlink:inventory"))
        argv, _ = self.argv(override={"--rules": "relative/rules.json"})
        code, result, _ = self.run_cli(argv)
        self.assertEqual((code, result["reason"]), (2, "path_not_absolute:rules"))
        argv, _ = self.argv(override={"--out-root": "relative-out"})
        code, result, _ = self.run_cli(argv)
        self.assertEqual((code, result["reason"]), (2, "out_root_not_absolute"))

    def test_strict_json_and_producer_run_identity(self):
        raw = b'{"version": "a", "version": "b", "entries": []}\n'
        path = self.write("rules", None, raw=raw)
        argv, _ = self.argv(override={"--rules": str(path), "--rules-sha256": hashlib.sha256(raw).hexdigest()})
        code, result, calls = self.run_cli(argv)
        self.assertEqual((code, result["reason"], calls), (2, "input_json_invalid:rules", 0))
        raw = b'{"as_of": NaN}\n'
        path = self.write("config", None, raw=raw)
        argv, _ = self.argv(override={"--config": str(path), "--config-sha256": hashlib.sha256(raw).hexdigest()})
        code, result, _ = self.run_cli(argv)
        self.assertEqual((code, result["reason"]), (2, "input_json_invalid:config"))
        values = copy.deepcopy(self.values)
        values["units"]["run_id"] = "regenerated-run-id"
        argv, _ = self.argv(values=values)
        code, result, calls = self.run_cli(argv)
        self.assertEqual((code, result["reason"], calls), (2, "producer_run_id_mismatch", 0))

    def test_persistence_requires_canonical_originals(self):
        partial = self.inputs / "missing-policy-original.sqlite"
        build_canonical(partial, originals=(SUBJECT,))
        argv, out = self.argv(canonical_path=partial)
        code, result, calls = self.run_cli(argv, canonical_path=partial)
        self.assertEqual(code, 4)
        self.assertEqual(calls, 1)
        self.assertEqual(result["outcome"], "persistence_failed")
        self.assertEqual(result["reason"], "canonical_original_missing")
        self.assertFalse(result["clean"])
        self.assertFalse((out / "detectors.sqlite").exists())
        self.assertEqual({row["status"] for row in result["persistence"]["detectors"].values()}, {"not_persisted"})

    def test_cli_entry_point_imports_without_installed_paths(self):
        tool = Path(detectors_run.__file__).resolve()
        self.assertEqual(tool.parent.name, "tools")
        self.assertEqual(tool.parents[1], Path(core.__file__).resolve().parents[3])


if __name__ == "__main__":
    unittest.main()
