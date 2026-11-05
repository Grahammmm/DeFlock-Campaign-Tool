# Response review checklist

Private. For the organizer who reads `run-outputs/<run_id>/outcomes/response-items.json`
(schema `records-response-items-v1`) before any follow-up is written.

**This is a machine rating, not a legal finding.** Each asked item of a request is
read against the agency's response text with fixed rules (no model). A rating
says what the extracted text appears to say, with the exact quote and page. It
never says that an exemption is valid, that records exist or that the agency
broke a duty. Review every rating against the original document before a
follow-up is drafted. The us-ca law package is draft.

For every rating, first confirm the quote is on the page named and that the
asked item (`ask-N`) really is the one the sentence speaks to. If the ask was
split wrongly, fix the request's scope in the tracker and rerun; do not
follow up on a mis-split item.

## answered

1. Open the attachment or page quoted. Is the record itself there, or only the
   words "attached" or "enclosed"?
2. Does what was produced cover the whole ask (all dates, all systems, all
   vendors) or only part? Note the gap by name.
3. Is anything partly redacted? If so the item also needs the exemption steps below.

## exemption_cited

1. Read the quoted sentence. Which section does it name? Is the section real and
   does it exist as the agency wrote it?
2. Does the citation cover the whole record or only part of it? The agency may
   owe the non-exempt part.
3. Did the agency say which records are withheld and why, in enough detail to
   tell what exists? A bare section number is not a description.
4. Did it say whether the rest of the record was produced? Record the answer.

## withheld (no citation)

1. Find the quoted refusal. Is any ground stated anywhere else in the response
   (another page, the cover email)? If so rate it by hand as an exemption.
2. If none is stated, the follow-up asks for the legal basis for each withheld item.

## no_records_stated

1. Did the agency say a search was made, and of which systems or custodians?
2. Is it "we do not have" (it may exist elsewhere, for example with a vendor) or
   "none exists"? The wording matters for the next ask.
3. Do other documents from the same agency imply the record exists (a policy
   cites a log, a contract cites an audit)? Note the document and page.

## missing

1. `missing` means the asked item was **not mentioned** in the text read. It does
   not prove the records exist.
2. Search the attachments by hand for the item under other names.
3. Check whether the text was complete (see `text_unreviewed`).
4. If still absent, the follow-up asks the agency to confirm whether the item was
   produced, withheld or does not exist.

## unclear

1. Read every quote listed. Two cues on one ask (for example "attached" and
   "withheld") may be two different parts of the item, or a contradiction.
2. Rate it by hand; do not follow up until you have.
3. With flag `text_unreviewed`: the extraction was not human-reviewed or has no
   page numbers (a scan or image). Read the original pages first.

## Request-level flags

- `scope_unknown`: the tracker has no record of what was asked. Fix the tracker;
  the tool does not guess.
- `text_unreviewed`: see above.
- `unlinked_items` (top level): response documents that could not be tied to an
  agency and request. Join them by hand.

## Before a follow-up draft is accepted

Score each draft 0 or 1 on every line; any 0 on lines 1 or 2 stops it.

1. Every fact cited (item number, quote, date, section) matches the original.
2. Nothing is invented: no record, date, duty or agency statement that is not in
   the response or the law package.
3. Tone is polite and neutral; no accusation of bad faith.
4. Specific: it names the item numbers, the agency's own words and the record by name.
5. The ask is answerable: one clear request per item and a date to reply by.
6. Nothing is sent until the organizer approves it.
