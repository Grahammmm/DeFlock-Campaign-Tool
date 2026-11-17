"""Turn one agency letter draft (NEXT-RESPONSE.md shape) plus an operator mapping into console review copies.

Writes one records-console-draft-input-v1 correspondence-draft JSON per mapped draft and proves each through the
real draft_import.import_draft(project, file, apply=False). Nothing is applied, approved, placed or sent, and the
markdown alone never supplies checked provenance: ids, section, why, citations and the checked recipient basis all
come from the operator's mapping.

source_attachment_sha256 is the SHA-256 of the exact raw --md file bytes. It hashes the draft markdown source only;
it is not an agency attachment digest and not proof of original evidence. Citations are passed through unchanged.
"""
import argparse
import hashlib
import json
import os
import re
import stat
import sys

from . import approval_queue as aq, draft_import as di

MAPPING_SCHEMA = "records-agency-draft-mapping-v1"
MAX_MD = 256 * 1024
MAX_MAPPING = 128 * 1024
NOTE = ("source_attachment_sha256 hashes the draft markdown source bytes, not an agency attachment "
        "and not proof of original evidence")
REQUIRED = ("draft_key", "agency_id", "request_id", "source_run_id", "source_section", "why", "envelope")
HEADING = re.compile(r"#{1,3}[ \t]+(.*?)[ \t]*")
MARKER = re.compile(r"\[EDIT ME[^\]\n]*\]")


class Refuse(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _read(path, cap, code):
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > cap:
            raise Refuse(code)
        raw = os.read(fd, cap + 1)
        if len(raw) > cap:
            raise Refuse(code)
        return raw
    except OSError:
        raise Refuse(code) from None
    finally:
        if fd is not None:
            os.close(fd)


def letter(markdown, section):
    """(subject, body) from the first quote block under the exact heading text."""
    lines = markdown.replace("\r\n", "\n").split("\n")
    heads = [i for i, line in enumerate(lines) if HEADING.fullmatch(line.rstrip("\r"))]
    starts = [i for i in heads if HEADING.fullmatch(lines[i].rstrip("\r")).group(1) == section]
    if not starts:
        raise Refuse("section_unknown")
    block = []
    for line in lines[starts[0] + 1:]:
        if HEADING.fullmatch(line.rstrip("\r")):
            break
        if line.startswith(">"):
            block.append(line[2:] if line.startswith("> ") else line[1:])
        elif block:
            break
    if not block:
        raise Refuse("letter_missing")
    match = re.fullmatch(r"Subject:[ \t]*(.+?)[ \t]*", block[0])
    if not match:
        raise Refuse("subject_missing")
    rest = block[1:]
    if rest and rest[0] == "":
        rest = rest[1:]
    while rest and rest[-1] == "":
        rest.pop()
    return match.group(1), "\n".join(rest) + "\n"


def build(entry, identity, markdown, digest):
    """One import document, or Refuse(code)."""
    for name in REQUIRED:
        if not entry.get(name):
            raise Refuse("field_missing:" + name)
    if not all(isinstance(entry[n], str) for n in REQUIRED[:-1]) or not isinstance(entry["envelope"], dict):
        raise Refuse("mapping_invalid")
    if not di.ID.fullmatch(entry["draft_key"]):
        raise Refuse("draft_key_invalid")
    agencies = identity["agencies"]
    if entry["agency_id"] not in agencies:
        raise Refuse("agency_unknown")
    if entry["request_id"] not in agencies[entry["agency_id"]]:
        raise Refuse("request_agency_mismatch" if any(entry["request_id"] in v for v in agencies.values())
                     else "request_unknown")
    if entry["source_run_id"] not in identity["runs"]:
        raise Refuse("run_unknown")
    subject, body = letter(markdown, entry["source_section"])
    envelope = entry["envelope"]
    if envelope.get("subject") != subject:
        raise Refuse("subject_mismatch")
    placeholders = entry.get("placeholders", [])
    if not isinstance(placeholders, list) or not all(isinstance(p, str) for p in placeholders):
        raise Refuse("mapping_invalid")
    if any(m not in placeholders for m in MARKER.findall(body)):
        raise Refuse("placeholder_unlisted")
    if any(p not in body for p in placeholders):
        raise Refuse("placeholder_not_found")
    basis = envelope.get("recipient_basis")
    if not isinstance(basis, dict) or basis.get("checked") is not True \
            or basis.get("source_type") not in ("custodian", "source-header"):
        raise Refuse("basis_unchecked")
    if envelope.get("thread_item_id") is None and basis["source_type"] != "custodian":
        raise Refuse("thread_basis_invalid")
    citations = entry.get("citations", [])
    if not isinstance(citations, list):
        raise Refuse("mapping_invalid")
    return {"schema": di.SCHEMA, "draft_key": entry["draft_key"], "kind": di.KIND, "agency_id": entry["agency_id"],
            "request_id": entry["request_id"], "body": body, "why": entry["why"],
            "source_run_id": entry["source_run_id"], "source_attachment_sha256": digest,
            "source_section": entry["source_section"], "citations": citations, "envelope": envelope}


def _identity(mapping):
    ident = mapping.get("identity") if isinstance(mapping, dict) else None
    drafts = mapping.get("drafts") if isinstance(mapping, dict) else None
    if not (isinstance(mapping, dict) and mapping.get("schema") == MAPPING_SCHEMA and isinstance(ident, dict)
            and isinstance(ident.get("agencies"), dict) and isinstance(ident.get("runs"), list)
            and all(isinstance(v, list) for v in ident["agencies"].values())
            and isinstance(drafts, list) and drafts and all(isinstance(d, dict) for d in drafts)):
        raise Refuse("mapping_invalid")
    return ident, drafts


def export(md_path, mapping_path, project, out):
    """Report dict; never applies. Global refusals raise Refuse."""
    raw = _read(md_path, MAX_MD, "md_invalid")
    try:
        markdown = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise Refuse("md_invalid") from None
    try:
        mapping = aq._strict_load(_read(mapping_path, MAX_MAPPING, "mapping_invalid"), "mapping_invalid")
    except aq.ApprovalQueueError:
        raise Refuse("mapping_invalid") from None
    ident, drafts = _identity(mapping)
    out = os.path.abspath(out)
    real_out, real_project = os.path.realpath(out), os.path.realpath(project)
    if real_out == real_project or real_out.startswith(real_project + os.sep):
        raise Refuse("out_inside_project")
    if os.path.lexists(out) and (os.path.islink(out) or not os.path.isdir(out) or os.listdir(out)):
        raise Refuse("out_not_empty")
    digest = hashlib.sha256(raw).hexdigest()
    keys = [d.get("draft_key") for d in drafts]
    os.makedirs(out, mode=0o700, exist_ok=True)
    files, refused = [], []
    for entry in drafts:
        key, path = entry.get("draft_key"), None
        try:
            if keys.count(key) > 1:
                raise Refuse("draft_key_duplicate")
            doc = build(entry, ident, markdown, digest)
            if "/" in key:
                raise Refuse("draft_key_filename_invalid")
            path = os.path.join(out, key + ".json")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            try:
                result = di.import_draft(project, path, apply=False)
                if result.get("action") != "would_queue" or result.get("applied") is not False:
                    raise Refuse("import_refused:" + str(result.get("action")))
            except aq.ApprovalQueueError as error:
                raise Refuse("import_refused:" + error.code) from None
            except Refuse:
                raise
            except (OSError, ValueError):
                raise Refuse("import_refused:input_invalid") from None
            files.append(path)
        except Refuse as error:
            if path is not None and os.path.lexists(path):
                os.unlink(path)
            refused.append({"draft_key": key if isinstance(key, str) else None, "code": error.code})
    return {"ok": not refused, "files": files, "refused": refused, "source_digest_note": NOTE,
            "source_attachment_sha256": digest}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="records agency-draft-export", description=__doc__)
    for name in ("--md", "--mapping", "--project", "--out"):
        parser.add_argument(name, required=True)
    args = parser.parse_args(argv)
    try:
        report = export(args.md, args.mapping, args.project, args.out)
    except Refuse as error:
        report = {"ok": False, "files": [], "refused": [{"draft_key": None, "code": error.code}],
                  "source_digest_note": NOTE}
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
