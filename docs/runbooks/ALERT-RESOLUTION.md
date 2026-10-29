# Attended exact-key alert resolution (T4)

This standalone stdlib tool resolves a frozen list, not a count, prefix or LIKE
selection. Offline tests do not authorize live use. Live use remains an attended
operator session with a backup and a custodian-confirmed contract. No timer,
service, migration, download, send or publication is performed by the tool.

## Engine vocabulary and retry contract

Source references checked on main `b9bef82`:
- `campaign_tool/records/native_extract.py:163-184`: only `state='open'` holds
  extraction; any non-open value releases the hold.
- `campaign_tool/records/run.py:337-350`: `_alert` reopens the exact key and
  increments its retained count; `_resolve_alert` writes `resolved`.
- `campaign_tool/records/schedule.py:187`: repeats reopen and increment counts.
- `campaign_tool/records/ledger/migrations/v001.py:130`: alert columns are
  `id, key, first_seen, last_seen, count, owner, state`; no state CHECK exists.

The default contract spelling is `resolved`. A confirmed contract may instead
name `resolved_with_evidence`. The tool never changes `needs_review` rows. Unlike
`Pipeline._resolve_alert`, it preserves `last_seen`, count, owner and all other
non-state columns. A subsequent engine failure reopens the key with count + 1;
resolution is neither deletion nor a reset of the retry history.

## Contract template (not a real confirmation)

The attended custodian must replace the empty confirmation placeholders. Empty
values deliberately refuse. Never commit a completed production contract or keys.

```json
{
  "schema_version": 1,
  "target_state": "resolved",
  "confirmed_by": "",
  "confirmed_at": "",
  "confirmation_reference": "",
  "retry_semantics": {
    "hold_state": "open",
    "non_open_releases": true,
    "repeat_failure": "reopen_and_increment_retained_count",
    "needs_review": "untouched_but_not_an_engine_hold",
    "resolution_preserves": ["id", "key", "first_seen", "last_seen", "count", "owner"]
  }
}
```

## Attended live order — operator steps, not build-time actions

1. Obtain the separate live-use approval; pause the records timer and confirm no
   records run is active. The tool does not pause or inspect a timer.
2. Back up the ledger using the existing attended backup procedure.
3. Prepare a JSON array of unique, exact, nonempty keys; obtain the custodian's
   completed contract. Write the flag's exact bytes `T4_QUIESCENT\n` (LF).
   Each input and ledger must be an owner-owned, single-link regular 0600 file,
   with distinct inodes and no symlink ancestors. The ledger parent must be 0700.
   Freeze the keys/contract hashes and expected count; each input is at most 1 MiB.
4. Run from the repository (substitute the attended paths and frozen hashes):
   ```sh
   python3 -B tools/resolve_alerts.py --root <processing-v1> \
     --keys-file <keys.json> --keys-sha256 <frozen-hex> --expected-count <N> \
     --contract-file <contract.json> --contract-sha256 <frozen-hex> \
     --quiescence-flag <flag>
   ```
   The ledger is always `<root>/ledger.sqlite`. The tool takes `<root>/run.lock`
   before SQLite and retains it through commit or rollback. The flag remains a
   required operator attestation. Do not bypass `root_locked` or any refusal.
5. Independently read integrity and reconcile the counts against the backup.
6. Resume the timer only after attended verification succeeds.

One `BEGIN IMMEDIATE` transaction checks integrity, the exact alerts column list,
exact-open rowcount per key, total changes and all table counts, then re-verifies
frozen inputs before commit. Any mismatch rolls back. Success is one JSON line
of counts plus `integrity_ok`; refusal is `{"refused":"<fixed_code>"}`, exit 2,
without keys, paths or SQLite text. No partially successful list is committed.

Offline acceptance: `python3 -B -W error::ResourceWarning -m unittest tests.test_resolve_alerts`.
