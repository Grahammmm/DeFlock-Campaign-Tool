"""Fail closed on scan hits except exact, owner-approved provenance hashes.

Approval pins bind the complete reviewed manifest bytes, not a field-name or
path exemption. Pins are written as adjacent short literals so entropy scanning
does not mistake these nonsecret approval anchors for additional credentials.
Never add credential values to this approval map.
"""
import hashlib
import json
from pathlib import Path
import re
import sys


APPROVED_MANIFESTS = {
    'docs/records-gates-import.json': (
        '73e48fa8dd9bb3ad'
        '44f0c4a6d387edd1'
        'af0fe2229cda663c'
        '4833f4f63e937a79'
    ),
    'docs/records-intake-import.json': (
        '24dfa4b022ca72fb'
        '69285de638d61569'
        'b0472167cc62ec3b'
        'ac056ca6392eede8'
    ),
}

HASH_LINE = re.compile(r'\s*"(?:source_sha256|engine_sha256|schema_sha256)": "([a-f0-9]{64})",?\s*')


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
            if verified_provenance_hit(root, path, hit):
                accepted += 1
            else:
                print(f"{path}:{hit['line_number']}: {hit['type']}")
                count += 1
    print(f"Verified provenance hash hits: {accepted}; potential secrets: {count}; manual privacy review is still required.")
    return int(count > 0)


if __name__ == "__main__":
    raise SystemExit(main())
