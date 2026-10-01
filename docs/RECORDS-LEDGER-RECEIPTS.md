# WP1 receipt-evidence sidecar

Offline capture only. No canonical receipts, stages, acceptance, reviews, jobs,
network operations or publication writes. `binding_verified` means an explicitly
mapped original exists and captured artifact bytes match the declared artifact
hash without unresolved capture/role errors. It does NOT mean review accepted,
independence established, coverage checked, supersession applied or legal/privacy
clearance. Reviewed-content hashes remain attributed declarations.

## Caller contract

Create a frozen, private root owned by the invoking user, mode 0700. All paths
below are relative to that root. Pass an initialized candidate ledger and a
version-1 UTF-8 JSON manifest with exact frozen JSONL index hashes. Example:

```json
{"version":1,"indexes":[{"path":"reviews.jsonl","sha256":"<exact index SHA-256>","kind":"reviews","fields":{"artifact_path":"path","artifact_hash":"receipt_sha256","original_hash_candidates":"candidate_source_hashes","reviewed_content_hash":"reviewed_content_sha256"},"declarations":{"reviewer":"reviewer_id","role":"role","verdict":"verdict","coverage":"coverage","locators":"locators","challenges":"challenges","supersedes":"supersedes","holds":"holds"}}]}
```

`kind`: documents, digests or reviews. Field selectors are explicit dot-separated
object keys, never recursive discovery. Allowed hash roles: original_hash OR
original_hash_candidates (a singleton list only), artifact_hash,
reviewed_content_hash. `artifact_path` selects a root-relative regular file.
Both original roles together, missing roles, invalid hashes or multi-candidate
lists remain unresolved. Documents may be declaration-only with no artifact.
Digest mappings can explicitly map source_sha256_declared to original_hash and
receipt_sha256 to artifact_hash; this is caller attribution, not inferred truth.
Receipt bodies are preserved, not recursively interpreted. Map index declarations
for reviewer/role/verdict/coverage/locators/challenges/supersedes/holds/created_at/
status as needed. Missing declarations stay null, not fabricated. The complete
index line and snapshot preserve unmapped data for later adapters.

```sh
python3 -B -m campaign_tool.records.ledger.receipt_projection \
  --database /private/candidate/ledger.sqlite --root /private/frozen-inputs \
  --manifest receipt-manifest.json --batch-size 100 --max-batches 1
```

## Storage, replay and bounds

Disjoint `rp_*` tables contain immutable manifest/index snapshots, original
index lines, exact artifact bytes/hash/length, mapped attributed declarations,
explicit gaps, evidence bindings and append-only item checkpoints. Each batch
commits artifacts/items/gaps/checkpoints atomically. A failed batch rolls back;
prior committed batches remain. Identical declarations on different lines are
preserved separately; identical artifact bytes deduplicate by SHA-256. Changing
manifest content creates a new capture. Same manifest with another root fails.
Replay verifies retained snapshots, reconstructed mappings, original bindings,
artifact bytes, evidence digests, gaps and checkpoint coverage. It uses captured
snapshots, not subsequently changed external artifacts. First captures detect
file changes during reading; missing, escaping, symlink, nonregular, oversized
and hash-mismatched input gets an explicit gap. No oversized bytes are claimed
preserved. SQL prevents UPDATE/DELETE/REPLACE even with recursive triggers off;
fresh inserts enforce snapshot/role/evidence consistency. Missing registered
validation functions also fails closed for external SQL writers.

Hard bounds: manifest 256 KiB; 16 indexes, each 8 MiB, aggregate 32 MiB;
10,000 JSONL lines per manifest; each interpretable line 256 KiB; each artifact
2 MiB; sidecar artifact budget 64 MiB across captures. Oversized lines remain
in their captured index snapshot with an explicit gap. Batch 1..1,000 items,
1..100 batches per invocation. Index/manifest limit overflow fails closed or is
an explicit source gap. Source snapshot publication is atomic. Ledger database,
indexes and artifacts remain private. Raw correspondence is never a public
fixture. SQLite integrity protects evidence, not semantic truth.

## Output and limits

Output includes manifest ID, processed/added/remaining, gap count, observed
binding_verified count, and capture status. Always: stage_promotions=0,
review_accepted=false, publication_ready=false, end_to_end_complete=null.
Failed source indexes count as gaps, not processed records. Holds/challenges/
supersession declarations are retained but not resolved. No filename-based
independence, inferred review role or accepted completion. This adapter does not
merge legacy digest status or activate the canonical ledger. Owner-supplied
manifest mappings and subsequent substantive review remain required.

## Frozen parent-capture integration

The historical `private-receipt-index-capture-v1` manifest is not itself a role
binding manifest. Preserve it unchanged. Construct the explicit version-1 caller
manifest above using each entry's exact snapshot bytes/hash and relative snapshot
path. An optional `source_locator` records the attributed original index locator
in the immutable manifest; arbitrary top-level provenance is retained but not
verified. Do not rewrite frozen index rows to change their artifact paths. Instead,
each index may supply `artifact_paths`, an explicit dictionary mapping original
locator strings to frozen root-relative artifact files. The plan retains both
`artifact_source_locator` and the resolved root-relative `artifact_path`. An
unmapped absolute locator fails with a path-escape gap; mapped targets still
receive no-symlink/root/bounds/hash checks. No outside-root lookup is implicit.
The parent captures referenced artifacts separately before supplying mappings.
Historical index row counts are not whole-corpus denominators.
