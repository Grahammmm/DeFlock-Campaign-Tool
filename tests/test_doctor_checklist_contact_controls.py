"""RF0600-1: refuse control-bearing stale-contact agency ids before the checklist displays them."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from campaign_tool.doctor_checklist import checklist, render

REPO = Path(__file__).resolve().parents[1]
MESSAGE = "checklist stale contact agency_id contains a control character; repair kit/agencies.json"
CONTROLS = [chr(c) for c in (*range(0, 32), *range(127, 160), 0x2028, 0x2029)]
FORGED = "ca-synthetic\n[ok] FORGED"
OLD = {"selected": True, "verified": True, "verification": {"checked_on": "2001-01-01"}}


def report(ids, more=0):
    return {"stale_contacts": [{"agency_id": i} for i in ids], "stale_contacts_more": more}


def snapshot(root):
    return {str(p.relative_to(root)): (p.stat().st_mode, p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
            for p in [root, *sorted(root.rglob("*"))]}


class DirectTests(unittest.TestCase):
    def refused(self, call):
        with self.assertRaises(ValueError) as caught:
            call()
        self.assertEqual(str(caught.exception), MESSAGE)

    def test_displayed_contact_control_matrix_refuses_value_free(self):
        git = self.enterContext(mock.patch("campaign_tool.git_exposure.git_exposure_row", return_value=None))
        for control in CONTROLS:
            for slot in range(3):
                ids = ["ok-a", "ok-b", "ok-c"]
                ids[slot] = "x" + control + "y"
                with self.subTest(code=ord(control), slot=slot):
                    self.refused(lambda: checklist(report(ids), "D"))
                    self.refused(lambda: render(report(ids), "D"))
                    self.refused(lambda: render(report(ids), "D", as_json=True))
        self.assertTrue(git.called)

    def test_only_first_three_displayed_contacts_are_admitted(self):
        for hidden in (3, 4):
            ids = ["a", "b", "c", "d", "e"]
            ids[hidden] = FORGED
            row = {r["key"]: r for r in checklist(report(ids, more=2), "D")}["stale_contacts"]
            self.assertIn("Re-check 7 contact(s)", row["next"])
            self.assertIn("for a, b, c, …;", row["next"])
        self.refused(lambda: checklist(report([FORGED, "b"]), "D"))

    def test_ordinary_advice_is_unchanged(self):
        for ids, more, expected in ((["a"], 0, "for a;"), (["a", "b", "c"], 0, "for a, b, c;"),
                                    (["a", "b", "c", "d"], 0, "for a, b, c, …;"), (["é-ü"], 0, "for é-ü;"),
                                    (["a"], 100, "for a, …;")):
            row = {r["key"]: r for r in checklist(report(ids, more), "D")}["stale_contacts"]
            self.assertIn(expected, row["next"])
            self.assertEqual(row["owner"], "organizer")
        self.assertEqual({r["key"]: r for r in checklist({"stale_contacts": []}, "D")}["stale_contacts"]["state"], "ok")

    def test_directory_control_keeps_precedence(self):
        with self.assertRaises(ValueError) as caught:
            checklist(report([FORGED]), "bad\ndir")
        self.assertIn("checklist directory contains", str(caught.exception))


class RealChainTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-contact-controls-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "campaign"
        self.assertEqual(self.cli("init", "--directory", str(self.root), "--name", "Synthetic Campaign",
                                  "--county", "Synthetic County", "--state", "ca").returncode, 0)

    def cli(self, *args):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(self.base))
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO, env=env,
                              capture_output=True, text=True, timeout=120)

    def agencies(self, ids):
        (self.root / "kit").mkdir(exist_ok=True)
        entries = [dict(OLD, agency_id=i, name="N") for i in ids]
        (self.root / "kit" / "agencies.json").write_text(json.dumps(entries))

    def doctor(self, *extra):
        return self.cli("doctor", "--directory", str(self.root), "--contact-max-age-days", "30", *extra)

    def test_real_producer_payload_reaches_checklist_boundary(self):
        for payload in (FORGED, "ca-esc\x1b[2J", "ca-nel\x85", "ca-ls\u2028"):
            with self.subTest(payload=ascii(payload)):
                self.agencies([payload])
                plain = self.doctor()
                self.assertEqual(plain.returncode, 0, plain.stderr)
                self.assertEqual(json.loads(plain.stdout)["stale_contacts"][0]["agency_id"], payload)

    def test_real_cli_text_and_json_have_no_partial_success(self):
        self.agencies(["ca-ok-a", FORGED])
        before = snapshot(self.base)
        for extra in (("--checklist",), ("--checklist", "--json")):
            with self.subTest(extra=extra):
                done = self.doctor(*extra)
                self.assertEqual((done.returncode, done.stdout, done.stderr), (1, "", "Stopped: " + MESSAGE + "\n"))
        self.assertEqual(snapshot(self.base), before)

    def test_cli_success_is_unchanged_and_nonsticky(self):
        self.agencies(["ca-ok-a", "ca-ok-b"])
        good = self.doctor("--checklist")
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertIn("Re-check 2 contact(s)", good.stdout)
        self.assertIn("for ca-ok-a, ca-ok-b;", good.stdout)
        self.agencies([FORGED])
        self.assertEqual(self.doctor("--checklist").returncode, 1)
        self.agencies(["ca-ok-a", "ca-ok-b"])
        again = self.doctor("--checklist")
        self.assertEqual((again.returncode, again.stdout), (0, good.stdout))
        no_age = self.cli("doctor", "--directory", str(self.root), "--checklist")
        self.assertEqual(no_age.returncode, 0, no_age.stderr)


if __name__ == "__main__":
    unittest.main()
