# Architecture and delivery boundaries

## Initial implementation

Python standard-library CLI: init, doctor, ingest, status, build.
Campaign directories are external to the source repository. JSON configuration,
private SQLite ledger, private SHA-256 objects, and a generated public directory
are separate. No live connectors or AI provider dependencies exist yet.

The review module checks content-bound receipt structure only. It does not
authenticate reviewers, verify document bytes, certify law, or authorize publishing.
Do not use user-supplied receipts as authorization. The website builder intentionally
does not consume findings until an authenticated publication service exists.

## Target architecture

Cloudflare Workers Static Assets for public pages; separate Access-protected
workspace; Worker API; D1 for campaign state; private R2 for originals; a separate
allowlisted sanitized-public artifact store. Durable jobs use Workflows/Queues
with idempotency, retry budgets, and explicit external-action receipts.

Keep parsing untrusted PDFs, spreadsheets, mail, and archives in an isolated
resource-limited runner. Website availability must not depend on that runner.
Disable document macros, remote resources, arbitrary fetches, and instruction execution.
Use explicit network allowlists and never bypass a denied access path.

Hosted newsletter forms are the first integration. Subscriber consent and
suppression remain provider-authoritative. Avoid requiring a home server or
fixed-IP relay merely to launch a campaign.

## Portable records model

Campaign, jurisdiction, agency, governing body, request/scope version, correspondence,
deadline basis, original object, receipt occurrence, extraction, digest, rule/policy
version, comparison, finding, review receipt, publication artifact, correction,
meeting, job, and incident must have durable identities and export contracts.

Original bytes and derivative text are different objects. Byte deduplication does
not erase separate agency productions. Event-date law is distinct from current law.
Downloaded, extracted, digested, reviewed, and published are separate coverage stages.

## Security and ownership

Do not trust visitor identity headers. Validate Access JWT signature, issuer,
audience, and expiry with rotating keys in any protected backend. Public static
routing must never fall through to private files. Deploy only public outputs,
never the campaign directory. Do not place subscribers or case evidence in GitHub.

## Licensing and origin

This foundation is newly authored, not an export of the production SLO application.
No SLO code, live credentials, original records, or private infrastructure settings
are included. Before later reuse, inventory exact source/version, author/rights,
dependency licenses, privacy class, and proposed destination for each component.
