# Corrections and takedown process (organizer duties)

`corrections.html` opens with "How to ask for a correction or takedown". Every published
finding links to it ("Ask for a correction"). The text is fixed in
`campaign_tool/corrections_policy.py`; it is not editable from `site.json`. The site sends
nothing and stores nothing: there is no form, script, inbox or tracker.

## What the public text promises

- An organizer reviews every request against the cited records.
- A change is listed on the corrections page with its date.
- A request that is not accepted gets a reply saying why.
- A page reported as showing personal information about a private person is reviewed first.
- No deadline, no legal right and no guaranteed takedown is promised.

## Organizer duties behind it

1. Set `contact_email` in `content/site.json`. Without it the page says requests cannot be
   made through this site yet; set it before publishing.
2. Read requests that arrive at that address. Check the claim against the cited records, not
   against the sender's description.
3. Privacy first: if the request says a page shows a private person's personal information,
   review that page before the others and, if the claim holds, remove or redact it through
   the normal review gates before replying.
4. If the finding was wrong, add an entry to the finding's `corrections` list (`date`, `note`)
   and rebuild; the entry appears on the finding and on `corrections.html`. Do not silently
   edit the finding text.
5. If the request is not accepted, reply to the sender with the reason. Do not forward the
   sender's address or message anywhere public.
6. Keep request emails out of this repository and out of `public/`.

## Logging requests

Log each request privately with `correction-request add --finding ID --received D --summary T`
and later `decide --id N accepted|declined --on D --note T`; only `private/correction-requests.json`
(0600) is written. Record what was asked, never who asked: no names or contact details. `accepted`
is refused until the finding has a correction dated on or after the request (`finding correct`);
`doctor` names requests open over 14 days. Reply by hand; the tool sends and publishes nothing.

### Optional: publish quarterly counts

Off by default: the site build never opens the private log. To publish totals, set `content/site.json`
`"corrections": {"show_request_counts": true}` (omitted, `{}` or false mean off; anything else is refused by field name).
`corrections.html` then gains "Correction requests we received": one row per calendar quarter in which a request was
received, newest first, with Received, Accepted, Declined and Still open. Requests are counted in the quarter they were
received and by their current state, so a later decision changes the original quarter's row; these are not quarter-end
totals. Only these counts leave the log: no summaries, notes, dates, finding or request ids, or anything about who asked.
A missing log shows "No correction requests have been logged yet." A malformed, symlinked or unreadable log stops the build
with `private/correction-requests.json: cannot publish correction request counts; check the private log` and no value.
`content-check` validates the setting only and never reads the log; a build or preview with the flag on also validates it.
Small counts can reveal that a request exists; you choose whether to publish, and there is no anonymity guarantee or
minimum count.

Out of scope: a web form, automatic replies, automatic retraction, legal obligations.
