"""T4B slice 3, test group G: synthetic acceptance that mirrors the TEST acceptance shape.

Fictional bytes only. This is NOT the TEST acceptance run (design group H), which needs the
owner's approval of a TEST policy and real data and is not part of this slice.
"""
import hashlib
import json
import unittest

from campaign_tool.records import mail_adoption
from campaign_tool.records.ledger import original_resolution, store
from tests.records.mail_adoption_resolution_fixtures import ResolutionAdoptionCase
from tests.records.mail_resolution_fixtures import sha


def digest(rows):
    return hashlib.sha256(json.dumps([list(map(str, r)) for r in rows]).encode()).hexdigest()


class SyntheticAcceptance(ResolutionAdoptionCase):
    def setUp(self):
        super().setUp()
        self.ra = self.mk(71, [])
        self.rb = self.mk(72, ["acceptance O1", "acceptance O2", "acceptance O2", "acceptance X"])
        self.rc = self.mk(73, ["acceptance O1"])
        self.rd = self.mk(74, ["acceptance O3 held"])
        self.re = self.mk_rfc822(75, ["acceptance O1"])
        self.o1, self.o2, self.x = self.rb.atts[0], self.rb.atts[1], self.rb.atts[3]
        self.o3 = self.rd.atts[0]
        self.lost = {"sha256": sha("acceptance L"), "bytes": len(b"acceptance L")}
        self.declare_legacy(self.o1, self.o2, self.o3, self.lost)
        self.build()
        self.install_baseline_row()
        ids = self.occ(self.rb, self.o2["sha256"])
        self.assertEqual(len(ids), 2)
        self.policy_sha = self.pin({
            self.o1["sha256"]: self.entry_for(self.o1, self.rb, self.occ(self.rb, self.o1["sha256"])[0],
                                              [self.rc, self.re]),
            self.o2["sha256"]: self.entry_for(self.o2, self.rb, ids[0])}, held=[self.o3["sha256"]])

    def install_baseline_row(self):
        """A pre-existing resolution row for the unrelated original L (resolution_baseline.rows = 1)."""
        with store.ledger(self.database) as con:
            con.execute("PRAGMA foreign_keys=OFF")
            con.execute("BEGIN IMMEDIATE")
            original_resolution.ensure_schema(con)
            con.create_function("original_resolution_append_allowed", -1, lambda *row: 1)
            con.execute("INSERT INTO original_resolutions VALUES(?,?,?,?,?,?,?)",
                        (self.lost["sha256"], sha("L binding"), sha("L declaration"), sha("local policy"),
                         sha("L receipt"), sha("L occurrence"), json.dumps({"schema": "original_resolution_v1"})))
            con.commit()

    def originals(self):
        return {r[0]: r for r in self.rows("SELECT * FROM originals ORDER BY sha256")}

    def legacy_digests(self):
        return [digest(self.rows("SELECT * FROM legacy_rows ORDER BY 1,2,3")),
                digest(self.rows("SELECT * FROM occurrence_projection_rows ORDER BY 1,2,3")),
                digest(self.rows("SELECT * FROM occurrences WHERE acquisition_method LIKE 'captured_legacy_%' ORDER BY id"))]

    def test_40_dry_run_apply_and_rerun(self):
        k0 = self.originals()
        legacy0 = self.legacy_digests()
        baseline_rows = self.resolution_rows()
        self.assertEqual(len(baseline_rows), 1)
        dry = self.adopt()
        self.invariants(dry)
        self.assertEqual((dry["receipts"], dry["eligible_new_messages"], dry["resolvable_messages"],
                          dry["resolvable_binding_messages"], dry["resolvable_dependent_messages"]), (5, 1, 2, 1, 1))
        self.assertEqual((dry["policy_originals"], dry["policy_originals_resolved"], dry["resolvable_originals"],
                          dry["policy_originals_blocked"], dry["held_originals"], dry["resolution_drift"]),
                         (2, 0, 2, 0, 1, {}))
        self.assertEqual(dry["coded_gaps"], {"original_scope_unresolved": 1, "unsupported_rfc822_part": 1})
        self.assertEqual((dry["unaccepted_coded_gaps"], dry["resolution_baseline"]["rows"]), ({}, 1))
        self.assertFalse(mail_adoption.blocking(dry))

        applied = self.adopt(apply=True)
        self.invariants(applied)
        self.assertEqual((applied["adopted_messages"], applied["resolved_originals"], applied["failures"],
                          applied["resolution_after"]["rows"]), (3, 2, {}, 3))
        self.assertFalse(mail_adoption.blocking(applied))
        rows = {r[0]: r for r in self.resolution_rows()}
        self.assertEqual(rows[self.lost["sha256"]], baseline_rows[0])  # L is byte-identical
        new = set(rows) - {self.lost["sha256"]}
        self.assertEqual(new, {self.o1["sha256"], self.o2["sha256"]})  # and nothing for O3
        for subject in new:
            payload = json.loads(rows[subject][6])
            self.assertEqual((payload["schema"], rows[subject][3], rows[subject][4]),
                             ("original_resolution_mail_export_v1", self.policy_sha, self.rb.digest))
        # Originals: every K0 row is byte-identical; only E_a, E_b, E_c and X are new.
        after = self.originals()
        for key, row in k0.items():
            self.assertEqual(after[key], row)
        added = set(after) - set(k0)
        self.assertEqual(added, {self.ra.eml["sha256"], self.rb.eml["sha256"], self.rc.eml["sha256"], self.x["sha256"]})
        for key in added:
            self.assertEqual((after[key][4], after[key][5], after[key][6]),
                             ("original", "in_scope", str(self.root / "intake" / "blobs" / key)))
            self.assertTrue(self.rows("SELECT 1 FROM occurrences WHERE original_sha256=? AND "
                                      "acquisition_method='verified_mail_export'", (key,)))
        for key in (self.o1["sha256"], self.o2["sha256"]):
            self.assertEqual((after[key][4], after[key][5]), ("unresolved", "unresolved"))
        self.assertEqual(self.legacy_digests(), legacy0)

        settled = hashlib.sha256((self.root / "ledger.sqlite").read_bytes()).hexdigest()
        for again in (self.adopt(), self.adopt(apply=True)):
            self.assertEqual((again["adopted_messages"], again["resolved_originals"], again["policy_originals_resolved"],
                              again["resolvable_originals"], again["policy_originals_blocked"],
                              again["resolution_drift"]), (0, 0, 2, 0, 0, {}))
            self.assertEqual(again["resolution_baseline"], applied["resolution_after"])
            self.assertFalse(mail_adoption.blocking(again))
        self.assertEqual(set(self.originals()), set(k0) | added)
        self.assertEqual(hashlib.sha256((self.root / "ledger.sqlite").read_bytes()).hexdigest(), settled)


if __name__ == "__main__":
    unittest.main()
