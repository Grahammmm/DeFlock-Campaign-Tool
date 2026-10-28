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

Coverage: all 58 counties. City lists are believed complete for San Luis
Obispo, Santa Barbara, Monterey, Santa Cruz, Ventura, Kern, Fresno,
Sacramento, San Diego, Alameda and Los Angeles counties; other counties list
the cities a contributor could name with confidence and omit the rest rather
than guess. `records_url`, `records_email`, `portal.url`, `place_fips`,
`flock_transparency_slug` and `muckrock_agency_id` are null until someone
verifies them, and `verified` is false everywhere. A wrong policing value is
a bug: open a PR against the table with the official page as the source.

### Filling `place_fips` (network, organizer-run)

`python3 tools/refresh_agency_seed_online.py` downloads the Census Gazetteer
place file, keeps California incorporated places, writes
`data/agencies/us-ca.place-fips.json`, and rebuilds the seed. The build script
merges that overlay whenever it exists, so the JSON stays a deterministic
function of the tables plus the overlay. Tests and CI never run it; commit the
overlay and the rebuilt seed together.

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

`kit` uses `campaign.json`'s `location_query` when present, otherwise
`<county> County`. The resolved location is written back to `campaign.json` as
`location` and `jurisdiction` (additive keys; older files still load).

`agencies_for(location, seed, include=...)` returns copies of the seed entries
with `suggested: true`. For a city match the city's own agencies come first,
then the county-wide ones; for a county match the county-wide agencies come
first, then every city's agencies in seed order. `--include` (repeatable)
restricts and orders kinds: `sheriff`, `police`, `county_board`,
`city_council`, `district_attorney`, `chp`.

## Online mode

`kit --online` is only consulted when the offline seed cannot resolve the
query. It calls the Census geocoder
(`geocoding.geo.census.gov/geocoder/geographies/onelineaddress`, 10-second
timeout, urllib, no key) and reads county and incorporated-place GEOIDs. The
agencies still come from the seed; if the geocoder names a city that the seed
does not list yet, `kit` stops and says so. Nothing else in the kit path uses
the network, and tests never do.

## What the kit writes

Under `<campaign>/kit/`:

| File | Purpose |
| --- | --- |
| `agencies.json` | Organizer-owned list, `selected: true` by default. Never overwritten once it exists. |
| `agencies.suggested.json` | Fresh suggestions on re-runs; `summary.json` reports the id diff count. |
| `requests/<agency_id>.md` | One combined draft per selected sheriff/police/CHP agency covering every request scope in the law package. Stale drafts for deselected agencies are removed. |
| `governing-bodies.json` | Boards of supervisors, city councils and district attorneys (no request draft). |
| `law.json` | Law-package summary, or `{"status": "missing"}` when none exists; drafts then come from `templates/records-request.md` with a banner. |
| `summary.json` | Counts, location, package status, next steps, `sent: false`. |
| `README.md` | What the folder is and that nothing was sent. |

`doctor` reports `agencies_selected` and `kit_built`.

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
