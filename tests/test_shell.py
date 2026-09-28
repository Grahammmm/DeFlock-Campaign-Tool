import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from campaign_tool.shell import render
from campaign_tool.site import preview

ROOT = Path(__file__).resolve().parents[1]
CONFIG = {"name": "Cedar Records", "county": "Cedar County (fictional)", "state": "CA"}


def payload():
    return {"schema_version": 1, "language": "en",
            "html": {"head": "", "header": "", "main": "", "footer": ""},
            "styles": {"font_family": "Georgia,serif", "font_faces": ""}}


class ShellTests(unittest.TestCase):
    def test_deterministic(self):
        self.assertEqual(preview(CONFIG), preview(CONFIG))

    def test_exact_structure(self):
        self.assertEqual(render(payload())["html"], '<!doctype html>\n<html lang="en">\n<head>\n</head>\n<body>\n  <main id="top">\n  </main>\n</body>\n</html>\n')

    def test_slots_are_single_pass(self):
        data = payload()
        data["html"]["main"] = "{{FOOTER}}"
        data["html"]["footer"] = "NOT_INSERTED_IN_MAIN"
        self.assertIn("{{FOOTER}}  </main>", render(data)["html"])

    def test_escapes_all_public_fields(self):
        result = preview({key: '<script>"&' for key in CONFIG})["html"]
        self.assertNotIn("<script>", result)
        self.assertIn("&lt;script&gt;&quot;&amp;", result)

    def test_unknown_private_config_is_not_exported(self):
        result = preview({**CONFIG, "private": "DO_NOT_PUBLISH", "newsletter": {"key": "DO_NOT_PUBLISH"}})
        self.assertNotIn("DO_NOT_PUBLISH", json.dumps(result))

    def test_language_rejects_attribute_injection(self):
        data = payload()
        data["language"] = 'en" onload="alert(1)'
        with self.assertRaises(ValueError):
            render(data)

    def test_contract_rejects_extra_or_missing_fields(self):
        for section in (None, "html", "styles"):
            for operation in ("extra", "missing"):
                data = payload()
                target = data if section is None else data[section]
                if operation == "extra":
                    target["unexpected"] = ""
                else:
                    target.pop(next(iter(target)))
                with self.assertRaises(ValueError):
                    render(data)

    def test_rejects_nonstring_slots(self):
        data = payload()
        data["html"]["head"] = []
        with self.assertRaises(ValueError):
            render(data)

    def test_schema_version_is_strict(self):
        for version in (True, 2, "1"):
            data = payload()
            data["schema_version"] = version
            with self.assertRaises(ValueError):
                render(data)

    def test_no_remote_assets_or_script_in_preview(self):
        page = preview(CONFIG)
        self.assertNotRegex(page["html"], r"<(?:script|iframe|img)\b")
        self.assertNotRegex(page["css"], r"url\(")
        self.assertNotIn("sibforms", json.dumps(page))

    def test_mobile_and_reduced_motion_styles_preserved(self):
        css = preview(CONFIG)["css"]
        self.assertIn("@media(max-width:850px)", css)
        self.assertIn("prefers-reduced-motion:reduce", css)

    def test_cli_still_initializes_ingests_and_builds(self):
        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory) / "campaign"
            def cli(*args):
                return subprocess.run([sys.executable, "-m", "campaign_tool", *args,
                                       "--directory", str(campaign)], cwd=ROOT,
                                      capture_output=True, text=True, check=True)
            cli("init", "--name", CONFIG["name"], "--county", CONFIG["county"], "--state", "CA")
            source = Path(directory) / "synthetic.txt"
            source.write_text("Synthetic record. Not a finding.", encoding="utf-8")
            cli("ingest", "--file", str(source), "--source-id", "fixture-001")
            cli("ingest", "--file", str(source), "--source-id", "fixture-001")
            status = json.loads(cli("status").stdout)
            self.assertEqual(status["receipt_occurrences"], 1)
            cli("build")
            self.assertIn("campaign-banner", (campaign / "public/index.html").read_text())
            self.assertNotIn("Synthetic record", (campaign / "public/index.html").read_text())
            self.assertFalse(json.loads(cli("doctor").stdout)["production_ready"])


if __name__ == "__main__":
    unittest.main()
