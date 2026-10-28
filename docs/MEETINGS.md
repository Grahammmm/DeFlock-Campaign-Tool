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
 "meetings": [{"id": "legistar-examplecity-1001", "body": "Example City Council",
               "starts_at": "2026-10-14T18:00:00", "agenda_url": "https://...",
               "agenda_item": "Item 7: ...", "relevance": "alpr_item", "source": "legistar",
               "event_id": 1001, "matched_terms": ["alpr", "flock", "license plate"], "matched_items": 2}],
 "note": "Agenda matches are keyword hits, not confirmation that ALPR will be discussed. ..."}
```

Network is used only with `--online`; `--from-json` replays recorded responses keyed by URL
path suffix (the fixture is synthetic, `example.invalid` hosts). Responses are bounded to
4 MB and 20 seconds; only `https://` agenda links are kept; angle brackets are stripped from
body and item text. Events without a parseable date are skipped. Keyword hits are leads, not
findings: verify the posted agenda before publishing a meeting.

The CLI does not write to the hosted workspace. Organizers add the meetings they verified
on the Meetings screen, or feed `kit/meetings.json` into `content/meetings.json` for the
offline site build ([SITE-CONTENT.md](SITE-CONTENT.md)).

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
