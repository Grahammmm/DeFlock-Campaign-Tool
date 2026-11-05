# Agency discovery and the organizer kit

`python3 -m campaign_tool kit --directory ../my-campaign` turns a campaign's
county or city into a reviewed-by-organizer agency list and one drafted records
request per law-enforcement agency. It runs offline from a seed file and sends
nothing. Every suggestion is unverified until an organizer confirms it on an
official page; no entry claims that an agency uses ALPR.

## Seed format

Seeds live at `data/agencies/<country>-<state>.json` and follow
`schemas/agency-seed.schema.json` (see [CONTRACTS.md](CONTRACTS.md)). The
California seed is generated, not hand-edited:

```sh
python3 tools/build_agency_seed.py          # rewrite data/agencies/us-ca.json
python3 tools/build_agency_seed.py --check  # CI-style staleness check
```

Contributors edit the tables in `tools/build_agency_seed.py`. Each county row
holds the FIPS code, name, seat and a list of `(city, policing)` pairs where
policing is `"pd"` (own police department, gets a `police` entry),
`"sheriff"` (contracts with the county sheriff, gets only a `city_council`
entry whose source note says so), `("contract", "<agency>")` (contracts with
another agency) or `None` (unknown; council only, source note says verify).
Every county gets a sheriff, board of supervisors, district attorney and one
CHP entry (`jurisdiction_name: "California Highway Patrol"`, so county kits
include the statewide agency; requests go to CHP headquarters public records).

UC and CSU campus police departments (33, from the `CAMPUSES` table) are
seeded per county as kind `campus_police`, after the CHP entry. They are
opt-in: a kit lists them only with `--include campus_police`, never by
default, so a kit without it is unchanged. Each entry is unverified with null
contacts; its source note says to submit to the campus records office, which
is separate from the city and county records offices and has its own
procedure. Private universities are not seeded.

California community college district police departments (36, from the
`CCD_POLICE` table) are seeded in the county of the district office as kind
`ccd_police`, after the campus police entries. Only districts that operate
their own police department are listed; a district that contracts a city
police department or the county sheriff, or that staffs unsworn campus
safety officers, is not, and 36 of the 72 districts in the Chancellor's
Office directory are absent for that reason or because no official page
confirmed a department. They are opt-in: a kit lists them only with
`--include ccd_police`, never by default, so a kit without it is unchanged.
Each entry is unverified with null contacts; its source note says the entry
was seeded from the district's official page and to submit to the district
records office, which is separate from the city, county and campus records
offices. The page each row came from is recorded in the pull request that
added it, not in the seed.

California K-12 school district police departments (19, from the
`SCHOOL_POLICE` table) are seeded in the county of the district office as kind
`school_police`, after the community college district entries. Only districts
that operate their own police department (Education Code 38000) are listed. A
district served by school resource officers from a city police department or
the county sheriff is not listed: those officers' records are the city's or
the sheriff's, so request them from that agency. A district with only a
security or safety division, or whose department could not be confirmed from
an official page, is also absent. They are opt-in: a kit lists them only with
`--include school_police`, never by default, so a kit without it is
unchanged. Each entry is unverified with null contacts; its source note says
the entry was seeded from the district's official page and to submit to the
district records office, which is separate from the city, county, campus and
community college records offices. The page each row came from is recorded in
the pull request that added it, not in the seed.

California special-district police departments (9 entries for 4 districts
and joint powers authorities, from the `DISTRICT_POLICE` table) are seeded as
kind `district_police`, after the school district entries: transit (BART),
port and harbor (San Diego Unified Port District) and park district (East
Bay Regional Park District, Fulton-El Camino Recreation and Park District)
police. A district serving several counties has one entry per county it
serves, with the county slug in its id (for example
`ca-san-francisco-bay-area-rapid-transit-district-police-alameda`), so a
county kit finds it; the entry's source note names its type and that county.
Only a district that employs its own sworn police department, and whose
official pages name each county it serves, is listed: the Burbank-Glendale-
Pasadena Airport Authority and Stockton Port District police pages do not name
a county, so those two are absent until an official page does. A
district that contracts with a sheriff or a city police department (SacRT,
VTA, the Oxnard Harbor District) is not listed: those officers' records are
the contracted agency's, so request them from that agency. A port, harbor or
airport police department that is a department of a city (Los Angeles, Long
Beach, Oakland, San Francisco, San Jose) is not a district and is absent too.
They are opt-in: a kit lists them only with `--include district_police`,
never by default, so a kit without it is unchanged. Each entry is unverified
with null contacts; its source note says the entry was seeded from the
district's official page and to submit to the district records office, which
is separate from the city, county, campus, community college and school
records offices. The page each row came from is recorded in the pull request
that added it, not in the seed.

### California fusion centers (opt-in)

Fusion-center suggestions are unverified; a listing is not evidence of ALPR use or data sharing.

California regional intelligence (fusion) centers are seeded as kind `fusion_center` from the `FUSION_CENTERS`
table in `tools/build_agency_seed.py`, after the special-district entries and before the city entries. A center is
listed only when official pages state both of two facts: the counties it serves (one official page) and its host or
governing agency (one official page, which may be a different page). Official means the center's own site, its host
agency's own site, or a state or federal government page (G decision 2026-11-04). Each fact is cited by the page that
states it; a county list or host is never inferred from a news story, directory, map or similar center. A statewide
center (the State Threat Assessment Center, STAC) has no enumerated counties and is not listed. The validator checks
transcription structure only (https pages, a real non-future `checked_on` date, known county codes, no duplicates),
never that a page says what the row claims, so review the pages below before relying on a row.

Each center gets one entry per county it explicitly serves, id `ca-<center>-fusion-center-<county>`; none is copied
to a neighboring county or placed by headquarters. Every entry is unverified with null contacts, and its two
`sources` entries each carry the https page, `accessed` and `checked_on` for one fact (the county, the host). The
source title tells you to verify the records custodian: the host's records office may not be the center's.

| Center (as the page names it) | Host or governing agency (host page) | County served (county page) | checked_on |
| --- | --- | --- | --- |
| Orange County Intelligence Assessment Center | Orange County Sheriff's Department ([Counter-Terrorism Bureau page](https://www.ocsheriff.gov/commands-divisions/investigations-special-operations-command/operations-support-intelligence-0)) | Orange ([DHS page](https://www.dhs.gov/see-something-say-something/reporting/california/ociac)) | 2026-11-04 |

1 center, 1 county entry. Left out, with the exact missing fact (pages read 2026-11-04):

| Center | Page(s) checked | Missing fact |
| --- | --- | --- |
| Joint Regional Intelligence Center (JRIC) | jric.org home (names Los Angeles, Riverside, San Bernardino, San Luis Obispo, Santa Barbara and Ventura); the FBI 2006 story and the LA Sheriff's manual name partners only | no official page states a host or governing agency; the center's page says only that it is a partnership of agencies |
| San Diego Law Enforcement Coordination Center (SD-LECC) | sd-lecc.org home (names San Diego and Imperial); the San Diego Sheriff's Special Investigations Division page | the county page calls it a collaborative partnership and the Sheriff page lists it among a division's units without saying the Sheriff hosts or governs it; no page states a host |
| California Central Intelligence Center / Sacramento Regional Threat Assessment Center (CCIC/SacRTAC) | sacrtac.org, ccic.ca.gov, DHS | no official page enumerates its counties (the "34 counties of the Eastern District" are not listed) and none states the host |
| Northern California Regional Intelligence Center (NCRIC) | ncric.ca.gov | the "15 county region" is not enumerated on the current pages and an older page lists 13 counties; the governing body is not an agency host |
| State Threat Assessment Center (STAC) | caloes.ca.gov | statewide: it serves no enumerated counties |

The other California centers were not checked and are not listed; add one only with its two official pages.

The seed entries are opt-in: a kit lists them only with `--include fusion_center`, never by default. On a new
private kit run `python3 -m campaign_tool kit --directory D --include sheriff --include fusion_center --scope sharing`.
`--include` is repeatable and explicit. An existing `kit/agencies.json` is kept as it is, so running `kit` again with
another `--include` does not insert rows. For an existing kit, add the reviewed seed agency with `kit-agency add` or
`kit-agency import`, select its exact agency ID with `kit-agency select --directory D --id ID`, and run `kit` again
with `--scope sharing`; `kit-agency deselect` stops its drafting. Nothing is selected without the organizer's action.
The data-sharing scope in `jurisdictions/us-ca/package.json` applies unchanged, with its draft-status and legal
caveats; no request wording changes. The hosted wizard snapshot under `workers/` is frozen and is not updated here,
so `workers/scripts/sync-data.mjs --check` will report it stale after this seed change until a hosted update lands.

Coverage: all 58 counties, and all 482 incorporated cities, each with a
`city_council` entry and a policing value (own police department, county
sheriff, another agency, or not recorded). A city whose policing arrangement
is not recorded has a council entry only, and its source note says to verify.
`records_url`, `records_email`, `portal.url`, `place_fips`,
`flock_transparency_slug` and `muckrock_agency_id` are null until someone
verifies them, and `verified` is false everywhere. A wrong policing value is
a bug: open a PR against the table with the official page as the source.
Still unseeded: private security, tribal police, harbor patrols run by a
sheriff or a city, any special district whose police are contracted, and any
transit, port, airport or park district department no official page has
confirmed yet.

`flock_transparency_slug` is the last path part of the agency's own Flock
transparency page (`https://transparency.flocksafety.com/<slug>`), checked by
the organizer: lowercase letters, digits and single hyphens, at most 80
characters, or the build stops naming the field. The site links it only on
`verified: true` agencies and never fetches it.

### Filling `place_fips` (network, organizer-run)

`python3 tools/refresh_agency_seed_online.py` downloads the Census Gazetteer
place file, keeps California incorporated places, writes
`data/agencies/us-ca.place-fips.json`, and rebuilds the seed. The build script
merges that overlay whenever it exists, so the JSON stays a deterministic
function of the tables plus the overlay. Tests and CI never run it; commit the
overlay and the rebuilt seed together.

## Automated evidence pass (`tools/agency_verify.py`)

The seed above is unverified: it lists agencies, not ALPR users. `agency_verify` is the
evidence pass that turns an unverified seed row into a **cited** verdict per agency. It reads
public web pages the seed already names - it opens no gate and closes none.

```sh
python3 tools/agency_verify.py run --seed data/agencies/us-ca.json --out NEWDIR [--agency SLUG ...] [--limit N]
python3 tools/agency_verify.py report NEWDIR
```

`run` fetches each agency's recorded index-page link (`records_url`) once and writes one
`agency-evidence-v1` row per agency into a new `NEWDIR` (0700, the row file 0600). A verdict is
a **field** decided from the citations alone, never from an agency's name, its county, or a
neighbour's verdict: `evidenced` (a citation naming an ALPR system or vendor on a page under the
agency's independently recorded optional `official_domain`), `likely` (a citation on a
third-party/regional page or with ownership unknown), `no-evidence`
(every recorded link fetched, nothing found), `unreachable` (a recorded link could not be
fetched, the reason named), `not-checked` (no link recorded yet). `evidenced` with zero
citations is structurally impossible. A citation stores the URL, fetch time, HTTP status, the
SHA-256 of the fetched bytes, the quote, and the quote's byte offset - so the claim is
checkable later and a page change shows as a changed hash, not a silent drift. The quote is
rejected if its context matches shared email/phone/plate or conservative address/officer-name
patterns; later clean windows are still considered. These heuristics do not replace privacy review.

**Limits.** Fetching is https-only, one request per recorded link, no crawling, no off-host
redirects, cached plain-text robots policies (missing permits, unreadable refuses), a fixed
polite delay, a per-run cap (`--limit`, default 25), a byte cap, a fixed
timeout, and a plain identifying user agent. Nothing is logged in, submitted, or sent; no form,
no search query, no robots-disallowed path. Fetched bytes are not stored - only the hash, the
status, and the quote. Nothing is written into the seed: promoting an `evidenced` row is a
separate, attended decision. The verdict is evidence about a public body, not an allegation.

## How `locate` works

`campaign_tool.discovery.locate(query, state, seed)` normalizes the query
(case, accents, punctuation) and matches it against county names and the
city names implied by `City of X` / `Town of X` jurisdiction names:

- `San Luis Obispo County` or `kern county` resolves to the county.
- `Morro Bay`, `City of San Luis Obispo` or `Morro Bay city` resolves to the city.
- A bare name that is both a county and a city (`San Luis Obispo`,
  `Sacramento`) raises `ValueError` listing the candidates; set
  `location_query` in `campaign.json` (or `init --location`) to the exact form.
- Abbreviations (`SLO`) are not resolved; the error says how to fix it.
- No match ends `Did you mean: <up to 3 seed labels>?` when a seed name is close
  (closest first); a suggestion is never selected automatically.

`kit` uses `campaign.json`'s `location_query` when present, otherwise
`<county> County`. The resolved location is written back to `campaign.json` as
`location` and `jurisdiction` (additive keys; older files still load).

`agencies_for(location, seed, include=...)` returns copies of the seed entries
with `suggested: true`. For a city match the city's own agencies come first,
then the county-wide ones; for a county match the county-wide agencies come
first, then every city's agencies in seed order. `--include` (repeatable)
restricts and orders kinds: `sheriff`, `police`, `county_board`,
`city_council`, `district_attorney`, `chp`, and the opt-in `campus_police`,
`ccd_police`, `school_police` and `district_police`.

## Online mode

`kit --online` is only consulted when the offline seed cannot resolve the
query. It calls the Census geocoder
(`geocoding.geo.census.gov/geocoder/geographies/onelineaddress`, 10-second
timeout, urllib, no key) and reads county and incorporated-place GEOIDs. The
agencies still come from the seed; if the geocoder names a city that the seed
does not list yet, `kit` stops and says so. Nothing else in the kit path uses
the network, and tests never do.
If the geocoder also fails, the error keeps the offline message, hint included, and the online reason.

## What the kit writes

Under `<campaign>/kit/`:

| File | Purpose |
| --- | --- |
| `agencies.json` | Organizer-owned list, `selected: true` by default. Never overwritten once it exists. |
| `agencies.suggested.json` | Fresh suggestions on re-runs; `summary.json` reports the id diff count. |
| `requests/<agency_id>.md` | One combined draft per selected sheriff/police/CHP agency covering every request scope in the law package. `kit/drafts-written.json` records the SHA-256 of each draft kit wrote; on a re-run a draft you edited is kept and named, and the fresh draft goes to `requests.suggested/<agency_id>.md`. Unedited drafts for deselected agencies are removed; files kit never wrote are kept. |
| `governing-bodies.json` | Boards of supervisors, city councils and district attorneys (no request draft). |
| `law.json` | Law-package summary, or `{"status": "missing"}` when none exists; drafts then come from `templates/records-request.md` with a banner. |
| `summary.json` | Counts, location, package status, next steps, `sent: false`. |
| `README.md` | What the folder is and that nothing was sent. |

`doctor` reports `agencies_selected` and `kit_built`.

### Request settings

To fill every draft at once, write a private `kit/request-settings.json` (never commit it):

    {"schema_version": 1, "requester": ["A. Organizer", "a@example.org"], "fee_cap_usd": 50,
     "date_from": "2023-01-01", "date_to": "today"}

`requester` is 1–8 lines of 1–120 characters (no control characters); `fee_cap_usd` is a number from 0 to
10000 and prints as `$50.00` (`$0.00` means no fees authorized); `date_from` is a real `YYYY-MM-DD` date
not after today, and `date_to` is such a date not before it or `"today"` (default `"today"`; `date_to`
alone is refused). The next `kit` run puts them into every law-package and template draft; a missing
key keeps that placeholder and its `next_steps` line, and `summary.json` reports `request_settings`.
Any other key or a bad value stops `kit` before a draft is written, naming the key but never the value.
Edited drafts are still kept, with the fresh draft in `requests.suggested/`.

`kit --scope agreements --scope policies` drafts only those law-package `request_scopes` (package order,
duplicates ignored) and lists only the rules they cite; `summary.json` reports `scopes` (`"all"` without the flag).
An unknown scope id, or `--scope` with no law package, stops `kit` before anything is written. Switching scopes
refreshes unedited drafts; edited ones are kept, with the fresh draft in `requests.suggested/`.

For a plain-language explanation of what each scope asks for, who may hold it and what to check in a response, see the
[California records primer](RECORDS-PRIMER.md). It covers record types and response checks, not legal advice.

## What organizers must verify before sending

The kit is a starting point, not a mailing list. Before any request leaves:

1. **Custodian address.** Find the public-records email, form or mailing
   address on the agency's own official website (not a directory or a
   search-engine snippet) and record it in `agencies.json`
   (`records_email`, `records_url`). Confirm the agency actually polices the
   area: contract cities receive service from the sheriff or a neighbor.
2. **Portal.** Many agencies require a portal (NextRequest, GovQA, JustFOIA).
   Record `portal.vendor` and `portal.url`; a portal submission is still a
   request and needs the same approval and receipt.
3. **Fee policy.** Read the agency's fee schedule, set an explicit cap in the
   draft, and keep the approval on file.
4. **Scope and dates.** Fill the date range and requester block; narrow the
   audit-log window if the agency's policy suggests a large volume.
5. **Law package status.** `draft` or `missing` means no reviewed legal basis
   is attached; deadlines in `law.json` are illustrative and skip holidays.
6. **Approval.** Sending, fee payment and publication happen only after an
   organizer approval outside this tool, with the send receipt recorded.

## Recording a checked contact

After reading the contact on the agency's own official page, record it with that source and the date:

    python3 -m campaign_tool kit-verify-agency --directory ../my-campaign --agency-id ca-example-police \
      --source-url https://police.example/records --checked-on 2026-10-01 --records-email records@police.example

Give one or more of `--records-email`, `--records-url` and `--portal` (URLs must be `https://`). The entry gets
`verified: true` and `verification` (`source_url`, `checked_on`, `fields`); a later call replaces `verification`
and changes only the fields it names. The organizer, not the tool, checks the page: `source_url` is never
fetched and nothing is sent. Re-running `kit` keeps the entry and uses the email in unedited drafts.

To request by post, record the custodian's postal address from the same official page:

    python3 -m campaign_tool kit-verify-agency --directory ../my-campaign --agency-id ca-example-police \
      --source-url https://police.example/records --checked-on 2026-10-01 \
      --mailing-address 'Records Unit|123 Example Street|Exampleville, CA 90000'

`--mailing-address` is 2-5 lines separated by `|`, each 1-80 characters after trimming; angle brackets, control or
format characters and line separators are refused, and an error names the rule, never the address. It can be the only
contact given. It is stored as `mailing_address` with its own `mailing_verification` (`source_url`, `checked_on`: this
call's source and date) in `kit/agencies.json`, which stays private: the public site never carries either key, and
`content/agencies.json` refuses them. A later electronic `kit-verify-agency` (single or `import`) keeps the postal proof,
and `verification.fields` lists only the electronic fields. The organizer, not the tool, checks the page; nothing is
fetched. `import --csv` does not take postal addresses and refuses `--mailing-address` (exit 2).

To record many checked contacts at once, use `kit-verify-agency import --directory D --csv FILE [--check]`. The
first row must be exactly `agency_id,source_url,checked_on,records_email,records_url,portal_vendor,portal_url`
(UTF-8, a leading byte-order mark is fine); 1-500 data rows, at most 512 KiB, and an empty cell means "not given".
`portal_vendor` must be one of `nextrequest`, `govqa`, `justfoia`, `none`, `unknown` and needs a `portal_url`; a
`portal_url` alone keeps the entry's vendor (or `unknown`). Every row passes the same checks as the single-agency
command against the current `kit/agencies.json`, and an `agency_id` may appear only once in the file. If any row is
bad, each is named as `line <L>: <reason>` (the header is line 1; the first 20, then `and N more`), the command exits 2
and nothing is written; otherwise all rows are applied in file order in one write. `--check` runs every check and
writes nothing. Output lists agency ids only, never an e-mail address, and no URL is fetched and nothing is sent.

Contacts go out of date. `doctor --directory ../my-campaign --contact-max-age-days 180` adds `stale_contacts`: each
selected, verified entry whose `verification.checked_on` is more than 180 days old (`agency_id`, `checked_on`,
`age_days`, oldest first; a missing or invalid date is listed with nulls), at most 100 plus `stale_contacts_more`,
and a next action to re-check them with `kit-verify-agency`. It reads only `kit/agencies.json` (not the flock
store's `CONTACT.csv`), writes nothing and fetches nothing. Without the option `doctor` is unchanged.

## Recording a hand-sent request

Before sending, `kit-check --directory ../my-campaign [--json]` lists, for each selected law-enforcement agency, the
draft's unfilled `[placeholders]` with line numbers, `fee cap missing`, `date range missing` and `recipient not
verified` (no `verified: true`; see `kit-verify-agency`), then `N of M drafts need attention`. It writes nothing and
exits 3 when any draft needs attention. A Markdown link `[text](url)` is not a placeholder. `kit-sent` runs the same
check for its agency and refuses a draft that still fails it.

The draft's `To:` line follows a fixed order. A `records_email` is printed as is. Otherwise, for an agency with
`verified: true`, the first route that qualifies: `To: records portal URL (VENDOR)` (a recorded `portal` with a
supported vendor, `unknown` included, and an https URL), else `To: by post to the verified mailing address (kit-copy
--for mail)` (a verified postal address with its source and checked date). After `kit-verify-agency`, run `kit` again
to regenerate it; an unedited draft is refreshed, while an edited one is kept byte for byte and the new text goes to
`kit/requests.suggested/`, as for any other change. An unverified agency, an agency with only a `records_url`, or a
malformed portal or postal record keeps the `[official records custodian]` placeholder: a records page is not a
portal. The no-law-package fallback draft does not use `records_email` in its `To:` line (a known limitation, not
changed here), but does name a verified portal or postal route. Only the `To:` line changes; the other placeholders
(fee cap, date range, requester) must still be filled, the postal address never appears in the draft, and approval
and sending stay manual. Nothing is sent.

When `kit` kept your edited draft it saved the fresh one as `kit/requests.suggested/<id>.md`. If that file differs from
`kit/requests/<id>.md`, `kit-check` adds `edited draft differs from the fresh draft in requests.suggested/` to that
agency's row, marks the row not ok (exit 3, as for a placeholder) and lists: `## ` scope headings only in the fresh
draft or only in your edited draft, whether the date-range, fee-cap and law-package-status lines differ, and the
rule-citation lines (`- <rule_id>: ...`) only in one of the two. `--json` adds a `suggested_differs` object to that row
only. Compare with `diff -u -- kit/requests/<id>.md kit/requests.suggested/<id>.md` from the campaign directory
(`kit-check` never runs it). This is a prompt to review, not a verdict that your edit is wrong; nothing is merged or
overwritten. A suggestion that is a symlink, a directory or another non-regular file, or cannot be read, is named and
never followed. Without a suggestion the output is unchanged. `kit-sent` and `kit-copy` gates were not expanded.

To copy a draft out, `kit-copy --directory D --agency ID --for portal|email|mail [--json]` writes only the sendable part,
the Subject line and the request text after it, to `kit/outgoing/ID.txt` (with its `characters` count, to compare a
portal's limit) or an unsent `kit/outgoing/ID.eml` addressed to the verified `records_email` (no From or Date; your
mail client adds them). The banner, `To:`/`Agency:` lines, `Statutory context` and `Operator checklist` are left out:
they are notes for you, not for the agency. Unfilled `[placeholders]` in that part, or (email) no verified address,
stop with exit 3. Nothing is sent and `kit/sent.json` is untouched; record the send yourself with `kit-sent`.

To post a request, `kit-copy --directory D --agency ID --for mail [--json]` writes `kit/outgoing/ID.letter.html`, a
printable letter, and `kit/outgoing/ID.letter.txt`, its plain-text twin: the requester block, today's date, the
verified postal address, then the `Subject:` line and the sendable text exactly as `portal` copies it, signature
included (the repeated return address is intended). The HTML is inert escaped text with inline print styles: no
scripts, links, images, forms or external loads, and nothing opens a print dialog. It needs `requester` in
`kit/request-settings.json`, a verified postal address, and a draft that passes `kit-check` for that agency in full
(including the `To:` line and the parts left out of the copy; a regenerated draft for a verified agency with a posted
route already names it, see above); other agencies' drafts do not matter. Anything
missing stops with exit 3 before a file is written. The two files are written one after the other, not as a pair: if
the second write fails, the first may already be the new letter; the command says so, and running it again rewrites both.
Review, print and post the letter yourself, then record it with the unchanged `kit-sent --channel mail`. Nothing is
printed or sent and `kit/sent.json` is untouched.

After you send a draft yourself, record it (nothing is sent by the tool):

    python3 -m campaign_tool kit-sent --directory ../my-campaign --agency ca-example-police --date 2026-10-01 --channel email

`--channel` is `email`, `portal` or `mail`; the date must be a real day between 2000-01-01 and today. `kit/sent.json`
keeps the date, channel and SHA-256 of `requests/<agency_id>.md`. A second record is refused; `--replace` corrects
one with no steps. `kit-status --directory D` lists each request with `determination_due` and `with_extension` from
the law package (`null` when it is missing) and flags drafts edited since sending. Dates are provisional reminders:
the period runs from the agency's receipt and holidays are not modelled. `kit` re-runs never touch `sent.json`.
The JSON it prints is described by `schemas/kit-status.schema.json`; `tests/test_kit_json_contracts.py` fails when the keys drift.

### Recording the day the agency received the request

By default the provisional determination and extended dates count from the day you sent the request. When you have
evidence of the day the agency actually received it, record that day and the evidence, as a pair:

    python3 -m campaign_tool kit-sent ... --received-on 2026-10-03 --receipt-evidence portal
    python3 -m campaign_tool kit-received --directory ../my-campaign --agency ca-example-police --on 2026-10-03 --evidence portal

`kit-received` works on a request already recorded (including one from `kit-sent-import`) and takes `--request NAME`
(default `main`) and `--replace`. The evidence is exactly one of `portal` (a portal receipt or timestamp, whatever
channel the request used), `acknowledgement` (the agency's own written acknowledgement), `return_receipt` (a mail or
courier delivery receipt) or `other`. You state it; the tool never checks it. The date may equal the sent day or today
but not come before the sent day or after today. One value is kept: a second needs `--replace`, even when identical.
`kit-sent --replace` keeps a stored receipt and refuses a corrected sent day later than it; give both receipt flags to
correct them together. There is no way to delete a receipt.

Only a request WITH a receipt changes: `kit-status`, `kit-due`, `kit-followup`, `kit-calendar` and `today` count its
dates from the receipt and add the note `counted from receipt D (KIND)` to its rows, reminders, calendar events and
follow-up draft (JSON adds `received_on` and `receipt_evidence` for that request only). A request without a receipt
prints exactly what it did before. An agency's own stated production date, next installment and download expiry are
never moved. Recording `kit-response --kind acknowledged` is a response step and never sets the receipt. `kit-timing`
is unchanged. All of these dates stay provisional reminders, never legal deadlines; nothing is sent.

### Several requests to one agency

A second or narrowed request to an agency you already recorded gets its own name: draft it yourself in
`kit/requests/<agency_id>--<name>.md`, then record it with `kit-sent ... --request <name>`. Names are 1-40 characters
`a-z`, `0-9` or `-`, starting with a letter or digit; the first request is `main` (the default, drafted in
`requests/<agency_id>.md`). Each request keeps its own date, channel, draft SHA-256 and steps, and `--replace` only
touches the named one. In `kit/sent.json` a named request is keyed `<agency_id>#<name>`; files written before this
change read unchanged, every entry being request `main` (a `#main` key is refused as malformed). `kit-status` and
`kit-due` print `request` on every row and take `--request NAME` to list only that name; `kit-response --request NAME`
records a step on it. A name with no recorded request is refused (`no sent request <agency>/<name>`).

Record what the agency did, and when, with `kit-response --directory D --agency ID --date YYYY-MM-DD --kind K
[--note TEXT]`. `K` is `acknowledged`, `extension`, `determination`, `produced`, `denied` or `closed`. Dates must
not be before the send date or the previous step, or in the future. `acknowledged` is first and once only; `extension`
is once only and not after `determination`, `produced` or `denied`. `determination` and `denied` are once each;
`produced` may repeat. `closed` needs an earlier step, and nothing follows it. The note is plain text (no `<`, `>` or
control characters, at most 200 characters); never paste record contents. `kit-status` then shows `extended`,
`closed` and `last_step`, and `determination_due` uses the extension once one is recorded.

When the agency quotes a fee, record it with `--kind fee_estimate --amount USD` (for example `245.50`: digits with
at most two decimals, 0 to 100000; no sign, commas or exponent). `--amount` is required for `fee_estimate` and refused
for any other kind; it is stored as whole `amount_cents`, and estimates may repeat. `kit-status` shows each request's
latest estimate (latest date, then the later recorded step) as `fee_estimate`, and `over_fee_cap` compares it with
`fee_cap_usd` in `kit/request-settings.json` (`null` without a cap or an estimate; equal is not over). An estimate over
the cap also prints `<agency>: fee estimate $245.50 is over your fee cap: decide before paying` on stderr. The check is
a reminder, not advice: the tool never pays, replies to or contacts the agency.

`kit-fees --directory D [--csv FILE]` is the one read-only fee report over every recorded request: only requests with a
recorded estimate appear (latest estimate, `fee_state` `unpaid`, `paid` or `waived`, `fee_paid` when the payment step
carried an amount, `over_fee_cap`), unpaid first, plus exact-cents totals (`quoted`, `paid`, `unpaid` and a `counts`
object), taken from the stored `amount_cents` with the same rules `kit-status` uses. `--csv` writes the rows and a final
`TOTAL` row to a new private file (0600, never overwritten, refused inside `public/` or `content/`, spreadsheet-formula
safe). The amounts are what the organizer recorded: not an invoice, not legal advice, and nothing is paid or sent.

`kit-due --directory D [--as-of YYYY-MM-DD]` (default today) lists requests still waiting on the agency under `open`,
sorted by provisional `due` date (extended when recorded), with `days` left (negative once past) and `state`
`past_due`, `due_today` or `upcoming`. Requests with a `determination`, `produced` or `denied` step appear under
`answered`; closed requests are left out. Without a law package `state` is `no_law_package`. It exits 3 when any
request is past due, else 0. These are reminders, not lateness conclusions.
The JSON it prints is described by `schemas/kit-due.schema.json`; `tests/test_kit_json_contracts.py` fails when the keys or `state` values drift.

`kit-followup --directory D --agency ID [--as-of YYYY-MM-DD] [--replace]` (as-of not after today) writes a private
draft `kit/followups/<agency_id>-<as_of>.md` (0600, folder 0700) for a request that `kit-due` lists as `past_due`. It
cites the send date, channel, provisional determination date, the records law name and citation and, unless the law
package is `reviewed`, its status. It refuses a request never recorded, answered (`determination`, `produced`,
`denied`), closed or not yet past due, and when there is no law package. An existing draft is kept unless `--replace`;
symlinks are refused. Nothing is sent and `sent.json` is not changed: send it yourself after review.

`kit-denial-reply --directory D --agency ID [--rule RULE_ID]... [--replace]` writes a private draft
`kit/followups/<agency_id>-denial-<denied date>.md` (0600, folder 0700) for a request with a recorded `denied` step
(the latest one when there are several). It asks which exemption the agency relied on, for the non-exempt parts with
only the exempt portions redacted, and for a written explanation; fill in `[EDIT ME: what you asked for]` and `[name]`.
The tool never picks an exemption. Each `--rule` you name must be a `rule_id` in the law package in force on the denial
date; it is cited as `RULE_ID (law package status: draft): <the package's citation>`. Unknown, repeated or out-of-force
ids, `--rule` without a law package, and a request without a `denied` step are refused and nothing is written. Without
`--rule` the draft has no citations. An existing draft is kept unless `--replace`; symlinks are refused. Nothing is sent
and `sent.json` is not changed. The draft is not legal advice.

`kit-clarify-reply --directory D --agency ID [--request NAME] [--scope ID ...] [--date-range FROM:TO] [--replace]`
(for example `--agency ID` for the main request, or `--agency ID --request contract-2026` for a named one) writes a
private draft `kit/followups/<agency_id>[--<request>]-clarify-<today>.md` (0600, folder 0700) answering the agency's
open `clarification_requested` step: the reference and portal lines, `On D you asked us to clarify or narrow this
request`, the request re-rendered from the law package with your request settings (requester block, fee cap) for the
scopes you keep, and a `No longer requested` list naming every scope the sent request asked for that you left out
(`None — all originally asked scopes are retained.` when none), so nothing is dropped silently. Without `--scope` all
originally asked scopes are kept; only scopes recorded as asked by that sent request can be chosen (`--scope` takes
several ids; unknown, unasked, repeated or empty selections are refused). `--date-range` is two real `YYYY-MM-DD` dates
(FROM not after TO, neither after today); without it the range comes from `request-settings.json`, else the draft's
visible placeholder. The original range is not stored, so check any narrowing against what was actually sent. It is
refused when no clarification is open (never asked, or already clarified, closed or withdrawn). An existing draft is
kept unless `--replace`; symlinks and non-file targets are refused. The draft ends with the `kit-response ... --kind
clarified` command: run it only after you have sent the reply yourself, with `--date` set to the day you sent it.
Nothing is sent, and `sent.json`, the recorded scopes and the original draft hash are not changed.

`kit-scope-reply --directory D --agency ID [--request NAME] [--replace]` (default request `main`) writes a private draft
`kit/followups/<stem>-scopes-<today>.md` (0600, folder 0700; `<stem>` is the agency id, or `<agency>--<name>` for a named
request) for a request with recorded scopes. One numbered paragraph per asked scope, named by its law-package title and in
asked order: a scope whose latest recorded answer is `no_records_stated` (asks which offices, systems and accounts were
searched, including any kept for the agency by a contractor or vendor, and whether such records existed earlier and under
which retention schedule they were removed), and a scope with no recorded answer once a `produced` or `determination`
step exists (asks whether records exist and when they will be produced). Scopes last answered `produced`,
`partly_produced` or `withheld` are only listed as answered. The questions never say a search was inadequate. Refused,
with nothing written: scopes not recorded, no scope with a gap, no law package, or an unknown request. An existing draft is
kept unless `--replace`; symlinks are refused. Nothing is sent and `sent.json` is not changed. Review and edit it yourself;
it is not legal advice.

On a `produced` step, link the ingested records the agency sent with `--record PREFIX` (repeatable, at most 200):
at least 12 lowercase hex of each record's SHA-256 in the private ledger. Every prefix must match exactly one
ingested record and none may already be linked to the request; otherwise nothing is written. Full hashes stay in
private `kit/sent.json`; `kit-response` and `kit-status` print 12-character prefixes only (`records`,
`record_prefixes`, at most 50 then `more`). Which records an agency produced is never published.

`kit-coverage --directory D [--csv]` prints one row per selected agency's main request in `kit/agencies.json`, plus one
row for each of its recorded named requests (`--request` names): whether the request's draft exists (a regular file, not
a symlink), sent date and channel, last response step, distinct records linked on `produced` steps, `content/sources.json`
entries and published findings for the agency, then the `request` name and its `owner` (`kit-assign`; blank when
unassigned). Unassigned requests come first, unsent ones before sent ones, then a totals line (selected, drafted, sent,
answered, with records, with findings, requests for agencies no longer selected, fee unpaid, `requests` and
`unassigned_requests`). The agency totals count each agency once however many requests it has; `requests` and
`unassigned_requests` count rows, and sources and findings are per agency, so they repeat on each of its rows. Without
`content/` sources and findings are 0. `--csv` writes RFC 4180 CSV to stdout;
cells starting with `=`, `+`, `-` or `@` get a leading `'`. It writes nothing and sends nothing.

### Track requests in a spreadsheet

`kit-status --directory D --csv FILE` writes the same requests as `kit-status`, in the same order, to a new private
spreadsheet file and prints `{"written", "rows", "law_package_status", "note"}`. Columns: `agency_id, name, sent_on,
channel, provisional_determination_due, provisional_with_extension, extended, closed, steps, last_step_on,
last_step_kind, linked_records, draft_changed_since_sent, fee_estimate, fee_state, fee_paid, request_url, owner`; `yes`/`no` for true/false and an empty cell for an unknown
value (for example both due columns when there is no law package). Record hashes are not exported, only the count.
The file is created with mode 0600 and is never overwritten: `FILE` must end in `.csv`, its folder must exist, and it
must not exist yet (a symlink counts as existing) or be inside the campaign's `public/` or `content/` folder (also
through a symlinked folder); each is refused by name and nothing is written. A cell starting with `=`, `+`, `-`, `@`,
a tab or a carriage return gets a leading `'` so a spreadsheet does not run it as a formula. Dates are provisional
reminders; the period runs from the agency's receipt. Without `--csv` the JSON output is unchanged. The file stays on
this computer: nothing is sent, uploaded or published.

## Add many agencies from a spreadsheet

`kit-agency import --directory D --csv FILE [--check]` adds every row of one CSV to `kit/agencies.json` with the
same rules as `kit-agency add`, in one write. Export the sheet as UTF-8 CSV (a leading byte-order mark is fine). The
first row must be exactly `agency_id,name,kind` or `agency_id,name,kind,note`; 1-500 data rows, blank lines ignored,
at most 256 KiB. For example (fictional agencies):

```csv
agency_id,name,kind,note
ca-fictional-north-police,Fictional North Police,police,
ca-fictional-county-sheriff,Fictional County Sheriff,sheriff,Seen at a meeting
ca-fictional-county-board,Fictional County Board,county_board,
```

Each row is checked like `add` (id pattern and state prefix, a 3-120 character plain-text name, a known kind, a note
of at most 300 characters), and also refused when its id is already in `kit/agencies.json` or repeats an earlier
line of the file. If any row is bad nothing is written and each bad row is named by its line number in the file (the
header is line 1; the first 20 are listed, then a count). Otherwise all rows are appended in file order as selected,
`verified: false`, `added_by: organizer`, and the next kit run drafts requests for the law-enforcement ones. `--check`
runs every check and prints the same summary with `"written": false` without changing the file. Nothing is sent.

## Cross-check a saved Atlas of Surveillance export (private, read-only)

`kit-agency atlas --csv FILE --directory D [--json]` compares a CSV you saved by hand from the Atlas of Surveillance
with the county seed and `kit/agencies.json`. **An Atlas listing is a lead, not evidence**: it does not show that an
agency operates plate readers, and an agency the Atlas does not list is not shown to have none. Nothing is fetched,
written, selected or published, and the campaign folder is only read.

The file must be a regular UTF-8 file of at most 10 MiB (one leading byte-order mark is fine; a symlink, folder or
named pipe is refused). It needs the header columns `Agency`, `City`, `County`, `State` and `Technology`, spelled
exactly, in any order; a missing one is refused by name. A vendor column is optional and may be called `Company` or
`Vendor` (not both); without one the vendor shows as `vendor not supplied`, and a vendor is printed exactly as written.
Other columns are ignored and never printed. Blank rows are skipped. A malformed file (bad quoting, a row with the
wrong number of cells, a NUL, a control or bidirectional character in `Agency`, `City` or the vendor, an empty
`Agency`, `County`, `State` or `Technology`) is refused as a whole, naming the line and the rule but not the cell.

Only rows whose `Technology` is exactly `Automated License Plate Readers` (case and spacing ignored), whose `State`
is the campaign's two-letter code and whose `County` is the campaign's county (an optional trailing ` County` is
ignored) are used. `City` is shown, never used to match or exclude. There is no fuzzy matching and no acronym or
substring rule. An Atlas agency name matches a seed agency of that county when both are equal after lowercasing,
deleting apostrophes, turning other punctuation into spaces and removing trailing `department`, `dept` and `office`
words; nothing else is normalized (accents, `police`, `sheriff` and abbreviations are compared as written). Two seed
agencies with one normalized name are never guessed between: the row is listed as `ambiguous_seed_name` with the
candidate ids.

The report has three sections, each headed by the notice above: seed agencies the Atlas lists (marked `selected` or
`not selected` by the same agency id in `kit/agencies.json`, never by name, with the Atlas agency, vendor and CSV line);
Atlas rows that match no seed agency; and every selected agency in `kit/agencies.json`, organizer-added ones included,
that no Atlas row matched. The last list is not a finding about use. `--json` prints the same report as JSON and
nothing else. Use `kit-agency select` yourself if a lead is worth a request; this command never does.

## Agencies that receive our agencies' data, as request leads (private, read-only)

`kit-agency suggest --from-sharing --directory D` reads the recorded vendor-portal sharing lists
(`content/agencies.json`, `profile.sharing`, see `content agency-sharing`) and lists the agencies they name as receivers
as leads for requests of your own. **Receiving data is a reason to ask, not evidence of misuse**; it says nothing about
access, transfer or compliance. Nothing is selected, written, fetched or sent.

Matching: an in-state receiver is matched to the agency seed of the campaign's state (every county, not only yours) by
name, equal after case, whitespace and one trailing period are normalized; accents, abbreviations, other punctuation and
near spellings do not match. Exactly one seed agency makes a suggestion, with the seed name and id, the spellings the
lists use and how many of the campaign's agencies list it (each counted once, not per list entry). Spellings that reach
one seed agency are one suggestion. A name no seed agency has, or one that more than one seed agency has, is listed
separately as written, with candidate ids and no recommendation. Federal (kind `federal` or state `US`), private and
out-of-state listings are counted, not suggested or named; the counts are listings, not agencies.

Each suggestion shows one of three states by agency id in `kit/agencies.json`: `selected`, `not selected`, or
`not in kit`. `kit-agency select` only marks an agency already in the file, so a lead marked `not in kit` is added by a
separate step you take yourself (`kit-agency add` or `import`); this command never adds or selects anything. The seed
covers only the agencies it lists, so a receiver outside it appears under unmatched names. With no recorded sharing
list the report says `No recorded sharing lists.`

## Count a saved ALPR search log (private, read-only)

`kit-search-log --csv FILE --agency ID` counts a locally saved agency search log by month, organization and purpose category,
without printing any purpose text. See [SEARCH-LOG.md](SEARCH-LOG.md).
