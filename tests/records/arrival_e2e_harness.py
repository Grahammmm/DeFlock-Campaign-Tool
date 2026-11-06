"""Offline harness for the REC-AUTO-6 arrival proof (synthetic roots only, nothing sent).

It reuses the producer-shaped synthetic ledger/extraction builders of the received-item tests and the
real outputs stage, approval queue, panel server and Telegram composer. See docs/RECORDS-ARRIVAL-PROOF.md
for exactly which links are real and which are stood in for.
"""
import contextlib
import io
import json
import os
import re
import threading
from pathlib import Path
from unittest import mock

from campaign_tool.records import admin_panel, approval_queue, run_stages
from campaign_tool.records.outcomes import received_items as ri
from tests.records import test_outcome_received_items as _received
from tests.records.test_admin_panel import Http

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples" / "run-report" / "records-run-report-v1.synthetic.json"
RUN_1 = "run_20261105T000000Z_abcdef01"
RUN_2 = "run_20261105T010000Z_abcdef02"
CALTRANS = _received.CALTRANS
OLD_DENIAL = ["Atascadero Police Department\nWe deny your request."]
BASE = _received.ReceivedItemsTests


class Roots:
    """Borrow the synthetic-ledger setUp and document builders without re-running those tests."""
    setUp = BASE.setUp
    private, document, enroll, tree, mail = BASE.private, BASE.document, BASE.enroll, BASE.tree, BASE.mail


def run_outputs(project, run_id, arrival=True):
    """Run the real outputs stage in-process; returns (exit code, its single JSON result line)."""
    environment = {"RECORDS_RUN_TRIGGER": "arrival"} if arrival else {}
    out = io.StringIO()
    with mock.patch.dict(os.environ, environment), contextlib.redirect_stdout(out):
        code = run_stages.main(["outputs", "--project", str(project), "--run-id", run_id])
    return code, json.loads(out.getvalue())


def pending_for(project, item_id):
    return [row for row in approval_queue.load_queue(project)
            if row.get("item_id") == item_id and row["status"] == "pending"]


class Panel:
    """The real panel on 127.0.0.1 in a bounded thread; always shut down."""

    def __init__(self, project, run_id, actor="operator-1"):
        self.server = admin_panel.build_server(project, run_id, actor=actor, as_of="2026-11-05", system=False)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.http = Http(self.server.server_address[1])

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.thread.join(5)
        self.server.server_close()

    def page(self):
        return self.http.send("GET", "/", headers={"Content-Length": None, "Content-Type": None})[2]

    def forms(self):
        """(token, draft id) pairs parsed from the approve forms of the served page."""
        return re.findall(r'action="/approvals/approve"><input type="hidden" name="csrf_token" value="([^"]+)">'
                          r'<input type="hidden" name="id" value="([^"]+)">', self.page())


def arrival_report(summary, run_id=RUN_1):
    """A schema-valid arrival report carrying the outputs stage's own summary."""
    report = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    report.update(trigger="arrival", mode="arrival", arrivals=[], run_id=run_id)
    report["arrival_summary"] = summary
    return report
