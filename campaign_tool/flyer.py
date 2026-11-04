"""Private, offline one-page campaign flyer (kit/flyer/flyer.html + flyer.txt); prints, sends and publishes nothing."""
import html
import os
from pathlib import Path

from .cli import read_config
from .content import _read_json, load_finding, load_site, utc_moment
from .kit import _kit_folder, _write_text
from .meeting_pack import _unlinked
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
                  '.small { font-size: 9pt } a { overflow-wrap: anywhere }</style><body>']
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
            html_parts.append(f'<p><a href="{e(url)}">{e(url)}</a></p>')
            text.append(url)
        html_parts.append('</article>')
    if truncated:
        more = f'{truncated} more on the site' + (f': {base}' if base else '')
        html_parts.append(f'<p>{e(more)}</p>')
        text += ['', more]
    if base:
        html_parts += ['<h2>Get involved</h2>', f'<p><a href="{e(base)}">{e(base)}</a></p>']
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
        temporary = folder / '.logo.tmp'
        temporary.write_bytes(logo.read_bytes())
        os.replace(temporary, folder / logo_name)
    _write_text(folder / 'flyer.html', page)
    _write_text(folder / 'flyer.txt', text)
    for name in names:
        os.chmod(folder / name, 0o600)
    return {'written': ['kit/flyer/' + name for name in names], 'findings': len(findings), 'truncated': truncated}
