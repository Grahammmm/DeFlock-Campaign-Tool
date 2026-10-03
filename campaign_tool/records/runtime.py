"""Bounded, synthetic preflight for the Linux records intake runtime.

No campaign data, credentials, network calls or campaign directories are opened.
The portable starter is separate from this supported extraction runtime.
"""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import sys
import tempfile


def platform_status():
    supported = platform.system() == "Linux" and sys.version_info >= (3, 11)
    return {"supported": supported,
            "failure_code": None if supported else "unsupported_records_runtime",
            "required": "Linux, Python 3.11 or newer"}


def check(root=None):
    report = {**platform_status(), "filesystem_checked": False,
              "atomic_no_replace": False, "parser_versions": {}}
    for package in ("pypdf", "Pillow", "openpyxl", "extract-msg"):
        try:
            report["parser_versions"][package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            report["parser_versions"][package] = None
    if not report["supported"]:
        return report
    try:
        from .extract.ocr import _directory, _publish_directory
        # Probe the same filesystem as the planned root, without creating it.
        parent = Path(os.path.abspath(root if root else tempfile.gettempdir()))
        while not os.path.lexists(parent):
            parent = parent.parent
        fd = _directory(parent)
        os.close(fd)
        with tempfile.TemporaryDirectory(prefix="records-runtime-", dir=parent) as name:
            probe = Path(name)
            source, destination, duplicate = (probe / p for p in ("source", "published", "duplicate"))
            source.mkdir()
            (source / "marker").write_bytes(b"synthetic original")
            duplicate.mkdir()
            (duplicate / "marker").write_bytes(b"synthetic replacement")
            if not _publish_directory(source, destination):
                raise RuntimeError("initial_publication_failed")
            if _publish_directory(duplicate, destination):
                raise RuntimeError("publication_replaced_existing")
            if (source.exists() or not duplicate.exists()
                    or (destination / "marker").read_bytes() != b"synthetic original"):
                raise RuntimeError("publication_invariant_failed")
        report.update(filesystem_checked=True, atomic_no_replace=True)
    except (OSError, ValueError, RuntimeError):
        report.update(supported=False, failure_code="records_filesystem_primitives_unavailable")
    return report


def require(root=None):
    report = check(root)
    if not report["supported"]:
        # Stable reason codes only; do not print private paths or OS error text.
        print(json.dumps({"status": "failed", "exit_code": 2, **report}, sort_keys=True))
        return False
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", help="planned records root; probe nearest existing parent without creating the root")
    args = parser.parse_args(argv)
    report = check(args.root)
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0 if report["supported"] else 2
