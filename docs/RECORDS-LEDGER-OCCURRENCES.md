# Bounded canonical occurrence projection

This additive WP1 candidate reads captured legacy occurrence rows in an existing
private ledger. It never reopens original files, captured SQLite inputs, mail or
portals. It is not full WP1 acceptance or active runtime deployment.

## Invocation

    python3 -m campaign_tool.records.ledger.occurrence_projection \
      --database "$PRIVATE_CANDIDATE/ledger.sqlite" --import-id "$CAPTURE_ID"

The import must have status captured. Unknown and checkpointed imports fail.
The bound is at most 20,000 rows and 64 MiB of payload JSON; exceeding it fails
instead of claiming partial coverage. No services, models or network are used.

## Supported explicit facts

- Local: filesystem path/root plus integer mtime_ns, ctime_ns, device and inode
  receipt fields, and a timezone-bearing observed_at. An EML on disk is still a
  local observation; neither a filename nor first_seen proves mail delivery.
- Mail: an explicit receipt.mail_identity object with exactly account, folder,
  uidvalidity and uid, plus receipt.eml_sha256 matching an EML original. UIDs
  must be positive integers, not booleans or numeric strings.
- Attachment: a decoded_mime_payload receipt naming both parent_sha256 and
  parent_legacy_oid, which resolves to a typed mail candidate in this capture.
  Identity is the canonical parent message occurrence and hierarchical MIME path.

The explicit mail and parent-oid fields are conservative adapter inputs, not a
claim that the filesystem exporter already supplies them. Missing fields are
never manufactured. The current intake worker records decoded_mime_payload and
reserialized_rfc822 relations, hierarchical locator.mime paths rooted at "1",
and ordinarily parent hashes rather than delivery identities. Native attachment
joins may therefore correctly remain blocked.

The hierarchy accepts "1", "1.2", "1.10.2", etc. Child ordinals are positive
without leading zeroes. Integers, booleans, header marker "0", arbitrary roots,
malformed paths and mixed conventions are not coerced. Explicit mime_part_index
or traversal_index values stay unresolved traversal indexes, never hierarchical
paths. A conflicting declared mime_convention blocks projection. Reserialized
message children are not asserted to be original attachment octets.

Archive and portal acquisition adapters remain outside this slice. Unsupported
container relations and explicit portal contexts get bound gaps. Agency hints,
directory labels, filenames and a unique-looking parent hash do not supply
native agency, request, message or MIME identities.

## Provenance and replay

An additive extension migration records its checksum under ledger_meta and
creates occurrence_projection_runs and occurrence_projection_rows. The bootstrap
migration checksum and user_version are unchanged. Installation and the complete
bounded projection occur in one transaction.

Each captured payload SHA-256 and (oid, sha) identity is checked. Projection
evidence retains exact payload JSON and hash, import/table/row identifiers,
source database and manifest hashes, and the canonical occurrence or reason.
Composite foreign keys bind it to legacy_rows. Extension evidence rejects
UPDATE, DELETE and duplicate-key REPLACE independently of recursive triggers.

Canonical identities do not depend on capture IDs. Later captures retain their
distinct source payloads without duplicating the same canonical occurrence.
Replay checks retained payloads and adds no rows. Conflicting original hashes
under one native identity, or contradictory existing canonical records, abort
the transaction. No existing occurrence or captured payload is edited.

Unresolved source identities create explicit import_gaps. Known, non-excluded
originals receive agency/request joins with null IDs and status blocked.
Canonical out-of-scope originals receive retained scope-excluded evidence, not
new canonical occurrences.

## Counts and holds

Results distinguish source rows, projected rows, blocked rows, scope-excluded
rows and unique canonical occurrences. Existing ledger counts see the canonical
occurrences; raw legacy counts remain unchanged. CLI output excludes payloads
and private source paths.

Stage slots/history, review declarations and acceptance metadata are unchanged.
Stage promotions and verified agency/request joins remain zero; end-to-end
completion remains null and publication readiness false. Typed candidates express
captured declarations, not independent authentication or completed stage gates.

Synthetic test scope: tests.records.test_ledger_occurrences and the affected
tests.records.test_ledger suite. No real-corpus projection, fresh-mail retrieval,
detector completion, publication or service deployment is claimed.
