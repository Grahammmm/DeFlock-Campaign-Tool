# Site content model and multi-page build

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

## Content directory

| Path | Required | Purpose |
| --- | --- | --- |
| `content/site.json` | yes | tagline, `mission_md`, `about_md`, `contact_email`, `signup`, `social[]`, `language`, `base_url`, `county_profile`, `source_link_label`, `font_family`, `not_legal_advice` |
| `content/findings/*.json` | no | one published finding per file (schema below) |
| `content/meetings/*.json` | no | `body`, `starts_at`, `agenda_url`, `agenda_item`, `location`, `comment_kit_md`, `agency_id` |
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
`**bold**`, `- ` lists and `[text](https://...)` links only. Raw HTML is
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

Every finding page shows the classification, a confidence label, the event
date, each source with the first 12 characters of its SHA-256 and its locator,
limitations, counterevidence, a Corrections section and the
"This is not legal advice" line.

`/corrections.html` lists every correction on every published finding, newest
first (same date: by finding slug, then the order in the finding), each linking
to the finding's `#corrections` section. The footer and each finding page link
to it, and finding lists mark a corrected finding with a "Corrected" pill
showing its latest correction date. The starter preview has no such page.

Share previews: every page carries `og:*` and `twitter:card` `summary` tags from its own title and description
(a finding page: its title and summary, `og:type` `article` with its published/updated times); `og:url` only
when `site.json.base_url` is set. There is no share image by design and nothing is fetched.

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

`source_aliases` are derived: a city agency whose `agency_id` has entries in
`sources.json` routes its evidence link to that entry's `fragment`, which is
also the `id` of the matching section in `sources.html`. Agencies without
sources route to `sources.html#library`.

Each `sources.html` entry ends with `Cited by:` and links to every published
finding that cites its `sha256` (model order, once each), or with
`Not cited by a published finding`. Drafts never appear.

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

## Generated pages and files

`index.html` (hero map when configured, agency cards, mission, latest findings,
signup, meetings), `agencies.html`, `findings/index.html`,
`findings/<slug>.html`, `sources.html` (with `#library` and one
`#<fragment>` section per source group), `meetings.html`, `about.html`,
`style.css`, `_headers`, `robots.txt`, `sitemap.xml`, `feed.xml` (Atom of
findings), `findings/agency/<slug>.xml` (the same Atom entries for one agency,
only for an agency with at least one published finding; linked as "Findings
feed" from its `agencies.html` entry), `site-data.js` (`CITIES`, `COUNTY_BOUNDS`, `MAP_STYLE`,
`CAMERA_DATA_URL`, `BOUNDARY_DATA_URL`), `agency-cards.js`
(`agency_cards.render` output), `app.js` (map controller plus page bootstrap, or
a picker-only bootstrap without a map), `data/*.geojson` and `vendor/*` when
present. `public/` is regenerated from scratch on every content build; do not
hand-edit it. `build --check` runs the `tools/check_public_tree.py` pattern scan
over the output and fails on any hit. The command prints a manifest of every
written file with its size. Meetings on or after the build date (the typed
`starts_at` date, no time-zone conversion) are upcoming, soonest first (home: next
three); `meetings.html` then lists `#past-meetings`, most recent first.

Pages contain no inline scripts, iframes or images other than those under
`public-assets/`; all script tags are `defer` references to the self-hosted
files above.

Printing: `style.css` ends with one `@media print` block. Nav, signup, map and picker chrome are hidden; text is black on white;
each external `http(s)` link prints its address after the text (relative, `#` and `mailto:` links do not). Closed comment
kits print open only in browsers that support `::details-content`; elsewhere they print closed (open them before printing).

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
A writing run needs `D/campaign.json` (exit 2 otherwise, before any network call). An answer with no points never
replaces a map that has points (exit 3) unless `--allow-empty` is given. Each file is replaced atomically
through a fresh temp file; a symlinked `content`, `content/map` or output file is refused (exit 2).

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
  `private/`, `kit/requests/`, email addresses other than `contact_email`,
  subscriber data, portal credentials.
- Anything in `public-assets/` outside the extension allowlist or over 5 MiB.

## Limits

The offline tests (`tests/test_content.py`, `tests/test_site_build.py`,
`tests/test_markdown_lite.py`, `tests/site-build.test.mjs`) check structure,
escaping, CSP text, refusal paths and the generated script wiring in a mocked
DOM. They do not render WebGL, load tiles, verify provider terms, or review the
truth of any finding. The fictional example places its three points in the
ocean near 0,0 and makes no statement about a real agency.
