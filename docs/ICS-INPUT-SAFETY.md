# iCalendar feed input safety

This page covers one guarantee of the `meetings --platform ics` importer (see
[iCalendar agenda feed import (CLI)](MEETINGS.md#icalendar-agenda-feed-import-cli)): a feed file that was cut short or
whose components are misnested is an **unreadable source**, never a short meeting list. A download that stops halfway,
or a hand-edited feed with a missing `END:` line, used to drop the unfinished event and return the rest. That shorter
list could then replace a good `kit/meetings.json`. Now the whole feed is refused.

## What a complete feed looks like

One `VCALENDAR`, each `BEGIN:` matched by an `END:` with the same name, and every `VEVENT` directly inside the calendar:

```ics
BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:example-1@example.gov
DTSTART:20261112T180000Z
SUMMARY:ALPR policy
END:VEVENT
END:VCALENDAR
```

The same feed cut off inside the event (no `END:VEVENT`, no `END:VCALENDAR`) is refused. No meeting is read from it,
including the complete events before the cut:

```ics
BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:example-1@example.gov
DTSTART:20261112T180000Z
SUMMARY:ALPR policy
```

## What is refused

The importer checks the component structure of the whole file before it reads any event. It refuses:

- an empty file, or a file with no `BEGIN:VCALENDAR`;
- a file that ends before its `END:VCALENDAR`, or any line after it (for example a second calendar pasted on the end);
- an `END:` that does not close the component that is open (`BEGIN:VALARM` closed by `END:VTODO`), or one with nothing
  open;
- a `VCALENDAR` inside the calendar, or a `VEVENT` anywhere other than directly in the calendar (inside a `VTIMEZONE`,
  a `VALARM`, another `VEVENT` or an unknown component);
- a `BEGIN:` or `END:` line with parameters, an empty name, or a name that is not ASCII letters, digits and hyphens;
- more than 64 levels of nesting, counting the calendar itself.

Every one of these gives the same single line, which never repeats text from the feed:

```text
feed has malformed iCalendar component structure; nothing was written
```

Names are read case-insensitively, folded lines are unfolded first, and a byte-order mark, blank lines and either CRLF or
LF line endings are accepted, as before. `VTIMEZONE` (with `STANDARD`/`DAYLIGHT`), `VALARM` and unknown components are
still allowed when balanced, and are still ignored: their `SUMMARY`, `DTSTART`, `UID` or `STATUS` never become an
event's. A complete calendar with no events is a valid, empty source.

This is a structure check only. It does not require `VERSION`, and does not tighten how dates, UIDs or other properties
are read; skip counts and the duplicate-UID revision rules are unchanged.

## What happens to `kit/meetings.json`

- **One feed** (`meetings --platform ics --from-ics FILE` or `--online`): the command stops with exit code 1 and the line
  above, prints no report, and writes nothing. An existing `kit/meetings.json` stays byte-for-byte the same, with the
  same file mode; if there was none, no `kit` folder or file is created.
- **Several sources** (`meeting_sources` in campaign.json): as for any other source failure, the bad feed is reported
  `failed` and its previous meetings are kept, marked `stale`. The other sources still update. The run is not a no-write
  run, and the bad feed never counts as an empty successful source.

## Limits

| Limit | Value |
| --- | --- |
| Feed size | 2 MiB |
| `VEVENT` blocks in one feed | 2,000 (counted before duplicate UIDs are merged) |
| Component nesting | 64 levels, including `VCALENDAR` |

The low-level `parse_events` helper in `campaign_tool/meetings_ics.py` is unchanged and still reads whatever events it
can; the check above is applied when the importer reads a feed.
