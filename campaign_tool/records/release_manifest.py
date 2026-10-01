"""Report source or installed-package provenance without network access."""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile

from campaign_tool import __version__

MANIFEST_SCHEMA = 1
LEDGER_SCHEMA = 1  # campaign_tool.records.ledger.migrations.v001.VERSION (checked by tests).
FEATURES = (
    "offline_finding_gates", "filesystem_intake", "catalog_snapshot_import",
    "private_catalog_board", "agency_candidate_reconciliation",
    "release_manifest", "public_tree_scan", "canonical_ledger",
)
GENERATED = {"records/_release_manifest.json", "records/_release_dependencies.txt"}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def package_files(package):
    result = {}
    for path in sorted(package.rglob("*")):
        relative = path.relative_to(package).as_posix()
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"} or relative in GENERATED:
            continue
        if path.is_symlink():
            raise ValueError("symlink in package")
        if path.is_file():
            result[relative] = digest(path.read_bytes())
    return result


def _git(root, *arguments):
    try:
        return subprocess.check_output(["git", "-C", str(root), *arguments],
                                       stderr=subprocess.DEVNULL, timeout=10).decode().strip()
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise ValueError("source Git provenance is unavailable") from error


def committed_package_files(root):
    """Hash committed package blobs, never ignored or index-only working bytes."""
    try:
        raw = subprocess.check_output(
            ["git", "-C", str(root), "archive", "--format=tar", "HEAD", "campaign_tool"],
            stderr=subprocess.DEVNULL, timeout=30)
        result = {}
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            for member in archive:
                path = PurePosixPath(member.name)
                if not path.parts or path.parts[0] != "campaign_tool" or ".." in path.parts:
                    raise ValueError("invalid committed package path")
                if member.isdir():
                    continue
                relative = PurePosixPath(*path.parts[1:]).as_posix()
                if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"} or relative in GENERATED:
                    continue
                if not member.isfile():
                    raise ValueError("non-regular committed package input")
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError("committed package input unavailable")
                result[relative] = digest(source.read())
        if not result:
            raise ValueError("committed package inventory missing")
        return result
    except (OSError, subprocess.SubprocessError, tarfile.TarError) as error:
        raise ValueError("committed package provenance is unavailable") from error


def source_manifest(root):
    root = Path(root).resolve()
    if Path(_git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise ValueError("source root is not the Git repository root")
    commit = _git(root, "rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("invalid source commit")
    files = package_files(root / "campaign_tool")
    # Status omits ignored inputs and may hide assume-unchanged tracked edits.
    dirty = bool(_git(root, "status", "--porcelain", "--untracked-files=normal"))
    dirty = dirty or files != committed_package_files(root)
    tags = _git(root, "tag", "--points-at", "HEAD").splitlines()
    lock = (root / "requirements-records-test.txt").read_bytes()
    try:
        committed_lock = subprocess.check_output(
            ["git", "-C", str(root), "show", "HEAD:requirements-records-test.txt"],
            stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("committed dependency lock is unavailable") from error
    dirty = dirty or lock != committed_lock
    return {
        "manifest_schema_version": MANIFEST_SCHEMA,
        "commit": commit,
        "package_version": __version__,
        "ledger_schema_version": LEDGER_SCHEMA,
        "dependency_lock_sha256": digest(lock),
        "dependency_lock_name": "requirements-records-test.txt",
        "enabled_features": list(FEATURES),
        "runtime_services_enabled": [],
        "source_dirty": dirty,
        "release_tag": __version__ if __version__ in tags else None,
        "release_status": "tagged_release" if not dirty and __version__ in tags else "candidate",
        "package_files": files,
    }


def verify_installed(package, manifest):
    if not isinstance(manifest, dict) or type(manifest.get("manifest_schema_version")) is not int or manifest["manifest_schema_version"] != MANIFEST_SCHEMA:
        raise ValueError("unsupported release manifest")
    if manifest.get("package_version") != __version__:
        raise ValueError("package version does not match manifest")
    if not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("commit", ""))):
        raise ValueError("invalid installed commit")
    if manifest.get("source_dirty") is not False:
        raise ValueError("wheel was built from a dirty source tree")
    expected = manifest.get("package_files")
    if not isinstance(expected, dict) or not expected:
        raise ValueError("package inventory missing")
    for name, sha in expected.items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or str(relative) != name or "\\" in name:
            raise ValueError("invalid package inventory path")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ValueError("invalid package inventory hash")
    if package_files(package) != expected:
        raise ValueError("installed package files do not match release manifest")
    lock = package / "records" / "_release_dependencies.txt"
    if lock.is_symlink() or digest(lock.read_bytes()) != manifest.get("dependency_lock_sha256"):
        raise ValueError("installed dependency lock does not match manifest")
    if manifest.get("ledger_schema_version") != LEDGER_SCHEMA or type(manifest.get("ledger_schema_version")) is not int:
        raise ValueError("ledger schema declaration does not match this package")
    if manifest.get("enabled_features") != list(FEATURES) or manifest.get("runtime_services_enabled") != []:
        raise ValueError("feature declaration does not match this package")
    if manifest.get("release_status") not in {"candidate", "tagged_release"}:
        raise ValueError("invalid release status")
    tag = manifest.get("release_tag")
    if tag not in {None, __version__} or (manifest["release_status"] == "tagged_release") != (tag == __version__):
        raise ValueError("release tag and status do not agree")
    return manifest


def version_report():
    package = Path(__file__).resolve().parents[1]
    stored = package / "records" / "_release_manifest.json"
    if stored.exists():
        if stored.is_symlink() or stored.stat().st_size > 1024 * 1024:
            raise ValueError("invalid installed manifest file")
        manifest = verify_installed(package, json.loads(stored.read_text()))
        installation = "installed_wheel"
    else:
        manifest = source_manifest(package.parent)
        installation = "source_checkout"
    return {**manifest, "installation_kind": installation,
            "manifest_sha256": digest(canonical(manifest)),
            "limits": ["Code features are not proof of a running service or completed records processing.",
                       "File integrity is checked; this manifest is not a signature or owner approval.",
                       "The dependency lock identifies pinned optional parser/test versions, not installed dependency inventory."]}


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(arguments)
    try:
        report = version_report()
    except (ValueError, OSError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, sort_keys=True, indent=2))
    else:
        print(f"records {report['package_version']} {report['commit']} ({report['release_status']}, {report['installation_kind']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
