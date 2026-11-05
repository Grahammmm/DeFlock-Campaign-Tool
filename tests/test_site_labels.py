"""DF2022-06: content/labels.json replaces the site's fixed UI strings by key; English stays the byte-identical default."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from campaign_tool import labels as labels_module
from campaign_tool.cli import read_config
from campaign_tool.agency_actions import ACTION_KINDS, add_action
from campaign_tool.content import ContentError, load_content
from campaign_tool.labels import DEFAULTS, JS_DEFAULTS, PLACEHOLDER, html_text, load_labels
from campaign_tool.site import FILTER_JS, build_site, preview
from campaign_tool.supporters import KINDS, add_supporter

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "fictional-campaign"
SOON, LATE = date(2026, 10, 1), date(2027, 1, 1)  # LATE: every meeting past, every contract ended


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def enrich(root, maplibre=False):
    """Reach the fixed strings the example alone does not: five findings (filter, related, corrections, updated,
    no agency), contact, hosted signup, verified agency with portal and contracts, a cancelled meeting."""
    content = root / "content"
    site = json.loads((content / "site.json").read_text(encoding="utf-8"))
    site.update(mission_md="", contact_email="zq-team@example.invalid",
                press={"contact_role": "Zq press lead"},  # DF2207-10: inherits the address; reaches every press label but the logo ones
                signup={"mode": "brevo_hosted", "form_html_allowlisted_url": "https://zq.sibforms.com/serve/zq",
                        "label": "Zq join", "note": "Zq note."})
    _write(content / "site.json", site)
    base = json.loads((content / "findings" / "cedar-policy-posted.json").read_text(encoding="utf-8"))
    for n, (cls, agency, year) in enumerate((("information_gap", "ca-cedar-police", "2025"),
                                             ("redaction", None, "2024"),
                                             ("agency_assertion", "ca-cedar-city-council", "2025"),
                                             ("documented_fact", "ca-cedar-police", "2024")), start=2):
        _write(content / "findings" / f"zq-{n}.json", dict(
            base, id=f"fnd_000000000000000{n}", slug=f"zq-{n}", title=f"Zq title {n}", summary=f"Zq summary {n}",
            classification=cls, confidence="likely", agency_id=agency, event_date=f"{year}-03-0{n}",
            updated_at="2026-03-01T00:00:00+00:00" if n == 2 else None, corrections=[], limitations=[],
            counterevidence=["Zq counter."] if n == 3 else [], body_md="",
            sources=base["sources"] + ([dict(base["sources"][0], sha256="beef" * 16)] if n in (2, 5) and maplibre else [])))
    agencies = json.loads((content / "agencies.json").read_text(encoding="utf-8"))
    contract = {"vendor": "Zq vendor", "start": "2024-01-01", "end": "2026-12-01", "renewal_note": "Zq renewal.",
                "source_url": "https://example.invalid/zq-contract", "checked_on": "2026-09-01"}
    police = agencies[1]
    police.update(verified=True, flock_transparency_slug="zq-pd", portal={"vendor": "govqa", "url": "https://example.invalid/zq"},
                  verification={"source_url": "https://example.invalid/zq-check", "checked_on": "2026-09-01"})
    police["profile"]["contract"] = contract
    agencies[0]["profile"]["contract"] = dict(contract, end="2026-01-01")
    _write(content / "agencies.json", agencies)
    _write(content / "topics" / "zq-topic.json", {"title": "Zq topic", "intro_md": "Zq intro.",
                                                  "finding_ids": ["fnd_0000000000000002", "fnd_0000000000000003"]})
    _write(content / "glossary.json", {  # DF1952-04: reaches every glossary label (a rule-linked term, an alias, a finding that uses it)
        "schema_version": 1, "notice_md": "Zq glossary notice.",
        "terms": [{"term": "Zq title", "aliases": ["Zq alias"], "definition_md": "Zq definition.",
                   "rule_id": "ca-civ-1798.90.5-definitions",
                   "source_url": "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=CIV&sectionNum=1798.90.5."}]})
    _write(content / "faq.json", {  # DF0022-01: reaches every faq label (a cited finding, a law reference, a glossary term in the question)
        "schema_version": 1, "entries": [{"question": "Zq title question?", "answer_md": "Zq answer.",
                                          "finding_ids": ["fnd_0000000000000002"], "rule_id": "ca-civ-1798.90.5-definitions"}]})
    _write(content / "meetings" / "zq-cancelled.json", {
        "id": "zq-cancelled", "body": "Zq board", "starts_at": "2026-09-01T18:00:00-07:00", "status": "cancelled"})
    if maplibre:
        (content / "vendor").mkdir()
        (content / "vendor" / "maplibre-gl.js").write_text("// zq stub\n", encoding="utf-8")
        _write(content / "map" / "snapshot.json", {"schema_version": 1, "osm_base": "2026-09-30T00:00:00Z", "source": "overpass"})


def bare(root):
    """Only site.json and an empty agency list: the empty states."""
    content = root / "content"
    for name in ("findings", "meetings", "map"):
        shutil.rmtree(content / name)
    (content / "sources.json").unlink()
    _write(content / "agencies.json", [])


SCENARIOS = (("rich", lambda r: enrich(r), SOON), ("late", lambda r: enrich(r, maplibre=True), LATE),
             ("bare", bare, SOON))


def tree(public):
    return {p.relative_to(public).as_posix(): p.read_bytes() for p in sorted(public.rglob("*")) if p.is_file()}


class LabelsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def root(self, name="rich", labels=None):
        root = self.tmp / f"{name}-{len(list(self.tmp.iterdir()))}"
        shutil.copytree(EXAMPLE, root)
        dict((s[0], s[1]) for s in SCENARIOS)[name](root)
        if labels is not None:
            _write(root / "content" / "labels.json", {"schema_version": 1, "labels": labels})
        return root

    def build(self, name="rich", labels=None):
        root = self.root(name, labels)
        build_site(root, today=dict((s[0], s[2]) for s in SCENARIOS)[name])
        return tree(root / "public")

    def refused(self, labels, *needles, document=None):
        root = self.root()
        _write(root / "content" / "labels.json", document if document is not None else {"schema_version": 1, "labels": labels})
        with self.assertRaises(ContentError) as caught:
            load_content(root)
        for needle in needles:
            self.assertIn(needle, str(caught.exception))
        return str(caught.exception)

    def test_no_labels_file_is_byte_identical_to_every_default(self):
        for name, _, _ in SCENARIOS:
            with self.subTest(name):
                plain, explicit = self.build(name), self.build(name, labels=dict(DEFAULTS))
                self.assertEqual(sorted(plain), sorted(explicit))
                self.assertEqual([p for p in plain if plain[p] != explicit[p]], [])

    def test_two_labels_change_only_their_strings_on_every_page(self):
        plain = self.build()
        changed = self.build(labels={"nav.home": "Inicio", "heading.findings": "Todo lo que publicamos cita un registro."})
        pages = [p for p in plain if p.endswith(".html")]
        self.assertGreater(len(pages), 8)
        for path, old in plain.items():
            new = old.decode()
            if path.endswith(".html"):
                self.assertIn('">Inicio</a>', changed[path].decode(), path)
                new = new.replace('">Home</a>', '">Inicio</a>').replace(
                    "<h2>Everything we publish cites a record.</h2>", "<h2>Todo lo que publicamos cita un registro.</h2>")
            self.assertEqual(changed[path].decode(), new, path)
        self.assertIn("<h2>Todo lo que publicamos cita un registro.</h2>", changed["findings/index.html"].decode())

    def test_unknown_key_refused_and_public_untouched(self):
        root = self.root()
        build_site(root, today=SOON)
        before = tree(root / "public")
        _write(root / "content" / "labels.json", {"schema_version": 1, "labels": {"nav.hom": "Inicio"}})
        with self.assertRaises(ContentError) as caught:
            build_site(root, today=SOON)
        self.assertIn("labels.json.labels.nav.hom: unknown label", str(caught.exception))
        self.assertEqual(tree(root / "public"), before)
        self.assertEqual(sorted(p.name for p in root.iterdir() if p.name.startswith(".public")), [])

    def test_placeholders_outside_the_key_default_refused(self):
        self.refused({"nav.home": "Inicio {bogus}"}, "labels.json.labels.nav.home", "{bogus}")
        self.refused({"finding.filter_count": "{count} de {bogus}"}, "labels.json.labels.finding.filter_count", "{bogus}")
        self.refused({"nav.home": "Inicio {count}"}, "labels.json.labels.nav.home", "{count}", "allowed: none")
        self.refused({"heading.agencies": "En {county} {state}"}, "heading.agencies", "{state}", "allowed: {county}")
        self.refused({"finding.filter_count": "{count!r}"}, "finding.filter_count", "{count!r}")
        self.refused({"finding.filter_count": "{0} de {count}"}, "finding.filter_count", "{0}")
        for text, brace in (("Inicio {", "{"), ("Inicio }", "}"), ("{{count}}", "{"), ("{count", "{")):
            with self.subTest(text):
                self.refused({"finding.filter_count": text}, "labels.json.labels.finding.filter_count", f"unpaired brace {brace}")
        merged = load_labels({"schema_version": 1, "labels": {"finding.filter_count": "Mostrando {count} de {count}",
                                                              "agency.portal": "Portal"}})  # a placeholder may be left out
        self.assertEqual((merged["finding.filter_count"], merged["agency.portal"]), ("Mostrando {count} de {count}", "Portal"))

    def test_bad_text_refused(self):
        for text in ("a <b>", "a > b", "a\x07b", "line\nbreak", "tab\there", "\x85", "", "   ", "x" * 301, 7, None, ["Inicio"]):
            with self.subTest(text=text):
                self.refused({"nav.home": text}, "labels.json.labels.nav.home")
        self.assertEqual(load_labels({"schema_version": 1, "labels": {"nav.home": "x" * 300}})["nav.home"], "x" * 300)

    def test_ampersand_and_quote_escaped_once(self):
        files = self.build(labels={"heading.findings": 'Fish & "Chips"', "nav.label": 'A & "B"', "title.feed": "R&D"})
        page = files["findings/index.html"].decode()
        self.assertIn("<h2>Fish &amp; &quot;Chips&quot;</h2>", page)
        self.assertIn('aria-label="A &amp; &quot;B&quot;"', page)
        self.assertNotIn("&amp;amp;", page)

        class Read(HTMLParser):
            h2, nav, alt, inside = [], [], [], False

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                self.inside = tag == "h2"
                self.nav += [attrs["aria-label"]] if tag == "nav" else []
                self.alt += [attrs["title"]] if tag == "link" and attrs.get("rel") == "alternate" else []

            def handle_data(self, data):
                self.h2 += [data] if self.inside else []
                self.inside = False
        parsed = Read()
        parsed.feed(page)
        self.assertIn('Fish & "Chips"', parsed.h2)
        self.assertEqual((parsed.nav, parsed.alt), (['A & "B"'], ["R&D"]))

    def test_bad_document_refused(self):
        self.refused(None, "labels.json.schema_version: must be 1", document={"schema_version": 2, "labels": {}})
        self.refused(None, "labels.json.schema_version: must be 1", document={"schema_version": True, "labels": {}})
        self.refused(None, "labels.json.schema_version: must be 1", document={"labels": {}})
        self.refused(None, "labels.json: expected an object", document=[["nav.home", "Inicio"]])
        self.refused(None, "labels.json.labels: expected an object", document={"schema_version": 1, "labels": [["nav.home", "x"]]})
        self.refused(None, "labels.json: unknown field(s) ['language']", document={"schema_version": 1, "labels": {}, "language": "es"})
        root = self.root()
        path = root / "content" / "labels.json"
        path.write_text(json.dumps({"schema_version": 1, "labels": {}, "_": "x" * 65536}), encoding="utf-8")
        with self.assertRaisesRegex(ContentError, "labels.json: file too large"):
            load_content(root)
        real = self.tmp / "real-labels.json"
        _write(real, {"schema_version": 1, "labels": {"nav.home": "Inicio"}})
        path.unlink()
        os.symlink(real, path)
        with self.assertRaisesRegex(ContentError, "labels.json: symlinks are not read"):
            load_content(root)
        path.unlink()
        os.symlink(self.tmp / "missing.json", path)  # a dangling link is refused too, never skipped
        with self.assertRaisesRegex(ContentError, "labels.json: symlinks are not read"):
            load_content(root)

    def test_every_fixed_string_replaced(self):
        markers = {key: f"X{key}X" for key in DEFAULTS}
        phrases = [chunk.strip() for text in DEFAULTS.values() for chunk in PLACEHOLDER.split(text)
                   if len(chunk.split()) >= 3]
        self.assertGreater(len(phrases), 60)
        seen = set()
        for name, _, _ in SCENARIOS:
            files = self.build(name, labels=markers)
            for path, data in files.items():
                if path.endswith(".html"):
                    page = data.decode()
                    seen |= {key for key in DEFAULTS if f"X{key}X" in page}
                    left = [p for p in phrases if p in page or html_text(p) in page]
                    self.assertEqual(left, [], f"{name}: {path}")
        # Codes the fixture has no finding or agency for; they render through the same t("<family>." + code) call.
        # officials.*: the fixture has no officials.json (tests/test_site_officials.py covers those labels).
        # map.history_*: the fixture has no map/history.json (tests/test_site_cameras_page.py covers those labels).
        # press.logo, press.download_logo: the fixture has no brand logo (tests/test_site_press_page.py covers those labels).
        # map.check_*: the fixture has no map/point-checks.json (tests/test_map_point_checks.py covers those labels).
        # map.direction_line: the fixture has no facing tag (tests/test_camera_direction.py covers that label).
        # agency.transparency_*: the fixture has no transparency snapshot (tests/test_agency_transparency.py covers those).
        # agency.mapped_reported_*: the fixture has no transparency snapshot (tests/test_agency_mapped_reported.py covers those).
        # schedule.*: the fixture has no body-schedules.json (tests/test_body_schedules.py covers those labels).
        # meeting.comments_*: the fixture records no public-comment counts (tests/test_meeting_comment_counts.py covers them).
        # meeting.votes*, vote.*: the fixture records no votes (tests/test_meeting_votes.py covers those labels).
        # handout.*: only the private meeting-pack handout writes these (tests/test_meeting_pack.py covers them).
        # agency.votes_*: the fixture records no votes (tests/test_site_member_votes.py covers those labels).
        # agency.contract_cost, agency.cost_*: the fixture has no contract cost (tests/test_agency_contract_cost.py covers those).
        # agency.funding_*: the fixture records no funding (tests/test_agency_funding.py covers those).
        # agency.action*: the fixture records no agency actions (test_agency_actions_labels_replace_every_fixed_string below).
        absent = ["agency.action_kind_cameras_added", "agency.action_kind_cameras_removed",
                  "agency.action_kind_contract_ended", "agency.action_kind_contract_paused",
                  "agency.action_kind_contract_renewed", "agency.action_kind_contract_signed", "agency.action_kind_other",
                  "agency.action_kind_policy_adopted", "agency.action_kind_policy_amended", "agency.action_kind_policy_posted",
                  "agency.action_kind_retention_changed", "agency.action_kind_sharing_ended",
                  "agency.action_kind_sharing_started", "agency.action_line", "agency.actions_heading",
                  "agency.contract_cost", "agency.cost_annual", "agency.cost_one_time", "agency.cost_source",
                  "agency.cost_total", "agency.funding_fiscal_year", "agency.funding_kind_asset_forfeiture",
                  "agency.funding_kind_donation", "agency.funding_kind_general_fund", "agency.funding_kind_grant",
                  "agency.funding_kind_other", "agency.funding_provenance",
                  "agency.mapped_reported_caveat", "agency.mapped_reported_counts", "agency.mapped_reported_unknown",
                  "agency.transparency_cameras", "agency.transparency_checked", "agency.transparency_down",
                  "agency.transparency_heading", "agency.transparency_hotlist_alerts", "agency.transparency_retention_days",
                  "agency.transparency_same", "agency.transparency_searches_last_30_days",
                  "agency.transparency_shared_with_agencies", "agency.transparency_up",
                  "agency.votes_heading", "agency.votes_more", "agency.votes_none", "agency.votes_source",
                  "finding.agency_response", "finding.quote_page", "finding.quote_transcribed", "finding.reply_declined", "finding.reply_no_response", "finding.reply_received",
                  "footer.license", "footer.sharing",
                  "handout.agenda", "handout.corrected", "handout.more_on_site", "handout.not_recorded",
                  "handout.nothing_published", "handout.record_explainer", "handout.source_line", "handout.title",
                  "handout.what_we_found",
                  "kind.campus_police", "kind.chp", "kind.county_board", "kind.district_attorney", "kind.other",
                  "license.about", "license.about_camera", "license.csv_note",
                  "map.check_duplicate", "map.check_earlier", "map.check_heading",
                  "map.check_in_agency_record", "map.check_not_found_on_site", "map.check_seen_in_person",
                  "map.direction_line",
                  "map.history_added", "map.history_date", "map.history_note", "map.history_removed",
                  "map.history_title", "map.history_total",
                  "meeting.comments_both", "meeting.comments_spoke", "meeting.comments_written",
                  "meeting.continued_from", "meeting.continued_to", "meeting.cosigned_many", "meeting.cosigned_one",
                  "meeting.minutes_confirmed", "meeting.minutes_pending", "meeting.recording", "meeting.recording_from",
                  "meeting.staff_direction",
                  "meeting.votes", "meeting.votes_member", "meeting.votes_source", "meeting.votes_vote",
                  "officials.heading", "officials.listed", "officials.source", "press.download_logo", "press.logo",
                  "privacy.signup_policy",
                  "requests.caption", "requests.col_agency", "requests.col_asked_on", "requests.col_state",
                  "requests.col_topic", "requests.col_updated_on", "requests.not_recorded",
                  "requests.state_acknowledged", "requests.state_answered", "requests.state_awaiting_response",
                  "requests.state_declined_in_writing", "requests.state_partly_answered", "requests.state_withdrawn",

                  "schedule.join_many", "schedule.join_two", "schedule.ordinal_1", "schedule.ordinal_2",
                  "schedule.ordinal_3", "schedule.ordinal_4", "schedule.ordinal_last", "schedule.rule",
                  "schedule.stale", "schedule.usually", "schedule.weekday_friday", "schedule.weekday_monday",
                  "schedule.weekday_saturday", "schedule.weekday_sunday", "schedule.weekday_thursday",
                  "schedule.weekday_tuesday", "schedule.weekday_wednesday",
                  "sharing.aliases", "sharing.checked", "sharing.count", "sharing.federal", "sharing.in_state",
                  "sharing.kind", "sharing.kind_federal", "sharing.kind_local", "sharing.kind_private", "sharing.kind_state",
                  "sharing.kind_unknown", "sharing.law_status", "sharing.lead", "sharing.out_of_state", "sharing.receiver",
                  "sharing.source_list", "sharing.sources", "sharing.state", "sharing.title",
                  "supporters.description", "supporters.joined", "supporters.kind.business",
                  "supporters.kind.civil_liberties", "supporters.kind.community", "supporters.kind.faith",
                  "supporters.kind.labor", "supporters.kind.neighborhood", "supporters.kind.other", "supporters.title",
                  "vote.absent", "vote.abstain", "vote.no", "vote.recused", "vote.yes"]
        self.assertEqual(sorted(set(DEFAULTS) - set(JS_DEFAULTS) - seen), absent)  # js.*: the script tests below

    def test_supporters_labels_replace_every_fixed_string_when_organizations_are_listed(self):
        root = self.root("rich", labels={key: f"X{key}X" for key in DEFAULTS})
        for kind in KINDS:
            add_supporter(root, name=f"Zq {kind} Org", kind=kind, organization=True, confirmed_on="2026-09-01", how="form",
                          today=SOON)
        build_site(root, today=SOON)
        page = (root / "public" / "supporters.html").read_text(encoding="utf-8")
        keys = [key for key in DEFAULTS if key.startswith("supporters.")]
        self.assertEqual(len(keys), 10)
        for key in keys:
            self.assertIn(f"X{key}X", page, key)
        for text in (DEFAULTS[key] for key in keys):
            for chunk in PLACEHOLDER.split(text):
                if len(chunk.split()) >= 2:
                    self.assertNotIn(chunk.strip(), page)
        self.assertIn('<a href="/supporters.html">Xsupporters.titleX</a>', (root / "public" / "index.html").read_text(encoding="utf-8"))

    def test_agency_actions_labels_replace_every_fixed_string_when_actions_are_recorded(self):
        markers = {key: f"X{key}X" for key in DEFAULTS}
        # Keep interpolation so the nested kind labels remain observable.
        markers["agency.action_line"] += " {on} {kind} {statement} {source} {checked}"
        root = self.root("rich", labels=markers)
        for kind in ACTION_KINDS:
            add_action(root, "ca-cedar-police", on="2026-08-01", kind=kind, as_stated=f"Zq {kind} statement",
                       source_url=f"https://example.invalid/zq-{kind}", checked_on="2026-09-01", today=SOON)
        build_site(root, today=SOON)
        page = (root / "public" / "agencies" / "cedar.html").read_text(encoding="utf-8")
        keys = [key for key in DEFAULTS if key.startswith("agency.action")]
        self.assertEqual(len(keys), 15)
        for key in keys:
            self.assertIn(f"X{key}X", page, key)
        for text in (DEFAULTS[key] for key in keys):
            for chunk in PLACEHOLDER.split(text):
                if chunk.strip() and len(chunk.split()) >= 2:
                    self.assertNotIn(chunk.strip(), page)
        self.assertNotIn("Changes on record", page)

    def test_non_page_outputs_and_starter_ignore_labels(self):
        markers = {key: f"X{key}X" for key in DEFAULTS if key not in JS_DEFAULTS}  # no js.* key: scripts unchanged
        for name, _, _ in SCENARIOS:
            plain, marked = self.build(name), self.build(name, labels=markers)
            other = [p for p in plain if not p.endswith(".html")]
            self.assertTrue({"feed.xml", "site-data.js", "agency-cards.js", "app.js", "_headers"} <= set(other))
            self.assertEqual([p for p in other if plain[p] != marked[p]], [], name)
        cfg = read_config(self.root())
        before = hashlib.sha256(preview(cfg)["html"].encode()).hexdigest()
        with mock.patch.dict(labels_module.DEFAULTS, markers):
            self.assertEqual(hashlib.sha256(preview(cfg)["html"].encode()).hexdigest(), before)


SPANISH = {"js.filter_count": "Mostrando {shown} de {total}", "js.mapped_point": "{count} punto mapeado",
           "js.mapped_points": "{count} puntos mapeados", "js.city_no_findings": "Sin hallazgos publicados."}
SCRIPTS = ("site-data.js", "agency-cards.js", "app.js", "findings/filter.js", "sources/filter.js")
# DF1103-12: a nonempty source library always transports the count label, so site-data.js carries exactly this line
# when no js.* label differs; sources/filter.js reads LABELS by design and is not one of the unchanged-English scripts.
SOURCES_TRANSPORT = b'var LABELS={"js.filter_count":"Showing {shown} of {total}"};\n'


class ScriptLabelsTest(unittest.TestCase):
    """DF0137-14: js.* labels reach the generated scripts through LABELS in site-data.js."""
    setUp, root, build, refused = LabelsTest.setUp, LabelsTest.root, LabelsTest.build, LabelsTest.refused

    def labels_line(self, files):
        lines = [line for line in files["site-data.js"].decode().splitlines() if line.startswith("var LABELS=")]
        self.assertEqual(len(lines), 1)
        return json.loads(lines[0][len("var LABELS="):-1])

    def test_without_js_labels_scripts_are_todays(self):
        for name, _, _ in SCENARIOS:
            with self.subTest(name):
                files = self.build(name)
                self.assertEqual(files["site-data.js"].count(SOURCES_TRANSPORT), 1 if "sources/filter.js" in files else 0)
                legacy = [p for p in SCRIPTS if p in files and p != "sources/filter.js"]
                self.assertNotIn(b"LABELS", b"".join(files[p] for p in legacy).replace(SOURCES_TRANSPORT, b""))
                if "findings/filter.js" in files:
                    self.assertEqual(files["findings/filter.js"].decode(), FILTER_JS)
                    self.assertNotIn("/site-data.js", files["findings/index.html"].decode())
                self.assertNotIn(b"fill(", files["app.js"])

    def test_some_js_labels_set_rest_keep_english_defaults(self):
        plain, files = self.build("late"), self.build("late", labels=SPANISH)
        self.assertEqual(self.labels_line(files), dict(JS_DEFAULTS, **SPANISH))
        filter_js = files["findings/filter.js"].decode()
        self.assertNotIn("Showing", filter_js)
        self.assertIn('LABELS["js.filter_count"]', filter_js)
        index = files["findings/index.html"].decode()
        self.assertIn('<script defer src="/site-data.js"></script>\n  <script defer src="/findings/filter.js"></script>', index)
        app = files["app.js"].decode()
        for english in ('" mapped point', "could not load", "ALPR point", "not specified", "View source record"):
            self.assertNotIn(english, app)
        self.assertNotIn("innerHTML", app)
        self.assertEqual(files["_headers"], plain["_headers"])  # CSP unchanged: no inline script, same origins
        changed = sorted(p for p in plain if plain[p] != files[p])
        self.assertEqual(changed, ["agency-cards.js", "app.js", "findings/filter.js", "findings/index.html", "search.html", "site-data.js", "sources.html"])
        cards = files["agency-cards.js"].decode()
        for english in ('"no published findings yet"', '" published finding"'):
            self.assertNotIn(english, cards)
        for key in ("zero", "one", "many"):
            self.assertIn(f'"js.agency_findings_{key}"', cards)
        self.assertIn(b'"Sin hallazgos publicados."', files["site-data.js"])  # Python-side js.* text goes into CITIES

    def test_quotes_backslash_and_slash_emitted_escaped_and_scripts_parse(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node not installed")
        self.refused({"js.filter_count": "</script>"}, "labels.json.labels.js.filter_count", "must not contain <")
        files = self.build("late", labels={"js.filter_count": 'Mostrando "{shown}" \\ de {total} /script/',
                                           "js.popup_title": 'R&D "x" \\ /'})
        root = self.tmp / "parse"
        for name in SCRIPTS:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(files[name])
            result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, f"{name}: {result.stderr}")
        self.assertIn('"js.filter_count":"Mostrando \\"{shown}\\" \\\\ de {total} /script/"', files["site-data.js"].decode())
        self.assertEqual(self.labels_line(files)["js.popup_title"], 'R&D "x" \\ /')

    def test_unknown_js_key_or_foreign_placeholder_refused(self):
        self.refused({"js.filter_cuont": "Mostrando"}, "labels.json.labels.js.filter_cuont: unknown label")
        self.refused({"js.filter_count": "{count} de {total}"}, "labels.json.labels.js.filter_count", "{count}",
                     "allowed: {shown}, {total}")
        self.refused({"js.mapped_point": "{shown} punto"}, "labels.json.labels.js.mapped_point", "allowed: {count}")
        self.refused({"finding.filter_count": "{shown} de {total}"}, "labels.json.labels.finding.filter_count", "{shown}")
        self.refused({"js.popup_title": "{count}"}, "labels.json.labels.js.popup_title", "allowed: none")
        self.refused({"js.map_failed": "Falla {evidence"}, "labels.json.labels.js.map_failed", "unpaired brace {")


if __name__ == "__main__":
    unittest.main()
