"""DF1207-03: kit-asked proposes content/agencies.json 'asked' items from the private kit/sent.json (synthetic only)."""
import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit_asked, law  # noqa: E402
from campaign_tool.content import load_content  # noqa: E402
from campaign_tool.content_check import content_problems  # noqa: E402
from campaign_tool.site import build_site  # noqa: E402

EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
PACKAGE = law.load_package("us-ca")["request_scopes"]
A, B, C = (scope["scope_id"] for scope in PACKAGE[:3])
TITLE = {scope["scope_id"]: scope["title"] for scope in PACKAGE}
POLICE, SHERIFF = "ca-cedar-police", "ca-cedar-county-sheriff"
TODAY = date(2026, 11, 1)
SECRET = ("PRIVATE-NOTE-7731", "PRIVATE-BASIS-7731", "PRIVATE-REF-7731", "f" * 63 + "e")
LINKED = "zqlinked-4821"  # a request name that must never be rendered


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


class KitAskedTests(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket", "socket.create_connection", "urllib.request.urlopen"):
            patch = mock.patch(target, side_effect=AssertionError("network used"))
            patch.start()
            self.addCleanup(patch.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-asked-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root)
        self.path = self.root / "content" / "agencies.json"
        self.sent_path = self.root / "kit" / "sent.json"
        self.agencies = json.loads(self.path.read_text(encoding="utf-8"))

    # --- fixtures -----------------------------------------------------------------------------------------------
    def save_content(self):
        self.path.write_text(json.dumps(self.agencies, indent=2) + "\n", encoding="utf-8")

    def index(self, agency):
        return [a["agency_id"] for a in self.agencies].index(agency)

    def asked(self, agency=POLICE):
        return json.loads(self.path.read_text(encoding="utf-8"))[self.index(agency)].get("asked", [])

    def sent(self, *entries):
        """entries: (agency, request, steps, scopes or None[, extra])."""
        requests = {}
        for agency, request, steps, scopes, *extra in entries:
            key = agency if request == "main" else f"{agency}#{request}"
            stem = agency if request == "main" else f"{agency}--{request}"
            requests[key] = {"sent_on": "2026-03-02", "channel": "email", "draft": f"requests/{stem}.md",
                             "draft_sha256": "a" * 64, "recorded_at": "2026-03-02T10:00:00+00:00", "steps": steps,
                             **({"scopes": scopes} if scopes else {}), **(extra[0] if extra else {})}
        self.sent_path.parent.mkdir(exist_ok=True)
        self.sent_path.write_text(json.dumps({"schema_version": 1, "requests": requests}), encoding="utf-8")

    def propose(self, *flags):
        return run("kit-asked", "--directory", self.root, *flags)

    def applied(self, steps, scopes=(A, B), extra=None):
        self.sent((POLICE, "main", steps, list(scopes) or None, extra or {}))
        code, out, err = self.propose("--apply")
        self.assertEqual(code, 0, err)
        return self.asked()[0]

    # --- 1. no steps ---------------------------------------------------------------------------------------------
    def test_sent_without_steps_is_awaiting_and_dry_run_writes_nothing(self):
        self.sent((POLICE, "main", [], [A, B]))
        before = self.path.read_bytes()
        code, out, err = self.propose()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("+ new item", out)
        self.assertIn(f"content/agencies.json {POLICE}.asked[0]", out)
        self.assertEqual(self.path.read_bytes(), before)
        item = self.applied([])
        self.assertEqual(item, {"topic": f"{TITLE[A]}; {TITLE[B]}", "asked_on": "2026-03-02",
                                "state": "awaiting_response", "request": "main"})
        self.assertNotIn("updated_on", item)

    def test_no_recorded_scopes_topic_is_generic(self):
        self.assertEqual(self.applied([], scopes=())["topic"], "Records request")

    def test_topic_is_cut_at_a_title_boundary_within_120_characters(self):
        self.assertEqual(kit_asked.topic_text(["a" * 50, "b" * 50, "c" * 50]), "a" * 50 + "; " + "b" * 50)
        self.assertEqual(kit_asked.topic_text(["x" * 200, "y"]), "x" * 117 + "...")
        self.assertEqual(kit_asked.topic_text(["x" * 120]), "x" * 120)
        self.assertEqual(kit_asked.topic_text([]), "Records request")

    # --- 2. state map --------------------------------------------------------------------------------------------
    def test_waiting_kinds_map_to_acknowledged_with_latest_step_date(self):
        for kind in ("acknowledged", "extension", "determination", "fee_estimate", "clarification_requested"):
            item = self.applied([step("2026-03-04", "acknowledged"), step("2026-03-09", kind)])
            self.assertEqual((item["state"], item["updated_on"]), ("acknowledged", "2026-03-09"), kind)

    def test_denied_without_production_is_declined_in_writing(self):
        item = self.applied([step("2026-03-04", "acknowledged"), step("2026-03-12", "denied")])
        self.assertEqual((item["state"], item["updated_on"]), ("declined_in_writing", "2026-03-12"))

    def test_produced_with_a_withheld_scope_is_partly_answered(self):
        item = self.applied([step("2026-03-12", "produced", scope_answers={A: "produced", B: "withheld"})])
        self.assertEqual(item["state"], "partly_answered")

    def test_produced_with_every_scope_answered_is_answered(self):
        item = self.applied([step("2026-03-12", "produced", scope_answers={A: "produced", B: "no_records_stated"})])
        self.assertEqual(item["state"], "answered")

    def test_latest_answer_per_scope_counts(self):
        steps = [step("2026-03-12", "produced", scope_answers={A: "produced", B: "withheld"}),
                 step("2026-03-20", "produced", scope_answers={B: "produced"})]
        self.assertEqual(self.applied(steps)["state"], "answered")

    def test_produced_without_answers_or_scopes_or_with_a_scope_unanswered_is_partly_answered(self):
        self.assertEqual(self.applied([step("2026-03-12", "produced")])["state"], "partly_answered")
        self.assertEqual(self.applied([step("2026-03-12", "produced")], scopes=())["state"], "partly_answered")
        self.assertEqual(self.applied([step("2026-03-12", "produced", scope_answers={A: "produced"})])["state"],
                         "partly_answered")
        both = {A: "partly_produced", B: "produced"}
        self.assertEqual(self.applied([step("2026-03-12", "produced", scope_answers=both)])["state"],
                         "partly_answered")

    def test_rolling_production_is_partly_answered_even_when_scopes_are_answered(self):
        steps = [step("2026-03-12", "produced", partial=True, scope_answers={A: "produced", B: "produced"})]
        self.assertEqual(self.applied(steps)["state"], "partly_answered")

    def test_produced_then_closed_keeps_the_production_state(self):
        steps = [step("2026-03-12", "produced", scope_answers={A: "produced", B: "produced"}),
                 step("2026-03-15", "closed")]
        item = self.applied(steps)
        self.assertEqual((item["state"], item["updated_on"]), ("answered", "2026-03-15"))

    # --- 3. closed without production ----------------------------------------------------------------------------
    def test_closed_without_production_is_left_for_the_organizer(self):
        self.sent((POLICE, "main", [step("2026-03-04", "acknowledged"), step("2026-03-20", "closed")], [A]))
        code, out, err = self.propose("--apply")
        self.assertEqual(code, 0, err)
        self.assertIn("organizer to choose", out)
        self.assertNotIn("+ new item", out)
        self.assertNotIn("asked", json.loads(self.path.read_text())[self.index(POLICE)])

    def test_closed_request_leaves_an_existing_linked_item_as_it_is(self):
        self.agencies[self.index(POLICE)]["asked"] = [
            {"topic": "Mine", "asked_on": "2026-03-02", "state": "acknowledged", "updated_on": "2026-03-04",
             "request": "main", "note": "keep"}]
        self.save_content()
        self.sent((POLICE, "main", [step("2026-03-04", "acknowledged"), step("2026-03-20", "closed")], [A]))
        before = self.path.read_bytes()
        code, out, err = self.propose("--apply")
        self.assertEqual(code, 0, err)
        self.assertIn(f"{POLICE}.asked[0]: closed without production; organizer to choose", out)
        self.assertEqual(self.path.read_bytes(), before)

    # --- 4. organizer-written items ------------------------------------------------------------------------------
    def test_apply_keeps_organizer_items_notes_and_the_linked_items_note(self):
        organizer = {"topic": "Hand typed", "asked_on": "2026-02-01", "state": "answered", "note": "my own words"}
        linked = {"topic": "Old topic", "asked_on": "2026-03-02", "state": "awaiting_response", "request": "main",
                  "note": "linked note"}
        self.agencies[self.index(POLICE)]["asked"] = [organizer, linked]
        self.save_content()
        self.sent((POLICE, "main", [step("2026-03-04", "denied")], [A]))
        code, out, err = self.propose("--apply")
        self.assertEqual(code, 0, err)
        items = self.asked()
        self.assertEqual(items[0], organizer)
        self.assertEqual(items[1], {"topic": TITLE[A], "asked_on": "2026-03-02", "state": "declined_in_writing",
                                    "request": "main", "note": "linked note", "updated_on": "2026-03-04"})
        self.assertIn(f'{POLICE}.asked[1].state: "awaiting_response" -> "declined_in_writing"', out)
        self.assertIn(f'{POLICE}.asked[1].topic: "Old topic" -> ', out)
        self.assertIn(f"{POLICE}.asked[1].updated_on: (absent) -> ", out)
        self.assertNotIn("asked[0]", out)
        self.assertNotIn("linked note", out)
        self.assertNotIn("my own words", out)

    def test_second_run_proposes_nothing_and_does_not_rewrite(self):
        self.sent((POLICE, "main", [step("2026-03-04", "acknowledged")], [A]))
        self.assertEqual(self.propose("--apply")[0], 0)
        after = self.path.read_bytes()
        code, out, err = self.propose("--apply")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("nothing to propose", out)
        self.assertEqual(self.path.read_bytes(), after)

    def test_two_requests_to_one_agency_each_get_their_own_item_and_a_missing_agency_is_skipped(self):
        self.sent((POLICE, "main", [], [A]), (POLICE, "second", [step("2026-03-05", "acknowledged")], [B]),
                  ("ca-not-in-content", "main", [], [A]))
        code, out, err = self.propose("--apply")
        self.assertEqual(code, 0, err)
        self.assertEqual([(i["request"], i["state"]) for i in self.asked()],
                         [("main", "awaiting_response"), ("second", "acknowledged")])
        self.assertIn("ca-not-in-content: agency not in content/agencies.json; skipped", out)

    def test_nothing_to_add_leaves_the_file_byte_equal(self):
        self.sent((POLICE, "main", [step("2026-03-20", "closed")], [A]))
        before = self.path.read_bytes()
        self.assertEqual(self.propose("--apply")[0], 0)
        self.assertEqual(self.path.read_bytes(), before)

    # --- 5. refusals write nothing -------------------------------------------------------------------------------
    def test_apply_that_would_exceed_20_items_is_stopped_and_nothing_is_written(self):
        self.agencies[self.index(POLICE)]["asked"] = [
            {"topic": f"T{i}", "asked_on": "2026-03-02", "state": "awaiting_response"} for i in range(20)]
        self.save_content()
        self.sent((POLICE, "main", [], [A]))
        before = self.path.read_bytes()
        code, out, err = self.propose("--apply")
        self.assertEqual(code, 3)
        self.assertIn("Stopped:", err)
        self.assertIn("at most 20", err)
        self.assertEqual(self.path.read_bytes(), before)

    def test_apply_that_breaks_validation_is_stopped_and_nothing_is_written(self):
        self.agencies[self.index(SHERIFF)]["asked"] = [{"topic": "Bad", "asked_on": "2026-03-02", "state": "bogus"}]
        self.save_content()
        self.sent((POLICE, "main", [], [A]))
        before = self.path.read_bytes()
        code, out, err = self.propose("--apply")
        self.assertEqual(code, 3)
        self.assertIn("Stopped:", err)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.propose()[0], 3)  # a dry run names the same refusal

    def test_a_step_dated_before_the_send_date_is_stopped(self):
        self.sent((POLICE, "main", [step("2026-02-01", "acknowledged")], [A]))
        before = self.path.read_bytes()
        code, out, err = self.propose("--apply")
        self.assertEqual(code, 3)
        self.assertIn("updated_on", err)
        self.assertEqual(self.path.read_bytes(), before)

    def test_unreadable_sent_json_and_missing_content_are_stopped(self):
        self.sent_path.parent.mkdir()
        self.sent_path.write_text("not json", encoding="utf-8")
        code, out, err = self.propose("--apply")
        self.assertEqual(code, 3)
        self.assertIn("kit/sent.json unreadable", err)
        self.path.unlink()
        code, out, err = self.propose()
        self.assertEqual(code, 3)
        self.assertIn("content/agencies.json not found", err)

    def test_no_sent_json_proposes_nothing(self):
        before = self.path.read_bytes()
        code, out, err = self.propose("--apply")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("nothing to propose", out)
        self.assertEqual(self.path.read_bytes(), before)

    # --- 6. the request key --------------------------------------------------------------------------------------
    def test_a_bad_request_value_is_a_content_check_problem(self):
        self.assertEqual(content_problems(self.root), [])
        for bad in ("main#2", "a" * 41, "Upper", "", 5):
            self.agencies[self.index(POLICE)]["asked"] = [
                {"topic": "T", "asked_on": "2026-03-02", "state": "awaiting_response", "request": bad}]
            self.save_content()
            problems = content_problems(self.root)
            self.assertTrue(any(".asked[0].request: 1-40 characters" in p for p in problems), (bad, problems))
        self.agencies[self.index(POLICE)]["asked"] = [
            {"topic": "T", "asked_on": "2026-03-02", "state": "awaiting_response", "request": "a" * 40}]
        self.save_content()
        self.assertEqual(content_problems(self.root), [])

    def test_two_items_linked_to_one_request_are_a_problem(self):
        item = {"topic": "T", "asked_on": "2026-03-02", "state": "awaiting_response", "request": "main"}
        self.agencies[self.index(POLICE)]["asked"] = [item, dict(item)]
        self.save_content()
        self.assertTrue(any("asked[1].request: already linked by asked[0]" in p for p in content_problems(self.root)))

    def test_the_request_name_is_never_published(self):
        self.agencies[self.index(POLICE)]["asked"] = [
            {"topic": "T", "asked_on": "2026-03-02", "state": "awaiting_response", "request": LINKED}]
        self.save_content()
        build_site(self.root, today=TODAY)
        files = [p for p in (self.root / "public").rglob("*") if p.is_file()]
        self.assertTrue(files)
        self.assertEqual([p.name for p in files if LINKED.encode() in p.read_bytes()], [])
        agency = [a for a in load_content(self.root, TODAY)["agencies"] if a["agency_id"] == POLICE][0]
        self.assertEqual(agency["asked"][0]["request"], LINKED)  # kept in the model, rendered nowhere

    def test_older_content_without_request_reads_unchanged(self):
        self.agencies[self.index(POLICE)]["asked"] = [{"topic": "T", "asked_on": "2026-03-02", "state": "answered"}]
        self.save_content()
        agency = [a for a in load_content(self.root, TODAY)["agencies"] if a["agency_id"] == POLICE][0]
        self.assertEqual(agency["asked"], [{"topic": "T", "asked_on": "2026-03-02", "state": "answered"}])

    # --- 7. content-check staleness ------------------------------------------------------------------------------
    def link(self, **fields):
        self.agencies[self.index(POLICE)]["asked"] = [
            {"topic": "T", "asked_on": "2026-03-02", "state": "acknowledged", "request": "main", **fields}]
        self.save_content()

    def test_content_check_names_a_linked_item_older_than_its_requests_latest_step(self):
        self.link(updated_on="2026-03-05")
        self.sent((POLICE, "main", [step("2026-03-04", "acknowledged"), step("2026-03-10", "denied")], [A]))
        self.assertEqual(content_problems(self.root),
                         ["content/agencies.json[1].asked[0]: older than its request's latest step "
                          "(denied on 2026-03-10); run kit-asked"])
        code, out, err = run("content-check", "--directory", self.root)
        self.assertEqual(code, 3)
        self.assertIn("older than its request's latest step (denied on 2026-03-10); run kit-asked", out)

    def test_content_check_uses_asked_on_when_there_is_no_updated_on(self):
        self.link()
        self.sent((POLICE, "main", [step("2026-03-03", "acknowledged")], [A]))
        self.assertEqual(len(content_problems(self.root)), 1)

    def test_content_check_equal_or_later_item_date_is_no_problem(self):
        self.link(updated_on="2026-03-10")
        self.sent((POLICE, "main", [step("2026-03-10", "acknowledged")], [A]))
        self.assertEqual(content_problems(self.root), [])
        self.sent((POLICE, "main", [step("2026-03-04", "acknowledged")], [A]))
        self.assertEqual(content_problems(self.root), [])

    def test_content_check_without_a_tracker_or_steps_is_silent(self):
        self.link(updated_on="2026-03-05")
        self.assertEqual(content_problems(self.root), [])  # no kit/sent.json at all
        self.sent((POLICE, "main", [], [A]))
        self.assertEqual(content_problems(self.root), [])

    def test_content_check_names_a_link_to_a_request_the_tracker_does_not_have(self):
        self.link(request="other")
        self.sent((POLICE, "main", [], [A]))
        self.assertEqual(content_problems(self.root),
                         ["content/agencies.json[1].asked[0]: links request other not in kit/sent.json"])

    def test_content_check_leaves_organizer_items_alone_and_names_an_unreadable_tracker(self):
        self.agencies[self.index(POLICE)]["asked"] = [{"topic": "T", "asked_on": "2026-03-02", "state": "answered"}]
        self.save_content()
        self.sent((POLICE, "main", [step("2026-03-10", "denied")], [A]))
        self.assertEqual(content_problems(self.root), [])
        self.link()
        self.sent_path.write_text("not json", encoding="utf-8")
        self.assertEqual(content_problems(self.root), ["kit/sent.json unreadable: not JSON"])

    def test_apply_clears_the_staleness_problem(self):
        self.link(updated_on="2026-03-05")
        self.sent((POLICE, "main", [step("2026-03-10", "denied")], [A]))
        self.assertEqual(len(content_problems(self.root)), 1)
        self.assertEqual(self.propose("--apply")[0], 0)
        self.assertEqual(content_problems(self.root), [])

    # --- 8. nothing private reaches content ----------------------------------------------------------------------
    def test_private_step_fields_never_appear_in_content_or_the_printout(self):
        note, basis, reference, records = SECRET
        steps = [step("2026-03-04", "acknowledged", note=note),
                 step("2026-03-12", "denied", note=note, withheld_basis=[basis], records=[records])]
        self.sent((POLICE, "main", steps, [A, B], {"reference": reference, "correspondence": [records]}))
        code, out, err = self.propose("--apply")
        self.assertEqual(code, 0, err)
        content = self.path.read_text(encoding="utf-8")
        for secret in SECRET:
            self.assertNotIn(secret, content)
            self.assertNotIn(secret, out + err)
        self.assertEqual(set(self.asked()[0]), {"topic", "asked_on", "state", "updated_on", "request"})


if __name__ == "__main__":
    unittest.main()
