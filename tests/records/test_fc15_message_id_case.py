"""Independently Sol-authored fictional FC15 cases; ordinary discovery is inert.

Script execution installs scoped offline sentinels before product imports. Every
ordinary test also installs/restores its own sentinels. No permanent audit hooks.
"""
import argparse
import ast
from contextlib import ExitStack, contextmanager
import copy
import hashlib
import importlib
import inspect
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests/fixtures/fc15-message-id-case/cases.json"


@contextmanager
def offline():
    with ExitStack() as stack:
        for target in ("socket.socket.connect", "socket.socket.connect_ex",
                       "socket.socket.bind", "socket.create_connection",
                       "socket.getaddrinfo", "sqlite3.connect", "subprocess.Popen",
                       "os.system"):
            stack.enter_context(mock.patch(target, side_effect=RuntimeError("offline_sentinel")))
        yield


class MessageIDCaseTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(offline())
        self.c = importlib.import_module("campaign_tool.records.understanding.correspondence")
        self.e = importlib.import_module("campaign_tool.records.understanding.evidence")
        self.f = json.loads(FIXTURE.read_text())

    def event(self, token="lowercase", **extra):
        return {"agency_id": self.f["agency"], "request_number": self.f["request"],
                "message_id": token, "event_type": "request", "occurred_at": self.f["time"],
                "text": "Please provide fictional audit logs.", "items_requested": ["audit logs"],
                **extra}

    def track(self, events, **kwargs):
        before = copy.deepcopy(events)
        bundle_before = copy.deepcopy(kwargs.get("bundle"))
        result = self.c.track(events, as_of=self.f["as_of"], **kwargs)
        self.assertEqual(events, before, "input preservation")
        self.assertEqual(kwargs.get("bundle"), bundle_before, "source preservation")
        self.assertIs(result["review_acceptance"], False)
        self.assertEqual(result["semantic_assessment"], "NOT_ASSESSED")
        for request in result["requests"]:
            self.assertIs(request["private_followup_draft"]["send"], False)
            self.assertIs(request["private_followup_draft"]["requires_human_review"], True)
            self.assertEqual(request["semantic_assessment"], "NOT_ASSESSED")
        return result

    def assert_distinct(self, events, **kwargs):
        out = self.track(events, **kwargs)
        self.assertEqual(out["holds"], [])
        self.assertEqual(len(out["requests"]), 1)
        rows = out["requests"][0]["timeline"]
        self.assertEqual(len(rows), 2, "case_distinct_requires_two_rows")
        self.assertEqual({row["deduplication_key"] for row in rows},
                         {"message:" + event["message_id"].strip() for event in events})
        self.assertEqual([row["occurrence_count"] for row in rows], [1, 1])
        self.assertEqual(out["requests"][0]["occurrence_count"], 2)
        return out

    def test_case_distinct(self):
        for upper, lower in self.f["case_pairs"]:
            for reverse in (False, True):
                with self.subTest(case="case_distinct", pair=[upper, lower], reverse=reverse):
                    events = [self.event(upper, occurrence_id="one", source_id="source-one"),
                              self.event(lower, occurrence_id="two", source_id="source-two")]
                    out = self.assert_distinct(events[::-1] if reverse else events)
                    occurrences = [o for row in out["requests"][0]["timeline"] for o in row["occurrences"]]
                    self.assertEqual({o["occurrence_id"] for o in occurrences}, {"one", "two"})
                    self.assertEqual({o["source_id"] for o in occurrences}, {"source-one", "source-two"})
                    self.assertEqual({o["occurred_at"] for o in occurrences}, {self.f["time"]})

    def test_exact_replay(self):
        for token in ("lowercase", "A", " opaque  ID ", ""):
            with self.subTest(case="exact_replay", token=token):
                events = [self.event(token, occurrence_id=str(i)) for i in range(3)]
                out = self.track(events)
                self.assertEqual(out["holds"], [])
                rows = out["requests"][0]["timeline"]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["occurrence_count"], 3)
                self.assertEqual([o["occurrence_id"] for o in rows[0]["occurrences"]], ["0", "1", "2"])
                if token == "lowercase":
                    self.assertEqual(rows[0]["deduplication_key"], "message:lowercase")
                elif not token:
                    self.assertTrue(rows[0]["deduplication_key"].startswith("sha256:"))
        with self.subTest(case="exact_replay", token="normalized_whitespace"):
            out = self.track([self.event(" opaque\tID "), self.event("opaque ID")])
            self.assertEqual(len(out["requests"][0]["timeline"]), 1)
            self.assertEqual(out["requests"][0]["timeline"][0]["occurrence_count"], 2)

    def test_fallback_event_id(self):
        for missing in (None, "", False):
            for reverse in (False, True):
                with self.subTest(case="fallback_event_id", missing=missing, reverse=reverse):
                    events = [self.event(missing, event_id=x) for x in ("A", "a")]
                    if missing is None:
                        for event in events:
                            event.pop("message_id")
                    out = self.track(events[::-1] if reverse else events)
                    self.assertEqual(out["holds"], [])
                    self.assertEqual({r["deduplication_key"] for r in out["requests"][0]["timeline"]},
                                     {"message:A", "message:a"})
                    self.assertEqual([r["occurrence_count"] for r in out["requests"][0]["timeline"]], [1, 1])
        with self.subTest(case="fallback_event_id", precedence=True):
            out = self.track([self.event("chosen", event_id="A"), self.event("chosen", event_id="a")])
            self.assertEqual(out["holds"], [])
            self.assertEqual(len(out["requests"][0]["timeline"]), 1)
            self.assertEqual(out["requests"][0]["timeline"][0]["deduplication_key"], "message:chosen")
            self.assertEqual(out["requests"][0]["timeline"][0]["occurrence_count"], 2)

    def test_content_conflict(self):
        for reverse in (False, True):
            with self.subTest(case="content_conflict", reverse=reverse):
                events = [self.event("Same", items_requested=["audit logs"]),
                          self.event("Same", items_requested=["contracts"])]
                out = self.track(events[::-1] if reverse else events)
                self.assertEqual([h["reason"] for h in out["holds"]], ["duplicate_message_content_conflict"])
                self.assertEqual(len(out["requests"][0]["timeline"]), 1)
                self.assertEqual(out["requests"][0]["occurrence_count"], 1)
                self.assertEqual(out["requests"][0]["timeline"][0]["items_requested"],
                                 events[1 if reverse else 0]["items_requested"])

    def cited(self):
        bundles = []
        for token in ("A", "a"):
            original = hashlib.sha256(("fictional-original-" + token).encode()).hexdigest()
            text = "Message-ID: " + token + "\nPublic records request " + self.f["request"]
            receipt = {"schema": "format-extraction-v1", "original_sha256": original,
                       "original_changed": False, "status": "complete", "parser": "synthetic",
                       "parser_version": "0", "route": {"format": "text"}, "pages": [],
                       "units": [{"kind": "text", "locator": {"part": "body"}, "text": text,
                                  "data": {"headers": [["Message-ID", token]]}}]}
            bundles.append(self.e.normalize_receipt(receipt, original, self.e.sha(receipt)))
        bundle = self.e.bundle([u for b in bundles for u in b["units"]],
                               {k: v for b in bundles for k, v in b["source_context"].items()},
                               [r for b in bundles for r in b["source_receipts"]])
        events = self.c.events_from_bundle(bundle, self.f["agency"], [self.f["request"]])
        for event in events:
            event["occurred_at"] = self.f["time"]
        return bundle, events

    def test_source_cited(self):
        bundle, events = self.cited()
        self.assertEqual(len(events), 2)
        self.assertEqual({e["message_id"] for e in events}, {"A", "a"})
        snapshot = bundle["snapshot_sha256"]
        for reverse in (False, True):
            with self.subTest(case="source_cited", verified=True, reverse=reverse):
                out = self.assert_distinct(events[::-1] if reverse else events, bundle=bundle)
                self.assertEqual(out["snapshot_sha256"], snapshot)
                for row in out["requests"][0]["timeline"]:
                    for cite in row["citations"]:
                        self.e.validate_citation(cite, bundle)
                    self.e.validate_citation(row["message_id_citation"], bundle)
        for mode in ("missing", "mismatch"):
            with self.subTest(case="source_cited", verified=False, mode=mode):
                event = copy.deepcopy(events[0])
                if mode == "missing":
                    event.pop("message_id_citation")
                    reason = "message_id_unverified"
                else:
                    event["message_id"] = event["message_id"].swapcase()
                    reason = "message_id_source_value_mismatch"
                out = self.track([event], bundle=bundle)
                self.assertEqual([h["reason"] for h in out["holds"]], [reason])
                fields = ("agency_id", "request_number", "request_numbers", "occurred_at",
                          "event_type", "kind", "text", "items_requested", "items_delivered",
                          "due_at", "due_kind")
                normalized = dict(event, request_number=self.f["request"])
                raw = json.dumps({k: normalized[k] for k in fields if k in normalized},
                                 sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
                self.assertEqual(out["requests"][0]["timeline"][0]["deduplication_key"],
                                 "sha256:" + hashlib.sha256(raw).hexdigest())
                self.assertTrue(out["requests"][0]["timeline"][0]["deduplication_key"].startswith("sha256:"))

    def test_scope_and_preservation(self):
        with self.subTest(case="scope_and_preservation", grouping=True):
            events = [self.event("same"), self.event("same", agency_id=self.f["agency"].upper(),
                                                     request_number=self.f["request"].lower())]
            out = self.track(events)
            self.assertEqual(out["holds"], [])
            self.assertEqual(len(out["requests"]), 1)
            self.assertEqual(out["requests"][0]["request_id"], "example agency::r-fc15")
            self.assertEqual(out["requests"][0]["timeline"][0]["occurrence_count"], 2)
        with self.subTest(case="scope_and_preservation", content_hash=True):
            fields = ("agency_id", "request_number", "request_numbers", "occurred_at", "event_type",
                      "kind", "text", "items_requested", "items_delivered", "due_at", "due_kind")
            event = self.event("")
            raw = json.dumps({k: event[k] for k in fields if k in event}, sort_keys=True,
                             ensure_ascii=False, separators=(",", ":"), default=str).encode()
            expected = "sha256:" + hashlib.sha256(raw).hexdigest()
            self.assertEqual(self.track([event])["requests"][0]["timeline"][0]["deduplication_key"], expected)
        with self.subTest(case="scope_and_preservation", priority=True):
            event = self.event("priority", text="We received your request. Attached responsive records.")
            event.pop("event_type")
            self.assertEqual(self.track([event])["requests"][0]["timeline"][0]["event_type"], "delivery")
        with self.subTest(case="scope_and_preservation", clock=True):
            out = self.track([self.event("A", occurred_at=None), self.event("a")])
            self.assertIn("event_order_uncertain", [h["reason"] for h in out["holds"]])

    def test_deciding_control(self):
        # Change only the return expression in an isolated source copy; retain
        # every other statement, including content hashing and whitespace rules.
        src = textwrap.dedent(inspect.getsource(self.c._event_key))
        old = 'return "message:" + message_id'
        new = 'return "message:" + message_id.casefold()'
        self.assertEqual(src.count(old), 1)
        self.assertNotIn("message_id.casefold()", src)
        namespace = dict(self.c.__dict__)
        exec(compile(ast.parse(src.replace(old, new)), "<synthetic-casefold-mutant>", "exec"), namespace)
        with mock.patch.object(self.c, "_event_key", namespace["_event_key"]):
            events = [self.event("A"), self.event("a")]
            self.assertEqual(len(self.track(events)["requests"][0]["timeline"]), 1)
            with self.assertRaisesRegex(AssertionError, "case_distinct_requires_two_rows"):
                self.assert_distinct(events)
            replay = self.track([self.event("lowercase"), self.event("lowercase")])
            self.assertEqual(len(replay["requests"][0]["timeline"]), 1)
            self.assertEqual(replay["requests"][0]["timeline"][0]["occurrence_count"], 2)
        self.assert_distinct([self.event("A"), self.event("a")])


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ids = []
        self.subcases = []

    def startTest(self, test):
        self.ids.append(test.id())
        super().startTest(test)

    def addSubTest(self, test, subtest, err):
        self.subcases.append({"id": subtest.id(), "passed": err is None})
        super().addSubTest(test, subtest, err)


def script_main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--temporary-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--mode", choices=("suite", "baseline", "compatibility", "peer"), default="suite")
    args = parser.parse_args()
    args.temporary_root.mkdir(parents=True, exist_ok=True)
    old_temp, old_bytecode = tempfile.tempdir, sys.dont_write_bytecode
    try:
        tempfile.tempdir = str(args.temporary_root.resolve())
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(REPO))
        with offline():
            loader = unittest.TestLoader()
            if args.mode == "compatibility":
                suite = loader.loadTestsFromName("tests.records.test_understanding.CorrespondenceTests")
            elif args.mode == "baseline":
                suite = unittest.TestSuite([MessageIDCaseTests("test_case_distinct"),
                                            MessageIDCaseTests("test_exact_replay")])
            elif args.mode == "peer":
                environment, temp = dict(os.environ), tempfile.tempdir
                original_connect, original_popen = socket.socket.connect, subprocess.Popen

                class Peer(unittest.TestCase):
                    def runTest(self):
                        self.assertEqual(dict(os.environ), environment)
                        self.assertEqual(tempfile.tempdir, temp)
                        self.assertIs(socket.socket.connect, original_connect)
                        self.assertIs(subprocess.Popen, original_popen)
                        with tempfile.TemporaryDirectory() as directory:
                            self.assertEqual(Path(directory).parent, Path(temp))

                suite = unittest.TestSuite([Peer(), loader.discover(str(REPO / "tests/records"),
                                          pattern=Path(__file__).name), Peer()])
            else:
                suite = loader.loadTestsFromTestCase(MessageIDCaseTests)
            result = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(suite)
        report = {"tests": result.testsRun, "ids": result.ids, "subcases": result.subcases,
                  "skips": [(t.id(), reason) for t, reason in result.skipped],
                  "failures": [(t.id(), detail) for t, detail in result.failures],
                  "errors": [(t.id(), detail) for t, detail in result.errors],
                  "successful": result.wasSuccessful(), "mode": args.mode,
                  "fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest()}
        args.report.write_text(json.dumps(report, indent=2))
        return 0 if result.wasSuccessful() and not result.skipped else 1
    finally:
        tempfile.tempdir, sys.dont_write_bytecode = old_temp, old_bytecode


if __name__ == "__main__":
    raise SystemExit(script_main())
