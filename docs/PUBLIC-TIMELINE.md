# Public campaign timeline

`campaign_tool build` adds `public/timeline.html` to content-model sites.
The navigation links to Timeline; a configured `base_url` includes it in the sitemap.
The neutral starter without `content/` is unchanged.

The timeline uses only the already-loaded public content model:
- Source-library `received_at`, agency name and source title, linking to the library group.
- Published finding `published_at` and title, linking to the finding.
- Published finding correction dates, linking to its Corrections section.
- Meeting `starts_at` and body, linking to the existing meeting entry.

Dates preserve the field's calendar day, not a conversion to another time zone.
Events are newest-day-first, then records received, findings published, corrections,
and meetings, then link (and text for otherwise identical ties).
Months have headings and each event has a machine-readable date.
Meetings today or later say “Scheduled meeting”; past dates say only “Meeting”,
not that attendance or the meeting actually occurred. Cancelled meetings are omitted.
An unknown agency is “an agency”; absent source dates are never replaced with today
(the current content loader requires them). Empty content has an explicit empty state.

No requests, due dates, response dates, delay calculations or legal conclusions are added.
Request and response dates require a separate reviewed public-content addition first.
The timeline does not read the request kit, private records, ledger, mail or OCR.
It does not display record hashes, archive paths, email addresses or internal record IDs.
Existing public navigation and frame remain shared with the other site pages.
No new JavaScript, network access, publication approval or deployment is involved.

The existing loader still refuses drafts, attorney-review holds and malformed content
before replacing an existing public tree. Tests use fictional offline fixtures only;
passing tests establish structural behavior, not substantive human/privacy/legal review.
