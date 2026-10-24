"""Offline tests for tools/portal_reconcile.py (design item M-09).

Synthetic fixtures only: example.test hosts and low-entropy placeholder hashes.
No mailbox, queue store, ledger, allowlist or network is touched.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tools import portal_reconcile
from tools.portal_reconcile import CLASSES, reconcile

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "portal_reconcile"
OBSERVATIONS = FIXTURES / "observations.json"
QUEUE = FIXTURES / "queue-export.json"
SCRIPT = ROOT / "tools" / "portal_reconcile.py"

HOST = "records.example.test"
VOCABULARY = {
    "portal-pointer-observations/v1", "portal-queue-export/v1",
    "nextrequest", "govqa", "microsoft_delivery", "other_delivery", "tracked_redirect", "dropbox",
    "pending", "fetching", "retry", "received", "blocked", "done",
    "retry_exhausted", "timeout", "expired_link",
}


def h(char):
    """Low-entropy synthetic 64-hex placeholder (never a real URL hash)."""
    return char * 64


def nr(url_char, request_id=None, item_id=None, path=None, **extra):
    row = {"host_class": "nextrequest", "host": HOST, "url_sha256": h(url_char)}
    if request_id is not None:
        row["request_id"] = request_id
    if item_id is not None:
        row["item_id"] = item_id
    if path is not None:
        row["path"] = path
    row.update(extra)
    return row


def q(request_id, item_id, state, **extra):
    row = {"host": HOST, "request_id": request_id, "item_id": item_id, "state": state}
    row.update(extra)
    return row


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from strings(item)


class PortalReconcileTests(unittest.TestCase):
    def setUp(self):
        self.observations = json.loads(OBSERVATIONS.read_text())["observations"]
        self.queue = json.loads(QUEUE.read_text())["items"]

    def invoke(self, *args):
        return subprocess.run([sys.executable, "-B", str(SCRIPT), *args],
                              capture_output=True, text=True, cwd=ROOT)

    def single(self, observation, queue_rows):
        report = reconcile([observation], queue_rows)
        hits = [name for name in CLASSES if report["classes"][name]]
        self.assertEqual(len(hits), 1, report["classes"])
        return hits[0], report

    # --- every class, one isolated case each -------------------------------

    def test_each_class_isolated(self):
        queue = [q("r1", "fetched-item", "received"),
                 q("r1", "queued-item", "pending"),
                 q("r1", "failed-item", "blocked", reason="retry_exhausted")]
        cases = [
            (nr("1", "r1", "fetched-item"), "fetched"),
            (nr("2", "r1", "queued-item"), "queued"),
            (nr("3", "r1", "failed-item"), "failed"),
            (nr("4", "r1", "absent-item"), "unproven"),
            ({"host_class": "govqa", "host": HOST, "url_sha256": h("5")}, "held_policy"),
            ({"host_class": "microsoft_delivery", "url_sha256": h("6")}, "held_policy"),
            ({"host_class": "other_delivery", "url_sha256": h("7")}, "held_policy"),
            (nr("8", "r1", "fetched-item", allowlisted=False), "held_policy"),
            ({"host_class": "tracked_redirect", "url_sha256": h("9")}, "held_redirect"),
            (nr("a", None, "fetched-item"), "missing_request_identity"),
        ]
        for observation, expected in cases:
            with self.subTest(expected=expected, url=observation["url_sha256"][:1]):
                self.assertEqual(self.single(observation, queue)[0], expected)

    def test_queue_states_and_version_receipt(self):
        for row, expected in [
            (q("r1", "i1", "pending"), "queued"),
            (q("r1", "i1", "fetching"), "queued"),
            (q("r1", "i1", "retry", reason="timeout"), "queued"),
            (q("r1", "i1", "blocked", reason="expired_link"), "failed"),
            (q("r1", "i1", "received"), "fetched"),
            (q("r1", "i1", "received", has_version=True), "fetched"),
            # Refreshed after an earlier verified version: still fetched.
            (q("r1", "i1", "pending", has_version=True), "fetched"),
            (q("r1", "i1", "blocked", has_version=True, reason="timeout"), "fetched"),
            (q("r1", "i1", "pending", has_version=False), "queued"),
        ]:
            with self.subTest(state=row["state"], version=row.get("has_version")):
                self.assertEqual(self.single(nr("1", "r1", "i1"), [row])[0], expected)

    def test_unknown_queue_state_never_matches(self):
        cls, report = self.single(nr("1", "r1", "i1"), [q("r1", "i1", "done")])
        self.assertEqual(cls, "unproven")
        self.assertEqual(report["detail"]["unproven"]["not_in_queue"], 1)
        self.assertEqual(report["queue"]["malformed_reasons"], {"unknown_state": 1})

    def test_received_without_version_is_malformed(self):
        cls, report = self.single(nr("1", "r1", "i1"), [q("r1", "i1", "received", has_version=False)])
        self.assertEqual(cls, "unproven")
        self.assertEqual(report["queue"]["malformed_reasons"], {"received_without_version": 1})

    # --- NextRequest /documents/<item> shape --------------------------------

    def test_documents_item_shape_parsing(self):
        queue = [q("r1", "Doc_9-x", "received")]
        for path in ("/documents/Doc_9-x", "/documents/Doc_9-x/download"):
            with self.subTest(path=path):
                self.assertEqual(self.single(nr("1", "r1", path=path), queue)[0], "fetched")
        # Explicit item id equal to the parsed one is accepted.
        self.assertEqual(self.single(nr("1", "r1", "Doc_9-x", path="/documents/Doc_9-x"), queue)[0], "fetched")

    def test_no_item_id_case(self):
        queue = [q("r1", "i1", "received")]
        for path in ("/requests/r1", "/documents/", "/documents/i1/extra", "/documents/-i1",
                     "/x/documents/i1", "/documents/i1/download/"):
            with self.subTest(path=path):
                cls, report = self.single(nr("1", "r1", path=path), queue)
                self.assertEqual(cls, "unproven")
                self.assertEqual(report["detail"]["unproven"]["no_item_id"], 1)
        cls, report = self.single(nr("1", "r1"), queue)
        self.assertEqual((cls, report["detail"]["unproven"]["no_item_id"]), ("unproven", 1))

    def test_path_rows_refused_when_unsafe_or_inconsistent(self):
        for row, reason in [
            (nr("1", "r1", path="/documents/i1?v=1"), "invalid_path"),
            (nr("1", "r1", path="/documents/i1#frag"), "invalid_path"),
            (nr("1", "r1", path="documents/i1"), "invalid_path"),
            (nr("1", "r1", path="/documents/i 1"), "invalid_path"),
            (nr("1", "r1", "i2", path="/documents/i1"), "item_id_path_mismatch"),
        ]:
            with self.subTest(reason=reason, path=row["path"]):
                report = reconcile([row], [q("r1", "i1", "received")])
                self.assertEqual(report["mail"]["malformed_reasons"], {reason: 1})
                self.assertEqual(sum(report["classes"].values()), 0)

    # --- duplicates ---------------------------------------------------------

    def test_duplicate_observations_count_once(self):
        row = nr("1", "r1", "i1")
        report = reconcile([row, copy.deepcopy(row), copy.deepcopy(row)], [q("r1", "i1", "received")])
        self.assertEqual(report["mail"]["observation_rows"], 3)
        self.assertEqual(report["mail"]["duplicate_observations"], 2)
        self.assertEqual(report["mail"]["unique_urls"], 1)
        self.assertEqual(report["classes"]["fetched"], 1)

    def test_two_urls_same_item_count_two_urls_one_item(self):
        rows = [nr("1", "r1", "i1"), nr("2", "r1", path="/documents/i1/download")]
        report = reconcile(rows, [q("r1", "i1", "received")])
        self.assertEqual(report["classes"]["fetched"], 2)
        self.assertEqual(report["detail"]["fetched_queue_items"], 1)
        self.assertEqual(report["queue"]["referenced_by_mail"], 1)

    def test_conflicting_duplicate_is_counted_not_classified(self):
        rows = [nr("1", "r1", "i1"), {"host_class": "govqa", "host": HOST, "url_sha256": h("1")}]
        for order in (rows, list(reversed(rows))):
            report = reconcile(order, [q("r1", "i1", "received")])
            self.assertEqual(report["mail"]["conflicting_urls"], 1)
            self.assertEqual(report["mail"]["unique_urls"], 1)
            self.assertEqual(sum(report["classes"].values()), 0)
            self.assertEqual(report["classes"]["fetched"], 0)

    def test_duplicate_queue_identity_is_ambiguous(self):
        queue = [q("r1", "i1", "received"), q("r1", "i1", "received")]
        cls, report = self.single(nr("1", "r1", "i1"), queue)
        self.assertEqual(cls, "unproven")
        self.assertEqual(report["detail"]["unproven"]["queue_identity_ambiguous"], 1)
        self.assertEqual(report["queue"]["malformed_reasons"], {"duplicate_identity": 1})

    # --- negative control: held classes are never fetched -------------------

    def test_held_class_never_reported_as_fetched(self):
        queue = [q("r1", "i1", "received", has_version=True)]
        for row, expected in [
            ({"host_class": "govqa", "host": HOST, "request_id": "r1", "item_id": "i1", "url_sha256": h("1")}, "held_policy"),
            ({"host_class": "microsoft_delivery", "host": HOST, "request_id": "r1", "item_id": "i1", "url_sha256": h("2")}, "held_policy"),
            ({"host_class": "other_delivery", "host": HOST, "request_id": "r1", "path": "/documents/i1", "url_sha256": h("3")}, "held_policy"),
            ({"host_class": "tracked_redirect", "host": HOST, "request_id": "r1", "item_id": "i1", "url_sha256": h("4")}, "held_redirect"),
            (nr("5", "r1", "i1", allowlisted=False), "held_policy"),
        ]:
            with self.subTest(host_class=row["host_class"]):
                cls, report = self.single(row, queue)
                self.assertEqual(cls, expected)
                self.assertEqual(report["classes"]["fetched"], 0)
                self.assertEqual(report["detail"]["fetched_queue_items"], 0)
                self.assertEqual(report["queue"]["referenced_by_mail"], 0)

    def test_negative_control_gate_is_decisive(self):
        """Disabling the held-first gate makes the same input report fetched.

        Proves test_held_class_never_reported_as_fetched can fail: only the
        policy-held class set is mutated, every other input is unchanged.
        """
        row = {"host_class": "govqa", "host": HOST, "request_id": "r1", "item_id": "i1", "url_sha256": h("1")}
        queue = [q("r1", "i1", "received", has_version=True)]
        self.assertEqual(self.single(row, queue)[0], "held_policy")
        with mock.patch.object(portal_reconcile, "POLICY_HELD_CLASSES", set()), \
                mock.patch.object(portal_reconcile, "ALLOWLISTED_CLASSES", {"nextrequest", "govqa"}):
            self.assertEqual(self.single(row, queue)[0], "fetched")
        self.assertEqual(self.single(row, queue)[0], "held_policy")

    # --- malformed rows are counted -----------------------------------------

    def test_malformed_observations_counted(self):
        rows = [
            42, None, [],
            {"host_class": "dropbox", "url_sha256": h("1")},
            {"host_class": "nextrequest", "host": HOST, "url_sha256": "ABC"},
            {"host_class": "nextrequest", "host": HOST, "url_sha256": h("a").upper()},
            nr("1", "r1", "i1", url="https://records.example.test/documents/i1"),
            nr("1", "r1", "i1", subject="x"),
            nr("1", "r1", "i1", allowlisted="yes"),
            {"host_class": "nextrequest", "request_id": "r1", "item_id": "i1", "url_sha256": h("1")},
            nr("1", "r1", "i1", host="Bad.Host"),
            nr("1", "r 1", "i1"),
            nr("1", "r1", 7),
        ]
        report = reconcile(rows, [])
        self.assertEqual(report["mail"]["observation_rows"], len(rows))
        self.assertEqual(report["mail"]["malformed_rows"], len(rows))
        self.assertEqual(report["mail"]["malformed_reasons"], {
            "not_object": 3, "unknown_host_class": 1, "invalid_url_sha256": 2,
            "unknown_field": 2, "invalid_allowlisted": 1, "missing_host": 1,
            "invalid_host": 1, "invalid_identity": 2})
        self.assertEqual(sum(report["classes"].values()), 0)

    def test_malformed_queue_rows_counted(self):
        rows = ["x", q("r1", "i1", "done"), {"host": HOST, "item_id": "i1", "state": "pending"},
                q("r1", "i1", "pending", url="https://records.example.test/documents/i1"),
                q("r1", "i1", "pending", has_version="yes"),
                q("r1", "i1", "blocked", reason=5)]
        report = reconcile([], rows)
        self.assertEqual(report["queue"]["rows"], 6)
        self.assertEqual(report["queue"]["malformed_rows"], 6)
        self.assertEqual(report["queue"]["items"], 0)

    def test_unrecognized_reason_text_is_not_echoed(self):
        report = reconcile([nr("1", "r1", "i1")], [q("r1", "i1", "blocked", reason="Failed GET https://x.example.test/a")])
        self.assertEqual(report["detail"]["failed_reasons"], {"unrecognized_reason": 1})
        self.assertNotIn("https", json.dumps(report))

    # --- full synthetic fixture through the CLI -----------------------------

    def test_fixture_counts(self):
        report = reconcile(self.observations, self.queue)
        self.assertEqual(report["classes"], {
            "fetched": 3, "queued": 2, "failed": 2, "unproven": 3, "held_policy": 4,
            "held_redirect": 1, "missing_request_identity": 1})
        self.assertEqual(report["mail"], {
            "observation_rows": 28, "malformed_rows": 9, "duplicate_observations": 2,
            "unique_urls": 17, "conflicting_urls": 1, "classified_urls": 16,
            "malformed_reasons": {"invalid_host": 1, "invalid_identity": 1, "invalid_path": 1,
                                  "invalid_url_sha256": 1, "item_id_path_mismatch": 1,
                                  "missing_host": 1, "not_object": 1, "unknown_field": 1,
                                  "unknown_host_class": 1}})
        self.assertEqual(report["detail"], {
            "unproven": {"no_item_id": 1, "not_in_queue": 1, "queue_identity_ambiguous": 1},
            "failed_reasons": {"expired_link": 1, "retry_exhausted": 1},
            "held_policy_by_host_class": {"govqa": 1, "microsoft_delivery": 1, "nextrequest": 1, "other_delivery": 1},
            "queued_by_state": {"fetching": 0, "pending": 1, "retry": 1},
            "fetched_queue_items": 2})
        self.assertEqual(report["queue"], {
            "rows": 15, "malformed_rows": 6, "items": 8, "ambiguous_identities": 1,
            "malformed_reasons": {"duplicate_identity": 1, "invalid_identity": 1, "not_object": 1,
                                  "received_without_version": 1, "unknown_field": 1, "unknown_state": 1},
            "by_state": {"blocked": 3, "fetching": 0, "pending": 2, "received": 2, "retry": 1},
            "with_version_receipt": 3,
            "failed_reasons": {"expired_link": 1, "retry_exhausted": 1, "unrecognized_reason": 1},
            "referenced_by_mail": 6, "not_referenced_by_mail": 2})
        self.assertEqual(report["by_host_class"]["nextrequest"], {
            "fetched": 3, "queued": 2, "failed": 2, "unproven": 3, "held_policy": 1,
            "held_redirect": 0, "missing_request_identity": 1})
        self.assertEqual(report["by_host_class"]["govqa"]["fetched"], 0)
        self.assertEqual(report["by_host_class"]["tracked_redirect"]["held_redirect"], 1)
        self.assertTrue(report["counts_only"])
        self.assertTrue(report["review_required"])
        # Every unique, non-conflicting URL has exactly one class.
        self.assertEqual(sum(report["classes"].values()),
                         report["mail"]["unique_urls"] - report["mail"]["conflicting_urls"])

    def test_cli_stdout_is_deterministic_and_counts_only(self):
        first = self.invoke("--observations", str(OBSERVATIONS), "--queue-export", str(QUEUE))
        second = self.invoke("--observations", str(OBSERVATIONS), "--queue-export", str(QUEUE))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stderr, "")
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(json.loads(first.stdout), reconcile(self.observations, self.queue))
        sensitive = set()
        for path in (OBSERVATIONS, QUEUE):
            sensitive.update(s for s in strings(json.loads(path.read_text())) if s not in VOCABULARY)
        self.assertIn(HOST, sensitive)
        self.assertIn("26-101", sensitive)
        self.assertIn(h("1"), sensitive)
        for value in sorted(sensitive):
            self.assertNotIn(value, first.stdout)
        for marker in ("https", "http:", "example", "/documents", "@", "doc-"):
            self.assertNotIn(marker, first.stdout)

    def test_output_order_independent(self):
        forward = portal_reconcile.render(reconcile(self.observations, self.queue))
        backward = portal_reconcile.render(reconcile(list(reversed(self.observations)), list(reversed(self.queue))))
        self.assertEqual(forward, backward)

    def test_cli_output_file_new_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.json"
            first = self.invoke("--observations", str(OBSERVATIONS), "--queue-export", str(QUEUE), "--output", str(out))
            self.assertEqual((first.returncode, first.stdout, first.stderr), (0, "", ""))
            self.assertEqual(out.stat().st_mode & 0o777, 0o600)
            saved = out.read_bytes()
            again = self.invoke("--observations", str(OBSERVATIONS), "--queue-export", str(QUEUE), "--output", str(out))
            self.assertEqual(again.returncode, 2)
            self.assertEqual(json.loads(again.stderr), {"error": "output_not_created"})
            self.assertEqual(out.read_bytes(), saved)
            self.assertNotIn(tmp, again.stderr)

    def test_cli_fatal_inputs_are_coded(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "bad.json").write_text("{not json")
            (base / "wrong.json").write_text(json.dumps({"schema": "other", "observations": []}))
            (base / "norows.json").write_text(json.dumps({"schema": "portal-pointer-observations/v1"}))
            os.symlink(OBSERVATIONS, base / "link.json")
            os.mkfifo(base / "fifo.json")
            for name, code in [("bad.json", "input_not_json"), ("wrong.json", "input_schema_mismatch"),
                               ("norows.json", "input_rows_missing"), ("link.json", "input_unreadable"),
                               ("fifo.json", "input_not_regular_file"), ("missing.json", "input_unreadable")]:
                with self.subTest(name=name):
                    result = self.invoke("--observations", str(base / name), "--queue-export", str(QUEUE))
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(json.loads(result.stderr), {"error": code})


if __name__ == "__main__":
    unittest.main()
