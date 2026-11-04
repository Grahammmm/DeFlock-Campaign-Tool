"""MuckRock-filed requests in the kit: kit-sent --channel muckrock --request-url and kit-muckrock-import from a saved
JSON. Offline, synthetic: invented request ids, and no socket may be opened anywhere in this module."""
import contextlib
import io
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit, kit_track  # noqa: E402
from tests.test_kit_check import ready_drafts  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "muckrock"
URL = "https://www.muckrock.com/foi/synthetic-county-123/synthetic-alpr-policy-request-90001/"
SENT_ON = "2026-03-02"


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


class KitMuckrockTests(unittest.TestCase):
    def setUp(self):
        for target, effect in (("socket.socket", AssertionError("socket opened")),
                               ("urllib.request.urlopen", AssertionError("network used"))):
            patcher = mock.patch(target, side_effect=effect)
            patcher.start()
            self.addCleanup(patcher.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-muckrock-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        self.assertEqual(run("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                             "--state", "CA")[0], 0)
        self.assertEqual(run("kit", "--directory", self.root)[0], 0)
        self.sent = self.root / "kit" / "sent.json"
        drafts = sorted((self.root / "kit" / "requests").glob("*.md"))
        self.agency, self.other = drafts[0].stem, drafts[1].stem
        ready_drafts(self.root)

    def send(self, channel="muckrock", *extra, url: str | None = URL, agency=None):
        args = ["kit-sent", "--directory", self.root, "--agency", agency or self.agency, "--date", SENT_ON,
                "--channel", channel, *extra]
        return run(*args, *(["--request-url", url] if url is not None else []))

    def filed(self, steps=()):  # a muckrock entry, then hand-recorded steps (kind, date)
        self.assertEqual(self.send()[0], 0)
        for kind, on in steps:
            code, _, err = run("kit-response", "--directory", self.root, "--agency", self.agency, "--date", on,
                               "--kind", kind)
            self.assertEqual(code, 0, err)

    def saved(self, **fields):  # a copy of the base fixture with fields replaced (None removes one)
        document = {**json.loads((FIXTURES / "request-90001.json").read_text(encoding="utf-8")), **fields}
        path = self.tmp / "saved.json"
        path.write_text(json.dumps({k: v for k, v in document.items() if v is not None}), encoding="utf-8")
        return path

    def imp(self, path, *extra, agency=None):
        return run("kit-muckrock-import", "--directory", self.root, "--agency", agency or self.agency,
                   "--from-json", path, *extra)

    def steps(self):
        return json.loads(self.sent.read_text(encoding="utf-8"))["requests"][self.agency]["steps"]

    # --- kit-sent --channel muckrock --request-url
    def test_request_url_is_recorded_for_muckrock(self):
        code, out, err = self.send()
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out[:out.rindex("}") + 1])["request_url"], URL)
        self.assertEqual(json.loads(self.sent.read_text(encoding="utf-8"))["requests"][self.agency]["request_url"], URL)
        self.assertEqual(kit_track.muckrock_id(kit_track.read_sent(self.root)[self.agency]), "90001")
        code, out, _ = run("kit-status", "--directory", self.root)
        self.assertEqual((code, json.loads(out)["requests"][0]["channel"]), (0, "muckrock"))

    def test_request_url_rules_refuse_and_write_nothing(self):
        self.assertEqual(self.send(url=None)[0], 1)  # required with muckrock
        self.assertEqual(self.send("email")[0], 1)  # refused with another channel
        base = "https://www.muckrock.com/foi/synthetic-county-123/request-90001/"
        for bad in (base + "?x=1", base.replace("https", "http"), base.replace("www.muckrock.com", "example.org"),
                    base.replace("www.", "user@www."), base.replace(".com", ".com:443"), base + "#top",
                    "https://www.muckrock.com/foi/synthetic-county-123/request/", "https://www.muckrock.com/foi/",
                    "https://www.muckrock.com/foi/a-1/b-2/c-3/d-4/", "https://muckrock.com/foi/request-90001/",
                    "https://www.muckrock.com/foi/request-90001/\n", "https://www.muckrock.com/foi/" + "a" * 300 + "-1/"):
            with self.subTest(url=bad[:70]):
                code, _, err = self.send(url=bad)
                self.assertEqual(code, 1)
                self.assertIn("--request-url", err)
        self.assertFalse(self.sent.exists())
        for good in ("https://www.muckrock.com/foi/request-90001", base, "https://www.muckrock.com/foi/a-1/b-2/c-3"):
            with self.subTest(url=good):
                self.assertEqual(self.send("muckrock", "--replace", url=good, agency=self.other)[0], 0)
                self.assertEqual(kit_track.read_sent(self.root)[self.other]["request_url"], good)

    def test_stored_request_url_is_validated_only_on_muckrock_entries(self):
        self.filed()
        document = json.loads(self.sent.read_text(encoding="utf-8"))
        document["requests"][self.agency]["request_url"] = "http://www.muckrock.com/foi/request-90001/"
        self.sent.write_text(json.dumps(document), encoding="utf-8")
        code, _, err = run("kit-status", "--directory", self.root)
        self.assertEqual(code, 1)
        self.assertIn("malformed entry", err)
        document["requests"][self.agency].update(request_url=URL, channel="email")  # url on a non-muckrock entry
        self.sent.write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(run("kit-status", "--directory", self.root)[0], 1)

    def test_coverage_table_shows_the_muckrock_channel(self):
        self.filed()
        code, out, err = run("kit-coverage", "--directory", self.root)
        self.assertEqual(code, 0, err)
        row = next(line for line in out.splitlines() if line.startswith(self.agency + " "))
        self.assertIn(" muckrock ", row)

    # --- kit-muckrock-import
    def test_each_status_maps_to_the_frozen_step(self):
        cases = [("processed", "acknowledged", "2026-03-10", {}), ("fix", "clarification_requested", "2026-03-10", {}),
                 ("payment", "fee_estimate", "2026-03-10", {"price": "245.50"}),
                 ("rejected", "denied", "2026-03-12", {}),
                 ("no_docs", "closed", "2026-03-12", {"note": "no responsive documents (MuckRock)"}),
                 ("partial", "produced", "2026-03-12", {"partial": True}), ("done", "produced", "2026-03-12", {}),
                 ("abandoned", "closed", "2026-03-12", {"note": "withdrawn (MuckRock)"})]
        for status, kind, on, extra in cases:
            with self.subTest(status=status):
                self.sent.unlink(missing_ok=True)
                self.filed([] if status == "processed" else [("acknowledged", "2026-03-03")])
                before = self.sent.read_bytes()
                path = self.saved(status=status, datetime_done="2026-03-12T23:59:00Z", price=extra.get("price"))
                code, out, err = self.imp(path, "--json")
                self.assertEqual(code, 0, err)
                result = json.loads(out[:out.rindex("}") + 1])
                self.assertEqual((result["step"]["kind"], result["step"]["on"]), (kind, on))
                self.assertEqual(result["step"].get("partial", False), extra.get("partial", False))
                self.assertEqual(result["step"].get("note"), extra.get("note"))
                self.assertFalse(result["applied"])
                self.assertEqual(self.sent.read_bytes(), before)  # a dry run writes nothing
                self.assertIn("Read only from", out)
                self.assertIn("Nothing was fetched or sent.", out)
                code, out, err = self.imp(path, "--apply")
                self.assertEqual(code, 0, err)
                self.assertEqual(len(self.steps()), (0 if status == "processed" else 1) + 1)
                last = self.steps()[-1]
                self.assertEqual((last["kind"], last["on"], last.get("partial", False), last.get("note")),
                                 (kind, on, extra.get("partial", False), extra.get("note")))
                code, out, _ = self.imp(path, "--apply")  # a rerun
                self.assertEqual(code, 0)
                self.assertIn("already recorded", out)
                self.assertEqual(len(self.steps()), (0 if status == "processed" else 1) + 1)

    def test_a_status_that_waits_or_needs_hand_recording_proposes_no_step(self):
        self.filed()
        for status, words in (("started", "waiting for the agency"), ("submitted", "waiting for the agency"),
                              ("ack", "waiting for the agency"), ("appealing", "record by hand"),
                              ("lawsuit", "record by hand")):
            with self.subTest(status=status):
                code, out, err = self.imp(self.saved(status=status), "--apply")
                self.assertEqual(code, 0, err)
                self.assertIn(words, out)
        self.assertEqual(self.steps(), [])

    def test_done_prefers_datetime_done_and_falls_back_to_updated(self):
        self.filed([("acknowledged", "2026-03-03")])
        code, out, _ = self.imp(FIXTURES / "done.json")
        self.assertEqual(code, 0)
        self.assertIn("--kind produced --date 2026-03-18", out)
        code, out, _ = self.imp(self.saved(status="done", datetime_done=None, datetime_updated="2026-03-20T08:00:00Z"))
        self.assertIn("--kind produced --date 2026-03-20", out)
        code, _, err = self.imp(self.saved(status="done", datetime_done=None, datetime_updated=None))
        self.assertEqual(code, 1)
        self.assertIn("datetime_done or datetime_updated", err)
        code, _, err = self.imp(self.saved(status="done", datetime_done="last week"))
        self.assertEqual(code, 1)
        self.assertIn("datetime_done", err)
        code, _, err = self.imp(self.saved(status="processed", datetime_updated=None))  # updated is the only date here
        self.assertEqual((code, "datetime_updated" in err), (1, True))

    def test_a_partial_is_not_already_recorded_by_a_final_production_on_the_same_day(self):
        self.filed([("acknowledged", "2026-03-03")])
        self.assertEqual(self.imp(self.saved(status="partial", datetime_done="2026-03-12"), "--apply")[0], 0)
        code, out, _ = self.imp(self.saved(status="done", datetime_done="2026-03-12"))
        self.assertIn("Proposed: kit-response", out)
        self.assertNotIn("already recorded", out)

    def test_unknown_status_is_refused_by_name_and_nothing_is_written(self):
        self.filed()
        before = self.sent.read_bytes()
        for path in (FIXTURES / "weird-status.json", self.saved(status="")):
            code, out, err = self.imp(path, "--apply")
            self.assertEqual(code, 1)
            self.assertIn("unknown MuckRock status", err)
            self.assertEqual(self.sent.read_bytes(), before)
        self.assertIn("'weird'", self.imp(FIXTURES / "weird-status.json")[2])

    def test_a_json_for_another_request_or_a_non_muckrock_entry_is_refused(self):
        self.filed()
        before = self.sent.read_bytes()
        code, _, err = self.imp(self.saved(id=90002), "--apply")
        self.assertEqual(code, 1)
        self.assertIn("this JSON is request 90002, not 90001", err)
        for bad in ({"id": "90001"}, {"id": True}, {"id": None}, {"status": 5}, {"status": None}):
            with self.subTest(bad=bad):
                self.assertEqual(self.imp(self.saved(**bad), "--apply")[0], 1)
        self.assertEqual(self.imp(self.saved(), agency=self.other)[0], 1)  # nothing recorded for that agency
        self.assertEqual(self.sent.read_bytes(), before)
        self.sent.unlink()
        self.assertEqual(self.send("email", url=None)[0], 0)
        code, _, err = self.imp(self.saved(), "--apply")
        self.assertEqual(code, 1)
        self.assertIn("muckrock", err)
        self.assertEqual(self.steps(), [])

    def test_payment_amount_comes_from_the_price_and_follows_the_amount_rule(self):
        self.filed([("acknowledged", "2026-03-03")])
        code, out, err = self.imp(FIXTURES / "payment-245.json", "--apply")
        self.assertEqual(code, 0, err)
        self.assertEqual((self.steps()[-1]["kind"], self.steps()[-1]["amount_cents"]), ("fee_estimate", 24550))
        self.sent.unlink()
        self.filed([("acknowledged", "2026-03-03")])
        for price, accepted in ((245.5, 24550), (245, 24500)):  # a JSON number is accepted too
            self.assertEqual(self.imp(self.saved(status="payment", price=price), "--apply")[0], 0)
            self.assertEqual(self.steps()[-1]["amount_cents"], accepted)
            self.sent.unlink()
            self.filed([("acknowledged", "2026-03-03")])
        for price in (None, ""):
            code, out, _ = self.imp(self.saved(status="payment", price=price), "--apply")
            self.assertEqual(code, 0)
            self.assertIn("record the estimate by hand", out)
        for price in (-1, "-1", "1e3", "NaN", "245.505", "1,000", 1e-7, [1], {"a": 1}, True):
            with self.subTest(price=price):
                code, _, err = self.imp(self.saved(status="payment", price=price), "--apply")
                self.assertEqual(code, 1)
                self.assertTrue("--amount" in err or "price" in err, err)
        self.assertEqual([s["kind"] for s in self.steps()], ["acknowledged"])

    def test_abandoned_with_no_prior_step_shows_the_closed_refusal_and_writes_nothing(self):
        self.filed()
        before = self.sent.read_bytes()
        for extra in ((), ("--apply",)):
            code, out, err = self.imp(self.saved(status="abandoned", datetime_done="2026-03-12"), *extra)
            self.assertEqual(code, 1)
            self.assertIn("Proposed: kit-response", out)
            self.assertIn("closed needs at least one prior step", err)
            self.assertEqual(self.sent.read_bytes(), before)

    def test_step_rules_still_apply(self):
        self.filed([("acknowledged", "2026-03-10")])
        code, _, err = self.imp(self.saved(status="processed"), "--apply")  # already recorded, nothing to do
        self.assertEqual(code, 0)
        code, _, err = self.imp(self.saved(status="rejected", datetime_done="2026-03-05"), "--apply")
        self.assertEqual(code, 1)
        self.assertIn("before the previous step", err)
        code, _, err = self.imp(self.saved(status="rejected", datetime_done="2999-01-01"), "--apply")
        self.assertEqual(code, 1)
        self.assertIn("future", err)
        self.assertEqual(len(self.steps()), 1)

    # --- semantic comparison of the mapped step with the request's last step (RF0220-1)
    def pay(self, price, status="payment", **fields):
        return self.saved(status=status, price=price, datetime_updated="2026-03-10T14:30:00Z", **fields)

    def test_revised_same_day_payment_is_proposed_applied_then_idempotent(self):
        self.filed([("acknowledged", "2026-03-03")])
        self.assertEqual(self.imp(self.pay("245.50"), "--apply")[0], 0)
        first = self.steps()[-1]
        for price, cents in (("300.00", 30000), ("250", 25000), ("0.00", 0)):  # up, down, then zero
            with self.subTest(price=price):
                before, count = self.sent.read_bytes(), len(self.steps())
                code, out, err = self.imp(self.pay(price))  # dry run proposes and writes nothing
                self.assertEqual(code, 0, err)
                self.assertIn(f"--kind fee_estimate --date 2026-03-10 --amount {price}", out)
                self.assertNotIn("already recorded", out)
                self.assertEqual(self.sent.read_bytes(), before)
                self.assertEqual(self.imp(self.pay(price), "--apply")[0], 0)
                self.assertEqual((len(self.steps()), self.steps()[-1]["amount_cents"]), (count + 1, cents))
                applied = self.sent.read_bytes()
                code, out, _ = self.imp(self.pay(price), "--apply")  # the new latest value is a no-op
                self.assertEqual(code, 0)
                self.assertIn("already recorded", out)
                self.assertEqual(self.sent.read_bytes(), applied)
        self.assertEqual(self.steps()[1], first)  # the old estimate is kept, never replaced
        self.assertEqual([s.get("amount_cents") for s in self.steps()], [None, 24550, 30000, 25000, 0])

    def test_payment_identity_uses_cents_not_json_representation(self):
        self.filed([("acknowledged", "2026-03-03")])
        for price in ("245", 245, 245.0, "245.0", "245.00"):
            with self.subTest(price=price):
                self.assertEqual(self.imp(self.pay(price), "--apply")[0], 0)
                count = len(self.steps())
                self.assertEqual(self.steps()[-1]["amount_cents"], 24500)
                for same in ("245", 245, 245.0, "245.0", "245.00"):
                    code, out, err = self.imp(self.pay(same), "--apply")
                    self.assertEqual(code, 0, err)
                    self.assertIn("already recorded", out)
                self.assertEqual(len(self.steps()), 1 + 1)  # acknowledged + the one estimate
        self.sent.unlink()
        self.filed([("acknowledged", "2026-03-03")])
        for price in ("0", 0, 0.0, "0.00", "100000", 100000, "100000.00"):
            with self.subTest(zero_or_max=price):
                code, out, err = self.imp(self.pay(price), "--apply")
                self.assertEqual(code, 0, err)
        self.assertEqual([s.get("amount_cents") for s in self.steps()], [None, 0, 10_000_000])
        # organizer-added notes and the stored recording time do not defeat equality on a non-closed step
        document = json.loads(self.sent.read_text(encoding="utf-8"))
        document["requests"][self.agency]["steps"][-1]["note"] = "organizer's own words"
        self.sent.write_text(json.dumps(document), encoding="utf-8")
        before = self.sent.read_bytes()
        code, out, _ = self.imp(self.pay("100000.00"), "--apply")
        self.assertEqual(code, 0)
        self.assertIn("already recorded", out)
        self.assertEqual(self.sent.read_bytes(), before)

    def test_invalid_repeated_payment_cannot_hide_behind_dedup(self):
        self.filed([("acknowledged", "2026-03-03")])
        self.assertEqual(self.imp(self.pay("245.50"), "--apply")[0], 0)
        before = self.sent.read_bytes()
        bad_prices = ("NaN", "Infinity", "-245.50", "+245.50", "2.4550e2", "245.505", "1,000", "100000.01", "1000000",
                      " 245.50", True, [245.5], {"a": 1}, float("nan"), float("inf"), 1e-7, -1)
        for price in bad_prices:
            for extra in ((), ("--apply",), ("--json",), ("--json", "--apply")):
                with self.subTest(price=price, extra=extra):
                    path = self.saved(status="payment", price=price, datetime_updated="2026-03-10T14:30:00Z")
                    saved_bytes = path.read_bytes()
                    code, out, err = self.imp(path, *extra)
                    self.assertEqual(path.read_bytes(), saved_bytes)  # the saved file is never touched
                    self.assertEqual(code, 1, out)
                    self.assertNotIn("already recorded", out)
                    self.assertTrue(err.startswith("Stopped: "), err)
                    self.assertTrue("--amount" in err or "price must be" in err, err)
                    self.assertNotIn("Traceback", err)
                    self.assertEqual(self.sent.read_bytes(), before)
        for price in (None, ""):  # no price at all is still the distinct, successful manual-recording no-op
            for extra in ((), ("--apply",), ("--json",)):
                with self.subTest(price=price, extra=extra):
                    code, out, _ = self.imp(self.pay(price), *extra)
                    self.assertEqual(code, 0)
                    self.assertIn("record the estimate by hand", out)
                    self.assertNotIn("already recorded", out)
                    self.assertEqual(self.sent.read_bytes(), before)

    def test_closure_reason_is_identity_and_change_obeys_closed_rule(self):
        reasons = {"no_docs": "no responsive documents (MuckRock)", "abandoned": "withdrawn (MuckRock)"}
        for first, other in (("no_docs", "abandoned"), ("abandoned", "no_docs")):
            with self.subTest(first=first):
                self.sent.unlink(missing_ok=True)
                self.filed([("acknowledged", "2026-03-03")])
                closing = self.saved(status=first, datetime_done="2026-03-12")
                self.assertEqual(self.imp(closing, "--apply")[0], 0)
                self.assertEqual(self.steps()[-1]["note"], reasons[first])
                before = self.sent.read_bytes()
                for extra in ((), ("--apply",)):  # an exact replay is a no-op even though the request is closed
                    code, out, err = self.imp(closing, *extra)
                    self.assertEqual(code, 0, err)
                    self.assertIn("already recorded", out)
                changed = self.saved(status=other, datetime_done="2026-03-12")
                for extra in ((), ("--apply",), ("--json",), ("--json", "--apply")):
                    with self.subTest(other=other, extra=extra):
                        code, out, err = self.imp(changed, *extra)
                        self.assertEqual(code, 1)
                        self.assertNotIn("already recorded", out)
                        self.assertIn(reasons[other], out)  # the proposal is shown ...
                        self.assertIn("is closed; no step can follow closed", err)  # ... and the normal rule refuses it
                        self.assertEqual(self.sent.read_bytes(), before)
                self.assertEqual(self.steps()[-1]["note"], reasons[first])

    def test_semantic_dedup_is_request_local_and_latest_only(self):
        self.filed([("acknowledged", "2026-03-03")])
        named = "named"
        requests = self.root / "kit" / "requests"
        (requests / f"{self.agency}--{named}.md").write_bytes((requests / f"{self.agency}.md").read_bytes())
        code, _, err = run("kit-sent", "--directory", self.root, "--agency", self.agency, "--date", SENT_ON,
                           "--channel", "muckrock", "--request-url", URL, "--request", named)
        self.assertEqual(code, 0, err)
        self.assertEqual(run("kit-response", "--directory", self.root, "--agency", self.agency, "--date", "2026-03-03",
                             "--kind", "acknowledged", "--request", named)[0], 0)
        self.assertEqual(self.imp(self.pay("245.50"), "--apply")[0], 0)
        main_before = [dict(s) for s in self.steps()]
        code, out, err = self.imp(self.pay("245.50"), "--apply", "--request", named)  # not deduped by the main history
        self.assertEqual(code, 0, err)
        self.assertNotIn("already recorded", out)
        self.assertEqual(self.steps(), main_before)
        self.assertEqual(self.imp(self.pay("300.00"), "--apply", "--request", named)[0], 0)
        self.assertEqual(self.steps(), main_before)  # a named-request revision changes only its own entry
        entries = json.loads(self.sent.read_text(encoding="utf-8"))["requests"]
        key = next(k for k in entries if k != self.agency and k.startswith(self.agency))
        self.assertEqual([s.get("amount_cents") for s in entries[key]["steps"]], [None, 24550, 30000])
        # an intervening step is not looked through: only the LAST step decides
        self.assertEqual(run("kit-response", "--directory", self.root, "--agency", self.agency, "--date", "2026-03-11",
                             "--kind", "clarification_requested")[0], 0)
        before = self.sent.read_bytes()
        code, out, err = self.imp(self.pay("245.50"))
        self.assertEqual(code, 1)  # proposed, then the normal chronology rule refuses a step dated before the last
        self.assertNotIn("already recorded", out)
        self.assertIn("before the previous step", err)
        self.assertEqual(self.sent.read_bytes(), before)

    def test_revised_import_updates_status_and_private_fee_totals(self):
        (self.root / "kit" / kit.SETTINGS_NAME).write_text(json.dumps({"schema_version": 1, "fee_cap_usd": 250}),
                                                          encoding="utf-8")
        self.filed([("acknowledged", "2026-03-03")])
        self.assertEqual(self.imp(self.pay("245.50"), "--apply")[0], 0)
        self.assertEqual(self.imp(self.pay("300.00"), "--apply")[0], 0)
        code, out, err = run("kit-status", "--directory", self.root)
        self.assertEqual(code, 0, err)
        row = next(r for r in json.loads(out)["requests"] if r["agency_id"] == self.agency)
        self.assertEqual((row["fee_estimate"], row["over_fee_cap"]), ("$300.00", True))
        code, out, err = run("kit-fees", "--directory", self.root)
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report["schema"], "kit-fees-v1")
        self.assertEqual(len(report["requests"]), 1)
        text = json.dumps(report)
        self.assertIn("$300.00", text)
        self.assertNotIn("$545.50", text)
        self.assertNotIn("545.50", text)

    def test_other_mapped_steps_keep_rerun_and_partial_rules(self):
        self.filed()
        for status, on in (("processed", "2026-03-10"), ("rejected", "2026-03-12"), ("done", "2026-03-12")):
            with self.subTest(status=status):
                path = self.saved(status=status, datetime_done=f"{on}T10:00:00Z")
                self.assertEqual(self.imp(path, "--apply")[0], 0)
                before = self.sent.read_bytes()
                code, out, err = self.imp(path, "--apply")
                self.assertEqual(code, 0, err)
                self.assertIn("already recorded", out)
                self.assertEqual(self.sent.read_bytes(), before)
        self.sent.unlink()
        self.filed([("acknowledged", "2026-03-03")])
        self.assertEqual(self.imp(self.saved(status="done", datetime_done="2026-03-12"), "--apply")[0], 0)
        before = self.sent.read_bytes()
        code, out, err = self.imp(self.saved(status="partial", datetime_done="2026-03-12"), "--apply")  # final -> partial
        self.assertEqual(code, 1)
        self.assertNotIn("already recorded", out)
        self.assertEqual(self.sent.read_bytes(), before)
        for date, words in (("2026-03-05", "before the previous step"), ("2999-01-01", "future")):
            code, _, err = self.imp(self.saved(status="done", datetime_done=date), "--apply")
            self.assertEqual(code, 1)
            self.assertIn(words, err)
            self.assertEqual(self.sent.read_bytes(), before)


    def test_unsafe_files_are_refused_before_sent_json_is_read(self):
        target = self.saved()
        link = self.tmp / "link.json"
        link.symlink_to(target)
        big = self.tmp / "big.json"
        big.write_text(json.dumps({"id": 90001, "status": "processed", "pad": "x" * (1 << 20)}), encoding="utf-8")
        latin = self.tmp / "latin.json"
        latin.write_bytes('{"id": 90001, "status": "caf\xe9"}'.encode("latin-1"))
        cases = {"symlink": link, "too big": big, "not utf-8": latin, "missing": self.tmp / "none.json",
                 "directory": self.tmp, "array": self.tmp / "array.json", "scalar": self.tmp / "scalar.json",
                 "broken": self.tmp / "broken.json"}
        cases["array"].write_text("[1]", encoding="utf-8")
        cases["scalar"].write_text("5", encoding="utf-8")
        cases["broken"].write_text("{", encoding="utf-8")
        with mock.patch.object(kit_track, "read_sent", side_effect=AssertionError("kit/sent.json read")):
            for name, path in cases.items():
                with self.subTest(file=name):
                    code, _, err = self.imp(path, "--apply")
                    self.assertEqual(code, 1)
                    self.assertIn("Stopped", err)

    def test_the_help_and_text_say_nothing_is_fetched_or_sent(self):
        self.filed()
        code, out, _ = self.imp(self.saved())
        self.assertEqual(code, 0)
        self.assertIn(f"Read only from {self.tmp / 'saved.json'}. Nothing was fetched or sent.", out)
        self.assertIn("Dry run: nothing written. Add --apply to record it.", out)
        code, out, _ = self.imp(self.saved(), "--apply")
        self.assertIn("Recorded.", out)


if __name__ == "__main__":
    unittest.main()
