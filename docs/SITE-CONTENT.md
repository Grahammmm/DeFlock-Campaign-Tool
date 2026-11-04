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

`python3 -m campaign_tool content init --directory D` writes the minimal valid tree (`site.json` with `[EDIT ME: ...]` placeholders, empty `findings/` and `meetings/`, `sources.json` `[]`); it refuses an existing `content/`.

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
| `content/site.json` | yes | tagline, `mission_md`, `about_md`, `contact_email`, `signup`, `social[]`, `language`, `base_url`, `county_profile`, `source_link_label`, `font_family`, `not_legal_advice`, `privacy_contact`, `brand` (below) |
| `content/findings/*.json` | no | one published finding per file (schema below) |
| `content/meetings/*.json` | no | `body`, `starts_at`, `agenda_url`, `agenda_item`, `location`, `comment_kit_md`, `agency_id`, optional `cited_findings` (1 to 3 unique finding ids, written by `meetings-stage`) |
| `content/sources.json` | no | source-library entries: `sha256`, `title`, `agency_id`, `received_at`, `public_url` or null, `fragment`, `pages`, `note` |
| `content/agencies.json` | no | override list; default is the selected entries of `kit/agencies.json` plus `kit/governing-bodies.json` |
| `content/map/map-config.json` | no | controller settings (`schemas/map-controller.schema.json` keys) plus `county_bounds`, `style_url`, `tile_hosts`, `group_label`, `attribution` |
| `content/map/cameras.geojson` | with map | Point features; properties `manufacturer`, `operator`, `url` (openstreetmap.org object URL or null) |
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

Every finding page shows the classification, a confidence label, the event
date, each source with the first 12 characters of its SHA-256 and its locator,
limitations, counterevidence, a Corrections section and the
"This is not legal advice" line.

`/corrections.html` lists every correction on every published finding, newest
first (same date: by finding slug, then the order in the finding), each linking
to the finding's `#corrections` section. The footer and each finding page link
to it, and finding lists mark a corrected finding with a "Corrected" pill
showing its latest correction date. The starter preview has no such page.

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

An agency's optional `asked` (≤ 20 items) lists what the organizer asked it: `{"topic", "asked_on", "state",
"updated_on"?, "note"?}`. `topic` ≤ 120 and `note` ≤ 200 plain-text characters; `asked_on`/`updated_on` are real
YYYY-MM-DD dates not after the build date, `updated_on` not before `asked_on`; `state` is one of `awaiting_response`,
`acknowledged`, `partly_answered`, `answered`, `declined_in_writing`, `withdrawn`. A bad item stops the build naming
`content/agencies.json[n].asked[i].<field>`; `content-check` lists every such problem, one line each. Kit agencies
never carry it. It is shown only on `agencies.html` (docs/AGENCY-CARDS.md), never exported to data or feeds.

`agencies.html` opens with an "At a glance" table (`#at-a-glance`, labels `agencies.glance_*`): one row per agency in
the list's own order, never sorted by any number, with no ranking, score or "best/worst" wording. Cells: cameras,
searches in the last 30 days and retention days from the newest `profile.transparency` snapshot (each linked to its
`source_url`, checked date shown), the posted `alpr_policy` ("posted", linked to the policy), `contract.end` linked
to the contract source, `contract.cost` as stated (`$12,345.60 per year`, linked to the contract source with its checked
date; "not checked" without a cost) and the number of `asked` items linked to that card's `#<slug>-asked` list (0 shows "0").
A missing value shows "not checked"; content has no "policy not found" record yet, so that state is never shown.
No agencies, no table. On screens ≤ 640 px rows stack with each cell's header; print keeps the bordered table.

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

`source_aliases` are derived: a city agency whose `agency_id` has entries in
`sources.json` routes its evidence link to that entry's `fragment`, which is
also the `id` of the matching section in `sources.html`. Agencies without
sources route to `sources.html#library`.

Each `sources.html` entry ends with `Cited by:` and links to every published
finding that cites its `sha256` (model order, once each), or with
`Not cited by a published finding`. Drafts never appear.

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
[--differs-because redacted|converted|unknown]` hashes a copy you downloaded yourself from an entry's `public_url`
(a regular local file, no symlink, at most 200 MiB; nothing is fetched, copied or stored) and records
`public_copy: {"matches", "checked_on", "reason"?}` on every entry of that record that has a `public_url`. A differing
copy needs `--differs-because`; a matching one refuses it. `checked_on` may not be in the future or before
`received_at`. Refusals print `Stopped: <reason>`, exit 3 and write nothing. `sources.html`, the citing finding pages
and `data/sources.json` then show "Public copy checked D: same file as this record number" or "differs (redacted
copy)", with a short "How to check a copy" note. `sources edit --public-url`/`--clear-public-url` drops the check.

### Pages (`content/pages/<slug>.json`, optional)

Each file becomes `/<slug>.html`: `{"title", "body_md", "description"?, "nav"?}`, nothing else. `title` is
1-120 characters and `description` at most 300 (default: the title); both are escaped. `body_md` (non-empty) is
rendered by `markdown_lite`, so raw HTML shows as text. The file stem is the slug; files not ending in `.json` are
ignored, symlinks and folders are refused, at most 50 files. Slugs that are starter pages, generated names (`404`,
`corrections`, `feed`, `sitemap`, `robots`, `style`, `site-data`, `agency-cards`, `app`, `data`, `vendor`) or a
published finding slug are refused, as is a `public-assets/<slug>.html` entry. Every page is listed in
`sitemap.xml`; `nav: true` (strictly a boolean, at most 3 pages) adds the title to the header nav after About.

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

## Generated pages and files

`index.html` (hero map when configured, agency cards, mission, latest findings,
signup, meetings), `agencies.html`, `findings/index.html`,
`findings/<slug>.html`, `sources.html` (with `#library` and one
`#<fragment>` section per source group), `meetings.html`, `about.html`,
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
`public_copy_matches`, `public_copy_checked_on` and `public_copy_reason`. A cell that begins with `=`, `+`, `-`, `@`, a tab or a
carriage return is written with a leading `'` so a spreadsheet does not run it as a formula (the `kit-status --csv` rule; the JSON
is not changed). A meetings CSV is not built.
`data/meetings.json` is the staged meetings as open data (`schemas/meetings-open-data.schema.json`), linked from
`meetings.html` and `about.html`, newest `starts_at` first, cancelled ones included (`"meetings": []` with none):
top level `schema_version`, `generated_at` (the build date at 00:00 UTC) and `meetings`; per meeting `id`, `body`,
`agency_id` or null, `starts_at`, `status`, `agenda_url` or null, `url` (the page path `/meetings/<id>.html`) and, for a
held non-cancelled meeting with a recorded outcome, `outcome` (`result`, `on`, `source_url`, `vote` and the per-member
`votes` as `{name, vote}` in stored order, each only when recorded). It never includes the comment kit, `participation`,
contacts, `location`, `agenda_item`, `verified_by`, `verified_at` or any other key; the build refuses with
`data/meetings.json: non-public key ...` and writes nothing.
`data/agencies.json` is the agencies as open data (`schemas/agencies-open-data.schema.json`), linked from
`agencies.html` and `about.html`: only agencies that have a public page (`/agencies/<slug>.html`), sorted by `name` then
`agency_id` (`"agencies": []` with none). Top level `schema_version`, `campaign`, `generated_from`, `base_url`, `url_kind`
and `agencies`; per agency `agency_id`, `name`, `kind`, `url` (the page path, with the base URL when one is set), `asked`
(`topic`, `state`, `asked_on`, `updated_on` or null, newest `asked_on` first as the card lists them), `alpr_policy`
(`url`, `checked_on`) or null, `contract` (`vendor`, `start`, `end`, `source_url`, `checked_on` and, only when the contract
has one, `cost`: `amount_usd`, `period`, `as_stated_in`) or null, `transparency`
(the newest snapshot only: the portal numbers the page shows, `source_url` and `checked_on`) or null, and `camera_count`
(the card's camera-count sentence) or null. It never includes `records_email`, `records_url`, `portal`, the asked-item
`note`, the contract `renewal_note`, kit files, notes or any other key; the build refuses with
`data/agencies.json: non-public key ...` and writes nothing.
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
`signup.*`, `js.*` (scripts, below), `handout.*` (the private meeting-pack handout, below), and `classification.<code>`, `confidence.<code>`, `kind.<code>`. A key left out
keeps its English default.
A complete Spanish draft of every key is in [`examples/labels/es.json`](../examples/labels/es.json) ([README](../examples/labels/README.md)).
Machine-made draft. A fluent Spanish reader must check every line before a campaign uses it; nothing here is reviewed.
Refused, and the build writes nothing: an unknown key; text that is not 1-300 characters after trimming or that holds
`<`, `>` or a control character; a `{placeholder}` the key's default does not use (only `{name}`, `{county}`,
`{state}`, `{count}`, `{date}` exist, plus `{shown}`/`{total}` in `js.filter_count` and `{evidence}`/`{source}` in
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
always used. While every `js.*` label equals its default every script file is byte-identical; once one differs,
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
