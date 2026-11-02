"""kit/request-settings.json fills requester, fee cap and date range in every draft. Offline, synthetic."""
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit  # noqa: E402

REQUESTER = ["Pat Example", "100 Synthetic Lane, Faketown CA 00000", "pat@example.org"]
FULL = {"schema_version": 1, "requester": REQUESTER, "fee_cap_usd": 50, "date_from": "2023-01-01",
        "date_to": "today"}
DRAFT = "ca-morro-bay-police.md"
FEE_STEP, FILL_STEP = kit.NEXT_STEPS[2], kit.NEXT_STEPS[3]


def cli(*arguments):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, arguments)],
                          cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)


class RequestSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-settings-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        result = cli("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                     "--state", "CA")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.kit = self.root / "kit"
        self.requests = self.kit / "requests"
        self.settings = self.kit / kit.SETTINGS_NAME

    def build(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return kit.build_kit(self.root, **kwargs)

    def write(self, document=None, raw=None):
        self.kit.mkdir(exist_ok=True)
        if raw is None:
            raw = json.dumps(document).encode("utf-8")
        self.settings.write_bytes(raw if isinstance(raw, bytes) else raw.encode("utf-8"))

    def drafts(self, folder=None):
        return {p.name: p.read_text(encoding="utf-8") for p in sorted((folder or self.requests).glob("*.md"))}

    def tree(self):
        if not self.requests.exists():
            return "absent"
        digest = hashlib.sha256()
        for path in sorted(self.kit.rglob("*")):
            if path.is_file() and path.name != kit.SETTINGS_NAME:
                digest.update(str(path.relative_to(self.kit)).encode() + b"\0" + path.read_bytes())
        return digest.hexdigest()

    def assert_refused(self, key, document=None, raw=None):
        before = self.tree()
        self.write(document, raw)
        with self.assertRaises(ValueError) as caught:
            self.build()
        message = str(caught.exception)
        self.assertTrue(message.startswith(f"kit/request-settings.json: {key}: "), message)
        for secret in REQUESTER + ["Faketown"]:
            self.assertNotIn(secret, message)
        self.assertEqual(self.tree(), before)
        return message

    def test_no_settings_file_keeps_placeholders(self):
        summary = self.build()
        drafts = self.drafts()
        self.assertGreater(len(drafts), 1)
        for text in drafts.values():
            self.assertIn(kit.DEFAULT_REQUESTER, text)
            self.assertIn(kit.DEFAULT_FEE_CAP, text)
            self.assertIn("[date range, e.g. 2023-01-01 through today]", text)
        agency = next(a for a in json.loads((self.kit / "agencies.json").read_text()) if a["agency_id"] + ".md" == DRAFT)
        package = kit.law.load_package("us-ca")
        self.assertEqual(drafts[DRAFT], kit.render_combined_request(package, agency))
        self.assertEqual(summary["request_settings"], {"requester": False, "fee_cap": False, "date_range": False})
        self.assertEqual(summary["next_steps"], kit.NEXT_STEPS)
        self.assertEqual(kit.load_request_settings(self.kit), {})

    def test_full_settings_fill_every_law_package_and_template_draft(self):
        self.write(FULL)
        summary = self.build()
        empty = self.tmp / "no-law"
        empty.mkdir()
        law_drafts = self.drafts()
        self.build(law_base=empty)
        template_drafts = self.drafts()
        self.assertIn("NO REVIEWED LAW PACKAGE", template_drafts[DRAFT])
        for text in list(law_drafts.values()) + list(template_drafts.values()):
            self.assertIn("\n".join(REQUESTER), text)
            self.assertIn("beyond $50.00.", text)
            self.assertIn("2023-01-01 through today", text)
            for placeholder in ("[requester-approved", "[explicit approved cap", "[date range", "[narrow date range]"):
                self.assertNotIn(placeholder, text)
        self.assertEqual(summary["request_settings"], {"requester": True, "fee_cap": True, "date_range": True})
        self.assertNotIn(FEE_STEP, summary["next_steps"])
        self.assertFalse([s for s in summary["next_steps"] if s.startswith("Fill the")])
        self.assertEqual(json.loads((self.kit / "summary.json").read_text())["next_steps"], summary["next_steps"])

    def test_only_fee_cap_fills_fee_and_keeps_other_placeholders(self):
        self.write({"schema_version": 1, "fee_cap_usd": 1234.5})
        summary = self.build()
        for text in self.drafts().values():
            self.assertIn("beyond $1,234.50.", text)
            self.assertIn(kit.DEFAULT_REQUESTER, text)
            self.assertIn("[date range, e.g. 2023-01-01 through today]", text)
        self.assertEqual(summary["request_settings"], {"requester": False, "fee_cap": True, "date_range": False})
        self.assertNotIn(FEE_STEP, summary["next_steps"])
        self.assertIn(FILL_STEP, summary["next_steps"])

    def test_zero_cap_partial_slots_and_date_from_alone(self):
        self.write({"schema_version": 1, "fee_cap_usd": 0, "date_from": "2024-02-29"})
        summary = self.build()
        text = self.drafts()[DRAFT]
        self.assertIn("beyond $0.00.", text)
        self.assertIn("2024-02-29 through today", text)
        self.assertIn("Fill the requester block in each kit/requests/*.md draft.", summary["next_steps"])
        self.assertEqual(kit.load_request_settings(self.kit), {"fee_cap": "$0.00",
                                                                "date_range": "2024-02-29 through today"})
        self.write({"schema_version": 1, "requester": ["Pat Example"], "fee_cap_usd": 10000,
                    "date_from": "2023-01-01", "date_to": "2023-12-31"})
        self.assertEqual(kit.load_request_settings(self.kit),
                         {"requester_block": "Pat Example", "fee_cap": "$10,000.00",
                          "date_range": "2023-01-01 through 2023-12-31"})

    def test_bad_fee_cap_stops_before_any_draft(self):
        for raw in ("-1", "10000.01", "NaN", "Infinity", '"50"', "true", "null"):
            with self.subTest(fee=raw):
                self.assert_refused("fee_cap_usd", raw='{"schema_version": 1, "fee_cap_usd": %s}' % raw)
        self.assertFalse(self.requests.exists())

    def test_bad_fee_cap_on_rerun_changes_no_draft(self):
        self.build()
        self.assert_refused("fee_cap_usd", {"schema_version": 1, "requester": REQUESTER, "fee_cap_usd": -1})

    def test_cli_stops_naming_key_without_echoing_value(self):
        self.write({"schema_version": 1, "requester": REQUESTER + ["x" * 121]})
        result = cli("kit", "--directory", self.root)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stderr.startswith("Stopped: kit/request-settings.json: requester: "), result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        for secret in REQUESTER:
            self.assertNotIn(secret, result.stderr + result.stdout)
        self.assertFalse(self.requests.exists())

    def test_bad_dates_are_refused_naming_the_key(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        cases = [
            ("date_from", {"date_from": tomorrow}),
            ("date_to", {"date_from": "2023-01-01", "date_to": tomorrow}),
            ("date_to", {"date_from": "2023-06-01", "date_to": "2023-05-31"}),
            ("date_to", {"date_to": "today"}),
            ("date_to", {"date_to": "2023-05-31"}),
            ("date_from", {"date_from": "2023-02-30"}),
            ("date_from", {"date_from": "2023-1-01"}),
            ("date_from", {"date_from": "today"}),
            ("date_to", {"date_from": "2023-01-01", "date_to": "Today"}),
            ("date_from", {"date_from": 20230101}),
        ]
        for key, fields in cases:
            with self.subTest(fields=fields):
                self.assert_refused(key, {"schema_version": 1, "requester": REQUESTER, **fields})

    def test_bad_file_shapes_are_refused_without_echo(self):
        secret = json.dumps(REQUESTER)
        cases = [
            ("Pat_Example", '{"schema_version": 1, "Pat_Example": 1}'),
            ("(unnamed)", '{"schema_version": 1, "Pat Example, 100 Synthetic Lane": 1}'),
            ("schema_version", '{"schema_version": 2, "requester": %s}' % secret),
            ("schema_version", '{"schema_version": true, "requester": %s}' % secret),
            ("schema_version", '{"requester": %s}' % secret),
            ("file", "[%s]" % secret),
            ("file", '{"schema_version": 1, "requester": %s' % secret),
            ("file", b'{"schema_version": 1, "requester": ["Pat \xff Example"]}'),
            ("file", "[" * 5000 + "]" * 5000),
            ("file", '{"schema_version": 1, "requester": %s}%s' % (secret, " " * 65536)),
        ]
        for key, raw in cases:
            with self.subTest(key=key, raw=raw[:40]):
                self.assert_refused(key, raw=raw)

    def test_symlinked_or_folder_settings_refused(self):
        outside = self.tmp / "outside.json"
        outside.write_text(json.dumps(FULL))
        self.kit.mkdir()
        self.settings.symlink_to(outside)
        with self.assertRaises(ValueError) as caught:
            self.build()
        self.assertTrue(str(caught.exception).startswith("kit/request-settings.json: file: "))
        self.assertFalse(self.requests.exists())
        os.unlink(self.settings)
        self.settings.mkdir()
        with self.assertRaises(ValueError):
            self.build()
        self.assertFalse(self.requests.exists())

    def test_bad_requester_lines_refused(self):
        for lines in (["Pat\x07Example"], ["x" * 121], [f"line {n}" for n in range(9)], [], [""], ["Pat\nExample"],
                      ["Pat\x85Example"], "Pat Example", [1]):
            with self.subTest(lines=repr(lines)[:30]):
                self.assert_refused("requester", {"schema_version": 1, "requester": lines})
        self.write({"schema_version": 1, "requester": ["x" * 120] * 8})
        self.assertEqual(kit.load_request_settings(self.kit)["requester_block"], "\n".join(["x" * 120] * 8))

    def test_edited_draft_kept_and_unedited_rewritten_on_settings_change(self):
        self.write(FULL)
        self.build()
        edited = self.requests / DRAFT
        edited.write_bytes(edited.read_bytes() + b"Organizer note\n")
        kept = edited.read_bytes()
        self.write({**FULL, "fee_cap_usd": 75, "date_from": "2024-01-01"})
        with contextlib.redirect_stdout(io.StringIO()) as out:
            summary = kit.build_kit(self.root)
        self.assertIn(f"kept edited draft requests/{DRAFT}", out.getvalue())
        self.assertEqual(edited.read_bytes(), kept)
        self.assertEqual(summary["drafts_kept_edited"], 1)
        fresh = (self.kit / "requests.suggested" / DRAFT).read_text()
        self.assertIn("beyond $75.00.", fresh)
        self.assertIn("2024-01-01 through today", fresh)
        record = json.loads((self.kit / "drafts-written.json").read_text())["drafts"]
        others = {name: text for name, text in self.drafts().items() if name != DRAFT}
        self.assertTrue(others)
        for name, text in others.items():
            self.assertIn("beyond $75.00.", text)
            self.assertNotIn("$50.00", text)
            self.assertEqual(record[name], hashlib.sha256(text.encode("utf-8")).hexdigest())


if __name__ == "__main__":
    unittest.main()
