"""kit fee-reply writes a private, never-sent reply draft to the newest recorded fee estimate. Offline, synthetic."""
import contextlib
import copy
import hashlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit_fee_reply, kit_track, law  # noqa: E402

A = "ca-aaa-police"
TODAY = date(2026, 11, 1)
SHIPPED = REPO_ROOT / "campaign_tool" / "_resources" / "jurisdictions"
BASIS = "Fees may cover direct duplication costs only. Synthetic fee basis text."
SENT = "Draft only. Nothing was sent."


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


def snapshot(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns) for p in Path(root).rglob("*")
            if p.is_file() and not p.is_symlink()}


class FeeReplyTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-fee-reply-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        (self.root / "kit" / "requests").mkdir(parents=True)
        (self.root / "campaign.json").write_text(json.dumps(
            {"schema_version": 1, "name": "Synthetic", "county": "San Luis Obispo", "state": "CA"}), encoding="utf-8")
        self.agencies = [{"agency_id": A, "name": "Aaa Police Department", "kind": "police", "selected": True}]
        (self.root / "kit" / "agencies.json").write_text(json.dumps(self.agencies), encoding="utf-8")
        self.sent = {}
        self.send("main", "2026-03-02", "R0123-110125")
        self.base = self.package()

    def package(self, mutate=None, name="law"):  # a synthetic us-ca package with one exact-citation fee rule
        package = json.loads((SHIPPED / "us-ca" / "package.json").read_text(encoding="utf-8"))
        rule = copy.deepcopy(package["rules"][0])
        rule.update(rule_id="ca-gov-7922.530-copies-synthetic", citation="Gov. Code § 7922.530",
                    effective_from="2023-01-01", effective_to=None)
        package["rules"].append(rule)
        package["records_law"]["fee_basis"] = BASIS
        if mutate:
            mutate(package)
        folder = self.tmp / name / "us-ca"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "package.json").write_text(json.dumps(package), encoding="utf-8")
        return self.tmp / name

    def send(self, request, on, reference=None):
        stem = kit_track._stem(A, request)
        data = f"Synthetic request {stem} for audit logs.\n".encode("utf-8")
        (self.root / "kit" / "requests" / f"{stem}.md").write_bytes(data)
        entry = {"sent_on": on, "channel": "email", "draft": kit_track._draft_path(A, request),
                 "draft_sha256": hashlib.sha256(data).hexdigest(), "recorded_at": "2026-03-02T10:00:00+00:00",
                 "steps": []}
        if reference:
            entry["reference"] = reference
        self.sent[kit_track._key(A, request)] = entry
        self.save()

    def save(self):
        (self.root / "kit" / "sent.json").write_text(
            json.dumps({"schema_version": 1, "requests": self.sent}), encoding="utf-8")

    def step(self, kind, on="2026-03-10", request="main", **extra):
        self.sent[kit_track._key(A, request)]["steps"].append({"on": on, "kind": kind, **extra})
        self.save()

    def estimate(self, cents=24550, on="2026-03-10", request="main"):
        self.step("fee_estimate", on, request, amount_cents=cents)

    def make(self, request="main", **kwargs):
        kwargs.setdefault("law_base", self.base)
        return kit_fee_reply.fee_reply(self.root, A, request=request, today=TODAY, **kwargs)

    def letter(self, result):
        return (self.root / result["written"]).read_text(encoding="utf-8")

    def refuses(self, text, *args, **kwargs):
        before = snapshot(self.root)
        with self.assertRaises(ValueError) as caught:
            self.make(*args, **kwargs)
        self.assertIn(text, str(caught.exception))
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse((self.root / "kit" / "followups").exists())

    def test_main_named_same_day_latest(self):
        self.estimate(100)
        self.estimate(2500000 // 25, "2026-03-10")  # same day, later insertion wins: $1,000.00
        result = self.make()
        self.assertEqual(result, {"written": f"kit/followups/{A}--main-fee-2026-03-10.md", "agency_id": A,
                                  "request": "main", "estimate_on": "2026-03-10", "amount_cents": 100000,
                                  "rule_id": "ca-gov-7922.530-copies-synthetic", "law_package_status": "draft",
                                  "review_flags": []})
        text = self.letter(result)
        for expected in ("# Fee estimate reply draft: Aaa Police Department", "DRAFT — NOT SENT. Review before sending it yourself.",
                         "To: Aaa Police Department, records custodian", "Regarding your reference R0123-110125,",
                         "On 2026-03-10 we recorded your fee estimate of $1,000.00 for this request.",
                         kit_fee_reply.ASK_ITEMIZED, kit_fee_reply.ASK_ELECTRONIC,
                         "Fee basis as encoded in the law package (ca-gov-7922.530-copies-synthetic; Gov. Code § 7922.530):",
                         "> " + BASIS, "Draft only. Not legal advice.", "Law package status: draft",
                         "Not independently reviewed; verify applicability before sending."):
            self.assertIn(expected, text)
        self.assertNotIn("Would limiting the scope", text)
        self.assertEqual(stat.S_IMODE(os.stat(self.root / result["written"]).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.root / "kit" / "followups").st_mode), 0o700)
        self.send("second-ask", "2026-04-01")
        self.estimate(0, "2026-04-05", "second-ask")
        named = self.make("second-ask")
        self.assertEqual((named["written"], named["amount_cents"]), (f"kit/followups/{A}--second-ask-fee-2026-04-05.md", 0))
        self.assertIn("$0.00", self.letter(named))
        self.assertNotIn("R0123", self.letter(named))  # no reference of another request copied
        self.assertEqual(self.letter(result), text)  # the main draft is untouched
        self.estimate(10000000, "2026-05-01", "second-ask")
        self.assertIn("$100,000.00", self.letter(self.make("second-ask", replace=True)))

    def test_no_latest_fee_or_terminal_request(self):
        self.refuses("No recorded fee estimate for this request.")
        self.refuses("No recorded fee estimate for this request.", "other-ask")  # no main fallback
        self.estimate()
        for kind in ("acknowledged", "fee_paid", "fee_waived", "clarification_requested", "denied", "determination"):
            self.step(kind, "2026-03-12")
            self.refuses("not a fee estimate")
            self.sent[A]["steps"].pop()
        self.step("produced", "2026-03-12", partial=True)
        self.refuses("not a fee estimate")
        self.sent[A]["steps"].pop()
        self.estimate(5000, "2026-03-20")  # a newer estimate after an older settled one is the one answered
        self.assertEqual(self.make()["amount_cents"], 5000)
        for terminal in ("closed", "withdrawn"):
            self.sent[A]["steps"].append({"on": "2026-03-25", "kind": terminal})
            self.save()
            self.refuses(terminal)
            self.sent[A]["steps"].pop()

    def test_invalid_tracker_and_agency(self):
        self.estimate()
        for bad in ("../x", "Bad", "", "a" * 41):
            with self.assertRaises(ValueError):
                self.make(bad)
        for bad in ("nope", "CA-AAA-POLICE", "../ca-aaa-police"):
            with self.assertRaises(ValueError):
                kit_fee_reply.fee_reply(self.root, bad, today=TODAY, law_base=self.base)
        self.agencies[0]["selected"] = False
        (self.root / "kit" / "agencies.json").write_text(json.dumps(self.agencies), encoding="utf-8")
        self.refuses("not a selected agency")
        self.agencies[0]["selected"] = True
        (self.root / "kit" / "agencies.json").write_text(json.dumps(self.agencies), encoding="utf-8")
        for amount in (True, -1, 10000001, 1.5, "10"):
            self.sent[A]["steps"][0]["amount_cents"] = amount
            self.save()
            self.refuses("unreadable")
        self.sent[A]["steps"][0]["amount_cents"] = 100
        self.step("acknowledged", "2026-03-01")  # decreasing date
        self.save()
        self.refuses("unreadable")
        self.sent[A]["steps"] = [{"on": "2026-12-01", "kind": "fee_estimate", "amount_cents": 1}]  # after today
        self.save()
        self.refuses("")
        (self.root / "kit" / "sent.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            self.make()
        self.assertNotIn("{not json", str(caught.exception))

    def test_package_rule_attribution_and_refusal(self):
        self.estimate()
        self.refuses("No unambiguous fee rule in the law package; nothing written.", law_base=SHIPPED)  # shipped: no fee rule
        cases = {
            "blank": lambda p: p["records_law"].update(fee_basis="  "),
            "expired": lambda p: p["rules"][-1].update(effective_to="2026-03-01"),
            "future": lambda p: p["rules"][-1].update(effective_from="2026-04-01"),
            "ambiguous": lambda p: p["rules"].append({**p["rules"][-1], "rule_id": "ca-gov-7922.530-copies-two"}),
            "angle": lambda p: p["records_law"].update(fee_basis="see <b>this</b>"),
            "none": lambda p: p["rules"].pop(),
        }
        for name, mutate in cases.items():
            self.refuses("No unambiguous fee rule", law_base=self.package(mutate, name))
        self.refuses("No unambiguous fee rule", law_base=self.tmp / "absent")
        self.assertEqual(self.make()["law_package_status"], "draft")
        reviewed = self.package(lambda p: p.update(status="reviewed", reviewed_by=["Synthetic Reviewer"],
                                                   reviewed_at="2026-01-01"), "reviewed")
        text = self.letter(self.make(law_base=reviewed, replace=True))
        self.assertIn("Law package status: reviewed", text)
        self.assertNotIn("Not independently reviewed", text)
        (self.root / "campaign.json").write_text(json.dumps(
            {"schema_version": 1, "name": "S", "county": "X", "state": "TX"}), encoding="utf-8")
        self.refuses("No unambiguous fee rule", law_base=self.base)  # no us-tx package

    def test_narrow_boundaries_and_alert_only_guard(self):
        self.estimate()
        result = self.make(narrow="Only 2026 audit logs & *hot* [lists]")
        text = self.letter(result)
        self.assertIn("Would limiting the scope to the following change the estimate?", text)
        self.assertIn("> Only 2026 audit logs \\& \\*hot\\* \\[lists\\]", text)
        self.assertIn("This is a question about a possible change, not a replacement request.", text)
        self.assertTrue(self.make(narrow="x", replace=True)["written"].endswith("-fee-2026-03-10.md"))
        self.assertTrue(self.make(narrow="é" * 300, replace=True)["written"])
        for bad in ("", " ", " x", "x ", "x" * 301, "a\nb", "a\tb", "<b>x</b>", "a<b", "a\u202eb", "a\u2028b", "a\x00b"):
            before = snapshot(self.root)
            with self.assertRaises(ValueError) as caught:
                self.make(narrow=bad, replace=True)
            self.assertNotIn("x" * 20, str(caught.exception))
            self.assertEqual(snapshot(self.root), before)
        flagged = self.make(narrow="The agency violated the law by charging this", replace=True)  # alert only
        self.assertTrue(flagged["review_flags"])
        self.assertTrue(all("violated" not in n for n in flagged["review_flags"]))
        with mock.patch.object(kit_fee_reply, "legal_notes", side_effect=lambda items: [
                "review-required: legal-claim wording (guard-error)"]):
            self.assertEqual(self.make(narrow="ok", replace=True)["review_flags"],
                             ["review-required: legal-claim wording (guard-error)"] * 2)

    def test_private_writer_replace_and_failures(self):
        self.estimate()
        result = self.make(narrow="first scope")
        path = self.root / result["written"]
        path.write_text(path.read_text(encoding="utf-8") + "MY EDIT\n", encoding="utf-8")
        edited = path.read_bytes()
        self.refuses_keep(path, edited, "already exists")  # no --replace, even for identical regenerated bytes
        self.estimate(7000, "2026-03-11")  # a revised estimate gets its own file
        self.assertNotEqual(self.make()["written"], result["written"])
        self.sent[A]["steps"].pop()
        self.save()
        self.make(replace=True)
        self.assertNotIn("first scope", path.read_text(encoding="utf-8"))  # omitted --narrow removes the old one
        self.assertNotIn("MY EDIT", path.read_text(encoding="utf-8"))
        good = path.read_bytes()
        for patched in ("os.replace", "os.fsync"):
            with mock.patch(f"campaign_tool.kit_fee_reply.{patched}", side_effect=OSError("disk")):
                with self.assertRaises(OSError):
                    self.make(replace=True)
            self.assertEqual(path.read_bytes(), good)
            self.assertEqual(sorted(p.name for p in path.parent.iterdir()), [path.name])  # no temp left behind
        with self.assertRaises(ValueError):
            self.make(narrow="<b>", replace=True)
        self.assertEqual(path.read_bytes(), good)

    def refuses_keep(self, path, data, text, **kwargs):
        with self.assertRaises(ValueError) as caught:
            self.make(**kwargs)
        self.assertIn(text, str(caught.exception))
        self.assertEqual(path.read_bytes(), data)

    def test_private_writer_links_and_folders(self):
        self.estimate()
        name = f"{A}--main-fee-2026-03-10.md"
        folder = self.root / "kit" / "followups"
        folder.mkdir(mode=0o700)
        outside = self.tmp / "outside.txt"
        outside.write_text("keep", encoding="utf-8")
        (folder / name).symlink_to(outside)
        for replace in (False, True):
            with self.assertRaises(ValueError):
                self.make(replace=replace)
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep")
        (folder / name).unlink()
        os.link(outside, folder / name)  # a hardlinked target
        with self.assertRaises(ValueError):
            self.make(replace=True)
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep")
        (folder / name).unlink()
        os.mkfifo(folder / name)
        with self.assertRaises(ValueError):
            self.make(replace=True)
        (folder / name).unlink()
        folder.chmod(0o750)
        with self.assertRaises(ValueError):
            self.make()
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o750)  # refused, never chmoded
        self.assertEqual(list(folder.iterdir()), [])
        folder.chmod(0o700)
        folder.rmdir()
        folder.symlink_to(self.tmp, target_is_directory=True)  # a linked followups folder
        with self.assertRaises(ValueError):
            self.make()
        folder.unlink()
        linked = self.tmp / "linked"
        linked.symlink_to(self.root, target_is_directory=True)  # a linked campaign folder
        with self.assertRaises(ValueError):
            kit_fee_reply.fee_reply(linked, A, today=TODAY, law_base=self.base)
        shutil.move(self.root / "kit" / "agencies.json", self.tmp / "agencies.real")
        (self.root / "kit" / "agencies.json").symlink_to(self.tmp / "agencies.real")  # a linked input
        with self.assertRaises(ValueError):
            self.make()
        (self.root / "kit" / "agencies.json").unlink()
        with self.assertRaises(OSError):  # a missing input is never created
            self.make()
        self.assertFalse(folder.exists())

    def test_nested_cli_preserves_generator(self):
        self.estimate()
        before = snapshot(self.root)
        with mock.patch.object(law, "JURISDICTIONS", self.base):
            code, out, err = run("kit", "fee-reply", "--directory", self.root, "--agency", A, "--narrow", "just 2026")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.endswith(SENT + "\n"), out)
        printed = json.loads(out[:-len(SENT) - 1])
        self.assertEqual(set(printed), {"written", "agency_id", "request", "estimate_on", "amount_cents", "rule_id",
                                        "law_package_status"})
        self.assertEqual(snapshot(self.root).keys() - before.keys(), {printed["written"]})
        with mock.patch.object(cli, "kit") as generator:
            for args in (("fee-reply",), ("fee-reply", "--agency", A, "--online"), ("fee-reply", "--agency", A, "--scope", "s"),
                         ("fee-reply", "--agency", A, "--include", "police"), ("--agency", A), ("--request", "main"),
                         ("--narrow", "x"), ("--replace",), ("nope",)):
                code, out, err = run("kit", "--directory", self.root, *args)
                self.assertEqual((code, out), (2, ""), args)
            generator.assert_not_called()
            self.assertEqual(run("kit", "--directory", self.root)[0], 0)
            generator.assert_called_once()
        code, out, err = run("kit", "fee-reply", "--help")
        self.assertEqual(code, 0)
        self.assertIn("--narrow", out)
        code, out, err = run("kit", "fee-reply", "--directory", self.root, "--agency", A)  # shipped package: no fee rule
        self.assertEqual((code, out), (1, ""))
        self.assertIn("No unambiguous fee rule", err)
        self.assertNotIn("Traceback", err)

    def test_reports_and_preservation_coexist(self):
        self.estimate()
        reports = (("kit-due", "--as-of", "2026-06-01"), ("kit-fees",), ("kit-status",), ("kit-status", "--csv"))
        results = {}
        with mock.patch.object(law, "JURISDICTIONS", self.base):
            for report in reports:
                results[report] = run(*report[:1], "--directory", self.root, *report[1:])
            before = snapshot(self.root)
            code, out, err = run("kit", "fee-reply", "--directory", self.root, "--agency", A)
            self.assertEqual(code, 0, err)
            written = json.loads(out.split(SENT)[0])["written"]
            after = snapshot(self.root)
            self.assertEqual(after.keys() - before.keys(), {written})
            self.assertEqual({k: v for k, v in after.items() if k != written}, before)  # bytes and mtimes unchanged
            self.assertFalse((self.root / "kit" / "preservation").exists())
            self.assertFalse((self.root / "public").exists())
            for report in reports:
                self.assertEqual(run(*report[:1], "--directory", self.root, *report[1:]), results[report], report)


if __name__ == "__main__":
    unittest.main()
