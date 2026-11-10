"""Read-only freshness check for private press drafts and letters (DF1622-06).

Never writes, regenerates, sends or publishes. Every folder and file is opened without following links, and a link,
FIFO or other special file is named, never read. An ``ok`` row means only the limited checks documented in
docs/PUBLICATION.md ("Check existing press drafts"), not independent factual or legal review.
"""
import errno
import json
import os
import re
import stat
from datetime import date, datetime, timezone

from .content import FINDING_ID, SLUG, ContentError, load_finding, load_meeting, utc_moment

MAX_MARKDOWN_BYTES = 1 << 20
MAX_JSON_BYTES = 2 * 1024 * 1024
# (folder parts, kind, is_letter) in the frozen inspection order.
FOLDERS = ((('kit', 'press'), 'draft', False), (('kit', 'press.suggested'), 'draft', False),
           (('kit', 'press', 'letters'), 'letter', True), (('kit', 'press', 'letters.suggested'), 'letter', True))
_ID = FINDING_ID.pattern
_HEADER = re.compile(r'<!-- DRAFT: not sent\. Review before any use\. findings (' + _ID + r'(?:, ?' + _ID + r')*) -->')
_NAME_DATE = re.compile(r'(\d{4}-\d{2}-\d{2})-')
_MEETING_CLOSING = re.compile(r'I encourage readers to review the next .+ meeting on \S+: '
                              r'(?:https://\S*?)?/meetings/([a-z0-9][a-z0-9-]*)\.html\.')
_SIGNUP_CLOSING = re.compile(r'I encourage readers to visit the campaign signup page: \S*/#participate\.')
_CONTROLS = re.compile('[\\x00-\\x1f\\x7f-\\x9f\\u2028\\u2029]')
STATUS_WORDS = {'stale': 'stale', 'not_checked': 'not checked', 'not_regular': 'not regular', 'unreadable': 'unreadable'}
_NOT_CHECKED = {'header': 'missing or invalid draft header', 'filename': 'missing or invalid filename date',
                'closing': 'unrecognized letter closing', 'findings': 'finding index unavailable',
                'finding_file': 'finding file invalid or duplicated', 'meeting': 'meeting file invalid or duplicated'}


class _Hazard(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def _hazard_reason(hazard):
    return [{'code': hazard.status, 'message': hazard.message}]


def _open_dir(root_fd, parts):
    """(fd, None) for a real folder; (None, None) when absent; (None, (index, hazard)) for a link/special/unreadable one."""
    fd = os.dup(root_fd)
    for index, part in enumerate(parts):
        try:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
        except OSError as exc:
            os.close(fd)
            if exc.errno == errno.ENOENT:
                return None, None
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                return None, (index, _Hazard('not_regular', 'not a regular folder (link or special file; not followed)'))
            return None, (index, _Hazard('unreadable', 'cannot be opened'))
        os.close(fd)
        fd = nxt
    return fd, None


def _read(dir_fd, name, limit):
    """Bytes of a regular file opened without following links; _Hazard for anything else."""
    try:
        before = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        raise _Hazard('unreadable', 'cannot be read') from None
    if not stat.S_ISREG(before.st_mode):
        raise _Hazard('not_regular', 'not a regular file (link or special file; not followed)')
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENXIO):
            raise _Hazard('not_regular', 'not a regular file (link or special file; not followed)') from None
        raise _Hazard('unreadable', 'cannot be read') from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _Hazard('not_regular', 'not a regular file (link or special file; not followed)')
        chunks, total = [], 0
        while total <= limit:
            try:
                chunk = os.read(fd, limit + 1 - total)
            except OSError:
                raise _Hazard('unreadable', 'cannot be read') from None
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    finally:
        os.close(fd)
    if total > limit:
        raise _Hazard('unreadable', 'file is too large')
    return b''.join(chunks)


def _names(fd, suffix):
    try:
        return sorted(name for name in os.listdir(fd) if name.endswith(suffix))
    except OSError:
        raise _Hazard('unreadable', 'folder cannot be listed') from None


def _row(path, kind, status, findings=(), reasons=()):
    return {'path': path, 'kind': kind, 'status': status, 'findings': list(findings), 'reasons': list(reasons)}


class _Index:
    """Current findings and meetings, read lazily and only as far as the drafts need."""

    def __init__(self, root_fd):
        self.root_fd, self.rows = root_fd, []
        self._findings = self._meetings = None

    def _folder(self, parts, kind='input'):
        fd, bad = _open_dir(self.root_fd, parts)
        if bad:
            path = '/'.join(parts[:bad[0] + 1])
            if not any(row['path'] == path for row in self.rows):
                self.rows.append(_row(path, kind, bad[1].status, reasons=_hazard_reason(bad[1])))
            return None, False
        return fd, True

    def _json(self, fd, name, prefix):
        """(document, None) or (None, True) after naming the hazard; the file is never quoted."""
        path = f'{prefix}/{name}'
        try:
            data = _read(fd, name, MAX_JSON_BYTES)
            return json.loads(data.decode('utf-8')), False
        except _Hazard as hazard:
            self.rows.append(_row(path, 'input', hazard.status, reasons=_hazard_reason(hazard)))
        except (UnicodeDecodeError, ValueError, RecursionError):
            self._invalid(path, 'file is not valid UTF-8 JSON')
        return None, True

    def _invalid(self, path, message):
        self.rows.append(_row(path, 'input', 'unreadable', reasons=[{'code': 'invalid_input', 'message': message}]))

    def _duplicates(self, seen, prefix, skip=()):
        dup = {ident for ident, names in seen.items() if len(names) > 1}
        for ident in sorted(dup):
            for name in seen[ident]:
                if name in skip:
                    continue  # already reported once as failing its own checks
                self._invalid(f'{prefix}/{name}', 'duplicate id')
        return dup

    def findings(self):
        if self._findings is not None:
            return self._findings
        state = {'ok': True, 'withdrawn': set(), 'published': {}, 'unpublished': set(), 'uncertain': set()}
        seen = {}
        fd, listed = self._folder(('content', 'findings'))
        if not listed:
            state['ok'] = False
        elif fd is not None:
            try:
                for name in _names(fd, '.json'):
                    document, failed = self._json(fd, name, 'content/findings')
                    if failed:
                        state['ok'] = False
                        continue
                    ident = document.get('id') if isinstance(document, dict) else None
                    if not isinstance(ident, str) or not FINDING_ID.fullmatch(ident):
                        self._invalid(f'content/findings/{name}', 'missing or invalid finding id')
                        state['ok'] = False
                        continue
                    seen.setdefault(ident, []).append(name)
                    if document.get('state') != 'published':
                        if isinstance(document.get('state'), str):
                            state['unpublished'].add(ident)
                            continue
                        self._invalid(f'content/findings/{name}', 'missing or invalid finding state')
                        state['uncertain'].add(ident)
                        continue
                    try:
                        state['published'][ident] = load_finding(document, f'findings/{name}')['corrections']
                    except (ContentError, KeyError, TypeError, ValueError):
                        self._invalid(f'content/findings/{name}', 'finding fails the publish checks')
                        state['uncertain'].add(ident)
            except _Hazard as hazard:
                self.rows.append(_row('content/findings', 'input', hazard.status, reasons=_hazard_reason(hazard)))
                state['ok'] = False
            finally:
                os.close(fd)
        state['uncertain'] |= self._duplicates(seen, 'content/findings')
        seen = {}
        fd, listed = self._folder(('private', 'withdrawn'))
        if not listed:
            state['ok'] = False
        elif fd is not None:
            try:
                for name in _names(fd, '.json'):
                    document, failed = self._json(fd, name, 'private/withdrawn')
                    ident = document.get('id') if isinstance(document, dict) else None
                    if failed:
                        state['ok'] = False
                    elif not isinstance(ident, str) or not FINDING_ID.fullmatch(ident):
                        self._invalid(f'private/withdrawn/{name}', 'missing or invalid finding id')
                        state['ok'] = False
                    else:
                        seen.setdefault(ident, []).append(name)
                        state['withdrawn'].add(ident)
            except _Hazard as hazard:
                self.rows.append(_row('private/withdrawn', 'input', hazard.status, reasons=_hazard_reason(hazard)))
                state['ok'] = False
            finally:
                os.close(fd)
        self._duplicates(seen, 'private/withdrawn')
        self._findings = state
        return state

    def meetings(self):
        """{'ok', 'by_id': {id: (status, starts_at)}, 'uncertain': ids}."""
        if self._meetings is not None:
            return self._meetings
        state = {'ok': True, 'by_id': {}, 'uncertain': set()}
        seen, failed_names = {}, set()
        fd, listed = self._folder(('content', 'meetings'))
        if not listed:
            state['ok'] = False
        elif fd is not None:
            try:
                for name in _names(fd, '.json'):
                    document, failed = self._json(fd, name, 'content/meetings')
                    if failed:
                        state['ok'] = False
                        continue
                    stem = name[:-len('.json')]
                    try:
                        meeting = load_meeting(document, f'meetings/{name}')
                    except (ContentError, KeyError, TypeError, ValueError):
                        self._invalid(f'content/meetings/{name}', 'meeting fails the staging checks')
                        state['uncertain'].add(stem)
                        failed_names.add(name)
                        # a malformed document still claims the identity it names, so it cannot be ignored
                        claim = document.get('id') if isinstance(document, dict) else None
                        if claim is None and isinstance(document, dict) and 'id' not in document:
                            claim = stem
                        if isinstance(claim, str) and SLUG.fullmatch(claim):
                            seen.setdefault(claim, []).append(name)
                            state['uncertain'].add(claim)
                        continue
                    ident = meeting['id'] or stem
                    seen.setdefault(ident, []).append(name)
                    state['by_id'][ident] = (meeting['status'], meeting['starts_at'])
            except _Hazard as hazard:
                self.rows.append(_row('content/meetings', 'input', hazard.status, reasons=_hazard_reason(hazard)))
                state['ok'] = False
            finally:
                os.close(fd)
        state['uncertain'] |= self._duplicates(seen, 'content/meetings', failed_names)
        self._meetings = state
        return state


def _finding_reasons(ident, index, day):
    state = index.findings()
    if ident in state['withdrawn']:
        return [{'code': 'withdrawn', 'finding_id': ident}], True
    if not state['ok']:
        return [{'code': 'not_checked', 'message': _NOT_CHECKED['findings']}], False
    if ident in state['uncertain']:
        return [{'code': 'not_checked', 'message': _NOT_CHECKED['finding_file']}], False
    if ident not in state['published']:
        return [{'code': 'unpublished', 'finding_id': ident}], True
    if day is None:
        return [], False
    later = sorted({c['date'] for c in state['published'][ident] if c['date'] > day})
    return ([{'code': 'corrected', 'finding_id': ident, 'dates': later}], True) if later else ([], False)


def _meeting_reasons(text, index, now):
    closings = [m for m in (_MEETING_CLOSING.fullmatch(line.rstrip('\r')) for line in text.split('\n')) if m]
    signup = [line for line in text.split('\n') if _SIGNUP_CLOSING.fullmatch(line.rstrip('\r'))]
    if len(closings) + len(signup) != 1:
        return [{'code': 'not_checked', 'message': _NOT_CHECKED['closing']}], False
    if signup:
        return [], False
    ident = closings[0].group(1)
    state = index.meetings()
    if ident in state['by_id'] and ident not in state['uncertain']:
        status, starts_at = state['by_id'][ident]
        reasons = []
        if status == 'cancelled':
            reasons.append({'code': 'meeting_cancelled', 'meeting_id': ident})
        if utc_moment(starts_at) < now:
            reasons.append({'code': 'meeting_past', 'meeting_id': ident})
        return reasons, bool(reasons)
    if not state['ok'] or ident in state['uncertain']:
        return [{'code': 'not_checked', 'message': _NOT_CHECKED['meeting']}], False
    return [{'code': 'meeting_missing', 'meeting_id': ident}], True


def _check_file(fd, name, path, kind, is_letter, index, now):
    try:
        data = _read(fd, name, MAX_MARKDOWN_BYTES)
        text = data.decode('utf-8')
    except _Hazard as hazard:
        return _row(path, kind, hazard.status, reasons=_hazard_reason(hazard))
    except UnicodeDecodeError:
        return _row(path, kind, 'unreadable', reasons=[{'code': 'unreadable', 'message': 'file is not valid UTF-8'}])
    match = _HEADER.fullmatch(text.split('\n', 1)[0].rstrip('\r'))
    if not match:
        return _row(path, kind, 'not_checked', reasons=[{'code': 'not_checked', 'message': _NOT_CHECKED['header']}])
    ids = list(dict.fromkeys(re.split(r', ?', match.group(1))))
    reasons, stale = [], False
    day = None
    named = _NAME_DATE.match(name)
    if named:
        try:
            date.fromisoformat(named.group(1))
            day = named.group(1)
        except ValueError:
            pass
    if day is None:
        reasons.append({'code': 'not_checked', 'message': _NOT_CHECKED['filename']})
    for ident in ids:
        found, is_stale = _finding_reasons(ident, index, day)
        reasons += found
        stale = stale or is_stale
    if is_letter:
        found, is_stale = _meeting_reasons(text, index, now)
        reasons += found
        stale = stale or is_stale
    status = 'stale' if stale else ('not_checked' if reasons else 'ok')
    return _row(path, kind, status, ids, reasons)


def check_press(root, *, now=None):
    """Report on every draft and letter under the four fixed press folders; returns {'drafts': rows, 'problems': N}."""
    if now is None:
        now = datetime.now(timezone.utc)
    elif not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('now must be a timezone-aware datetime')
    root = os.fspath(root)
    if os.path.islink(root) or not os.path.isdir(root) or not os.path.isfile(os.path.join(root, 'campaign.json')):
        raise ValueError('not a campaign folder: campaign.json is missing')
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        index = _Index(root_fd)
        rows, reported = [], set()
        for parts, kind, is_letter in FOLDERS:
            fd, bad = _open_dir(root_fd, parts)
            if bad:
                path = '/'.join(parts[:bad[0] + 1])
                if path not in reported and not any(path.startswith(done + '/') for done in reported):
                    reported.add(path)
                    rows.append(_row(path, kind, bad[1].status, reasons=_hazard_reason(bad[1])))
                continue
            if fd is None:
                continue
            prefix = '/'.join(parts)
            try:
                for name in _names(fd, '.md'):
                    rows.append(_check_file(fd, name, f'{prefix}/{name}', kind, is_letter, index, now))
            except _Hazard as hazard:
                rows.append(_row(prefix, kind, hazard.status, reasons=_hazard_reason(hazard)))
            finally:
                os.close(fd)
        rows += sorted(index.rows, key=lambda row: row['path'])
    finally:
        os.close(root_fd)
    return {'drafts': rows, 'problems': sum(1 for row in rows if row['status'] != 'ok')}


def _shown(text):
    return _CONTROLS.sub(lambda m: '\\x%02x' % ord(m.group()) if ord(m.group()) < 256 else '\\u%04x' % ord(m.group()), text)


def _reason_text(reason):
    parts = [reason['code']]
    for key in ('finding_id', 'meeting_id'):
        if key in reason:
            parts.append(reason[key])
    if 'dates' in reason:
        parts.append('after-filename-date ' + ','.join(reason['dates']))
    text = ' '.join(parts)
    if 'message' in reason:
        text += ': ' + reason['message']
    return text.replace('not_checked: ', 'not checked: ', 1) if reason['code'] == 'not_checked' else text


def format_text(report):
    if not report['drafts']:
        return 'No private press drafts found.'
    lines = []
    for row in report['drafts']:
        path = _shown(row['path'])
        if row['status'] == 'ok':
            lines.append(f'{path}: ok')
            continue
        cited = f" (findings {', '.join(row['findings'])})" if row['findings'] else ''
        lines.append(f"{path}: {STATUS_WORDS[row['status']]}{cited}: " + '; '.join(_reason_text(r) for r in row['reasons']))
    return '\n'.join(lines)
