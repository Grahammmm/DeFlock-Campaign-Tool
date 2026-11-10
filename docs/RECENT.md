# Recent changes report (`recent`)

`recent --directory D --since YYYY-MM-DD [--until YYYY-MM-DD] [--md FILE] [--json]` prints one private list of what
happened in a period, for a weekly organizing meeting. Each line is `DATE  what happened  → command that shows more`,
grouped under **Requests**, **Records**, **Findings** and **Meetings**, newest first; an area with nothing prints
`nothing recorded`. A fifth area, **Outreach**, is printed after them only when it has an event or an unreadable file,
so a campaign without statements, coverage or campaign events gets exactly the report it got before. It reads existing
files only and never writes, sends or publishes anything.

| Area | Listed, on the stored date |
| --- | --- |
| Requests | each request sent (`sent_on`) and each agency step recorded (`kit-response`), by agency name; each agency action in `content/agency-actions.json` (`on`) as `Agency action: <kind> — <agency>` |
| Records | records added to `content/sources.json` (`received_at`): a count and 12-character hash prefixes per day |
| Findings | published (`published_at`), corrected (each correction date), updated (each `updates` date, as `Finding updated: <title>`), withdrawn (withdrawal time) and each review receipt (`reviewed_at`), by finding title; each correction request in `private/correction-requests.json` received (`received_on`, as `Correction request received (open)`) and decided (`decided_on`, as `Correction request decided (accepted\|declined)`) |
| Meetings | each staged meeting on its `starts_at` day, each recorded outcome on its `verified_at` day and each comment submitted by hand in `kit/comments-sent.json` (`on`, as `Comment submitted by hand: <meeting id>`) |
| Outreach | each official statement in `content/statements.json` (`said_on`, as `Official statement: <official> — <body>`), each press item in `content/coverage.json` (`published_on`, as `Press coverage: <outlet>`) and each campaign event in `content/events.json` on the written day of its `starts_at` (as `Campaign event: <title>`) |

The agency in an agency action and the body in a statement are the agency's name in `content/agencies.json`, or its
id when that file has no entry for it. Updates come from published findings and withdrawn ones (a withdrawn copy wins
over a public copy left behind by an interrupted withdrawal), never from drafts. A campaign event is listed on the day
it starts; no announcement date is stored, so none is shown, and an event whose start day is after today is outside every window.
A correction request is listed as received with its first state (`open`) even when it has since been decided; its
decision is a separate line. Repeated entries (two updates or two requests on one day) are each listed.

A draft finding stores no date, so "drafted" is never listed. The same holds for any event whose date is missing or not
a real date: it is never guessed from file times, and the report ends with `N undated items skipped`.

**Window.** `--since` is required; `--until` defaults to today (local date). Both days are included. A bad date, a
`--since` after `--until` (or after today), a future `--until`, or a window over 366 days stops with exit code 2 naming
the flag.

**Private by construction.** Only agency names, finding titles, review role and decision, 12-character prefixes,
dates, the meeting result, action kinds, officials' names, outlets, campaign event titles, meeting ids and the state of
a correction request are shown: never request text, addresses, notes, reviewer names, correction or withdrawal reasons,
votes, full hashes, update notes, an action's wording or source, quotes, context notes, recording times, headlines,
links, event descriptions, venues or accessibility notes, how or when a comment was recorded, letter hashes or sizes,
or a correction request's id, finding, summary, decision note or requester.

**`--md FILE`** also writes the report as Markdown to a new file with mode 0600. It is refused (exit 2) when the path
resolves inside the campaign's `public/` or `content/` (symlinks included), when the file already exists (it is never
overwritten) or when its folder is missing. **`--json`** prints the events as a JSON list of
`{"on", "area", "what", "command"}` instead (notes about unreadable or undated items go to stderr).

**Unreadable files.** A file that cannot be read (for example a corrupt `kit/sent.json`) becomes one line in its area
(`kit/sent.json unreadable`); the rest of the report is still printed and the exit code is 3. Each of the stores above
is read on its own and checked by the same validator the rest of the tool uses: a link (even one that points inside the
campaign), folder, FIFO or other special file, a file over 2 MiB, bad UTF-8 or JSON, a wrong shape or an invalid date is
named the same way and hides nothing else. A finding whose `updates` list is invalid is named
(`content/findings/<id>.json unreadable`) while its other history is still listed; an unreadable
`content/agencies.json` is named once and ids are shown. The line never repeats the file's contents or the error.

**Unsafe input folders.** Before anything is collected, `recent` checks these campaign folders, parents first, without
following links: `kit/`, `content/`, `private/`, `content/findings/`, `content/meetings/`, `private/drafts/`,
`private/withdrawn/` and `private/reviews/`. A folder that is missing is simply empty. One that exists must be a real
folder: a symlink (even one that points inside the campaign, or dangles) or a regular file stops the run with
`Stopped: recent: <folder>/ must be a real directory, not a symlink or file` (or `cannot be inspected`), exit code 1,
no report on stdout and no `--md` file. This differs from an unreadable file above (exit 3, partial report). Nothing
below a refused folder is read, and the message never shows where a link points. Replace the link with a real campaign
folder yourself (an attended local copy or restore); `recent` never copies, unlinks, follows or repairs anything. The
campaign folder you select may itself be reached through a link; only the folders listed above are checked. The check
covers the folders as they are when the command starts, not a concurrent swap.
