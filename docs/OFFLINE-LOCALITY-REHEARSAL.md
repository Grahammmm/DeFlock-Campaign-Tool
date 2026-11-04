# Offline second-locality configuration rehearsal

Issue #7 / M6 requires a second California locality. The existing installed-wheel
smoke exercises Alameda alone; it does not compare an isolated pair of campaigns.
Run `python3 -B -m tools.rehearse_localities` from the public source checkout.
To reuse reviewed public branding instead, see [Second locality](SITE-CONTENT.md#second-locality).

The fixed synthetic San Luis Obispo and Alameda journeys reuse CLI initialization,
offline public agency seeds, kit generation, byte intake, doctor and scanned
starter generation. They use separate temporary campaign roots. Each receives
one synthetic original twice under one delivery identity and once under another:
one object and two retained receipts must result. Original bytes are compared.
Public hashes are reported without temporary/private paths; the two starter
index hashes must differ. Temporary fixtures are removed on completion.

Each locality then gets the fixed synthetic `content/` (two published findings,
one meeting, one agency) and a second checked build. Every public file of that
site must hold none of the synthetic source bytes, no `private/objects/` text
and no temporary-root path (`content_private_bytes_in_public`); each finding
page, `sources.html` and `feed.xml` must exist (`content_build_missing_page`),
and the `build --check` leak scan and internal-link check must then pass on the
finished site (`content_build_check_failed`).
Readiness holds are re-checked and the two content index hashes must differ.

Every campaign must retain draft law-package status, unverified jurisdiction,
disabled automatic sends and false production readiness. No model, portal,
mailbox, owner accounts, live settings, local policy or private corpus is used.
Seed agency contacts remain unverified. The pattern scan and synthetic canary
checks are not independent privacy review.

This is an offline configuration/preservation rehearsal, not a new-organizer
usability pilot, reviewed local-law package, complete records extraction or
live deployment. M6 remains open. A real organizer, verified local contacts and
policies, factual/legal/privacy reviews and separately approved activation are
still needed. Record source revision/interpreter and JSON output with any run;
a successful rehearsal never supplies those missing approvals.

## Real-agency rehearsal

The locality rehearsal above uses fixed synthetic fixtures. To see where one real
agency actually stands, the operator runs, attended and against the private store:
`python3 tools/rehearse_agency.py --store STORE --agency SLUG --out NEWDIR`.
It reads the store and writes one private receipt (`REHEARSAL.md` and
`rehearsal.json`, `records-rehearsal-v1`, review required) with one row per stage:
agency-verified, request-drafted, request-approved, request-sent,
response-received, preserved, extracted, ocr, catalogued, detected, reviewed and
published. Each row names its state (from a recorded field, never prose), the file
or tool it read, the next human step and the human gate it waits on (send, publish
or law). The verdict names the first break; OCR reads `not-wired` until df-ocr-live
lands. It sends nothing, publishes nothing and writes nothing under the store, so
it proves the chain's wiring and its gaps, not delivery.
