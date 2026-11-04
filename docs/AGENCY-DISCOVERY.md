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
procedure. Community colleges, private universities and school police are not
seeded.

Coverage: all 58 counties. City lists are believed complete for San Luis
Obispo, Santa Barbara, Monterey, Santa Cruz, Ventura, Kern, Fresno,
Sacramento, San Diego, Alameda and Los Angeles counties; other counties list
the cities a contributor could name with confidence and omit the rest rather
than guess. `records_url`, `records_email`, `portal.url`, `place_fips`,
`flock_transparency_slug` and `muckrock_agency_id` are null until someone
verifies them, and `verified` is false everywhere. A wrong policing value is
a bug: open a PR against the table with the official page as the source.

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
agency's own domain), `likely` (a citation on a third-party or regional page), `no-evidence`
(every recorded link fetched, nothing found), `unreachable` (a recorded link could not be
fetched, the reason named), `not-checked` (no link recorded yet). `evidenced` with zero
citations is structurally impossible. A citation stores the URL, fetch time, HTTP status, the
SHA-256 of the fetched bytes, the quote, and the quote's byte offset - so the claim is
checkable later and a page change shows as a changed hash, not a silent drift. The quote is
rejected if it matches the shared email/phone/plate patterns, so a staff directory line can
never become a citation.

**Limits.** Fetching is https-only, one request per recorded link, no crawling, no off-host
redirects, a fixed polite delay, a per-run cap (`--limit`, default 25), a byte cap, a fixed
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
`city_council`, `district_attorney`, `chp`, and the opt-in `campus_police`.

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

To copy a draft out, `kit-copy --directory D --agency ID --for portal|email [--json]` writes only the sendable part,
the Subject line and the request text after it, to `kit/outgoing/ID.txt` (with its `characters` count, to compare a
portal's limit) or an unsent `kit/outgoing/ID.eml` addressed to the verified `records_email` (no From or Date; your
mail client adds them). The banner, `To:`/`Agency:` lines, `Statutory context` and `Operator checklist` are left out:
they are notes for you, not for the agency. Unfilled `[placeholders]` in that part, or (email) no verified address,
stop with exit 3. Nothing is sent and `kit/sent.json` is untouched; record the send yourself with `kit-sent`.

After you send a draft yourself, record it (nothing is sent by the tool):

    python3 -m campaign_tool kit-sent --directory ../my-campaign --agency ca-example-police --date 2026-10-01 --channel email

`--channel` is `email`, `portal` or `mail`; the date must be a real day between 2000-01-01 and today. `kit/sent.json`
keeps the date, channel and SHA-256 of `requests/<agency_id>.md`. A second record is refused; `--replace` corrects
one with no steps. `kit-status --directory D` lists each request with `determination_due` and `with_extension` from
the law package (`null` when it is missing) and flags drafts edited since sending. Dates are provisional reminders:
the period runs from the agency's receipt and holidays are not modelled. `kit` re-runs never touch `sent.json`.

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

`kit-due --directory D [--as-of YYYY-MM-DD]` (default today) lists requests still waiting on the agency under `open`,
sorted by provisional `due` date (extended when recorded), with `days` left (negative once past) and `state`
`past_due`, `due_today` or `upcoming`. Requests with a `determination`, `produced` or `denied` step appear under
`answered`; closed requests are left out. Without a law package `state` is `no_law_package`. It exits 3 when any
request is past due, else 0. These are reminders, not lateness conclusions.

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

On a `produced` step, link the ingested records the agency sent with `--record PREFIX` (repeatable, at most 200):
at least 12 lowercase hex of each record's SHA-256 in the private ledger. Every prefix must match exactly one
ingested record and none may already be linked to the request; otherwise nothing is written. Full hashes stay in
private `kit/sent.json`; `kit-response` and `kit-status` print 12-character prefixes only (`records`,
`record_prefixes`, at most 50 then `more`). Which records an agency produced is never published.

`kit-coverage --directory D [--csv]` prints one row per selected agency in `kit/agencies.json`: whether its draft
exists (a regular file, not a symlink), sent date and channel, last response step, distinct records linked on
`produced` steps, `content/sources.json` entries and published findings for the agency. Agencies with no sent request
come first, then a totals line (selected, drafted, sent, answered, with records, with findings, and requests for
agencies no longer selected). Without `content/` sources and findings are 0. `--csv` writes RFC 4180 CSV to stdout;
cells starting with `=`, `+`, `-` or `@` get a leading `'`. It writes nothing and sends nothing.

### Track requests in a spreadsheet

`kit-status --directory D --csv FILE` writes the same requests as `kit-status`, in the same order, to a new private
spreadsheet file and prints `{"written", "rows", "law_package_status", "note"}`. Columns: `agency_id, name, sent_on,
channel, provisional_determination_due, provisional_with_extension, extended, closed, steps, last_step_on,
last_step_kind, linked_records, draft_changed_since_sent`; `yes`/`no` for true/false and an empty cell for an unknown
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
