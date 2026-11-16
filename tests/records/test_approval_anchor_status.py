"""FLK-CON-1: the pinned export's approval-anchor state is shown on Home and Settings, read-only."""
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from campaign_tool import approval
from campaign_tool.records import approval_anchor_status as status, console_export, console_snapshot
from tests.records.test_console_export import tree_state
from tests.records.test_console_model import RUN
from tests.records.test_console_snapshot import OpenSpy, Panel, RUN2, RUN3

ABSENT = object()
TAIL = " Approval anchor state from the pinned export; verification still occurs when decisions are applied."
DEFERRED = "approvals are recorded but DEFERRED until the trust anchor is available"
SNAPSHOTS = {
    "ok": '<p role="status" id="approval-anchor">approvals verify.' + TAIL + "</p>",
    "unavailable": '<p role="status" id="approval-anchor">' + DEFERRED + "." + TAIL + "</p>",
    "unknown": '<p role="status" id="approval-anchor">unknown — ' + DEFERRED + "." + TAIL + "</p>",
}


class FakePinned:
    snapshot = True

    def __init__(self, path):
        self.pin = mock.Mock(path=str(path))


class Normalize(unittest.TestCase):
    def state(self, manifest_bytes):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "EXPORT.json").write_bytes(manifest_bytes)
            return status.from_source(FakePinned(tmp))

    def test_only_the_exact_strings_are_recognised_and_nothing_leaks(self):
        for anchor, expected in (({"state": "ok"}, "ok"), ({"state": "unavailable", "reason": "SECRET-REASON"}, "unavailable"),
                                 (ABSENT, "unknown"), (None, "unknown"), ([], "unknown"), ("ok", "unknown"),
                                 ({}, "unknown"), ({"state": True}, "unknown"), ({"state": ["ok"]}, "unknown"),
                                 ({"state": "OK"}, "unknown"), ({"state": "Unavailable"}, "unknown"),
                                 ({"state": "SECRET-STATE"}, "unknown")):
            manifest = {} if anchor is ABSENT else {"approval_anchor": anchor}
            got = self.state(json.dumps(manifest).encode())
            self.assertEqual(got, expected, anchor)
            text = status.paragraph(got) + status.message(got)
            self.assertNotIn("SECRET", text)
            self.assertEqual(("approvals verify" in text), expected == "ok", anchor)

    def test_missing_oversized_nonregular_malformed_and_live_sources_are_unknown(self):
        self.assertEqual(self.state(b"{not json"), "unknown")
        self.assertEqual(self.state(b'{"approval_anchor": {"state": "ok"}, "approval_anchor": {"state": "ok"}}'), "unknown")
        self.assertEqual(self.state(b" " * (console_snapshot.MAX_MANIFEST + 1)), "unknown")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(status.from_source(FakePinned(tmp)), "unknown")  # no manifest
            os.mkdir(os.path.join(tmp, "EXPORT.json"))
            self.assertEqual(status.from_source(FakePinned(tmp)), "unknown")  # not a regular file
        self.assertEqual(status.from_source(console_snapshot.LiveSource("/nonexistent")), "unknown")
        self.assertEqual(status.from_source(object()), "unknown")
        self.assertEqual(status.from_export_root("/nonexistent/root"), "unknown")
        self.assertEqual(status.paragraph("ok", False), "Approval anchor state: unknown (no pinned export).")


class Served(Panel):
    def export_with(self, anchor, run=RUN):
        """Build a real export of the synthetic live project whose manifest records ``anchor``."""
        dest = self.tmp / ("dest%d" % len(list(self.tmp.glob("dest*"))))
        dest.mkdir(mode=0o700)
        console_export.build(str(self.live), run, str(dest))
        manifest = json.loads((dest / "EXPORT.json").read_text())
        manifest.pop("approval_anchor")
        if anchor is not ABSENT:
            manifest["approval_anchor"] = anchor
        (dest / "EXPORT.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
        buf = io.BytesIO()
        console_export.write_tar(str(dest), buf)
        buf.seek(0)
        return console_export.install(str(self.root), buf)

    def test_home_and_settings_snapshots_for_ok_unavailable_and_old_exports(self):
        self.export_with({"state": "ok"})
        self.up()
        for (anchor, key), run in zip((({"state": "ok"}, "ok"), ({"state": "unavailable", "reason": "SECRET-REASON"}, "unavailable"),
                                       (ABSENT, "unknown")), (RUN, RUN2, RUN3)):
            if key != "ok":
                self.export_with(anchor, run)  # `current` now pins the newer export; the next request follows
            home, settings = self.get("/"), self.get("/settings")
            self.assertIn(SNAPSHOTS[key], home)
            self.assertIn(SNAPSHOTS[key], settings)
            self.assertLess(home.index("<h1>Home</h1>"), home.index(SNAPSHOTS[key]))
            self.assertLess(home.index(SNAPSHOTS[key]), home.index("need your decision"))
            self.assertLess(settings.index("Read-only. Changes are owner-only"), settings.index(SNAPSHOTS[key]))
            self.assertLess(settings.index(SNAPSHOTS[key]), settings.index("<h2>Sending</h2>"))
            for page in (home, settings):
                self.assertNotIn("SECRET", page)
                for other in set(SNAPSHOTS) - {key}:
                    self.assertNotIn(SNAPSHOTS[other], page)
                if key != "ok":
                    self.assertNotIn("approvals verify", page)

    def test_a_swap_after_pinning_keeps_this_requests_state_and_reading_writes_nothing(self):
        self.export_with({"state": "ok"})
        pin = console_snapshot.Pinner(str(self.root)).pin()
        source = console_snapshot.SnapshotSource(pin)
        self.export_with({"state": "unavailable"}, RUN2)  # `current` moves on; the held pin does not
        before = tree_state(self.root)
        spy = OpenSpy(self.root)
        boom = mock.Mock(side_effect=AssertionError("trust/verify touched"))
        with mock.patch("os.open", spy), mock.patch.object(approval, "verify", boom), \
                mock.patch.object(approval, "anchor_status", boom), mock.patch.object(approval, "anchor_state", boom), \
                mock.patch.object(subprocess, "run", boom):
            self.assertEqual(status.from_source(source), "ok")
            self.assertEqual(status.from_export_root(str(self.root)), "unavailable")  # a fresh pin sees the new one
        self.assertEqual((spy.writes, tree_state(self.root)), ([], before))
        boom.assert_not_called()

    def test_a_bad_export_root_is_never_ok_and_is_quiet(self):
        self.export_with({"state": "ok"})
        os.unlink(self.root / "current")
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(status.from_export_root(str(self.root)), "unknown")
        self.assertNotIn(str(self.root), err.getvalue())

