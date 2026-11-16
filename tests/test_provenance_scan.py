"""Synthetic adversarial cases for narrow provenance-only scan approvals."""
import copy
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from tools.check_secret_report import (APPROVED_MANIFESTS, APPROVED_PIN_FILES, PIN_FIELDS,
                                       verified_pin_hit, verified_provenance_hit)


class ProvenanceScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.code = self.root / "module.py"
        self.code.write_text("# synthetic public implementation\n")
        self.manifest_name = "docs/records-intake-import.json"
        self.path = self.root / self.manifest_name
        self.path.parent.mkdir()
        self.data = {"files": [{"engine_path": "module.py",
            "engine_sha256": hashlib.sha256(self.code.read_bytes()).hexdigest(),
            "source_sha256": hashlib.sha256(b"reviewed synthetic predecessor").hexdigest()}]}
        self.path.write_text(json.dumps(self.data, indent=2) + "\n")
        self.approved = {self.manifest_name: hashlib.sha256(self.path.read_bytes()).hexdigest()}
        self.hit = self.hit_for("engine_sha256")

    def hit_for(self, field):
        value = self.data["files"][0][field]
        lines = self.path.read_text().splitlines()
        return {"type": "Hex High Entropy String",
                "line_number": next(i for i, line in enumerate(lines, 1) if '"' + field + '"' in line),
                "hashed_secret": hashlib.sha1(value.encode()).hexdigest()}

    def allowed(self, hit=None, path=None):
        return verified_provenance_hit(self.root, path or self.manifest_name,
                                       self.hit if hit is None else hit, self.approved)

    def test_exact_reviewed_engine_and_source_hashes_pass(self):
        self.assertTrue(self.allowed())
        self.assertTrue(self.allowed(self.hit_for("source_sha256")))

    def test_changed_engine_bytes_fail(self):
        self.code.write_text("changed code")
        self.assertFalse(self.allowed())

    def test_modified_manifest_hash_fails_even_if_scanner_digest_matches(self):
        self.data["files"][0]["source_sha256"] = hashlib.sha256(b"unapproved replacement").hexdigest()
        self.path.write_text(json.dumps(self.data, indent=2) + "\n")
        self.assertFalse(self.allowed(self.hit_for("source_sha256")))

    def test_added_credential_invalidates_entire_manifest_approval(self):
        self.data["api_key"] = "synthetic-credential-" + "example" * 8  # pragma: allowlist secret
        self.path.write_text(json.dumps(self.data, indent=2) + "\n")
        self.assertFalse(self.allowed())

    def test_no_other_file_is_exempt(self):
        self.assertFalse(self.allowed(path="docs/another.json"))
        self.assertFalse(self.allowed(path="./" + self.manifest_name))

    def test_other_detector_is_never_exempt(self):
        hit = {**self.hit, "type": "Secret Keyword"}
        self.assertFalse(self.allowed(hit))

    def test_hit_must_match_exact_line_and_exact_scanned_value(self):
        for change in ({"line_number": 1}, {"line_number": 0}, {"line_number": True},
                       {"line_number": 1000}, {"hashed_secret": "wrong"}):  # pragma: allowlist secret
            with self.subTest(change=change):
                self.assertFalse(self.allowed({**self.hit, **change}))

    def test_code_symlink_fails(self):
        replacement = self.root / "other.py"
        self.code.rename(replacement)
        self.code.symlink_to(replacement)
        self.assertFalse(self.allowed())

    def test_manifest_symlink_fails(self):
        replacement = self.root / "other.json"
        self.path.rename(replacement)
        self.path.symlink_to(replacement)
        self.assertFalse(self.allowed())

    def test_missing_code_fails(self):
        self.code.unlink()
        self.assertFalse(self.allowed())

    def test_actual_approved_manifests_match_code_and_cli_still_blocks_credentials(self):
        root = Path(__file__).resolve().parents[1]
        results = {}
        for name, pin in APPROVED_MANIFESTS.items():
            raw = (root / name).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), pin)
            entries = []
            for number, line in enumerate(raw.decode().splitlines(), 1):
                from tools.check_secret_report import HASH_LINE
                match = HASH_LINE.fullmatch(line)
                if match:
                    hit = {"type": "Hex High Entropy String", "line_number": number,
                           "hashed_secret": hashlib.sha1(match[1].encode()).hexdigest()}
                    self.assertTrue(verified_provenance_hit(root, name, hit))
                    entries.append(hit)
            self.assertTrue(entries)
            results[name] = entries
        report = self.root / "report.json"
        report.write_text(json.dumps({"results": results}))
        cmd = [sys.executable, str(root / "tools/check_secret_report.py"), str(report)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        marker = "not-for-output-synthetic-marker"
        results["module.py"] = [{"type": "Secret Keyword", "line_number": 1, "hashed_secret": marker}]
        report.write_text(json.dumps({"results": results}))
        result = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn(marker, result.stdout + result.stderr)


class PinScanTests(unittest.TestCase):
    """Synthetic adversarial cases for the reviewed build-pin approval."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.name = "container/flock-ocr/pins.json"
        self.path = self.root / self.name
        self.path.parent.mkdir(parents=True)
        self.data = {"engine": {
            "commit": hashlib.sha1(b"synthetic commit").hexdigest(),
            "tree": hashlib.sha1(b"synthetic tree").hexdigest(),
            "manifest_sha256": hashlib.sha256(b"synthetic manifest").hexdigest(),
            "dependency_lock_sha256": hashlib.sha256(b"synthetic lock").hexdigest()}}
        self.write()

    def write(self):
        self.path.write_text(json.dumps(self.data, indent=2) + "\n")
        self.approved = {self.name: hashlib.sha256(self.path.read_bytes()).hexdigest()}

    def hit_for(self, field, value=None):
        lines = self.path.read_text().splitlines()
        value = self.data["engine"][field] if value is None else value
        return {"type": "Hex High Entropy String",
                "line_number": next(i for i, line in enumerate(lines, 1) if '"' + field + '"' in line),
                "hashed_secret": hashlib.sha1(value.encode()).hexdigest()}

    def allowed(self, hit, path=None):
        return verified_pin_hit(self.root, path or self.name, hit, self.approved)

    def test_exact_reviewed_pins_pass(self):
        for field in PIN_FIELDS:
            with self.subTest(field=field):
                self.assertTrue(self.allowed(self.hit_for(field)))

    def test_changed_file_bytes_fail_even_for_unchanged_line(self):
        hit = self.hit_for("commit")
        self.data["engine"]["note"] = "x"
        self.path.write_text(json.dumps(self.data, indent=2) + "\n")
        self.assertFalse(self.allowed(hit))

    def test_unlisted_field_or_wrong_length_fails(self):
        self.data["engine"]["api_token"] = hashlib.sha256(b"synthetic").hexdigest()
        self.data["engine"]["commit"] = hashlib.sha256(b"too long for a commit").hexdigest()
        self.write()
        self.assertFalse(self.allowed(self.hit_for("api_token")))
        self.assertFalse(self.allowed(self.hit_for("commit")))

    def test_field_outside_engine_or_duplicated_fails(self):
        self.data = {"engine": {}, "other": {"commit": hashlib.sha1(b"x").hexdigest()}}
        self.write()
        self.assertFalse(self.allowed({**self.hit_for("commit", self.data["other"]["commit"])}))
        self.data = {"engine": {"commit": hashlib.sha1(b"x").hexdigest()},
                     "other": {"commit": hashlib.sha1(b"x").hexdigest()}}
        self.write()
        self.assertFalse(self.allowed(self.hit_for("commit")))

    def test_wrong_line_value_detector_path_or_symlink_fails(self):
        hit = self.hit_for("tree")
        for change in ({"line_number": 1}, {"line_number": 0}, {"line_number": True}, {"line_number": 999},
                       {"hashed_secret": "wrong"}, {"type": "Secret Keyword"}):  # pragma: allowlist secret
            with self.subTest(change=change):
                self.assertFalse(self.allowed({**hit, **change}))
        self.assertFalse(self.allowed(hit, path="container/other/pins.json"))
        self.assertFalse(verified_provenance_hit(self.root, self.name, hit, self.approved))
        replacement = self.root / "other.json"
        self.path.rename(replacement)
        self.path.symlink_to(replacement)
        self.assertFalse(self.allowed(hit))

    def test_actual_pins_accept_only_identity_lines_and_cli_passes(self):
        root = Path(__file__).resolve().parents[1]
        results = {}
        for name, pin in APPROVED_PIN_FILES.items():
            raw = (root / name).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), pin)
            entries = []
            for number, line in enumerate(raw.decode().splitlines(), 1):
                for value in re.findall(r"[a-f0-9]{40,64}", line):
                    hit = {"type": "Hex High Entropy String", "line_number": number,
                           "hashed_secret": hashlib.sha1(value.encode()).hexdigest()}
                    self.assertTrue(verified_pin_hit(root, name, hit), (name, number))
                    entries.append(hit)
            self.assertEqual(len(entries), 4)
            results[name] = entries
            stray = {"type": "Hex High Entropy String", "line_number": 1,
                     "hashed_secret": entries[0]["hashed_secret"]}
            self.assertFalse(verified_pin_hit(root, name, stray))
        report = self.root / "report.json"
        report.write_text(json.dumps({"results": results}))
        cmd = [sys.executable, str(root / "tools/check_secret_report.py"), str(report)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("potential secrets: 0", result.stdout)


ROOT = Path(__file__).resolve().parents[1]
ANCHOR = "import-manifest-pin-refresh-checklist"
MANIFESTS = ("docs/records-intake-import.json", "docs/records-gates-import.json")


def guidance_rows(text):
    section = text.split("## Import-manifest pin refresh checklist", 1)[-1]
    return dict(re.findall(r"^\| `([^`]+)` \| `([^`]+)` \|$", section, re.M))


def guidance_problems(text, expected):
    problems = []
    if "## Import-manifest pin refresh checklist" not in text:
        problems.append("heading")
    if guidance_rows(text) != expected:
        problems.append("mapping")
    for token in ("engine_sha256", "APPROVED_MANIFESTS", "tools/check_secret_report.py",
                  "entire manifest", "final bytes", "same PR", "source_sha256",
                  "weaken the scanner", "not publication clearance"):
        if token not in text:
            problems.append(token)
    if text.find("engine_sha256") > text.find("entire manifest") >= 0:
        problems.append("order")
    return problems


def template_problems(text, agents):
    problems = []
    match = re.search(r"\]\(\.\./AGENTS\.md#([^)]+)\)", text)
    if not match or match.group(1) != ANCHOR or "## Import-manifest pin refresh checklist" not in agents:
        problems.append("link")
    for token in ("engine_sha256", "APPROVED_MANIFESTS", "tools/check_secret_report.py",
                  "not applicable"):
        if token not in text:
            problems.append(token)
    return problems


class ImportPinGuidanceTests(unittest.TestCase):
    def setUp(self):
        self.agents = (ROOT / "AGENTS.md").read_text()
        self.template = (ROOT / ".github/pull_request_template.md").read_text()
        self.expected = {}
        for name in MANIFESTS:
            for entry in json.loads((ROOT / name).read_text())["files"]:
                self.expected[entry["engine_path"]] = name

    def test_guidance_maps_every_manifest_engine_path(self):
        self.assertEqual(len(self.expected), 9)
        self.assertEqual(guidance_rows(self.agents), self.expected)
        self.assertEqual(guidance_problems(self.agents, self.expected), [])

    def test_guidance_rejects_missing_or_misassigned_path(self):
        for path, manifest in self.expected.items():
            row = "| `%s` | `%s` |\n" % (path, manifest)
            self.assertIn(row, self.agents)
            self.assertIn("mapping", guidance_problems(self.agents.replace(row, ""), self.expected))
            other = MANIFESTS[1] if manifest == MANIFESTS[0] else MANIFESTS[0]
            moved = self.agents.replace(row, "| `%s` | `%s` |\n" % (path, other))
            self.assertIn("mapping", guidance_problems(moved, self.expected))

    def test_guidance_requires_both_hash_layers_in_order(self):
        for token in ("engine_sha256", "entire manifest", "final bytes", "same PR"):
            problems = guidance_problems(self.agents.replace(token, "xx"), self.expected)
            self.assertTrue(problems, token)
        swapped = self.agents.replace("engine_sha256", "source_sha256", 1)
        self.assertTrue(guidance_problems(swapped, self.expected) or
                        "engine_sha256" not in swapped.split("Order, all")[1].split("2.")[0])

    def test_template_links_to_real_refresh_anchor_and_requires_evidence(self):
        self.assertEqual(template_problems(self.template, self.agents), [])
        self.assertIn("link", template_problems(self.template.replace(ANCHOR, "nope"), self.agents))
        self.assertIn("link", template_problems(self.template, "# nothing"))
        for token in ("APPROVED_MANIFESTS", "not applicable"):
            self.assertIn(token, template_problems(self.template.replace(token, "xx"), self.agents))

    def test_guidance_preserves_scanner_and_historical_provenance(self):
        low = self.agents.lower()
        self.assertIn("blanket allowlist", low)
        self.assertIn("weaken the scanner", low)
        self.assertIn("historical `source_sha256` and `schema_sha256`", self.agents)
        self.assertIn("not publication clearance", low)
        self.assertNotIn("--no-verify", self.agents)


if __name__ == "__main__":
    unittest.main()
