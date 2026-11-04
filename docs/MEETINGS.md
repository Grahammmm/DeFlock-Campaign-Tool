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
`python3 -m campaign_tool meetings-stage --directory D --id <meeting id> [--agency-id ID] [--finding ID ...] [--location TEXT] [--participation FILE] [--replace]`,
which writes `content/meetings/<id>.json` with a comment kit that cites published findings only;
nothing is fetched, and `--replace` regenerates an existing file.

The organizer picks the cited findings with `--finding ID` (repeatable, 1 to 3 ids): the kit cites
exactly those, in the order given, in both the two-minute comment and `## Sources`. An id that is not a
published finding in `content/findings/` (unknown, or a private draft), a repeated id or a fourth id is
refused before anything is written. Without `--finding` the kit cites at most 3 findings: those of the
meeting's `--agency-id` first, then the others, each group newest `event_date` first (ties by id), so the
choice never depends on file order. This replaced the earlier default of every published finding (only
the agency's when `--agency-id` was given) in load order; `--agency-id` now orders the default choice and
sets the meeting's `agency_id` but never filters `--finding` ids. The command prints the cited ids as
`findings` next to the `findings_cited` count.

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
A stated deadline also shows on the meetings list, the home page and the meeting's own page as
`Written comments due <weekday, month day, year, h:mm AM/PM (UTC±hh:mm)>` in the deadline's own offset, or as
`Written comment period closed` when it is at or before the build date at 00:00 UTC; cancelled meetings show neither.

`speaker_minutes` (optional, a whole number 1–10, or `null`) is the per-speaker time limit stated by the same
`source_url`. When it is set the kit adds `## Spoken comment (<m> minutes, about <130 × m> words)` after the
two-minute comment: the same greeting and closing with as many whole finding bullets as fit at 130 words a
minute (dropped from the end, never cut), then `Word count: <n> of <budget>.` If greeting and closing alone
do not fit, it says to use the first sentence and one ask. The tool never assumes a limit: without the
field there is no spoken section.

When `written_comment` is set (it is only ever stored verified, with the fields above), the kit adds
`## Written comment` after the asks: `Send by: <deadline as in How to take part>` (or `Send by: deadline not
stated by the source` when it is `null`), `How: <how>`, then a short letter `To the <body>,` with `Re: <agenda
item>` when there is one, a fixed paragraph, one bullet per cited finding (`- <title>: <summary> (<finding
URL, absolute when base_url is set>; sources <12-character hash prefixes>)` plus `(corrected <date>)` when
corrected), the three asks numbered, and `Sincerely,` `[name]` `[city]`. The letter is at most 400 words;
whole finding bullets are dropped from the end, never cut. If the body, agenda item or campaign name alone
would push it over 400 words, the section keeps `Send by:` and `How:` and says to write a short letter by
hand instead. The tool never adds a recipient, email or postal
address (any named in `how` appears only on the `How:` line) and never sends the letter.

### The one keyword list

The four keyword alternatives live only in `data/meeting-keywords.json`
(`{"schema_version": 1, "flags": "i", "alternatives": [...]}`). `campaign_tool/meetings.py` builds
`KEYWORDS` from it at import (joined with `|`, case-insensitive); a missing or malformed file raises
`MeetingsError` naming the file, with no built-in fallback. `workers/scripts/sync-data.mjs` generates
`workers/shared/src/generated/meeting-keywords.ts` (`MEETING_KEYWORDS_SOURCE`, `MEETING_KEYWORDS`,
`matchesMeetingKeywords`) for the Workers; nothing imports it yet.

To change a keyword: edit the JSON, run `npm run sync` in `workers/`, commit both files, and add the
new case to `tests/fixtures/meeting-keyword-parity.json`. `npm run sync:check` fails naming
`meeting-keywords.ts` while they disagree. Alternatives may use only literal text, `[ -]`-style classes
of literals, `\b` and `?`; the generator refuses anything else (`(?i)`, groups, `\p{..}`, backreferences)
by name and writes nothing. The generated matcher spells out Python's Unicode `\b` and its `i`/`İ`/`ı`
case rule so both sides give the same verdict (`tests/test_meeting_keywords.py`,
`workers/shared/test/meeting-keywords.test.ts`).

## Workspace Meetings screen

`POST /meetings` adds a meeting manually: `body_name`, `starts_at` (ISO-8601), optional
https `agenda_url`, `agenda_item`, `agency_id` and `relevance`
(`alpr_item | budget | consent_calendar | none`); `source` is `manual`. Meetings appear in the
content manifest and therefore on the public meetings page after the next build and deploy,
and in newsletter drafts as "Upcoming meetings".

### Row shape (migration `0004_meeting_identity.sql`)

A `meeting` row adds `date_state` (`known | unknown`), `source_url`, `item_id`, `title_sha256`,
`fetched_at`, `revision` (from 1) and `supersedes`. `starts_at` is NULL exactly when
`date_state = 'unknown'`; the schema refuses any other combination. Manual rows have no
`source_url`/`item_id`. A polled meeting is filed by `Repo.fileMeeting` once per
`(campaign, source_url, item_id, revision)` (a re-file of an unchanged item writes nothing and
returns `inserted: false`; a changed title is refused there). A changed title goes through
`Repo.fileMeetingRevision`, which inserts `revision + 1` with `supersedes` set to the previous
head and never edits or deletes the previous row; a stale (non-head) or unchanged revision is
refused. No method changes an identity column on an existing row. The screen lists undated
meetings first as `date unknown` (no comment kit until dated), shows a chain only by its head
with `revision N of <source_url> item <item_id>`, and newest first after that.

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

A cited finding with corrections shows `(corrected <latest date>)` after its bullet and, in Sources,
a link to `<finding link>#corrections`; the summary is always the current text. `doctor` lists staged kits
that predate a finding's latest correction as `stale_comment_kits`; re-stage them with `meetings-stage --id <id> --replace`.

## Printing a meeting pack

`python3 -m campaign_tool meeting-pack --directory D --id M` reads a staged meeting
and writes private `kit/meeting-packs/M/handout.html`, `comment.md` and `sources.csv`.
Open the handout locally and print on letter paper; the comment is the staged text verbatim.
Only published findings are included (newest first, five by default; `--max-findings 1..10`).
Sources use 12-character record fingerprints; a fingerprint is not proof of authorship.
Cancelled, past and stale kits stop: re-stage corrections with `meetings-stage --id M --replace`.
Use `--allow-past` for an archive, or `--replace` to regenerate an existing private pack.
Nothing is sent or published. Review the print preview before handing it over.

`meeting-pack --directory D --upcoming [--within-days N] [--replace]` builds the pack for every staged
meeting that starts from now up to (not including) N days ahead (default 14, `1..90`), earliest first,
through the same checks as `--id`. It prints `{window, built, skipped}`; each skipped meeting has the
reason (cancelled, stale kit, pack exists, invalid id, symlinked or unreadable file). Meetings outside
the window are not listed. Exit 0 when nothing was skipped, 3 when something needs attention.
`--allow-past` is refused with `--upcoming`. Nothing is sent or published.

## Recording a comment you submitted

After you have sent a written comment (or spoken one) yourself, record it with
`python3 -m campaign_tool meetings-comment-sent --directory D --id M --on YYYY-MM-DD --channel email|portal|mail|in_person [--file LETTER] [--replace]`.
It writes the private `kit/comments-sent.json` (`{"schema_version": 1, "comments": {"M": {"on", "channel",
"letter_sha256"?, "letter_bytes"?, "recorded_at"}}}`, mode 0600), prints the entry with a 12-character hash
prefix and `Recorded only. Nothing was sent.` The tool sends and submits nothing. With `--file` (a regular,
non-symlink file of at most 5 MiB) only the letter's SHA-256 and byte count are kept, never its text or path.
It refuses an unknown or cancelled meeting, an id with anything but letters, digits, dots, underscores or
hyphens, an `--on` that is in the future or after the meeting's own day, and a second record for the same
meeting unless `--replace` (the refusal names the first date). Once a meeting has a recorded comment `today`
no longer lists its `comment_deadline` item, and `meeting-pack` returns `"comment_sent": {"on", "channel"}`
(`null` when none) and prints `Comment recorded as submitted on <on> (<channel>).`; the three pack files are
unchanged. A malformed `kit/comments-sent.json` shows in `today` as one `unreadable` item (every deadline is
still listed), `meeting-pack` treats it as no record, and `meetings-comment-sent` refuses until it is fixed.

## Recording an outcome

After a staged meeting has happened, an organizer who read the result on the agency's own record
(minutes, vote page or video) records it with
`python3 -m campaign_tool meetings-outcome --directory D --id <meeting id> --result approved|denied|continued|amended|no_action|informational --on YYYY-MM-DD --source <https URL> --verified-by NAME [--vote TEXT] [--replace]`.
It adds `outcome` to `content/meetings/<id>.json` with `verified_at` set to today, prints it and
`Recorded only. Nothing was published.` It refuses a meeting that has not happened yet, a cancelled
meeting, a symlinked file, an `--on` date before the meeting or after today, a non-https source,
an empty `--verified-by`, and a `--vote` or name over 120 characters or with angle brackets or
control characters; an existing outcome is kept unless `--replace`. `meetings-stage --replace` keeps
the outcome. The site shows `Outcome: <label> on <date> (<vote>) · Official record · verified by <name>`
on past meetings and the meeting page, never on upcoming ones. The tool fetches no minutes and
says nothing about what an outcome means legally.

`--votes FILE` adds the per-member roll call read from the same `--source` record (votes have no URL of
their own). FILE is a regular, non-symlink UTF-8 CSV of at most 64 KiB with the header `name,vote` and
1 to 30 rows; `vote` is `yes`, `no`, `abstain`, `absent` or `recused`, and each name must match that
meeting's `agency_id` body in `content/officials.json` exactly (case-sensitive; Unicode NFC, runs of
spaces collapsed). Problems are listed together by file row (the header is row 1), for example
`votes row 3: unknown name "X" (not in officials.json for <agency_id>)` or `votes row 5: name listed twice`,
and nothing is written. Votes are never inferred from `--vote`. Changing votes needs `--replace`, and
`--replace` without `--votes` drops them (`votes: none`). The meeting page shows a "Recorded votes" table.

`--recording-url URL [--recording-start H:MM:SS]` adds the body's own official https video or audio of the meeting,
which the organizer has checked, to the same outcome. It is only ever a link: the tool never fetches, embeds,
downloads or rewrites it (the start time is shown as text, not added to the URL). There is no recording-only
command; a recording goes in with the outcome, or with `--replace` (which drops the old recording unless it is
given again). The URL must be https with no user name or password, at most 500 characters; the start is
`H:MM:SS` (minutes and seconds two digits) and at most `12:00:00`, and needs a URL
(`recording_start needs recording_url`). Bad values are refused naming `recording_url` or `recording_start`,
and `content-check` and `build` refuse the same mistakes in a hand-edited file. Past meetings in the meetings
list and the meeting page then end the outcome line with `Watch the discussion (from H:MM:SS)`; the wording
comes from the `meeting.recording` and `meeting.recording_from` labels. Outcomes without a recording render as before.

## Worker Legistar poll (inline, authored, undeployed)

`runScheduled` (`workers/workspace/src/cron.ts`) polls Legistar inline on the existing cron slots
through `pollLegistar` (`workers/workspace/src/legistar_poll.ts`). There is no second scheduler and no
job row, so the runner is not involved. The optional `LEGISTAR_FEEDS` setting accepts https
events feed URLs separated by spaces or commas, but a supported expanded collection source has
**not been established**. If the setting is absent, nothing is
fetched, the run summary records `not_configured`, and the dashboard shows
`legistar_poll: not configured`.
- Each feed gets one GET with no redirect followed, no credential and no cookie. Limits match the CLI:
  20 s, 4 MiB and at most 60 events. A feed that breaks a limit, is not https, has credentials in its
  URL, redirects, returns bad status/JSON, or has missing/null/non-array `EventItems` files nothing. It raises one deduplicated
  incident naming the feed by host and path, with no response body, and the slot's outcome becomes
  `partial`. The other feeds still run.
- An expanded event is kept only when an agenda item matches `matchesMeetingKeywords`, the generated
  copy of `data/meeting-keywords.json`. Only evaluated non-matches are counted as `discarded`.
- A kept event is filed through `fileMeeting`, using the source URL `<feed path>/<EventId>` and the
  item id `EventId`. A re-poll files nothing. Changed bytes file one revision, and the previous row is
  left unchanged. Only the body name, date, ids, URL and hash are stored. Item text, comments and
  attendee fields are not.
- An event with no usable date or time is filed `date_state: unknown` with `starts_at` NULL and is never
  guessed. A meeting already in the past is filed with its real date and counted `past`.

**Source-contract hold:** Legistar documents `EventItems=1` on the
[single-event endpoint](https://webapi.legistar.com/Help/Api/GET-v1-Client-Events-EventId_EventItems_AgendaNote_MinutesNote_EventItemAttachments),
not the [events collection](https://webapi.legistar.com/Help/Api/GET-v1-Client-Events).
The frozen one-request-per-feed design cannot discover events and then fetch their items.
The spec owner must authorize bounded detail requests or establish an expanded collection source
before this can be considered a supported live poll. No detail requests or new source are authorized here.


## Not done

No RSVP collection (the `rsvp_count` column is
reserved), no other agenda platforms (Granicus, PrimeGov, CivicClerk), and no calendar
feed. Each of those should keep the same keyword-lead, human-verify rule.
