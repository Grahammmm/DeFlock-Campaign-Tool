"""DF2007-14: ``kit-asked --outcomes`` proposes the public answer details from the private tracker (synthetic only).

The default run is pinned against the same histories: no flag and ``outcomes=False`` are byte-identical to the
four-field proposal, and the new fields appear only with the opt-in.
"""
import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit_asked, law  # noqa: E402
from campaign_tool.content import ContentError, load_agencies, load_content  # noqa: E402
from campaign_tool.content_check import content_problems  # noqa: E402
from campaign_tool.site import build_site  # noqa: E402

EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SCHEMA = json.loads((REPO_ROOT / "schemas" / "agencies-open-data.schema.json").read_text(encoding="utf-8"))
PACKAGE = law.load_package("us-ca")["request_scopes"]
A, B = (scope["scope_id"] for scope in PACKAGE[:2])
POLICE = "ca-cedar-police"
TODAY = date(2026, 11, 1)
SENTINELS = ("PRIVATE-NOTE-9917", "PRIVATE-BASIS-9917", "PRIVATE-REASON-9917", "PRIVATE-REF-9917", "f" * 63 + "e")
OUTCOME_KEYS = ("fee_quoted", "extension", "withheld_sections")


def run(*arguments):
    out, err, code = io.StringIO(), io.StringIO(), 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def step(on, kind, **extra):
    return {"on": on, "kind": kind, **extra}


def answers(disposition, *scopes):
    return {"scope_answers": {scope: disposition for scope in scopes}}


class OutcomesTests(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket", "socket.create_connection", "urllib.request.urlopen"):
            patch = mock.patch(target, side_effect=AssertionError("network used"))
            patch.start()
            self.addCleanup(patch.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-asked-outcomes-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.path = self.root / "content" / "agencies.json"
        self.sent_path = self.root / "kit" / "sent.json"

    def sent(self, steps, scopes=(A, B), extra=None):
        request = {"sent_on": "2026-03-02", "channel": "email", "draft": f"requests/{POLICE}.md",
                   "draft_sha256": "a" * 64, "recorded_at": "2026-03-02T10:00:00+00:00", "steps": steps,
                   **({"scopes": list(scopes)} if scopes else {}), **(extra or {})}
        self.sent_path.parent.mkdir(exist_ok=True)
        self.sent_path.write_text(json.dumps({"schema_version": 1, "requests": {POLICE: request}}), encoding="utf-8")

    def items(self):
        agencies = json.loads(self.path.read_text(encoding="utf-8"))
        return [a for a in agencies if a["agency_id"] == POLICE][0].get("asked", [])

    def proposed(self, steps, scopes=(A, B), outcomes=True, today=TODAY, **extra):
        """The one proposed item for ``steps``; the whole document must pass the loader."""
        self.sent(steps, scopes, extra or None)
        result = kit_asked.propose(self.root, today, outcomes=outcomes)
        self.assertIsNone(result["error"])
        load_agencies(result["document"], "content/agencies.json", today)
        return [a for a in result["document"] if a["agency_id"] == POLICE][0]["asked"][0]

    def details(self, steps, **kw):
        item = self.proposed(steps, **kw)
        return {k: item[k] for k in OUTCOME_KEYS if k in item}

    # --- fee ------------------------------------------------------------------------------------------------------
    def test_unpaid_estimate_of_12_50_proposes_exactly_the_fee(self):
        item = self.proposed([step("2026-03-05", "fee_estimate", amount_cents=1250)])
        self.assertEqual(item, {"topic": item["topic"], "asked_on": "2026-03-02", "state": "acknowledged",
                                "updated_on": "2026-03-05", "request": "main",
                                "fee_quoted": {"amount_usd": "12.50", "status": "unpaid"}})

    def test_fee_status_follows_a_settlement_after_the_latest_estimate_only(self):
        paid = [step("2026-03-05", "fee_estimate", amount_cents=1250), step("2026-03-06", "fee_paid", amount_cents=999)]
        self.assertEqual(self.details(paid), {"fee_quoted": {"amount_usd": "12.50", "status": "paid"}})
        waived = [step("2026-03-05", "fee_estimate", amount_cents=5), step("2026-03-06", "fee_waived")]
        self.assertEqual(self.details(waived), {"fee_quoted": {"amount_usd": "0.05", "status": "waived"}})
        later = [*paid, step("2026-03-07", "fee_estimate", amount_cents=0), step("2026-03-08", "determination")]
        self.assertEqual(self.details(later), {"fee_quoted": {"amount_usd": "0.00", "status": "unpaid"}})
        top = [step("2026-03-05", "fee_estimate", amount_cents=10_000_000)]
        self.assertEqual(self.details(top), {"fee_quoted": {"amount_usd": "100000.00", "status": "unpaid"}})

    # --- extension ------------------------------------------------------------------------------------------------
    def test_extension_proposes_its_own_date_and_stated_deadline(self):
        steps = [step("2026-03-05", "extension", extension_expected_on="2026-03-19", extension_reason="PRIVATE-REASON-9917")]
        self.assertEqual(self.details(steps), {"extension": {"noticed_on": "2026-03-05", "until": "2026-03-19"}})
        later = [*steps, step("2026-03-10", "determination")]  # a later step does not erase what the agency said
        self.assertEqual(self.details(later), {"extension": {"noticed_on": "2026-03-05", "until": "2026-03-19"}})

    def test_extension_without_a_stated_date_proposes_none(self):
        for extra in ({}, {"extension_reason": "PRIVATE-REASON-9917"}):
            steps = [step("2026-03-05", "extension", **extra), step("2026-03-06", "determination", expected_on="2026-04-01")]
            self.assertEqual(self.details(steps), {}, extra)

    def test_extension_noticed_today_with_a_deadline_14_days_out_builds(self):
        today = date.today()
        until = (today + timedelta(days=14)).isoformat()
        self.sent([step(today.isoformat(), "extension", extension_expected_on=until)])
        code, out, err = run("kit-asked", "--directory", self.root, "--outcomes")
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f'.extension: (absent) -> {{"noticed_on": "{today.isoformat()}", "until": "{until}"}}', out)
        code, out, err = run("kit-asked", "--directory", self.root, "--outcomes", "--apply")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.items()[0]["extension"], {"noticed_on": today.isoformat(), "until": until})
        self.assertEqual(content_problems(self.root, today=today) or [], [])
        load_content(self.root, today)  # the loader keeps the future deadline
        build_site(self.root, today=today)
        sentence = f"On {today.isoformat()} the agency said it would respond by {until}."
        for page in ("requests.html", "agencies.html"):
            self.assertEqual((self.root / "public" / page).read_text(encoding="utf-8").count(sentence), 1, page)
        data = json.loads((self.root / "public" / "data" / "agencies.json").read_text(encoding="utf-8"))
        law.validate(data, SCHEMA)

    def test_extension_on_a_frozen_day_passes_the_loader_and_check(self):
        self.sent([step("2026-11-01", "extension", extension_expected_on="2026-11-15")])
        code, out, err = run("kit-asked", "--directory", self.root, "--outcomes", "--apply")  # real clock is later
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.items()[0]["extension"], {"noticed_on": "2026-11-01", "until": "2026-11-15"})
        self.assertEqual(content_problems(self.root, today=TODAY) or [], [])

    def test_future_notice_and_deadline_before_notice_are_refused_unchanged(self):
        for steps in ([step("2026-11-02", "extension", extension_expected_on="2026-11-20")],  # noticed after TODAY
                      [step("2026-10-20", "extension", extension_expected_on="2026-10-19")]):
            self.sent(steps)
            before = self.path.read_bytes()
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(kit_asked.kit, "_write_json", side_effect=AssertionError("written")), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = kit_asked.run_cli(self.root, True, TODAY, outcomes=True)
            self.assertEqual((code, out.getvalue()), (3, ""))
            self.assertIn("Stopped:", err.getvalue())
            self.assertEqual(self.path.read_bytes(), before)

    # --- withheld sections ----------------------------------------------------------------------------------------
    def test_denial_citing_two_sections_proposes_only_the_numbers(self):
        basis = ["PRIVATE-BASIS-9917 under GC 7923.600", "Gov. Code 7927.705 <b>x</b> https://example.invalid/f"]
        steps = [step("2026-03-05", "denied", withheld_basis=basis)]
        item = self.proposed(steps)
        self.assertEqual(item["withheld_sections"], ["7923.600", "7927.705"])
        self.assertEqual(item["state"], "declined_in_writing")
        self.assertEqual(self.details(steps), {"withheld_sections": ["7923.600", "7927.705"]})
        self.assertFalse(any(s in json.dumps(item) for s in SENTINELS))

    def test_basis_without_a_number_proposes_none_and_duplicates_collapse(self):
        steps = [step("2026-03-05", "denied", withheld_basis=["PRIVATE-BASIS-9917", "Gov. Code section"])]
        self.assertEqual(self.details(steps), {})
        steps = [step("2026-03-05", "denied", withheld_basis=["GC 7923.600", "Government Code 7923.600", "GC 7923.615"])]
        self.assertEqual(self.details(steps), {"withheld_sections": ["7923.600", "7923.615"]})

    def test_more_than_five_distinct_sections_refuse_the_proposal(self):
        def basis(*numbers):
            return [f"GC {n}" for n in numbers]
        self.sent([step("2026-03-05", "denied", withheld_basis=basis("7923.600", "7923.605", "7923.610")),
                   step("2026-03-06", "produced", withheld_basis=basis("7923.615", "7923.620", "7923.625"))])
        before = self.path.read_bytes()
        code, out, err = run("kit-asked", "--directory", self.root, "--outcomes", "--apply")
        self.assertEqual((code, out), (3, ""))
        self.assertIn(kit_asked.SECTIONS_CAPACITY, err)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(self.details([step("2026-03-05", "denied", withheld_basis=basis(
            "7923.600", "7923.605", "7923.610", "7923.615", "7923.620"))])["withheld_sections"]), 5)

    # --- no records -----------------------------------------------------------------------------------------------
    def test_all_no_records_request_proposes_the_state(self):
        steps = [step("2026-03-05", "produced", **answers("no_records_stated", A, B))]
        item = self.proposed(steps)
        self.assertEqual((item["state"], item["updated_on"]), ("no_records_stated", "2026-03-05"))
        self.assertFalse(any(k in item for k in OUTCOME_KEYS))
        closed = [step("2026-03-05", "denied", **answers("no_records_stated", A, B)), step("2026-03-06", "closed")]
        self.assertEqual(self.proposed(closed)["state"], "no_records_stated")

    def test_no_records_needs_every_recorded_scope_and_yields_to_withdrawn(self):
        mixed = [step("2026-03-05", "produced", **answers("no_records_stated", A))]
        self.assertEqual(self.proposed(mixed)["state"], "partly_answered")
        revised = [step("2026-03-05", "produced", **answers("no_records_stated", A, B)),
                   step("2026-03-06", "produced", **answers("produced", B))]
        self.assertEqual(self.proposed(revised)["state"], "answered")
        self.assertEqual(self.proposed([step("2026-03-05", "denied")])["state"], "declined_in_writing")
        none = [step("2026-03-05", "produced")]
        self.assertEqual(self.proposed(none, scopes=())["state"], "partly_answered")
        gone = [step("2026-03-05", "produced", **answers("no_records_stated", A, B)), step("2026-03-06", "withdrawn", note="PRIVATE-NOTE-9917")]
        self.assertEqual(self.proposed(gone)["state"], "withdrawn")
        closed = [step("2026-03-04", "determination"), step("2026-03-05", "closed", **answers("produced", A, B))]
        self.sent(closed)
        result = kit_asked.propose(self.root, TODAY, outcomes=True)
        self.assertIn("organizer to choose", "\n".join(result["lines"]))
        self.assertFalse(result["changed"])

    # --- default mode ---------------------------------------------------------------------------------------------
    def test_default_mode_is_the_four_field_proposal_for_the_same_histories(self):
        histories = ([step("2026-03-05", "fee_estimate", amount_cents=1250)],
                     [step("2026-03-05", "extension", extension_expected_on="2026-03-19")],
                     [step("2026-03-05", "denied", withheld_basis=["GC 7923.600", "GC 7927.705"])],
                     [step("2026-03-05", "produced", **answers("no_records_stated", A, B))])
        states = ("acknowledged", "acknowledged", "declined_in_writing", "answered")
        for steps, state in zip(histories, states):
            self.sent(steps)
            default = kit_asked.propose(self.root, TODAY)
            explicit = kit_asked.propose(self.root, TODAY, outcomes=False)
            self.assertEqual(default, explicit)
            item = default["document"][[a["agency_id"] for a in default["document"]].index(POLICE)]["asked"][0]
            self.assertEqual(sorted(item), ["asked_on", "request", "state", "topic", "updated_on"])
            self.assertEqual(item["state"], state)
            self.assertEqual(len(default["lines"]), 1)
            self.assertTrue(default["lines"][0].startswith(f"+ new item content/agencies.json {POLICE}.asked[0]: "))
            code, out, err = run("kit-asked", "--directory", self.root)
            self.assertEqual((code, err, out.splitlines()[:1]), (0, "", default["lines"]))

    def test_cli_flag_is_opt_in_and_forwarded(self):
        code, out, err = run("kit-asked", "--help")
        self.assertEqual(code, 0)
        self.assertIn("--outcomes", out)
        self.sent([])
        with mock.patch.object(kit_asked, "run_cli", return_value=0) as called:
            run("kit-asked", "--directory", self.root, "--outcomes")
            run("kit-asked", "--directory", self.root)
        self.assertEqual([c.kwargs for c in called.call_args_list], [{"outcomes": True}, {"outcomes": False}])

    # --- field lines, preservation, idempotence ---------------------------------------------------------------------
    def rich(self):
        return [step("2026-03-04", "extension", extension_expected_on="2026-03-18"),
                step("2026-03-05", "fee_estimate", amount_cents=1250),
                step("2026-03-06", "denied", withheld_basis=["GC 7923.600"], **answers("withheld", A, B))]

    def test_every_changed_field_has_its_own_line_and_nothing_private_is_printed(self):
        self.sent([*self.rich()[:2], step("2026-03-06", "denied", withheld_basis=["PRIVATE-BASIS-9917 GC 7923.600"],
                                          note="PRIVATE-NOTE-9917")], extra={"reference": "PRIVATE-REF-9917"})
        code, out, err = run("kit-asked", "--directory", self.root, "--outcomes")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(sum(l.startswith("+ new item") for l in lines), 1)
        for field in OUTCOME_KEYS:
            self.assertEqual(sum(f".asked[0].{field}: (absent) -> " in l for l in lines), 1, field)
        self.assertNotIn('"fee_quoted"', lines[0])
        self.assertFalse(any(s in out for s in SENTINELS))

    def test_existing_item_changes_per_field_and_manual_values_are_kept(self):
        self.sent(self.rich())
        agencies = json.loads(self.path.read_text(encoding="utf-8"))
        police = [a for a in agencies if a["agency_id"] == POLICE][0]
        police["asked"] = [{"topic": "Old", "asked_on": "2026-03-02", "state": "acknowledged", "request": "main",
                            "note": "kept", "withheld_sections": ["7923.615"], "fee_quoted": {"amount_usd": "12.50", "status": "paid"}},
                           {"topic": "Typed by hand", "asked_on": "2026-01-01", "state": "answered"}]
        self.path.write_text(json.dumps(agencies, indent=2) + "\n", encoding="utf-8")
        result = kit_asked.propose(self.root, TODAY, outcomes=True)
        text = "\n".join(result["lines"])
        self.assertIn('.fee_quoted: {"amount_usd": "12.50", "status": "paid"} -> {"amount_usd": "12.50", "status": "unpaid"}', text)
        self.assertIn('.withheld_sections: ["7923.615"] -> ["7923.600"]', text)
        self.assertIn(".extension: (absent) -> ", text)
        item = [a for a in result["document"] if a["agency_id"] == POLICE][0]["asked"]
        self.assertEqual((item[0]["note"], item[1]), ("kept", police["asked"][1]))
        # No extension recorded: an existing organizer value is left, not removed; default mode never touches it either.
        self.sent([step("2026-03-05", "fee_estimate", amount_cents=1250)])
        police["asked"][0]["extension"] = {"noticed_on": "2026-03-01", "until": "2026-03-10"}
        self.path.write_text(json.dumps(agencies, indent=2) + "\n", encoding="utf-8")
        item = [a for a in kit_asked.propose(self.root, TODAY, outcomes=True)["document"]
                if a["agency_id"] == POLICE][0]["asked"][0]
        self.assertEqual(item["extension"], {"noticed_on": "2026-03-01", "until": "2026-03-10"})
        self.assertEqual(item["withheld_sections"], ["7923.615"])

    def test_apply_writes_once_and_a_rerun_changes_nothing(self):
        self.sent(self.rich())
        code, out, err = run("kit-asked", "--directory", self.root, "--outcomes", "--apply")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Written to content/agencies.json (one write)", out)
        load_agencies(json.loads(self.path.read_text(encoding="utf-8")), "content/agencies.json", date.today())
        written = self.path.read_bytes()
        with mock.patch.object(kit_asked.kit, "_write_json", side_effect=AssertionError("written")), \
                mock.patch.object(kit_asked.os, "chmod", side_effect=AssertionError("chmod")):
            code, out, err = run("kit-asked", "--directory", self.root, "--outcomes", "--apply")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("nothing to propose", out)
        self.assertIn("Nothing written.", out)
        self.assertEqual(self.path.read_bytes(), written)

    # --- existing guards still apply ------------------------------------------------------------------------------
    def test_invalid_history_unresolved_scope_and_dry_run_still_refuse_without_writing(self):
        before = self.path.read_bytes()
        broken = [step("2026-03-05", "fee_estimate", amount_cents=1250), step("2026-03-04", "no-such-kind")]
        self.sent(broken)
        code, out, err = run("kit-asked", "--directory", self.root, "--outcomes", "--apply")
        self.assertEqual((code, out), (3, ""))
        self.sent([step("2026-03-05", "fee_estimate", amount_cents=1250)], scopes=("no-such-scope",))
        with self.assertRaisesRegex(ContentError, "cannot resolve all recorded scope titles"):
            kit_asked.propose(self.root, TODAY, outcomes=True)
        self.sent([step("2026-03-05", "fee_estimate", amount_cents=1250)])
        with mock.patch.object(kit_asked.kit, "_write_json", side_effect=AssertionError("written")), \
                mock.patch.object(kit_asked.os, "chmod", side_effect=AssertionError("chmod")):
            code, out, err = run("kit-asked", "--directory", self.root, "--outcomes")
        self.assertEqual(code, 0)
        self.assertIn("Dry run: nothing written", out)
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
