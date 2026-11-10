"""Synthetic admission checks for an existing campaign.json in guided setup; offline."""
import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from campaign_tool import cli

FIXED = ("campaign.json could not be read as a supported campaign configuration; "
         "nothing changed. Repair it and run setup again.")
SENTINEL = "private-sentinel-7f3a"
FLAGS = ["--name", "Synthetic Campaign", "--county", "San Luis Obispo", "--state", "CA", "--yes"]
DOWNSTREAM = ("campaign_tool.cli.initialize", "campaign_tool.kit.build_kit",
              "campaign_tool.site.build_site", "campaign_tool.cli.doctor_report")


def invoke(root, flags=FLAGS):
    stdout, stderr = io.StringIO(), io.StringIO()
    argv = ["campaign_tool", "setup", "--directory", str(root)] + list(flags)
    with patch("sys.argv", argv), patch("sys.stdin", io.StringIO()), redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            cli.main()
            code = 0
        except SystemExit as exc:
            code = exc.code
    return code, stdout.getvalue(), stderr.getvalue()


def snapshot(root):
    result = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        data = path.read_bytes() if path.is_file() else None
        result[str(path.relative_to(root))] = (path.is_dir(), data, info.st_mode, info.st_mtime_ns)
    return result


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "campaign"
        self.root.mkdir()
        reference = self.base / "reference"
        self.assertEqual(invoke(reference)[0], 0)
        self.valid = (reference / "campaign.json").read_bytes()

    def refuse(self, data=None, root=None):
        """Run setup with every downstream step mocked and a forbidden network; check the refusal."""
        root = root or self.root
        if data is not None:
            (root / "campaign.json").write_bytes(data)
        before = snapshot(root)
        with ExitStack() as stack:
            mocks = [stack.enter_context(patch(name)) for name in DOWNSTREAM]
            stack.enter_context(patch("socket.socket", side_effect=AssertionError("network forbidden")))
            code, out, err = invoke(root)
        self.assertEqual(code, 1, err)
        self.assertEqual([line for line in out.splitlines() if line.startswith("Step ")],
                         ["Step 1 of 4: Campaign file … stopped"])
        self.assertIn("Stopped at step 1 (Campaign file): " + FIXED, err)
        self.assertNotIn("Traceback", err)
        self.assertNotIn("kept", out)
        self.assertNotIn("Setup finished", out)
        for mock in mocks:
            mock.assert_not_called()
        self.assertEqual(snapshot(root), before)
        return out + err

    def test_non_object_configuration_stops_at_step_one(self):
        for name, data in (("list", b"[]"), ("null", b"null"), ("true", b"true"), ("integer", b"7"),
                           ("string", b'"text"'), ("nested", b"[" * 1500 + b"]" * 1500)):
            with self.subTest(name):
                self.refuse(data)

    def test_schema_and_identity_shapes_are_validated_before_kept(self):
        base = json.loads(self.valid)
        cases = {"no schema": ("schema_version", None), "schema 99": ("schema_version", 99),
                 "no name": ("name", None), "blank name": ("name", "  "), "number name": ("name", 5),
                 "object name": ("name", {"leak": SENTINEL}), "no county": ("county", None),
                 "blank county": ("county", ""), "list county": ("county", [SENTINEL]),
                 "no state": ("state", None), "number state": ("state", 12),
                 "lowercase state": ("state", "ca"), "long state": ("state", "Calif")}
        for label, (key, value) in cases.items():
            with self.subTest(label):
                cfg = dict(base)
                if value is None:
                    cfg.pop(key)
                else:
                    cfg[key] = value
                shown = self.refuse(json.dumps(cfg).encode())
                self.assertNotIn(SENTINEL, shown)
                self.assertNotIn("leak", shown)

    def test_bad_encoding_syntax_and_parser_depth_are_fixed_refusals(self):
        for label, data in (("encoding", b"\xff\xfe{"), ("syntax", b"{not json " + SENTINEL.encode())):
            with self.subTest(label):
                self.assertNotIn(SENTINEL, self.refuse(data))
        (self.root / "campaign.json").write_bytes(self.valid)
        with patch("campaign_tool.cli.read_config", side_effect=RecursionError(SENTINEL)):
            self.assertNotIn(SENTINEL, self.refuse())

    def test_unreadable_configuration_does_not_initialize_or_continue(self):
        (self.root / "campaign.json").write_bytes(self.valid)
        os.chmod(self.root / "campaign.json", 0o640)
        error = PermissionError(13, "denied", "/" + SENTINEL + "/campaign.json")
        with patch("campaign_tool.cli.read_config", side_effect=error):
            shown = self.refuse()
        self.assertNotIn(SENTINEL, shown)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["campaign.json"])

    def test_refusal_preserves_existing_campaign_tree(self):
        (self.root / "kit/requests").mkdir(parents=True)
        (self.root / "kit/requests/edited.md").write_text("Organizer edited draft.\n")
        (self.root / "public").mkdir()
        (self.root / "public/keep.html").write_text("public sentinel\n")
        (self.root / "private").mkdir(mode=0o750)
        (self.root / "private/note.txt").write_text("unrelated sentinel\n")
        self.refuse(b'{"schema_version": 99}')
        self.assertFalse((self.root / "kit/requests.suggested").exists())

    def test_valid_identity_and_optional_fields_keep_original_bytes(self):
        cfg = json.loads(self.valid)
        cfg["location_query"] = "Morro Bay"
        cfg["future_optional_key"] = {"nested": [1, 2]}
        data = json.dumps(cfg, indent=4).encode()
        (self.root / "campaign.json").write_bytes(data)
        self.assertEqual(invoke(self.root)[0], 0)
        data = (self.root / "campaign.json").read_bytes()  # later steps may normalize it once
        self.assertIn(b"future_optional_key", data)
        draft = next((self.root / "kit/requests").glob("*.md"))
        draft.write_text("Organizer edited draft.\n")
        code, out, err = invoke(self.root, FLAGS[:5] + ["ca", "--yes"])
        self.assertEqual(code, 0, err)
        self.assertIn("Step 1 of 4: Campaign file … kept", out)
        self.assertEqual((self.root / "campaign.json").read_bytes(), data)
        self.assertEqual(draft.read_text(), "Organizer edited draft.\n")
        self.assertTrue(out.rstrip().endswith("sending is always your decision."))
        before = snapshot(self.root)
        for field, value in (("--county", "Monterey"), ("--name", "Other"), ("--state", "OR")):
            flags = list(FLAGS)
            flags[flags.index(field) + 1] = value
            code, out, err = invoke(self.root, flags)
            self.assertEqual(code, 1)
            self.assertIn("already describes", err)
            self.assertNotIn("Step 2", out)
            self.assertEqual(snapshot(self.root), before)

    def test_repair_then_rerun_completes_without_reset(self):
        bad = b'{"schema_version": 99}'
        self.refuse(bad)
        self.assertEqual((self.root / "campaign.json").read_bytes(), bad)
        (self.root / "campaign.json").write_bytes(self.valid)
        code, out, err = invoke(self.root)
        self.assertEqual(code, 0, err)
        self.assertEqual([line for line in out.splitlines() if line.startswith("Step ")],
                         ["Step 1 of 4: Campaign file … kept", "Step 2 of 4: Request drafts … done",
                          "Step 3 of 4: Starter site … done", "Step 4 of 4: Readiness … done"])
        self.assertNotIn("Stopped", err)
        self.assertEqual((self.root / "campaign.json").read_bytes(), self.valid)


if __name__ == "__main__":
    unittest.main()
