# Records CI ownership and tiers

This is an execution inventory, not semantic acceptance or private-export clearance.
Every tracked `tests/**/test_*.py` module is assigned one source-test owner by
`tools/ci_affected.py`. New modules default to Offline engine checks; they cannot
silently disappear from the suite. Print the complete current module inventory:

```sh
python3 -B tools/ci_affected.py --tier full --inventory
python3 -B tools/ci_affected.py --tier pr --inventory
python3 -B tools/ci_affected.py --self-test
```

## Single source-test owners

| Owner/workflow | Test set |
|---|---|
| records.yml / offline | All modules not assigned below; full discovery is not repeated in intake, release or shell workflows |
| records-wp5.yml | catalog_stage, catalog_stage_drift, ledger_catalog, pipeline_composition; existing 66+4 launcher and source-origin guards |
| records-recovery.yml | recovery, publication_review_bridge; existing exact-61 zero-skip composition launcher |
| records-queue.yml runner step | mail_delta, runner, export_proof, runner_wp1_overlay, runner_reliability |
| records-extraction.yml | extraction_routes, extraction_ledger, extraction_validation, ocr_closed_cas, workbook_policy, extraction_inherited_budget, reconciliation_crash_composition |
| records-queue.yml detectors step | detectors, detector_persistence |
| records-queue.yml queue step | queue_runtime, queue, queue_scope, queue_integration |
| records-imap.yml | imap_intake, imap_config_sources, imap_failure_codes |
| records-native-msg.yml | mail_wire_intake, native_completion, rfc822_consumer; native dependency contract remains required |
| records-controller-kernel-faults.yml | controller_kernel_faults; actual flock/subprocess environment retained |
| records-queue.yml portal step | nine portal modules listed in SOURCE_OWNERS, including seven top-level test_records_portal modules |
| records-run.yml | sixteen runtime/publish/context/OCR modules listed in OWNERS; separate actual retirement_boundary launcher is not discovery |
| runner.yml | tests.runner.*, test_outbox, test_outbox_gateway, test_redact; fake-workspace smoke is a separate CLI contract |
| campaign-install.yml | test_campaign_install_resources, test_campaign_wheel_harness, test_cli, test_discovery, test_kit, test_law; public-resource parity and example build reproducibility |
| workers.yml (unchanged) | test_review_fixture plus Worker TypeScript/interoperability checks |
| offline.yml | Node tests/*.test.mjs only; no Python discovery or duplicate example build |

Names without a package prefix in records-owner rows mean
`tests.records.test_<name>`. The executable inventory is authoritative for exact
module names, not an independently maintained duplicate list.

The full-tier records integration owners retain their existing parser installs,
zero-skip checks, exact composition counts, pinned dependency identities and
source-origin validation. PR execution delegates their ordinary source modules
to the affected shards, except WP5 and recovery, whose special launchers remain
their only owners. Their integration environments are not replaced by a green
ordinary unittest run. Runner, campaign-install and Workers own their source
modules on both tiers and are excluded from offline selection.

## Fast tier: PR and merge group

Python 3.12 only. Offline engine checks runs five deterministic disjoint shards
of affected source tests. The required `offline (3.12)` check aggregates their
results and fails unless every shard succeeded. Pin guard and public-tree scan
remain explicit checks. The PR/merge-group `pin-guard` job also owns exactly one
`python -B -m campaign_tool.records scan-public --patterns-only` invocation,
without path filtering or affected-test dependencies: docs-only and unknown-path
changes receive the same strict-pattern gate. Release artifact steps remain
full-tier-only. The selector's unit checks use only synthetic graphs;
they do not assert the contents of other tests or workflows.

Selection follows static Python imports transitively. Dynamic CLI/import callers
are conservatively included for source changes. Shared ledger/runtime changes,
unknown paths, fixtures/build metadata, missing diffs, syntax-analysis failures,
empty selection and deleted/unmapped modules select broader coverage rather than
an empty successful suite. Very narrow selections broaden if needed to populate
all five shards. `--list` prints the exact planned module set without running it.
Shard packing is deterministic largest-source-first, not a claimed duration model.

WP5/recovery/runner source checks run for unknown paths too. Existing required
package/image check names still report on PRs, but artifact steps do not run.
There are no PR wheel builds, Docker image builds or rootless daemon rehearsals.
The Python rootless driver regressions remain ordinary affected source tests.
No self-hosted runner is introduced; changed workflows use ubuntu-24.04.

Target: under 10 elapsed minutes and approximately 30 aggregate runner-minutes.
Timeouts enforce a ten-minute bound per affected shard; actual PR observations,
not timeout settings or local timings, establish performance. Billing data remains
unknown unless returned by an authorized GitHub billing endpoint. Do not infer
billable usage from job wall time or claim pending/skipped checks are green tests.

## Full tier: main and nightly

Existing workflows also run nightly at 07:17 UTC. Full source coverage runs on
Python 3.11–3.13 through the offline owner plus the existing integration owners;
the offline owner runs it as two disjoint `full (<python>, <shard>)` jobs per
Python, and each `offline (<python>)` check fails unless every full shard
succeeded (one 4-CPU job took 20–35 minutes and hit its 35-minute timeout);
native MSG, runner and campaign installation keep their existing 3.12-only
specialized environments. Container builds, source-bound records wheel builds,
installed-wheel composition and reproducible example output remain full-tier
contracts. Installed-wheel composition deliberately reuses source-test modules
against an installed artifact outside the checkout: it verifies a distinct import
and provenance boundary, not another copy of source discovery. Rootless OCR swap
rehearsals remain synthetic disposable-container work, never a live host swap.

Public-tree, pattern, Node secret and detect-secrets scans are security gates,
not substitutes for source tests. Existing per-workflow security scans remain
in place, including their deliberately separate scanner implementations.

## Bounded source-executor candidate (AUDINV-FIX-144)

`affected` and `full` use `ci_affected.py --jobs 2` and
`strategy.max-parallel: 5` / `6` (CI-DF-FANOUT-1, 2026-11-16; was 1, which queued
PR legs ~50 min). Each leg is its own hosted VM, so at most two source-test
**executor children per leg** are live at once; AUDINV-FIX-144 bounds per-VM I/O
and does not require serial legs. All five PR shards and all two-shard ×
three-Python full entries remain and now run in parallel. Parent SERIAL tests run only
after children finish. Nested subprocesses deliberately launched by individual
tests are outside this executor-child bound. The selector's general jobs API,
workflow concurrency identity, failure aggregation and security scans are unchanged.

This is not a host-global or repository-global semaphore: other workflows and
separate PR/main runs can overlap. There are no runner or repository-variable
changes. Sequential matrices can regress duration, especially with the existing
full-tier timeout history. Synthetic acceptance is not production contention
closure, a CI speedup, legal/semantic acceptance or authority for live processing.
The merged runtime-built Basic Auth fixture and credential scanning remain intact.

### Isolated comparison receipt

Checked main: `8c10b9fe186d54c233d89de4862eb4383a83e440` (newer than the spec's
checked head; the two uncapped commands and unbounded matrix concurrency remain).
The unchanged selector source SHA-256 is
`4a1605a05299ac8663a57151d1772aaedec2191e88c50e506585b2c0cceb4b48`.
The historical candidate workflow diff consists only of two `--jobs 0` → `--jobs 2`
arguments and two `max-parallel: 1` keys. The later CI-DF-FANOUT-1 configuration
changes those limits to 5 and 6, as described above; it is not the workflow bound
to this historical measurement. Historical candidate file SHA-256 values:

- `.github/workflows/records.yml`: `37affc5e9ab64700b26c659bee4ca68c90035847f07150b5e648ff5f98ad7ded`.
- `tests/test_ci_contention_cap.py`: `74e1c1f4220583e9c74eeef95c054d958289f291bb113015a104b27335a2a624`.

The initial measurement below used test source
`1e57f38ccc3e23f6e59d9c22a3f96c390fa3711db1008f184b8ca03c771761c1`;
the final candidate adds only printing ordinary control rows to that source.
Source and candidate hashes bind the receipt without a self-referential document
or commit hash. All selected acceptance modules are rerun on the final candidate.

Reproduction from the checkout, with the provisioned pinned-dependency interpreter
(`repo-python` means that environment's Python 3.12.3, importing this checkout):

```sh
memcap -- repo-python -B -W error::ResourceWarning -m unittest tests.test_ci_contention_cap tests.test_ci_affected_parallel tests.test_ci_affected_timing tests.test_ci_affected_packing -q
```

The harness invokes the same `run_shard` with jobs 0 (CPU discovery fixed at four)
and jobs 2. Four deterministic `ci_contention_fixture.test_0` through `test_3`
modules each execute `Case.test_a` and `Case.test_b`. Every test writes, fsyncs,
reads and SHA-256-verifies a 4 MiB payload under its isolated scratch directory.
The payload hash is
`2b07811057df887086f06a67edc6ebf911de8b6741156e7a2eb1416a4b8b1b2e`;
each green module's source hash is
`f39e70c599edc151bc33dfe528c217348488665cef24a589f75a74a38b673bd7`.
A pipe barrier releases children only after launch fan-out is complete, making
live-PID observation deterministic without sleep-as-workload. Timings include
launch/barrier/interpreter overhead. Temporary files are isolated from the
repository and removed; no campaign input or network is used. `fsync` does not
prove physical disk throughput (scratch may be memory-backed or cached).

Initial local run: Python 3.12.3, GCC 13.3.0;
Linux-7.0.0-42-generic-x86_64-with-glibc2.39. Monotonic elapsed seconds, in actual
alternating execution order:

| Order | jobs | Elapsed seconds | Peak live executor children | Expected/ran | Exit | failures/errors/skips |
|---|---:|---:|---:|---|---:|---|
| 1 | 0 | 0.077329 | 4 | 8/8 | 0 | 0/0/0 |
| 2 | 2 | 0.096431 | 2 | 8/8 | 0 | 0/0/0 |
| 3 | 2 | 0.086892 | 2 | 8/8 | 0 | 0/0/0 |
| 4 | 0 | 0.078853 | 4 | 8/8 | 0 | 0/0/0 |
| 5 | 0 | 0.092341 | 4 | 8/8 | 0 | 0/0/0 |
| 6 | 2 | 0.092776 | 2 | 8/8 | 0 | 0/0/0 |

Medians: jobs 0 **0.078853 s**, jobs 2 **0.092776 s**. The candidate was slower
in this synthetic sample; no minimum speed improvement is required or inferred.
Every ordinary comparison asserts identical module IDs, module/payload hashes,
individual test IDs, expected/ran counts, failure/error/skip counts and exit status.
The six-run receipt is printed as `CI_CONTENTION_COMPARISON` on every rerun.

Controls cover CPU discovery 1/2/4/16 and 1/2/4 modules, empty-suite refusal,
real assertion failure, import failure, skip, ResourceWarning promoted to error,
and SIGKILL. Both settings preserve ordinary pass/fail/skip outcomes. Import
failure yields a counted loader error, not execution of the absent module's I/O
tests. Workload event IDs identify tests entered, not proof they completed.
SIGKILL remains red: the initial baseline completed 6/8 versus candidate 4/8;
different chunk grouping loses different counts. This is explicitly **not equal
completed coverage**. Both emit missing-count errors and exit 1. Forged stdout,
missing/malformed record-channel verdicts and child TMPDIR cleanup retain the
existing parallel regressions. SERIAL ordering and isolated per-child roots are
also checked. The unchanged shell aggregator is exercised with success, failure,
cancelled, skipped, expired, stale and empty results; only success passes.

Initial acceptance output: `Ran 30 tests in 4.042s`, `OK`, exit 0. The receipt
is candidate-only; no hosted CI or full live pipeline was executed for measurement.

## Evidence to record for a candidate

Record repository, exact head SHA, each Actions run and check ID, conclusion,
actual job duration, aggregate runner wall time, and whether billing was available.
Observe at least the broad/unknown-path PR case; source-specific selection can
also be inspected with the selector's JSON output. A passing candidate still
requires review; this map does not provide legal or privacy acceptance, authorize
publication, or prove a live runtime was upgraded.
