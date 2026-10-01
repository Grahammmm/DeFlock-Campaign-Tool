# Security

This is an unvalidated development alpha, not a supported production security boundary.
Do not expose the local evidence ledger or create a public admin service from it.

Do not report vulnerabilities with credentials, private records or live exploit data
in a public issue. If GitHub private vulnerability reporting is available, use the
repository Security tab. Otherwise open a minimal issue asking for a private contact
without disclosing the vulnerability details.

Authentication of review issuers, encrypted backups, hosted admin access and isolated
document execution are planned, not provided by the offline prototype.

Hosted workspace (`workers/`): organizer identity comes from a verified Cloudflare Access
JWT; the runner's bearer token (`RUNNER_TOKEN`) can read every job, every original by hash
and the complete D1 export, so treat it as a database credential and rotate it from
Settings after any exposure. Backups are integrity-checked by `verify`; tamper detection
needs the `manifest_sha256` printed at export, kept apart from the archive.
