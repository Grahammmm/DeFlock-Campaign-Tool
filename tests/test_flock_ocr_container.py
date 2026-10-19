"""Static checks for the pinned OCR container recipe (no Docker, no network)."""
import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import re
import tempfile
import unittest
import zipfile
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
                                  wheel_sha256=SHA, engine_manifest_sha256=SHA, tag="t")
        command = preflight.build_command(args)
        self.assertIn("--pull=false", command)
        self.assertNotIn("--pull", [c for c in command if c != "--pull=false"])
        self.assertIn(f"ENGINE_MANIFEST_SHA256={SHA}", command)

    def test_preflight_requires_the_pinned_engine_commit(self):
        args = argparse.Namespace(engine_commit="a" * 40)
        with self.assertRaisesRegex(preflight.PreflightError, "pins.json"):
            preflight.check_pins(args)
        args = argparse.Namespace(engine_commit=PINS["engine"]["commit"])
        preflight.check_pins(args)
        self.assertEqual(args.engine_manifest_sha256, PINS["engine"]["manifest_sha256"])


PINS = json.loads((RECIPE / "pins.json").read_text())
_BW_SPEC = importlib.util.spec_from_file_location("flock_ocr_build_wheel", RECIPE / "build_wheel.py")
build_wheel = importlib.util.module_from_spec(_BW_SPEC)
_BW_SPEC.loader.exec_module(build_wheel)
_VF_SPEC = importlib.util.spec_from_file_location("flock_ocr_verify", RECIPE / "verify.py")
verify = importlib.util.module_from_spec(_VF_SPEC)
_VF_SPEC.loader.exec_module(verify)


class FlockOcrPinsTests(unittest.TestCase):
    def test_pins_are_concrete(self):
        engine = PINS["engine"]
        self.assertRegex(engine["commit"], r"^[0-9a-f]{40}$")
        self.assertRegex(engine["tree"], r"^[0-9a-f]{40}$")
        for key in ("manifest_sha256", "dependency_lock_sha256"):
            self.assertRegex(engine[key], r"^[0-9a-f]{64}$")
        self.assertIsInstance(engine["commit_time_epoch"], int)
        for requirement in PINS["wheel_build"]["requirements"]:
            self.assertRegex(requirement, r"^[a-z]+==\d+(\.\d+)+$")
        for value in PINS["apt"].values():
            self.assertNotIn(value.lower(), ("latest", "", "*"))

    def test_dockerfile_apt_pins_equal_pins_json(self):
        self.assertEqual(dict(spec.split("=", 1) for spec in apt_install_specs()), PINS["apt"])

    def test_wheel_build_tools_match_repository_build_pins(self):
        self.assertEqual(sorted(PINS["wheel_build"]["requirements"]),
                         sorted(f"{n}=={v}" for n, v in pins(ROOT / "requirements-build.txt").items()))

    def test_engine_version_matches_package_and_lock_digest_is_reproducible_locally(self):
        from campaign_tool import __version__
        engine = PINS["engine"]
        self.assertEqual(engine["wheel_filename"], f"deflock_campaign_tool-{engine['package_version']}-py3-none-any.whl")
        # The pinned commit is an ancestor of this tree; the lock is unchanged since then.
        self.assertEqual(__version__, engine["package_version"])
        lock = (ROOT / "requirements-records-test.txt").read_bytes()
        self.assertEqual(hashlib.sha256(lock).hexdigest(), engine["dependency_lock_sha256"])

    def test_expected_versions_cover_every_requested_tool(self):
        commands = {command.split()[0] for command in PINS["expected_versions"]}
        self.assertLessEqual({"ocrmypdf", "pdftotext", "qpdf", "tesseract"}, commands)
        for command, expected in PINS["expected_versions"].items():
            upstream = expected.split()[-1]
            package = {"ocrmypdf": "ocrmypdf", "pdftotext": "poppler-utils", "pdftoppm": "poppler-utils",
                       "qpdf": "qpdf", "tesseract": "tesseract-ocr"}[command.split()[0]]
            self.assertTrue(PINS["apt"][package].split(":")[-1].startswith(upstream), command)


def _synthetic_wheel(root, manifest, name=None):
    wheel = Path(root) / (name or PINS["engine"]["wheel_filename"])
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(build_wheel.MANIFEST_MEMBER, json.dumps(manifest))
    return wheel


class FlockOcrBuildWheelTests(unittest.TestCase):
    """Synthetic manifests only; no build, network or real wheel."""

    def manifest(self, files=None):
        count = PINS["engine"]["package_file_count"]
        return {"commit": PINS["engine"]["commit"], "source_dirty": False,
                "package_files": files or {f"f{i}": SHA for i in range(count)}}

    def test_check_wheel_rejects_manifest_that_differs_from_pin(self):
        with tempfile.TemporaryDirectory() as tmp:
            wheel = _synthetic_wheel(tmp, self.manifest())
            with self.assertRaisesRegex(build_wheel.BuildError, "manifest_sha256"):
                build_wheel.check_wheel(wheel, PINS)

    def test_check_wheel_accepts_manifest_bound_by_pin(self):
        manifest = self.manifest()
        local = json.loads(json.dumps(PINS))
        local["engine"]["manifest_sha256"] = build_wheel.canonical_sha256(manifest)
        with tempfile.TemporaryDirectory() as tmp:
            wheel = _synthetic_wheel(tmp, manifest)
            self.assertEqual(build_wheel.check_wheel(wheel, local), hashlib.sha256(wheel.read_bytes()).hexdigest())

    def test_check_wheel_rejects_dirty_wrong_commit_or_name(self):
        local = json.loads(json.dumps(PINS))
        for change in ({"source_dirty": True}, {"commit": "b" * 40}):
            manifest = {**self.manifest(), **change}
            local["engine"]["manifest_sha256"] = build_wheel.canonical_sha256(manifest)
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaisesRegex(build_wheel.BuildError, "clean build"):
                    build_wheel.check_wheel(_synthetic_wheel(tmp, manifest), local)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(build_wheel.BuildError, "wheel name"):
                build_wheel.check_wheel(_synthetic_wheel(tmp, self.manifest(), "other.whl"), PINS)

    def test_build_python_must_carry_pinned_tools(self):
        def fake(versions):
            return lambda *a, **k: argparse.Namespace(returncode=0, stdout=json.dumps(versions), stderr="")
        good = dict(r.split("==") for r in PINS["wheel_build"]["requirements"])
        self.assertEqual(build_wheel.check_build_python("py", PINS, run=fake(good)), good)
        with self.assertRaisesRegex(build_wheel.BuildError, "setuptools"):
            build_wheel.check_build_python("py", PINS, run=fake({**good, "setuptools": "68.1.2"}))


def _fake_container(overrides=None):
    """Synthetic command responses for a correctly built image."""
    engine = PINS["engine"]
    table = {cmd: (0, expected + " (synthetic)\n", "") for cmd, expected in PINS["expected_versions"].items()}
    table["pdftotext -v"] = (0, "", PINS["expected_versions"]["pdftotext -v"] + "\nCopyright\n")
    table["dpkg-query"] = (0, "".join(f"{n}={v}\n" for n, v in PINS["apt"].items()), "")
    table["apt-mark showhold"] = (0, "\n".join(PINS["apt"]) + "\n", "")
    table["cat /opt/campaign-tool/ENGINE_COMMIT"] = (0, engine["commit"] + "\n", "")
    table["version"] = (0, json.dumps({"installation_kind": "installed_wheel", "commit": engine["commit"],
                                       "source_dirty": False, "manifest_sha256": engine["manifest_sha256"],
                                       "dependency_lock_sha256": engine["dependency_lock_sha256"],
                                       "package_version": engine["package_version"]}), "")
    table["--doctor"] = (0, json.dumps({"ready": True, "missing": [], "version_unverified": []}), "")
    table.update(overrides or {})

    def run(argv):
        joined = " ".join(argv)
        for key in ("dpkg-query", "version", "--doctor"):
            if key in argv:
                return table[key]
        return table.get(joined, (127, "", "not found"))
    return run


class FlockOcrVerifyTests(unittest.TestCase):
    def run_verify(self, overrides=None):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = verify.main([], run=_fake_container(overrides))
        return code, out.getvalue()

    def test_verify_passes_for_pinned_image(self):
        code, out = self.run_verify()
        self.assertEqual(code, 0, out)
        self.assertNotIn("FAIL", out)

    def test_verify_fails_on_any_drift(self):
        engine = PINS["engine"]
        cases = {
            "qpdf --version": (0, "qpdf version 11.9.1\n", ""),
            "ocrmypdf --version": (127, "", "not found"),
            "dpkg-query": (0, "qpdf=11.3.0-1\n", ""),
            "apt-mark showhold": (0, "qpdf\n", ""),
            "cat /opt/campaign-tool/ENGINE_COMMIT": (0, "b" * 40, ""),
            "version": (0, json.dumps({"installation_kind": "source_checkout", "commit": engine["commit"]}), ""),
            "--doctor": (2, json.dumps({"ready": False, "missing": ["qpdf"], "version_unverified": []}), ""),
        }
        for key, response in cases.items():
            with self.subTest(drift=key):
                code, out = self.run_verify({key: response})
                self.assertEqual(code, 1, out)
                self.assertIn("FAIL", out)

    def test_image_mode_runs_without_network(self):
        calls = []
        original = verify.subprocess.run
        verify.subprocess.run = lambda command, **kw: calls.append(command) or argparse.Namespace(
            returncode=0, stdout="", stderr="")
        try:
            verify.docker_runner(image="sha256:" + "a" * 64)(["qpdf", "--version"])
        finally:
            verify.subprocess.run = original
        self.assertEqual(calls[0][:6], ["docker", "run", "--rm", "--network", "none", "--entrypoint"])


_RB_SPEC = importlib.util.spec_from_file_location("flock_ocr_check_runbook", RECIPE / "check_runbook.py")
check_runbook = importlib.util.module_from_spec(_RB_SPEC)
_RB_SPEC.loader.exec_module(check_runbook)
RUNBOOK = json.loads((RECIPE / "runbook.json").read_text())
DOC = (ROOT / "docs" / "RECORDS-OCR-CONTAINER.md").read_text()


class FlockOcrRunbookOrderTests(unittest.TestCase):
    """Synthetic command-order regressions; no lifecycle command is executed."""

    def mutated(self, change):
        plan = json.loads(json.dumps(RUNBOOK))
        change(plan)
        return check_runbook.check(plan)

    @staticmethod
    def move(steps, kind, before_kind):
        step = next(s for s in steps if s["kind"] == kind)
        steps.remove(step)
        steps.insert(next(i for i, s in enumerate(steps) if s["kind"] == before_kind), step)

    def test_reviewed_runbook_passes(self):
        self.assertEqual(check_runbook.check(RUNBOOK), [])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(check_runbook.main([str(RECIPE / "runbook.json")]), 0)
        self.assertIn("runbook: OK", out.getvalue())

    def test_doc_lists_the_same_steps_in_the_same_order(self):
        section = DOC[DOC.index("## Owner runbook"):]
        found = re.findall(r"^\s*\d+\.\s+\[([A-Z0-9-]+)\]", section, re.M)
        self.assertEqual(found, check_runbook.ordered_ids(RUNBOOK))

    def test_doc_requires_wal_safe_owner_preserving_verified_snapshot(self):
        section = DOC[DOC.index("[S1]"):DOC.index("[B1]")]
        for needle in ("--numeric-owner", "-wal", ".backup", "PRAGMA integrity_check",
                       "content manifest", "snapshot_ok", "empty scratch directory"):
            self.assertIn(needle, section)
        quiesce = DOC[DOC.index("[Q1]"):DOC.index("[S1]")]
        self.assertIn("existing owner-controlled pause/lock", quiesce)
        self.assertIn("Do not disable", quiesce)

    def test_snapshot_before_quiescence_is_rejected(self):
        errors = self.mutated(lambda p: self.move(p["main"], "snapshot", "pause_writers"))
        self.assertTrue(any("out of order" in e for e in errors), errors)

    def test_build_or_recreate_before_snapshot_gate_is_rejected(self):
        for kind in ("build", "verify_image", "rename_old", "create_new"):
            with self.subTest(kind=kind):
                errors = self.mutated(lambda p, k=kind: self.move(p["main"], k, "snapshot"))
                self.assertTrue(any("before the snapshot gate" in e for e in errors), errors)

    def test_missing_restore_test_or_gate_is_rejected(self):
        for kind in ("restore_test", "snapshot_gate", "stop_all_mount_users"):
            with self.subTest(kind=kind):
                errors = self.mutated(lambda p, k=kind: p["main"].remove(
                    next(s for s in p["main"] if s["kind"] == k)))
                self.assertTrue(errors)

    def test_reviewed_defect_start_old_before_restore_is_rejected(self):
        errors = self.mutated(lambda p: self.move(p["rollback"]["RB-C"], "start_old", "compare_data"))
        self.assertTrue(any("restores data after a container has started" in e for e in errors), errors)

    def test_start_old_without_validation_is_rejected(self):
        for path in ("RB-A", "RB-B", "RB-C"):
            with self.subTest(path=path):
                errors = self.mutated(lambda p, n=path: p["rollback"][n].remove(
                    next(s for s in p["rollback"][n] if s["kind"] == "validate_data")))
                self.assertTrue(any("validate data" in e for e in errors), errors)

    def test_restore_without_stopping_every_mount_user_is_rejected(self):
        def change(plan):
            plan["rollback"]["RB-C"][0]["kind"] = "confirm_stopped"
        errors = self.mutated(change)
        self.assertTrue(any("stop every mount user first" in e for e in errors), errors)

    def test_failure_before_replacement_exists_restores_old_name(self):
        errors = self.mutated(lambda p: p["rollback"]["RB-B"].remove(
            next(s for s in p["rollback"]["RB-B"] if s["kind"] == "rename_old_back")))
        self.assertTrue(any("rename the old container back" in e for e in errors), errors)
        self.assertEqual([s["on_fail"] for s in RUNBOOK["main"] if s["kind"] in ("rename_old", "create_new")],
                         ["RB-B", "RB-B"])

    def test_writers_reenabled_only_last(self):
        for path in ("RB-A", "RB-B", "RB-C"):
            with self.subTest(path=path):
                errors = self.mutated(lambda p, n=path: self.move(p["rollback"][n], "release_writers", "start_old"))
                self.assertTrue(any("re-enable writers" in e for e in errors), errors)
        errors = self.mutated(lambda p: self.move(p["main"], "release_writers", "start_new"))
        self.assertTrue(errors)

    def test_rollback_never_starts_replacement_and_every_step_has_a_path(self):
        errors = self.mutated(lambda p: p["rollback"]["RB-C"].insert(1, {"id": "X", "kind": "start_new"}))
        self.assertTrue(any("never start the replacement" in e for e in errors), errors)
        def drop(plan):
            plan["main"][5]["on_fail"] = "RB-Z"
        self.assertTrue(any("no rollback path" in e for e in self.mutated(drop)))


if __name__ == "__main__":
    unittest.main()
