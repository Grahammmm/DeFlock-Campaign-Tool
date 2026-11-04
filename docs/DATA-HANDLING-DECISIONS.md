# Data handling: decisions for G

This is a private decision aid, not an adopted policy. Run the inventory only
on an explicitly admitted store. Its measurements require human review and do
not establish that a scan is complete. No real-store measurement is included here.

## Retention

**Question:** How long should originals, extracted text, mail, subscriber data
and signed links be kept, and what should deletion mean for receipt hashes?

**Measured facts:** The inventory reports area sizes and age ranges when run;
it does not establish a retention schedule. Hashes in a receipt can remain even
when the corresponding bytes are removed.

**Code today:** The store checker verifies object hashes against the ledger;
receipt relationships and hash deduplication are described in DATA-HANDLING.md.

**Options and consequences:** Keep material indefinitely for traceability, at
continued exposure and storage cost; set periods by data type, requiring an
attended process; or delete bytes while retaining a receipt and hash, preserving
some provenance but not the original content.

Recommendation (operator, not a decision): Consider periods by data type and
separately decide whether receipts and hashes should remain.

Decision: OPEN — G

## Access

**Question:** Who may read the store, and what should change if a second person joins?

**Measured facts:** The inventory reports numeric owners, modes and whether any
entry is group- or world-readable. It does not identify every effective reader,
including access granted through host controls or backups.

**Code today:** The CLI sets a restrictive umask; the store checker identifies
loose modes and its explicit fix mode can narrow them without following links.
Host accounts, access controls and disk encryption are separate matters.

**Options and consequences:** Restrict access to one operator for simplicity;
or authorize a second person with separate access, adding coordination and
revocation work. Either option needs an actual host-level review.

Recommendation (operator, not a decision): Consider named, minimum-necessary
access and review permissions before adding another person.

Decision: OPEN — G

## Device loss

**Question:** What should happen if a device holding the store or a backup is lost?

**Measured facts:** An inventory can describe the measured store, not confirm
whether a device or its backup is encrypted or who can recover it.

**Code today:** See BACKUP.md and FDF-01 for archive encryption and FDF-03 for
local scheduling. Encryption can limit exposure when keys remain protected; it
does not recover a missing device, guarantee an intact backup or replace keys.
The actual backup and device states require an attended check.

**Options and consequences:** Prepare an operator response and test recovery in
advance, requiring upkeep; or defer a procedure, leaving uncertainty after loss.

Recommendation (operator, not a decision): Consider a written response and a
reviewed recovery exercise that includes backup and key availability.

Decision: OPEN — G

## Subject requests

**Question:** What should the campaign do when a person asks what is held about
them, given its public-records sources and the queued df-corrections route?

**Measured facts:** Pattern counts are not an index of people, do not prove
identity and can miss data. No individual lookup is performed by this sheet.

**Code today:** The inventory is text-blind; df-corrections is queued, not an
implemented guarantee. Source and correction context require human review.

**Options and consequences:** Establish an attended identification and review
process linked to corrections or takedown requests, requiring staff time and
care with disclosure; or handle requests individually, with less consistency.

Recommendation (operator, not a decision): Consider an attended response path
with identity and disclosure safeguards and a link to the correction process.

Decision: OPEN — G
