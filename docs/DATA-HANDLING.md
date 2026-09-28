# Data handling

Use an external campaign directory and keep private originals, mail, subscriber
data, signed links, credentials and analysis out of source control. The CLI sets
a restrictive umask and private storage permissions, but does not encrypt disk:
use appropriate host encryption, access controls and backups.

For each receipt record stable provider/message/attachment identity, original name,
source agency/request, receipt time and SHA-256. The starter captures only the
minimal source ID, name, time and bytes; full provenance relationships are planned.

Hash-deduplicate bytes, not correspondence context. A portal notice is not a
received file. An extraction success is not proof of full reading. Track unsupported,
expired, denied, redacted and partially extracted items explicitly.

Avoid reporting a blank export cell as a proven missing native value. Obtain schema,
related tables, redaction explanations and actual duty scope. Keep counterevidence.

Public exports require an exact artifact allowlist, privacy review, redistribution
rights, accessible source locators and correction linkage. Do not publish originals
merely because an agency supplied them. Public records can still expose private people.

Backups and portable exports must preserve originals, hashes, receipt relationships,
reviews and corrections. A website-only backup is not a records backup.
