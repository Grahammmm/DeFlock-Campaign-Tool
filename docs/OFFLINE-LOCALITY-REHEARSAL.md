# Offline second-locality configuration rehearsal

Issue #7 / M6 requires a second California locality. The existing installed-wheel
smoke exercises Alameda alone; it does not compare an isolated pair of campaigns.
Run `python3 -B -m tools.rehearse_localities` from the public source checkout.

The fixed synthetic San Luis Obispo and Alameda journeys reuse CLI initialization,
offline public agency seeds, kit generation, byte intake, doctor and scanned
starter generation. They use separate temporary campaign roots. Each receives
one synthetic original twice under one delivery identity and once under another:
one object and two retained receipts must result. Original bytes are compared.
Public hashes are reported without temporary/private paths; the two starter
index hashes must differ. Temporary fixtures are removed on completion.

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
