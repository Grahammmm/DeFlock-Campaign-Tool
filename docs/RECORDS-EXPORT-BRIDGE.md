# Export receipts and collection completeness

This source candidate connects already exported email to the existing records
pipeline without accessing a mailbox or credential configuration. It is not an
installed event trigger, collection downloader, independent review or publication
service. Source review and a separately designated synthetic gate are required.

## Existing components reused

`export_bridge.enroll_export` sends the original exporter receipt to
`CanonicalMailBackend.preserve`. The existing `mail_delta` importer validates
original and attachment bytes, MIME membership and immutable occurrence identity.
It already understands integer MIME walk indexes and hash-addressed export paths.
There is no renamed `.eml` inbox copy or synthetic UID. Account, folder,
UIDVALIDITY, UID, exporter-declared internal date and exact receipt bytes are
retained. The date and namespace are exporter declarations, not provider attestation.

`Pipeline.run(exports=...)` enrolls at most 60 explicitly selected receipts while
holding the same canonical root lock as ordinary intake. It then calls the same
`advance_all` stage implementation. No alternate extraction, OCR, model, catalog,
review, recovery or public-output framework is added. Existing stage leases,
budgets, malformed-output checks and owner publication gates still apply.

The existing library selection is a mapping with `root` (the explicit existing export
root), `receipts` (bounded exact paths) and `inventory` (a `CollectionInventory`
whose existing `Queue` is at the selected canonical root's `portal` directory).
The ordinary CLI can also construct this selection as described below. No
configuration fallback, live source scan, scheduler or listener is installed.
A reviewed operator binding must select the receipts and runtime.
There is no implicit full-backlog or mailbox-coverage claim.

## Explicit selected-export CLI

On a separately qualified Linux runtime, an operator can select existing local
receipts with the ordinary runner. This example is a command that writes the
selected canonical root and runs its existing stages, not a read-only probe or
permission to process private data:

```sh
python -m campaign_tool.records.run --root /approved/private/canonical \
  --export-root /approved/read-only/exports \
  --export-receipt receipts/selected-message.json \
  --collection-host archive.example.invalid \
  --collection-exclude-host social.example.invalid --json
```

Repeat `--export-receipt` for 1–60 unique explicit paths. Relative paths are
relative to `--export-root`; absolute paths must remain beneath that root.
Traversal, symlinks, sources inside canonical `intake`, duplicate normalized
selections, invalid/conflicting hosts, incomplete options and other intake modes
are rejected before the runtime probe or root creation. Receipt contents and
original bytes are still checked by the unchanged bridge during enrollment.
There is no glob, folder scan, latest-message guess or mailbox configuration read.

The immutable selection opens the existing portal queue and inventory only under
the runner's existing root lock, and closes the constructed queue even if inventory
construction or the core run fails. Collection-host flags select inventory scope;
they do not grant HTTP/credential permission. Omitted hosts leave links explicitly
unapproved rather than silently complete. Scope changes do not refresh exhausted
attempts. Unresolved retrieval keeps `completed_with_gaps` and exit code 3.

`--unattended` accepts a separate, hash-bound export selection: add
`--export-manifest-sha256 SHA` to `--export-root` and 1–60 relative
`--export-receipt` paths. The manifest binds receipt bytes and every declared
original/attachment (sha256 and size), in selection order, with a 1 GiB cap.
All components must be unlinked owner-only directories/files (0700/0600).
There is no discovery; changed bytes, metadata or inode identity stop intake
before enrollment. Disk capacity is checked before starting the mail backend.
One backend lifecycle preserves original identities; identical replays add no mail.
Reports omit paths, accounts and URLs. Incomplete retrieval remains exit 3.
Inbox, mailbox and document selectors cannot be combined with exports.

The container host's `export-manifest` command produces the sanitized hash/counts;
exports profiles require an exact read-only mount and network `none`. Container
entry forwards no collection-host approval: every unresolved body link stays a gap.
Synthetic checks do not authorize real intake, source acquisition, installed
profiles/timers, macOS qualification, legal acceptance, publication or email.
Real exports need the custodian's separate sanitized source contract/admission;
canonical stores and the existing Qwen queue remain with their existing owners.
Existing library callers and their explicit mappings retain their interface.

## Expectations before retrieval

The inventory extension uses the **existing private portal SQLite sidecar** and
its object store. It adds version-1 tables for export events, body-link occurrences,
collection nodes, immutable parsed-page manifests and expected members. These are
additive sidecar schema changes on first construction; the canonical schema is
unchanged. Construction is not authorized against an installed store by this PR.

After exact preservation, the bridge examines every qualified plain-text and HTML
body alternative. Attachment payloads are never treated as body instructions.
Each body-part/link ordinal is retained. Named/attached multipart, nested mail and
native-message body discovery outside this first adapter's qualification remain
blocked, rather than silently incomplete. The existing canonical importer may
preserve those originals, but the event cannot claim full discovery.

The caller supplies reviewed host scope and explicit irrelevant-host exclusions.
`Browse.aspx`, `/browse`, `/collection` and `/index` pages create pending collection
expectations even without file extensions. Recognized document URLs use the existing
portal item queue. Unknown paths, unapproved origins, insecure or malformed URLs
remain visible exceptions. An email cannot add an approved origin or exclusion.
Scope drift for an enrolled occurrence is rejected before discovery, page replay
or mutation, worklist construction and readiness evaluation, including after a
queue reopen. Shared collection nodes also bind the exact scope fingerprint:
another event with different origins, exclusions or bounds cannot populate that
node or change the first event's membership through a broader plan.

Signed URL queries remain private and byte-preserved, not sorted or decoded.
Direct-document path identity excludes the query, so a replacement token cannot
silently reopen an existing lineage. Collection member IDs must be stable within
the provider host namespace, supplied by a reviewed parser; changing an existing
member's URL requires the existing explicit generation-aware refresh process.
New messages, replay or parser versions do not reset exhausted portal attempts.

## Collection traversal contract

`pending_pages(event, limit=60)` returns a **local worklist**, not permission to
fetch it. It walks only the event's registered collection graph, with depth at
most 3 and a bounded inventory walk. Repeated nodes and cycles cannot expand the
worklist indefinitely. Every collection is limited to 100 pages and 1,000 members;
body discovery is limited to 2 MiB and 1,000 links. Limits can be lowered.

`accept_page` accepts a provider-neutral parsed page description only when:

- The exact original page exists in the existing portal CAS and passes its hash
  and byte checks, with a 25 MiB page cap.
- The page carries an exact parser source hash, current cursor, stable member IDs,
  member URLs/types, next cursor and explicit expected total (or unknown).
- The collection is reachable in the bounded worklist, and pagination remains on
  the original approved collection host.
- A repeated page is byte/manifest/parser-identical. Conflicts, pagination loops,
  changing totals, duplicate IDs and member conflicts fail closed.

Unknown totals and interrupted enumeration remain partial. An explicit empty
collection needs a terminal page declaring exactly zero members. Terminal known
totals must equal the distinct immediate members; nested collections must also
reconcile. A member does not count as retrieved until the existing portal queue
has received its current bytes and delivered its version to the canonical ledger.
Collection/page/parent relationships stay in the private sidecar. The existing
portal ledger adapter still marks bytes pending trusted stage validation.
Portal document notices and delivered canonical provenance reference the verified
original page-byte SHA, separately from the collection URL identity. A member seen
on two collection pages retains both page digests through delivery.

This candidate does **not** implement a live WebLink HTML parser or a collection
HTTP reader. It never calls an injected unbounded loader. A qualified provider
adapter must prove pagination and denominator semantics on synthetic pages, then
reuse the existing approval/egress/streaming transport checks under a reviewed
deadline and containment boundary. It must not convert a login page or a page
with no recognized links into a zero-member collection. No credential forwarding,
SSRF bypass, browser login, challenge bypass or guessed expired URL is permitted.

Collection authorization, expired-link, unsupported-navigation and exhausted
retry exceptions are durable. This slice adds no collection retry runner: no new
attempt allowance is inferred from source, code version or a new timer. Existing
document downloads retain their three-attempt policy. Explicit authorized refresh
and recovery remain separate operations.

## What the reports mean

`readiness(event)` reports only this event's retrieval reconciliation, document
occurrences and unique preserved document hashes. The original email is separate
from document counts. It always reports `digestion_complete: false` and
`publication_authorized: false`; it cannot manufacture stage or review receipts.
For an export-selected pipeline run, incomplete retrieval makes the run
`completed_with_gaps` even if the currently available documents finish stages.
The pre-existing seven-stage candidate count is scoped to enrolled originals;
it is not evidence that undiscovered linked documents were processed.

## Synthetic acceptance and adoption

The candidate suite is `tests.records.test_export_bridge` (14 cases). Its positive
oracle has attachment A and seven collections: B/C, C/D, empty, paged E/F, G, H,
I. The existing mocked portal transport and real ledger adapter must preserve nine
unique documents across ten document occurrences, plus the original EML. A hash
deduplicates C while preserving both collection occurrences. No external HTTP
socket, credential loader, model or real input is used.

Other controls cover exact UID identity, replay/UIDVALIDITY reset, corrupt originals,
missing MIME members, plain/HTML discovery, unknown denominators, pagination loops,
three-attempt exhaustion, authorization exceptions, nested-depth bounds, unapproved
links, scope drift, page integrity and pipeline branch/status integration. The
reopen controls reject broader origins, changed exclusions and changed bounds on
page replay, new-page submission, discovery, worklist and readiness paths without
re-registering. A second event cannot populate a collection under a different
plan. The positive fixture also asserts exact page-byte hashes in notices, portal
versions and delivered canonical run/occurrence provenance for shared member C.
These controls cover same-plan reopen persistence, not injected crash acceptance.
The pipeline integration control substitutes only the already existing stage driver;
it does not claim a new extraction, OCR, legal or real-document acceptance result.

Do not run this suite or trigger PR CI until the exact source/fixture seal receives
its independent review and a designated qualification route. It is separate from
any held containment qualification and cannot reset or consume that gate.

Installed adoption additionally needs a reviewed engine/runtime binding, current
exporter/receipt selection, collection provider parser and permitted transport,
model/egress binding, failure recovery, one intake owner and actual trigger timing.
Keep original exports read-only. Do not install a competing timer. Real collection
retrieval, ingestion and model work need their exact qualified scope; no synthetic
result grants private-input or public-finding permission.

Rollback is to stop selecting the new library interface and retain the prior
engine revision. Leave all original bytes, canonical records and added sidecar
tables/evidence intact. Do not drop tables, remove failure markers, reset attempts
or prune data as rollback.
