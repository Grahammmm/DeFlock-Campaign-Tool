# Agency, finding and evidence-link renderer

This compatibility slice extracts the existing profile renderer: agency identity,
status, summary, count, public findings list, uncertainty flags, next-action text,
and two links to source notes. The campaign supplies county-wide copy and source
routing through the schema; selected agencies still come from the campaign-owned
CITIES object. The controller calls the generated setCityText function.

No legal conclusion is generated or promoted by this renderer. All wording must
already have completed the campaign's evidence, privacy and publication review.
Evidence-note links are not a claim that original records are downloadable.

## Contract and boundaries

campaign_tool.agency_cards.render(config) accepts schemas/agency-cards.schema.json.
The county profile uses the existing name/vendor/status/summary/cameraCount/facts/
open/flags fields. Source destinations must be local HTML filenames and safe
fragment slugs. Explicit alias mappings handle a group's differently named source
section without hard-coding a jurisdiction in the engine.
A city's empty `cameraCount` is filled by the site build with a computed "N community-mapped points inside city
limits; ownership unconfirmed" label when the campaign has a map (docs/SITE-CONTENT.md); a typed value is kept.
`agency_cards.problems(config)` lists every problem at once (sorted, dotted field names such as `county_profile.vendor`, never a value); `render` raises them joined by `; `.

**Check a config.** `python3 -m campaign_tool.agency_cards --check cards.json` (≤ 2 MB UTF-8 JSON object;
`NaN`/`Infinity` refused) builds nothing and prints each problem, then `problems=N`. Exit 0 none, 1 problems,
2 unreadable file or usage (one stderr line, no value echoed).

New county text rejects angle-bracket markup, control characters other than newline/tab, invisible format and
direction-override characters (Unicode `Cc`/`Cf`, e.g. U+200B, U+202E, U+2066, U+FEFF) and U+2028/U+2029: the field is a
named problem, never rewritten or stripped. The selected-agency CITIES object
remains **trusted, reviewed campaign source**, not an arbitrary JSON upload or
record-extraction result. Its legacy list rendering uses innerHTML for compatibility;
this is not an HTML sanitization boundary. Do not wire user input or AI drafts
directly to it. A later data-only adapter must validate every selected-agency field
before this compatibility global is retired.

The synthetic example makes no real agency allegation and supplies no real
contact address, subscriber, production configuration or private record.

## Agency contract term

An agency's `profile.contract` is `{vendor, start, end, renewal_note?, source_url, checked_on}`, checked by an organizer
on the official source and never fetched. `vendor` (≤ 80) and `renewal_note` (≤ 200) are plain text; `start`/`end` are
YYYY-MM-DD dates with `end` not before `start`; `source_url` is https; `checked_on` is not after the build date. A
contract without `source_url` or `checked_on` stops the build. `agencies.html` shows it on the card; the home page lists
contracts ending within 180 days of the build date, soonest first; `doctor` reports `contracts_ending` within 90 days.
It never enters the CITIES card config, and `site.json` `county_profile` has no `contract` key.

## Transparency-portal snapshots

An organizer may transcribe what a verified agency's Flock transparency portal showed on a given day into
`profile.transparency`: a list of at most 24 snapshots, newest first, each
`{checked_on, source_url, cameras?, retention_days?, searches_last_30_days?, hotlist_alerts?, shared_with_agencies?}`.
`checked_on` is the day you looked (YYYY-MM-DD, not after the build date, no two snapshots on the same day);
`source_url` is exactly `https://transparency.flocksafety.com/<flock_transparency_slug>` (a trailing `/` is allowed), so
the agency needs `verified: true` and its slug. Each number is a non-negative integer copied as shown, or `null` (or
left out) when the portal did not show it; `retention_days` is at most 3650. Add a new snapshot at the top; keep older
ones so the site can show the change. Anything else stops `build` and `content-check` with the same message.

The tool never fetches the portal. The card and the agency page show the newest snapshot under "Reported on the Flock
transparency portal", one line per number, each with "up N", "down N" or "no change" since the previous snapshot when
that snapshot has the number, then "as reported by the vendor, checked DATE" linking the portal. These are the vendor's
own claims: the default wording never calls them verified, confirmed or fact, and reviewed `labels.json` wording must not
either. Snapshots never enter the card JS config.

## Reported and mapped counts together

A city agency's own page (`agencies/<slug>.html`, not the card list) adds a section right after the transparency block
when two numbers exist: the camera count of its **newest** `profile.transparency` snapshot (a `null` or missing count
there means no section; an older snapshot is never used instead) and the community-mapped count that the build already
computes for the same picker city (`map/cities.json` `name`, else `jurisdiction_name`, else the agency name, matched
exactly against the boundary names). The agency must itself be a picker city, so county, sheriff, CHP, campus and other
agencies, and city agencies outside the picker, show nothing, whatever their names look like. **Zero is a real count** on
either side (it shows); a missing number is not zero (nothing shows). No map means no section.

Two paragraphs, from the labels `agency.mapped_reported_counts` and `agency.mapped_reported_caveat`:

> The agency's transparency portal reported N cameras (checked DATE). Community mappers have mapped M points inside the
> city limits (map data as of DATE).
>
> These are counted differently: the portal counts the agency's own cameras, the map counts points inside a boundary
> whoever owns them.

The first date is the snapshot's `checked_on`; the second is the map data day from `map/snapshot.json` (never the build
date or a file time). When that day is unknown the label `agency.mapped_reported_unknown` (`unknown`) stands in the
sentence; it is a single word, not the full "Map data date unknown" sentence. The two counts measure different
populations and are never subtracted, compared as a percentage, ranked, or described as missing cameras, and neither is
called complete. The numbers stay the vendor's report and the community map; the page makes no ownership claim.
Organizer verification of the agency is not verification of either number. Nothing changes in the camera cards, the card
JS, the `cameraCount` text from the map counts (see above), or any page without both numbers.

## Confirmed, not known, asked

Each agency on `agencies.html` has three blocks after its summary, from organizer-reviewed `content/agencies.json`
only. "What is confirmed": `profile.facts` plus "N published finding(s) with sources", else "Nothing is confirmed by
a published finding yet." "What is not known": `profile.open` then `profile.flags`, else "Nothing listed yet." "What
we asked": each `asked` item, newest `asked_on` first, as "<topic> — asked <date>. <state sentence>", else "No request
is listed for this agency yet." Wording is neutral and fixed: "No written response recorded as of <updated_on, else
the build date>", "Acknowledged <date>; no records yet.", "Partly answered <date>.", "Answered <date>.", "Declined in
writing on <date>.", "We withdrew this request <date>." (without a date when `updated_on` is absent). Never
"ignored", "refused", "late", "overdue" or "violated"; no legal characterisation is made.

## Map city panel: agency page link

When an agency has an `/agencies/<slug>.html` page, its `CITIES` entry carries
`agencyPage` (that path) and `findingCount` (published findings with its `agency_id`).
The optional `#city-agency-link` shows "Agency page · N published findings"; its label
comes from the `data-label` attribute in the page markup (the `agency.page` label),
rendered only when agency pages exist, so neither `site-data.js` nor the card script
carries a label. Other cities, the county view and the group hide the link.

## Pilot preservation and remaining work

The pilot builds its profile renderer from campaign/agency-cards.json and a pinned
engine template. It keeps its old profile function as a frozen comparison fixture.
All app.js, other public-asset hashes and security headers must remain unchanged.

Static source-library pages, individual source metadata/provenance cards, full
agency data schemas, and a complete fresh-campaign wiring wizard are not extracted
in this slice. It must not be presented as a finished evidence publication system.

Tests cover deterministic configuration, rejected markup/paths/schema versions,
single-pass replacement, selected/default profiles, source aliases, counts and
optional link elements. Browser, independent factual review and live signup tests
are separate; these offline tests send no network requests.
