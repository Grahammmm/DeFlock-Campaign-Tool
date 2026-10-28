"""G40 synthetic acceptance: baseline-keyed immutability plus legitimate delta."""
import json
import unittest

from campaign_tool.records import mail_adoption as adoption
from campaign_tool.records.ledger.original_resolution import digest
from tests.records import test_mail_adoption_resolution as fixtures


class SyntheticAcceptanceTests(unittest.TestCase):
    def test_g40_baseline_delta_provenance_and_exact_resolution_set(self):
        w = fixtures.World(self, baseline=True)
        ra = w.export(10)
        rb = w.export(11, (fixtures.O1, fixtures.O2, fixtures.O2, fixtures.X))
        rc = w.export(12, (fixtures.O1,))
        rd = w.export(13, (fixtures.O3,))
        re = w.export(14, (fixtures.O1,), nested=True)
        # Choose byte layout only, not an alternate policy binding algorithm.
        # This freezes a census-proposed Rb binding smaller than Rc's digest.
        raw = w.paths[11].read_bytes()
        limit = fixtures.sha(w.paths[12].read_bytes())
        for spaces in range(256):
            candidate = raw + b" " * spaces
            if fixtures.sha(candidate) < limit:
                fixtures.write_private(w.paths[11], candidate)
                break
        else:
            self.fail("synthetic binding digest fixture exhausted")
        for value in (fixtures.O1, fixtures.O2, fixtures.O3):
            w.declare(value)
        w.policy(held=[fixtures.sha(fixtures.O3)])
        self.assertEqual({entry["verification_receipt_sha256"] for entry in w.value["originals"].values()},
                         {fixtures.sha(w.paths[11].read_bytes())})
        original_rows = w.rows("SELECT * FROM originals ORDER BY sha256")
        k0 = {row[0] for row in original_rows}
        d0 = digest(original_rows)
        r0 = w.rows("SELECT * FROM original_resolutions ORDER BY original_sha256")
        self.assertEqual(len(r0), 1)
        legacy = {table: w.rows("SELECT * FROM " + table + " ORDER BY 1,2")
                  for table in ("legacy_rows", "occurrence_projection_rows")}
        captured = w.rows("SELECT * FROM occurrences WHERE acquisition_method LIKE 'captured_legacy_%' ORDER BY id")
        self.assertTrue(captured)
        self.assertTrue(legacy["occurrence_projection_rows"])
        before = w.tree()
        dry = w.run()
        self.assertEqual(before, w.tree())
        for key, value in {"eligible_new_messages": 1, "resolvable_messages": 2, "resolvable_binding_messages": 1,
                           "resolvable_dependent_messages": 1, "policy_originals": 2, "policy_originals_resolved": 0,
                           "resolvable_originals": 2, "policy_originals_blocked": 0, "held_originals": 1,
                           "resolution_drift": {}, "unaccepted_coded_gaps": {},
                           "coded_gaps": {"original_scope_unresolved": 1, "unsupported_rfc822_part": 1}}.items():
            self.assertEqual(dry[key], value, key)
        self.assertFalse(adoption.blocking(dry), dry)
        self.assertEqual(dry["resolution_baseline"], {"rows": 1, "sha256": digest([(r0[0][0], r0[0][1])])})
        report = w.run(True)
        self.assertFalse(adoption.blocking(report), report)
        self.assertEqual((report["adopted_messages"], report["resolved_originals"], report["resolution_after"]["rows"]), (3, 2, 3))
        after = w.rows("SELECT * FROM original_resolutions ORDER BY original_sha256")
        for row in r0:
            self.assertIn(row, after)
        baseline_keys = {row[0] for row in r0}
        new_resolutions = {row[0]: row for row in after if row[0] not in baseline_keys}
        expected = {fixtures.sha(fixtures.O1), fixtures.sha(fixtures.O2)}
        self.assertEqual(set(new_resolutions), expected)  # S == E_r
        for subject, row in new_resolutions.items():
            entry = w.value["originals"][subject]
            payload = json.loads(row[-1])
            self.assertEqual(payload["schema"], "original_resolution_mail_export_v1")
            self.assertEqual(row[3], w.current.sha256)
            self.assertEqual(row[4], entry["verification_receipt_sha256"])
            self.assertEqual(row[5], entry["binding_occurrence_id"])
        self.assertNotIn(fixtures.sha(fixtures.O3), new_resolutions)
        originals = w.rows("SELECT * FROM originals ORDER BY sha256")
        preserved = [row for row in originals if row[0] in k0]
        self.assertEqual(preserved, original_rows)
        self.assertEqual(digest(preserved), d0)
        for subject in expected:
            row = next(row for row in preserved if row[0] == subject)
            self.assertEqual((row[4], row[5], row[6]), ("unresolved", "unresolved", None))
        inserted = {row[0]: row for row in originals if row[0] not in k0}
        a_new = {ra["original_eml"]["sha256"], rb["original_eml"]["sha256"], rc["original_eml"]["sha256"], fixtures.sha(fixtures.X)}
        self.assertEqual(set(inserted), a_new)
        self.assertFalse(a_new & expected)
        adopted_digests = {fixtures.sha(w.paths[uid].read_bytes()) for uid in (10, 11, 12)}
        for subject, row in inserted.items():
            self.assertEqual((row[4], row[5], row[6]), ("original", "in_scope", str(w.root / "intake" / "blobs" / subject)))
            provenance = json.loads(row[9])
            self.assertEqual(provenance["method"], "verified_export_receipt")
            self.assertIn(provenance["receipt_sha256"], adopted_digests)
            occurrences = w.rows("SELECT evidence FROM occurrences WHERE original_sha256=? AND acquisition_method='verified_mail_export'", (subject,))
            self.assertTrue(occurrences)
            self.assertTrue(all(json.loads(o[0])["receipt_sha256"] in adopted_digests for o in occurrences))
        for table, rows in legacy.items():
            self.assertEqual(w.rows("SELECT * FROM " + table + " ORDER BY 1,2"), rows)
        self.assertEqual(w.rows("SELECT * FROM occurrences WHERE acquisition_method LIKE 'captured_legacy_%' ORDER BY id"), captured)
        self.assertEqual(report["after"]["coded_gaps"], dry["coded_gaps"])
        self.assertEqual(report["after"]["policy_originals_resolved"], 2)
        before = w.tree()
        for apply in (False, True):
            rerun = w.run(apply)
            self.assertEqual((rerun["adopted_messages"], rerun["resolved_originals"]), (0, 0))
            self.assertEqual(rerun["policy_originals_resolved"], 2)
            self.assertEqual((rerun["resolvable_originals"], rerun["policy_originals_blocked"]), (0, 0))
            self.assertEqual(rerun["resolution_after"], report["resolution_after"])
            self.assertEqual(rerun["resolution_drift"], {})
            self.assertFalse(adoption.blocking(rerun), rerun)
        self.assertEqual(before, w.tree())
        self.assertEqual({r[0] for r in w.rows("SELECT sha256 FROM originals")}, k0 | a_new)
        self.assertEqual(w.rows("PRAGMA quick_check"), [("ok",)])
        self.assertEqual(w.rows("PRAGMA foreign_key_check"), [])


if __name__ == "__main__":
    unittest.main()
