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

### Attachment links (never fetched)

Each matched meeting and matter carries `attachments` (`name`, `url`, at most 20, de-duplicated by URL) from Legistar
(`eventitems?Attachments=1`, `matters/<id>/attachments`) and `attachments_dropped`, the count of links refused. Only
`https://` links on `*.legistar.com`, `legistar.granicus.com` or `legistar<N>.granicus.com` are kept (no port or
credentials); the files themselves are never downloaded. The staged comment kit lists them under `## Read first`.
A changed attachment list alone is never `agenda_changed`. Matters without a recorded attachment response get none.

### Matters (`--matters`, Legistar only)

`meetings --matters` lists contracts, ordinances and resolutions that may not be on an upcoming
agenda yet. It runs **instead of** the events import and writes `kit/matters.json` (same
`--online`/`--from-json` rule; `--platform civicclerk` stops with `--matters is Legistar only`).
It reads `matters` filtered to `MatterLastModifiedUtc ge <--since>` (default today minus 365
days, newest first, at most 100). Matters whose file number, name or title match the ALPR
keywords are kept, newest `intro_date` first, with `id`, `file`, `title`, `type`, `status`,
`body`, `intro_date`, `agenda_date`, `passed_date` (`YYYY-MM-DD` or `null`), `url` (the official
`LegislationDetail.aspx` link, or `null` without a valid `MatterGuid`) and `matched_terms`. These
are keyword leads, not confirmation of ALPR use: verify on the official Legistar page before citing.

The CLI does not write to the hosted workspace. Organizers add the meetings they verified
on the Meetings screen, or stage one verified entry for the offline site build
([SITE-CONTENT.md](SITE-CONTENT.md)) with
`python3 -m campaign_tool meetings-stage --directory D --id <meeting id> [--agency-id ID] [--finding ID ...] [--location TEXT] [--participation FILE] [--ask T ...] [--elsewhere N ...] [--replace]`,
which writes `content/meetings/<id>.json` with a comment kit that cites published findings only;
nothing is fetched, and `--replace` regenerates an existing file.

**The campaign's own asks (DF2207-07).** By default the kit's `## Three asks` are the fixed relevance-based
ones, and campaigns that set nothing keep byte-identical kits. To use your own, set `site.json.comment_asks`
(1 to 3 plain-text strings of 1 to 200 characters) for every kit, or give `meetings-stage --ask T` (repeatable,
1 to 3) for that meeting only. `--ask` replaces `comment_asks` as a whole, and `comment_asks` replaces the
defaults; lists are never merged. The asks section is headed `## One ask`, `## Two asks` or `## Three asks` to
match the count, the written letter numbers the same asks in the same order, and with a verified `speaker_minutes`
the spoken version ends with `I ask you to:` and the numbered asks (finding bullets are dropped to fit, never an
ask; if greeting, closing and asks alone exceed the limit the spoken section says so and points to the asks
section). A letter over 400 words says `hand from the ask above.`, `the two asks above.` or `the three asks above.`
An empty, over-long, multi-line or control-character ask, a fourth ask or an empty list is refused, naming the
position (`--ask[2]`, `site.json.comment_asks[2]`), before anything is written. The override is not stored
anywhere but the staged kit: `--replace` without `--ask` returns to `comment_asks` or the defaults. Legal-claim
wording in your asks is flagged as an advisory `review_flags` note (printed on stderr, positions and rule names
only); the kit is still written ([LEGAL-CLAIM-GUARD.md](LEGAL-CLAIM-GUARD.md)).

**Decisions elsewhere in the kit (DF1107-06).** `meetings-stage --elsewhere N` (repeatable, at most 3, distinct) cites
decision N of `content/elsewhere.json`, counted 1-based in the file's STORED order (never the page's kind/date sorting).
An index of zero, below zero, past the end or given twice is refused by flag and value; with no `content/elsewhere.json`
the flag is refused. The kit gets a plain `Elsewhere:` paragraph after the cited findings in the two-minute, spoken and
written versions (a spoken or written version still needs its verified participation), one line each in the order
given: `PLACE, STATE: BODY, DECIDED_ON: as_stated (official source URL)`. The lines are never shortened or dropped: if
they do not fit the spoken or written word budget, staging is refused. The meeting stores full snapshots as
`cited_elsewhere`, so reordering `elsewhere.json` cannot change a staged kit. `build` and `content-check` find each
snapshot by place, body, `decided_on` and kind, and name one that no longer exists, or whose state, `as_stated` or
source URL changed (a new `checked_on` or `news_url` alone is fine); restage with `--replace`, which repairs only its own
meeting. `--replace` without `--elsewhere` removes the paragraph. These are decisions of other governments, not of this
campaign's agencies; the tool sends nothing. Without `--elsewhere` the kit and the report are byte-identical to before.

**Co-signed written comments (DF2207-14).** `meetings-stage --cosign SUPPORTER_ID` (repeatable, 1 to 30 unique ids, in
the order given) adds a bold `Co-signed by` line and one bullet per organization name after `[city]` in the
`## Written comment` letter, and stores the ids as `cosigners` on the staged meeting. The meeting page then shows
`Co-signed by N organizations` linking `/supporters.html`. Listing is not permission to co-sign: the organization's
private consent record needs its own grant (below), covering this exact meeting id, and the organization must still be
listed. `--cosign` also needs `--participation` with a verified `written_comment` (an agenda link or unverified
`kit/meetings.json` text is not a substitute), and refuses an unknown or repeated id, an organization without a grant
that covers this meeting, a cancelled meeting, and a letter that fell back to the over-400-word advisory. Names are
never added to the spoken comment or the asks. The roster is a separate bounded block, not letter body words: the
400-word cap counts the letter up to `[city]` only, and a roster never removes a finding bullet. Names are shown
literally (markup characters are backslash-escaped); the shared Markdown subset renders only `## ` headings, so the
roster heading is bold text rather than `### `. Without `--cosign` the kit and the report are byte-identical to before.

Grant or revoke from the campaign folder (this edits only the private `kit/supporters-consent.json`, mode 0600, and
never restages or publishes anything):

```
supporters consent --directory D --id ID --cosign any --confirmed-on YYYY-MM-DD --how email|letter|meeting|form [--correspondence PREFIX]
supporters consent --directory D --id ID --cosign MEETING_ID [MEETING_ID ...] --confirmed-on ... --how ...
supporters consent --directory D --id ID --clear-cosign
```

`any` must stand alone; a list holds 1 to 100 unique meeting ids, matched exactly (no prefixes). The grant has its own
`cosign_confirmed_on`, `cosign_how` and `cosign_correspondence`; the listing confirmation is never overwritten.
No grant, or a null, false, empty or malformed one, means no permission.

**Removal and repair.** `supporters remove` still succeeds when staged meetings list the id, but the next
`content-check` names every such meeting (`meetings/<id>.json.cosigners: <supporter>: ...`) and `build` refuses until it
is fixed. So does a narrowed or revoked grant, an edited listing, and a missing, duplicated or hand-edited roster block.
Repair one meeting with `meetings-stage --id M --replace` and the current `--cosign` flags (or none): the replace skips
only that meeting's old co-signer cross-check, resolves only the flags you give (yesterday's grant is never inherited)
and validates the new document fully before writing. Any other stale meeting still refuses, so repair each one, or list
the organization again and re-grant it. A co-signer count asserts neither attendance, delivery nor any legal
endorsement; the comment stays a draft you send by hand.

The organizer picks the cited findings with `--finding ID` (repeatable, 1 to 3 ids): the kit cites
exactly those, in the order given, in both the two-minute comment and `## Sources`. An id that is not a
published finding in `content/findings/` (unknown, or a private draft), a repeated id or a fourth id is
refused before anything is written. Without `--finding` the kit cites at most 3 findings: those of the
meeting's `--agency-id` first, then the others, each group newest `event_date` first (ties by id), so the
choice never depends on file order. This replaced the earlier default of every published finding (only
the agency's when `--agency-id` was given) in load order; `--agency-id` now orders the default choice and
sets the meeting's `agency_id` but never filters `--finding` ids. The command prints the cited ids as
`findings` next to the `findings_cited` count.

**Cited findings (DF2207-06).** The staged file also stores the ids the kit cites as `cited_findings` (1 to 3
unique ids, in the order the kit cites them), so each published finding page can list the staged meetings
that cite it. The ids come from the same selection that wrote the kit, never from the kit text. A kit that
cites no finding (the "No published findings yet" text) omits the field. `--replace` replaces the whole list
(or omits it when nothing is cited now) and keeps a recorded outcome as before. Meetings staged before this
field existed have no list: their kit text is never parsed, so they are not listed on any finding page. A
finding page's **Discussed at public meetings** section shows each citing meeting (body linked to its
`meetings/<id>.html`, recorded date and time in its own UTC offset, agenda item, and the outcome result when one
was recorded) newest first, ties by meeting id; cancelled and future staged meetings are listed too. A meeting
citing a finding that was withdrawn, never published or unknown never fails the build and lists nothing for it.
The list records what a staged kit cites; it is not proof of attendance, speaking, adoption or endorsement.

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
corrected), the asks numbered (the three defaults, or your own), and `Sincerely,` `[name]` `[city]`. The letter is at most 400 words;
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
- three asks, the first chosen by `relevance` (or your own 1 to 3 asks, see above),
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

The three files are one pack, written as one prepared generation: the tool encodes and writes all three into a
private `.M.new-<random>` directory beside the pack (0700 directory, 0600 files), then swaps that directory into
`kit/meeting-packs/M`. A handled error (a full disk, a permission error) never leaves a new handout beside an old
comment or sources sheet: a new pack is either complete or absent, and `--replace` moves the old directory to
`.M.old-<random>`, promotes the new one, and puts the old one back if promotion fails. `--replace` refuses a pack that
holds anything but the three expected regular files (an extra note, a subdirectory, a missing file, a link or a
special file) with `meeting pack has unexpected contents; move it aside before regenerating`; it never deletes your
files. If the old directory cannot be put back, the error names it (`previous pack retained at PATH`) and the next
run refuses with `meeting pack recovery required; inspect PATH before rebuilding`: check which generation you want,
then move or remove that `.M.old-*` directory yourself. If only the final removal of the old directory fails, the new
pack is complete and the result carries `warnings` naming the retained copy; remove it after checking the new pack
(`--replace` will not run until you do). An abandoned `.M.new-*` directory left by a killed process is never swept
automatically; inspect it and remove it by hand. Not promised: durability after power loss, that the pack is
invisible for the instant between the two renames, safety with two writers at once, or that `--upcoming` is
all-or-nothing (packs finished before a later failure stay, and the error still stops the run).

With `site.json.base_url` set, the handout prints a QR code beside each finding URL and one last link with a QR
code to the campaign site, after the findings and before the record explainer (also when there are no findings).
Codes are made offline by the tool itself (`campaign_tool/qr.py`, standard library only; byte mode, error
correction level M, versions 1-10, four-module quiet zone) and encode exactly the URL printed beside them; the
agenda link, the sources and the "N more on the site" line get none. A URL over 213 bytes stops that pack with
"QR URL exceeds 213-byte capacity (version 10, level M)." before anything is written, `--replace` included, so
an existing pack is left untouched; `--upcoming` lists that meeting as skipped with the same reason and builds
the others. Without `base_url` there is no QR code and the handout is byte-identical to before.
`comment.md` and `sources.csv` never change.

The handout follows the campaign's language: `<html lang>` is `site.json.language`, and its fixed headings and notes
(title, "What we found", the empty-state sentence, the source line, "corrected", the agenda link, "N more on the
site", the record-number explainer, and the classification and confidence wording) come from `content/labels.json`
(`handout.*`, `classification.*`, `confidence.*`; see `docs/SITE-CONTENT.md`, Labels). The tool translates nothing:
with no labels file the handout is the English it always was, byte for byte, and a Spanish `language` without
translated labels gets `lang="es"` over English text. A labels file the site would refuse (unknown key, foreign
placeholder, symlink) stops the pack with the same message and writes nothing. `comment.md` (the staged text
verbatim), the `not_legal_advice` text (`site.json`) and `sources.csv` (English column headers, machine-readable) never
change with the labels. `--upcoming` applies the same labels to every pack it builds.

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

### Confirming an outcome against the approved minutes

`python3 -m campaign_tool meetings-outcome --directory D --id <meeting id> --minutes-url <https URL> --minutes-approved-on YYYY-MM-DD [--replace]`
is the second mode of the same command. It attaches the body's approved official minutes to an outcome that is
already recorded, without retyping or replacing its result, votes, recording or verifier. Both flags are needed
together and cannot be mixed with `--result`, `--on`, `--source`, `--verified-by`, `--vote`, `--votes`,
`--recording-url` or `--recording-start`; without either minutes flag all four creation flags are required, as before.
The URL must be https with no user name or password, at most 500 characters. The date must be a real `YYYY-MM-DD` day
from the meeting's own date to today inclusive (the approval may come after the outcome was recorded, so it is not
compared with `--on`). It refuses a meeting with no recorded outcome (`minutes_url: needs a recorded outcome`), a
cancelled or not yet held one, a symlinked file and a malformed value, each naming the field, and writes nothing.
Minutes already attached are kept unless `--replace`, which replaces only that pair. It prints the whole outcome
then `Recorded only. Nothing was published.` The tool fetches nothing; the link is an organizer-attested pointer.

Caveat: an ordinary full `--replace` of the outcome (creation mode) drops the old minutes, as it drops omitted votes
and recording, so a changed result must be confirmed against the minutes again.

The meeting page, the meetings list and the agency page's "Past meetings" then show a second line under the outcome:
`Confirmed in the minutes approved on D` (a link to the minutes) when both fields are present, otherwise
`Recorded by the campaign; minutes not yet approved`. A meeting with no outcome shows neither. The wording comes
from the `meeting.minutes_confirmed` (with `{date}`), `meeting.minutes_pending` and `agency.past_meetings` labels.
`data/meetings.json` carries `minutes_url` and `minutes_approved_on` on the outcome, both or neither.
`today` lists a `minutes_pending` item for each outcome recorded more than 45 days ago (by `--on`) that has no
minutes, with the command to attach them; it only asks you to check whether the minutes are approved.

### Following a continued item to its stated date

`--result continued` may add one of `--continued-to MEETING_ID` (a staged meeting of exactly the same body, status
scheduled, whose own local date is after this meeting's) or `--continued-to-date YYYY-MM-DD` (a real calendar date after
the meeting, any future date allowed), as the official source states it. Neither is required, and the flags only
exist when creating or fully replacing an outcome: other results refuse them (`continued_to: requires result continued`,
`continued_to_date: requires result continued`), the two together are refused
(`--continued-to and --continued-to-date are mutually exclusive`), the minutes-only mode refuses both, and there is no
continuation-only update. A target id that is unknown, this meeting, cancelled, of another body (compared exactly:
case, spaces, agency) or not dated after the meeting is refused naming `meetings/<id>.outcome.continued_to` and
writes nothing. Dates are the meeting's local calendar dates; no time zone is converted, so a later hour of the same day
is not a later date. A full `--replace` drops an omitted continuation (so it repairs a reference that went bad); the
minutes-only `--replace` keeps it. `build` and `content-check` apply the same rules to hand-edited files.

A stated date is matched, not resolved: the meeting page of the source says `Continued to D`, linking the later
meeting when exactly one non-cancelled meeting of the same body is staged on that date; with none the plain date is
shown, and with several the plain date is followed by one `Meeting page (ID)` link each. Every matched later
meeting says `Continued from D` and links back, source by source in start order. A same-body, same-date match is a
follow-up cue only, never confirmation that the agenda item was heard or resolved; an explicit `--continued-to` is
the organizer's reading of the source. The wording comes from the `meeting.continued_to` and `meeting.continued_from`
labels (each with `{date}`), and the extra links use `meeting.page`. `today` lists a `continued_pending` item for a
`--continued-to-date` whose date has passed with no meeting of that body staged on it, with the command to open the
outcome's source and stage the follow-up (`meetings-stage`); a broken explicit reference or a duplicate meeting id
shows as an `unreadable` item instead. `data/meetings.json` carries `continued_to` or `continued_to_date` on the
outcome.

### Recording a staff direction and its report-back date

When the official record says the body told its staff to do something and report back (for example "return with a
draft usage policy"), record it with `--direction TEXT --return-by YYYY-MM-DD [--direction-source URL]`. Two modes:

- **With the outcome** (the four creation flags): any result may carry a direction. Only a direction with no return-by
  (or the reverse) is refused, and `--direction-source` alone refuses `direction_source: needs direction and return_by`.
- **Direction only** (`--directory`, `--id`, `--direction`, `--return-by`, no creation flag): adds the direction to an
  outcome already recorded (`direction: needs a recorded outcome`), on a held, non-cancelled meeting. The first direction
  needs no `--replace`; a second is refused (`direction already recorded; pass --replace to overwrite it`) and
  `--replace` replaces exactly the three direction fields, keeping the result, verifier, votes, recording, minutes,
  continuation and every other key as stored. `--vote`, `--votes`, `--recording-*` and `--continued-to*` are refused here
  (`direction-only mode cannot be combined with FLAG`), and the minutes mode refuses every direction flag
  (`minutes mode cannot be combined with direction flags`). A full `--replace` of the outcome drops an omitted direction
  (as it drops omitted votes); the minutes-only `--replace` keeps it. There is no way to clear a direction by itself.

`--direction` is one line of 1 to 200 plain-text characters, kept exactly as given (no leading or trailing space, no
angle brackets, control characters or tabs). `--return-by` is a real calendar day on or after the meeting's own date
(`return_by: must be a YYYY-MM-DD date on or after the meeting`); it may be before the outcome date, already past or far
ahead, because it is the date the source states, never a legal deadline. `--direction-source` is an https URL of at most
500 characters; omitted, it is the outcome's own `--source` (never the minutes URL). Nothing is fetched. The three
fields are stored together on the outcome (all present or all absent); `build` and `content-check` refuse a hand-edited
partial, null or malformed group. Success prints the whole updated outcome and `Recorded only. Nothing was published.`

The legal-claim guard is alert-only: wording that asserts a violation prints a `review-required:
meetings/ID.outcome.direction: legal-claim wording (RULES)` note on stderr when recording and on every `build`, scanning
the raw text. The note never changes the result or the exit code and never repeats the text.

The meeting page says `Staff were directed to: T (by D, as stated in SOURCE)` (SOURCE a plain link to the direction's
source) under the outcome, minutes and continuation lines; a meeting without a direction is byte-identical to before. The
wording is the `meeting.staff_direction` label (`{direction}`, `{date}`, `{source}`; Spanish draft in
`examples/labels/es.json`). `today` lists a `direction_pending` item (after `continued_pending`, due = the return-by)
when the return-by date is 14 days away or less, with no lower limit, unless a non-cancelled meeting of exactly the same
body, on a later local calendar date, has a recorded outcome dated on or before the day asked. A different body, the same
day, a later meeting with no outcome and a malformed file never suppress it. This is a workflow reminder only: a later
outcome does not prove staff did what was asked, its absence does not mean they did not, and no fulfillment status is
stored. `data/meetings.json` carries `direction`, `return_by` and `direction_source` on the outcome, all or none.

### Recording public-comment counts

When the official minutes or record state how many members of the public spoke on the item and how many written
comments were received, record them with `--spoke N`, `--written M` and `--comment-source URL`. Either count or both,
including an explicit `0`; a count the record does not state is simply left out (absent is never shown as zero). Each is
ASCII decimal digits only, 0 to 999 (leading zeros are read as the integer): `--spoke: must be a whole number from 0 to
999` (likewise `--written`) refuses a sign, fraction, exponent, space, non-ASCII digit or blank. Two modes:

- **With the outcome** (the four creation flags): the counts are part of the new outcome. The source defaults to the
  outcome's `--source`. `--comment-source` without a count is refused (`--comment-source needs --spoke or --written`)
  before anything is written.
- **Counts only** (`--directory`, `--id`, `--spoke` and/or `--written`, no creation flag): adds the counts to an outcome
  already recorded (`public comment counts: needs a recorded outcome`), on a held, non-cancelled meeting. The source
  defaults to the outcome's current minutes URL, else its `--source`. Only the three count keys are merged; the result,
  verifier, votes, recording, minutes, direction, continuation and every other key stay as stored. The first counts need
  no `--replace`; a second is refused (`public comment counts already recorded; pass --replace to overwrite them`) and
  `--replace` replaces the whole group: a count not given again is removed and the source is resolved anew. Counts-only
  mode refuses `--vote`, `--votes`, `--recording-*`, `--continued-to*` and the direction flags
  (`counts-only mode cannot be combined with FLAG`); minutes mode refuses the count flags; a partial set of creation
  flags still names what is missing. A full `--replace` of the outcome drops omitted counts (as it drops omitted votes);
  the minutes-only and direction-only `--replace` keep them. There is no way to clear counts by themselves.

The three keys are stored flat on the outcome (`spoke`, `written`, `comment_source_url`, https, at most 500 characters,
the source required with a count and not alone); `build` and `content-check` refuse a hand-edited null, boolean, float,
string, out-of-range or sourceless group by field name. Nothing is fetched. The meeting page says
`N people spoke and M written comments were received, as stated in SOURCE` (or `N people spoke, as stated in SOURCE`,
or `M written comments were received, as stated in SOURCE`; SOURCE a plain link) under the direction line; the numbers
are the record's literal totals, not a count of unique people, support or opposition, or comments sent by this campaign.
The wording is the `meeting.comments_both`, `meeting.comments_spoke` and `meeting.comments_written` labels (Spanish draft
in `examples/labels/es.json`). A meeting without counts is byte-identical to before; `today` is unchanged.
`data/meetings.json` carries `spoke`, `written` and `comment_source_url` on the outcome.

## Regular meeting schedules

`content/body-schedules.json` (optional, organizer-verified, never fetched) records when a body usually meets so that
`today` can suggest agenda-watch dates and `meetings.html` can say `Usually meets: ...`. The exact schema, exact body
naming (as staged meetings name the body), time-zone interpretation, the 365-day stale threshold and how to re-check
and update it are in [SITE-CONTENT.md](SITE-CONTENT.md#regular-meeting-schedules-contentbody-schedulesjson-optional).
An `Expected BODY meeting D: watch for the agenda` line is a prompt to look at the official agenda, not a scheduled
meeting: stage a meeting only from the official record.

## Meetings feed (`meetings.xml`)

When `content/meetings/` stages at least one meeting, `build` writes `public/meetings.xml`, one Atom document
(UTF-8, namespace `http://www.w3.org/2005/Atom`) with one entry per staged meeting. With no staged meeting there is
no file and no feed link, and a build without `content/` (the starter) is unchanged. The name `meetings.xml` is
reserved, so a public asset cannot take it; removing the last staged meeting removes the file on the next build.
It is Atom, not iCalendar: there is no `.ics` file and no RSVP.

- **Who is listed:** a scheduled meeting whose local start day is today or later, every cancelled meeting, and every
  meeting with a recorded outcome. A past scheduled meeting with no outcome is left out. The day split is the site's
  own (the date written in `starts_at`, the same one the meetings page uses). A staged list whose meetings are all
  left out still gets a feed with no entries, so a reader's subscription does not break.
- **Entry:** id `urn:deflock-meeting:<id>`; title `<body>: <agenda item>` (`Cancelled: ` first for a cancelled
  meeting); a link to the meeting page, an `agenda_url` link and the official outcome source link when they exist;
  a plain-text summary with the stored zoned start, the body, the agenda item, `Cancelled.`, the agenda link and,
  when an outcome is recorded, `Outcome: <result> on <date>.` and `Vote: <text>.`. It never carries the comment kit,
  who verified an outcome, roll-call names, participation contacts or recording links; those stay on the meeting page.
- **Dates come from content only.** An entry's `updated` is the outcome date (as midnight UTC) when an outcome is
  recorded, otherwise the stored start. The feed's `updated` is the latest of those in UTC (`2000-01-01T00:00:00+00:00`
  with no entries). The build time, file times and `verified_at` are never used, so the same content, config, labels and
  build day give byte-identical output. Entries are ordered newest `updated` first, equal instants by id. Because
  a cancellation has no recorded date in the public content, marking a meeting cancelled can change the entry without
  moving its `updated`. When a build day crosses a meeting's start day, a past meeting with no outcome leaves the
  feed, so bytes are identical only for builds on the same side of that day.
- **Links:** `/meetings.xml` is linked from the meetings page, the footer of every page and, as an
  `application/atom+xml` alternate, the head of the meetings page and each meeting page. It is not in the sitemap.
  Feed text stays English like the findings feed; the link labels `meeting.feed` and `title.meetings_feed` follow `labels.json`.

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
reserved), and no other agenda platforms (PrimeGov). Each of those should keep the same keyword-lead, human-verify rule.

## CivicClerk import (CLI)

```
python3 -m campaign_tool meetings --directory D --platform civicclerk --client <slug> (--online | --from-json REC) [--since YYYY-MM-DD] [--tz ZONE]
```

`CivicClerkClient` reads `https://<slug>.api.civicclerk.com/v1/Events` (`startDateTime ge <since>`, at most 60 events;
a `value` envelope or a bare list) and each event's agenda items from `/v1/Meetings/<agendaId>` (`items`, nested `childItems`,
`agendaObjectItemName` and `agendaObjectItemDescription`). Same keyword filter, `kit/meetings.json` shape and change report as
Legistar, with ids `civicclerk-<slug>-<event id>` and `source: "civicclerk"`. `startDateTime` (UTC) is shown in `--tz`; an agenda
link is kept only if `https://` on `civicclerk.com`; a cancelled mark in the event name or notice sets `status: "cancelled"`.

## Granicus import (CLI)

```
python3 -m campaign_tool meetings --directory D --platform granicus --client <name>.granicus.com --view-id N (--online | --from-xml FEED) [--all-events] [--since YYYY-MM-DD] [--tz ZONE]
```

Find the host and `view_id` in the address of your city's Granicus agenda page (`https://<name>.granicus.com/ViewPublisher.php?view_id=N`).
Only the agendas in the public feed `https://<host>/ViewPublisherRSS.php?view_id=N&mode=agendas` are read (host must be `*.granicus.com`,
`view_id` 1-9999; at most 60 events, the earliest by date, and the report says how many were cut). `--from-xml` reads a saved copy
(a regular file, not a symlink, at most 2 MiB); a feed with a `<!DOCTYPE` or `<!ENTITY` (in any encoding), or one that is not well-formed, is refused
and `kit/meetings.json` is left alone. Item title plus description is scanned with the same keyword list; the date comes from
`pubDate`, or from the title (`Mon D, YYYY`) when `pubDate` is missing or invalid, with no time of day, in `--tz`;
events before `--since` (default today) are skipped. Ids are `granicus-<name>-<event id>`; a cancelled mark in the
title sets `status: "cancelled"`; a link is kept only if `https://` on the same host. An item with no description has no text beyond its
title: it is dropped unless `--all-events`, which keeps it as `not_scanned: true` with no matches, meaning nobody has checked its agenda.

## CivicPlus AgendaCenter import (CLI)

```
python3 -m campaign_tool meetings --directory D --platform agendacenter --client <host> (--online | --from-xml FEED) [--all-events] [--since YYYY-MM-DD] [--tz ZONE]
```

The host is the domain of your city's AgendaCenter page (`https://<host>/AgendaCenter`), lowercase, with no port, user, path or IP address.
Only the public feed `https://<host>/RSSFeed.aspx?ModID=65&CID=All-0` is read, with the same safe reader, `--from-xml` limits and
DOCTYPE/ENTITY refusal as Granicus. **Only the feed text (title, category, description) is scanned**; the agenda PDF is recorded as
`agenda_url` (kept only if `https://` on the same host under `/AgendaCenter/`) and is never fetched. The meeting date comes from the
`_MMDDYYYY-` token in the item link, else from `pubDate`; an item with neither is dropped and counted (`dropped_no_date`). The body is the
item `category`, else the title before ` - `. Ids are `agendacenter-<first host label>-<number at the end of the link>` (a hash of the
link when there is none). An item with no description is dropped unless `--all-events`, which keeps it as `not_scanned: true`. Cancelled
and changes rules, the 60-event cap and the keyword-lead, verify-by-hand rule are the same as for Legistar.

## iCalendar agenda feed import (CLI)

```
python3 -m campaign_tool meetings --directory D --platform ics --feed-url https://... --body "City Council" (--online | --from-ics FILE) [--since YYYY-MM-DD] [--tz ZONE]
```

For a body that publishes only an `.ics` feed. `--feed-url` is an https URL (no user or password, at most 500 characters) and
`--body` is the governing body's name (plain text, at most 120; the feed's own calendar name is not trusted). `--client` is not
used and is refused. `--online` makes **one GET of the feed** and nothing else (at most 2 MiB); `--from-ics` reads a saved copy offline.
Output has the Legistar shape with `source: "ics"`, `id` `ics-<body slug>-<12 hex of sha256(UID)>` and `event_id` the UID.

Only `VEVENT`s are read (more than 2 000 refuse the run and write nothing). Times: `TZID=<IANA zone>` is kept in that zone, `...Z`
is converted to `--tz`, `VALUE=DATE` is date-only (`time_known: false`); in a repeated DST hour the first occurrence is used. A
floating time (no zone), an unknown `TZID`, a UID over 200 characters, a missing UID and an unreadable start are skipped and counted.
**Recurring events (`RRULE`, `RDATE`, `RECURRENCE-ID`) are skipped and counted, not expanded.** The report prints
`skipped` with each count. Only `SUMMARY` and `DESCRIPTION` are scanned for the ALPR keywords; `STATUS:CANCELLED` sets
`status: "cancelled"`. `agenda_url` is the `URL` property if `https://`, else the first `https://` `ATTACH`; attachment links are
recorded and **never fetched**. Changes (new, moved, cancelled) and the keyword-lead rule are the same as for Legistar.

## Calendar feed

A built site publishes `/meetings.ics` (RFC 5545), linked from the meetings page, so supporters can subscribe. It lists the
upcoming, not cancelled meetings only (same split as the page: the build day counts as upcoming); past and cancelled ones are
omitted. Each event has `UID:meeting-<id>@<host of base_url>`, a UTC `DTSTART` converted from the meeting's offset, the
agenda URL, location and agenda item when set, and a link back to the meeting on the page. The feed has no `DTEND`, and for
one build day it is byte-identical. The `.ics` extension is on the runner's upload allowlist (`text/calendar`).

## Private speaker plan

`meetings-speakers --directory D --id ID (--add LABEL --point POINT | --remove LABEL | --list)` keeps a private plan of
which volunteer label covers which point of a staged meeting's comment kit, in `kit/meetings/speakers/<id>.json` (mode 0600,
folders 0700). It is never written to `content/` or `public/`, and never reaches the site, an export or a meeting pack. It
sends nothing and changes no staging, request owner (`kit-assign`) or other store.

- A **point** is `finding:ID` (an id the staged kit cites, taken from the staged `cited_findings`) or `ask:N` (the Nth
  bullet of the kit's `One ask`/`Two asks`/`Three asks` section, counted from 1). Nothing else is accepted. A kit staged
  before findings were recorded with it is refused: re-stage the meeting.
- A **label** follows the `kit-assign` label rule: 1-40 characters, whitespace collapsed, case kept, no angle brackets,
  control characters, `@` or phone-like numbers. It is a team alias, not a verified identity and not consent. Refusals name
  the flag and never repeat the text.
- Each `--add` records one label/point pair. Repeating an exact pair changes nothing. A label may cover several points
  and several labels may cover one point (up to 200 pairs). `--remove LABEL` drops every pair of that label; an unknown
  label changes nothing.
- `--list` prints one JSON document: `id`, `speaker_minutes` (the organizer-verified per-speaker limit from the staged
  `participation`, or `null` when unknown: nothing is assumed), `points` with their `speakers`, `uncovered` points and
  `multiple_points` (labels on two or more points).
- A cancelled meeting (staged or in `kit/meetings.json`), one that has started or is past, a symlink anywhere on the path
  or an unknown point is refused.
- The plan stores a SHA-256 of the staged kit text. If the meeting is re-staged with a different kit, `--list` and `--add`
  refuse (`speaker plan is stale`) rather than silently re-map `ask:N`. Repair: `--remove` every label, then `--add` again.

```
campaign_tool meetings-speakers --directory campaign --id council-2099-01-05 --add "Volunteer A" --point ask:1
campaign_tool meetings-speakers --directory campaign --id council-2099-01-05 --list
```

## Several governing bodies

A county campaign can follow the county board and city councils in one run. List them in `campaign.json` (1-12 entries, Legistar only; `label` is plain text up to 80 characters and optional):

```
"meeting_sources": [{"platform": "legistar", "client": "examplecity", "label": "Example City Council"}]
```

`python3 -m campaign_tool meetings --directory D (--online | --from-json F) [--since YYYY-MM-DD]` (no `--client`) scans every entry into one `kit/meetings.json`: `client` is `multi`, `clients` lists the slugs, each meeting carries `client` and `source_label`, `sources` gives each body's `ok` or `failed` status, and `changes` is keyed by client. A body that fails (network, bad JSON) is named with its error, keeps its previous meetings marked `stale: true`, and the run exits 3 after writing; the other bodies are unaffected. A previous single-client file for a listed client is compared, not treated as another client. A bad `meeting_sources` entry stops the run naming its index. `--client X` keeps the single-body behaviour unchanged.
