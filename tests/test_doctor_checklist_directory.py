"""RF0115-14: replay offline checklist advice from option-looking campaign paths."""
import contextlib
from datetime import date
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import tempfile
import unittest
from unittest import mock

from campaign_tool import cli
from campaign_tool.doctor_checklist import checklist, render

REPO = Path(__file__).resolve().parents[1]
REPORT = {"agencies_selected": 0, "kit_built": False,
          "stale_contacts": [{"agency_id": "synthetic-agency"}],
          "stale_comment_kits": [{"meeting": "m2"}, {"meeting": "m1"}, {"meeting": "m1"}],
          "safe_to_send_automatically": False}


def commands(rows):
    return [shlex.split(command) for row in rows for command in re.findall(
        r"`(python3 -m campaign_tool [^`]+)`", row["next"] or "")]


def snapshot(root):
    return {str(p.relative_to(root)): (p.stat().st_mode, p.read_bytes() if p.is_file() else None)
            for p in [root, *sorted(root.rglob("*"))]}


class DirectoryAdviceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-checklist-directory-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.old_cwd = Path.cwd()
        os.chdir(self.base)
        self.addCleanup(os.chdir, self.old_cwd)
        self.enterContext(mock.patch("socket.socket.connect", side_effect=AssertionError("network used")))
        self.enterContext(mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used")))
        self.name = "--campaign"
        self.root = self.base / self.name
        self.neighbor = self.base / "neighbor"
        self.neighbor.mkdir()
        (self.neighbor / "sentinel").write_bytes(b"synthetic neighbor must not change\n")
        self.neighbor_before = snapshot(self.neighbor)

    def run_cli(self, *args, code=0):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.argv", ["campaign_tool", *args]), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                result = cli.main()
            except SystemExit as exc:
                result = exc.code
        self.assertEqual(result or 0, code, err.getvalue())
        self.assertNotIn("argument --directory: expected one argument", err.getvalue())
        self.assertEqual(snapshot(self.neighbor), self.neighbor_before)
        return out.getvalue(), err.getvalue()

    def init(self):
        self.run_cli("init", "--directory=" + self.name, "--name", "Synthetic Campaign",
                     "--county", "San Luis Obispo", "--state", "ca")

    def doctor(self):
        return json.loads(self.run_cli("doctor", "--directory=" + self.name, "--checklist", "--json")[0])

    def replay(self, command, values=None, code=0):
        self.assertEqual(command[:3], ["python3", "-m", "campaign_tool"])
        return self.run_cli(*[(values or {}).get(token, token) for token in command[3:]], code=code)

    def test_real_leading_hyphen_campaign_kit_advice_replays(self):
        self.init()
        before = snapshot(self.base)
        rows = self.doctor()
        self.assertEqual(snapshot(self.base), before)  # advice itself never builds the kit
        advice = commands([row for row in rows if row["key"] in ("agencies_selected", "kit_built")])
        self.assertEqual(len(advice), 2)
        for command in advice:
            self.replay(command)  # no directory-token rewriting, including repeated kit generation
        self.assertTrue((self.root / "kit" / "agencies.json").is_file())
        self.assertTrue(any(a["selected"] for a in json.loads((self.root / "kit" / "agencies.json").read_text())))
        self.assertFalse(any(row["state"] == "todo" for row in self.doctor()
                             if row["key"] in ("agencies_selected", "kit_built")))

    def test_every_next_command_preserves_option_looking_directory(self):
        for name in ("-campaign", "--campaign", "--directory", "-", "-odd campaign's"):
            for exists, count in ((False, 5), (True, 7)):
                with self.subTest(name=name, agency_list_exists=exists):
                    rows = checklist(REPORT, name, exists)
                    emitted = commands(rows)
                    self.assertEqual(len(emitted), count)
                    for command in emitted:
                        operand = command[command.index("--directory") + 1]
                        self.assertEqual(operand, "./" + name)
                        self.assertEqual(Path(operand).resolve(), self.base / name)
                        self.assertEqual(command.count("--directory"), 1)
                    restages = [c for c in emitted if c[3] == "meetings-stage"]
                    self.assertEqual([c[c.index("--id") + 1] for c in restages], ["m1", "m2"])
                    self.assertEqual(checklist(REPORT, name, exists), rows)  # no accumulating prefix

    def test_select_add_and_contact_guidance_reach_real_handlers(self):
        self.init()
        self.replay(commands(self.doctor())[0])
        path = self.root / "kit" / "agencies.json"
        agencies = json.loads(path.read_text())
        for agency in agencies:
            agency["selected"] = False
        path.write_text(json.dumps(agencies))  # fixture preparation, not rendering
        rows = self.doctor()
        row = next(r for r in rows if r["key"] == "agencies_selected")
        select, add, rerun = commands([row])
        self.replay(select, {"ID": "ca-synthetic-missing"}, code=1)
        self.replay(select, {"ID": agencies[0]["agency_id"]})
        self.replay(add, {"ID": "ca-synthetic-extra", "NAME": "Synthetic Agency", "KIND": "police"})
        before_duplicate = snapshot(self.root)
        self.replay(add, {"ID": "ca-synthetic-extra", "NAME": "Synthetic Agency", "KIND": "police"}, code=1)
        self.assertEqual(snapshot(self.root), before_duplicate)
        self.replay(rerun)
        saved = json.loads(path.read_text())
        self.assertTrue(next(a for a in saved if a["agency_id"] == agencies[0]["agency_id"])["selected"])
        self.assertTrue(any(a["agency_id"] == "ca-synthetic-extra" for a in saved))
        saved[0].update(verified=True, verification={"checked_on": "2001-01-01"})
        path.write_text(json.dumps(saved))
        stale = json.loads(self.run_cli("doctor", "--directory=" + self.name, "--checklist", "--json",
                                       "--contact-max-age-days", "30")[0])
        contact = commands([next(r for r in stale if r["key"] == "stale_contacts")])[0]
        values = {"ID": saved[0]["agency_id"], "EMAIL": "records@agency.example",
                  "URL": "https://agency.example/records", "YYYY-MM-DD": date.today().isoformat()}
        no_contact = contact.copy()
        index = no_contact.index("--records-email")
        del no_contact[index:index + 2]
        before_refusal = snapshot(self.root)
        self.replay(no_contact, values, code=1)
        self.assertEqual(snapshot(self.root), before_refusal)
        self.replay(contact, values)
        checked = next(a for a in json.loads(path.read_text()) if a["agency_id"] == saved[0]["agency_id"])
        self.assertEqual(checked["verification"]["checked_on"], date.today().isoformat())
        self.assertEqual(checked["records_email"], "records@agency.example")

    def test_restaging_guidance_uses_same_campaign(self):
        shutil.copytree(REPO / "examples" / "fictional-campaign", self.root,
                        ignore=shutil.ignore_patterns("public"))
        kit = self.root / "kit" / "meetings.json"
        kit.parent.mkdir(exist_ok=True)
        entries = [{"id": mid, "body": "Synthetic Council", "starts_at": "2026-11-18T18:00:00-08:00"}
                   for mid in ("m2", "m1")]
        kit.write_text(json.dumps({"schema_version": 1, "meetings": entries}))
        for mid in ("m2", "m1"):
            self.run_cli("meetings-stage", "--directory=" + self.name, "--id", mid)
        finding_path = self.root / "content" / "findings" / "cedar-policy-posted.json"
        finding = json.loads(finding_path.read_text())
        finding["corrections"] = [{"date": "2026-10-15", "note": "Synthetic corrected source."}]
        finding_path.write_text(json.dumps(finding))
        row = next(r for r in self.doctor() if r["key"] == "stale_comment_kits")
        advice = commands([row])
        self.assertEqual([c[c.index("--id") + 1] for c in advice], ["m1", "m2"])
        entries[0]["status"] = "cancelled"
        kit.write_text(json.dumps({"schema_version": 1, "meetings": entries}))
        before = snapshot(self.root)
        self.replay(advice[1], code=1)
        self.assertEqual(snapshot(self.root), before)
        del entries[0]["status"]
        kit.write_text(json.dumps({"schema_version": 1, "meetings": entries}))
        for command in advice:
            self.replay(command)
            mid = command[command.index("--id") + 1]
            document = json.loads((self.root / "content" / "meetings" / (mid + ".json")).read_text())
            self.assertIn("corrected 2026-10-15", document["comment_kit_md"])
        self.assertEqual(next(r for r in self.doctor() if r["key"] == "stale_comment_kits")["state"], "ok")
        self.assertFalse((self.root / "public").exists())

    def test_ordinary_directory_advice_is_byte_identical(self):
        operands = {"D": "D", "/tmp/campaign": "/tmp/campaign", "relative/campaign": "relative/campaign",
                    "../campaign": "../campaign", "./-campaign": "./-campaign", "~/campaign": "'~/campaign'",
                    "a b": "'a b'", "a'b": "'a'\"'\"'b'", "a;$HOME": "'a;$HOME'"}
        for directory, operand in operands.items():
            with self.subTest(directory=directory):
                rows = checklist(REPORT, directory, True)
                expected = [
                    "Select at least one agency in kit/agencies.json: `python3 -m campaign_tool kit-agency select --directory {d} --id ID` (ids are listed in that file; add one with `python3 -m campaign_tool kit-agency add --directory {d} --id ID --name NAME --kind KIND`), then rerun `python3 -m campaign_tool kit --directory {d}`",
                    "Build the kit: `python3 -m campaign_tool kit --directory {d}`",
                    "Re-check 1 contact(s): `python3 -m campaign_tool kit-verify-agency --directory {d} --agency-id ID --records-email EMAIL --source-url URL --checked-on YYYY-MM-DD` for synthetic-agency; give the contact you checked: --records-email EMAIL, --records-url URL or --portal URL (at least one is required)",
                    "Re-stage with corrected findings: `python3 -m campaign_tool meetings-stage --directory {d} --id m1 --replace`; `python3 -m campaign_tool meetings-stage --directory {d} --id m2 --replace`"]
                self.assertEqual([r["next"] for r in rows if r["state"] == "todo"],
                                 [text.format(d=operand) for text in expected])
                missing = checklist({"agencies_selected": 0}, directory)[0]
                self.assertEqual(missing["next"], "Select agencies: `python3 -m campaign_tool kit --directory " + operand + "`")

    def test_render_keeps_git_input_and_does_not_apply_repairs(self):
        self.init()
        before = snapshot(self.base)
        with mock.patch("campaign_tool.git_exposure.git_exposure_row", return_value=None) as git_input:
            rows = checklist(REPORT, self.name, True)
            encoded = json.loads(render(REPORT, self.name, as_json=True, agency_list_exists=True))
            text = render(REPORT, self.name, agency_list_exists=True)
        self.assertEqual(git_input.call_args_list, [mock.call(self.name), mock.call(self.name)])
        self.assertEqual(encoded, rows)
        for row in rows:
            if row["state"] == "todo":
                self.assertIn(row["next"], text)
        ordinary = checklist(REPORT, "D", True)
        self.assertEqual([{k: v for k, v in r.items() if k != "next"} for r in rows],
                         [{k: v for k, v in r.items() if k != "next"} for r in ordinary])
        self.assertIn("4 to do · 0 ok · not a legal status", text)
        self.assertIn("every send is approved by a person", text)
        self.assertEqual(snapshot(self.base), before)


if __name__ == "__main__":
    unittest.main()
