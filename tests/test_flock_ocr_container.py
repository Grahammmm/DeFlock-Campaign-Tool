"""Static checks for the pinned OCR container recipe (no Docker, no network)."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECIPE = ROOT / "container" / "flock-ocr"
DOCKERFILE = (RECIPE / "Dockerfile").read_text()
LOCK = (RECIPE / "requirements.lock").read_text()

REQUIRED_APT = {"tesseract-ocr", "tesseract-ocr-eng", "poppler-utils", "qpdf", "ocrmypdf"}


def apt_install_specs():
    match = re.search(r"apt-get install -y --no-install-recommends(.*?)&& apt-mark hold", DOCKERFILE, re.S)
    if match is None:
        raise AssertionError("apt-get install block not found")
    return [token for token in match.group(1).replace("\\", " ").split() if token]


def pins(path):
    found = {}
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if "==" in line:
            name, version = line.split()[0].split("==")
            found[name.lower().replace("_", "-")] = version
    return found


class FlockOcrContainerRecipeTests(unittest.TestCase):
    def test_every_apt_package_is_pinned_exactly(self):
        specs = apt_install_specs()
        self.assertTrue(specs)
        names = set()
        for spec in specs:
            self.assertRegex(spec, r"^[a-z0-9][a-z0-9.+-]*=[0-9][A-Za-z0-9.+:~-]*$", spec)
            names.add(spec.split("=", 1)[0])
        self.assertLessEqual(REQUIRED_APT, names)
        self.assertEqual(len(names), len(specs), "duplicate apt package")

    def test_qpdf_cli_and_library_share_a_version(self):
        versions = dict(spec.split("=", 1) for spec in apt_install_specs())
        self.assertEqual(versions["qpdf"], versions["libqpdf29"])

    def test_python_lock_is_hashed_and_matches_repository_pins(self):
        entries = re.findall(r"^([A-Za-z0-9_.-]+)==(\S+) \\\n\s+--hash=sha256:([0-9a-f]{64})$", LOCK, re.M)
        requirement_lines = [line for line in LOCK.splitlines() if "==" in line and not line.startswith("#")]
        self.assertEqual(len(entries), len(requirement_lines))
        locked = {name.lower().replace("_", "-"): version for name, version, _ in entries}
        runner = pins(ROOT / "requirements-runner.txt")
        self.assertEqual(locked, runner)
        records = pins(ROOT / "requirements-records-test.txt")
        for name, version in locked.items():
            self.assertEqual(records.get(name), version, name)

    def test_build_is_fail_closed_and_bound_to_inputs(self):
        self.assertRegex(DOCKERFILE, r"(?m)^ARG BASE_IMAGE$")  # no default base
        self.assertIn("FROM ${BASE_IMAGE}", DOCKERFILE)
        for argument in ("ENGINE_COMMIT", "WHEEL_SHA256"):
            self.assertRegex(DOCKERFILE, rf"(?m)^ARG {argument}$")
        self.assertIn("sha256sum -c -", DOCKERFILE)
        self.assertIn("--require-hashes --only-binary=:all:", DOCKERFILE)
        self.assertIn("pip install --no-deps --no-index", DOCKERFILE)
        self.assertIn('r["commit"]==sys.argv[1]', DOCKERFILE)
        for smoke in ("ocrmypdf --version", "pdftotext -v", "qpdf --version",
                      "campaign_tool.records.extract.ocr --doctor"):
            self.assertIn(smoke, DOCKERFILE)

    def test_recipe_does_not_change_runtime_identity_or_carry_secrets(self):
        for directive in ("USER", "ENTRYPOINT", "CMD", "VOLUME", "EXPOSE"):
            self.assertNotRegex(DOCKERFILE, rf"(?m)^{directive}\b", directive)
        self.assertNotRegex(DOCKERFILE, r"(?im)^(ENV|ARG)\s+\S*(TOKEN|SECRET|PASSWORD|KEY)\b")
        self.assertNotRegex(DOCKERFILE, r"(?m)^ADD\b")
        self.assertNotIn("curl", DOCKERFILE)
        self.assertNotIn("wget", DOCKERFILE)

    def test_build_context_only_admits_lock_and_wheel(self):
        lines = (RECIPE / ".dockerignore").read_text().splitlines()
        rules = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
        self.assertEqual(rules[0], "*")
        self.assertEqual(sorted(rules[1:]), sorted(["!requirements.lock", "!dist/",
                                                    "!dist/deflock_campaign_tool-*-py3-none-any.whl"]))


if __name__ == "__main__":
    unittest.main()
