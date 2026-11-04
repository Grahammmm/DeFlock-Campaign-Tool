# Recent changes report (`recent`)

`recent --directory D --since YYYY-MM-DD [--until YYYY-MM-DD] [--md FILE] [--json]` prints one private list of what
happened in a period, for a weekly organizing meeting. Each line is `DATE  what happened  → command that shows more`,
grouped under **Requests**, **Records**, **Findings** and **Meetings**, newest first; an area with nothing prints
`nothing recorded`. It reads existing files only and never writes, sends or publishes anything.

| Area | Listed, on the stored date |
| --- | --- |
| Requests | each request sent (`sent_on`) and each agency step recorded (`kit-response`), by agency name |
| Records | records added to `content/sources.json` (`received_at`): a count and 12-character hash prefixes per day |
| Findings | published (`published_at`), corrected (each correction date), withdrawn (withdrawal time) and each review receipt (`reviewed_at`), by finding title |
| Meetings | each staged meeting on its `starts_at` day and each recorded outcome on its `verified_at` day |

A draft finding stores no date, so "drafted" is never listed. The same holds for any event whose date is missing or not
a real date: it is never guessed from file times, and the report ends with `N undated items skipped`.

**Window.** `--since` is required; `--until` defaults to today (local date). Both days are included. A bad date, a
`--since` after `--until` (or after today), a future `--until`, or a window over 366 days stops with exit code 2 naming
the flag.

**Private by construction.** Only agency names, finding titles, review role and decision, 12-character prefixes,
dates and the meeting result are shown: never request text, addresses, notes, reviewer names, correction or
withdrawal reasons, votes or full hashes.

**`--md FILE`** also writes the report as Markdown to a new file with mode 0600. It is refused (exit 2) when the path
resolves inside the campaign's `public/` or `content/` (symlinks included), when the file already exists (it is never
overwritten) or when its folder is missing. **`--json`** prints the events as a JSON list of
`{"on", "area", "what", "command"}` instead (notes about unreadable or undated items go to stderr).

**Unreadable files.** A file that cannot be read (for example a corrupt `kit/sent.json`) becomes one line in its area
(`kit/sent.json unreadable`); the rest of the report is still printed and the exit code is 3.
