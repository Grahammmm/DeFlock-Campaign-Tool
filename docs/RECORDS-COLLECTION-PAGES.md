# Bounded collection-page acquisition

This source slice connects preserved export collection expectations to a typed raw
page path and a trusted parser registry. It does not enable a live provider,
install an engine, fetch private records, or grant network/publication authority.
The only registered profile is `synthetic-archive-v1`, restricted to hosts ending
in `.example.invalid`. WebLink and every other real provider remain unsupported
until representative, permitted page bytes establish their schema and pagination
semantics. The synthetic DOM is an artificial contract, not a guessed provider
selector.

## Composition and authority

`run_collection_pages` takes an existing `CollectionInventory`, immutable event,
explicit source-registered profile ID, and the existing injected approval loader,
egress checker and no-redirect bounded transport. Plan mode reports pending page
count without fetching. Apply mode additionally requires the canonical root lock.
There is no CLI, configuration loader, cookie jar, login flow, browser, executable
plugin loader or default network transport in this module. Mail and page content
cannot select executable code or enlarge the event's accepted scope.

`engine.fetch_collection_page` uses the same attempt, stream, redirect, timeout,
object promotion and sanitized error machinery as document downloads. The typed
page binding records the profile ID and SHA-256 of the registry/parser module.
Document requests retain their existing HTML MIME/sniff rejection and PDF/ZIP
handling. The reserved request namespace `collection-page-v1` is excluded from
ordinary document fetching. A caller must not enroll documents under that name.

Each response hop still needs the existing approval and egress checks. HTTP
401/403 and 410 become visible authorization or expired-link exceptions. Cookie
check, login, sign-in, challenge and authentication paths stop before opening that
hop; no cookie or form workaround is implemented. Unknown layouts, login HTML,
unsupported media/encoding, incomplete streams, denominator conflicts and loops
never become empty collections. Exceptions retain objects and attempt history.

## Bytes, inventory and completeness

Raw pages are stored byte-for-byte in the existing Queue content-addressed store.
Their existing `portal_versions` rows use `ledger_state=collection_page`; rejected
parses use `collection_blocked`. These states stay outside the canonical document
outbox, which accepts only `pending` versions. Page bytes therefore do not inflate
canonical original/document counts. No table or canonical schema is added.

The parser reads the verified object and submits its actual page SHA-256, byte
count, parser SHA-256, stable member IDs, member kinds, normalized URLs, next
cursor and known total to the existing `CollectionInventory.accept_page`.
That inventory enforces the immutable event/node scope, cursor history, member
consistency, deduplication, page/depth/member bounds and terminal denominator.
Document notices retain the real page-byte hash, including all pages observing a
repeated member. The existing document downloader and ledger adapter subsequently
preserve member originals and occurrences; page enumeration does not download
members, run extraction, promote review stages or publish output.

Retrieval completeness comes only from the existing inventory after every
collection is enumerated and every required document is preserved and delivered.
It does not imply digestion, legal review or publication readiness. A zero total
requires an explicit recognized empty marker and terminal page. Unknown remains
unknown. Nested collection members use the same bounded inventory worklist;
they do not confer permission to visit a new origin or choose a new parser.

## Bounds, interruption and operator holds

The synthetic profile allows at most 2 MiB per page, 10,000 start tags, 30
attributes per tag and 1,000 listed members. An invocation examines at most 60
pending pages (or a smaller explicit item limit), at most three redirects and
three retained attempts, and at most 25 seconds per page attempt. Existing
stream/chunk/length checks remain in force. Each invocation visits a URL at most
once; backoff and exhausted lineages are not reset by re-entry.

A stable page item identity is derived from its URL, not from a new parser version
or a new invocation. After a parser interruption, an unchanged binding can reuse
the verified cached page without another request. A committed inventory page is
the authoritative acknowledgment; no second delivery transaction is required.
Cached parser/generation/type drift fails closed. The slice introduces no refresh
command: changing a signed URL, refreshing stale page bytes, reopening a blocked
collection or adopting a different parser needs a separately qualified operator
plan. Do not edit the database to reset tries, dispositions or parser hashes.

The root lock serializes the composition and the existing queue lock serializes
attempt/CAS updates. This is not a sandbox or a process supervisor. It makes no
new containment, credential, installed-service, scheduling or backup claim.

## Authored synthetic acceptance (not an execution receipt)

`tests/test_records_collection_pages.py` embeds eight artificial HTML oracles and
uses injected responses only. Its cases cover two-page overlap/provenance and
subsequent canonical document delivery; explicit empty versus unknown; login and
cookie-check boundaries; 401/403/410; redirect authorization and redirect budget;
egress denial; partial streams; unsupported encoding; bytes/deadline/attempt
bounds; pagination and denominator conflicts; unapproved members; bounded resume;
parser interruption/cache reuse; parser binding drift; plan/root-lock boundaries;
reopened scope rejection; malformed/truncated markup; unsupported real profiles;
and unchanged document HTML/PDF behavior. No private fixture or live URL is
included. Synthetic success cannot qualify a real provider schema.

Before running the suite, bind an independently reviewed exact source candidate
to one designated invocation and a private temporary directory whose ancestors
meet the canonical storage permission checks. Run it with the existing export
bridge and document-portal regression coverage as qualified by that plan. Do not
infer an additional runtime allowance or retry from this document. Separate held
containment work and any earlier failed attempts retain their own history.

## Adoption and rollback

This patch changes only the shared engine, this new module, synthetic tests and
this document. It adds no dependency, migration, schedule, service or production
setting. New sidecar values and the reserved request namespace require explicit
installed-version compatibility qualification before use. Existing readers must
continue excluding page versions from canonical document delivery.

Rollback means stop invoking the new page path and restore the pinned prior
source/launcher through the approved deployment process. Preserve all object,
version, attempt and collection rows and their receipts; no deletion, pruning,
state reset or schema downgrade is needed or authorized. Prior software should
not process the reserved page namespace. Installed adoption, live provider
qualification, retries of blocked access, private inputs and publication remain
separate gates.
