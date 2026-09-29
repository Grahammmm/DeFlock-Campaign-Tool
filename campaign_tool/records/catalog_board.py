"""Pure private metadata HTML; no I/O, original text, external assets or server.

All cards are rendered before progressive client-side pagination. No catalog JSON
is embedded: untrusted values appear only as escaped text, never script or URLs.
Serving this file still requires separate owner-only access controls.
"""
import base64
import hashlib
import html
import math
import re
from collections.abc import Mapping


_STYLE = """
:root{color-scheme:light;--ink:#173e3a;--paper:#f8f3e7;--line:#bdcdc1}
*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,#edf3e9,var(--paper));color:var(--ink);font:16px/1.5 Georgia,serif}
header,main,footer{max-width:1200px;margin:auto;padding:24px}h1{font-size:clamp(2rem,5vw,3rem);line-height:1.1;margin:10px 0}h2{font-size:1.4rem}h3{margin-top:0;font-size:1.15rem}
.notice{padding:12px 18px;border-left:4px solid #168073;background:#e1ecdf}.eyebrow,code,.total{font-family:monospace}.eyebrow{text-transform:uppercase;letter-spacing:.12em}.total{font-size:2rem}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}.card{min-width:0;background:#fffdf6;border:1px solid var(--line);border-top:3px solid #168073;padding:20px}
.fields{display:grid;grid-template-columns:1fr 1fr;gap:0 16px}dt{font-size:.85rem;font-weight:bold;color:#48665e}dd{margin:0 0 12px;white-space:pre-wrap;overflow-wrap:anywhere}
code,li{overflow-wrap:anywhere}ul{padding-left:20px;white-space:pre-wrap}summary{cursor:pointer;font-weight:bold}details{margin:15px 0}.controls,.pager{display:flex;flex-wrap:wrap;gap:12px;align-items:end;margin:18px 0}
label{display:flex;flex-direction:column;font-weight:bold;gap:5px}.search{flex:1;min-width:180px}input,select,button{font:inherit;padding:9px;max-width:100%;border:1px solid #729186;border-radius:3px;color:inherit;background:#fffdf6}
button{cursor:pointer;background:var(--ink);color:white}button:disabled{opacity:.45;cursor:default}:focus-visible{outline:3px solid #b56f2a;outline-offset:3px}
.counts{display:grid;grid-template-columns:1fr 1fr;gap:5px 16px}.counts dd{margin:0}footer{color:#48665e;font-size:.9rem}[hidden]{display:none!important}
@media(max-width:680px){header,main,footer{padding:18px}.grid,.fields{grid-template-columns:1fr}.controls label{width:100%}}
"""

_SCRIPT = """
(() => {
'use strict';
const cards=Array.from(document.querySelectorAll('article.card'));
const search=document.getElementById('search');
const filters=['agency','type','stage','analysis'].map(id=>document.getElementById(id));
const result=document.getElementById('result');
const previous=document.getElementById('previous'),next=document.getElementById('next');
const indexed=cards.map(card=>({card,text:card.textContent.toLocaleLowerCase(),
 agency:Array.from(card.querySelectorAll('[data-agency]'),n=>n.textContent),
 type:card.querySelector('[data-type]').textContent,
 stage:card.querySelector('[data-stage]').textContent,
 analysis:card.querySelector('[data-analysis]').textContent}));
let page=0; const size=50;
filters.forEach(select=>{
 Array.from(new Set(indexed.flatMap(item=>item[select.id]))).sort().forEach(value=>{
  const option=document.createElement('option');option.value=value;option.textContent=value;select.append(option);
 });
});
function draw(){
 const words=search.value.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
 const matching=indexed.filter(item=>words.every(word=>item.text.includes(word)) &&
  filters.every(select=>!select.value || (select.id==='agency'?item.agency.includes(select.value):item[select.id]===select.value)));
 const pages=Math.max(1,Math.ceil(matching.length/size));page=Math.min(page,pages-1);
 cards.forEach(card=>{card.hidden=true;});
 matching.slice(page*size,(page+1)*size).forEach(item=>{item.card.hidden=false;});
 result.textContent=matching.length+' matching of '+cards.length+' unique catalog identities. Showing '+(matching.length?page*size+1:0)+'-'+Math.min((page+1)*size,matching.length)+'.';
 document.getElementById('page-label').textContent='Page '+(page+1)+' of '+pages;
 previous.disabled=page===0;next.disabled=page+1>=pages;
}
[search,...filters].forEach(control=>control.addEventListener('input',()=>{page=0;draw();}));
document.getElementById('reset').addEventListener('click',()=>{[search,...filters].forEach(control=>{control.value='';});page=0;draw();});
previous.addEventListener('click',()=>{if(page>0){page--;draw();}});
next.addEventListener('click',()=>{page++;draw();});
document.getElementById('controls').hidden=false;document.getElementById('pager').hidden=false;draw();
})();
"""


def _text(value):
    return value if isinstance(value, str) and value.strip() else "Unknown"


def _escape(value):
    return html.escape(str(value), quote=True)


def _listing(value, attribute=""):
    items = [item for item in value if isinstance(item, str)] if isinstance(value, list) else []
    return "<ul>" + "".join(f"<li{attribute}>{_escape(item)}</li>" for item in (items or ["Unknown"])) + "</ul>"


def _field(label, value, attribute=""):
    return f"<div><dt>{label}</dt><dd{attribute}>{_escape(value)}</dd></div>"


def _counters(value, prefix="", depth=0):
    if not isinstance(value, Mapping) or depth > 3:
        return
    for key, count in value.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_ .:/-]{1,80}", key):
            continue
        label = f"{prefix} / {key}" if prefix else key
        if isinstance(count, Mapping):
            yield from _counters(count, label, depth+1)
        elif type(count) in (int, float) and count >= 0 and (type(count) is int or math.isfinite(count)):
            yield label, count


def _card(card, sha):
    dates = card.get("dates") if isinstance(card.get("dates"), Mapping) else {}
    size, score = card.get("bytes"), card.get("value_score")
    size = f"{size:,} bytes" if type(size) is int and size >= 0 else "Unknown"
    score = str(score) if type(score) is int else "Unknown"
    fields = "".join((
        _field("Type", _text(card.get("format")), " data-type"),
        _field("Size", size),
        _field("Catalog status", _text(card.get("catalog_status"))),
        _field("Extraction stage", _text(card.get("extraction_stage")), " data-stage"),
        _field("Analysis state", _text(card.get("analysis_state")), " data-analysis"),
        _field("Role", _text(card.get("role"))),
        _field("Document date", _text(dates.get("document_date"))),
        _field("First seen (not document date)", _text(dates.get("first_seen"))),
        _field("Last seen (not document date)", _text(dates.get("last_seen"))),
        _field("Provisional value score", score),
    ))
    declarations = card.get("snapshot_declarations")
    declarations = declarations if isinstance(declarations, Mapping) else {}
    declaration_html = "".join(f"<dt>{_escape(k)}</dt><dd>{_escape(v)}</dd>"
                               for k, v in declarations.items() if isinstance(k, str) and isinstance(v, str))
    return (f'<article class="card" id="doc-{sha}" data-sha256="{sha}">'
            f'<h3>Original metadata</h3><strong>SHA-256</strong><br><code>{sha}</code>'
            '<p><strong>Agency hints</strong></p>' + _listing(card.get("agency_hints"), " data-agency")
            + f'<dl class="fields">{fields}</dl><strong>Score basis (provisional)</strong>'
            + _listing(card.get("value_score_basis")) + '<strong>Flags</strong>' + _listing(card.get("flags"))
            + '<details><summary>Private provenance metadata</summary><strong>Source paths (text only)</strong>'
            + _listing(card.get("source_paths")) + '<strong>Parent hashes</strong>' + _listing(card.get("parents"))
            + '<dl>' + (declaration_html or '<dt>Snapshot declarations</dt><dd>Unknown</dd>')
            + '</dl></details></article>')


def _hash(content):
    return base64.b64encode(hashlib.sha256(content.encode("utf-8")).digest()).decode("ascii")


def render_board(catalog) -> str:
    """Render schema v1 allowlisted metadata without modifying the input.

    Duplicate or invalid identities fail explicitly. Summary values are imported
    counters, not the authoritative card total. All records remain accessible
    without JavaScript; with JavaScript, 50 matching cards appear per page.
    """
    if not isinstance(catalog, Mapping) or type(catalog.get("schema_version")) is not int or catalog["schema_version"] != 1:
        raise ValueError("Expected catalog schema_version 1")
    cards = catalog.get("cards")
    if not isinstance(cards, list):
        raise ValueError("Catalog cards must be a list")
    rendered, identities = [], set()
    for card in cards:
        sha = card.get("sha256") if isinstance(card, Mapping) else None
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha):
            raise ValueError("Each card requires a SHA-256 identity")
        sha = sha.lower()
        if sha in identities:
            raise ValueError("Duplicate SHA-256 card identity")
        identities.add(sha)
        rendered.append(_card(card, sha))
    counters = "".join(f"<dt>{_escape(k)}</dt><dd>{_escape(v)}</dd>" for k, v in _counters(catalog.get("summary")))
    counters = counters or "<dt>Imported counters</dt><dd>Unavailable</dd>"
    roles = {}
    for card in cards:
        role = _text(card.get("role"))
        roles[role] = roles.get(role, 0) + 1
    role_counts = "".join(f"<dt>{_escape(role)}</dt><dd>{count}</dd>"
                          for role, count in sorted(roles.items()))
    policy = ("default-src 'none'; base-uri 'none'; form-action 'none'; "
              f"script-src 'sha256-{_hash(_SCRIPT)}'; style-src 'sha256-{_hash(_STYLE)}'")
    controls = "".join(f'<label for="{key}">{label}<select id="{key}"><option value="">All</option></select></label>'
                       for key, label in [("agency","Agency"),("type","Type"),("stage","Extraction"),("analysis","Analysis")])
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta name="referrer" content="no-referrer"><meta name="robots" content="noindex,nofollow,noarchive">'
            f'<meta http-equiv="Content-Security-Policy" content="{_escape(policy)}">'
            '<title>Private records catalog</title><style>' + _STYLE + '</style></head><body>'
            '<header><div class="eyebrow">Private workspace / Metadata only</div><h1>Records catalog</h1>'
            '<p class="notice">Private metadata. Keep this file owner-only. This static file does not authenticate visitors.</p>'
            '<p>Cataloged is not analyzed. Extracted is not reviewed. Scores are provisional, not findings or publication approval.</p>'
            f'<p>Snapshot: <code>{_escape(_text(catalog.get("snapshot_id")))}</code></p></header><main>'
            '<section aria-label="Coverage"><p>Unique catalog identities / exact card count<br>'
            f'<strong class="total" id="total-count">{len(cards)}</strong></p>'
            f'<h2>Declared document roles</h2><dl class="counts">{role_counts}</dl>'
            '<details><summary>Imported counters by stage</summary>'
            '<p>Independent source counters may overlap; they are not added to the card total.</p>'
            f'<dl class="counts">{counters}</dl></details></section>'
            '<section aria-labelledby="catalog-heading"><h2 id="catalog-heading">Every catalog identity, one card</h2>'
            '<noscript><p>JavaScript is off: all cards are shown. Use browser search to find any hash or metadata.</p></noscript>'
            '<div class="controls" id="controls" hidden><label class="search" for="search">Search metadata'
            '<input id="search" type="search" placeholder="Hash, agency, source path, date..."></label>' + controls
            + '<button type="button" id="reset">Clear filters</button></div>'
            f'<p id="result" role="status" aria-live="polite">{len(cards)} unique catalog identities. All cards shown.</p>'
            '<div class="pager" id="pager" hidden><button type="button" id="previous">Previous 50</button>'
            '<span id="page-label"></span><button type="button" id="next">Next 50</button></div>'
            '<div class="grid">' + "".join(rendered) + '</div></section></main>'
            '<footer>Private inventory only. No original document contents, receipt contents, publication actions or external resources.</footer>'
            '<script>' + _SCRIPT + '</script></body></html>')
