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
| records-runner.yml | mail_delta, runner, export_proof, runner_wp1_overlay, runner_reliability |
| records-extraction.yml | extraction_routes, extraction_ledger, extraction_validation, ocr_closed_cas, workbook_policy, extraction_inherited_budget, reconciliation_crash_composition |
| records-detectors.yml | detectors, detector_persistence |
| records-queue.yml | queue_runtime, queue, queue_scope, queue_integration |
| records-imap.yml | imap_intake, imap_config_sources, imap_failure_codes |
| records-native-msg.yml | mail_wire_intake, native_completion, rfc822_consumer; native dependency contract remains required |
| records-controller-kernel-faults.yml | controller_kernel_faults; actual flock/subprocess environment retained |
| records-portal.yml | nine portal modules listed in SOURCE_OWNERS, including seven top-level test_records_portal modules |
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

Python 3.12 only. Offline engine checks runs three deterministic disjoint shards
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
all three shards. `--list` prints the exact planned module set without running it.
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

## Evidence to record for a candidate

Record repository, exact head SHA, each Actions run and check ID, conclusion,
actual job duration, aggregate runner wall time, and whether billing was available.
Observe at least the broad/unknown-path PR case; source-specific selection can
also be inspected with the selector's JSON output. A passing candidate still
requires review; this map does not provide legal or privacy acceptance, authorize
publication, or prove a live runtime was upgraded.
