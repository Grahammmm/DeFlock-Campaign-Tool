"""Private, offline one-page campaign flyer (kit/flyer/flyer.html + flyer.txt); prints, sends and publishes nothing."""
import html
import os
import tempfile
from pathlib import Path

from .cli import read_config
from .content import _read_json, load_finding, load_site, utc_moment
from .kit import _kit_folder, _write_text
from .meeting_pack import _unlinked
from .qr import CSS as QR_CSS, svg  # QR_CSS is emitted only when a base URL puts QR codes on the page
from .site import CLASSIFICATION_LABELS, CONFIDENCE_LABELS


def _published(content, limit):
    """(findings, truncated): published findings only, newest published_at first; titles shown as text, never markup."""
    found = []
    for path in sorted((content / 'findings').glob('*.json')):
        document = _read_json(path)
        if document.get('state') != 'published':
            continue
        title = document.get('title')
        finding = load_finding({**document, 'title': html.escape(title) if isinstance(title, str) else title}, path.name)
        finding['title'] = title
        found.append(finding)
    found.sort(key=lambda f: (utc_moment(f['published_at']), f['slug']), reverse=True)
    return found[:limit], max(0, len(found) - limit)


def _logo(content, document):
    """The brand logo's source path and validated brand, or (None, brand) without one; a missing file is refused by path."""
    site = load_site(document)  # names every bad brand field before any file is looked at
    brand = site.get('brand', {})
    if 'logo' not in brand:
        return None, site
    assets = content / 'public-assets'
    parts = brand['logo'].split('/')
    source = assets.joinpath(*parts)
    for step in (assets, *(assets.joinpath(*parts[:n]) for n in range(1, len(parts) + 1))):
        if step.is_symlink():
            raise ValueError(f"logo {source.relative_to(content.parent)} is or sits under a symlink; use a regular file")
    if not source.is_file():
        raise ValueError(f"logo file missing: {source.relative_to(content.parent)}")
    return source, load_site(document, assets)


def _render(name, place, site, findings, truncated, logo_name):
    e = lambda value: html.escape(str(value), quote=True)
    base = (site.get('base_url') or '').rstrip('/')
    brand = site.get('brand', {})
    html_parts = ['<!doctype html><html lang="en"><meta charset="utf-8">',
                  f'<title>{e(name)}</title>',
                  '<style>@page { size: letter; margin: 0.5in } body { font: 12pt sans-serif; margin: 0 }',
                  'h1 { font-size: 26pt; margin: .2em 0 } h2 { font-size: 15pt; margin: .8em 0 .3em }',
                  'p { margin: .3em 0 } article { break-inside: avoid; margin: .5em 0 } img { max-height: 1in; width: auto }',
                  '.small { font-size: 9pt } a { overflow-wrap: anywhere }' + (QR_CSS if base else '') + '</style><body>']
    text = []
    if logo_name:
        size = brand.get('logo_size')
        dims = f' width="{size[0]}" height="{size[1]}"' if size else ''
        html_parts.append(f'<img src="{e(logo_name)}" alt="{e(brand["logo_alt"])}"{dims}>')
    html_parts += [f'<h1>{e(name)}</h1>', f'<p>{e(site["tagline"])}</p>', f'<p>{e(place)}</p>', '<h2>What we found</h2>']
    text += [name, site['tagline'], place, '', 'What we found']
    if not findings:
        html_parts.append('<p>Records are requested; nothing is published yet.</p>')
        text.append('Records are requested; nothing is published yet.')
    for finding in findings:
        label = f'{CLASSIFICATION_LABELS[finding["classification"]]}; {CONFIDENCE_LABELS[finding["confidence"]]}'
        html_parts.append(f'<article><strong>{e(finding["title"])}</strong><p>{e(label)}</p>')
        text += ['', finding['title'], label]
        if base:
            url = f'{base}/findings/{finding["slug"]}.html'
            html_parts.append(f'<p><a href="{e(url)}">{e(url)}</a>{svg(url)}</p>')
            text.append(url)
        html_parts.append('</article>')
    if truncated:
        more = f'{truncated} more on the site' + (f': {base}' if base else '')
        html_parts.append(f'<p>{e(more)}</p>')
        text += ['', more]
    if base:
        html_parts += ['<h2>Get involved</h2>', f'<p><a href="{e(base)}">{e(base)}</a>{svg(base)}</p>']
        text += ['', 'Get involved', base]
    html_parts.append(f'<p class="small">{e(site["not_legal_advice"])}</p></body></html>')
    text += ['', site['not_legal_advice']]
    return '\n'.join(html_parts), '\n'.join(text) + '\n'


def build(root, max_findings=3, *, replace=False):
    if type(max_findings) is not int or not 1 <= max_findings <= 6:
        raise ValueError("--max-findings must be 1..6")
    root = Path(root).absolute()
    content = root / 'content'
    _unlinked(root)
    _unlinked(content)
    if not content.is_dir():
        raise ValueError("content/ missing; run init/content first")
    cfg = read_config(root)
    logo, site = _logo(content, _read_json(content / 'site.json'))
    findings, truncated = _published(content, max_findings)
    logo_name = 'logo' + logo.suffix.lower() if logo else ''
    page, text = _render(cfg['name'], f'{cfg["county"]}, {cfg["state"]}', site, findings, truncated, logo_name)
    folder = root / 'kit' / 'flyer'
    names = ['flyer.html', 'flyer.txt', *([logo_name] if logo_name else [])]
    for path in (root / 'kit', folder, *(folder / name for name in names)):
        _unlinked(path)
    existing = [name for name in names if (folder / name).exists()]
    if existing and not replace:
        raise ValueError(f"flyer exists ({', '.join('kit/flyer/' + n for n in existing)}); use --replace to overwrite")
    for directory in (root / 'kit', folder):
        _kit_folder(directory, 0o700)
        os.chmod(directory, 0o700)
    for old in folder.glob('logo.*'):  # an older logo is removed so the folder holds only what the flyer uses
        if replace and old.name != logo_name and old.is_file() and not old.is_symlink():
            old.unlink()
    if logo:
        # Stage in a fresh exclusive 0600 file (never a predictable name, so a pre-existing .logo.tmp symlink, hardlink
        # or file is neither followed nor touched); publish only the complete logo, and remove only this call's staging.
        descriptor, name = tempfile.mkstemp(prefix='.logo.', suffix='.tmp', dir=folder)
        staging = Path(name)
        try:
            try:
                handle = os.fdopen(descriptor, 'wb')
            except BaseException:
                os.close(descriptor)
                raise
            with handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(logo.read_bytes())
            os.replace(staging, folder / logo_name)
        except BaseException:
            staging.unlink(missing_ok=True)
            raise
    _write_text(folder / 'flyer.html', page)
    _write_text(folder / 'flyer.txt', text)
    for name in names:
        os.chmod(folder / name, 0o600)
    return {'written': ['kit/flyer/' + name for name in names], 'findings': len(findings), 'truncated': truncated}


LOGO_SUFFIXES = ('.png', '.webp', '.svg')


def _shown_moment(value):
    """The supplied date, time and offset exactly as given (Z is shown as explicit UTC); never converted."""
    day, rest = value.split('T')
    return f'{day} {rest[:-1]} UTC' if rest.endswith('Z') else f'{day} {rest[:8]} (UTC{rest[8:]})'


def _render_event(name, place, site, event, logo_name):
    e = lambda value: html.escape(str(value), quote=True)
    base = (site.get('base_url') or '').rstrip('/')
    brand = site.get('brand', {})
    anchor = f'{base}/events.html#event-{event["id"]}'
    where = 'Online' if event.get('online') else event['venue']
    when = f'{_shown_moment(event["starts_at"])} to {_shown_moment(event["ends_at"])}'
    note = event.get('accessibility_note')
    parts = ['<!doctype html><html lang="en"><meta charset="utf-8">', f'<title>{e(event["title"])}</title>',
             '<style>@page { size: letter; margin: 0.5in } body { font: 12pt sans-serif; margin: 0 }',
             'h1 { font-size: 26pt; margin: .2em 0 } h2 { font-size: 15pt; margin: .8em 0 .3em }',
             'p { margin: .3em 0 } img { max-height: 1in; width: auto }',
             '.small { font-size: 9pt } .desc { white-space: pre-wrap } a { overflow-wrap: anywhere }</style><body>']
    if logo_name:
        size = brand.get('logo_size')
        dims = f' width="{size[0]}" height="{size[1]}"' if size else ''
        parts.append(f'<img src="{e(logo_name)}" alt="{e(brand["logo_alt"])}"{dims}>')
    parts += [f'<p>{e(name)}</p>', f'<h1>{e(event["title"])}</h1>', f'<p>{e(when)}</p>', f'<p>{e(where)}</p>']
    text = [name, '', event['title'], when, where]
    if note:
        parts.append(f'<p>Accessibility: {e(note)}</p>')
        text.append(f'Accessibility: {note}')
    parts += [f'<p class="desc">{e(event["description_md"])}</p>', f'<p><a href="{e(anchor)}">{e(anchor)}</a></p>',
              f'<p class="small">{e(site["tagline"])} {e(place)}</p>',
              f'<p class="small">{e(site["not_legal_advice"])}</p></body></html>']
    text += ['', event['description_md'], '', anchor, '', f'{site["tagline"]} {place}', site['not_legal_advice']]
    return '\n'.join(parts), '\n'.join(text) + '\n'


def build_event(root, event_id, today=None, *, replace=False):
    """Private kit/flyer/event-<id>.html + .txt for one upcoming event of content/events.json (start-based, UTC build day)."""
    from datetime import datetime, timezone
    from .content_events import build_cutoff, partition, read_events
    from .content import SLUG
    if type(event_id) is not str or not SLUG.fullmatch(event_id):
        raise ValueError("--event must be a lowercase event id of 1 to 64 letters, digits and hyphens starting with a letter")
    if today is None:
        today = datetime.now(timezone.utc).date()
    elif isinstance(today, datetime) or not hasattr(today, 'year') or not hasattr(today, 'day'):
        raise ValueError("today must be a date")
    root = Path(root).absolute()
    content = root / 'content'
    _unlinked(root)
    _unlinked(content)
    if not content.is_dir():
        raise ValueError("content/ missing; run init/content first")
    cfg = read_config(root)
    events = read_events(root)
    if not events:
        raise ValueError(f"event {event_id}: content/events.json has no events")
    event = next((ev for ev in events if ev['id'] == event_id), None)
    if event is None:
        raise ValueError(f"unknown event {event_id}: not in content/events.json")
    upcoming, _ = partition(events, build_cutoff(today))
    if event not in upcoming:
        raise ValueError(f"event {event_id} is past (it started before {today.isoformat()} 00:00 UTC); only upcoming events get a flyer")
    logo, site = _logo(content, _read_json(content / 'site.json'))
    stem = f'event-{event_id}'
    logo_name = f'{stem}-logo' + logo.suffix.lower() if logo else ''
    page, text = _render_event(cfg['name'], f'{cfg["county"]}, {cfg["state"]}', site, event, logo_name)
    folder = root / 'kit' / 'flyer'
    for path in (root / 'kit', folder):
        _unlinked(path)
    mine = [f'{stem}.html', f'{stem}.txt', *(f'{stem}-logo{s}' for s in LOGO_SUFFIXES)]
    for name in mine:
        _unlinked(folder / name)
    existing = [n for n in mine if os.path.lexists(folder / n)]
    if existing and not replace:
        raise ValueError(f"event flyer exists ({', '.join('kit/flyer/' + n for n in existing)}); use --replace to overwrite")
    for name in existing:
        if not (folder / name).is_file():
            raise ValueError(f"kit/flyer/{name} is not a regular file; remove it first")
    names = [f'{stem}.html', f'{stem}.txt', *([logo_name] if logo_name else [])]
    for directory in (root / 'kit', folder):
        _kit_folder(directory, 0o700)
        os.chmod(directory, 0o700)
    for old in existing:
        if old not in names:
            (folder / old).unlink()
    if logo:
        descriptor, name = tempfile.mkstemp(prefix=f'.{stem}-logo.', suffix='.tmp', dir=folder)  # exclusive, new regular file
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(logo.read_bytes())
            os.replace(temporary, folder / logo_name)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    _write_text(folder / f'{stem}.html', page)
    _write_text(folder / f'{stem}.txt', text)
    for name in names:
        os.chmod(folder / name, 0o600)
    return {'written': ['kit/flyer/' + name for name in names], 'event': event_id}
