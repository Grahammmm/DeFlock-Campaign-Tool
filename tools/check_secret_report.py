"""Fail closed on scan hits except exact, owner-approved provenance hashes.

Approval pins bind the complete reviewed manifest bytes, not a field-name or
path exemption. Pins are adjacent literals with exact-line scanner annotations for these
reviewed, nonsecret approval anchors; no detector or directory is disabled.
Reviewed build-pin files (APPROVED_PIN_FILES) are bound the same way: whole-file
digest, then only the public engine commit/tree/manifest/lock identity lines.
Never add credential values to this approval map.
"""
import hashlib
import json
from pathlib import Path
import re
import sys


APPROVED_MANIFESTS = {
    'docs/records-gates-import.json': (
        '7fdbd0576e72dad7'  # pragma: allowlist secret
        '4cd567436dec32b0'  # pragma: allowlist secret
        '3a745b4fe8f23b7e'  # pragma: allowlist secret
        '7776a834fcf471b5'  # pragma: allowlist secret
    ),
    'docs/records-intake-import.json': (
        'c6d125c64208ac43'  # pragma: allowlist secret
        'c7390ffba30091a1'  # pragma: allowlist secret
        'bff6be77f51adbeb'  # pragma: allowlist secret
        '19ee8a9e707c291d'  # pragma: allowlist secret
    ),
}

HASH_LINE = re.compile(r'\s*"(?:source_sha256|engine_sha256|schema_sha256)": "([a-f0-9]{64})",?\s*')

# Reviewed public build pins. Approval binds the complete pins file bytes; only
# the listed `engine` identity fields qualify, each with its exact hex length,
# and the scanned line must equal the parsed JSON value. Nothing else in the
# file, and no other file, is exempt. Changing pins.json requires a reviewed
# update of this digest.
APPROVED_PIN_FILES = {
    'container/flock-ocr/pins.json': (
        '8a42359935e6cac7'  # pragma: allowlist secret
        'bd874dec62be7482'  # pragma: allowlist secret
        '156cd9ec2a70fb08'  # pragma: allowlist secret
        '8a3735d5a303716b'  # pragma: allowlist secret
    ),
}

PIN_FIELDS = {"commit": 40, "tree": 40, "manifest_sha256": 64, "dependency_lock_sha256": 64}
PIN_LINE = re.compile(r'\s*"(commit|tree|manifest_sha256|dependency_lock_sha256)": "([a-f0-9]{40}|[a-f0-9]{64})",?\s*')


def regular_file(root, relative):
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Invalid provenance path")
    target = root / path
    if any((root / Path(*path.parts[:n])).is_symlink() for n in range(1, len(path.parts) + 1)):
        raise ValueError("Provenance symlink rejected")
    if not target.is_file():
        raise ValueError("Provenance file unavailable")
    return target


def verified_provenance_hit(root, path, hit, approved=None):
    approved = APPROVED_MANIFESTS if approved is None else approved
    if path not in approved or hit.get("type") != "Hex High Entropy String":
        return False
    try:
        raw = regular_file(root, path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != approved[path]:
            return False
        number = hit.get("line_number")
        if type(number) is not int or number < 1:
            return False
        lines = raw.decode("utf-8").splitlines()
        if number > len(lines):
            return False
        match = HASH_LINE.fullmatch(lines[number - 1])
        if not match or hashlib.sha1(match[1].encode()).hexdigest() != hit.get("hashed_secret"):
            return False
        manifest = json.loads(raw)
        entries = manifest["files"]
        if not isinstance(entries, list) or not entries:
            return False
        for entry in entries:
            actual = hashlib.sha256(regular_file(root, entry["engine_path"]).read_bytes()).hexdigest()
            if actual != entry["engine_sha256"]:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def verified_pin_hit(root, path, hit, approved=None):
    approved = APPROVED_PIN_FILES if approved is None else approved
    if path not in approved or hit.get("type") != "Hex High Entropy String":
        return False
    try:
        raw = regular_file(root, path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != approved[path]:
            return False
        number = hit.get("line_number")
        if type(number) is not int or number < 1:
            return False
        lines = raw.decode("utf-8").splitlines()
        if number > len(lines):
            return False
        match = PIN_LINE.fullmatch(lines[number - 1])
        if not match or len(match[2]) != PIN_FIELDS[match[1]]:
            return False
        if hashlib.sha1(match[2].encode()).hexdigest() != hit.get("hashed_secret"):
            return False
        if sum(1 for line in lines if (m := PIN_LINE.fullmatch(line)) and m[1] == match[1]) != 1:
            return False
        engine = json.loads(raw)["engine"]
        return isinstance(engine, dict) and engine.get(match[1]) == match[2]
    except (OSError, ValueError, KeyError, TypeError):
        return False


def main():
    root = Path(__file__).resolve().parents[1]
    data = json.loads(Path(sys.argv[1]).read_text())
    results = data["results"]
    if not isinstance(results, dict):
        raise ValueError("Invalid secret-scan report")
    count = accepted = 0
    for path, hits in sorted(results.items()):
        if not isinstance(hits, list):
            raise ValueError("Invalid scan entries")
        for hit in hits:
            if verified_provenance_hit(root, path, hit) or verified_pin_hit(root, path, hit):
                accepted += 1
            else:
                print(f"{path}:{hit['line_number']}: {hit['type']}")
                count += 1
    print(f"Verified provenance hash hits: {accepted}; potential secrets: {count}; manual privacy review is still required.")
    return int(count > 0)


if __name__ == "__main__":
    raise SystemExit(main())
