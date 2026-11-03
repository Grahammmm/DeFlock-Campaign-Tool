"""kit-copy: the sendable part of kit/requests/<id>.md as kit/outgoing/<id>.txt (for a records portal) or an unsent
kit/outgoing/<id>.eml: the Subject line and the request text after it, before ``Statutory context`` or ``Operator
checklist:`` (banner, ``To:`` and ``Agency:`` come before Subject). Nothing is sent: no network or SMTP code, and
kit/sent.json is not touched (record a send with kit-sent).
"""
import json
import os
import stat
import sys
from email.message import EmailMessage
from pathlib import Path

from . import kit, kit_check

STATUTORY = "## Statutory context (for the requester, not for sending)"
CHECKLIST = "Operator checklist:"
MAX_BYTES = 1 << 20


def sendable(text):
    """{"subject", "body"}: the first ``Subject: `` line's text and the lines after it, before the first end marker."""
    lines = text.split("\n")  # never splitlines(): a CR in the Subject line must stay visible, not become a line
    start = next((i for i, line in enumerate(lines) if line.startswith("Subject: ")), None)
    if start is None:
        raise ValueError("draft has no Subject line")
    subject = lines[start][len("Subject: "):].strip(" ")
    if not all(c.isprintable() for c in subject):
        raise ValueError("subject must be one line")
    end = next((i for i in range(start + 1, len(lines)) if lines[i] == STATUTORY or lines[i].startswith(CHECKLIST)),
               len(lines))
    kept = [i for i in range(start + 1, end) if lines[i].strip()]  # leading/trailing blank lines are trimmed
    return {"subject": subject, "body": "\n".join(lines[kept[0]:kept[-1] + 1]) if kept else ""}


def _regular(path, label):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular file (not a symlink)")
    if path.stat().st_size > MAX_BYTES:
        raise ValueError(f"{label} is larger than 1 MiB")
    return path.read_bytes().decode("utf-8")  # bytes, not read_text: universal newlines would hide a CR


def _agency(root, agency_id):
    agencies = root / "kit" / "agencies.json"
    if agencies.is_symlink() or not agencies.is_file():
        raise ValueError("run kit first: kit/agencies.json is missing")
    entry = next((e for e in kit._load_existing(agencies) if e["agency_id"] == agency_id), None)
    if entry is None:
        raise ValueError(f"agency_id {agency_id!r} is not in kit/agencies.json")
    return entry


def _write(out, name, data):
    """Write ``out/name`` 0600 via a temp file and os.replace inside ``out``, opened without following a symlink."""
    try:
        directory = os.open(out, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise ValueError("kit/outgoing must be a directory (not a symlink)") from None
    temporary = f".{name}.{os.getpid()}.tmp"
    try:
        os.fchmod(directory, 0o700)
        if name in os.listdir(directory) and not stat.S_ISREG(os.stat(name, dir_fd=directory,
                                                                         follow_symlinks=False).st_mode):
            raise ValueError(f"kit/outgoing/{name} must be a regular file (not a symlink)")
        handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(handle, "wb") as stream:
            os.fchmod(handle, 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(handle)
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
    except BaseException:
        if temporary in os.listdir(directory):
            os.unlink(temporary, dir_fd=directory)
        raise
    finally:
        os.close(directory)


def copy(root, agency_id, target):
    """Write kit/outgoing/<id>.txt (``target`` portal) or an unsent .eml (email); return written/characters/subject."""
    root = Path(root)
    entry = _agency(root, agency_id)
    kit_dir = root / "kit"
    if kit_dir.is_symlink() or (kit_dir / "requests").is_symlink():
        raise ValueError("kit/ and kit/requests/ must be directories (not symlinks)")
    part = sendable(_regular(kit_dir / "requests" / f"{agency_id}.md", f"kit/requests/{agency_id}.md"))
    if not part["subject"] or not part["body"]:
        raise ValueError(f"kit/requests/{agency_id}.md has no subject or no request text after its Subject line")
    hits = kit_check.draft_problems(part["subject"] + "\n" + part["body"])
    if hits:
        shown = ", ".join(kit_check._shown(hit["placeholder"]) for hit in hits[:3])
        raise ValueError(f"{len(hits)} unfilled placeholder(s) in the sendable part: {shown}; run kit-check")
    if target == "email":
        address = entry.get("records_email")
        if entry.get("verified") is not True or not isinstance(address, str) or not kit.EMAIL.fullmatch(address):
            raise ValueError(f"{agency_id} has no verified records_email; run kit-verify-agency first")
        message = EmailMessage()  # no From, Date or Message-ID: the organizer's mail client adds them
        message["To"], message["Subject"], message["X-Unsent"] = address, part["subject"], "1"
        message.set_content(part["body"] + "\n", charset="utf-8")
        name, data = f"{agency_id}.eml", message.as_bytes()
    else:
        name, data = f"{agency_id}.txt", (part["subject"] + "\n\n" + part["body"] + "\n").encode("utf-8")
    out = kit_dir / "outgoing"
    if not os.path.lexists(out):
        out.mkdir(mode=0o700)
    _write(out, name, data)
    return {"written": f"kit/outgoing/{name}", "characters": len(part["subject"]) + len(part["body"]),
            "subject": part["subject"]}


def run_cli(root, agency_id, target, as_json=False):
    """Print the result (text or JSON); a refusal prints ``Stopped: <reason>`` and exits 3."""
    try:
        result = copy(root, agency_id, target)
    except ValueError as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        sys.exit(3)
    print(json.dumps(result, indent=2, ensure_ascii=False) if as_json else "\n".join(f"{k}: {v}" for k, v in result.items()))
    print("Copy only. Nothing was sent.", file=sys.stderr)
