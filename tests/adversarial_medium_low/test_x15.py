"""X15 repro copied from adversarial source d0264ab; always enabled."""

import json

from pathlib import Path

import random

import shutil

import tempfile

import unittest

from campaign_tool.records import native_extract, ocr_stage

from campaign_tool.records.intake import rfc822_adapter, rfc822_inventory

from tests.adversarial_medium_low._support import call

class OutcomeFileNames(unittest.TestCase):
    """C1: two different run ids can map to the same outcome file, so the later run replaces the earlier one."""

    def test_safe_name_compatibility_and_unsafe_identity_restart(self):
        root = Path(tempfile.mkdtemp(prefix="adv-extract-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "runs").mkdir()
        ids = ("run-t11k", "", "run/1", "run1", "../run1", "run\n1", "é", "x" * 300)
        for run_id in ids:
            native_extract.write_private_list(root, run_id, [{"outcome": "EXTRACTED", "marker": run_id}])
        paths = list((root / "runs").glob("extract-*.json"))
        self.assertEqual(len(paths), len(ids))
        self.assertTrue((root / "runs" / "extract-run-t11k.json").exists())
        self.assertEqual({json.loads(p.read_text())["run_id"] for p in paths}, set(ids))
        native_extract.write_private_list(root, "run/1", [{"outcome": "CORRUPT_DOCUMENT"}])
        self.assertEqual(len(list((root / "runs").glob("extract-*.json"))), len(ids))
        rows = [json.loads(p.read_text()) for p in paths]
        self.assertEqual(next(r for r in rows if r["run_id"] == "run/1")["documents"], [{"outcome": "CORRUPT_DOCUMENT"}])
        self.assertEqual(next(r for r in rows if r["run_id"] == "run1")["documents"][0]["outcome"], "EXTRACTED")

    def test_distinct_run_ids_get_distinct_outcome_files(self):
        root = Path(tempfile.mkdtemp(prefix="adv-extract-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "runs").mkdir()
        native_extract.write_private_list(root, "2026-10-22/run-1", [{"original_sha256": "a" * 64, "outcome": "EXTRACTED"}])
        native_extract.write_private_list(root, "2026-10-22run-1", [{"original_sha256": "b" * 64, "outcome": "CORRUPT_DOCUMENT"}])
        self.assertEqual(len(list((root / "runs").glob("extract-*.json"))), 2,
                         "the second run's outcome list silently replaced the first run's")
