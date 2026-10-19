"""Static checks for the pinned OCR container recipe (no Docker, no network)."""
import argparse
import contextlib
import hashlib
import importlib.util
import io
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECIPE = ROOT / "container" / "flock-ocr"
_SPEC = importlib.util.spec_from_file_location("flock_ocr_preflight", RECIPE / "preflight.py")
preflight = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(preflight)
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

    def test_dockerignore_denies_by_default(self):
        lines = (RECIPE / ".dockerignore").read_text().splitlines()
        rules = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
        self.assertEqual(rules[0], "*")
        self.assertNotIn("!dist/", rules)
        self.assertNotIn("!dist", rules)

    def test_copy_is_narrowed_to_the_wheel_and_dist_is_checked(self):
        self.assertNotRegex(DOCKERFILE, r"(?m)^COPY dist/\s")
        self.assertIn("COPY dist/deflock_campaign_tool-*-py3-none-any.whl /opt/campaign-tool/dist/", DOCKERFILE)
        self.assertIn('test "$(find . -mindepth 1 | wc -l)" -eq 1', DOCKERFILE)
        self.assertIn("-type f -name 'deflock_campaign_tool-*-py3-none-any.whl'", DOCKERFILE)

    def test_base_image_must_be_immutable_in_dockerfile(self):
        self.assertRegex(DOCKERFILE, r"(?ms)^FROM \$\{BASE_IMAGE\}\nARG BASE_IMAGE$")
        match = re.search(r"\"\$\{BASE_IMAGE\}\" \| grep -Eqx '([^']+)'", DOCKERFILE)
        self.assertIsNotNone(match)
        regex = re.compile(match.group(1))
        for good in ("sha256:" + "a" * 64, "example/base@sha256:" + "0" * 64,
                     "registry.example:5000/base:1@sha256:" + "f" * 64):
            self.assertTrue(regex.fullmatch(good), good)
        for bad in MUTABLE_REFS:
            self.assertFalse(regex.fullmatch(bad), bad)


SHA = "0123456789abcdef" * 4
WHEEL = "deflock_campaign_tool-0.1.0-py3-none-any.whl"
MUTABLE_REFS = ("example/base:latest", "example/base", "example/base:20260912",
                "sha256:" + "a" * 63, "sha256:" + "A" * 64, "example/base@sha256:abc",
                "example/base:tag@sha256:" + "g" * 64, "")


def _make_context(root, extras=(), wheel_bytes=b"synthetic wheel"):
    """Synthetic build context: the recipe's real .dockerignore plus fake files."""
    root = Path(root)
    (root / ".dockerignore").write_text((RECIPE / ".dockerignore").read_text())
    (root / "Dockerfile").write_text("synthetic\n")
    (root / "requirements.lock").write_text("synthetic\n")
    (root / "dist").mkdir()
    (root / "dist" / WHEEL).write_bytes(wheel_bytes)
    for rel in extras:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic extra\n")
    return root


class FlockOcrBuildContextTests(unittest.TestCase):
    """Behavioral checks of the .dockerignore semantics (Docker's matcher, ported)."""

    EXTRAS = ("dist/private.txt", "dist/nested/receipt.json",
              "dist/deflock_campaign_tool-9.9.9-py3-none-any.whl/hidden.txt",
              "requirements.lock.bak", "notes/receipt.json", ".git/config", ".env")

    def sent(self, root, ignore_text=None):
        root = Path(root)
        if ignore_text is not None:
            (root / ".dockerignore").write_text(ignore_text)
        return preflight.context_files(root, preflight.read_dockerignore(root / ".dockerignore"))

    def test_matcher_reproduces_the_reviewed_parent_dir_leak(self):
        old = "*\n!requirements.lock\n!dist/\n!dist/deflock_campaign_tool-*-py3-none-any.whl\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_context(tmp, extras=self.EXTRAS[:2])
            sent = self.sent(root, old)
        self.assertIn("dist/private.txt", sent)
        self.assertIn("dist/nested/receipt.json", sent)

    def test_context_sends_only_lock_and_top_level_wheels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_context(tmp, extras=self.EXTRAS)
            self.assertEqual(self.sent(root), ["dist/" + WHEEL, "requirements.lock"])

    def test_extra_wheel_reaches_context_but_preflight_rejects_it(self):
        extra = "dist/deflock_campaign_tool-0.2.0-py3-none-any.whl"
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_context(tmp, extras=(extra,))
            self.assertIn(extra, self.sent(root))  # Dockerfile count check is the backstop
            with self.assertRaisesRegex(preflight.PreflightError, "exactly one engine wheel"):
                preflight.check_context(root, SHA)

    def test_preflight_rejects_any_extra_under_dist(self):
        for extra in self.EXTRAS[:3]:
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as tmp:
                root = _make_context(tmp, extras=(extra,))
                with self.assertRaises(preflight.PreflightError):
                    preflight.check_context(root, SHA)

    def test_preflight_accepts_clean_context_and_checks_hash(self):
        data = b"synthetic wheel"
        good = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_context(tmp, extras=self.EXTRAS[3:], wheel_bytes=data)
            self.assertEqual(preflight.check_context(root, good), ["dist/" + WHEEL, "requirements.lock"])
            with self.assertRaisesRegex(preflight.PreflightError, "sha256"):
                preflight.check_context(root, SHA)

    def test_preflight_rejects_mutable_base_refs(self):
        preflight.validate_base_image("sha256:" + "a" * 64)
        preflight.validate_base_image("example/base@sha256:" + "0" * 64)
        for bad in MUTABLE_REFS:
            with self.subTest(ref=bad), self.assertRaises(preflight.PreflightError):
                preflight.validate_base_image(bad)

    def test_preflight_main_fails_closed_on_tag_without_docker(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = preflight.main(["--base-image", "example/base:latest", "--engine-commit", "a" * 40,
                                   "--wheel-sha256", SHA, "--skip-docker"])
        self.assertEqual(code, 1)
        self.assertIn("not a tag", err.getvalue())

    def test_build_command_never_pulls(self):
        args = argparse.Namespace(base_image="sha256:" + "a" * 64, engine_commit="a" * 40,
                                  wheel_sha256=SHA, tag="t")
        command = preflight.build_command(args)
        self.assertIn("--pull=false", command)
        self.assertNotIn("--pull", [c for c in command if c != "--pull=false"])


if __name__ == "__main__":
    unittest.main()
