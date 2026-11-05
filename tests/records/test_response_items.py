"""Per-asked-item response ratings: rules, golden cases, IO and the outputs seam (synthetic only)."""
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records import run_stages
from campaign_tool.records.outcomes import response_items as rs
from tests.records import test_outcome_received_items as _t

Base = _t.ReceivedItemsTests
del _t

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "response_items"
H1, H2, H3 = "11" * 32, "22" * 32, "33" * 32


def item(item_id, kind="records", agency="a", request="r", **extra):
    return {"item_id": item_id, "original_sha256": item_id, "agency_id": agency, "request_id": request,
            "kind": kind, "diagnostics": [], "needs_visual_review_unresolved": False, **extra}


def register(asked, agency="a", ledger="r"):
    return {"requests": [{"agency_key": agency, "request_id": "SYN-1", "ledger_request_id": ledger, "asked": asked}]}


def run(asked, text, **kw):
    out = rs.analyse(register(asked), {"items": [item(H1, **kw)]}, {H1: [(1, text)]}, run_id="t")
    return out["requests"][0]["asks"]


class GoldenTests(unittest.TestCase):
    def test_six_golden_cases_byte_exact(self):
        cases = sorted(p for p in GOLDEN.iterdir() if p.is_dir())
        self.assertEqual([p.name for p in cases], ["blanket-denial", "extension-only", "full-production",
                                                   "no-records", "ocr-unreviewed", "partial-with-exemption"])
        for case in cases:
            load = lambda n: json.loads((case / n).read_text(encoding="utf-8"))  # noqa: E731
            pages = {k: [tuple(x) for x in v] for k, v in load("pages.json").items()}
            got = rs.canon(rs.analyse(load("register.json"), load("received.json"), pages, run_id="golden")) + "\n"
            self.assertEqual(got, (case / "expected.json").read_text(encoding="utf-8"), case.name)


class SplitTests(unittest.TestCase):
    def texts(self, value):
        return [a["text"] for a in rs.split_asked(value)]

    def test_markers_bullets_semicolons_single_empty_unknown(self):
        self.assertEqual(self.texts("1. alpha logs\n2) beta policy\n(3) gamma contract"),
                         ["alpha logs", "beta policy", "gamma contract"])
        self.assertEqual(self.texts("(a) alpha logs; b. beta policy"), ["alpha logs", "beta policy"])
        self.assertEqual(self.texts("Requested:\n- alpha logs\n* beta policy\n• gamma contract"),
                         ["alpha logs", "beta policy", "gamma contract"])
        self.assertEqual(self.texts("alpha logs; beta policy;; gamma contract"),
                         ["alpha logs", "beta policy", "gamma contract"])
        self.assertEqual(self.texts("All audit logs"), ["All audit logs"])
        self.assertEqual([a["ask_id"] for a in rs.split_asked("1. x\n2. y")], ["ask-1", "ask-2"])
        for empty in ("", "  ", None, rs.UNKNOWN_SCOPE):
            self.assertEqual(rs.split_asked(empty), [])

    def test_unknown_scope_is_flagged_and_not_guessed(self):
        out = rs.analyse(register(rs.UNKNOWN_SCOPE), {"items": [item(H1)]}, {H1: [(1, "Attached.")]}, run_id="t")
        self.assertEqual((out["requests"][0]["asks"], out["requests"][0]["flags"]), ([], ["scope_unknown"]))

    def test_long_ask_truncated_and_flagged(self):
        ask = rs.split_asked("word " * 120)[0]
        self.assertEqual((len(ask["text"]), ask["text"][-1], ask["flags"]), (500, "…", ["ask_truncated"]))


class RatingTests(unittest.TestCase):
    def test_negation_and_no_records(self):
        self.assertEqual(run("audit logs", "We are not withholding the audit logs.")[0]["rating"], "missing")
        got = run("audit logs", "There are no responsive records for the audit logs.")[0]
        self.assertEqual(got["rating"], "no_records_stated")
        self.assertEqual(len(got["evidence"]), 1)  # "responsive records" inside the no-records cue is not also an answer

    def test_conflict_is_unclear_with_both_quotes(self):
        text = "The audit logs are attached.\nThe audit logs for March are withheld."
        got = run("audit logs", text)[0]
        self.assertEqual(got["rating"], "unclear")
        self.assertEqual([e["quote"] for e in got["evidence"]],
                         ["The audit logs for March are withheld.", "The audit logs are attached."])
        self.assertIn("conflicting_cues:withheld+answered", got["flags"])

    def test_one_keyword_ask_and_stopword_only_ask(self):
        self.assertEqual(run("contracts", "The contracts are enclosed.")[0]["rating"], "answered")
        self.assertEqual(run("contracts", "The policy is enclosed.")[0]["rating"], "missing")
        self.assertEqual(run("all of the records", "Records enclosed.")[0]["rating"], "missing")  # no content words

    def test_one_shared_word_is_not_enough_for_a_longer_ask(self):
        self.assertEqual(run("camera vendor contracts", "The camera list is enclosed.")[0]["rating"], "missing")

    def test_exemption_quotes_exact_sentence_and_page(self):
        pages = [(1, "Intro line."), (2, "The usage policy is exempt under Gov. Code section 7922.000. Other text.")]
        out = rs.analyse(register("usage policy"), {"items": [item(H1)]}, {H1: pages}, run_id="t")
        ask = out["requests"][0]["asks"][0]
        self.assertEqual(ask["rating"], "exemption_cited")
        entry = ask["evidence"][0]
        self.assertEqual((entry["page"], entry["item_id"]), (2, H1))
        self.assertEqual(entry["quote"], "The usage policy is exempt under Gov. Code section 7922.000.")
        self.assertIn(entry["quote"], pages[1][1])

    def test_citation_with_withhold_is_exemption_not_withheld(self):
        self.assertEqual(run("usage policy", "We withhold the usage policy under Government Code 6254.")[0]["rating"],
                         "exemption_cited")

    def test_unreviewed_text_turns_missing_into_unclear(self):
        got = run("audit logs", "Nothing here.", needs_visual_review_unresolved=True)[0]
        self.assertEqual((got["rating"], "unclear_reason:text_unreviewed" in got["flags"]), ("unclear", True))
        got = run("audit logs", "Audit logs attached.", diagnostics=["page_locator_unavailable"])[0]
        self.assertEqual((got["rating"], "text_unreviewed" in got["flags"]), ("answered", True))

    def test_long_quote_is_clipped_and_flagged(self):
        got = run("audit logs", "Audit logs attached " + "x" * 700 + ".")[0]
        self.assertEqual((len(got["evidence"][0]["quote"]), "quote_truncated" in got["flags"]), (600, True))


class JoinTests(unittest.TestCase):
    def test_null_identity_unlinked_other_ignored_and_no_response_omitted(self):
        received = {"items": [item(H1, agency=None), item(H2, request=None), item(H3, kind="other"),
                              item("44" * 32, agency="z")]}
        out = rs.analyse(register("audit logs"), received, {}, run_id="t")
        self.assertEqual((out["unlinked_items"], out["requests"]), (2, []))
        self.assertEqual(sum(out["counts"].values()), 0)

    def test_other_request_response_is_not_joined(self):
        out = rs.analyse(register("audit logs"), {"items": [item(H1, request="other")]},
                         {H1: [(1, "Audit logs attached.")]}, run_id="t")
        self.assertEqual(out["requests"], [])

    def test_no_text_response_is_unclear_never_missing(self):
        out = rs.analyse(register("audit logs"), {"items": [item(H1)]}, {}, run_id="t")
        self.assertEqual(out["requests"][0]["asks"][0]["rating"], "unclear")

    def test_tripwire_no_socket_or_model(self):
        with patch.object(socket.socket, "connect", side_effect=AssertionError("network")):
            run("audit logs", "Audit logs attached.")


class IoTests(Base):
    """Uses the received-item fixture helpers for a real hash-bound extraction."""
    for _name in [n for n in dir(Base) if n.startswith("test_")]:
        locals()[_name] = None
    del _name

    def seed(self, request="r1"):
        doc, _ = self.document(_t_CALTRANS, "caltrans")
        run_dir = self.project / "run-outputs" / "test-run"
        (run_dir / "outcomes").mkdir(parents=True, mode=0o700)
        self.register_path = run_dir / "CURRENT-REQUEST-REGISTER.json"
        self.received_path = run_dir / "outcomes" / "received-items.json"
        self.register_path.write_text(json.dumps(register("1. The response period; 2. Audit logs", "caltrans", "r1")))
        self.received_path.write_text(json.dumps({
            "schema": "records-received-item-v1", "run_id": "test-run",
            "items": [item(doc, "extension", "caltrans", request)]}))
        return doc

    def out(self):
        return self.project / "run-outputs" / "test-run" / "outcomes" / "response-items.json"

    def test_reads_real_extraction_writes_0600_and_replays(self):
        doc = self.seed()
        report = rs.write_run_response_items(self.project, "test-run")
        asks = report["requests"][0]["asks"]
        self.assertEqual([a["rating"] for a in asks], ["missing", "missing"])
        self.assertIn("mentioned_without_status", asks[0]["flags"])
        self.assertEqual(stat.S_IMODE(self.out().stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.out().parent.stat().st_mode), 0o700)
        inode = self.out().stat().st_ino
        rs.write_run_response_items(self.project, "test-run")
        self.assertEqual(self.out().stat().st_ino, inode)
        self.out().write_bytes(self.out().read_bytes() + b" ")
        with self.assertRaises(OSError) as caught:
            rs.write_run_response_items(self.project, "test-run")
        self.assertEqual(str(caught.exception), "response_items_run_already_exists")
        self.assertTrue(doc)

    def test_missing_inputs_write_nothing(self):
        self.seed()
        self.received_path.unlink()
        self.assertEqual(rs.write_run_response_items(self.project, "test-run")["skipped"], "inputs_missing")
        self.assertFalse(self.out().exists())

    def test_stale_run_and_symlink_and_oversize_refused(self):
        self.seed()
        data = json.loads(self.received_path.read_text())
        self.received_path.write_text(json.dumps(data | {"run_id": "older"}))
        with self.assertRaises(OSError):
            rs.write_run_response_items(self.project, "test-run")
        self.received_path.write_text(json.dumps(data))
        with patch.object(rs, "MAX_INPUT_BYTES", 10), self.assertRaises(OSError):
            rs.write_run_response_items(self.project, "test-run")
        outcomes = self.out().parent
        moved = outcomes.with_name("real-outcomes")
        outcomes.rename(moved)
        outcomes.symlink_to(moved)
        with self.assertRaises(OSError):
            rs.write_run_response_items(self.project, "test-run")

    def test_stage_writes_after_received_items_and_isolates_failure(self):
        self.document(_t_CALTRANS, "caltrans")
        (self.processing / "runs").mkdir(mode=0o700)
        (self.processing / "runs" / "extract-test-run.json").write_text(json.dumps(
            {"schema": "records-extract-outcomes-v1", "run_id": "test-run", "documents": []}))
        code, result = self.stage()
        self.assertEqual((code, result["status"]), (0, "ok"))
        self.assertTrue(self.artifact().exists() and self.out().exists())
        before = self.artifact().read_bytes()
        self.out().unlink()
        with patch.object(rs, "write_run_response_items", side_effect=RuntimeError("private text")):
            code, result = self.stage()
        self.assertEqual((code, result["status"], result["error_code"]), (0, "warn", "OUTPUTS_RESPONSE_ITEMS_UNAVAILABLE"))
        self.assertEqual(self.artifact().read_bytes(), before)
        with patch.object(rs, "write_run_response_items", side_effect=run_stages.StageTimeout()):
            code, result = self.stage()
        self.assertEqual((code, result["error_code"]), (1, "STAGE_CONTAINER_TIMEOUT"))


_t_CALTRANS = ["Caltrans District 5\nWe have received your public records request.\n"
               "Caltrans is extending the response period. We are extending the determination period.\n"
               "We will respond to your request by November 30."]

if __name__ == "__main__":
    unittest.main()
