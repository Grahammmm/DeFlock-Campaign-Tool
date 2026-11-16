"""FLK-CMP-5: the export-time count projection and the counts-only reader (synthetic fictional data only)."""
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from campaign_tool.records import campaign_summary as cs, console_export as ce, console_home as home
from campaign_tool.records import console_snapshot as snap, public_updates as pu
from tests.records.test_console_board import RUN
from tests.records.test_console_home import NOW, fixture

PID = "0123456789abcdef"
STAMP = "2026-11-05T00:00:00Z"
SUMMARY = {"schema": cs.SCHEMA, "source_project_id": PID, "run_id": RUN, "evaluated_at": STAMP,
           "counts": {"drafts_waiting": 3, "messages_to_tie": 0, "requests_overdue": 1, "holds_needing_decision": 2},
           "unavailable": []}


def hand_export(root, summary=SUMMARY, listed=True, present=True, exported_at=STAMP, raw=None):
    """A synthetic export under root (private dirs, canonical current link) holding only a manifest and a summary."""
    name = "20261105T000000Z-" + RUN
    top = Path(root) / "exports" / name
    (top / "console-model").mkdir(parents=True, mode=0o700)
    body = raw if raw is not None else json.dumps(summary).encode()
    files = {"queue.json-stand-in": "0" * 64}
    if listed:
        files[cs.REL] = hashlib.sha256(body).hexdigest()
    if present:
        (top / cs.REL).write_bytes(body)
    manifest = {"schema": ce.SCHEMA, "run_id": RUN, "source_project_id": PID, "exported_at": exported_at, "files": files}
    (top / "EXPORT.json").write_text(json.dumps(manifest))
    for path in (Path(root), Path(root) / "exports", top, top / "console-model"):
        os.chmod(path, 0o700)
    os.symlink("exports/" + name, Path(root) / "current")
    return top


def real_export(base, now=NOW):
    """A genuine export of test_console_home's fixture, built at `now`, installed and pinned: (export_root, pin)."""
    live = Path(base) / "live"
    live.mkdir(mode=0o700)
    fixture(live)
    pu.init(str(live))  # a readable (OFF) switch: otherwise the public source is honestly unavailable in the export
    dest = Path(base) / "dest"
    dest.mkdir(mode=0o700)
    with mock.patch.object(ce.time, "time", return_value=now.timestamp()):
        ce.build(str(live), RUN, str(dest))
    out = Path(base) / "export-root"
    out.mkdir(mode=0o700)
    buffer = io.BytesIO()
    ce.write_tar(str(dest), buffer)
    buffer.seek(0)
    ce.install(str(out), buffer)
    return out, snap.Pinner(str(out)).pin()


class CampaignSummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="synthetic-cs-")))
        self.addCleanup(self.cleanup)
        self.binding = {"export_root": str(self.tmp / "root"), "source_project_id": PID}
        (self.tmp / "root").mkdir(mode=0o700)

    def cleanup(self):
        for base, dirs, names in os.walk(self.tmp):
            os.chmod(base, 0o700)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def read(self, now=NOW):
        return cs.read_export(self.binding, now=now)

    def test_counts_match_console_home_for_the_same_export(self):
        root, pin = real_export(self.tmp)
        model = home.load(pin.path, pin.run_id, now=NOW, source=snap.SnapshotSource(pin))
        decisions = home.decision_rows(model, NOW.date())
        independent = {"drafts_waiting": sum(r["status"] == "pending" and r["kind"] != "message_link" for r in model["queue"]["rows"]),
                       "messages_to_tie": sum(r["status"] == "pending" and r["kind"] == "message_link" for r in model["queue"]["rows"]),
                       "requests_overdue": sum(bool(r["overdue"]) for r in model["waiting"]["rows"]),
                       "holds_needing_decision": sum(r["source"] in ("public", "packets") for r in decisions)}
        self.assertEqual(independent["drafts_waiting"], 3)
        self.assertEqual(independent["requests_overdue"], 1)
        self.assertGreater(independent["holds_needing_decision"], 0)
        self.binding = {"export_root": str(root), "source_project_id": pin.source_project_id}
        got = self.read(NOW + timedelta(hours=5))
        self.assertEqual({k: got[k] for k in cs.COUNTS}, independent)
        self.assertEqual((got["state"], got["code"], got["unavailable"], got["age_hours"], got["stale"]), ("ok", None, [], 5, False))
        raw = Path(pin.path, cs.REL).read_text()
        for secret in ("BODY-SENTINEL-1", "person@example.invalid", "alpha-pd", "R-1", "LINE-TEXT-SENTINEL"):
            self.assertNotIn(secret, raw)
        manifest = json.loads(Path(pin.path, "EXPORT.json").read_text())
        self.assertEqual(manifest["files"][cs.REL], hashlib.sha256(raw.encode()).hexdigest())
        self.assertEqual(json.loads(raw)["evaluated_at"], manifest["exported_at"])
        self.assertEqual(oct(os.stat(Path(pin.path, cs.REL)).st_mode & 0o777), "0o400")

    def test_reader_opens_only_the_manifest_and_the_summary(self):
        top = hand_export(self.tmp / "root")
        (top / "queue.json").write_text("ORIGINALS-SENTINEL")
        (top / "mail.eml").write_text("MAIL-SENTINEL")
        opened, live = [], [True]
        hook = lambda event, args: opened.append(os.fsdecode(args[0])) if live[0] and event == "open" and isinstance(args[0], (str, bytes)) else None  # noqa: E731
        sys.addaudithook(hook)
        try:
            got = self.read()
        finally:
            live[0] = False
        self.assertEqual(got["state"], "ok")
        window = opened
        self.assertNotIn("queue.json", window)
        self.assertNotIn("mail.eml", window)
        self.assertTrue({"EXPORT.json", "campaign-summary.json"} <= set(window))

    def test_missing_old_and_corrupt_exports_have_fixed_codes(self):
        self.assertEqual((self.read()["state"], self.read()["code"]), ("no_export", "RECORDS_NO_EXPORT"))
        self.binding["export_root"] = str(self.tmp / "absent")
        self.assertEqual(self.read()["state"], "no_export")
        self.binding["export_root"] = str(self.tmp / "root")
        os.symlink("exports/20261105T000000Z-" + RUN, self.tmp / "root" / "current")  # dangling
        self.assertEqual(self.read()["code"], "RECORDS_EXPORT_UNAVAILABLE")
        os.unlink(self.tmp / "root" / "current")
        cases = {"old": dict(listed=False, present=False), "listed-missing": dict(present=False),
                 "unlisted-present": dict(listed=False), "tampered": dict(raw=b'{"x": 1}'),
                 "bool-count": dict(summary={**SUMMARY, "counts": {**SUMMARY["counts"], "drafts_waiting": True}}),
                 "negative": dict(summary={**SUMMARY, "counts": {**SUMMARY["counts"], "drafts_waiting": -1}}),
                 "null-mismatch": dict(summary={**SUMMARY, "counts": {**SUMMARY["counts"], "drafts_waiting": None}}),
                 "foreign-run": dict(summary={**SUMMARY, "run_id": "run_other"}),
                 "foreign-project": dict(summary={**SUMMARY, "source_project_id": "f" * 16}),
                 "extra-key": dict(summary={**SUMMARY, "reason": "x"}),
                 "nonfinite": dict(raw=b'{"counts": NaN}'), "duplicate": dict(raw=b'{"a": 1, "a": 2}'),
                 "bad-utf8": dict(raw=b"\xff\xfe"), "oversize": dict(raw=b" " * (cs.MAX_SUMMARY + 1))}
        for label, kwargs in cases.items():
            with self.subTest(label):
                shutil.rmtree(self.tmp / "root")
                (self.tmp / "root").mkdir(mode=0o700)
                hand_export(self.tmp / "root", **kwargs)
                got = self.read()
                want = "RECORDS_SUMMARY_NOT_IN_EXPORT" if label == "old" else "RECORDS_EXPORT_UNAVAILABLE"
                self.assertEqual((got["state"], got["code"], got["drafts_waiting"]), ("unavailable", want, None))

    def test_symlinked_leaf_and_wrong_owner_mode_refuse(self):
        top = hand_export(self.tmp / "root")
        (top / "console-model" / "real.json").write_bytes(json.dumps(SUMMARY).encode())
        os.unlink(top / cs.REL)
        os.symlink("real.json", top / cs.REL)
        self.assertEqual(self.read()["code"], "RECORDS_EXPORT_UNAVAILABLE")
        os.chmod(self.tmp / "root", 0o755)
        self.assertEqual(self.read()["code"], "RECORDS_EXPORT_UNAVAILABLE")

    def test_age_and_clock_boundary(self):
        hand_export(self.tmp / "root")
        at = datetime(2026, 11, 5, tzinfo=timezone.utc)
        for delta, hours, stale in ((timedelta(hours=23, minutes=59), 23, False), (timedelta(hours=24), 24, True),
                                    (timedelta(hours=49, minutes=30), 49, True)):
            got = self.read(at + delta)
            self.assertEqual((got["age_hours"], got["stale"], got["code"]), (hours, stale, None))
            self.assertEqual(got["drafts_waiting"], 3)  # counts never change with the clock
        future = self.read(at - timedelta(seconds=1))
        self.assertEqual((future["age_hours"], future["stale"], future["code"], future["drafts_waiting"]),
                         (None, None, "RECORDS_EXPORT_TIME_UNAVAILABLE", 3))
        self.assertIn("stale (49 hours old)", cs.line(self.read(at + timedelta(hours=49))))

    def test_partial_sources_never_zero(self):
        ok = {"state": "ok"}
        def row(status, kind):
            return {"id": kind + status, "status": status, "kind": kind, "agency_id": "a", "request_id": "r",
                    "created_at": None, "revision": 1, "evidence": {}}
        model = {"now": NOW, "queue": {"state": "ok", "rows": [row("pending", "message_link"), row("pending", "reminder-no-response"),
                                                                  row("approved", "first-request")]},
                 "timeline": {"state": "ok", "doc": {"requests": []}},
                 "waiting": {"state": "ok", "rows": [{"overdue": True}, {"overdue": False}]}, "received": ok,
                 "public": {"state": "ok", "lines": [], "switch": "OFF"}, "packets": {"state": "ok", "rows": []},
                 "sends": ok}
        self.assertEqual(cs.from_home(model), {"drafts_waiting": 1, "messages_to_tie": 1, "requests_overdue": 1,
                                                "holds_needing_decision": 0})
        got = cs.from_home({**model, "queue": {"state": "unavailable"}, "packets": {"state": "unavailable"}})
        self.assertEqual((got["drafts_waiting"], got["messages_to_tie"], got["holds_needing_decision"], got["requests_overdue"]),
                         (None, None, None, 1))
        got = cs.from_home({**model, "received": {"state": "unavailable"}})
        self.assertEqual((got["requests_overdue"], got["drafts_waiting"]), (None, 1))
        doc = cs.project(self.tmp / "no-such-export", RUN, PID, "x", NOW.timestamp())  # unreadable source: all null
        self.assertEqual((doc["counts"], doc["unavailable"]), (dict.fromkeys(cs.COUNTS), sorted(cs.COUNTS)))


if __name__ == "__main__":
    unittest.main()
