# Flock fact store (schema v2)

One CSV per agency in a private store outside the repo; `tools/flock_facts.py` imports and checks it.

| Column | Rule |
| --- | --- |
| `agency`, `request`, `fact_type`, `value` | `agency`, `fact_type`, `value` non-empty; `request` non-empty when kind is `record` |
| `source_sha256` | 64 lowercase hex |
| `page` | integer >= 1 |
| `verbatim_quote` | non-empty, unless `value` is `UNREADABLE` |
| `fact_id` | derived, see below |
| `kind` | `record`, `policy`, `law` or `public` |
| `kind` `policy` (`--check STORE`) | source must be the POLICY version it quotes: a `doc_sha256` in that agency's POLICY.csv |
| `kind` `law` (`--check STORE`) | source must be the LAW row it quotes: a `text_sha256` in `STORE/LAW.csv`; `--check FILE` skips both links |
| `effective_from`, `effective_to` | empty or YYYY-MM-DD, from <= to |
| `added_by`, `added_at` | importer name; YYYY-MM-DD |
| `status` | `unreviewed`, `checked` or `disputed` |

The v1 import header is the first seven columns. Any other header is an error. Files are read as
`utf-8-sig` (CRLF accepted) and written as UTF-8 with `\n` line ends.

- **Slug:** agency lowercased, each run of non-`[a-z0-9]` characters becomes `-`, ends stripped.
- **Layout:** `<store>/agencies/<slug>/FACTS.csv`; directories are mode 0700 and a row's slug must match its folder.
- **Dedupe key:** (slug, `source_sha256`, `page`, `fact_type`, `value`), each stripped.
- **fact_id:** `F-` plus the first 12 hex of sha256 over the key joined with `\x1f`; import is idempotent.
- **Privacy:** the store and real FACTS.csv are never committed. The CLI prints row numbers, field
  names and counts only, and refuses a store inside the repository checkout.
- A clean `--check` is machine validity, not review or acceptance.

**Stale facts** (`stale --store DIR --date YYYY-MM-DD [--agency SLUG] [--findings FINDINGS.csv]`, read-only, ids only):
a fact is *expired* when `effective_to` is before the date (the end day itself is still in force) and *not-yet* when
`effective_from` is after it. It is *superseded* when, in the same agency, another fact with the same `kind` and
`fact_type` has a later `effective_from` on or before the date, while this fact has an `effective_from` and no
`effective_to`; the latest such fact is named. `disputed` facts are listed too. With `--findings`, each finding citing an
expired, superseded or disputed fact is listed; exit 3 when any does (without it: any expired or superseded fact).

**Review log** (`review --store DIR --agency SLUG --ids ID[,ID...] --set checked|disputed --reviewer NAME [--date D] [--note T]`):
the only way to change `status` after import. A reviewer equal to the fact's `added_by` (case and spaces ignored), an unknown or
duplicate id, a date after today or before `added_at`, or any `--check` error refuses the whole call. Each change appends a
row to the private `<store>/agencies/<slug>/FACT-REVIEWS.csv` (v2: `fact_id,from_status,to_status,reviewer,reviewed_at,note,evidence_fp`,
mode 0600); a fact already at the target is `unchanged` and not logged. `--check` validates the log and names a fact whose
FACTS status differs from its last logged `to_status` (a hand edit). Ids and counts only; the note is never printed.

**FACT-REVIEWS v2** binds each row to the evidence it reviewed: a row is *bound* to the FACTS row with the same `fact_id` and
`evidence_fp`, and the status of a row is the last `to_status` of its bound chain. A row whose evidence changed (for example a new
quote under the same id) or was removed is *unbound* history: kept verbatim, format-checked only, never rebound and never conferring
status. A status other than `unreviewed` with no bound chain is a raw edit, not a review (`overlay_errors`; `--check` still accepts
a manually imported store). v1 logs stay readable; `review` upgrades a v1 log that passes the v1 check, stamping each row with the
fp of the current row of its `fact_id`, in the same write as the new rows. An inconsistent v1 log is refused and never upgraded.

**Review history** (`reviews --store DIR --agency SLUG [--fact ID] [--since YYYY-MM-DD]`, read-only): after the same full check as
`review`, one `reviews | slug | fact_id | from -> to | reviewed_at` line per log row (file order, `--since` inclusive), then counts
and `reversals` (facts changed into both checked and disputed). No log: zero counts. Never a reviewer, note, value or quote.

**Unreadable pages** (`unreadable --store DIR --out FILE.csv [--agency SLUG]`): writes a new private 0600 CSV `agency,source_sha256,page,fact_rows` (distinct pages with an `UNREADABLE` row; header only when none) outside the repo and `DIR/agencies`; stdout counts agencies checked, pages, documents and rows only.

## Store check

`python3 tools/flock_store_check.py STORE [--backups DIR --max-backup-age-days N] [--today YYYY-MM-DD]` runs the facts,
law/policy, requests, contact, watch list and publish checkers in-process, then `slugs` (a ROSTER slug or WATCHLIST agency
with no agency folder is an error; a folder in no ROSTER is a note; linked folders are named, never followed) and `backup`
(the newest `DIR/*.tar` whose first member is a `flock-store` manifest, by `created_at`; hashes are not re-verified, use
`flock_backup.py verify`). One `<section> | ` block each, at most 20 error lines; exit 0 only with no errors. `--json` prints the same lines as one `flock-store-check-v1` document; `--json-out NEWFILE` also writes it to a new 0600 file outside STORE, backups and the repo, its only write.
`--section NAME` (repeatable, no repeats) runs only those checks, in the order above, and the summary reads `sections=K of 8 (<names>)`; `--json` then lists only those sections plus `"selected"`.
An agency folder with no `CONTACT.csv` adds nothing to `contacts`; with none in any folder it reads `contacts | none`.

## Starting a new store

`python3 tools/flock_store_init.py STORE --agency SLUG [--agency SLUG ...]` creates an empty private store: `STORE/LAW.csv`
and, per agency, `agencies/<slug>/FACTS.csv` (v2) and `POLICY.csv`, header rows only (folders 0700, files 0600). STORE
must be outside the repository, missing or an empty folder, not a link, with an existing parent; slugs match
`[a-z0-9]+(-[a-z0-9]+)*`, at most 64 characters, no repeats. Every refusal is named before any write and nothing is
written (exit 1); the tree is built in `STORE.tmp-<pid>` and renamed into place. Stdout names slugs, never the path. The
store check passes on the result; import facts with `flock_facts.py import` and back it up with `flock_backup.py`.
`flock_store_init.py add STORE --agency SLUG [...]` adds empty `agencies/<slug>/` folders to an existing store (each built as `.<slug>.tmp-<pid>`, renamed in); every existing file keeps its bytes and mode. A slug already there (folder, file or link) or a store check exit 2 refuses it, nothing written; check errors are only counted (`errors=E`). A bare `STORE --agency …` is `init`.

## Report text state

`tools/flock_report.py` reads an optional `review.text_state_file` (absolute path outside the repository to the extraction run's
`run-outputs/<run>/text-state.json`, schema `records-text-state-v1`). Each cited record that is not `text_reviewed` there is
labelled on its bullet, its Sources line (and so in `region`) and as `text_state` in `SOURCES.json`: `(OCR text, not
visually reviewed)`, `(text state: <words>)`, or `(text state not recorded)` when the file does not list it. A missing,
linked, oversize or invalid file is a `config:` error and nothing is written. The label never changes which findings are
included. Operators should point live report configs at the latest text-state file; without the key a report makes no
claim about text state.

## Store write lock

`import` (not `--dry-run`) and `review` take an exclusive `fcntl.flock` on the store folder itself (a read-only descriptor, no lock file, so no listing, backup or DAG output snapshot ever sees one). They wait up to 30 s, then print `locked: fact store busy` and exit 1 with the store untouched; a refused import still creates no store. `--dry-run`, `--check`, `status`, `coverage`, `quote-check`, `stale`, `reviews` and `unreadable` take no lock. `evidence_fp(row)` (`E-` + 16 hex over every reviewed column but `status`) and `projection_sha256(path)` (hash of the file's rows with every status `unreviewed`; equals the bytes an unreviewed import wrote) are the shared helpers later units use.

## Crash recovery (review transaction)

`review` writes FACTS.csv and FACT-REVIEWS.csv as one journaled transaction: both new files are staged (`.FACTS.csv.txn`, `.FACT-REVIEWS.csv.txn`, fsynced), then `.FACTS-TXN.json` (`flock-facts-txn-v1`, old and new sha256 of each file) is committed by an atomic replace, then the log and FACTS are replaced and the journal removed. A crash before the commit leaves the old pair (the next writer unlinks the leftover temps); a crash after it is rolled forward exactly once by `recover(store, slug)`, which every `review` and every non-dry-run `import` runs under the exclusive lock before reading the store. A journal whose hashes fit neither file prints `REVIEW_TXN_CORRUPT` (exit 1) and touches nothing. `read_reviewed(store, slug)` is the packet readers' view under the shared lock: `REVIEW_TXN_PENDING` for a journal, `REVIEW_STATE_INVALID` for a failed check or a raw status edit; it never writes or recovers. `import --dry-run` stays lock-free, recovery-free and write-free.

## Reconcile (fresh projection + review overlay)

`flock_facts.py reconcile --store ACTIVE --from STAGING --agency SLUG --superseded DIR` installs the unreviewed import in `STAGING` over `ACTIVE` without losing bound reviews. Under the exclusive lock it recovers a pending journal, validates the log, copies the current FACTS.csv and FACT-REVIEWS.csv (those present) to `DIR/<UTC>[-n]/` (never deleted), and writes the merged FACTS.csv and log in one journaled transaction. A row's status is the last `to_status` of the log chain bound to its (fact_id, evidence_fp), else `unreviewed`. Output: `reconcile | <slug> | kept=K reset=R new=N removed=M raw_dropped=D` (kept: carries a bound decision; reset: had a decision but its evidence changed; raw_dropped: active status with no bound chain). A staging file that is invalid or has any status but `unreviewed` prints `RECONCILE_STAGING_INVALID`; a malformed or linked log, an inconsistent v1 log or an invalid active FACTS.csv prints `REVIEW_STATE_INVALID`; both exit 1 with nothing written. A consistent v1 log is upgraded to v2 in the same transaction; a missing active FACTS.csv is simply installed (no log, nothing superseded).

| case | result |
| --- | --- |
| unchanged evidence, bound checked/disputed | status, log and marks kept |
| changed evidence under the same fact_id | `unreviewed`; old log rows stay as unbound history |
| added evidence | `unreviewed` |
| removed evidence | row gone; its log rows stay as unbound history |
| evidence reverts to an earlier fingerprint | regains the last decision for exactly that (fact_id, fp) |
| dispute, or reversal checked ↔ disputed | last `to_status` of the bound chain; full chain kept |
| raw status edit (no bound chain) | not a review; written as the log-derived status (`raw_dropped`) |
| log absent | every row `unreviewed`, no log created |
| log malformed or a link, inconsistent v1 log | `REVIEW_STATE_INVALID`, nothing written |
| concurrent review | serialized by the lock; the log is read at merge time |
