"""Synthetic config contract: no campaign originals or settings are read."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records import campaign_config as cc
from campaign_tool.records import console_board as cb
from campaign_tool.records import public_packets as pp
from tests.records import test_public_packets as tp


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


FICTIONAL = {"schema_version": 1, "campaign_id": "cedar-civic", "display_name": "Cedar Civic Records",
             "law_package": "us-ca", "public_base_url": "https://records.cedar.example",
             "records_mailbox_label": "Cedar records inbox", "timezone": "UTC"}
SLO_TEXT = ("deflockslo", "San Luis Obispo", "SLO")


class CampaignFieldsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, doc):
        (self.root / "campaign.json").write_text(doc if isinstance(doc, str) else json.dumps(doc))

    def test_fictional_config_loads_and_missing_file_is_neutral(self):
        self.assertEqual(cc.load(self.root), cc.CampaignConfig())
        self.assertEqual(cc.CampaignConfig().display_name, "Records project")
        self.write(FICTIONAL)
        got = cc.load(self.root)
        self.assertEqual((got.campaign_id, got.display_name, got.law_package, got.public_base_url,
                          got.records_mailbox_label, got.timezone),
                         ("cedar-civic", "Cedar Civic Records", "us-ca", "https://records.cedar.example",
                          "Cedar records inbox", "UTC"))
        self.write({"schema_version": 1})
        self.assertEqual(cc.load(self.root), cc.CampaignConfig())

    def test_each_invalid_field_and_unknown_key_refuses(self):
        bad = {"campaign_id": ["A", "x", "a_b", "x" * 41, 1], "display_name": ["", " x", "x" * 81, 1, "a\nb"],
               "law_package": ["US-CA", "usca", "us-cal", 1],
               "public_base_url": ["http://a.example", "https://a.example/p", "https://a.example/?q=1", "https://",
                                   "https://u@a.example", "a.example", 1],
               "records_mailbox_label": ["", "x" * 81, 1], "timezone": ["No/Such_Zone", 1]}
        cases = [dict(FICTIONAL, **{k: v}) for k, vs in bad.items() for v in vs]
        cases.append(dict(FICTIONAL, surprise=1))
        for doc in cases:
            with self.subTest(doc=str(doc)[:120]):
                self.write(doc)
                with self.assertRaisesRegex(ValueError, "^campaign_config_invalid$"):
                    cc.load(self.root)
        self.write('{"schema_version":1,"campaign_id":"ab","campaign_id":"ab"}')
        with self.assertRaisesRegex(ValueError, "^campaign_config_invalid$"):
            cc.load(self.root)

    def test_load_timezone_still_ignores_unknown_keys(self):
        self.write(dict(FICTIONAL, surprise=1))
        self.assertEqual(cc.load_timezone(self.root), "UTC")

    def test_board_shows_campaign_names_and_no_slo_text(self):
        from tests.records import test_console_board as tb
        tb.build(self.root)
        plain = cb.render_board(cb.load(self.root, tb.RUN), tb.TODAY)
        plain_agency = cb.render_agency(cb.load(self.root, tb.RUN), "alpha-pd")
        self.write({"schema_version": 1})  # unconfigured: output identical to no file
        self.assertEqual(cb.render_board(cb.load(self.root, tb.RUN), tb.TODAY), plain)
        self.write(dict(FICTIONAL, law_package="us-ny"))
        model = cb.load(self.root, tb.RUN)
        page = cb.render_board(model, tb.TODAY) + cb.render_agency(model, "alpha-pd")
        self.assertIn("Cedar Civic Records", page)
        self.assertIn("Cedar records inbox", page)
        self.assertIn("law package (us-ny)", page)
        for text in SLO_TEXT:
            self.assertNotIn(text, page)
            self.assertNotIn(text, plain + plain_agency)
        self.write(dict(FICTIONAL, law_package="bad"))
        with self.assertRaisesRegex(ValueError, "^campaign_config_invalid$"):
            cb.load(self.root, tb.RUN)


class CampaignPacketTests(tp.Base):
    def change(self):
        ident = self.approved()
        self.turn_on()
        got = pp.write_change(self.project, ident, now=tp.T0 + 60)
        with open(os.path.join(self.project, got["file"])) as f:
            return json.load(f)

    def test_change_file_carries_campaign_names_without_slo_text(self):
        plain = self.change()
        self.assertNotIn("campaign", plain)
        Path(self.project, "campaign.json").write_text(json.dumps(FICTIONAL))
        ident = self.approved(tp.doc(source="doc-9"))
        got = pp.write_change(self.project, ident, now=tp.T0 + 60)
        raw = Path(self.project, got["file"]).read_text()
        self.assertEqual(json.loads(raw)["campaign"], {
            "campaign_id": "cedar-civic", "display_name": "Cedar Civic Records", "law_package": "us-ca",
            "public_base_url": "https://records.cedar.example"})
        for text in SLO_TEXT:
            self.assertNotIn(text, raw)

    def test_invalid_campaign_file_refuses_the_change(self):
        ident = self.approved()
        self.turn_on()
        Path(self.project, "campaign.json").write_text(json.dumps(dict(FICTIONAL, law_package="bad")))
        self.assertEqual(self.code(pp.write_change, self.project, ident, now=tp.T0 + 60), "campaign_config_invalid")
        self.assertFalse(Path(self.project, pp.OUT).exists() and any(Path(self.project, pp.OUT).iterdir()))
