"""Synthetic config contract: no campaign originals or settings are read."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records import campaign_config as cc


class CampaignConfigTests(unittest.TestCase):
    def test_campaign_timezone_defaults_and_invalid_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "campaign.json"
            self.assertEqual(cc.load_timezone(root), cc.DEFAULT_TIMEZONE)
            self.assertEqual(list(root.iterdir()), [])
            for doc, expected in (({"schema_version": 1, "name": "Harbor Pilot"}, cc.DEFAULT_TIMEZONE),
                                  *(({"schema_version": 1, "timezone": z}, z)
                                    for z in (cc.DEFAULT_TIMEZONE, "UTC", "Asia/Kolkata"))):
                config.write_text(json.dumps(doc))
                before = (config.read_bytes(), config.stat().st_mode)
                self.assertEqual(cc.load_timezone(root), expected)
                self.assertEqual((config.read_bytes(), config.stat().st_mode), before)
            invalid = [json.dumps({"schema_version": 1, "timezone": z}) for z in (
                None, 1, True, [], {}, "", " ", " UTC", "UTC ", "No/Such_Zone", "x" * 129)]
            invalid += ["{", "[]", "null", "true", "{}", '{"schema_version":true}',
                        '{"schema_version":1.0}', '{"schema_version":2}',
                        '{"schema_version":1,"timezone":"UTC","timezone":"UTC"}',
                        '{"schema_version":1,"extra":{"x":1,"x":2}}',
                        '{"schema_version":1,"schema_version":1}', " " * (64 * 1024 + 1)]
            for raw in invalid:
                with self.subTest(raw=raw[:160]):
                    config.write_text(raw)
                    before = (config.read_bytes(), config.stat().st_mode)
                    with self.assertRaisesRegex(ValueError, "^campaign_timezone_invalid$"):
                        cc.load_timezone(root)
                    self.assertEqual((config.read_bytes(), config.stat().st_mode), before)
            raw = b'{"schema_version":1,"timezone":"UTC"}'
            config.write_bytes(raw + b" " * (64 * 1024 - len(raw)))
            self.assertEqual(cc.load_timezone(root), "UTC")
            config.write_bytes(b"\xff")
            with self.assertRaisesRegex(ValueError, "^campaign_timezone_invalid$"):
                cc.load_timezone(root)

    def test_unsafe_unreadable_and_nonregular_without_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "campaign.json"
            target = root / "target.json"
            target.write_text('{"schema_version":1,"timezone":"UTC"}')
            before = (target.read_bytes(), target.stat().st_mode)
            config.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "^campaign_timezone_invalid$"):
                cc.load_timezone(root)
            self.assertTrue(config.is_symlink())
            self.assertEqual((target.read_bytes(), target.stat().st_mode), before)
            config.unlink()
            os.mkfifo(config)
            mode = config.lstat().st_mode
            with self.assertRaisesRegex(ValueError, "^campaign_timezone_invalid$"):
                cc.load_timezone(root)
            self.assertEqual(config.lstat().st_mode, mode)
            config.unlink()
            config.mkdir()
            with self.assertRaisesRegex(ValueError, "^campaign_timezone_invalid$"):
                cc.load_timezone(root)
            config.rmdir()
            config.write_bytes(target.read_bytes())
            with patch.object(cc.os, "open", side_effect=PermissionError("synthetic")):
                with self.assertRaisesRegex(ValueError, "^campaign_timezone_invalid$"):
                    cc.load_timezone(root)
            self.assertEqual(config.read_bytes(), target.read_bytes())
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["campaign.json", "target.json"])

    def test_project_boundary_no_ancestor_or_sibling_lookup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "campaign.json").write_text('{"schema_version":1,"timezone":"UTC"}')
            project = root / "records"
            project.mkdir()
            self.assertEqual(cc.load_timezone(project), cc.DEFAULT_TIMEZONE)
            link = root / "linked"
            link.symlink_to(project, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "^campaign_timezone_invalid$"):
                cc.load_timezone(link)
            with self.assertRaisesRegex(ValueError, "^campaign_timezone_invalid$"):
                cc.load_timezone(root / "campaign.json")
            self.assertEqual(list(project.iterdir()), [])
