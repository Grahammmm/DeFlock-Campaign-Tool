"""Private, offline press drafts based only on published findings."""
import json
import os
import re
import stat
from datetime import date, datetime, timezone
from pathlib import Path

from .content import ContentError, _read_json, load_content, load_finding
from .kit import _kit_folder, _write_json, _write_text
from .legal_claims import legal_notes
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


def _check_day(day):
    try:
        if not isinstance(day, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
            raise ValueError
        date.fromisoformat(day)
    except ValueError:
        raise ValueError('--date must be a real YYYY-MM-DD date') from None


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
    _check_day(day)
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


# --- private letters to the editor (DF2207-09) --------------------------------------------------------------
LETTER_DEFAULT_WORDS, LETTER_MIN_WORDS, LETTER_MAX_WORDS = 250, 150, 400
LETTER_MAX_FINDINGS = 3
PLACEHOLDERS = ('[YOUR NAME]', '[YOUR CITY]')
_LETTER_COMMENT = re.compile(r'\A<!-- DRAFT: not sent\. Review before any use\.[^\n]*?-->\n*')
_LETTER_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.md')
_HEX64 = re.compile(r'[0-9a-f]{64}')
_RECORD_LIMIT = 1 << 20
_CONTROLS = re.compile('[\\x00-\\x1f\\x7f-\\x9f\\u2028\\u2029]')


def letter_word_count(text):
    """Words of the visible letter: whitespace tokens with a letter or digit, the two placeholders and the generated
    initial comment excluded."""
    body = _LETTER_COMMENT.sub('', text, count=1)
    for placeholder in PLACEHOLDERS:
        body = body.replace(placeholder, ' ')
    return sum(1 for token in body.split() if any(ch.isalnum() for ch in token))


def _finding_paragraph(finding, base):
    parts = [' '.join(finding['summary'].split()), f"(finding {finding['id']})"]
    if finding['corrections']:
        parts.append(f"Corrected {max(c['date'] for c in finding['corrections'])}.")
    if base:
        parts.append(f"{base.rstrip('/')}/findings/{finding['slug']}.html")
    return ' '.join(parts)


def _letter_closing(meeting, base):
    prefix = base.rstrip('/') if base else ''
    if meeting:
        return (f"I encourage readers to review the next {' '.join(meeting['body'].split())} meeting on "
                f"{meeting['starts_at']}: {prefix}/meetings/{meeting['id']}.html.")
    return f'I encourage readers to visit the campaign signup page: {prefix}/#participate.'


def letter_text(findings, site, meeting, outlet=None):
    """The full private letter file: a draft comment naming the retained findings, then the first-person body."""
    if not findings:
        raise ValueError('a letter needs at least one finding')
    base = (site or {}).get('base_url')
    ids = ', '.join(f['id'] for f in findings)
    parts = [f'<!-- DRAFT: not sent. Review before any use. findings {ids} -->',
             'To the editor' + (f' of {outlet}' if outlet else '') + ':',
             'I am writing to ask readers to consider these published records.']
    parts += [_finding_paragraph(f, base) for f in findings]
    parts += [_letter_closing(meeting, base), '\n'.join(PLACEHOLDERS)]
    return '\n\n'.join(parts) + '\n'


def _next_letter_meeting(model, findings, now):
    """The earliest scheduled staged meeting at or after ``now`` for the first finding's agency, else None."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('now must be a timezone-aware datetime')
    agency = findings[0].get('agency_id') if findings else None
    if not agency:
        return None
    best = None
    for meeting in model.get('meetings') or ():
        if meeting.get('agency_id') != agency or meeting.get('status', 'scheduled') != 'scheduled' or not meeting.get('id'):
            continue
        start = datetime.fromisoformat(meeting['starts_at'].replace('Z', '+00:00'))
        if start.tzinfo is None or start < now:
            continue
        if best is None or (start, meeting['id']) < best[0]:
            best = ((start, meeting['id']), meeting)
    return best[1] if best else None


def _letter_ids(finding_ids):
    ids = [] if finding_ids is None else finding_ids
    if isinstance(ids, str) or not isinstance(ids, (list, tuple)) or not all(isinstance(i, str) for i in ids):
        raise ValueError('--finding must be finding id text')
    ids = list(dict.fromkeys(ids))
    if not ids:
        raise ValueError('press letter needs at least one --finding')
    if len(ids) > LETTER_MAX_FINDINGS:
        raise ValueError(f'press letter permits at most {LETTER_MAX_FINDINGS} finding ids')
    return ids


def _letter_outlet(outlet):
    if outlet is None:
        return None
    text = outlet.strip() if isinstance(outlet, str) else None
    if not text or len(text) > 120 or _CONTROLS.search(text):
        raise ValueError('--outlet must be 1 to 120 characters of plain text without line breaks or control characters')
    return text


def _plain_folder(path):
    """True when ``path`` is an existing real folder; refuses a symlink or a non-folder. Creates nothing."""
    if os.path.islink(path):
        raise ValueError(f'{path.name}/ must not be a symlink')
    if os.path.lexists(path) and not path.is_dir():
        raise ValueError(f'{path.name}/ is not a folder')
    return os.path.lexists(path)


def _regular_bytes(path):
    """The bytes of a regular file, or None for a symlink, folder, FIFO or other special file (never followed)."""
    return path.read_bytes() if stat.S_ISREG(os.lstat(path).st_mode) else None


def _no_duplicate_keys(pairs):
    found = {}
    for key, value in pairs:
        if key in found:
            raise ValueError('duplicate key')
        found[key] = value
    return found


def _letter_record(path):
    """Generated-file hashes {filename: sha256}; {} when absent, a refusal when present but unsafe or malformed."""
    label = f'{path.parent.name}/{path.name}'
    if not os.path.lexists(path):
        return {}
    status = os.lstat(path)
    if not stat.S_ISREG(status.st_mode) or status.st_size > _RECORD_LIMIT:
        raise ValueError(f'{label} must be a regular file under 1 MiB; fix or remove it before writing a letter')
    try:
        record = json.loads(path.read_bytes().decode('utf-8'), object_pairs_hook=_no_duplicate_keys)
    except (ValueError, OSError, RecursionError):
        raise ValueError(f'{label} is not valid JSON without duplicate keys; fix or remove it before writing a letter') from None
    if not isinstance(record, dict) or not all(
            _LETTER_NAME.fullmatch(k) and isinstance(v, str) and _HEX64.fullmatch(v) for k, v in record.items()):
        raise ValueError(f'{label} must map simple .md file names to lowercase SHA-256 hashes; fix or remove it')
    return record


def build_letter(root, finding_ids, day=None, *, words=LETTER_DEFAULT_WORDS, outlet=None, now=None):
    """Write one private letter-to-the-editor draft from published findings; preserve edits and never send it."""
    root = Path(root)
    ids = _letter_ids(finding_ids)
    if type(words) is not int or not LETTER_MIN_WORDS <= words <= LETTER_MAX_WORDS:
        raise ValueError(f'--words must be a whole number from {LETTER_MIN_WORDS} to {LETTER_MAX_WORDS}')
    outlet = _letter_outlet(outlet)
    if day is None:
        day = date.today().isoformat()
    _check_day(day)
    if now is None:
        now = datetime.now(timezone.utc)
    elif not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('now must be a timezone-aware datetime')
    try:
        model = load_content(root)
    except ContentError:
        _explain_unpublished(root, ids)
        raise
    if model is None:
        raise ValueError('press letters need a content/ directory')
    available = {f['id']: f for f in model['findings']}
    _bad_ids(ids, available)
    findings = [available[i] for i in ids]
    meeting = _next_letter_meeting(model, findings, now)
    kept = len(findings)
    while True:
        text = letter_text(findings[:kept], model['site'], meeting, outlet)
        count = letter_word_count(text)
        if count <= words:
            break
        if kept == 1:
            raise ValueError(f'the first finding {ids[0]} needs {count} words with the fixed letter text but the limit '
                             f'is {words}; raise --words or shorten its published summary')
        kept -= 1
    name = f"{day}-{findings[0]['slug']}.md"
    try:
        flags = legal_notes([(f'press/letters/{name}', _LETTER_COMMENT.sub('', text, count=1))])
    except Exception:  # noqa: BLE001 - alert-only: a scan that cannot run is itself a flag
        flags = [f'review-required: press/letters/{name}: legal-claim wording (guard-error)']
    # Preflight every path before the first write.
    kit = root / 'kit'
    press_dir, letters, suggested = kit / 'press', kit / 'press' / 'letters', kit / 'press' / 'letters.suggested'
    for folder in (kit, press_dir, letters):
        _plain_folder(folder)
    target, record_path = letters / name, letters / '.written.json'
    record = _letter_record(record_path)
    edited = False
    if os.path.lexists(target):
        current = _regular_bytes(target)
        edited = current is None or _sha(current) != record.get(name)
    if edited:
        _plain_folder(suggested)
        target, record_path = suggested / name, suggested / '.written.json'
        record = _letter_record(record_path)
        if os.path.lexists(target):
            current = _regular_bytes(target)
            if current is None or _sha(current) != record.get(name):
                raise ValueError(f'kit/press/letters.suggested/{name} was edited or is not a regular file; '
                                 'move it aside to get a fresh suggestion')
    for folder in (kit, press_dir, letters) + ((suggested,) if edited else ()):
        _kit_folder(folder, 0o700)
    _write_text(target, text)
    record[name] = _sha(text.encode('utf-8'))
    _write_json(record_path, dict(sorted(record.items())))
    return {'path': target.relative_to(root).as_posix(), 'findings': ids[:kept], 'dropped_findings': ids[kept:],
            'words': count, 'word_limit': words, 'kept_edited': edited, 'sent': False, 'review_flags': flags}
