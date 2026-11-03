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
| `content/site.json` | yes | tagline, `mission_md`, `about_md`, `contact_email`, `signup`, `social[]`, `language`, `base_url`, `county_profile`, `source_link_label`, `font_family`, `not_legal_advice`, `brand` (below) |
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
says which), `limitations`, `counterevidence`, `corrections` (`date`, `note`) and
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
`signup.*`, and `classification.<code>`, `confidence.<code>`, `kind.<code>`. A key left out keeps its English default.
Refused, and the build writes nothing: an unknown key; text that is not 1-300 characters after trimming or that holds
`<`, `>` or a control character; a `{placeholder}` the key's default does not use (only `{name}`, `{county}`,
`{state}`, `{count}`, `{date}` exist) or an unpaired brace; another top-level field, `schema_version` other than 1, a
file over 64 KiB or a symlink. `content-check` names the same refusals, with the build's text, beside every other
problem. Text is HTML-escaped once (`&`, `"`); `lang` still comes only from `site.json.language`.
Stays English by design: the starter page, `feed.xml`/agency feeds, `sitemap.xml`, `robots.txt`, `_headers`,
`data/findings.json`, all JavaScript (`site-data.js`, `agency-cards.js`, `app.js`, `findings/filter.js`, which
rewrites the "Showing N of M" count), and text from shared modules: the source sentence and provenance explainer
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
