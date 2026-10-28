"""Scope stays in inventory but must never stall unrelated claims."""
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from campaign_tool.records import queue

class ScopeTests(unittest.TestCase):
    def fixture(self):
        outside=hashlib.sha256(b"synthetic excluded").hexdigest()
        inside=hashlib.sha256(b"synthetic included").hexdigest()
        originals=[{"sha256":outside,"scope":"out_of_scope"},{"sha256":inside,"scope":"in_scope"}]
        rows=[{"original_sha256":x["sha256"],"stage":s,"status":"pending","receipt_sha256":None} for x in originals for s in queue.STAGES]
        return outside,inside,originals,rows
    def test_outside_original_visible_but_not_claimable(self):
        outside,inside,originals,rows=self.fixture()
        result=queue.plan(originals,rows,[],now="2026-01-01T00:00:00Z",facts={outside:{"new_production_open_request":True,"document_type":"policy"}})
        self.assertEqual(result["snapshot_originals"],2)
        self.assertEqual([x["original_sha256"] for x in result["top"]],[inside])
        self.assertEqual(result["excluded_from_claims"],[{"original_sha256":outside,"reason":"original_out_of_scope"}])
    def test_excluded_original_still_needs_complete_stage_inventory(self):
        _,_,originals,rows=self.fixture()
        with self.assertRaises(queue.QueueError):queue.plan(originals,rows[1:],[],now="2026-01-01T00:00:00Z")
    def test_sqlite_projection_retains_scope(self):
        outside,inside,originals,rows=self.fixture()
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/"fixture.sqlite"
            with sqlite3.connect(db) as c:
                c.execute("CREATE TABLE originals(sha256 TEXT,scope TEXT)")
                c.execute("CREATE TABLE stage_state(original_sha256 TEXT,stage TEXT,status TEXT,receipt_sha256 TEXT)")
                c.execute("CREATE TABLE work_leases(item_key TEXT)")
                c.executemany("INSERT INTO originals VALUES(?,?)",[(x["sha256"],x["scope"]) for x in originals])
                c.executemany("INSERT INTO stage_state VALUES(?,?,?,?)",[(x["original_sha256"],x["stage"],x["status"],None) for x in rows])
            result=queue.from_ledger(db,now="2026-01-01T00:00:00Z")
        self.assertEqual(result["snapshot_originals"],2)
        self.assertEqual(result["eligible"],1)
        self.assertEqual(result["top"][0]["original_sha256"],inside)
