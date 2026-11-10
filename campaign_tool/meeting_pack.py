"""Private, offline printable packs from staged meetings and published findings."""
import csv
import html
import io
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from .cli import read_config
from .content import _read_json, load_campaign_agencies, load_finding, load_meeting, load_officials, load_site, load_sources
from .content_statements import newest_first, read_statements
from .labels import DEFAULTS, MAX_LABELS_BYTES, html_text, load_labels
from . import meeting_comments
from .meeting_stage import stale_kits
from .qr import CSS as QR_CSS, svg  # QR_CSS is emitted only when a base URL puts QR codes on the page
from .site import FULL_HASH


def _unlinked(path):
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("symlink on meeting-pack path; use real directories and files")


def _load_labels(content):
    """``content/labels.json`` merged over the English defaults, read as the site reads it (a symlink is refused)."""
    path = content / 'labels.json'
    return load_labels(_read_json(path, MAX_LABELS_BYTES)) if path.is_file() or path.is_symlink() else dict(DEFAULTS)


def _handout(name, meeting, findings, library, site, truncated, labels, statements=()):
    e = lambda value: html.escape(str(value), quote=True)
    # Label text is escaped once; values are filled in by format, never re-formatted (a title may hold braces).
    t = lambda key, **values: html_text(labels[key]).format(**values)
    base = (site.get("base_url") or "").rstrip("/")
    parts = ['<!doctype html><html lang="' + e(site['language']) + '"><meta charset="utf-8"><title>' + t('handout.title') + '</title>',
             '<style>@page { size: letter; margin: 0.6in } body { font: 10pt sans-serif; }',
             'h1 { font-size: 16pt } h2 { font-size: 12pt } p { margin: .3em 0 }',
             'article { break-inside: avoid; margin: .6em 0 } .summary { white-space: nowrap;',
             'overflow: hidden; text-overflow: ellipsis } a { overflow-wrap: anywhere }' + (QR_CSS if base else '') + ('\n.quote { white-space: pre-wrap }' if statements else ''),
             '@media print { body { margin: 0 } }</style><body>',
             f'<h1>{e(name)}</h1><p>{e(meeting["body"])} — {e(meeting["starts_at"])}</p>',
             f'<p>{e(meeting["agenda_item"])} · {e(meeting["location"])}</p>']
    if meeting.get("agenda_url"):
        parts.append(f'<p><a href="{e(meeting["agenda_url"])}">' + t('handout.agenda') + '</a></p>')
    parts.append('<h2>' + t('handout.what_we_found') + '</h2>')
    if not findings:
        parts.append('<p>' + t('handout.nothing_published') + '</p>')
    for finding in findings:
        parts.append(f'<article><strong>{e(finding["title"])}</strong>'
                     f'<p class="summary">{e(" ".join(finding["summary"].split()))}</p>'
                     f'<p>{t("classification." + finding["classification"])}; '
                     f'{t("confidence." + finding["confidence"])}</p>')
        if finding["corrections"]:
            parts.append('<p>' + t('handout.corrected', date=e(max(c['date'] for c in finding['corrections']))) + '</p>')
        for source in finding["sources"]:
            entry = library.get(source["sha256"], {})
            parts.append('<p>' + t('handout.source_line', title=e(source['title']),
                                   date=e(entry.get('received_at', labels['handout.not_recorded'])),
                                   record=source['sha256'][:12]) + '</p>')
        if base:
            url = base + '/findings/' + finding['slug'] + '.html'
            parts.append(f'<p><a href="{e(url)}">{e(url)}</a>{svg(url)}</p>')
        parts.append('</article>')
    if truncated:
        parts.append('<p>' + t('handout.more_on_site', count=truncated) + (f': {e(base)}' if base else '') + '</p>')
    if base:
        parts.append(f'<p><a href="{e(base)}">{e(base)}</a>{svg(base)}</p>')
    if statements:  # DF1407-07: exact stored quotes only; the quote text is escaped once and never shortened or collapsed
        parts.append('<h2>' + t('handout.statements_heading') + '</h2><p>' + t('handout.statements_note') + '</p>')
        for person, row in statements:
            parts.append('<article><p><strong>' + e(person) + '</strong>: <span class="quote">'
                         + html_text(labels['statements.quote']).format(quote=e(row['quote'])) + '</span></p>'
                         + '<p>' + e(row['said_on']) + ' (' + t('statements.context_' + row['context']) + ') <a href="'
                         + e(row['source_url']) + '">' + t('statements.source') + '</a></p></article>')
    parts.extend(['<p>' + t('handout.record_explainer') + '</p>',
                  f'<p>{e(site["not_legal_advice"])}</p></body></html>'])
    return '\n'.join(parts)


def _discard(path):
    """Remove this invocation's own incomplete directory; a failure here never hides the original error."""
    try:
        shutil.rmtree(path)
    except OSError:
        pass


def _check_pack_contents(folder, names):
    """An existing pack must be a real directory holding exactly the expected regular files (nothing is read)."""
    unexpected = ValueError("meeting pack has unexpected contents; move it aside before regenerating")
    _unlinked(folder)
    if not stat.S_ISDIR(os.lstat(folder).st_mode):
        raise unexpected
    with os.scandir(folder) as scan:
        found = {entry.name: entry.stat(follow_symlinks=False).st_mode for entry in scan}
    if set(found) != set(names) or not all(stat.S_ISREG(mode) for mode in found.values()):
        raise unexpected


def _write_member(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        stream = os.fdopen(fd, 'wb')
    except BaseException:
        os.close(fd)
        raise
    with stream:
        stream.write(data)


def _commit_pack(folder, outputs, *, replace):
    """Publish the whole pack as one prepared directory; return warnings (empty unless cleanup of the old pack failed).

    Handled errors never leave a subset or a mix of generations: the old directory is put back when promotion fails.
    Not promised: durability across power loss, no gap between the two renames, concurrent writers, batch atomicity.
    """
    parent = folder.parent
    _unlinked(folder)
    pattern = re.compile(r'\.' + re.escape(folder.name) + r'\.old-[0-9a-f]+')
    backups = sorted(parent / entry for entry in os.listdir(parent) if pattern.fullmatch(entry)) if parent.is_dir() else []
    if backups:
        raise ValueError(f"meeting pack recovery required; inspect {backups[0]} before rebuilding")
    existing = os.path.lexists(folder)
    if existing and not replace:
        raise ValueError("meeting pack exists; use --replace to overwrite")
    if existing:
        _check_pack_contents(folder, outputs)
    for directory in (parent.parent, parent):
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
    prepared = Path(tempfile.mkdtemp(prefix=f'.{folder.name}.new-', dir=parent))
    try:
        for name, text in outputs.items():
            _write_member(prepared / name, text.encode('utf-8'))
        _unlinked(folder)
        if not existing:
            if os.path.lexists(folder):
                raise ValueError("meeting pack exists; use --replace to overwrite")
            os.replace(prepared, folder)
            return []
        _check_pack_contents(folder, outputs)
        old = parent / f'.{folder.name}.old-{secrets.token_hex(8)}'
        os.replace(folder, old)
    except BaseException:
        _discard(prepared)
        raise
    try:
        os.replace(prepared, folder)
    except OSError as error:
        _discard(prepared)
        try:
            os.replace(old, folder)
        except OSError as restore:
            raise OSError(f"meeting pack restore failed; previous pack retained at {old}") from restore
        raise error
    try:
        shutil.rmtree(old)
    except OSError:
        return [f"previous pack retained at {old}; remove it after checking the new pack"]
    return []


def _statements(root, meeting, day):
    """DF1407-07: [(official, statement)] for the meeting's body, roster order, two newest each; [] means the old pack."""
    agency = meeting.get('agency_id')
    first = read_statements(root, None, day) if agency else None  # a malformed present file refuses here
    if not first or not any(row['agency_id'] == agency for row in first):
        return []
    path = root / 'content' / 'officials.json'
    roster = load_officials(_read_json(path), load_campaign_agencies(root, day), day) if path.is_file() or path.is_symlink() else {}
    rows = read_statements(root, roster, day)  # the roster check, strict this time
    chosen = []
    for person in roster.get(agency, []):
        mine = [row for row in rows if row['agency_id'] == agency and row['official'] == person['name']]
        chosen += [(person['name'], row) for row in newest_first(mine)[:2]]
    return chosen


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
    labels = _load_labels(content)
    statements = _statements(root, meeting, current.date())
    findings = []
    for path in sorted((content / 'findings').glob('*.json')):
        document = _read_json(path)
        if not isinstance(document, dict):
            raise ValueError(f"findings/{path.name}: expected an object")
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
    extra = ['kind', 'said_on', 'source_url'] if statements else []
    writer.writerow(['title', 'record_prefix', 'received', 'locator', 'public_copy', 'cited_by'] + extra)
    for row in rows.values():
        writer.writerow([*row[:3], '; '.join(row[3]), row[4], '; '.join(row[5])] + (['source', '', ''] if statements else []))
    for _person, row in statements:
        writer.writerow([''] * 6 + ['statement', row['said_on'], row['source_url']])
    outputs = {'handout.html': _handout(read_config(root)['name'], meeting, findings, library, site, truncated, labels, statements),
               'comment.md': raw.get('comment_kit_md', ''), 'sources.csv': sheet.getvalue()}
    if any(FULL_HASH.search(text) for text in outputs.values()):
        raise ValueError("pack contains a full hash; re-stage using 12-character record prefixes")
    folder = root / 'kit' / 'meeting-packs' / meeting_id
    _unlinked(folder)
    for name in outputs:
        _unlinked(folder / name)
    if folder.exists() and not replace:
        raise ValueError("meeting pack exists; use --replace to overwrite")
    warnings = _commit_pack(folder, outputs, replace=replace)
    try:  # a missing or unreadable kit/comments-sent.json is simply "none recorded"; `today` reports a bad file
        sent = meeting_comments.read(root).get(meeting_id)
    except ValueError:
        sent = None
    report = {'written': [str((folder / name).relative_to(root)) for name in outputs],
              'findings': len(findings), 'truncated': truncated,
              'comment_sent': {'on': sent['on'], 'channel': sent['channel']} if sent else None}
    if warnings:
        report['warnings'] = warnings
    return report


def build_upcoming(root, within_days=14, today=None, *, max_findings=5, replace=False):
    """Build every staged pack starting in [today, today + within_days); list each skip with its reason."""
    if type(within_days) is not int or not 1 <= within_days <= 90:
        raise ValueError("--within-days must be 1..90")
    current = today or datetime.now(timezone.utc)
    if isinstance(current, date) and not isinstance(current, datetime):
        current = datetime.combine(current, time.min, tzinfo=timezone.utc)
    if current.tzinfo is None:
        raise ValueError("today needs a time zone")
    end = current + timedelta(days=within_days)
    root = Path(root).absolute()
    content = root / 'content'
    for path in (root, content, content / 'meetings'):
        _unlinked(path)
    if not content.is_dir():
        raise ValueError("content/ missing; run init/content first")
    skipped, kept = [], []
    folder = content / 'meetings'
    for path in sorted(folder.glob('*.json')) if folder.is_dir() else []:
        meeting_id = path.stem
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", meeting_id) or meeting_id in ('.', '..'):
            skipped.append({'id': meeting_id, 'reason': 'invalid meeting id'})
            continue
        if path.is_symlink():
            skipped.append({'id': meeting_id, 'reason': 'symlink on meeting-pack path; use real directories and files'})
            continue
        try:
            if not path.is_file():
                raise ValueError('not a regular file')
            meeting = load_meeting(_read_json(path), 'meeting pack')
        except (OSError, ValueError):
            skipped.append({'id': meeting_id, 'reason': 'unreadable meeting file'})
            continue
        starts = datetime.fromisoformat(meeting['starts_at'].replace('Z', '+00:00'))
        if current <= starts < end:
            kept.append((starts, meeting_id, meeting))
    built = []
    for starts, meeting_id, meeting in sorted(kept, key=lambda item: (item[0], item[1])):
        if meeting['status'] == 'cancelled':
            skipped.append({'id': meeting_id, 'reason': 'cancelled'})
            continue
        try:
            report = build(root, meeting_id, max_findings, current, replace=replace)
        except ValueError as exc:
            skipped.append({'id': meeting_id, 'reason': str(exc)})
            continue
        built.append({'id': meeting_id, 'starts_at': meeting['starts_at'], **report})
    return {'window': {'from': current.isoformat(), 'to': end.isoformat(), 'days': within_days},
            'built': built, 'skipped': skipped}
