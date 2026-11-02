#!/usr/bin/env python3
"""Render an already-produced private correspondence report; no source access."""
import argparse
from collections import defaultdict
from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat
import unicodedata

MAX_BYTES = 32 * 1024 * 1024
GAP_TEXT = {
    "outstanding_items_and_follow_up_unreviewed": "Outstanding items and follow-up have not been reviewed.",
    "determination_due_timezone_or_format_unresolved": "The recorded determination date needs date or time zone clarification.",
    "portal_bytes_not_received": "A portal item is listed but its document bytes have not been received.",
    "thread_parent_missing": "A reply names a message that is not in this report.",
    "message_id_collision": "Different message files share a message identifier; the reply link is uncertain.",
    "self_reply_reference": "A message names itself as its reply parent.",
    "thread_reply_cycle": "Reply references form a loop, so no thread link is established.",
    "header_projection_invalid": "The recorded header evidence cannot support a thread link.",
    "thread_headers_unsupported": "The recorded headers use an unsupported format.",
    "message_id_missing_or_ambiguous": "A message identifier is missing or ambiguous.",
    "message_id_invalid": "A recorded message identifier has an unsupported format.",
    "header_message_id_mismatch": "The message identifier does not match its recorded header evidence.",
    "ambiguous_reply_headers": "Reply headers name ambiguous parents.",
    "unsupported_reply_header_syntax": "Reply headers use syntax this report cannot interpret.",
    "request_join_unverified": "A request association has not been verified.",
    "request_join_invalid": "A request association lacks consistent recorded evidence.",
    "request_join_scope_excluded": "A request association uses evidence excluded from this report.",
    "occurrence_ancestry_invalid": "A document's parent chain contains a loop or exceeds the supported depth.",
    "occurrence_parent_missing": "A document's recorded parent is missing.",
    "request_binding_conflict": "Document associations disagree about which request they belong to.",
    "request_binding_ambiguous": "A document cannot be associated with just one request.",
}


def text(value):
    value = str(value if value is not None else "")
    value = "".join("?" if unicodedata.category(c).startswith("C") or c in "\u2028\u2029" else c for c in value)
    value = re.sub(r"[0-9a-fA-F]{40,}", lambda m: m[0][:12], value)
    value = re.sub(r"([\\*_`\[\]<>#|])", r"\\\1", value)
    return re.sub(r"^(\s*)([-+]|\d+\.)", r"\1\\\2", value)


def date(value):
    if not value:
        return "none is recorded"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return text(value) + " (date format unresolved)"
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    shown = f"{parsed.day} {months[parsed.month - 1]} {parsed.year}"
    if parsed.tzinfo is None:
        shown += f" ({text(value)}) (time zone not recorded)"
    return shown


def order(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return (0, parsed.timestamp()) if parsed.tzinfo else (1, parsed.isoformat())
    except (ValueError, AttributeError, OverflowError, OSError):
        return (2, str(value or ''))


def thread_lines(request, report):
    mail = {m["id"]: m for m in report.get("mail", [])}
    by_hash = defaultdict(list)
    for message in mail.values():
        by_hash[message["eml_sha256"]].append(message)
    replies = {r["mail_id"]: r for r in report.get("thread_candidates", [])}
    occurrences = request.get("occurrences", [])
    by_id = {o["id"]: o for o in occurrences}
    groups = defaultdict(list)
    for occurrence in occurrences:
        groups[occurrence["original_sha256"]].append(occurrence)
    events = []
    for digest, copies in groups.items():
        messages = sorted(by_hash.get(digest, []), key=lambda m: (order(m.get("received_at")), m["id"]))
        occurrence = min(copies, key=lambda o: (order(o.get("acquired_at")), o["id"]))
        if messages:
            message = messages[0]
            when = message.get("received_at")
            line = f"Received {date(when)} in {text(message.get('folder'))}, file {text(digest[:12])}"
            count = max(len(messages), len(copies))
            if count > 1:
                line += f"; received {count} times"
            parents = {p for m in messages for p in replies.get(m['id'], {}).get('parent_mail_ids', [])}
            for p in sorted(parents):
                if p in mail:
                    line += f"; reply to {date(mail[p].get('received_at'))} message (from reply headers; not verified)"
        else:
            when = occurrence.get("acquired_at")
            line = f"Document acquired {date(when)}, {text(occurrence.get('kind'))}, file {text(digest[:12])}"
        parent_row = by_id.get(occurrence.get('parent_occurrence_id'), {})
        events.append((order(when), occurrence["id"], digest, parent_row.get('original_sha256'), line))
    children = defaultdict(list)
    for event in sorted(events):
        children[event[3] if event[3] in groups and event[3] != event[2] else None].append(event)
    result, visited = [], set()
    stack = [(e, 0) for e in reversed(children[None])]
    while stack:
        event, depth = stack.pop()
        if event[2] in visited:
            continue
        visited.add(event[2])
        result.append('  ' * depth + '- ' + event[4] + '.')
        stack.extend((e, depth + 1) for e in reversed(children[event[2]]))
    result.extend('- ' + e[4] + '.' for e in sorted(events) if e[2] not in visited)
    return result


def render(report):
    lines = ["# Private correspondence", ""]
    requests = report.get("requests", [])
    if not requests:
        lines += ["No requests are recorded.", ""]
    agencies = {a["id"]: a.get("name", a["id"]) for a in report.get("agencies", [])}
    for aid in sorted({r['agency_id'] for r in requests}, key=lambda a: (agencies.get(a, a), a)):
        lines += ["## " + text(agencies.get(aid, aid)), ""]
        for request in sorted((r for r in requests if r['agency_id'] == aid),
                              key=lambda r: (order(r.get('submitted_at')), r['id'])):
            lines += ["### " + text(request.get("external_ref") or request['id']), "",
                      f"Submitted {date(request.get('submitted_at'))}.",
                      f"Determination was due {date(request.get('determination_due_at'))}; "
                      + (f"determination recorded {date(request['determination_at'])}." if request.get('determination_at') else "none is recorded."),
                      f"Production: {date(request.get('production_at'))}."]
            if request.get('fulfillment') == 'not_assessed':
                lines.append("Whether this request was fully answered has not been assessed.")
            lines += [""] + thread_lines(request, report) + [""]
    lines += ["## Still open", ""]
    for reminder in report.get('reminder_proposals', []):
        lines.append(f"- Request {text(reminder.get('request_id'))}: Suggested follow-up on {date(reminder.get('at'))} (proposal only; the owner decides).")
    if not report.get('reminder_proposals'):
        lines.append("No follow-up proposals are recorded.")
    lines += ["", "## What this report cannot tell you", "", "### Gaps in words", ""]
    for gap in report.get('gaps', []):
        code = gap.get('code', '')
        lines.append("- " + GAP_TEXT.get(code, "Other gap: `" + text(code) + "`"))
    lines += ["", "### Report limits", ""] + [text(limit) for limit in report.get('limits', [])]
    lines += ["", f"As of {date(report.get('as_of'))}. Snapshot {text(str(report.get('snapshot_sha256', ''))[:12])}.", ""]
    return "\n".join(lines)


class Refusal(Exception):
    pass


def absolute(value):
    if not os.path.isabs(value) or os.path.normpath(value) != value:
        raise Refusal('path_not_absolute')
    return Path(value)


def open_directory(path):
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except OSError:
        os.close(fd)
        raise


def unique_members(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate member')
        result[key] = value
    return result


def load_report(path):
    try:
        parent = open_directory(path.parent)
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        finally:
            os.close(parent)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise Refusal('report_unreadable')
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise Refusal('report_unreadable')
    except OSError:
        raise Refusal('report_unreadable') from None
    try:
        report = json.loads(data, object_pairs_hook=unique_members)
    except (ValueError, UnicodeError, RecursionError):
        raise Refusal('report_not_json') from None
    if not isinstance(report, dict) or report.get('schema') != 'correspondence-report-v1':
        raise Refusal('schema_unsupported')
    if 'error' in report:
        raise Refusal('report_has_error')
    if report.get('visibility') != 'private':
        raise Refusal('report_not_private')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', required=True)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args(argv)
    try:
        source, output = absolute(args.report), absolute(args.output_dir)
        report = load_report(source)
        try:
            content = render(report)
        except (KeyError, TypeError, AttributeError, ValueError, RecursionError):
            raise Refusal('report_not_json') from None
        try:
            parent = open_directory(output.parent)
        except OSError:
            raise Refusal('output_parent_unsafe') from None
        try:
            try:
                os.mkdir(output.name, 0o700, dir_fd=parent)
            except FileExistsError:
                raise Refusal('output_dir_exists') from None
            directory = os.open(output.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                os.fchmod(directory, 0o700)
                fd = os.open('correspondence.md', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(content)
            finally:
                os.close(directory)
        finally:
            os.close(parent)
    except (Refusal, OSError) as error:
        print(json.dumps({'error': str(error) if isinstance(error, Refusal) else 'output_parent_unsafe'}))
        return 2
    print(json.dumps({'ok': True, 'agencies': len(report.get('agencies', [])),
                      'requests': len(report.get('requests', [])), 'gaps': len(report.get('gaps', []))}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
