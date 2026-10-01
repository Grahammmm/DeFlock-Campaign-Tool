"""Regressions for confirmed accepted-evidence drift and missing exact positions."""
import json
import unittest
from unittest import mock
from tests.records import test_catalog_stage as fixture

module, stages, store = fixture.module, fixture.stages, fixture.store


class CatalogDriftTests(unittest.TestCase):
    setUp = fixture.CatalogStageTests.setUp
    write = fixture.CatalogStageTests.write
    save = fixture.CatalogStageTests.save
    prepare = fixture.CatalogStageTests.prepare
    process = fixture.CatalogStageTests.process
    advance_prerequisites = fixture.CatalogStageTests.advance_prerequisites

    def test_blank_and_untyped_positions_are_never_accepted(self):
        for locator in (None, "", " ", "null", "{}", "[]", '""', "page:0", '{"line":null}', '{"line":0}'):
            with self.subTest(locator=locator):
                self.card["supports"][0]["locator"] = locator
                with store.ledger(self.database) as con:
                    con.execute("UPDATE units SET locator=?", (locator if locator is not None else "",))
                    con.commit()
                self.assertEqual(self.process()["reason"], "invalid_exact_locator")
        self.assertEqual(stages.query_counts(self.database)["stages"]["catalog"]["done"], 0)

    def test_typed_json_position_remains_supported(self):
        locator = '{"line":1}'
        self.card["supports"][0]["locator"] = locator
        with store.ledger(self.database) as con:
            con.execute("UPDATE units SET locator=?", (locator,))
            con.commit()
        self.assertEqual(self.prepare()["envelope"]["supports"][0]["locator"], locator)

    def test_bad_new_quote_does_not_invalidate_healthy_acceptance(self):
        self.advance_prerequisites()
        accepted = self.process()
        self.card["supports"][0]["quote"] = "absent new candidate quotation"
        result = self.process()
        self.assertEqual(result["reason"], "quote_not_in_source")
        self.assertFalse(result["canonical_stage_changed"])
        self.assertEqual(stages.query_counts(self.database)["stages"]["catalog"]["done"], 1)
        with store.ledger(self.database, readonly=True) as con:
            self.assertEqual(con.execute("SELECT receipt_sha256 FROM stage_state WHERE stage='catalog'").fetchone()[0], accepted["receipt_sha256"])

    def test_transient_read_failure_preserves_current_acceptance(self):
        self.advance_prerequisites()
        self.process()
        original_read = self.adapter._read
        def unavailable(path, limit):
            if str(path).endswith("unit.txt"):
                raise PermissionError("synthetic temporary unavailability")
            return original_read(path, limit)
        with mock.patch.object(self.adapter, "_read", side_effect=unavailable):
            result = self.process()
        self.assertEqual(result["reason"], "artifact_io_unavailable")
        self.assertFalse(result["canonical_stage_changed"])
        self.assertEqual(stages.query_counts(self.database)["stages"]["catalog"]["done"], 1)

    def advance_detector(self):
        run_id = self.run_id + "-detect"
        with store.ledger(self.database) as con:
            con.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)", (run_id,"stage-runner",store.now(),None,"synthetic-engine",None,self.config,None,"running","{}"))
            con.commit()
        content = b"Synthetic exact detector result"
        runner = stages.testing_runner(self.database, run_id=run_id, owner="synthetic-detector",
            engine_version="synthetic-engine", config_sha256=self.config,
            validators={"detect": lambda context: context["content"] == content and context["subject_sha256"] == self.subject},
            version="synthetic-detector-v1")
        registered = runner.set_content(self.subject, "detect", content, author_id="synthetic-author", tier="A")
        claim = runner.claim(self.subject, "detect")
        with store.ledger(self.database, readonly=True) as con:
            state = {r["stage"]: r["receipt_sha256"] for r in con.execute("SELECT * FROM stage_state WHERE original_sha256=?", (self.subject,))}
        inputs = {"original": self.subject, **{key: state[key] for key in stages.PREREQUISITES["detect"]}}
        packet = {"schema":"ledger-stage-receipt-v1","subject_sha256":self.subject,"stage":"detect",
            "content_sha256":registered["content_sha256"],"tier":"A","author_id":"synthetic-author",
            "reviewer_id":"synthetic-detector","role":stages.ROLES["detect"],"verdict":"pass",
            "coverage":{"denominator":{"kind":"items","total":1},"covered":1,"scope":"selected"},
            "locators":["page:1"],"rationale":"Synthetic bounded detector evidence","model_or_tool":"synthetic",
            "created_at_tz":store.now(),"input_hashes":inputs,"reviews":[]}
        return runner.promote(self.subject,"detect",module.catalog.encoded(packet),claim_id=claim["claim_id"])

    def test_proven_drift_invalidates_catalog_and_accepted_dependents(self):
        self.advance_prerequisites()
        accepted = self.process()
        detector = self.advance_detector()
        before = stages.query_counts(self.database)
        self.assertEqual(before["stages"]["detect"]["done"], 1)
        self.write("unit.txt", b"Changed synthetic supporting bytes")
        result = self.process()
        self.assertTrue(result["canonical_stage_changed"])
        self.assertEqual(set(result["invalidated"]), {"catalog", "detect"})
        after = stages.query_counts(self.database)
        for name in ("catalog", "detect", "review", "compare", "privacy"):
            self.assertEqual(after["stages"][name]["done"], 0)
        self.assertEqual(after["stages"]["extract"]["done"], 1)
        self.assertEqual(after["stages"]["preserve"]["done"], 1)
        with store.ledger(self.database, readonly=True) as con:
            for receipt in (accepted["receipt_sha256"], detector["receipt_sha256"]):
                self.assertIsNotNone(con.execute("SELECT 1 FROM stage_artifacts WHERE sha256=?", (receipt,)).fetchone())
        self.write("unit.txt", self.original)
        self.assertEqual(self.process()["status"], "done")
        self.assertEqual(stages.query_counts(self.database)["stages"]["detect"]["done"], 0)
