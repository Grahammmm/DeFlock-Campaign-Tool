"""kit fee-reply: a private, never-sent reply draft to the newest recorded fee estimate of one request.

Offline. Reads the tracker (kit/sent.json), the agency list and the law package; writes one draft to
``kit/followups/`` (0600 in a 0700 folder). It restates the recorded estimate, asks for an itemized basis and for
electronic copies, quotes the law package's fee_basis with its rule_id and status, and optionally asks whether a
narrower scope would change the estimate. It disputes nothing, pays nothing, changes no scope and sends nothing.
"""
import os
import re
import stat
import tempfile
import unicodedata
from datetime import date
from pathlib import Path

from . import kit, kit_track, law
from .legal_claims import legal_notes

NOTE = "Draft only. Nothing was sent."
FEE_CITATION = "Gov. Code § 7922.530"  # the one bounded selector: an exact citation, never a keyword match
NARROW_MAX = 300
NO_ESTIMATE = "No recorded fee estimate for this request."
NO_RULE = "No unambiguous fee rule in the law package; nothing written."
CAVEAT = "Not independently reviewed; verify applicability before sending."
ASK_ITEMIZED = "Please provide an itemized basis for the estimate, including the quantities and rates used."
ASK_ELECTRONIC = "Where the requested records exist electronically, please provide electronic copies."
ASK_NARROW = "Would limiting the scope to the following change the estimate?"
NARROW_NOTE = "This is a question about a possible change, not a replacement request."
TEMPLATE = """# Fee estimate reply draft: {name}

DRAFT — NOT SENT. Review before sending it yourself.

To: {name}, records custodian

{first}This concerns the records request {request_line}sent to {name} on {sent_on}. On {estimate_on} we recorded your fee estimate of {amount} for this request.

{ask_itemized}

{ask_electronic}

{narrow}Fee basis as encoded in the law package ({rule_id}; {citation}):

{basis}

[YOUR NAME]

Draft only. Not legal advice.
Law package status: {status}
{caveat}"""


def _escape(text):  # dynamic text cannot open a heading, list, link, emphasis, code span or tag
    return re.sub(r"([\\`*_{}\[\]<>()#+\-.!|~&])", r"\\\1", text)


def _narrow(narrow):  # plain text, 1..300 characters, trimmed; the text is never echoed in an error
    if narrow is None:
        return None
    if (not isinstance(narrow, str) or not 1 <= len(narrow) <= NARROW_MAX or narrow != narrow.strip()
            or "<" in narrow or ">" in narrow
            or any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in narrow)):
        raise ValueError(f"--narrow must be 1-{NARROW_MAX} characters of trimmed plain text on one line "
                         "with no angle brackets or control characters")
    return narrow


def _plain_line(value):  # one printable line, else None
    ok = isinstance(value, str) and value.strip() and not any(c in "<>" or unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp")
                                                              for c in value)
    return value.strip() if ok else None


def _folder(path, label):  # an existing real directory, never a link
    if os.path.islink(path) or not os.path.isdir(path):
        raise ValueError(f"{label} must be an existing real folder, not a symlink or file")


def _regular(path, label):
    if os.path.islink(path) or not os.path.isfile(path):
        raise ValueError(f"{label} must be a regular file, not a symlink")


def _fee_step(root, agency_id, request, today):
    entry = kit_track.read_sent(root).get(kit_track._key(agency_id, request))
    if entry is None or not entry["steps"]:
        raise ValueError(NO_ESTIMATE)
    label = kit_track._label(agency_id, request)
    kit_track.validate_steps(entry, label, today=today)
    steps = entry["steps"]
    for terminal in ("closed", "withdrawn"):
        if any(s["kind"] == terminal for s in steps):
            raise ValueError(f"request for {label} is {terminal}; no fee reply drafted")
    if steps[-1]["kind"] != "fee_estimate":
        raise ValueError(f"the newest recorded step for {label} is {steps[-1]['kind']}, not a fee estimate; "
                         "no fee reply drafted")
    return entry, steps[-1]


def fee_rule(package, estimate_on):  # (rule, fee_basis) of the one rule in force with the exact citation, else ValueError
    found = [r for r in law.rules_in_force(package, estimate_on) if r["citation"] == FEE_CITATION]
    basis = package["records_law"].get("fee_basis")
    if package["jurisdiction"] != "us-ca" or len(found) != 1 or _plain_basis(basis) is None \
            or _plain_line(found[0]["rule_id"]) is None or _plain_line(found[0]["citation"]) is None:
        raise ValueError(NO_RULE)
    return found[0], basis


def _plain_basis(basis):  # a nonblank string with no tag or control character other than newlines, else None
    ok = isinstance(basis, str) and basis.strip() and not any(
        c in "<>" or (unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") and c != "\n") for c in basis)
    return basis.strip() if ok else None


def _write(folder, name, text, replace):
    folder = Path(folder)
    if os.path.lexists(folder):
        _folder(folder, "kit/followups")
        if stat.S_IMODE(folder.stat().st_mode) & 0o077:
            raise ValueError("kit/followups must be private (no group or other permissions); not changed")
    else:
        folder.mkdir(mode=0o700)
    target = folder / name
    before = None
    if os.path.lexists(target):
        before = os.lstat(target)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError(f"kit/followups/{name} must be a regular single-link file, not a link or special file")
        if not replace:
            raise ValueError(f"kit/followups/{name} already exists; use --replace to rewrite it")
    fd, temp = tempfile.mkstemp(dir=folder, prefix=".fee-reply.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if before is None:
            try:
                os.link(temp, target)  # exclusive: never replaces a file that appeared meanwhile
            except FileExistsError:
                raise ValueError(f"kit/followups/{name} already exists; use --replace to rewrite it") from None
        else:
            now = os.lstat(target)
            if (now.st_ino, now.st_dev) != (before.st_ino, before.st_dev) or now.st_nlink != 1 \
                    or not stat.S_ISREG(now.st_mode):
                raise ValueError(f"kit/followups/{name} changed while writing; nothing replaced")
            os.replace(temp, target)
    finally:
        if os.path.lexists(temp):
            os.unlink(temp)


def fee_reply(root, agency_id, *, request="main", narrow=None, replace=False, law_base=None, today=None):
    """Write the private fee-estimate reply draft; return its relative path and the recorded facts.

    ``root`` should be the campaign folder as given (not resolved), so a symlink on the path is still visible."""
    from .cli import read_config  # local import: cli imports this module lazily
    root = Path(root)
    if not isinstance(agency_id, str) or not kit.AGENCY_ID.fullmatch(agency_id):
        raise ValueError("--agency must look like ca-example-police")
    request, narrow = kit_track._request(request), _narrow(narrow)
    for part, label in ((root, "the campaign folder"), (root / "kit", "kit"), (root / "kit" / "followups", "kit/followups")):
        if part == root / "kit" / "followups" and not os.path.lexists(part):
            continue
        _folder(part, label)
    _regular(root / "campaign.json", "campaign.json")
    _regular(root / "kit" / "agencies.json", "kit/agencies.json")
    agency = next((a for a in kit._load_existing(root / "kit" / "agencies.json") if a["agency_id"] == agency_id), None)
    if agency is None or agency.get("selected") is not True:
        raise ValueError(f"agency_id {agency_id!r} is not a selected agency in kit/agencies.json")
    entry, step = _fee_step(root, agency_id, request, today or date.today())
    estimate_on, cents = step["on"], step["amount_cents"]
    try:
        package = law.load_package(kit._jurisdiction(read_config(root)), law_base)
    except ValueError:
        raise ValueError(NO_RULE) from None
    rule, basis = fee_rule(package, estimate_on)
    name = _plain_line(agency.get("name"))
    if name is None:
        raise ValueError("the agency name must be one line of plain text")
    _, first = kit_track.reference_lines(entry)
    quoted = "\n".join("> " + line if line else ">" for line in _plain_basis(basis).splitlines())
    status = package["status"]
    block = f"{ASK_NARROW}\n\n> {_escape(narrow)}\n\n{NARROW_NOTE}\n\n" if narrow is not None else ""
    text = TEMPLATE.format(
        name=_escape(name), first=first and first + "\n\n", sent_on=entry["sent_on"], estimate_on=estimate_on,
        request_line=f"({request}) ", amount=kit_track._dollars(cents),
        ask_itemized=ASK_ITEMIZED, ask_electronic=ASK_ELECTRONIC, narrow=block,
        rule_id=rule["rule_id"], citation=rule["citation"], basis=quoted, status=status,
        caveat=CAVEAT if status == "draft" else "")
    notes = legal_notes([("--narrow", narrow)] if narrow is not None else [])  # raw text first, alert-only
    notes += legal_notes([("fee-reply draft", text)])
    _write(root / "kit" / "followups", f"{agency_id}--{request}-fee-{estimate_on}.md", text.rstrip("\n") + "\n", replace)
    return {"written": f"kit/followups/{agency_id}--{request}-fee-{estimate_on}.md", "agency_id": agency_id,
            "request": request, "estimate_on": estimate_on, "amount_cents": cents, "rule_id": rule["rule_id"],
            "law_package_status": status, "review_flags": notes}
