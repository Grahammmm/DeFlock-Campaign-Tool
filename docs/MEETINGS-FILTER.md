# Meetings body filter

`meetings.html` can narrow both meeting lists (upcoming and past) to one governing body.

- **Threshold.** The filter exists only when the staged meetings (`content/meetings/*.json`) name two or more distinct
  `body` strings. The exact string is the identity: cancelled, past-only and agency-less meetings count; case variants are
  different bodies; a second regular-meeting schedule or a second agency alone does not enable it. Below the threshold
  nothing changes: no form, no script, byte-identical files.
- **What is added.** A hidden form with one select, a count line and an empty note per list, and `meetings/filter.js`
  (about 2 KB of ES5, same origin, `textContent` only; no network, storage or cookies). Each existing article is wrapped,
  unchanged, in a `div`. No other file changes: meeting pages, `meetings.ics`, `meetings.xml`, `data/meetings.json`, the
  CSS and the CSP stay as they were. The page is also valid without the script.
- **Without JavaScript** both complete lists show, with the existing empty wording, and the regular-meetings section is
  never filtered. Cancelled meetings stay in the past list, marked as before, and count in its total.
- **Counts.** Each list says `Showing M of N` for its own meetings. Regular schedules are not counted.
- **State.** The choice is kept in the address as `#body=<slug>`; `all` is omitted. Slugs are made from the body name
  (lower case, letters and digits, `-2`, `-3` when two names collide) and belong to this build only. An unknown value,
  a malformed escape or an unrelated anchor (`#past-meetings`) shows everything; the address is changed with
  `history.replaceState`, never by navigating.
- **Labels.** `js.meetings_label`, `js.meetings_body`, `js.meetings_all_bodies`, `js.meetings_count` (`{shown}`, `{total}`)
  and `js.meetings_empty` can be set in `content/labels.json`. They are read at build time into `meetings.html`; the script
  is the same for every site and `site-data.js` is not involved.
