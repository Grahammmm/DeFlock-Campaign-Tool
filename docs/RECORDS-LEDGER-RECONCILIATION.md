# Evidence inventory reconciliation

`python -m campaign_tool.records.ledger.reconcile mail|portal|digests --input INPUT --report NEW_REPORT` produces an immutable private report. Input contains `sources`, `index`, and `objects` (the caller's byte-verified original hashes). It does not itself rehash originals or promote stages. The caller must retain the inventory provenance; `acceptance=supplied_evidence_only` makes this boundary explicit.

Mail identity is account, folder, UIDVALIDITY and UID, plus hierarchical MIME part path for attachments. Equal bytes in two folders still require two occurrences. Missing index rows are stale indexes, not permission to discard duplicate deliveries. Hash conflicts, missing bytes, incomplete identity, explicit exclusions, parser failures and unmatched index rows remain separately counted. Filenames and Message-ID are never identities.

Portal identity is host, request and item. Bytes are joined only when explicit binding evidence repeats that identity, original hash and exact receipt hash. Notice-only and unbound-byte rows remain gaps. Digest coverage differences require reconciliation and never establish accepted review. Every supplied row has a locator and classification; classified gaps do not mean resolved gaps.

Reports contain private identities and must remain outside public assets. The public module and its tests use synthetic data only. Maximum rows: 100,000. CLI reports are created exclusively with owner-only mode and existing reports are not overwritten.

Version 2 stores index-match groups once, with rows referencing group IDs, so duplicate groups have linear output size. Explicit portal parser errors remain unresolved even when supplied binding fields match.
