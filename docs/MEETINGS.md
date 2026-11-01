# Meetings

Find upcoming public meetings with license plate reader agenda items, record them, and build
a public-comment kit from published findings. Code: `campaign_tool/meetings.py`,
`workers/workspace/src/comment_kit.ts`, the Meetings screen in `views/screens.ts`.

## Legistar import (CLI)

```
python3 -m campaign_tool meetings --directory D --client <slug> --online [--since YYYY-MM-DD]
python3 -m campaign_tool meetings --directory D --client examplecity --from-json tests/fixtures/legistar-events.json
```

`LegistarClient` reads `https://webapi.legistar.com/v1/<client>/events` filtered to
`EventDate ge <since>` (default today, at most 60 events) and each event's `eventitems`.
`relevant_events` keeps events where an item title, matter name, file number or action text
matches `license plate`, `ALPR`, `Flock` or `surveillance` (case-insensitive) and writes
`kit/meetings.json`:

```json
{"schema_version": 1, "source": "legistar", "client": "examplecity", "generated": "...",
 "time_zone": "America/Los_Angeles",
 "meetings": [{"id": "legistar-examplecity-1001", "body": "Example City Council",
               "starts_at": "2026-10-14T18:00:00-07:00", "time_known": true, "agenda_url": "https://...",
               "agenda_item": "Item 7: ...", "relevance": "alpr_item", "source": "legistar",
               "event_id": 1001, "matched_terms": ["alpr", "flock", "license plate"], "matched_items": 2}],
 "note": "Agenda matches are keyword hits, not confirmation that ALPR will be discussed. ..."}
```

`starts_at` is the local wall time with the UTC offset of `--tz` (IANA name, default
`America/Los_Angeles`) for that date (DST-correct: `-07:00` in October, `-08:00` after the November
change); an unknown zone exits 2
(`meetings: unknown time zone ZONE`) before any fetch. A missing or unparseable `EventTime` gives
local midnight with `time_known: false` (verify the time on the agenda). An older kit without
offsets is compared by wall time, so the upgrade alone reports nothing `rescheduled`;
`meetings-stage` refuses such a kit (run `meetings` again). Content meetings need an offset or `Z`.

Network is used only with `--online`; `--from-json` replays recorded responses keyed by URL
path suffix (the fixture is synthetic, `example.invalid` hosts). Responses are bounded to
4 MB and 20 seconds; only `https://` agenda links are kept; angle brackets are stripped from
body and item text. Events without a parseable date are skipped. Keyword hits are leads, not
findings: verify the posted agenda before publishing a meeting.

Before writing, the CLI compares the new list with the previous `kit/meetings.json` and prints
`previous` (`none`, `unreadable`, `other_client` or `compared`) with change counts. When a
previous file exists, the new file gains `changes`: `added`, `removed`, `rescheduled`
(`id`, `from`, `to`), `agenda_changed` (item text or agenda link) and `past`, ids sorted. A
meeting that dropped out of the list is `past` when it was dated before `--since` (default
today) and `removed` otherwise. A missing, symlinked, unparseable or other-client previous file
is named and never blocks the write; a failed fetch leaves the old file untouched.

Every meeting carries `status`: `cancelled` when Legistar's `EventAgendaStatusName` is
`Cancelled`/`Canceled` (any case, trimmed) or `EventComment` contains the word `cancelled` or
`canceled`, otherwise `scheduled`. The CLI prints `cancelled_meetings`; `changes.cancelled` lists
ids that were scheduled (or had no `status`) in the previous file and are cancelled now, and a
cancelled meeting is never `agenda_changed`. `meetings-stage` refuses a cancelled meeting and
writes nothing; a content meeting may set `status` (`scheduled` default, or `cancelled`), and the
site never lists a cancelled meeting as upcoming: it stays under past meetings marked `Cancelled`.

The CLI does not write to the hosted workspace. Organizers add the meetings they verified
on the Meetings screen, or stage one verified entry for the offline site build
([SITE-CONTENT.md](SITE-CONTENT.md)) with
`python3 -m campaign_tool meetings-stage --directory D --id <meeting id> [--agency-id ID] [--location TEXT] [--participation FILE] [--replace]`,
which writes `content/meetings/<id>.json` with a comment kit that cites published findings only;
nothing is fetched, and `--replace` regenerates an existing file.

### Participation (organizer-verified)

`meetings-stage --participation FILE` adds the facts an organizer checked on the agency's own page.
The kit never guesses them: no default deadline, no model-filled value, nothing scraped.

```json
{"in_person": true, "remote": {"available": true, "how": "Zoom link on the posted agenda"},
 "written_comment": {"how": "Email the city clerk", "deadline": "2026-10-14T12:00:00-07:00"},
 "source_url": "https://example.invalid/council/agenda", "verified_by": "Synthetic Organizer",
 "verified_at": "2026-10-10"}
```

`null` (or an absent key) means "not stated by the source" and prints nothing. `source_url` (https),
`verified_by` (not empty) and `verified_at` (`YYYY-MM-DD`) are required; unknown keys are refused. The
deadline (or `null`) needs a UTC offset and must be before `starts_at`; `verified_at` must not be in the
future nor more than 30 days before the meeting; `how` is at most 200 characters with angle brackets
stripped. The file must be a regular file (no symlink) of at most 64 KB, and it is validated before
anything is written. The kit adds `## How to take part` after the asks: one line per stated fact, then
`Source: <url> (checked <date>)`. Without the flag the document has no `participation` and the section
says only `Check the posted agenda for how and when to comment; participation details were not verified.`

## Workspace Meetings screen

`POST /meetings` adds a meeting manually: `body_name`, `starts_at` (ISO-8601), optional
https `agenda_url`, `agenda_item`, `agency_id` and `relevance`
(`alpr_item | budget | consent_calendar | none`); `source` is `manual`. Meetings appear in the
content manifest and therefore on the public meetings page after the next build and deploy,
and in newsletter drafts as "Upcoming meetings".

## Comment kit

`POST /meetings/:id/comment-kit` generates Markdown from published findings only (each with
its publication path and source hashes) and stores it on the meeting row
(`comment_kit_md`). The Python twin `campaign_tool.meetings.comment_kit_md(meeting, findings,
campaign_name, base_url)` produces the same structure for the offline kit:

- a header with body and date,
- a two-minute comment with `[name]` and `[city]` placeholders and up to three findings
  (summary, classification, confidence),
- three asks, the first chosen by `relevance`,
- a source list linking each finding and its records by locator and hash prefix,
- the standing note that findings describe records as of the event date and are not legal
  advice.

With no published findings the kit says so and cites the records request instead. Kits
never include subscriber data, correspondence or unpublished analysis.

## Not done

No automatic Legistar polling from the Worker, no RSVP collection (the `rsvp_count` column is
reserved), no other agenda platforms (Granicus, PrimeGov, CivicClerk), and no calendar
feed. Each of those should keep the same keyword-lead, human-verify rule.
