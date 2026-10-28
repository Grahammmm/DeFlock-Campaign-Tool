"""File-based WP5 CI orchestration over explicit immutable dependency checkouts.

No installs, checkout, network, source edits or production work. The workflow
supplies four reviewed full commit IDs; the launcher checks them before hashing
all installed Python inputs. In-tree mode: a dependency whose marker file is
present in this engine checkout must be passed as the engine root itself; its
commit pin and distinctness checks are skipped and the receipt lists it. All generated files live in a new owner-private
outside-Git directory. Child tests run this actual file, never Python stdin,
so multiprocessing.spawn can safely import a real __main__ entry point.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


SUITES = {
    "catalog": (66, (
        "tests.records.test_catalog_stage",
        "tests.records.test_catalog_stage_drift",
        "tests.records.test_ledger_catalog",
    )),
    "composition": (4, ("tests.records.test_pipeline_composition",)),
}
DEPENDENCIES = ("wp1", "wp2", "wp4", "pr19")
# Explicit file markers: a dependency is in tree when its marker exists here.
IN_TREE_MARKERS = {
    "wp1": "campaign_tool/records/ledger/store.py",
    "wp2": "campaign_tool/records/runner/canonical_mail.py",
    "wp4": "campaign_tool/records/extraction_ledger.py",
    "pr19": "campaign_tool/records/catalog_links.py",
}


def require(value, reason):
    if not value:
        raise ValueError(reason)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def commit(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value),
            "all dependency revisions must be explicit full lowercase Git commit IDs")
    return value


def outside_git(path):
    require(path.is_absolute() and path.resolve() == path, "canonical private output required")
    require(not any((parent / ".git").exists() for parent in (path, *path.parents)),
            "private output must be outside every Git repository")


def write_private(path, value):
    raw = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
    return sha(raw)


def git(root, *args):
    process = subprocess.run(["git", "-C", str(root), *args],
        capture_output=True, text=True, check=True, timeout=20)
    return process.stdout.strip()


def checked_root(path, revision=None):
    root = Path(path).resolve(strict=True)
    require(root.is_dir() and (root / "campaign_tool/records").is_dir(), "records dependency root missing")
    require(Path(git(root, "rev-parse", "--show-toplevel")).resolve() == root,
            "dependency must be its own explicit checkout root")
    if revision is not None:
        require(git(root, "rev-parse", "HEAD") == commit(revision), "dependency commit mismatch")
        require(not git(root, "status", "--porcelain", "--untracked-files=all"),
                "pinned dependency checkout must be clean")
    return root


def in_tree_labels(engine, roots):
    """Labels whose root is the engine checkout; each must carry its marker file.

    A marker present in the engine with a different root is rejected, because
    the engine copy would shadow the external overlay at import time.
    """
    labels = []
    for name in DEPENDENCIES:
        present = (engine / IN_TREE_MARKERS[name]).is_file()
        if roots[name] == engine:
            require(present, "in-tree dependency marker missing: " + name)
            labels.append(name)
        else:
            require(not present, "dependency is in tree; pass the engine root: " + name)
    return labels


def unique(paths):
    """Order-preserving de-duplication for package search paths."""
    result = []
    for path in paths:
        if path not in result:
            result.append(path)
    return result


def pins(root):
    files = sorted((root / "campaign_tool/records").rglob("*.py"))
    files += sorted((root / "tests/records").rglob("*.py"))
    require(bool(files), "dependency Python inputs missing")
    result = {}
    for path in files:
        require(not any(p.is_symlink() for p in (path, *path.parents)), "dependency symlink not allowed")
        result[str(path.relative_to(root))] = sha(path.read_bytes())
    return result


def checkout_files(root):
    """Hash every tracked/nonignored checkout file, not Git administrative data."""
    raw = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z", "--cached",
                                   "--others", "--exclude-standard"], timeout=20)
    result = {}
    for name in sorted(set(raw.decode("utf-8").split("\0")) - {""}):
        relative = Path(name)
        require(not relative.is_absolute() and ".." not in relative.parts, "noncanonical checkout file")
        path = root / relative
        require(path.is_file() and not any(p.is_symlink() for p in (path, *path.parents)),
                "checkout file unavailable or symlinked")
        result[name] = sha(path.read_bytes())
    return result


def successful(result, expected):
    return (result.testsRun == expected and result.wasSuccessful()
            and not result.skipped and not result.expectedFailures and not result.unexpectedSuccesses)


def worker(args):
    manifest = json.loads(Path(args.manifest).read_bytes())
    roots = {key: Path(value) for key, value in manifest["roots"].items()}
    require(set(roots) == {*DEPENDENCIES, "wp5"}, "five explicit roots required")
    for name, root in roots.items():
        require(pins(root) == manifest["pins"][name], "source changed before test worker: " + name)
        if "checkout_files" in manifest:
            require(checkout_files(root) == manifest["checkout_files"][name],
                    "checkout changed before test worker: " + name)
    root = roots["wp5"]
    os.chdir(root)
    sys.path.insert(0, str(root))
    external = [str(roots[name]) for name in ("wp1", "pr19") if roots[name] != root]
    os.environ["RECORDS_TEST_DEPENDENCIES"] = json.dumps(unique(external))
    os.environ["RECORDS_COMPOSITION_MANIFEST"] = str(Path(args.manifest).resolve())
    output = Path(args.output).resolve(strict=True)
    outside_git(output)
    require(output.stat().st_uid == os.geteuid() and not output.stat().st_mode & 0o077,
            "owner-private worker output required")
    os.environ["RECORDS_COMPOSITION_OUTPUT"] = str(output)
    os.environ["TMPDIR"] = str(output)
    tempfile.tempdir = str(output)
    expected, names = SUITES[args.worker]
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromNames(names)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    stable = all(pins(path) == manifest["pins"][name] for name, path in roots.items())
    if "checkout_files" in manifest:
        stable = stable and all(checkout_files(path) == manifest["checkout_files"][name]
                                for name, path in roots.items())
    report = {"suite": args.worker, "python": sys.version.split()[0], "expected": expected,
        "tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
        "skipped": len(result.skipped), "expected_failures": len(result.expectedFailures),
        "unexpected_successes": len(result.unexpectedSuccesses), "sources_unchanged": stable,
        "in_tree": manifest.get("in_tree", []),
        "passed": successful(result, expected) and stable}
    write_private(output / "ci-test-result.json", report)
    print(json.dumps(report, sort_keys=True), flush=True)
    return 0 if report["passed"] else 1


def launch(args):
    engine = checked_root(Path(__file__).resolve().parents[1])
    requested = {name: Path(getattr(args, name)).resolve(strict=True) for name in DEPENDENCIES}
    in_tree = in_tree_labels(engine, requested)
    pinned = [name for name in DEPENDENCIES if name not in in_tree]
    # Check the pending WP4 decision before touching any checkout or output.
    revisions = {name: commit(getattr(args, name + "_revision")) for name in pinned}
    roots = {name: checked_root(requested[name], revisions[name]) for name in pinned}
    roots.update({name: engine for name in in_tree})
    roots["wp5"] = engine
    require(len({roots[name] for name in pinned} | {engine}) == len(pinned) + 1,
            "pinned dependency checkouts must be distinct from each other and the engine")
    print("WP5 CI dependency sources: in-tree=" + (",".join(in_tree) or "none")
          + " pinned=" + (",".join(pinned) or "none"), flush=True)
    parent = Path(args.output_parent).resolve(strict=True)
    outside_git(parent)
    root = Path(tempfile.mkdtemp(prefix="wp5-ci-", dir=parent))
    root.chmod(0o700)
    manifest = {"roots": {key: str(value) for key, value in roots.items()},
                "pins": {key: pins(value) for key, value in roots.items()},
                "checkout_files": {key: checkout_files(value) for key, value in roots.items()},
                "revisions": revisions, "in_tree": in_tree,
                "wp5_revision": git(roots["wp5"], "rev-parse", "HEAD")}
    manifest_path = root / "composition-manifest.json"
    manifest_hash = write_private(manifest_path, manifest)
    outcomes = {}
    for suite in SUITES:
        output = root / suite
        output.mkdir(mode=0o700)
        command = [sys.executable, "-B", str(Path(__file__).resolve()), "--worker", suite,
                   "--manifest", str(manifest_path), "--output", str(output)]
        try:
            result = subprocess.run(command, cwd=roots["wp5"], timeout=240, check=False)
            outcomes[suite] = result.returncode
        except subprocess.TimeoutExpired:
            outcomes[suite] = 124
    write_private(root / "ci-run.json", {"schema": "wp5-ci-v1", "manifest_sha256": manifest_hash,
        "python": sys.version.split()[0], "outcomes": outcomes, "in_tree": in_tree,
        "scope": "synthetic offline tests only; no publication or deployment"})
    print("WP5 private CI receipt:", root / "ci-run.json", flush=True)
    return int(any(outcomes.values()))


def self_test():
    """Pure launcher guards; no dependency checkout, network or environment changes."""
    for value in (None, "", "main", "pending", "a" * 39, "A" * 40):
        try:
            commit(value)
        except ValueError:
            pass
        else:
            raise AssertionError("non-pinned revision accepted")
    assert commit("a" * 40) == "a" * 40
    assert unique(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]
    class Result:
        testsRun = 64
        skipped = []
        expectedFailures = []
        unexpectedSuccesses = []
        def wasSuccessful(self):
            return True
    sample = Result()
    assert successful(sample, 64)
    assert not successful(sample, 4)
    sample.skipped = [("synthetic", "missing dependency")]
    assert not successful(sample, 64)
    sample.skipped = []
    sample.expectedFailures = [("synthetic", "unresolved expectation")]
    assert not successful(sample, 64)
    print("Launcher guards passed: exact commit pins, exact counts, zero skips, zero expected failures, "
          "order-preserving path de-duplication.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--worker", choices=tuple(SUITES), help=argparse.SUPPRESS)
    parser.add_argument("--manifest", help=argparse.SUPPRESS)
    parser.add_argument("--output", help=argparse.SUPPRESS)
    parser.add_argument("--output-parent")
    for name in DEPENDENCIES:
        parser.add_argument("--" + name)
        parser.add_argument("--" + name + "-revision", default=os.environ.get(name.upper() + "_REVISION"))
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if args.worker:
        require(args.manifest and args.output, "worker manifest and private output required")
        return worker(args)
    require(args.output_parent and all(getattr(args, name) for name in DEPENDENCIES),
            "explicit dependency roots and outside-Git output parent required")
    return launch(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print("WP5 CI preflight/runtime failed:", str(error), file=sys.stderr)
        raise SystemExit(1)
