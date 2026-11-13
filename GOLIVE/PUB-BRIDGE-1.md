# PUB-BRIDGE-1 — GAP found; offline bridge added

## Verify-first result

**GAP**, not VERIFIED-EXISTS. Audited fresh upstream commits:

- Engine `41b642d8f05089c8597e0b3f43b3d960a989c586`:
  `campaign_tool/records/public_packets.py::write_change` emits
  `records-site-change-v1` under `<project>/site-changes/<packet>-r<revision>.json`.
  Its existing CLI had no `export-changes` action.
- Site `57e5cda21dc6dbe6063cb7ac8d965305e88f2e3c`:
  `scripts/render-records-story.mjs` consumes location/thread/story data;
  `records-hosting.mjs` validates hosted URLs;
  `publication-list.mjs` consumes a manifest/source CSV and private upload staging;
  `publication-verify.mjs` consumes upload CSV/receipts. None consumes the change
  schema. `git grep -n -e records-site-change -e export-changes -e CHANGE_SCHEMA`
  found no site consumer. There was no confirmed container-to-host handoff.

## One implementation PR, owned by the engine

The engine owns the producer contract, the new CLI export action, and the portable
site-staging script/renderer. No second implementation PR is needed here. The
operator later opens the **separate content PR** in deflockslo-site.

1. `campaign_tool/records/packet_changes.py` reads a host-visible project **without
   writing the project or creating its synchronization file**. It only selects
   current `written` journal rows, checks their recorded change-file SHA-256 and
   schema/identity, and copies those exact JSON files to a private external DIR.
   No records, journal, approval signatures or configuration files are copied.
   No recursion through record storage or container commands exist.
2. Identity is packet id + revision. Identical exports are skipped; differing bytes
   at the same identity refuse rather than overwrite. All collisions are checked
   before new copies. The active `changes-manifest.json` lists only current written
   revisions, so orphan files and superseded exports are not staged. Re-running
   after interruption resumes. The manifest is a snapshot, not a signature or a
   live revocation service. Use a fresh export/revalidation before content review.
3. `tools/stage_packet_changes.py` requires a clean, disposable site git checkout.
   It verifies manifest hashes, writes a public-only projection to
   `content/updates/<packet>.json`, installs `scripts/render-packet-updates.mjs`,
   and adds its build call immediately after the existing library render.
   The producer's suggested target is validated but not used as a filesystem path.
   A stable packet filename allows a later revision to replace that packet only;
   same-revision changes and revision downgrades refuse.
4. The site renderer inserts escaped plain text, citations and document hashes in
   `/records.html`. It never turns citation text into links or offers record
   downloads. Signer identity, campaign configuration, input target and free-form
   internal notes are excluded from the site's JSON and HTML.
5. Only `/records.html` gets a new exact intentional-asset receipt; the original
   baseline and all other asset receipts are preserved. The script first requires
   `npm run check` green on the unmodified site base, then runs it again after
   staging, followed by `npm run scan:secrets`. Failure exits nonzero and leaves an
   uncommitted draft for diagnosis. It never adds/commits/pushes files, opens a PR,
   uploads originals, sends, merges or deploys.

## Operator commands (not executed on real records here)

After separately obtaining an approved, read-only host view of the project:

```sh
records public-packets export-changes --project /approved/read-only/project --out /private/packet-export
records public-packets export-changes --project /approved/read-only/project --out /private/packet-export
python3 tools/stage_packet_changes.py --changes /private/packet-export --site /disposable/deflockslo-site
```

Use the reviewed engine version for `records`; do not run an old container CLI.
The source path is the host-visible mount already provided by the attended
operator, **not** a new mount created by this script. If none is available, stop
for the existing runtime operator; this task grants no container/mount permission.
Keep the export outside git, mode 0700 (files 0600), and on this host. It contains
approval principal metadata; never commit or export the raw files off-box.

Review the site diff: public JSON, renderer, build hook, generated records page,
and only the records-page asset receipt. Check citations and redaction/legal
acceptance separately. The operator opens that content PR with exact-head checks
and model review; **G-07 remains required for merge/deploy of the content batch**.
No merge, production approval or publication is performed by this bridge task.
Document packets remain metadata/text only: S17's document proposal/redaction
work and the site's independently approved upload/verification path still own
actual downloadable files. Existing site packets absent from a new export are
not deleted: withdrawals/corrections require an explicit reviewed content change.

## Real acceptance evidence

Run from the engine checkout, against a clean disposable site clone at the
last CI-green site commit `fbfae352e0cf49a77536bfa8de3acc28568e69f0`:

```sh
python3 -B -W error::ResourceWarning -m unittest tests.records.test_packet_changes tests.records.test_public_packets -v
python3 tests/records/prove_packet_bridge.py --site /disposable/deflockslo-site
```

Observed output:

```text
Ran 23 tests
OK
{"active": 1, "existing": 0, "exported": 1}
{"active": 1, "existing": 1, "exported": 0}
PASS: 50 public assets and _headers match the captured baseline or 12 exact documented transitions.
{"checked": 333, "existingGaps": [], "newGaps": []}
# tests 420
# pass 420
# fail 0
PASS: source files contain no detected credential signatures or forbidden storage paths. Names/placeholders require human review; this is not a guarantee against every secret format.
{"prepared": 1, "publication": "not_performed", "site_check": "passed"}
PASS: fixture visible in built /records.html; no signer or record bytes; no PR or deployment
```

The proof stubs the signature verifier **only while constructing a synthetic
approved fixture**, not in production code. It runs the candidate records CLI,
exporter, staging script, real site build/check and output inspection. This is
structural fixture evidence, not G's approval or human semantic acceptance.

**Current-main limitation:** site `57e5cda21dc6dbe6063cb7ac8d965305e88f2e3c`
fails `npm run check` with `Undocumented baseline differences: /app.js` both in
an untouched control clone and with the bridge. GitHub's same-head check also
fails: <https://github.com/Grahammmm/deflockslo-site/actions/runs/43494587465>.
The bridge did not modify the app receipt to hide this unrelated regression.
Site content must be rechecked on repaired current main before its PR can ship.
`npm ci` additionally reported four existing high-severity dependency advisories;
no dependency updates or forced fixes were attempted in this scope.

## Rollback and boundaries

Nothing live changed. Discard the disposable draft, or have the operator close
its own unmerged content PR. Do not close another author's PR. No live records,
mail, credentials, timers, runtime wrapper, LIVE.json, container, ledger or
canonical store was read/changed by the fixture proof. An export hash or green
machine check never replaces packet approval, redaction or legal review.
