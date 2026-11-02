"""Private, offline printable packs from staged meetings and published findings."""
import csv
import html
import io
import json
import os
import re
import tempfile
from datetime import date, datetime, time, timezone
from pathlib import Path

from .cli import read_config
from .content import _read_json, load_finding, load_meeting, load_site, load_sources
from .meeting_stage import stale_kits
from .site import CLASSIFICATION_LABELS, CONFIDENCE_LABELS, FULL_HASH


def _unlinked(path):
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("symlink on meeting-pack path; use real directories and files")


def _handout(name, meeting, findings, library, site, truncated):
    e = lambda value: html.escape(str(value), quote=True)
    base = (site.get("base_url") or "").rstrip("/")
    parts = ['<!doctype html><html lang="en"><meta charset="utf-8"><title>Meeting handout</title>',
             '<style>@page { size: letter; margin: 0.6in } body { font: 10pt sans-serif; }',
             'h1 { font-size: 16pt } h2 { font-size: 12pt } p { margin: .3em 0 }',
             'article { break-inside: avoid; margin: .6em 0 } .summary { white-space: nowrap;',
             'overflow: hidden; text-overflow: ellipsis } a { overflow-wrap: anywhere }',
             '@media print { body { margin: 0 } }</style><body>',
             f'<h1>{e(name)}</h1><p>{e(meeting["body"])} — {e(meeting["starts_at"])}</p>',
             f'<p>{e(meeting["agenda_item"])} · {e(meeting["location"])}</p>']
    if meeting.get("agenda_url"):
        parts.append(f'<p><a href="{e(meeting["agenda_url"])}">Agenda</a></p>')
    parts.append('<h2>What we found</h2>')
    if not findings:
        parts.append('<p>Records are requested; nothing is published yet.</p>')
    for finding in findings:
        parts.append(f'<article><strong>{e(finding["title"])}</strong>'
                     f'<p class="summary">{e(" ".join(finding["summary"].split()))}</p>'
                     f'<p>{e(CLASSIFICATION_LABELS[finding["classification"]])}; '
                     f'{e(CONFIDENCE_LABELS[finding["confidence"]])}</p>')
        if finding["corrections"]:
            parts.append(f'<p>corrected {e(max(c["date"] for c in finding["corrections"]))}</p>')
        for source in finding["sources"]:
            entry = library.get(source["sha256"], {})
            parts.append(f'<p>Source: {e(source["title"])}, received '
                         f'{e(entry.get("received_at", "not recorded"))}, record {source["sha256"][:12]}</p>')
        if base:
            url = base + '/findings/' + finding['slug'] + '.html'
            parts.append(f'<p><a href="{e(url)}">{e(url)}</a></p>')
        parts.append('</article>')
    if truncated:
        parts.append(f'<p>{truncated} more on the site' + (f': {e(base)}' if base else '') + '</p>')
    parts.extend(['<p>What the record number means: the first 12 characters of a SHA-256 fingerprint of the file '
                  'exactly as received. It shows the file has not changed since; it does not by itself prove who wrote it.</p>',
                  f'<p>{e(site["not_legal_advice"])}</p></body></html>'])
    return '\n'.join(parts)


def build(root, meeting_id, max_findings=5, today=None, *, replace=False, allow_past=False):
    if not isinstance(meeting_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", meeting_id) or meeting_id in ('.', '..'):
        raise ValueError("invalid meeting id; use 1–80 letters, digits, dots, underscores or hyphens")
    if type(max_findings) is not int or not 1 <= max_findings <= 10:
        raise ValueError("--max-findings must be 1..10")
    root = Path(root).absolute()
    _unlinked(root)
    content = root / 'content'
    _unlinked(content)
    if not content.is_dir():
        raise ValueError("content/ missing; run init/content first")
    meeting_path = content / 'meetings' / (meeting_id + '.json')
    _unlinked(meeting_path)
    if not meeting_path.is_file():
        raise ValueError(f"meeting not staged; run `meetings-stage --id {meeting_id}` first")
    raw = _read_json(meeting_path)
    meeting = load_meeting(raw, 'meeting pack')
    if meeting.get('id') not in (None, meeting_id):
        raise ValueError("meeting id differs from filename; re-stage the requested meeting")
    if meeting['status'] == 'cancelled':
        raise ValueError("meeting cancelled; choose a scheduled meeting")
    starts = datetime.fromisoformat(meeting['starts_at'].replace('Z', '+00:00'))
    current = today or datetime.now(timezone.utc)
    if isinstance(current, date) and not isinstance(current, datetime):
        current = datetime.combine(current, time.min, tzinfo=starts.tzinfo)
    if current.tzinfo is None:
        raise ValueError("today needs a time zone")
    if starts < current and not allow_past:
        raise ValueError("meeting is past; use --allow-past for an archive pack")
    if any(item['meeting'] == meeting_id for item in stale_kits(root)):
        raise ValueError(f"stale kit; re-stage with `meetings-stage --id {meeting_id} --replace`")
    site = load_site(_read_json(content / 'site.json'))
    library = load_sources(_read_json(content / 'sources.json')) if (content / 'sources.json').exists() else []
    library = {entry['sha256']: entry for entry in library}
    findings = []
    for path in sorted((content / 'findings').glob('*.json')):
        document = _read_json(path)
        if document.get('state') != 'published':
            continue
        # Titles are displayed as escaped text, never interpreted as markup.
        title = document.get('title')
        finding = load_finding({**document, 'title': html.escape(title) if isinstance(title, str) else title}, path.name)
        finding['title'] = title
        if meeting['agency_id'] is None or finding['agency_id'] == meeting['agency_id']:
            findings.append(finding)
    findings.sort(key=lambda f: (f['event_date'], f['slug']), reverse=True)
    truncated = max(0, len(findings) - max_findings)
    findings = findings[:max_findings]
    rows = {}
    for finding in findings:
        for source in finding['sources']:
            digest = source['sha256']
            entry = library.get(digest, {})
            row = rows.setdefault(digest, [source['title'], digest[:12], entry.get('received_at', 'not recorded'),
                                          [], entry.get('public_url') or 'held privately', []])
            if source['locator'] not in row[3]:
                row[3].append(source['locator'])
            if finding['slug'] not in row[5]:
                row[5].append(finding['slug'])
    sheet = io.StringIO(newline='')
    writer = csv.writer(sheet)
    writer.writerow(['title', 'record_prefix', 'received', 'locator', 'public_copy', 'cited_by'])
    for row in rows.values():
        writer.writerow([*row[:3], '; '.join(row[3]), row[4], '; '.join(row[5])])
    outputs = {'handout.html': _handout(read_config(root)['name'], meeting, findings, library, site, truncated),
               'comment.md': raw.get('comment_kit_md', ''), 'sources.csv': sheet.getvalue()}
    if any(FULL_HASH.search(text) for text in outputs.values()):
        raise ValueError("pack contains a full hash; re-stage using 12-character record prefixes")
    folder = root / 'kit' / 'meeting-packs' / meeting_id
    _unlinked(folder)
    for name in outputs:
        _unlinked(folder / name)
    if folder.exists() and not replace:
        raise ValueError("meeting pack exists; use --replace to overwrite")
    for directory in (root / 'kit', folder.parent, folder):
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
    for name, text in outputs.items():
        fd, temporary = tempfile.mkstemp(dir=folder, prefix='.meeting-pack-')
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(text.encode('utf-8'))
            os.replace(temporary, folder / name)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return {'written': [str((folder / name).relative_to(root)) for name in outputs],
            'findings': len(findings), 'truncated': truncated}
