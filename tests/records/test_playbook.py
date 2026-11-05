import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from campaign_tool.records import playbook as pb

PATTERN_IDS = {"extension", "partial_production", "blanket_denial", "fee", "no_records",
               "records_contradict_policy", "silence", "vague_answer"}


class ShippedPlaybookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.book = pb.load()

    def test_eight_followup_patterns_with_all_parts(self):
        self.assertEqual({p["id"] for p in self.book["followup_patterns"]}, PATTERN_IDS)
        for entry in self.book["followup_patterns"]:
            for key in ("when", "goal", "questions", "cite", "do_not"):
                self.assertTrue(entry[key] or key == "cite", entry["id"] + "." + key)

    def test_first_request_order_exit_ramp_then_logs_audits_sharing_then_contracts(self):
        first = self.book["first_request"]
        self.assertIn("treat this request as complete", first["exit_ramp"])
        self.assertEqual([i["key"] for i in first["items"]][:3], ["native_log_export", "completed_audits", "sharing"])
        self.assertEqual(first["items"][-1]["key"], "contracts")
        self.assertEqual([i["tier"] for i in first["items"][:2]], [1, 1])
        self.assertIn("redacted", first["items"][0]["redaction_offer"])

    def test_playbook_is_campaign_data_not_agency_specific(self):
        text = json.dumps(self.book).lower()
        for name in ("sheriff", "grover", "arroyo", "morro", "paso robles", "atascadero", "pismo", "san luis"):
            self.assertNotIn(name, text)
        self.assertEqual(self.book["jurisdictions"], ["us-ca"])

    def test_tier_assignment_puts_logs_before_policy_before_contracts(self):
        tier = lambda text: pb.tier_for(self.book, text)  # noqa: E731
        self.assertEqual(tier("Native access log export for January"), 1)
        self.assertEqual(tier("Completed periodic audits under policy 12"), 1)
        self.assertEqual(tier("Data-sharing list and executed MOUs"), 2)
        self.assertEqual(tier("The retention setting"), 2)
        self.assertEqual(tier("Contracts, order forms and invoices"), 3)
        self.assertEqual(tier("Audit of the vendor contract"), 1)
        self.assertIsNone(tier("Weather reports"))
        self.assertIsNone(tier(""))

    def test_tier_keyword_matches_whole_words_only(self):
        self.assertIsNone(pb.tier_for(self.book, "Auditorium schedule"))

    def test_lint_flags_every_banned_class(self):
        cases = {
            "violation": "This was a violation of the law.",
            "illegal": "That is illegal.",
            "unlawful": "Records were unlawfully withheld.",
            "you_failed": "You failed to respond.",
            "failed_to_comply": "The agency failed to comply.",
            "all_communications": "Provide all communications about it.",
            "accusation_words": "This looks like a cover-up.",
            "no_audits": "There were zero audits.",
            "coordinates": "Camera at 35.28275, -120.65962.",
            "street_address": "The reader is at 1200 Marsh Street.",
        }
        for ident, text in cases.items():
            result = pb.lint(self.book, text)
            self.assertEqual(result["status"], "needs edit", ident)
            self.assertIn(ident, {h["id"] for h in result["hits"]}, ident)

    def test_lint_passes_clean_text_and_offered_exclusions(self):
        text = ("We are not asking for plate numbers, hotlist contents, case numbers or camera locations. "
                "Please produce any existing record of a completed audit under Policy 466.5(e), or confirm that "
                "none exists. No documented audits were produced for 2025. Please produce the 5 audit reports.")
        self.assertEqual(pb.lint(self.book, text), {"status": "pass", "hits": []})

    def test_lint_normalises_curly_quotes_and_case(self):
        self.assertEqual(pb.lint(self.book, "YOU\u2019VE FAILED")["status"], "needs edit")

    def test_lint_hits_ordered_by_position(self):
        hits = pb.lint(self.book, "illegal then a violation")["hits"]
        self.assertEqual([h["id"] for h in hits], ["illegal", "violation"])

    def test_lint_rejects_non_text(self):
        with self.assertRaises(pb.PlaybookError) as ctx:
            pb.lint(self.book, None)
        self.assertEqual(ctx.exception.code, "text_invalid")

    def test_pattern_lookup(self):
        self.assertEqual(pb.pattern(self.book, "fee")["cite"], ["fees"])
        with self.assertRaises(pb.PlaybookError) as ctx:
            pb.pattern(self.book, "nope")
        self.assertEqual(ctx.exception.code, "pattern_unknown")

    def test_every_pattern_question_and_item_passes_its_own_lint(self):
        for entry in self.book["followup_patterns"]:
            for question in entry["questions"]:
                self.assertEqual(pb.lint(self.book, question)["status"], "pass", entry["id"])
        for item in self.book["first_request"]["items"]:
            self.assertEqual(pb.lint(self.book, item["ask"])["status"], "pass", item["key"])


class RefusalTests(unittest.TestCase):
    def setUp(self):
        self.good = pb.load()

    def refuse(self, mutate, code):
        book = copy.deepcopy(self.good)
        mutate(book)
        with self.assertRaises(pb.PlaybookError) as ctx:
            pb.validate(book)
        self.assertEqual(ctx.exception.code, code)

    def test_structural_refusals(self):
        self.refuse(lambda b: b.update(schema="x"), "schema_mismatch")
        self.refuse(lambda b: b.update(tiers=[]), "tiers_invalid")
        self.refuse(lambda b: b["tiers"].reverse(), "tiers_invalid")
        self.refuse(lambda b: b["citations"]["fees"].update(status="maybe"), "citations_invalid")
        self.refuse(lambda b: b["first_request"]["items"][0].update(tier=9), "first_request_invalid")
        self.refuse(lambda b: b["first_request"]["items"][0].update(cite=["ghost"]), "citation_unknown")
        self.refuse(lambda b: b["first_request"]["items"].reverse(), "first_request_invalid")
        self.refuse(lambda b: b["followup_patterns"][1].update(id="extension"), "pattern_duplicate")
        self.refuse(lambda b: b["followup_patterns"][0].pop("do_not"), "patterns_invalid")
        self.refuse(lambda b: b["followup_patterns"][0].update(cite=["ghost"]), "citation_unknown")
        self.refuse(lambda b: b["banned_phrases"].append({"id": "x", "pattern": "(", "reason": "r"}), "banned_pattern_invalid")
        self.refuse(lambda b: b.update(banned_phrases=[]), "banned_invalid")
        self.refuse(lambda b: b["banned_phrases"].append(dict(b["banned_phrases"][0])), "banned_invalid")

    def test_malformed_field_types_refuse_with_playbook_error(self):
        self.refuse(lambda b: b["first_request"]["items"][0].update(cite=None), "first_request_invalid")
        self.refuse(lambda b: b["first_request"]["items"][0].update(cite=[{}]), "first_request_invalid")
        self.refuse(lambda b: b["followup_patterns"][0].update(cite=[{}]), "patterns_invalid")
        self.refuse(lambda b: b["followup_patterns"][0].update(cite=None), "patterns_invalid")
        self.refuse(lambda b: b["banned_phrases"][0].update(id=[]), "banned_invalid")
        self.refuse(lambda b: b["banned_phrases"][0].update(id=None), "banned_invalid")

    def test_default_playbook_ships_inside_the_package(self):
        package = Path(pb.__file__).resolve().parents[1]
        self.assertTrue(pb.DEFAULT_PATH.is_file())
        self.assertIn(package, pb.DEFAULT_PATH.parents)
        manifest = (package.parent / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("_resources/playbooks/records-alpr-v1.json", manifest)

    def test_non_object_and_file_refusals(self):
        with self.assertRaises(pb.PlaybookError) as ctx:
            pb.validate([])
        self.assertEqual(ctx.exception.code, "not_object")
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("{nope", encoding="utf-8")
            for path, code in ((bad, "not_json"), (Path(tmp) / "missing.json", "unreadable")):
                with self.assertRaises(pb.PlaybookError) as ctx:
                    pb.load(path)
                self.assertEqual(ctx.exception.code, code)
            big = Path(tmp) / "big.json"
            big.write_bytes(b" " * (pb.MAX_BYTES + 1))
            with self.assertRaises(pb.PlaybookError) as ctx:
                pb.load(big)
            self.assertEqual(ctx.exception.code, "too_large")


class CliTests(unittest.TestCase):
    def run_cli(self, *argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.argv", ["records playbook", *argv]), mock.patch("sys.stdin", io.StringIO(stdin)), \
                redirect_stdout(out), redirect_stderr(err):
            code = pb.main()
        return code, out.getvalue(), err.getvalue()

    def test_check_and_lint_exit_codes(self):
        code, out, _ = self.run_cli("check")
        self.assertEqual((code, "8 patterns" in out), (0, True))
        code, out, _ = self.run_cli("lint", "-", stdin="Please produce the existing export.")
        self.assertEqual((code, out.strip()), (0, "pass"))
        code, out, _ = self.run_cli("lint", "-", stdin="That is illegal.")
        self.assertEqual(code, 1)
        self.assertIn("needs edit", out)

    def test_cli_refusals_exit_two_without_leaking_paths(self):
        code, _, err = self.run_cli("lint", "/nonexistent/draft.txt")
        self.assertEqual(code, 2)
        self.assertIn("draft_unreadable", err)
        self.assertNotIn("/nonexistent", err)
        code, _, err = self.run_cli("check", "--playbook", "/nonexistent/p.json")
        self.assertEqual(code, 2)
        self.assertIn("unreadable", err)

    def test_records_cli_routes_playbook(self):
        from campaign_tool.records import cli
        self.assertEqual(cli.COMMANDS["playbook"], ("playbook", "main"))


if __name__ == "__main__":
    unittest.main()
