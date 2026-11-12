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

The log is kept readable by `reply` and the optional counts, which read at most 4194304 bytes. An `add` or `decide`
whose complete new file would exceed that is refused as a whole with `update exceeds the 4194304-byte reply/count
reader limit; nothing was changed`; existing history is kept as it was, and nothing is sent or published.

### Drafting a reply (private, never sent)

`correction-request reply --directory DIR --id N` writes `kit/corrections/replies/N.md` (mode 0600; missing
`kit/`, `corrections/` and `replies/` folders are created 0700) from the request's recorded state only, and prints
`{"written": "kit/corrections/replies/N.md", "id": N, "state": ...}` then `Private draft only; nothing was sent or published.`
Every draft starts `DRAFT — NOT SENT. Review and edit before sending it yourself.` with generic
`[EDIT ME: add greeting outside this repository]` and `[EDIT ME: sign-off]` markers; the tool stores and adds no requester
name or address, and the log is never changed.

- **open:** acknowledges receipt on the recorded received date and says the request is being reviewed; no deadline,
  acceptance or takedown is promised.
- **accepted:** states the decision date and your recorded note (quoted, unchanged). The correction date is taken from the
  finding only when exactly one correction is dated from the received date through the decision date inclusive; with none or
  several the draft says `[EDIT ME: confirm the correction date]`. The log keeps no link to a correction, so every accepted
  draft also says `[EDIT ME: confirm the correction addressed this request]`.
- **declined:** says the request was not accepted and quotes the recorded note as the reason.

The finding URL (`base_url` + `/findings/<slug>.html`) appears only when the finding is currently published and
`site.json` sets `base_url`; otherwise the draft says `[EDIT ME: add the finding URL]`. An unknown id, a missing or invalid
log, unreadable public content, a link in the path or an existing draft is refused with nothing written; to change a kept
draft, edit it by hand. Review the draft and send it yourself outside this tool; a draft is not evidence of delivery
and not a new decision.

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
