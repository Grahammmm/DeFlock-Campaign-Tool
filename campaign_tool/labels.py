"""Fixed UI strings of the content-model site, replaceable by an organizer-reviewed ``content/labels.json``.

``DEFAULTS`` is the exact English the pages always used, so a site without labels.json is byte-identical. The tool
translates nothing: an organizer supplies reviewed wording key by key; see docs/SITE-CONTENT.md ("Labels")."""
import re
from html import escape

from .content import ContentError

MAX_LABELS_BYTES, MAX_LABEL_CHARS = 64 * 1024, 300
CLASSIFICATION_LABELS = {"documented_fact": "Documented fact", "apparent_conflict": "Apparent conflict",
                         "confirmed_conflict": "Confirmed conflict", "information_gap": "Information gap",
                         "redaction": "Redaction", "agency_assertion": "Agency assertion", "no_conflict": "No conflict found"}
CONFIDENCE_LABELS = {"verified": "Verified against the record", "likely": "Likely; awaiting further records"}
KIND_LABELS = {"sheriff": "County sheriff", "police": "City police", "chp": "State highway patrol",
               "campus_police": "Campus police", "district_attorney":
               "District attorney", "county_board": "County board", "city_council": "City council", "other": "Other public body"}
DEFAULTS = {
    "nav.label": "Site", "nav.home": "Home", "nav.agencies": "Agencies", "nav.findings": "Findings",
    "nav.sources": "Sources", "nav.meetings": "Meetings", "nav.about": "About", "nav.corrections": "Corrections",
    "nav.home_page": "Home page", "nav.topics": "Topics", "header.skip": "Skip to content", "header.attend": "Attend a meeting",
    "footer.credit": "{name} is an independent local initiative. Built with DeFlock Campaign Tool.",
    "footer.contact": "Contact:", "footer.feed": "Findings feed", "footer.sitemap": "Sitemap", "footer.search": "Search",
    "footer.license": "Data and text:", "license.csv_note": "(license: {name})",
    "license.about": "Text and data on this site are published under {license}.",
    "license.about_camera": "Camera points: \u00a9 {osm}, {odbl}.",
    "search.title": "Search", "search.description": "Search the published findings and the source library",
    "search.label": "Search findings and sources",
    "search.intro": ("Looks through the titles, summaries and agency names of published findings and the titles of the "
                     "sources they cite. Everything is matched in your browser; what you type is not sent anywhere."),
    "search.fallback": "Search needs JavaScript. Without it, browse the {findings} or the {sources}.",
    "footer.privacy": "Privacy", "footer.glossary": "Glossary", "privacy.title": "Privacy",
    "privacy.signup_policy": "Read the provider's privacy policy",
    "privacy.lead": "What this site loads, whether it stores anything on your device, and where a signup goes.",
    "privacy.scripts": "Scripts, stylesheets and fonts are served from this site only. This site loads no third-party scripts.",
    "privacy.cookies": "This site sets no cookies and runs no analytics or tracking scripts.",
    "privacy.map": "The interactive map loads its style and map tiles from these hosts, which your browser contacts when it opens: {hosts}.",
    "privacy.signup": "Your email address goes to {host} when you submit the signup form; this site never receives it.",
    "privacy.contact": "Privacy contact:", "privacy.no_contact": "This campaign has not published a privacy contact.",
    "privacy.notice": "This page is generated from the site's build settings. It is not legal advice and not a reviewed privacy policy.",
    "title.feed": "Findings", "title.home": "Home", "title.agencies": "Agencies",
    "title.agencies_description": "Public bodies covered by the campaign", "title.findings": "Findings",
    "title.findings_description": "Published, source-linked findings", "title.corrections": "Corrections",
    "title.corrections_description": "Every correction issued on a published finding",
    "title.not_found": "Page not found", "title.not_found_description": "This page does not exist on this site.",
    "title.sources": "Sources", "title.sources_description": "Hashed source records behind published findings",
    "title.meetings": "Meetings", "title.meetings_description": "Public meetings and comment kits",
    "title.meeting_description": "Public meeting and comment kit", "title.about": "About",
    "title.about_description": "About the campaign", "title.topics": "Topics",
    "title.topics_description": "Published findings grouped into topics, each with its classification, date and corrections",
    "eyebrow.agencies": "Agencies", "eyebrow.contracts": "Contracts", "eyebrow.meetings": "Meetings",
    "eyebrow.mission": "Evidence before conclusions", "eyebrow.latest_findings": "Latest findings",
    "eyebrow.findings": "Findings", "eyebrow.sources": "Source library", "eyebrow.meeting": "Meeting",
    "eyebrow.about": "About", "eyebrow.agency_profile": "Agency profile", "eyebrow.open_questions": "Open questions",
    "heading.contracts": "Contracts ending soon", "heading.agencies_home": "What the records show so far.",
    "heading.meetings_home": "Show up informed.", "heading.findings_home": "Published with sources.",
    "heading.participate": "Participate with evidence.", "heading.agencies": "Public bodies in {county}.",
    "heading.findings": "Everything we publish cites a record.", "heading.corrections": "Corrections",
    "heading.topics": "Findings grouped by topic",
    "heading.not_found": "Page not found", "heading.sources": "Every document we cite, and where it came from.",
    "heading.meetings": "Official channels for informed questions.", "heading.past_meetings": "Past meetings",
    "intro.step1_title": "Request", "intro.step2_title": "Understand", "intro.step3_title": "Participate",
    "intro.step1": "Ask for policies, agreements, sharing settings and audit records.",
    "intro.step2": "Preserve originals, compare applicable rules and independently challenge every material finding.",
    "intro.step3": "Use reviewed evidence and official meeting channels to ask informed questions.",
    "intro.agencies_home": "Choose an area to see its status, published facts and the source notes behind them.",
    "intro.full_agency_list": "Full agency list", "intro.all_meetings": "All meetings and comment kits",
    "intro.all_findings": "All findings", "intro.atom_feed": "Atom feed", "intro.open_data": "Open data (JSON)",
    "intro.open_data_csv": "Spreadsheet (CSV)",
    "intro.agencies": "Listing an agency here says only that we asked or intend to ask it for records. No agency is "
                      "asserted to operate license plate readers unless a published finding says so with sources.",
    "intro.findings": "Each finding names its classification, confidence, event date and the hashed source records behind it.",
    "intro.corrections": "Every correction issued on a published finding, newest first.",
    "intro.not_found": "This page does not exist on this site. Findings are never silently removed: corrections and "
                       "withdrawals are listed on",
    "intro.sources": "Originals stay in the private archive unless a public copy is linked. Each entry names the "
                     "published findings that cite it.",
    "intro.meetings": "Comment kits summarize reviewed findings; they are not legal advice and do not ask anyone to flood a meeting.",
    "intro.open_data_about": "Published findings are also available as open data:",
    "intro.meetings_open_data": "Meetings and outcomes as open data (JSON)",
    "intro.meetings_calendar": "Subscribe to the meetings calendar (.ics)",
    "intro.meetings_open_data_about": "Meetings with their recorded outcomes, votes and official sources:",
    "intro.agencies_open_data": "Agencies as open data (JSON)",
    "intro.agencies_open_data_about": "Agencies with their asked items, posted policy, contract end and portal numbers, as each source reported them:",
    "notice.no_findings": "No findings have been published yet.", "notice.no_upcoming": "No upcoming meetings are listed.",
    "notice.past_meetings_link": "Past meetings and their comment kits", "notice.no_meetings": "No meetings are listed yet.",
    "notice.no_agencies": "No agencies are configured yet.", "notice.no_filter_match": "No published finding matches these filters.",
    "notice.no_corrections": "No corrections have been issued.", "notice.no_sources": "No source records have been catalogued yet.",
    "notice.meeting_cancelled": "This meeting was cancelled. The comment kit is kept for the record.",
    "notice.no_kit": "No comment kit has been staged for this meeting.",
    "finding.corrected": "Corrected {date}", "finding.event_date": "Event date {date}", "finding.agency": "Agency:",
    "finding.confidence": "Confidence:", "finding.published": "Published {date}", "finding.updated": "Updated {date}",
    "finding.sources": "Sources", "finding.limitations": "Limitations", "finding.none_stated": "None stated.",
    "finding.counterevidence": "Counterevidence considered", "finding.filter_count": "Showing {count} of {count}",
    "finding.none_identified": "None identified at publication.", "finding.corrections": "Corrections",
    "finding.all_corrections": "All corrections", "finding.ask_correction": "Ask for a correction",
    "finding.cite": "Cite this finding", "finding.cite_copy": "Copy", "finding.cite_copied": "Copied",
    "finding.cite_select": "Select and copy",
    "finding.quote_transcribed": "Transcribed by the campaign; not checked against the record text.",
    "finding.quote_page": "Page {page}",
    "finding.agency_response": "Agency response", "finding.reply_received": "The agency replied on {on}. We asked on {asked_on}.",
    "finding.reply_declined": "The agency declined to comment on {on}. We asked on {asked_on}.",
    "finding.reply_no_response": "asked on {asked_on}; no response by {reply_by}",
    "finding.part_of": "Part of: {topic}", "finding.related": "Related findings", "finding.same_agency": "Same agency",
    "finding.terms": "Terms used on this page",
    "glossary.also": "Also called: {aliases}", "glossary.citation": "Law package citation: {citation}",
    "glossary.source": "Source", "glossary.status": "Status of this law package: {status}",
    "title.glossary": "Glossary", "title.glossary_description": "Definitions of terms used on this site",
    "heading.glossary": "Glossary",
    "faq.title": "Frequently asked questions", "footer.faq": "FAQ", "faq.findings": "Related findings",
    "faq.terms": "Terms used in this answer", "faq.rule": "Law reference",
    "faq.law_status": "Law package status: {status}",
    "finding.shares_one": "Shares {count} source record", "finding.shares_many": "Shares {count} source records",
    "finding.filter_label": "Filter findings", "finding.filter_agency": "Agency",
    "finding.filter_all_agency": "All agencies", "finding.filter_no_agency": "No agency named",
    "finding.filter_classification": "Classification", "finding.filter_all_classification": "All classifications",
    "finding.filter_year": "Event year", "finding.filter_all_year": "All years",
    "meeting.agenda": "Agenda", "meeting.page": "Meeting page", "meeting.agenda_item": "Agenda item:",
    "meeting.cancelled": "Cancelled:", "meeting.kit": "Public comment kit", "meeting.all": "All meetings",
    "meeting.feed": "Meetings feed", "title.meetings_feed": "Meetings Atom feed",
    "meeting.votes": "Recorded votes", "meeting.votes_member": "Member", "meeting.votes_vote": "Vote",
    "meeting.votes_source": "From the official record:", "meeting.recording": "Watch the discussion",
    "meeting.recording_from": "(from {start})", "vote.yes": "Yes", "vote.no": "No", "vote.abstain": "Abstained",
    "meeting.minutes_confirmed": "Confirmed in the minutes approved on {date}",
    "meeting.minutes_pending": "Recorded by the campaign; minutes not yet approved", "agency.past_meetings": "Past meetings",
    "meeting.continued_to": "Continued to {date}", "meeting.continued_from": "Continued from {date}",
    "meeting.cosigned_one": "Co-signed by {count} organization", "meeting.cosigned_many": "Co-signed by {count} organizations",
    "meeting.staff_direction": "Staff were directed to: {direction} (by {date}, as stated in {source})",
    "vote.absent": "Absent", "vote.recused": "Recused",
    "agency.votes_heading": "How members voted", "agency.votes_none": "No recorded vote",
    "agency.votes_source": "Official record", "agency.votes_more": "\u2026 and {count} earlier votes on the meeting pages",
    "sources.record_no": "Record no.", "sources.public_copy": "Public copy", "agency.official_source": "the official source",
    "sources.private": "Original held in the private archive; not downloadable here", "sources.pages": "{count} page(s)",
    "sources.cited_by": "Cited by:", "sources.not_cited": "Not cited by a published finding",
    "sources.record_description": "Source record {record}", "sources.record_page": "Record details",
    "agency.records": "Records request page", "agency.portal": "Portal ({name})",
    "agency.flock_portal": "Flock transparency portal", "agency.sources": "{count} source record(s)",
    "agency.feed": "Findings feed", "agency.findings": "{count} published finding(s):",
    "agency.no_findings": "No published findings.", "agency.contact_checked": "Contact checked {date} on",
    "agency.unverified": "Contact details unverified: confirm on the agency's official site.",
    "agency.contract": "Contract with {name}", "agency.contract_ended": "ended", "agency.checked": "checked {date} on",
    "agency.the_source": "the source", "agency.contract_ends": "contract with {name} ends {date}",
    "agency.contract_cost": "Contract cost as stated: {amount} {period} ({document}; {source}, checked {date}).",
    "agency.cost_annual": "per year", "agency.cost_total": "total", "agency.cost_one_time": "one time",
    "agency.cost_source": "source",
    "agency.funding_provenance": "How it is paid for, as stated in {document} ({source}, checked {date})",
    "agency.funding_fiscal_year": "Fiscal year {year}", "agency.funding_kind_grant": "Grant",
    "agency.funding_kind_general_fund": "General fund", "agency.funding_kind_asset_forfeiture": "Asset forfeiture",
    "agency.funding_kind_donation": "Donation", "agency.funding_kind_other": "Other",
    "agency.page": "Agency page", "agency.page_description": "Records and findings for {name}",
    "agency.published_findings": "Published findings", "agency.source_records": "Source records",
    "agency.received": "received {date}", "agency.in_library": "In the source library",
    "agency.upcoming": "Upcoming meetings", "agency.no_upcoming": "No upcoming meetings are listed for this body.",
    "agency.transparency_heading": "Reported on the Flock transparency portal",
    "agency.transparency_checked": "as reported by the vendor, checked {date}",
    "agency.transparency_cameras": "Cameras: {count}", "agency.transparency_retention_days": "Retention days: {count}",
    "agency.transparency_searches_last_30_days": "Searches in the last 30 days: {count}",
    "agency.transparency_hotlist_alerts": "Hotlist alerts: {count}",
    "agency.transparency_shared_with_agencies": "Shared with agencies: {count}",
    "agency.transparency_up": "up {count} since {date}", "agency.transparency_down": "down {count} since {date}",
    "agency.transparency_same": "no change since {date}",
    "agency.mapped_reported_counts": (
        "The agency's transparency portal reported {reported} cameras (checked {checked}). "
        "Community mappers have mapped {mapped} points inside the city limits (map data as of {map_date})."),
    "agency.mapped_reported_caveat": (
        "These are counted differently: the portal counts the agency's own cameras, "
        "the map counts points inside a boundary whoever owns them."),
    "agency.mapped_reported_unknown": "unknown",
    "agencies.glance_heading": "At a glance",
    "agencies.glance_note": "Numbers are as each source reported them on the date shown. This table does not rank agencies.",
    "agencies.glance_caption": "Each tracked agency side by side, in the order of the list below",
    "agencies.glance_agency": "Agency", "agencies.glance_cameras": "Cameras (portal)",
    "agencies.glance_searches_last_30_days": "Searches last 30 days (portal)",
    "agencies.glance_retention_days": "Retention days (portal)", "agencies.glance_policy": "ALPR policy",
    "agencies.glance_contract": "Contract ends", "agencies.glance_cost": "Contract cost as stated",
    "agencies.glance_asked": "Records items asked",
    "agencies.glance_not_checked": "not checked", "agencies.glance_posted": "posted", "agencies.glance_checked": "checked {date}",
    "officials.heading": "Who decides", "officials.source": "source",
    "officials.listed": ("Listed from the body's official site ({source}), checked {date}. "
                         "Contact officials only through the channels on that site."),
    "map.picker_label": "Choose an area", "map.region_label": "Community-mapped ALPR points",
    "map.unavailable": "Interactive map unavailable: the campaign has not self-hosted the MapLibre library yet.",
    "map.loading": "Loading the interactive map.", "map.still_choose": "You can still choose an area below or",
    "map.source_map": "open the DeFlock source map", "map.region": "Community-mapped points",
    "map.count": "{count} mapped points", "map.hint": "Points are community reports, not confirmed agency ownership.",
    "map.date": "Map data as of {date} (OpenStreetMap)", "map.date_unknown": "Map data date unknown (OpenStreetMap)",
    "map.history_title": "Mapped points over time", "map.history_date": "Date", "map.history_total": "Mapped points",
    "map.history_added": "Added", "map.history_removed": "Removed",
    "map.history_note": "One row per map refresh. A mapped point is not evidence of agency ownership.",
    "map.check_heading": "Organizer check", "map.check_seen_in_person": "Seen in person",
    "map.check_in_agency_record": "Listed in an agency record", "map.check_not_found_on_site": "Not found on site",
    "map.check_duplicate": "Duplicate of another point", "map.check_earlier": "and {count} earlier",
    "signup.email": "Email address", "signup.placeholder": "you@example.org",
    "signup.provider_note": "Submitting opens the provider's hosted confirmation page.",
    "signup.none": "No email addresses are collected by this site. Follow meetings and findings here or by feed.",
    "supporters.title": "Supporting organizations",
    "supporters.description": ("Organizations that have confirmed they support this campaign. A listing is not a legal "
                               "endorsement and does not let anyone speak for the organization."),
    "supporters.joined": "Joined {date}",
    "supporters.kind.civil_liberties": "Civil liberties", "supporters.kind.community": "Community",
    "supporters.kind.faith": "Faith", "supporters.kind.labor": "Labor", "supporters.kind.neighborhood": "Neighborhood",
    "supporters.kind.business": "Business", "supporters.kind.other": "Other",
    "footer.press": "Press", "press.title": "Press",
    "press.description": "Campaign background, press contact, recent findings with citation text, logo and open data for reporters.",
    "press.contact": "Press contact", "press.findings": "Recent findings", "press.logo": "Logo",
    "press.data": "Open data", "press.download_logo": "Download the logo", "press.license_unspecified": "License not specified",
}
# DF0137-14: fixed strings the generated scripts write into the page. Defaults are today's exact English; site.py
# switches the scripts to LABELS lookups (site-data.js) only when a js.* key differs from its default.
JS_DEFAULTS = {
    "js.filter_count": "Showing {shown} of {total}",
    "js.mapped_point": "{count} mapped point", "js.mapped_points": "{count} mapped points",
    "js.map_failed": "The interactive map could not load in this browser. You can still select a city below, {evidence}, or {source}.",
    "js.map_evidence": "review the evidence notes", "js.map_source": "open the DeFlock source map",
    "js.data_failed": "Camera data could not load. {source}", "js.data_source": "Open the DeFlock source map",
    "js.popup_title": "Community-mapped ALPR point", "js.popup_no_manufacturer": "Manufacturer not specified",
    "js.popup_no_operator": "Operator not specified", "js.popup_source": "View source record",
    "js.county_findings": "{count} published finding(s) with hashed sources.",
    "js.county_no_findings": "No findings have been published yet; records requests are in progress.",
    "js.city_points_one": "{count} community-mapped point inside city limits; ownership unconfirmed",
    "js.city_points_many": "{count} community-mapped points inside city limits; ownership unconfirmed",
    "js.city_no_findings": "No published findings for this agency yet.",
    "js.agency_findings_zero": "no published findings yet",
    "js.agency_findings_one": "{count} published finding",
    "js.agency_findings_many": "{count} published findings",
    "js.search_hint": "Type 2 to 100 characters to search.", "js.search_none": "No published finding or source matches.",
    "js.search_count_one": "{count} result", "js.search_count_many": "{count} results",
    "js.search_unavailable": "Search is unavailable.", "js.search_finding": "Finding", "js.search_source": "Source",
    "js.group_vendor": "Not applicable", "js.group_status": "Ownership unconfirmed",
    "js.group_summary": "{name}. Points outside city boundaries without an operator tag are research leads, not evidence of agency ownership.",
    "js.group_fact": "Community-mapped points; verify with records requests.",
    "js.group_open": "Which agency, if any, operates these points is unconfirmed.", "js.group_flag": "Unconfirmed",
}
# DF0137-17: the fixed text of the private meeting-pack handout (meeting_pack.py), today's exact English.
HANDOUT_DEFAULTS = {
    "handout.title": "Meeting handout", "handout.what_we_found": "What we found", "handout.agenda": "Agenda",
    "handout.nothing_published": "Records are requested; nothing is published yet.",
    "handout.source_line": "Source: {title}, received {date}, record {record}",
    "handout.not_recorded": "not recorded", "handout.corrected": "corrected {date}",
    "handout.more_on_site": "{count} more on the site",
    "handout.record_explainer": "What the record number means: the first 12 characters of a SHA-256 fingerprint of the "
                                "file exactly as received. It shows the file has not changed since; it does not by "
                                "itself prove who wrote it.",
}
DEFAULTS.update(JS_DEFAULTS)
DEFAULTS.update(HANDOUT_DEFAULTS)
for _family, _labels in (("classification", CLASSIFICATION_LABELS), ("confidence", CONFIDENCE_LABELS), ("kind", KIND_LABELS)):
    DEFAULTS.update({f"{_family}.{code}": text for code, text in _labels.items()})
PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
ALLOWED = {key: set(PLACEHOLDER.findall(text)) for key, text in DEFAULTS.items()}  # each key: its default's set


def html_text(text):
    """Escape label text once for a text node or a double-quoted attribute; a ``'`` stays literal as in DEFAULTS."""
    return escape(text, quote=False).replace('"', "&quot;")


def load_labels(document):
    """Validate a parsed ``labels.json`` and return DEFAULTS merged with its labels; ``ContentError`` names the key."""
    if not isinstance(document, dict):
        raise ContentError("labels.json: expected an object")
    unknown = sorted(set(document) - {"schema_version", "labels"})
    if unknown:
        raise ContentError(f"labels.json: unknown field(s) {unknown}")
    version = document.get("schema_version")
    if type(version) is not int or version != 1:
        raise ContentError("labels.json.schema_version: must be 1")
    if not isinstance(document.get("labels"), dict):
        raise ContentError("labels.json.labels: expected an object")
    merged = dict(DEFAULTS)
    for key, text in document["labels"].items():
        where = f"labels.json.labels.{key}"
        if key not in DEFAULTS:
            raise ContentError(f"{where}: unknown label")
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= MAX_LABEL_CHARS:
            raise ContentError(f"{where}: must be text of 1-{MAX_LABEL_CHARS} characters")
        if "<" in text or ">" in text or CONTROL.search(text):
            raise ContentError(f"{where}: must not contain <, > or control characters")
        for name in PLACEHOLDER.findall(text):
            if name not in ALLOWED[key]:
                allowed = ", ".join("{" + n + "}" for n in sorted(ALLOWED[key])) or "none"
                raise ContentError(f"{where}: placeholder {{{name}}} is not allowed here (allowed: {allowed})")
        rest = PLACEHOLDER.sub("", text)
        if "{" in rest or "}" in rest:
            raise ContentError(f"{where}: unpaired brace {'{' if '{' in rest else '}'}")
        merged[key] = text
    return merged
