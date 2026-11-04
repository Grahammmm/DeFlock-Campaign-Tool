"""kit-copy: the sendable part of kit/requests/<id>.md as kit/outgoing/<id>.txt (for a records portal) or an unsent
kit/outgoing/<id>.eml: the Subject line and the request text after it, before ``Statutory context`` or ``Operator
checklist:`` (banner, ``To:`` and ``Agency:`` come before Subject). Nothing is sent: no network or SMTP code, and
kit/sent.json is not touched (record a send with kit-sent).

``--for mail`` writes a printable private letter, kit/outgoing/<id>.letter.html, and its plain .txt twin: the
requester block from kit/request-settings.json, today's date, the verified postal address and the sendable text.
Nothing is printed or sent. The two files are replaced one after the other, not as a pair: if the second write
fails the first may already be new; the command reports the failure and running it again rewrites both.
"""
import html
import json
import os
import stat
import sys
from datetime import date
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


LETTER_STYLE = ("@page{margin:1in}body{margin:0;font-family:Georgia,'Times New Roman',serif;font-size:12pt;"
                "line-height:1.4;color:#000;background:#fff}.block{white-space:pre-wrap;margin:0 0 1.6em}")
LETTER_CSP = "default-src 'none'; style-src 'unsafe-inline'"


def _letter_texts(blocks):
    """(html bytes, plain-text bytes, plain text) for the letter blocks; every block is escaped inert text."""
    plain = "\n\n".join(blocks) + "\n"
    paragraphs = "\n".join(f'<p class="block">{html.escape(block, quote=True)}</p>' for block in blocks)
    page = (f'<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            f'<meta http-equiv="Content-Security-Policy" content="{LETTER_CSP}">\n'
            f'<title>Records request letter</title>\n<style>{LETTER_STYLE}</style>\n</head>\n'
            f'<body>\n{paragraphs}\n</body>\n</html>\n')
    return page.encode("utf-8"), plain.encode("utf-8"), plain


def _preflight(out, names):
    """Refuse a symlink or non-directory kit/outgoing and a non-regular destination before anything is written."""
    if os.path.lexists(out) and (out.is_symlink() or not out.is_dir()):
        raise ValueError("kit/outgoing must be a directory (not a symlink)")
    for name in names:
        target = out / name
        if os.path.lexists(target) and not stat.S_ISREG(target.lstat().st_mode):
            raise ValueError(f"kit/outgoing/{name} must be a regular file (not a symlink)")


def _mail(root, entry, part, today):
    """The letter for ``entry``: {"written": [html, txt], "characters", "subject"}; checks everything before writing."""
    agency_id, kit_dir = entry["agency_id"], root / "kit"
    report = kit_check.agency_report(root, entry)
    if not report["ok"]:
        shown = ", ".join(kit_check._shown(hit["placeholder"]) for hit in report.get("placeholders", [])[:3])
        why = ([f"unfilled: {shown}"] if shown else []) + ([] if report["recipient_verified"] else ["recipient not verified"])
        raise ValueError(f"kit-check fails for {agency_id} (whole draft{''.join('; ' + item for item in why)}); run kit-check")
    address = kit.postal_proof(entry, today)
    requester = kit.load_request_settings(kit_dir, today).get("requester_block")
    if not requester:
        raise ValueError("kit/request-settings.json needs a requester block for a mailed letter")
    blocks = [requester, (today or date.today()).isoformat(), "\n".join(address),
              "Subject: " + part["subject"], part["body"]]
    hits = kit_check.draft_problems("\n".join(blocks))
    if hits:
        shown = ", ".join(kit_check._shown(hit["placeholder"]) for hit in hits[:3])
        raise ValueError(f"{len(hits)} unfilled placeholder(s) in the letter: {shown}; fix the requester block or draft")
    page, text, plain = _letter_texts(blocks)
    names = (f"{agency_id}.letter.html", f"{agency_id}.letter.txt")
    out = kit_dir / "outgoing"
    _preflight(out, names)
    if not os.path.lexists(out):
        out.mkdir(mode=0o700)
    for index, (name, data) in enumerate(zip(names, (page, text))):
        try:
            _write(out, name, data)
        except OSError as exc:
            window = f"; {names[0]} was already replaced, run the same command again" if index else ""
            raise ValueError(f"kit/outgoing/{name} could not be written ({exc.strerror or 'I/O error'}){window}") from None
    return {"written": [f"kit/outgoing/{name}" for name in names], "characters": len(plain),
            "subject": part["subject"]}


def copy(root, agency_id, target, *, today=None):
    """Write kit/outgoing/<id>.txt (``target`` portal), an unsent .eml (email) or the letter pair (mail)."""
    if target not in ("portal", "email", "mail"):
        raise ValueError("copy target must be portal, email or mail")
    root = Path(root)
    entry = _agency(root, agency_id)
    kit_dir = root / "kit"
    if kit_dir.is_symlink() or (kit_dir / "requests").is_symlink():
        raise ValueError("kit/ and kit/requests/ must be directories (not symlinks)")
    part = sendable(_regular(kit_dir / "requests" / f"{agency_id}.md", f"kit/requests/{agency_id}.md"))
    if not part["subject"] or not part["body"]:
        raise ValueError(f"kit/requests/{agency_id}.md has no subject or no request text after its Subject line")
    if target == "mail":
        return _mail(root, entry, part, today)
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
    print(json.dumps(result, indent=2, ensure_ascii=False) if as_json else "\n".join(
        f"{k}: {', '.join(v) if isinstance(v, list) else v}" for k, v in result.items()))
    print("Copy only. Nothing was sent.", file=sys.stderr)
