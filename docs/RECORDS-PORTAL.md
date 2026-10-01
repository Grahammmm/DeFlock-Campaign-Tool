# WP3 portal retrieval and WP1 enrollment candidate

This component has a durable queue, an optional stdlib HTTPS transport, and an
explicit canonical-ledger adapter. Tests use synthetic bytes and socket/DNS
fixtures. Live retrieval, activation and independent acceptance remain pending.
The canonical WP1 implementation is imported as a dependency, never copied here.

## Commands and opt-in

```sh
python3 -B -m campaign_tool.records.portal --root /private/portal inventory --input /private/notices.json
python3 -B -m campaign_tool.records.portal --root /private/portal status
python3 -B -m campaign_tool.records.portal --root /private/portal fetch
```

Fetch defaults to inventory-only. A trusted runner can inject transport, egress
and ledger into `main()` or `fetch_queue()`. The standalone CLI can explicitly
select the prepared HTTPS transport:

```sh
python3 -B -m campaign_tool.records.portal --root /private/portal fetch \
  --apply --https-transport --approval-file /private/admin/approval.json \
  --egress-file /private/admin/egress.json
```

This is an integration command, not an instruction to activate or extend access.
The standalone CLI has no canonical ledger destination argument: the runner
constructs the explicit adapter. Without it, preserved bytes remain in the
sidecar's pending outbox. Top-level `records portal` dispatch remains an owning
integration change; no WP2 files are modified by this branch.

Inventory input is a private array of `host`, `request_id`, `item_id`, `url`,
`source_sha256` objects. The caller binds source hashes to preserved notices.
`inventory_notice()` recognizes `/documents/<item>` links in bounded plain/HTML
bodies with an explicitly supplied request ID and known hosts. Recognition grants
no download permission. Missing request identity must remain a caller gap.
Other portal URL schemes need separately tested provider adapters.

## Queue, attempts and versions

The stable identity is `(host, request_id, item_id)`. A changed signed URL updates
`last_url_private` and increments the generation of that same item. Duplicate
notices preserve hash relationships. Signed URLs, query strings, arbitrary
exception text and response Location values never enter status or attempt reports.

An owner-only directory contains SQLite, a nonblocking writer lock and SHA-256
originals. Attempts commit before transport begins. Evidence records host-only
redirect sequence, HTTP status, MIME type and observed byte count. Originals are
fsynced, linked without overwrite and verified before a version receipt commits.
Changed bytes link to the previous hash. Repeated current bytes create no new
version. Final objects are mode 0400; temporary files are removed on failure.
Schema version 1 is an unreleased candidate; existing production schemas are not
silently upgraded. Direct database writes are outside the supported API.

A crash after bytes but before a receipt retries against the same object and
checks its hash. A crash after canonical ledger commit but before outbox
acknowledgement repeats the same receipt. Interrupted exhausted attempts become
visible `retry_exhausted` blocks. Failed items do not halt unrelated retrievals.
Permanent failures need a refreshed link or a future audited operator requeue.

Default limits: 25 MiB body, 25 seconds total fetch budget, 3 redirects, 3 attempts,
30-second exponential retry backoff and 60 items per call. Settings have hard caps.
HTML MIME types and sniffed HTML/form/input/script content, expired/denied links,
invalid lengths, encoded bodies, unexpected PDF signature, unsafe URLs and
unapproved redirects fail visibly without a document receipt. Conservative HTML
sniffing may block legitimate files containing code; a reviewed format-specific
adapter is needed to resolve those cases.

ZIP handoff inspects metadata without extracting. Limits cover 1,000 members,
100 MiB declared expansion, 100:1 compression ratio, traversal, symlinks and
encryption. The extraction sandbox must independently enforce streamed expansion,
nesting, CRC and time/memory limits; preserving a ZIP is not extraction completion.

## Separate approval and egress policies

Both policy readers require bounded JSON, root ownership, regular non-symlink
files, no group/world write, and an admin-owned non-writable immediate parent.
Opening uses nonblocking mode so a misconfigured FIFO cannot stall the reader.
Policies have version 1, `enabled: true`, and a future timezone-aware
`expires_utc`. Files are checked for change during reading.

Retrieval approval uses purpose `portal-document-retrieval`, `portal_hosts`,
`redirect_hosts` and `path_prefixes` for every allowed host. Prefixes end in `/`.
Egress approval uses a distinct purpose `independent-portal-egress` and an explicit
`hosts` list. Existing deployment-specific policy formats are not rewritten by
this code. Any conversion/provisioning needs a separate reviewed admin change.
An egress file records a grant; it cannot override a browser/tool denial or a
network restriction. Activation still requires an independently permitted path.

Every request and redirect rechecks both gates. The engine also accepts an
independent `egress(url)` checker. Fixture callbacks returning True are used only
in synthetic tests. No raw record can select a policy, callable or transport.

## Prepared production HTTPS transport

`StdlibHTTPS` is disabled unless `apply=True`; the CLI additionally requires
`--https-transport`. It ignores ambient proxy settings and credentials. HTTPS and
port 443 only; user information, fragments and control characters are refused.

DNS runs in a killable child process with a deadline. The complete response must
contain only approved public-address classes; any private, loopback, link-local,
reserved, multicast, mapped or transition/NAT64 address rejects the response.
The TCP connection uses an already validated numeric IP, checks the connected
peer, and verifies TLS using the original hostname and the standard trust store.
There is no second hostname lookup at connect time. Each redirected hostname goes
through the same process; no HTTP library follows redirects automatically.

The original total deadline bounds DNS, connection, TLS, headers and body. A
socket reader reapplies the remaining deadline on every receive, including slow
header/body reads. HTTP body bytes are streamed in bounded chunks with size caps;
ambiguous Content-Length/Transfer-Encoding and duplicate sensitive headers fail.
The resolver's terminated child has a bounded cleanup allowance of one second.
Tests substitute resolver, connector and socket fixtures, and forbid real socket
creation and DNS calls. These tests do not establish live certificate, network,
DNS-provider or service availability. Independent security review and controlled
permitted retrieval are required before operational claims.

## Explicit WP1 outbox adapter

`WP1LedgerAdapter(queue=..., database=..., cas_root=...)` imports the installed
`campaign_tool.records.ledger.store` and uses its checked database context/schema.
The source overlay environment variable exists only in integration tests. This
branch contains no WP1 migration or store copy.

Before enrollment it verifies the original's CAS path, bytes, size and SHA-256;
receipt ID; portal identity; generation; source-notice hash references; and exact
successful attempt evidence. It copies bytes into the configured canonical CAS
with no overwrite and checks that copy. In one transaction it enrolls the
canonical original, portal occurrence, portal item, delivery run and all seven
pending stage slots. The first run summary and occurrence retain frozen receipt
provenance. Replays reject changed run/occurrence/portal bindings and corrupt CAS.
Changed content creates a versioned original/occurrence; older receipt replay
cannot roll back the current portal item. Same bytes for two requests retain
separate occurrences and one original. History examination is capped at 10,000
versions per item. Unsupported/conflicting existing storage bindings are explicit
errors; immutable prior originals are never silently patched.

`pending_card_inputs()` exposes enrolled originals awaiting catalog. It does not
claim to have generated catalog summaries or board cards. `preserve`, `extract`,
`catalog`, `detect`, `review`, `compare` and `privacy` all remain pending for a new
original. `validate_and_promote()` refuses until a separate trusted portal stage
adapter is configured. No existing completed stage is reopened or overwritten.
Notice hashes are retained references; a mail occurrence ID is left null until a
trusted source relationship is available. Agency and title remain unknown.

## WP2 runner boundary

`PortalRunnerBridge.run(apply=...)` delegates retrieval and canonical enrollment,
then returns pending card inputs and explicit zero stage promotions. It conforms
to the separation in WP2's runner contract: byte enrollment is distinct from
`LedgerBackend.validate_and_promote(StageReceipt, run_identity)`. It does not
impersonate a mail UID checkpoint or claim canonical stage acceptance.
`canonical_integration_verified=false` and `pipeline_complete=false` remain until
the owning runner integrates the trusted stage registry, run/runtime identity,
leases and actual acceptance checks. Queue and runner locks must have a consistent
acquisition order; the sidecar adapter is called while the queue writer lock is
held. New originals and pending work can be resumed after interruption.

## Tests and remaining handoff

```sh
python3 -B -m unittest tests.test_records_portal tests.test_records_portal_https -v
RECORDS_WP1_SOURCE_ROOT=/trusted/engine-candidate python3 -B -m unittest tests.test_records_portal_wp1 -v
```

The optional overlay exercises the actual WP1 candidate on temporary synthetic
ledgers. On a checkout without WP1, these tests explicitly skip; they must pass
with the intended integrated version before merge/deployment. The parent should
review this branch and the exact WP1 dependency hashes in the private test receipt.

Remaining: independent review, canonical trusted preservation-stage adapter,
source-notice binding, WP2 scheduling/lease/runtime wiring, provider-specific
notice discovery, exact admin policy compatibility and controlled authorized
network acceptance. No real portal requests, host permission changes, package
installs, schedules, commits, pushes or deployments were performed for this work.
