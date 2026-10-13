# Backup, verify and restore

One verified tar per campaign that an organizer can keep outside any provider and restore
into an empty directory. Code: `campaign_tool/backup.py`, `workers/workspace/src/export.ts`,
the `backup` job kind, and the Settings > Backup button.

## Archive format

An uncompressed PAX tar (originals are usually already compressed; the archive is meant to
be encrypted by the organizer before it is stored anywhere shared). The first member is
`manifest.json`:

```json
{"schema_version": 1, "engine_version": "0.1.0.dev0", "created_at": "...",
 "source": {"kind": "local", "root_name": "campaign"} | {"kind": "hosted", "campaign_id": "...", "exported_at": "..."},
 "files": {"campaign.json": {"sha256": "...", "bytes": 123}, "...": {}},
 "counts": {"objects": 1, "receipts": 1, "files": 5, "bytes": 4096}}
```

Members, in order, for a local campaign: `campaign.json`, `content/**`, `kit/**`,
`private/ledger.sqlite` (a consistent copy taken with the SQLite backup API),
`private/objects/<sha256>`. Every member has mode `0600`, uid/gid 0 and no owner names.
Symlinks, `public/` (rebuildable), `.env` and anything else are never archived. Export refuses
to overwrite an existing path and refuses a stored object whose bytes do not match its
hash-named file (`intake integrity problem`), leaving nothing behind on failure.

The ledger snapshot is taken before file enumeration. Every object referenced by
that snapshot must have an archived original with the same SHA-256 and byte count;
every receipt must reference an object in the snapshot. Missing originals, dangling
references and unreadable or unsupported ledgers stop export. Both SQLite connections
close before the standalone snapshot is hashed. Export verifies the completed archive
before returning success, catching file changes between hashing and archive writing.

For a hosted campaign the members are `campaign.json` (derived from the campaign row),
`private/d1-export.json` (canonical JSON of `GET /api/runner/export.json`) and
`private/objects/<sha256>` for every `original` row, each fetched by hash and verified
before it is stored.
The D1 table inventory, original byte counts and original references are validated.
Re-exporting a restored hosted archive preserves its D1 metadata. A directory with
both a local ledger and a D1 export is refused as ambiguous.

## Commands

```
python3 -m campaign_tool backup  --directory D --out backup.tar
python3 -m campaign_tool verify  --file backup.tar
python3 -m campaign_tool restore --file backup.tar --directory NEW_EMPTY_DIR
```

`backup` prints `manifest_sha256`, the digest of the manifest as written. Record it
somewhere the archive is not (the campaign's private notes, a password manager). Pass it
back with `verify --expect-manifest-sha256 <digest>`: the manifest travels inside the
archive, so without it `verify` is an integrity and consistency check (corruption, a
truncated copy, a member swapped without updating the manifest), not tamper detection —
whoever can rewrite a member can rewrite `manifest.json` in the same archive.

`verify` re-hashes every member against the manifest and reports `ok`, `files`, `verified`,
`manifest_sha256` and `problems` with stable codes: `manifest_digest_mismatch` (when an
expected digest was given), `hash_mismatch`, `size_mismatch` (reported once, not also
as missing), `missing_member`, `not_in_manifest`, `unsafe_member_name` (absolute paths,
`..`, drive letters, NUL), `unsafe_member_type` (links, devices, FIFOs), and
`manifest.json missing`. Any problem raises `BackupError`; the CLI exits 1 with
`Stopped: ...` and no traceback.

Verification also reads the archived ledger or D1 export and checks its required
originals against the manifest, including their hashes, lengths, references and
counts. An older incomplete archive is rejected even when every listed member
matches its checksum. Duplicate JSON keys or tar members, malformed entries and
conflicting paths are refused. Member count and size limits apply during verification.

`restore` refuses a non-empty or symlinked target, verifies first, extracts member by member
with `O_EXCL`, directories `0700` and files `0600`, re-hashes every file on disk, and deletes
the whole target if anything fails. It rechecks byte counts and metadata-to-original
closure on the restored files. The restored ledger is a working database
(`campaign_tool status` reads it).

Python API: `backup.export(root, out_path) -> manifest` (with `manifest_sha256`),
`backup.verify(path, expected_manifest_sha256=None) -> report`,
`backup.restore(path, root) -> report`, `backup.export_hosted(client, out_path) -> manifest`
where `client.get(path) -> bytes` is bound to the runner token.

## Hosted campaigns

- `GET /api/export.json` (Access-protected) streams every D1 table for the campaign, table
  by table, as one JSON document (`schema_version`, `campaign_id`, `exported_at`, `tables`,
  `rows`). Originals are not included; they are fetched by hash.
- `GET /api/runner/export.json` is the same document for the runner (bearer token), which
  makes the runner token a whole-database credential (see WORKERS.md).
- `POST /api/backup` (Settings > Queue backup job) enqueues one `backup` job per organizer
  per day (idempotency key `backup:<day>:<email>`). The runner (not yet built, see
  [ROADMAP.md](ROADMAP.md)) is expected to execute `export_hosted(client, out)` and store the
  archive where the organizer configured, with the manifest counts and archive hash in the
  job outputs. Until then the job sits queued and `export_hosted` can be run by hand with a
  client bound to the runner token.

## Limits and cautions

Member limit 2 GiB and 200,000 members. The archive contains private records, the receipt
ledger and the D1 export, so treat it as the most sensitive file the campaign produces:
encrypt it (`age` or GPG), store it off the provider, verify after copying, and test a
restore into a scratch directory before relying on it. Backups are not a publication path
and never reach the public bucket.

These checks cover the archive's documented originals and metadata. They do not
provide a transaction spanning mutable `content/` and `kit/` files, an inventory
of the full records runtime, or recovery of hosted derived R2 text. The records
recovery-v2 library has a separate declared-profile and caller-quiescence contract.

## Not done

Encryption inside the tool, incremental or scheduled backups from cron, restore of a hosted
campaign back into D1 and R2 (the export is readable JSON and hash-named objects, so a restore
script is straightforward but not written), and retention policy.
