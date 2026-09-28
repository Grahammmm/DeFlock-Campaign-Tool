"""Conservative offline pre-push scan; prints locations, never matched values.

Not a complete privacy or entropy scanner. Human review of staged paths/diff is
still required. Scans tracked and nonignored untracked files, including CI input.
"""
import re
import subprocess
from pathlib import Path


PATTERNS = {
    "private_key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github_token": re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    "aws_access_key": re.compile(rb"(?:AKIA|ASIA)[A-Z0-9]{16}"),
    "brevo_key": re.compile(rb"xkeysib-[A-Za-z0-9_-]{30,}"),
    "openai_key": re.compile(rb"sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{40,}"),
    "signed_url": re.compile(rb"[?&](?:X-Amz-Signature|X-Goog-Signature|sig)=[A-Za-z0-9%+/=_-]{16,}", re.I),
    "literal_credential": re.compile(rb'''(?im)^\s*["']?(?:api_key|access_token|client_secret|password)["']?\s*[:=]\s*["'][A-Za-z0-9+/=_-]{20,}["']'''),
    "pilot_host_path": re.compile(rb"/(?:workspace/" + rb"flockbot|Users/" + rb"gt)(?:/|\b)"),
}
DENIED_PARTS = {"private", "originals", "mail-history", "blobs", "credentials"}
DENIED_SUFFIXES = {".eml", ".mbox", ".sqlite", ".sqlite3", ".db", ".pem", ".key", ".p12", ".pfx"}


def violations(path, data):
    findings = []
    p = Path(path)
    if (set(p.parts) & DENIED_PARTS or p.suffix.lower() in DENIED_SUFFIXES
            or p.name == ".env" or p.name.startswith(".env.") and p.name != ".env.example"):
        findings.append((path, 0, "private_path"))
    for label, pattern in PATTERNS.items():
        for match in pattern.finditer(data):
            findings.append((path, data.count(b"\n", 0, match.start()) + 1, label))
    return findings


def main():
    root = Path(__file__).resolve().parents[1]
    files = subprocess.check_output(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=root)
    findings = []
    scanned = 0
    for relative in sorted(set(files.decode().split("\0")) - {""}):
        path = root / relative
        if path.is_symlink():
            findings.append((relative, 0, "symlink_not_scanned"))
        elif path.is_file():
            findings.extend(violations(relative, path.read_bytes()))
            scanned += 1
    for path, line, label in findings:
        print(f"{path}:{line}: {label}")
    print(f"Scanned {scanned} files; {len(findings)} potential leaks. Manual privacy review still required.")
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
