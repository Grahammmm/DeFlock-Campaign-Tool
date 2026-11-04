"""kit-status and kit-due JSON documents match schemas/kit-{status,due}.schema.json (synthetic campaigns, offline)."""
import contextlib
import copy
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit_track, law  # noqa: E402
from tests.test_kit_check import ready_drafts  # noqa: E402

STATUS = json.loads((REPO_ROOT / "schemas" / "kit-status.schema.json").read_text(encoding="utf-8"))
DUE = json.loads((REPO_ROOT / "schemas" / "kit-due.schema.json").read_text(encoding="utf-8"))
ROW = STATUS["properties"]["requests"]["items"]
OPEN = DUE["properties"]["open"]["items"]
CONDITIONAL = {"more", "correspondence", "correspondence_prefixes", "correspondence_more", "downloads_pending",
               "withdrawn_on"}
STATES = {"waiting_on_you", "producing", "installment_passed", "no_law_package", "past_due", "due_today", "upcoming",
          "download_pending", "download_expired"}
STAMP = "2026-03-01T00:00:00+00:00"
FILLER = "abcd" * 16  # low-entropy synthetic sha256


def run(*arguments):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def hashes(count, start=1):  # distinct synthetic full hashes
    return [format(start + i, "064x") for i in range(count)]


def step(on, kind, **more):
    return {"on": on, "kind": kind, **more}


def check_keys(document, schema, where="$"):
    """Key-set check: no key outside the schema's properties, every required key present (recursive objects)."""
    if isinstance(document, dict):
        extra, missing = set(document) - set(schema["properties"]), set(schema.get("required", [])) - set(document)
        assert not extra and not missing, f"{where}: unexpected keys {sorted(extra)}, missing keys {sorted(missing)}"
        for key, value in document.items():
            sub = schema["properties"][key]
            if "properties" in sub or "items" in sub:
                check_keys(value, sub, f"{where}.{key}")
    elif isinstance(document, list) and "items" in schema:
        for index, item in enumerate(document):
            check_keys(item, schema["items"], f"{where}[{index}]")


def objects(node):  # every object-typed schema node, with its path
    if isinstance(node, dict):
        if "properties" in node:
            yield node
        for value in node.values():
            yield from objects(value)
    elif isinstance(node, list):
        for value in node:
            yield from objects(value)


class KitJsonContractTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-json-contracts-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        code, _, err = run("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                           "--state", "CA")
        self.assertEqual(code, 0, err)
        self.assertEqual(run("kit", "--directory", self.root)[0], 0)
        ready_drafts(self.root)
        self.a, self.b, self.c = (p.stem for p in sorted((self.root / "kit" / "requests").glob("*.md"))[:3])
        self.package = law.load_package("us-ca")
        self.as_of = law.deadline(self.package, "2026-03-05").isoformat()  # b main is due today

    def fixture(self, cap: int | None = 100):
        """One campaign: every conditional key, every row state but no_law_package, a closed request."""
        a, b, c = self.a, self.b, self.c
        main_sha = hashlib.sha256((self.root / "kit" / "requests" / f"{a}.md").read_bytes()).hexdigest()
        rows = {  # (agency, request): (sent_on, extra entry keys, steps)
            (a, "main"): ("2026-03-02", {"draft_sha256": main_sha,
                                                  "scopes": [s["scope_id"] for s in self.package["request_scopes"]]},
                            []),  # plain sent: past due, recorded scopes with no answers
            (a, "over-cap"): ("2026-03-10", {"reference": "R0123-110125"},
                              [step("2026-03-11", "acknowledged"), step("2026-03-12", "fee_estimate", amount_cents=15000)]),
            (a, "paid"): ("2026-03-10", {}, [step("2026-03-11", "extension"),
                                             step("2026-03-12", "fee_estimate", amount_cents=5000),
                                             step("2026-03-13", "fee_paid", amount_cents=5000)]),
            (a, "determined"): ("2026-03-02", {}, [step("2026-03-05", "determination", expected_on="2026-03-10")]),
            (a, "denied"): ("2026-03-02", {}, [step("2026-03-06", "denied", note="synthetic", withheld_basis=["Synthetic exemption"])]),
            (b, "main"): ("2026-03-05", {"correspondence": hashes(2, 900)},
                          [step("2026-03-06", "acknowledged", correspondence=hashes(1, 950))]),
            (b, "bulk"): ("2026-03-02", {}, [step("2026-03-05", "determination"),
                                             step("2026-03-06", "produced", records=hashes(51, 100))]),
            (b, "letters"): ("2026-03-10", {"correspondence": hashes(51, 300)}, []),
            (c, "main"): ("2026-03-10", {}, [step("2026-03-12", "clarification_requested")]),
            (c, "rolling"): ("2026-03-02", {}, [step("2026-03-05", "determination"),
                                                step("2026-03-10", "produced", partial=True, expected_on="2026-03-12",
                                                     records=hashes(2, 500))]),
            (c, "rolling-later"): ("2026-03-02", {}, [step("2026-03-10", "produced", partial=True,
                                                           expected_on="2026-03-25")]),
            (c, "closed"): ("2026-03-02", {}, [step("2026-03-03", "acknowledged"), step("2026-03-04", "closed")]),
            (c, "withdrawn"): ("2026-03-02", {}, [step("2026-03-03", "acknowledged"),
                                                  step("2026-03-04", "withdrawn", note="Sent twice; superseded")]),
        }
        requests = {}
        for (agency, name), (sent, extra, steps) in rows.items():
            key = agency if name == "main" else f"{agency}#{name}"
            stem = agency if name == "main" else f"{agency}--{name}"
            requests[key] = {"sent_on": sent, "channel": "email", "draft": f"requests/{stem}.md",
                             "draft_sha256": FILLER, "recorded_at": STAMP, "steps": steps, **extra}
        (self.root / "kit" / "sent.json").write_text(json.dumps({"schema_version": 1, "requests": requests}),
                                                     encoding="utf-8")
        settings = self.root / "kit" / "request-settings.json"
        if cap is None:
            settings.unlink(missing_ok=True)
        else:
            settings.write_text(json.dumps({"schema_version": 1, "fee_cap_usd": cap}), encoding="utf-8")

    def status(self, *extra):
        code, out, err = run("kit-status", "--directory", self.root, *extra)
        self.assertEqual(code, 0, err)
        return json.loads(out), err

    def due(self, expect=3, *extra):
        code, out, err = run("kit-due", "--directory", self.root, "--as-of", self.as_of, *extra)
        self.assertEqual(code, expect, err)
        return json.loads(out)

    def test_schema_objects_are_closed_and_conditional_keys_are_the_only_optional_ones(self):
        for schema in (STATUS, DUE):
            for node in objects(schema):
                self.assertIs(node.get("additionalProperties"), False, sorted(node["properties"]))
                self.assertLessEqual(set(node.get("required", [])), set(node["properties"]))
        self.assertEqual(set(ROW["properties"]) - set(ROW["required"]), CONDITIONAL)
        self.assertEqual(set(OPEN["properties"]) - set(OPEN["required"]), {"since", "note", "kind", "produced_on"})
        self.assertEqual(set(STATUS["required"]), set(STATUS["properties"]))
        self.assertEqual(set(DUE["properties"]) - set(DUE["required"]), {"download_expired"})

    def test_enums_match_the_code(self):
        self.assertEqual(set(ROW["properties"]["channel"]["enum"]), set(kit_track.CHANNELS))
        self.assertEqual(set(ROW["properties"]["last_step"]["properties"]["kind"]["enum"]), set(kit_track.KINDS))
        self.assertEqual(set(ROW["properties"]["fee_state"]["enum"]), {None, "unpaid", *kit_track.SETTLED.values()})
        self.assertEqual(set(OPEN["properties"]["state"]["enum"]), STATES)

    def test_empty_campaign_documents_validate(self):
        self.assertFalse((self.root / "kit" / "sent.json").exists())
        document, _ = self.status()
        self.assertEqual(document["requests"], [])
        law.validate(document, STATUS)
        document = self.due(expect=0)
        law.validate(document, DUE)
        self.assertEqual((document["open"], document["answered"], document["stated_passed"]), ([], [], []))
        self.assertEqual([document[k] for k in ("past_due", "stated_date_passed", "installment_passed",
                                                "waiting_on_you")], [0, 0, 0, 0])

    def test_status_rows_use_exactly_the_schema_keys_and_each_conditional_key_its_own_fixture(self):
        self.fixture()
        document, stderr = self.status()
        law.validate(document, STATUS)
        check_keys(document, STATUS)
        self.assertEqual(len(document["requests"]), 13)
        extras = {(r["agency_id"], r["request"]): set(r) & CONDITIONAL for r in document["requests"]}
        self.assertEqual({k: v for k, v in extras.items() if v}, {
            (self.b, "main"): {"correspondence", "correspondence_prefixes"},
            (self.b, "bulk"): {"more"},
            (self.b, "letters"): {"correspondence", "correspondence_prefixes", "correspondence_more"},
            (self.c, "withdrawn"): {"withdrawn_on"}})
        rows = {(r["agency_id"], r["request"]): r for r in document["requests"]}
        self.assertEqual((rows[(self.b, "bulk")]["more"], rows[(self.b, "bulk")]["records"]), (1, 51))
        self.assertEqual(len(rows[(self.b, "bulk")]["record_prefixes"]), kit_track.PREFIXES_MAX)
        self.assertEqual(rows[(self.b, "letters")]["correspondence_more"], 1)
        self.assertEqual(rows[(self.a, "over-cap")]["over_fee_cap"], True)
        self.assertEqual(rows[(self.a, "over-cap")]["fee_state"], "unpaid")
        self.assertEqual((rows[(self.a, "paid")]["fee_state"], rows[(self.a, "paid")]["fee_paid"]), ("paid", "$50.00"))
        self.assertTrue(rows[(self.a, "paid")]["extended"] and rows[(self.c, "closed")]["closed"])
        self.assertEqual(rows[(self.c, "main")]["waiting_on_you_since"], "2026-03-12")
        self.assertTrue(rows[(self.c, "rolling")]["producing"])
        self.assertEqual(rows[(self.a, "main")]["withheld_basis"], [])
        self.assertEqual(rows[(self.a, "denied")]["withheld_basis"],
                         [{"on": "2026-03-06", "kind": "denied", "basis": "Synthetic exemption"}])
        self.assertIn("over your fee cap", stderr)
        union = set().union(*map(set, document["requests"]))
        kit_track.record_step(self.root, self.a, "2026-03-11", "produced", download_expires_on="2026-03-20")
        pending, _ = self.status()
        law.validate(pending, STATUS)
        check_keys(pending, STATUS)
        row = next(r for r in pending["requests"] if (r["agency_id"], r["request"]) == (self.a, "main"))
        self.assertEqual(len(row["downloads_pending"]), 1)
        union.update(set().union(*map(set, pending["requests"])))
        self.assertEqual(union, set(ROW["properties"]))  # every schema property is emitted by some row

    def test_status_without_a_fee_cap_reports_over_fee_cap_null(self):
        self.fixture(cap=None)
        document, _ = self.status()
        law.validate(document, STATUS)
        rows = {(r["agency_id"], r["request"]): r for r in document["requests"]}
        self.assertIsNone(rows[(self.a, "over-cap")]["over_fee_cap"])
        self.assertEqual(rows[(self.a, "over-cap")]["fee_estimate"], "$150.00")

    def test_due_document_states_and_keys(self):
        self.fixture()
        document = self.due()
        law.validate(document, DUE)
        check_keys(document, DUE)
        states = {(r["agency_id"], r["request"]): r["state"] for r in document["open"]}
        self.assertEqual(states, {
            (self.a, "main"): "past_due", (self.b, "main"): "due_today", (self.a, "over-cap"): "upcoming",
            (self.a, "paid"): "upcoming", (self.b, "letters"): "upcoming", (self.c, "main"): "waiting_on_you",
            (self.c, "rolling"): "installment_passed", (self.c, "rolling-later"): "producing"})
        self.assertEqual({r["request"] for r in document["answered"]}, {"determined", "denied", "bulk"})
        self.assertEqual([r["state"] for r in document["stated_passed"]], ["stated_date_passed"])
        self.assertEqual({r["request"] for r in document["open"] if "since" in r or "note" in r}, {"main"})
        waiting = [r for r in document["open"] if r["state"] == "waiting_on_you"]
        self.assertEqual(len(waiting), 1)
        self.assertEqual(set(waiting[0]) - set(OPEN["required"]), {"since", "note"})
        self.assertEqual([document[k] for k in ("past_due", "stated_date_passed", "installment_passed",
                                                "waiting_on_you")], [1, 1, 1, 1])
        self.assertNotIn("closed", {r["request"] for r in document["open"] + document["answered"]})

    def test_no_law_package_state_and_null_determination_due_validate(self):
        self.fixture()
        empty = Path(self.root.parent / "no-packages")
        empty.mkdir()
        report = kit_track.status(self.root, law_base=empty)
        self.assertEqual(report["law_package_status"], "missing")
        self.assertEqual({r["determination_due"] for r in report["requests"]}, {None})
        law.validate(report, STATUS)
        document = kit_track.due(self.root, self.as_of, law_base=empty)
        law.validate(document, DUE)
        self.assertIn("no_law_package", {r["state"] for r in document["open"]})
        self.assertEqual(STATES - {r["state"] for r in document["open"]},
                         {"past_due", "due_today", "upcoming", "download_pending", "download_expired"})

    def test_a_planted_extra_key_or_bad_value_fails_the_checks(self):
        self.fixture()
        status, due = self.status()[0], self.due()
        planted = copy.deepcopy(status)
        planted["requests"][0]["surprise"] = 1
        with self.assertRaisesRegex(AssertionError, r"\$\.requests\[0\]: unexpected keys \['surprise'\]"):
            check_keys(planted, STATUS)
        with self.assertRaisesRegex(ValueError, "unexpected field"):
            law.validate(planted, STATUS)
        missing = copy.deepcopy(status)
        del missing["requests"][0]["fee_state"]
        with self.assertRaisesRegex(AssertionError, "missing keys \\['fee_state'\\]"):
            check_keys(missing, STATUS)
        for mutate in (lambda d: d["requests"][0].update(sent_on="03/02/2026"),
                       lambda d: d["requests"][0].update(fee_state="refunded"),
                       lambda d: d["requests"][0].update(over_fee_cap="yes"),
                       lambda d: d["requests"][0].update(channel="fax"),
                       lambda d: d.update(law_package_status="unknown")):
            broken = copy.deepcopy(status)
            mutate(broken)
            with self.subTest(), self.assertRaises(ValueError):
                law.validate(broken, STATUS)
        for mutate in (lambda d: d["open"][0].update(state="overdue"), lambda d: d.update(extra=1),
                       lambda d: d["open"][0].update(surprise=1), lambda d: d["open"][0].pop("days"),
                       lambda d: d["stated_passed"][0].update(state="past_due")):
            broken = copy.deepcopy(due)
            mutate(broken)
            with self.subTest(), self.assertRaises(ValueError):
                law.validate(broken, DUE)
        with self.assertRaisesRegex(AssertionError, "unexpected keys"):
            check_keys({**due, "extra": 1}, DUE)


if __name__ == "__main__":
    unittest.main()
