# Site content model and multi-page build

The findings index links to `/findings/packet-all.html`, a printable summary of all published findings, with source references and corrections. The packet links to each full finding and omits its body. A published finding whose output path conflicts with a generated page causes the build to refuse without changing `public/`.

`campaign_tool build --directory D` renders `D/public/` from `D/campaign.json`
plus a reviewed `D/content/` directory. Without `content/` the command still
writes the neutral one-page starter (`index.html`, `style.css`, `_headers`),
unchanged. With it, the build produces the full campaign site described here,
wires the agency-card renderer ([AGENCY-CARDS.md](AGENCY-CARDS.md)) and, when a
map is configured, the MapLibre controller ([MAP-CONTROLLER.md](MAP-CONTROLLER.md)).

`content/` holds **reviewed public copy only**. Records originals, mail,
extracted text, subscribers, AI drafts and private analysis never belong there.
The loader (`campaign_tool/content.py`) refuses malformed or unpublishable input
and the build writes nothing in that case; it is not a publication approval,
a legal review or an HTML sanitizer.

**Check before build.** `python3 -m campaign_tool content-check --directory D`
lists every problem in `content/` at once (`content/: N problem(s)`, one line per
file problem, capped at 200) with exactly the rules `build` enforces. It is
read-only and exits 0 when clean or without `content/`, 3 when anything is listed.
Add `--json` for one JSON line (`content-check-v1`, [schema](../schemas/content-check.schema.json)):
`{"count": N, "law_package": ..., "problems": [{"file": "findings/x.json", "message": "..."}], "schema": ..., "truncated": false}`
with the same problems, the first 200 only (`truncated` says more exist, `count` is the full number), `file` null
when a line names no file, and the same exit codes, so an editor or CI step can show each problem at its file.

## Content directory

`python3 -m campaign_tool content init --directory D` writes the minimal valid tree (`site.json` and a draft `faq.json` with `[EDIT ME: ...]` placeholders, empty `findings/` and `meetings/`, `sources.json` `[]`); it refuses an existing `content/`.

### Draft FAQ starter

A fresh `content init` also writes `content/faq.json`: fifteen plain-language questions (what an ALPR is, what the campaign
asks for and why, public-records requests, how long an agency has to answer, why plate reads are not published, how findings
are reviewed, labelled and corrected, how to ask for a correction, how to help, whether the campaign is anti-police, and
whether the site gives legal advice). It is read from the packaged copy of `examples/faq.json`, so it works from an
installed wheel with no repository checkout. Every answer starts `[EDIT ME: verify for this campaign]` and is unfinished
organizer copy: the marker stays visible on `/faq.html` until you replace it, and it is not an approval gate.

- Timing: for a California campaign the deadline answer carries the law package rule `ca-gov-7922.535-determination-and-extension-2026`
  and the package's draft caveat. For any other state that one answer is replaced by generic text that sends the reader to
  the applicable law package and official guidance, with no rule id; no California rule is applied elsewhere.
- Replace every marker, including the correction answer's `[EDIT ME: add a verified public correction contact]`: add a
  contact only after you have verified it. The starter creates no mail link or form. The report's `placeholders` count sums
  the markers in `site.json` and `faq.json`.
- To publish no FAQ, remove `content/faq.json` and rebuild; the page and its links disappear. `content init --from OLD`
  still copies only public branding: a source `faq.json` is listed as skipped and no starter is added.
- Before any publication run `content-check` and `build --check` offline. Neither approves wording: the legal-claim
  guard stays advisory and a clean run does not mean the answers were reviewed. Existing campaigns are not changed.

### Second locality

Initialize the new campaign first, then run `python3 -m campaign_tool content init --directory NEW --from OLD`.
Only public site text, labels and eligible pages are copied; contact, signup and base URL are reset.
The summary lists copied files, replacements, skipped entries and text needing review. Review every page and
all locality-specific claims before any publication; this command builds and deploys nothing.

Finding citations and the source library lead with “From a document we hold”,
the receipt date and agency when known, and public-copy or private-original status.
Uncatalogued citations say so without inventing a receipt date. “Record no.” keeps
the 12-character SHA-256 prefix and existing library links. The on-page explainer
distinguishes unchanged bytes from authorship or truth; it is not authentication.

| Path | Required | Purpose |
| --- | --- | --- |
| `content/site.json` | yes | tagline, `mission_md`, `about_md`, `contact_email`, `signup`, `social[]`, `language`, `base_url`, `county_profile`, `source_link_label`, `font_family`, `not_legal_advice`, `privacy_contact`, `brand` (below), `license` (below), optional `press` (below), optional `corrections` (`{"show_request_counts": true|false}`, default off: publishes per-quarter correction-request counts only, see [CORRECTIONS-PROCESS.md](CORRECTIONS-PROCESS.md); a build with it on reads `private/correction-requests.json`, `content-check` never does), optional `comment_asks` (1 to 3 plain-text asks of 1 to 200 characters for staged comment kits, see [MEETINGS.md](MEETINGS.md); not published as data) |
| `content/findings/*.json` | no | one published finding per file (schema below) |
| `content/meetings/*.json` | no | `body`, `starts_at`, `agenda_url`, `agenda_item`, `location`, `comment_kit_md`, `agency_id`, optional `cited_findings` (1 to 3 unique finding ids, written by `meetings-stage`) |
| `content/sources.json` | no | source-library entries: `sha256`, `title`, `agency_id`, `received_at`, `public_url` or null, `fragment`, `pages`, `note` |
| `content/agencies.json` | no | override list; default is the selected entries of `kit/agencies.json` plus `kit/governing-bodies.json` |
| `content/map/map-config.json` | no | controller settings (`schemas/map-controller.schema.json` keys) plus `county_bounds`, `style_url`, `tile_hosts`, `group_label`, `attribution` |
| `content/map/cameras.geojson` | with map | Point features; properties `manufacturer`, `operator`, `url` (openstreetmap.org object URL or null) and optional `direction` (an OpenStreetMap facing tag string or null; see Map data) |
| `content/map/<city-boundaries>.geojson` | with map | the file named by `city_boundaries_url` (`data/<file>.geojson`); features need `NAME` or `BASENAME` |
| `content/map/county.geojson` | no | county outline, served as `/data/county.geojson` |
| `content/map/cities.json` | no | `{slug: {bounds: [[w,s],[e,n]], name}}` for police/city_council agencies shown in the picker |
| `content/map/attribution.txt` | no | attribution text; the OSM fetch tool writes it |
| `content/vendor/maplibre-gl.js`, `.css` | no | self-hosted MapLibre; see below |
| `content/public-assets/**` | no | images copied into `public/` (png, jpg, jpeg, svg, webp, ico, pdf; 5 MiB each; generated names refused) |

Every text field must pass the same rule as `agency_cards.text`: a string
without `<` or `>`. Markdown fields (`*_md`) are rendered by
`campaign_tool/markdown_lite.py`, which supports paragraphs, `## ` headings,
`**bold**`, `- ` lists, `1. ` ordered lists (`1) ` stays plain text), simple
pipe tables (a `| a | b |` header row, a `|---|:---:|` separator row with the
same 1-12 cells, then rows of that width; `\|` is a literal pipe) and
`[text](https://...)` links only. Raw HTML is
escaped and any link that is not an absolute `https://` URL is rendered as its
text. There are no third-party dependencies.

### Findings

```json
{
  "id": "fnd_0000000000000001", "title": "...", "slug": "cedar-policy-posted",
  "summary": "...", "body_md": "...",
  "classification": "documented_fact", "confidence": "verified",
  "event_date": "2026-01-15", "agency_id": "ca-cedar-police",
  "sources": [{"sha256": "<64 hex>", "locator": "page:1-2", "title": "...", "rule_id": null}],
  "published_at": "2026-02-01T18:00:00+00:00", "updated_at": null,
  "corrections": [{"date": "2026-02-03", "note": "..."}],
  "state": "published", "author": "...", "limitations": ["..."], "counterevidence": []
}
```

A finding is rendered only when `state` is `published`, `confidence` is
`verified` or `likely`, and `campaign_tool.review.review_blockers` reports no
structural blocker (classification, `id`/`author`/`summary`/`sources`/
`limitations`/`counterevidence`, hashed locators, and `event_date`/
`rule_version`/`duty`/`exceptions` for conflict classifications). A finding with
`confidence: needs_attorney_review` stops the whole build with a clear error.
Review receipts are not evaluated by the build; the organizer's publication
approval (see [CONTRACTS.md](CONTRACTS.md), approval card `publish_finding`)
remains the gate that moves a finding into `content/findings/`.

Optional `also_agency_ids` (DF1952-07): a list of 1 to 10 distinct agency ids, none equal to `agency_id`, for a finding that
is about several agencies. It needs a non-empty primary `agency_id` and every id (primary included) must be one of the
campaign's agencies, or `load_finding`, `build`, `content-check`, `finding publish` and `finding correct` refuse it by
field and index; the order you enter is kept. The one finding page then appears on every named agency's page and Atom
feed, counts once in each agency card and city `findingCount`, is listed under each agency in the findings filter and in
related findings (any shared agency counts as "same agency"), and its page and the printable packet link every agency.
`data/findings.json` carries `also_agency_ids` only for such findings. The field is part of the reviewed content hash:
adding, removing or reordering an agency invalidates earlier review receipts, so for a published finding it takes a
`finding correct --from DRAFT` with fresh review. Findings without the field render byte for byte as before. Search,
kits, meeting packs, newsletters, the timeline and law-rule scope still use the primary agency only.

Rule check: every id in `rule_ids` (unique, non-empty, at most 120 characters) and every non-null `sources[].rule_id` must be a
rule in the campaign's law package (`campaign.json` country-state) in force on `event_date`, or `build` stops and
`content-check` names it; `content-check` also prints `law package <jurisdiction>: draft|reviewed|missing`.

Optional `sources[].page` (a whole number from 1 to 100000) adds "Open page N of the public copy" under the source,
linking the library entry's `public_url` with `#page=N` when its path ends in `.pdf` and it has no fragment (otherwise
the bare URL, "Open the public copy (page N)"). `build` stops and `content-check` names a page on a source that is not
in `sources.json`, has no `public_url`, or is above the entry's `pages` (when that is above 0). `data/findings.json`
carries `page` only when it is set.

Optional `sources[].quote` (DF1952-08) is a short passage you copied by hand from the cited record: 1 to 400
characters, at least one that is not a space, plain text with no control or line-break characters (tabs, newlines, DEL,
bidi marks), kept exactly as written (no trimming, no Markdown; angle brackets are ordinary text and are escaped). It
needs the same `locator` as every source. The finding page and the packet show it as a blockquote under the source
line with "Transcribed by the campaign; not checked against the record text." and, when `page` is set, "Page N".
Nothing compares the quote with the record: the campaign checks the transcription by hand. `data/findings.json`
carries `quote` only when it is set; a quote holding a 40-character hex run stops the build (it is never redacted or
cut). The labels `finding.quote_transcribed` and `finding.quote_page` can be overridden in `labels.json`.

Every finding page shows the classification, a confidence label, the event
date, each source with the first 12 characters of its SHA-256 and its locator,
limitations, counterevidence, a Corrections section and the
"This is not legal advice" line.

`/corrections.html` lists every correction on every published finding, newest
first (same date: by finding slug, then the order in the finding), each linking
to the finding's `#corrections` section. The footer and each finding page link
to it, and finding lists mark a corrected finding with a "Corrected" pill
showing its latest correction date. The starter preview has no such page.

A finding with `updates` (`finding update`, see PUBLICATION.md) shows an **Updates** section (`#updates`) under its body,
before the sources: each entry is the label `Update`, its date and its note as escaped literal text, newest date first
(same date: the entry added last first). An update is not a correction: it adds no Corrected pill, no row on
`/corrections.html` and no `Corrected` feed lead, and the labels `finding.updates` and `finding.update` can be
overridden in `labels.json`. `data/findings.json` carries `updates` (`date`, `note`) in stored order only on a finding
that has them; `data/findings.csv` has no such column, so a campaign without updates builds byte-identical files.

A finding page also lists, under **Discussed at public meetings**, the staged meetings whose
`cited_findings` include its id (DF2207-06): newest first, each linking `meetings/<id>.html` with the body,
the recorded date and time in its own UTC offset, the agenda item and the recorded outcome result when any.
Cancelled and future staged meetings are listed. It records that a staged comment kit cites the finding, not
attendance, speaking, adoption or endorsement. Meetings without `cited_findings` (staged earlier, or whose kit
cited nothing) are never inferred from their kit text; a cited id that is not a published finding renders
nothing and never fails the build. A finding with no such meetings renders unchanged.

Share previews: every page carries `og:*` and `twitter:card` `summary` tags from its own title and description
(a finding page: its title and summary, `og:type` `article` with its published/updated times); `og:url` only
when `site.json.base_url` is set on non-finding pages. A finding emits `og:url` only for a root HTTPS
origin (for example `https://example.invalid`, optionally ending in `/`), without credentials, port,
subpath, query or fragment. Configure that origin for a complete absolute preview; this build does
not provision one. Accepted non-root base values keep their existing canonical behavior, but no
finding `og:url` is invented. An optional share image
is set with `site.json.share_image` = `{"path": "<file in public-assets>", "alt": "<1-300 characters, one line>"}`:
the file must be a PNG or JPEG (WebP and SVG are refused) with no image metadata, 1200x630 to 2400x1260
pixels at about a 1.905 ratio, and the alt text is required. Each indexed page then adds `og:image` (with
`og:image:width`, `og:image:height`, `og:image:alt`) and `twitter:card` `summary_large_image` with
`twitter:image:alt`; the image URL is absolute and `twitter:image` is added only when `base_url` is set (without it
`og:image` is root-relative). Refusals name `site.json.share_image.path` or `.alt`. 404 and noindex pages carry no
share tags. Without the key output is unchanged; nothing is fetched.

Each finding has a **Share this finding** link to `/findings/<slug>.html`; copy its target with normal
browser behavior. No clipboard script, send endpoint, tracking or new approval is involved. Preview
title and description are exactly the validated public title and summary, not private source text.
A refused rebuild leaves the previous public tree intact: it does not retract an earlier page.
Removing a finding and successfully rebuilding removes its generated page; slug changes provide no
redirect or link-durability promise. External approval expiry/revocation and crawler caches remain
the publication owner's responsibility; rendering neither verifies nor refreshes approval receipts.

### Open-data licence (`site.json.license`)

`site.json.license` = `{"id": "CC-BY-4.0" | "CC-BY-SA-4.0" | "CC0-1.0", "attribution": "<optional, plain text, one line, at most 200 characters>"}`.
The organizer picks the licence; the tool chooses none and gives no legal advice. Any other id (including a different
case, such as `cc-by-4.0`), an extra key, a non-string `attribution`, one with markup (`<`, `>`), an email address or a
phone-like number, or one over 200 characters is refused as `site.json.license.id`, `site.json.license` or
`site.json.license.attribution`. The id maps to a fixed name and creativecommons.org deed URL in `content.py`.

With the key set: the footer gains one paragraph ("Data and text: <licence>, <attribution>", `rel="license"`, label
`footer.license`); the about page names the licence in one sentence and, when the site has a map, adds a separate line
"Camera points: © OpenStreetMap contributors, ODbL" with links; each CSV link's text ends " (license: <name>)" (the CSV
bytes do not change); and `data/findings.json`, `data/sources.json`, `data/meetings.json` and `data/agencies.json` gain
top-level `license` (the id) and `license_url`. Without the key none of these change.

**ODbL rule.** Mapped camera points come from OpenStreetMap, so `data/cameras.geojson` always carries the top-level
GeoJSON foreign members `license` = `ODbL-1.0`, `license_url` (the opendatacommons.org ODbL 1.0 text), `attribution` =
`© OpenStreetMap contributors` and `attribution_url`, whatever `site.json.license` says and even when it is absent. The
points are not changed. This is the one file a build without `license` still changes. Boundary files are not derived
from the camera data and are copied as before.

### Press page for reporters (`site.json.press`)

Optional. `site.json.press` = `{"contact_role": "<1-80 characters>", "contact_email": "<optional>", "about_md": "<optional, up to 1500 characters>"}`
builds one public page, `/press.html`, with the campaign description, a press contact, the ten newest published
findings with their citation text, the logo download and the open-data files with their licences.

```json
"press": {"contact_role": "Media contact", "contact_email": "media@your-campaign.example", "about_md": "Reporters: we can walk you through any finding and its records."}
```

**Review the address before you set it.** The role and the address are published on a public page and in the page
source. Use an address the organizers have agreed to make public; the tool never falls back to `privacy_contact`, an
author, an agency or a kit contact, and `init --from` never copies `press` to another locality.

- `contact_role` is required: one line, 1 to 80 characters, no control characters; the page escapes it.
- `contact_email` defaults to the validated `site.json.contact_email`. An explicit value must be a non-empty valid
  address; `null`, an empty string or a malformed address is refused as `site.json.press.contact_email`, and so is a
  missing one when `site.json.contact_email` is not set either.
- `about_md` is plain text without angle brackets, up to 1500 characters (empty is fine). It renders after the
  `site.json.about_md` campaign description and never replaces it.
- Unknown fields, a non-object `press` and `null`/`false`/`[]` are refused as `site.json.press`. Without the key there is
  no page, no footer link and no sitemap entry, and every other output is byte-identical. Removing the key and
  rebuilding removes `press.html`.

Findings: the ten newest by publication instant (UTC, then slug, descending; a correction, update or event date never
reorders them), each with title link, summary, classification, confidence, publication date, the same "Cite this
finding" text as the finding page and `data/findings.json`, and, when it has corrections, the usual "Corrected <date>"
marker (newest correction) linking to that finding's `#corrections`. No copy button or script is added. With no
findings the page shows the usual no-findings notice. Only published findings are listed.

Logo and data: with `site.json.brand.logo` the page shows it and links the same copied `/<logo>` file with a `download`
attribute (no second copy). It then links only the files this build writes: `data/findings`, `sources`, `agencies` and
`meetings` as `.json` and `.csv`, `data/cameras.geojson` with a map, and the copied `data/county.geojson` and
city-boundary file when present. Beside each link: `cameras.geojson` always shows ODbL and the OpenStreetMap credit;
every other file shows `site.json.license` (name, deed link, attribution), or "License not specified" when you have set
none (the tool never picks one). The page adds no keys to any data file.

`press` is a reserved page and finding slug: a `content/pages/press.json` is refused even with press disabled, by `build`
and by `content-check` (which now also runs the pages check). `press.html` is a generated top-level name, so a redirect
or public asset at that path is refused. There is no top-nav item, feed entry or script. Labels: `footer.press`,
`press.title`, `press.description`, `press.contact`, `press.findings`, `press.logo`, `press.data`,
`press.download_logo`, `press.license_unspecified`; the finding labels (`finding.cite`, `finding.corrected`,
`finding.published`, `notice.no_findings`) are reused. `examples/labels/es.json` carries Spanish drafts (still unreviewed).

### Agencies, cards and the picker

Agencies come from `content/agencies.json` or the kit. Each gets a slug (the
`agency_id` without its state prefix unless `slug` is set). The county profile
for the card renderer is assembled from `campaign.json` (county name) and
`site.json.county_profile`; per-agency `profile` blocks fill the `CITIES`
entries. Police and city_council agencies appear in the picker; when a map is
configured only those with bounds in `map/cities.json` do, because the
controller fits the map to `CITIES[slug].bounds`.

An agency with `verified: true` must carry `verification` (written by
`kit-verify-agency`): `{"source_url": "https://…", "checked_on": "YYYY-MM-DD",
"fields": [...]}`. `source_url` must be an https URL, `checked_on` a real date
not after the build date, and `fields` (optional) names only `records_email`,
`records_url` or `portal`; unknown keys are refused. `verified: true` without it
stops the build naming `<file>[n].verification`. A verified agency shows
"Contact checked <date> on the official source" on `agencies.html`; the URL is a
link only and is never fetched. With `verified` not true the object is still
validated but not shown, and the "Contact details unverified" hint stays.

An agency's optional `profile.alpr_policy` links a posted ALPR usage policy an
organizer found: `{"url": "https://…", "source_url": "https://…", "checked_on":
"YYYY-MM-DD"}`, exactly these three keys. `url` is the policy itself and
`source_url` the page where it was found (both https); `checked_on` is a real
date not after the build date. A missing key, an extra key or a bad value stops
the build naming `<file>[n].profile.alpr_policy` (and the field); `null` or
absent shows nothing. `agencies.html` shows "Posted ALPR usage policy (checked
<date>, where we found it)" on that agency's card only; neither URL is fetched.
Linking a policy says only that it was posted; it is not a finding about compliance.

An agency's optional `profile.policy_check` records an organizer's reading of the posted `alpr_policy` against the
draft checklist `jurisdictions/us-ca/policy-checklist.json` (bound to the law-package rule
`ca-civ-1798.90.51-usage-and-privacy-policy`, Civ. Code § 1798.90.51(b); status `draft`, packaged byte-identical and
checked by `tools/sync_campaign_resources.py`): `{"checked_on": "YYYY-MM-DD", "policy_url": "https://…", "elements":
{"<id>": {"status": "addressed"|"not_found"|"unclear", "page"?: 1–9999}}}`. `policy_url` must equal
`profile.alpr_policy.url` (so `alpr_policy` must be present); every checklist id (`authorized_purposes`,
`authorized_users_training`, `security_monitoring`, `sharing_restrictions`, `custodian_title`, `accuracy_correction`,
`retention_destruction`) must be present and no other; `page` only with `addressed`; `checked_on` a real date not after
the build date and not before `alpr_policy.checked_on`. A bad entry stops the build and `content-check` flags it, naming
every field as `<file>[n].profile.policy_check.<field>` on one line. The agency page and the agencies-list card show
"What the posted policy covers (organizer reading, not a legal review)": one line per element in checklist order
("addressed (page N)", "not found in the posted policy" or "unclear"), the check date, a link to the posted policy and the
law package status label. It is an organizer's reading of what the document says, not a finding about compliance, and
`not found` is never read as more than that. Nothing is fetched; the section is not in card JS, open data or feeds.

An agency's optional `profile.pre_implementation_meeting` records the public meeting an organizer found before the
agency's ALPR program started, for the public-comment rule in Civ. Code § 1798.90.55(a):
`{"status": "found"|"not_found_yet", "checked_on", "meeting_on"?, "body"?, "source_url"?, "note"?}`. `found` needs
`meeting_on` (a real YYYY-MM-DD date not after the build date), `body` (1-120 characters) and an `https://` `source_url`
(the official record); `not_found_yet` refuses all three. `checked_on` is a real date not after the build date and not
before `meeting_on`; `note` is up to 200 plain-text characters. Unknown fields are refused. Each problem is named
`<file>[n].profile.pre_implementation_meeting.<field>`; the build stops at the first and `content-check` lists them all.
The agency page and the agencies-list card show "Public meeting before the camera program": "`{body}` discussed it on
`{meeting_on}` (official record). Checked `{checked_on}`." or "We have not found a record of one yet (checked
`{checked_on}`).", then "Rule: `{citation}`. Law package: `{status}`." and the note. The citation and status come from the
shipped law package by rule id `ca-civ-1798.90.55-public-comment-before-implementation`; if the package lacks the rule
the Rule line is left out and the build is not refused. It is an organizer-verified record and never fetched: "not found
yet" is not a statement that no meeting was held, and the section makes no finding that one was required, missed or
adequate. It is not in card JS, open data or feeds.

An agency's optional `profile.sharing` (DF1207-08) records the agencies the vendor transparency portal names as receiving
the agency's data, as an organizer transcribed them: `{"checked_on", "source_url", "recipients": [{"name", "state", "kind"}]}`.
Record it with `python3 -m campaign_tool content agency-sharing --directory D --agency ID --csv FILE --checked-on YYYY-MM-DD
--source-url URL [--replace]`. The CSV is UTF-8 (a BOM is allowed) with the header exactly `name,state,kind`: `name` 1-160
characters without control characters, `state` a two-letter USPS code (50 states, DC, PR, AS, GU, MP, VI) or `US` for a
federal agency (`US` only with `federal`), `kind` one of `local`, `state`, `federal`, `private`, `unknown`; 1 to 2000 rows,
and the same name (any letter case) and state twice is refused. `--source-url` is an `https://` page on
`transparency.flocksafety.com`; `--checked-on` is a real date not after today. Every bad row is named by its line (at most 20,
then "and N more"); on any error nothing is written. The file is read as a plain file, never fetched, and a symlink is refused.
The whole `content/agencies.json` is validated and then written once; an agency that already has a list needs `--replace`.
Recipients are stored sorted by state, then name. The agency page and the agencies-list card show "Who the vendor portal lists
as receiving data": "As listed on the vendor transparency portal, transcribed by the organizer (checked `{date}`): `{n}`
agencies.", a count by state (the campaign's own state first) and by kind, then "Out of state and federal:" with the name,
state and kind of every entry outside the campaign's state or of kind `federal` (at most 100, then "and N more"), then the
sharing rule (`ca-civ-1798.90.55-sharing-limits`) and the 2023 Attorney General bulletin
(`ca-oag-2023-dle-06-out-of-state-sharing-guidance`) as cited by the shipped law package, and "Law package: `{status}`." A
rule the package lacks leaves out its line and never refuses the build. It lists what the portal names and draws no
conclusion about any agency; `build` and `content-check` refuse a hand-edited list that breaks these rules. It is not in card
JS, open data or feeds, and the portal is never contacted.

With at least one agency list, the build also writes `/sharing.html` (DF0237-04), "Who receives data from our agencies", with
a footer link "Data-sharing lists" and a sitemap entry (only with a `base_url`); with none, none of these exist and every other
file is unchanged. One row per receiving agency, grouped "Out of state", "Federal" (kind `federal` or state `US`), then
"In state", each group a captioned table: receiving agency, state, kind (Local / State / Federal / Private / Unknown), how many
of the campaign's agencies list it and, for each of those, a link to its agency page, a `source list` link and `checked DATE`.
Two names are one row only when they differ in letter case, whitespace or one trailing period, and have the same state and
kind; the smallest spelling is shown with the others under "Also transcribed as:". The count is agencies that list a receiver,
not plates, transfers or searches; rows are in alphabetical order inside a group, never ranked by count. The page cites the
same two rules with their official links and "Law package: `{status}`." ("status unavailable" if the package cannot be read),
and says a listing does not establish that a transfer occurred. Its wording is in labels `sharing.*` and `footer.sharing`
(`examples/labels/es.json` holds an unreviewed Spanish draft); the legal-claim guard scans it, alert-only, as `sharing.html`
in `site_notes`. `sharing` is a reserved page, topic and finding slug even with no lists.

An agency's optional `profile.contract_check` (DF2207-03) records an organizer's reading of the linked ALPR vendor
agreement against the draft, tool-wide checklist `checklists/contract-checklist.json` (not part of any law package;
status `draft`, packaged byte-identical and checked by `tools/sync_campaign_resources.py`): `{"checked_on": "YYYY-MM-DD",
"contract_url": "https://…", "elements": {"<id>": {"status": "addressed"|"not_found"|"unclear", "page"?: 1–9999,
"note"?: 1–200 characters}}}`. `contract_url` must equal `profile.contract.source_url` exactly (so a valid
`profile.contract` must be present); "verified" here means the organizer checked that source on that date, not the
agency contact flag. Every checklist id (`term_renewal`, `price_changes`, `data_ownership`, `retention`,
`sharing_network`, `vendor_data_use`, `audit_search_logs`, `termination_data_exit`) must be present once and no other;
`page` is allowed with any status; `note` is plain text (no angle brackets or control characters) kept as written;
`checked_on` is a real date not before `contract.checked_on` and not after the build date. A bad entry stops the build
with the first problem and `content-check` lists every problem, each named `<file>[n].profile.contract_check.<field>`.
The agency page and the agencies-list card show "What the contract says (organizer reading, not a legal review)":
one line per element in checklist order ("addressed", "not found in the linked contract" or "unclear", then "(page N)"
and the note), the check date, a link to the contract and "Checklist: draft". "Not found" describes the reading only,
not whether a term exists elsewhere; there is no score, verdict or inferred duty. The `legal_claims` guard stays
alert-only and can still flag an accusation typed into a note. Nothing is fetched; the section is not in card JS, open
data or feeds, and an agency without `contract_check` shows no section.

An agency's optional `asked` (≤ 20 items) lists what the organizer asked it: `{"topic", "asked_on", "state",
"updated_on"?, "note"?}`. `topic` ≤ 120 and `note` ≤ 200 plain-text characters; `asked_on`/`updated_on` are real
YYYY-MM-DD dates not after the build date, `updated_on` not before `asked_on`; `state` is one of `awaiting_response`,
`acknowledged`, `partly_answered`, `answered`, `declined_in_writing`, `withdrawn`. A bad item stops the build naming
`content/agencies.json[n].asked[i].<field>`; `content-check` lists every such problem, one line each. Kit agencies
never carry it. It is shown only on `agencies.html` (docs/AGENCY-CARDS.md) and `requests.html` (below), never exported to data or feeds.

`agencies.html` opens with an "At a glance" table (`#at-a-glance`, labels `agencies.glance_*`): one row per agency in
the list's own order, never sorted by any number, with no ranking, score or "best/worst" wording. Cells: cameras,
searches in the last 30 days and retention days from the newest `profile.transparency` snapshot (each linked to its
`source_url`, checked date shown), the posted `alpr_policy` ("posted", linked to the policy), `contract.end` linked
to the contract source, `contract.cost` as stated (`$12,345.60 per year`, linked to the contract source with its checked
date; "not checked" without a cost) and the number of `asked` items linked to that card's `#<slug>-asked` list (0 shows "0").
A missing value shows "not checked"; content has no "policy not found" record yet, so that state is never shown.
No agencies, no table. On screens ≤ 640 px rows stack with each cell's header; print keeps the bordered table.

#### What we have asked for (`requests.html`)

`/requests.html` (DF1103-13, heading and title "What we have asked for") is always built for a content-backed campaign,
including with no agencies or no `asked` items; the no-content starter never gets it. Its only source is the already
validated `asked` list of each agency in `content/agencies.json`: it never reads `kit/agencies.json`, `kit/sent.json`,
a correction-request file, the ledger or any other organizer state. One row per list element, so identical entries stay
separate rows. Columns, in order: Agency (linked to its `agencies/<slug>.html` page, or to its card `agencies.html#<slug>`
when it has no individual page of its own), Topic, Asked on, State, Updated on. Rows run newest `asked_on` first; equal
days go by `agency_id`, then by position in that agency's list, and an `updated_on` never reorders them. State is exactly
the sentence the agency card uses (`asked_sentence`, no note added); `awaiting_response` with no `updated_on` shows the
build day, like the card, so that sentence changes from day to day. A missing `updated_on` shows "Not recorded", never the
asked date or today's date. The lead gives the number of rows and a count for each of the six states in the order
`awaiting_response`, `acknowledged`, `partly_answered`, `answered`, `declined_in_writing`, `withdrawn` (zero counts included),
counted from the very rows shown; no state is inferred and nothing says a response is late. With no rows the page says "No
public requests are listed yet." after the zero counts and shows no table. It reuses the `.glance-table` markup, so rows
stack on phones with each cell's header, and adds no script, style, CSP rule or asset. It is linked as "What we have
asked for" in the footer between the findings feed and Corrections (on every page), is listed once in `sitemap.xml` (with
`base_url`, no `lastmod`), `requests.html` is a generated path (`public-assets/requests.html` is refused) and `requests` is a
reserved page slug. Labels: `requests.*` and `footer.requests`.

### Contract cost as stated (`profile.contract.cost`)

A contract may carry an optional `cost`, `{amount_usd, period, as_stated_in}` (exactly these three; `null` is invalid),
transcribed by an organizer from the document the contract's own `source_url` points to. It adds no second source or
date: the enclosing contract's `source_url` and `checked_on` are mandatory and are the cost's provenance. `amount_usd`
is a decimal string from 0 to 100000000 with at most two decimals, no sign, exponent, spaces, separators or leading
zeroes (a JSON number is refused; it is never converted to a float and the accepted string is kept as written).
`period` is `annual`, `total` or `one_time`. `as_stated_in` is the document's plain title, 1 to 120 characters, with no
angle brackets, control or invisible characters and not a URL. The card and the agency page then show
`Contract cost as stated: $12,345.60 per year (Example agreement; source, checked 2026-10-21).` (`total`, `one time`
for the other periods; `source` links the contract source), and the glance table has a "Contract cost as stated"
column between "Contract ends" and "Records items asked". A bad cost stops `build` (`Stopped: ...`, exit 1) and is
listed by `content-check` (exit 3) with the same line, e.g.
`content/agencies.json[1].profile.contract.cost.period: choose annual, total or one_time`; the supplied value is never
echoed. The site never adds costs up, ranks by them or divides them by camera counts; agencies keep the list's order.
Labels: `agency.contract_cost`, `agency.cost_annual`, `agency.cost_total`, `agency.cost_one_time`, `agency.cost_source`,
`agencies.glance_cost`. A contract without a cost reads and renders exactly as before.

### How an agency is paid for, as stated (`profile.funding`)

An optional `profile.funding` is a list of 1 to 10 entries (`null`, `[]` and a single object are refused), each
transcribed by an organizer from one document: `kind` (`grant`, `general_fund`, `asset_forfeiture`, `donation` or
`other`), `name` (the program or donor organization exactly as the document writes it, 1 to 120 characters, already
trimmed, no angle brackets, control or invisible characters; the organizer checks that it is not a person), `as_stated_in`
(the document's plain title, the same rule as the contract cost), `source_url` (https, never fetched) and `checked_on`
(a real `YYYY-MM-DD` day not after the build date). Optional `amount_usd` with `period` (both or neither; the contract
cost rules and display), and `fiscal_year` (four ASCII digits as the document states it, first digit 1 to 9; a future year
is allowed, `checked_on` is not). Each entry carries its own document, source and date, so two entries never share
provenance. Entries keep the order written and are never summed, sorted, merged or compared. The agency card and an
existing agency page then show `How it is paid for, as stated in DOC (source, checked DATE)` after the kind, name,
amount and fiscal year of each entry; funding alone does not create a page. `data/agencies.json` carries an optional
`funding` array (the same keys; omitted when none) for agencies that have a page. A bad entry stops `build` (exit 1) and
is listed by `content-check` (exit 3) with the entry's path and rule, e.g.
`content/agencies.json[1].profile.funding[0].kind: choose one of [...]`; the supplied name or key is never echoed.
The site says nothing about a donor beyond the name in the document. Labels: `agency.funding_provenance`,
`agency.funding_fiscal_year`, `agency.funding_kind_grant`, `agency.funding_kind_general_fund`,
`agency.funding_kind_asset_forfeiture`, `agency.funding_kind_donation`, `agency.funding_kind_other`. A profile without
`funding` reads and renders exactly as before.

### Agency actions outside a meeting (`content/agency-actions.json`)

What an agency's own official source says it changed outside a recorded meeting outcome (a policy posted, sharing ended, a
contract signed) can be transcribed by an organizer, one dated action at a time. The file holds exactly
`{"schema_version": 1, "actions": [ROW, ...]}` (an empty list is valid; no file means no section, no page and no event) and each
ROW has exactly `agency_id` (one of this campaign's selected agencies, matched exactly), `on` (the day it took effect), `kind`
(`policy_adopted`, `policy_amended`, `policy_posted`, `sharing_started`, `sharing_ended`, `retention_changed`,
`contract_signed`, `contract_renewed`, `contract_ended`, `contract_paused`, `cameras_added`, `cameras_removed` or `other`),
`as_stated` (1 to 200 characters exactly as the official source states it, already trimmed; no angle brackets, control,
invisible or line-separator characters; accents, apostrophes and ampersands are kept as written), `source_url` (an https
address of that source, 1 to 500 characters; recorded, never fetched) and `checked_on` (the day the organizer checked the
source). Dates are real `YYYY-MM-DD` days with `on` not after `checked_on` and `checked_on` not in the future. An agency has at
most 50 actions, and the same agency + `on` + `kind` + `source_url` twice is refused, whatever the statement or checked date.

```
python3 -m campaign_tool content agency-action add --directory DIR --agency ID --on 2026-09-01 --kind policy_posted \
    --as-stated "Use policy posted on the department website" --source-url https://example.org/policy --checked-on 2026-10-01
python3 -m campaign_tool content agency-action remove --directory DIR --agency ID --index 2
```

Both commands need `--directory` and an existing `content/`; `add` needs every flag and has no replace or update form, and
neither creates `content/` or touches any other file. They print `Agency action added: ID, index N.` or
`Agency action removed: ID, index N.` and `Content only; nothing was built, sent or deployed.`; a refusal exits 1 (a missing or
unknown flag exits 2). `--index` is the 1-based position among that agency's actions in the **file** (insertion order), not the
page's newest-first order, and is the `N` that `add` prints; removing the last action leaves a valid empty file. The whole
file is checked before and after a change and written with one rename, so a refusal or a failed write leaves the old bytes;
links, hard links, special files and a file that changed meanwhile are refused. Only one organizer should edit at a time.
Problems name `content/agency-actions.json.actions[N].FIELD` (N counts from 0 in file order, as the other content messages do)
and never repeat a statement, address or unknown key, from `build` (exit 1) and `content-check` (exit 3) alike.

The agency's own page then shows **Changes on record** after its funding and before the transparency block, newest `on` first
(same-day actions keep file order), one line each: `D: <kind label>: T (source, checked DATE)` with the source link. An agency
with an action has a page even without a finding or source. `timeline.html` lists each as kind `agency_action`
(`AGENCY: KIND: T`, linked to the agency page with its official record) and, only when there is one, appends "Agency actions are
transcribed from their cited official sources." to its lead. `data/agencies.json` carries an optional `actions` array (omitted
when none; `on`, `kind`, `as_stated`, `source_url`, `checked_on`, newest first, 1 to 50). It is not in `data/agencies.csv`, whose
header never changes with the data. Labels: `agency.actions_heading`, `agency.action_line` (`{on}`, `{kind}`, `{statement}`,
`{source}`, `{checked}`; the source word is `agency.cost_source`) and `agency.action_kind_<KIND>`; `examples/labels/es.json` has
unreviewed Spanish drafts.

**The organizer is responsible for the transcription.** The software does not fetch, read or confirm the source, and it infers
nothing: a policy that is posted is not a finding that it is followed, and no sentence says the campaign or anyone else caused
a change. An alert-only legal-claim check runs on each statement: `add` prints a `review-required` note on stderr (location and
rule names, never the text) and still succeeds, and the build's review notes cover hand-edited rows the same way. The note is
for a human at the publish gate and is never an error. Meeting decisions stay separate (`meetings/*.json` outcomes); nothing is
merged with, or removed because of, a meeting.

### Local rules on record (agency page, from `law/local-rules.json`)

An existing agency page (`agencies/<slug>.html`) shows a `Local rules on record` section, after the facts,
transparency, officials and votes blocks and before `Published findings`, with one entry per organizer-transcribed local
rule whose `agency_ids` names that agency's exact `agency_id` (every dated version of a rule is its own entry; no prefix,
agency-name, city or county matching). The rules are the validated overlay the build already loaded (see LAW-PACKAGES.md);
the section reads nothing else, fetches nothing and adds no field to `data/agencies.json`, `data/findings.json` or
`data/sources.json`. Each entry has its own heading with the citation, the `rule_id`, `Duty as written` (the transcribed text,
escaped, never Markdown or HTML and never restated as an obligation of the agency), `Effective from` and `to` (`open` when
there is no end), a link `Official source (organizer-recorded)` and the day it was `checked`, and always the exact label
`local rule, organizer-transcribed, not legally reviewed`, even when the state package is reviewed: the checked day is the
organizer's, not a legal review date. Entries are ordered by `rule_id` then `effective_from`.

The build date (the day the build runs; `build_site(today=...)` in tests) classifies each entry with inclusive bounds:
`Past — not in force on build date` when `effective_to` is before it, `Not yet in force on build date` when `effective_from`
is after it (a scheduled rule is not described as already law), otherwise `In force on build date`. An agency with no scoped
rule gets no heading, list or whitespace: its page is byte-identical to one built without the overlay, and a local rule
alone never creates a page or an `agencies.html` entry (a page still needs a published finding or a source record).
The section states that these are organizer transcriptions, not a determination about the agency's conduct. It is not legal
advice and never says an agency complied or did not comply. The organizer and a human reviewer remain responsible for whether the source is
authentic and whether the ordinance applies; nothing is checked automatically.

`source_aliases` are derived: a city agency whose `agency_id` has entries in
`sources.json` routes its evidence link to that entry's `fragment`, which is
also the `id` of the matching section in `sources.html`. Agencies without
sources route to `sources.html#library`.

Each `sources.html` entry ends with `Cited by:` and links to every published
finding that cites its `sha256` (model order, once each), or with
`Not cited by a published finding`. Drafts never appear.

Record pages (DF1103-11): every library entry also gets `sources/<first 12 hex of its sha256>.html` (labels
`sources.record_description`, `sources.record_page` = "Record details"). The page shows the title, the received date, the
agency (linked to its page when it has one), the record number with the "How to check a copy" steps (on every record page,
checked or not), the page count (0 included), the public copy link or the private-archive notice, the recorded copy check,
the public note and each published finding that cites it, once, in model order. It is built only from the published model:
originals, the ledger, the kit and private drafts are never read. Library entries link to it ("Record details"), and the
record-number link on a finding page points to it when the entry is in the library; an uncatalogued citation stays unlinked.
Packet, timeline, agency-card and agency-page links still use the `sources.html#<fragment>` group anchors. Record pages are in
`sitemap.xml` (with a `base_url`, no `lastmod`), last, in library order; with no library entries none are written.

Record numbers must be unique: any two library entries sharing the same 12-character prefix, including one `sha256` listed under
two fragments, refuse the build naming the prefix and the entries' positions in `sources.json` (never a full hash). Nothing is
overwritten or merged for you: consolidate the aliases by hand (keep one entry per record) before building. `sources add`,
`sources edit` and `sources verify-copy` keep their own rules and do not rewrite the library for this. The top-level name
`sources/` is reserved: `public-assets/sources/...` is refused as a generated path, with or without entries, and the slug
`sources` is already a reserved content page.

Hook contract (read by the library filter below): each outer library `<li>` has `class="source-entry"`,
`data-agency` (the entry's `agency_id`), `data-year` (the first four characters of `received_at`, no time-zone conversion),
`data-cited` (`true` when a published finding cites it, else `false`) and `data-record` (the 12 lowercase hex characters).
Every entry stays visible; the agency-document-group sections keep their ids.

#### Filtering the library (`sources/filter.js`)

A library with at least one record gets a hidden filter form above the groups on `sources.html`: an **Agency** select
(agencies by display name, then id; an `agency_id` the campaign does not list is shown as written), a **Received year**
select (newest first, the calendar year in the stored `received_at`, no time-zone conversion), a **Cited by a finding**
checkbox and a live count ("Showing N of M", counted in records, not groups). Both selects and the checkbox appear even
for one agency or one year. `sources/filter.js` (first-party, under 4096 bytes, `textContent` and `hidden` only, no
fetch, storage or `innerHTML`) applies agency AND year AND cited, hides a group when none of its records is visible,
shows "No source record matches these filters." at zero, and reveals the form only after it has applied the filter once.
Only the outer `source-entry` rows are records.

The state lives in the URL hash, in the order `agency`, `year`, `cited`; defaults are omitted and a checked box is
`cited=1` (for example `/sources.html#agency=ca-example-police&year=2026&cited=1`). A value that is not one of the
form's options is ignored, the last valid duplicate wins, a malformed escape or unknown key is ignored, and every
`hashchange` is read from fresh defaults. A hash with a filter key is rewritten to its normalized form with
`history.replaceState` (never by assigning `location.hash`); a hash with no filter key, such as a group anchor
`#cedar-police-records`, is left alone and shows the full list. Changing a control back to all defaults resets the
address to the path and query string. Without the History API the filter still works.

Without JavaScript nothing changes: every group and record is visible in `content/sources.json` order with its anchors
and record links; only the form and the empty message are hidden. Printing shows the complete library whatever the
filter hid on screen, and hides the controls. The count text always comes from `js.filter_count`: `sources.html`
loads `/site-data.js` before `/sources/filter.js`, and on a site with no relabelled `js.*` key `site-data.js` carries a
one-key `LABELS` line (`js.filter_count` only) so long as the library is not empty. An empty library has no form, no
script and no such line. Labels: `sources.filter_label`, `sources.filter_agency`, `sources.filter_year`,
`sources.filter_all_agency`, `sources.filter_all_year`, `sources.filter_cited`, `sources.filter_empty`.

`content/officials.json` (optional, never fetched) lists who decides for a body:
`{"schema_version": 1, "bodies": [{"agency_id", "officials": [{"name", "role",
"profile_url"?, "source_url", "checked_on"}]}]}`. Each `agency_id` is a loaded agency,
listed once, with 1 to 30 officials (at most 100 bodies). `name` and `role` are plain
text of at most 120 characters with no `@` and no phone-like run of 7 digits; there are
no email or phone fields, and both URLs are https only. `checked_on` (the day you checked
the body's official site) must not be after the build date or more than 365 days before
it: re-check and update it. The body's agency page and each meeting page with its
`agency_id` show a "Who decides" list (names linked to `profile_url` when given) with the
newest `checked_on` and its source link. Labels: `officials.*`.

### Adding a source record

```
python3 -m campaign_tool sources add --directory D --sha256 PREFIX --title T \
  --agency-id A --received-at 2026-01-20T16:30:00+00:00 --fragment F \
  [--public-url https://...] [--pages N] [--note TEXT]
```

`PREFIX` is the full hash printed by `ingest` or a unique prefix of at least 12
hex characters; it must name an object in `private/ledger.sqlite` (opened
read-only). The new entry is appended, the whole list is validated by the same
loader as `build`, `agency_id` must be one of the campaign's agencies, and the
file is replaced atomically; on any refusal it is left untouched. `--public-url`
is only recorded, never fetched or uploaded, and nothing is published.

`sources add-batch --directory D --source-id ID --agency-id A --received-at T
[--fragment F] [--dry-run]` registers every record received under one ingest
`source_id` (read through `status --list`'s in-memory ledger copy) whose hash
is not yet in the library, in one validated atomic write of at most 500 new
entries. Each title is the original file name stem as plain text (at most 120
characters) followed by `[EDIT ME: title]`; `public_url` and `pages` are null
and the note says which batch added it. `--fragment` defaults to the agency's
one existing fragment. It prints `{"added": [12-char prefixes],
"already_listed": n, "dry_run": bool}`; `--dry-run` writes nothing, and a
second run adds nothing. Nothing is fetched or published.

`sources add-batch --directory D --from-request AGENCY [--request NAME] [--tz ZONE] [--fragment F] [--dry-run]`
fills the library from a request's production evidence instead of an ingest `source_id`: every record linked with
`kit-response --kind produced --record` on that request's produced steps (`kit/sent.json`; `--request` defaults to
`main`) that is not yet in the library, in the same one validated write (same 500-entry cap, fragment choice and size
cap). Records linked as `--correspondence` are never added. `agency_id` is the request's agency (it must be one of the
campaign's agencies) and `received_at` is the step's date at 00:00 in `--tz` (an IANA zone, default
`America/Los_Angeles`; there is no campaign-wide time zone setting), written with its offset; a record linked on two
steps takes the earlier date. The title is the ledger's original file name followed by `[EDIT ME: title]`, or
`Record <12 hex> [EDIT ME: title]` when the ledger has no name. `--source-id`, `--agency-id` and `--received-at` cannot
be combined with `--from-request`. It prints the same `{"added", "already_listed", "dry_run"}` summary and then
`nothing to add` when every record is already listed (nothing is written). A request with no linked production records,
an unknown request or agency, or an unknown `--tz` is refused and nothing is written. Nothing is fetched or published;
`public_url` stays null.

`sources verify-copy --directory D --sha256 PREFIX --file DOWNLOADED --checked-on YYYY-MM-DD
[--fragment FRAGMENT] [--differs-because redacted|converted|unknown]` hashes a copy you downloaded yourself from an
entry's `public_url` (a regular local file, no symlink, at most 200 MiB; nothing is fetched, copied or stored) and
records `public_copy: {"matches", "checked_on", "reason"?}` on every entry of that record whose `public_url` is the
same string as the one you checked. A record whose entries all share one URL needs no `--fragment`. When the record
has several different public URLs the command refuses (`record has multiple public URLs; add --fragment to choose
the copy`) before it reads the file: `--fragment` names a library fragment of that record, and the check covers
every entry of the record with that entry's exact `public_url`, whatever their fragments. URLs are compared as
stored, character for character: a different scheme case, trailing slash, query or `#fragment` is a different URL,
and nothing is normalized, decoded or fetched. `--fragment` is the library grouping name, not a URL fragment. Other
URLs of the record keep their own check untouched, so a redaction warning on one copy is never overwritten by a
match on another. A differing copy needs `--differs-because`; a matching one refuses it. `checked_on` may not be in
the future or before the `received_at` of the entries being checked. Refusals print `Stopped: <reason>`, exit 3 and
write nothing. `sources.html`, the citing finding pages and `data/sources.json` then show "Public copy checked D:
same file as this record number" or "differs (redacted copy)" per entry, with a short "How to check a copy" note.
`sources edit --public-url`/`--clear-public-url` drops the check. Checking bytes proves neither where a file came
from nor what the remote address serves today. Checks saved by earlier versions covered every URL of a record; they
are not migrated, so with several URLs re-download and recheck each one. Two synthetic examples, one per copy:

```
sources verify-copy --directory D --sha256 0123456789ab --fragment agency-original --file ./original.pdf --checked-on 2026-02-01
sources verify-copy --directory D --sha256 0123456789ab --fragment agency-redacted --file ./redacted.pdf --checked-on 2026-02-01 --differs-because redacted
```

### Pages (`content/pages/<slug>.json`, optional)

Each file becomes `/<slug>.html`: `{"title", "body_md", "description"?, "nav"?}`, nothing else. `title` is
1-120 characters and `description` at most 300 (default: the title); both are escaped. `body_md` (non-empty) is
rendered by `markdown_lite`, so raw HTML shows as text. The file stem is the slug; files not ending in `.json` are
ignored, symlinks and folders are refused, at most 50 files. Slugs that are starter pages, generated names (`404`,
`corrections`, `feed`, `sitemap`, `robots`, `style`, `site-data`, `agency-cards`, `app`, `data`, `vendor`) or a
published finding slug are refused, as is a `public-assets/<slug>.html` entry. Every page is listed in
`sitemap.xml`; `nav: true` (strictly a boolean, at most 3 pages) adds the title to the header nav after About.

### Glossary (`content/glossary.json`, optional)

Definitions an organizer has checked, written once, become `/glossary.html` and a "Terms used on this page" list on
each finding that uses them. The file is `{"schema_version": 1, "terms": [...], "notice_md"?}`; nothing else.
```json
{"schema_version": 1, "notice_md": "Plain-language notes, not legal advice.",
 "terms": [{"term": "ALPR", "aliases": ["license plate reader"], "definition_md": "A camera system that reads plates.",
            "rule_id": "ca-civ-1798.90.5-definitions", "source_url": "https://leginfo.legislature.ca.gov/..."}]}
```
- `terms`: 0 to 80 entries; an empty list (or no file) switches the feature off completely: no page, no footer link, no
  sitemap line, no list on findings, and every other output is byte-identical to a build without the file.
- `term`: 1-100 characters of plain single-line text (no `<`, `>` or control characters), trimmed. `aliases`: 1 to 3 names
  of the same kind. `definition_md`: non-blank, at most 8000 characters, rendered by the same escaped `markdown_lite` as
  pages, so raw HTML shows as text. `notice_md` (at most 1000 characters, rendered above the entries) is optional;
  `null` is refused, never read as "absent". Unknown keys, a `schema_version` other than the integer 1, wrong types and
  non-string aliases are refused rather than coerced.
- Names are compared after Unicode NFC, casefolding and collapsing whitespace. A term or alias that repeats another
  one (across entries or within one) is refused naming both names and `glossary.json`; nothing is silently merged.
- Terms are sorted by that normalized term, then spelling. Each gets the anchor `term-` plus the normalized term with
  every run outside `a-z0-9` turned into `-`, so editing a definition or reordering the file never moves an anchor. A term
  with no `a-z`/`0-9` at all, or two terms whose anchors would collide, is refused by name (no `-2` suffixes).
- `rule_id` (optional) must be an exact rule id in the campaign's shipped law package (`campaign.json`'s jurisdiction)
  and needs `source_url`, which must be one of that rule's `sources[].url`; no package, no rule or another URL refuses
  the term. The page then shows the rule's citation and the package's own status ("draft, not legally reviewed" unless
  the package says reviewed). Definitions are not gated on a finding's event date and never promote a review state.
  `source_url` alone is only checked as an https URL: the organizer attests that it is the official source, and the build
  never fetches it or judges its host.
- `Terms used on this page` on a finding lists, in glossary order, each term whose name or alias appears in the
  finding's `title`, `summary` or `body_md`. Each field is searched on its own (a phrase never spans two fields), after the
  same normalization, as a literal (no patterns, stems or plurals) with a non-word character or the text edge on both
  sides; letters, digits and `_` of any script are word characters. Markdown is searched as written, so link
  destinations and code spans count. Definitions, sources, the footer and other findings never count. Nothing in the
  prose is rewritten or auto-linked, and the finding hash, packet and open data are unchanged.
- `glossary.html` joins the footer (next to Privacy) and, with `base_url`, `sitemap.xml`. `glossary` is a reserved slug
  with or without the file: a `pages/glossary.json`, a finding slugged `glossary` and a `public-assets/glossary.html/...`
  file are refused. The input JSON is never copied into `public/`.
- `content-check` runs the build's own reader and lists every independent problem with `glossary.json` in its path;
  a symlink (even dangling), folder or other non-regular `glossary.json`, an oversize file (2 MiB) or invalid UTF-8
  or JSON is refused, not skipped. A refused glossary leaves the existing `public/` untouched; removing the file and
  rebuilding removes the page and all its links.
- Labels: `footer.glossary`, `title.glossary`, `title.glossary_description`, `heading.glossary`, `finding.terms` and
  `glossary.also`, `glossary.citation`, `glossary.source`, `glossary.status` (see Labels).

### FAQ (`content/faq.json`, optional)

Questions an organizer has checked, in the order written, become `/faq.html`. The file is
`{"schema_version": 1, "entries": [...]}`; nothing else.
```json
{"schema_version": 1, "entries": [{"question": "What is a retention period?",
  "answer_md": "How long a record is kept. See the finding below.",
  "finding_ids": ["fnd_0000000000000001"], "rule_id": "ca-civ-1798.90.5-definitions"}]}
```
- `entries`: 1 to 40 entries (an empty list is refused; to switch the page off, remove the file). Each entry has
  `question` and `answer_md` and may have `finding_ids` and `rule_id`; unknown keys, `null`, a boolean, a
  `schema_version` other than the integer 1 and wrong types are refused rather than coerced.
- `question`: 1-160 characters of trimmed plain single-line text (no `<`, `>`, line break, tab or other control or
  invisible format character). Questions are compared after Unicode NFC, casefolding and collapsing whitespace; a
  second one that repeats an earlier one is refused as `faq.json.entries[N].question: duplicates entries[M].question`
  (zero-based positions).
- `answer_md`: non-blank, at most 1500 characters, rendered by `markdown_lite` (raw HTML shows as text, images are not
  resolved; `<` and `>` are refused). Each question is an `h2` with the anchor `#q-1`, `#q-2`, ... in file order, so
  adding a question at the end never moves an earlier anchor.
- `finding_ids` (optional, 0 to 3, no repeats): exact ids of findings that are published in this campaign's
  `content/findings/`, not slugs. The page links each one under the answer, in the order given, as
  `/findings/<slug>.html`. An id that is not a published finding is refused as
  `faq.json.entries[N].finding_ids[J]: not a published finding id`, a repeated one as `... duplicate finding id`; a
  finding that is later withdrawn blocks the rebuild until the entry is edited.
- `rule_id` (optional): an exact rule id (at most 120 characters, no prefix matching) of the campaign's shipped law
  package (`campaign.json`'s jurisdiction). It needs a usable package even when no finding cites a rule
  (`... rule_id: no usable campaign law package`; an unknown id is `... rule_id: unknown law-package rule id`).
  The page shows the package's citation, the rule id and the package's own status ("draft, not legally reviewed"
  unless the package says reviewed). It is not an event-date check, a computed deadline or a claim that the FAQ was
  legally reviewed. The package is read only when an entry has a `rule_id`.
- Glossary terms are linked as on finding pages: when `content/glossary.json` has terms, "Terms used in this answer"
  lists those whose name or alias is in the question or in the answer, each searched on its own.
- The legal-claim guard is advisory: `build` and `content-check` print `review-required: faq.json.entries[N].answer_md:
  legal-claim wording (...)` for every answer that asserts a legal violation (to stderr in `content-check`, which never
  changes its JSON stdout, count or exit code). Notes name the place and the rule, never the text; a rule citation
  does not silence them, and a note never fails a build.
- `faq.html` joins the footer (last paragraph) and, with `base_url`, `sitemap.xml`. `faq` is a reserved page and finding
  slug with or without the file, and `faq.html` cannot be a `public-assets` name. Without the file there is no page,
  no link, and every other output is byte-identical; removing the file and rebuilding removes the page and its links.
  A refused FAQ leaves the existing `public/` untouched, and the input JSON is never copied into `public/`.
- `content-check` runs the build's own reader and lists every independent problem with `faq.json` in its path; a
  symlink (even dangling), folder or other non-regular `faq.json`, an oversize file (2 MiB), invalid UTF-8 or JSON
  or an unreadable file is refused, not skipped.
- Labels: `faq.title`, `footer.faq`, `faq.findings`, `faq.terms`, `faq.rule` and `faq.law_status` (see Labels). The
  Spanish values in `examples/labels/es.json` are a machine-made draft like the rest of that file.

### In the news (`content/coverage.json`, optional)

Press coverage an organizer has read and typed in becomes `/news.html`. The tool stores only the outlet, the headline
as published, the article link, its date, a kind and the findings the story reported; it never fetches an article,
quotes it, summarizes it, shows its byline or image, or embeds it. **Listing a story is not verification of it and does
not mean the outlet endorses the campaign.** Typing the headline exactly as published is the organizer's responsibility.
```json
{"schema_version": 1, "entries": [{"outlet": "Example News", "headline": "Published headline",
  "url": "https://news.example.test/story", "published_on": "2026-11-01", "findings": ["F1"], "kind": "news"}]}
```
- Record and remove entries with the CLI; both need an existing `content/` with a valid `site.json`, never scaffold,
  build, publish or use the network, and print `Nothing was built, published or sent.`:
  `python3 -m campaign_tool content coverage add --directory D --outlet N --headline H --url U --published-on YYYY-MM-DD
  [--finding ID ...] [--kind news|opinion|broadcast|other]` (`--finding` once per id) and
  `python3 -m campaign_tool content coverage remove --directory D --url U`. Success prints
  `{"action": "added"|"removed", "url": U}`; a refusal exits 2 and changes nothing. You can also edit the file by hand.
- Envelope: exactly `schema_version` (the integer 1) and `entries` (a list of at most 1000). Each entry needs `outlet`,
  `headline`, `url` and `published_on` and may have `findings` (default `[]`) and `kind` (default `news`); unknown keys,
  `null`, booleans and wrong types are refused, not coerced. A file written by the CLI always shows the defaults.
- `outlet` 1-80 and `headline` 1-200 characters of non-blank text with no `<`, `>`, control character or line break.
  They are kept exactly as written (no trimming, rewriting or Markdown) and shown escaped.
- `url`: an `https://` link by the same rule as other source links (a plain host, no credentials). **Duplicates are
  decided by the exact text**: `https://x.test/a` and `https://x.test/a?utm=1` are two different entries; there is no
  canonicalization, tracking-parameter stripping or redirect following. A repeat is refused as
  `coverage.json.entries[N].url: duplicate URL`. There is no separate id: the URL identifies the entry.
- `published_on`: a real calendar day written `YYYY-MM-DD`, not after today (`... published_on: must not be after
  today`). It may be before or the same day as a finding's publication; no chronology is checked.
- `kind`: exactly `news`, `opinion`, `broadcast` or `other`.
- `findings`: 0 to 3 distinct exact ids of findings published in `content/findings/` (not slugs or prefixes; letters,
  digits, `_` and `-`). A repeated, unknown, draft or withdrawn id is refused as
  `coverage.json.entries[N].findings: must name at most 3 unique published finding IDs`. Drafts are never read.
- Absent, empty and filled: without the file there is no page and no link, and every other output is byte-identical.
  With the file, even `{"schema_version": 1, "entries": []}`, `news.html` exists (an empty notice when there are no
  entries). Removing the last entry keeps the empty file; delete the file and rebuild to remove the page and every link
  to it. A refused file leaves `public/` untouched; the input JSON is never copied into `public/`.
- The page lists entries newest first (equal dates by URL), each with the outlet, the headline linking the article
  (`rel="noopener noreferrer"`), the date, the kind and links to the findings it names. Each named finding page gets one
  `Reported in: OUTLET (DATE)` paragraph per entry, outside its facts, citation and corrections. `press.html`, when it
  exists, gets a separate paragraph linking `news.html`. `news.html` is linked once in the footer of every page and in
  `sitemap.xml` (with `base_url`); there is no top-nav item, feed entry, search entry, JSON export or hash change.
- Repair: if a cited finding is later withdrawn, the build and `content-check` name the entry
  (`coverage.json.entries[N].findings: ...`). `coverage remove --url U` still removes that entry, but refuses while
  another stale reference remains, so fix or remove those too; nothing is dropped silently.
- `news` is a reserved page slug and `news.html` a generated top-level name with or without the file, so a
  `content/pages/news.json`, a redirect or a `public-assets` file at that path is refused. `content-check` runs the
  build's own reader and names problems as `coverage.json...`; a symlink (even dangling), folder or other non-regular
  file, an oversize file (2 MiB), invalid UTF-8 or JSON is refused, not skipped.
- Labels: `news.title`, `news.description`, `news.empty`, `news.findings`, `news.kind.news`, `news.kind.opinion`,
  `news.kind.broadcast`, `news.kind.other`, `finding.reported_in` (`{outlet}` and `{date}`), `footer.news` and
  `press.news`. The Spanish values in `examples/labels/es.json` are a machine-made draft like the rest of that file.
  Default examples and `content init` do not enable coverage.

### Newsletter archive (`content/newsletters/<YYYY-MM-DD>.json`, optional)

Written by `newsletter archive --date D --publish` (see NEWSLETTER.md); exactly `{"schema_version": 1, "sent_on", "title",
"body_md"}`. The file name is the sent day and must match `sent_on`; `title` is 1-200 characters, `body_md` non-blank and at
most 256 KiB, the file at most 512 KiB, at most 1000 issues. Raw HTML, non-https links, email addresses, phone numbers,
64-hex hash runs, control characters (other than newline and tab), duplicate JSON keys, unsubscribe wording and template
markers are refused naming the file. `build` and `content-check` use the same validator, and a refusal leaves the previous
`public/` untouched. With at least one issue the build writes `newsletters.html` (newest first) and
`newsletters/<D>.html` (the Markdown through `markdown_lite`, a backlink, no signup or consent text), adds a footer link and
the sitemap entries; with none, output is unchanged. `newsletters` is a reserved page and finding slug, and
`newsletters.html`/`newsletters/` cannot be `public-assets` names. The loader never reads `kit/`.

### Topic pages (`content/topics/<slug>.json`, optional)

An organizer groups an ordered series of already-published findings into a stable page. Each file becomes
`/topics/<slug>.html` and holds exactly `{"title", "intro_md", "finding_ids"}`: `title` is 1-120 characters,
`intro_md` a string of at most 200 KiB (empty allowed; rendered by `markdown_lite`, so raw HTML shows as text) and
`finding_ids` 1-100 distinct ids (`fnd_...`, not slugs or file names) of findings loaded from `content/findings/`.
An unknown, draft or withdrawn id is refused by file, field and id; remove it from the topic first, because a refused
build leaves the previous `public/` tree untouched. The listing keeps the order of `finding_ids` and reuses the
findings list, so each entry shows its classification, confidence, event date and newest correction marker. Each
listed finding page gets `Part of: <topic links>` (topics in slug order, once each). `/topics/` lists every topic;
a Topics nav item, a footer link and sitemap entries for `/topics/` and each topic appear only when topics exist. No
`lastmod` is invented for a topic and topics are not in the Atom feeds.

The file stem is the slug (`index`, `topics`, `timeline`, `cameras`, every reserved page name, an explainer page slug
or a published finding slug is refused, naming both). At most 30 files; files not ending in `.json` are ignored;
symlinks, folders named `*.json`, a symlinked or non-folder `topics` path, malformed UTF-8/JSON and files over
2 MiB are refused. While topics exist a page or finding slug `topics` and `public-assets/topics/...` are also
refused; without topics nothing changes and the output is byte-identical. `content-check` lists every bad topic file.
New labels: `nav.topics`, `heading.topics`, `title.topics`, `title.topics_description`, `finding.part_of`
(`{topic}` is the already-escaped link list); `examples/labels/es.json` carries each.

### Supporting organizations (`content/supporters.json`, optional)

An organizer lists an organization only after recording its permission privately:

```
supporters add --directory DIR --name N --kind civil_liberties|community|faith|labor|neighborhood|business|other \
  [--url https://...] --organization --confirmed-on YYYY-MM-DD --how email|letter|meeting|form [--correspondence PREFIX]
supporters remove --directory DIR --id ID
```

`--organization` is required and is an explicit attestation, never inferred from the name or kind: only organizations
are listed, never individuals. `add` writes the public row to `content/supporters.json` and the consent record to
`kit/supporters-consent.json` (the `kit/` folder is created, or tightened, to 0700; the file is 0600) together; `remove` drops both. The command prints only
`{"id", "action"}`: no consent record, hash, evidence text or private path. `--correspondence` is optional: 12 to 64
lowercase hex characters of one ingested record, stored privately as its full digest; it is never published.

The public file is exactly `{"schema_version": 1, "supporters": [{"id", "name", "kind", "url", "joined_on",
"organization": true}]}` (all keys required, `url` null or https, at most 200 rows and 1 MiB, unknown or duplicate keys
refused). The id is the lowercase name with other characters turned into single hyphens and must start with a letter and
fit in 64 characters; a duplicate id or name (Unicode-folded) is refused. Names are never cleaned silently: leading or
trailing spaces, control characters, angle brackets, a duplicate or a failed lint is a refusal. `joined_on` is the
confirmation date (a real date from 2000-01-01 up to today).

The private record is `{"organization": true, "confirmed_on", "how", "public_sha256", "correspondence"}` and
`public_sha256` is the SHA-256 of the exact public row, so editing a listed row (name, kind, URL or date) needs a new
confirmation: a matching id alone is not permission. `build` refuses with a `ContentError` (before any public write)
for a public row with no record, a hash or date mismatch, a future date, a malformed store or a private store that
group/other can read; `content-check` names every such id (sorted, no values) as `content/supporters.json: <id>: ...`.
Read-only commands never create, change or fix either store.

`supporters.html` appears only when at least one organization is listed. It groups organizations by kind (civil
liberties, community, faith, labor, neighborhood, business, other), alphabetically by name, each with its
`<time>` joined date and an optional external link. A footer link and a `sitemap.xml` entry appear with it; with no file,
or an empty list, the site is byte-identical to before. The page says only that these are supporting organizations; it
is not a legal endorsement and does not let anyone speak for them. `supporters` is a reserved page and finding slug even
without a file. Labels: `supporters.title`, `supporters.description`, `supporters.joined` (`{date}`) and
`supporters.kind.<kind>`; `examples/labels/es.json` carries Spanish drafts (still unreviewed).

**Co-sign grant (DF2207-14).** A private record may also carry `cosign` (`"any"` or 1 to 100 meeting ids) with
`cosign_confirmed_on`, `cosign_how` and `cosign_correspondence`: all four or none. Set it with
`supporters consent --directory DIR --id ID --cosign any|MEETING_ID ... --confirmed-on D --how HOW [--correspondence PREFIX]`
or remove it with `--clear-cosign` (see [MEETINGS.md](MEETINGS.md)). It is read only by `meetings-stage --cosign`,
`load_content` and `content-check`; it never reaches the public file or the site. A listing alone never allows co-signing.
Staged meetings store `cosigners` (1 to 30 supporter ids), shown on the meeting page as `Co-signed by N organizations`
(labels `meeting.cosigned_one` and `meeting.cosigned_many`, both with `{count}`; Spanish drafts in `examples/labels/es.json`).

Limits: the lint refuses `@`, phone-number-like digit runs and contact labels (`email:`, `phone:`, `contact:`, `mailto:`)
in names and URLs, but it does not recognise a person's name. The organization attestation and the organizer's own
review before every `add` remain mandatory; no program can prove that free text names an organization rather than a
person. The two files are replaced one after the other with a rollback on an ordinary write failure; an interrupted
`add` can leave an orphan private record (never a public row without consent), which blocks further `add`/`remove`
until the saved pair of files is restored. There is no crash-atomic transaction and no concurrent writer support; run
one writer. Evidence originals are never deleted.

## Regular meeting schedules (`content/body-schedules.json`, optional)

A source-backed rule for when a body usually meets, for bodies with or without a Legistar feed. Organizers write it by
hand from the body's own calendar page and re-check it; the tool never fetches it. Exactly:

```json
{"schema_version": 1, "schedules": [{"body": "Synthetic Council", "weekday": "tuesday", "ordinals": [1, 3, "last"],
  "local_start_time": "18:30", "time_zone": "America/Los_Angeles", "source_url": "https://example.invalid/calendar",
  "checked_on": "2026-11-01"}]}
```

- 0 to 100 rows, every key required, no others. `body` is written exactly as the staged meetings name it (1 to 200
  plain-text characters, exact case and spacing, no leading or trailing space, newline or tab; never matched by agency,
  fuzzy name or case) and may appear once. A body need not have a staged meeting yet.
- `weekday`: `monday` to `sunday`. `ordinals`: unique, from `1`, `2`, `3`, `4` or `"last"` (never 5); `last` is the final
  such weekday of the month and may coincide with `4`. `local_start_time`: `HH:MM`, 00:00 to 23:59.
- `time_zone` is the IANA zone the body's meetings use (the organizer supplies it; staged meetings store only a UTC
  offset). It decides which local calendar day a staged meeting is on, across daylight saving. Only dates are
  calculated: no time of day is ever turned into a scheduled timestamp.
- `source_url`: https, at most 500 characters. `checked_on`: a real `YYYY-MM-DD` date, not in the future. An unknown
  weekday, an ordinal outside 1-4 or `last`, a duplicate body, an unknown zone or any other malformed row is refused by
  `build` and named by `content-check` (field by field); invalid rows are never skipped. A symlink is refused.
- A schedule checked more than 365 days ago (exactly 365 is fresh) is stale: `meetings.html` adds `Stale schedule:
  re-check the source` and `today` lists a `stale_body_schedule` item.
- `meetings.html` shows, once per body and sorted by body, `Usually meets: first and third Tuesday at 18:30
  (America/Los_Angeles) (source, checked DATE)`. Labels: `schedule.usually`, `schedule.stale`, `schedule.rule`,
  `schedule.ordinal_1` to `_4` and `_last`, `schedule.weekday_<day>`, `schedule.join_two`, `schedule.join_many`
  (Spanish drafts in `examples/labels/es.json`, unreviewed). There is no new public JSON, calendar feed, sitemap entry
  or meeting model; with no file every page is byte-identical.
- `today` lists, for each body, an `expected_regular_meeting` item for each of the next two regular dates (today
  included) on which no meeting of exactly that body is staged: `Expected BODY meeting D: watch for the agenda`. A staged
  meeting of any status, including a cancelled one, answers the question for its local date. This is a prompt to check
  the official agenda, never a claim that a meeting is scheduled, late or overdue. Holidays, special meetings and
  skipped sessions stay unknown until a verified meeting is staged. Nothing is said about agenda posting deadlines.

## Generated pages and files

`index.html` (hero map when configured, agency cards, mission, latest findings,
signup, meetings), `agencies.html`, `requests.html` (every public asked item across agencies: see "What we have asked for"), `findings/index.html`,
`findings/<slug>.html`, `sources.html` (with `#library` and one
`#<fragment>` section per source group), `sources/<12-hex record number>.html` (one per library entry), `meetings.html`, `about.html`,
`style.css`, `_headers`, `robots.txt`, `sitemap.xml` (only when `base_url` is
set: absolute `<loc>`s, finding pages and `/`, `/findings/` with the UTC day of
the latest update or correction as `<lastmod>`; without `base_url` no sitemap,
no footer link, and `build` notes the skip on stderr), `feed.xml` (Atom of
findings), `findings/agency/<slug>.xml` (the same Atom entries for one agency,
only for an agency with at least one published finding; linked as "Findings
feed" from its `agencies.html` entry), `agencies/<slug>.html` (one page per agency with at least one
published finding or `sources.json` entry: its card facts and official links, its findings, its source records
with 12-character hash prefixes and `sources.html#<fragment>` links, that body's upcoming meetings by `agency_id`,
its findings feed when it has findings; linked as "Agency page" from `agencies.html` and from each of its finding
pages, listed in the sitemap; other agencies get no page and no link; a `public-assets/agencies/` file is refused),
`cameras.html` (only with a map: the no-JS list of mapped points, one section
per picker city by name, each point in the first city boundary holding it by the counts' half-open rule, then the
`group_label` section; manufacturer/operator or "not tagged", an OSM node link or "no OSM link"; over 2 000 points each
section lists 500 rows, then "and N more on the map"; linked from the map fallback and footer, in `sitemap.xml`),
`glossary.html` (only with a non-empty `content/glossary.json`: see Glossary),
`faq.html` (only with a `content/faq.json`: see FAQ),
`news.html` (only with a `content/coverage.json`: see In the news),
`site-data.js` (`CITIES`, `COUNTY_BOUNDS`, `MAP_STYLE`,
`CAMERA_DATA_URL`, `BOUNDARY_DATA_URL`), `agency-cards.js`
(`agency_cards.render` output), `app.js` (map controller plus page bootstrap, or
a picker-only bootstrap without a map), `data/*.geojson` and `vendor/*` when
present. `data/findings.json` is the published findings as open data
(`schemas/findings-open-data.schema.json`), linked from `findings/index.html` and
`about.html`: per finding `id`, `slug`, `title`, `summary`, `body_md`,
`classification`, `confidence`, `event_date`, `published_at`, `updated_at`,
`agency_id`, `url` (absolute only when `base_url` is set; top-level `url_kind`
says which), `also_agency_ids` (only on a finding that has it), `limitations`, `counterevidence`, `corrections` (`date`, `note`) and
`sources` (`sha256_prefix`, the 12 hex characters shown on the page, `title`,
`locator`, `rule_id`). It never includes `author`, `state`, `rule_version`,
`duty`, `exceptions`, `rule_ids` or full hashes; the build refuses and writes
nothing if any other key or a 40+ hex string would appear. `data/sources.json`
is the source library as open data (`schemas/sources-open-data.schema.json`),
linked from `sources.html` and `about.html`, in `content/sources.json` order (an
empty `sources` list without that file): per entry `sha256_prefix` (12 hex
characters), `title`, `agency_id`, `agency_name` (null when the agency is not
configured), `received_at`, `public_url` or null, `pages` or null and
`cited_by` (ids of the published findings citing it, in site order; drafts
never count). It never includes `note`, `fragment` or the full hash; the build
refuses with `data/sources.json: ...` and writes nothing on any other key or a
40+ hex string. The starter build (no `content/`) writes no `data/sources.json`.
`data/findings.csv` and `data/sources.csv` (DF1207-14) are spreadsheet copies of those two files, linked beside each JSON link
on `findings/index.html`, `sources.html` and `about.html` (label `intro.open_data_csv`, default "Spreadsheet (CSV)"; not in
`sitemap.xml`, like the JSON). Each is built from the already-checked JSON document, so a column the JSON does not carry cannot
exist: one header row, then one row per record in the JSON's order, columns in the order of the JSON's keys; UTF-8 without a
byte-order mark, CRLF line ends, standard quoting. Flattening: `null` is an empty cell, `true`/`false` stay `true`/`false`, a
list of strings is joined with `; `, `sources` (findings) is the `; `-joined `sha256_prefix` values, `corrections` the
`; `-joined `date: note`, `cited_by` the `; `-joined finding ids, and `public_copy` (sources) becomes the three columns
`public_copy_matches`, `public_copy_checked_on` and `public_copy_reason`. Optional `also_agency_ids` and `agency_response`
are empty cells when omitted by the public JSON (including a pending response); present `also_agency_ids` are joined with
`; `, and a present `agency_response` is a JSON object serialized with sorted keys in one cell, using only its public fields.
A cell that begins with `=`, `+`, `-`, `@`, a tab or a
carriage return is written with a leading `'` so a spreadsheet does not run it as a formula (the `kit-status --csv` rule; the JSON
is not changed).
`data/meetings.json` is the staged meetings as open data (`schemas/meetings-open-data.schema.json`), linked from
`meetings.html` and `about.html`, newest `starts_at` first, cancelled ones included (`"meetings": []` with none):
top level `schema_version`, `generated_at` (the build date at 00:00 UTC) and `meetings`; per meeting `id`, `body`,
`agency_id` or null, `starts_at`, `status`, `agenda_url` or null, `url` (the page path `/meetings/<id>.html`) and, for a
held non-cancelled meeting with a recorded outcome, `outcome` (`result`, `on`, `source_url`, `vote` and the per-member
`votes` as `{name, vote}` in stored order, each only when recorded), plus the pair `minutes_url` and
`minutes_approved_on` (the body's approved official minutes, both or neither; no derived confirmation flag), plus
the optional `continued_to` (a staged meeting id) or `continued_to_date` (a stated date) of a `continued` result (one
of the two, never both; no derived back-reference), plus the optional trio `direction`, `return_by` and
`direction_source` (a staff direction as the official source states it; all or none; no reminder or fulfillment flag), plus the
optional `spoke` and `written` whole numbers (0 to 999) with their https `comment_source_url` (the public-comment totals the
official record states; at least one count with the source; no for/against split, name or quote). It never
includes the comment kit, `participation`,
contacts, `location`, `agenda_item`, `verified_by`, `verified_at` or any other key; the build refuses with
`data/meetings.json: non-public key ...` and writes nothing.
`data/agencies.json` is the agencies as open data (`schemas/agencies-open-data.schema.json`), linked from
`agencies.html` and `about.html`: only agencies that have a public page (`/agencies/<slug>.html`), sorted by `name` then
`agency_id` (`"agencies": []` with none). Top level `schema_version`, `campaign`, `generated_from`, `base_url`, `url_kind`
and `agencies`; per agency `agency_id`, `name`, `kind`, `url` (the page path, with the base URL when one is set), `asked`
(`topic`, `state`, `asked_on`, `updated_on` or null, newest `asked_on` first as the card lists them), `alpr_policy`
(`url`, `checked_on`) or null, `contract` (`vendor`, `start`, `end`, `source_url`, `checked_on` and, only when the contract
has one, `cost`: `amount_usd`, `period`, `as_stated_in`) or null, `transparency`
(the newest snapshot only: the portal numbers the page shows, `source_url` and `checked_on`) or null, an optional
`funding` array (only when recorded: `kind`, `name`, `as_stated_in`, `source_url`, `checked_on` and, when given,
`amount_usd` with `period`, and `fiscal_year`), an optional `actions` array (only when recorded, DF0237-01: `on`, `kind`,
`as_stated`, `source_url`, `checked_on`, newest first; see "Agency actions outside a meeting"), and `camera_count`
(the card's camera-count sentence) or null. It never includes `records_email`, `records_url`, `portal`, the asked-item
`note`, the contract `renewal_note`, kit files, notes or any other key; the build refuses with
`data/agencies.json: non-public key ...` and writes nothing.
`data/agencies.csv` and `data/meetings.csv` (DF2207-11) are spreadsheet copies of those two files, linked beside every link
to the matching JSON (`agencies.html`, `meetings.html` and `about.html`; label `intro.open_data_csv`, not in `sitemap.xml`).
Each is read back from the JSON text the build has already rendered and checked, so columns contain only allowlisted public
JSON fields, and there is no CSV without its JSON. The header includes required and optional public record keys in the order
the JSON file writes them (alphabetical), for no records too. In particular, agencies always have a `funding` column, even
when no agency has recorded funding (`actions` is JSON-only: no column, so the header never changes). A key a record lacks (including `funding` or a meeting's `outcome`) or a null is an empty
cell, never a missing column;
then one row per record in the JSON array's order. Text stays as it is, a number keeps its JSON spelling, `true`/`false` stay
`true`/`false`, a list is joined with `; ` in stored order (a string item as text, any other item as compact JSON), and an
object (`asked` or `funding` items, `alpr_policy`, `contract`, `transparency`, a meeting's `outcome` with its `votes`) is one cell of compact
JSON (`{"checked_on":"2026-10-20","url":"..."}`), never spread over more columns. Encoding and the formula guard are those of
`findings.csv`: UTF-8 without a byte-order mark, CRLF line ends, standard quoting, and a cell that begins with `=`, `+`, `-`, `@`,
a tab or a carriage return is written with a leading `'` (interior characters are not touched; the JSON is not changed).
`public/` is
regenerated from scratch on every content build; do not
hand-edit it. `build --check` runs the `tools/check_public_tree.py` pattern scan
over the output and fails on any hit, and on any site-local `href`/`src` that
names no written `public/` file or `#fragment` with no matching `id` on its
target page (`public/<page>: broken link <href>`; external links are never
fetched). It also runs an accessibility pattern lint (`public/<page>: <code> <detail>`): `img-alt` (no `alt`;
`alt=""` is fine), `heading-skip` (e.g. `h2→h4`), `link-text` (empty link with no `aria-label`/`title`),
`duplicate-id` and `html-lang`. It is a pattern lint, not an accessibility audit. The command prints a manifest of every
written file with its size. `build --budget KB` then prints each page's first-party
weight (HTML plus site-local stylesheets, preloads, scripts and `img`/`source` files it
references, each once; on-disk bytes, uncompressed) and exits 3 naming every page over
`KB*1024` bytes with its three largest assets. Files a script fetches at run time (map
GeoJSON) are not counted; external hosts are never fetched; a failed `--check` wins.
To see growth over time rather than only the ceiling, `--budget` also takes `--weights-out FILE`, which saves
the per-page table as a private `page-weights-v1` JSON record (no timestamp, so equal builds give byte-identical files;
a new file only, mode 0600, never inside `public/` or `content/`), and `--weights-baseline FILE`, which reads such a record
and prints each page as `<page> <bytes> bytes (+N|-N|±0 since baseline)`, `(new page)`, or
`removed since baseline: <page>`. The baseline never changes the exit code (3 only when a page is over budget); an
unreadable, symlinked, over 2 MiB or wrong-schema baseline stops the build with exit 1 before the table. Both
options need `--budget`. These are uncompressed first-party bytes of the built files, not a field measurement.
Meetings on or after the build date (the typed
`starts_at` date, no time-zone conversion) are upcoming, soonest first (home: next
three); `meetings.html` then lists `#past-meetings`, most recent first.
Every meeting (upcoming, past or cancelled) also gets `meetings/<id>.html`: when,
where, agenda item and link, and its comment kit shown open for linking and
printing; a cancelled meeting is headed `Cancelled:` with a notice and keeps its
kit. Home and `meetings.html` link it as "Meeting page", and `sitemap.xml` lists
it. `content/public-assets/meetings/` is refused as a generated path, and two
meeting files resolving to the same id (explicit `id` or filename) are refused
before `public/` changes.

Pages contain no inline scripts, iframes or images other than those under
`public-assets/`; all script tags are `defer` references to the self-hosted
files above.

Not-found page: `404.html` is always generated (site shell, links to findings, agencies, meetings and home, a note
that corrections and withdrawals are on `corrections.html`). It is `noindex`, has no canonical or share tags and
is never in `sitemap.xml` or the feeds; the name is reserved, so a `public-assets/404.html` is refused.

How to read our findings: `methods.html` is always generated and is not editable. It defines every classification
and confidence label (`#classification-<code>`, `#confidence-<code>`), explains record numbers and the factual,
legal and privacy review by two people other than the author. The definitions live in `campaign_tool/site.py` next to
the label tables; each badge on finding pages and the findings index links to its definition. It is in the footer and
`sitemap.xml`; the slug `methods` and `public-assets/methods.html` are refused. `feed.xml` and `data/findings.json` keep plain labels.

### Search page
`search.html` and `search.js` are always generated and are not editable (DF1207-15). The page has a search box, a result
list and a count line; `search.js` fetches the already-published `/data/findings.json` and `/data/sources.json` once
(same origin; the CSP is unchanged) and matches in the browser: findings by title, summary, agency name and cited source
titles, sources by title and agency name. Matching ignores case and accents (`José` finds `jose`); every word typed must
appear. A query is 2 to 100 characters after trimming, otherwise the page shows the hint. At most 50 results, findings
first, each linked to the finding page or to the agency's section of `sources.html`. The query lives in the URL hash
(`/search.html#q=alpr%20contract`), is read on load and on `hashchange`, and typing updates it with `history.replaceState`;
a malformed hash is ignored. Results are written with `createElement` and `textContent` only (never `innerHTML`). Nothing
is searched on a server and nothing is logged. Without JavaScript, or when either JSON file is missing or malformed, the
page shows a paragraph linking `/findings/` and `/sources.html` (with the second case also showing the unavailable
message). The "Search" link is in the footer beside the feed link and `/search.html` is in `sitemap.xml` when `base_url`
is set; the slug `search` and `public-assets/search.html` are refused. Finding bodies and private records are not searched.

### Privacy page
`privacy.html` is always generated and is not editable (DF1207-16). It states, in plain words, that scripts, stylesheets
and fonts come from this site only, that the site sets no cookies and runs no analytics, which hosts the interactive map
contacts (the style and tile hosts of `map/map-config.json`, sorted; none without a map), where a newsletter signup goes
(only with `signup.mode` `brevo_hosted`: the provider host from `form_html_allowlisted_url`, plus `signup.note`) and the
campaign's privacy contact. Every sentence is a `privacy.*` label (`footer.privacy` for the footer link) chosen from
`Site.privacy_facts()`, which reads the same settings as the Content-Security-Policy; `content/labels.json` can reword the
sentences but never changes which facts are stated. It is in the footer of every page and, with a `base_url`, in `sitemap.xml`;
the slug `privacy` and `public-assets/privacy.html` are refused.

Two optional keys feed it. `site.json.privacy_contact` is an email address (validated like `contact_email`); without it the page
uses `contact_email`, and with neither it says "This campaign has not published a privacy contact." and never invents one.
`site.json.signup.privacy_url` is an https link to the provider's policy on the same allowlisted host suffix as the signup
form; it is linked only with a hosted signup, and without it no provider-policy link is shown (no URL is guessed). A bad value
is refused by field name.

The build refuses to write anything when a claim contradicts the final CSP or file list, with
`privacy.html: "<claim>" contradicts <directive or file>`: `script-src`, `style-src` or `font-src` other than exactly `'self'`;
a built script (a `.js`/`.mjs` file or a `<script src>`) whose name holds `gtag`, `analytics`, `plausible`, `matomo` or `umami`;
a `Set-Cookie` header in `_headers`; map hosts that differ from the https hosts in `img-src`/`connect-src`; a `form-action`
other than the named signup host (or `'none'`). The page is not legal advice and not a reviewed privacy policy (it says so);
the independent privacy review in ROADMAP item 15 stays open. The CSP itself is unchanged.

Photos: a phone or camera photo usually carries metadata, often including the GPS position where it was taken
(an organizer's home, say). `build` and `content-check` therefore refuse a `.jpg`, `.jpeg`, `.png` or `.webp` in
`content/public-assets/` that carries EXIF (`EXIF GPS` when it has a GPS block), XMP, IPTC, a JPEG comment, PNG text
chunks or bytes after the image end, naming the file and the kinds but never a value
(`public-assets/photos/council.jpg: image metadata (EXIF GPS); run content strip-metadata`). A file the checker cannot
parse is refused as `unreadable image`; re-export it. To clean one, run
`python3 -m campaign_tool content strip-metadata content/public-assets/photos/council.jpg` (optional `--out PATH`): it writes
`council.clean.jpg` beside it with those segments or chunks removed and every image byte kept (no re-encoding; colour
profiles stay), prints `{"removed": [...], "written": ...}`, and refuses a symlink, an existing target or a target
inside a `public/` folder. Check the copy, then replace the original yourself. ICO files are not checked.

PDFs and SVGs: `build` and `content-check` also refuse, naming file and kinds but never a script, URL or name
(`public-assets/docs/agenda.pdf: PDF (JavaScript, more than one revision)`), a PDF with JavaScript, open/launch
actions (`OpenAction`, `Launch`, `AA`), embedded files, an `Author` field or XMP creator, or more than one revision
(an incremental update keeps the earlier text, so a redaction saved that way can still be read), names in compressed
streams included (every `stream` keyword is swept, whatever `/Length` says, and read again with any PNG/TIFF
`/DecodeParms` predictor undone); and an SVG with a `DOCTYPE`/entity, `script`, `foreignObject`, `on*` handlers or a link, `url()` or
`@import` pointing anywhere but `#` in the file (`data:` too). Unparseable, or a PDF stream encoded other than plain
Flate or an image codec (`LZWDecode`, `ASCII85Decode`, filter chains, an indirect `/DecodeParms`, another
predictor, or a predictor followed by a string or comment before its stream): `unreadable PDF`/`SVG`. Fix: save the PDF
flattened as a new single-revision file without author fields; export the SVG as plain shapes, or use a PNG.

Images in text: in a finding, page, about, mission or comment-kit text, a line that is only `![Council chamber, 3 June](/photos/council.jpg)` shows `content/public-assets/photos/council.jpg` (PNG, JPEG or WebP) with that alt text (required, 1-200 characters), its own width and height and lazy loading; anything else (inline, in a list, another site, a missing file or SVG, empty alt) stays text, and `markdown_lite --lint` names it as `image`. The newsletter shows no images.

Printing: `style.css` ends with one `@media print` block. Nav, signup, map and picker chrome are hidden; text is black on white;
each external `http(s)` link prints its address after the text (relative, `#` and `mailto:` links do not). Closed comment
kits print open only in browsers that support `::details-content`; elsewhere they print closed (open them before printing).

## Labels (`content/labels.json`, optional)
Organizer-reviewed wording for the fixed UI strings of the HTML pages; the tool translates nothing. Without the file
every page is byte-identical to the English defaults in `campaign_tool/labels.py` (`DEFAULTS`, one entry per key).
```json
{"schema_version": 1, "labels": {"nav.home": "Inicio", "heading.agencies": "Organismos públicos en {county}."}}
```
Key families: `nav.*`, `header.*`, `footer.*`, `title.*` (page titles and descriptions), `eyebrow.*`, `heading.*`,
`intro.*`, `notice.*` (empty states and fixed notices), `finding.*`, `meeting.*`, `sources.*`, `agency.*`, `map.*`,
`signup.*`, `glossary.*`, `schedule.*`, `js.*` (scripts, below), `handout.*` (the private meeting-pack handout, below), and `classification.<code>`, `confidence.<code>`, `kind.<code>`. A key left out
keeps its English default.
A complete Spanish draft of every key is in [`examples/labels/es.json`](../examples/labels/es.json) ([README](../examples/labels/README.md)).
Machine-made draft. A fluent Spanish reader must check every line before a campaign uses it; nothing here is reviewed.
Refused, and the build writes nothing: an unknown key; text that is not 1-300 characters after trimming or that holds
`<`, `>` or a control character; a `{placeholder}` the key's default does not use (only `{name}`, `{county}`,
`{state}`, `{count}`, `{date}` exist (plus `{outlet}` in `finding.reported_in`), plus `{shown}`/`{total}` in `js.filter_count` and `{evidence}`/`{source}` in
`js.map_failed`/`js.data_failed`, and `{title}`/`{record}` in `handout.source_line`) or an unpaired brace; another top-level field, `schema_version` other than 1, a
file over 64 KiB or a symlink. `content-check` names the same refusals, with the build's text, beside every other
problem. Text is HTML-escaped once (`&`, `"`); `lang` still comes only from `site.json.language`.
Meeting-pack handout (`handout.*`): `meeting-pack` (`docs/MEETINGS.md`) reads the same `labels.json` and writes
`handout.html` with `<html lang>` taken from `site.json.language`. Keys: `handout.title` (page title), `handout.what_we_found`,
`handout.agenda` (agenda link text), `handout.nothing_published` (no published finding), `handout.source_line`
("Source: {title}, received {date}, record {record}"; `{title}` is the source title, `{record}` the 12-character
prefix), `handout.not_recorded` (stands for `{date}` when the library has no received date), `handout.corrected`
("corrected {date}"), `handout.more_on_site` ("{count} more on the site") and `handout.record_explainer`; finding
classification and confidence use `classification.<code>` and `confidence.<code>`. Placeholder values are escaped
once and filled into the label only: a title that contains `{count}` prints as written. `comment.md` and
`sources.csv` do not change with the labels.
Scripts (`js.*`): the fixed text the generated JavaScript writes into the page. Defaults are the English the scripts
always used. While every `js.*` label equals its default every script file is byte-identical (a nonempty source library adds only
the one-key `LABELS` line for `js.filter_count` to `site-data.js`, for `sources/filter.js`); once one differs,
`site-data.js` gains one line `var LABELS={...}` holding every `js.*` key (unset keys keep English), the findings index
loads `/site-data.js` before `/findings/filter.js`, and the scripts read `LABELS` (placeholders filled through
`textContent`; popup text through the map's escaper; never raw HTML). Keys: `js.filter_count` ("Showing {shown} of
{total}", findings filter); `js.mapped_point`/`js.mapped_points` (map selection count); `js.map_failed` with
`js.map_evidence`/`js.map_source` (map could not load; the two link texts fill `{evidence}`/`{source}`);
`js.data_failed` with `js.data_source` (camera data could not load); `js.popup_title`, `js.popup_no_manufacturer`,
`js.popup_no_operator`, `js.popup_source` (point popup); `js.county_findings`/`js.county_no_findings` (county card
facts when site.json lists none); `js.city_points_one`/`js.city_points_many`, `js.city_no_findings` (agency cards);
`js.group_vendor`, `js.group_status`, `js.group_summary` (`{name}` is the map's group label), `js.group_fact`,
`js.group_open`, `js.group_flag` (the unconfirmed-ownership group card).
Search page labels: `footer.search` (footer link), `search.title`, `search.description`, `search.label` (the box label),
`search.intro`, `search.fallback` (`{findings}` and `{sources}` are the two links, text from `nav.findings`/`nav.sources`);
script texts `js.search_hint`, `js.search_none`, `js.search_count_one`/`js.search_count_many` (`{count}`),
`js.search_unavailable`, `js.search_finding` and `js.search_source` (the tag after each result). `search.js` is the same
file for every site: it carries the English defaults and reads `LABELS` when `site-data.js` is loaded first, which
`search.html` does only once a `js.*` label differs from its default.
Stays English by design: the starter page, `feed.xml`/agency feeds, `sitemap.xml`, `robots.txt`, `_headers`,
`data/findings.json`, and text from shared modules: the source sentence and provenance explainer
(`provenance_text.py`) and the corrections policy (`corrections_policy.py`). Content-supplied text is never touched.

## Redirects (`content/redirects.json`, optional)
Old paths an organizer lists (a migrated campaign site, a renamed finding slug) become `public/_redirects`, one sorted
`<from> <to> 301` line per row; 301 is the only status (no 302s, 200 rewrites, splats or placeholders). Format:
`{"schema_version": 1, "redirects": [{"from": "/old/report", "to": "/findings/"}]}`; no file or no rows: no `_redirects`.
Refused, naming the row, and the build writes nothing: more than 500 rows, a key other than `from`/`to`; a `from` that
is not an ASCII path of at most 300 characters (`..`, `?`, `#`, `*`, `:`, spaces), is `/`, or is a path the build writes
(a page, `/data/...`, `/sources.html`, a published finding); a `to` that is external or not a page the build writes; a
repeated `from`, a chain or a loop; a withdrawn finding's path pointed at a different finding (`/findings/` and
`/corrections.html` are fine). `content-check` lists the same refusals; the page rules run once the rest is clean.

## Brand (`site.json.brand`, optional)
Example: `{"logo": "logo.png", "logo_alt": "Cedar Valley Watch", "accent": "#b83d2c", "ink": "#092b2d", "favicon": "favicon.ico"}`.
Every key is optional. `logo` (a PNG, WebP or SVG in `content/public-assets/`, plain names, no symlink, no image metadata,
1-2000 px each side) shows inside the header's brand link with its own width and height; `logo_alt` (1-120 characters
of plain text) is required with it. `favicon` (`.ico`, a metadata-free `.png` or an `.svg`) adds `<link rel="icon">` to every
page. `accent` and `ink` (`#rrggbb`, stored lowercase) append one `:root{--coral:…;--ink:…}` rule to `style.css` for
the keys set. The build checks WCAG 2.x contrast of at least 4.5:1 for `ink` on cream (`#fff8ec`) and paper
(`#f3eee4`) and `accent` on cream, and refuses with the field, the pair and the ratio. A logo or favicon may also be
an `.svg` that passes the same SVG safety check as any public asset (no `DOCTYPE`/entity, `script`, `foreignObject`,
`on*` handler, link, `url()` or external reference; a refusal names the kinds, never the value). Its size is the
root's numeric `width` and `height` (unitless or `px`), else the root `viewBox` (four numbers, width and height above
zero, rounded to whole pixels); percent, `em`, other units or neither readable is refused as `unreadable SVG (no
readable width and height)`. A logo is 1-2000 px each side as for PNG; it is always an `<img>` (never inline SVG)
and an SVG favicon is `<link rel="icon" type="image/svg+xml">`. The CSP is unchanged (`img-src 'self'` covers it).
A missing, misnamed, oversize or unreadable file, an unknown key or a bad
colour is refused by field name (the value is never printed); `content-check` lists the same lines. Without `brand`
every built file is byte-identical.

## Phones
At widths up to 850 px, content-page menus wrap with 44 px tap targets and a visible keyboard focus outline.
Agency cards use a single column; long finding, agency and meeting text wraps, and wide finding tables scroll locally.
Filter and signup controls have 44 px minimum heights. Starter and print layouts and the CSP are unchanged.

## Content-Security-Policy

`_headers` is generated exactly from the content model:

```
default-src 'none'; script-src 'self'; style-src 'self';
img-src 'self' data: [blob: https://<tile host>...];
connect-src 'self' [https://<style host> https://<tile host>...];
font-src 'self'; [worker-src blob:;] base-uri 'none';
form-action 'none' | https://<signup host>; frame-ancestors 'none'
```

The bracketed parts appear only when `map/map-config.json` exists (`blob:` and
`worker-src blob:` are required by MapLibre's web workers) or when
`signup.mode` is `brevo_hosted`. The signup URL host must end in
`.sibforms.com`; the page renders a plain `<form method="post">` to that URL with
an `EMAIL` field, so no provider script or iframe is loaded and no address is
collected by the site itself. `X-Content-Type-Options`, `Referrer-Policy:
no-referrer` and a restrictive `Permissions-Policy` are always set.

## Map data and MapLibre

`tools/fetch_osm_alprs.py --directory D --bbox S W N E | --relation ID | --from-file SAVED.json`
queries the Overpass API (network; never run by tests), or reads a reviewed saved answer offline, for
`man_made=surveillance` + `surveillance:type=ALPR` nodes and writes
`content/map/cameras.geojson` and `content/map/attribution.txt`. `--check` writes nothing: it compares by
OSM node URL with the current `cameras.geojson`, prints added/removed/moved (beyond `--move-metres`, default 10)/retagged
counts and up to 20 node URLs each, and exits 3 when any point would be removed. The data is
(c) OpenStreetMap contributors under the ODbL; most points are contributed by
the DeFlock community, which is the community source of these observations. A
mapped point says a camera was observed, not which agency owns it. The
controller labels points outside city boundaries without an operator tag as
candidates with ownership unconfirmed; keep that label.
At build time each picker city whose `profile.cameraCount` is empty gets `"N community-mapped points inside city
limits; ownership unconfirmed"` (`1 ... point`, `0 ...` too): the `cameras.geojson` points inside the city boundary
feature whose `BASENAME` or `NAME` equals the city name (Polygon or MultiPolygon, holes excluded; a point on a shared
edge counts once). A typed `cameraCount` always wins. `build` prints `note: boundary <name> matches no city agency` and
`note: city boundary feature <index>: <reason>` on stderr; notes never change the exit code.
A writing run needs `D/campaign.json` (exit 2 otherwise, before any network call). An answer with no points never
replaces a map that has points (exit 3) unless `--allow-empty` is given. Each file is replaced atomically
through a fresh temp file; a symlinked `content`, `content/map` or output file is refused (exit 2).

Each writing run (never `--check`, never a refused write) also records one dated entry in
`content/map/history.json` (`{"schema_version": 1, "records": [...]}`): `recorded_on` (the OpenStreetMap data day
from `osm_base`, else the run's UTC day), `osm_base` (or null), `source`, `total` points, `by_city` (points inside each
`content/map/city-boundaries.geojson` feature by the build's own boundary rule, or null without that file) and
`added`/`removed` node counts against the map the run replaced (0/0 for a first map). A later run on the same day
(so the same `osm_base` too) replaces that day's record; the newest 120 records are kept and the oldest dropped. A
history file the build would refuse is never overwritten, and a map day older than the last record is not recorded
(exit 2 for both; the new map is still written). `build` validates
the file and refuses an unknown key, `schema_version` other than 1, more than 120 records, a non-ISO date, dates
that do not strictly increase, a negative or non-integer count, a symlink or a file over 256 KiB, naming the field
and never its value; `content-check` lists the same line. With two or more records `cameras.html` shows "Mapped points
over time", a plain table (date, total, added, removed, and per-city columns when every record has `by_city`); with
none or one the built site is unchanged. A mapped point, and a rising count, is not evidence of agency ownership.

**Facing tag (`direction`, optional).** `fetch_osm_alprs.py` keeps a node's `direction` tag (else its `camera:direction`
tag; a present but unusable `direction` is never replaced by the other key) in `cameras.geojson` as
`properties.direction`, exactly as tagged, when it is 1 to 4 values joined by `;` (no spaces, at most 15 characters)
and each value is a whole number 0 to 359 (no sign, fraction or leading zero) or one of the 16 compass words N, NNE,
NE, ENE, E, ESE, SE, SSE, S, SSW, SW, WSW, W, WNW, NW, NNW in any case. Anything else on a retained point is written
as `null`, and the run prints `direction invalid=N` on stderr (a count only, never the tag text); a point without
either tag gets no `direction` property, so a map fetched without any facing tag is byte-identical to before and a
`cameras.geojson` without the property reads unchanged. `--check` treats a changed value as `retagged`, comparing
the strings exactly (`NE` to `ne` or to `SW` is retagged; a missing property and `null` are both "unknown"). `build`
and `content-check` refuse a hand-edited `direction` outside the grammar (`map/cameras.geojson.features[N].direction:
expected an OpenStreetMap facing tag or null`, never the value). `cameras.html` adds `Faces: NE (OpenStreetMap tag)`
under the OpenStreetMap link of a tagged point (label `map.direction_line`) and the map popup shows the same text;
untagged points show nothing new. The value is a volunteer's tag: no bearing is converted, and nothing is inferred
about a road, a plate, coverage, ownership or an independent check.

`content/map/point-checks.json` (optional; organizer-edited by hand, never fetched or written by the tool) records what
an organizer found at single mapped points: `{"schema_version": 1, "checks": [{"node": 123, "status": "seen_in_person",
"checked_on": "2026-11-01", "source": "<12-hex library prefix or https:// URL>", "note": "optional, 200 characters"}]}`.
`node` must be an OpenStreetMap node id present in `cameras.geojson` (its `.../node/N` URL); `status` is
`seen_in_person`, `in_agency_record`, `not_found_on_site` or `duplicate`; `checked_on` is a calendar day not after the
build day; `source` is either the first 12 hex characters of one `sources.json` entry's sha256 (it must match exactly
one) or an `https://` URL (no userinfo, at most 500 characters); `note` is plain text without an address or phone-like
number. At most 5 checks per node, 2000 checks and 512 KiB; a symlink is refused. `build` refuses the file naming the
field (`map/point-checks.json.checks[3].node: not a mapped point`) and never the value; `content-check` lists every
refused entry. With the file, `cameras.html` gains an "Organizer check" column (newest check: status, date, source link;
"and N earlier" when more; a dash when none) and the map popup one escaped line "Organizer check: <status>, <date>"
(the note and the source stay off the popup and out of `site-data.js`). Without the file nothing is built differently.
A check never removes or edits a point and never writes to OpenStreetMap: `duplicate` and `not_found_on_site` points stay
on the map and in every count. `in_agency_record` says an agency record lists the point; it is never ownership.

The build never downloads MapLibre. To enable the interactive map, copy the
library into `content/vendor/maplibre-gl.js` and `content/vendor/maplibre-gl.css`
from the official `maplibre-gl` npm package: the controller is written against
the MapLibre GL JS 4.x API (for example `maplibre-gl@4.7.1`, files
`dist/maplibre-gl.js` and `dist/maplibre-gl.css`); pin the exact version you
copy and verify it in a browser, since no offline test exercises it. MapLibre GL JS is BSD-3-Clause licensed; keep its
LICENSE text with your campaign repository. Record the exact version and file
hashes in the campaign's lock. Without the vendor files the map section renders
the static fallback and the picker still drives the agency cards. The map style
(`style_url`) and tiles come from a provider the campaign chooses; their terms
and attribution are the campaign's responsibility, and the CSP names only the
hosts you declare in `style_url` and `tile_hosts`.

## What is never rendered

- Findings that are not `state: published`, carry `needs_attorney_review`, or
  fail a structural review blocker.
- Any field not in the allowlists above (unknown keys stop the build).
- Angle brackets in any text field; non-https or `javascript:` links; remote
  scripts, fonts, iframes or images.
- Full SHA-256 values on pages (12-character prefixes only), records originals,
  `private/`, `kit/requests/`, email addresses other than `contact_email` and `privacy_contact`,
  subscriber data, portal credentials.
- Anything in `public-assets/` outside the extension allowlist or over 5 MiB.

## Limits

The offline tests (`tests/test_content.py`, `tests/test_site_build.py`,
`tests/test_markdown_lite.py`, `tests/site-build.test.mjs`) check structure,
escaping, CSP text, refusal paths and the generated script wiring in a mocked
DOM. They do not render WebGL, load tiles, verify provider terms, or review the
truth of any finding. The fictional example places its three points in the
ocean near 0,0 and makes no statement about a real agency.
