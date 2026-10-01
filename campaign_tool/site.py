"""Static campaign site generation.

``preview(config)`` renders the neutral one-page starter from campaign.json
alone (kept for campaigns without a ``content/`` directory). ``build_site(root)``
renders the multi-page site described in docs/SITE-CONTENT.md from the
validated content model, wiring the agency-card renderer and, when a map is
configured, the MapLibre controller. All generated JavaScript is self-hosted;
pages contain no inline scripts, and the emitted ``_headers`` file carries a
Content-Security-Policy that names only the hosts the content model declared.
"""
import json
import re
import shutil
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from xml.etree import ElementTree as ET

from . import agency_cards, map_controller
from .content import CITY_KINDS, ContentError, load_content
from .markdown_lite import render as markdown
from .shell import render

ASSET_EXTENSIONS = {".png", ".jpg", ".jpeg", ".svg", ".webp", ".ico", ".pdf"}
ASSET_MAX_BYTES = 5 * 1024 * 1024
VENDOR_MAX_BYTES = 4 * 1024 * 1024
GENERATED_TOP_LEVEL = {"index.html", "agencies.html", "sources.html", "meetings.html", "about.html",
                       "style.css", "_headers", "robots.txt", "sitemap.xml", "feed.xml",
                       "site-data.js", "agency-cards.js", "app.js", "findings", "data", "vendor"}
CLASSIFICATION_LABELS = {
    "documented_fact": "Documented fact", "apparent_conflict": "Apparent conflict",
    "confirmed_conflict": "Confirmed conflict", "information_gap": "Information gap",
    "redaction": "Redaction", "agency_assertion": "Agency assertion", "no_conflict": "No conflict found",
}
CONFIDENCE_LABELS = {"verified": "Verified against the record", "likely": "Likely; awaiting further records"}
KIND_LABELS = {"sheriff": "County sheriff", "police": "City police", "chp": "State highway patrol",
               "district_attorney": "District attorney", "county_board": "County board",
               "city_council": "City council", "other": "Other public body"}
EXTRA_CSS = """
/* Content-model pages */
.site-nav{display:flex;gap:1rem;flex-wrap:wrap;margin-top:.6rem}.site-nav a{color:#d8e7e3;text-decoration:none;font-size:.9rem}.site-nav a[aria-current=page]{color:#fff;text-decoration:underline}
.finding-list{list-style:none;padding:0;margin:0;display:grid;gap:1px;background:var(--line)}.finding-list li{background:var(--paper);padding:1.4rem}.finding-list h3{margin:.3rem 0 .5rem}.finding-list a{color:var(--ink)}
.finding-meta{display:flex;gap:.6rem;flex-wrap:wrap;font-size:.8rem;color:var(--muted)}.finding-meta .status-pill{color:var(--ink)}
.finding-body{max-width:760px}.finding-body h3{margin-top:2rem}
.source-table{list-style:none;padding:0;margin:0}.source-table li{padding:.9rem 0;border-bottom:1px solid var(--line);overflow-wrap:anywhere}.source-table code{font-size:.85rem;background:var(--paper);padding:.1rem .3rem}
.notice{border-left:3px solid var(--coral);background:var(--paper);padding:1rem 1.25rem;margin:1.5rem 0}
.agency-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:1rem}.agency-list article{background:#fff;padding:1.4rem;border-top:3px solid var(--coral)}.agency-list h3{margin:.4rem 0}.agency-list a{color:var(--ink)}
.meeting-list article{padding:1.4rem 0;border-bottom:1px solid var(--line)}.meeting-list time{font-weight:700}
.signup-form{max-width:480px}.signup-row{display:flex;gap:.5rem}.signup-row input{flex:1;padding:.75rem;font:inherit;border:1px solid var(--line)}.signup-row button{font:inherit;font-weight:700;background:var(--coral);color:#fff;border:0;padding:.75rem 1.1rem;cursor:pointer}
.city-panel-static{position:relative}.city-panel-static .city-picker{position:static;margin-bottom:1.5rem;box-shadow:none;padding:0}
.site-footer p{margin:0}.site-footer a{color:inherit}
"""


def preview(config):
    """Neutral one-page starter; explicit public allowlist, unrelated settings ignored."""
    name, county, state = (escape(config[key], quote=True)
                           for key in ("name", "county", "state"))
    return render({
        "schema_version": 1,
        "language": "en",
        "html": {
            "head": f'  <meta charset="utf-8">\n  <meta name="viewport" content="width=device-width, initial-scale=1">\n  <meta name="description" content="A local initiative for transparent ALPR policies and evidence-based public participation.">\n  <title>{name} | Local accountability</title>\n  <link rel="stylesheet" href="style.css">\n',
            "header": f'  <a class="skip-link" href="#records">Skip to our process</a>\n  <header class="topbar campaign-banner"><div class="banner-intro"><a class="brand" href="#top">{name}</a><h1>Local records. Public understanding.</h1><p>{county}, {state}</p></div><a class="banner-signup-link" href="#participate">Get involved</a></header>\n',
            "main": f'    <section class="section" id="records"><div class="section-heading"><div><p class="eyebrow">Evidence before conclusions</p><h2>Know how your community uses license plate readers.</h2></div><p>We are building a source-backed picture of policies, contracts and oversight in {county}. Questions deserve records, not assumptions.</p></div><div class="mission-grid"><article><span>01</span><h3>Request</h3><p>Ask for policies, agreements, sharing settings and audit records.</p></article><article><span>02</span><h3>Understand</h3><p>Preserve originals, compare applicable rules and independently challenge every material finding.</p></article><article><span>03</span><h3>Participate</h3><p>Use reviewed evidence and official meeting channels to ask informed questions.</p></article></div></section>\n    <section class="section movement-section" id="participate"><h2>A campaign built on evidence.</h2><p>This is a starter preview. Contacts, meetings, maps, newsletter signup and findings have not been configured.</p><p>No email addresses are collected by this preview.</p></section>\n',
            "footer": '  <footer class="site-footer"><p>Independent local initiative. No agency findings are asserted by this starter. Built with DeFlock Campaign Tool.</p></footer>\n'
        },
        "styles": {"font_family": "Georgia,serif", "font_faces": ""}
    })


STARTER_HEADERS = ("/*\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: no-referrer\n"
                   "  Content-Security-Policy: default-src 'none'; style-src 'self'; "
                   "base-uri 'none'; form-action 'none'; frame-ancestors 'none'\n")


def build_starter(cfg, public):
    """The pre-content starter: one page, stylesheet and headers. Unchanged behavior."""
    site = preview(cfg)
    public.mkdir(exist_ok=True)
    (public / "index.html").write_text(site["html"], encoding="utf-8")
    (public / "style.css").write_text(site["css"], encoding="utf-8")
    (public / "_headers").write_text(STARTER_HEADERS, encoding="utf-8")
    return [public / "index.html", public / "style.css", public / "_headers"]


# --- helpers ---------------------------------------------------------------

def e(value):
    return escape(str(value), quote=True)


def short_hash(sha256):
    return sha256[:12]


def date_label(value):
    return e(value[:10])


def js_json(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).replace("</", "<\\/")


class Site:
    def __init__(self, root, cfg, model):
        self.root = Path(root)
        self.cfg = cfg
        self.model = model
        self.site = model["site"]
        self.name = e(cfg["name"])
        self.county = e(cfg["county"])
        self.state = e(cfg["state"])
        self.base = (self.site["base_url"] or "").rstrip("/")
        self.agencies_by_id = {a["agency_id"]: a for a in model["agencies"]}
        self.fragment_for_agency = {}
        for source in model["sources"]:
            self.fragment_for_agency.setdefault(source["agency_id"], source["fragment"])
        self.fragment_for_sha = {}
        for source in model["sources"]:
            self.fragment_for_sha.setdefault(source["sha256"], source["fragment"])
        self.city_agencies = [a for a in model["agencies"] if a["kind"] in CITY_KINDS]
        self.map = model["map"]
        self.maplibre = "maplibre-gl.js" in model["vendor"]

    # -- shared frame ------------------------------------------------------

    def picker_cities(self):
        """Agencies shown in the picker: all city agencies, or only those with bounds when mapped."""
        if not self.map:
            return self.city_agencies
        return [a for a in self.city_agencies if a["slug"] in self.map["cities"]]

    def nav(self, current):
        items = [("/", "Home", "index"), ("/agencies.html", "Agencies", "agencies"),
                 ("/findings/", "Findings", "findings"), ("/sources.html", "Sources", "sources"),
                 ("/meetings.html", "Meetings", "meetings"), ("/about.html", "About", "about")]
        links = "".join(
            f'<a href="{href}"' + (' aria-current="page"' if key == current else "") + f'>{label}</a>'
            for href, label, key in items)
        return f'<nav class="site-nav" aria-label="Site">{links}</nav>'

    def header(self, current):
        cta = ('<a class="banner-signup-link" href="/#participate">' + e(self.site["signup"]["label"]) + '</a>'
               if self.site["signup"]["mode"] != "none" else
               '<a class="banner-signup-link" href="/meetings.html">Attend a meeting</a>')
        return ('  <a class="skip-link" href="#content">Skip to content</a>\n'
                '  <header class="topbar campaign-banner"><div class="banner-intro">'
                f'<a class="brand" href="/">{self.name}</a><h1>{e(self.site["tagline"])}</h1>'
                f'<p>{self.county}, {self.state}</p>{self.nav(current)}</div>{cta}</header>\n')

    def footer(self):
        parts = [f'<p>{self.name} is an independent local initiative. Built with DeFlock Campaign Tool.</p>']
        if self.site["contact_email"]:
            mail = e(self.site["contact_email"])
            parts.append(f'<p>Contact: <a href="mailto:{mail}">{mail}</a></p>')
        if self.site["social"]:
            links = " · ".join(f'<a href="{e(link["url"])}" rel="noopener noreferrer">{e(link["label"])}</a>'
                               for link in self.site["social"])
            parts.append(f'<p>{links}</p>')
        if self.map:
            parts.append(f'<p>{e(self.map["attribution"])}</p>')
        parts.append(f'<p>{e(self.site["not_legal_advice"])}</p>')
        parts.append('<p><a href="/feed.xml">Findings feed</a> · <a href="/sitemap.xml">Sitemap</a></p>')
        return '  <footer class="site-footer">' + "".join(parts) + '</footer>\n'

    def head(self, title, description, path, scripts=(), stylesheets=()):
        canonical = f'  <link rel="canonical" href="{e(self.base + path)}">\n' if self.base else ""
        extra = "".join(f'  <link rel="stylesheet" href="{href}">\n' for href in stylesheets)
        extra += "".join(f'  <script defer src="{src}"></script>\n' for src in scripts)
        return ('  <meta charset="utf-8">\n  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
                f'  <meta name="description" content="{e(description)}">\n  <title>{e(title)} | {self.name}</title>\n'
                '  <link rel="stylesheet" href="/style.css">\n'
                '  <link rel="alternate" type="application/atom+xml" title="Findings" href="/feed.xml">\n'
                + canonical + extra)

    def page(self, current, title, description, path, main, scripts=(), stylesheets=()):
        return render({
            "schema_version": 1,
            "language": self.site["language"],
            "html": {"head": self.head(title, description, path, scripts, stylesheets),
                     "header": self.header(current), "main": main, "footer": self.footer()},
            "styles": {"font_family": self.site["font_family"], "font_faces": ""},
        })

    # -- fragments -----------------------------------------------------------

    def finding_item(self, finding):
        return (f'<li><div class="finding-meta"><span class="status-pill">{e(CLASSIFICATION_LABELS[finding["classification"]])}</span>'
                f'<span>{e(CONFIDENCE_LABELS[finding["confidence"]])}</span><span>Event date {date_label(finding["event_date"])}</span></div>'
                f'<h3><a href="/findings/{e(finding["slug"])}.html">{e(finding["title"])}</a></h3><p>{e(finding["summary"])}</p></li>')

    def findings_list(self, findings, empty="No findings have been published yet."):
        if not findings:
            return f'<p>{empty}</p>'
        return '<ul class="finding-list">' + "".join(self.finding_item(f) for f in findings) + '</ul>'

    def meeting_item(self, meeting):
        agenda = (f' · <a href="{e(meeting["agenda_url"])}" rel="noopener noreferrer">Agenda</a>'
                  if meeting["agenda_url"] else "")
        item = f'<p>Agenda item: {e(meeting["agenda_item"])}</p>' if meeting["agenda_item"] else ""
        where = f' · {e(meeting["location"])}' if meeting["location"] else ""
        kit = ('<details><summary>Public comment kit</summary>' + markdown(meeting["comment_kit_md"]) + '</details>'
               if meeting["comment_kit_md"] else "")
        return (f'<article id="meeting-{e(meeting["id"])}"><time datetime="{e(meeting["starts_at"])}">{e(meeting["starts_at"].replace("T", " ")[:16])}</time>'
                f'{where}{agenda}<h3>{e(meeting["body"])}</h3>{item}{kit}</article>')

    def picker(self):
        buttons = ['<button type="button" class="city-choice is-active" id="reset-county-map" data-city="" aria-pressed="true">'
                   + self.county + '</button>']
        for agency in self.picker_cities():
            buttons.append(f'<button type="button" class="city-choice" data-city="{e(agency["slug"])}" aria-pressed="false">'
                           f'{e(agency["jurisdiction_name"] or agency["name"])}</button>')
        if self.map:
            slug = self.map["controller"]["group_slug"]
            buttons.append(f'<button type="button" class="city-choice" data-city="{e(slug)}" aria-pressed="false">'
                           f'{e(self.map["group_label"])}</button>')
        return '<div class="city-picker" role="group" aria-label="Choose an area">' + "".join(buttons) + '</div>'

    def card_panel(self):
        return ('<article class="city-panel" id="agency-profile" aria-live="polite"><div>'
                '<p class="eyebrow">Agency profile</p><h3 id="city-name"></h3>'
                '<p class="city-meta"><span class="status-pill" id="city-status"></span> <span class="city-vendor" id="city-vendor"></span> '
                '<span id="city-camera-count" hidden></span></p><p class="city-summary" id="city-summary"></p>'
                '<ul class="city-facts" id="city-facts"></ul><p><a class="text-link" id="city-records-link" href="sources.html#library"></a></p></div>'
                '<aside class="city-watch"><p class="eyebrow">Open questions</p><p id="city-open"></p>'
                '<div class="policy-flags" id="city-flags"></div></aside></article>')

    def map_section(self):
        if not self.map:
            return ""
        fallback = ("Interactive map unavailable: the campaign has not self-hosted the MapLibre library yet. "
                    if not self.maplibre else "Loading the interactive map. ")
        return ('    <section class="hero" id="map"><div class="hero-map-wrap">'
                '<div id="county-map" class="map" role="region" aria-label="Community-mapped ALPR points"></div>'
                f'<p class="map-fallback">{fallback}You can still choose an area below or '
                f'<a href="{e(self.map["controller"]["source_map_url"])}" rel="noopener noreferrer">open the DeFlock source map</a>.</p>'
                f'<div class="map-key county-map-badge"><div class="county-map-identity"><span class="county-map-monogram">{e(self.cfg["county"][:2].upper())}</span>'
                f'<div><span class="county-map-region">Community-mapped points</span><strong>{self.county}</strong></div></div>'
                f'<div class="county-map-legend"><span class="camera-dot"></span><span id="map-selection-count">{self.map["camera_count"]} mapped points</span></div>'
                '<p class="county-map-hint">Points are community reports, not confirmed agency ownership.</p></div>'
                f'<p class="map-credit">{e(self.map["attribution"])}</p>{self.picker()}</div></section>\n')

    def signup_section(self):
        signup = self.site["signup"]
        if signup["mode"] == "brevo_hosted":
            form = (f'<form class="signup-form" method="post" action="{e(signup["url"])}">'
                    '<div class="signup-row"><label class="visually-hidden" for="signup-email">Email address</label>'
                    '<input id="signup-email" type="email" name="EMAIL" autocomplete="email" required placeholder="you@example.org">'
                    f'<button type="submit">{e(signup["label"])}</button></div>'
                    f'<p class="form-note">{e(signup["note"])} Submitting opens the provider\'s hosted confirmation page.</p></form>')
        else:
            form = '<p>No email addresses are collected by this site. Follow meetings and findings here or by feed.</p>'
        return ('    <section class="section movement-section" id="participate"><h2>Participate with evidence.</h2>'
                f'{form}</section>\n')

    # -- pages ---------------------------------------------------------------

    def index_html(self):
        mission = markdown(self.site["mission_md"]) if self.site["mission_md"] else (
            '<div class="mission-grid"><article><span>01</span><h3>Request</h3><p>Ask for policies, agreements, sharing settings and audit records.</p></article>'
            '<article><span>02</span><h3>Understand</h3><p>Preserve originals, compare applicable rules and independently challenge every material finding.</p></article>'
            '<article><span>03</span><h3>Participate</h3><p>Use reviewed evidence and official meeting channels to ask informed questions.</p></article></div>')
        cards = ('    <section class="section cities-section" id="agencies"><div class="section-heading"><div><p class="eyebrow">Agencies</p>'
                 '<h2>What the records show so far.</h2></div><p>Choose an area to see its status, published facts and the source notes behind them. '
                 '<a href="/agencies.html">Full agency list</a>.</p></div>'
                 + ('' if self.map else '<div class="city-panel-static">' + self.picker() + '</div>')
                 + self.card_panel() + '</section>\n')
        upcoming = [m for m in self.model["meetings"]][:3]
        meetings = ('    <section class="section" id="meetings"><div class="section-heading"><div><p class="eyebrow">Meetings</p><h2>Show up informed.</h2></div>'
                    '<p><a href="/meetings.html">All meetings and comment kits</a></p></div><div class="meeting-list">'
                    + ("".join(self.meeting_item(m) for m in upcoming) or '<p>No meetings are listed yet.</p>') + '</div></section>\n')
        main = (self.map_section() + cards
                + '    <section class="section" id="mission"><div class="section-heading"><div><p class="eyebrow">Evidence before conclusions</p>'
                  f'<h2>{e(self.site["tagline"])}</h2></div></div>{mission}</section>\n'
                + '    <section class="section" id="findings"><div class="section-heading"><div><p class="eyebrow">Latest findings</p><h2>Published with sources.</h2></div>'
                  '<p><a href="/findings/">All findings</a> · <a href="/feed.xml">Atom feed</a></p></div>'
                + self.findings_list(self.model["findings"][:5]) + '</section>\n'
                + self.signup_section() + meetings)
        scripts = (["/vendor/maplibre-gl.js"] if self.map and self.maplibre else []) + ["/site-data.js", "/agency-cards.js", "/app.js"]
        stylesheets = ["/vendor/maplibre-gl.css"] if self.map and "maplibre-gl.css" in self.model["vendor"] else []
        return self.page("index", "Home", self.site["tagline"], "/", main, scripts, stylesheets)

    def agencies_html(self):
        articles = []
        for agency in self.model["agencies"]:
            findings = [f for f in self.model["findings"] if f["agency_id"] == agency["agency_id"]]
            sources = [s for s in self.model["sources"] if s["agency_id"] == agency["agency_id"]]
            links = []
            if agency["records_url"]:
                links.append(f'<a href="{e(agency["records_url"])}" rel="noopener noreferrer">Records request page</a>')
            if agency["portal"]["url"]:
                links.append(f'<a href="{e(agency["portal"]["url"])}" rel="noopener noreferrer">Portal ({e(agency["portal"]["vendor"])})</a>')
            if sources:
                links.append(f'<a href="/sources.html#{e(self.fragment_for_agency[agency["agency_id"]])}">{len(sources)} source record(s)</a>')
            body = (f'<p class="eyebrow">{e(KIND_LABELS[agency["kind"]])}</p><h3>{e(agency["name"])}</h3>'
                    f'<p>{e(agency["jurisdiction_name"])}</p><p><span class="status-pill">{e(agency["profile"]["status"])}</span></p>'
                    + (f'<p>{e(agency["profile"]["summary"])}</p>' if agency["profile"]["summary"] else "")
                    + (f'<p>{len(findings)} published finding(s): ' + ", ".join(
                        f'<a href="/findings/{e(f["slug"])}.html">{e(f["title"])}</a>' for f in findings) + '</p>' if findings else '<p>No published findings.</p>')
                    + (f'<p>{" · ".join(links)}</p>' if links else "")
                    + ('' if agency["verified"] else '<p class="archive-hint">Contact details unverified: confirm on the agency\'s official site.</p>'))
            articles.append(f'<article id="{e(agency["slug"])}">{body}</article>')
        main = ('    <section class="section" id="content"><div class="section-heading"><div><p class="eyebrow">Agencies</p>'
                f'<h2>Public bodies in {self.county}.</h2></div><p>Listing an agency here says only that we asked or intend to ask it for records. '
                'No agency is asserted to operate license plate readers unless a published finding says so with sources.</p></div>'
                + ('<div class="agency-list">' + "".join(articles) + '</div>' if articles else '<p>No agencies are configured yet.</p>')
                + '</section>\n')
        return self.page("agencies", "Agencies", "Public bodies covered by the campaign", "/agencies.html", main)

    def findings_index_html(self):
        main = ('    <section class="section" id="content"><div class="section-heading"><div><p class="eyebrow">Findings</p>'
                '<h2>Everything we publish cites a record.</h2></div><p>Each finding names its classification, confidence, event date and the '
                f'hashed source records behind it. <a href="/feed.xml">Atom feed</a>.</p></div>{self.findings_list(self.model["findings"])}'
                f'<p class="notice">{e(self.site["not_legal_advice"])}</p></section>\n')
        return self.page("findings", "Findings", "Published, source-linked findings", "/findings/", main)

    def finding_html(self, finding):
        sources = []
        for source in finding["sources"]:
            fragment = self.fragment_for_sha.get(source["sha256"])
            title = (f'<a href="/sources.html#{e(fragment)}">{e(source["title"])}</a>' if fragment else e(source["title"]))
            rule = f' · rule {e(source["rule_id"])}' if source["rule_id"] else ""
            sources.append(f'<li>{title} · <code>sha256 {e(short_hash(source["sha256"]))}</code> · {e(source["locator"])}{rule}</li>')
        corrections = ("".join(f'<li><time datetime="{e(c["date"])}">{e(c["date"])}</time>: {e(c["note"])}</li>' for c in finding["corrections"])
                       or '<li>No corrections have been issued.</li>')
        agency = self.agencies_by_id.get(finding["agency_id"])
        agency_line = (f'<p>Agency: <a href="/agencies.html#{e(agency["slug"])}">{e(agency["name"])}</a></p>' if agency else "")
        limits = ("".join(f'<li>{e(item)}</li>' for item in finding["limitations"]) or "<li>None stated.</li>")
        counter = ("".join(f'<li>{e(item)}</li>' for item in finding["counterevidence"]) or "<li>None identified at publication.</li>")
        updated = f' · Updated {date_label(finding["updated_at"])}' if finding["updated_at"] else ""
        main = ('    <article class="section finding-body" id="content">'
                f'<div class="finding-meta"><span class="status-pill">{e(CLASSIFICATION_LABELS[finding["classification"]])}</span>'
                f'<span>Confidence: {e(CONFIDENCE_LABELS[finding["confidence"]])}</span>'
                f'<span>Event date <time datetime="{e(finding["event_date"])}">{date_label(finding["event_date"])}</time></span>'
                f'<span>Published {date_label(finding["published_at"])}{updated}</span></div>'
                f'<h2>{e(finding["title"])}</h2><p><strong>{e(finding["summary"])}</strong></p>{agency_line}'
                + (markdown(finding["body_md"]) if finding["body_md"] else "")
                + f'<h3>Sources</h3><ul class="source-table">{"".join(sources)}</ul>'
                + f'<h3>Limitations</h3><ul>{limits}</ul><h3>Counterevidence considered</h3><ul>{counter}</ul>'
                + f'<h3 id="corrections">Corrections</h3><ul>{corrections}</ul>'
                + f'<p class="notice">{e(self.site["not_legal_advice"])}</p></article>\n')
        return self.page("findings", finding["title"], finding["summary"], f'/findings/{finding["slug"]}.html', main)

    def sources_html(self):
        groups = {}
        for source in self.model["sources"]:
            groups.setdefault(source["fragment"], []).append(source)
        sections = []
        for fragment, entries in groups.items():
            agency = self.agencies_by_id.get(entries[0]["agency_id"])
            heading = e(agency["name"]) if agency else e(entries[0]["agency_id"])
            items = []
            for source in entries:
                link = (f'<a href="{e(source["public_url"])}" rel="noopener noreferrer">Public copy</a>' if source["public_url"]
                        else "Original held in the private archive; not downloadable here")
                pages = f' · {source["pages"]} page(s)' if source["pages"] is not None else ""
                note = f' · {e(source["note"])}' if source["note"] else ""
                items.append(f'<li><strong>{e(source["title"])}</strong> · <code>sha256 {e(short_hash(source["sha256"]))}</code> · '
                             f'received {date_label(source["received_at"])}{pages} · {link}{note}</li>')
            sections.append(f'<section class="agency-document-group" id="{e(fragment)}"><h3>{heading}</h3><ul class="source-table">{"".join(items)}</ul></section>')
        main = ('    <section class="section" id="content"><div class="section-heading" id="library"><div><p class="eyebrow">Source library</p>'
                '<h2>Every record we cite, by hash.</h2></div><p>Each entry lists the SHA-256 prefix of the original bytes as received. '
                'Originals stay in the private archive unless a public copy is linked.</p></div>'
                + ("".join(sections) or '<p>No source records have been catalogued yet.</p>') + '</section>\n')
        return self.page("sources", "Sources", "Hashed source records behind published findings", "/sources.html", main)

    def meetings_html(self):
        main = ('    <section class="section" id="content"><div class="section-heading"><div><p class="eyebrow">Meetings</p>'
                '<h2>Official channels for informed questions.</h2></div><p>Comment kits summarize reviewed findings; they are not legal advice and do not ask anyone to flood a meeting.</p></div>'
                '<div class="meeting-list">' + ("".join(self.meeting_item(m) for m in self.model["meetings"]) or '<p>No meetings are listed yet.</p>')
                + '</div></section>\n')
        return self.page("meetings", "Meetings", "Upcoming public meetings and comment kits", "/meetings.html", main)

    def about_html(self):
        contact = (f'<p>Contact: <a href="mailto:{e(self.site["contact_email"])}">{e(self.site["contact_email"])}</a></p>'
                   if self.site["contact_email"] else "")
        main = ('    <section class="section finding-body" id="content"><p class="eyebrow">About</p>'
                f'<h2>{self.name}</h2>{markdown(self.site["about_md"])}{contact}'
                f'<p class="notice">{e(self.site["not_legal_advice"])}</p></section>\n')
        return self.page("about", "About", "About the campaign", "/about.html", main)

    # -- scripts and data ----------------------------------------------------

    def county_profile(self):
        profile = dict(self.site["county_profile"])
        profile["name"] = self.cfg["county"]
        if not profile["facts"]:
            count = len(self.model["findings"])
            profile["facts"] = [f"{count} published finding(s) with hashed sources." if count else
                                "No findings have been published yet; records requests are in progress."]
        return profile

    def cards_config(self):
        aliases = {}
        for agency in self.city_agencies:
            fragment = self.fragment_for_agency.get(agency["agency_id"])
            if fragment and fragment != agency["slug"]:
                aliases[agency["slug"]] = fragment
        return {"schema_version": 1, "county_profile": self.county_profile(), "source_page": "sources.html",
                "default_fragment": "library", "source_aliases": aliases,
                "source_link_label": self.site["source_link_label"]}

    def cities(self):
        cities = {}
        for agency in self.picker_cities():
            entry = {"name": agency["jurisdiction_name"] or agency["name"], "displayName": agency["name"]}
            if self.map:
                city = self.map["cities"][agency["slug"]]
                entry["bounds"] = city["bounds"]
                if city["name"]:
                    entry["name"] = city["name"]
            entry.update(agency["profile"])
            if not entry["facts"]:
                findings = [f for f in self.model["findings"] if f["agency_id"] == agency["agency_id"]]
                entry["facts"] = [f["title"] for f in findings] or ["No published findings for this agency yet."]
            cities[agency["slug"]] = entry
        if self.map:
            slug = self.map["controller"]["group_slug"]
            cities[slug] = {"name": self.map["group_label"], "displayName": self.map["group_label"],
                            "bounds": self.map["county_bounds"], "vendor": "Not applicable",
                            "status": "Ownership unconfirmed", "summary": self.map["group_label"]
                            + ". Points outside city boundaries without an operator tag are research leads, not evidence of agency ownership.",
                            "cameraCount": "", "facts": ["Community-mapped points; verify with records requests."],
                            "open": "Which agency, if any, operates these points is unconfirmed.", "flags": ["Unconfirmed"]}
        return cities

    def site_data_js(self):
        county_bounds = self.map["county_bounds"] if self.map else [[-180, -90], [180, 90]]
        style = self.map["style_url"] if self.map else ""
        return ("// Generated by campaign_tool build from content/. Reviewed campaign data only.\n"
                f"var CITIES={js_json(self.cities())};\nvar COUNTY_BOUNDS={js_json(county_bounds)};\n"
                f"var MAP_STYLE={js_json(style)};\nvar CAMERA_DATA_URL=\"/data/cameras.geojson\";\n"
                "var BOUNDARY_DATA_URL=\"/data/county.geojson\";\n")

    def app_js(self):
        if self.map:
            controller = map_controller.render(self.map["controller"], "")
            boot = ("document.addEventListener(\"DOMContentLoaded\",function(){"
                    "document.querySelectorAll(\".city-choice\").forEach(function(b){b.addEventListener(\"click\",function(){chooseCity(b.getAttribute(\"data-city\")||null);});});"
                    "chooseCity(null);initMap();});\n")
            return "// Generated by campaign_tool build. Map controller plus page bootstrap.\n" + controller + boot
        return ("// Generated by campaign_tool build. Agency picker without a map.\n"
                "function chooseCity(slug){document.querySelectorAll(\".city-choice\").forEach(function(b){"
                "var active=slug?b.getAttribute(\"data-city\")===slug:b.id===\"reset-county-map\";"
                "b.classList.toggle(\"is-active\",active);b.setAttribute(\"aria-pressed\",active?\"true\":\"false\");});setCityText(slug||null);}\n"
                "document.addEventListener(\"DOMContentLoaded\",function(){"
                "document.querySelectorAll(\".city-choice\").forEach(function(b){b.addEventListener(\"click\",function(){chooseCity(b.getAttribute(\"data-city\")||null);});});"
                "chooseCity(null);});\n")

    # -- metadata files --------------------------------------------------------

    def csp(self):
        connect = ["'self'"]
        img = ["'self'", "data:"]
        directives = ["default-src 'none'", "script-src 'self'", "style-src 'self'"]
        if self.map:
            hosts = ["https://" + host for host in self.map["tile_hosts"]]
            if self.map["style_host"]:
                connect.append("https://" + self.map["style_host"])
            connect.extend(host for host in hosts if host not in connect)
            img.append("blob:")
            img.extend(hosts)
        directives += ["img-src " + " ".join(img), "connect-src " + " ".join(connect), "font-src 'self'"]
        if self.map:
            directives.append("worker-src blob:")
        directives.append("base-uri 'none'")
        if self.site["signup"]["mode"] == "brevo_hosted":
            from urllib.parse import urlsplit
            parts = urlsplit(self.site["signup"]["url"])
            directives.append("form-action https://" + parts.hostname.lower())
        else:
            directives.append("form-action 'none'")
        directives.append("frame-ancestors 'none'")
        return "; ".join(directives)

    def headers(self):
        return ("/*\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: no-referrer\n"
                "  Permissions-Policy: camera=(), microphone=(), geolocation=()\n"
                "  Content-Security-Policy: " + self.csp() + "\n")

    def page_paths(self):
        paths = ["/", "/agencies.html", "/findings/", "/sources.html", "/meetings.html", "/about.html"]
        paths += [f'/findings/{f["slug"]}.html' for f in self.model["findings"]]
        return paths

    def sitemap_xml(self):
        root = ET.Element("urlset", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")
        for path in self.page_paths():
            url = ET.SubElement(root, "url")
            ET.SubElement(url, "loc").text = self.base + path
        return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"

    def feed_xml(self):
        ns = "http://www.w3.org/2005/Atom"
        ET.register_namespace("", ns)
        feed = ET.Element(f"{{{ns}}}feed")
        ET.SubElement(feed, f"{{{ns}}}title").text = self.cfg["name"] + " findings"
        ET.SubElement(feed, f"{{{ns}}}id").text = (self.base + "/feed.xml") if self.base else "urn:deflock-campaign:" + re.sub(r"[^a-z0-9]+", "-", self.cfg["name"].lower())
        ET.SubElement(feed, f"{{{ns}}}link", href=self.base + "/feed.xml", rel="self")
        ET.SubElement(feed, f"{{{ns}}}link", href=self.base + "/")
        latest = max((f["updated_at"] or f["published_at"] for f in self.model["findings"]), default=None)
        ET.SubElement(feed, f"{{{ns}}}updated").text = latest or datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat()
        ET.SubElement(ET.SubElement(feed, f"{{{ns}}}author"), f"{{{ns}}}name").text = self.cfg["name"]
        for finding in self.model["findings"]:
            entry = ET.SubElement(feed, f"{{{ns}}}entry")
            ET.SubElement(entry, f"{{{ns}}}title").text = finding["title"]
            ET.SubElement(entry, f"{{{ns}}}id").text = "urn:deflock-finding:" + finding["id"]
            ET.SubElement(entry, f"{{{ns}}}link", href=f'{self.base}/findings/{finding["slug"]}.html')
            ET.SubElement(entry, f"{{{ns}}}published").text = finding["published_at"]
            ET.SubElement(entry, f"{{{ns}}}updated").text = finding["updated_at"] or finding["published_at"]
            ET.SubElement(entry, f"{{{ns}}}summary").text = (
                f'[{CLASSIFICATION_LABELS[finding["classification"]]}; {CONFIDENCE_LABELS[finding["confidence"]]}; '
                f'event date {finding["event_date"]}] {finding["summary"]}')
            ET.SubElement(entry, f"{{{ns}}}category", term=finding["classification"])
        return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(feed, encoding="unicode") + "\n"

    def robots_txt(self):
        return "User-agent: *\nAllow: /\n" + (f"Sitemap: {self.base}/sitemap.xml\n" if self.base else "")


# --- file emission -----------------------------------------------------------

def _write(path, text, written):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    written.append(path)


def _copy(source, target, written, limit):
    if source.is_symlink():
        raise ContentError(f"{source}: symlinks are not copied")
    if source.stat().st_size > limit:
        raise ContentError(f"{source.name}: exceeds the {limit // (1024 * 1024)} MiB copy limit")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    written.append(target)


def copy_public_assets(model, public, written):
    assets_dir = model["assets_dir"]
    for source in model["public_assets"]:
        relative = source.relative_to(assets_dir)
        if source.suffix.lower() not in ASSET_EXTENSIONS:
            raise ContentError(f"public-assets/{relative}: extension not allowed "
                               f"({', '.join(sorted(ASSET_EXTENSIONS))})")
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}", part) for part in relative.parts):
            raise ContentError(f"public-assets/{relative}: use plain file and folder names")
        if relative.parts[0] in GENERATED_TOP_LEVEL:
            raise ContentError(f"public-assets/{relative}: collides with a generated path")
        _copy(source, public / relative, written, ASSET_MAX_BYTES)


def build_site(root):
    """Build ``<root>/public`` from ``<root>/content``; returns the list of written paths.

    Falls back to the neutral starter when ``content/`` does not exist.
    Raises ``ContentError``/``ValueError`` and writes nothing when content is
    not publishable.
    """
    from .cli import read_config
    root = Path(root)
    cfg = read_config(root)
    public = root / "public"
    if public.is_symlink():
        raise ContentError("public/ must not be a symlink")
    model = load_content(root)
    if model is None:
        return build_starter(cfg, public)
    site = Site(root, cfg, model)
    # Render everything before touching public/ so a refused page leaves no partial site.
    pages = {
        "index.html": site.index_html(), "agencies.html": site.agencies_html(),
        "findings/index.html": site.findings_index_html(), "sources.html": site.sources_html(),
        "meetings.html": site.meetings_html(), "about.html": site.about_html(),
    }
    for finding in model["findings"]:
        pages[f'findings/{finding["slug"]}.html'] = site.finding_html(finding)
    css = pages["index.html"]["css"] + EXTRA_CSS
    files = {name: page["html"] for name, page in pages.items()}
    files.update({
        "style.css": css, "_headers": site.headers(), "robots.txt": site.robots_txt(),
        "sitemap.xml": site.sitemap_xml(), "feed.xml": site.feed_xml(),
        "site-data.js": site.site_data_js(), "agency-cards.js": agency_cards.render(site.cards_config()),
        "app.js": site.app_js(),
    })
    if public.exists():
        shutil.rmtree(public)
    public.mkdir()
    written = []
    for name, text in files.items():
        _write(public / name, text, written)
    if model["map"]:
        _copy(model["map"]["cameras_path"], public / "data" / "cameras.geojson", written, 8 * 1024 * 1024)
        _copy(model["map"]["boundary_path"], public / model["map"]["controller"]["city_boundaries_url"], written, 8 * 1024 * 1024)
        if model["map"]["county_boundary_path"]:
            _copy(model["map"]["county_boundary_path"], public / "data" / "county.geojson", written, 8 * 1024 * 1024)
        for name, path in model["vendor"].items():
            _copy(path, public / "vendor" / name, written, VENDOR_MAX_BYTES)
    copy_public_assets(model, public, written)
    return written
