"""Static campaign site generation.

``preview(config)`` renders the neutral one-page starter from campaign.json
alone (kept for campaigns without a ``content/`` directory). ``build_site(root)``
renders the multi-page site described in docs/SITE-CONTENT.md from the
validated content model, wiring the agency-card renderer and, when a map is
configured, the MapLibre controller. All generated JavaScript is self-hosted;
pages contain no inline scripts, and the emitted ``_headers`` file carries a
Content-Security-Policy that names only the hosts the content model declared.
"""
import csv
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from html import escape
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from . import agency_cards, camera_counts, finding_law, finding_updates, image_meta, law, map_controller
from .content import (CHECKLIST_JURISDICTION, CITY_KINDS, FINDING_KEYS, PRE_MEETING_RULE_ID, MAX_GEOJSON_BYTES, ODBL, POINT_CHECK_STATUSES, TRANSPARENCY_FIELDS,
                      ContentError, _read_json, check_finding_agencies, continuation_back_links, continuation_targets,
                      finding_agency_ids, glossary_terms_used, load_content, utc_moment)
from .corrections_policy import policy_html
from .timeline import render_timeline, timeline_events
from .labels import CLASSIFICATION_LABELS, CONFIDENCE_LABELS, DEFAULTS, JS_DEFAULTS, KIND_LABELS, html_text
from .markdown_lite import render as markdown
from .privacy_patterns import refusal
from .shell import render
from .provenance_text import EXPLAINER, source_line
from .redirects import load_redirects
from .review import ROLES

from .supporters import KINDS as SUPPORTER_KINDS

ASSET_EXTENSIONS = {".png", ".jpg", ".jpeg", ".svg", ".webp", ".ico", ".pdf"}
ASSET_MAX_BYTES = 5 * 1024 * 1024
VENDOR_MAX_BYTES = 4 * 1024 * 1024
SOURCES_FILTER_ASSET = Path(__file__).parent / "assets" / "sources-filter.js.tmpl"  # DF1103-12
GENERATED_TOP_LEVEL = {"index.html", "agencies.html", "sources.html", "meetings.html", "about.html", "corrections.html",
                       "timeline.html", "404.html", "cameras.html", "style.css", "_headers", "robots.txt", "sitemap.xml", "feed.xml", "meetings.ics",
                       "site-data.js", "agency-cards.js", "app.js", "findings", "data", "vendor", "meetings", "agencies", "sources",
                       "_redirects", "methods.html", "privacy.html", "glossary.html", "faq.html", "search.html", "search.js", "supporters.html", "newsletters.html", "newsletters", "meetings.xml"}
ANALYTICS_NAMES = ("gtag", "analytics", "plausible", "matomo", "umami")  # a built script with one of these refuses privacy.html
# CLASSIFICATION_LABELS/CONFIDENCE_LABELS/KIND_LABELS (labels.py) stay English for feed.xml; HTML pages use Site.t().
# methods.html (DF2037-14) defines each label; keys must equal the label tables (checked at import and at render).
CLASSIFICATION_DEFINITIONS = {
    "documented_fact": "A record states this directly. We report what the record says, not our interpretation of it.",
    "apparent_conflict": "A record appears to differ from a stated rule or policy; not a finding that anyone broke a "
                         "law. The agency may have an explanation the records we hold do not show.",
    "confirmed_conflict": "The conflict was checked against the rule in force on the event date and passed legal-role "
                          "review. It describes a difference between records and a rule, not a court's judgment.",
    "information_gap": "Records we expected were not provided, not found, or do not answer the question asked. A gap is "
                       "not evidence that something did or did not happen.",
    "redaction": "Part of a record was withheld or blacked out by the agency. We note what was withheld and the reason "
                 "given, if any.",
    "agency_assertion": "The agency says this in a record or a reply. We report the statement; we have not "
                        "independently confirmed it.",
    "no_conflict": "We compared the records with the applicable rule or policy and found no difference. This covers "
                   "only the records we hold.",
}
CONFIDENCE_DEFINITIONS = {
    "verified": "Each statement was checked against the cited source records by a reviewer other than the author.",
    "likely": "The cited records support this reading, but records we have asked for or expect could change it.",
}


def check_definitions():
    """Raise ValueError when a definition table's keys differ from its label table (no silent missing entry)."""
    for name, labels, definitions in (("classification", CLASSIFICATION_LABELS, CLASSIFICATION_DEFINITIONS),
                                      ("confidence", CONFIDENCE_LABELS, CONFIDENCE_DEFINITIONS)):
        if set(labels) != set(definitions):
            raise ValueError(f"{name} definitions must match the label keys exactly: "
                             f"missing {sorted(set(labels) - set(definitions))}, extra {sorted(set(definitions) - set(labels))}")


check_definitions()
# findings/filter.js (RF1510-3): ES5, no dependency, same-origin under script-src 'self'. Without it the index is the
# full list. Hash state: #agency=<id>&classification=<code>&year=<yyyy>; unknown keys and values are ignored.
FILTER_JS = """// Generated by campaign_tool build. Findings index filter; the full list shows without JavaScript.
(function(){var form=document.getElementById("finding-filter");if(!form)return;
var keys=["agency","classification","year"],items=document.querySelectorAll(".finding-list li");
var count=document.getElementById("filter-count"),empty=document.getElementById("filter-empty");
function pick(k){return document.getElementById("filter-"+k);}
function apply(keep){var shown=0,parts=[],i,j,s,ok;
for(j=0;j<keys.length;j++){s=pick(keys[j]);if(s&&s.value!=="all")parts.push(keys[j]+"="+encodeURIComponent(s.value));}
for(i=0;i<items.length;i++){ok=true;for(j=0;j<keys.length;j++){s=pick(keys[j]);
if(s&&s.value!=="all"&&items[i].getAttribute("data-"+keys[j])!==s.value)ok=false;}items[i].hidden=!ok;if(ok)shown++;}
count.textContent="Showing "+shown+" of "+items.length;empty.hidden=shown>0;
if(keep!==true&&window.history&&history.replaceState)history.replaceState(null,"",parts.length?"#"+parts.join("&"):location.pathname+location.search);
return shown;}
function has(s,v){for(var i=0;i<s.options.length;i++)if(s.options[i].value===v)return true;return false;}
var pairs=location.hash.slice(1).split("&"),found=false;
for(var n=0;n<pairs.length;n++){var kv=pairs[n].split("="),v;if(keys.indexOf(kv[0])<0)continue;found=true;
try{v=decodeURIComponent(kv.slice(1).join("="));}catch(err){continue;}var s=pick(kv[0]);if(s&&v!=="all"&&has(s,v))s.value=v;}
form.addEventListener("change",function(){apply();});form.hidden=false;apply(!found);
window.findingFilter={apply:apply};})();
"""
# findings/cite.js (DF1422-14): ES5, no dependency, same-origin under script-src 'self'. Without it the button stays
# hidden and the citation text is plain page text. Only textContent is read or written; never innerHTML.
CITE_JS = """// Generated by campaign_tool build. Finding citation copy button; the citation shows as text without JavaScript.
(function(){var buttons=document.querySelectorAll(".cite-copy"),i;
function restore(b,label){setTimeout(function(){b.textContent=label;},2000);}
function select(b,p,label){try{var r=document.createRange(),s=window.getSelection();r.selectNodeContents(p);s.removeAllRanges();s.addRange(r);}catch(err){}
b.textContent=b.getAttribute("data-fallback");restore(b,label);}
function wire(b){var p=b.parentNode.querySelector(".cite-text"),label=b.textContent,c=window.navigator&&navigator.clipboard,done;
if(!p)return;b.hidden=false;
b.addEventListener("click",function(){
if(!c||typeof c.writeText!=="function"){select(b,p,label);return;}
try{done=c.writeText(p.textContent);}catch(err){select(b,p,label);return;}
if(!done||typeof done.then!=="function"){select(b,p,label);return;}
done.then(function(){b.textContent=b.getAttribute("data-copied");restore(b,label);},function(){select(b,p,label);});});}
for(i=0;i<buttons.length;i++)wire(buttons[i]);})();
"""
# DF0137-14: a relabelled site (a js.* label differs from its default) reads the count from LABELS in site-data.js,
# which the findings index then loads first; the text is set through textContent only.
FILTER_COUNT_JS = 'count.textContent="Showing "+shown+" of "+items.length;'


# DF1952-07: used only when a finding names extra agencies. A row with data-agencies matches the agency select by exact
# space-delimited token; a row without it keeps today's scalar data-agency comparison. Classification and year unchanged.
FILTER_PICK = 'function pick(k){return document.getElementById("filter-"+k);}\n'
FILTER_MATCH = ('function match(li,k,v){var m=k==="agency"?li.getAttribute("data-agencies"):null;'
                'if(m!==null)return(" "+m+" ").indexOf(" "+v+" ")>=0;return li.getAttribute("data-"+k)===v;}\n')
FILTER_COMPARE = 'items[i].getAttribute("data-"+keys[j])!==s.value'


def filter_js(relabel=False, multi=False):
    """findings/filter.js: today's FILTER_JS, or with ``relabel`` the count read from ``LABELS["js.filter_count"]``;
    ``multi`` (some finding names extra agencies) adds exact token membership for the agency select."""
    script = FILTER_JS
    if multi:
        script = script.replace(FILTER_PICK, FILTER_PICK + FILTER_MATCH).replace(FILTER_COMPARE, "!match(items[i],keys[j],s.value)")
    if not relabel:
        return script
    return script.replace(FILTER_COUNT_JS, 'count.textContent=String(LABELS["js.filter_count"]).replace(/\\{(shown|total)\\}/g,'
                             'function(m,k){return k==="shown"?shown:items.length;});')
# search.js (DF1207-15): ES5, no dependency, same-origin under script-src 'self'. Reads only the already-published
# data/findings.json and data/sources.json; every result is written with createElement/textContent. Fixed texts are
# the js.search_* labels: the defaults below, or LABELS from site-data.js when the page loads it first.
SEARCH_JS = r"""// Generated by campaign_tool build. Site search over the published data/findings.json and data/sources.json; same-origin, no logging.
(function(){var form=document.getElementById("search-form");if(!form)return;
var input=document.getElementById("q"),list=document.getElementById("search-results"),count=document.getElementById("search-count");
var fallback=document.getElementById("search-fallback"),message=document.getElementById("search-message");
var T=__LABELS__,MAX=50,docs=null,k;
if(typeof LABELS!=="undefined"&&LABELS)for(k in T)if(typeof LABELS[k]==="string")T[k]=LABELS[k];
var agencies={};try{agencies=JSON.parse(form.getAttribute("data-agencies")||"{}")||{};}catch(err){agencies={};}
function fold(s){s=String(s==null?"":s);try{s=s.normalize("NFKD");}catch(err){}return s.replace(/[\u0300-\u036f]/g,"").toLowerCase();}
function safe(u){return typeof u==="string"&&/^(\/(?!\/)|https?:\/\/)/.test(u)?u:"";}
function agency(id){var a=id&&agencies.hasOwnProperty(id)?agencies[id]:null;return a?a:["",""];}
function build(findings,sources){var out=[],i,j,f,s,hay;
for(i=0;i<findings.length;i++){f=findings[i];if(!f||typeof f!=="object")continue;hay=[f.title,f.summary,agency(f.agency_id)[0]];
for(j=0;j<(f.sources||[]).length;j++)if(f.sources[j])hay.push(f.sources[j].title);
out.push({kind:"finding",title:String(f.title||""),url:safe(f.url),text:fold(hay.join(" "))});}
for(i=0;i<sources.length;i++){s=sources[i];if(!s||typeof s!=="object")continue;
out.push({kind:"source",title:String(s.title||""),url:"/sources.html"+(agency(s.agency_id)[1]?"#"+agency(s.agency_id)[1]:""),text:fold([s.title,s.agency_name].join(" "))});}
return out;}
function say(el,text){el.textContent=text;el.hidden=!text;}
function fill(t,n){return t.replace(/\{count\}/g,function(){return String(n);});}
function show(q){var terms,i,j,hits=[],ok,li,a,tag;list.textContent="";say(count,"");say(message,"");
q=String(q).replace(/^\s+|\s+$/g,"");
if(q.length<2||q.length>100){say(message,T["js.search_hint"]);return 0;}
if(!docs)return 0;
terms=fold(q).split(/\s+/);
for(i=0;i<docs.length&&hits.length<MAX;i++){ok=true;for(j=0;j<terms.length;j++)if(docs[i].text.indexOf(terms[j])<0){ok=false;break;}if(ok)hits.push(docs[i]);}
if(!hits.length){say(message,T["js.search_none"]);return 0;}
for(i=0;i<hits.length;i++){li=document.createElement("li");a=document.createElement(hits[i].url?"a":"span");
if(hits[i].url)a.setAttribute("href",hits[i].url);a.textContent=hits[i].title;li.appendChild(a);
tag=document.createElement("span");tag.setAttribute("class","search-kind");tag.textContent=" "+T[hits[i].kind==="finding"?"js.search_finding":"js.search_source"];li.appendChild(tag);list.appendChild(li);}
say(count,fill(T[hits.length===1?"js.search_count_one":"js.search_count_many"],hits.length));return hits.length;}
function fromHash(){var parts=String(location.hash||"").slice(1).split("&"),n,kv,v="";
for(n=0;n<parts.length;n++){kv=parts[n].split("=");if(kv[0]!=="q")continue;try{v=decodeURIComponent(kv.slice(1).join("="));}catch(err){v="";}break;}
return v;}
function sync(){var v=fromHash();input.value=v;return show(v);}
function onInput(){var v=input.value;
if(window.history&&history.replaceState)history.replaceState(null,"",v?"#q="+encodeURIComponent(v):location.pathname+location.search);
show(v);}
function ready(){form.hidden=false;if(fallback)fallback.hidden=true;sync();}
function broken(){if(fallback)fallback.hidden=false;form.hidden=true;list.textContent="";say(count,"");say(message,T["js.search_unavailable"]);}
function load(url,key){return fetch(url).then(function(r){if(!r.ok)throw new Error("status");return r.json();}).then(function(d){
if(!d||typeof d!=="object"||!Array.isArray(d[key]))throw new Error("shape");return d[key];});}
form.addEventListener("submit",function(ev){if(ev&&ev.preventDefault)ev.preventDefault();onInput();});
input.addEventListener("input",onInput);
window.addEventListener("hashchange",function(){if(docs)sync();});
try{Promise.all([load("/data/findings.json","findings"),load("/data/sources.json","sources")]).then(function(r){
try{docs=build(r[0],r[1]);}catch(err){broken();return;}ready();},broken);}catch(err){broken();}
})();
"""
SEARCH_LABELS = {key: text for key, text in JS_DEFAULTS.items() if key.startswith("js.search_")}


def search_js():
    """search.js: the same bytes for every site; ``LABELS`` (site-data.js, when loaded) overrides the English defaults."""
    return SEARCH_JS.replace("__LABELS__", js_json(SEARCH_LABELS))
# data/findings.json emits only these keys, built field by field (DF2237-09); anything else refuses the build.
PUBLIC_FINDING_FIELDS = ("id", "slug", "title", "summary", "body_md", "classification", "confidence",
                         "event_date", "published_at", "updated_at", "agency_id", "sources", "limitations",
                         "counterevidence", "corrections", "url", "citation", "also_agency_ids", "agency_response")
PUBLIC_SOURCE_FIELDS = ("sha256_prefix", "title", "locator", "rule_id", "page", "quote")
PUBLIC_CORRECTION_FIELDS = ("date", "note")
PUBLIC_UPDATE_FIELDS = ("date", "note")  # DF0022-06: optional "updates" key of a finding; not a CSV column, so no-update bytes are unchanged
# DF1103-08: only a received, declined or no_response reply is public; pending never is.
PUBLIC_RESPONSE_FIELDS = ("status", "asked_on", "reply_by", "on", "text", "record_sha256_prefix")
PUBLIC_TOP_LEVEL_FIELDS = ("schema_version", "campaign", "generated_from", "base_url", "url_kind", "findings")
# data/sources.json (DF1822-14): the source library as open data; never note, fragment or a full sha256.
PUBLIC_LIBRARY_FIELDS = ("sha256_prefix", "title", "agency_id", "agency_name", "received_at", "public_url", "pages",
                         "cited_by", "public_copy")
PUBLIC_COPY_FIELDS = ("matches", "checked_on", "reason")
PUBLIC_LIBRARY_TOP_LEVEL = ("schema_version", "campaign", "generated_from", "base_url", "url_kind", "sources")
PUBLIC_MEETING_TOP_LEVEL = {"schema_version", "generated_at", "meetings"}  # DF0137-09: meetings open data
PUBLIC_MEETING_FIELDS = ("id", "body", "agency_id", "starts_at", "status", "agenda_url", "url", "outcome")
PUBLIC_OUTCOME_FIELDS = ("result", "on", "source_url", "vote", "votes", "minutes_url", "minutes_approved_on",
                         "continued_to", "continued_to_date", "direction", "return_by",
                         "direction_source")  # never verified_by / verified_at
PUBLIC_VOTE_FIELDS = ("name", "vote")
# DF1207-14: data/findings.csv and data/sources.csv are flattened from the built public JSON documents, so no CSV column
# can exist that the JSON does not carry; "sources", "corrections" and "public_copy" are the only nested values.
FINDINGS_CSV_COLUMNS = PUBLIC_FINDING_FIELDS
SOURCES_CSV_COLUMNS = tuple(c for key in PUBLIC_LIBRARY_FIELDS
                            for c in ((f"public_copy_{k}" for k in PUBLIC_COPY_FIELDS) if key == "public_copy" else (key,)))
# data/agencies.json (DF1422-12): what each agency card and page already show; never contacts, kit data or notes.
PUBLIC_AGENCY_TOP_LEVEL = {"schema_version", "campaign", "generated_from", "base_url", "url_kind", "agencies"}
PUBLIC_AGENCY_FIELDS = ("agency_id", "name", "kind", "url", "asked", "alpr_policy", "contract", "transparency", "camera_count")
PUBLIC_AGENCY_OPTIONAL = ("funding",)  # DF2207-02: present only when the organizer recorded it
PUBLIC_ASKED_FIELDS = ("topic", "state", "asked_on", "updated_on")
PUBLIC_POLICY_FIELDS = ("url", "checked_on")
PUBLIC_CONTRACT_FIELDS = ("vendor", "start", "end", "source_url", "checked_on")  # never renewal_note
PUBLIC_COST_FIELDS = ("amount_usd", "period", "as_stated_in")  # contract.cost (DF1103-07): exactly these, when present
PUBLIC_FUNDING_FIELDS = ("kind", "name", "as_stated_in", "source_url", "checked_on", "amount_usd", "period", "fiscal_year")  # DF2207-02
PUBLIC_TRANSPARENCY_FIELDS = ("cameras", "retention_days", "searches_last_30_days", "hotlist_alerts",
                              "shared_with_agencies", "source_url", "checked_on")
FULL_HASH = re.compile(r"[0-9a-fA-F]{40,}")
EXTRA_CSS = """
/* Content-model pages */
.site-nav{display:flex;gap:1rem;flex-wrap:wrap;margin-top:.6rem}.site-nav a{color:#d8e7e3;text-decoration:none;font-size:.9rem}.site-nav a[aria-current=page]{color:#fff;text-decoration:underline}
.finding-list{list-style:none;padding:0;margin:0;display:grid;gap:1px;background:var(--line)}.finding-list li{background:var(--paper);padding:1.4rem}.finding-list h3{margin:.3rem 0 .5rem}.finding-list a{color:var(--ink)}
.finding-meta{display:flex;gap:.6rem;flex-wrap:wrap;font-size:.8rem;color:var(--muted)}.finding-meta .status-pill{color:var(--ink)}
.finding-body{max-width:760px}.finding-body h3{margin-top:2rem}
.source-table{list-style:none;padding:0;margin:0}.source-table li{padding:.9rem 0;border-bottom:1px solid var(--line);overflow-wrap:anywhere}.source-table code{font-size:.85rem;background:var(--paper);padding:.1rem .3rem}
.notice{border-left:3px solid var(--coral);background:var(--paper);padding:1rem 1.25rem;margin:1.5rem 0}
.agency-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(280px,100%),1fr));gap:1rem}.agency-list article{background:#fff;padding:1.4rem;border-top:3px solid var(--coral)}.agency-list h3{margin:.4rem 0}.agency-list a{color:var(--ink)}
.meeting-list article{padding:1.4rem 0;border-bottom:1px solid var(--line)}.meeting-list time{font-weight:700}
.signup-form{max-width:480px}.signup-row{display:flex;gap:.5rem}.signup-row input{flex:1;padding:.75rem;font:inherit;border:1px solid var(--line)}.signup-row button{font:inherit;font-weight:700;background:var(--coral);color:#fff;border:0;padding:.75rem 1.1rem;cursor:pointer}
.city-panel-static{position:relative}.city-panel-static .city-picker{position:static;margin-bottom:1.5rem;box-shadow:none;padding:0}
.finding-filter{display:flex;gap:1rem;flex-wrap:wrap;align-items:end;margin:0 0 1.5rem}.finding-filter[hidden]{display:none}.finding-filter label{display:block;font-size:.8rem;color:var(--muted)}.finding-filter select{font:inherit;padding:.4rem}.filter-count{flex-basis:100%;margin:0;font-size:.9rem}
.site-search[hidden],#search-count[hidden],#search-message[hidden],#search-fallback[hidden]{display:none}.site-search input{font:inherit;padding:.4rem;min-width:min(24rem,100%)}.search-results{padding-left:1.2rem}.search-kind{font-size:.8rem;color:var(--muted)}
.sources-filter{display:flex;gap:1rem;flex-wrap:wrap;align-items:end;margin:0 0 1.5rem}.sources-filter[hidden]{display:none}.sources-filter label{display:block;font-size:.8rem;color:var(--muted)}.sources-filter select{font:inherit;padding:.4rem;min-height:44px}.sources-filter .sources-filter-check{display:flex;align-items:center;gap:.5rem;min-height:44px;font-size:.9rem;color:var(--ink)}.sources-filter-count{flex-basis:100%;margin:0;font-size:.9rem}
.site-nav a:focus-visible{outline:3px solid var(--mint);outline-offset:2px}
@media(max-width:850px){
.campaign-banner .site-nav{display:flex;flex-wrap:wrap;gap:.25rem;margin-top:.5rem}
.campaign-banner .site-nav a{display:inline-flex;align-items:center;min-height:44px;padding:0 .7rem;font-size:1rem}
.campaign-banner .site-nav a[aria-current=page]{background:#ffffff1f}
.agency-list{grid-template-columns:minmax(0,1fr)}
.finding-body,.agency-list article,.meeting-list article,.finding-list li{overflow-wrap:anywhere}
.finding-filter select,.signup-row input,.signup-row button{min-height:44px}
.finding-body img,.finding-body table{max-width:100%}.finding-body table{display:block;overflow-x:auto}
}
.site-footer p{margin:0}.site-footer a{color:inherit}
@media print{
/* Print: chrome removed, black on white, external link addresses after the text */
.skip-link,.site-nav,.banner-signup-link,.hero,.city-picker,.map-key,.map-credit,.signup-form,.signup-embed,iframe,dialog,.finding-filter,.filter-count,.filter-empty,.sources-filter,.sources-filter-empty{display:none!important}
.finding-list li[hidden]{display:list-item}
.source-table li.source-entry[hidden]{display:list-item}.agency-document-group[hidden]{display:block}
body,.topbar,.campaign-banner,.site-footer,.finding-list li,.notice,.source-table code{background:#fff!important;color:#000!important;box-shadow:none!important}
.section{padding:0 0 1rem;max-width:none}a{color:#000}
a[href^="http"]::after{content:" (" attr(href) ")";font-size:.85em;overflow-wrap:anywhere}
.finding-body h3,.source-table li,.meeting-list article,.agency-document-group{break-inside:avoid}h2,h3{break-after:avoid}
.packet-finding{break-inside:avoid-page}.packet-finding+.packet-finding{break-before:auto}
details::details-content{content-visibility:visible;display:block}details>summary{list-style:none}
}
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


def _one_line(text):
    return " ".join(str(text).split())


def finding_path(finding):
    """Existing root-relative route; the content loader validates the slug."""
    return f'/findings/{finding["slug"]}.html'


def citation(finding, campaign_name, base):
    """Plain-text citation of one published finding (DF1422-14); the page and data/findings.json carry this exact string.

    The URL is added only when ``base`` (the validated base_url, no trailing slash) is set; never a relative path.
    """
    corrected = max((c["date"] for c in finding["corrections"]), default=None)
    text = (f'{_one_line(campaign_name)}. "{_one_line(finding["title"])}" (finding {_one_line(finding["id"])}). '
            f'Published {finding["published_at"][:10]}')
    if corrected:
        text += f", corrected {corrected}"
    if base:
        text += f". {base}{finding_path(finding)}"
    return text + "."


def _preview_origin(base):
    """Only a validated root HTTPS origin can identify an absolute finding URL."""
    parts = urlsplit(base)
    return (parts.scheme == "https" and bool(parts.hostname)
            and parts.netloc.lower() == parts.hostname and parts.path in ("", "/")
            and not parts.query and not parts.fragment and "?" not in base and "#" not in base)


def short_hash(sha256):
    return sha256[:12]


def date_label(value):
    return e(value[:10])


def time_tag(value):
    return f'<time datetime="{e(value)}">{e(value)}</time>'


def policy_line(policy):  # links a posted ALPR usage policy an organizer found; fixed wording only
    if not policy:
        return ""
    return (f'<p class="policy-link"><a href="{e(policy["url"])}" rel="noopener noreferrer">Posted ALPR usage policy</a> '
            f'(checked {date_label(policy["checked_on"])}, <a href="{e(policy["source_url"])}" '
            'rel="noopener noreferrer">where we found it</a>)</p>')


def law_status(package):  # the law-package status label shown beside any reading of its rules
    return "reviewed on " + package["reviewed_at"] if package["status"] == "reviewed" else "draft, not legally reviewed"


ASKED_SENTENCES = {"awaiting_response": "No written response recorded as of{day}.", "answered": "Answered{day}.",
                   "acknowledged": "Acknowledged{day}; no records yet.", "partly_answered": "Partly answered{day}.",
                   "declined_in_writing": "Declined in writing{on}.", "withdrawn": "We withdrew this request{day}."}


def asked_sentence(item, today):
    """Plain-text state sentence for one asked item; awaiting with no update uses the build date."""
    when = item.get("updated_on") or (today.isoformat() if item["state"] == "awaiting_response" else "")
    day, on = (f" {date_label(when)}", f" on {date_label(when)}") if when else ("", "")
    return ASKED_SENTENCES[item["state"]].format(day=day, on=on)


ATOM_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")
OUTCOME_LABELS = {"approved": "Approved", "denied": "Denied", "continued": "Continued to a later meeting",
                  "amended": "Approved with amendments", "no_action": "No action taken",
                  "informational": "Informational item only"}
# English names, independent of the host locale (DF1822-08 deadline line).
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")


COPY_DIFFERS = {"redacted": "redacted copy", "converted": "converted copy", "unknown": "reason unknown"}
COPY_HOWTO = ('<p class="copy-howto" id="check-a-copy"><strong>How to check a copy:</strong> download the public copy and '
              'compute its SHA-256 with <code>sha256sum FILE</code> (Linux), <code>shasum -a 256 FILE</code> (macOS) or '
              '<code>Get-FileHash FILE</code> (Windows PowerShell). If the result starts with the record number, it is '
              'the same file.</p>')


def page_link(entry, page):
    """DF2037-04: 'Open page N of the public copy' (``#page=N`` on a PDF path without a fragment); "" without page."""
    if page is None or not entry or not entry.get("public_url"):
        return ""
    url, parts = entry["public_url"], urlsplit(entry["public_url"])
    if parts.path.lower().endswith(".pdf") and not parts.fragment and "#" not in url:
        return f'<p class="record-page"><a href="{e(f"{url}#page={page}")}" rel="noopener noreferrer">Open page {page} of the public copy</a></p>'
    return f'<p class="record-page"><a href="{e(url)}" rel="noopener noreferrer">Open the public copy (page {page})</a></p>'


def copy_status(source):
    """' · Public copy checked D: same file as this record number' (DF2037-05); "" when no check is recorded."""
    check = (source or {}).get("public_copy")
    if not check or not source.get("public_url"):
        return ""
    verdict = ("same file as this record number" if check["matches"]
               else f'differs ({COPY_DIFFERS[check["reason"]]})')
    return f' · <span class="copy-check">Public copy checked {time_tag(check["checked_on"])}: {verdict}</span>'


def meeting_outcome_html(meeting, t=None):
    """The organizer-verified outcome line (DF2237-11); "" when none was recorded. ``t`` is ``Site.t`` (labels);
    the optional official recording (DF1422-10) is a plain link, never embedded, fetched or rewritten."""
    outcome = meeting.get("outcome")
    if not outcome:
        return ""
    t = t or (lambda key, **values: html_text(DEFAULTS[key]).format(**values))
    vote = f' ({e(outcome["vote"])})' if outcome.get("vote") else ""
    recording = ""
    if outcome.get("recording_url"):
        start = f' {t("meeting.recording_from", start=e(outcome["recording_start"]))}' if outcome.get("recording_start") else ""
        recording = (f' · <a href="{e(outcome["recording_url"])}" rel="noopener noreferrer">{t("meeting.recording")}</a>'
                     + start)
    return (f'<p class="meeting-outcome">Outcome: {e(OUTCOME_LABELS[outcome["result"]])} on {time_tag(outcome["on"])}{vote}'
            f' · <a href="{e(outcome["source_url"])}" rel="noopener noreferrer">Official record</a>'
            f' · verified by {e(outcome["verified_by"])}{recording}</p>' + _minutes_html(outcome, t))


def _minutes_html(outcome, t):
    """DF2207-05: the organizer's record versus the body's approved minutes; a plain link, never fetched or embedded."""
    if outcome.get("minutes_url") and outcome.get("minutes_approved_on"):
        return (f'<p class="meeting-minutes"><a href="{e(outcome["minutes_url"])}" rel="noopener noreferrer">'
                f'{t("meeting.minutes_confirmed", date=e(outcome["minutes_approved_on"]))}</a></p>')
    return f'<p class="meeting-minutes">{t("meeting.minutes_pending")}</p>'


def _day(value):
    return datetime.strptime(value, "%Y-%m-%d").date().isoformat()


def finding_updated(finding):
    """Latest of updated_at, published_at, each correction date and each update date (a date read as midnight UTC), as
    the winner's own string; a tie keeps that order."""
    candidates = [finding["updated_at"], finding["published_at"]]
    candidates += [_day(c["date"]) + "T00:00:00+00:00" for c in finding["corrections"]]
    candidates += [_day(u["date"]) + "T00:00:00+00:00" for u in finding.get("updates", ())]
    return max((value for value in candidates if value), key=utc_moment)


def js_json(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).replace("</", "<\\/")


# Popup line for app.js: ``pointChecks`` comes from site-data.js; every shown text goes through the controller's mapText.
POINT_CHECK_RENDERER = (
    r'function(p){var m=/^https:\/\/www\.openstreetmap\.org\/node\/(\d+)$/.exec(String(p.url||""));'
    r'var c=m&&typeof pointChecks==="object"?pointChecks[m[1]]:null;var l=c&&LABELS[c.status];'
    r'return l?"<br>"+mapText(HEADING+": "+l+", "+c.checked_on):"";}')


def point_check_renderer(labels):
    """The trusted popup renderer for ``labels`` (raw label texts, escaped later by mapText)."""
    names = {status: labels["map.check_" + status] for status in POINT_CHECK_STATUSES}
    return ("(function(LABELS,HEADING){return " + POINT_CHECK_RENDERER + ";})("
            + js_json(names) + "," + js_json(labels["map.check_heading"]) + ")")


def ics_text(value):
    """RFC 5545 TEXT value: control characters (CR included) dropped, then \\ ; , and newline escaped."""
    value = re.sub(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]", "", str(value))
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def ics_fold(line):
    """Fold to at most 75 octets per line (CRLF + one space), never inside a UTF-8 sequence."""
    parts, chunk, size, limit = [], "", 0, 75
    for char in line:
        width = len(char.encode("utf-8"))
        if size + width > limit:
            parts.append(chunk)
            chunk, size, limit = "", 0, 74  # the leading space of a continuation line is one octet
        chunk += char
        size += width
    parts.append(chunk)
    return "\r\n ".join(parts)


class Site:
    def __init__(self, root, cfg, model, today=None):
        self.root = Path(root)
        self.cfg = cfg
        self.model = model
        self.today = today or date.today()  # build date: contract terms
        # Split on the meeting's own typed calendar date (no time-zone conversion); today counts as upcoming.
        # A cancelled meeting is never upcoming; it stays listed under past meetings.
        cutoff = (today or date.today()).isoformat()
        self.upcoming = [m for m in model["meetings"] if m["starts_at"][:10] >= cutoff and m["status"] != "cancelled"]
        self.past = [m for m in model["meetings"] if m["starts_at"][:10] < cutoff or m["status"] == "cancelled"][::-1]
        self.site = model["site"]
        self.license = self.site.get("license")  # DF1422-13: None without site.json.license; nothing is rendered then
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
        self.cited_by = {}  # sha256 -> published findings citing it, model order, each finding once
        for finding in model["findings"]:
            for sha in dict.fromkeys(s["sha256"] for s in finding["sources"]):
                self.cited_by.setdefault(sha, []).append(finding)
        # DF2207-06: published finding id -> staged meetings whose kit stored it as cited_findings, newest first.
        # Only ids of findings in the validated public model count; a withdrawn or unknown id lists nothing.
        public_ids = {f["id"] for f in model["findings"]}
        self.meetings_by_finding = {}
        for meeting in sorted(model["meetings"], key=lambda m: m["id"]):  # stable id order, then newest moment first
            for finding_id in meeting.get("cited_findings", ()):
                if finding_id in public_ids:
                    self.meetings_by_finding.setdefault(finding_id, []).append(meeting)
        for listed in self.meetings_by_finding.values():
            listed.sort(key=lambda m: utc_moment(m["starts_at"]), reverse=True)
        self.city_agencies = [a for a in model["agencies"] if a["kind"] in CITY_KINDS]
        self.map = model["map"]
        self.maplibre = "maplibre-gl.js" in model["vendor"]
        self.labels = model.get("labels") or DEFAULTS  # content/labels.json merged over the English defaults
        # DF0137-14: the js.* labels passed to the scripts, or None (today's English scripts) when none differs.
        self.js_labels = ({key: self.labels[key] for key in JS_DEFAULTS}
                          if any(self.labels[key] != text for key, text in JS_DEFAULTS.items()) else None)
        # DF1822-13: "/<relative>" -> (width, height) of each readable PNG/JPEG/WebP public asset, for markdown images.
        self.image_sizes = {}
        for source in model.get("public_assets", ()):
            if (source.suffix.lower() in image_meta.FORMATS and not source.is_symlink() and source.is_file()
                    and source.stat().st_size <= ASSET_MAX_BYTES):
                size = image_meta.image_size(source.read_bytes(), source.suffix)
                if size:
                    self.image_sizes["/" + source.relative_to(model["assets_dir"]).as_posix()] = size
        # Legal basis (DF1507-02): the package is read once, only when a finding cites a rule.
        self.rule_citations, self.law_status = {}, None
        # DF1952-01: validated organizer-transcribed local rules (all versions); the package is read only for ids
        # that are not local ones, so a local-only citation never touches the state package.
        self.local_rules = model.get("local_rules") or []
        local_ids = {rule["rule_id"] for rule in self.local_rules}
        if any(rule_id not in local_ids for f in model["findings"] for rule_id in finding_law.cited(f)):
            name = finding_law.jurisdiction(self.root)
            try:
                package = law.load_package(name) if name else None
            except (OSError, ValueError):
                package = None
            if package is not None:
                self.rule_citations = {rule["rule_id"]: rule["citation"] for rule in package["rules"]}
                self.law_status = law_status(package)
        # DF1207-06: the checklist's own package (never the campaign's) gives the label shown with a posted-policy reading.
        self.checklist_law_status = None
        if any(a.get("policy_check") for a in model["agencies"]):
            try:
                self.checklist_law_status = law_status(law.load_package(CHECKLIST_JURISDICTION))
            except (OSError, ValueError):
                pass
        # DF1207-07: the rule's citation and the package label come from the shipped package by rule id; absent -> no Rule line.
        self.pre_meeting_rule = None
        if any(a.get("pre_implementation_meeting") for a in model["agencies"]):
            try:
                package = law.load_package(CHECKLIST_JURISDICTION)
                rule = next((r for r in package["rules"] if r["rule_id"] == PRE_MEETING_RULE_ID), None)
                self.pre_meeting_rule = (rule["citation"], law_status(package)) if rule else None
            except (OSError, ValueError):
                pass
        # DF2237-08: agencies with a published finding or a sources.json entry get /agencies/<slug>.html.
        for finding in model["findings"]:  # DF1952-07: a preview model's extra agencies must be this campaign's agencies
            check_finding_agencies(finding, self.agencies_by_id, f'findings/{finding["slug"]}.json')
        self.multi_agency = any(f.get("also_agency_ids") for f in model["findings"])
        qualifying = ({aid for f in model["findings"] for aid in finding_agency_ids(f)}
                      | {s["agency_id"] for s in model["sources"]})
        self._agency_pages = {a["agency_id"]: f'/agencies/{a["slug"]}.html'
                              for a in model["agencies"] if a["agency_id"] in qualifying}
        self._record_pages = self._catalogue_record_pages(model["sources"])  # DF1103-11: refuses a shared 12-char prefix
        self.timeline = timeline_events(model, self.today, self._agency_pages)
        # DF1952-04: content/glossary.json; an absent file or an empty terms list switches every glossary output off.
        self.glossary = model.get("glossary") if (model.get("glossary") or {}).get("terms") else None
        self.faq = model.get("faq") or None  # DF0022-01: content/faq.json entries; an absent file switches every FAQ output off
        # DF1952-06: curated topics (slug order); each finding id -> the topics listing it. No topics key = none.
        self.topics = model.get("topics", [])
        self.supporters = model.get("supporters") or []  # DF2207-13: validated public rows; [] = no page, no link
        self.finding_by_id = {f["id"]: f for f in model["findings"]}
        self.topics_of = {}
        for topic in self.topics:
            for finding_id in topic["finding_ids"]:
                self.topics_of.setdefault(finding_id, []).append(topic)

    def agency_pages(self):
        """``{agency_id: "/agencies/<slug>.html"}`` for agencies with findings or sources, model order."""
        return self._agency_pages

    @staticmethod
    def _catalogue_record_pages(sources):
        """``{sha256: "/sources/<12-hex prefix>.html"}``; any two library entries sharing a prefix (a prefix
        collision, or one hash in two fragments) are refused before anything renders, naming the prefix and the
        library positions, never a full hash."""
        owners = {}
        for index, source in enumerate(sources):
            owners.setdefault(short_hash(source["sha256"]), []).append(index)
        for prefix, indices in owners.items():
            if len(indices) > 1:
                raise ContentError(f"sources library: entries {', '.join(map(str, indices))} share record number {prefix}; "
                                   f"each record page needs its own 12-character prefix, so consolidate the duplicates")
        return {source["sha256"]: f'/sources/{short_hash(source["sha256"])}.html' for source in sources}

    def record_pages(self):
        """``{sha256: "/sources/<12-hex prefix>.html"}`` for every library entry, library order (DF1103-11)."""
        return self._record_pages

    def record_html(self, source):
        """``sources/<prefix>.html``: one library entry's provenance, copy check and the published findings citing it.
        Reads only the validated model; never the original, the ledger or the kit."""
        prefix = short_hash(source["sha256"])
        agency = self.agencies_by_id.get(source["agency_id"])
        agency_page = self.agency_pages().get(source["agency_id"])
        agency_name = e(agency["name"]) if agency else e(source["agency_id"])
        agency_html = f'<a href="{e(agency_page)}">{agency_name}</a>' if agency_page else agency_name
        sentence, record = source_line(source, source, agency["name"] if agency else None)
        link = (f'<a href="{e(source["public_url"])}" rel="noopener noreferrer">{self.t("sources.public_copy")}</a>'
                if source["public_url"] else self.t("sources.private"))
        pages = f' · {self.t("sources.pages", count=source["pages"])}' if source["pages"] is not None else ""
        note = f' · {e(source["note"])}' if source["note"] else ""
        citing = self.cited_by.get(source["sha256"], [])
        cited = (f'<h3>{self.t("sources.cited_by")}</h3><ul class="record-cited">'
                 + "".join(f'<li><a href="/findings/{e(f["slug"])}.html">{e(f["title"])}</a></li>' for f in citing) + '</ul>'
                 if citing else f'<p>{self.t("sources.not_cited")}</p>')
        main = (f'    <article class="section finding-body" id="content"><p class="eyebrow">{self.t("eyebrow.sources")}</p>'
                f'<h2>{e(source["title"])}</h2><p>{sentence}</p>'
                f'<p>{agency_html} · {self.t("agency.received", date=time_tag(source["received_at"]))}</p>'
                f'<p class="record-no">{self.t("sources.record_no")} <code>{record}</code>{pages} · {link}{copy_status(source)}{note}</p>'
                f'<p class="provenance-note">{e(EXPLAINER)}</p>{COPY_HOWTO}{cited}'
                f'<p><a href="/sources.html#{e(source["fragment"])}">{self.t("agency.in_library")}</a></p></article>\n')
        return self.page("sources", source["title"], self.labels["sources.record_description"].format(record=prefix),
                         f"/sources/{prefix}.html", main)

    def findings_for(self, agency_id):
        """Published findings about ``agency_id`` (primary or an also_agency_ids member), model order, each once."""
        return [f for f in self.model["findings"] if agency_id in finding_agency_ids(f)]

    def agency_links(self, finding):
        """The finding's agencies as links (primary first, then extras as entered); unknown agencies are skipped."""
        return ", ".join(
            f'<a href="{e(self.agency_pages().get(a["agency_id"], "/agencies.html#" + a["slug"]))}">{e(a["name"])}</a>'
            for a in (self.agencies_by_id.get(aid) for aid in finding_agency_ids(finding)) if a)

    def t(self, key, **values):
        """Label ``key`` escaped once; ``values`` are HTML the page already escaped (a name, a <time> tag, a count)."""
        return html_text(self.labels[key]).format(**values)

    def csv_link(self, path):
        """A CSV link; with ``site.json.license`` its text also names the licence (the CSV bytes are unchanged)."""
        note = f' {self.t("license.csv_note", name=e(self.license["name"]))}' if self.license else ""
        return f'<a href="{path}" type="text/csv">{self.t("intro.open_data_csv")}{note}</a>'

    def license_about(self):
        """The about page's licence paragraphs: the chosen licence and, with a map, the fixed ODbL camera-points line.
        Empty (so the page is unchanged) without ``site.json.license``."""
        if not self.license:
            return ""
        chosen = f'<a href="{e(self.license["url"])}" rel="license">{e(self.license["name"])}</a>'
        if "attribution" in self.license:
            chosen += f', {e(self.license["attribution"])}'
        out = f'<p class="license">{self.t("license.about", license=chosen)}</p>'
        if self.map:
            osm = f'<a href="{e(ODBL["attribution_url"])}" rel="noopener noreferrer">OpenStreetMap contributors</a>'
            odbl = f'<a href="{e(ODBL["url"])}" rel="license">ODbL</a>'
            out += f'<p class="license">{self.t("license.about_camera", osm=osm, odbl=odbl)}</p>'
        return out

    def license_top(self):
        """Top-level keys the open-data JSON files may carry beyond their own allowlist (none without a licence)."""
        return ("license", "license_url") if self.license else ()

    def stamp_license(self, doc):
        """``doc`` plus top-level ``license`` (the id) and ``license_url`` when ``site.json.license`` is set."""
        return {**doc, "license": self.license["id"], "license_url": self.license["url"]} if self.license else doc

    def cameras_geojson(self):
        """``data/cameras.geojson`` (DF1422-13): the reviewed points plus ODbL and the OpenStreetMap attribution as GeoJSON
        foreign members, always, whatever the site licence is; the points are untouched."""
        self.mapped_counts()  # the copy's size and symlink checks, then the parsed document (self._map_docs[0])
        doc = {**self._map_docs[0], "license": ODBL["id"], "license_url": ODBL["url"],
               "attribution": ODBL["attribution"], "attribution_url": ODBL["attribution_url"]}
        return json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n"

    def md(self, text):
        """``markdown_lite`` with this site's public-asset image sizes (own-line ``![alt](/path)`` images render)."""
        return markdown(text, images=self.image_sizes) if self.image_sizes else markdown(text)

    # -- shared frame ------------------------------------------------------

    def picker_cities(self):
        """Agencies shown in the picker: all city agencies, or only those with bounds when mapped."""
        if not self.map:
            return self.city_agencies
        return [a for a in self.city_agencies if a["slug"] in self.map["cities"]]

    def nav(self, current):
        items = [("/", "home", "index"), ("/agencies.html", "agencies", "agencies"), ("/findings/", "findings", "findings"),
                 ("/sources.html", "sources", "sources"), ("/meetings.html", "meetings", "meetings"), ("/about.html", "about", "about")]
        links = "".join(
            f'<a href="{href}"' + (' aria-current="page"' if key == current else "") + f'>{self.t("nav." + label)}</a>'
            for href, label, key in items)
        links += "".join(  # content pages with nav: true (DF2237-07), after About, labelled by their own title
            f'<a href="/{page["slug"]}.html"' + (' aria-current="page"' if current == "page:" + page["slug"] else "")
            + f'>{e(page["title"])}</a>' for page in self.model.get("pages", []) if page["nav"])
        links += '<a href="/timeline.html"' + (' aria-current="page"' if current == "timeline" else "") + ">Timeline</a>"
        if self.topics:  # DF1952-06: after every fixed item
            links += ('<a href="/topics/"' + (' aria-current="page"' if current == "topics" else "")
                      + f'>{self.t("nav.topics")}</a>')
        return f'<nav class="site-nav" aria-label="{self.t("nav.label")}">{links}</nav>'

    def header(self, current):
        cta = ('<a class="banner-signup-link" href="/#participate">' + e(self.site["signup"]["label"]) + '</a>'
               if self.site["signup"]["mode"] != "none" else
               f'<a class="banner-signup-link" href="/meetings.html">{self.t("header.attend")}</a>')
        brand = self.site.get("brand", {})
        logo = (f'<img class="brand-logo" src="/{e(brand["logo"])}" alt="{e(brand["logo_alt"])}" '
                f'width="{brand["logo_size"][0]}" height="{brand["logo_size"][1]}">' if "logo" in brand else "")
        return (f'  <a class="skip-link" href="#content">{self.t("header.skip")}</a>\n'
                '  <header class="topbar campaign-banner"><div class="banner-intro">'
                f'<a class="brand" href="/">{logo}{self.name}</a><h1>{e(self.site["tagline"])}</h1>'
                f'<p>{self.county}, {self.state}</p>{self.nav(current)}</div>{cta}</header>\n')

    def footer(self):
        parts = [f'<p>{self.t("footer.credit", name=self.name)}</p>']
        if self.site["contact_email"]:
            mail = e(self.site["contact_email"])
            parts.append(f'<p>{self.t("footer.contact")} <a href="mailto:{mail}">{mail}</a></p>')
        if self.site["social"]:
            links = " · ".join(f'<a href="{e(link["url"])}" rel="noopener noreferrer">{e(link["label"])}</a>'
                               for link in self.site["social"])
            parts.append(f'<p>{links}</p>')
        if self.map:
            parts.append(f'<p>{e(self.map["attribution"])} · <a href="/cameras.html">Mapped points</a></p>')
        if self.license:  # DF1422-13: one paragraph, only when site.json.license is set
            attribution = f', {e(self.license["attribution"])}' if "attribution" in self.license else ""
            parts.append(f'<p>{self.t("footer.license")} <a href="{e(self.license["url"])}" rel="license">{e(self.license["name"])}</a>{attribution}</p>')
        parts.append(f'<p>{e(self.site["not_legal_advice"])}</p>')
        if self.topics:  # DF1952-06
            parts.append(f'<p><a href="/topics/">{self.t("nav.topics")}</a></p>')
        if self.supporters:  # DF2207-13
            parts.append(f'<p><a href="/supporters.html">{self.t("supporters.title")}</a></p>')
        parts.append(f'<p><a href="/privacy.html">{self.t("footer.privacy")}</a>'
                     + (f' · <a href="/glossary.html">{self.t("footer.glossary")}</a>' if self.glossary else "") + '</p>')
        sitemap = f' · <a href="/sitemap.xml">{self.t("footer.sitemap")}</a>' if self.base else ""  # none without base_url
        meetings_feed = f' · <a href="/meetings.xml">{self.t("meeting.feed")}</a>' if self.model["meetings"] else ""  # DF1103-10
        archive = ' · <a href="/newsletters.html">Newsletter archive</a>' if self.model.get("newsletters") else ""  # DF1952-09
        parts.append(f'<p><a href="/feed.xml">{self.t("footer.feed")}</a>{meetings_feed} · <a href="/search.html">{self.t("footer.search")}</a> · <a href="/corrections.html">{self.t("nav.corrections")}</a>{sitemap}{archive}'
                     ' · <a href="/methods.html">How to read our findings</a></p>')
        if self.faq:  # DF0022-01: appended last, so every earlier footer paragraph is unchanged
            parts.append(f'<p><a href="/faq.html">{self.t("footer.faq")}</a></p>')
        return '  <footer class="site-footer">' + "".join(parts) + '</footer>\n'

    def share(self, title, description, path, kind, published, modified):
        """Open Graph + Twitter summary tags from the page's own text; og:image only from site.json.share_image, og:url only with base_url."""
        tags = [("property", "og:site_name", self.name),
                ("property", "og:title", e(title if kind == "article" else _one_line(title)).replace("\n", "&#10;").replace("\r", "&#13;").replace("\t", "&#9;")),
                ("property", "og:description", e(description if kind == "article" else _one_line(description)).replace("\n", "&#10;").replace("\r", "&#13;").replace("\t", "&#9;")),
                ("property", "og:type", kind)]
        if self.base and (kind != "article" or _preview_origin(self.base)):
            tags.append(("property", "og:url", e(self.base + path)))
        image = self.site.get("share_image")
        if image and image["size"]:
            url = e(self.base + "/" + image["path"] if self.base else "/" + image["path"])
            tags += [("property", "og:image", url), ("property", "og:image:width", str(image["size"][0])),
                     ("property", "og:image:height", str(image["size"][1])),
                     ("property", "og:image:alt", e(image["alt"]))]
        tags += [("name", "twitter:card", "summary_large_image" if image else "summary"),
                 ("name", "twitter:title", e(_one_line(title))),
                 ("name", "twitter:description", e(_one_line(description)))]
        if image and image["size"]:
            if self.base:
                tags.append(("name", "twitter:image", url))
            tags.append(("name", "twitter:image:alt", e(image["alt"])))
        if kind == "article":
            tags.append(("property", "article:published_time", e(_one_line(published))))
            if modified:
                tags.append(("property", "article:modified_time", e(_one_line(modified))))
        return "".join(f'  <meta {attr}="{key}" content="{value}">\n' for attr, key, value in tags)

    def head(self, title, description, path, scripts=(), stylesheets=(), kind="website", published=None, modified=None,
             index=True):
        """``index=False``: a robots noindex tag instead of the share tags, and no canonical link."""
        canonical = f'  <link rel="canonical" href="{e(self.base + path)}">\n' if self.base and index else ""
        extra = "".join(f'  <link rel="stylesheet" href="{href}">\n' for href in stylesheets)
        extra += "".join(f'  <script defer src="{src}"></script>\n' for src in scripts)
        favicon = self.site.get("brand", {}).get("favicon")
        svg_icon = ' type="image/svg+xml"' if favicon and favicon.lower().endswith(".svg") else ""
        icon = f'  <link rel="icon"{svg_icon} href="/{e(favicon)}">\n' if favicon else ""
        return ('  <meta charset="utf-8">\n  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
                f'  <meta name="description" content="{e(description)}">\n'
                + (self.share(title, description, path, kind, published, modified) if index
                   else '  <meta name="robots" content="noindex">\n')
                + f'  <title>{e(title)} | {self.name}</title>\n'
                '  <link rel="stylesheet" href="/style.css">\n' + icon
                + f'  <link rel="alternate" type="application/atom+xml" title="{self.t("title.feed")}" href="/feed.xml">\n'
                + (f'  <link rel="alternate" type="application/atom+xml" title="{self.t("title.meetings_feed")}" href="/meetings.xml">\n'
                   if self.model["meetings"] and (path == "/meetings.html" or path.startswith("/meetings/")) else "")
                + canonical + extra)

    def page(self, current, title, description, path, main, scripts=(), stylesheets=(),
             kind="website", published=None, modified=None, index=True):
        return render({
            "schema_version": 1,
            "language": self.site["language"],
            "html": {"head": self.head(title, description, path, scripts, stylesheets, kind, published, modified, index),
                     "header": self.header(current), "main": main, "footer": self.footer()},
            "styles": {"font_family": self.site["font_family"], "font_faces": ""},
        })

    # -- fragments -----------------------------------------------------------

    def pill(self, code):
        """A classification label linked to its definition on /methods.html; the visible text is unchanged."""
        return f'<a class="status-pill" href="/methods.html#classification-{e(code)}">{self.t("classification." + code)}</a>'

    def confidence_link(self, code):
        return f'<a href="/methods.html#confidence-{e(code)}">{self.t("confidence." + code)}</a>'

    def finding_item(self, finding, filter_attrs=False):
        latest = max((c["date"] for c in finding["corrections"]), default=None)
        corrected = (f'<span class="status-pill">{self.t("finding.corrected", date=time_tag(latest))}</span>'
                     if latest else "")
        attrs = (f' data-agency="{e(finding["agency_id"] or "none")}" data-classification="{e(finding["classification"])}"'
                 f' data-year="{e(finding["event_date"][:4])}"' if filter_attrs else "")
        if filter_attrs and finding.get("also_agency_ids"):  # only multi-agency rows; legacy rows keep today's markup
            attrs += f' data-agencies="{e(" ".join(finding_agency_ids(finding)))}"'
        return (f'<li{attrs}><div class="finding-meta">{self.pill(finding["classification"])}'
                f'<span>{self.confidence_link(finding["confidence"])}</span><span>{self.t("finding.event_date", date=date_label(finding["event_date"]))}</span>{corrected}</div>'
                f'<h3><a href="{e(finding_path(finding))}">{e(finding["title"])}</a></h3><p>{e(finding["summary"])}</p></li>')

    def findings_list(self, findings, empty=None, filter_attrs=False):
        if not findings:
            return f'<p>{empty or self.t("notice.no_findings")}</p>'
        return '<ul class="finding-list">' + "".join(self.finding_item(f, filter_attrs) for f in findings) + '</ul>'

    def finding_filter(self):
        """Hidden filter form for the findings index (RF1510-3): "" below 4 findings or when no dimension varies.

        A select appears only when its dimension has 2+ distinct values; findings/filter.js reveals and wires it."""
        findings = self.model["findings"]
        if len(findings) < 4:
            return ""
        names = {a["agency_id"]: a["name"] for a in self.model["agencies"]}
        ids = sorted({a for f in findings for a in finding_agency_ids(f)}, key=lambda a: (names.get(a, a).casefold(), a))
        agencies = [(a, names.get(a, a)) for a in ids] + (
            [("none", self.labels["finding.filter_no_agency"])] if any(not finding_agency_ids(f) for f in findings) else [])
        codes = {f["classification"] for f in findings}
        classifications = [(code, self.labels["classification." + code]) for code in CLASSIFICATION_LABELS if code in codes]
        years = [(y, y) for y in sorted({f["event_date"][:4] for f in findings}, reverse=True)]
        fields = "".join(
            f'<div><label for="filter-{key}">{self.t("finding.filter_" + key)}</label><select id="filter-{key}" name="{key}">'
            f'<option value="all">{self.t("finding.filter_all_" + key)}</option>' + "".join(f'<option value="{e(v)}">{e(t)}</option>' for v, t in options)
            + '</select></div>'
            for key, options in (("agency", agencies), ("classification", classifications), ("year", years)) if len(options) >= 2)
        if not fields:
            return ""
        return (f'<form class="finding-filter" id="finding-filter" aria-label="{self.t("finding.filter_label")}" hidden>{fields}'
                f'<p class="filter-count" id="filter-count" aria-live="polite">{self.t("finding.filter_count", count=len(findings))}</p></form>')

    def _written_deadline(self, meeting):
        """DF1822-08: the verified written-comment deadline line, in the deadline's own offset; "" when none or cancelled.

        Closed when the deadline is at or before the build instant, which is the build date (``self.today``) at
        00:00 UTC: the builder carries a date, not a clock time.
        """
        written = (meeting.get("participation") or {}).get("written_comment") or {}
        if meeting.get("status") == "cancelled" or not written.get("deadline"):
            return ""
        due = datetime.fromisoformat(written["deadline"].replace("Z", "+00:00"))
        if due <= datetime(self.today.year, self.today.month, self.today.day, tzinfo=timezone.utc):
            text = "Written comment period closed"
        else:
            offset = int(due.utcoffset().total_seconds()) // 60
            text = (f'Written comments due {WEEKDAYS[due.weekday()]}, {MONTHS[due.month - 1]} {due.day}, {due.year}, '
                    f'{(due.hour - 1) % 12 + 1}:{due.minute:02d} {"AM" if due.hour < 12 else "PM"} '
                    f'(UTC{"-" if offset < 0 else "+"}{abs(offset) // 60:02d}:{abs(offset) % 60:02d})')
        return f'<p class="comment-deadline"><time datetime="{e(written["deadline"])}">{text}</time></p>'

    def meeting_item(self, meeting):
        agenda = (f' · <a href="{e(meeting["agenda_url"])}" rel="noopener noreferrer">{self.t("meeting.agenda")}</a>'
                  if meeting["agenda_url"] else "") + f' · <a href="/meetings/{e(meeting["id"])}.html">{self.t("meeting.page")}</a>'
        item = f'<p>{self.t("meeting.agenda_item")} {e(meeting["agenda_item"])}</p>' if meeting["agenda_item"] else ""
        item += meeting_outcome_html(meeting, self.t) if meeting["starts_at"][:10] < self.today.isoformat() else ""
        item += self._written_deadline(meeting)
        where = f' · {e(meeting["location"])}' if meeting["location"] else ""
        cancelled = self.t("meeting.cancelled") + " " if meeting.get("status") == "cancelled" else ""
        kit = (f'<details><summary>{self.t("meeting.kit")}</summary>' + self.md(meeting["comment_kit_md"]) + '</details>'
               if meeting["comment_kit_md"] else "")
        return (f'<article id="meeting-{e(meeting["id"])}"><time datetime="{e(meeting["starts_at"])}">{e(meeting["starts_at"].replace("T", " ")[:16])}</time>'
                f'{where}{agenda}<h3>{cancelled}{e(meeting["body"])}</h3>{item}{kit}</article>')

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
        return f'<div class="city-picker" role="group" aria-label="{self.t("map.picker_label")}">' + "".join(buttons) + '</div>'

    def card_panel(self):
        return ('<article class="city-panel" id="agency-profile" aria-live="polite"><div>'
                f'<p class="eyebrow">{self.t("eyebrow.agency_profile")}</p><h3 id="city-name"></h3>'
                '<p class="city-meta"><span class="status-pill" id="city-status"></span> <span class="city-vendor" id="city-vendor"></span> '
                '<span id="city-camera-count" hidden></span></p><p class="city-summary" id="city-summary"></p>'
                '<ul class="city-facts" id="city-facts"></ul><p><a class="text-link" id="city-records-link" href="sources.html#library">' + e(self.site["source_link_label"]) + '</a> <a class="text-link" id="city-flock-link" rel="noopener noreferrer" hidden>'
                f'{self.t("agency.flock_portal")}</a> <a class="text-link" id="city-agency-link"'
                + (f' data-label="{self.t("agency.page")}"' if self.agency_pages() else '')
                + f' hidden></a></p></div><aside class="city-watch"><p class="eyebrow">{self.t("eyebrow.open_questions")}</p><p id="city-open"></p>'
                '<div class="policy-flags" id="city-flags"></div></aside></article>')

    def map_section(self):
        if not self.map:
            return ""
        fallback = self.t("map.unavailable" if not self.maplibre else "map.loading")
        return ('    <section class="hero" id="map"><div class="hero-map-wrap">'
                f'<div id="county-map" class="map" role="region" aria-label="{self.t("map.region_label")}"></div>'
                f'<p class="map-fallback">{fallback} {self.t("map.still_choose")} '
                f'<a href="{e(self.map["controller"]["source_map_url"])}" rel="noopener noreferrer">{self.t("map.source_map")}</a>. '
                '<a href="/cameras.html">Read the list of mapped points</a>.</p>'
                f'<div class="map-key county-map-badge"><div class="county-map-identity"><span class="county-map-monogram">{e(self.cfg["county"][:2].upper())}</span>'
                f'<div><span class="county-map-region">{self.t("map.region")}</span><strong>{self.county}</strong></div></div>'
                f'<div class="county-map-legend"><span class="camera-dot"></span><span id="map-selection-count">{self.t("map.count", count=self.map["camera_count"])}</span></div>'
                f'<p class="county-map-hint">{self.t("map.hint")}</p></div>'
                f'<p class="map-credit">{e(self.map["attribution"])}</p>{self.picker()}</div>'
                f'<p class="map-date">{self.map_date_line()}</p></section>\n')

    def map_date_line(self):
        """The OpenStreetMap data day from map/snapshot.json; never the build date."""
        day = self.map.get("snapshot_date")
        return self.t("map.date", date=date_label(day)) if day else self.t("map.date_unknown")

    def signup_section(self):
        signup = self.site["signup"]
        if signup["mode"] == "brevo_hosted":
            form = (f'<form class="signup-form" method="post" action="{e(signup["url"])}">'
                    f'<div class="signup-row"><label class="visually-hidden" for="signup-email">{self.t("signup.email")}</label>'
                    f'<input id="signup-email" type="email" name="EMAIL" autocomplete="email" required placeholder="{self.t("signup.placeholder")}">'
                    f'<button type="submit">{e(signup["label"])}</button></div>'
                    f'<p class="form-note">{e(signup["note"])} {self.t("signup.provider_note")}</p></form>')
        else:
            form = f'<p>{self.t("signup.none")}</p>'
        return (f'    <section class="section movement-section" id="participate"><h2>{self.t("heading.participate")}</h2>'
                f'{form}</section>\n')

    def contract_cost_amount(self, cost):
        """``$12,345.60`` from the transcribed decimal string; exact, never float-converted."""
        return "$" + format(Decimal(cost["amount_usd"]), ",.2f")

    def contract_cost_line(self, contract):
        """DF1103-07: the cost exactly as the document states it, with the contract's own source and checked date."""
        cost = contract["cost"]
        source = (f'<a href="{e(contract["source_url"])}" rel="noopener noreferrer">{self.t("agency.cost_source")}</a>')
        return ('<p class="contract-cost">' + self.t("agency.contract_cost", amount=e(self.contract_cost_amount(cost)),
                period=self.t("agency.cost_" + cost["period"]), document=e(cost["as_stated_in"]), source=source,
                date=date_label(contract["checked_on"])) + "</p>")

    def contract_line(self, contract):
        if not contract:
            return ""
        return (f'<p class="contract-term">{self.t("agency.contract", name=e(contract["vendor"]))}, {time_tag(contract["start"])} – '
                f'{time_tag(contract["end"])}' + (", " + self.t("agency.contract_ended") if contract["end"] < self.today.isoformat() else "")
                + (f'. {e(contract["renewal_note"])}' if contract["renewal_note"] else "")
                + f' ({self.t("agency.checked", date=date_label(contract["checked_on"]))} <a href="{e(contract["source_url"])}" '
                  f'rel="noopener noreferrer">{self.t("agency.the_source")}</a>).</p>'
                + (self.contract_cost_line(contract) if contract.get("cost") else ""))

    def funding_block(self, agency):
        """DF2207-02: how the agency says it is paid for, one entry per document; "" when none was recorded.

        Every entry carries its own document, source link and checked date; entries stay in the order recorded and
        nothing is added up, ranked or explained beyond the name the document uses.
        """
        items = []
        for entry in agency.get("funding", []):
            parts = [f'<span class="funding-kind">{self.t("agency.funding_kind_" + entry["kind"])}</span>',
                     f'<strong class="funding-name">{e(entry["name"])}</strong>']
            if "amount_usd" in entry:
                parts.append(e(self.contract_cost_amount(entry)) + " " + self.t("agency.cost_" + entry["period"]))
            if "fiscal_year" in entry:
                parts.append(self.t("agency.funding_fiscal_year", year=e(entry["fiscal_year"])))
            source = f'<a href="{e(entry["source_url"])}" rel="noopener noreferrer">{self.t("agency.cost_source")}</a>'
            provenance = self.t("agency.funding_provenance", document=e(entry["as_stated_in"]), source=source,
                                date=time_tag(entry["checked_on"]))
            items.append(f'<li>{" · ".join(parts)}. <span class="funding-provenance">{provenance}</span></li>')
        return f'<ul class="agency-funding">{"".join(items)}</ul>' if items else ""

    def policy_check_section(self, agency):
        """DF1207-06: the organizer's reading of which listed elements a posted policy addresses; fixed wording, no judgement."""
        check = agency.get("policy_check")
        if not check:
            return ""
        words = {"not_found": "not found in the posted policy", "unclear": "unclear", "addressed": "addressed"}
        items = "".join(f'<li>{e(item["label"])}: {words[item["status"]]}'
                        + (f' (page {item["page"]})' if "page" in item else "") + "</li>" for item in check["elements"])
        return ('<section class="policy-check"><h4>What the posted policy covers (organizer reading, not a legal review)</h4>'
                f'<ul>{items}</ul><p>Checked {date_label(check["checked_on"])} against <a href="{e(check["policy_url"])}" '
                f'rel="noopener noreferrer">the posted policy</a>. Law package: {e(self.checklist_law_status or "status unavailable")}.'
                '</p></section>')

    def pre_meeting_section(self, agency):
        """DF1207-07: the public meeting an organizer found before the camera program, or that none has been found yet.

        Fixed wording; a missing record is never turned into a statement that no meeting was held."""
        meeting = agency.get("pre_implementation_meeting")
        if not meeting:
            return ""
        checked = date_label(meeting["checked_on"])
        if meeting["status"] == "found":
            line = (f'{e(meeting["body"])} discussed it on {date_label(meeting["meeting_on"])} '
                    f'(<a href="{e(meeting["source_url"])}" rel="noopener noreferrer">official record</a>). Checked {checked}.')
        else:
            line = f"We have not found a record of one yet (checked {checked})."
        rule = (f"<p>Rule: {e(self.pre_meeting_rule[0])}. Law package: {e(self.pre_meeting_rule[1])}.</p>"
                if self.pre_meeting_rule else "")
        note = f'<p>{e(meeting["note"])}</p>' if meeting.get("note") else ""
        return f'<section class="pre-meeting"><h4>Public meeting before the camera program</h4><p>{line}</p>{rule}{note}</section>'

    def contract_check_block(self, agency, heading="h4"):
        """DF2207-03: the organizer's reading of the linked contract; fixed wording, no score and no judgement.

        \"\" when the agency has no ``contract_check``. Labels, notes and URLs are escaped; nothing is fetched."""
        check = agency.get("contract_check")
        if not check:
            return ""
        words = {"not_found": "not found in the linked contract", "unclear": "unclear", "addressed": "addressed"}
        items = "".join(f'<li>{e(item["label"])}: {words[item["status"]]}'
                        + (f' (page {item["page"]})' if "page" in item else "")
                        + (f' <span class="contract-check-note">— {e(item["note"])}</span>' if "note" in item else "") + "</li>"
                        for item in check["elements"])
        return (f'<section class="contract-check"><{heading}>What the contract says (organizer reading, not a legal review)</{heading}>'
                f'<ul>{items}</ul><p>Not found describes this reading, not whether a term exists elsewhere.</p>'
                f'<p>Checked {date_label(check["checked_on"])} against <a href="{e(check["contract_url"])}" '
                'rel="noopener noreferrer">the linked contract</a>. Checklist: draft.</p></section>')

    def transparency_block(self, agency, heading="h4"):
        """Newest organizer-transcribed portal snapshot, as the vendor reported it, with change since the previous one."""
        snapshots = agency.get("transparency")
        if not snapshots:
            return ""
        newest, previous, lines = snapshots[0], (snapshots[1:] or [{}])[0], []
        for key in TRANSPARENCY_FIELDS:
            if newest[key] is None:
                continue
            line, before = self.t(f"agency.transparency_{key}", count=newest[key]), previous.get(key)
            if before is not None:
                change = newest[key] - before
                word = "up" if change > 0 else "down" if change < 0 else "same"
                line += f' ({self.t("agency.transparency_" + word, count=abs(change), date=time_tag(previous["checked_on"]))})'
            lines.append(f"<li>{line}</li>")
        return (f'<section class="agency-transparency"><{heading}>{self.t("agency.transparency_heading")}</{heading}>'
                + (f'<ul>{"".join(lines)}</ul>' if lines else "")
                + f'<p><a href="{e(newest["source_url"])}" rel="noopener noreferrer">'
                + self.t("agency.transparency_checked", date=time_tag(newest["checked_on"])) + '</a></p></section>')

    def agency_mapped_reported_block(self, agency):
        """DF0022-13: the newest portal camera count beside the map's count for the same picker city, never compared.

        "" unless the agency is a picker city, its newest snapshot has a camera count (0 counts) and the map counts its
        boundary (0 counts). Two different populations; no difference, ratio or ranking is ever computed.
        """
        snapshots = agency.get("transparency") or []
        reported = snapshots[0].get("cameras") if snapshots else None
        if reported is None or not self.map or agency["agency_id"] not in {a["agency_id"] for a in self.picker_cities()}:
            return ""
        name = self.map["cities"][agency["slug"]]["name"] or agency["jurisdiction_name"] or agency["name"]
        mapped = self.mapped_counts()[0].get(name)
        if mapped is None:
            return ""
        unknown = self.t("agency.mapped_reported_unknown")
        checked, day = snapshots[0].get("checked_on"), self.map.get("snapshot_date")
        return ('<section class="agency-mapped-reported"><p>'
                + self.t("agency.mapped_reported_counts", reported=e(str(reported)), mapped=e(str(mapped)),
                         checked=time_tag(checked) if checked else unknown, map_date=time_tag(day) if day else unknown)
                + f'</p><p>{self.t("agency.mapped_reported_caveat")}</p></section>')

    def contracts_section(self):  # contracts ending in [build date, +180 days], soonest first; "" when none
        first, last = self.today.isoformat(), (self.today + timedelta(days=180)).isoformat()
        soon = sorted((a for a in self.model["agencies"] if a["contract"] and first <= a["contract"]["end"] <= last),
                      key=lambda a: (a["contract"]["end"], a["name"]))
        items = "".join(f'<li><a href="/agencies.html#{e(a["slug"])}">{e(a["name"])}</a>: '
                        f'{self.t("agency.contract_ends", name=e(a["contract"]["vendor"]), date=time_tag(a["contract"]["end"]))}</li>' for a in soon)
        return (f'    <section class="section" id="contracts"><div class="section-heading"><div><p class="eyebrow">{self.t("eyebrow.contracts")}</p>'
                f'<h2>{self.t("heading.contracts")}</h2></div></div><ul class="contract-list">{items}</ul></section>\n') if soon else ""

    # -- pages ---------------------------------------------------------------
    def timeline_html(self):
        return self.page("timeline", "Timeline", "Public campaign dates", "/timeline.html", render_timeline(self, self.timeline))

    def index_html(self):
        mission = self.md(self.site["mission_md"]) if self.site["mission_md"] else ('<div class="mission-grid">' + "".join(
            f'<article><span>0{n}</span><h3>{self.t(f"intro.step{n}_title")}</h3><p>{self.t(f"intro.step{n}")}</p></article>'
            for n in (1, 2, 3)) + '</div>')
        cards = (f'    <section class="section cities-section" id="agencies"><div class="section-heading" id="content"><div><p class="eyebrow">{self.t("eyebrow.agencies")}</p>'
                 f'<h2>{self.t("heading.agencies_home")}</h2></div><p>{self.t("intro.agencies_home")} '
                 f'<a href="/agencies.html">{self.t("intro.full_agency_list")}</a>.</p></div>'
                 + ('' if self.map else '<div class="city-panel-static">' + self.picker() + '</div>')
                 + self.card_panel() + '</section>\n')
        empty = (f'<p>{self.t("notice.no_upcoming")} <a href="/meetings.html#past-meetings">{self.t("notice.past_meetings_link")}</a>.</p>'
                 if self.past else f'<p>{self.t("notice.no_meetings")}</p>')
        meetings = (f'    <section class="section" id="meetings"><div class="section-heading"><div><p class="eyebrow">{self.t("eyebrow.meetings")}</p><h2>{self.t("heading.meetings_home")}</h2></div>'
                    f'<p><a href="/meetings.html">{self.t("intro.all_meetings")}</a></p></div><div class="meeting-list">'
                    + ("".join(self.meeting_item(m) for m in self.upcoming[:3]) or empty) + '</div></section>\n')
        main = (self.map_section() + cards + self.contracts_section()
                + f'    <section class="section" id="mission"><div class="section-heading"><div><p class="eyebrow">{self.t("eyebrow.mission")}</p>'
                  f'<h2>{e(self.site["tagline"])}</h2></div></div>{mission}</section>\n'
                + f'    <section class="section" id="findings"><div class="section-heading"><div><p class="eyebrow">{self.t("eyebrow.latest_findings")}</p><h2>{self.t("heading.findings_home")}</h2></div>'
                  f'<p><a href="/findings/">{self.t("intro.all_findings")}</a> · <a href="/feed.xml">{self.t("intro.atom_feed")}</a></p></div>'
                + self.findings_list(self.model["findings"][:5]) + '</section>\n'
                + self.signup_section() + meetings)
        scripts = (["/vendor/maplibre-gl.js"] if self.map and self.maplibre else []) + ["/site-data.js", "/agency-cards.js", "/app.js"]
        stylesheets = ["/vendor/maplibre-gl.css"] if self.map and "maplibre-gl.css" in self.model["vendor"] else []
        return self.page("index", self.labels["title.home"], self.site["tagline"], "/", main, scripts, stylesheets)

    def official_links(self, agency):
        """Records page, portal and Flock transparency links, as on ``agencies.html``."""
        links = []
        if agency["records_url"]:
            links.append(f'<a href="{e(agency["records_url"])}" rel="noopener noreferrer">{self.t("agency.records")}</a>')
        if agency["portal"]["url"]:
            links.append(f'<a href="{e(agency["portal"]["url"])}" rel="noopener noreferrer">{self.t("agency.portal", name=e(agency["portal"]["vendor"]))}</a>')
        if agency["flock_transparency_url"]:
            links.append(f'<a href="{e(agency["flock_transparency_url"])}" rel="noopener noreferrer">{self.t("agency.flock_portal")}</a>')
        return links

    def agency_blocks(self, agency, findings):
        """Confirmed / not known / asked, from organizer content only; awaiting with no update → the build date."""
        profile, asked = agency["profile"], []
        confirmed = [e(fact) for fact in profile["facts"]] + [f"{len(findings)} published finding(s) with sources"] * bool(findings)
        unknown = [e(text) for text in [profile["open"]] * bool(profile["open"]) + profile["flags"]]
        for item in sorted(agency.get("asked", []), key=lambda row: row["asked_on"], reverse=True):
            asked.append(f'{e(item["topic"])} — asked {date_label(item["asked_on"])}. ' + e(asked_sentence(item, self.today))
                         + (f' {e(item["note"])}' if item.get("note") else ""))
        return "".join(f'<section class="agency-block"><h4{anchor}>{title}</h4>' + (
            "<ul>" + "".join(f"<li>{value}</li>" for value in values) + "</ul>" if values else f"<p>{empty}</p>") + "</section>"
            for title, values, empty, anchor in (("What is confirmed", confirmed, "Nothing is confirmed by a published finding yet.", ""),
                                                 ("What is not known", unknown, "Nothing listed yet.", ""),
                                                 ("What we asked", asked, "No request is listed for this agency yet.",
                                                  f' id="{e(agency["slug"])}-asked"')))

    def glance_section(self):
        """DF0137-12: every agency side by side in model order (never sorted by a number); each cell links its source."""
        unchecked, rows = self.t("agencies.glance_not_checked"), []
        heads = [self.t(f"agencies.glance_{key}") for key in ("agency", "cameras", "searches_last_30_days",
                                                               "retention_days", "policy", "contract", "cost", "asked")]

        def sourced(url, text, checked_on):
            return (f'<a href="{e(url)}" rel="noopener noreferrer">{text}</a> '
                    f'<small>{self.t("agencies.glance_checked", date=time_tag(checked_on))}</small>')
        for agency in self.model["agencies"]:
            newest, policy, contract = (agency.get("transparency") or [{}])[0], agency.get("alpr_policy"), agency["contract"]
            asked, page = len(agency.get("asked", [])), self.agency_pages().get(agency["agency_id"], f'#{agency["slug"]}')
            cells = [f'<a href="{e(page)}">{e(agency["name"])}</a>'] + [
                sourced(newest["source_url"], e(newest[key]), newest["checked_on"]) if newest.get(key) is not None
                else unchecked for key in ("cameras", "searches_last_30_days", "retention_days")]
            cells += [sourced(policy["url"], self.t("agencies.glance_posted"), policy["checked_on"]) if policy else unchecked,
                      sourced(contract["source_url"], time_tag(contract["end"]), contract["checked_on"]) if contract else unchecked,
                      sourced(contract["source_url"], e(self.contract_cost_amount(contract["cost"])) + " "
                              + self.t("agency.cost_" + contract["cost"]["period"]), contract["checked_on"])
                      if contract and contract.get("cost") else unchecked,
                      f'<a href="#{e(agency["slug"])}-asked">{asked}</a>' if asked else "0"]
            rows.append('<tr><th scope="row" data-label="' + heads[0] + f'">{cells[0]}</th>'
                        + "".join(f'<td data-label="{head}">{cell}</td>' for head, cell in zip(heads[1:], cells[1:])) + "</tr>")
        return (f'<section class="at-a-glance" id="at-a-glance"><h3>{self.t("agencies.glance_heading")}</h3>'
                f'<p>{self.t("agencies.glance_note")}</p><table class="glance-table"><caption>{self.t("agencies.glance_caption")}'
                '</caption><thead><tr>' + "".join(f'<th scope="col">{head}</th>' for head in heads) + "</tr></thead><tbody>"
                + "".join(rows) + "</tbody></table></section>")

    def agencies_html(self):
        articles = []
        for agency in self.model["agencies"]:
            findings = self.findings_for(agency["agency_id"])
            sources = [s for s in self.model["sources"] if s["agency_id"] == agency["agency_id"]]
            links = self.official_links(agency)
            if sources:
                links.append(f'<a href="/sources.html#{e(self.fragment_for_agency[agency["agency_id"]])}">{self.t("agency.sources", count=len(sources))}</a>')
            if findings:
                links.append(f'<a href="/findings/agency/{e(agency["slug"])}.xml" type="application/atom+xml">{self.t("agency.feed")}</a>')
            if agency["agency_id"] in self.agency_pages():
                links.append(f'<a href="{e(self.agency_pages()[agency["agency_id"]])}">{self.t("agency.page")}</a>')
            body = (f'<p class="eyebrow">{self.t("kind." + agency["kind"])}</p><h3>{e(agency["name"])}</h3>'
                    f'<p>{e(agency["jurisdiction_name"])}</p><p><span class="status-pill">{e(agency["profile"]["status"])}</span></p>'
                    + (f'<p>{e(agency["profile"]["summary"])}</p>' if agency["profile"]["summary"] else "")
                    + self.agency_blocks(agency, findings)
                    + (f'<p>{self.t("agency.findings", count=len(findings))} ' + ", ".join(
                        f'<a href="/findings/{e(f["slug"])}.html">{e(f["title"])}</a>' for f in findings) + '</p>' if findings else f'<p>{self.t("agency.no_findings")}</p>')
                    + (f'<p>{" · ".join(links)}</p>' if links else "") + policy_line(agency.get("alpr_policy"))
                    + self.policy_check_section(agency)
                    + self.pre_meeting_section(agency)
                    + self.contract_line(agency["contract"]) + self.contract_check_block(agency) + self.funding_block(agency) + self.transparency_block(agency)
                    + (f'<p class="contact-provenance">{self.t("agency.contact_checked", date=date_label(agency["verification"]["checked_on"]))} '
                       f'<a href="{e(agency["verification"]["source_url"])}" rel="noopener noreferrer">{self.t("agency.official_source")}</a>.</p>'
                       if agency["verified"] and agency["verification"] else "")
                    + ('' if agency["verified"] else f'<p class="archive-hint">{self.t("agency.unverified")}</p>'))
            articles.append(f'<article id="{e(agency["slug"])}">{body}</article>')
        main = (f'    <section class="section" id="content"><div class="section-heading"><div><p class="eyebrow">{self.t("eyebrow.agencies")}</p>'
                f'<h2>{self.t("heading.agencies", county=self.county)}</h2></div><p>{self.t("intro.agencies")} '
                f'<a href="/data/agencies.json" type="application/json">{self.t("intro.agencies_open_data")}</a> · '
                f'{self.csv_link("/data/agencies.csv")}.</p></div>'
                + ('<div class="agency-list">' + self.glance_section() + "".join(articles) + '</div>' if articles
                   else f'<p>{self.t("notice.no_agencies")}</p>')
                + '</section>\n')
        return self.page("agencies", self.labels["title.agencies"], self.labels["title.agencies_description"], "/agencies.html", main)

    @staticmethod
    def _official_name(person):
        return (f'<a href="{e(person["profile_url"])}" rel="noopener noreferrer">{e(person["name"])}</a>'
                if person["profile_url"] else e(person["name"]))

    def votes_section(self, agency_id):
        """DF0137-08: each listed official's recorded votes in the body's past meetings, newest first; "" with none."""
        today = self.today.isoformat()
        held = sorted((m for m in self.model["meetings"] if m.get("agency_id") == agency_id and m["status"] != "cancelled"
                       and m["starts_at"][:10] < today and (m.get("outcome") or {}).get("votes")), key=lambda m: m["id"])
        held.sort(key=lambda m: m["starts_at"][:10], reverse=True)  # stable: same-day meetings stay in id order
        if not held:
            return ""

        def line(m, vote):
            return (f'<li>{self.t("vote." + vote)} · <a href="/meetings/{e(m["id"])}.html">{e(m["body"])}, '
                    f'{time_tag(m["starts_at"][:10])}</a> · <a href="{e(m["outcome"]["source_url"])}" '
                    f'rel="noopener noreferrer">{self.t("agency.votes_source")}</a></li>')
        out = ""
        for person in self.model.get("officials", {}).get(agency_id) or []:
            mine = [line(m, v["vote"]) for m in held for v in m["outcome"]["votes"] if v["name"] == person["name"]]
            out += f'<h4>{self._official_name(person)}</h4>'
            if not mine:
                out += f'<p>{self.t("agency.votes_none")}</p>'
                continue
            out += "<ul>" + "".join(mine[:50]) + "</ul>"
            out += f'<p>{self.t("agency.votes_more", count=len(mine) - 50)}</p>' if len(mine) > 50 else ""
        return f'<section class="member-votes"><h3>{self.t("agency.votes_heading")}</h3>{out}</section>' if out else ""

    def who_decides(self, agency_id):
        """DF2237-13: the body's organizer-checked officials, or "" when ``content/officials.json`` lists none."""
        people = self.model.get("officials", {}).get(agency_id)
        if not people:
            return ""

        items = "".join(f'<li>{self._official_name(p)}, {e(p["role"])}</li>' for p in people)
        newest = max(people, key=lambda p: p["checked_on"])  # first listed wins a tie
        source = f'<a href="{e(newest["source_url"])}">{self.t("officials.source")}</a>'
        return (f'<section class="who-decides"><h3>{self.t("officials.heading")}</h3><ul>{items}</ul><p>'
                + self.t("officials.listed", source=source, date=f'<time>{e(newest["checked_on"])}</time>')
                + '</p></section>')

    def local_rules_section(self, agency):
        """DF1952-02: the validated organizer-transcribed local rules whose ``agency_ids`` name ``agency`` (exact id, every
        dated version), each under its own heading and classified against the build date; "" when none.

        Reads only ``self.local_rules`` (the dependency-validated model snapshot). A transcription is not a finding
        about the agency and says nothing about its conduct; the duty is shown as written, escaped, never restated."""
        aid = agency["agency_id"]
        scoped = sorted((rule for rule in self.local_rules if aid in rule["agency_ids"]),
                        key=lambda rule: (rule["rule_id"], rule["effective_from"]))
        if not scoped:
            return ""
        today = self.today.isoformat()
        items = []
        for rule in scoped:
            end = rule["effective_to"]
            if end is not None and end < today:
                state = "Past — not in force on build date"
            elif rule["effective_from"] > today:
                state = "Not yet in force on build date"
            else:
                state = "In force on build date"
            items.append(
                f'<article class="local-rule"><h4>{e(rule["citation"])}</h4>'
                f'<p><span class="status-pill">{e(state)}</span> <span class="status-pill">{e(law.LOCAL_RULE_LABEL)}</span></p>'
                f'<p>Rule: <code>{e(rule["rule_id"])}</code></p>'
                f'<p>Duty as written: {e(rule["duty"])}</p>'
                f'<p>Effective from {time_tag(rule["effective_from"])} to '
                f'{time_tag(end) if end is not None else "open"}</p>'
                f'<p><a href="{e(rule["source_url"])}" rel="noopener noreferrer">Official source (organizer-recorded)</a>'
                f' · checked {time_tag(rule["checked_on"])}</p></article>')
        return ('<section class="local-rules"><h3>Local rules on record</h3>'
                "<p>These are organizer transcriptions of local ordinances, not a determination about the agency's "
                "conduct.</p>" + "".join(items) + "</section>")

    def agency_html(self, agency):
        """``agencies/<slug>.html`` (DF2237-08): one agency's card facts, findings, source records and upcoming meetings."""
        aid, profile = agency["agency_id"], agency["profile"]
        findings = self.findings_for(aid)
        records = "".join(
            f'<li><p>{e(s["title"])}</p><p class="record-no">sha256 <code>{e(short_hash(s["sha256"]))}</code> · '
            f'{self.t("agency.received", date=date_label(s["received_at"]))} · '
            f'<a href="/sources.html#{e(s["fragment"])}">{self.t("agency.in_library")}</a></p></li>'
            for s in self.model["sources"] if s["agency_id"] == aid)
        meetings = "".join(self.meeting_item(m) for m in self.upcoming if m.get("agency_id") == aid)
        past = "".join(self.meeting_item(m) for m in self.past if m.get("agency_id") == aid)  # DF2207-05
        links = self.official_links(agency)
        main = (f'    <article class="section finding-body" id="content"><p class="eyebrow">{self.t("kind." + agency["kind"])}</p>'
                f'<h2>{e(agency["name"])}</h2><p>{e(agency["jurisdiction_name"])}</p>'
                f'<p><span class="status-pill">{e(profile["status"])}</span></p>'
                + (f'<p>{e(profile["summary"])}</p>' if profile["summary"] else "")
                + ('<ul>' + "".join(f'<li>{e(fact)}</li>' for fact in profile["facts"]) + '</ul>' if profile["facts"] else "")
                + (f'<p>{e(profile["open"])}</p>' if profile["open"] else "")
                + ('<p>' + " ".join(f'<span class="status-pill">{e(flag)}</span>' for flag in profile["flags"]) + '</p>'
                   if profile["flags"] else "")
                + (f'<p>{" · ".join(links)}</p>' if links else "") + self.policy_check_section(agency) + self.pre_meeting_section(agency)
                + (self.contract_line(agency["contract"]) if (agency["contract"] or {}).get("cost") else "")
                + self.contract_check_block(agency, "h3")
                + self.funding_block(agency) + self.transparency_block(agency, "h3") + self.agency_mapped_reported_block(agency) + self.who_decides(aid) + self.votes_section(aid)
                + self.local_rules_section(agency)
                + f'<h3>{self.t("agency.published_findings")}</h3>' + self.findings_list(findings, self.t("agency.no_findings"))
                + (f'<p><a href="/findings/agency/{e(agency["slug"])}.xml" type="application/atom+xml">{self.t("agency.feed")}</a></p>'
                   if findings else "")
                + f'<h3>{self.t("agency.source_records")}</h3>'
                + (f'<ul class="source-table">{records}</ul>' if records else f'<p>{self.t("notice.no_sources")}</p>')
                + f'<h3>{self.t("agency.upcoming")}</h3>'
                + f'<div class="meeting-list">{meetings or "<p>" + self.t("agency.no_upcoming") + "</p>"}</div>'
                + (f'<h3>{self.t("agency.past_meetings")}</h3><div class="meeting-list">{past}</div>' if past else "")
                + ('' if agency["verified"] else f'<p class="archive-hint">{self.t("agency.unverified")}</p>')
                + f'<p class="notice">{e(self.site["not_legal_advice"])}</p></article>\n')
        description = profile["summary"] or self.labels["agency.page_description"].format(name=agency["name"])
        return self.page("agencies", agency["name"], description, self.agency_pages()[aid], main)

    def findings_index_html(self):
        form = self.finding_filter()
        empty = (f'<p class="filter-empty" id="filter-empty" hidden>{self.t("notice.no_filter_match")}</p>'
                 if form else "")
        main = (f'    <section class="section" id="content"><div class="section-heading"><div><p class="eyebrow">{self.t("eyebrow.findings")}</p>'
                f'<h2>{self.t("heading.findings")}</h2></div><p>{self.t("intro.findings")} <a href="/feed.xml">{self.t("intro.atom_feed")}</a> · '
                f'<a href="/data/findings.json" type="application/json">{self.t("intro.open_data")}</a> · '
                f'{self.csv_link("/data/findings.csv")} · <a href="/findings/packet-all.html">Printable packet</a>.</p>'
                '<p><a href="/methods.html">How to read our findings</a> defines each classification and confidence label.</p></div>'
                f'{form}{self.findings_list(self.model["findings"], filter_attrs=True)}{empty}'
                f'<p class="notice">{e(self.site["not_legal_advice"])}</p></section>\n')
        return self.page("findings", self.labels["title.findings"], self.labels["title.findings_description"], "/findings/", main,
                         ["/site-data.js", "/findings/filter.js"] if form and self.js_labels else
                         ["/findings/filter.js"] if form else [])

    def search_html(self):
        """/search.html (DF1207-15): the form and fallback; search.js reveals the form and fills the results."""
        known = {aid: [self.agencies_by_id[aid]["name"], self.fragment_for_agency.get(aid, "")]
                 for aid in self.agency_pages() if aid in self.agencies_by_id}
        data = json.dumps(known, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fallback = self.t("search.fallback", findings=f'<a href="/findings/">{self.t("nav.findings")}</a>',
                          sources=f'<a href="/sources.html">{self.t("nav.sources")}</a>')
        main = (f'    <section class="section" id="content"><div class="section-heading"><div><h2>{self.t("search.title")}</h2></div>'
                f'<p>{self.t("search.intro")}</p></div>'
                f'<form class="site-search" id="search-form" role="search" data-agencies="{e(data)}" hidden>'
                f'<label for="q">{self.t("search.label")}</label> <input type="search" id="q" name="q" autocomplete="off"></form>'
                '<p id="search-count" aria-live="polite" hidden></p><p id="search-message" role="status" hidden></p>'
                '<ol class="search-results" id="search-results" aria-live="polite"></ol>'
                f'<p id="search-fallback">{fallback}</p></section>\n')
        return self.page("search", self.labels["search.title"], self.labels["search.description"], "/search.html", main,
                         ["/site-data.js", "/search.js"] if self.js_labels else ["/search.js"])

    def packet_html(self):
        findings = self.model["findings"]
        contents = ''.join(f'<li><a href="#f-{e(f["slug"])}">{e(f["title"])}</a></li>' for f in findings)
        articles = []
        for f in findings:
            sources = []
            for source in f["sources"]:
                fragment = self.fragment_for_sha.get(source["sha256"])
                title = e(source["title"])
                if fragment:
                    title = f'<a href="/sources.html#{e(fragment)}">{title}</a>'
                rule = f' · rule {e(source["rule_id"])}' if source.get("rule_id") else ''
                sources.append(f'<li>{title} · sha256 {e(short_hash(source["sha256"]))} · {e(source["locator"])}{rule}{self.source_quote(source)}</li>')
            links = self.agency_links(f)
            agency_line = f'<p>Agency: {links}</p>' if links else ''  # DF1952-07: primary first, then each extra agency
            limits = ''.join(f'<li>{e(item)}</li>' for item in f["limitations"]) or '<li>None stated.</li>'
            corrections = ''.join(f'<li>{time_tag(c["date"])}: {e(c["note"])}</li>' for c in sorted(f["corrections"], key=lambda c: c["date"]))
            corrections = f'<ul>{corrections}</ul>' if corrections else f'<p>{self.t("notice.no_corrections")}</p>'
            updated = f' · Updated {date_label(f["updated_at"])}' if f["updated_at"] else ''
            url = self.base + f'/findings/{f["slug"]}.html'
            articles.append(f'<article class="packet-finding" id="f-{e(f["slug"])}"><h3>{e(f["title"])}</h3>'
                f'<p>{self.pill(f["classification"])} · {self.confidence_link(f["confidence"])} · {time_tag(f["event_date"])} · '
                f'Published {date_label(f["published_at"])}{updated}</p><p>{e(f["summary"])}</p>{agency_line}'
                f'<h4>Sources</h4><ul>{"".join(sources)}</ul><h4>Limitations</h4><ul>{limits}</ul>'
                f'<h4>Corrections</h4>{corrections}<p>Finding {e(f["id"])} · <a href="{e(url)}">{e(url)}</a></p></article>')
        main = (f'<section class="section" id="content"><h2>Findings packet</h2><p>As of {e(self.today.isoformat())} · '
                f'{len(findings)} published findings · print or save as PDF</p>'
                + (f'<ol>{contents}</ol>{"".join(articles)}' if findings else f'<p>{self.t("notice.no_findings")}</p>')
                + f'<p class="notice">{e(self.site["not_legal_advice"])}</p></section>\n')
        return self.page("findings", "Findings packet", "Every published finding on one printable page", "/findings/packet-all.html", main)

    def supporters_html(self):
        """/supporters.html (DF2207-13): consented organizations by kind (enum order), then name, with the joined date.
        Only the public rows are rendered; the fixed intro says a listing is not a legal endorsement."""
        groups = []
        for kind in SUPPORTER_KINDS:
            rows = sorted((row for row in self.supporters if row["kind"] == kind), key=lambda r: (r["name"].casefold(), r["id"]))
            if not rows:
                continue
            items = "".join(
                "<li>" + (f'<a href="{e(row["url"])}" rel="noopener noreferrer">{e(row["name"])}</a>' if row["url"]
                          else e(row["name"]))
                + f' · {self.t("supporters.joined", date=time_tag(row["joined_on"]))}</li>' for row in rows)
            groups.append(f'<h3>{self.t("supporters.kind." + kind)}</h3><ul>{items}</ul>')
        main = (f'    <section class="section" id="content"><div class="section-heading"><div>'
                f'<h2>{self.t("supporters.title")}</h2></div></div><p>{self.t("supporters.description")}</p>'
                f'{"".join(groups)}<p class="notice">{e(self.site["not_legal_advice"])}</p></section>\n')
        return self.page("supporters", self.labels["supporters.title"], self.labels["supporters.description"],
                         "/supporters.html", main)

    def topics_index_html(self):
        """/topics/ (DF1952-06): every topic in slug order, each title linked once."""
        items = "".join(f'<li><a href="/topics/{e(topic["slug"])}.html">{e(topic["title"])}</a></li>'
                        for topic in self.topics)
        main = (f'    <section class="section" id="content"><div class="section-heading"><div>'
                f'<p class="eyebrow">{self.t("nav.topics")}</p><h2>{self.t("heading.topics")}</h2></div></div>'
                f'<ul>{items}</ul><p class="notice">{e(self.site["not_legal_advice"])}</p></section>\n')
        return self.page("topics", self.labels["title.topics"], self.labels["title.topics_description"], "/topics/", main)

    def topic_html(self, topic):
        """/topics/<slug>.html (DF1952-06): the intro, then the findings in the topic's own order via findings_list."""
        listed = self.findings_list([self.finding_by_id[finding_id] for finding_id in topic["finding_ids"]])
        intro = self.md(topic["intro_md"]) if topic["intro_md"].strip() else ""
        main = (f'    <section class="section" id="content"><div class="section-heading"><div>'
                f'<p class="eyebrow">{self.t("nav.topics")}</p><h2>{e(topic["title"])}</h2></div></div>{intro}{listed}'
                f'<p><a href="/topics/">{self.t("heading.topics")}</a></p>'
                f'<p class="notice">{e(self.site["not_legal_advice"])}</p></section>\n')
        return self.page("topics", topic["title"], topic["title"], f'/topics/{topic["slug"]}.html', main)

    def part_of_html(self, finding):
        """``Part of: <links>`` to the topics listing this finding (slug order, each once); "" when none."""
        links = [f'<a href="/topics/{e(topic["slug"])}.html">{e(topic["title"])}</a>'
                 for topic in self.topics_of.get(finding["id"], [])]
        return f'<p>{self.t("finding.part_of", topic=", ".join(links))}</p>' if links else ""

    def related_findings(self, finding, limit=5):
        """Up to `limit` other published findings sharing an agency (primary or also_agency_ids) or a cited sha256, as
        (other, shared_count, same_agency): most shared records, then same agency, then newest published_at, then slug."""
        own = {source["sha256"] for source in finding["sources"]}
        related = []
        for other in self.model["findings"]:
            if other["id"] == finding["id"]:
                continue
            shared = len(own & {source["sha256"] for source in other["sources"]})
            same_agency = bool(set(finding_agency_ids(finding)) & set(finding_agency_ids(other)))  # DF1952-07: any shared agency
            if shared or same_agency:
                related.append((other, shared, same_agency))
        related.sort(key=lambda r: (-r[1], not r[2], -utc_moment(r[0]["published_at"]).timestamp(), r[0]["slug"]))
        return related[:limit]

    def rule_item(self, finding, rule_id, is_local):
        """One cited rule: a state rule's package citation, or the local version in force for the finding's agency and
        event date followed by its transcription label (never the package status, never an inferred duty)."""
        if not is_local:
            return f"<li><code>{e(rule_id)}</code> — {e(self.rule_citations.get(rule_id, 'citation unavailable'))}</li>"
        versions = [rule for rule in law.rules_in_force({"rules": []}, finding["event_date"], local_rules=self.local_rules,
                                                        agency_id=finding.get("agency_id")) if rule["rule_id"] == rule_id]
        citation = versions[0]["citation"] if versions else "citation unavailable"
        return f"<li><code>{e(rule_id)}</code> — {e(citation)} ({e(law.LOCAL_RULE_LABEL)})</li>"

    def legal_basis(self, finding):
        """The finding's own rule_version/duty/exceptions and each cited rule's package citation; "" when none."""
        cited = finding_law.cited(finding)
        version, duty, exceptions = (finding.get(key) for key in ("rule_version", "duty", "exceptions"))
        if not cited and not version and not duty and not exceptions:
            return ""
        parts = ['<section class="legal-basis"><h3 id="legal-basis">Legal basis</h3>']
        if version:
            parts.append(f'<p>Rule version: {e(version)}</p>')
        if duty:
            parts.append(f'<p>Duty: {e(duty)}</p>')
        if isinstance(exceptions, list) and exceptions:
            parts.append("<p>Exceptions:</p><ul>" + "".join(f"<li>{e(item)}</li>" for item in exceptions) + "</ul>")
        elif exceptions and not isinstance(exceptions, list):
            parts.append(f"<p>Exceptions: {e(exceptions)}</p>")
        if cited:
            local = {rule["rule_id"] for rule in self.local_rules}
            parts.append("<ul>" + "".join(self.rule_item(finding, rule_id, rule_id in local) for rule_id in cited) + "</ul>")
            if self.law_status and any(rule_id not in local for rule_id in cited):
                parts.append(f'<p class="notice">Law package status: {e(self.law_status)}.</p>')
        return "".join(parts) + "</section>"

    def finding_meetings_html(self, finding):
        """DF2207-06: the staged meetings whose comment kit cites this finding, newest first; "" with none.

        A staged citation is not proof of attendance, speaking, adoption or endorsement."""
        items = []
        for meeting in self.meetings_by_finding.get(finding["id"], ()):
            moment = datetime.fromisoformat(meeting["starts_at"].replace("Z", "+00:00"))
            offset = int(moment.utcoffset().total_seconds()) // 60
            shown = (f'{moment:%Y-%m-%d %H:%M} (UTC{"-" if offset < 0 else "+"}{abs(offset) // 60:02d}:{abs(offset) % 60:02d})')
            cancelled = self.t("meeting.cancelled") + " " if meeting.get("status") == "cancelled" else ""
            agenda = f' · {self.t("meeting.agenda_item")} {e(meeting["agenda_item"])}' if meeting["agenda_item"] else ""
            outcome = meeting.get("outcome")
            result = f' · Outcome: {e(OUTCOME_LABELS[outcome["result"]])}' if outcome else ""
            items.append(f'<li><time datetime="{e(meeting["starts_at"])}">{e(shown)}</time> · '
                         f'<a href="/meetings/{e(meeting["id"])}.html">{cancelled}{e(meeting["body"])}</a>{agenda}{result}</li>')
        return (f'<h3 id="discussed-at-public-meetings">Discussed at public meetings</h3><ul>{"".join(items)}</ul>'
                if items else "")

    def source_quote(self, source):
        """DF1952-08: the transcribed excerpt of a finding source as an escaped blockquote with its attribution (and the
        page when given); "" without a quote. Plain text only: never Markdown, never matched against the record."""
        quote = source.get("quote")
        if not quote:
            return ""
        page = source.get("page")
        shown = f' · {self.t("finding.quote_page", page=page)}' if page is not None else ""
        return (f'<blockquote class="source-quote"><p>{e(quote)}</p></blockquote>'
                f'<p class="source-quote-note">{self.t("finding.quote_transcribed")}{shown}</p>')

    def updates_html(self, finding):
        """DF0022-06: ``Updates`` under the body, newest date first (ties: newest entry first), each note as escaped literal
        text. Empty without updates; never a correction badge, correction row or corrected feed lead."""
        items = finding_updates.newest_first(finding.get("updates", ()))
        if not items:
            return ""
        label = self.t("finding.update")
        rows = "".join(f'<li>{label} · <time datetime="{e(u["date"])}">{e(u["date"])}</time>: {e(u["note"])}</li>' for u in items)
        return f'<h3 id="updates">{self.t("finding.updates")}</h3><ul>{rows}</ul>'

    def finding_html(self, finding):
        sources = []
        for source in finding["sources"]:
            record_page = self.record_pages().get(source["sha256"])
            entry = next((s for s in self.model["sources"] if s["sha256"] == source["sha256"]), None)
            owner = self.agencies_by_id.get(entry["agency_id"], {}) if entry else {}
            sentence, record = source_line(entry, source, owner.get("name"))
            number = f'{self.t("sources.record_no")} <code>{record}</code>'
            number = f'<a href="{e(record_page)}">{number}</a>' if record_page else number
            sources.append(f'<li><p>{sentence}</p>{self.source_quote(source)}{page_link(entry, source.get("page"))}'
                           f'<p class="record-no">{number}{copy_status(entry)}</p></li>')
        corrections = ("".join(f'<li><time datetime="{e(c["date"])}">{e(c["date"])}</time>: {e(c["note"])}</li>' for c in finding["corrections"])
                       or f'<li>{self.t("notice.no_corrections")}</li>')
        links = self.agency_links(finding)
        agency_line = f'<p>{self.t("finding.agency")} {links}</p>' if links else ""
        limits = ("".join(f'<li>{e(item)}</li>' for item in finding["limitations"]) or f'<li>{self.t("finding.none_stated")}</li>')
        counter = ("".join(f'<li>{e(item)}</li>' for item in finding["counterevidence"]) or f'<li>{self.t("finding.none_identified")}</li>')
        updated = f' · {self.t("finding.updated", date=date_label(finding["updated_at"]))}' if finding["updated_at"] else ""
        rows = []
        when = lambda f: f'<time datetime="{e(f["event_date"])}">{date_label(f["event_date"])}</time>'  # noqa: E731
        for other, shared, same_agency in self.related_findings(finding):
            why = ([self.labels["finding.same_agency"]] if same_agency else []) + (
                [self.labels["finding.shares_one" if shared == 1 else "finding.shares_many"].format(count=shared)] if shared else [])
            rows.append(f'<li><a href="/findings/{e(other["slug"])}.html">{e(other["title"])}</a> · '
                        f'{self.pill(other["classification"])} · '
                        f'{self.t("finding.event_date", date=when(other))} · '
                        f'<span class="related-why">{e(" · ".join(why))}</span></li>')
        related = f'<h3 id="related">{self.t("finding.related")}</h3><ul>{"".join(rows)}</ul>' if rows else ""
        main = ('    <article class="section finding-body" id="content">'
                f'<div class="finding-meta">{self.pill(finding["classification"])}'
                f'<span>{self.t("finding.confidence")} {self.confidence_link(finding["confidence"])}</span>'
                f'<span>{self.t("finding.event_date", date=when(finding))}</span>'
                f'<span>{self.t("finding.published", date=date_label(finding["published_at"]))}{updated}</span></div>'
                f'<h2>{e(finding["title"])}</h2><p><strong>{e(finding["summary"])}</strong></p>{agency_line}{self.part_of_html(finding)}'
                f'<p><a href="{e(finding_path(finding))}">Share this finding</a></p>'
                + self.cite_html(finding)
                + (self.md(finding["body_md"]) if finding["body_md"] else "")
                + self.updates_html(finding)
                + f'<h3>{self.t("finding.sources")}</h3><ul class="source-table">{"".join(sources)}</ul>'
                + f'<p class="provenance-note">{e(EXPLAINER)}</p>' + self.legal_basis(finding)
                + f'<h3>{self.t("finding.limitations")}</h3><ul>{limits}</ul><h3>{self.t("finding.counterevidence")}</h3><ul>{counter}</ul>'
                + self.agency_response_html(finding) + related
                + f'<h3 id="corrections">{self.t("finding.corrections")}</h3><ul>{corrections}</ul>'
                + f'<p><a href="/corrections.html">{self.t("finding.all_corrections")}</a></p>'
                + f'<p><a href="/corrections.html#request">{self.t("finding.ask_correction")}</a></p>'
                + self.terms_used_html(finding)

                + self.finding_meetings_html(finding)
                + f'<p class="notice">{e(self.site["not_legal_advice"])}</p></article>\n')
        return self.page("findings", finding["title"], finding["summary"], finding_path(finding), main,
                         scripts=["/findings/cite.js"],
                         kind="article", published=finding["published_at"], modified=finding["updated_at"])

    def terms_used_html(self, finding):
        """DF1952-04: ``Terms used on this page`` links to the glossary entries the finding's title, summary or body use;
        "" (no heading) without a glossary or a match."""
        used = glossary_terms_used(finding, self.glossary) if self.glossary else []
        if not used:
            return ""
        items = "".join(f'<li><a href="/glossary.html#{e(term["anchor"])}">{e(term["term"])}</a></li>' for term in used)
        return f'<h3 id="glossary-terms">{self.t("finding.terms")}</h3><ul>{items}</ul>'

    def glossary_html(self):
        """``/glossary.html`` (DF1952-04): the optional notice, then every term alphabetically with one ``#term-...`` id,
        its aliases, the escaped-markdown definition and, when given, the citation, source link and law-package status."""
        entries = []
        for term in self.glossary["terms"]:
            parts = [f'<h3 id="{e(term["anchor"])}">{e(term["term"])}</h3>']
            if term["aliases"]:
                parts.append(f'<p>{self.t("glossary.also", aliases=e(", ".join(term["aliases"])))}</p>')
            parts.append(self.md(term["definition_md"]))
            if "citation" in term:
                parts.append(f'<p>{self.t("glossary.citation", citation=e(term["citation"]))}</p>')
            if "source_url" in term:
                parts.append(f'<p><a href="{e(term["source_url"])}" rel="noopener noreferrer">{self.t("glossary.source")}</a></p>')
            if "law_status" in term:
                parts.append(f'<p class="notice">{self.t("glossary.status", status=e(term["law_status"]))}</p>')
            entries.append("".join(parts))
        notice = self.md(self.glossary["notice_md"]) if self.glossary["notice_md"].strip() else ""
        main = (f'    <section class="section finding-body" id="content"><h2>{self.t("heading.glossary")}</h2>{notice}'
                + "".join(entries) + '</section>\n')
        return self.page("glossary", self.labels["title.glossary"], self.labels["title.glossary_description"],
                         "/glossary.html", main)

    def faq_html(self):
        """``/faq.html`` (DF0022-01): the questions in file order, each an ``h2`` with a one-based ``q-N`` id, the answer
        through markdown_lite (no images), then the findings it cites, its law reference and the glossary terms used."""
        entries = []
        for number, entry in enumerate(self.faq, 1):
            parts = [f'<h2 id="q-{number}">{e(entry["question"])}</h2>', markdown(entry["answer_md"])]
            cited = [self.finding_by_id[finding_id] for finding_id in entry["finding_ids"]]
            if cited:
                links = "".join(f'<li><a href="/findings/{e(finding["slug"])}.html">{e(finding["title"])}</a></li>' for finding in cited)
                parts.append(f'<h3>{self.t("faq.findings")}</h3><ul>{links}</ul>')
            if "rule_id" in entry:
                parts.append(f'<p>{self.t("faq.rule")}: {e(entry["citation"])} (<code>{e(entry["rule_id"])}</code>)</p>'
                             f'<p class="notice">{self.t("faq.law_status", status=e(entry["law_status"]))}</p>')
            used = (glossary_terms_used({"title": entry["question"], "summary": "", "body_md": entry["answer_md"]}, self.glossary)
                    if self.glossary else [])
            if used:
                items = "".join(f'<li><a href="/glossary.html#{e(term["anchor"])}">{e(term["term"])}</a></li>' for term in used)
                parts.append(f'<h3>{self.t("faq.terms")}</h3><ul>{items}</ul>')
            entries.append("".join(parts))
        main = '    <section class="section finding-body" id="content">' + "".join(entries) + '</section>\n'
        return self.page("faq", self.labels["faq.title"], self.labels["faq.title"], "/faq.html", main)

    def agency_response_html(self, finding):
        """The agency's reply (or silence) under "Agency response" as escaped plain text, never Markdown; "" when
        the finding has no reply or only a prepared (pending) request. The record shows as its 12-character prefix."""
        reply = finding.get("agency_response")
        if not reply or reply["status"] == "pending":
            return ""
        if reply["status"] == "no_response":
            line = self.t("finding.reply_no_response", asked_on=e(reply["asked_on"]), reply_by=e(reply["reply_by"]))
            return f'<section class="agency-response" id="agency-response"><h3>{self.t("finding.agency_response")}</h3><p>{line}</p></section>'
        line = self.t(f'finding.reply_{reply["status"]}', on=e(reply["on"]), asked_on=e(reply["asked_on"]))
        text = f'<p class="reply-text">{e(reply["text"]).replace(chr(10), "<br>")}</p>' if "text" in reply else ""
        record = ""
        if "record_sha256" in reply:
            record_page = self.record_pages().get(reply["record_sha256"])
            number = f'{self.t("sources.record_no")} <code>{e(short_hash(reply["record_sha256"]))}</code>'
            if record_page:
                number = f'<a href="{e(record_page)}">{number}</a>'
            record = f'<p class="record-no">{number}</p>'
        return (f'<section class="agency-response" id="agency-response"><h3>{self.t("finding.agency_response")}</h3>'
                f'<p>{line}</p>{text}{record}</section>')

    def cite_html(self, finding):
        """The citation as page text; the copy button stays hidden until findings/cite.js unhides it. A draft preview
        shows the same markup but never loads or writes cite.js, so its button stays hidden."""
        button = (f'<button type="button" class="cite-copy" hidden data-copied="{html_text(self.labels["finding.cite_copied"])}" '
                  f'data-fallback="{html_text(self.labels["finding.cite_select"])}">{self.t("finding.cite_copy")}</button>')
        return (f'<section class="cite" id="cite"><h3>{self.t("finding.cite")}</h3>'
                f'<p class="cite-text">{e(citation(finding, self.cfg["name"], self.base))}</p>{button}</section>')

    def finding_preview_html(self, finding):
        """``finding_html`` without the published-only citation script, plus a robots noindex meta
        and a "Draft - not published" banner at the top of ``<main>``. Never part of build output."""
        page = self.finding_html(finding)
        html, title, main = page["html"], "  <title>", '  <main id="top">\n'
        html = html.replace('  <script defer src="/findings/cite.js"></script>\n', '')
        if html.count(title) != 1 or html.count(main) != 1:
            raise ValueError("finding preview: the page shell changed; refusing to place the draft banner")
        html = html.replace(title, '  <meta name="robots" content="noindex">\n' + title)
        html = html.replace(main, main + '    <p class="draft-banner" role="note">Draft - not published</p>\n')
        return dict(page, html=html)

    def corrections_html(self):
        rows = [(f, c, i) for f in self.model["findings"] for i, c in enumerate(f["corrections"])]
        rows.sort(key=lambda row: (row[0]["slug"], row[2]))
        rows.sort(key=lambda row: row[1]["date"], reverse=True)  # stable: same-date rows keep slug/index order
        items = "".join(f'<li><time datetime="{e(c["date"])}">{e(c["date"])}</time> · '
                        f'<a href="/findings/{e(f["slug"])}.html#corrections">{e(f["title"])}</a>: {e(c["note"])}</li>'
                        for f, c, _ in rows)
        main = (f'    <section class="section" id="content"><div class="section-heading"><div><p class="eyebrow">{self.t("eyebrow.findings")}</p>'
                f'<h2>{self.t("heading.corrections")}</h2></div><p>{self.t("intro.corrections")}</p></div>'
                + policy_html(self.site["contact_email"])
                + (f'<ul>{items}</ul>' if items else f'<p>{self.t("notice.no_corrections")}</p>') + '</section>\n')
        return self.page("findings", self.labels["title.corrections"], self.labels["title.corrections_description"], "/corrections.html", main)

    def not_found_html(self):
        """``404.html`` for unknown paths: site shell, noindex, no canonical/share tags; never in sitemap or feeds."""
        links = "".join(f'<li><a href="{href}">{self.t(key)}</a></li>' for href, key in (
            ("/findings/", "nav.findings"), ("/agencies.html", "nav.agencies"), ("/meetings.html", "nav.meetings"), ("/", "nav.home_page")))
        main = (f'    <section class="section" id="content"><div class="section-heading"><div><h2>{self.t("heading.not_found")}</h2></div>'
                f'<p>{self.t("intro.not_found")} <a href="/corrections.html">{self.t("nav.corrections")}</a>.</p></div>'
                f'<ul>{links}</ul></section>\n')
        # "not-found" matches no nav key, so no nav item is marked as the current page.
        return self.page("not-found", self.labels["title.not_found"], self.labels["title.not_found_description"], "/404.html", main,
                         index=False)

    def sources_filter(self):
        """DF1103-12: hidden filter form for the source library; \"\" for an empty library (no form, no script).

        Both selects and the checkbox appear for any nonempty library; sources/filter.js reveals and wires them."""
        sources = self.model["sources"]
        if not sources:
            return ""
        names = {a["agency_id"]: a["name"] for a in self.model["agencies"]}
        agencies = sorted({s["agency_id"] for s in sources}, key=lambda a: (names.get(a, a).casefold(), a))
        years = sorted({s["received_at"][:4] for s in sources}, reverse=True)
        total = len(sources)

        def select(key, options):
            return (f'<div><label for="sources-filter-{key}">{self.t("sources.filter_" + key)}</label>'
                    f'<select id="sources-filter-{key}" name="{key}"><option value="all">{self.t("sources.filter_all_" + key)}</option>'
                    + "".join(f'<option value="{e(v)}">{e(t)}</option>' for v, t in options) + '</select></div>')
        return (f'<form class="sources-filter" id="sources-filter" aria-label="{self.t("sources.filter_label")}" hidden>'
                + select("agency", [(a, names.get(a, a)) for a in agencies]) + select("year", [(y, y) for y in years])
                + f'<div><label class="sources-filter-check" for="sources-filter-cited"><input type="checkbox" id="sources-filter-cited" '
                  f'name="cited" value="1"> {self.t("sources.filter_cited")}</label></div>'
                + f'<p class="sources-filter-count" id="sources-filter-count" aria-live="polite">'
                  f'{self.t("js.filter_count", shown=total, total=total)}</p></form>')

    def sources_labels(self):
        """DF1103-12: only the count label, for a nonempty library on a site with no relabelled js.* key; else None."""
        return {"js.filter_count": self.labels["js.filter_count"]} if self.model["sources"] else None

    def sources_filter_js(self):
        """sources/filter.js: the packaged template; the count text comes from LABELS (site-data.js), never an English literal."""
        return SOURCES_FILTER_ASSET.read_text(encoding="utf-8")

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
                link = (f'<a href="{e(source["public_url"])}" rel="noopener noreferrer">{self.t("sources.public_copy")}</a>' if source["public_url"]
                        else self.t("sources.private"))
                pages = f' · {self.t("sources.pages", count=source["pages"])}' if source["pages"] is not None else ""
                note = f' · {e(source["note"])}' if source["note"] else ""
                citing = self.cited_by.get(source["sha256"], [])
                details = f' · <a href="{e(self.record_pages()[source["sha256"]])}">{self.t("sources.record_page")}</a>'
                cited = (f' · {self.t("sources.cited_by")} ' + ", ".join(f'<a href="/findings/{e(f["slug"])}.html">{e(f["title"])}</a>' for f in citing)
                         if citing else f' · {self.t("sources.not_cited")}')
                sentence, record = source_line(source, source, self.agencies_by_id.get(source["agency_id"], {}).get("name"))
                hooks = (f' class="source-entry" data-agency="{e(source["agency_id"])}" data-year="{e(source["received_at"][:4])}"'
                         f' data-cited="{"true" if citing else "false"}" data-record="{e(short_hash(source["sha256"]))}"')
                items.append(f'<li{hooks}><p>{sentence}</p><p class="record-no">{self.t("sources.record_no")} <code>{record}</code>{pages} · {link}'
                             f'{copy_status(source)}{note}{details}{cited}</p></li>')
            sections.append(f'<section class="agency-document-group" id="{e(fragment)}"><h3>{heading}</h3><ul class="source-table">{"".join(items)}</ul></section>')
        howto = COPY_HOWTO if any(copy_status(s) for s in self.model["sources"]) else ""  # unchanged page without checks
        form = self.sources_filter()
        empty = (f'<p class="sources-filter-empty" id="sources-filter-empty" hidden>{self.t("sources.filter_empty")}</p>' if form else "")
        main = (f'    <section class="section" id="content"><div class="section-heading" id="library"><div><p class="eyebrow">{self.t("eyebrow.sources")}</p>'
                f'<h2>{self.t("heading.sources")}</h2></div><p class="provenance-note">{e(EXPLAINER)} {self.t("intro.sources")} '
                f'<a href="/data/sources.json" type="application/json">{self.t("intro.open_data")}</a> · '
                f'{self.csv_link("/data/sources.csv")}.</p>{howto}</div>'
                + form + ("".join(sections) or f'<p>{self.t("notice.no_sources")}</p>') + empty + '</section>\n')
        return self.page("sources", self.labels["title.sources"], self.labels["title.sources_description"], "/sources.html", main,
                         ["/site-data.js", "/sources/filter.js"] if form else [])

    def meetings_html(self):
        main = (f'    <section class="section" id="content"><div class="section-heading"><div><p class="eyebrow">{self.t("eyebrow.meetings")}</p>'
                f'<h2>{self.t("heading.meetings")}</h2></div><p>{self.t("intro.meetings")} '
                f'<a href="/data/meetings.json" type="application/json">{self.t("intro.meetings_open_data")}</a> · '
                f'{self.csv_link("/data/meetings.csv")}.'
                + (f' <a href="/meetings.xml" type="application/atom+xml">{self.t("meeting.feed")}</a>' if self.model["meetings"] else "")
                + f' <a href="/meetings.ics">{self.t("intro.meetings_calendar")}</a></p></div>'
                '<div class="meeting-list">' + ("".join(self.meeting_item(m) for m in self.upcoming) or f'<p>{self.t("notice.no_upcoming")}</p>')
                + '</div></section>\n')
        if self.past:
            main += (f'    <section class="section" id="past-meetings"><h2>{self.t("heading.past_meetings")}</h2><div class="meeting-list">'
                     + "".join(self.meeting_item(m) for m in self.past) + '</div></section>\n')
        return self.page("meetings", self.labels["title.meetings"], self.labels["title.meetings_description"], "/meetings.html", main)

    def recorded_votes(self, meeting):
        """DF0137-07: the organizer-verified per-member votes, read from the outcome's official record; "" without."""
        votes = (meeting.get("outcome") or {}).get("votes")
        if not votes:
            return ""
        rows = "".join(f'<tr><td>{e(v["name"])}</td><td>{self.t("vote." + v["vote"])}</td></tr>' for v in votes)
        source = e(meeting["outcome"]["source_url"])
        return (f'<h3>{self.t("meeting.votes")}</h3><table class="recorded-votes"><thead><tr>'
                f'<th scope="col">{self.t("meeting.votes_member")}</th><th scope="col">{self.t("meeting.votes_vote")}</th>'
                f'</tr></thead><tbody>{rows}</tbody></table><p class="notice">{self.t("meeting.votes_source")} '
                f'<a href="{source}" rel="noopener noreferrer">{source}</a></p>')

    def meeting_continuation_html(self, meeting):
        """DF0022-07: ``Continued to D`` (the stated destination of a continued item) and one ``Continued from D`` per
        earlier meeting that points here; "" when neither applies. A date-only match is the same body on that exact
        date, not a confirmed item identity. Plain links to the pages of this site; nothing is fetched or embedded."""
        meetings, lines = self.model["meetings"], []
        held = lambda m: m["status"] != "cancelled" and m["starts_at"][:10] < self.today.isoformat()  # noqa: E731

        def line(key, day, target=None, more=""):
            text = self.t(key, date="\0")
            link = f'<a href="/meetings/{e(target["id"])}.html">{e(day)}</a>' if target else e(day)
            return f'<p class="meeting-continuation">{text.replace(chr(0), link)}{more}</p>'

        outcome = meeting.get("outcome") or {}
        if held(meeting) and ("continued_to" in outcome or "continued_to_date" in outcome):
            targets = continuation_targets(meeting, meetings)
            day = outcome.get("continued_to_date") or (targets[0]["starts_at"][:10] if targets else None)
            if day is not None:
                if len(targets) > 1:  # several same-body meetings on the stated date: say so, never pick one
                    lines.append(line("meeting.continued_to", day, more="".join(
                        f' · <a href="/meetings/{e(t["id"])}.html">{self.t("meeting.page")} ({e(t["id"])})</a>' for t in targets)))
                else:
                    lines.append(line("meeting.continued_to", day, targets[0] if targets else None))
        if getattr(self, "_continuation_back", None) is None:
            self._continuation_back = continuation_back_links(meetings)
        lines += [line("meeting.continued_from", source["starts_at"][:10], source)
                  for source in self._continuation_back.get(meeting["id"], []) if held(source)]
        return "".join(lines)

    def _cosigned(self, meeting):
        """DF2207-14: ``Co-signed by N organizations`` linking supporters.html, only for ids load_content validated."""
        count = len(meeting.get("cosigners") or ())
        if not count:
            return ""
        label = self.t("meeting.cosigned_one" if count == 1 else "meeting.cosigned_many", count=count)
        return f'<p class="cosigned"><a href="/supporters.html">{label}</a></p>'

    def meeting_direction_html(self, meeting):
        """DF0022-08: ``Staff were directed to: T (by D, as stated in SOURCE)`` for a held meeting whose outcome carries a
        direction; "" otherwise. The words are the official source's, attributed to it; the source is a plain link, never
        fetched or embedded, and the line states nothing about whether staff reported back."""
        outcome = meeting.get("outcome") or {}
        if "direction" not in outcome or meeting["status"] == "cancelled" or meeting["starts_at"][:10] >= self.today.isoformat():
            return ""
        source = e(outcome["direction_source"])
        link = f'<a href="{source}" rel="noopener noreferrer">{source}</a>'
        return (f'<p class="meeting-direction">'
                f'{self.t("meeting.staff_direction", direction=e(outcome["direction"]), date=e(outcome["return_by"]), source=link)}</p>')


    def meeting_html(self, meeting):
        """``meetings/<id>.html`` (DF2022-03): one meeting with its comment kit open, linkable and printable."""
        cancelled = meeting.get("status") == "cancelled"
        agenda = (f'<p><a href="{e(meeting["agenda_url"])}" rel="noopener noreferrer">{self.t("meeting.agenda")}</a></p>'
                  if meeting["agenda_url"] else "")
        kit = (self.md(meeting["comment_kit_md"]) if meeting["comment_kit_md"]
               else f'<p>{self.t("notice.no_kit")}</p>')
        main = (f'    <article class="section finding-body" id="content"><p class="eyebrow">{self.t("eyebrow.meeting")}</p>'
                f'<h2>{self.t("meeting.cancelled") + " " if cancelled else ""}{e(meeting["body"])}</h2>'
                f'<p><time datetime="{e(meeting["starts_at"])}">{e(meeting["starts_at"].replace("T", " ")[:16])}</time></p>'
                + (f'<p>{e(meeting["location"])}</p>' if meeting["location"] else "")
                + (f'<p>{self.t("meeting.agenda_item")} {e(meeting["agenda_item"])}</p>' if meeting["agenda_item"] else "") + agenda
                + self.who_decides(meeting.get("agency_id"))
                + (meeting_outcome_html(meeting, self.t) + self.recorded_votes(meeting)
                   if meeting["starts_at"][:10] < self.today.isoformat() else "")
                + self.meeting_continuation_html(meeting)
                + self.meeting_direction_html(meeting)
                + self._written_deadline(meeting)
                + self._cosigned(meeting)
                + (f'<p class="notice">{self.t("notice.meeting_cancelled")}</p>' if cancelled else "")
                + f'<h3>{self.t("meeting.kit")}</h3>{kit}<p class="notice">{e(self.site["not_legal_advice"])}</p>'
                f'<p><a href="/meetings.html">{self.t("meeting.all")}</a></p></article>\n')
        return self.page("meetings", f'{meeting["body"]}, {meeting["starts_at"][:10]}',
                         meeting["agenda_item"] or self.labels["title.meeting_description"], f'/meetings/{meeting["id"]}.html', main)

    def about_html(self):
        contact = (f'<p>{self.t("footer.contact")} <a href="mailto:{e(self.site["contact_email"])}">{e(self.site["contact_email"])}</a></p>'
                   if self.site["contact_email"] else "")
        main = (f'    <section class="section finding-body" id="content"><p class="eyebrow">{self.t("eyebrow.about")}</p>'
                f'<h2>{self.name}</h2>{self.md(self.site["about_md"])}{contact}'
                f'<p>{self.t("intro.open_data_about")} '
                '<a href="/data/findings.json" type="application/json">data/findings.json</a> · '
                f'{self.csv_link("/data/findings.csv")} and '
                '<a href="/data/sources.json" type="application/json">data/sources.json</a> · '
                f'{self.csv_link("/data/sources.csv")}. '
                f'{self.t("intro.meetings_open_data_about")} <a href="/data/meetings.json" type="application/json">data/meetings.json</a> · '
                f'{self.csv_link("/data/meetings.csv")}.'
                + (f' {self.t("notice.no_meetings")}' if not self.model["meetings"] else "")
                + f' {self.t("intro.agencies_open_data_about")} <a href="/data/agencies.json" type="application/json">data/agencies.json</a> · '
                + f'{self.csv_link("/data/agencies.csv")}.</p>'
                + (f'<p class="map-date">{self.map_date_line()}</p>' if self.map else "") + self.license_about() +
                f'<p class="notice">{e(self.site["not_legal_advice"])}</p></section>\n')
        return self.page("about", self.labels["title.about"], self.labels["title.about_description"], "/about.html", main)

    def content_page_html(self, page):
        """``/<slug>.html`` from ``content/pages/<slug>.json`` (DF2237-07); body_md through markdown_lite."""
        main = (f'    <article class="section finding-body" id="content"><h2>{e(page["title"])}</h2>{self.md(page["body_md"])}'
                f'<p class="notice">{e(self.site["not_legal_advice"])}</p></article>\n')
        return self.page(f'page:{page["slug"]}', page["title"], page["description"] or page["title"],
                         f'/{page["slug"]}.html', main)

    def newsletters_html(self):
        """/newsletters.html (DF1952-09): public issues from content/newsletters/, newest first; no signup or footer text."""
        rows = "".join(f'<li><a href="/newsletters/{issue["sent_on"]}.html">{e(issue["title"])}</a> '
                       f'<time datetime="{issue["sent_on"]}">{issue["sent_on"]}</time></li>'
                       for issue in reversed(self.model["newsletters"]))
        main = ('    <section class="section finding-body" id="content"><h2>Newsletter archive</h2>'
                f'<p>Past issues of our email newsletter, as sent.</p><ul>{rows}</ul>'
                f'<p class="notice">{e(self.site["not_legal_advice"])}</p></section>\n')
        return self.page("newsletters", "Newsletter archive", "Past issues of our email newsletter", "/newsletters.html", main)

    def newsletter_html(self, issue):
        """/newsletters/<day>.html (DF1952-09): one issue's public Markdown through markdown_lite."""
        day = issue["sent_on"]
        main = (f'    <article class="section finding-body" id="content"><h2>{e(issue["title"])}</h2>'
                f'<p>Sent <time datetime="{day}">{day}</time></p>{self.md(issue["body_md"])}'
                f'<p><a href="/newsletters.html">All newsletter issues</a></p>'
                f'<p class="notice">{e(self.site["not_legal_advice"])}</p></article>\n')
        return self.page("newsletters", issue["title"], f"Newsletter issue sent {day}", f"/newsletters/{day}.html", main)

    def methods_html(self):
        """/methods.html (DF2037-14): label definitions from the badge tables, the record-number explainer and the
        review gate from review.ROLES. Generated only; no organizer content."""
        check_definitions()
        tables = "".join(
            f'<h3>{heading}</h3><dl>' + "".join(
                f'<dt id="{name}-{e(code)}">{self.t(name + "." + code)}</dt><dd>{e(definitions[code])}</dd>'
                for code in labels) + '</dl>'
            for name, heading, labels, definitions in (
                ("classification", "Classifications", CLASSIFICATION_LABELS, CLASSIFICATION_DEFINITIONS),
                ("confidence", "Confidence", CONFIDENCE_LABELS, CONFIDENCE_DEFINITIONS)))
        roles = sorted(ROLES)
        gate = (f'Before a finding is published it needs {", ".join(roles[:-1])} and {roles[-1]} review by at least two '
                'people other than the author. Until then the build refuses to publish it.')
        interests = ('Reviewers declare agency interests privately. A declared interest excludes their review from '
                     'findings about that agency.')
        main = ('    <section class="section finding-body" id="content"><h2>How to read our findings</h2>'
                f'{tables}<h3 id="record-numbers">Record numbers</h3><p class="provenance-note">{e(EXPLAINER)}</p>'
                f'<h3 id="review">Review before publication</h3><p>{e(gate)}</p>'
                f'<p>{e(interests)}</p>'
                f'<p class="notice">{e(self.site["not_legal_advice"])}</p></section>\n')
        return self.page("findings", "How to read our findings",
                         "What each finding label means, what record numbers show and how findings are reviewed",
                         "/methods.html", main)

    def check_cell(self, items):
        """DF1422-11: the newest organizer check as ``status · date · source`` (+ ``and N earlier``), or a dash."""
        if not items:
            return "—"
        newest = items[0]
        source = newest["source"]
        if newest["fragment"] is not None:  # a library record: its row on the sources page
            link = f'<a href="/sources.html#{e(newest["fragment"])}">{e(newest["title"])}</a>'
        else:
            link = f'<a href="{e(source)}" rel="noopener noreferrer">{e(urlsplit(source).hostname)}</a>'
        cell = (f'{self.t("map.check_" + newest["status"])} · '
                f'<time datetime="{newest["checked_on"]}">{newest["checked_on"]}</time> · {link}')
        return cell + (f' · {self.t("map.check_earlier", count=len(items) - 1)}' if len(items) > 1 else "")

    def privacy_facts(self):
        """What the pages load and where a signup goes, from the settings csp() reads (map hosts, signup URL).
        privacy_html() states only these; check_privacy() holds them against the final CSP and file list."""
        signup, hosts = self.site["signup"], set()
        if self.map:
            hosts = {self.map["style_host"], *self.map["tile_hosts"]} - {None}
        host = urlsplit(signup["url"]).hostname.lower() if signup["mode"] == "brevo_hosted" else None
        return {"first_party_only": True, "cookies": False, "analytics": False, "map_hosts": sorted(hosts),
                "signup": {"host": host, "origin": "https://" + host, "policy_url": signup["privacy_url"]} if host else None}

    def privacy_html(self):
        """/privacy.html (DF1207-16): every sentence is a ``privacy.*`` label chosen from ``privacy_facts()``."""
        facts, signup = self.privacy_facts(), self.site["signup"]
        parts = [self.t("privacy.scripts"), self.t("privacy.cookies")]
        if facts["map_hosts"]:
            parts.append(self.t("privacy.map", hosts=", ".join(e(host) for host in facts["map_hosts"])))
        if facts["signup"]:
            policy = facts["signup"]["policy_url"]
            parts.append(self.t("privacy.signup", host=e(facts["signup"]["host"])) + f" {e(signup['note'])}" + (
                f' <a href="{e(policy)}" rel="noopener noreferrer">{self.t("privacy.signup_policy")}</a>' if policy else ""))
        contact = self.site["privacy_contact"] or self.site["contact_email"]
        parts.append(f'{self.t("privacy.contact")} <a href="mailto:{e(contact)}">{e(contact)}</a>' if contact
                     else self.t("privacy.no_contact"))
        main = (f'    <section class="section finding-body" id="content"><h2>{self.t("privacy.title")}</h2>'
                f'<p>{self.t("privacy.lead")}</p>' + "".join(f"<p>{part}</p>" for part in parts)
                + f'<p class="notice">{self.t("privacy.notice")}</p></section>\n')
        return self.page("privacy", self.labels["privacy.title"], self.labels["privacy.lead"], "/privacy.html", main)

    def check_privacy(self, names, files):
        """Raise ``ContentError`` when a claim on privacy.html contradicts the final CSP or the files being built."""
        facts, directives = self.privacy_facts(), {}
        for directive in self.csp().split("; "):
            directives[directive.split()[0]] = directive.split()[1:]

        def refuse(claim, against):
            raise ContentError(f'privacy.html: "{claim}" contradicts {against}')
        for name in ("script-src", "style-src", "font-src"):
            if directives.get(name) != ["'self'"]:
                refuse("scripts, stylesheets and fonts come from this site only", " ".join([name, *directives.get(name, [])]))
        scripts = [n.lower() for n in names if n.endswith((".js", ".mjs"))] + [
            src.lower() for name, html in files.items() if name.endswith(".html")
            for src in re.findall(r'<script[^>]*\ssrc="([^"]*)"', html)]
        found = sorted(n for n in scripts if any(word in n for word in ANALYTICS_NAMES))
        if found:
            refuse("no analytics", f"the built script {found[0]}")
        if "set-cookie" in files.get("_headers", "").lower():
            refuse("no cookies", "a Set-Cookie header in _headers")
        loaded = sorted({h[len("https://"):] for d in ("img-src", "connect-src") for h in directives.get(d, []) if h.startswith("https://")})
        if loaded != facts["map_hosts"]:
            refuse("map tiles load from " + (", ".join(facts["map_hosts"]) or "no host"), "img-src/connect-src hosts: " + (", ".join(loaded) or "none"))
        wanted = ["https://" + facts["signup"]["host"]] if facts["signup"] else ["'none'"]
        if directives.get("form-action") != wanted:
            refuse("signup goes to " + (facts["signup"]["host"] if facts["signup"] else "no host"), "form-action " + " ".join(directives.get("form-action", [])))


    def cameras_html(self):
        """No-JS list of mapped points (DF1507-13): each point in the first picker city (by name) whose boundary holds it,
        by the counts' own half-open test, else in the group section. Over 2 000 points: 500 rows per section."""
        self.mapped_counts()
        cameras, boundaries = self._map_docs
        picker = {agency["slug"] for agency in self.picker_cities()}
        names = sorted({entry["name"] for slug, entry in self.cities().items() if slug in picker})
        shapes = {name: [] for name in names}
        for feature in boundaries.get("features") or []:
            name = camera_counts._name(feature) if isinstance(feature, dict) else None
            polygons = camera_counts._polygons(feature.get("geometry")) if name in shapes else ""
            for rings in [] if isinstance(polygons, str) else polygons:
                xs, ys = [p[0] for p in rings[0]], [p[1] for p in rings[0]]
                shapes[name].append(((min(xs), max(xs), min(ys), max(ys)), camera_counts._Ring(rings[0]),
                                     [camera_counts._Ring(hole) for hole in rings[1:]]))
        groups, features = {name: [] for name in names + [None]}, cameras.get("features") or []
        checks = self.map.get("point_checks") or {}  # DF1422-11: {node: [checks, newest first]}
        check_head = f'<th scope="col">{self.t("map.check_heading")}</th>' if checks else ""
        for feature in features:
            props, (x, y) = feature.get("properties") or {}, feature["geometry"]["coordinates"]
            home = next((name for name in names for (w, east, s, n), outer, holes in shapes[name]
                         if w <= x <= east and s <= y <= n and outer.contains(x, y)
                         and not any(hole.contains(x, y) for hole in holes)), None)
            url = props.get("url") if isinstance(props.get("url"), str) else None
            node = re.fullmatch(r"https://www\.openstreetmap\.org/node/(\d+)", url or "")
            cells = [e(str(props.get(key)).strip() or "not tagged") if props.get(key) is not None else "not tagged"
                     for key in ("manufacturer", "operator")]
            link = (f'<a href="{e(url)}" rel="noopener noreferrer">node {node.group(1)}</a>' if node else "no OSM link")
            extra = [self.check_cell(checks.get(int(node.group(1))) if node else None)] if checks else []
            groups[home].append((url is None, url or "", "".join(f"<td>{c}</td>" for c in cells + [link] + extra)))
        sections = ""
        for name in names + [None]:
            rows = sorted(groups[name], key=lambda row: row[:2])
            shown = rows[:500] if len(features) > 2000 else rows
            table = ('<table><thead><tr><th scope="col">Manufacturer</th><th scope="col">Operator</th>'
                     '<th scope="col">OpenStreetMap</th>' + check_head + '</tr></thead><tbody>'
                     + "".join(f"<tr>{row[2]}</tr>" for row in shown) + '</tbody></table>') if rows else ""
            more = f'<p>and {len(rows) - len(shown)} more on the map</p>' if len(shown) < len(rows) else ""
            sections += (f'<section class="camera-group"><h3>{e(name or self.map["group_label"])}</h3>'
                         f'<p>{len(rows)} mapped point{"" if len(rows) == 1 else "s"}</p>{table}{more}</section>')
        # DF0137-13: map/history.json as a plain table once it has 2+ records (else nothing: the page is unchanged);
        # city columns only when every record has by_city, a city one record lacks shows a dash
        records, history = self.map.get("history") or [], ""
        if len(records) >= 2:
            cities = sorted({n for r in records for n in r["by_city"]}) if all(
                r["by_city"] is not None for r in records) else []
            head = "".join(f'<th scope="col">{self.t("map.history_" + key)}</th>'
                           for key in ("date", "total", "added", "removed"))
            head += "".join(f'<th scope="col">{e(name)}</th>' for name in cities)
            body = "".join(f'<tr><td><time datetime="{r["recorded_on"]}">{r["recorded_on"]}</time></td>'
                           + "".join(f"<td>{r[key]}</td>" for key in ("total", "added", "removed"))
                           + "".join(f'<td>{r["by_city"].get(name, "—")}</td>' for name in cities) + "</tr>"
                           for r in records)
            history = (f'<section class="camera-history"><h3>{self.t("map.history_title")}</h3>'
                       f'<p>{self.t("map.history_note")}</p><table><thead><tr>{head}</tr></thead>'
                       f'<tbody>{body}</tbody></table></section>')
        main = ('    <section class="section" id="content"><div class="section-heading"><div><p class="eyebrow">Mapped points</p>'
                '<h2>Community-mapped points; ownership unconfirmed.</h2></div>'
                '<p>Points are community reports from OpenStreetMap, not confirmed agency ownership.</p></div>'
                f'<p>{e(self.map["attribution"])}</p><p class="map-date">{self.map_date_line()}</p>{sections}'
                f'{history}</section>\n')
        return self.page("cameras", "Mapped points", "List of community-mapped points by city; ownership unconfirmed",
                         "/cameras.html", main)

    # -- scripts and data ----------------------------------------------------

    def county_profile(self):
        profile = dict(self.site["county_profile"])
        profile["name"] = self.cfg["county"]
        if not profile["facts"]:
            count = len(self.model["findings"])
            profile["facts"] = [self.labels["js.county_findings"].format(count=count) if count else
                                self.labels["js.county_no_findings"]]
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

    def mapped_counts(self):
        """``({boundary name: points inside}, problems)``, computed once from the map files; empty without a map."""
        if not hasattr(self, "_mapped_counts"):
            docs = []
            for key in ("cameras_path", "boundary_path") if self.map else ():  # the copy's own checks, then the loader caps
                _check_copy(self.map[key], MAX_GEOJSON_BYTES)
                docs.append(_read_json(self.map[key], MAX_GEOJSON_BYTES))
            self._mapped_counts = camera_counts.count_by_name(*docs) if docs else ({}, [])
            self._map_docs = docs
        return self._mapped_counts

    def camera_notes(self):  # boundary names matching no picker city, then every boundary problem
        counts, problems = self.mapped_counts()
        picker = {agency["slug"] for agency in self.picker_cities()}
        names = {entry["name"] for slug, entry in self.cities().items() if slug in picker}
        return [f"boundary {name} matches no city agency" for name in counts if name not in names] + problems

    def cities(self):
        cities = {}
        counts = self.mapped_counts()[0] if self.map else {}
        for agency in self.picker_cities():
            entry = {"name": agency["jurisdiction_name"] or agency["name"], "displayName": agency["name"]}
            if self.map:
                city = self.map["cities"][agency["slug"]]
                entry["bounds"] = city["bounds"]
                if city["name"]:
                    entry["name"] = city["name"]
            entry.update(agency["profile"], **({"flockTransparencyUrl": agency["flock_transparency_url"]} if agency["flock_transparency_url"] else {}))
            if entry["cameraCount"] == "" and entry["name"] in counts:  # an organizer-typed value always wins
                n = counts[entry["name"]]
                entry["cameraCount"] = self.labels["js.city_points_one" if n == 1 else "js.city_points_many"].format(count=n)
            if not entry["facts"]:
                findings = self.findings_for(agency["agency_id"])
                entry["facts"] = [f["title"] for f in findings] or [self.labels["js.city_no_findings"]]
            if agency["agency_id"] in self.agency_pages():
                entry["agencyPage"] = self.agency_pages()[agency["agency_id"]]
                entry["findingCount"] = len(self.findings_for(agency["agency_id"]))
            cities[agency["slug"]] = entry
        if self.map:
            slug, label = self.map["controller"]["group_slug"], self.labels.__getitem__
            cities[slug] = {"name": self.map["group_label"], "displayName": self.map["group_label"],
                            "bounds": self.map["county_bounds"], "vendor": label("js.group_vendor"),
                            "status": label("js.group_status"), "summary": label("js.group_summary").format(name=self.map["group_label"]),
                            "cameraCount": "", "facts": [label("js.group_fact")],
                            "open": label("js.group_open"), "flags": [label("js.group_flag")]}
        return cities

    def point_checks_data(self):
        """DF1422-11: ``{node: {status, checked_on}}``, the newest check only; no note and no source leave the build."""
        return {str(node): {"status": items[0]["status"], "checked_on": items[0]["checked_on"]}
                for node, items in sorted(self.map["point_checks"].items())}

    def site_data_js(self):
        checks = (self.map or {}).get("point_checks")
        county_bounds = self.map["county_bounds"] if self.map else [[-180, -90], [180, 90]]
        style = self.map["style_url"] if self.map else ""
        return ("// Generated by campaign_tool build from content/. Reviewed campaign data only.\n"
                f"var CITIES={js_json(self.cities())};\nvar COUNTY_BOUNDS={js_json(county_bounds)};\n"
                f"var MAP_STYLE={js_json(style)};\nvar CAMERA_DATA_URL=\"/data/cameras.geojson\";\n"
                "var BOUNDARY_DATA_URL=\"/data/county.geojson\";\n"
                + (f"var pointChecks={js_json(self.point_checks_data())};\n" if checks else "")
                + (f"var LABELS={js_json(labels)};\n" if (labels := self.js_labels or self.sources_labels()) else ""))

    def app_js(self):
        if self.map:
            controller = map_controller.render(self.map["controller"], "",
                                               point_check_renderer(self.labels) if self.map.get("point_checks") else "",
                                               relabel=bool(self.js_labels))
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
        paths = ["/", "/agencies.html", "/findings/", "/corrections.html", "/sources.html", "/meetings.html", "/about.html",
                 "/methods.html", "/privacy.html", "/search.html", "/findings/packet-all.html", "/timeline.html"]
        paths += ["/cameras.html"] if self.map else []
        paths += ["/glossary.html"] if self.glossary else []
        paths += ["/faq.html"] if self.faq else []  # DF0022-01
        if self.topics:  # DF1952-06: the index file topics/index.html is served at /topics/
            paths += ["/topics/"] + [f'/topics/{t["slug"]}.html' for t in self.topics]
        paths += [f'/{p["slug"]}.html' for p in self.model.get("pages", [])]
        paths += ["/supporters.html"] if self.supporters else []  # DF2207-13
        if self.model.get("newsletters"):  # DF1952-09: only with public issues
            paths += ["/newsletters.html"] + [f'/newsletters/{i["sent_on"]}.html' for i in reversed(self.model["newsletters"])]
        paths += list(self.agency_pages().values())
        paths += [f'/findings/{f["slug"]}.html' for f in self.model["findings"]]
        paths += [f'/meetings/{m["id"]}.html' for m in self.model["meetings"]]
        paths += list(self.record_pages().values())  # DF1103-11: library order, last
        return paths

    def sitemap_xml(self):
        """Only built with a base_url (absolute <loc>); lastmod = UTC day of each finding's latest change."""
        root = ET.Element("urlset", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")
        days = {f'/findings/{f["slug"]}.html': utc_moment(finding_updated(f)).astimezone(timezone.utc).date().isoformat()
                for f in self.model["findings"]}
        days["/"] = days["/findings/"] = days["/findings/packet-all.html"] = max(days.values(), default="")
        for path in self.page_paths():
            url = ET.SubElement(root, "url")
            ET.SubElement(url, "loc").text = self.base + path
            if days.get(path):
                ET.SubElement(url, "lastmod").text = days[path]
        return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"

    def feed_xml(self):
        return self._atom(self.cfg["name"] + " findings", "/feed.xml", "/", "", self.model["findings"])

    def agency_feeds(self):
        """``{"findings/agency/<slug>.xml": atom}`` for each agency with a published finding, model order."""
        feeds = {}
        for agency in self.model["agencies"]:
            findings = self.findings_for(agency["agency_id"])
            if findings:
                slug = agency["slug"]
                feeds[f"findings/agency/{slug}.xml"] = self._atom(
                    f'{self.cfg["name"]} findings: {agency["name"]}', f"/findings/agency/{slug}.xml",
                    f"/agencies.html#{slug}", f":agency:{slug}", findings)
        return feeds

    def _atom(self, title, self_path, alternate_path, urn_suffix, findings):
        ns = "http://www.w3.org/2005/Atom"
        ET.register_namespace("", ns)
        feed = ET.Element(f"{{{ns}}}feed")
        ET.SubElement(feed, f"{{{ns}}}title").text = title
        campaign_urn = "urn:deflock-campaign:" + re.sub(r"[^a-z0-9]+", "-", self.cfg["name"].lower())
        ET.SubElement(feed, f"{{{ns}}}id").text = (self.base + self_path) if self.base else campaign_urn + urn_suffix
        ET.SubElement(feed, f"{{{ns}}}link", href=self.base + self_path, rel="self")
        ET.SubElement(feed, f"{{{ns}}}link", href=self.base + alternate_path)
        latest = max((finding_updated(f) for f in findings), key=utc_moment, default=None)
        ET.SubElement(feed, f"{{{ns}}}updated").text = latest or datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat()
        ET.SubElement(ET.SubElement(feed, f"{{{ns}}}author"), f"{{{ns}}}name").text = self.cfg["name"]
        for finding in findings:
            entry = ET.SubElement(feed, f"{{{ns}}}entry")
            ET.SubElement(entry, f"{{{ns}}}title").text = finding["title"]
            ET.SubElement(entry, f"{{{ns}}}id").text = "urn:deflock-finding:" + finding["id"]
            ET.SubElement(entry, f"{{{ns}}}link", href=self.base + finding_path(finding))
            ET.SubElement(entry, f"{{{ns}}}published").text = finding["published_at"]
            ET.SubElement(entry, f"{{{ns}}}updated").text = finding_updated(finding)
            corrected = max((_day(c["date"]) for c in finding["corrections"]), default=None)
            ET.SubElement(entry, f"{{{ns}}}summary").text = (f"Corrected {corrected}. " if corrected else "") + (
                f'[{CLASSIFICATION_LABELS[finding["classification"]]}; {CONFIDENCE_LABELS[finding["confidence"]]}; '
                f'event date {finding["event_date"]}] {finding["summary"]}')
            ET.SubElement(entry, f"{{{ns}}}category", term=finding["classification"])
        return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(feed, encoding="unicode") + "\n"

    def meetings_ics(self):
        """Upcoming (not cancelled) meetings as an RFC 5545 calendar; deterministic for one build day."""
        host = urlsplit(self.base).hostname if self.base else "deflock-campaign.invalid"
        stamp = self.today.strftime("%Y%m%d") + "T000000Z"
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//DeFlock Campaign Tool//meetings//EN", "CALSCALE:GREGORIAN",
                 "X-WR-CALNAME:" + ics_text(self.cfg["name"] + " meetings")]
        for meeting in self.upcoming:
            start = datetime.fromisoformat(meeting["starts_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
            lines += ["BEGIN:VEVENT", f'UID:meeting-{meeting["id"]}@{host}', "DTSTAMP:" + stamp,
                      "DTSTART:" + start.strftime("%Y%m%dT%H%M%SZ"), "SUMMARY:" + ics_text(meeting["body"])]
            if meeting["location"]:
                lines.append("LOCATION:" + ics_text(meeting["location"]))
            if meeting["agenda_url"]:
                lines.append("URL:" + meeting["agenda_url"])
            link = f'{self.base}/meetings.html#meeting-{meeting["id"]}'
            lines += ["DESCRIPTION:" + ics_text((meeting["agenda_item"] + "\n" if meeting["agenda_item"] else "") + link),
                      "END:VEVENT"]
        lines.append("END:VCALENDAR")
        return "".join(ics_fold(line) + "\r\n" for line in lines)

    def meetings_feed_entries(self):
        """DF1103-10: the staged meetings the feed lists, newest ``updated`` first, one per id, as plain dicts.

        Scheduled meetings on or after the build day, every cancelled meeting, and every meeting with a recorded
        outcome. Only public staged fields are read; ``updated`` is the outcome date or the stored start, never a clock."""
        today, seen, entries = self.today.isoformat(), set(), []
        for meeting in self.model["meetings"]:
            outcome = meeting.get("outcome")
            cancelled = meeting.get("status") == "cancelled"
            if meeting["id"] in seen or not (cancelled or outcome or meeting["starts_at"][:10] >= today):
                continue
            seen.add(meeting["id"])
            title = meeting["body"] + (f': {meeting["agenda_item"]}' if meeting["agenda_item"] else "")
            summary = f'Starts: {meeting["starts_at"]}. Body: {meeting["body"]}.'
            summary += f' Agenda item: {meeting["agenda_item"]}.' if meeting["agenda_item"] else ""
            summary += " Cancelled." if cancelled else ""
            summary += f' Agenda: {meeting["agenda_url"]}.' if meeting["agenda_url"] else ""
            if outcome:
                summary += f' Outcome: {OUTCOME_LABELS[outcome["result"]]} on {outcome["on"]}.'
                summary += f' Vote: {outcome["vote"]}.' if outcome.get("vote") else ""
            updated = outcome["on"] + "T00:00:00+00:00" if outcome else meeting["starts_at"]
            if not ATOM_TIMESTAMP.fullmatch(updated):  # a stored start without seconds is still a valid instant
                updated = utc_moment(updated).isoformat()
            entries.append({"id": "urn:deflock-meeting:" + meeting["id"], "title": ("Cancelled: " if cancelled else "") + title,
                            "path": f'/meetings/{meeting["id"]}.html', "agenda_url": meeting["agenda_url"],
                            "source_url": outcome["source_url"] if outcome else None, "summary": summary,
                            "updated": updated, "key": meeting["id"]})
        entries.sort(key=lambda entry: (-utc_moment(entry["updated"]).timestamp(), entry["key"]))
        return entries

    def meetings_feed_xml(self):
        """``meetings.xml`` (DF1103-10): Atom for the staged meetings; English like the other non-page outputs."""
        ns = "http://www.w3.org/2005/Atom"
        ET.register_namespace("", ns)
        entries = self.meetings_feed_entries()
        feed = ET.Element(f"{{{ns}}}feed")
        ET.SubElement(feed, f"{{{ns}}}title").text = self.cfg["name"] + " meetings"
        campaign_urn = "urn:deflock-campaign:" + re.sub(r"[^a-z0-9]+", "-", self.cfg["name"].lower())
        ET.SubElement(feed, f"{{{ns}}}id").text = (self.base + "/meetings.xml") if self.base else campaign_urn + ":meetings"
        ET.SubElement(feed, f"{{{ns}}}link", href=self.base + "/meetings.xml", rel="self")
        ET.SubElement(feed, f"{{{ns}}}link", href=self.base + "/meetings.html", rel="alternate")
        latest = max((utc_moment(entry["updated"]) for entry in entries), default=datetime(2000, 1, 1, tzinfo=timezone.utc))
        ET.SubElement(feed, f"{{{ns}}}updated").text = latest.astimezone(timezone.utc).isoformat()
        ET.SubElement(ET.SubElement(feed, f"{{{ns}}}author"), f"{{{ns}}}name").text = self.cfg["name"]
        for item in entries:
            entry = ET.SubElement(feed, f"{{{ns}}}entry")
            ET.SubElement(entry, f"{{{ns}}}title").text = item["title"]
            ET.SubElement(entry, f"{{{ns}}}id").text = item["id"]
            ET.SubElement(entry, f"{{{ns}}}link", href=self.base + item["path"], rel="alternate")
            for url in (item["agenda_url"], item["source_url"]):
                if url:
                    ET.SubElement(entry, f"{{{ns}}}link", href=url, rel="related")
            ET.SubElement(entry, f"{{{ns}}}updated").text = item["updated"]
            ET.SubElement(entry, f"{{{ns}}}summary", type="text").text = item["summary"]
        return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(feed, encoding="unicode") + "\n"

    def robots_txt(self):
        return "User-agent: *\nAllow: /\n" + (f"Sitemap: {self.base}/sitemap.xml\n" if self.base else "")

    def _public_finding(self, finding):
        """One published finding, copied key by key from the allowlists (never a dict copy)."""
        value = {key: finding[key] for key in PUBLIC_FINDING_FIELDS
                 if key not in ("sources", "corrections", "url", "citation", "also_agency_ids", "agency_response")}
        if finding.get("also_agency_ids") is not None:  # DF1952-07: optional; omitted entirely when absent
            value["also_agency_ids"] = list(finding["also_agency_ids"])
        value["limitations"], value["counterevidence"] = list(finding["limitations"]), list(finding["counterevidence"])
        value["sources"] = [{"sha256_prefix": short_hash(s["sha256"]), "title": s["title"], "locator": s["locator"],
                             "rule_id": s["rule_id"], **({"page": s["page"]} if s.get("page") is not None else {}),
                             **({"quote": s["quote"]} if s.get("quote") else {})}  # DF1952-08: optional, absent stays absent
                            for s in finding["sources"]]
        value["corrections"] = [{"date": c["date"], "note": c["note"]} for c in finding["corrections"]]
        if "updates" in finding:  # DF0022-06: optional, copied through an explicit allowlist; absent stays absent
            value["updates"] = [{key: u[key] for key in PUBLIC_UPDATE_FIELDS} for u in finding["updates"]]
        value["url"] = f'{self.base}/findings/{finding["slug"]}.html'
        value["citation"] = citation(finding, self.cfg["name"], self.base)
        reply = finding.get("agency_response")
        if reply and reply["status"] != "pending":  # absent or pending: no key at all, so legacy rows are unchanged
            value["agency_response"] = {
                **{key: reply[key] for key in ("status", "asked_on", "reply_by", "on")},
                **({"text": reply["text"]} if "text" in reply else {}),
                **({"record_sha256_prefix": short_hash(reply["record_sha256"])} if "record_sha256" in reply else {})}
        return value

    def findings_json(self):
        """``data/findings.json``: published findings as open data; refuses any non-public key or full hash."""
        return json.dumps(self._findings_doc(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"

    def findings_csv(self):
        """``data/findings.csv`` (DF1207-14): the rows of the built findings document, one column per public key."""
        return _csv_text(FINDINGS_CSV_COLUMNS, [_flat_finding(f) for f in self._findings_doc()["findings"]])

    def _findings_doc(self):
        doc = {"schema_version": 1, "campaign": self.cfg["name"], "generated_from": "content/findings",
               "base_url": self.base or None, "url_kind": "absolute" if self.base else "relative",
               "findings": [self._public_finding(f) for f in self.model["findings"]]}
        finding = (PUBLIC_FINDING_FIELDS + ("updates",),
                   {"sources": (PUBLIC_SOURCE_FIELDS, {}), "corrections": (PUBLIC_CORRECTION_FIELDS, {}),
                    "updates": (PUBLIC_UPDATE_FIELDS, {}), "agency_response": (PUBLIC_RESPONSE_FIELDS, {})})
        _assert_public(doc, "data/findings.json", PUBLIC_TOP_LEVEL_FIELDS + self.license_top(), {"findings": finding})
        return self.stamp_license(doc)

    def _public_library_entry(self, source):
        """One library entry, copied key by key (never ``note``, ``fragment`` or the full sha256)."""
        agency = self.agencies_by_id.get(source["agency_id"])
        return {"sha256_prefix": short_hash(source["sha256"]), "title": source["title"], "agency_id": source["agency_id"],
                "agency_name": agency["name"] if agency else None, "received_at": source["received_at"],
                "public_url": source["public_url"] or None, "pages": source["pages"],
                "cited_by": [f["id"] for f in self.cited_by.get(source["sha256"], [])],
                "public_copy": source.get("public_copy") if source["public_url"] else None}

    def sources_json(self):
        """``data/sources.json``: the source library as open data; refuses any non-public key or full hash."""
        return json.dumps(self._sources_doc(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"

    def sources_csv(self):
        """``data/sources.csv`` (DF1207-14): the rows of the built source-library document."""
        return _csv_text(SOURCES_CSV_COLUMNS, [_flat_source(s) for s in self._sources_doc()["sources"]])

    def _sources_doc(self):
        doc = {"schema_version": 1, "campaign": self.cfg["name"], "generated_from": "content/sources.json",
               "base_url": self.base or None, "url_kind": "absolute" if self.base else "relative",
               "sources": [self._public_library_entry(s) for s in self.model["sources"]]}
        _assert_public(doc, "data/sources.json", PUBLIC_LIBRARY_TOP_LEVEL + self.license_top(),
                       {"sources": (PUBLIC_LIBRARY_FIELDS, {"public_copy": (PUBLIC_COPY_FIELDS, {})})})
        return self.stamp_license(doc)

    def _public_meeting(self, meeting):
        """One staged meeting, copied key by key: what its page shows, never the kit, participation or verifier."""
        value = {"id": meeting["id"], "body": meeting["body"], "agency_id": meeting.get("agency_id"),
                 "starts_at": meeting["starts_at"], "status": meeting["status"], "agenda_url": meeting["agenda_url"],
                 "url": f'/meetings/{meeting["id"]}.html'}
        outcome = meeting.get("outcome")
        if outcome and meeting["status"] != "cancelled" and meeting["starts_at"][:10] < self.today.isoformat():
            value["outcome"] = {key: outcome[key] for key in ("result", "on", "source_url", "vote", "minutes_url",
                                                              "minutes_approved_on", "continued_to",
                                                              "continued_to_date", "direction", "return_by",
                                                              "direction_source") if key in outcome}
            if "votes" in outcome:  # DF0137-07 per-member votes, in stored order
                value["outcome"]["votes"] = [{"name": v["name"], "vote": v["vote"]} for v in outcome["votes"]]
        return value

    def meetings_json(self):
        """``data/meetings.json`` (DF0137-09): staged meetings with outcomes and votes, newest first; refuses any other key."""
        ordered = sorted(self.model["meetings"], key=lambda m: (utc_moment(m["starts_at"]), m["id"]), reverse=True)
        doc = {"schema_version": 1, "generated_at": f"{self.today.isoformat()}T00:00:00Z",
               "meetings": [self._public_meeting(m) for m in ordered]}
        vote = (PUBLIC_VOTE_FIELDS, {})
        _assert_public(doc, "data/meetings.json", PUBLIC_MEETING_TOP_LEVEL | set(self.license_top()),
                       {"meetings": (PUBLIC_MEETING_FIELDS, {"outcome": (PUBLIC_OUTCOME_FIELDS, {"votes": vote})})})
        return json.dumps(self.stamp_license(doc), ensure_ascii=False, sort_keys=True, indent=2) + "\n"


    def _public_agency(self, agency, cities):
        """One agency with a public page, copied key by key: what its card and page show, never contacts or notes."""
        asked = sorted(agency.get("asked", []), key=lambda row: row["asked_on"], reverse=True)  # the page's order
        newest, policy, contract = (agency.get("transparency") or [None])[0], agency.get("alpr_policy"), agency["contract"]
        return {"agency_id": agency["agency_id"], "name": agency["name"], "kind": agency["kind"],
                "url": self.base + self.agency_pages()[agency["agency_id"]],
                "asked": [{"topic": a["topic"], "state": a["state"], "asked_on": a["asked_on"],
                           "updated_on": a.get("updated_on")} for a in asked],
                "alpr_policy": {k: policy[k] for k in PUBLIC_POLICY_FIELDS} if policy else None,
                "contract": ({**{k: contract[k] for k in PUBLIC_CONTRACT_FIELDS},
                              **({"cost": {k: contract["cost"][k] for k in PUBLIC_COST_FIELDS}} if contract.get("cost") else {})}
                             if contract else None),
                "transparency": ({k: newest[k] for k in PUBLIC_TRANSPARENCY_FIELDS if newest.get(k) is not None}
                                 if newest else None),
                **({"funding": [{k: f[k] for k in PUBLIC_FUNDING_FIELDS if k in f} for f in agency["funding"]]}
                   if agency.get("funding") else {}),  # DF2207-02: only when the organizer recorded it
                "camera_count": cities.get(agency["slug"], {}).get("cameraCount") or None}

    def agencies_json(self):
        """``data/agencies.json`` (DF1422-12): agencies with a public page as open data; refuses any other key."""
        cities = self.cities()
        listed = sorted((a for a in self.model["agencies"] if a["agency_id"] in self.agency_pages()),
                        key=lambda a: (a["name"], a["agency_id"]))
        doc = {"schema_version": 1, "campaign": self.cfg["name"], "generated_from": "content/agencies.json",
               "base_url": self.base or None, "url_kind": "absolute" if self.base else "relative",
               "agencies": [self._public_agency(a, cities) for a in listed]}
        _assert_public(doc, "data/agencies.json", PUBLIC_AGENCY_TOP_LEVEL | set(self.license_top()),
                       {"agencies": (PUBLIC_AGENCY_FIELDS + PUBLIC_AGENCY_OPTIONAL,
                                     {"asked": (PUBLIC_ASKED_FIELDS, {}),
                                                            "alpr_policy": (PUBLIC_POLICY_FIELDS, {}),
                                                            "contract": (PUBLIC_CONTRACT_FIELDS + ("cost",), {"cost": (PUBLIC_COST_FIELDS, {})}),
                                                            "funding": (PUBLIC_FUNDING_FIELDS, {}),
                                                            "transparency": (PUBLIC_TRANSPARENCY_FIELDS, {})})})
        return json.dumps(self.stamp_license(doc), ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _text(value):
    """``None`` is empty, a list is ``; ``-joined, a bool is true/false, anything else its string."""
    if value is None:
        return ""
    if isinstance(value, list):
        return "; ".join(_text(item) for item in value)
    return ("true" if value else "false") if isinstance(value, bool) else str(value)


def _cell(value):
    """One CSV cell: the flattened text, with the formula guard the kit CSV uses applied once to the whole cell."""
    from .kit_export import formula_safe  # local, as the other optional site steps
    return formula_safe(_text(value))


def _flat_finding(finding):
    """One findings.json record as a ``{column: cell}`` row (sources by prefix, corrections as ``date: note``)."""
    row = {key: finding.get(key) if key in ("also_agency_ids", "agency_response") else finding[key]
           for key in FINDINGS_CSV_COLUMNS}
    if row["agency_response"] is not None:
        row["agency_response"] = json.dumps(row["agency_response"], ensure_ascii=False, sort_keys=True)
    row["sources"] = [source["sha256_prefix"] for source in finding["sources"]]
    row["corrections"] = [f'{c["date"]}: {c["note"]}' for c in finding["corrections"]]
    return {key: _cell(value) for key, value in row.items()}


def _flat_source(entry):
    """One sources.json record as a ``{column: cell}`` row (``public_copy`` spread over three columns)."""
    copy = entry["public_copy"] or {}
    row = {key: entry[key] for key in PUBLIC_LIBRARY_FIELDS if key != "public_copy"}
    row.update({f"public_copy_{key}": copy.get(key) for key in PUBLIC_COPY_FIELDS})
    return {key: _cell(row[key]) for key in SOURCES_CSV_COLUMNS}


def _record_cell(value):
    """One agency or meeting CSV cell (DF2207-11) from a parsed public JSON value, with the shared formula guard.

    Strings stay as they are, numbers use their JSON spelling, ``true``/``false``, null is empty; a list joins its items with
    ``; `` (string items as text, others as compact JSON) and an object is one compact JSON cell."""
    from .kit_export import formula_safe  # local, as the other optional site steps

    def compact(item):
        return json.dumps(item, ensure_ascii=False, separators=(",", ":"))

    def text(item):
        return item if isinstance(item, str) else compact(item)
    if value is None:
        return ""
    return formula_safe("; ".join(text(item) for item in value) if isinstance(value, list) else text(value))


def public_records_csv(json_text, record_key, fields):
    """CSV text (DF2207-11) for the records of an already-built, allowlist-checked open-data JSON text.

    Columns are the allowed record keys in the order the JSON writer serializes them (``sort_keys``), also for no records and
    for optional keys the first record lacks; a record is one row in array order, a missing key is an empty cell."""
    columns = sorted(fields)
    records = json.loads(json_text)[record_key]
    return _csv_text(columns, [{column: _record_cell(record.get(column)) for column in columns} for record in records])


def _csv_text(columns, rows):
    """RFC 4180 text (CRLF, header first); the file is written as UTF-8 without a byte-order mark."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(columns)
    writer.writerows([row[column] for column in columns] for row in rows)
    return out.getvalue()


def _assert_public(doc, name, top, nested):
    """Raise ``ContentError`` naming ``name`` if ``doc`` holds a key outside the public allowlists (``top`` at the top
    level, ``nested`` maps a key to its ``(allowed, nested)`` pair) or a 40+ hex string."""
    def walk(value, allowed, nested):
        if isinstance(value, dict):
            for key, item in value.items():
                if key not in allowed:
                    raise ContentError(f"{name}: non-public key {key}")
                walk(item, *nested.get(key, ((), {})))
        elif isinstance(value, list):
            for item in value:
                walk(item, allowed, nested)
        elif isinstance(value, str) and FULL_HASH.search(value):
            raise ContentError(f"{name}: a string holds a full hash; publish 12-character prefixes only")
    walk(doc, top, nested)


# --- file emission -----------------------------------------------------------

def _write(path, text, written):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    written.append(path)


def _check_copy(source, limit):
    if source.is_symlink():
        raise ContentError(f"{source}: symlinks are not copied")
    if source.stat().st_size > limit:
        raise ContentError(f"{source.name}: exceeds the {limit // (1024 * 1024)} MiB copy limit")


def _copy(source, target, written, limit):
    _check_copy(source, limit)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    written.append(target)


def _asset_plan(model):
    from .image_meta import check_asset  # local: the only site.py change for the image-metadata gate
    from .doc_assets import check_asset as check_doc_asset
    assets_dir, plan = model["assets_dir"], []
    page_files = {f'{page["slug"]}.html' for page in model.get("pages", [])}
    for source in model["public_assets"]:
        relative = source.relative_to(assets_dir)
        if source.suffix.lower() not in ASSET_EXTENSIONS:
            raise ContentError(f"public-assets/{relative}: extension not allowed "
                               f"({', '.join(sorted(ASSET_EXTENSIONS))})")
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}", part) for part in relative.parts):
            raise ContentError(f"public-assets/{relative}: use plain file and folder names")
        if (relative.parts[0] in GENERATED_TOP_LEVEL or relative.parts[0] in page_files
                or (model.get("topics") and relative.parts[0] == "topics")):  # DF1952-06: reserved only with topics
            raise ContentError(f"public-assets/{relative}: collides with a generated path")
        check_asset(source, relative, ASSET_MAX_BYTES)
        check_doc_asset(source, relative, ASSET_MAX_BYTES)  # PDF/SVG: active content, revisions, author fields
        plan.append((source, relative))
    return plan


PRIVACY_GATE_PARTS = ("meetings", "pages", "newsletters", "topics", "glossary", "supporters", "officials", "map")
# Fields an organizer fills with a contact on purpose, or that are never published: not scanned. A library entry is scanned
# through its public projection only (never note, fragment or the full hash); an agency without its private contact and notes.
PRIVACY_GATE_SKIP = {"how", "contact_email", "privacy_contact"}
PRIVACY_GATE_AGENCY_SKIP = {"records_email", "notes", "muckrock_agency_id", "request"}
PRIVACY_GATE_LIBRARY = ("title", "agency_id", "received_at", "public_url", "pages", "public_copy")


def _privacy_gate(model, cfg=None):
    """Refuse the build when an email, phone or plate pattern sits in published text (the shared privacy lint, before anything is
    rendered or written). Names the category and the place, never the text. Scans the content parts, the source library's public
    fields, the agencies without their private contact and notes, site.json and the campaign name; the fields an organizer fills
    with a contact on purpose (a meeting's ``how``, the contact and privacy-contact addresses) are not scanned."""
    def walk(value, where, skip=PRIVACY_GATE_SKIP):
        if isinstance(value, dict):
            for key, item in value.items():
                if key not in skip:
                    yield from walk(item, f"{where}.{key}", skip)
        elif isinstance(value, (list, tuple)):
            for number, item in enumerate(value):
                yield from walk(item, f"{where}[{number}]", skip)
        elif isinstance(value, str):
            yield where, value
    parts = [(part, model.get(part)) for part in PRIVACY_GATE_PARTS]
    # Only this exact built-in illustrative address is intentional. Overrides (including other labels) still run the lint.
    parts.append(("labels", {key: value for key, value in (model.get("labels") or {}).items()
                             if not (key == "signup.placeholder" and value == "you@example.org")}))
    # A finding is scanned through its declared fields only: an undeclared key on the model is never published.
    parts.append(("findings", [{key: value for key, value in finding.items() if key in FINDING_KEYS}
                               for finding in model.get("findings") or ()]))
    parts.append(("sources", [{key: source.get(key) for key in PRIVACY_GATE_LIBRARY} for source in model.get("sources") or ()]))
    parts += [("site", model.get("site")), ("campaign", {key: (cfg or {}).get(key) for key in ("name", "county", "state")})]
    for key in ("cameras_path", "boundary_path", "county_boundary_path"):
        path = (model.get("map") or {}).get(key)
        if path:
            _check_copy(path, MAX_GEOJSON_BYTES)
            # Decode JSON escapes, include property keys and never apply the meeting/contact field exemptions to copies.
            text = json.dumps(_read_json(path, MAX_GEOJSON_BYTES), ensure_ascii=False)
            if note := refusal("map." + key, text):
                raise ContentError(note)
    for part, value in parts + [("agencies", model.get("agencies"))]:
        for where, text in walk(value, part, PRIVACY_GATE_SKIP | PRIVACY_GATE_AGENCY_SKIP if part == "agencies" else PRIVACY_GATE_SKIP):
            if note := refusal(where, text):
                raise ContentError(note)


def _plan_site(root, today=None, notes=None):
    """Render every page and check every copy without writing: ``(files, copies)``, or None without content/."""
    from .cli import read_config
    root = Path(root)
    cfg = read_config(root)
    if (root / "public").is_symlink():
        raise ContentError("public/ must not be a symlink")
    model = load_content(root, today)
    if model is None:
        return None
    _privacy_gate(model, cfg)
    site = Site(root, cfg, model, today=today or date.today())
    # Render everything before touching public/ so a refused page leaves no partial site.
    pages = {
        "index.html": site.index_html(), "agencies.html": site.agencies_html(),
        "findings/index.html": site.findings_index_html(), "findings/packet-all.html": site.packet_html(), "sources.html": site.sources_html(),
        "meetings.html": site.meetings_html(), "about.html": site.about_html(),
        "corrections.html": site.corrections_html(), "404.html": site.not_found_html(), "methods.html": site.methods_html(), "search.html": site.search_html(),
        "timeline.html": site.timeline_html(), "privacy.html": site.privacy_html(),
    }

    for meeting in model["meetings"]:
        pages[f'meetings/{meeting["id"]}.html'] = site.meeting_html(meeting)
    for page in model["pages"]:
        pages[f'{page["slug"]}.html'] = site.content_page_html(page)
    if site.supporters:  # DF2207-13: no rows, no page
        pages["supporters.html"] = site.supporters_html()
    if site.glossary:  # DF1952-04: the slug is reserved, so no content page can already hold this name
        pages["glossary.html"] = site.glossary_html()
    if site.faq:  # DF0022-01: the slug is reserved, so no content page can already hold this name
        pages["faq.html"] = site.faq_html()

    if model["newsletters"]:  # DF1952-09: newest first; the slug is reserved, so nothing else can own these paths
        pages["newsletters.html"] = site.newsletters_html()
        for issue in model["newsletters"]:
            pages[f'newsletters/{issue["sent_on"]}.html'] = site.newsletter_html(issue)
    if site.topics:  # DF1952-06
        pages["topics/index.html"] = site.topics_index_html()
        for topic in site.topics:
            pages[f'topics/{topic["slug"]}.html'] = site.topic_html(topic)
    for agency in model["agencies"]:
        if agency["agency_id"] in site.agency_pages():
            pages[site.agency_pages()[agency["agency_id"]][1:]] = site.agency_html(agency)
    if site.map:
        if "cameras.html" in pages:  # content/pages/cameras.json would otherwise be silently replaced
            raise ContentError("content/pages/cameras.json: collides with the generated cameras.html (the map's point list)")
        pages["cameras.html"] = site.cameras_html()
    for source in model["sources"]:  # DF1103-11: one page per library entry; the prefixes were checked in Site()
        pages[site.record_pages()[source["sha256"]][1:]] = site.record_html(source)
    for finding in model["findings"]:
        path = f'findings/{finding["slug"]}.html'
        if path in pages:
            raise ContentError(f'published finding {finding["id"]} ({path}): collides with generated page {path}')
        pages[path] = site.finding_html(finding)
    brand = model["site"].get("brand", {})  # site.json brand: one appended rule per set part, nothing without it
    colours = ";".join(f"--{var}:{brand[key]}" for key, var in (("accent", "coral"), ("ink", "ink")) if key in brand)
    css = (pages["index.html"]["css"] + EXTRA_CSS + (f":root{{{colours}}}\n" if colours else "")
           + (".brand-logo{max-height:48px;width:auto;height:auto}\n" if "logo" in brand else ""))
    files = {name: page["html"] for name, page in pages.items()}
    agencies_text, meetings_text = site.agencies_json(), site.meetings_json()  # each rendered once; its CSV is read back from it
    files.update({
        "style.css": css, "_headers": site.headers(), "robots.txt": site.robots_txt(),
        **({"sitemap.xml": site.sitemap_xml()} if site.base else {}), "feed.xml": site.feed_xml(),
        "meetings.ics": site.meetings_ics(), "site-data.js": site.site_data_js(), "search.js": search_js(),
        "agency-cards.js": agency_cards.render(site.cards_config(), relabel=bool(site.js_labels)),
        "app.js": site.app_js(), "data/findings.json": site.findings_json(), "data/findings.csv": site.findings_csv(),
        "data/sources.json": site.sources_json(), "data/sources.csv": site.sources_csv(), "data/meetings.json": meetings_text,
        "data/agencies.json": agencies_text,
        "data/meetings.csv": public_records_csv(meetings_text, "meetings", PUBLIC_MEETING_FIELDS),
        "data/agencies.csv": public_records_csv(agencies_text, "agencies", PUBLIC_AGENCY_FIELDS + PUBLIC_AGENCY_OPTIONAL),
    })
    if site.finding_filter():  # written only when the findings index loads it
        files["findings/filter.js"] = filter_js(bool(site.js_labels), site.multi_agency)
    if model["findings"]:  # written only when a finding page loads it
        files["findings/cite.js"] = CITE_JS
    if site.sources_filter():  # DF1103-12: written only when sources.html loads it; an empty library leaves no stale asset
        files["sources/filter.js"] = site.sources_filter_js()
    files.update(site.agency_feeds())
    if model["meetings"]:  # DF1103-10: no staged meetings, no feed (the tree swap drops a stale one)
        files["meetings.xml"] = site.meetings_feed_xml()
    plan = []
    if model["map"]:
        found = model["map"]
        files["data/cameras.geojson"] = site.cameras_geojson()  # DF1422-13: ODbL + OSM attribution members, always
        plan = [(found[key], relative, 8 * 1024 * 1024) for key, relative in (
            ("boundary_path", found["controller"]["city_boundaries_url"]),
            ("county_boundary_path", "data/county.geojson")) if found[key]]
        plan += [(path, f"vendor/{name}", VENDOR_MAX_BYTES) for name, path in model["vendor"].items()]
    plan += [(source, relative, ASSET_MAX_BYTES) for source, relative in _asset_plan(model)]
    for source, _, limit in plan:  # every copy is checked before the filesystem changes
        _check_copy(source, limit)
    if "redirects" in model:  # DF2037-16: any present file, even JSON null, is checked against every path written
        from .findings import withdrawn_names  # local, as the other optional site steps
        written = [*files, *(Path(relative).as_posix() for _, relative, _ in plan)]
        files["_redirects"] = load_redirects(model["redirects"], written, withdrawn_names(root)[1])
        if not files["_redirects"]:  # an empty list writes no file
            del files["_redirects"]
    site.check_privacy([*files, *(Path(relative).as_posix() for _, relative, _ in plan)], files)  # before anything is written
    if isinstance(notes, list):
        notes.extend(site.camera_notes())
    return files, plan


def build_site(root, today=None, notes=None):
    """Build ``<root>/public`` from ``<root>/content``; returns the list of written paths.

    ``today`` (a ``datetime.date``, default the build date) splits meetings into
    upcoming (on or after it) and past. ``notes``, when a list, receives
    boundary names that match no city agency and boundary feature problems.

    Falls back to the neutral starter when ``content/`` does not exist.
    Raises ``ContentError``/``ValueError`` and writes nothing when content is
    not publishable.
    """
    from .cli import read_config
    root = Path(root)
    public = root / "public"
    planned = _plan_site(root, today, notes)
    if planned is None:
        return build_starter(read_config(root), public)
    files, plan = planned
    staging = Path(tempfile.mkdtemp(dir=root, prefix=".public.new-"))  # swapped in only when complete
    new, written = staging / "public", []
    try:
        new.mkdir()
        for name, text in files.items():
            _write(new / name, text, written)
        for source, relative, limit in plan:
            _copy(source, new / relative, written, limit)
        if public.exists():
            old = public.rename(root / f".public.old-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}")
            try:
                new.rename(public)
            except BaseException:
                old.rename(public)
                raise
            shutil.rmtree(old)
        else:
            new.rename(public)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    if "sitemap.xml" not in files:  # planned exactly when content/site.json has a base_url
        print("sitemap.xml skipped: content/site.json has no base_url", file=sys.stderr)
    return [public / path.relative_to(new) for path in written]


def preview_site(root, today=None):
    """Compare a fresh render with the current ``public/`` by path, size and SHA-256; writes nothing."""
    public = Path(root) / "public"
    planned = _plan_site(root, today)
    if planned is None:
        raise ValueError("build --preview needs content/; the starter has nothing to compare")
    files, plan = planned

    def sized(data):
        return len(data), hashlib.sha256(data).hexdigest()
    new = {name: sized(text.encode("utf-8")) for name, text in files.items()}
    new.update({Path(relative).as_posix(): sized(Path(source).read_bytes()) for source, relative, _ in plan})
    old, unreadable = {}, []
    if public.is_dir():
        for folder, dirs, names in os.walk(public, followlinks=False):
            for name in names + [d for d in dirs if (Path(folder) / d).is_symlink()]:
                path = Path(folder) / name
                relative = path.relative_to(public).as_posix()
                if path.is_symlink() or not path.is_file():  # never followed or opened
                    unreadable.append(relative)
                else:
                    old[relative] = sized(path.read_bytes())
    both = sorted(new.keys() & old.keys())
    changed = [{"path": p, "bytes_before": old[p][0], "bytes_after": new[p][0], "delta": new[p][0] - old[p][0]}
               for p in both if old[p] != new[p]]
    return {"public_exists": public.exists(),
            "added": [{"path": p, "bytes": new[p][0]} for p in sorted(new.keys() - old.keys())],
            "removed": [{"path": p, "bytes": old[p][0]} for p in sorted(old.keys() - new.keys())],
            "changed": changed, "unchanged": len(both) - len(changed), "unreadable": sorted(unreadable)}
