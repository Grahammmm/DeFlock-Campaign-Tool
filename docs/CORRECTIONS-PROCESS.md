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

Out of scope: a web form, response tracking, automatic retraction, legal obligations.
