"""Offline public-tree scan. Findings contain locations and categories, not values."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

PATTERNS = {
    "private_key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github_token": re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    "aws_access_key": re.compile(rb"(?:AKIA|ASIA)[A-Z0-9]{16}"),
    "brevo_key": re.compile(rb"xkeysib-[A-Za-z0-9_-]{30,}"),
    "openai_key": re.compile(rb"sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{40,}"),
    "signed_url": re.compile(rb"[?&](?:X-Amz-Signature|X-Goog-Signature|sig)=[A-Za-z0-9%+/=_-]{16,}", re.I),
    "literal_credential": re.compile(rb'''(?i)(?<![A-Za-z0-9_])["']?(?:api_key|access_token|client_secret|password)["']?\s*[:=]\s*(?:"[^"\r\n]{20,}"|'[^'\r\n]{20,}')'''),
    "pilot_host_path": re.compile(rb"/(?:workspace/" + rb"flockbot|Users/" + rb"gt)(?:/|\b)"),
}
STRICT_PATTERNS = {
    "private_actor_path": re.compile(rb"/home/" + rb"coder(?:/|\b)"),
    "portal_host": re.compile(rb"\b[a-z0-9-]+\.(?:nextrequest\.com|mycusthelp\.com)\b", re.I),
}
DENIED_PARTS = {"private", "originals", "mail-history", "blobs", "credentials"}
DENIED_SUFFIXES = {".eml", ".mbox", ".sqlite", ".sqlite3", ".db", ".pem", ".key", ".p12", ".pfx"}


def violations(path, data, *, strict=False, private_index=None):
    findings = []
    p = Path(path)
    if (set(p.parts) & DENIED_PARTS or p.suffix.lower() in DENIED_SUFFIXES
            or p.name == ".env" or p.name.startswith(".env.") and p.name != ".env.example"):
        findings.append((path, 0, "private_path"))
    patterns = {**PATTERNS, **(STRICT_PATTERNS if strict else {})}
    for label, pattern in patterns.items():
        for match in pattern.finditer(data):
            findings.append((path, data.count(b"\n", 0, match.start()) + 1, label))
    if private_index:
        hashes = private_index.get("original_sha256", [])
        for match in re.finditer(r"(?<![a-fA-F0-9])[a-fA-F0-9]{64}(?![a-fA-F0-9])", str(path)):
            if match.group().lower() in hashes:
                findings.append((path, 0, "private_original_hash_path"))
        if data and hashlib.sha256(data).hexdigest() in hashes:
            findings.append((path, 0, "private_original_bytes"))
        for match in re.finditer(rb"(?<![a-fA-F0-9])[a-fA-F0-9]{64}(?![a-fA-F0-9])", data):
            if match.group().decode().lower() in hashes:
                findings.append((path, data.count(b"\n", 0, match.start()) + 1, "private_original_hash"))
        for field, label in (("portal_hosts", "private_portal_host"), ("text_fragments", "private_correspondence")):
            for value in private_index.get(field, []):
                needle = value.encode()
                haystack = data.lower() if field == "portal_hosts" else data
                needle = needle.lower() if field == "portal_hosts" else needle
                position = haystack.find(needle)
                if position >= 0:
                    findings.append((path, data.count(b"\n", 0, position) + 1, label))
    return findings


def load_private_index(path):
    if path is None:
        return None
    return validate_private_index(json.loads(path.read_text()))


def validate_private_index(value):
    if not isinstance(value, dict) or set(value) - {"original_sha256", "portal_hosts", "text_fragments", "coverage", "correspondence_sources"}:
        raise ValueError("invalid private scan index")
    for field in ("original_sha256", "portal_hosts", "text_fragments"):
        entries = value.get(field, [])
        if not isinstance(entries, list) or any(not isinstance(item, str) or not item for item in entries):
            raise ValueError("invalid private scan index entries")
    if any(not re.fullmatch(r"[0-9a-f]{64}", item) for item in value.get("original_sha256", [])):
        raise ValueError("invalid private original hash")
    if any(len(item) < 24 for item in value.get("text_fragments", [])):
        raise ValueError("private text fragments must have at least 24 characters")
    return value


def scan(root, *, strict=False, private_index=None):
    root = root.resolve()
    names = subprocess.check_output(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=root)
    findings, non_content_matches, scanned = [], [], 0
    for relative in sorted(set(names.decode().split("\0")) - {""}):
        path = root / relative
        if path.is_symlink():
            findings.append((relative, 0, "symlink_not_scanned"))
        elif path.is_file():
            data = path.read_bytes()
            findings.extend(violations(relative, data, strict=strict, private_index=private_index))
            if not data and private_index and hashlib.sha256(data).hexdigest() in private_index.get("original_sha256", []):
                non_content_matches.append({"path": relative, "classification": "zero_byte_original_non_content"})
            scanned += 1
    return {"scanned_files": scanned, "findings": findings, "strict": strict,
            "private_index_checked": private_index is not None,
            "private_export_clearance": False, "non_content_matches": non_content_matches,
            "limits": "Pattern and supplied-index coverage only; manual privacy review remains required."}


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--strict", action="store_true",
                      help="Require private indexed clearance of a separate exact export")
    mode.add_argument("--patterns-only", action="store_true",
                      help="Run all strict patterns without claiming private export clearance")
    parser.add_argument("--private-index", type=Path)
    receipt = parser.add_mutually_exclusive_group()
    receipt.add_argument("--attestation", type=Path)
    receipt.add_argument("--verify-attestation", type=Path)
    args = parser.parse_args(arguments)
    try:
        if args.strict:
            from .export_clearance import clearance
            if args.private_index is None or not (args.attestation or args.verify_attestation):
                raise ValueError("strict_requires_private_index_and_attestation")
            result = clearance(args.root, args.private_index,
                               args.verify_attestation or args.attestation,
                               verify=args.verify_attestation is not None)
            print(json.dumps(result, sort_keys=True))
            return int(bool(result["findings"]))
        if args.attestation or args.verify_attestation:
            raise ValueError("attestation_requires_strict")
        index = load_private_index(args.private_index)
        if args.private_index and (args.root.resolve() == args.private_index.resolve() or args.root.resolve() in args.private_index.resolve().parents):
            raise ValueError("private scan index must be outside the public repository")
        result = scan(args.root, strict=args.patterns_only, private_index=index)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(json.dumps({"error": str(error), "scan_complete": False}), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return int(bool(result["findings"]))


if __name__ == "__main__":
    raise SystemExit(main())
