"""DF1952-07: one reviewed finding about several agencies (optional ``also_agency_ids``).

Synthetic fixtures only (a temp copy of the fictional example); no network. The filter test runs the generated
findings/filter.js in a small Node DOM harness written to a temp folder.

Run from the repository root: python3 -B -m unittest tests.test_finding_also_agencies -v
"""
import copy
import hashlib
from html import unescape
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import finding_diff, findings, law, site as site_module  # noqa: E402
from campaign_tool.cli import read_config  # noqa: E402
from campaign_tool.content import ContentError, finding_agency_ids, load_content, load_finding  # noqa: E402
from campaign_tool.content_check import content_problems  # noqa: E402
from campaign_tool.site import FILTER_JS, Site, build_site, filter_js  # noqa: E402
from tests import test_findings_publish as publish_tests  # noqa: E402  (module import: not re-collected here)

EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SCHEMA = json.loads((REPO_ROOT / "schemas" / "findings-open-data.schema.json").read_text(encoding="utf-8"))
TODAY = date(2026, 6, 1)
POLICE, SHERIFF, COUNCIL = "ca-cedar-police", "ca-cedar-county-sheriff", "ca-cedar-city-council"
CITY = "ca-cedar-city"  # an id that is a prefix of COUNCIL: exact token membership must not match it
BASE_SLUG = "cedar-policy-posted"
KNOWN = {POLICE, SHERIFF, COUNCIL}
# sha256 over the artifacts a primary-only campaign publishes (filter.js, findings.json, feed.xml, per-agency feeds,
# findings index) for the fixture built by ``legacy_fixture``, recorded from main before DF1952-07.
LEGACY_DIGEST = "6ddbabd69b0aa4a0ec5bcccf522ee530fda34915104646c4875c6d2ba4c6959d"  # pragma: allowlist secret -- SHA-256 of synthetic public artifacts, not a credential

NODE_HARNESS = r"""
const fs=require('fs'),vm=require('vm'),path=require('path');
const [pub,relabel]=[process.argv[2],process.argv[3]==='relabel'];
const html=fs.readFileSync(path.join(pub,'findings','index.html'),'utf8');
const code=(relabel?fs.readFileSync(path.join(pub,'site-data.js'),'utf8')+'\n':'')+fs.readFileSync(path.join(pub,'findings','filter.js'),'utf8');
const scenarios=JSON.parse(fs.readFileSync(0,'utf8'));
function run(sc){
  const items=[...html.matchAll(/<li((?: data-[a-z-]+="[^"]*")*)><div.*?<h3><a href="\/findings\/([^"]+)\.html">/gs)].map(m=>{
    const attrs={};for(const a of m[1].matchAll(/ (data-[a-z-]+)="([^"]*)"/g))attrs[a[1]]=a[2];
    return {slug:m[2],attrs,hidden:false,getAttribute(k){return k in this.attrs?this.attrs[k]:null;}};});
  const nodes={'finding-filter':{hidden:true,handlers:{},addEventListener(e,f){this.handlers[e]=f;}},
    'filter-count':{textContent:''},'filter-empty':{hidden:true}};
  const options={};
  for(const m of html.matchAll(/<select id="(filter-[a-z]+)"[^>]*>(.*?)<\/select>/g)){
    options[m[1]]=[...m[2].matchAll(/<option value="([^"]*)">/g)].map(o=>o[1]);
    nodes[m[1]]={value:'all',options:options[m[1]].map(v=>({value:v}))};}
  const replaced=[];
  const context={document:{getElementById:id=>nodes[id]||null,querySelectorAll:s=>s==='.finding-list li'?items:[]},
    location:{hash:sc.hash||'',pathname:'/findings/',search:''},history:{replaceState:(s,t,u)=>replaced.push(u)},
    encodeURIComponent,decodeURIComponent};
  context.window=context;vm.createContext(context);vm.runInContext(code,context,{filename:'filter.js'});
  for(const [k,v] of Object.entries(sc.set||{}))nodes['filter-'+k].value=v;
  if(sc.set)nodes['finding-filter'].handlers.change();
  return {shown:items.filter(i=>!i.hidden).map(i=>i.slug),count:nodes['filter-count'].textContent,
    empty:nodes['filter-empty'].hidden,selected:Object.fromEntries(Object.keys(options).map(k=>[k,nodes[k].value])),
    replaced,options,formHidden:nodes['finding-filter'].hidden};
}
process.stdout.write(JSON.stringify(scenarios.map(run)));
"""


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Fixture(unittest.TestCase):
    """A temp copy of the fictional example plus helpers to add synthetic findings."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-also-agencies-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content, self.public = self.root / "content", self.root / "public"
        self.example = json.loads((self.content / "findings" / f"{BASE_SLUG}.json").read_text(encoding="utf-8"))
        self.count = 1

    def add(self, slug, agency, also=None, published="2026-02-10T00:00:00+00:00", sha=True, **changes):
        self.count += 1
        document = dict(copy.deepcopy(self.example), slug=slug, id=f"fnd_{self.count:016d}", title=f"Synthetic {slug}",
                        corrections=[], updated_at=None, agency_id=agency, published_at=published)
        if sha:  # a record of its own, so no two findings share a source unless a test says so
            document["sources"] = [{"sha256": hashlib.sha256(slug.encode()).hexdigest(), "locator": "page:1"}]
        if also is not None:
            document["also_agency_ids"] = also
        document.update(changes)
        (self.content / "findings" / f"{slug}.json").write_text(json.dumps(document), encoding="utf-8")
        return document

    def build(self, **kwargs):
        return build_site(self.root, today=TODAY, **kwargs)

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def tree(self):
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in sorted(self.public.rglob("*")) if p.is_file()}

    def model_site(self):
        return Site(self.root, read_config(self.root), load_content(self.root, TODAY), today=TODAY)

    def links(self, html):
        return re.findall(r'href="(/findings/[^"/]+)\.html"', html)

    def cities(self):
        text = self.read("site-data.js")
        return json.loads(text.split("var CITIES=", 1)[1].split(";\nvar COUNTY_BOUNDS", 1)[0])


class ExtraIdsTests(Fixture):
    def doc(self, **changes):
        base = {key: value for key, value in self.example.items() if key != "also_agency_ids"}
        base.update(changes)
        return base

    def test_extra_ids_shape_bounds_known_and_refusals(self):
        self.assertNotIn("also_agency_ids", load_finding(self.doc(), "f"))  # absent stays absent
        loaded = load_finding(self.doc(also_agency_ids=[COUNCIL, SHERIFF]), "f", known_agency_ids=KNOWN)
        self.assertEqual(loaded["also_agency_ids"], [COUNCIL, SHERIFF])  # entered order kept
        self.assertEqual(finding_agency_ids(loaded), [POLICE, COUNCIL, SHERIFF])
        ten = [f"ca-extra-{n}" for n in range(10)]
        self.assertEqual(load_finding(self.doc(also_agency_ids=ten), "f")["also_agency_ids"], ten)
        legacy_primary = load_finding(self.doc(agency_id="ca-ghost"), "f", known_agency_ids=KNOWN)  # never retroactive
        self.assertEqual(finding_agency_ids(legacy_primary), ["ca-ghost"])
        refused = [
            (self.doc(also_agency_ids=ten + ["ca-extra-10"]), "f.also_agency_ids: must be a list of 1 to 10"),
            (self.doc(also_agency_ids=[]), "f.also_agency_ids: must be a list of 1 to 10"),
            (self.doc(also_agency_ids=None), "f.also_agency_ids: must be a list of 1 to 10"),
            (self.doc(also_agency_ids=SHERIFF), "f.also_agency_ids: must be a list of 1 to 10"),
            (self.doc(also_agency_ids=[SHERIFF, 5]), "f.also_agency_ids[1]: invalid agency identifier"),
            (self.doc(also_agency_ids=[None]), "f.also_agency_ids[0]: invalid agency identifier"),
            (self.doc(also_agency_ids=["Bad Id"]), "f.also_agency_ids[0]: invalid agency identifier"),
            (self.doc(also_agency_ids=[SHERIFF, COUNCIL, SHERIFF]), "f.also_agency_ids[2]: duplicate agency identifier"),
            (self.doc(also_agency_ids=[COUNCIL, POLICE]), "f.also_agency_ids[1]: 'ca-cedar-police' is already the primary"),
            (self.doc(agency_id=None, also_agency_ids=[SHERIFF]), "f.also_agency_ids: needs a non-empty primary agency_id"),
            (self.doc(agency_id="", also_agency_ids=[SHERIFF]), "f.agency_id: invalid agency identifier"),
        ]
        no_primary = self.doc(also_agency_ids=[SHERIFF])
        del no_primary["agency_id"]
        refused.append((no_primary, "f.also_agency_ids: needs a non-empty primary agency_id"))
        for document, needle in refused:
            with self.subTest(needle=needle), self.assertRaises(ContentError) as caught:
                load_finding(document, "f")
            self.assertIn(needle, str(caught.exception))
        for document, needle in ((self.doc(also_agency_ids=[SHERIFF, "ca-nowhere"]), "f.also_agency_ids[1]: unknown agency 'ca-nowhere'"),
                                 (self.doc(agency_id="ca-ghost", also_agency_ids=[SHERIFF]), "f.agency_id: unknown agency 'ca-ghost'")):
            with self.subTest(known=needle), self.assertRaises(ContentError) as caught:
                load_finding(document, "f", known_agency_ids=KNOWN)
            self.assertIn(needle, str(caught.exception))

    def test_content_check_build_and_load_content_agree(self):
        self.add("multi", POLICE, [SHERIFF])
        self.assertEqual(content_problems(self.root), [])
        self.build()
        self.add("unknown-extra", POLICE, [SHERIFF, "ca-nowhere"])
        self.add("self-extra", POLICE, [POLICE])
        problems = content_problems(self.root)
        self.assertEqual(len(problems), 2, problems)
        self.assertTrue(problems[0].startswith("findings/self-extra.json.also_agency_ids[0]: 'ca-cedar-police' is already"))
        self.assertTrue(problems[1].startswith("findings/unknown-extra.json.also_agency_ids[1]: unknown agency 'ca-nowhere'"))
        (self.content / "findings" / "self-extra.json").unlink()
        for call in (load_content, build_site):
            with self.subTest(call=call.__name__), self.assertRaises(ContentError) as caught:
                call(self.root)
            self.assertIn("findings/unknown-extra.json.also_agency_ids[1]: unknown agency 'ca-nowhere'", str(caught.exception))
        # A broken agency list is already named; the unknown-id check does not add a line per finding.
        (self.content / "agencies.json").write_text("{not json", encoding="utf-8")
        problems = content_problems(self.root)
        self.assertTrue(problems)
        self.assertFalse([p for p in problems if "unknown agency" in p], problems)

    def test_publish_and_correct_check_known_ids_before_writing(self):
        helper = publish_campaign()
        helper.setUp_campaign()
        self.addCleanup(helper.tearDownTemp)
        helper.add_agencies()
        bad = publish_tests.draft(agency_id=POLICE, also_agency_ids=[SHERIFF, "ca-nowhere"])
        helper.write_draft(bad)
        helper.approve_all(document=bad)
        result = helper.publish(returncode=1)
        self.assertIn("also_agency_ids[1]: unknown agency 'ca-nowhere'", result.stderr)
        self.assertEqual((helper.published(), helper.log_lines()), ([], []))
        good = publish_tests.draft(agency_id=POLICE, also_agency_ids=[SHERIFF])
        helper.write_draft(good)
        helper.approve_all(document=good)
        helper.publish()
        self.assertEqual(helper.published_document()["also_agency_ids"], [SHERIFF])
        changed = publish_tests.draft(agency_id=POLICE, also_agency_ids=[SHERIFF, "ca-elsewhere"])
        helper.write_draft(changed)
        helper.approve_all(document=changed)
        before, lines, now = helper.published_bytes(), helper.log_lines(), datetime.now(timezone.utc)  # publish ran now
        with self.assertRaises(ValueError) as caught:
            findings.correct(helper.root, "syn-pub-1", now.date().isoformat(), "Added an agency.", helper.draft,
                             now=now, approved_by="Pat Organizer")
        self.assertIn("also_agency_ids[1]: unknown agency 'ca-elsewhere'", str(caught.exception))
        self.assertEqual((helper.published_bytes(), helper.log_lines()), (before, lines))


def publish_campaign():
    """The DF2237-02 synthetic campaign (init, ingested records, draft) without re-running its tests."""
    shared = vars(publish_tests.FindingPublishTests)

    class Campaign(unittest.TestCase):
        setUp_campaign, cli, register, write_draft, receipt, approve_all, log_lines, publish, published = (
            shared["setUp"], shared["cli"], shared["register"], shared["write_draft"], shared["receipt"],
            shared["approve_all"], shared["log_lines"], shared["publish"], shared["published"])

        def runTest(self):  # only so the class can be instantiated; never collected (defined in a function)
            pass

        def tearDownTemp(self):
            self.doCleanups()

        def add_agencies(self):
            shutil.copyfile(EXAMPLE / "content" / "agencies.json", self.content / "agencies.json")

        def published_document(self):
            return json.loads((self.content / "findings" / "synthetic-title.json").read_text(encoding="utf-8"))

        def published_bytes(self):
            return (self.content / "findings" / "synthetic-title.json").read_bytes()

    return Campaign()


class MembershipSurfaceTests(Fixture):
    def multi(self):
        self.add("multi", POLICE, [COUNCIL, SHERIFF], published="2026-03-01T00:00:00+00:00")
        self.add("other-police", POLICE, published="2026-02-20T00:00:00+00:00")
        self.build()

    def test_memberships_every_public_surface_once(self):
        self.multi()
        page = "/findings/multi.html"
        for slug in ("cedar", "cedar-city-council", "cedar-county-sheriff"):
            with self.subTest(slug=slug):
                self.assertEqual(self.links(self.read(f"agencies/{slug}.html")).count("/findings/multi"), 1)
                self.assertEqual(self.read(f"findings/agency/{slug}.xml").count("/findings/multi.html"), 1)
        # the extra-only agencies qualify for a page and a feed; each is listed once in the agencies page cards
        agencies = self.read("agencies.html")
        for slug in ("cedar", "cedar-city-council", "cedar-county-sheriff"):
            article = agencies.split(f'<article id="{slug}">', 1)[1].split("</article>", 1)[0]
            self.assertEqual(article.count(page), 1, slug)
        self.assertIn("/agencies/cedar-county-sheriff.html", self.read("sitemap.xml"))
        cities = self.cities()
        self.assertEqual(cities["cedar"]["findingCount"], 3)  # the example's own finding, multi and other-police
        # finding and packet pages link every agency, primary first, then the extras as entered
        names = ["Cedar Police Department (fictional)", "Cedar City Council (fictional)",
                 "Cedar County Sheriff's Office (fictional)"]
        for html in (self.read("findings/multi.html"), self.read("findings/packet-all.html").split('id="f-multi"', 1)[1]):
            line = re.search(r"<p>Agency:? ((?:<a [^>]*>[^<]*</a>(?:, )?)+)</p>", html)
            self.assertIsNotNone(line)
            self.assertEqual([unescape(n) for n in re.findall(r">([^<]*)</a>", line.group(1))], names)
            self.assertEqual(re.findall(r'href="(/agencies/[^"]+)"', line.group(1)),
                             ["/agencies/cedar.html", "/agencies/cedar-city-council.html", "/agencies/cedar-county-sheriff.html"])
        # primary-only markup is unchanged
        legacy = self.read("findings/other-police.html")
        self.assertRegex(legacy, r'<p>Agency: <a href="/agencies/cedar\.html">Cedar Police Department \(fictional\)</a></p>')
        # open data: the field only on the finding that has it, as entered, once per finding
        data = json.loads(self.read("data/findings.json"))
        law.validate(data, SCHEMA)
        rows = {f["slug"]: f for f in data["findings"]}
        self.assertEqual(rows["multi"]["also_agency_ids"], [COUNCIL, SHERIFF])
        self.assertEqual(rows["multi"]["agency_id"], POLICE)
        self.assertEqual([s for s, row in rows.items() if "also_agency_ids" in row], ["multi"])
        self.assertEqual(len(data["findings"]), len({f["id"] for f in data["findings"]}))
        self.assertEqual(self.read("feed.xml").count("/findings/multi.html"), 1)
        self.assertEqual(self.links(self.read("findings/index.html")).count("/findings/multi"), 1)

    def test_extra_before_primary_input_order_and_no_duplicates_across_pages(self):
        self.add("multi", SHERIFF, [POLICE], published="2026-03-01T00:00:00+00:00")
        self.build()
        data = {f["slug"]: f for f in json.loads(self.read("data/findings.json"))["findings"]}
        self.assertEqual((data["multi"]["agency_id"], data["multi"]["also_agency_ids"]), (SHERIFF, [POLICE]))
        # the only mapped city (police) gets the finding in its count and its page hook although it is an extra there
        self.assertEqual(self.cities()["cedar"]["findingCount"], 2)
        police_page = self.links(self.read("agencies/cedar.html"))
        self.assertEqual(sorted(police_page), ["/findings/cedar-policy-posted", "/findings/multi"])
        sheriff_page = self.links(self.read("agencies/cedar-county-sheriff.html"))
        self.assertEqual(sheriff_page.count("/findings/multi"), 1)
        schema_ids = SCHEMA["$defs"]["finding"]["properties"]["also_agency_ids"]
        self.assertEqual((schema_ids["minItems"], schema_ids["maxItems"], schema_ids["uniqueItems"]), (1, 10, True))
        self.assertNotIn("also_agency_ids", SCHEMA["$defs"]["finding"]["required"])
        for broken in ([], "x", [5]):
            document = json.loads(self.read("data/findings.json"))
            document["findings"][0]["also_agency_ids"] = broken
            with self.subTest(broken=broken), self.assertRaises(ValueError):
                law.validate(document, SCHEMA)

    def test_related_membership_intersection_keeps_ranking(self):
        self.add("r-main", POLICE, [SHERIFF], published="2026-03-01T00:00:00+00:00")
        self.add("s-sheriff", SHERIFF, published="2026-02-11T00:00:00+00:00")
        self.add("t-extra-extra", COUNCIL, [SHERIFF], published="2026-02-12T00:00:00+00:00")
        self.add("u-council", COUNCIL, published="2026-02-13T00:00:00+00:00")
        self.add("v-none", None, published="2026-02-14T00:00:00+00:00")
        self.add("w-police", POLICE, published="2026-02-10T00:00:00+00:00")
        self.add("y-both", COUNCIL, [POLICE, SHERIFF], published="2026-02-10T00:00:00+00:00")
        shared = hashlib.sha256(b"shared-record").hexdigest()
        self.add("x-source", COUNCIL, published="2026-01-01T00:00:00+00:00",
                 sources=[{"sha256": shared, "locator": "page:1"}])
        self.add("z-source", None, published="2026-01-02T00:00:00+00:00", sources=[{"sha256": shared, "locator": "page:2"}])
        self.add("r-twin", POLICE, [SHERIFF], published="2026-03-01T00:00:00+00:00",
                 sources=[{"sha256": shared, "locator": "page:3"}])
        site = self.model_site()
        main = next(f for f in site.model["findings"] if f["slug"] == "r-main")
        ranked = [(o["slug"], n, same) for o, n, same in site.related_findings(main)]
        # no shared record: newest first, then slug; y-both shares two agencies yet counts once; u-council and v-none never
        self.assertEqual(ranked, [("r-twin", 0, True), ("t-extra-extra", 0, True), ("s-sheriff", 0, True),
                                  ("w-police", 0, True), ("y-both", 0, True)])
        self.assertNotIn("r-main", [slug for slug, _, _ in ranked])
        self.assertEqual(len(site.related_findings(main, limit=3)), 3)
        twin = next(f for f in site.model["findings"] if f["slug"] == "r-twin")
        ranked = [(o["slug"], n, same) for o, n, same in site.related_findings(twin, limit=10)]
        # a shared record outranks agency overlap; x/z share only the record, so no agency relation is invented
        self.assertEqual(ranked[:2], [("z-source", 1, False), ("x-source", 1, False)])  # shared record first, newest first
        self.assertEqual([r[0] for r in ranked[2:3]], ["r-main"])
        self.assertNotIn("u-council", [r[0] for r in ranked[:2]])
        self.build()
        page = self.read("findings/r-main.html")
        section = page.split('id="related"', 1)[1].split("</ul>", 1)[0]
        self.assertEqual(re.findall(r'<a href="/findings/([^"]+)\.html"', section),
                         ["r-twin", "t-extra-extra", "s-sheriff", "w-police", "y-both"])
        self.assertEqual(section.count("Same agency"), 5)
        # a finding with no agency is related to nothing by agency
        none_site = [(o["slug"], same) for o, _, same in site.related_findings(
            next(f for f in site.model["findings"] if f["slug"] == "v-none"))]
        self.assertEqual(none_site, [])


class FilterTests(Fixture):
    def fixture(self):
        agencies = json.loads((self.content / "agencies.json").read_text(encoding="utf-8"))
        agencies.append({"agency_id": CITY, "name": "Cedar City (fictional)", "kind": "other"})
        (self.content / "agencies.json").write_text(json.dumps(agencies), encoding="utf-8")
        self.add("a-multi", POLICE, [SHERIFF, COUNCIL], classification="information_gap", event_date="2025-06-01")
        self.add("b-sheriff", SHERIFF, classification="information_gap", event_date="2026-03-02")
        self.add("c-council", COUNCIL, classification="documented_fact", event_date="2025-11-20")
        self.add("d-none", None, classification="documented_fact", event_date="2026-04-01")
        self.add("e-city", CITY, classification="information_gap", event_date="2025-02-02")
        self.add("f-city-extra", SHERIFF, [CITY], classification="documented_fact", event_date="2025-03-03")
        self.build()

    def harness(self, scenarios, relabel=False, root=None):
        script = self.base / "harness.js"
        script.write_text(NODE_HARNESS, encoding="utf-8")
        public = (root or self.root) / "public"
        result = subprocess.run(["node", str(script), str(public), "relabel" if relabel else ""], input=json.dumps(scenarios),
                                capture_output=True, text=True, timeout=60, cwd=self.base)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_filter_executes_exact_memberships(self):
        self.fixture()
        members = {"a-multi": {POLICE, SHERIFF, COUNCIL}, "b-sheriff": {SHERIFF}, "c-council": {COUNCIL}, "d-none": set(),
                   "e-city": {CITY}, "f-city-extra": {SHERIFF, CITY}, "cedar-policy-posted": {POLICE}}
        classes = {"a-multi": "information_gap", "b-sheriff": "information_gap", "c-council": "documented_fact",
                   "d-none": "documented_fact", "e-city": "information_gap", "f-city-extra": "documented_fact",
                   "cedar-policy-posted": "documented_fact"}
        years = {"a-multi": "2025", "b-sheriff": "2026", "c-council": "2025", "d-none": "2026", "e-city": "2025",
                 "f-city-extra": "2025", "cedar-policy-posted": "2026"}
        html = self.read("findings/index.html")
        self.assertIn(f'data-agencies="{POLICE} {SHERIFF} {COUNCIL}"', html)
        self.assertEqual(html.count("data-agencies="), 2)  # only the two multi-agency rows
        self.assertIn(f'<li data-agency="{SHERIFF}" data-classification="information_gap" data-year="2026">', html)  # legacy
        self.assertIn(f'<li data-agency="none" data-classification="documented_fact" data-year="2026">', html)
        self.assertEqual(self.links(html).count("/findings/a-multi"), 1)  # no-JS: the whole list, each finding once
        self.assertIn('id="finding-filter"', html)
        self.assertRegex(html, r'<form class="finding-filter" id="finding-filter"[^>]*hidden>')
        script = self.read("findings/filter.js")
        self.assertEqual(script, filter_js(False, True))
        self.assertNotEqual(script, FILTER_JS)
        self.assertNotRegex(script, r"https?:|import|require\(|eval|innerHTML")
        self.assertLess(len(script.encode("utf-8")), 2400)
        scenarios = [{}, {"set": {"agency": "all"}}]
        scenarios += [{"set": {"agency": agency}} for agency in (POLICE, SHERIFF, COUNCIL, CITY, "none")]
        scenarios += [{"set": {"agency": SHERIFF, "classification": "information_gap"}},
                      {"set": {"agency": SHERIFF, "classification": "documented_fact", "year": "2025"}},
                      {"set": {"agency": CITY, "year": "2025"}}, {"set": {"classification": "information_gap", "year": "2025"}},
                      {"hash": f"#agency={SHERIFF}&year=2025"}, {"hash": f"#agency={CITY}"},
                      {"hash": "#agency=ca-nowhere&classification=nope&bogus=1"}, {"hash": "#agency=%E0%A4%A"},
                      {"hash": "#agency=all"}]
        results = self.harness(scenarios)
        total = len(members)
        self.assertEqual(len(results[0]["shown"]), total)
        self.assertEqual(results[0]["count"], f"Showing {total} of {total}")
        self.assertFalse(results[0]["formHidden"])
        for scenario, result in zip(scenarios, results):
            chosen = dict(scenario.get("set", {}))
            if "hash" in scenario:
                chosen = {k: v for k, v in (pair.split("=", 1) for pair in scenario["hash"][1:].split("&") if "=" in pair)
                          if v in result["options"].get("filter-" + k, ()) and v != "all"}
            expected = {slug for slug in members
                        if (chosen.get("agency") in (None, "all") or (chosen["agency"] == "none" and not members[slug])
                            or chosen.get("agency") in members[slug])
                        and chosen.get("classification") in (None, "all", classes[slug])
                        and chosen.get("year") in (None, "all", years[slug])}
            with self.subTest(scenario=scenario):
                self.assertEqual(set(result["shown"]), expected)
                self.assertEqual(result["count"], f"Showing {len(expected)} of {total}")
                self.assertEqual(result["empty"], bool(expected))
        by_agency = dict(zip([s["set"]["agency"] for s in scenarios[2:7]], results[2:7]))
        self.assertEqual(set(by_agency[CITY]["shown"]), {"e-city", "f-city-extra"})  # not the council rows (prefix-like id)
        self.assertEqual(set(by_agency["none"]["shown"]), {"d-none"})
        self.assertEqual(set(by_agency[COUNCIL]["shown"]), {"a-multi", "c-council"})
        # every membership is an option, including agencies that are only ever an extra; fragment restores the select
        options = results[0]["options"]["filter-agency"]
        self.assertEqual(sorted(options), sorted(["all", POLICE, SHERIFF, COUNCIL, CITY, "none"]))
        restored = results[scenarios.index({"hash": f"#agency={SHERIFF}&year=2025"})]
        self.assertEqual((restored["selected"]["filter-agency"], restored["selected"]["filter-year"]), (SHERIFF, "2025"))
        self.assertEqual(restored["replaced"], [f"#agency={SHERIFF}&year=2025"])
        self.assertEqual(set(restored["shown"]), {"a-multi", "f-city-extra"})
        bad = results[scenarios.index({"hash": "#agency=ca-nowhere&classification=nope&bogus=1"})]
        self.assertEqual((bad["selected"]["filter-agency"], len(bad["shown"])), ("all", total))
        self.assertEqual(results[scenarios.index({"hash": "#agency=%E0%A4%A"})]["selected"]["filter-agency"], "all")

    def test_filter_translated_count_and_no_extra_agencies_keeps_original_bytes(self):
        self.fixture()
        (self.content / "labels.json").write_text(json.dumps(
            {"schema_version": 1, "labels": {"js.filter_count": "Mostrando {shown} de {total}"}}), encoding="utf-8")
        self.build()
        script = self.read("findings/filter.js")
        self.assertNotIn("Showing", script)
        self.assertIn('LABELS["js.filter_count"]', script)
        self.assertIn("data-agencies", script)
        self.assertEqual(script, filter_js(True, True))
        results = self.harness([{}, {"set": {"agency": SHERIFF}}], relabel=True)
        self.assertEqual(results[0]["count"], "Mostrando 7 de 7")
        self.assertEqual((results[1]["count"], sorted(results[1]["shown"])), ("Mostrando 3 de 7", ["a-multi", "b-sheriff", "f-city-extra"]))
        # remove every multi-agency finding: the original generator bytes come back, relabelled or not
        for name in ("a-multi", "f-city-extra"):
            (self.content / "findings" / f"{name}.json").unlink()
        self.build()
        self.assertEqual(self.read("findings/filter.js"), filter_js(True))
        (self.content / "labels.json").unlink()
        self.build()
        self.assertEqual(self.read("findings/filter.js"), FILTER_JS)
        self.assertNotIn("data-agencies", self.read("findings/index.html"))


class HashAndCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.helper = publish_campaign()
        self.helper.setUp_campaign()
        self.addCleanup(self.helper.tearDownTemp)
        self.helper.add_agencies()
        self.original = publish_tests.draft(agency_id=POLICE, also_agency_ids=[SHERIFF, COUNCIL])
        self.helper.write_draft(self.original)
        self.helper.approve_all(document=self.original)
        findings.publish(self.helper.root, self.helper.draft, "Pat Organizer", now=publish_tests.NOW)
        self.path = self.helper.content / "findings" / "synthetic-title.json"
        self.published = json.loads(self.path.read_text(encoding="utf-8"))

    def variants(self):
        return {"added": [SHERIFF, COUNCIL, "ca-cedar-city"], "removed": [SHERIFF], "reordered": [COUNCIL, SHERIFF],
                "dropped": None}

    def draft_with(self, extras):
        document = publish_tests.draft(agency_id=POLICE)
        if extras is not None:
            document["also_agency_ids"] = extras
        return document

    def test_hash_changes_require_fresh_correction_review(self):
        root = self.helper.root
        original_hash = findings.reviewed_hash(self.original)
        self.assertEqual(findings.reviewed_hash(self.published), original_hash)  # publication fields never count
        shutil.copyfile(EXAMPLE / "content" / "agencies.json", self.helper.content / "agencies.json")
        agencies = json.loads((self.helper.content / "agencies.json").read_text(encoding="utf-8"))
        agencies.append({"agency_id": "ca-cedar-city", "name": "Cedar City (fictional)", "kind": "other"})
        (self.helper.content / "agencies.json").write_text(json.dumps(agencies), encoding="utf-8")
        for name, extras in self.variants().items():
            changed = self.draft_with(extras)
            with self.subTest(change=name):
                self.assertNotEqual(findings.reviewed_hash(changed), original_hash)
                status = findings.review_status(root, changed)
                self.assertEqual((status["receipts_for_this_content"], status["stale_receipts"]), (0, 3))
                self.assertNotEqual(findings.review_payload(root, changed, "factual", "approve", "Rev One", "Checked."),
                                    findings.review_payload(root, self.original, "factual", "approve", "Rev One", "Checked."))
                path = self.helper.base / f"{name}.json"
                path.write_text(json.dumps(changed), encoding="utf-8")
                before, lines = self.path.read_bytes(), self.helper.log_lines()
                with self.assertRaises(ValueError) as caught:
                    findings.correct(root, "syn-pub-1", "2026-03-05", "Changed agencies.", path,
                                     now=publish_tests.NOW.replace(day=6), approved_by="Pat Organizer")
                self.assertIn("review blockers", str(caught.exception))
                self.assertEqual((self.path.read_bytes(), self.helper.log_lines()), (before, lines))
        self.assertEqual(findings.reviewed_hash(self.draft_with([SHERIFF, COUNCIL])), original_hash)  # the same list is the same hash

    def test_fresh_approved_correction_retains_identity_and_logs_history(self):
        root = self.helper.root
        shutil.copyfile(EXAMPLE / "content" / "agencies.json", self.helper.content / "agencies.json")
        changed = self.draft_with([COUNCIL, SHERIFF])
        path = self.helper.base / "reordered.json"
        path.write_text(json.dumps(changed), encoding="utf-8")
        self.helper.approve_all(document=changed)
        with self.assertRaises(ValueError):  # no named approver: the publication gate refuses
            findings.correct(root, "syn-pub-1", "2026-03-05", "Reordered agencies.", path, now=publish_tests.NOW.replace(day=6))
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), self.published)
        result = findings.correct(root, "syn-pub-1", "2026-03-05", "Reordered agencies.", path,
                                  now=publish_tests.NOW.replace(day=6), approved_by="Pat Organizer")
        self.assertEqual(result, {"corrected": "synthetic-title", "corrections": 1})
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(document["also_agency_ids"], [COUNCIL, SHERIFF])
        for key in ("id", "slug", "published_at", "state"):
            self.assertEqual(document[key], self.published[key])
        self.assertEqual(document["corrections"], [{"date": "2026-03-05", "note": "Reordered agencies."}])
        entry = json.loads(self.helper.log_lines()[-1])
        self.assertEqual((entry["action"], entry["text_changed"], entry["content_sha256"]),
                         ("correct", True, findings.reviewed_hash(changed)))
        self.assertEqual(len(self.helper.log_lines()), 2)
        self.assertEqual(finding_diff.diff(self.published, document),
                         [{"field": "also_agency_ids", "old": [SHERIFF, COUNCIL], "new": [COUNCIL, SHERIFF]}])


class DiffTests(unittest.TestCase):
    def finding(self, **changes):
        return dict({"title": "T", "agency_id": POLICE}, **changes)

    def test_diff_reports_primary_and_extra_changes(self):
        old = self.finding(also_agency_ids=[SHERIFF, COUNCIL])
        self.assertEqual(finding_diff.diff(old, self.finding(also_agency_ids=[SHERIFF, COUNCIL, "ca-cedar-city"])),
                         [{"field": "also_agency_ids", "removed": [], "added": ["ca-cedar-city"]}])
        self.assertEqual(finding_diff.diff(old, self.finding(also_agency_ids=[COUNCIL])),
                         [{"field": "also_agency_ids", "removed": [SHERIFF], "added": []}])
        self.assertEqual(finding_diff.diff(old, self.finding()),
                         [{"field": "also_agency_ids", "removed": [SHERIFF, COUNCIL], "added": []}])
        reordered = finding_diff.diff(old, self.finding(also_agency_ids=[COUNCIL, SHERIFF]))
        self.assertEqual(reordered, [{"field": "also_agency_ids", "old": [SHERIFF, COUNCIL], "new": [COUNCIL, SHERIFF]}])
        self.assertEqual(finding_diff.render(reordered), [f'also_agency_ids: ["{SHERIFF}", "{COUNCIL}"] -> ["{COUNCIL}", "{SHERIFF}"]'])
        self.assertEqual(finding_diff.diff(self.finding(), self.finding(agency_id=SHERIFF)),
                         [{"field": "agency_id", "old": POLICE, "new": SHERIFF}])
        self.assertEqual(finding_diff.render(finding_diff.diff(old, self.finding(also_agency_ids=[SHERIFF, "ca-x-y"]))),
                         ["also_agency_ids:", f'- "{COUNCIL}"', '+ "ca-x-y"'])
        # primary-only and absent-versus-empty comparisons stay equal
        self.assertEqual(finding_diff.diff(self.finding(), self.finding()), [])
        self.assertEqual(finding_diff.diff(self.finding(), self.finding(also_agency_ids=[])), [])
        self.assertEqual(finding_diff.diff(old, copy.deepcopy(old)), [])

    def test_diff_load_checks_the_shape(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-also-diff-") as folder:
            for name, value in (("also_agency_ids", "not a list"), ("agency_id", 5)):
                path = Path(folder) / f"{name}.json"
                path.write_text(json.dumps({"id": "syn-1", name: value}), encoding="utf-8")
                with self.subTest(field=name), self.assertRaises(ValueError) as caught:
                    finding_diff.load(path)
                self.assertIn(f"key {name!r}", str(caught.exception))


class LegacyAndAtomicTests(Fixture):
    def legacy_fixture(self):
        for n, (slug, agency, code) in enumerate((("two", POLICE, "information_gap"), ("three", SHERIFF, "information_gap"),
                                                   ("four", SHERIFF, "documented_fact"), ("five", None, "documented_fact"))):
            self.add(slug, agency, classification=code, event_date=f"2025-0{n + 1}-01")
        self.build()

    def test_absent_field_byte_identical_and_legacy_read_only(self):
        self.legacy_fixture()
        names = ["findings/filter.js", "data/findings.json", "feed.xml", "findings/index.html"] + sorted(
            p.relative_to(self.public).as_posix() for p in (self.public / "findings" / "agency").glob("*.xml"))
        self.assertEqual(digest("\n".join(f"{n}\n{self.read(n)}" for n in names)), LEGACY_DIGEST)
        self.assertEqual(self.read("findings/filter.js"), FILTER_JS)
        self.assertNotIn("data-agencies", self.read("findings/index.html"))
        self.assertNotIn("also_agency_ids", self.read("data/findings.json"))
        before_content = {p.relative_to(self.content).as_posix(): p.read_bytes() for p in self.content.rglob("*") if p.is_file()}
        self.build()
        self.assertEqual({p.relative_to(self.content).as_posix(): p.read_bytes()
                          for p in self.content.rglob("*") if p.is_file()}, before_content)
        # a legacy finding whose primary agency is not listed is still accepted, rendered and never reassigned
        self.add("ghost", "ca-ghost-agency")
        self.assertEqual(content_problems(self.root), [])
        self.build()
        ghost = {f["slug"]: f for f in json.loads(self.read("data/findings.json"))["findings"]}["ghost"]
        self.assertEqual(ghost["agency_id"], "ca-ghost-agency")
        self.assertNotIn("also_agency_ids", ghost)

    def test_invalid_membership_atomic_and_escaped(self):
        self.legacy_fixture()
        before = self.tree()
        self.add("bad", POLICE, [SHERIFF, "ca-nowhere"], author="PRIVATE-SENTINEL-AUTHOR")
        with mock.patch("socket.socket.connect", side_effect=AssertionError("network")), self.assertRaises(ContentError) as caught:
            self.build()
        self.assertIn("also_agency_ids[1]: unknown agency 'ca-nowhere'", str(caught.exception))
        self.assertEqual(self.tree(), before)
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".public")], [])
        (self.content / "findings" / "bad.json").unlink()
        # hostile direct-model names are escaped on every surface that prints them
        hostile_id, hostile_name = 'ca-evil" onmouseover="x', '<img src=x onerror=alert(1)>"&'
        self.add("fresh", POLICE, [SHERIFF], author="PRIVATE-SENTINEL-AUTHOR")
        model = load_content(self.root, TODAY)
        model["agencies"] = model["agencies"] + [{**next(a for a in model["agencies"] if a["agency_id"] == SHERIFF),
                                                  "agency_id": hostile_id, "name": hostile_name, "slug": "evil"}]
        for finding in model["findings"]:
            if finding["slug"] == "fresh":
                finding["also_agency_ids"] = [SHERIFF, hostile_id]
        site = Site(self.root, read_config(self.root), model, today=TODAY)
        fresh = next(f for f in model["findings"] if f["slug"] == "fresh")
        index = site.findings_index_html()["html"]
        outputs = [index, site.finding_html(fresh)["html"], site.agencies_html()["html"], site.packet_html()["html"]]
        for html in outputs:
            self.assertNotIn("<img src=x", html)
            self.assertNotIn('onmouseover="x', html)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;&quot;&amp;", outputs[1])
        self.assertIn(f'data-agencies="{POLICE} {SHERIFF} ca-evil&quot; onmouseover=&quot;x"', index)
        # a model naming an unknown extra is refused when the Site is assembled (preview and packet paths)
        for finding in model["findings"]:
            if finding["slug"] == "fresh":
                finding["also_agency_ids"] = [SHERIFF, "ca-nowhere"]
        with self.assertRaises(ContentError) as caught:
            Site(self.root, read_config(self.root), model, today=TODAY)
        self.assertIn("findings/fresh.json.also_agency_ids[1]: unknown agency 'ca-nowhere'", str(caught.exception))
        # open data: no author, no full hash; the source library keeps its own agency
        self.build()
        text = self.read("data/findings.json")
        self.assertNotIn("PRIVATE-SENTINEL-AUTHOR", text)
        self.assertIsNone(re.search(r"[0-9a-fA-F]{40,}", text))
        libraries = {s["title"]: s["agency_id"] for s in json.loads(self.read("data/sources.json"))["sources"]}
        self.assertEqual(set(libraries.values()), {POLICE})


if __name__ == "__main__":
    unittest.main()
