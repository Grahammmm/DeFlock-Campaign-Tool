"""Private, offline press drafts based only on published findings."""
import os
import re
from datetime import date
from pathlib import Path

from .content import ContentError, _read_json, load_content, load_finding
from .kit import _kit_folder, _write_json, _write_text
from .site import CLASSIFICATION_LABELS, CONFIDENCE_LABELS
from .social import _load_record, _sha


MAX_CHARS = 6000


def _bad_ids(ids, available):
    bad = sorted(set(ids) - set(available))
    if bad:
        raise ValueError('; '.join(f'finding {i} is not a published finding; only published findings can be cited' for i in bad))


def _explain_unpublished(root, ids):
    """On loader failure only, identify requested drafts without accepting invalid content."""
    available, saw_draft = set(), False
    for path in sorted((root / 'content' / 'findings').glob('*.json')):
        document = _read_json(path)
        if isinstance(document, dict) and document.get('state') == 'draft':
            saw_draft = True
        else:
            available.add(load_finding(document, 'findings/' + path.name)['id'])
    if saw_draft:
        _bad_ids(ids, available)


def _text(findings, base_url, contact):
    ids = ', '.join(f['id'] for f in findings)
    parts = [f'<!-- DRAFT: not sent. Review before any use. findings {ids} -->',
             f"# {findings[0]['title']}",
             ' '.join(f"{f['summary']} (finding {f['id']})" for f in findings)]
    for f in findings:
        parts += [f"## {f['title']}",
                  f"Classification: {CLASSIFICATION_LABELS[f['classification']]} · "
                  f"Confidence: {CONFIDENCE_LABELS[f['confidence']]} · Event date: {f['event_date']}",
                  'Limitations:\n' + ('\n'.join('- ' + item for item in f['limitations']) or 'None stated.'),
                  'Corrections:\n' + ('\n'.join(f"- {c['date']}: {c['note']}" for c in f['corrections'])
                                       or 'No corrections issued.'),
                  f"Read the finding and its sources: {base_url.rstrip('/')}/findings/{f['slug']}.html"]
    parts += [f"Contact: {contact or '[add a contact before use]'}",
              'This draft cites published findings only. It is not legal advice.']
    text = '\n\n'.join(parts) + '\n'
    if len(text) > MAX_CHARS:
        raise ValueError(f'press draft exceeds {MAX_CHARS} characters')
    return text


def build_press(root, finding_ids, day=None):
    """Write one private draft; preserve organizer edits and never send it."""
    root = Path(root)
    ids = list(dict.fromkeys(finding_ids or []))
    if not ids:
        raise ValueError('press draft needs at least one --finding')
    if len(ids) > 12:
        raise ValueError('press draft permits at most 12 finding ids')
    if day is None:
        day = date.today().isoformat()
    try:
        if not isinstance(day, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
            raise ValueError
        date.fromisoformat(day)
    except ValueError:
        raise ValueError('--date must be a real YYYY-MM-DD date') from None
    try:
        model = load_content(root)
    except ContentError:
        _explain_unpublished(root, ids)
        raise
    if model is None:
        raise ValueError('press drafts need a content/ directory')
    base = model['site'].get('base_url')
    if not base:
        raise ValueError('social drafts need content/site.json base_url')
    available = {f['id']: f for f in model['findings']}
    _bad_ids(ids, available)
    findings = [available[i] for i in ids]
    text = _text(findings, base, model['site'].get('contact_email'))
    kit = root / 'kit'
    folder = kit / 'press'
    for directory in (kit, folder):
        _kit_folder(directory)
    name = f"{day}-{findings[0]['slug']}.md"
    target = folder / name
    record_path = folder / '.written.json'
    record = _load_record(record_path)
    edited = False
    if os.path.lexists(target):
        edited = (target.is_symlink() or not target.is_file() or
                  _sha(target.read_bytes()) != record.get(name))
    if edited:
        folder = kit / 'press.suggested'
        _kit_folder(folder)
        target = folder / name
    else:
        record[name] = _sha(text.encode('utf-8'))
    _write_text(target, text)
    if not edited:
        _write_json(record_path, dict(sorted(record.items())))
    return {'path': target.relative_to(root).as_posix(), 'findings': ids, 'kept_edited': edited, 'sent': False}
