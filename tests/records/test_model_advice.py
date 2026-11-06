"""H2O shadow advice: questions, never-raising client, thresholds, files, stage wiring and CLI.

Recorded synthetic responses and a fake transport only; no live model and no record text.
"""
import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from campaign_tool.records import approval_queue as aq
from campaign_tool.records import h2o_questions as hq
from campaign_tool.records import model_advice as ma
from campaign_tool.records import run_stages
from tests.records.test_approval_drafts import item, received, sha
from tests.test_records_run_stages import StageTestCase, synthetic_ledger

FIXTURES = Path(__file__).parent / "fixtures" / "h2o"
CANARY = "CANARY-QUOTE-do-not-persist"
CONFIG = {"enabled": True, "endpoint": ma.DEFAULT_ENDPOINT, "timeout_seconds": 5}


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def reply(name):
    raw = (FIXTURES / name).read_bytes()
    return lambda url, body, timeout: raw


class Calls:
    """Fake transport recording its calls and returning one fixed body (or raising)."""

    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def __call__(self, url, body, timeout):
        self.calls.append((url, body, timeout))
        if self.error:
            raise self.error
        return self.response() if callable(self.response) else self.response


def write_config(project, doc=CONFIG, mode=0o600):
    path = Path(project) / ma.CONFIG_NAME
    path.write_text(json.dumps(doc), encoding="utf-8")
    os.chmod(path, mode)


def chunk_reply(per_call):
    """A transport answering successive calls from a list of (doc_type, p, urgency level)."""
    from tests.records.fixtures.h2o.make_fixtures import answers
    queue = list(per_call)

    def transport(url, body, timeout):
        doc, p, level = queue.pop(0)
        full = answers(doc, p, "yes", 0.95, "not_applicable", 0.05, level, "yes", 0.95)
        asked = json.loads(body)["questions"]
        return json.dumps({"answers": {k: v for k, v in full["answers"].items() if k in asked}}).encode()
    return transport


class QuestionTests(unittest.TestCase):
    def test_questions_match_deep_plan_text(self):
        self.assertEqual(hq.VERSION, "records-v1")
        self.assertEqual(list(hq.QUESTIONS), ["doc_type", "deadline_stated", "completeness", "privacy_present",
                                              "urgency", "followup_needed"])
        q = hq.QUESTIONS
        self.assertEqual(q["doc_type"]["instructions"], "What kind of message is this, from the point of view of "
                         "the person who sent the public-records request?")
        self.assertEqual(list(q["doc_type"]["criteria"]), ["acknowledgement", "extension", "denial", "fee_notice",
                                                            "records", "no_records", "clarification", "other"])
        self.assertEqual(q["deadline_stated"]["instructions"], "Does the message state a specific date, or a number "
                         "of days, by which the agency or the requester must act?")
        self.assertEqual(q["completeness"]["instructions"], "Does this message release all of the requested "
                         "records, part of them, or none?")
        self.assertEqual(q["privacy_present"]["type"], "noul")
        self.assertTrue(q["privacy_present"]["instructions"].startswith("The message contains personal information"))
        self.assertEqual(q["urgency"]["criteria"], ["no action needed", "within a month", "within a week",
                                                    "within two days"])
        self.assertEqual(q["followup_needed"]["instructions"], "Because of this message, must the requester act "
                         "(pay, clarify, appeal or challenge a withholding, or chase missing records)?")


class ClientTests(unittest.TestCase):
    def test_endpoint_must_be_loopback(self):
        for endpoint in ("http://10.0.0.5:8091/v1/systemone", "https://example.org", "file:///etc/passwd",
                         "http://" + "user" + ":" + "pw" + "@127.0.0.1:8091/x", "http://127.0.0.1.example.org/x"):
            transport = Calls(response=b"{}")
            out = ma.ask("text", config={**CONFIG, "endpoint": endpoint}, transport=transport)
            self.assertEqual((out["status"], out["code"]), ("refused", "endpoint_not_loopback"), endpoint)
            self.assertEqual(transport.calls, [])
        for endpoint in ("http://127.0.0.1:8091/v1/systemone", "http://localhost:8091/x", "http://[::1]:8091/x"):
            self.assertTrue(ma.loopback_endpoint(endpoint), endpoint)

    def test_never_raises(self):
        cases = {
            "timeout": Calls(error=TimeoutError()),
            "connection": Calls(error=ConnectionRefusedError()),
            "http_500": Calls(error=urllib.error.HTTPError("u", 500, "x", {}, None)),
            "bad_json": Calls(response=b"not json"),
            "schema": Calls(response=(FIXTURES / "malformed.json").read_bytes()),
        }
        bad = fixture("confident.json")
        bad["answers"]["privacy_present"]["noul"] = float("nan")
        nan = json.dumps(bad).encode()
        cases["schema_nan"] = Calls(response=nan)
        cases["schema_missing"] = Calls(response=b'{"answers": {}}')
        for code, transport in cases.items():
            out = ma.ask("text", config=CONFIG, transport=transport)
            self.assertEqual(out["status"], "unavailable", code)
            self.assertEqual(out["code"], "schema" if code.startswith("schema") else code)
            self.assertIsNone(out["decisions"])

    def test_stage_timeout_passes_through_the_client(self):
        transport = Calls(error=run_stages.StageTimeout())
        with self.assertRaises(run_stages.StageTimeout):
            ma.ask("text", config=CONFIG, transport=transport)
        with tempfile.TemporaryDirectory() as tmp:
            write_config(tmp)
            with self.assertRaises(run_stages.StageTimeout):
                ma.health(tmp, transport=transport)

    def test_budget_is_enforced_inside_chunked_calls(self):
        text = "x" * (ma.CHUNK_CHARS * 2 + 5)
        inner = chunk_reply([("other", 0.97, 1), ("denial", 0.93, 3), ("extension", 0.91, 0)])
        now = [0]
        seen = []

        def transport(url, body, timeout):
            seen.append(timeout)
            now[0] += 100
            return inner(url, body, timeout)
        out = ma.ask(text, config=CONFIG, transport=transport, deadline=130, clock=lambda: now[0])
        self.assertEqual((out["status"], out["code"]), ("unavailable", "budget"))
        self.assertEqual(seen, [5, 5])  # second call got min(5, 130-100)=5; the third is never made
        self.assertEqual(now[0], 200)

    def test_off_and_bad_config_do_not_call(self):
        transport = Calls(response=b"{}")
        self.assertEqual(ma.ask("t", config={"enabled": False}, transport=transport)["status"], "off")
        out = ma.ask("t", config={**CONFIG, "error": "config_not_private"}, transport=transport)
        self.assertEqual((out["status"], out["code"]), ("refused", "config_not_private"))
        self.assertEqual(transport.calls, [])

    def test_long_text_chunked_most_severe(self):
        text = "x" * (ma.CHUNK_CHARS * 2 + 5)
        transport = chunk_reply([("other", 0.97, 1), ("denial", 0.93, 3), ("extension", 0.91, 0)])
        calls = []
        wrapped = lambda u, b, t: (calls.append(json.loads(b)["questions"].keys()), transport(u, b, t))[1]  # noqa: E731
        out = ma.ask(text, config=CONFIG, transport=wrapped)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["answers"]["doc_type"]["choice"], "denial")  # non-other, highest p (0.93 > 0.91)
        self.assertEqual(out["answers"]["urgency"]["score"], 3.0)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(calls[0]), 6)
        self.assertEqual(set(calls[1]), set(hq.CHUNKED))
        calls.clear()
        transport = chunk_reply([("other", 0.97, 0)] * 4)
        ma.ask("y" * (ma.CHUNK_CHARS * 9), config=CONFIG, transport=wrapped)
        self.assertEqual(len(calls), ma.MAX_CHUNKS)  # never more than 4 calls


class DecisionTests(unittest.TestCase):
    def decide(self, name, rule_kind=None):
        out = ma.ask("t", config=CONFIG, transport=reply(name))
        return ma.decide(out["answers"], rule_kind)

    def test_thresholds_and_floor(self):
        from tests.records.fixtures.h2o.make_fixtures import answers
        def d(p, follow_p=0.95, privacy=0.05):
            raw = answers("extension", p, "yes", follow_p, "not_applicable", privacy, 2, "yes", follow_p)["answers"]
            return ma.decide(raw, "other")["typed"], ma.decide(raw, "other")
        self.assertTrue(d(0.90)[0]["doc_type"]["confident"])
        self.assertFalse(d(0.899)[0]["doc_type"]["confident"])
        typed, whole = d(0.95, follow_p=0.801)
        self.assertFalse(typed["followup_needed"]["confident"])  # exactly 0.801 is the floor, not an answer
        self.assertFalse(whole["followup_suggested"])
        self.assertFalse(d(0.95, follow_p=0.199)[0]["followup_needed"]["confident"])
        self.assertTrue(d(0.95, follow_p=0.95)[1]["followup_suggested"])
        self.assertTrue(d(0.95, privacy=0.50)[1]["privacy_flag"])
        self.assertTrue(d(0.95, privacy=0.801)[1]["privacy_flag"])
        self.assertFalse(d(0.95, privacy=0.199)[1]["privacy_flag"])

    def test_disagreement_and_suggested_kind_mapping(self):
        conf = self.decide("confident.json", "other")
        self.assertEqual(conf["suggested_kind"], "extension")
        self.assertTrue(conf["possible_deadline"])
        low = self.decide("lowconfidence.json", "other")
        self.assertIsNone(low["suggested_kind"])  # 0.62 is below the threshold: unsure, never acted on
        self.assertIn("doc_type", low["unsure"])
        self.assertFalse(low["possible_deadline"])
        self.assertIsNone(self.decide("floored.json", "denial")["followup_suggested"] or None)  # 0.801 never acts
        out = ma.ask("t", config=CONFIG, transport=reply("confident.json"))["answers"]
        out["doc_type"] = {"type": "choice", "choice": "records", "probabilities":
                           {k: (0.96 if k == "records" else 0.04 / 7) for k in hq.QUESTIONS["doc_type"]["criteria"]}}
        self.assertTrue(ma.decide(out, "denial")["kind_disagreement"])
        self.assertFalse(ma.decide(out, "records")["kind_disagreement"])
        out["doc_type"]["choice"] = "fee_notice"
        out["doc_type"]["probabilities"] = {k: (0.96 if k == "fee_notice" else 0.04 / 7)
                                            for k in hq.QUESTIONS["doc_type"]["criteria"]}
        self.assertFalse(ma.decide(out, "fee")["kind_disagreement"])  # fee_notice maps to fee
        self.assertIsNone(ma.decide(out, "fee")["suggested_kind"])


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "project"
        self.outcomes = self.project / "run-outputs" / "run-1" / "outcomes"
        self.outcomes.mkdir(parents=True, mode=0o700)
        items = [item(1, "other", quote=CANARY), item(2, "extension", quote=CANARY), item(3, "denial")]
        self.doc = received(items, run_id="run-1")
        (self.outcomes / "received-items.json").write_text(json.dumps(self.doc), encoding="utf-8")
        self.texts = {sha(n + 1000): f"{CANARY} body {n}" for n in (1, 2, 3)}
        write_config(self.project)

    def run_advice(self, transport, **kw):
        return ma.write_run_model_advice(self.project, "run-1", transport=transport, texts=self.texts, **kw)

    def test_off_by_default_no_files_no_calls(self):
        (self.project / ma.CONFIG_NAME).unlink()
        transport = Calls(response=reply("confident.json"))
        self.assertEqual(self.run_advice(transport), {"status": "off"})
        self.assertEqual(transport.calls, [])
        self.assertFalse((self.outcomes / "model-advice.json").exists())
        write_config(self.project, {"enabled": False})
        self.assertEqual(self.run_advice(transport)["status"], "off")
        write_config(self.project, CONFIG, mode=0o644)  # not owner-only: refused, nothing written
        self.assertEqual(self.run_advice(transport), {"status": "refused", "code": "config_not_private"})
        self.assertEqual(transport.calls, [])

    def test_advice_file_has_no_text_and_agreement(self):
        transport = Calls(response=lambda: (FIXTURES / "confident.json").read_bytes())
        with mock.patch.object(aq, "load_queue", return_value=[{"item_id": sha(2)}]):
            self.assertEqual(self.run_advice(transport), {"status": "ok", "items": 3})
        for name in ("model-advice.json", "model-advice-agreement.json"):
            path = self.outcomes / name
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertNotIn(CANARY, path.read_text(encoding="utf-8"))
        advice = json.loads((self.outcomes / "model-advice.json").read_text())
        self.assertEqual((advice["schema"], advice["version"], len(advice["items"])), (ma.ADVICE_SCHEMA, "records-v1", 3))
        self.assertEqual(advice["items"][0]["decisions"]["suggested_kind"], "extension")  # rule kind other
        agree = json.loads((self.outcomes / "model-advice-agreement.json").read_text())
        self.assertEqual(agree["kind_compared"], 3)
        self.assertAlmostEqual(agree["kind_agreement_rate"], 1 / 3)  # only the extension item agrees
        self.assertEqual(agree["followup_vs_draft"], {"suggested": 3, "suggested_with_draft": 1,
                                                       "suggested_without_draft": 2, "draft_not_suggested": 0})
        self.assertEqual(agree["unavailable"], 0)

    def test_unavailable_service_records_status_not_decisions(self):
        self.run_advice(Calls(error=ConnectionRefusedError()))
        advice = json.loads((self.outcomes / "model-advice.json").read_text())
        self.assertEqual({(i["status"], i["code"], i["decisions"]) for i in advice["items"]},
                         {("unavailable", "connection", None)})
        agree = json.loads((self.outcomes / "model-advice-agreement.json").read_text())
        self.assertEqual((agree["unavailable"], agree["kind_agreement_rate"]), (3, None))

    def test_budget_marks_rest_unavailable(self):
        ticks = iter([0, 0, 130, 131])
        transport = Calls(response=lambda: (FIXTURES / "confident.json").read_bytes())
        self.run_advice(transport, clock=lambda: next(ticks))
        statuses = [(i["status"], i.get("code")) for i in json.loads((self.outcomes / "model-advice.json").read_text())["items"]]
        self.assertEqual(statuses, [("ok", None), ("unavailable", "budget"), ("unavailable", "budget")])
        self.assertEqual(len(transport.calls), 1)

    def test_missing_input_raises_for_the_stage_to_warn(self):
        (self.outcomes / "received-items.json").unlink()
        with self.assertRaises(OSError):
            self.run_advice(Calls(response=b"{}"))


class StageAndCliTests(StageTestCase):
    def seam(self, doc):
        if not self.ledger.exists():
            import sqlite3
            from contextlib import closing
            synthetic_ledger(self.ledger)
            with closing(sqlite3.connect(self.portal / "portal.sqlite")) as pdb, pdb:
                pdb.execute("CREATE TABLE portal_items(id TEXT)")
            (self.processing / "runs").mkdir(mode=0o700)
            (self.processing / "runs" / "extract-run-1.json").write_text(json.dumps(
                {"schema": "records-extract-outcomes-v1", "run_id": "run-1", "documents": []}))
        from campaign_tool.records.outcomes import received_items, response_items

        def fake(project, run_id):
            target = Path(project) / "run-outputs" / run_id / "outcomes"
            target.mkdir(parents=True, mode=0o700, exist_ok=True)
            (target / "received-items.json").write_text(json.dumps(doc), encoding="utf-8")
        with mock.patch.object(received_items, "write_run_received_items", fake), \
                mock.patch.object(response_items, "write_run_response_items", lambda project, run_id: None):
            return self.stage("outputs")

    def tree(self):
        return {str(p.relative_to(self.project)): p.read_bytes()
                for p in sorted(self.project.rglob("*")) if p.is_file() and p != self.project / ma.CONFIG_NAME}

    def test_shadow_guarantee(self):
        doc = received([item(1, "other"), item(2, "extension"), item(4, "response")], run_id="run-1")
        texts = {sha(n + 1000): "body" for n in (1, 2, 4)}
        transport = Calls(response=lambda: (FIXTURES / "confident.json").read_bytes())
        self.assertEqual(self.seam(doc)[1]["status"], "ok")
        before = self.tree()
        self.assertFalse([k for k in before if "model-advice" in k])
        self.assertEqual(transport.calls, [])
        write_config(self.project)
        with mock.patch.object(ma, "item_texts", lambda project, items: texts), mock.patch.object(ma, "_post", transport):
            code, parsed, _ = self.seam(doc)
        self.assertEqual((code, parsed["status"]), (0, "ok"))
        after = self.tree()
        self.assertEqual({k for k in after} - {k for k in before},
                         {"run-outputs/run-1/outcomes/model-advice.json",
                          "run-outputs/run-1/outcomes/model-advice-agreement.json"})
        for key, value in before.items():  # received-items, queue.jsonl and every report byte-identical
            self.assertEqual(after[key], value, key)
        self.assertTrue(any(k.endswith("received-items.json") for k in before))
        self.assertTrue(any(k.endswith("queue.jsonl") for k in before))
        self.assertEqual(len(transport.calls), 3)

    def test_stage_warning_only(self):
        doc = received([item(1, "other")], run_id="run-1")
        write_config(self.project)
        with mock.patch.object(ma, "write_run_model_advice", side_effect=RuntimeError("boom /private/path")):
            code, parsed, raw = self.seam(doc)
        self.assertEqual((code, parsed["error_code"]), (0, "OUTPUTS_MODEL_ADVICE_UNAVAILABLE"))
        self.assertNotIn("boom", raw)
        self.assertTrue((self.project / "run-outputs" / "run-1.json").is_file())
        with mock.patch.object(ma, "write_run_model_advice", side_effect=run_stages.StageTimeout()):
            code, parsed, _ = self.seam(doc)
        self.assertEqual((code, parsed["error_code"]), (1, "STAGE_CONTAINER_TIMEOUT"))

    def test_cli_run_and_health(self):
        from campaign_tool.records import cli
        self.assertEqual(cli.COMMANDS["model-advice"], ("model_advice", "main"))
        outcomes = self.project / "run-outputs" / "run-1" / "outcomes"
        outcomes.mkdir(parents=True, mode=0o700)
        (outcomes / "received-items.json").write_text(json.dumps(received([item(1, "other")])), encoding="utf-8")
        write_config(self.project)
        texts = {sha(1001): "body"}

        def cli_run(*argv):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), mock.patch("sys.argv", ["records model-advice", *argv]):
                code = ma.main()
            return code, out.getvalue().strip()
        transport = Calls(response=lambda: (FIXTURES / "confident.json").read_bytes())
        with mock.patch.object(ma, "TRANSPORT", transport), mock.patch.object(ma, "item_texts", lambda p, i: texts):
            self.assertEqual(cli_run("run", "--project", str(self.project), "--run-id", "run-1"), (0, "ok"))
            self.assertEqual(cli_run("health", "--project", str(self.project)), (0, "ok"))
        self.assertTrue((outcomes / "model-advice.json").is_file())
        self.assertTrue(transport.calls[-1][0].endswith("/health"))
        down = Calls(error=ConnectionRefusedError())
        with mock.patch.object(ma, "TRANSPORT", down):
            self.assertEqual(cli_run("health", "--project", str(self.project)), (1, "unavailable connection"))
        write_config(self.project, {**CONFIG, "endpoint": "http://10.0.0.5:8091/x"})
        self.assertEqual(cli_run("health", "--project", str(self.project)), (1, "refused endpoint_not_loopback"))


if __name__ == "__main__":
    unittest.main()
